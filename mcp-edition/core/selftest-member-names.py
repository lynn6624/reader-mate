"""Display-name changes against disposable identities, HTTP/MCP and browsers."""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import member_store as store

ROOT = Path(__file__).resolve().parent
PW = 'synthetic administrator password'
TEXT = '第一章\n甲乙丙丁戊己。\n第二段合成正文。\n'


def expect(error, fn):
    try: fn()
    except error: pass
    else: raise AssertionError('expected ' + error.__name__)


def unit():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'identities.json'; p = str(path)
        original = b'{"user":"owner-test", "004":"legacy-agent"}'
        path.write_bytes(original)
        row = store.rename(p, '004', '  同读者😀  ', '004')
        assert row['nickname'] == '同读者😀' and row['kind'] == 'legacy'
        assert row['credential'] == dict(id='legacy-004', channel='legacy', status='active', revoked_at=None)
        backup = path.parent / '.token-backups' / ('identities-' + hashlib.sha256(original).hexdigest() + '.json')
        assert backup.read_bytes() == original
        store.rename(p, 'user', '书桌主人', 'user')
        assert store.authenticate(store.read(p), 'owner-test') == 'user'
        expect(FileExistsError, lambda: store.revoke(p, 'user'))
        added = store.create(p, '新成员', 'human', 'web'); ident = added['member']['id']
        doc = store.read(p); doc[store.META]['extra'] = {'keep': True}
        doc[store.META]['members'][ident]['custom'] = ['preserve']
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        store.rename(p, ident, '同读者😀', '新成员')  # Duplicate display names are valid.
        expected = copy.deepcopy(doc); expected[store.META]['members'][ident]['nickname'] = '同读者😀'
        assert store.read(p) == expected
        before = path.read_bytes()
        for name in ('', '   ', '字' * 65, 'bad\nname', 'bad\x7fname', '\ud800', None):
            expect(ValueError, lambda: store.rename(p, ident, name, '同读者😀'))
            assert path.read_bytes() == before
        expect(FileExistsError, lambda: store.rename(p, ident, '别处修改', '旧名字'))
        expect(ValueError, lambda: store.rename(p, ident, '名字', None))
        expect(KeyError, lambda: store.rename(p, 'missing', '名字', 'missing'))
        assert path.read_bytes() == before
        real = store.os.replace
        def fail_commit(src, dst):
            if str(dst) == p: raise OSError('synthetic failed commit')
            return real(src, dst)
        with patch.object(store.os, 'replace', side_effect=fail_commit):
            expect(OSError, lambda: store.rename(p, ident, '不应保存', '同读者😀'))
        assert path.read_bytes() == before
        # Both writers read the same expected name; the transaction permits one.
        workers = [subprocess.Popen([sys.executable, __file__, '--worker', p, ident, name])
                   for name in ('并发甲', '并发乙')]
        assert sorted(w.wait(timeout=20) for w in workers) == [0, 3]
        current = store.profiles(store.read(p))[ident]
        store.revoke(p, ident, current['credential']['id'])
        before_doc = store.read(p)
        store.rename(p, ident, '已撤销成员的新名字', current['nickname'])
        before_doc[store.META]['members'][ident]['nickname'] = '已撤销成员的新名字'
        assert store.read(p) == before_doc and not store.authenticate(store.read(p), added['token'])
        for raw in (b'{broken', b'{"user":"a","user":"b"}'):
            path.write_bytes(raw)
            expect(store.StoreError, lambda: store.rename(p, 'user', '名字', 'user'))
            assert path.read_bytes() == raw
    print('PASS names storage: legacy/user/revoked members, duplicate names, credentials and extensions preserved, validation, exact backup, failed commit and two-process conflict', flush=True)


def browser(web, portal, human):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        chrome = pw.chromium.launch(executable_path=os.environ.get('CHROME_TEST', '/usr/bin/chromium'), headless=True)
        try:
            for width in (1280, 390, 320):
                context = chrome.new_context(viewport={'width': width, 'height': 844},
                    is_mobile=width < 600, has_touch=width < 600)
                page = context.new_page(); errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(portal); page.locator('#adminPw').fill(PW); page.locator('#refreshBtn').click()
                row = page.locator('[data-member-id="004"]'); row.wait_for()
                old = row.locator('strong').inner_text()
                row.get_by_role('button', name='修改显示名', exact=True).click()
                row.get_by_label('新显示名').fill('取消修改'); row.get_by_role('button', name='取消', exact=True).click()
                assert row.locator('strong').inner_text() == old
                name = f'同读者😀{width}'
                for ident, nickname in (('004', name), ('user', f'书桌主人{width}'), (human['member']['id'], name)):
                    row = page.locator('[data-member-id="' + ident + '"]')
                    row.get_by_role('button', name='修改显示名', exact=True).click()
                    field = row.get_by_label('新显示名'); field.fill(nickname)
                    if ident == '004':
                        page.locator('#adminPw').fill('wrong password')
                        row.get_by_role('button', name='保存显示名', exact=True).click()
                        row.locator('.msg.err').wait_for()
                        assert field.input_value() == nickname and row.locator('strong').inner_text() == old
                        page.locator('#adminPw').fill(PW)
                    row.get_by_role('button', name='保存显示名', exact=True).click()
                    row.locator('form').wait_for(state='hidden')
                    assert row.locator('strong').inner_text() == nickname
                owner = page.locator('[data-member-id="user"]')
                assert owner.get_by_role('button', name='撤销凭据').count() == 0
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert page.evaluate('JSON.stringify(localStorage)') == '{}'
                if os.environ.get('NAME_SCREENSHOT_DIR'):
                    directory = Path(os.environ['NAME_SCREENSHOT_DIR']); directory.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(directory / f'members-{width}.png'), full_page=True)
                page.reload(); assert page.locator('#adminPw').input_value() == ''
                page.locator('#adminPw').fill(PW); page.locator('#refreshBtn').click()
                page.wait_for_function('(n)=>document.querySelector(\'[data-member-id="004"] strong\')?.textContent===n', arg=name)
                # Original credentials log in to the SAME identity and history after renaming.
                page.goto(web + '/?who_token=legacy-agent')
                page.wait_for_function('typeof state!=="undefined" && state.viewerId==="004" && state.book')
                assert page.evaluate('readerLabel("004")') == name
                assert page.evaluate('readerLabel("user")') == f'书桌主人{width}'
                page.locator('#reader mark.shared-hl').first.click()
                assert name in page.locator('#hlPanelTitle').inner_text()
                page.reload(); page.wait_for_function('typeof state!=="undefined" && state.viewerId==="004" && state.book')
                assert page.evaluate('readerLabel("004")') == name
                page.locator('#btnLib').click(); page.locator('[data-tab="progress"]').click()
                page.wait_for_function('(n)=>document.querySelector("#progressList").textContent.includes(n)', arg=name)
                assert not errors, errors
                context.close()
                context = chrome.new_context(viewport={'width': width, 'height': 844})
                page = context.new_page(); page.goto(web + '/?who_token=owner-test')
                page.wait_for_function('typeof state!=="undefined" && state.book')
                page.locator('#btnLib').click(); page.locator('#btnReview').click()
                page.locator('.rm-review-meta').first.wait_for()
                assert '004（' + name + '）' in page.locator('.rm-review-meta').first.inner_text()
                context.close()
        finally: chrome.close()
    print('PASS names UI: desktop/390px/320px rename, cancel, failure retry, duplicate names, refresh, legacy login, progress/annotations/review labels and no overflow', flush=True)


async def integration(with_browser):
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder); core = base / 'core'; core.mkdir(); data = core / 'data'; data.mkdir(); (core / '书库').mkdir()
        for filename in ('reader_server.py', 'member_store.py', 'book_drafts.py', 'revision_proposals.py',
                         'navigation.py', 'chapter_index.py', 'progress_catalog.py', 'mcp_server.py', 'MCP工具标准调用参考_只读.txt'):
            shutil.copy(ROOT / filename, core / filename)
        shutil.copytree(ROOT / 'web', core / 'web'); portal = base / 'token-mcp'; portal.mkdir()
        for filename in ('server.py', 'index.html'): shutil.copy(ROOT.parent / 'token-mcp' / filename, portal / filename)
        identities = data / 'identities.json'; identities.write_text('{"user":"owner-test","004":"legacy-agent"}')
        people = [store.create(str(identities), kind + channel, kind, channel)
                  for kind, channel in (('human', 'web'), ('agent', 'mcp'), ('agent', 'mcp-url'))]
        human, agent, urlagent = people
        original_doc = store.read(str(identities)); secrets = [v for v in original_doc.values() if isinstance(v, str)]
        book = core / '书库/test.txt'; book.write_text(TEXT, encoding='utf-8')
        (data / 'progress.json').write_text('{"books":{}}'); (data / 'notes.json').write_text('{"version":2,"books":{}}')
        salt = '01' * 16
        (data / '.token-admin.json').write_text(json.dumps(dict(salt=salt, hash=hashlib.sha256(bytes.fromhex(salt) + PW.encode()).hexdigest())))
        for filename, key in (('path', 'T' * 24), ('gpt', 'G' * 24)):
            (base / filename).write_text(key); (base / filename).chmod(0o600)
        sockets = [socket.socket() for _ in range(3)]
        for sock in sockets: sock.bind(('127.0.0.1', 0))
        wp, mp, tp = [sock.getsockname()[1] for sock in sockets]
        for sock in sockets: sock.close()
        web, mcp, url = [f'http://127.0.0.1:{p}' for p in (wp, mp, tp)]
        env = dict(os.environ, READER_CLOUD='1', READER_TOKEN='', READER_HOST='127.0.0.1', READER_PORT=str(wp),
            READER_MCP_PORT=str(mp), TOKEN_PORT=str(tp), TOKEN_HOST='127.0.0.1', READER_DATA_DIR=str(data), READER_API_URL=web,
            READER_MCP_PATH_KEY_FILE=str(base / 'path'), READER_MCP_004_KEY_FILE=str(base / 'gpt'), READER_MCP_EXTRA_HOST='mcp.example.com')
        log = (base / 'run.log').open('w+', encoding='utf-8')
        commands = [core / 'reader_server.py', core / 'mcp_server.py', portal / 'server.py']
        processes = [subprocess.Popen([sys.executable, str(command)], env=env, stdout=log, stderr=log) for command in commands]
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            async def ready():
                for _ in range(150):
                    try:
                        if ((await client.get(web + '/api/ping')).status_code == 200 and
                            (await client.get(url + '/api/status')).status_code == 200 and
                            (await client.post(mcp + '/mcp')).status_code == 404): return
                    except httpx.HTTPError: pass
                    if any(p.poll() is not None for p in processes): break
                    await asyncio.sleep(.05)
                log.seek(0); raise AssertionError(log.read())
            async def call(path, headers):
                async with httpx.AsyncClient(trust_env=False, headers=headers) as hc:
                    async with streamable_http_client(mcp + path, http_client=hc) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize(); result = await session.call_tool('list_books', {})
                            assert not result.isError, result
                            return json.loads(result.content[0].text)['viewer_id']
            async def rename(ident, nickname, expected, password=PW, **extra):
                return await client.post(url + '/api/members/rename', json=dict(password=password, member_id=ident,
                    nickname=nickname, expected_nickname=expected, **extra))
            try:
                await ready(); headers = {'X-Who-Token': 'legacy-agent'}
                revision = (await client.get(web + '/api/book?name=test.txt', headers=headers)).json()['book_revision']
                for route, body in (
                    ('/api/progress', dict(offset=4, expected_revision=0, book_revision=revision)),
                    ('/api/notes/highlight', dict(start=4, end=6, text='甲乙')),
                    ('/api/proposals/edit', dict(book_revision=revision, operation='replace', quote='丙丁', replacement='修订'))):
                    response = await client.post(web + route, headers=headers, json=dict(name='test.txt', **body))
                    assert response.status_code == 200, response.text
                protected = {p: p.read_bytes() for p in [book, data / 'progress.json', data / 'notes.json', *list((data / 'proposals').glob('*.json'))]}
                assert (await rename('004', '未授权', '004', password='legacy-agent')).status_code == 403
                assert (await client.post(url + '/api/members/rename', json={})).status_code == 403
                for nickname in ('', ' ' * 3, 'x' * 65, 'bad\nname'):
                    assert (await rename('004', nickname, '004')).status_code == 400
                assert (await rename('missing', '名字', 'missing')).status_code == 404
                for ident, row in store.profiles(original_doc).items():
                    response = await rename(ident, '<b>同读者</b>', row['nickname'], token='forged', kind='human', credential={'channel': 'web'})
                    assert response.status_code == 200, response.text
                    assert not any(secret in response.text for secret in secrets)
                    assert 'token' not in response.json() and 'url' not in response.json()
                before = identities.read_bytes()
                assert (await rename('004', '陈旧覆盖', '004')).status_code == 409
                assert identities.read_bytes() == before
                renamed = store.read(str(identities))
                for ident, row in original_doc[store.META]['members'].items(): renamed[store.META]['members'][ident]['nickname'] = row['nickname']
                assert renamed == original_doc
                # A display name equal to 'user' must never grant administrator rights.
                hid = human['member']['id']
                assert (await rename(hid, 'user', '<b>同读者</b>')).status_code == 200
                assert (await client.post(web + '/api/books/category', headers={'X-Who-Token': human['token']},
                    json=dict(name='test.txt', category='humanities'))).status_code == 403
                if with_browser: await asyncio.to_thread(browser, web, url, human)
                assert await call('/mcp/' + 'T' * 24, {'Authorization': 'Bearer ' + agent['token']}) == agent['member']['id']
                assert await call('/mcp/member/' + urlagent['token'], {'Host': 'mcp.example.com'}) == urlagent['member']['id']
                assert await call('/mcp/' + 'G' * 24, {'Host': 'mcp.example.com'}) == '004'
                for token, ident in (('owner-test', 'user'), ('legacy-agent', '004'), (human['token'], hid)):
                    response = await client.get(web + '/api/auth-check', headers={'X-Who-Token': token})
                    assert response.status_code == 200 and response.json()['who'] == ident
                assert (await client.get(web + '/api/auth-check', headers={'X-Who-Token': agent['token']})).status_code == 401
                assert (await client.post(url + '/api/credentials/revoke', json=dict(password=PW, member_id='user', credential_id='legacy-user'))).status_code == 409
                saved = identities.read_bytes()
                for process in processes: process.terminate(); process.wait(timeout=10)
                processes = [subprocess.Popen([sys.executable, str(command)], env=env, stdout=log, stderr=log) for command in commands]
                await ready()
                result = await client.post(url + '/api/members/list', json=dict(password=PW))
                assert result.status_code == 200 and not any(secret in result.text for secret in secrets)
                assert identities.read_bytes() == saved
                for path, raw in protected.items(): assert path.read_bytes() == raw, path.name
                final_doc = store.read(str(identities))
                for ident, row in original_doc[store.META]['members'].items(): final_doc[store.META]['members'][ident]['nickname'] = row['nickname']
                assert final_doc == original_doc
                log.flush(); assert not any(secret in (base / 'run.log').read_text() for secret in secrets)
                print('PASS names HTTP/MCP: admin-only, validation/conflicts, metadata-only changes, spoofed fields ignored, same legacy/Bearer/URL credentials, restart and protected history bytes', flush=True)
            finally:
                for process in processes:
                    if process.poll() is None: process.terminate()
                    process.wait(timeout=10)
                log.close()


if __name__ == '__main__':
    if '--worker' in sys.argv:
        try: store.rename(sys.argv[2], sys.argv[3], sys.argv[4], '同读者😀')
        except FileExistsError: sys.exit(3)
    else:
        parser = argparse.ArgumentParser(); parser.add_argument('--no-browser', action='store_true'); args = parser.parse_args()
        unit(); asyncio.run(integration(not args.no_browser))
