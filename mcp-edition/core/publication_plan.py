"""Pure publication/rollback candidates. No reader imports, HTTP or disk access.

Callbacks: normalize_text(bytes)->strictly decoded LF text;
normalize_highlight(dict)->the OLD compatibility view; text_position(text, u16)
returns (paragraph, column_u16, total_u16, python_index); chapter_at(text,u16,
raw_revision)->a label. Nothing here authenticates a caller or commits a file.
Unknown JSON fields stay opaque. Only the target book's known positions and
missing stable IDs are changed. Unknown extension coordinate semantics require
an upper-layer policy; this module does not claim to migrate those extensions.
"""
import copy
import hashlib
import json
import re

import book_drafts as drafts
import chapter_index
import navigation
from progress_catalog import _integer, verified_position
import publication_mapping as kernel


class CandidateError(ValueError):
    def __init__(self, code, message, record_id=None):
        super().__init__(message)
        self.issue = dict(code=code, message=message, record_id=record_id,
                          operation_id=None, operation_index=None)


def _require(condition, code, message, record_id=None):
    if not condition:
        raise CandidateError(code, message, record_id)


def _pairs(rows):
    out = {}
    for key, value in rows:
        _require(key not in out, 'duplicate_json_key', 'Duplicate JSON key is ambiguous.')
        out[key] = value
    return out


def _constant(value):
    raise CandidateError('invalid_json_number', 'Nonfinite JSON number is invalid.')


def _store(value, *, books=True):
    if value is None:
        return {}
    if isinstance(value, (bytes, bytearray)):
        try:
            value = json.loads(bytes(value).decode('utf-8-sig'), object_pairs_hook=_pairs,
                               parse_constant=_constant)
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, CandidateError):
                raise
            raise CandidateError('invalid_json', 'Store is not strict UTF-8 JSON.') from exc
    _require(isinstance(value, dict), 'invalid_store', 'Store must be a JSON object.')
    if books:
        _require(isinstance(value.get('books', {}), dict), 'invalid_books', 'books must be an object.')
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError) as exc:
        raise CandidateError('invalid_json', 'Store contains invalid JSON values or Unicode.') from exc
    return copy.deepcopy(value)


def _raw_revision(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def _text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _position(text, offset, locate, record_id):
    _require(type(offset) is int, 'invalid_offset', 'Offset must be a strict integer.', record_id)
    position = locate(text, offset)
    _require(isinstance(position, (tuple, list)) and len(position) == 4,
             'invalid_offset', 'Position is outside text or splits a surrogate pair.', record_id)
    return position


def _minimal_quote(text, position):
    return text[position[3]:position[3] + 1]


def _anchor(text, position):
    return re.sub(r'\s+', ' ', text[position[3]:position[3] + 40])


def _label(chapter_at, text, offset, revision, record_id):
    value = chapter_at(text, offset, revision)
    _require(isinstance(value, str), 'invalid_chapter_view', 'Chapter provider must return a string.', record_id)
    return value


def _progress_rows(book):
    _require(isinstance(book, dict), 'invalid_progress_book', 'Progress book must be an object.')
    flat = {'offset', 'percent', 'chars', 'chapter', 'anchor', 'tts_offset', 'updated',
            'revision', 'book_revision', 'paragraph_idx', 'char_offset'}
    if any(k in book and not isinstance(book[k], dict) for k in flat):
        _require(not any(k in book and isinstance(book[k], dict) for k in flat)
                 and not any(isinstance(v, dict) and flat.intersection(v) for v in book.values()),
                 'ambiguous_progress_shape', 'Mixed flat/member progress cannot be interpreted safely.')
        return [('user', book)]  # retain the original flat shape, including extra fields
    rows = []
    for ident, record in book.items():
        _require(isinstance(ident, str) and bool(ident) and isinstance(record, dict),
                 'invalid_progress_identity', 'Progress identity keys and records must remain unambiguous.')
        rows.append((ident, record))
    return rows


def _verify_progress(record, text, revision, locate, record_id):
    offset = _integer(record.get('offset'))
    _require(offset is not None, 'missing_progress_offset', 'Nonempty progress needs a valid saved offset.', record_id)
    position = _position(text, offset, locate, record_id)
    for field in ('total_u16', 'total_units'):
        if field in record:
            _require(_integer(record[field]) == position[2], 'progress_length_mismatch', 'Stored total differs from old UTF-16 length.', record_id)
    # Older anchors used either 40 Python characters or at most 40 UTF-16 units.
    # Adapt only the validation copy; never overwrite raw evidence during reads.
    check = copy.deepcopy(record)
    anchor = record.get('anchor')
    if isinstance(anchor, str) and anchor:
        norm = lambda value: re.sub(r'\s+', ' ', value).strip()
        candidates = (_anchor(text, position), navigation.quote_at(text.encode('utf-16-le'), offset))
        _require(any(norm(anchor) == norm(item) for item in candidates),
                 'old_progress_anchor_mismatch', 'Old anchor matches neither supported 40-character convention.', record_id)
        check['anchor'] = _anchor(text, position)
    evidence = verified_position(check, text, revision, locate, lambda item, content, version: item)
    _require(evidence is not None, 'unverified_progress', 'Saved-position evidence is incomplete or inconsistent.', record_id)
    saved_revision = _integer(record.get('revision', 0))
    _require(saved_revision is not None and saved_revision >= 0,
             'invalid_progress_revision', 'Progress revision must be a nonnegative integer.', record_id)
    return offset, position, saved_revision


def _heading_line(text, position):
    start = position[3]
    newline = text.find('\n', start)
    return text[start:] if newline < 0 else text[start:newline + 1]


def _chapter_heading_issues(mapping, items, old_items, locate):
    """An unchanged automatic label needs its whole old heading to survive.

Known manual aliases describe an explicitly selected target position, so their
labels need not equal body text. Unknown/legacy provenance is never guessed.
"""
    if not mapping.operations:
        return []
    original = {row['id']: row for row in navigation.automatic(mapping.source_text, mapping.base_revision)['items']}
    original.update({row['id']: row for row in old_items})
    issues = []
    for item in items:
        if item.get('basis') in ('quick_selection', 'administrator_verified'):
            continue
        ident = 'chapter:' + item['id']
        issue = dict(code='chapter_origin_unverified', message='Chapter needs explicit manual verification; its old heading cannot be proved.',
                     record_id=ident, operation_id=None, operation_index=None)
        old = original.get(item['id'])
        if old is None or old.get('title') != item['title']:
            issues.append(issue); continue
        position = _position(mapping.source_text, old['offset'], locate, ident)
        heading = _heading_line(mapping.source_text, position)
        if not heading or heading.strip() != old['title'].strip():
            issues.append(issue); continue
        result = kernel.map_range(mapping, old['offset'], old['offset'] + drafts.units(heading), heading, record_id=ident)
        if not result['ok']:
            for problem in result['issues']:
                issues.append(dict(problem, code='chapter_heading_changed', cause=problem['code'],
                                   message='The full old heading was edited; verify or rename this chapter explicitly.'))
            continue
        mapped = result['target']['start']
        target_position = _position(mapping.target_text, mapped, locate, ident)
        if mapped != item['offset'] or _heading_line(mapping.target_text, target_position).strip() != item['title'].strip():
            issues.append(dict(issue, code='chapter_heading_changed',
                               message='Chapter moved or its final heading line changed; explicit verification is required.'))
    return issues


def build_candidate(name, source_bytes, draft, notes_raw, progress_raw, *,
                    normalize_text, normalize_highlight, text_position, chapter_at,
                    navigation_raw=None, target_bytes=None, operations_override=None):
    """Return a complete candidate or issues with every new_* field set to None.

Normal mode binds draft.base_revision to source bytes and uses final draft.items.
Override mode strictly replays caller-supplied inverse edits against current bytes;
draft/navigation_raw are target historical templates, not source version evidence.
target_bytes, when supplied, must decode exactly to the replay result and retain
their original encoding/newlines. A directory-only publish retains source bytes.
new_draft is rebased; a coordinator may instead restore historical operations with
a separately monotonic draft revision during rollback.
"""
    issues = []
    summary = dict(name=name, highlights=0, progress_records=0, empty_progress=0,
                   frozen_highlight_fields=0, frozen_annotation_ids=0)
    mapping = None

    def failed():
        return dict(ok=False, issues=issues, summary=summary, mapping=mapping,
                    new_book_bytes=None, new_notes=None, new_progress=None,
                    new_navigation=None, new_draft=None)

    try:
        _require(isinstance(name, str) and bool(name), 'invalid_name', 'Book name is required.')
        _require(isinstance(source_bytes, bytes) and len(source_bytes) <= drafts.MAX_SOURCE_BYTES,
                 'source_byte_limit', 'Source must be bytes of at most 8 MiB.')
        source_text = normalize_text(source_bytes)
        _require(isinstance(source_text, str) and '\r' not in source_text,
                 'invalid_source_text', 'Strict decoder must return LF-normalized text.')
        source_revision = _raw_revision(source_bytes)
        _require(isinstance(draft, dict) and draft.get('name') == name,
                 'invalid_draft', 'Draft belongs to a different book or is missing.')
        _require(type(draft.get('revision')) is int and draft['revision'] >= 1,
                 'invalid_draft_revision', 'Draft revision must be a positive integer.')
        _require(isinstance(draft.get('chapter_issues'), list) and not draft['chapter_issues'],
                 'unresolved_chapters', 'All draft chapter issues must be resolved first.')
        if 'proposal_receipts' in draft:
            _require(isinstance(draft['proposal_receipts'], dict), 'invalid_receipts', 'Approval receipts must be preserved as an object.')
        override = operations_override is not None
        operations = operations_override if override else draft.get('operations')
        built = kernel.build_mapping(source_text, operations,
                    base_revision=source_revision if override else draft.get('base_revision'),
                    current_revision=source_revision, source_size_bytes=len(source_bytes))
        if not built['ok']:
            issues.extend(built['issues'])
            return failed()
        mapping = built['mapping']
        if target_bytes is not None:
            _require(isinstance(target_bytes, bytes) and len(target_bytes) <= drafts.MAX_SOURCE_BYTES,
                     'invalid_target_bytes', 'Historical target must be bytes of at most 8 MiB.')
            new_book_bytes = target_bytes
        elif not mapping.operations:
            new_book_bytes = source_bytes
        else:
            _require('\r' not in mapping.target_text, 'noncanonical_edit_newlines', 'Text edits must use LF before publication.')
            new_book_bytes = mapping.target_text.encode('utf-8')
        _require(normalize_text(new_book_bytes) == mapping.target_text,
                 'target_text_mismatch', 'Target bytes do not exactly represent replayed text.')
        new_revision = _raw_revision(new_book_bytes)
        old_hash, new_hash = _text_hash(source_text), _text_hash(mapping.target_text)
        notes, progress = _store(notes_raw), _store(progress_raw)
        nav_template = _store(navigation_raw, books=False) if navigation_raw is not None else {}
        _require(isinstance(nav_template, dict), 'invalid_navigation', 'Navigation must be an object.')
        if nav_template:
            _require(nav_template.get('name', name) == name and nav_template.get('schema_version') == navigation.SCHEMA,
                     'invalid_navigation', 'Navigation name/schema is inconsistent.')
            expected_nav_revision = new_revision if override else source_revision
            _require(nav_template.get('book_revision') == expected_nav_revision,
                     'navigation_version_mismatch', 'Navigation template does not match its bound text.')
            if not override:
                navigation.validate(nav_template.get('items'), source_text)
        items = nav_template.get('items') if override and navigation_raw is not None else draft.get('items')
        navigation.validate(items, mapping.target_text)  # validation only: retain all unknown item fields
        items = copy.deepcopy(items)
        if not override:
            # Earlier chapter editors can rebuild known fields. Carry opaque
            # metadata forward for surviving stable IDs, without restoring an
            # old parent/offset/quote that the draft intentionally changed.
            canonical = {'id', 'title', 'offset', 'parent_id', 'order', 'quote', 'basis'}
            previous_items = nav_template.get('items', [])
            _require(isinstance(previous_items, list) and all(isinstance(row, dict) for row in previous_items),
                     'invalid_navigation', 'Original navigation items must be objects.')
            by_id = {row.get('id'): row for row in previous_items}
            for row in items:
                for key, value in by_id.get(row['id'], {}).items():
                    if key not in canonical and key not in row:
                        row[key] = copy.deepcopy(value)
            issues.extend(_chapter_heading_issues(mapping, items, previous_items, text_position))
            if issues:
                return failed()
        cursor = draft.get('cursor_offset', 0)
        _position(mapping.target_text, cursor, text_position, 'draft-cursor')
        nav_revision = nav_template.get('revision', 0)
        _require(type(nav_revision) is int and nav_revision >= 0,
                 'invalid_navigation_revision', 'Navigation revision must be nonnegative.')
        new_navigation = dict(nav_template, schema_version=navigation.SCHEMA, name=name,
                              book_revision=new_revision, revision=nav_revision + 1,
                              items=copy.deepcopy(items))
        new_navigation.setdefault('source', 'publication')
        new_draft = copy.deepcopy(draft)
        new_draft.update(base_revision=new_revision, operations=[], revision=draft['revision'] + 1,
                         items=copy.deepcopy(items), chapter_issues=[])
        summary.update(source_revision=source_revision, target_revision=new_revision,
                       source_units=drafts.units(source_text), target_units=drafts.units(mapping.target_text),
                       operations=len(mapping.operations), directory_only=not mapping.operations)
    except CandidateError as exc:
        issues.append(exc.issue)
        return failed()
    except Exception as exc:
        issues.append(dict(code='candidate_validation_failed', message=type(exc).__name__,
                           record_id=None, operation_id=None, operation_index=None))
        return failed()

    target_book = notes.get('books', {}).get(name)
    if name in notes.get('books', {}):
        try:
            _require(isinstance(target_book, dict) and isinstance(target_book.get('highlights', []), list),
                     'invalid_notes_book', 'Target notes book/highlights have an invalid shape.')
            seen = set()
            for index, highlight in enumerate(target_book.get('highlights', [])):
                ident = 'highlight:' + str(index)
                try:
                    _require(isinstance(highlight, dict), 'invalid_highlight', 'Highlight must be an object.', ident)
                    view = normalize_highlight(copy.deepcopy(highlight))
                    hid = view['id']
                    ident = hid
                    _require(hid not in seen, 'duplicate_highlight_id', 'Highlight ID is duplicated.', ident)
                    seen.add(hid)
                    _require(not highlight.get('book_id') or highlight['book_id'] == name,
                             'highlight_book_mismatch', 'Historical highlight points to another book.', ident)
                    _require(not highlight.get('book_hash') or highlight['book_hash'] == old_hash,
                             'old_notes_hash_mismatch', 'Highlight hash is not the old normalized-text hash.', ident)
                    start, end = _integer(highlight.get('start')), _integer(highlight.get('end'))
                    result = kernel.map_range(mapping, start, end, highlight.get('text'), record_id=ident)
                    if not result['ok']:
                        issues.extend(result['issues']); continue
                    for field in ('id', 'highlight_id'):
                        if not highlight.get(field):
                            highlight[field] = hid
                            summary['frozen_highlight_fields'] += 1
                    comments = highlight.get('thread', [])
                    annotation_ids = set()
                    for comment, old_view in zip(comments, view['thread']):
                        _require(not comment.get('highlight_id') or comment['highlight_id'] == hid,
                                 'annotation_parent_mismatch', 'Comment points to a different highlight.', ident)
                        aid = old_view['annotation_id']
                        _require(aid not in annotation_ids, 'duplicate_annotation_id', 'Comment ID is duplicated in its thread.', ident)
                        annotation_ids.add(aid)
                        if not comment.get('annotation_id'):
                            comment['annotation_id'] = aid
                            summary['frozen_annotation_ids'] += 1
                    mapped = result['target']
                    label = _label(chapter_at, mapping.target_text, mapped['start'], new_revision, ident)
                    highlight.update(start=mapped['start'], end=mapped['end'], book_hash=new_hash,
                                     chapter=label, chapter_id=label or '正文')
                    summary['highlights'] += 1
                except CandidateError as exc:
                    issues.append(exc.issue)
                except Exception as exc:
                    issues.append(dict(code='invalid_highlight', message=type(exc).__name__, record_id=ident,
                                       operation_id=None, operation_index=None))
        except CandidateError as exc:
            issues.append(exc.issue)

    progress_book = progress.get('books', {}).get(name)
    if name in progress.get('books', {}):
        try:
            rows = _progress_rows(progress_book)
        except CandidateError as exc:
            issues.append(exc.issue); rows = []
        for ident, record in rows:
            rid = 'progress:' + ident
            if not record:
                summary['empty_progress'] += 1
                continue
            try:
                offset, old_position, revision = _verify_progress(record, source_text, source_revision, text_position, rid)
                result = kernel.map_point(mapping, offset, _minimal_quote(source_text, old_position), record_id=rid)
                if not result['ok']:
                    issues.extend(result['issues']); continue
                new_offset = result['target']['offset']
                position = _position(mapping.target_text, new_offset, text_position, rid)
                tts = None
                if 'tts_offset' in record:
                    tts = _integer(record['tts_offset'])
                    _require(tts is not None and tts >= 0, 'invalid_tts_offset', 'TTS offset must be nonnegative.', rid)
                    tts_position = _position(source_text, tts, text_position, rid)
                    if tts > 0:
                        tts_result = kernel.map_point(mapping, tts, _minimal_quote(source_text, tts_position), record_id=rid + ':tts')
                        if not tts_result['ok']:
                            issues.extend(tts_result['issues']); continue
                        tts = tts_result['target']['offset']
                label = _label(chapter_at, mapping.target_text, new_offset, new_revision, rid)
                indexed = chapter_index.chapter_at(chapter_index.index_for_text(mapping.target_text, new_revision), new_offset)
                record.update(offset=new_offset, chars=position[2], percent=new_offset / position[2] if position[2] else 0,
                              anchor=_anchor(mapping.target_text, position), paragraph_idx=position[0], char_offset=position[1],
                              chapter=label, chapter_index_version=chapter_index.INDEX_VERSION,
                              chapter_location_status=indexed['region'], book_revision=new_revision, revision=revision + 1)
                for field in ('total_u16', 'total_units'):
                    if field in record:
                        record[field] = position[2]
                if tts is not None and tts > 0:
                    record['tts_offset'] = tts
                elif 'tts_offset' in record and _integer(record['tts_offset']) > 0:
                    record['tts_offset'] = tts
                summary['progress_records'] += 1
            except CandidateError as exc:
                issues.append(exc.issue)
            except Exception as exc:
                issues.append(dict(code='invalid_progress_record', message=type(exc).__name__, record_id=rid,
                                   operation_id=None, operation_index=None))
    if issues:
        return failed()
    return dict(ok=True, issues=[], summary=summary, new_book_bytes=new_book_bytes,
                new_notes=notes, new_progress=progress, new_navigation=new_navigation,
                new_draft=new_draft, mapping=mapping)
