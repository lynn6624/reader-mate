"""Single-process synthetic publication resource benchmark; no HTTP/network.

Defaults approach 2M UTF-16 units, 200 edits and the 4 MiB draft envelope.
The draft envelope includes explicitly labelled legal extension padding; this
does NOT claim all 200 edits can simultaneously use the maximum selection size.
Only generated TemporaryDirectory data are published/rolled back and removed.
An optional JSON report outside that directory survives normal cleanup.
"""
import argparse
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone


NAME = 'synthetic-resource-book.txt'
OTHER = 'synthetic-other-book.txt'
STAMP = '2026-01-01T00:00:00'


class BenchmarkFailure(AssertionError):
    pass


def require(condition, code):
    if not condition:
        raise BenchmarkFailure(code)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def memory_peak():
    if sys.platform.startswith('linux'):
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return dict(peak_resident_kib=value, vm_hwm_kib=value,
                    source='resource.getrusage(RUSAGE_SELF).ru_maxrss; Linux KiB')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        counters = Counters(); counters.cb = ctypes.sizeof(counters)
        get_current = ctypes.windll.kernel32.GetCurrentProcess
        get_current.restype = wintypes.HANDLE
        query = ctypes.windll.psapi.GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        require(bool(query(get_current(), ctypes.byref(counters), counters.cb)), 'memory_query_failed')
        return dict(peak_working_set_bytes=counters.PeakWorkingSetSize, vm_hwm_kib=None,
                    source='Windows GetProcessMemoryInfo.PeakWorkingSetSize; not Linux VmHWM')
    return dict(vm_hwm_kib=None, source='unsupported platform')


def disk_usage(root):
    logical = allocated = files = history = 0
    allocation_supported = True
    if root is not None and root.exists():
        for path in root.rglob('*'):
            if not path.is_file():
                continue
            info = path.stat(); files += 1; logical += info.st_size
            if hasattr(info, 'st_blocks'):
                allocated += info.st_blocks * 512
            else:
                allocation_supported = False
            if 'publications' in path.relative_to(root).parts:
                history += info.st_size
    return dict(logical_bytes=logical, allocated_bytes=allocated if allocation_supported else None,
                files=files, publication_history_bytes=history)


def report_to(path, report):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    temporary.write_bytes((json.dumps(report, ensure_ascii=True, indent=2) + '\n').encode('ascii'))
    os.replace(temporary, path)


def make_fixture(core, units_requested, edits_requested, draft_bytes_requested, modules):
    drafts, navigation, reader = modules['drafts'], modules['nav'], modules['reader']
    prefix = '广告' * 1999 + '\n'  # 3999 units: one large, valid front deletion
    heading = '第一章 合成资源基准\n'
    tail = '\n第二章 固定尾部\n' + ''.join(f'资源锚点{i:02}甲😀乙。\n' for i in range(28)) + '结束。\n'
    filler_units = units_requested - drafts.units(prefix + heading + tail)
    span = 64
    require(filler_units >= span * (edits_requested - 1), 'requested_text_too_small_for_edits')
    source_text = prefix + heading + '甲' * filler_units + tail
    target_text = heading + '乙' * (span * (edits_requested - 1)) + '甲' * (filler_units - span * (edits_requested - 1)) + tail
    source_bytes = source_text.replace('\n', '\r\n').encode('utf-8')
    revision = 'sha256:' + sha(source_bytes)
    source_nav = dict(navigation.automatic(source_text, revision), name=NAME,
                      resource_fixture={'synthetic': True, 'keep': 1})
    draft = drafts.create(NAME, source_text, revision, source_nav)
    operations = []
    for index in range(edits_requested):
        if index == 0:
            start, before, after = 0, prefix, ''
        else:
            start = drafts.units(heading) + (index - 1) * span
            before, after = '甲' * span, '乙' * span
        request = dict(base_revision=revision, expected_revision=index + 1,
                       start=start, end=start + drafts.units(before), quote=before, replacement=after)
        digest = sha(json.dumps(request, ensure_ascii=False, sort_keys=True).encode('utf-8'))
        operations.append(dict(id='resource_edit_' + str(index).zfill(3), start=start,
            end=request['end'], before=before, after=after, request_digest=digest,
            author_id='user', created='2026-01-01T00:00:00+00:00'))
    items = copy.deepcopy(source_nav['items']); target_raw = target_text.encode('utf-16-le')
    for item in items:
        item['offset'] -= drafts.units(prefix)
        item['quote'] = navigation.quote_at(target_raw, item['offset'])
    draft.update(operations=operations, revision=edits_requested + 1, items=items, cursor_offset=0)
    draft['resource_fixture'] = {'synthetic': True, 'padding_purpose': 'legal metadata envelope boundary', 'padding': ''}
    padding_bytes = draft_bytes_requested - len(encoded(draft))
    require(padding_bytes >= 0, 'draft_envelope_target_too_small')
    draft['resource_fixture']['padding'] = 'p' * padding_bytes
    draft_raw = encoded(draft)
    require(len(draft_raw) == draft_bytes_requested <= drafts.MAX_FILE, 'draft_envelope_size_mismatch')
    (core / 'data').mkdir(parents=True); (core / '书库').mkdir()
    (core / '书库' / NAME).write_bytes(source_bytes)
    (core / '书库' / OTHER).write_bytes(b'Synthetic unrelated book, unchanged.\n')
    notes, progress = [], {}
    authors = ['user', '004', 'retired.synthetic', 'unknown.synthetic']
    total = drafts.units(source_text)
    for index in range(28):
        quote = f'资源锚点{index:02}甲😀乙。'
        start = drafts.units(source_text[:source_text.index(quote)])
        ident = authors[index] if index < len(authors) else 'synthetic_member_' + str(index)
        notes.append(dict(id='resource_highlight_' + str(index), start=start,
            end=start + drafts.units(quote), text=quote, book_hash=sha(source_text.encode('utf-8')),
            author_id=ident, owner=ident, extra={'preserve': index},
            thread=[dict(annotation_id='resource_annotation_' + str(index), who='ai',
                name='retired.synthetic', author_id='retired.synthetic', text='合成评论', extra={'preserve': index})]))
        position = reader.text_position(source_text, start)
        progress[ident] = dict(offset=start, tts_offset=start, revision=0, book_revision=revision,
            anchor=reader.re.sub(r'\s+', ' ', source_text[position[3]:position[3] + 40]),
            chars=total, percent=start / total, paragraph_idx=position[0], char_offset=position[1],
            updated=STAMP, extra={'preserve': index})
    old_notes = {'version': 2, 'updated': STAMP, 'books': {NAME: {'highlights': notes}, OTHER: {'opaque': {'keep': 1}, 'highlights': []}}}
    old_progress = {'version': 1, 'books': {NAME: progress, OTHER: {'old.synthetic': {'offset': 4, 'extra': [1]}}}}
    (core / 'data/notes.json').write_bytes(encoded(old_notes))
    (core / 'data/progress.json').write_bytes(encoded(old_progress))
    (core / 'data/library.json').write_bytes(encoded({'version': 1, 'books': {NAME: {'category': 'literature'}, OTHER: {'keep': 11}}}))
    nav_path = navigation.metadata_path(core / 'data', NAME); nav_path.parent.mkdir()
    nav_path.write_bytes(encoded(source_nav))
    drafts.save(core / 'data', NAME, draft)
    loaded, replayed = drafts.load(core / 'data', NAME, source_text, revision)
    require(replayed == target_text and len(loaded['operations']) == edits_requested, 'fixture_replay_failed')
    del loaded, replayed
    # Prove the metadata limit refuses a write; never put an oversized file live.
    oversized = dict(draft, resource_fixture={'synthetic': True, 'padding': 'p' * (drafts.MAX_FILE + 1)})
    try:
        drafts.save(core / 'data', NAME, oversized)
    except drafts.DraftError as error:
        require(error.status == 413, 'wrong_over_limit_status')
    else:
        raise BenchmarkFailure('oversized_draft_not_rejected')
    require(drafts.path_for(core / 'data', NAME).read_bytes() == draft_raw, 'oversized_write_changed_live_draft')
    return dict(source_bytes=source_bytes, target_text=target_text, notes=old_notes,
                progress=old_progress, operations=operations,
                sizes=dict(source_bytes=len(source_bytes), source_utf16_units=total,
                    target_utf16_units=drafts.units(target_text), operations=edits_requested,
                    largest_before_units=max(drafts.units(op['before']) for op in operations),
                    regular_replace_units=span, draft_bytes=len(draft_raw), draft_limit_bytes=drafts.MAX_FILE,
                    draft_extension_padding_bytes=padding_bytes,
                    draft_fraction_of_limit=len(draft_raw) / drafts.MAX_FILE))


def run(args, report):
    source_core = args.core.resolve()
    if not (source_core / 'reader_server.py').is_file() and (source_core / 'core/reader_server.py').is_file():
        source_core /= 'core'
    require((source_core / 'reader_server.py').is_file(), 'source_core_missing')
    names = ('reader_server.py', 'book_drafts.py', 'navigation.py', 'publication_mapping.py',
             'publication_plan.py', 'publication_store.py', 'publication_service.py')
    hashes = {name: sha((source_core / name).read_bytes()) for name in names}
    report['source_sha256'] = hashes
    sys.path.insert(0, str(source_core))
    modules = {key: importlib.import_module(name) for key, name in (
        ('reader', 'reader_server'), ('drafts', 'book_drafts'), ('nav', 'navigation'),
        ('store', 'publication_store'), ('service', 'publication_service'))}
    reader = modules['reader']
    temporary_parent = args.work_dir.resolve() if args.work_dir else Path(tempfile.gettempdir()).resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pub-res-', dir=temporary_parent) as folder:
        workspace = Path(folder).resolve()
        require(workspace.parent == temporary_parent, 'temporary_directory_boundary_failed')
        core = workspace / 'core'; core.mkdir()
        report['temporary_workspace'] = str(workspace)
        def phase(name, action):
            report.update(current_phase=name, status='running')
            report_to(args.output, report)
            print(json.dumps({'phase': name, 'status': 'start'}), flush=True)
            started = time.monotonic()
            result = action()
            row = dict(seconds=round(time.monotonic() - started, 6), memory=memory_peak(), disk=disk_usage(core))
            report['phases'][name] = row
            report_to(args.output, report)
            print(json.dumps({'phase': name, 'status': 'complete', **row}), flush=True)
            return result
        fixture = phase('fixture_and_limit_validation', lambda: make_fixture(core, args.units, args.edits, args.draft_bytes, modules))
        report['fixture'] = fixture['sizes']
        reader.BASE = str(core); reader.BOOKS_DIR = str(core / '书库'); reader.DATA_DIR = str(core / 'data')
        for field, filename in (('NOTES_FILE', 'notes.json'), ('PROGRESS_FILE', 'progress.json'),
                                ('LIBRARY_FILE', 'library.json'), ('MD_FILE', '阅读进度.md'),
                                ('IDENTITIES_FILE', 'identities.json'), ('AUDIT_FILE', 'audit.log')):
            setattr(reader, field, str(core / 'data' / filename))
        store = modules['store'].Store(core)
        controller = modules['service'].Controller(reader, store)
        def exclusive(action):
            with store.barrier(exclusive=True):
                return action()
        preview = phase('preview', lambda: exclusive(lambda: controller.preview(NAME)))
        require(preview['can_commit'] is True, 'preview_blocked')
        publication = phase('publish', lambda: exclusive(lambda: controller.commit(NAME, preview['preview_digest'], 'resource_publish_0001')))
        require((core / '书库' / NAME).read_bytes() == fixture['target_text'].encode('utf-8'), 'published_text_mismatch')
        current_notes = json.loads((core / 'data/notes.json').read_bytes())
        current_progress = json.loads((core / 'data/progress.json').read_bytes())
        require(current_notes['books'][OTHER] == fixture['notes']['books'][OTHER], 'publication_other_notes_changed')
        require(current_progress['books'][OTHER] == fixture['progress']['books'][OTHER], 'publication_other_progress_changed')
        require(all(row['revision'] == 1 for row in current_progress['books'][NAME].values()), 'publication_progress_revision_failed')
        rollback_preview = phase('rollback_preview', lambda: exclusive(lambda: controller.rollback_preview(NAME, publication['publication_id'])))
        require(rollback_preview['can_commit'] is True, 'rollback_preview_blocked')
        phase('rollback', lambda: exclusive(lambda: controller.rollback(NAME, publication['publication_id'], rollback_preview['preview_digest'], 'resource_rollback_0001')))
        require((core / '书库' / NAME).read_bytes() == fixture['source_bytes'], 'rollback_original_bytes_mismatch')
        restored_notes = json.loads((core / 'data/notes.json').read_bytes())
        restored_progress = json.loads((core / 'data/progress.json').read_bytes())
        require(restored_notes['books'][OTHER] == fixture['notes']['books'][OTHER], 'rollback_other_notes_changed')
        require(restored_progress['books'][OTHER] == fixture['progress']['books'][OTHER], 'rollback_other_progress_changed')
        for old, new in zip(fixture['notes']['books'][NAME]['highlights'], restored_notes['books'][NAME]['highlights']):
            require(all(old[key] == new[key] for key in ('id', 'start', 'end', 'text', 'author_id', 'owner', 'thread', 'extra')), 'rollback_note_preservation_failed')
        for ident, old in fixture['progress']['books'][NAME].items():
            new = restored_progress['books'][NAME][ident]
            require(all(old[key] == new[key] for key in ('offset', 'tts_offset', 'updated', 'extra')) and new['revision'] == 2,
                    'rollback_progress_preservation_failed')
        restored = json.loads(modules['drafts'].path_for(core / 'data', NAME).read_bytes())
        require(restored['operations'] == fixture['operations'], 'rollback_draft_operations_failed')
        report['checks'] = dict(preview=True, published=True, rollback_preview=True, rollback=True,
            original_crlf_bytes_restored=True, other_books_preserved=True, identities_and_threads_preserved=True,
            oversized_draft_rejected=True, source_unchanged=all(sha((source_core / name).read_bytes()) == digest for name, digest in hashes.items()))
        report['disk_before_cleanup'] = disk_usage(core)
        report['peak_memory'] = memory_peak()
    require(not workspace.exists(), 'temporary_workspace_not_removed')
    report['temporary_workspace_removed'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--units', type=int, default=1_999_900)
    parser.add_argument('--edits', type=int, default=200)
    parser.add_argument('--draft-bytes', type=int, default=4 * 1024 * 1024 - 8192)
    args = parser.parse_args()
    if args.output:
        args.output = args.output.resolve()
    report = dict(status='starting', synthetic_only=True, single_process=True,
        platform=platform.platform(), python=sys.version.split()[0],
        started_utc=datetime.now(timezone.utc).isoformat(), phases={},
        memory_measurement='Cumulative process high-water mark, including fixture setup and real Controller+Store I/O; not per-phase deltas.',
        scope='Synthetic fixed-boundary fixture; extension padding is not a claim that all edits reach maximum per-edit selection size.')
    started = time.monotonic()
    try:
        require(1 <= args.edits <= 200 and 1 <= args.units <= 2_000_000,
                'invalid_requested_fixture_limits')
        require(1 <= args.draft_bytes <= 4 * 1024 * 1024, 'invalid_requested_draft_bytes')
        run(args, report)
        report.update(status='passed', elapsed_seconds=round(time.monotonic() - started, 6))
        code = 0
    except Exception as exc:
        report.update(status='failed', elapsed_seconds=round(time.monotonic() - started, 6),
                      error_type=type(exc).__name__, error_code=str(exc) if isinstance(exc, BenchmarkFailure) else 'exception_in_' + report.get('current_phase', 'setup'))
        code = 1
    report_to(args.output, report)
    print(json.dumps({'status': report['status'], 'elapsed_seconds': report['elapsed_seconds'],
                      'peak_memory': report.get('peak_memory'), 'error_code': report.get('error_code')}), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
