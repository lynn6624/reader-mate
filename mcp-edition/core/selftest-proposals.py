"""Synthetic revision queue, shared-draft approval, real MCP and browser acceptance."""
import argparse
import asyncio
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
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import book_drafts as drafts
import navigation as nav
import revision_proposals as proposals

ROOT = Path(__file__).resolve().parent
TEXT = '第一章 起点\n前段广告，甲😀乙。\n重复原文。\n-- 篇十二 --\n第二处广告，原词。\n重复原文。\n第二章 终点\n结尾文字。\n'


def fixture():
    revision = 'sha256:' + hashlib.sha256(TEXT.encode()).hexdigest()
    table = nav.automatic(TEXT, revision)
    return revision, table, drafts.create('test.txt', TEXT, revision, table)


def expect(status, fn):
    try: fn()
    except drafts.DraftError as exc: assert exc.status == status, (exc.status, status)
    else: raise AssertionError('expected error %s' % status)


def unit():
    revision, table, value = fixture()
    queue = dict(schema_version=1, name='test.txt', proposals=[])
    def submit(operation, quote, replacement='', **extra):
        return proposals.propose(queue, 'test.txt', TEXT, revision, table, 'agent-a', 'edit',
            dict(book_revision=revision,operation=operation,quote=quote,replacement=replacement,**extra))[0]
    expect(409, lambda: submit('delete','重复原文。'))
    selected = submit('delete','重复原文。', context_before='甲😀乙。\n')
    assert selected['spec']['author_id'] == 'agent-a'
    assert submit('delete','重复原文。', context_before='甲😀乙。\n')['id'] == selected['id']
    expect(409, lambda: submit('replace','前段广告','改',start=0))
    expect(400, lambda: submit('insert_after','甲😀乙。'))
    expect(400, lambda: submit('delete','甲😀乙。','不合法'))
    expect(413, lambda: submit('replace','原词','字'*4001))
    requests = [submit('delete','前段广告'), submit('replace','原词','正确词'),
                submit('insert_before','结尾文字。','前插。'), submit('insert_after','甲😀乙。','后插。')]
    text = TEXT
    for row in requests:
        value, text = proposals.apply(row,value,text,revision,proposals.decision_for(row,{'action':'accept'}))
        assert proposals.public(row,value)['status']=='accepted'
    assert '前段广告' not in text and '正确词' in text and '前插。结尾文字。' in text and '甲😀乙。后插。' in text
    expect(409, lambda: proposals.preview(requests[0],value,text,revision))
    # Another insertion at the same boundary must conflict, not reorder silently.
    same_place = submit('insert_after','甲😀乙。','另一个插入')
    expect(409, lambda: proposals.preview(same_place,value,text,revision))
    expect(409, lambda: proposals.preview(requests[0],value,text,'changed-source'))
    def chapter(operation, **kw):
        return proposals.propose(queue,'test.txt',TEXT,revision,table,'agent-b','chapter',
            dict(book_revision=revision,operation=operation,**kw))[0]
    add = chapter('add',quote='-- 篇十二 --',title='篇十二')
    rename = chapter('update',target_title='第二章 终点',title='终点校正',parent_title='第一章 起点')
    remove = chapter('delete',target_title='第一章 起点')
    for row in (add,rename,remove):
        value,text = proposals.apply(row,value,text,revision,proposals.decision_for(row,{'action':'accept'}))
    assert any(r['title']=='篇十二' for r in value['items'])
    assert next(r for r in value['items'] if r['title']=='终点校正')['parent_id'] is None
    assert all(r['title']!='第一章 起点' for r in value['items'])
    expect(409, lambda: proposals.preview(rename,value,text,revision))
    assert len(proposals.receipts(value))==7
    with tempfile.TemporaryDirectory() as root:
        proposals.save(root,'test.txt',queue); drafts.save(root,'test.txt',value)
        saved,replayed = drafts.load(root,'test.txt',TEXT,revision)
        assert replayed==text and proposals.public(requests[0],saved)['status']=='accepted'
        source = drafts.path_for(root,'test.txt').read_bytes()
        with patch.object(drafts.os,'replace',side_effect=OSError('synthetic commit failure')):
            try: drafts.save(root,'test.txt',dict(value,revision=value['revision']+1))
            except OSError: pass
            else: raise AssertionError('failure ignored')
        assert drafts.path_for(root,'test.txt').read_bytes()==source
        p = proposals.path_for(root,'test.txt'); original = p.read_bytes()
        with patch.object(proposals.os,'replace',side_effect=OSError('synthetic queue failure')):
            try: proposals.save(root,'test.txt',queue)
            except OSError: pass
            else: raise AssertionError('queue failure ignored')
        assert p.read_bytes()==original
        for corrupt in (b'{broken',b'{"name":"test.txt","name":"test.txt"}'):
            p.write_bytes(corrupt);expect(503,lambda: proposals.load(root,'test.txt'));assert p.read_bytes()==corrupt
    print('PASS proposals unit: exact/ambiguous locations, sequential UTF-16 mapping, four text operations, chapter add/update/delete, receipts and failed writes',flush=True)


def interrupted(root, phase, ready):
    revision, table, value = fixture()
    queue = dict(schema_version=1,name='test.txt',proposals=[])
    row,_ = proposals.propose(queue,'test.txt',TEXT,revision,table,'agent-a','edit',
        dict(book_revision=revision,operation='delete',quote='前段广告'))
    value,text = proposals.apply(row,value,TEXT,revision,proposals.decision_for(row,{'action':'accept'}))
    replace = drafts.os.replace
    def paused(src,dst):
        if phase=='before': ready.set(); time.sleep(30)
        replace(src,dst)
        if phase=='after': ready.set(); time.sleep(30)
    drafts.os.replace = paused
    drafts.save(root,'test.txt',value)


def crashes():
    ctx=multiprocessing.get_context('spawn')
    for phase in ('before','after'):
        with tempfile.TemporaryDirectory() as root:
            revision,table,value=fixture();drafts.save(root,'test.txt',value)
            event=ctx.Event();process=ctx.Process(target=interrupted,args=(root,phase,event));process.start()
            try: assert event.wait(15)
            finally: process.terminate();process.join(5)
            saved,text=drafts.load(root,'test.txt',TEXT,revision)
            assert ('前段广告' not in text)==(phase=='after')
            assert len(proposals.receipts(saved))==(1 if phase=='after' else 0)
    print('PASS proposal termination: draft and acceptance receipt remain old together or new together',flush=True)


def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def browser(origin, rows):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        chrome=pw.chromium.launch(executable_path=os.environ.get('CHROME_TEST','/usr/bin/chromium'),headless=True)
        try:
            for mobile in (False,True):
                context=chrome.new_context(viewport={'width':390 if mobile else 1280,'height':844},is_mobile=mobile,has_touch=mobile)
                page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(origin+'/?who_token=test-owner');page.wait_for_function('typeof state!=="undefined" && state.book')
                name='ui-mobile.txt' if mobile else 'ui-desktop.txt'
                page.evaluate('(name)=>openBook(name)',name);page.wait_for_function('(name)=>state.book?.name===name',arg=name)
                page.locator('#btnReview').click();page.locator('.rm-review-card').first.wait_for()
                assert page.locator('#reviewBook').inner_text()=='书籍：'+name
                assert page.locator('#reviewList img').count()==0
                accepted_id,rejected_id=rows[name]
                card=page.locator('[data-proposal-id="'+accepted_id+'"]')
                card.get_by_role('button',name='预览并审批').click()
                card.get_by_label('接受内容（可手动调整）').fill('管理员校正')
                if os.environ.get('REVIEW_SCREENSHOT_DIR'):
                    output=Path(os.environ['REVIEW_SCREENSHOT_DIR']);output.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(output/('review-mobile.png' if mobile else 'review-desktop.png')))
                card.get_by_role('button',name='接受当前内容').click()
                page.wait_for_function('(id)=>!reviewBusy && !document.querySelector(`[data-proposal-id="${id}"]`)',arg=accepted_id)
                card=page.locator('[data-proposal-id="'+rejected_id+'"]')
                card.get_by_role('button',name='预览并审批').click();card.get_by_role('button',name='拒绝',exact=True).click()
                page.wait_for_function('!reviewBusy && document.querySelectorAll(".rm-review-card").length===0')
                page.locator('#reviewStatus').select_option('accepted')
                page.get_by_text('实际接受内容',exact=True).wait_for()
                assert '管理员校正' in page.locator('#reviewList').inner_text()
                page.locator('#reviewClose').click();page.locator('#btnDraft').click()
                page.wait_for_function('state.book?.isDraft===true')
                assert page.evaluate('state.book.text.includes("管理员校正") && state.book.text.includes("原词")')
                assert not errors,errors
                context.close()
            context=chrome.new_context();page=context.new_page();page.goto(origin+'/?who_token=test-a')
            page.wait_for_function('typeof state!=="undefined" && state.book')
            assert page.locator('#btnReview').is_hidden()
            context.close()
        finally:chrome.close()
    print('PASS review UI: desktop/mobile preview, manual adjustment, accept/reject, escaped content, shared PR5 draft view',flush=True)


async def integration(with_browser):
    with tempfile.TemporaryDirectory() as root:
        base=Path(root)
        for filename in ('reader_server.py','member_store.py','book_drafts.py','revision_proposals.py',
                         'navigation.py','chapter_index.py','progress_catalog.py','mcp_server.py','MCP工具标准调用参考_只读.txt'):
            shutil.copy(ROOT/filename,base/filename)
        shutil.copytree(ROOT/'web',base/'web');(base/'data').mkdir();(base/'书库').mkdir()
        for name in ('test.txt','stale.txt','ui-mobile.txt','ui-desktop.txt'):(base/'书库'/name).write_text(TEXT,encoding='utf-8')
        identities=base/'data/identities.json';identities.write_text(json.dumps({'user':'test-owner','agent-a':'test-a','agent-b':'test-b','004':'test-bearer-004'}))
        for filename in ('progress.json','notes.json'):(base/'data'/filename).write_text('{"version":2,"books":{}}')
        protected={p:p.read_bytes() for p in [identities,base/'data/progress.json',base/'data/notes.json',*list((base/'书库').glob('*.txt'))]}
        key='A'*24;special='B'*24
        for filename,value in (('key',key),('special',special)):
            (base/filename).write_text(value);(base/filename).chmod(0o600)
        hp,mp=port(),port();origin=f'http://127.0.0.1:{hp}';mcp_origin=f'http://127.0.0.1:{mp}/mcp/{key}'
        env=dict(os.environ,READER_CLOUD='1',READER_PORT=str(hp),READER_MCP_PORT=str(mp),READER_API_URL=origin,
                 READER_MCP_PATH_KEY_FILE=str(base/'key'),READER_MCP_004_KEY_FILE=str(base/'special'))
        log=open(base/'test.log','w+',encoding='utf-8');workers=[]
        def launch():
            for script in ('reader_server.py','mcp_server.py'):
                workers.append(subprocess.Popen([sys.executable,str(base/script)],env=env,stdout=log,stderr=log))
        launch()
        async with httpx.AsyncClient(trust_env=False,timeout=20) as client:
            async def request(path,data=None,who='user'):
                headers={'X-Who-Token':{'user':'test-owner','agent-a':'test-a','agent-b':'test-b'}.get(who,'invalid')}
                response=await (client.get(origin+path,headers=headers) if data is None else client.post(origin+path,json=data,headers=headers))
                return response.status_code,response.json()
            async def ready():
                for _ in range(150):
                    try:
                        if (await request('/api/ping'))[0]==200 and (await client.post(f'http://127.0.0.1:{mp}/mcp')).status_code==404:return
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.05)
                log.seek(0);raise AssertionError(log.read())
            async def submit(quote,replacement='',operation='replace',name='test.txt',who='agent-a',**extra):
                revision=(await request('/api/book?name='+name))[1]['book_revision']
                code,result=await request('/api/proposals/edit',dict(name=name,book_revision=revision,quote=quote,
                    replacement=replacement,operation=operation,author_id='user',reason='合成测试',**extra),who)
                assert code==200,result
                return result['proposal']
            async def preview(row):
                code,result=await request('/api/proposals/preview?name='+row['spec']['name']+'&id='+row['id'])
                assert code==200,result
                return result
            async def accept(row,**extra):
                detail=await preview(row)
                return await request('/api/proposals/review',dict(name=row['spec']['name'],id=row['id'],action='accept',
                    expected_revision=detail['expected_revision'],**extra))
            try:
                await ready()
                first=await submit('前段广告','替换广告');second=await submit('原词','正确词',who='agent-b')
                assert first['spec']['author_id']=='agent-a'
                assert not list((base/'data/drafts').glob('*.json'))
                again=await submit('前段广告','替换广告');assert again['id']==first['id']
                assert len((await request('/api/proposals?name=test.txt',who='agent-a'))[1]['proposals'])==1
                assert (await request('/api/proposals/review',dict(name='test.txt',id=first['id'],action='accept',expected_revision=0),'agent-a'))[0]==403
                assert (await request('/api/proposals/preview?name=test.txt&id='+first['id'],who='agent-a'))[0]==403
                assert (await request('/api/draft/start',dict(name='test.txt'),'agent-a'))[0]==403
                assert (await request('/api/proposals?name=test.txt',who='bad'))[0]==401
                # Two administrators accepting against the same revision: only one wins.
                results=await asyncio.gather(*[request('/api/proposals/review',dict(name='test.txt',id=row['id'],action='accept',expected_revision=0)) for row in (first,second)])
                assert sorted(r[0] for r in results)==[200,409],results
                pending=first if results[0][0]==409 else second
                assert (await accept(pending))[0]==200
                assert len(list((base/'data/drafts').glob('*.json')))==1
                saved=(await request('/api/draft?name=test.txt'))[1]
                assert '替换广告' in saved['text'] and '正确词' in saved['text']
                raw=drafts.path_for(base/'data','test.txt').read_bytes()
                code,repeated=await request('/api/proposals/review',dict(name='test.txt',id=first['id'],action='accept',expected_revision=0))
                assert code==200 and repeated['already_applied'] and drafts.path_for(base/'data','test.txt').read_bytes()==raw
                assert (await request('/api/proposals/review',dict(name='test.txt',id=first['id'],action='accept',replacement='不同内容')))[0]==409
                overlap=await submit('前段广告','另一修改',who='agent-b')
                assert (await preview(overlap))['proposal']['status']=='conflict'
                checked=await request('/api/proposals/preview',dict(name='test.txt',id=overlap['id']))
                assert checked[1]['proposal']['status']=='conflict'
                assert (await accept(overlap))[0]==409
                conflict=(await request('/api/proposals?name=test.txt&status=conflict'))[1]['proposals'];assert conflict[0]['id']==overlap['id']
                rejected=await submit('结尾文字。','拒绝内容')
                assert (await request('/api/proposals/review',dict(name='test.txt',id=rejected['id'],action='reject')))[0]==200
                assert drafts.path_for(base/'data','test.txt').read_bytes()==raw
                assert (await accept(rejected))[0]==409
                insert=await submit('结尾文字。','插入😀。',operation='insert_before');assert (await accept(insert))[0]==200
                revision=(await request('/api/book?name=test.txt'))[1]['book_revision']
                code,chapter=await request('/api/proposals/chapter',dict(name='test.txt',book_revision=revision,operation='add',quote='-- 篇十二 --',title='篇十二'),'agent-a')
                assert code==200,chapter
                chapter=chapter['proposal'];assert (await accept(chapter,title='篇十二校正'))[0]==200
                assert any(r['title']=='篇十二校正' for r in (await request('/api/draft/navigation?name=test.txt'))[1]['items'])
                # Queue status derives from the same committed draft after restart.
                for worker in workers:worker.terminate();worker.wait(timeout=10)
                workers.clear();launch();await ready()
                assert (await request('/api/proposals/review',dict(name='test.txt',id=chapter['id'],action='accept',title='篇十二校正')))[1]['already_applied']
                stale=await submit('原词','新词',name='stale.txt')
                stale_path=base/'书库/stale.txt';stale_path.write_text(TEXT+'原书新版本',encoding='utf-8')
                assert (await preview(stale))['proposal']['status']=='conflict'
                assert (await accept(stale))[0]==409
                assert not drafts.path_for(base/'data','stale.txt').exists()
                stale_path.write_bytes(protected[stale_path])
                # An ordinary PR5 edit invalidates the review's revision but keeps receipts.
                later=await submit('结尾文字。','复核结尾')
                old_preview=await preview(later)
                draft=(await request('/api/draft?name=test.txt'))[1]
                start=drafts.units(draft['text'][:draft['text'].index('正确词')])
                code,_=await request('/api/draft/edit',dict(name='test.txt',base_revision=revision,
                    expected_revision=draft['draft_revision'],request_id='manual_pr5_edit',
                    start=start,end=start+3,quote='正确词',replacement='管理员直接校正'))
                assert code==200
                assert (await request('/api/proposals/review',dict(name='test.txt',id=later['id'],action='accept',expected_revision=old_preview['expected_revision'])))[0]==409
                assert (await accept(later))[0]==200
                assert (await request('/api/proposals/review',dict(name='test.txt',id=first['id'],action='accept')))[1]['already_applied']
                # Real MCP schemas and tool calls; legacy tools stay available.
                async with httpx.AsyncClient(trust_env=False,headers={'Authorization':'Bearer test-a'}) as hc:
                    async with streamable_http_client(mcp_origin,http_client=hc) as (r,w,_):
                        async with ClientSession(r,w) as session:
                            await session.initialize();tools=await session.list_tools();names={t.name for t in tools.tools}
                            assert len(names)==15 and {'propose_edit','propose_chapter','list_proposals','read_book','update_progress'}<=names
                            async def tool(name,data):
                                result=await session.call_tool(name,data)
                                assert not result.isError,result
                                return json.loads(result.content[0].text)
                            proposed=await tool('propose_edit',dict(name='test.txt',book_revision=revision,operation='insert_after',quote='第二处广告',replacement='MCP建议',reason='测试'))
                            assert proposed['proposal']['spec']['author_id']=='agent-a' and proposed['proposal']['status']=='pending'
                            await tool('propose_chapter',dict(name='test.txt',book_revision=revision,operation='update',target_title='第二章 终点',title='新终点'))
                            listed=await tool('list_proposals',dict(name='test.txt',limit=1));assert len(listed['proposals'])==1 and listed['next_offset']==1
                            bad=await session.call_tool('propose_edit',dict(name='test.txt',book_revision=revision,operation='delete',quote='重复原文。'))
                            assert bad.isError
                ui_rows={}
                for name in ('ui-mobile.txt','ui-desktop.txt'):
                    accepted=await submit('前段广告','建议替换',name=name)
                    rejected=await submit('原词','<img src=x onerror=alert(1)>',name=name)
                    ui_rows[name]=(accepted['id'],rejected['id'])
                if with_browser:await asyncio.to_thread(browser,origin,ui_rows)
                for path,original in protected.items():assert path.read_bytes()==original,path.name
                print('PASS proposals HTTP/MCP: server identity, permissions, one draft, sequential approval, CAS race, duplicate decisions, conflict/reject, restart, paging and protected bytes',flush=True)
            finally:
                for worker in workers:
                    if worker.poll() is None:worker.terminate()
                for worker in workers:worker.wait(timeout=10)
                log.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    unit();crashes();asyncio.run(integration(not args.no_browser))
