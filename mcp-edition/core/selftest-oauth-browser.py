"""Real Chrome + TLS OAuth acceptance on synthetic data; no model API or production access."""
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from playwright.sync_api import sync_playwright

from oauth_server import COOKIE
from oauth_store import Store, digest, now
from oauth_testbed import Services, free_port, prepare


def require(value, message):
    if not value:
        raise AssertionError(message)


def certificate(root):
    import ipaddress
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ReaderMate synthetic test CA')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('oauth-local.example.test'),
            x509.DNSName('oauth-client.example.test'),
            x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False).sign(key, hashes.SHA256()))
    cert_path, key_path = root/'test-ca.pem', root/'test-key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM)); cert_path.chmod(0o600)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption())); key_path.chmod(0o600)
    spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return cert_path, key_path, base64.b64encode(hashlib.sha256(spki).digest()).decode()



from oauth_test_transport import bridge


async def mcp_check(url, headers, verify, expected, save=False):
    async with httpx.AsyncClient(trust_env=False, headers=headers, verify=verify) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                require(len((await session.list_tools()).tools)==15, 'MCP_tool_count')
                async def call(name, arguments):
                    result = await session.call_tool(name, arguments)
                    require(not result.isError, 'MCP_tool_failed_'+name)
                    return json.loads(result.content[0].text)
                require((await call('list_books', {}))['viewer_id']==expected, 'MCP_identity')
                if save:
                    await call('read_book', {'name':'oauth-stage2.txt','offset':0,'length':40})
                    await call('update_progress', {'name':'oauth-stage2.txt','offset':6})
                    note = await call('add_highlight', {'name':'oauth-stage2.txt','start':4,'end':6,'text':'你好'})
                    await call('add_comment', {'name':'oauth-stage2.txt','highlight_id':note['id'],'text':'合成浏览器测试'})
                    require((await call('get_reading_progress', {'name':'oauth-stage2.txt'}))['progress']['offset']==6,
                            'progress_continuity')


def run_mcp(*args):
    # Playwright sync owns an event loop; the SDK runs in a separate thread.
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: asyncio.run(mcp_check(*args))).result()


def exercise(spec, root, endpoint, verify, browser_args, screenshots, report, invitations=None):
    issuer = spec['issuer']
    callback = spec.get('callback', issuer+'/client-callback')
    report['cross_origin_callback'] = urlsplit(callback).netloc != urlsplit(issuer).netloc
    host = urlsplit(issuer).netloc
    store = Store(root/'state'/'oauth.sqlite3', issuer, spec['resource']) if invitations is None else None
    all_secrets = (list(json.loads((root/'core'/'data'/'identities.json').read_text(encoding='utf-8')).values())
                   if store else [])
    before_ids = hashlib.sha256((root/'core'/'data'/'identities.json').read_bytes()).hexdigest() if store else None
    with httpx.Client(trust_env=False, headers={'Host':host}, verify=verify, timeout=30) as client:
        for _ in range(100):
            try:
                response = client.post(endpoint+'/mcp')
                if response.status_code == 401:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        else:
            raise AssertionError('TLS_fixture_not_ready')
        metadata = client.get(endpoint+'/.well-known/oauth-protected-resource/mcp')
        require(metadata.status_code==200 and metadata.json()['resource']==spec['resource'], 'TLS_metadata')
        report['tls_metadata'] = True
        chrome = os.environ.get('CHROME_TEST')
        if not chrome and os.name == 'nt':
            chrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
        with sync_playwright() as playwright:
            options = dict(headless=True, args=browser_args)
            if chrome:
                options['executable_path'] = chrome
            browser = playwright.chromium.launch(**options)
            try:
                for label, width, subject in (('desktop',1280,'004'),('mobile',390,'op004')):
                    registration = client.post(endpoint+'/oauth/register', json={'redirect_uris':[callback],
                        'grant_types':['authorization_code','refresh_token']})
                    require(registration.status_code==201, 'DCR_browser')
                    cid = registration.json()['client_id']
                    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
                    params = dict(client_id=cid,response_type='code',redirect_uri=callback,
                        resource=spec['resource'],scope='reader:mcp offline_access',state=state,
                        code_challenge=create_s256_code_challenge(verifier),code_challenge_method='S256')
                    invitation = store.invite(subject, ttl=3600)[1] if store else invitations[subject]
                    all_secrets.append(invitation)
                    context = browser.new_context(viewport={'width':width,'height':844},
                        is_mobile=width<500, has_touch=width<500, ignore_https_errors=False)
                    page = context.new_page()
                    page.on('console', lambda message: report.update(csp_form_block=True)
                            if 'form-action' in message.text and 'violat' in message.text.lower() else None)
                    report['step'] = label+'_navigate'
                    responses = []
                    page.on('response', lambda response: responses.append({'path':urlsplit(response.url).path,'status':response.status}))
                    report['browser_responses'] = responses
                    page.goto(issuer+'/oauth/authorize?'+urlencode(params), wait_until='domcontentloaded')
                    cookies = [cookie for cookie in context.cookies() if cookie['name']==COOKIE]
                    require(len(cookies)==1 and cookies[0]['secure'] and cookies[0]['httpOnly']
                            and cookies[0]['sameSite']=='Lax', 'secure_cookie_'+label)
                    report['step'] = label+'_invite'
                    page.locator('input[name=invite]').fill(invitation)
                    page.locator('button[value=verify]').click()
                    page.locator('button[value=approve]').wait_for()
                    body = page.locator('body').inner_text()
                    require(subject in body and not any(value in body for value in all_secrets), 'confirmation_identity_'+label)
                    screenshots.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshots/(label+'-confirm.png')), full_page=True)
                    report[label+'_secure_cookie'] = True
                    require(page.evaluate('document.documentElement.scrollWidth') <= width, 'horizontal_overflow_'+label)
                    require(page.evaluate('Object.keys(localStorage).length === 0'), 'browser_secret_storage_'+label)
                    report['step'] = label+'_approve'
                    page.locator('button[value=approve]').click()
                    page.wait_for_url('**/client-callback?*')
                    query = parse_qs(urlsplit(page.url).query)
                    require(query.get('state')==[state] and query.get('iss')==[issuer], 'callback_state_issuer_'+label)
                    code = query['code'][0]
                    report['step'] = label+'_token_mcp'
                    all_secrets.append(code)
                    token_response = client.post(endpoint+'/oauth/token', data=dict(grant_type='authorization_code',
                        client_id=cid, code=code, code_verifier=verifier, redirect_uri=callback,
                        resource=spec['resource']))
                    require(token_response.status_code==200, 'browser_token_exchange_'+label)
                    token = token_response.json()
                    all_secrets.extend([token['access_token'], token['refresh_token']])
                    auth_headers = {'Host':host,'Authorization':'Bearer '+token['access_token']}
                    run_mcp(endpoint+'/mcp', auth_headers, verify, subject, True)
                    # Natural expiry exercises the configured 30-second access lifetime, not database time travel.
                    if label == 'desktop':
                        if spec['access_ttl'] <= 60:
                            time.sleep(spec['access_ttl']+1)
                            require(client.post(endpoint+'/mcp', headers=auth_headers).status_code==401, 'expired_access')
                        renewed = client.post(endpoint+'/oauth/token', data=dict(grant_type='refresh_token',
                            client_id=cid, refresh_token=token['refresh_token'], resource=spec['resource']))
                        require(renewed.status_code==200, 'refresh_after_expiry')
                        token = renewed.json()
                        all_secrets.extend([token['access_token'], token['refresh_token']])
                        auth_headers['Authorization'] = 'Bearer '+token['access_token']
                        run_mcp(endpoint+'/mcp', auth_headers, verify, subject)
                        report['natural_expiry_and_refresh' if spec['access_ttl'] <= 60 else 'public_refresh_and_reconnect'] = True
                    revoked = client.post(endpoint+'/oauth/revoke', data=dict(client_id=cid,token=token['access_token']))
                    require(revoked.status_code==200 and client.post(endpoint+'/mcp', headers=auth_headers).status_code==401,
                            'revoke_connected_identity_'+label)
                    require(client.post(endpoint+'/oauth/token', data=dict(grant_type='refresh_token',client_id=cid,
                        refresh_token=token['refresh_token'],resource=spec['resource'])).status_code==400, 'revoke_refresh_'+label)
                    context.close()
                    report[label+'_authorization_mcp_revoke'] = True
            finally:
                browser.close()
    if store:
        require(hashlib.sha256((root/'core'/'data'/'identities.json').read_bytes()).hexdigest()==before_ids, 'identity_bytes_changed')
        for logfile in (root/'logs').glob('*.log'):
            logged = logfile.read_bytes()
            require(all(value.encode() not in logged for value in all_secrets), 'credential_in_log')
        report['no_credential_log_or_identity_changes'] = True
    else:
        # The remote administrator can scan logs using hashes; the client never receives a legacy reader key.
        report['client_secret_digests'] = sorted({hashlib.sha256(value.encode()).hexdigest() for value in all_secrets})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--public-fixture', type=Path)
    args = parser.parse_args()
    report = {'public_https':False,'browser':'Chrome','tls':'ephemeral private CA, narrowly pinned in browser',
              'production_access':False,'pass':False}
    args.artifacts.mkdir(parents=True, exist_ok=True)
    try:
        if args.public_fixture:
            config=json.loads(args.public_fixture.read_text(encoding='utf-8'))
            require(config.get('marker')=='reader-mate-oauth-public-client-v1', 'wrong_public_test_fixture')
            require(urlsplit(config['issuer']).hostname.endswith('.trycloudflare.com'), 'not_temporary_test_host')
            report.update(public_https=True,tls='public CA verification enabled; no certificate exceptions')
            exercise(config, args.public_fixture.parent, config['issuer'], True, ['--no-proxy-server'],
                     args.artifacts, report, invitations=config['browser_invites'])
            report['pass']=True
        else:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                tls_port = free_port()
                issuer = 'https://oauth-local.example.test:'+str(tls_port)
                spec = prepare(base/'fixture', issuer)
                spec['callback'] = 'https://oauth-client.example.test:'+str(tls_port)+'/client-callback'
                cert, key, pin = certificate(base)
                context = ssl.create_default_context(cafile=str(cert))
                proxy = bridge(tls_port, spec['mcp_port'], cert, key)
                try:
                    with Services(base/'fixture'):
                        try:
                            exercise(spec, base/'fixture', 'https://127.0.0.1:'+str(tls_port), context,
                                ['--no-proxy-server','--host-resolver-rules=MAP oauth-local.example.test 127.0.0.1,MAP oauth-client.example.test 127.0.0.1',
                                 '--ignore-certificate-errors-spki-list='+pin], args.artifacts, report)
                        finally:
                            report['request_headers_sanitized'] = getattr(proxy,'sanitized_requests',[])
                    report['pass'] = True
                finally:
                    proxy.shutdown(); proxy.server_close()
    except Exception as error:
        # Playwright errors may contain a filled invitation; never print their detail/traceback.
        report['failure_type'] = type(error).__name__
        if isinstance(error, AssertionError):
            report['failure'] = str(error)
    (args.artifacts/'browser-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
    raise SystemExit(0 if report['pass'] else 1)


if __name__ == '__main__':
    main()
