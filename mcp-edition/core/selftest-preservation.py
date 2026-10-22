"""Stage 0: disposable legacy notes, commit failure/concurrency, HTTP and browser proof."""
import argparse,concurrent.futures,copy,json,os,shutil,socket,subprocess,sys,tempfile,time,urllib.request,urllib.error
from pathlib import Path
import reader_server as reader

ROOT=Path(__file__).parent
BOOK='甲乙丙丁戊己庚辛。\n第二段正文。'
def seed():
    comments=[{'who':'retired_agent','text':'历史长评论。'*200,'at':'2026-10-10T00:00:00','extra':{'source':'legacy'}},
              {'who':'ai','name':'removed001','text':'已撤销身份的批注','at':'2026-10-11T00:00:00'},
              {'who':'strange\" onmouseover=\"__injected=1','text':'身份原样保留，显示须转义'},
              {'text':'作者没有可靠来源','extra':{'keep':True}},
              {'who':'user','text':'旧主人批注'}]
    return {'version':2,'updated':'2026-10-12T00:00:00','extension':{'keep':[1,2]},'books':{'test.txt':{'extra':'book metadata','highlights':[
        {'id':'h_unknown','start':0,'end':2,'text':'甲乙','thread':comments,'extra':{'keep':True}},
        {'id':'h_owned','start':4,'end':6,'text':'戊己','owner':'user','thread':[]}]}}}
def contains(actual,original):
    if isinstance(original,dict):return all(k in actual and contains(actual[k],v) for k,v in original.items())
    if isinstance(original,list):return len(actual)>=len(original) and all(contains(a,b) for a,b in zip(actual,original))
    return actual==original
def configure(directory):
    reader.DATA_DIR=str(directory);reader.NOTES_FILE=str(directory/'notes.json')
def worker(directory,tag):
    directory=Path(directory);configure(directory);snapshot=reader.load_notes()
    (directory/(tag+'.ready')).write_text('ready')
    while not (directory/'go').exists():time.sleep(.01)
    item={'who':tag,'text':'process '+tag}
    snapshot['books']['test.txt']['highlights'][1]['thread'].append(item)
    try:reader.save_notes(snapshot);result='first'
    except reader.NotesConflictError:
        snapshot=reader.load_notes();snapshot['books']['test.txt']['highlights'][1]['thread'].append(item)
        reader.save_notes(snapshot);result='retried'
    (directory/(tag+'.result')).write_text(result)
def unit():
    old=(reader.DATA_DIR,reader.NOTES_FILE)
    try:
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder);configure(directory);path=directory/'notes.json'
            raw=json.dumps(seed(),ensure_ascii=False).encode();path.write_bytes(raw)
            first=reader.load_notes();second=reader.load_notes()
            highlight=first['books']['test.txt']['highlights'][0]
            assert len(highlight['thread'])==5 and highlight['author_id'] is None
            assert highlight['thread'][0]['author_id']=='retired_agent'
            assert highlight['thread'][1]['author_id']=='removed001'
            assert highlight['thread'][3]['author_id'] is None
            assert len(highlight['thread'][0]['text'])>reader.COMMENT_CHARS_HARD_MAX
            assert path.read_bytes()==raw
            ids=[c['annotation_id'] for c in highlight['thread']]
            reader.save_notes(first)
            after=path.read_bytes();assert contains(json.loads(after)['books'],seed()['books'])
            assert next((directory/'notes-backups').glob('*.json')).read_bytes()==raw
            assert [c['annotation_id'] for c in reader.load_notes()['books']['test.txt']['highlights'][0]['thread']]==ids
            try:reader.save_notes(second)
            except reader.NotesConflictError:pass
            else:raise AssertionError('stale snapshot accepted')
            assert path.read_bytes()==after
            original_replace=reader.os.replace
            snapshot=reader.load_notes();snapshot['extra']='new'
            def fail(*args):raise OSError('simulated disk/replace failure')
            reader.os.replace=fail
            try:
                try:reader.save_notes(snapshot)
                except OSError:pass
                else:raise AssertionError('failure not raised')
            finally:reader.os.replace=original_replace
            assert path.read_bytes()==after and not list(directory.glob('.notes-*'))
            reader.save_notes(snapshot);assert json.loads(path.read_bytes())['extra']=='new'
            assert len(list((directory/'notes-backups').glob('*.json')))==1
            broken=[b'{broken',b'{"books":{},"books":{}}',b'{"books":[]}',b'{"books":{"x":{"highlights":[4]}}}',b'{"books":{"x":{"highlights":[{"thread":[4]}]}}}',b'{"books":{"x":{"highlights":[{"author_id":[]}]}}}']
            for bad in broken:
                path.write_bytes(bad)
                try:reader.load_notes()
                except reader.NotesStoreError:pass
                else:raise AssertionError('corrupt notes became empty')
                assert path.read_bytes()==bad
            path.write_bytes(after)
            processes=[subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',str(directory),tag]) for tag in ('one','two')]
            try:
                deadline=time.monotonic()+15
                while not all((directory/(tag+'.ready')).exists() for tag in ('one','two')):
                    assert time.monotonic()<deadline;time.sleep(.02)
                (directory/'go').write_text('go')
                for process in processes:assert process.wait(timeout=15)==0
            finally:
                for process in processes:
                    if process.poll() is None:process.terminate();process.wait()
            current=reader.load_notes()
            assert {c['who'] for c in current['books']['test.txt']['highlights'][1]['thread']}=={'one','two'}
            assert sorted((directory/(tag+'.result')).read_text() for tag in ('one','two'))==['first','retried']
            assert contains(current['books'],seed()['books'])
    finally:reader.DATA_DIR,reader.NOTES_FILE=old
    print('PASS: arbitrary/revoked identities, unknown authors, long history/extras, stable IDs, exact upgrade backup, corrupt store refusal, stale snapshots, failed-write retry, cooperating process CAS')

def integration(browser=True,html_override=None):
    with tempfile.TemporaryDirectory() as folder:
        base=Path(folder)
        for name in ('reader_server.py', 'member_store.py','chapter_index.py','progress_catalog.py','navigation.py'):shutil.copy(ROOT/name,base/name)
        shutil.copytree(ROOT/'web',base/'web');(base/'data').mkdir();(base/'书库').mkdir()
        if html_override:shutil.copyfile(html_override,base/'web/reader.html')
        (base/'书库/test.txt').write_text(BOOK,encoding='utf-8')
        ids={'user':'owner','004':'peer'};credentials=base/'data/identities.json';credentials.write_text(json.dumps(ids))
        path=base/'data/notes.json';raw=json.dumps(seed(),ensure_ascii=False).encode();path.write_bytes(raw)
        progress=base/'data/progress.json';progress.write_text(json.dumps({'books':{'test.txt':{'offset':0,'revision':0,'anchor':'甲乙丙丁戊己庚辛。 第二段正文。','updated':'2026-10-12T00:00:00'}}},ensure_ascii=False));before_progress=progress.read_bytes()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}';log=open(base/'server.log','w+',encoding='utf-8')
        process=subprocess.Popen([sys.executable,str(base/'reader_server.py')],env=dict(os.environ,READER_CLOUD='1',READER_PORT=str(port)),stdout=log,stderr=log)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(route,data=None,token='owner',method=None):
            headers={'X-Who-Token':token} if token is not None else {}
            body=json.dumps(data).encode() if data is not None else None
            req=urllib.request.Request(origin+route,data=body,headers=headers,method=method)
            try:
                with opener.open(req,timeout=15) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as exc:return exc.code,json.load(exc)
        try:
            for _ in range(100):
                try:
                    if request('/api/books')[0]==200:break
                except OSError:pass
                time.sleep(.05)
            else:log.seek(0);raise AssertionError(log.read())
            for token in (None,'invalid'):
                assert request('/api/notes?name=test.txt',token=token)[0]==401
                assert request('/api/notes/comment',{'name':'test.txt'},token=token)[0]==401
            status,notes=request('/api/notes?name=test.txt',token='peer');assert status==200
            unknown=next(h for h in notes['highlights'] if h['id']=='h_unknown')
            assert unknown['author_id'] is None and len(unknown['thread'])==5 and path.read_bytes()==raw
            assert request('/api/notes/highlight?name=test.txt&id=h_unknown',method='DELETE')[0]==403
            assert request('/api/notes/comment/edit',{'name':'test.txt','id':'h_unknown','index':3,'text':'冒认未知作者'})[0]==403
            comment={'name':'test.txt','id':'h_unknown','who':'ai','name_ai':'004','text':'新的共读评论','author_id':'user'}
            status,added=request('/api/notes/comment',comment,token='peer');assert status==200,added
            assert added['annotation']['author_id']=='004' and contains(json.loads(path.read_bytes())['books'],seed()['books'])
            assert next((base/'data/notes-backups').glob('*.json')).read_bytes()==raw
            def add(i):return request('/api/notes/highlight',{'name':'test.txt','start':6,'end':8,'text':'庚辛','author_id':'004'})
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results=list(pool.map(add,range(6)))
            assert all(status==200 for status,_ in results)
            assert len({data['id'] for _,data in results})==6
            assert all(h['author_id']=='user' for h in request('/api/notes?name=test.txt')[1]['highlights'] if h['id'] not in ('h_owned','h_unknown'))
            def own_comment(text):
                status,result=request('/api/notes/comment',{'name':'test.txt','id':'h_owned','who':'user','text':text})
                assert status==200
                return result['annotation']['annotation_id']
            one,two=own_comment('第一条'),own_comment('第二条')
            assert request('/api/notes/comment/delete',{'name':'test.txt','id':'h_owned','index':0,'annotation_id':one})[0]==200
            assert request('/api/notes/comment/edit',{'name':'test.txt','id':'h_owned','index':1,'annotation_id':two,'text':'第二条已修改'})[0]==200
            assert request('/api/notes/comment/delete',{'name':'test.txt','id':'h_owned','index':1,'annotation_id':two})[0]==200
            remaining=own_comment('新追加的第三条')
            assert request('/api/notes/comment/delete',{'name':'test.txt','id':'h_owned','index':0,'annotation_id':two})[0]==409
            assert request('/api/notes/comment/edit',{'name':'test.txt','id':'h_owned','annotation_id':remaining,'text':'冒名修改'},token='peer')[0]==403
            assert request('/api/notes/annotations?name=test.txt&highlight_id=h_owned')[1]['annotations'][0]['text']=='新追加的第三条'
            if browser:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    chrome=p.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    for mobile in (True,False):
                        context=chrome.new_context(viewport={'width':390 if mobile else 1280,'height':844},is_mobile=mobile,has_touch=mobile)
                        page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(origin+'/?who_token=owner');page.wait_for_function('notesCache.list.some(h=>h.id==="h_unknown")')
                        page.evaluate('openHlPanel(notesCache.list.find(h=>h.id==="h_unknown"))')
                        assert '历史作者未知' in page.locator('#hlPanelTitle').inner_text()
                        assert not page.locator('#btnDelHighlight').is_visible()
                        assert 'retired_agent' in page.locator('#hlPanelThread').inner_text()
                        assert page.locator('#hlPanelThread [onmouseover]').count()==0
                        assert page.evaluate('typeof __injected')=='undefined'
                        assert not errors,errors
                        context.close()
                    chrome.close()
            credentials.write_text(json.dumps({'user':'owner'}))
            assert request('/api/notes?name=test.txt',token='peer')[0]==401
            assert len(next(h for h in request('/api/notes?name=test.txt')[1]['highlights'] if h['id']=='h_unknown')['thread'])==6
            saved=path.read_bytes();path.write_bytes(b'{invalid')
            assert request('/api/notes?name=test.txt')[0]==503
            assert request('/api/notes/highlight',{'name':'test.txt','start':0,'end':2,'text':'甲乙'})[0]==503
            assert path.read_bytes()==b'{invalid'
            if browser:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    chrome=p.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    page=chrome.new_page(viewport={'width':390,'height':844})
                    page.goto(origin+'/?who_token=owner')
                    page.locator('#errbar[data-kind="notes"]').wait_for()
                    assert '原文件已保留' in page.locator('#errbar').inner_text()
                    assert path.read_bytes()==b'{invalid'
                    path.write_bytes(saved);page.evaluate('loadHighlights("test.txt")')
                    page.wait_for_function('!document.querySelector("#errbar[data-kind=notes]")')
                    chrome.close()
            path.write_bytes(saved);assert request('/api/notes?name=test.txt')[0]==200
            assert progress.read_bytes()==before_progress
        finally:process.terminate();process.wait(timeout=10);log.close()
    print('PASS: actual HTTP read/save preservation, owner checks, spoof rejection, revoked credential denial, corrupt-store 503, concurrent threads, mobile/desktop unknown author and escaped historical who, unchanged legacy progress')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--worker',nargs=2);parser.add_argument('--no-browser',action='store_true');parser.add_argument('--html');args=parser.parse_args()
    if args.worker:worker(*args.worker)
    else:unit();integration(not args.no_browser,args.html)
