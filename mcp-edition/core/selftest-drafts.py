"""Draft/quick chapter acceptance with disposable books and real desktop/mobile UI."""
import argparse,concurrent.futures,copy,hashlib,json,os,shutil,socket,subprocess,sys,tempfile,time
from pathlib import Path
from unittest.mock import patch
import urllib.request,urllib.error
import book_drafts as d
import navigation as nav
ROOT=Path(__file__).parent
TEXT='序\n开场正文。\n-- 篇十二 --\n广告水印，甲😀乙正文。\n********\n下一段正文。\n后记\n结束。\n'
def units(t):return len(t.encode('utf-16-le'))//2
def off(t,needle):return units(t[:t.index(needle)])
def unit():
    text=TEXT;revision=hashlib.sha256(text.encode()).hexdigest();table=nav.automatic(text,revision)
    rows=table['items']
    title='-- 篇十二 --';start=off(text,title)
    rows=d.change_chapter(rows,text,dict(start=start,end=start+units(title),quote=title,title='篇十二',parent_id=None))
    value=d.create('test.txt',text,revision,dict(table,items=rows))
    request=dict(base_revision=revision,expected_revision=1,start=off(text,'广告'),end=off(text,'广告')+2,quote='广告',replacement='新😀正文',request_id='request_001')
    edited,result,repeat=d.edit(value,text,request);assert not repeat and '新😀正文水印' in result
    assert value['revision']==1 and not value['operations']
    same,same_text,repeat=d.edit(edited,result,request);assert repeat and same_text==result
    after=next(r for r in edited['items'] if r['title']=='后记');assert after['offset']==off(result,'后记')
    bad=dict(request,request_id='request_002',expected_revision=1)
    try:d.edit(edited,result,bad)
    except d.DraftError as e:assert e.status==409
    else:raise AssertionError('stale draft accepted')
    bad=dict(request,request_id='request_emoji',start=off(text,'😀')+1,end=off(text,'😀')+2,quote='?',expected_revision=1)
    try:d.edit(value,text,bad)
    except d.DraftError as e:assert e.status==400
    else:raise AssertionError('split emoji accepted')
    capped=copy.deepcopy(value);capped['operations']=[{'id':'old'+str(i)} for i in range(d.MAX_EDITS)]
    try:d.edit(capped,text,dict(request,request_id='over_edit_limit'))
    except d.DraftError as e:assert e.status==413
    else:raise AssertionError('edit count limit ignored')
    try:d.edit(value,text,dict(request,request_id='over_text_limit',replacement='甲'*4001))
    except d.DraftError as e:assert e.status==413
    else:raise AssertionError('selection limit ignored')
    title_row=next(r for r in value['items'] if r['title']=='篇十二')
    request=dict(base_revision=revision,expected_revision=1,start=start,end=start+units(title),quote=title,replacement='',request_id='delete_title')
    changed,newtext,_=d.edit(value,text,request)
    assert changed['chapter_issues'][0]['item']['id']==title_row['id']
    assert all(r['id']!=title_row['id'] for r in changed['items'])
    with tempfile.TemporaryDirectory() as folder:
        d.save(folder,'test.txt',edited);path=d.path_for(folder,'test.txt');original=path.read_bytes()
        saved,saved_text=d.load(folder,'test.txt',text,revision);assert saved_text==result and saved==edited
        with patch.object(d.os,'replace',side_effect=OSError('simulated commit failure')):
            try:d.save(folder,'test.txt',changed)
            except OSError:pass
            else:raise AssertionError('failure swallowed')
        assert path.read_bytes()==original
        try:d.load(folder,'test.txt',text,'changed-source')
        except d.DraftError as e:assert e.status==409
        else:raise AssertionError('changed source applied')
        assert path.read_bytes()==original
        path.write_bytes(b'{broken')
        try:d.load(folder,'test.txt',text,revision)
        except d.DraftError as e:assert e.status==503
        else:raise AssertionError('damaged draft accepted')
        assert path.read_bytes()==b'{broken'
    print('PASS draft unit: UTF-16, idempotent retry, stale/split-surrogate denial, chapter shift/issues, failed commit, restart and changed-source preservation')


def integration(browser=True):
    with tempfile.TemporaryDirectory() as directory:
        base=Path(directory)
        for n in ('reader_server.py','member_store.py','book_drafts.py','chapter_index.py','progress_catalog.py','navigation.py'):shutil.copy(ROOT/n,base/n)
        shutil.copytree(ROOT/'web',base/'web');(base/'书库').mkdir();(base/'data').mkdir()
        for name in ('test.txt','ui-desktop.txt','ui-mobile.txt'):(base/'书库'/name).write_bytes(TEXT.replace('\n','\r\n').encode())
        (base/'书库'/'long.txt').write_text('甲😀长正文。\n'*30000,encoding='utf8')
        ids=base/'data/identities.json';ids.write_text(json.dumps({'user':'owner','004':'peer'}))
        for name in ('notes.json','progress.json'):(base/'data'/name).write_text('{"version":2,"books":{}}')
        protected={p:p.read_bytes() for p in [ids,base/'data/notes.json',base/'data/progress.json',*list((base/'书库').glob('*.txt'))]}
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}';env=dict(os.environ,READER_CLOUD='1',READER_PORT=str(port))
        log=open(base/'test.log','w+',encoding='utf8');proc=subprocess.Popen([sys.executable,str(base/'reader_server.py')],env=env,stdout=log,stderr=log)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path,data=None,who='user'):
            headers={'X-Who-Token':'owner' if who=='user' else 'peer' if who=='004' else 'bad'}
            raw=None if data is None else json.dumps(data,ensure_ascii=False).encode()
            try:
                with opener.open(urllib.request.Request(origin+path,headers=headers,data=raw),timeout=20) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as exc:return exc.code,json.load(exc)
        try:
            for _ in range(100):
                try:
                    if request('/api/ping')[0]==200:break
                except OSError:pass
                time.sleep(.05)
            else:log.seek(0);raise AssertionError(log.read())
            book=request('/api/book?name=test.txt')[1];rev=book['book_revision']
            assert request('/api/draft/start',{'name':'test.txt','book_revision':rev},'004')[0]==403
            assert request('/api/draft?name=test.txt',who='004')[0]==403
            assert request('/api/chapter',{},'bad')[0]==401
            table=request('/api/navigation?name=test.txt')[1];title='-- 篇十二 --';start=off(TEXT,title)
            chapter=dict(name='test.txt',book_revision=rev,expected_revision=table['revision'],start=start,end=start+units(title),quote=title,title='篇十二',parent_id=None)
            assert request('/api/chapter',chapter,'004')[0]==403
            assert request('/api/chapter',chapter)[0]==200
            assert request('/api/chapter',chapter)[0]==409
            table=request('/api/navigation?name=test.txt')[1];row=next(i for i in table['items'] if i['title']=='篇十二')
            # Rename/reparent/delete use the same stable metadata and audit protocol.
            rename=dict(chapter,expected_revision=table['revision'],id=row['id'],title='修正篇十二')
            assert request('/api/chapter',rename)[0]==200
            table=request('/api/navigation?name=test.txt')[1]
            assert next(i for i in table['items'] if i['id']==row['id'])['title']=='修正篇十二'
            child_text='下一段正文。';childstart=off(TEXT,child_text)
            child=dict(chapter,expected_revision=table['revision'],start=childstart,end=childstart+units(child_text),quote=child_text,title='子章节',parent_id=row['id'])
            assert request('/api/chapter',child)[0]==200
            table=request('/api/navigation?name=test.txt')[1]
            assert request('/api/chapter',dict(chapter,expected_revision=table['revision'],action='delete',id=row['id']))[0]==200
            table=request('/api/navigation?name=test.txt')[1]
            assert next(i for i in table['items'] if i['title']=='子章节')['parent_id'] is None
            assert request('/api/chapter',dict(chapter,expected_revision=table['revision']))[0]==200
            table=request('/api/navigation?name=test.txt')[1];row=next(i for i in table['items'] if i['title']=='篇十二')
            audit=[json.loads(line) for line in (base/'data/audit.log').read_text(encoding='utf8').splitlines()]
            assert any(line['act']=='POST /api/chapter' for line in audit)
            assert row['offset']==start and row['quote'].startswith(title)
            assert not any(i['title']=='********' for i in table['items'])
            code,view=request('/api/draft/start',{'name':'test.txt','book_revision':rev});assert code==200 and view['isDraft']
            edit=dict(name='test.txt',base_revision=rev,expected_revision=view['draft_revision'],start=off(TEXT,'广告'),end=off(TEXT,'广告')+2,quote='广告',replacement='干净😀',request_id='http_edit_001')
            code,edited=request('/api/draft/edit',edit);assert code==200 and '干净😀水印' in edited['text'],edited
            assert request('/api/draft/edit',edit)[1]['already_applied']
            stale=dict(edit,request_id='http_edit_002');assert request('/api/draft/edit',stale)[0]==409
            wrong=dict(edit,expected_revision=edited['draft_revision'],quote='错误',request_id='http_wrong');assert request('/api/draft/edit',wrong)[0]==409
            current=edited['text'];start=off(current,'水印')
            racing=dict(edit,expected_revision=edited['draft_revision'],start=start,end=start+2,quote='水印',replacement='')
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results=list(pool.map(lambda i:request('/api/draft/edit',dict(racing,request_id='parallel_'+str(i))),range(2)))
            assert sorted(r[0] for r in results)==[200,409],results
            # Resume after process restart, independent of formal progress.
            proc.terminate();proc.wait(timeout=10);proc=subprocess.Popen([sys.executable,str(base/'reader_server.py')],env=env,stdout=log,stderr=log)
            for _ in range(100):
                try:
                    code,resume=request('/api/draft?name=test.txt')
                    if code==200:break
                except OSError:pass
                time.sleep(.05)
            assert resume['draft_revision']==3 and '广告' not in resume['text'] and '水印' not in resume['text']
            draftnav=request('/api/draft/navigation?name=test.txt')[1]
            pending=dict(name='test.txt',base_revision=rev,expected_revision=resume['draft_revision'],request_id='remove_chapter',start=off(resume['text'],title),end=off(resume['text'],title)+units(title),quote=title,replacement='')
            code,removed=request('/api/draft/edit',pending);assert code==200 and len(removed['chapter_issues'])==1
            issue=removed['chapter_issues'][0]['item']
            clean=removed['text'];quote='下一段正文。';loc=off(clean,quote)
            restore=dict(name='test.txt',base_revision=rev,expected_revision=removed['draft_revision'],start=loc,end=loc+units(quote),quote=quote,title='重新定位篇十二',parent_id=None,resolve_issue_id=issue['id'])
            code,restored=request('/api/draft/chapter',restore);assert code==200 and not restored['chapter_issues']
            recovered=request('/api/draft/navigation?name=test.txt')[1]
            assert next(r for r in recovered['items'] if r['id']==issue['id'])['offset']==loc
            longbook=request('/api/book?name=long.txt&window=1')[1]
            large=request('/api/draft/start',{'name':'long.txt','book_revision':longbook['book_revision'],'view_offset':180000})[1]
            assert len(large['text'])<=60000 and large['text_base']>0 and large['more_before']
            if browser:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    b=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    for mobile in (False,True):
                        name='ui-mobile.txt' if mobile else 'ui-desktop.txt'
                        ctx=b.new_context(viewport={'width':390 if mobile else 1280,'height':844 if mobile else 900},is_mobile=mobile,has_touch=mobile)
                        page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(origin+'/?who_token=owner');page.wait_for_function('typeof state!=="undefined" && state.book')
                        page.evaluate('(name)=>openBook(name)',name);page.wait_for_function('(name)=>notesCache.name===name && notesCache.revision!==""',arg=name);page.evaluate('setTemporaryView(true)')
                        def select(text):
                            page.evaluate('''needle=>{const seg=[...document.querySelectorAll('#reader .seg')].find(p=>p.textContent.includes(needle));
                                const walker=document.createTreeWalker(seg,NodeFilter.SHOW_TEXT);let node;while(node=walker.nextNode()){const i=node.textContent.indexOf(needle);if(i>=0){const r=document.createRange();r.setStart(node,i);r.setEnd(node,i+needle.length);const s=getSelection();s.removeAllRanges();s.addRange(r);onSelectionChange();return;}}throw Error('selection missing');}''',text)
                        select(title);page.locator('[data-act="chapter"]').click();page.locator('#chapterTitle').fill('手机篇十二' if mobile else '桌面篇十二');page.locator('#chapterSave').click()
                        page.wait_for_function("document.querySelector('#chapterDialog').hidden")
                        page.locator('#btnToc').click();page.wait_for_selector('.toc-row')
                        assert page.locator('#tocBody').inner_text().find('篇十二')>=0
                        page.locator('#tocClose').click();select('广告');page.locator('[data-act="edit"]').click()
                        page.wait_for_selector('#draftEditDialog:not([hidden])');page.locator('#draftReplacement').fill('干净文字😀')
                        page.wait_for_function("document.querySelector('#draftEditStatus').textContent.startsWith('已保存')")
                        page.locator('#draftEditClose').click();assert page.evaluate('state.book.isDraft')
                        assert '干净文字😀' in page.locator('#reader').inner_text()
                        page.reload();page.wait_for_function('typeof state!=="undefined" && state.book')
                        page.evaluate('(name)=>openBook(name)',name);page.locator('#btnDraft').click();page.wait_for_function('state.book.isDraft===true')
                        assert '干净文字😀' in page.locator('#reader').inner_text()
                        page.evaluate('saveNow()');page.evaluate("dispatchEvent(new Event('beforeunload'))")
                        page.locator('#exitDraft').click();page.wait_for_function('state.book && !state.book.isDraft')
                        assert '广告' in page.locator('#reader').inner_text()
                        assert not errors,errors;ctx.close()
                    b.close()
            assert all(p.read_bytes()==raw for p,raw in protected.items()),'formal text/identity/progress/notes changed'
            # External source changes must retain the draft without guessing new positions.
            draftfile=d.path_for(base/'data','test.txt');saved=draftfile.read_bytes();(base/'书库/test.txt').write_text(TEXT+'changed',encoding='utf8')
            assert request('/api/draft?name=test.txt')[0]==409 and draftfile.read_bytes()==saved
            print('PASS draft HTTP/UI: administrator-only, exact chapters, metadata CAS, edit CAS/retry/race, restart, bounded long books, mobile/desktop selection/autosave/reopen, formal bytes and legacy stores unchanged')
        finally:proc.terminate();proc.wait(timeout=10);log.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    unit();integration(not args.no_browser)
