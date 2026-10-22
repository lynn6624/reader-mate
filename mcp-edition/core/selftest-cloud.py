"""Integration checks use disposable data and actual HTTP/MCP servers, not your bookshelf."""
import asyncio, json, os, pathlib, shutil, socket, subprocess, sys, tempfile, time
import urllib.request, urllib.error
import httpx
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = pathlib.Path(__file__).parent

def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]

async def run():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        for filename in ('reader_server.py', 'mcp_server.py', 'chapter_index.py'):
            shutil.copy(ROOT / filename, base / filename)
        guide_name = 'MCP工具标准调用参考_只读.txt'
        shutil.copy(ROOT / guide_name, base / guide_name)
        shutil.copytree(ROOT / 'web', base / 'web')
        (base / 'data').mkdir(); (base / '书库').mkdir()
        (base / 'state').mkdir()
        path_key = 'T' * 24
        key_file = base / 'state' / 'mcp-path-key.txt'
        key_file.write_text(path_key, encoding='ascii')
        key_file.chmod(0o600)
        chatgpt_key = 'G' * 24
        chatgpt_key_file = base / 'state' / 'mcp-004-url-key.txt'
        chatgpt_key_file.write_text(chatgpt_key, encoding='ascii')
        chatgpt_key_file.chmod(0o600)
        identities = {n: 'test-' + n for n in ('user', 'op001', 'op004', '004')}
        (base / 'data' / 'identities.json').write_text(json.dumps(identities))
        (base / '书库' / 'test.txt').write_text('第一章\n你好😀一起读书。\n第二段。', encoding='utf8')
        shutil.copy(ROOT / guide_name, base / '书库' / guide_name)
        (base / 'data' / 'progress.json').write_text(json.dumps({
            'version':1, 'books':{'test.txt':{
                'offset':4, 'anchor':'你好😀一起读书。 第二段。',
            'updated':'2026-10-14T00:00:00'}}}, ensure_ascii=False), encoding='utf-8')
        hp, mp = port(), port()
        env = dict(os.environ, READER_CLOUD='1', READER_PORT=str(hp), READER_MCP_PORT=str(mp),
                   READER_API_URL='http://127.0.0.1:' + str(hp),
                   READER_MCP_PATH_KEY_FILE=str(key_file),
                   READER_MCP_004_KEY_FILE=str(chatgpt_key_file))
        logs = open(base / 'test.log', 'w+')
        processes = [subprocess.Popen([sys.executable, str(base / f)], env=env, stdout=logs, stderr=logs)
                     for f in ('reader_server.py', 'mcp_server.py')]
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                for _ in range(100):
                    try:
                        a = await client.get(f'http://127.0.0.1:{hp}/api/ping')
                        b = await client.post(f'http://127.0.0.1:{mp}/mcp')
                        if a.status_code == 200 and b.status_code == 404: break
                    except httpx.HTTPError: pass
                    await asyncio.sleep(.1)
                else: raise AssertionError('Servers did not start')
                assert a.json()['extensions'] == []
                assert (await client.get(f'http://127.0.0.1:{hp}/kindle',
                    headers={'X-Who-Token':'test-user'})).status_code == 404
                assert (await client.post(f'http://127.0.0.1:{hp}/api/kindle/pairings',
                    headers={'X-Who-Token':'test-user'})).status_code == 404
                assert '<div id="kindlePairing"' not in (await client.get(f'http://127.0.0.1:{hp}/')).text
                assert (await client.get(f'http://127.0.0.1:{hp}/api/books')).status_code == 401
                legacy = (await client.get(f'http://127.0.0.1:{hp}/api/reading-progress',
                    params={'name':'test.txt'}, headers={'X-Who-Token':'test-user'})).json()
                assert legacy['progress']['location_status'] == 'legacy_anchor_verified'
                assert legacy['progress']['revision'] == 0
                assert (await client.get(f'http://127.0.0.1:{hp}/api/books', headers={'X-Who-Token':'bad'})).status_code == 401
                assert (await client.post(f'http://127.0.0.1:{hp}/api/delete?name=test.txt', headers={'X-Who-Token':'test-op001'})).status_code == 403
                books = (await client.get(f'http://127.0.0.1:{hp}/api/books', headers={'X-Who-Token':'test-user'})).json()
                assert guide_name in json.dumps(books, ensure_ascii=False)
                assert (await client.post(f'http://127.0.0.1:{hp}/api/delete',
                    params={'name':guide_name}, headers={'X-Who-Token':'test-user'})).status_code == 403
                assert (await client.post(f'http://127.0.0.1:{hp}/api/import',
                    params={'name':guide_name}, headers={'X-Who-Token':'test-user'},
                    content=b'replacement')).status_code == 403
                assert (await client.post(f'http://127.0.0.1:{hp}/api/books/category',
                    json={'name':guide_name, 'category':'humanities'},
                    headers={'X-Who-Token':'test-user'})).status_code == 200
                assert (await client.post(f'http://127.0.0.1:{mp}/mcp/{path_key}')).status_code == 401
                assert (await client.post(f'http://127.0.0.1:{mp}/mcp', headers={'Authorization':'Bearer test-user'})).status_code == 404
                assert (await client.post(f'http://127.0.0.1:{mp}/mcp/{chatgpt_key}')).status_code == 404
                assert (await client.post(f'http://127.0.0.1:{mp}/mcp/{chatgpt_key}',
                    headers={'Host':'mcp.example.com.evil.example'})).status_code == 404
            async def visit(who, create=False):
                headers = {'Authorization':'Bearer test-' + who}
                if who == 'op004':
                    headers['Host'] = 'mcp.example.com'
                async with httpx.AsyncClient(trust_env=False, headers=headers) as hc:
                    async with streamable_http_client(f'http://127.0.0.1:{mp}/mcp/{path_key}', http_client=hc) as (r,w,_):
                        async with ClientSession(r,w) as session:
                            await session.initialize()
                            tools = await session.list_tools(); assert len(tools.tools) == 12
                            async def call(name, args):
                                result = await session.call_tool(name, args)
                                assert not result.isError, result
                                return result.structuredContent or json.loads(result.content[0].text)
                            guide = await call('get_calling_guide', {})
                            assert guide['name'] == guide_name and 'update_progress' in guide['text']
                            listing = await call('list_books', {})
                            assert len(listing['books']) == 2 and len(listing['categories']) == 6
                            list_schema = next(t.inputSchema for t in tools.tools if t.name == 'list_books')
                            assert 'humanities' in list_schema['properties']['category']['enum']
                            filtered = await call('list_books', {'category':'humanities'})
                            assert [book['name'] for book in filtered['books']] == [guide_name]
                            invalid = await session.call_tool('list_books', {'category':'invented'})
                            assert invalid.isError
                            book = await call('read_book', {'name':'test.txt', 'offset':0, 'length':7})
                            assert book['text'] == '第一章\n你好' and book['next_offset'] == 6
                            assert list(book)[-1] == 'reminder' and 'update_progress' in book['reminder']
                            before = await call('get_reading_progress', {'name':'test.txt'})
                            revision = before['revision']
                            saved = await call('update_progress', {'name':'test.txt',
                                'offset': 4 if who == 'op001' else 6, 'expected_revision': revision})
                            assert saved['revision'] == revision + 1
                            own = await call('get_reading_progress', {'name':'test.txt'})
                            assert own['progress']['revision'] == saved['revision']
                            assert own['progress']['location_status'] == 'ok'
                            assert (own['progress']['paragraph_idx'], own['progress']['char_offset']) == (
                                (1, 0) if who == 'op001' else (1, 2))
                            shared = await call('list_reading_progress', {'name':'test.txt'})
                            assert any(p['reader_id'] == who for p in shared['progress'])
                            if not create and who == 'op004':
                                peer = await call('get_reading_progress', {
                                    'name':'test.txt', 'reader_id':'op001'})
                                assert peer['progress']['offset'] == 4
                                notes = await call('list_comments', {'name':'test.txt'})
                                chapter = await call('get_highlights', {'name':'test.txt', 'chapter_id':'第一章'})
                                assert any(h['author_id'] == 'op001' for h in chapter['highlights'])
                                anns = await call('get_annotations', {'name':'test.txt', 'highlight_id':notes['highlights'][0]['id']})
                                assert anns['annotations'] == []
                                await call('add_comment', {'name':'test.txt', 'highlight_id':notes['highlights'][0]['id'], 'text':'来自004'})
                            if create:
                                h = await call('add_highlight', {'name':'test.txt','start':4,'end':6,'text':'你好'})
                                return h['id']
                            return await call('list_comments', {'name':'test.txt'})
            hid = await visit('op001', True)
            # ChatGPT sends no fixed Bearer token. The grey URL key maps only to 004,
            # even if a client tries to supply a different identity's Bearer.
            async with httpx.AsyncClient(trust_env=False, headers={
                    'Host':'mcp.example.com',
                    'Authorization':'Bearer test-op001'}) as hc:
                async with streamable_http_client(
                        f'http://127.0.0.1:{mp}/mcp/{chatgpt_key}', http_client=hc) as (r,w,_):
                    async with ClientSession(r,w) as session:
                        await session.initialize()
                        assert len((await session.list_tools()).tools) == 12
                        result = await session.call_tool('get_highlights', {
                            'name':'test.txt', 'chapter_id':'第一章'})
                        assert not result.isError, result
                        chapter = result.structuredContent or json.loads(result.content[0].text)
                        assert chapter['viewer_id'] == '004'
                        assert any(h['author_id'] == 'op001' for h in chapter['highlights'])
                        result = await session.call_tool('update_progress', {'name':'test.txt', 'offset':4})
                        assert not result.isError, result
            await asyncio.gather(visit('op001'), visit('op004'))
            async with httpx.AsyncClient(trust_env=False) as hc:
                url = f'http://127.0.0.1:{hp}'
                def hdr(n): return {'X-Who-Token':'test-' + n}
                resp = await hc.post(url+'/api/notes/comment', headers=hdr('op001'), json={'name':'test.txt','id':hid,'who':'ai','name_ai':'op004','text':'spoof'})
                assert resp.status_code == 403
                resp = await hc.post(url+'/api/notes/comment', headers=hdr('op001'), json={'name':'test.txt','id':hid,'who':'ai','text':'来自001'})
                assert resp.status_code == 200
                resp = await hc.post(url+'/api/notes/comment/edit', headers=hdr('op004'), json={'name':'test.txt','id':hid,'index':1,'text':'改别人的话'})
                assert resp.status_code == 403
                resp = await hc.delete(url+f'/api/notes/highlight?name=test.txt&id={hid}', headers=hdr('op004'))
                assert resp.status_code == 403
                notes = (await hc.get(url+'/api/notes?name=test.txt', headers=hdr('user'))).json()
                assert [c['name'] for c in notes['highlights'][0]['thread']] == ['op004', 'op001']
                unchanged = (await hc.get(url+'/api/notes', params={
                    'name':'test.txt', 'since':notes['revision']}, headers=hdr('user'))).json()
                assert unchanged['not_modified'] and 'highlights' not in unchanged
                assert notes['viewer_id'] == 'user'
                assert notes['highlights'][0]['highlight_id'] == hid
                assert all(c['annotation_id'] and c['highlight_id'] == hid and c['author_id'] for c in notes['highlights'][0]['thread'])
                book_text = '第一章\n你好😀一起读书。\n第二段。'
                def quote(a,b): return book_text.encode('utf-16-le')[a*2:b*2].decode('utf-16-le')
                async def add(who, a, b, **extra):
                    resp = await hc.post(url+'/api/notes/highlight', headers=hdr(who), json={
                        'name':'test.txt','start':a,'end':b,'text':quote(a,b), **extra})
                    assert resp.status_code == 200, resp.text
                    return resp.json()['id']
                h002 = await add('user', 4, 6, author_id='op004')
                h004_same = await add('op004', 4, 6)
                h004_other = await add('op004', 9, 11)
                h001_overlap = await add('op001', 5, 9)
                assert len({h002,h004_same,h004_other,h001_overlap,hid}) == 5
                all_notes = (await hc.get(url+'/api/notes', params={'name':'test.txt','chapter_id':'第一章','limit':2}, headers=hdr('op004'))).json()
                assert all_notes['total'] == 5 and len(all_notes['highlights']) == 2 and all_notes['next_offset'] == 2
                assert all(h['chapter_id'] == '第一章' and h['location_status'] == 'ok' for h in all_notes['highlights'])
                own = (await hc.get(url+'/api/notes', params={'name':'test.txt','chapter_id':'第一章','author_id':'op004'}, headers=hdr('user'))).json()
                assert {h['id'] for h in own['highlights']} == {h004_same,h004_other}
                other = (await hc.get(url+'/api/notes', params={'name':'test.txt','chapter_id':'第一章','author_id':'user'}, headers=hdr('op004'))).json()
                assert [h['id'] for h in other['highlights']] == [h002]
                resp = await hc.post(url+'/api/notes/highlight', headers=hdr('op004'), json={
                    'name':'test.txt','start':4,'end':6,'text':'伪造','author_id':'user'})
                assert resp.status_code == 409
                resp = await hc.post(url+'/api/notes/comment', headers=hdr('op004'), json={
                    'name':'test.txt','id':h002,'who':'ai','text':'004在002划线下留言','author_id':'user'})
                assert resp.status_code == 200 and resp.json()['annotation']['author_id'] == 'op004', (resp.status_code, resp.text)
                anns = (await hc.get(url+'/api/notes/annotations', params={'name':'test.txt','highlight_id':h002}, headers=hdr('op004'))).json()
                assert anns['annotations'][0]['author_id'] == 'op004'
                resp = await hc.post(url+'/api/notes/highlight', headers=hdr('user'), json={
                    'name':'test.txt','id':h002,'start':5,'end':6,'text':quote(5,6)})
                assert resp.status_code == 409
                resp = await hc.delete(url+f'/api/notes/highlight?name=test.txt&id={h002}', headers=hdr('user'))
                assert resp.status_code == 409
                resp = await hc.delete(url+f'/api/notes/highlight?name=test.txt&id={h004_other}', headers=hdr('user'))
                assert resp.status_code == 403
                resp = await hc.delete(url+f'/api/notes/highlight?name=test.txt&id={h004_other}', headers=hdr('op004'))
                assert resp.status_code == 200
                assert (await hc.get(url+'/api/notes', params={'name':'test.txt'}, headers=hdr('bad'))).status_code == 401
                assert (await hc.get(url+'/api/notes/annotations', params={'name':'test.txt','highlight_id':h002}, headers=hdr('bad'))).status_code == 401
                pr = (await hc.get(url+'/api/progress', headers=hdr('user'))).json()['progress']['books']['test.txt']
                assert pr['op001']['offset'] == 4 and pr['op004']['offset'] == 6
                assert pr['004']['offset'] == 4
                current_book = (await hc.get(url+'/api/book?name=test.txt', headers=hdr('op001'))).json()
                assert current_book['book_revision'].startswith('sha256:')
                emoji_end = await hc.post(url+'/api/progress', headers=hdr('user'), json={
                    'name':'test.txt', 'offset':8, 'expected_revision':0,
                    'book_revision':current_book['book_revision']})
                assert emoji_end.status_code == 200 and emoji_end.json()['paragraph_idx'] == 1
                assert emoji_end.json()['char_offset'] == 4
                current_revision = pr['op001']['revision']
                # A forged author cannot redirect a write; explicit backward movement is valid.
                moved = await hc.post(url+'/api/progress', headers=hdr('op001'), json={
                    'name':'test.txt', 'who':'op004', 'offset':0,
                    'expected_revision':current_revision,
                    'book_revision':current_book['book_revision']})
                assert moved.status_code == 200 and moved.json()['who'] == 'op001'
                assert moved.json()['revision'] == current_revision + 1
                peer = (await hc.get(url+'/api/reading-progress',
                    params={'name':'test.txt','reader_id':'op004'}, headers=hdr('op001'))).json()
                assert peer['progress']['offset'] == 6
                stale = await hc.post(url+'/api/progress', headers=hdr('op001'), json={
                    'name':'test.txt','offset':6, 'expected_revision':current_revision,
                    'book_revision':current_book['book_revision']})
                assert stale.status_code == 409 and stale.json()['code'] == 'progress_conflict'
                assert stale.json()['current_revision'] == current_revision + 1
                assert (await hc.post(url+'/api/progress', headers=hdr('op001'), json={
                    'name':'test.txt','offset':7, 'expected_revision':current_revision + 1,
                    'book_revision':current_book['book_revision']})).status_code == 400
                assert (await hc.post(url+'/api/progress', headers=hdr('op001'), json={
                    'name':'test.txt','offset':4})).status_code == 428
                assert (await hc.get(url+'/api/reading-progress?name=test.txt',
                    headers=hdr('bad'))).status_code == 401
                listed = (await hc.get(url+'/api/reading-progress',
                    params={'name':'test.txt','all':'1'}, headers=hdr('004'))).json()
                assert {p['reader_id'] for p in listed['progress']} >= {'op001','op004','004'}
                assert (await hc.get(url+'/api/audit', headers=hdr('op001'))).status_code == 403
                assert (await hc.get(url+'/api/audit', headers=hdr('user'))).status_code == 200
                assert (await hc.get(url+'/api/tts?text=你好', headers=hdr('user'))).status_code == 501
            class FakeVoice(BaseHTTPRequestHandler):
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    assert body['input'] == '你好'
                    self.send_response(200); self.end_headers(); self.wfile.write(b'ID3-test-audio')
                def log_message(self, *args): pass
            voice = ThreadingHTTPServer(('127.0.0.1', 0), FakeVoice)
            threading.Thread(target=voice.serve_forever, daemon=True).start()
            try:
                (base/'data'/'tts.json').write_text(json.dumps({'provider':'openai', 'key':'test-only', 'base':f'http://127.0.0.1:{voice.server_port}'}))
                async with httpx.AsyncClient(trust_env=False) as hc:
                    audio = await hc.get(f'http://127.0.0.1:{hp}/api/tts', params={'text':'你好'}, headers={'X-Who-Token':'test-user'})
                    assert audio.status_code == 200 and audio.content == b'ID3-test-audio'
                    assert audio.headers['content-type'] == 'audio/mpeg'
            finally:
                voice.shutdown(); voice.server_close()
            (base / '书库' / 'test.txt').write_text('第一章\n前' + book_text, encoding='utf8')
            async with httpx.AsyncClient(trust_env=False) as hc:
                changed = (await hc.get(f'http://127.0.0.1:{hp}/api/notes', params={'name':'test.txt','chapter_id':'第一章'}, headers={'X-Who-Token':'test-user'})).json()
                assert changed['highlights'] and all(h['location_status'] == 'content_changed' for h in changed['highlights'])
                progress_changed = (await hc.get(f'http://127.0.0.1:{hp}/api/reading-progress',
                    params={'name':'test.txt', 'reader_id':'op001'},
                    headers={'X-Who-Token':'test-004'})).json()
                assert progress_changed['progress']['location_status'] == 'content_changed'
                old_write = await hc.post(f'http://127.0.0.1:{hp}/api/progress',
                    headers={'X-Who-Token':'test-op001'}, json={
                    'name':'test.txt','offset':0,'expected_revision':current_revision + 1,
                    'book_revision':current_book['book_revision']})
                assert old_write.status_code == 409 and old_write.json()['code'] == 'book_changed'
            print('PASS: MCP/HTTP co-reading, legacy progress, shared queries, CAS, UTF-16 positions, ownership, content-change detection, TTS')
        except BaseException:
            logs.flush(); logs.seek(0); print(logs.read()); raise
        finally:
            for p in processes: p.terminate()
            for p in processes: p.wait(timeout=10)
            logs.close()

asyncio.run(run())
