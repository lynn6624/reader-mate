"""Synthetic HTTP publication benchmark: one actual reader_server child only.

Fixture setup reuses the frozen resource benchmark in the client parent. Large
fixture objects are released before starting the web child. Reported web RSS/HWM
come from that child's PID, never the parent setup high-water mark. This excludes
MCP, the token portal, the tunnel and other production slice processes.
"""
import argparse
import concurrent.futures
import gc
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


class BenchmarkFailure(AssertionError):
    pass


def require(value, code):
    if not value:
        raise BenchmarkFailure(code)


def file_sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_report(path, report):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    temporary.write_bytes((json.dumps(report, ensure_ascii=True, indent=2) + '\n').encode('ascii'))
    os.replace(temporary, path)


def process_memory(pid):
    """Read only the benchmark's known PID; no process enumeration/commands."""
    if sys.platform.startswith('linux'):
        fields = {}
        for line in Path('/proc', str(pid), 'status').read_text().splitlines():
            name, _, value = line.partition(':')
            if name in ('VmRSS', 'VmHWM'):
                fields[name] = int(value.split()[0])
        require('VmRSS' in fields and 'VmHWM' in fields, 'child_proc_memory_fields_missing')
        return dict(rss_bytes=fields['VmRSS'] * 1024, peak_rss_bytes=fields['VmHWM'] * 1024,
                    vm_rss_kib=fields['VmRSS'], vm_hwm_kib=fields['VmHWM'],
                    source='/proc/<benchmark_pid>/status VmRSS and VmHWM; Linux KiB')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        open_process = ctypes.windll.kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        handle = open_process(0x0400 | 0x0010, False, pid)
        require(bool(handle), 'child_memory_handle_failed')
        close = ctypes.windll.kernel32.CloseHandle; close.argtypes = [wintypes.HANDLE]
        try:
            counters = Counters(); counters.cb = ctypes.sizeof(counters)
            query = ctypes.windll.psapi.GetProcessMemoryInfo
            query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            require(bool(query(handle, ctypes.byref(counters), counters.cb)), 'child_memory_query_failed')
            return dict(rss_bytes=counters.WorkingSetSize, peak_rss_bytes=counters.PeakWorkingSetSize,
                        working_set_bytes=counters.WorkingSetSize, peak_working_set_bytes=counters.PeakWorkingSetSize,
                        vm_rss_kib=None, vm_hwm_kib=None,
                        source='GetProcessMemoryInfo(benchmark child); Windows working set, not Linux VmHWM')
        finally:
            close(handle)
    raise BenchmarkFailure('unsupported_child_memory_platform')


def run(args, report):
    source = args.core.resolve()
    if not (source / 'reader_server.py').is_file() and (source / 'core/reader_server.py').is_file():
        source /= 'core'
    require((source / 'reader_server.py').is_file(), 'source_core_missing')
    helper = source / 'selftest-publication-resources.py'
    require(helper.is_file(), 'frozen_fixture_helper_missing')
    sys.path.insert(0, str(source))
    spec = importlib.util.spec_from_file_location('publication_resource_fixture', helper)
    fixture_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture_module)
    modules = {key: importlib.import_module(name) for key, name in (
        ('reader', 'reader_server'), ('drafts', 'book_drafts'), ('nav', 'navigation'))}
    temporary_parent = args.work_dir.resolve() if args.work_dir else Path(tempfile.gettempdir()).resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    child = None
    with tempfile.TemporaryDirectory(prefix='pub-http-res-', dir=temporary_parent) as folder:
        workspace = Path(folder).resolve()
        require(workspace.parent == temporary_parent, 'temporary_directory_boundary_failed')
        core = workspace / 'core'; core.mkdir()
        report['temporary_workspace'] = str(workspace)
        copied = {}
        for path in sorted(source.glob('*.py')):
            if path.name.startswith('selftest'):
                continue
            digest = file_sha(path)
            shutil.copyfile(path, core / path.name)
            require(file_sha(core / path.name) == digest == file_sha(path), 'runtime_copy_changed')
            copied[path.name] = digest
        shutil.copytree(source / 'web', core / 'web', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        report['runtime_source_sha256'] = copied
        report['frozen_fixture_helper_sha256'] = file_sha(helper)
        report.update(current_phase='fixture_setup_in_parent', status='running')
        write_report(args.output, report)
        started = time.monotonic()
        fixture = fixture_module.make_fixture(core, args.units, args.edits, args.draft_bytes, modules)
        expected = dict(source_sha256=hashlib.sha256(fixture['source_bytes']).hexdigest(),
                        target_sha256=hashlib.sha256(fixture['target_text'].encode('utf-8')).hexdigest(),
                        notes=fixture['notes'], progress=fixture['progress'], operations=fixture['operations'])
        report['fixture'] = fixture['sizes']
        del fixture
        gc.collect()
        owner = secrets.token_urlsafe(32)
        (core / 'data/identities.json').write_bytes(json.dumps({'user': owner}).encode('ascii'))
        identity_hash = file_sha(core / 'data/identities.json')
        report['fixture_setup_parent_seconds'] = round(time.monotonic() - started, 6)
        report['client_parent_memory_after_fixture_release'] = process_memory(os.getpid())
        report['disk_after_fixture'] = fixture_module.disk_usage(core)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
        base = 'http://127.0.0.1:' + str(port)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('READER_', 'TOKEN_')) and key.upper() not in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')}
        env.update(READER_CLOUD='1', READER_HOST='127.0.0.1', READER_PORT=str(port), READER_TOKEN='',
                   PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                   NO_PROXY='127.0.0.1,localhost,::1')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(route, body=None, timeout=None):
            data = None if body is None else json.dumps(body, separators=(',', ':')).encode('ascii')
            req = urllib.request.Request(base + route, data=data,
                headers={'Content-Type': 'application/json', 'X-Who-Token': owner})
            try:
                with opener.open(req, timeout=timeout or args.request_timeout) as response:
                    require(response.status == 200, 'unexpected_http_status')
                    value = json.load(response)
            except urllib.error.HTTPError as error:
                raise BenchmarkFailure('http_status_' + str(error.code)) from None
            require(isinstance(value, dict) and value.get('ok') is True, 'http_response_not_ok')
            return value
        try:
            report.update(current_phase='web_startup', status='running')
            write_report(args.output, report)
            started = time.monotonic()
            child = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-B', str(core / 'reader_server.py')],
                cwd=core, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            report.update(web_child_pid=child.pid, web_children_started=1)
            deadline = time.monotonic() + args.startup_timeout
            while True:
                require(child.poll() is None, 'web_child_exited_during_startup')
                try:
                    request('/api/ping', timeout=1)
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    require(time.monotonic() < deadline, 'web_startup_timeout')
                    time.sleep(.05)
            report['web_startup_seconds'] = round(time.monotonic() - started, 6)
            baseline = process_memory(child.pid)
            report['web_baseline'] = baseline
            write_report(args.output, report)
            def phase(name, route, body):
                report.update(current_phase=name, status='running')
                write_report(args.output, report)
                print(json.dumps({'phase': name, 'status': 'start'}), flush=True)
                before = process_memory(child.pid)
                max_rss, max_hwm, samples = before['rss_bytes'], before['peak_rss_bytes'], 0
                started = time.monotonic()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(request, route, body)
                    while not pending.done():
                        require(child.poll() is None, 'web_child_exited_during_' + name)
                        memory = process_memory(child.pid)
                        max_rss = max(max_rss, memory['rss_bytes'])
                        max_hwm = max(max_hwm, memory['peak_rss_bytes'])
                        samples += 1
                        time.sleep(args.sample_interval)
                    value = pending.result()
                elapsed = time.monotonic() - started
                after = process_memory(child.pid)
                max_rss = max(max_rss, after['rss_bytes'])
                max_hwm = max(max_hwm, after['peak_rss_bytes'])
                row = dict(seconds=round(elapsed, 6), memory_before=before, memory_after_response=after,
                           sampled_max_rss_bytes=max_rss, child_cumulative_peak_rss_bytes=max_hwm,
                           sampled_rss_increase_over_baseline_bytes=max_rss - baseline['rss_bytes'],
                           peak_rss_increase_over_baseline_bytes=max_hwm - baseline['peak_rss_bytes'],
                           samples=samples, disk=fixture_module.disk_usage(core))
                report['phases'][name] = row
                write_report(args.output, report)
                print(json.dumps({'phase': name, 'status': 'complete', 'seconds': row['seconds'],
                    'sampled_max_rss_bytes': max_rss, 'child_cumulative_peak_rss_bytes': max_hwm,
                    'disk_logical_bytes': row['disk']['logical_bytes']}), flush=True)
                return value
            name = fixture_module.NAME; other = fixture_module.OTHER
            preview = phase('http_preview', '/api/publication/preview', {'name': name})
            require(preview.get('can_commit') is True, 'http_preview_blocked')
            publication = phase('http_commit', '/api/publication/commit',
                dict(name=name, preview_digest=preview['preview_digest'], request_id='http_resource_publish_0001'))
            require(file_sha(core / '书库' / name) == expected['target_sha256'], 'http_published_text_mismatch')
            rollback = phase('http_rollback_preview', '/api/publication/rollback-preview',
                dict(name=name, publication_id=publication['publication_id']))
            require(rollback.get('can_commit') is True, 'http_rollback_preview_blocked')
            phase('http_rollback', '/api/publication/rollback', dict(name=name, publication_id=publication['publication_id'],
                preview_digest=rollback['preview_digest'], request_id='http_resource_rollback_0001'))
            require(file_sha(core / '书库' / name) == expected['source_sha256'], 'http_original_bytes_not_restored')
            restored_notes = json.loads((core / 'data/notes.json').read_bytes())
            restored_progress = json.loads((core / 'data/progress.json').read_bytes())
            require(restored_notes['books'][other] == expected['notes']['books'][other], 'http_other_notes_changed')
            require(restored_progress['books'][other] == expected['progress']['books'][other], 'http_other_progress_changed')
            for old, new in zip(expected['notes']['books'][name]['highlights'], restored_notes['books'][name]['highlights']):
                require(all(old[key] == new[key] for key in ('id', 'start', 'end', 'text', 'author_id', 'owner', 'thread', 'extra')),
                        'http_note_preservation_failed')
            for ident, old in expected['progress']['books'][name].items():
                new = restored_progress['books'][name][ident]
                require(all(old[key] == new[key] for key in ('offset', 'tts_offset', 'updated', 'extra')) and new['revision'] == 2,
                        'http_progress_preservation_failed')
            restored = json.loads(modules['drafts'].path_for(core / 'data', name).read_bytes())
            require(restored['operations'] == expected['operations'], 'http_old_draft_operations_not_restored')
            require(file_sha(core / 'data/identities.json') == identity_hash, 'synthetic_identity_file_changed')
            require(all(file_sha(source / filename) == digest for filename, digest in copied.items()), 'source_changed_during_run')
            report['web_final_memory'] = process_memory(child.pid)
            report['web_peak_rss_bytes'] = max([report['web_final_memory']['peak_rss_bytes']] +
                [row['child_cumulative_peak_rss_bytes'] for row in report['phases'].values()])
            report['checks'] = dict(all_four_http_operations=True, original_crlf_bytes_restored=True,
                multi_identity_records_preserved=True, other_books_preserved=True, old_draft_operations_restored=True,
                synthetic_identity_file_unchanged=True, source_unchanged=True)
            report['disk_before_cleanup'] = fixture_module.disk_usage(core)
        finally:
            if child is not None:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        child.kill(); child.wait(timeout=15)
                        report['web_child_forced_kill'] = True
                report['web_child_exitcode_after_requested_stop'] = child.returncode
                report['web_child_stopped'] = child.poll() is not None
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
    parser.add_argument('--sample-interval', type=float, default=.05)
    parser.add_argument('--request-timeout', type=float, default=120)
    parser.add_argument('--startup-timeout', type=float, default=30)
    args = parser.parse_args()
    if args.output:
        args.output = args.output.resolve()
    report = dict(status='starting', synthetic_only=True, platform=platform.platform(),
        python=sys.version.split()[0], started_utc=datetime.now(timezone.utc).isoformat(), phases={},
        memory_scope='Actual isolated reader_server web child PID only; fixture/client parent excluded.',
        excluded_processes=['fixture/client parent', 'MCP server', 'token portal', 'tunnel', 'other production slice processes'],
        budget_scope='Add independently measured production non-web process RSS before assessing the production slice. No whole-slice headroom claim.',
        fixture_scope='Same near-limit fixture helper as the frozen Controller benchmark; legal metadata padding, not 200 maximum-sized edits.')
    started = time.monotonic()
    try:
        require(1 <= args.units <= 2_000_000 and 1 <= args.edits <= 200,
                'invalid_fixture_limits')
        require(1 <= args.draft_bytes <= 4 * 1024 * 1024 and .01 <= args.sample_interval <= 1,
                'invalid_measurement_parameters')
        require(args.request_timeout > 0 and args.startup_timeout > 0, 'invalid_timeout')
        run(args, report)
        report.update(status='passed', elapsed_seconds=round(time.monotonic() - started, 6))
        code = 0
    except Exception as exc:
        report.update(status='failed', elapsed_seconds=round(time.monotonic() - started, 6),
            error_type=type(exc).__name__,
            error_code=str(exc) if isinstance(exc, BenchmarkFailure) else 'exception_in_' + report.get('current_phase', 'setup'))
        code = 1
    write_report(args.output, report)
    print(json.dumps({'status': report['status'], 'elapsed_seconds': report['elapsed_seconds'],
        'web_peak_rss_bytes': report.get('web_peak_rss_bytes'), 'error_code': report.get('error_code')}), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
