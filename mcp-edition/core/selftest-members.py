"""PR4 disposable registry, HTTP/MCP and browser acceptance. No production data."""
import asyncio
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import member_store as store

ROOT = Path(__file__).parent
PORTAL = ROOT.parent / "token-mcp"
if not PORTAL.is_dir():
    PORTAL = ROOT.parent.parent / "token-mcp"
PW = "isolated administrator password"


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


def unit():
    with tempfile.TemporaryDirectory() as folder:
        p = Path(folder) / "identities.json"
        original = b'{"user":"owner-test", "004":"legacy-agent"}'
        p.write_bytes(original)
        # Storage failure before commit leaves both member and token absent.
        with patch.object(store.os, "replace", side_effect=OSError("replace failure")):
            try: store.create(str(p), "name", "human", "web")
            except OSError: pass
            else: raise AssertionError("failure not raised")
        assert p.read_bytes() == original
        a = store.create(str(p), "same", "human", "web")
        b = store.create(str(p), "same", "agent", "mcp")
        assert a["member"]["id"] != b["member"]["id"]
        assert store.read(str(p))["user"] == "owner-test"
        assert (p.parent / ".token-backups" / ("identities-"+hashlib.sha256(original).hexdigest()+".json")).read_bytes() == original
        before = p.read_bytes()
        # Failure specifically at final commit, after a valid backup was saved.
        real = store.os.replace
        def fail_commit(src, dst):
            if str(dst) == str(p): raise OSError("final commit failure")
            return real(src, dst)
        with patch.object(store.os, "replace", side_effect=fail_commit):
            try: store.create(str(p), "failed", "agent", "mcp")
            except OSError: pass
            else: raise AssertionError("failure not raised")
        assert p.read_bytes() == before
        # Separate writers use the same full transaction lock.
        workers = [subprocess.Popen([sys.executable, __file__, "--worker", str(p), str(i)]) for i in range(5)]
        assert [w.wait(timeout=20) for w in workers] == [0] * len(workers)
        assert len(store.profiles(store.read(str(p)))) == 9
        ident = a["member"]["id"]
        store.revoke(str(p), ident, a["member"]["credential"]["id"])
        assert not store.authenticate(store.read(str(p)), a["token"])
        assert store.profiles(store.read(str(p)))[ident]["credential"]["status"] == "revoked"
        try: store.create(str(p), "reuse", "legacy", "legacy", ident)
        except FileExistsError: pass
        else: raise AssertionError("revoked identity reused")
        before = p.read_bytes()
        try: store.revoke(str(p), "user")
        except FileExistsError: pass
        else: raise AssertionError("reserved identity revoked")
        assert p.read_bytes() == before
        for bad in (b'{broken', b'{"user":"a","user":"b"}', b'{"user":"a","004":"a"}'):
            p.write_bytes(bad)
            try: store.create(str(p), "no", "human", "web")
            except ValueError: pass
            else: raise AssertionError("damaged registry overwritten")
            assert p.read_bytes() == bad
    print("PASS members storage: atomic failure, exact backups, 5-process race, reserved/revoked IDs, malformed registry")


def browser(web, portal, token, ident):
    from playwright.sync_api import sync_playwright
    chrome = os.environ.get("CHROME_TEST", "C:/Program Files/Google/Chrome/Application/chrome.exe")
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=chrome, headless=True)
        for viewport in ({"width": 1280, "height": 900}, {"width": 390, "height": 844}):
            ctx = b.new_context(viewport=viewport, is_mobile=viewport["width"] < 500, has_touch=viewport["width"] < 500)
            page = ctx.new_page(); errors=[]
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(portal); page.locator('#adminPw').fill(PW);page.locator('#refreshBtn').click()
            page.wait_for_function("document.querySelector('#list').textContent.includes('human-test')")
            page.locator('#nickname').fill('浏览器同名成员');page.locator('#createBtn').click()
            page.wait_for_selector('#tokenBox:not([hidden])')
            assert page.locator('#tokenVal').inner_text()
            assert page.evaluate('JSON.stringify(localStorage)') == '{}'
            page.locator('#dismissBtn').click();assert page.locator('#tokenVal').inner_text() == ''
            page.goto(web);page.locator('#gateToken').fill(token);page.locator('#gateSubmit').click()
            page.wait_for_function("typeof state !== 'undefined' && state.viewerId === " + json.dumps(ident))
            assert page.evaluate('readerLabel('+json.dumps(ident)+')') == 'human-test'
            page.wait_for_function("document.querySelector('#reader mark.shared-hl') !== null")
            page.locator('#reader mark.shared-hl').first.click()
            page.locator('#hlInput').fill('browser comment')
            page.locator('#hlSend').click()
            page.wait_for_function("document.querySelector('#hlPanelThread').textContent.includes('browser comment')")
            page.reload();page.wait_for_function("typeof state !== 'undefined' && state.viewerId === " + json.dumps(ident))
            assert not errors, errors
            ctx.close()
        b.close()
    print("PASS members browser: desktop/mobile portal create, no secret persistence, independent human login and reload")


async def integration():
    with tempfile.TemporaryDirectory() as directory:
        base=Path(directory);core=base/'core';core.mkdir();data=core/'data';data.mkdir();(core/'书库').mkdir()
        for name in ('reader_server.py','member_store.py','book_drafts.py','chapter_index.py','progress_catalog.py','navigation.py','mcp_server.py','MCP工具标准调用参考_只读.txt'):
            shutil.copy(ROOT/name,core/name)
        shutil.copytree(ROOT/'web',core/'web');portal=base/'token-mcp';portal.mkdir()
        for name in ('server.py','index.html'):shutil.copy(PORTAL/name,portal/name)
        p=data/'identities.json';p.write_text(json.dumps({'user':'owner-test','004':'legacy-agent'}))
        (core/'书库'/'test.txt').write_text('第一章\n甲乙丙丁戊己。\n第二段。',encoding='utf8')
        (data/'progress.json').write_text('{"version":1,"books":{}}')
        (data/'notes.json').write_text('{"version":2,"books":{}}')
        salt='01'*16
        (data/'.token-admin.json').write_text(json.dumps({'salt':salt,'hash':hashlib.sha256(bytes.fromhex(salt)+PW.encode()).hexdigest()}))
        for name,key in (('path','T'*24),('gpt','G'*24)):
            (base/name).write_text(key);(base/name).chmod(0o600)
        wp,mp,tp=port(),port(),port();web=f'http://127.0.0.1:{wp}';mcp=f'http://127.0.0.1:{mp}';url=f'http://127.0.0.1:{tp}'
        env=dict(os.environ,READER_CLOUD='1',READER_PORT=str(wp),READER_MCP_PORT=str(mp),TOKEN_PORT=str(tp),
            READER_DATA_DIR=str(data),READER_API_URL=web,READER_MCP_PATH_KEY_FILE=str(base/'path'),READER_MCP_004_KEY_FILE=str(base/'gpt'),
            READER_MCP_PUBLIC_HOST='reader.example.com',READER_MCP_EXTRA_HOST='mcp.example.com',
            READER_WEB_PUBLIC_BASE='https://reader.example.com/',READER_MCP_URL_PUBLIC_BASE='https://mcp.example.com')
        log=open(base/'run.log','w+')
        commands=[core/'reader_server.py',core/'mcp_server.py',portal/'server.py']
        processes=[subprocess.Popen([sys.executable,str(n)],env=env,stdout=log,stderr=log) for n in commands]
        try:
            async with httpx.AsyncClient(trust_env=False,timeout=10) as c:
                for _ in range(100):
                    try:
                        if (await c.get(web+'/api/ping')).status_code==200 and (await c.get(url+'/api/status')).status_code==200 and (await c.post(mcp+'/mcp')).status_code==404:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.1)
                else:raise AssertionError('test servers failed')
                assert (await c.post(url+'/api/members',json={'nickname':'no','kind':'human','channel':'web'})).status_code==403
                assert (await c.post(url+'/api/members/list',json={})).status_code==403
                assert (await c.post(url+'/api/members',json={'password':PW,'nickname':'x','kind':'human','channel':'mcp'})).status_code==400
                people=[]
                for kind,channel,nick in [('human','web','human-test'),('agent','mcp','agent-test'),('agent','mcp-url','url-test')]:
                    r=await c.post(url+'/api/members',json={'password':PW,'nickname':nick,'kind':kind,'channel':channel,'id':'user','admin':True})
                    assert r.status_code==200,r.text
                    people.append(r.json());assert people[-1]['member']['id']!='user'
                human,agent,urlagent=people;hid=human['member']['id']
                headers={'X-Who-Token':human['token']}
                a=await c.get(web+'/api/auth-check',headers=headers);assert a.status_code==200 and a.json()['who']==hid
                assert (await c.get(web+'/api/auth-check',headers={'X-Who-Token':agent['token']})).status_code==401
                book=(await c.get(web+'/api/book',params={'name':'test.txt'},headers=headers)).json()
                progress={'name':'test.txt','offset':4,'chars':book['chars'],'percent':.1,'anchor':'甲乙','expected_revision':0,'book_revision':book['book_revision'],'who':'user'}
                r=await c.post(web+'/api/progress',json=progress,headers=headers);assert r.status_code==200,r.text
                saved=json.loads((data/'progress.json').read_bytes());assert hid in saved['books']['test.txt'] and saved['books']['test.txt'].get('user',{}).get('offset')!=4
                # Administrative operations still require stable reserved user, not human kind.
                r=await c.post(web+'/api/books/category',json={'name':'test.txt','category':'humanities'},headers=headers)
                assert r.status_code == 403,r.text
                h=await c.post(web+'/api/notes/highlight',json={'name':'test.txt','start':4,'end':6,'text':'甲乙','author_id':'user'},headers=headers)
                assert h.status_code==200,h.text
                highlight=h.json().get('highlight') or h.json()
                highid=highlight.get('id') or h.json().get('id')
                r=await c.post(web+'/api/notes/comment',json={'name':'test.txt','id':highid,'who':'ai','name_ai':hid,'text':'human comment','author_id':'user'},headers=headers)
                assert r.status_code==200 and r.json()['annotation']['author_id']==hid and r.json()['annotation']['who']=='user',r.text
                async def call(path,headers):
                    async with httpx.AsyncClient(headers=headers,trust_env=False) as mc:
                        async with streamable_http_client(mcp+path,http_client=mc) as (read,write,_):
                            async with ClientSession(read,write) as session:
                                await session.initialize();tools=await session.list_tools();assert len(tools.tools)==12
                                result=await session.call_tool('list_books',{});assert not result.isError
                                return json.loads(result.content[0].text)
                normal=await call('/mcp/'+'T'*24,{'Authorization':'Bearer '+agent['token']})
                assert normal['viewer_id']==agent['member']['id'],normal
                keyed='/mcp/member/'+urlagent['token']
                result=await call(keyed,{'Host':'mcp.example.com'});assert result['viewer_id']==urlagent['member']['id']
                assert (await c.post(mcp+keyed,headers={'Host':'wrong.example.com'})).status_code==404
                assert (await c.post(mcp+keyed,headers={'Host':'mcp.example.com','Authorization':'Bearer wrong'})).status_code==401
                assert (await c.post(mcp+'/mcp/'+'T'*24,headers={'Authorization':'Bearer '+human['token']})).status_code==401
                # Legacy URL-only adapter and all existing tools stay compatible.
                old=await call('/mcp/'+'G'*24,{'Host':'mcp.example.com'});assert old['viewer_id']=='004'
                listing=(await c.post(url+'/api/members/list',json={'password':PW})).text
                public=(await c.get(url+'/api/status')).text
                assert all(x['token'] not in listing+public for x in people)
                if '--no-browser' not in sys.argv:await asyncio.to_thread(browser,web,url,human['token'],hid)
                untouched={n:(data/n).read_bytes() for n in ('progress.json','notes.json')}
                for person in people:
                    m=person['member'];r=await c.post(url+'/api/credentials/revoke',json={'password':PW,'member_id':m['id'],'credential_id':m['credential']['id']})
                    assert r.status_code==200,r.text
                    assert (await c.get(web+'/api/books',headers={'X-Who-Token':person['token']})).status_code==401
                assert (await c.post(mcp+keyed,headers={'Host':'mcp.example.com'})).status_code==401
                assert (await c.post(mcp+'/mcp/'+'T'*24,headers={'Authorization':'Bearer '+agent['token']})).status_code==401
                assert all((data/n).read_bytes()==raw for n,raw in untouched.items())
                assert (await c.get(web+'/api/books',headers={'X-Who-Token':'owner-test'})).status_code==200
                # Restart reloads revoked state; no stale in-memory credentials.
                processes[2].terminate();processes[2].wait(timeout=10)
                processes[2]=subprocess.Popen([sys.executable,str(commands[2])],env=env,stdout=log,stderr=log)
                for _ in range(50):
                    try:
                        listing=await c.post(url+'/api/members/list',json={'password':PW});break
                    except httpx.HTTPError:await asyncio.sleep(.1)
                assert next(x for x in listing.json()['members'] if x['id']==hid)['credential']['status']=='revoked'
                before=p.read_bytes();(base/'path').unlink()
                r=await c.post(url+'/api/members',json={'password':PW,'nickname':'missing-url','kind':'agent','channel':'mcp'})
                assert r.status_code==503 and p.read_bytes()==before
                log.flush();content=(base/'run.log').read_text()
                assert all(x['token'] not in content for x in people)
                print('PASS members HTTP/MCP: distinct identities, author/progress binding, admin denial, both MCP modes, legacy 004, revoke/restart, no secret lists/logs, no history change')
        finally:
            for proc in processes:
                if proc.poll() is None:proc.terminate()
                proc.wait(timeout=10)
            log.close()


if __name__=='__main__':
    if '--worker' in sys.argv:store.create(sys.argv[2], 'worker-'+sys.argv[3], 'human', 'web')
    else:unit();asyncio.run(integration())
