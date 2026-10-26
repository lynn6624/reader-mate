"""Revision proposals; callers hold the existing per-book draft lock for all writes.

Proposals never create drafts. Acceptance commits an application receipt together
with the existing PR5 draft, so there is no queue/draft two-file commit window.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile

import book_drafts as drafts
import navigation as nav

MAX_PROPOSALS = 500
MAX_FILE = 4 * 1024 * 1024
STATES = ('pending', 'accepted', 'rejected', 'conflict')
EDIT_ACTIONS = ('replace', 'delete', 'insert_before', 'insert_after')
Error = drafts.DraftError


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def path_for(root, name):
    return Path(root) / 'proposals' / (hashlib.sha256(name.encode()).hexdigest() + '.json')


def load(root, name):
    path = path_for(root, name)
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_FILE + 1)
    except FileNotFoundError:
        return dict(schema_version=1, name=name, proposals=[])
    except OSError as exc:
        raise Error('建议队列不可读，原文件已保留', 503) from exc
    try:
        if len(raw) > MAX_FILE:
            raise ValueError()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=drafts._pairs)
        if (value['schema_version'] != 1 or value['name'] != name or
                not isinstance(value['proposals'], list) or len(value['proposals']) > MAX_PROPOSALS):
            raise ValueError()
        seen = set()
        for row in value['proposals']:
            if (row['id'] in seen or row['id'] != 'p_' + digest(row['spec']) or
                    row['status'] not in ('pending', 'rejected', 'conflict') or
                    not isinstance(row['created_at'], str)):
                raise ValueError()
            seen.add(row['id'])
        return value
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise Error('建议队列损坏，原文件已保留；请核对备份', 503) from exc


def save(root, name, value):
    raw = (json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode()
    if len(raw) > MAX_FILE:
        raise Error('此书建议队列已达4MB上限，请保留记录', 413)
    target = path_for(root, name)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.proposals-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def bounded(value, field, limit, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise Error(field + '必须为有效文本', 400)
    try:
        size = drafts.units(value)
    except UnicodeError as exc:
        raise Error(field + '含不完整字符', 400) from exc
    if size > limit:
        raise Error(field + '超过长度限制', 413)
    return value


def locate(text, data):
    quote = bounded(data.get('quote', ''), '原文片段', drafts.MAX_SELECTION, True)
    before = bounded(data.get('context_before', ''), '前文', 160)
    after = bounded(data.get('context_after', ''), '后文', 160)
    start = data.get('start', -1)
    raw = text.encode('utf-16-le')
    if type(start) is not int or start < -1:
        raise Error('start须为UTF-16位置或省略', 400)
    if start >= 0:
        end = start + drafts.units(quote)
        if drafts.span(raw, start, end) != quote:
            raise Error('原文片段与指定位置不一致')
        index = len(raw[:start * 2].decode('utf-16-le'))
        matches = [index] if text.endswith(before, 0, index) and text.startswith(after, index + len(quote)) else []
    else:
        matches = []; cursor = 0
        while len(matches) < 2:
            index = text.find(quote, cursor)
            if index < 0:
                break
            if text.endswith(before, 0, index) and text.startswith(after, index + len(quote)):
                matches.append(index)
            cursor = index + 1
    if not matches:
        raise Error('找不到匹配原文及上下文，请重新读取正文')
    if len(matches) != 1:
        raise Error('原文出现多次，定位有歧义；请补充紧邻上下文或指定start')
    index = matches[0]; start = drafts.units(text[:index]); end = start + drafts.units(quote)
    return dict(start=start, end=end, quote=quote,
                context_before=text[max(0, index - 60):index],
                context_after=text[index + len(quote):index + len(quote) + 60])


def select_chapter(items, ident, title):
    matches = [row for row in items if (row['id'] == ident if ident else row['title'] == title)]
    if len(matches) != 1:
        raise Error('章节不存在或名称有歧义，请提供准确的章节ID或唯一标题')
    return copy.deepcopy(matches[0])


def propose(queue, name, text, revision, table, actor, kind, data):
    if data.get('book_revision') != revision:
        raise Error('正式正文版本已变化，请重新读取后提交')
    spec = dict(name=name, author_id=actor, book_revision=revision, kind=kind,
                reason=bounded(data.get('reason', ''), '理由', 2000))
    action = data.get('operation')
    if kind == 'edit':
        if action not in EDIT_ACTIONS:
            raise Error('正文建议类型无效', 400)
        spec.update(locate(text, data), operation=action)
        replacement = bounded(data.get('replacement', ''), '建议内容', drafts.MAX_SELECTION)
        if action == 'delete' and replacement:
            raise Error('删除建议不能同时提供替换文字', 400)
        if action.startswith('insert_') and not replacement:
            raise Error('插入内容不能为空', 400)
        if action == 'replace' and replacement == spec['quote']:
            raise Error('建议未改变正文', 400)
        spec['replacement'] = replacement
    elif kind == 'chapter':
        if action not in ('add', 'update', 'delete'):
            raise Error('章节建议类型无效', 400)
        if table.get('status') in ('invalid_metadata', 'content_changed'):
            raise Error('现有目录需要管理员核对')
        target = None if action == 'add' else select_chapter(table['items'], data.get('chapter_id'), data.get('target_title'))
        selection = data
        if target and (action == 'delete' or not data.get('quote')):
            # A directory quote may include following paragraphs. Locate its own
            # heading line so edits to later body text do not invalidate a rename.
            tail = text.encode('utf-16-le')[target['offset'] * 2:].decode('utf-16-le')
            heading = tail.split('\n', 1)[0] or target['quote']
            selection = dict(quote=heading, start=target['offset'])
        spec.update(locate(text, selection), operation=action, target=target)
        title = data.get('title') or (target['title'] if target else spec['quote'].strip())
        title = bounded(title, '章节标题', 160, True)
        parent_title = data.get('parent_title')
        parent = (select_chapter(table['items'], target.get('parent_id'), None)
                  if target and target.get('parent_id') and parent_title is None else None)
        if parent_title:
            parent = select_chapter(table['items'], None, parent_title)
        spec.update(title=title, parent=parent)
        # Reuse PR5 validation before recording invalid chapter structures.
        if action != 'delete':
            nav_data = dict(action='upsert', id=target['id'] if target else None, title=title,
                            parent_id=parent['id'] if parent else None,
                            start=spec['start'], end=spec['end'], quote=spec['quote'])
            drafts.change_chapter(table['items'], text, nav_data)
    else:
        raise Error('建议类型无效', 400)
    ident = 'p_' + digest(spec)
    prior = next((row for row in queue['proposals'] if row['id'] == ident), None)
    if prior:
        return prior, False
    if len(queue['proposals']) >= MAX_PROPOSALS:
        raise Error('此书已达500条建议上限，请保留记录', 413)
    row = dict(id=ident, spec=spec, status='pending', created_at=drafts.stamp())
    queue['proposals'].append(row)
    return row, True


def receipts(metadata):
    result = metadata.get('proposal_receipts', {}) if metadata else {}
    if not isinstance(result, dict) or len(result) > MAX_PROPOSALS:
        raise Error('草稿审批记录损坏，原文件已保留', 503)
    for ident, receipt in result.items():
        if (not isinstance(receipt, dict) or receipt.get('status') != 'accepted' or
                receipt.get('reviewed_by') != 'user' or receipt.get('proposal_id') != ident or
                not isinstance(receipt.get('decision'), dict) or
                receipt.get('decision_digest') != digest(receipt['decision']) or
                type(receipt.get('draft_revision')) is not int or
                not 1 <= receipt['draft_revision'] <= metadata['revision']):
            raise Error('草稿审批记录损坏，原文件已保留', 503)
    return result


def public(row, metadata, receipt_map=None):
    receipt = (receipts(metadata) if receipt_map is None else receipt_map).get(row['id'])
    if receipt and receipt.get('proposal_digest') != digest(row['spec']):
        raise Error('建议与草稿审批记录不一致，请核对备份', 503)
    return dict(row, status='accepted' if receipt else row['status'], decision=receipt)


def mapped(start, end, operations, insertion=None):
    for op in operations:
        a, b = op['start'], op['end']; delta = drafts.units(op['after']) - (b - a)
        if insertion is not None and a <= insertion <= b:
            raise Error('插入位置已被其他编辑使用，请重新提交建议')
        if b <= start:
            start += delta; end += delta
            if insertion is not None: insertion += delta
        elif a < end:
            raise Error('建议原文与已有草稿编辑重叠，请核对后重新提交')
    return start, end


def verify_chapter(original, value):
    if not original:
        return
    target = next((row for row in value['items'] if row['id'] == original['id']), None)
    start, _ = mapped(original['offset'], original['offset'] + 1, value['operations'])
    if (target is None or target['offset'] != start or
            any(target.get(k) != original.get(k) for k in ('title', 'parent_id'))):
        raise Error('目标章节或上级章节已调整，请重新提交建议')


def preview(row, value, text, revision):
    spec = row['spec']
    if revision != spec['book_revision'] or value['base_revision'] != revision:
        raise Error('正式正文版本已变化，建议不能应用到当前草稿')
    insertion = (spec['start'] if spec['operation'] == 'insert_before' else spec['end']) if spec['operation'].startswith('insert_') else None
    start, end = mapped(spec['start'], spec['end'], value['operations'], insertion)
    raw = text.encode('utf-16-le')
    if drafts.span(raw, start, end) != spec['quote']:
        raise Error('当前草稿原文不匹配，请重新提交建议')
    if spec['kind'] == 'chapter':
        verify_chapter(spec['target'], value); verify_chapter(spec['parent'], value)
        if spec['operation'] == 'add' and any(r['offset'] == start and r['title'] == spec['title'] for r in value['items']):
            raise Error('此章节已存在，不能重复添加')
    if insertion is not None:
        start = end = start if spec['operation'] == 'insert_before' else end
    return dict(start=start, end=end, quote=drafts.span(raw, start, end),
                replacement=spec.get('replacement', ''), title=spec.get('title', ''),
                expected_revision=value['revision'])


def decision_for(row, data):
    result = dict(action=data.get('action'))
    if result['action'] not in ('accept', 'reject'):
        raise Error('审批操作无效', 400)
    if result['action'] == 'accept':
        field = 'replacement' if row['spec']['kind'] == 'edit' else 'title'
        result[field] = bounded(data.get(field, row['spec'][field]), field,
                                drafts.MAX_SELECTION if field == 'replacement' else 160,
                                required=field == 'title')
    return result


def apply(row, value, text, revision, decision):
    """Compute a PR5 edit plus receipt. Caller commits once using drafts.save."""
    position = preview(row, value, text, revision)
    spec = row['spec']
    if spec['kind'] == 'edit':
        request = dict(position, base_revision=revision, request_id='approve_' + row['id'][2:],
                       replacement=decision['replacement'])
        value, text, _ = drafts.edit(value, text, request)
    else:
        value = copy.deepcopy(value)
        value['items'] = drafts.change_chapter(value['items'], text, dict(position,
            action='delete' if spec['operation'] == 'delete' else 'upsert', title=decision['title'],
            id=spec['target']['id'] if spec['target'] else None,
            parent_id=spec['parent']['id'] if spec['parent'] else None))
        value.update(revision=value['revision'] + 1, updated=drafts.stamp(), updated_by='user')
    receipt = dict(status='accepted', proposal_id=row['id'], proposal_digest=digest(spec),
        author_id=spec['author_id'], reviewed_by='user', decided_at=drafts.stamp(),
        draft_revision=value['revision'], decision=decision, decision_digest=digest(decision))
    value.setdefault('proposal_receipts', {})[row['id']] = receipt
    return value, text
