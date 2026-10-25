"""Progress preservation acceptance. Only disposable synthetic books and identities."""
import concurrent.futures
import copy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from unittest.mock import patch

import reader_server as reader

ROOT = Path(__file__).resolve().parent
TEXT = '第一章 合成测试\n甲😀乙丙丁戊己庚辛壬癸。\n第二章 回归测试\n合成正文结束。\n'


def configure(base):
    reader.DATA_DIR = str(base)
    reader.PROGRESS_FILE = str(Path(base) / 'progress.json')
    reader.MD_FILE = str(Path(base) / 'mirror.md')
    reader.LIBRARY_FILE = str(Path(base) / 'library.json')


def fixture():
    return {'version': 1, 'extension': {'keep': ['metadata']}, 'books': {
        'legacy.txt': {'offset': 3, 'revision': 0, 'anchor': '合成旧锚点', 'custom': [1, 2]},
        'test.txt': {'user': {'offset': 1, 'revision': 2, 'custom': {'keep': True}},
                     'agent': {'offset': 2}, 'old.id': {'offset': 3},
                     'oldid': {'offset': 4}, 'revoked': {'offset': 5},
                     'offset': {'offset': 6}, 'updated': {'offset': 7}}}}


def write_fixture(base):
    path = Path(base) / 'progress.json'
    path.write_bytes(('\ufeff' + json.dumps(fixture(), ensure_ascii=False, indent=2)).encode())
    return path


def expect_error(status, operation):
    try:
        operation()
    except reader.ProgressStoreError as exc:
        assert exc.status == status, (exc.status, status)
    else:
        raise AssertionError('Expected progress error %s' % status)


def unit():
    with tempfile.TemporaryDirectory() as base:
        configure(base)
        path = write_fixture(base)
        original = path.read_bytes()
        value = reader.load_progress()
        assert reader.load_progress() == value and path.read_bytes() == original
        assert value['books']['legacy.txt'] == {'user': fixture()['books']['legacy.txt']}
        assert value['books']['test.txt'] == fixture()['books']['test.txt']
        expected = copy.deepcopy(value)
        expected['books']['test.txt']['user']['offset'] = 9
        value['books']['test.txt']['user']['offset'] = 9
        assert reader.save_progress(value)
        saved = reader.load_progress()
        assert saved['books'] == expected['books'] and saved['extension'] == fixture()['extension']
        backups = list((Path(base) / 'progress-backups').glob('*.json'))
        assert len(backups) == 1 and backups[0].read_bytes() == original
        if os.name != 'nt':
            assert backups[0].stat().st_mode & 0o777 == 0o600
            assert path.stat().st_mode & 0o777 == 0o600
        reader.save_progress(saved)
        assert list((Path(base) / 'progress-backups').glob('*.json')) == backups
        stale = reader.load_progress()
        current = reader.load_progress()
        current['books']['test.txt']['agent']['offset'] = 11
        reader.save_progress(current)
        expect_error(409, lambda: reader.save_progress(stale))
        expect_error(409, lambda: reader.save_progress(dict(current)))
        assert reader.load_progress()['books']['test.txt']['agent']['offset'] == 11
        # Mirror failure must not report a failed canonical save or roll back a revision.
        with patch.object(reader, 'write_progress_md', side_effect=OSError('synthetic mirror failure')):
            current['books']['test.txt']['agent']['offset'] = 12
            assert reader.save_progress(current) is False
        committed = path.read_bytes()
        assert reader.load_progress()['books']['test.txt']['agent']['offset'] == 12
        assert reader.rebuild_progress_mirror() and path.read_bytes() == committed

        # A failed sole commit leaves exactly the old bytes and permits a safe retry.
        original_replace = reader.os.replace
        def fail_replace(source, target):
            if str(target) == str(path):
                raise OSError('synthetic commit failure')
            return original_replace(source, target)
        current['books']['test.txt']['agent']['offset'] = 13
        with patch.object(reader.os, 'replace', side_effect=fail_replace):
            expect_error(503, lambda: reader.save_progress(current))
        assert path.read_bytes() == committed
        assert not list(Path(base).glob('.progress-*'))
        assert reader.save_progress(current)

        malformed = [b'{broken', b'[]', b'{"books":[]}', b'{"books":{"bad":[]}}',
                     b'{"books":{"bad":{"agent":null}}}',
                     b'{"books":{},"books":{}}',
                     b'{"books":{"bad":{"agent":{"offset":1,"offset":2}}}}',
                     b'{"books":{"bad":{"agent":{"offset":NaN}}}}',
                     b'{"books":{"bad":{"offset":1,"updated":{"offset":2}}}}',
                     b'{"books":{"bad":{"offset":1,"agent":{"offset":2}}}}']
        for raw in malformed:
            path.write_bytes(raw)
            expect_error(503, reader.load_progress)
            assert path.read_bytes() == raw
            expect_error(409, lambda: reader.save_progress(current))
        # Read errors cannot become an empty progress store.
        with patch.object(reader, '_progress_raw', side_effect=reader.ProgressStoreError('synthetic read failure')):
            expect_error(503, reader.load_progress)

    with tempfile.TemporaryDirectory() as base:
        configure(base)
        path = write_fixture(base)
        original = path.read_bytes()
        current = reader.load_progress()
        with patch.object(reader.os, 'fsync', side_effect=OSError('synthetic backup failure')):
            expect_error(503, lambda: reader.save_progress(current))
        assert path.read_bytes() == original
        assert not list((Path(base) / 'progress-backups').glob('*.json'))
        # A preexisting incomplete/wrong backup must not be replaced silently.
        backup = Path(base) / 'progress-backups' / ('legacy-' + hashlib.sha256(original).hexdigest() + '.json')
        backup.write_bytes(b'bad backup')
        expect_error(503, lambda: reader.save_progress(current))
        assert path.read_bytes() == original and backup.read_bytes() == b'bad backup'
    print('PASS progress unit: lossless IDs/legacy/extras, corrupt refusal, exact private backup, CAS, write failure and mirror repair', flush=True)


def stale_worker(base, who, ready, go, results):
    configure(base)
    value = reader.load_progress()
    value['books']['test.txt'][who]['offset'] = 20
    ready.put(who)
    assert go.wait(10)
    try:
        reader.save_progress(value)
        results.put('committed')
    except reader.ProgressConflictError:
        # Retry only after rereading, preserving the other writer's update.
        current = reader.load_progress()
        current['books']['test.txt'][who]['offset'] = 20
        reader.save_progress(current)
        results.put('conflict_then_retry')


def interrupted_worker(base, phase, ready):
    configure(base)
    value = reader.load_progress()
    value['books']['test.txt']['user']['offset'] = 30
    original = reader._progress_atomic
    def paused(path, raw):
        canonical = path == reader.PROGRESS_FILE
        if canonical and phase == 'before':
            ready.set()
            time.sleep(30)
        original(path, raw)
        if canonical and phase == 'after':
            ready.set()
            time.sleep(30)
    reader._progress_atomic = paused
    reader.save_progress(value)


def processes():
    ctx = multiprocessing.get_context('spawn')
    with tempfile.TemporaryDirectory() as base:
        configure(base)
        write_fixture(base)
        ready, go, results = ctx.Queue(), ctx.Event(), ctx.Queue()
        workers = [ctx.Process(target=stale_worker, args=(base, who, ready, go, results))
                   for who in ('user', 'agent')]
        try:
            for worker in workers: worker.start()
            for _ in workers: ready.get(timeout=10)
            go.set()
            assert sorted(results.get(timeout=10) for _ in workers) == ['committed', 'conflict_then_retry']
            for worker in workers:
                worker.join(10)
                assert worker.exitcode == 0
            saved = reader.load_progress()['books']['test.txt']
            assert saved['user']['offset'] == saved['agent']['offset'] == 20
            assert saved['old.id'] == fixture()['books']['test.txt']['old.id']
        finally:
            for worker in workers:
                if worker.is_alive(): worker.terminate(); worker.join(5)
    for phase in ('before', 'after'):
        with tempfile.TemporaryDirectory() as base:
            configure(base)
            path = write_fixture(base)
            original = path.read_bytes()
            ready = ctx.Event()
            worker = ctx.Process(target=interrupted_worker, args=(base, phase, ready))
            worker.start()
            try: assert ready.wait(10)
            finally: worker.terminate(); worker.join(5)
            saved = reader.load_progress()
            assert saved['books']['test.txt']['user']['offset'] == (1 if phase == 'before' else 30)
            if phase == 'before': assert path.read_bytes() == original
            assert reader.rebuild_progress_mirror()  # exited writer did not retain its lock
            reader.save_progress(saved)
    print('PASS progress processes: stale writer rejected/retried; termination before/after sole commit preserves a complete version', flush=True)


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def integration():
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        for name in ('reader_server.py', 'member_store.py', 'book_drafts.py', 'navigation.py',
                     'chapter_index.py', 'progress_catalog.py'):
            shutil.copy(ROOT / name, base / name)
        shutil.copytree(ROOT / 'web', base / 'web')
        (base / '书库').mkdir(); (base / 'data').mkdir()
        book = base / '书库/test.txt'; book.write_text(TEXT, encoding='utf-8')
        identities = base / 'data/identities.json'
        identities.write_text(json.dumps({'user': 'test-owner', 'agent': 'test-agent'}))
        progress = base / 'data/progress.json'
        progress.write_text(json.dumps({'books': {'test.txt': {'user': {'custom': 'preserve'},
            'old.id': {'offset': 2}, 'oldid': {'offset': 3}}}}))
        original = {p: p.read_bytes() for p in (book, identities)}
        log = open(base / 'test.log', 'w+', encoding='utf-8')
        ports = [port(), port()]
        workers = []
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(route, data=None, who='user', server=0):
            headers = {'X-Who-Token': {'user': 'test-owner', 'agent': 'test-agent'}.get(who, 'invalid')}
            body = None if data is None else json.dumps(data).encode()
            req = urllib.request.Request('http://127.0.0.1:%d%s' % (ports[server], route), headers=headers, data=body)
            try:
                with opener.open(req, timeout=10) as response: return response.status, json.load(response)
            except urllib.error.HTTPError as exc: return exc.code, json.load(exc)
        try:
            for number in ports:
                env = dict(os.environ, READER_CLOUD='1', READER_PORT=str(number), READER_TOKEN='')
                workers.append(subprocess.Popen([sys.executable, str(base / 'reader_server.py')], env=env, stdout=log, stderr=log))
            for server in (0, 1):
                for _ in range(100):
                    try:
                        if request('/api/ping', server=server)[0] == 200: break
                    except OSError: pass
                    time.sleep(.05)
                else: raise AssertionError('HTTP server startup failed')
            revision = request('/api/book?name=test.txt')[1]['book_revision']
            payload = dict(name='test.txt', offset=3, book_revision=revision, expected_revision=0, who='user')
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda n: request('/api/progress', payload, ('user', 'agent')[n], n), (0, 1)))
            assert [r[0] for r in results] == [200, 200], results
            saved = json.loads(progress.read_text())['books']['test.txt']
            assert saved['user']['revision'] == saved['agent']['revision'] == 1
            assert saved['user']['custom'] == 'preserve'
            assert saved['old.id'] == {'offset': 2} and saved['oldid'] == {'offset': 3}
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda n: request('/api/progress', dict(payload, expected_revision=1), server=n), (0, 1)))
            assert sorted(r[0] for r in results) == [200, 409], results
            assert request('/api/progress', payload, who='bad')[0] == 401
            committed = progress.read_bytes()
            for raw in (b'{broken', b'{"books":[],"books":{}}'):
                progress.write_bytes(raw)
                assert request('/api/progress?name=test.txt')[0] == 503
                assert request('/api/progress', payload)[0] == 503
                assert request('/api/delete?name=test.txt', {})[0] == 503
                assert progress.read_bytes() == raw and book.read_bytes() == original[book]
            progress.write_bytes(committed)
            # The HTTP result must acknowledge a committed revision when its mirror fails.
            mirror = base / 'data/阅读进度.md'; mirror.unlink(); mirror.mkdir()
            status, saved = request('/api/progress', dict(payload, expected_revision=2))
            assert status == 200 and saved['revision'] == 3 and saved['mirror_updated'] is False
            assert request('/api/progress', dict(payload, expected_revision=2))[0] == 409
            mirror.rmdir()
            status, saved = request('/api/progress', dict(payload, expected_revision=3))
            assert status == 200 and saved['mirror_updated'] is True and mirror.is_file()
            for path, raw in original.items(): assert path.read_bytes() == raw
        finally:
            for worker in workers: worker.terminate()
            for worker in workers: worker.wait(timeout=10)
            log.close()
    print('PASS progress HTTP: two server processes, per-identity revisions, forged/invalid identity, corrupt refusal before deletion, mirror failure acknowledgement', flush=True)


if __name__ == '__main__':
    unit()
    processes()
    integration()
