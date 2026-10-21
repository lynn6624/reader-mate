"""Disposable actual HTTP/MCP tests for bounded resume and legacy validity."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import reader_server as reader
from progress_catalog import verified_position, saved_time

ROOT = Path(__file__).parent
TEXT = '第一章\n' + ('正文跨行。\n接着读书。\n' * 100)
TOTAL = len(TEXT.encode('utf-16-le'))//2
HASH = 'sha256:' + hashlib.sha256(TEXT.encode()).hexdigest()


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); return sock.getsockname()[1]


def record(offset, updated='2026-10-21T12:11:30', legacy=False, **extra):
    p = reader.text_position(TEXT,offset)
    out = dict(offset=offset, percent=offset/TOTAL, chars=TOTAL, updated=updated,
               revision=0 if legacy else 2, anchor=TEXT[p[3]:p[3]+40],
               paragraph_idx=p[0], char_offset=p[1])
    if not legacy:
        out['book_revision'] = HASH
    out.update(extra)
    return out


async def run():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        for name in ('reader_server.py','mcp_server.py','chapter_index.py','progress_catalog.py','MCP工具标准调用参考_只读.txt'):
            if (ROOT/name).exists(): shutil.copy(ROOT/name,base/name)
        shutil.copytree(ROOT/'web',base/'web')
        (base/'data').mkdir(); (base/'书库').mkdir(); (base/'state').mkdir()
        (base/'data/identities.json').write_text(json.dumps({n:'test-'+n for n in ('user','op001','004')}))
        key='T'*24; urlkey='G'*24
        (base/'state/key').write_text(key); (base/'state/urlkey').write_text(urlkey)
        books = {f'own-{i:02}.txt':{'op001':record(4, f'2026-10-21T12:{i:02}:00')} for i in range(22)}
        books.update({
            'valid-user.txt':{'user':record(40,'2026-10-20T10:00:00')},
            'exact-one-percent.txt':{'user':record(1,'2026-10-19T10:00:00')},
            'accidental-open.txt':{'user':record(1,'2026-10-21T23:59:59')},
            'empty.txt':{'user':{},'004':{}},
            'anchor-only.txt':{'user':{'anchor':'正文','updated':'2026-10-22T00:00:00'},'op001':{'anchor':'正文'}},
            'legacy.txt':{'004':record(40,'2026-10-21T12:11:30',legacy=True)},
            'real-zero.txt':{'004':record(0,'2026-10-20T10:00:00')},
            'pending.txt':{'004':record(50,'2026-10-23T10:00:00',legacy=True,anchor='不相符的旧锚点')},
            'changed.txt':{'004':record(50,'2026-10-23T10:00:00',book_revision='sha256:stale')},
            'bad-coordinates.txt':{'004':record(50,'2026-10-23T10:00:00',char_offset=9999)},
            'zero-init.txt':{'004':{'offset':0,'percent':0,'chars':TOTAL,'revision':0}},
            'flat.txt':record(30,'2026-10-18T10:00:00',legacy=True),
        })
        for name in books:
            (base/'书库'/name).write_bytes(TEXT.encode())
        # Exactly 1% is not rounded to zero or filtered at the boundary.
        exact='字'*100
        (base/'书库/exact-one-percent.txt').write_bytes(exact.encode())
        books['exact-one-percent.txt']['user'].update(chars=100,percent=.01,paragraph_idx=0,char_offset=1,
            anchor=exact[1:41],book_revision='sha256:'+hashlib.sha256(exact.encode()).hexdigest())
        store = base/'data/progress.json'
        store.write_text(json.dumps({'version':1,'books':books},ensure_ascii=False),encoding='utf-8')
        before = store.read_bytes()
        hp,mp=port(),port(); origin=f'http://127.0.0.1:{hp}'
        env=dict(os.environ,READER_CLOUD='1',READER_PORT=str(hp),READER_MCP_PORT=str(mp),
                 READER_API_URL=origin,READER_MCP_PATH_KEY_FILE=str(base/'state/key'),
                 READER_MCP_004_KEY_FILE=str(base/'state/urlkey'))
        logs=open(base/'test.log','w+',encoding='utf-8')
        processes=[subprocess.Popen([sys.executable,str(base/n)],env=env,stdout=logs,stderr=logs)
                   for n in ('reader_server.py','mcp_server.py')]
        try:
            async with httpx.AsyncClient(trust_env=False) as http:
                for _ in range(100):
                    try:
                        if (await http.get(origin+'/api/ping')).status_code==200 and (await http.post(f'http://127.0.0.1:{mp}/mcp')).status_code==404:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.1)
                else:
                    logs.seek(0); raise AssertionError(logs.read())
                for path in ('/api/reading-progress/resume','/api/books?progress_scope=self'):
                    assert (await http.get(origin+path)).status_code==401
                for extra in ('limit=20','page=2','offset=1','include_below_threshold=true','reader_id=user&reader_id=004'):
                    response=await http.get(origin+'/api/reading-progress/resume?'+extra,headers={'X-Who-Token':'test-004'})
                    assert response.status_code==400, response.text
            async def visit(who):
                async with httpx.AsyncClient(trust_env=False,headers={'Authorization':'Bearer test-'+who}) as hc:
                    async with streamable_http_client(f'http://127.0.0.1:{mp}/mcp/{key}',http_client=hc) as (r,w,_):
                        async with ClientSession(r,w) as session:
                            await session.initialize()
                            tools=(await session.list_tools()).tools
                            schema=next(t.inputSchema for t in tools if t.name=='list_reading_progress')
                            assert 'name' not in schema.get('required',[]) and 'reader_id' in schema['properties']
                            assert set(schema['properties'])=={'name','reader_id'}
                            get=next(t.inputSchema for t in tools if t.name=='get_reading_progress')
                            assert 'name' in get['required']
                            async def call(tool,args={}):
                                value=await session.call_tool(tool,args)
                                assert not value.isError,value
                                return value.structuredContent or json.loads(value.content[0].text)
                            guide=await call('get_calling_guide')
                            assert 'list_reading_progress({})' in guide['text'] and '1%' in guide['text']
                            own=await call('list_reading_progress')
                            assert own['viewer_id']==who and own['reader_id']==who
                            if who=='op001':
                                assert len(own['entries'])==20 and own['entries'][0]['name']=='own-21.txt'
                                assert own['entries'][-1]['name']=='own-02.txt'
                                assert own['entries'][0]['percent'] < .01
                            else:
                                assert [e['name'] for e in own['entries']]==['legacy.txt','real-zero.txt']
                                assert own['entries'][0]['revision']==0
                                assert own['entries'][0]['location_status']=='legacy_anchor_verified'
                                assert own['entries'][1]['offset']==0
                                assert own['entries'][0]['last_read_at']=='2026-10-21T12:11:30+08:00'
                            peer=await call('list_reading_progress',{'reader_id':'user'})
                            assert len(peer['entries'])==1 and peer['entries'][0]['name']=='valid-user.txt'
                            assert peer['max_entries']==1 and peer['reader_id']=='user'
                            assert (await call('list_reading_progress',{'reader_id':'op001'}))['entries'][0]['name']=='own-21.txt'
                            for extra in ({'limit':20},{'page':2},{'offset':10},{'include_below_threshold':True}):
                                result=await session.call_tool('list_reading_progress',dict(reader_id='user',**extra))
                                if not result.isError:
                                    same=result.structuredContent or json.loads(result.content[0].text)
                                    assert same['entries']==peer['entries']
                            shared=await call('list_reading_progress',{'name':'accidental-open.txt'})
                            assert shared['progress'][0]['percent'] < .01  # original per-book visibility
                            target=await call('get_reading_progress',{'name':'legacy.txt','reader_id':'004'})
                            assert target['progress']['offset']==40 and target['revision']==0
                            listing=await call('list_books')
                            assert listing['viewer_id']==who and listing['progress_scope']=='self'
                            indexed={b['name']:b for b in listing['books']}
                            assert indexed['own-21.txt']['offset']==(4 if who=='op001' else None)
                            await call('read_book',{'name':'empty.txt','length':20})
                            return indexed
            await visit('op001'); await visit('004')
            assert store.read_bytes()==before, 'queries/read_book mutated reading data'
            async with httpx.AsyncClient(trust_env=False,headers={'X-Who-Token':'test-user'}) as owner:
                own=(await owner.get(origin+'/api/reading-progress/resume')).json()
                assert own['max_entries']==20 and len(own['entries'])==3
                assert all(e['percent']>=.01 for e in own['entries'])
            async with httpx.AsyncClient(trust_env=False,headers={'X-Who-Token':'test-004'}) as http:
                human=(await http.get(origin+'/api/books')).json()
                assert 'progress_scope' not in human
                assert next(b for b in human['books'] if b['name']=='empty.txt')['percent']==0
                # Remove only temp candidates to exercise filter-before-latest and empty result.
                changed=json.loads(before)
                del changed['books']['valid-user.txt']
                store.write_text(json.dumps(changed),encoding='utf-8')
                result=(await http.get(origin+'/api/reading-progress/resume?reader_id=user')).json()
                assert result['entries'][0]['name']=='exact-one-percent.txt'
                for n in ('exact-one-percent.txt','flat.txt'): del changed['books'][n]
                store.write_text(json.dumps(changed),encoding='utf-8')
                result=(await http.get(origin+'/api/reading-progress/resume?reader_id=user')).json()
                assert result['entries']==[] and 'accidental-open.txt' not in json.dumps(result)
            assert saved_time('2026-10-21T12:11:30').isoformat()=='2026-10-21T12:11:30+08:00'
            assert saved_time('2026-10-21T04:11:30+00:00').isoformat()=='2026-10-21T12:11:30+08:00'
        finally:
            for process in processes:
                process.terminate(); process.wait(timeout=10)
            logs.close()
    print('PASS: actual MCP optional schema; own latest20, peers latest1; user >=1% before latest; revision0/newline anchors/real0; pending excluded; no read writes; timezone; scoped listings; old HTTP/per-book compatibility')


def fixtures(path):
    root=Path(path); baseline=json.loads((root/'baseline.json').read_text(encoding='utf-8'))
    result=[]
    for label,who in (('locked','user'),('no-toc','user'),('webnovel','user'),('no-toc','004'),('crowd','user')):
        name=baseline['selected'][label]; raw=baseline['progress'][name][who]
        text=(root/(label+'.txt')).read_text(encoding='utf-8-sig')
        revision=raw.get('book_revision') or 'legacy-fixture'
        found=verified_position(raw,text,revision,reader.text_position,
            lambda value,content,version:reader.progress_view(value,who,content,version))
        if label=='crowd': assert found is None; continue
        assert found and abs(found['percent']-raw['percent'])<1e-12
        if who=='004': assert found['revision']==0 and found['location_status']=='legacy_anchor_verified'
        result.append((label,who,found))
    user=[entry for label,who,entry in result if who=='user' and entry['percent']>=.01]
    assert len(user)==2 and max(user,key=lambda e:e['last_read_at'])['offset']==6260
    assert next(e for label,who,e in result if label=='webnovel')['percent']<.01
    print('PASS: private cloud fixtures locked6260 / elevator15915 qualify; webnovel2636 excluded; 004 legacy4281 revision0 verified; empty crowd user excluded')


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--fixtures'); args=parser.parse_args()
    asyncio.run(run())
    if args.fixtures: fixtures(args.fixtures)
