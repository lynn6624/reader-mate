"""Read cgroup memory for a separately launched, synthetic Web/MCP/portal fixture.

The workload controller stays outside the service cgroup. This test never creates,
changes or stops units, never reads production data, and reports its exact scope.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from oauth_store import Store
from oauth_testbed import load


def require(value, label):
    if not value:
        raise AssertionError(label)


def counters(path):
    return {key:int(value) for key,value in (line.split() for line in path.read_text().splitlines())}


def authorize(spec, root, subject):
    issuer = spec['issuer']; endpoint = 'http://127.0.0.1:'+str(spec['mcp_port'])
    store=Store(root/'state'/'oauth.sqlite3',issuer,spec['resource'])
    with store.transaction(False) as db:
        registered=[value for _,value in store.rows(db,'client')
                    if value['redirect_uris']==['https://client.example.test/callback']]
    with httpx.Client(trust_env=False,headers={'Host':urlsplit(issuer).netloc},timeout=30) as client:
        if registered:
            cid=registered[0]['client_id']
        else:
            registration = client.post(endpoint+'/oauth/register',json={'redirect_uris':['https://client.example.test/callback'],
                'grant_types':['authorization_code','refresh_token']})
            require(registration.status_code==201, 'DCR')
            cid = registration.json()['client_id']
        verifier = secrets.token_urlsafe(48)
        params = dict(client_id=cid,response_type='code',redirect_uri='https://client.example.test/callback',
            resource=spec['resource'],scope='reader:mcp offline_access',state=secrets.token_urlsafe(24),
            code_challenge=create_s256_code_challenge(verifier),code_challenge_method='S256')
        page = client.get(endpoint+'/oauth/authorize',params=params)
        require(page.status_code==200, 'authorize_page')
        csrf = re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        cookie = page.headers['set-cookie'].split(';')[0]
        _, invitation = Store(root/'state'/'oauth.sqlite3',issuer,spec['resource']).invite(subject)
        headers={'Cookie':cookie}
        proof = client.post(endpoint+'/oauth/authorize',headers=headers,data=dict(csrf=csrf,action='verify',invite=invitation))
        require(proof.status_code==200, 'invitation')
        response = client.post(endpoint+'/oauth/authorize',headers=headers,data=dict(csrf=csrf,action='approve'))
        require(response.status_code==302, 'consent')
        code = parse_qs(urlsplit(response.headers['location']).query)['code'][0]
        token = client.post(endpoint+'/oauth/token',data=dict(grant_type='authorization_code',client_id=cid,code=code,
            code_verifier=verifier,redirect_uri='https://client.example.test/callback',resource=spec['resource']))
        require(token.status_code==200, 'exchange')
        return cid, token.json()


async def workload(spec, grants, rounds):
    url = 'http://127.0.0.1:'+str(spec['mcp_port'])+'/mcp'
    async def reader(subject, token):
        async with httpx.AsyncClient(trust_env=False,headers={'Host':urlsplit(spec['issuer']).netloc,
               'Authorization':'Bearer '+token['access_token']}, timeout=60) as transport:
            async with streamable_http_client(url,http_client=transport) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    require(len((await session.list_tools()).tools)==15, 'tools')
                    for _ in range(rounds):
                        listing = await session.call_tool('list_books',{})
                        require(not listing.isError and json.loads(listing.content[0].text)['viewer_id']==subject, 'identity')
                        read_book = await session.call_tool('read_book',{'name':'oauth-large.txt','offset':25,'length':3000})
                        require(not read_book.isError, 'large_read')
                        result = await session.call_tool('update_progress',{'name':'oauth-large.txt','offset':25})
                        require(not result.isError, 'progress')
    await asyncio.gather(*(reader(subject, token) for subject, (_,token) in grants.items()))


def run(args, report):
    root = args.fixture.resolve(); spec = load(root)
    require(spec['large'], 'large_synthetic_book_required')
    group = args.cgroup.resolve()
    require(str(group).startswith('/sys/fs/cgroup/') and 'oauth' in group.name, 'wrong_cgroup_scope')
    pids = json.loads((root/'pids.json').read_text())
    fixture_pids=([pids['launcher']] if pids.get('launcher') else [])+pids['children']
    for pid in fixture_pids:
        require(str(group.relative_to('/sys/fs/cgroup')) in Path('/proc',str(pid),'cgroup').read_text(), 'fixture_pid_not_in_group')
    require((group/'memory.max').read_text().strip()==str(256*1024*1024), 'budget_not_256MiB')
    require((group/'memory.high').read_text().strip()==str(192*1024*1024), 'soft_limit_not_192MiB')
    before = counters(group/'memory.events')
    report.update(scope=pids.get('budget_scope','synthetic Web + MCP OAuth + token portal + small launcher/loopback proxy; controller/tunnel excluded'),
                  memory_max_bytes=256*1024*1024,memory_high_bytes=192*1024*1024,
                  readers=3,rounds=args.rounds,
                  utf16_units=len((root/'core'/'书库'/'oauth-large.txt').read_text(encoding='utf-8').encode('utf-16-le'))//2)
    samples, stop = [], threading.Event()
    def sample():
        while not stop.is_set():
            try:
                samples.append(int((group/'memory.current').read_text()))
            except OSError:
                return
            stop.wait(.05)
    sampler = threading.Thread(target=sample,daemon=True); sampler.start()
    identities = root/'core'/'data'/'identities.json'
    identity_sha = hashlib.sha256(identities.read_bytes()).hexdigest()
    started = time.monotonic()
    try:
        with httpx.Client(trust_env=False,headers={'Host':urlsplit(spec['issuer']).netloc},timeout=2) as probe:
            for _ in range(100):
                try:
                    if probe.get('http://127.0.0.1:'+str(spec['mcp_port'])+'/.well-known/oauth-protected-resource/mcp').status_code==200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise AssertionError('fixture_startup_not_ready')
        grants = {subject:authorize(spec,root,subject) for subject in ('004','op004','op001')}
        asyncio.run(workload(spec,grants,args.rounds))
        endpoint='http://127.0.0.1:'+str(spec['mcp_port'])
        with httpx.Client(trust_env=False,headers={'Host':urlsplit(spec['issuer']).netloc},timeout=30) as client:
            for subject,(cid,token) in grants.items():
                renewed=client.post(endpoint+'/oauth/token',data=dict(grant_type='refresh_token',client_id=cid,
                    refresh_token=token['refresh_token'],resource=spec['resource']))
                require(renewed.status_code==200,'refresh')
                require(client.post(endpoint+'/oauth/revoke',data=dict(client_id=cid,
                    token=renewed.json()['access_token'])).status_code==200,'revoke')
                require(client.post(endpoint+'/mcp',headers={'Authorization':'Bearer '+token['access_token']}).status_code==401,'revoked_access')
            require(client.get('http://127.0.0.1:'+str(spec['portal_port'])+'/api/status').status_code==200,'portal_alive')
        require(hashlib.sha256(identities.read_bytes()).hexdigest()==identity_sha,'identity_changed')
    finally:
        stop.set();sampler.join(timeout=2)
        after=counters(group/'memory.events')
        peak_file=group/'memory.peak'
        report.update(elapsed_seconds=round(time.monotonic()-started,3),sampled_peak_bytes=max(samples,default=0),
            cgroup_peak_bytes=int(peak_file.read_text()) if peak_file.exists() else None,
            peak_source='memory.peak' if peak_file.exists() else '50ms sampling; kernel has no memory.peak',
            memory_event_delta={key:after[key]-before.get(key,0) for key in after})
    require(all(report['memory_event_delta'].get(key,0)==0 for key in ('max','oom','oom_kill')),'hard_limit_pressure_or_OOM')
    report['pass']=True


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',type=Path,required=True)
    parser.add_argument('--cgroup',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=2)
    args=parser.parse_args()
    report={'pass':False,'production_access':False}
    try:
        run(args,report)
    except Exception as error:
        report['failure_type']=type(error).__name__
        def failure_tree(value):
            if getattr(value,'exceptions',None):
                return [failure_tree(item) for item in value.exceptions]
            return {'type':type(value).__name__,'label':str(value) if isinstance(value,AssertionError) else None}
        report['failure_tree']=failure_tree(error)
        if isinstance(error,AssertionError):
            report['failure']=str(error)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=True,indent=2))
    print(json.dumps(report,ensure_ascii=True))
    raise SystemExit(0 if report['pass'] else 1)
