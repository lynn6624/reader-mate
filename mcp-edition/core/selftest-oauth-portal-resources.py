"""External controller: shared HTTPS portal/OAuth routing and bounded three-service workload."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import secrets
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from oauth_store import Store
from oauth_testbed import load


def check(ok, label):
    if not ok:
        raise AssertionError(label)


def events(group):
    return dict((k,int(v)) for k,v in (line.split() for line in (group/'memory.events').read_text().splitlines()))


async def readers(endpoint, host, verify, grants, rounds):
    async def one(subject, token):
        async with httpx.AsyncClient(trust_env=False,verify=verify,timeout=60,
            headers={'Host':host,'Authorization':'Bearer '+token['access_token']}) as client:
            async with streamable_http_client(endpoint+'/mcp',http_client=client) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    check(len((await session.list_tools()).tools)==15,'tool_count')
                    for _ in range(rounds):
                        result=await session.call_tool('list_books',{})
                        check(not result.isError and json.loads(result.content[0].text)['viewer_id']==subject,'identity')
                        result=await session.call_tool('read_book',dict(name='oauth-large.txt',offset=25,length=3000))
                        check(not result.isError,'large_read')
                        result=await session.call_tool('update_progress',dict(name='oauth-large.txt',offset=25))
                        check(not result.isError,'progress')
    await asyncio.gather(*(one(subject,token) for subject,(_,token) in grants.items()))


def run(args, report):
    root=args.fixture.resolve();spec=load(root);group=args.cgroup.resolve()
    check(spec['large'],'large_fixture_required')
    check(str(group).startswith('/sys/fs/cgroup/') and 'oauth' in group.name,'test_cgroup_required')
    check((group/'memory.max').read_text().strip()==str(256*1024*1024),'hard_limit')
    check((group/'memory.high').read_text().strip()==str(192*1024*1024),'soft_limit')
    pids=json.loads((root/'pids.json').read_text())
    for pid in pids['children']:
        check(str(group.relative_to('/sys/fs/cgroup')) in Path('/proc',str(pid),'cgroup').read_text(),'pid_scope')
    check(len(pids['children'])==3 and not pids.get('launcher'),'three_business_processes')
    credentials=json.loads(args.credentials.read_text())
    check(credentials['marker']=='reader-mate-oauth-portal-test-v1','test_credentials_marker')
    verify=ssl.create_default_context(cafile=str(args.ca))
    host=urlsplit(spec['issuer']).netloc
    identities=root/'core/data/identities.json'
    before_ids=identities.read_bytes()
    store=Store(root/'state/oauth.sqlite3',spec['issuer'],spec['resource'])
    # Preload more than one page outside the workload; plaintext values are never printed.
    for _ in range(55):store.invite('004')
    counts_before=events(group);samples=[];stop=threading.Event()
    def sample():
        while not stop.is_set():
            samples.append(int((group/'memory.current').read_text()));stop.wait(.05)
    sampler=threading.Thread(target=sample,daemon=True);sampler.start()
    started=time.monotonic();grants={};sensitive=[credentials['password']]
    try:
        with httpx.Client(trust_env=False,verify=verify,timeout=60,
                headers={'Host':host,'Origin':spec['issuer']}) as client:
            endpoint=args.endpoint.rstrip('/')
            def admin(action,**fields):
                response=client.post(endpoint+'/api/oauth/'+action,json=dict(password=credentials['password'],**fields))
                check(response.status_code==200,'admin_'+action)
                return response.json()
            check(client.get(endpoint+'/').status_code==200,'portal_route')
            check(client.post(endpoint+'/mcp').status_code==401,'mcp_route')
            check(client.get(endpoint+'/.well-known/oauth-protected-resource/mcp').json()['resource']==spec['resource'],'discovery_route')
            overview=admin('overview')
            check(len(overview['invites'])==50 and overview['invite_total']>=55,'bounded_page')
            check(admin('overview',page=1)['invites'],'second_page')
            registration=client.post(endpoint+'/oauth/register',json={'redirect_uris':['https://client.example.test/callback'],
                'grant_types':['authorization_code','refresh_token']})
            check(registration.status_code==201,'registration_route');cid=registration.json()['client_id']
            for subject in ('004','op004','op001'):
                invitation=admin('invite',reader_id=subject,ttl=3600,uses=1)['invitation']
                sensitive.append(invitation);verifier=secrets.token_urlsafe(48)
                params=dict(client_id=cid,response_type='code',redirect_uri='https://client.example.test/callback',
                    resource=spec['resource'],scope='reader:mcp',state=secrets.token_urlsafe(24),
                    code_challenge=create_s256_code_challenge(verifier),code_challenge_method='S256')
                page=client.get(endpoint+'/oauth/authorize',params=params)
                check(page.status_code==200,'authorize_page')
                csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
                # Request URL uses the loopback IP with the test hostname in Host. Cookies
                # remain under this same HTTPS endpoint; no redirect is followed.
                check(client.post(endpoint+'/oauth/authorize',data=dict(action='verify',csrf=csrf,invite=invitation)).status_code==200,'invitation')
                approval=client.post(endpoint+'/oauth/authorize',data=dict(action='approve',csrf=csrf))
                check(approval.status_code==302,'consent')
                code=parse_qs(urlsplit(approval.headers['location']).query)['code'][0];sensitive.append(code)
                token=client.post(endpoint+'/oauth/token',data=dict(grant_type='authorization_code',client_id=cid,code=code,
                    code_verifier=verifier,redirect_uri=params['redirect_uri'],resource=spec['resource']))
                check(token.status_code==200,'exchange')
                token=token.json();check(token['scope']=='reader:mcp' and 'refresh_token' in token,'refresh_consent')
                sensitive.extend([token['access_token'],token['refresh_token']]);grants[subject]=(cid,token)
            # Admin queries execute concurrently with three readers under the same cap.
            errors=[]
            def admin_load():
                try:
                    with httpx.Client(trust_env=False,verify=verify,timeout=60,
                            headers={'Host':host,'Origin':spec['issuer']}) as other:
                        for _ in range(6):
                            r=other.post(endpoint+'/api/oauth/overview',json={'password':credentials['password']})
                            check(r.status_code==200 and len(r.json()['invites'])<=50,'concurrent_admin')
                except Exception:errors.append('concurrent_admin_failed')
            thread=threading.Thread(target=admin_load);thread.start()
            try:asyncio.run(readers(endpoint,host,verify,grants,args.rounds))
            finally:thread.join(timeout=90)
            check(not thread.is_alive() and not errors,'concurrent_admin_finished')
            for subject,(_,token) in grants.items():
                refreshed=client.post(endpoint+'/oauth/token',data=dict(grant_type='refresh_token',client_id=cid,
                    refresh_token=token['refresh_token'],resource=spec['resource']))
                check(refreshed.status_code==200,'refresh')
                fresh=refreshed.json();sensitive.extend([fresh['access_token'],fresh['refresh_token']])
                with store.transaction(False) as db:
                    target=next(row['id'] for _,row in store.rows(db,'grant') if row['subject']==subject and not row.get('revoked'))
                admin('revoke-grant',grant_id=target)
                check(client.post(endpoint+'/mcp',headers={'Authorization':'Bearer '+fresh['access_token']}).status_code==401,'revoked_access')
                check(client.post(endpoint+'/oauth/token',data=dict(grant_type='refresh_token',client_id=cid,
                    refresh_token=fresh['refresh_token'],resource=spec['resource'])).status_code==400,'revoked_refresh')
            check(client.get(endpoint+'/api/status').status_code==200,'portal_still_online')
            check(identities.read_bytes()==before_ids,'identity_unchanged')
            report.update(shared_https_routes=True,portal_generated_invites=True,concurrent_admin=True,
                bounded_pages=True,readers=3,rounds=args.rounds,tool_count=15,refresh_and_admin_revocation=True,
                identity_unchanged=True,utf16_units=2000009)
    finally:
        stop.set();sampler.join(timeout=2);after=events(group)
        report.update(elapsed_seconds=round(time.monotonic()-started,3),sampled_peak_bytes=max(samples,default=0),
            cgroup_peak_bytes=int((group/'memory.peak').read_text()) if (group/'memory.peak').exists() else None,
            peak_source='50ms memory.current sampling; memory.peak reported separately when available',
            memory_event_delta={k:after[k]-counts_before.get(k,0) for k in after},
            memory_events_total=after,memory_max_bytes=256*1024*1024,memory_high_bytes=192*1024*1024,
            scope='synthetic Web + MCP OAuth + OAuth-enabled Token portal; controller and separate Caddy excluded')
    for log in (root/'logs').glob('*.log'):
        check(all(value.encode() not in log.read_bytes() for value in sensitive),'credential_in_log')
    check(all(report['memory_event_delta'].get(k,0)==0 for k in ('max','oom','oom_kill')),'hard_pressure_or_oom')
    report.update(pass_=True,logs_no_plaintext_credentials=True,
        tls='ephemeral test CA verified; loopback HTTPS endpoint with canonical Host; not public CA')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--fixture',type=Path,required=True)
    p.add_argument('--cgroup',type=Path,required=True);p.add_argument('--endpoint',required=True)
    p.add_argument('--ca',type=Path,required=True);p.add_argument('--credentials',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True);p.add_argument('--rounds',type=int,default=2)
    args=p.parse_args();report={'pass_':False,'production_access':False}
    try:run(args,report)
    except Exception as e:
        report['failure_type']=type(e).__name__
        if isinstance(e,AssertionError):report['failure']=str(e)
    args.report.write_text(json.dumps(report,indent=2))
    print(json.dumps(report));raise SystemExit(0 if report['pass_'] else 1)
