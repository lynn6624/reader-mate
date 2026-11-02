"""Pure, conservative UTF-16 mapping; no store, HTTP, authentication or commit.

The caller supplies text from a fixed snapshot and its actual byte count/version.
Versions are compared, never inferred from normalized text. Operations address
the successive drafts, in their original order. Text is never normalized here.
The caller must establish evidence for a legacy saved position before mapping it;
revision=0, identity membership and timestamps have no meaning in this module.
"""
from dataclasses import dataclass

import book_drafts as drafts


@dataclass(frozen=True)
class Operation:
    index: int
    id: str
    start: int
    end: int
    before: str
    after: str
    after_units: int


@dataclass(frozen=True)
class Mapping:
    source_text: str
    target_text: str
    base_revision: str
    operations: tuple


def _issue(code, message, record_id=None, operation=None, operation_index=None):
    return dict(code=code, message=message, record_id=record_id,
                operation_id=operation.id if isinstance(operation, Operation) else operation,
                operation_index=operation.index if isinstance(operation, Operation) else operation_index)


def _build_failure(code, message, operation=None, index=None):
    return dict(ok=False, mapping=None,
                issues=[_issue(code, message, operation=operation, operation_index=index)])


def build_mapping(text, operations, *, base_revision, current_revision, source_size_bytes):
    """Validate and replay a fixed draft; failure returns no partial mapping.

source_size_bytes is the byte count of the original file, not len(text). Limits
match PR5: 8 MiB source, 2M UTF-16 units, 200 operations and 4000 units per edit.
No operation dictionaries or caller-owned objects are retained in the plan.
"""
    if (not isinstance(base_revision, str) or not base_revision
            or not isinstance(current_revision, str) or not current_revision
            or base_revision != current_revision):
        return _build_failure('base_revision_mismatch', 'Draft base does not match the fixed source version.')
    if type(source_size_bytes) is not int or not 0 <= source_size_bytes <= drafts.MAX_SOURCE_BYTES:
        return _build_failure('source_byte_limit', 'Original source byte count is invalid or exceeds 8 MiB.')
    if not isinstance(text, str):
        return _build_failure('invalid_source', 'Source text must be a string.')
    try:
        raw = text.encode('utf-16-le')
    except UnicodeError:
        return _build_failure('invalid_source_unicode', 'Source contains an incomplete Unicode character.')
    if len(raw) // 2 > drafts.MAX_UNITS:
        return _build_failure('source_unit_limit', 'Source exceeds 2M UTF-16 units.')
    if not isinstance(operations, (list, tuple)) or len(operations) > drafts.MAX_EDITS:
        return _build_failure('operation_limit', 'Operations must be an ordered list of at most 200 edits.')
    checked, seen = [], set()
    for index, item in enumerate(operations):
        ident = item.get('id') if isinstance(item, dict) else None
        if not isinstance(ident, str) or not ident:
            return _build_failure('invalid_operation_id', 'Operation needs a nonempty stable ID.', index=index)
        if ident in seen:
            return _build_failure('duplicate_operation_id', 'Operation ID appears more than once.', ident, index)
        seen.add(ident)
        before, after = item.get('before'), item.get('after')
        if not isinstance(before, str) or not isinstance(after, str):
            return _build_failure('invalid_operation_text', 'before and after must be strings.', ident, index)
        try:
            before_units, after_units = drafts.units(before), drafts.units(after)
        except UnicodeError:
            return _build_failure('invalid_operation_unicode', 'Edit contains an incomplete Unicode character.', ident, index)
        if max(before_units, after_units) > drafts.MAX_SELECTION:
            return _build_failure('selection_limit', 'Edit exceeds 4000 UTF-16 units.', ident, index)
        start, end = item.get('start'), item.get('end')
        try:
            actual = drafts.span(raw, start, end)
        except drafts.DraftError:
            return _build_failure('invalid_operation_span', 'Edit is outside the current draft or splits a surrogate pair.', ident, index)
        if actual != before:
            return _build_failure('operation_before_mismatch', 'before does not exactly match this successive draft.', ident, index)
        if before == after:
            return _build_failure('noop_operation', 'A stored edit must change text.', ident, index)
        raw = raw[:start * 2] + after.encode('utf-16-le') + raw[end * 2:]
        if len(raw) // 2 > drafts.MAX_UNITS:
            return _build_failure('target_unit_limit', 'An intermediate draft exceeds 2M UTF-16 units.', ident, index)
        checked.append(Operation(index, ident, start, end, before, after, after_units))
    return dict(ok=True, mapping=Mapping(text, raw.decode('utf-16-le'), base_revision, tuple(checked)), issues=[])


def _result(kind, record_id, source, target=None, issue=None):
    return dict(ok=issue is None, kind=kind, record_id=record_id, source=source,
                target=target, issues=[] if issue is None else [issue])


def _check_quote(text, start, end, quote):
    if not isinstance(quote, str):
        return False
    try:
        return drafts.span(text.encode('utf-16-le'), start, end) == quote
    except (drafts.DraftError, UnicodeError):
        return False


def map_range(mapping, start, end, quote, *, record_id):
    """Map an unchanged, nonempty quoted [start,end) range.

Insertion at start is excluded by shifting right; insertion at end is excluded
by keeping the end left. Internal insertion and every replacement/deletion
overlap stop this record. No trimming, splitting or text searching is attempted.
"""
    source = dict(start=start, end=end, quote=quote)
    fail = lambda issue: _result('range', record_id, source, issue=issue)
    if not isinstance(record_id, str) or not record_id:
        return fail(_issue('invalid_record_id', 'Record needs a nonempty stable ID.', record_id))
    if not isinstance(mapping, Mapping):
        return fail(_issue('invalid_mapping', 'Build a valid mapping first.', record_id))
    if type(start) is not int or type(end) is not int or end <= start:
        return fail(_issue('invalid_range', 'Range must have strict integer bounds and positive length.', record_id))
    if not _check_quote(mapping.source_text, start, end, quote):
        return fail(_issue('old_quote_mismatch', 'Old coordinates/quote are invalid; no relocation was attempted.', record_id))
    left, right = start, end
    for op in mapping.operations:
        a, b = op.start, op.end
        delta = op.after_units - (b - a)
        if a == b:
            if a <= left:
                left += delta; right += delta
            elif a < right:
                return fail(_issue('insertion_inside_range', 'Insertion changes the inside of this reference.', record_id, op))
        elif b <= left:
            left += delta; right += delta
        elif a < right:
            scope = 'whole' if a <= left and b >= right else 'partial'
            action = 'deleted' if not op.after else 'replaced'
            issue = _issue('range_overlap', 'Reference is partially or wholly edited; preserve it for review.', record_id, op)
            issue.update(overlap=scope, action=action)
            return fail(issue)
    if not _check_quote(mapping.target_text, left, right, quote):
        return fail(_issue('new_quote_mismatch', 'Mapped quote is not identical in the target.', record_id))
    return _result('range', record_id, source, dict(start=left, end=right, quote=quote))


def map_point(mapping, offset, quote, *, record_id, affinity='right'):
    """Map a point plus an exact following quote, or an empty quote only at EOF.

The default right affinity follows old text after an insertion. A caller may
explicitly choose left affinity; its new quote must still match. A consumed
character at replacement start is unresolved, as is any point in [start,end).
EOF is a valid boundary, unlike a range of zero length. This checks mechanical
quote/coordinate consistency only and never certifies an actual legacy save.
"""
    source = dict(offset=offset, quote=quote)
    fail = lambda issue: _result('point', record_id, source, issue=issue)
    if not isinstance(record_id, str) or not record_id:
        return fail(_issue('invalid_record_id', 'Record needs a nonempty stable ID.', record_id))
    if not isinstance(mapping, Mapping):
        return fail(_issue('invalid_mapping', 'Build a valid mapping first.', record_id))
    if affinity not in ('left', 'right'):
        return fail(_issue('invalid_affinity', 'Point affinity must be left or right.', record_id))
    try:
        size = drafts.units(quote) if isinstance(quote, str) else -1
        total = drafts.units(mapping.source_text)
    except UnicodeError:
        size, total = -1, -1
    if (type(offset) is not int or size < 0
            or (not quote and offset != total)
            or not _check_quote(mapping.source_text, offset, offset + size, quote)):
        return fail(_issue('old_quote_mismatch', 'Old point/quote is invalid; an empty quote is valid only at EOF.', record_id))
    point, boundary_op = offset, None
    for op in mapping.operations:
        a, b = op.start, op.end
        delta = op.after_units - (b - a)
        if a == b:
            if point < a < point + size:
                return fail(_issue('point_quote_changed', 'Insertion changes the following quote.', record_id, op))
            if a < point or (a == point and affinity == 'right'):
                point += delta
            elif a == point:
                boundary_op = op
        else:
            if a <= point < b:
                return fail(_issue('point_overlap', 'Point refers to a replaced or deleted character.', record_id, op))
            if size and max(a, point) < min(b, point + size):
                return fail(_issue('point_quote_changed', 'Edit changes the following quote.', record_id, op))
            if b <= point:
                point += delta
    if not _check_quote(mapping.target_text, point, point + size, quote):
        return fail(_issue('new_quote_mismatch', 'Mapped point quote is not identical in the target.', record_id, boundary_op))
    return _result('point', record_id, source, dict(offset=point, quote=quote, affinity=affinity))


def map_records(mapping, records):
    """Collect independent record results; identity/revision/extra fields are ignored.

Each record supplies id, kind and quote, plus offset or start/end. No notes or
progress normalization, membership filtering, legacy authentication, or storage
format conversion occurs. A failed record makes the complete report not ok.
"""
    if not isinstance(mapping, Mapping):
        return dict(ok=False, records=[], issues=[_issue('invalid_mapping', 'Build a valid mapping first.')])
    if not isinstance(records, (list, tuple)):
        return dict(ok=False, records=[], issues=[_issue('invalid_records', 'Records must be a list.')])
    results, seen = [], set()
    for index, record in enumerate(records):
        ident = record.get('id') if isinstance(record, dict) else None
        if not isinstance(ident, str) or not ident or ident in seen:
            issue = _issue('invalid_record_id', 'Record ID is missing, invalid or duplicated.', ident)
            issue['record_index'] = index
            results.append(_result('unknown', ident, None, issue=issue))
            continue
        seen.add(ident)
        kind = record.get('kind')
        if kind == 'range':
            result = map_range(mapping, record.get('start'), record.get('end'), record.get('quote'), record_id=ident)
        elif kind == 'point':
            result = map_point(mapping, record.get('offset'), record.get('quote'), record_id=ident,
                               affinity=record.get('affinity', 'right'))
        else:
            result = _result(kind, ident, None, issue=_issue('invalid_record_kind', 'Unknown record kind.', ident))
        results.append(result)
    issues = [issue for result in results for issue in result['issues']]
    return dict(ok=not issues, records=results, issues=issues)
