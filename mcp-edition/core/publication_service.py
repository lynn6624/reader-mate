"""Administrator publication orchestration over the existing reader and stores.

Every public method is called under the publication EX barrier. Snapshot data
is reread on confirmation; only the selected book is migrated in shared JSON.
No identity credential is included in a publication or rollback transaction.
"""
import copy
import hashlib
import json
from pathlib import Path

import book_drafts as drafts
import navigation as nav
from publication_plan import build_candidate
from publication_store import ConflictError, ValidationError, PublicationError


MAX_DOCUMENT = 8 * 1024 * 1024


class RequestError(PublicationError):
    def __init__(self, message, status=409, **detail):
        super().__init__(message)
        self.status = status
        self.detail = detail


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def pairs(rows):
    value = {}
    for key, item in rows:
        if key in value:
            raise RequestError('元数据有重复字段，原记录已保留', 503)
        value[key] = item
    return value


def parse(raw, default=None):
    if raw is None:
        return copy.deepcopy(default)
    if len(raw) > MAX_DOCUMENT:
        raise RequestError('本阶段发布元数据超过8MB上限，原记录已保留', 413)
    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as exc:
        raise RequestError('发布所需元数据不可安全读取，原记录已保留', 503) from exc


class Controller:
    def __init__(self, reader, store):
        self.reader = reader
        self.store = store
        self.core = Path(reader.BASE)

    def _read(self, relative, maximum=MAX_DOCUMENT):
        path = self.core / relative
        for part in [path, *path.parents]:
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
                raise RequestError('发布不接受链接文件', 400)
            if part == self.core:
                break
        try:
            if path.stat().st_size > maximum:
                raise RequestError('发布输入超过本阶段大小限制', 413)
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def _name(self, name):
        if (not isinstance(name, str) or self.reader.safe_name(name) != name
                or name == self.reader.REFERENCE_BOOK_NAME):
            raise RequestError('请选择可编辑的完整书名', 400)
        return name

    def _paths(self, name):
        key = hashlib.sha256(name.encode('utf-8')).hexdigest()
        return {'book': '书库/' + name, 'notes': 'data/notes.json',
                'progress': 'data/progress.json', 'library': 'data/library.json',
                'navigation': 'data/navigation/' + key + '.json',
                'draft': 'data/drafts/' + key + '.json'}

    def _snapshot(self, name):
        self._name(name)
        paths = self._paths(name)
        with self.reader._lock:
            source = self._read(paths['book'], drafts.MAX_SOURCE_BYTES)
            if source is None:
                raise RequestError('书库里没有这本书', 404)
            # Validate all shared records, but never serialize a normalized view.
            self.reader.load_progress()
            self.reader.load_notes()
            raw = {key: self._read(path) for key, path in paths.items() if key != 'book'}
            current = {key: parse(value, {}) for key, value in raw.items()}
            current['draft'] = parse(raw['draft'])
            current['navigation'] = parse(raw['navigation'])
            if current['draft'] is None:
                raise RequestError('请先建立共享草稿，再校验发布', 409)
            # Existing bounded validator also verifies receipts/operation envelope.
            verified = drafts.read_metadata(self.reader.DATA_DIR, name)
            if verified != current['draft']:
                raise RequestError('草稿已更新，请重新校验')
            import revision_proposals as proposals
            try:
                proposals.receipts(verified)
            except proposals.Error as exc:
                raise RequestError(str(exc), 503) from exc
        return dict(name=name, paths=paths, source=source, raw=raw, **current,
                    head=self.store.current(name))

    def _candidate(self, snapshot, *, target_bytes=None, operations=None,
                   draft=None, navigation=None):
        r = self.reader
        return build_candidate(snapshot['name'], snapshot['source'],
            draft if draft is not None else snapshot['draft'],
            snapshot['notes'], snapshot['progress'],
            normalize_text=lambda raw: r.norm_text(r.decode_bytes(raw, strict=True)),
            normalize_highlight=r._norm_highlight, text_position=r.text_position,
            chapter_at=lambda text, offset, revision: r._chapter_at(text, offset, revision),
            navigation_raw=navigation if navigation is not None else snapshot['navigation'],
            target_bytes=target_bytes, operations_override=operations)

    def _binding(self, snapshot, action, publication_id=None):
        return fingerprint({'name': snapshot['name'], 'action': action,
            'publication_id': publication_id,
            'book_revision': 'sha256:' + hashlib.sha256(snapshot['source']).hexdigest(),
            'draft': snapshot['draft'], 'navigation': snapshot['navigation'],
            'head': snapshot['head']})

    def _public(self, snapshot, candidate, action, publication_id=None):
        issues = candidate.get('issues', [])
        raw_summary=candidate.get('summary',{})
        summary=dict(raw_summary,old_units=raw_summary.get('source_units',0),
            new_units=raw_summary.get('target_units',0),edit_count=raw_summary.get('operations',0),
            highlight_count=raw_summary.get('highlights',0),
            annotation_count=sum(len(h.get('thread',[])) for h in
                snapshot['notes'].get('books',{}).get(snapshot['name'],{}).get('highlights',[])),
            progress_count=raw_summary.get('progress_records',0),
            chapter_count=len((candidate.get('new_navigation') or snapshot['draft']).get('items',[])))
        return dict(name=snapshot['name'], action=action,
            can_commit=bool(candidate.get('ok')), summary=summary,
            issues=issues[:100], issues_total=len(issues),
            preview_digest=self._binding(snapshot, action, publication_id),
            base_revision='sha256:' + hashlib.sha256(snapshot['source']).hexdigest(),
            draft_revision=snapshot['draft']['revision'])

    def preview(self, name):
        snapshot = self._snapshot(name)
        return self._public(snapshot, self._candidate(snapshot), 'publish')

    def _existing_receipt(self, name, request_id, action, confirmation):
        if not isinstance(request_id, str):
            raise RequestError('缺少本次确认标识', 400)
        # Looking up a durable receipt before checking the new draft makes a
        # response-lost retry idempotent even though publication rebased it.
        receipt = self.store.request_receipt(name, request_id)
        if receipt is None:
            return None
        report = receipt.get('report', {})
        if report.get('action') != action or report.get('preview_digest') != confirmation:
            raise RequestError('同一确认标识不能用于另一项发布操作')
        return receipt

    def _replacements(self, snapshot, candidate, request_id):
        paths = snapshot['paths']
        if candidate['new_notes'] != snapshot['notes']:
            # A stable epoch invalidates polling caches while keeping retries
            # byte-identical even after a failure before the COMMIT marker.
            candidate['new_notes']['publication_revision'] = request_id
        values = {paths['book']: candidate['new_book_bytes'],
                  paths['notes']: encoded(candidate['new_notes']),
                  paths['progress']: encoded(candidate['new_progress']),
                  paths['navigation']: encoded(candidate['new_navigation']),
                  paths['draft']: encoded(candidate['new_draft'])}
        library = copy.deepcopy(snapshot['library'])
        books = library.setdefault('books', {})
        entry = dict(books.get(snapshot['name'], {}))
        raw = candidate['new_book_bytes']
        text = self.reader.norm_text(self.reader.decode_bytes(raw, strict=True))
        entry.update(name=snapshot['name'], bytes=len(raw), chars=len(text), mtime=0)
        entry.setdefault('title', snapshot['name'][:-4])
        entry.setdefault('category', self.reader.DEFAULT_CATEGORY)
        entry.setdefault('category_source', 'default')
        books[snapshot['name']] = entry
        values[paths['library']] = encoded(library)
        # Do not reserialize shared documents when no selected-book data changed.
        for key, field in (('notes', 'new_notes'), ('progress', 'new_progress')):
            if candidate[field] == snapshot[key]:
                values.pop(paths[key])
        return values

    def commit(self, name, preview_digest, request_id):
        name = self._name(name)
        receipt = self._existing_receipt(name, request_id, 'publish', preview_digest)
        if receipt:
            return receipt
        snapshot = self._snapshot(name)
        if preview_digest != self._binding(snapshot, 'publish'):
            raise RequestError('正文、草稿或目录已变化，请重新校验')
        candidate = self._candidate(snapshot)  # Latest notes/progress, not an old preview.
        if not candidate['ok']:
            raise RequestError('仍有无法可靠迁移的位置，未覆盖正式正文',
                               issues=candidate['issues'][:100], issues_total=len(candidate['issues']))
        report = dict(action='publish', preview_digest=preview_digest,
                      summary=candidate['summary'], issues=[])
        result = self.store.commit(name, request_id, self._replacements(snapshot, candidate, request_id), report)
        self._mirror()
        return result

    def _rollback_candidate(self, name, publication_id):
        snapshot = self._snapshot(name)
        head = snapshot['head'] or {}
        if head.get('publication_id') != publication_id or head.get('action') != 'publish':
            raise RequestError('只能回滚当前最近一次正式发布')
        manifest = self.store.read_manifest(name, publication_id)
        if not manifest or manifest.get('report', {}).get('action') != 'publish':
            raise RequestError('找不到可回滚的发布记录', 404)
        paths = snapshot['paths']
        old_book = self.store.old_payload(name, publication_id, paths['book'])
        old_draft = parse(self.store.old_payload(name, publication_id, paths['draft']))
        old_navigation = parse(self.store.old_payload(name, publication_id, paths['navigation']))
        new_draft = parse(self.store.new_payload(name, publication_id, paths['draft']))
        new_navigation = parse(self.store.new_payload(name, publication_id, paths['navigation']))
        if snapshot['draft'] != new_draft or snapshot['navigation'] != new_navigation:
            raise RequestError('发布后的草稿或正式目录已有新修改；已保留，请先处理这些修改')
        if old_book is None or old_draft is None:
            raise RequestError('回滚材料不完整，未改变现有数据', 503)
        inverse = [dict(id='inverse_' + str(i), start=op['start'],
                        end=op['start'] + drafts.units(op['after']),
                        before=op['after'], after=op['before'])
                   for i, op in enumerate(reversed(old_draft['operations']))]
        target_text = self.reader.norm_text(self.reader.decode_bytes(old_book, strict=True))
        revision = 'sha256:' + hashlib.sha256(old_book).hexdigest()
        target_nav = old_navigation or nav.automatic(target_text, revision)
        template = dict(old_draft, base_revision='sha256:' + hashlib.sha256(snapshot['source']).hexdigest(),
                        revision=snapshot['draft']['revision'], items=target_nav['items'], chapter_issues=[],cursor_offset=0)
        candidate = self._candidate(snapshot, target_bytes=old_book, operations=inverse,
                                    draft=template, navigation=target_nav)
        if candidate['ok']:
            restored = copy.deepcopy(old_draft)
            restored.update(revision=snapshot['draft']['revision'] + 1, updated_by='user')
            candidate['new_draft'] = restored
            candidate['new_navigation'] = copy.deepcopy(target_nav)
            candidate['new_navigation']['revision'] = max(
                (snapshot['navigation'] or {}).get('revision', 0), target_nav.get('revision', 0)) + 1
        return snapshot, candidate

    def rollback_preview(self, name, publication_id):
        snapshot, candidate = self._rollback_candidate(name, publication_id)
        return self._public(snapshot, candidate, 'rollback', publication_id)

    def rollback(self, name, publication_id, preview_digest, request_id):
        name = self._name(name)
        receipt = self._existing_receipt(name, request_id, 'rollback', preview_digest)
        if receipt:
            return receipt
        snapshot, candidate = self._rollback_candidate(name, publication_id)
        if preview_digest != self._binding(snapshot, 'rollback', publication_id):
            raise RequestError('回滚预检已过期，请重新校验')
        if not candidate['ok']:
            raise RequestError('新版中有无法反向定位的记录，未回滚或删除任何内容',
                               issues=candidate['issues'][:100], issues_total=len(candidate['issues']))
        report = dict(action='rollback', rollback_of=publication_id,
                      preview_digest=preview_digest, summary=candidate['summary'], issues=[])
        result = self.store.commit(name, request_id, self._replacements(snapshot, candidate, request_id), report)
        self._mirror()
        return result

    def history(self, name):
        self._name(name)
        head = self.store.current(name) or {}
        result = []
        for item in self.store.list_history(name):
            row = {key: item.get(key) for key in
                   ('publication_id', 'action', 'created_at', 'status', 'bytes')}
            latest = row['publication_id'] == head.get('publication_id')
            row.update(can_rollback=latest and row['action'] == 'publish' and row['status'] == 'applied',
                       can_cleanup=not latest and row['status'] == 'applied')
            result.append(row)
        return dict(name=name, items=result)

    def cleanup_preview(self, name, publication_ids):
        self._name(name)
        return dict(self.store.preview_cleanup(name, publication_ids),count=len(publication_ids))

    def cleanup(self, name, publication_ids, confirm_digest):
        self._name(name)
        return self.store.cleanup(name, publication_ids, confirm_digest)

    def _mirror(self):
        # A derived Markdown failure cannot undo a durably committed book.
        try:
            self.reader._refresh_progress_mirror(self.reader.load_progress())
        except Exception:
            pass
