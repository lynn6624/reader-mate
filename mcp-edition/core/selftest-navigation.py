"""Disposable PR3 acceptance: native EPUB, metadata CAS, windows and real browser navigation."""
import argparse
import ast
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import reader_server as reader
from navigation import automatic,validate,reading_window
from tools.epub_navigation import extract_document

ROOT=Path(__file__).parent
CHROME='C:/Program Files/Google/Chrome/Application/chrome.exe'


def units(text):return len(text.encode('utf-16-le'))//2
def digest(raw):return 'sha256:'+hashlib.sha256(raw).hexdigest()
def quote(text,off):return text.encode('utf-16-le')[off*2:(off+40)*2].decode('utf-16-le',errors='ignore')


def epub(ncx=False,broken=None):
    files={
      'META-INF/container.xml':'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>',
      'OEBPS/book.opf':'''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>测试合集😀</dc:title></metadata><manifest>
      <item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>
      <item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>
      <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
      <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
      </manifest><spine toc="ncx"><itemref idref="one"/><itemref idref="two"/></spine></package>''',
      'OEBPS/one.xhtml':'<html><head><title>不应重复</title><style>不应出现</style></head><body><h1 id="one">青蛇 · 第一章</h1><p>😀这是<em>第一段</em>，带 &amp; 和&nbsp;空格。</p><p id="inner">续段<br/>跨行。</p></body></html>',
      'OEBPS/two.xhtml':'<html><body><h1 id="two">霸王别姬 · 正文</h1><p>第二篇正文。</p><p>末尾段落。</p></body></html>',
      'OEBPS/nav.xhtml':'<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol><li><span>青蛇（目录用名）</span><ol><li><a href="one.xhtml#one">第一章（修订）</a></li><li><a href="one.xhtml#inner">第二节</a></li></ol></li><li><a href="two.xhtml#two">霸王别姬</a></li></ol></nav></body></html>',
      'OEBPS/toc.ncx':'<ncx><navMap><navPoint><navLabel><text>青蛇</text></navLabel><content src="one.xhtml#one"/><navPoint><navLabel><text>第二节</text></navLabel><content src="one.xhtml#inner"/></navPoint></navPoint><navPoint><navLabel><text>霸王别姬</text></navLabel><content src="two.xhtml#two"/></navPoint></navMap></ncx>'}
    if ncx:files['OEBPS/book.opf']=files['OEBPS/book.opf'].replace('properties="nav"','')
    if broken=='missing':del files['OEBPS/two.xhtml']
    if broken=='fragment':files['OEBPS/nav.xhtml']=files['OEBPS/nav.xhtml'].replace('#two','#missing')
    if broken=='xml':files['OEBPS/two.xhtml']='<html><body><p>损坏'
    if broken=='entity':files['OEBPS/one.xhtml']='<!DOCTYPE html [<!ENTITY x "secret">]>'+files['OEBPS/one.xhtml']
    if broken=='oversize':files['OEBPS/two.xhtml']='x'*(4*1024*1024+1)
    if broken=='outside':files['OEBPS/nav.xhtml']=files['OEBPS/nav.xhtml'].replace('two.xhtml#two','https://example.com/private')
    if broken in ('image-cover','image-fragment'):
        files['OEBPS/book.opf']=files['OEBPS/book.opf'].replace('<item id="two"','<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/><item id="two"').replace('<itemref idref="one"/>','<itemref idref="cover"/><itemref idref="one"/>')
        files['OEBPS/cover.xhtml']='<html><body><img src="cover.jpg"/></body></html>'
        files['OEBPS/nav.xhtml']=files['OEBPS/nav.xhtml'].replace('one.xhtml#one','cover.xhtml'+('#missing' if broken=='image-fragment' else ''))
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype','application/epub+zip')
        for name,text in files.items():z.writestr(name,text)
    return stream.getvalue()


def unit_tests():
    collection='合集\n目录\n青蛇\n第一章\n第二章\n霸王别姬\n第一章\n第二章\n\n青蛇\n第一章\n甲。\n第二章\n乙。\n霸王别姬\n第一章\n丙。\n第二章\n丁。\n'
    nav=automatic(collection,digest(collection.encode()))
    titles=[i['title'] for i in nav['items']]
    assert titles==['青蛇','第一章','第二章','霸王别姬','第一章','第二章'],titles
    assert nav['items'][0]['offset']==units(collection[:collection.index('青蛇',collection.index('第二章')+4)])
    assert nav['items'][1]['parent_id']==nav['items'][0]['id']
    assert nav['items'][-1]['parent_id']==nav['items'][3]['id']
    nested='合集\n目录\n作品甲\n第一章\n第二章\n第三章\n作品乙\n\n作者介绍。\n目录\n第一章\n第二章\n第三章\n第一章\n正文甲。\n第二章\n正文乙。\n第三章\n正文丙。\n'
    mapped=automatic(nested,'nested')['items']
    assert mapped[0]['title']=='作品甲' and mapped[0]['kind']=='work'
    assert mapped[0]['offset']==mapped[1]['offset'] and mapped[1]['parent_id']==mapped[0]['id']
    assert not automatic('一段无章节的普通正文。\n继续阅读。','none')['items']
    assert not automatic('第一章\n第二章\n第三章','heading-list')['items']
    assert automatic('序\n序正文。\n第一章\n甲。\n后记\n乙。','preface')['items'][0]['title']=='序'
    for mode in (False,True):
        doc=extract_document(io.BytesIO(epub(ncx=mode)));text=doc['text'];items=doc['items']
        assert text.index('青蛇 · 第一章')<text.index('霸王别姬 · 正文')
        assert '不应出现' not in text and '第一段' in text and '\n\n' in text and '\xa0' in text
        assert items[0]['offset']==units(text[:text.index('青蛇 · 第一章')])
        assert items[-1]['offset']==units(text[:text.index('霸王别姬 · 正文')])
        assert items[1]['parent_id']==items[0]['id'] and validate(items,text)
        for i in items:assert quote(text,i['offset'])==i['quote']
    cover=extract_document(io.BytesIO(epub(broken='image-cover')))
    assert cover['items'][1]['offset']==cover['items'][0]['offset']
    assert any('纯图片扉页' in message for message in cover['warnings'])
    for mode in ('missing','fragment','xml','entity','oversize','outside','image-fragment'):
        try:extract_document(io.BytesIO(epub(broken=mode)))
        except ValueError:pass
        else:raise AssertionError('Incomplete/malformed EPUB accepted: '+mode)
    long=('😀第一章\n'+('这是长段正文。\n'*20000))
    previous=None
    for offset in (0,10000,60000,120000,units(long)):
        w=reading_window(long,offset)
        assert w['text']==long.encode('utf-16-le')[w['text_base']*2:w['window_end']*2].decode('utf-16-le')
        assert units(w['text'])<=60000 and w['window_start']<=min(offset,units(long)-1)<w['window_end']
    eof='第一章\n'+'甲'*140000+'无换行的末段'
    end=0
    while end<units(eof):
        w=reading_window(eof,end);assert w['window_start']<=end<w['window_end'];end=w['window_end']
    assert end==units(eof)
    print('PASS: TXT anthology hierarchy; native EPUB3/nav and EPUB2/NCX spine+fragment offsets; labels differing from body; malformed/incomplete/resource-limit failures; bounded UTF-16 windows')


def integration(browser=True):
    with tempfile.TemporaryDirectory() as directory:
        base=Path(directory)
        for n in ('reader_server.py', 'member_store.py','book_drafts.py','chapter_index.py','progress_catalog.py','navigation.py'):shutil.copy(ROOT/n,base/n)
        shutil.copytree(ROOT/'web',base/'web');shutil.copytree(ROOT/'tools',base/'tools')
        (base/'data').mkdir();(base/'书库').mkdir()
        (base/'data/identities.json').write_text(json.dumps({'user':'owner','004':'peer'}))
        small='序\r\n前置说明。\r\n第一章\r\n😀第一段。\r\n第二章\r\n第二段正文。\r\n'
        long='目录\n第一章\n第二章\n第三章\n\n第一章\n'+('甲😀长段落。\n'*10000)+'\n第二章\n'+('乙长段落。\n'*10000)+'\n第三章\n终章段落。\n'
        originals={'small.txt':small.encode(),'long.txt':long.encode()}
        many=''.join('第%d章\n正文第%d段。\n'%(i,i) for i in range(1,451))
        originals['many.txt']=many.encode()
        for name,raw in originals.items():(base/'书库'/name).write_bytes(raw)
        records={}
        for name,raw in originals.items():
            text=reader.norm_text(raw.decode());pos=reader.text_position(text,0)
            records[name]={'user':dict(offset=0,percent=0,chars=units(text),revision=3,updated='2026-10-21T12:00:00',book_revision=digest(raw),anchor=text[:40],paragraph_idx=0,char_offset=0),'004':dict(offset=5,percent=.02,revision=0,anchor='旧锚点保持原样',updated='2026-10-15T00:00:00')}
        store=base/'data/progress.json';store.write_text(json.dumps({'version':1,'books':records},ensure_ascii=False),encoding='utf-8')
        notes=base/'data/notes.json';notes.write_text(json.dumps({'version':2,'books':{}},ensure_ascii=False),encoding='utf-8')
        before=store.read_bytes();before_notes=notes.read_bytes()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}';env=dict(os.environ,READER_CLOUD='1',READER_PORT=str(port))
        log=open(base/'test.log','w+',encoding='utf-8');process=subprocess.Popen([sys.executable,str(base/'reader_server.py')],env=env,stdout=log,stderr=log)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path,data=None,who='user',raw=None):
            headers={'X-Who-Token':'owner' if who=='user' else 'peer' if who=='004' else 'bad'}
            body=json.dumps(data,ensure_ascii=False).encode() if data is not None else raw
            req=urllib.request.Request(origin+path,headers=headers,data=body)
            try:
                with opener.open(req,timeout=30) as r:return r.status,json.load(r)
            except urllib.error.HTTPError as exc:return exc.code,json.load(exc)
        try:
            for _ in range(100):
                try:
                    if request('/api/books')[0]==200:break
                except OSError:pass
                time.sleep(.05)
            else:log.seek(0);raise AssertionError(log.read())
            assert request('/api/navigation?name=small.txt',who='bad')[0]==401
            code,nav=request('/api/navigation?name=small.txt');assert code==200 and nav['items'][0]['title']=='序'
            cached={p:p.read_bytes() for p in (base/'data').glob('navigation/*.json')}
            assert request('/api/navigation?name=small.txt',data={},who='004')[0]==403
            draft=dict(name='small.txt',book_revision=nav['book_revision'],expected_revision=nav['revision'],items=copy.deepcopy(nav['items']))
            draft['items'][0]['title']='人工修正的序'
            assert request('/api/navigation',data=draft)[0]==428
            for fault in ('offset','parent','quote','id'):
                bad=copy.deepcopy(draft);item=bad['items'][0]
                if fault=='offset':item['offset']=9999999
                if fault=='parent':item['parent_id']=item['id']
                if fault=='quote':item['quote']='不符'
                if fault=='id':bad['items'].append(dict(item))
                assert request('/api/navigation',data=dict(bad,dry_run=True))[0]==400,fault
            code,preview=request('/api/navigation',data=dict(draft,dry_run=True));assert code==200
            assert all(p.read_bytes()==raw for p,raw in cached.items())
            draft['confirm_digest']=preview['preview_digest']
            assert request('/api/navigation',data=draft)[0]==200
            assert request('/api/navigation',data=draft)[0]==409
            new=request('/api/navigation?name=small.txt')[1];assert new['source']=='manual' and new['items'][0]['title']=='人工修正的序'
            assert store.read_bytes()==before and notes.read_bytes()==before_notes
            code,result=request('/api/import?name=native.epub&category=literature',raw=epub());assert code==200,result
            native=request('/api/navigation?name=native.txt')[1];assert native['source']=='epub' and len(native['items'])==4
            doc=request('/api/book?name=native.txt')[1]
            for item in native['items']:assert quote(doc['text'],item['offset'])==item['quote']
            for broken in ('missing','fragment','xml'):
                assert request('/api/import?name='+broken+'.epub',raw=epub(broken=broken))[0]==400
                assert not (base/'书库'/(broken+'.txt')).exists()
            assert request('/api/import?name=forbidden.epub',who='004',raw=epub())[0]==403
            listing=request('/api/books')[1];assert not any(b['name'].endswith('missing.txt') for b in listing['books'])
            longnav=request('/api/navigation?name=long.txt')[1]
            third=next(i for i in longnav['items'] if i['title']=='第三章')
            note_offset=units(long[:long.index('终章段落')])
            status,highlight=request('/api/notes/highlight',data=dict(name='long.txt',start=note_offset,end=note_offset+4,text='终章段落'))
            assert status==200
            assert request('/api/notes/comment',who='004',data=dict(name='long.txt',id=highlight['id'],who='ai',text='已有共读批注保留'))[0]==200
            before_notes=notes.read_bytes()
            window=request('/api/book?name=long.txt&window=1&offset='+str(third['offset']))[1]
            assert window['windowed'] and units(window['text'])<=60000 and window['text_base']>0
            assert quote(long,third['offset'])==third['quote']
            assert len(request('/api/book/search?name=long.txt&q='+urllib.parse.quote('终章段落'))[1]['hits'])==1
            # Holding the first request body keeps the import slot; a second upload is rejected.
            connection=http.client.HTTPConnection('127.0.0.1',port,timeout=10);body=epub()
            connection.putrequest('POST','/api/import?name=busy.epub');connection.putheader('X-Who-Token','owner');connection.putheader('Content-Length',str(len(body)));connection.endheaders();time.sleep(.15)
            assert request('/api/import?name=second.epub',raw=body)[0]==409
            connection.send(body);response=connection.getresponse();assert response.status==200;response.read();connection.close()
            if browser:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    chrome=p.chromium.launch(executable_path=CHROME,headless=True)
                    try:
                        for mobile in (True,False):
                            context=chrome.new_context(viewport={'width':390 if mobile else 1280,'height':844},is_mobile=mobile,has_touch=mobile)
                            tree=ast.parse((ROOT/'selftest-tts.py').read_text(encoding='utf-8'))
                            mock=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign) and any(getattr(t,'id',None)=='MOCK_SPEECH' for t in node.targets))
                            context.add_init_script('('+mock+')()')
                            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                            page.goto(origin+'/?who_token=owner');page.wait_for_function('state.book!==null')
                            page.evaluate("openBook('long.txt')");page.wait_for_function("state.book.name==='long.txt' && state.book.windowed")
                            page.wait_for_timeout(1500)
                            stable=store.read_bytes();stable_notes=notes.read_bytes()
                            page.locator('#btnToc').click();page.locator('.toc-jump').filter(has_text='第三章').wait_for()
                            page.locator('#tocClose').click();page.wait_for_timeout(1400)
                            assert store.read_bytes()==stable,'Opening/closing TOC saved progress'
                            page.locator('#btnToc').click();page.locator('.toc-jump').filter(has_text='第三章').click()
                            page.wait_for_function('state.temporaryView && state.book.text_base>0')
                            assert page.evaluate('state.segs[state.currentSeg].start')==third['offset']
                            assert page.evaluate('state.book.text.length')<=60000
                            page.locator('mark[data-hl-id="'+highlight['id']+'"]').wait_for()
                            assert page.locator('mark[data-hl-id="'+highlight['id']+'"]').inner_text()=='终章段落'
                            page.wait_for_timeout(1500);assert store.read_bytes()==stable
                            assert notes.read_bytes()==stable_notes
                            page.locator('#btnFontInc').click();page.locator('#btnFontDec').click()
                            assert not errors,errors
                            # Explicitly saving the destination is the only navigation-triggered write.
                            page.locator('#btnKeepPosition').click();page.wait_for_function('!state.temporaryView && !saveInFlight')
                            assert request('/api/reading-progress?name=long.txt')[1]['progress']['offset']==third['offset']
                            page.locator('#btnToc').click();page.locator('.toc-jump').filter(has_text='第一章').click()
                            page.wait_for_function('state.book.text_base===0 && state.temporaryView')
                            # Crossing a preloaded body window still queues speech in the end callback.
                            reading_offset=request('/api/reading-progress?name=long.txt')[1]['progress']['offset']
                            page.evaluate('ttsStart(state.segs[state.segs.length-1].start)')
                            page.wait_for_function('speechWindow && speechWindow.data && __spoken.length===1')
                            page.evaluate('__finish()')
                            page.wait_for_function('__spoken.length===2 && state.book.text_base>0')
                            page.evaluate('ttsStopInternal(true)')
                            page.wait_for_timeout(100)
                            assert request('/api/reading-progress?name=long.txt')[1]['progress']['offset']==reading_offset
                            # Real owner JSON preview/apply UI; metadata only, no progress changes.
                            page.evaluate("openBook('small.txt')");page.wait_for_function("state.book.name==='small.txt'")
                            page.locator('#btnToc').click();page.locator('#tocAdmin').evaluate('(el)=>el.open=true')
                            edit=request('/api/navigation?name=small.txt')[1]
                            payload=dict(name='small.txt',book_revision=edit['book_revision'],expected_revision=edit['revision'],items=edit['items'])
                            payload['items'][0]['title']='界面修正的序'
                            plain=reader.norm_text(small)
                            midpoint=units(plain[:plain.index('第二段正文')+2])
                            payload['items'][0].update(offset=midpoint,quote=quote(plain,midpoint))
                            stable=store.read_bytes()
                            page.locator('#tocFile').set_input_files({'name':'navigation.json','mimeType':'application/json','buffer':json.dumps(payload,ensure_ascii=False).encode()})
                            page.locator('#tocApply').wait_for(state='visible');assert store.read_bytes()==stable
                            page.locator('#tocApply').click();page.wait_for_function('revision=>tocState.revision===revision',arg=edit['revision']+1)
                            assert store.read_bytes()==stable
                            page.locator('.toc-jump').filter(has_text='界面修正的序').click()
                            page.wait_for_function('off=>state.preciseOffset===off && state.temporaryView',arg=midpoint)
                            page.locator('#btnKeepPosition').click();page.wait_for_function('!saveInFlight')
                            assert request('/api/reading-progress?name=small.txt')[1]['progress']['offset']==midpoint
                            # Hundreds of rows are paged without an unbounded DOM.
                            page.evaluate("openBook('many.txt')");page.wait_for_function("state.book.name==='many.txt'")
                            page.locator('#btnToc').click();page.wait_for_function('document.querySelectorAll(".toc-jump").length===200')
                            page.get_by_role('button',name='下一页目录',exact=True).click()
                            page.wait_for_function('tocState.page===1 && document.querySelectorAll(".toc-jump").length===200')
                            page.get_by_role('button',name='下一页目录',exact=True).click()
                            page.wait_for_function('tocState.page===2 && document.querySelectorAll(".toc-jump").length===50')
                            page.evaluate("openBook('native.txt')");page.wait_for_function("state.book.name==='native.txt'")
                            page.locator('#btnToc').click();page.wait_for_function('document.querySelectorAll(".toc-jump").length===4')
                            page.locator('.toc-toggle').first.click();assert page.locator('.toc-jump').count()==2
                            page.locator('.toc-toggle').first.click();assert page.locator('.toc-jump').count()==4
                            assert not errors,errors
                            context.close()
                    finally:chrome.close()
            assert all((base/'书库'/n).read_bytes()==raw for n,raw in originals.items())
            # Only user explicit saves above may change; legacy reader and notes stay untouched.
            after=json.loads(store.read_bytes())['books']
            assert all(after[n]['004']==records[n]['004'] for n in originals)
            assert notes.read_bytes()==before_notes
            (base/'书库/small.txt').write_bytes(originals['small.txt']+b'changed')
            assert request('/api/navigation?name=small.txt')[1]['status']=='content_changed'
            assert request('/api/navigation',data=draft)[0]==409
        finally:
            process.terminate();process.wait(timeout=10);log.close()
    print('PASS: real HTTP imports/native sidecar; no partial files; owner-only preview/CAS edits; content-change guard; serialized uploads; bounded long-book+search; desktop/mobile TOC and 450-row paging; JSON edit UI; precise mid-paragraph save; speech window handoff; unchanged original TXT/legacy progress/notes')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    unit_tests();integration(not args.no_browser)
