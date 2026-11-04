"""Synthetic portal -> invitation -> OAuth -> MCP -> portal revoke, with optional Chrome UI."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import member_store
from oauth_server import Config, create_server
from oauth_store import Store
from oauth_testbed import prepare, Services
from reader_oauth import IdentityAdapter


async def check_mcp(spec, token):
    async with httpx.AsyncClient(trust_env=False, headers={'Host':urlsplit(spec['issuer']).netloc,
            'Authorization':'Bearer '+token}) as client:
        async with streamable_http_client('http://127.0.0.1:'+str(spec['mcp_port'])+'/mcp',
                http_client=client) as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize()
                result=await session.call_tool('list_books',{})
                assert not result.isError
                assert json.loads(result.content[0].text)['viewer_id']=='004'


def run(browser, artifacts=None):
    # The administrator module must stay independent of Flask/Authlib's resident memory.
    probe=subprocess.run([sys.executable,'-c',
        "import sys,oauth_management; assert 'flask' not in sys.modules and 'authlib' not in sys.modules"],
        capture_output=True,cwd=Path(__file__).parent)
    assert probe.returncode==0
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)/'fixture'
        spec=prepare(root,'https://oauth-admin.example.test',access_ttl=900)
        ids=root/'core/data/identities.json'
        web=member_store.create(str(ids),'Web only','human','web')['member']['id']
        original=ids.read_bytes()
        history={name:b'{"books":{},"retained_history":true}' for name in ('notes.json','progress.json')}
        for name,raw in history.items():(ids.parent/name).write_bytes(raw)
        password='synthetic-admin-password'
        salt='0a'*16
        (ids.parent/'.token-admin.json').write_text(json.dumps(dict(salt=salt,
            hash=hashlib.sha256(bytes.fromhex(salt)+password.encode()).hexdigest())))
        base='http://127.0.0.1:'+str(spec['portal_port'])
        old={key:os.environ.get(key) for key in ('READER_OAUTH_MANAGEMENT','TOKEN_PUBLIC_ORIGIN')}
        os.environ.update(READER_OAUTH_MANAGEMENT='1',TOKEN_PUBLIC_ORIGIN=base)
        secrets_seen=[]
        try:
            with Services(root):
                with httpx.Client(trust_env=False,timeout=10) as client:
                    for _ in range(100):
                        try:
                            if client.get(base+'/api/status').status_code==200:break
                        except httpx.HTTPError:pass
                        time.sleep(.1)
                    else:raise AssertionError('portal startup')
                    def post(path, **body):
                        return client.post(base+'/api/oauth/'+path,json=dict(password=password,**body))
                    assert client.post(base+'/api/oauth/overview',json={}).status_code==403
                    assert client.post(base+'/api/oauth/overview',json={'password':password},
                        headers={'Origin':'https://evil.example'}).status_code==403
                    assert client.post(base+'/api/oauth/overview',content='password=x',
                        headers={'Content-Type':'text/plain'}).status_code==400
                    overview=post('overview')
                    assert overview.status_code==200
                    assert web not in {s['id'] for s in overview.json()['subjects']}
                    assert post('invite',reader_id=web).status_code==400
                    assert post('invite',reader_id='missing').status_code==400
                    assert post('invite',reader_id='004',ttl=True).status_code==400
                    result=post('invite',reader_id='004',ttl=3600,uses=1)
                    assert result.status_code==200
                    issued=result.json();invitation=issued['invitation'];secrets_seen.append(invitation)
                    assert invitation not in post('overview').text
                    config=Config(str(root/'state/oauth.sqlite3'),spec['issuer'],spec['resource'])
                    app,store=create_server(config,IdentityAdapter(str(ids)).lookup)
                    oauth=app.test_client()
                    cid=oauth.post('/oauth/register',base_url=spec['issuer'],json={
                        'redirect_uris':['https://client.example.test/callback'],
                        'grant_types':['authorization_code','refresh_token']}).json['client_id']
                    verifier=secrets.token_urlsafe(48)
                    page=oauth.get('/oauth/authorize',base_url=spec['issuer'],query_string=dict(
                        client_id=cid,response_type='code',redirect_uri='https://client.example.test/callback',
                        resource=spec['resource'],scope='reader:mcp',state='test-state',
                        code_challenge=create_s256_code_challenge(verifier),code_challenge_method='S256'))
                    csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
                    assert oauth.post('/oauth/authorize',base_url=spec['issuer'],
                        data=dict(action='verify',csrf=csrf,invite=invitation)).status_code==200
                    approved=oauth.post('/oauth/authorize',base_url=spec['issuer'],data=dict(action='approve',csrf=csrf))
                    code=parse_qs(urlsplit(approved.headers['Location']).query)['code'][0]
                    response=oauth.post('/oauth/token',base_url=spec['issuer'],data=dict(
                        grant_type='authorization_code',client_id=cid,code=code,code_verifier=verifier,
                        redirect_uri='https://client.example.test/callback',resource=spec['resource']))
                    assert response.status_code==200
                    token=response.json;secrets_seen.extend([code,token['access_token'],token['refresh_token']])
                    asyncio.run(check_mcp(spec,token['access_token']))
                    view=post('overview').json()
                    assert view['invites'][0]['remaining']==0
                    assert all(secret not in json.dumps(view) for secret in secrets_seen)
                    # Revoke an invitation independently; the active client remains valid.
                    extra=post('invite',reader_id='004').json()
                    assert post('revoke-invite',invite_id=extra['invite_id']).status_code==200
                    asyncio.run(check_mcp(spec,token['access_token']))
                    assert post('revoke-grant',grant_id=view['grants'][0]['id']).status_code==200
                    assert store.access(token['access_token'],spec['resource']) is None
                    assert oauth.post('/oauth/token',base_url=spec['issuer'],data=dict(
                        grant_type='refresh_token',client_id=cid,refresh_token=token['refresh_token'],
                        resource=spec['resource'])).status_code==400
                    assert post('revoke-grant',grant_id=extra['invite_id']).status_code==400
                    if browser:
                        from playwright.sync_api import sync_playwright
                        with sync_playwright() as p:
                            options={'headless':True}
                            if os.name=='nt':options['executable_path']=os.environ.get('CHROME_TEST',
                                'C:/Program Files/Google/Chrome/Application/chrome.exe')
                            chrome=p.chromium.launch(**options)
                            try:
                                for width in (1280,390):
                                    context=chrome.new_context(viewport={'width':width,'height':900})
                                    page=context.new_page();page.goto(base)
                                    page.locator('#adminPw').fill(password)
                                    page.locator('#oauthRefreshBtn').click()
                                    page.locator('#oauthControls').wait_for(state='visible')
                                    page.select_option('#oauthReader','004')
                                    page.locator('#oauthInviteBtn').click()
                                    page.locator('#oauthSecretBox').wait_for(state='visible')
                                    secret=page.locator('#oauthSecret').inner_text();assert secret.startswith('rmi_')
                                    secrets_seen.append(secret)
                                    page.locator('#oauthDismissBtn').click()
                                    assert page.locator('#oauthSecret').inner_text()==''
                                    if artifacts:
                                        artifacts.mkdir(parents=True,exist_ok=True)
                                        page.screenshot(path=str(artifacts/('portal-'+str(width)+'.png')),full_page=True)
                                    assert page.evaluate('Object.keys(localStorage).length')==0
                                    assert page.evaluate('document.documentElement.scrollWidth')<=width
                                    page.locator('#adminPw').fill('changed-password')
                                    assert page.locator('#oauthControls').is_hidden()
                                    context.close()
                            finally:chrome.close()
                    # Paging keeps one management response bounded.
                    for _ in range(51):store.invite('004')
                    assert len(post('overview').json()['invites'])==50
                    assert post('overview',page=1).json()['invites']
                    assert ids.read_bytes()==original
                    assert all((ids.parent/name).read_bytes()==raw for name,raw in history.items())
                    (root/'state/oauth.sqlite3').write_bytes(b'corrupt-test-database')
                    assert post('overview').status_code==503
        finally:
            for key,value in old.items():
                if value is None:os.environ.pop(key,None)
                else:os.environ[key]=value
        for file in (root/'logs').glob('*.log'):
            assert all(secret.encode() not in file.read_bytes() for secret in secrets_seen)
    print('PASS OAuth management: admin/CSRF guards, invite -> OAuth -> MCP, isolated revoke, no identity changes/log secrets, paging'+
          (', desktop/mobile Chrome' if browser else '; browser explicitly skipped'))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--artifacts',type=Path)
    args=parser.parse_args()
    run(not args.no_browser,args.artifacts)
