#!/usr/bin/env python3
"""Real LOCAL book bytes, exclusively synthetic reading/annotation records.

--book is opened read-only. All copies, synthetic records, draft/proposal state,
publication history and diagnostics remain below --work-dir (default: the
workspace's .private/publish-20261031/realcopy). No production records, network,
HTTP listener, credentials, or external services are read. Stdout contains only
counts and SHA256 values; neither text nor individual records are printed.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import traceback

import book_drafts as drafts
import navigation as nav
import publication_store
from publication_store import Store
from publication_service import Controller, RequestError, encoded
import reader_server as reader
import revision_proposals as proposals


COUNT = 24
STAMP = '2026-10-01T09:00:00'
LATER = '2026-10-31T12:00:00'
PREFIX = '【合成验收新增导语😀】\n'


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def long_path(path):
    path = Path(path).absolute()
    if os.name == 'nt' and not str(path).startswith('\\\\?\\'):
        path = Path('\\\\?\\UNC\\' + str(path)[2:] if str(path).startswith('\\\\') else '\\\\?\\' + str(path))
    return publication_store._safe(path)


def write(path, value, private_root):
    path = long_path(path)
    assert path.is_relative_to(private_root) and path != private_root
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = value if isinstance(value, bytes) else encoded(value)
    with path.open('wb') as stream:
        stream.write(raw)
    os.chmod(path, 0o600)


def progress_record(text, offset, revision, count=0, updated=STAMP, extra=None):
    position = reader.text_position(text, offset)
    assert position is not None
    return dict(offset=offset, chars=position[2], percent=offset / position[2] if position[2] else 0,
                anchor=reader.re.sub(r'\s+', ' ', text[position[3]:position[3]+40]),
                paragraph_idx=position[0], char_offset=position[1], chapter=reader._chapter_at(text, offset, revision),
                book_revision=revision, revision=count, updated=updated, tts_offset=offset,
                extension=copy.deepcopy(extra or {'opaque': 'synthetic-progress'}))


def assert_records(notes, progress, originals, text, delta, revisions, user_offset=None):
    name, old_notes, old_progress = originals
    old_highlights = old_notes['books'][name]['highlights']
    actual = notes['books'][name]['highlights']
    assert len(actual) == COUNT
    raw = text.encode('utf-16-le')
    for before, after in zip(old_highlights, actual):
        for field in ('id', 'highlight_id', 'author_id', 'owner', 'created', 'extension', 'thread'):
            # One deliberately newer synthetic comment is allowed; the entire
            # old thread, including opaque metadata and authors, remains exact.
            if field == 'thread':
                assert after[field][:len(before[field])] == before[field]
            else:
                assert after[field] == before[field]
        assert after['start'] == before['start'] + delta
        assert after['end'] == before['end'] + delta
        assert raw[after['start']*2:after['end']*2].decode('utf-16-le') == before['text']
    assert notes['extension'] == old_notes['extension']
    assert notes['books'][name]['extension'] == old_notes['books'][name]['extension']
    actual_records = progress['books'][name]
    assert set(actual_records) == set(old_progress['books'][name])
    for ident, before in old_progress['books'][name].items():
        after = actual_records[ident]
        expected = user_offset if ident == 'user' and user_offset is not None else before['offset'] + delta
        assert after['offset'] == expected
        assert after['revision'] == before['revision'] + revisions + (1 if ident == 'user' and user_offset is not None else 0)
        assert after['updated'] == (LATER if ident == 'user' and user_offset is not None else before['updated'])
        assert after['extension'] == before['extension']
        assert after['tts_offset'] == before['tts_offset'] + delta
    assert progress['extension'] == old_progress['extension']


def run(source, work):
    os.umask(0o077)
    source, work = long_path(source), long_path(work)
    assert source.is_file() and not source.is_relative_to(work)
    before_sha = sha(source)
    source_raw = source.read_bytes()  # The only source-project file opened.
    assert hashlib.sha256(source_raw).hexdigest() == before_sha
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_root = work / ('run-' + secrets.token_hex(8))
    run_root.mkdir(mode=0o700)
    core = run_root / 'core'
    core.mkdir(mode=0o700)
    store = Store(core)
    core = store.core
    private_copy = run_root / 'source-copy.bin'
    write(private_copy, source_raw, work)
    checks = 0
    def source_unchanged():
        nonlocal checks
        assert sha(source) == before_sha
        checks += 1
    source_unchanged()
    name = source.name
    paths = {'BASE': core, 'DATA_DIR': core/'data', 'BOOKS_DIR': core/'书库',
             'PROGRESS_FILE': core/'data/progress.json', 'LIBRARY_FILE': core/'data/library.json',
             'NOTES_FILE': core/'data/notes.json', 'MD_FILE': core/'data/阅读进度.md',
             'IDENTITIES_FILE': core/'data/identities.json', 'AUDIT_FILE': core/'data/audit.log'}
    for field, path in paths.items():
        setattr(reader, field, str(path))
    reader._publication_instance = store
    book_path = core/'书库'/name
    write(book_path, source_raw, work)
    text = reader.norm_text(reader.decode_bytes(source_raw, strict=True))
    assert len(text) >= 2000
    revision = 'sha256:' + before_sha
    table = dict(nav.automatic(text, revision), name=name)
    starts_py = [len(text) * (i + 1) // (COUNT + 1) for i in range(COUNT)]
    heading_lines = []
    for row in table['items']:
        py = reader.text_position(text, row['offset'])[3]
        ending = text.find('\n', py)
        heading_lines.append((py, len(text) if ending < 0 else ending + 1))
    deletion = None
    for py in range(16, starts_py[0] - 16):
        fragment = text[py:py+8]
        line_start = text.rfind('\n', 0, py) + 1
        line_end = text.find('\n', py)
        line_end = len(text) if line_end < 0 else line_end
        if (fragment.strip() and '\n' not in fragment and line_end-line_start >= 20
                and not any(a < py+8 and py < b for a,b in heading_lines)):
            deletion = py
            break
    assert deletion is not None, 'No safe synthetic test deletion before the first synthetic anchor'
    removed = text[deletion:deletion+8]
    delta = drafts.units(PREFIX) - drafts.units(removed)
    assert delta > 0
    identities = ['user','004','retired_agent','unknown.id','legacy.agent','offset','updated']
    identities += ['synthetic_%02d' % i for i in range(len(identities), COUNT)]
    highlights, records = [], {}
    for i, py in enumerate(starts_py):
        quote = text[py:py+8]
        start = drafts.units(text[:py])
        ident, hid = identities[i], 'h_synthetic_%02d' % i
        thread = []
        for j in range(2):
            author = identities[(i+j+1) % len(identities)]
            thread.append(dict(who='user' if author=='user' else 'ai', name=author,
                               author_id=author, annotation_id='a_synthetic_%02d_%d' % (i,j),
                               highlight_id=hid, text='全合成验收评论，不来自任何生产记录。',
                               at=STAMP, created=STAMP, author_status='recorded', extension={'opaque': [i,j]}))
        highlights.append(dict(id=hid, highlight_id=hid, start=start, end=start+drafts.units(quote),
                               text=quote, book_hash=hashlib.sha256(text.encode()).hexdigest(),
                               owner=ident, author_id=ident, created=STAMP, thread=thread,
                               extension={'opaque': [i], 'legacy_label': 'synthetic-only'}))
        records[ident] = progress_record(text, start, revision, count=0 if i < 8 else i,
                                         extra={'opaque': [i], 'historic_id': ident})
    notes = dict(version=2, updated=STAMP, extension={'opaque': ['notes-root']}, books={
        name:dict(highlights=highlights, extension={'opaque':['book']}),
        'synthetic-other.txt':dict(highlights=[], extension={'preserve':'other-book'})})
    progress = dict(version=2, updated=STAMP, extension={'opaque':['progress-root']}, books={
        name:records, 'synthetic-other.txt':{'retired.other':{'offset':9,'updated':STAMP,'opaque':['keep']}}})
    library = dict(version=1, updated=STAMP, extension={'opaque':['library-root']}, books={
        name:dict(name=name,title=name[:-4],category='literature',category_source='manual',
                  category_updated_by='user',category_updated_at=STAMP,imported=STAMP,extension={'opaque':'book'}),
        'synthetic-other.txt':dict(category='humanities',extension={'preserve':'other-book'})})
    write(core/'data/notes.json', notes, work)
    write(core/'data/progress.json', progress, work)
    write(core/'data/library.json', library, work)
    identity_bytes = b'{"user":"synthetic-owner-only","004":"synthetic-agent-only"}'
    write(core/'data/identities.json', identity_bytes, work)
    write(nav.metadata_path(reader.DATA_DIR, name), table, work)
    draft = drafts.create(name, text, revision, table)
    queue = dict(schema_version=1, name=name, proposals=[])
    row, created = proposals.propose(queue, name, text, revision, table, '004', 'edit',
        dict(book_revision=revision, operation='insert_before', quote=text[:64], start=0,
             replacement=PREFIX, reason='全合成验收，仅修改私有副本。'))
    assert created
    draft, draft_text = proposals.apply(row, draft, text, revision,
        proposals.decision_for(row, dict(action='accept', replacement=PREFIX)))
    delete_start = drafts.units(PREFIX) + drafts.units(text[:deletion])
    draft, draft_text, _ = drafts.edit(draft, draft_text,
        dict(request_id='synthetic_realcopy_delete',base_revision=revision,expected_revision=draft['revision'],
             start=delete_start,end=delete_start+drafts.units(removed),quote=removed,replacement=''))
    assert not draft['chapter_issues']
    drafts.save(reader.DATA_DIR, name, draft)
    proposals.save(reader.DATA_DIR, name, queue)
    proposal_path = proposals.path_for(reader.DATA_DIR, name)
    proposal_sha = sha(proposal_path)
    originals = (name, copy.deepcopy(notes), copy.deepcopy(progress))
    controller = Controller(reader, store)
    with store.barrier(exclusive=True):
        preview = controller.preview(name)
    assert preview['can_commit']
    # A normal cooperating other-book writer arrives AFTER preview.
    notes['books']['synthetic-other.txt']['extension']['after_preview'] = True
    progress['books']['synthetic-other.txt']['retired.other']['after_preview'] = True
    with store.barrier():
        with reader._lock:
            write(core/'data/notes.json', notes, work)
            write(core/'data/progress.json', progress, work)
    pub_id = 'pub_realcopy_' + secrets.token_hex(16)
    with store.barrier(exclusive=True):
        result = controller.commit(name, preview['preview_digest'], pub_id)
    assert not result['already_applied'] and store.current(name)['generation'] == 1
    published_raw = book_path.read_bytes()
    assert published_raw.decode('utf-8') == draft_text
    published_notes = json.loads((core/'data/notes.json').read_text(encoding='utf-8'))
    published_progress = json.loads((core/'data/progress.json').read_text(encoding='utf-8'))
    assert_records(published_notes, published_progress, originals, draft_text, delta, 1)
    assert sum(len(h['thread']) for h in published_notes['books'][name]['highlights']) == COUNT*2
    assert published_notes['books']['synthetic-other.txt'] == notes['books']['synthetic-other.txt']
    assert published_progress['books']['synthetic-other.txt'] == progress['books']['synthetic-other.txt']
    assert (core/'data/identities.json').read_bytes() == identity_bytes and sha(proposal_path) == proposal_sha
    published_library = json.loads((core/'data/library.json').read_text(encoding='utf-8'))
    assert published_library['extension'] == library['extension']
    assert published_library['books']['synthetic-other.txt'] == library['books']['synthetic-other.txt']
    assert all(published_library['books'][name][k] == value for k,value in library['books'][name].items())
    assert proposals.public(row, drafts.read_metadata(reader.DATA_DIR,name))['status'] == 'accepted'
    source_unchanged()
    # Editing through a synthetic quoted range must block formal replacement.
    published_draft = drafts.path_for(reader.DATA_DIR, name).read_bytes()
    current_draft, current_text = drafts.load(reader.DATA_DIR, name, draft_text, store.current(name)['book_revision'])
    first = published_notes['books'][name]['highlights'][0]
    blocked_draft, _, _ = drafts.edit(current_draft, current_text,
        dict(request_id='synthetic_ambiguous_delete',base_revision=current_draft['base_revision'],
             expected_revision=current_draft['revision'],start=first['start'],end=first['end'],
             quote=first['text'],replacement=''))
    with store.barrier(exclusive=True):
        drafts.save(reader.DATA_DIR, name, blocked_draft)
        before_block = {p:sha(p) for p in (book_path,core/'data/notes.json',core/'data/progress.json',core/'data/library.json')}
        blocked = controller.preview(name)
        assert not blocked['can_commit'] and blocked['issues_total'] > 0
        try:
            controller.commit(name,blocked['preview_digest'],'pub_block_' + secrets.token_hex(16))
        except RequestError as error:
            assert error.status == 409
        else:
            raise AssertionError('Deleted synthetic anchor did not block publication')
        assert all(sha(path)==value for path,value in before_block.items())
        # Restore only this deliberately modified disposable synthetic draft.
        write(drafts.path_for(reader.DATA_DIR,name), published_draft, work)
    source_unchanged()
    # New reading activity after publication must survive inverse migration.
    with store.barrier():
        with reader._lock:
            nt = reader.load_notes()
            first_current = nt['books'][name]['highlights'][0]
            newer_comment = dict(who='ai',name='unknown.after_publish',author_id='unknown.after_publish',
                annotation_id='a_after_publish',highlight_id=first_current['id'],text='全合成发布后新增评论。',
                at=LATER,created=LATER,author_status='recorded',extension={'opaque':['new-comment']})
            first_current['thread'].append(newer_comment)
            reader.save_notes(nt)
            with reader._progress_guard():
                pr = reader.load_progress()
                user = pr['books'][name]['user']
                old_revision = user['revision']
                updated_user = progress_record(draft_text, originals[2]['books'][name][identities[1]]['offset']+delta,
                    store.current(name)['book_revision'],count=old_revision+1,updated=LATER,extra=user['extension'])
                # The listening point is a distinct saved position; retain it.
                updated_user['tts_offset'] = user['tts_offset']
                pr['books'][name]['user'].update(updated_user)
                reader.save_progress(pr)
    rollback_id = 'pub_rollback_' + secrets.token_hex(16)
    with store.barrier(exclusive=True):
        rollback_preview = controller.rollback_preview(name,pub_id)
        assert rollback_preview['can_commit']
        controller.rollback(name,pub_id,rollback_preview['preview_digest'],rollback_id)
    assert book_path.read_bytes() == source_raw
    restored_notes = json.loads((core/'data/notes.json').read_text(encoding='utf-8'))
    restored_progress = json.loads((core/'data/progress.json').read_text(encoding='utf-8'))
    assert_records(restored_notes, restored_progress, originals, text, 0, 2,
                   user_offset=originals[2]['books'][name][identities[1]]['offset'])
    assert restored_notes['books'][name]['highlights'][0]['thread'][-1] == newer_comment
    assert sum(len(h['thread']) for h in restored_notes['books'][name]['highlights']) == COUNT*2+1
    assert restored_notes['books']['synthetic-other.txt'] == notes['books']['synthetic-other.txt']
    assert restored_progress['books']['synthetic-other.txt'] == progress['books']['synthetic-other.txt']
    assert store.current(name)['generation'] == 2 and store.current(name)['action'] == 'rollback'
    assert proposals.public(row, drafts.read_metadata(reader.DATA_DIR,name))['status'] == 'accepted'
    assert sha(proposal_path) == proposal_sha and (core/'data/identities.json').read_bytes() == identity_bytes
    restored_library = json.loads((core/'data/library.json').read_text(encoding='utf-8'))
    assert restored_library['extension'] == library['extension']
    assert restored_library['books']['synthetic-other.txt'] == library['books']['synthetic-other.txt']
    assert all(restored_library['books'][name][k] == value for k,value in library['books'][name].items())
    source_unchanged()
    with store.barrier(exclusive=True):
        repeated = controller.commit(name,preview['preview_digest'],pub_id)
    assert repeated['already_applied'] and store.current(name)['generation'] == 2
    source_unchanged()
    assert sha(private_copy) == before_sha
    source_unchanged()
    final_highlights = restored_notes['books'][name]['highlights']
    authors = {h['author_id'] for h in final_highlights}
    authors.update(c['author_id'] for h in final_highlights for c in h['thread'])
    summary = dict(ok=1,production_records=0,input_bytes=len(source_raw),synthetic_highlights=COUNT,
        synthetic_comments=sum(len(h['thread']) for h in final_highlights),
        synthetic_progress_records=len(restored_progress['books'][name]),synthetic_identity_count=len(authors),
        initial_revision_zero_records=8,synthetic_approved_proposals=1,blocked_publications=1,
        completed_publications=1,completed_rollbacks=1,idempotent_retries=1,source_hash_checks=checks,
        source_sha256=before_sha,published_sha256=hashlib.sha256(published_raw).hexdigest(),
        rollback_sha256=sha(book_path))
    write(run_root/'summary.json',summary,work)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--book',required=True,help='Existing local TXT; opened read-only')
    parser.add_argument('--work-dir',type=Path,default=Path(__file__).resolve().parents[4]/'.private/publish-20261031/realcopy')
    args = parser.parse_args()
    original_sha = sha(Path(args.book))
    try:
        result = run(Path(args.book),args.work_dir)
        assert sha(Path(args.book)) == original_sha
    except BaseException:
        # Diagnostics stay private. Public output never contains book text,
        # synthetic quotes/records or user-project paths from a traceback.
        work = long_path(args.work_dir)
        work.mkdir(mode=0o700,parents=True,exist_ok=True)
        write(work/('failure-'+secrets.token_hex(6)+'.txt'),traceback.format_exc().encode('utf-8'),work)
        print(json.dumps(dict(ok=0,error_count=1,production_records=0,source_sha256=original_sha,
                              final_source_sha256=sha(Path(args.book)))))
        return 1
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__=='__main__':
    sys.exit(main())
