"""Pure synthetic candidate tests; no HTTP, browser, filesystem fixtures or commit."""
import copy
import hashlib
import json

import book_drafts as drafts
import navigation
import publication_plan as plan
import reader_server as reader  # callbacks only; candidate module never imports it


COUNT = 0
NAME = 'synthetic.txt'
STAMP = '2026-10-01T09:00:00'


def check(value, detail=None):
    global COUNT
    COUNT += 1
    assert value, detail


def decode(raw):
    return reader.norm_text(reader.decode_bytes(raw, strict=True))


def sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def anchor(text, offset, u16=False):
    if u16:
        return navigation.quote_at(text.encode('utf-16-le'), offset)
    pos = reader.text_position(text, offset)
    return reader.re.sub(r'\s+', ' ', text[pos[3]:pos[3] + 40])


def make_fixture(count=28, edited=True, encoded=False):
    text = '第一章 起点\n' + ''.join(f'第{i:02}行甲😀乙稳定原文。\n' for i in range(count)) + '第二章 终点\n最后正文。\n'
    raw = b'\xff\xfe' + text.replace('\n', '\r\n').encode('utf-16-le') if encoded else text.encode('utf-8')
    revision = sha(raw)
    nav = dict(navigation.automatic(text, revision), name=NAME, extra={'keep': ['nav-metadata']})
    nav['items'][0]['extension'] = {'keep': 11}
    draft = drafts.create(NAME, text, revision, nav)
    target = text
    if edited:
        for request_id, before, after in (('publication_prefix', '', '编者说明。\n'), ('publication_far_edit', '原文', '修订文')):
            start = 0 if not before else drafts.units(target[:target.index(before)])
            draft, target, _ = drafts.edit(draft, target,
                dict(request_id=request_id, base_revision=revision, expected_revision=draft['revision'],
                     start=start, end=start + drafts.units(before), quote=before, replacement=after))
    draft['cursor_offset'] = 0
    decision = {'action': 'accept', 'replacement': '修订文'}
    digest = hashlib.sha256(json.dumps(decision, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    draft['proposal_receipts'] = {'pr_preserve': dict(status='accepted', proposal_id='pr_preserve',
        author_id='004', reviewed_by='user', draft_revision=draft['revision'], decision=decision,
        decision_digest=digest, proposal_digest='preserve-exactly')}
    draft['extension'] = {'keep': ['draft metadata']}
    identities = ['user', '004', 'retired_agent', 'unknown.id', 'offset', 'updated']
    highlights, records = [], {}
    for i in range(count):
        line = f'第{i:02}行甲😀乙稳定原文。'
        line_index = text.index(line)
        start = drafts.units(text[:line_index + len(f'第{i:02}行')])
        ident = identities[i] if i < len(identities) else 'historic-' + str(i)
        h = dict(start=start, end=start + drafts.units('甲😀乙'), text='甲😀乙',
                 book_hash=text_hash(text), extra={'keep': [i]},
                 thread=[dict(who='ai', name='retired_agent', author_id=None, text='历史评论',
                              at=STAMP, extra={'keep': 'comment metadata'})])
        if i:
            h.update(id='h_' + str(i), owner=ident, author_id=ident)
            h['thread'][0]['annotation_id'] = 'annotation-' + str(i)
        highlights.append(h)
        off = drafts.units(text[:line_index]); pos = reader.text_position(text, off)
        records[ident] = dict(offset=off, chars=pos[2], total_u16=pos[2], percent=off / pos[2],
                             anchor=anchor(text, off, u16=i % 2 == 1), updated=STAMP, revision=0,
                             paragraph_idx=pos[0], char_offset=pos[1], tts_offset=start if i == 0 else 0,
                             extra={'keep': [ident]}, author_id=ident)
        if i % 3:
            records[ident]['book_revision'] = revision
    notes = dict(version=2, updated=STAMP, extra={'keep': ['root']}, books={
        NAME: dict(highlights=highlights, extra={'keep': 'book'}),
        'unrelated.txt': dict(highlights=[{'opaque': {'offset': 444}}], extra=['unchanged'])})
    progress = dict(version=1, updated=STAMP, extra=['root'], books={
        NAME: records, 'unrelated.txt': {'revoked.old': {'offset': 333, 'unknown': [1, 2]}}})
    return dict(text=text, raw=raw, target=target, draft=draft, notes=notes, progress=progress, nav=nav)


def candidate(fixture, **extra):
    args = dict(name=NAME, source_bytes=fixture['raw'], draft=fixture['draft'],
                notes_raw=fixture['notes'], progress_raw=fixture['progress'],
                normalize_text=decode, normalize_highlight=reader._norm_highlight,
                text_position=reader.text_position, chapter_at=reader._chapter_at,
                navigation_raw=fixture['nav'])
    args.update(extra)
    return plan.build_candidate(**args)


def rejected(result, expected):
    check(not result['ok'], result)
    check(any(item['code'] == expected for item in result['issues']), result['issues'])
    for key in ('new_book_bytes', 'new_notes', 'new_progress', 'new_navigation', 'new_draft'):
        check(result[key] is None, key)


def test_complete_candidate():
    fixture = make_fixture(encoded=True)
    original = copy.deepcopy(fixture)
    result = candidate(fixture)
    check(result['ok'], result['issues'])
    check(fixture == original)
    check(result['new_book_bytes'] == fixture['target'].encode('utf-8'))
    check(b'\r' not in result['new_book_bytes'] and not result['new_book_bytes'].startswith(b'\xff\xfe'))
    check(result['summary']['highlights'] == 28 and result['summary']['progress_records'] == 28)
    check(result['new_notes']['books']['unrelated.txt'] == fixture['notes']['books']['unrelated.txt'])
    check(result['new_progress']['books']['unrelated.txt'] == fixture['progress']['books']['unrelated.txt'])
    check(result['new_navigation']['extra'] == fixture['nav']['extra'])
    check(result['new_navigation']['items'] == fixture['draft']['items'])
    check(result['new_navigation']['items'][0]['extension'] == {'keep': 11})
    check(result['new_draft']['proposal_receipts'] == fixture['draft']['proposal_receipts'])
    check(result['new_draft']['revision'] == fixture['draft']['revision'] + 1)
    check(result['new_draft']['base_revision'] == sha(result['new_book_bytes']) and result['new_draft']['operations'] == [])
    check(result['new_draft']['extension'] == fixture['draft']['extension'])
    old_h = fixture['notes']['books'][NAME]['highlights'][0]
    old_view = reader._norm_highlight(old_h)
    new_h = result['new_notes']['books'][NAME]['highlights'][0]
    check(new_h['id'] == old_view['id'] and new_h['highlight_id'] == old_view['id'])
    check(new_h['thread'][0]['annotation_id'] == old_view['thread'][0]['annotation_id'])
    check('author_id' not in new_h and 'owner' not in new_h)
    check(new_h['thread'][0]['author_id'] is None and 'author_status' not in new_h['thread'][0])
    for old, new in zip(fixture['notes']['books'][NAME]['highlights'], result['new_notes']['books'][NAME]['highlights']):
        check(old['text'] == new['text'] and old['extra'] == new['extra'])
        check(new['book_hash'] == text_hash(fixture['target']))
        check(reader._quote_at(fixture['target'], new['start'], new['end']) == new['text'])
        for key, value in old['thread'][0].items():
            check(new['thread'][0][key] == value, key)
        if 'author_id' in old:
            check(new['author_id'] == old['author_id'])
    for ident, old in fixture['progress']['books'][NAME].items():
        new = result['new_progress']['books'][NAME][ident]
        check(new['revision'] == 1 and new['updated'] == old['updated'])
        check(new['author_id'] == old['author_id'] and new['extra'] == old['extra'])
        check(new['chars'] == new['total_u16'] == drafts.units(fixture['target']))
        check(new['book_revision'] == sha(result['new_book_bytes']))
        check(new['percent'] == new['offset'] / new['chars'])
    first = result['new_progress']['books'][NAME]['user']
    check('修订文' in first['anchor'])  # far end of old anchor changed; point still valid
    check(first['tts_offset'] > fixture['progress']['books'][NAME]['user']['tts_offset'])
    check(result['new_progress']['books'][NAME]['004']['tts_offset'] == 0)
    for key in ('updated', 'extra', 'version'):
        check(result['new_notes'][key] == fixture['notes'][key])
        check(result['new_progress'][key] == fixture['progress'][key])


def test_legacy_empty_and_raw_formats():
    fixture = make_fixture(count=1)
    flat = copy.deepcopy(fixture['progress']['books'][NAME]['user'])
    fixture['progress']['books'][NAME] = flat
    result = candidate(fixture, notes_raw=b'\xef\xbb\xbf' + json.dumps(fixture['notes'], ensure_ascii=False).encode('utf-8'),
                       progress_raw=json.dumps(fixture['progress'], ensure_ascii=False).encode('utf-8'),
                       navigation_raw=json.dumps(fixture['nav'], ensure_ascii=False).encode('utf-8'))
    check(result['ok'], result['issues'])
    check('user' not in result['new_progress']['books'][NAME] and result['new_progress']['books'][NAME]['revision'] == 1)
    check(result['new_progress']['books'][NAME]['updated'] == STAMP)
    fixture = make_fixture(count=1, edited=False, encoded=True)
    fixture['draft']['items'][0]['title'] = '只改目录名称'
    result = candidate(fixture, notes_raw={}, progress_raw={'books': {NAME: {'user': {}, 'retired': {}}}})
    check(result['ok'], result['issues'])
    check(result['new_book_bytes'] == fixture['raw'])
    check(result['new_draft']['base_revision'] == sha(fixture['raw']))
    check(result['new_progress'] == {'books': {NAME: {'user': {}, 'retired': {}}}})
    check(result['new_notes'] == {} and result['summary']['progress_records'] == 0)
    check(result['new_navigation']['items'][0]['title'] == '只改目录名称')
    check(candidate(fixture, progress_raw={}, notes_raw={})['new_progress'] == {})
    fixture['draft']['items'][0].pop('extension')
    result = candidate(fixture, notes_raw={}, progress_raw={})
    check(result['ok'], result['issues'])
    check(result['new_navigation']['items'][0]['extension'] == {'keep': 11})


def test_failures():
    fixture = make_fixture(count=1)
    bad = copy.deepcopy(fixture['draft']); bad['base_revision'] = 'wrong'
    rejected(candidate(fixture, draft=bad), 'base_revision_mismatch')
    bad = copy.deepcopy(fixture['draft']); bad['operations'][0]['before'] = 'wrong'
    rejected(candidate(fixture, draft=bad), 'operation_before_mismatch')
    bad = copy.deepcopy(fixture['draft']); bad['chapter_issues'] = [{'preserve': 'unresolved'}]
    rejected(candidate(fixture, draft=bad), 'unresolved_chapters')
    bad = copy.deepcopy(fixture['draft']); bad['items'][0]['quote'] = 'wrong'
    rejected(candidate(fixture, draft=bad), 'candidate_validation_failed')
    bad = copy.deepcopy(fixture['notes']); bad['books'][NAME]['highlights'][0]['book_hash'] = 'sha256:wrong'
    rejected(candidate(fixture, notes_raw=bad), 'old_notes_hash_mismatch')
    bad = copy.deepcopy(fixture['notes']); bad['books'][NAME]['highlights'][0]['text'] = 'wrong'
    rejected(candidate(fixture, notes_raw=bad), 'old_quote_mismatch')
    bad = copy.deepcopy(fixture['notes']); bad['books'][NAME]['highlights'][0]['thread'][0]['highlight_id'] = 'another-highlight'
    rejected(candidate(fixture, notes_raw=bad), 'annotation_parent_mismatch')
    bad = copy.deepcopy(fixture['progress']); bad['books'][NAME]['user']['anchor'] = 'wrong'
    rejected(candidate(fixture, progress_raw=bad), 'old_progress_anchor_mismatch')
    bad = copy.deepcopy(fixture['progress']); bad['books'][NAME]['user'] = {'offset': 1, 'revision': 0}
    rejected(candidate(fixture, progress_raw=bad), 'unverified_progress')
    bad = copy.deepcopy(fixture['progress']); bad['books'][NAME]['user']['book_revision'] = 'wrong'
    rejected(candidate(fixture, progress_raw=bad), 'unverified_progress')
    bad = copy.deepcopy(fixture['progress']); bad['books'][NAME]['user']['tts_offset'] = 1_000_000
    rejected(candidate(fixture, progress_raw=bad), 'invalid_offset')
    bad = copy.deepcopy(fixture['progress']); bad['books'][NAME]['user'].pop('offset')
    rejected(candidate(fixture, progress_raw=bad), 'missing_progress_offset')
    rejected(candidate(fixture, notes_raw=b'{"books":{},"books":{}}'), 'duplicate_json_key')
    rejected(candidate(fixture, progress_raw={'books': {NAME: None}}), 'invalid_progress_book')
    rejected(candidate(fixture, notes_raw={'books': {NAME: None}}), 'invalid_notes_book')
    rejected(candidate(fixture, source_bytes=b'\xff\xfe\x00'), 'candidate_validation_failed')
    rejected(candidate(fixture, target_bytes=b'not the replay target'), 'target_text_mismatch')
    bad = copy.deepcopy(fixture['notes']); bad['books'][NAME]['highlights'][0]['start'] += 2
    rejected(candidate(fixture, notes_raw=bad), 'old_quote_mismatch')
    # The selected old quote, not an identical string elsewhere, must survive.
    original = make_fixture(count=2, edited=False)
    h = original['notes']['books'][NAME]['highlights'][0]
    raw_text = original['text']
    for suffix, start, end in (('partial', h['start'], h['start'] + 1), ('whole', h['start'], h['end'])):
        edited = copy.deepcopy(original)
        before = drafts.span(raw_text.encode('utf-16-le'), start, end)
        edited['draft'], edited['target'], _ = drafts.edit(edited['draft'], raw_text,
            dict(request_id='delete_' + suffix, base_revision=sha(edited['raw']), expected_revision=edited['draft']['revision'],
                 start=start, end=end, quote=before, replacement=''))
        rejected(candidate(edited), 'range_overlap')


def inverse(operations):
    return [dict(id='inverse_' + item['id'], start=item['start'], end=item['start'] + drafts.units(item['after']),
                 before=item['after'], after=item['before']) for item in reversed(operations)]


def test_rollback_candidates():
    fixture = make_fixture(count=2, encoded=True)
    published = candidate(fixture)
    check(published['ok'], published['issues'])
    notes = copy.deepcopy(published['new_notes'])
    notes['books'][NAME]['highlights'][0]['thread'].append(
        dict(annotation_id='new_comment', who='custom_retired', text='发布后的新评论', extra={'keep': 999}))
    original_notes = copy.deepcopy(notes)
    restored = candidate(fixture, source_bytes=published['new_book_bytes'],
        notes_raw=notes, progress_raw=published['new_progress'], target_bytes=fixture['raw'],
        operations_override=inverse(fixture['draft']['operations']), navigation_raw=fixture['nav'])
    check(restored['ok'], restored['issues'])
    check(notes == original_notes)
    check(restored['new_book_bytes'] == fixture['raw'])
    check(restored['new_navigation']['items'] == fixture['nav']['items'])
    check(restored['new_draft']['proposal_receipts'] == fixture['draft']['proposal_receipts'])
    check(restored['new_notes']['books'][NAME]['highlights'][0]['thread'][-1] == notes['books'][NAME]['highlights'][0]['thread'][-1])
    check(restored['new_notes']['books']['unrelated.txt'] == notes['books']['unrelated.txt'])
    check(restored['new_progress']['books'][NAME]['user']['revision'] == 2)
    check(restored['new_progress']['books'][NAME]['user']['updated'] == STAMP)
    for old, new in zip(fixture['notes']['books'][NAME]['highlights'], restored['new_notes']['books'][NAME]['highlights']):
        check((old['start'], old['end'], old['text']) == (new['start'], new['end'], new['text']))
    # New note in inserted publication text cannot be attached to a nearby old sentence.
    unsafe = copy.deepcopy(notes)
    unsafe['books'][NAME]['highlights'].append(dict(id='new_prefix_note', start=0, end=2, text='编者',
        book_hash=text_hash(fixture['target']), author_id='new_reader', thread=[]))
    result = candidate(fixture, source_bytes=published['new_book_bytes'], notes_raw=unsafe,
        progress_raw=published['new_progress'], target_bytes=fixture['raw'],
        operations_override=inverse(fixture['draft']['operations']), navigation_raw=fixture['nav'])
    rejected(result, 'range_overlap')
    check(any(item.get('record_id') == 'new_prefix_note' for item in result['issues']))


def test_full_chapter_heading_guard():
    fixture = make_fixture(count=1, edited=False)
    text = fixture['text']
    start = drafts.units(text[:text.index('起点') + 1])
    draft, target, _ = drafts.edit(fixture['draft'], text,
        dict(request_id='delete_title_tail', base_revision=sha(fixture['raw']), expected_revision=1,
             start=start, end=start + 1, quote='点', replacement=''))
    check(not draft['chapter_issues'])  # existing edit only checked the heading start
    check(draft['items'][0]['title'] == fixture['draft']['items'][0]['title'])
    fixture.update(draft=draft, target=target)
    result = candidate(fixture)
    rejected(result, 'chapter_heading_changed')
    check(any(item.get('operation_id') == 'delete_title_tail' for item in result['issues']))
    for basis in ('quick_selection', 'administrator_verified'):
        verified = copy.deepcopy(draft)
        verified['items'][0].update(title='明确的手工别名', basis=basis)
        result = candidate(fixture, draft=verified)
        check(result['ok'], result['issues'])
        check(result['new_navigation']['items'][0]['title'] == '明确的手工别名')
    uncertain = copy.deepcopy(draft)
    uncertain['items'][0].update(title='来历不明的旧别名', basis='legacy_unknown')
    rejected(candidate(fixture, draft=uncertain), 'chapter_origin_unverified')
    # Growing a heading at EOF changes its full line even though range-end
    # insertion affinity correctly excludes the appended text from the old quote.
    text = '第一章 末尾'
    raw = text.encode('utf-8'); nav = dict(navigation.automatic(text, sha(raw)), name=NAME)
    # A single bare line is intentionally not accepted by the automatic finder;
    # supply a valid older heading row to exercise this stored-metadata boundary.
    nav.update(status='ok', items=[dict(id='old_eof_heading', title=text, offset=0, parent_id=None,
        order=0, quote=text, basis='legacy_heading')])
    value = drafts.create(NAME, text, sha(raw), nav)
    value, target, _ = drafts.edit(value, text,
        dict(request_id='append_heading_tail', base_revision=sha(raw), expected_revision=1,
             start=drafts.units(text), end=drafts.units(text), quote='', replacement='追加'))
    tiny = dict(raw=raw, text=text, target=target, draft=value, nav=nav, notes={}, progress={})
    rejected(candidate(tiny), 'chapter_heading_changed')


def test_additional_legacy_and_tts_edges():
    fixture = make_fixture(count=1)
    record = fixture['progress']['books'][NAME]['user']
    record.pop('updated')  # complete anchor/length/percentage evidence can prove a revision=0 save
    record.pop('book_revision', None)
    result = candidate(fixture)
    check(result['ok'], result['issues'])
    check('updated' not in result['new_progress']['books'][NAME]['user'])
    bad = copy.deepcopy(fixture['progress'])
    bad['books'][NAME]['user']['chars'] = len(fixture['text'])
    rejected(candidate(fixture, progress_raw=bad), 'unverified_progress')
    text = '甲乙丙'
    raw = text.encode('utf-8'); nav = dict(navigation.automatic(text, sha(raw)), name=NAME)
    value = drafts.create(NAME, text, sha(raw), nav)
    value, target, _ = drafts.edit(value, text,
        dict(request_id='delete_before_tts', base_revision=sha(raw), expected_revision=1,
             start=0, end=2, quote='甲乙', replacement=''))
    progress = {'books': {NAME: {'retired.old': dict(offset=2, chars=3, percent=2/3, anchor='丙',
        tts_offset=2, updated=STAMP, book_revision=sha(raw), revision=0)}}}
    tiny = dict(raw=raw, text=text, target=target, draft=value, nav=nav, notes={}, progress=progress)
    result = candidate(tiny)
    check(result['ok'], result['issues'])
    check(result['new_progress']['books'][NAME]['retired.old']['tts_offset'] == 0)


if __name__ == '__main__':
    test_complete_candidate()
    test_legacy_empty_and_raw_formats()
    test_failures()
    test_rollback_candidates()
    test_full_chapter_heading_guard()
    test_additional_legacy_and_tts_edges()
    print(f'PASS publication plan: {COUNT} assertions; 28 multi-identity notes/progress records; raw identity/extra preservation, stable legacy IDs, revision=0 evidence, minimum point quote, both old anchor conventions, draft/navigation templates, strict bytes/hash, directory-only byte retention, rollback/new-comment preservation and insertion-region refusal; no HTTP/disk/commit')
