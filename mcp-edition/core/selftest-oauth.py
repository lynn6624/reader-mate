"""Disposable OAuth protocol, CLI and real HTTP/MCP regression tests. No production paths."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from oauth_server import Config, COOKIE, create_server
from oauth_store import Store, StoreError, digest, now
from reader_oauth import IdentityAdapter, OAuthRouter

ROOT = Path(__file__).parent
ISSUER = 'https://oauth.example.test'
CALLBACK = 'https://client.example.test/callback'


class Protocol(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.ids = self.base / 'identities.json'
        self.identities = {ident: secrets.token_urlsafe(32) for ident in ('user', '004', 'op004', 'op001')}
        self.ids.write_text(json.dumps(self.identities), encoding='utf-8')
        self.config = Config(str(self.base/'state'/'oauth.sqlite3'), ISSUER, ISSUER+'/mcp')
        self.store = Store(self.config.db, ISSUER, ISSUER+'/mcp')
        self.store.initialize()
        self.app, _ = create_server(self.config, IdentityAdapter(str(self.ids)).lookup)
        self.client = self.app.test_client()
        self.client_id = self.register()

    def send(self, path, client=None, **kwargs):
        return (client or self.client).open(path, base_url=ISSUER, **kwargs)

    def register(self, **metadata):
        response = self.send('/oauth/register', method='POST', json=dict(
            redirect_uris=[CALLBACK], grant_types=['authorization_code', 'refresh_token'], **metadata))
        self.assertEqual(response.status_code, 201)
        return response.json['client_id']

    def start(self, client=None, **extra):
        verifier = secrets.token_urlsafe(48)
        params = dict(client_id=self.client_id, response_type='code', redirect_uri=CALLBACK,
                      scope='reader:mcp offline_access', resource=self.config.resource, state='unique-state',
                      code_challenge=create_s256_code_challenge(verifier), code_challenge_method='S256')
        params.update(extra)
        response = self.send('/oauth/authorize', client, query_string=params)
        self.assertEqual(response.status_code, 200)
        csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
        return verifier, csrf

    def authorization(self, subject='004', client=None, **extra):
        verifier, csrf = self.start(client, **extra)
        _, invite = self.store.invite(subject)
        response = self.send('/oauth/authorize', client, method='POST', data=dict(csrf=csrf, action='verify', invite=invite))
        self.assertEqual(response.status_code, 200)
        self.assertIn(subject, response.text)
        self.assertNotIn(invite, response.text)
        response = self.send('/oauth/authorize', client, method='POST', data=dict(csrf=csrf, action='approve', who='user'))
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlsplit(response.headers['Location']).query)
        self.assertEqual(query['iss'], [ISSUER])
        self.assertEqual(query['state'], ['unique-state'])
        return query['code'][0], verifier, invite

    def exchange(self, code, verifier, **extra):
        data = dict(client_id=self.client_id, grant_type='authorization_code', code=code,
                    code_verifier=verifier, redirect_uri=CALLBACK, resource=self.config.resource)
        data.update(extra)
        return self.send('/oauth/token', method='POST', data=data)

    def issued(self, subject='004', **extra):
        code, verifier, invite = self.authorization(subject, **extra)
        response = self.exchange(code, verifier)
        self.assertEqual(response.status_code, 200)
        return response.json, invite

    def refresh(self, token, **extra):
        data = dict(client_id=self.client_id, grant_type='refresh_token', refresh_token=token,
                    resource=self.config.resource)
        data.update(extra)
        return self.send('/oauth/token', method='POST', data=data)

    def test_discovery_binding_and_no_implicit_store_creation(self):
        metadata = self.send('/.well-known/oauth-protected-resource/mcp').json
        self.assertEqual(metadata['resource'], self.config.resource)
        self.assertEqual(metadata['scopes_supported'], ['reader:mcp'])
        server = self.send('/.well-known/oauth-authorization-server').json
        self.assertEqual(server['code_challenge_methods_supported'], ['S256'])
        self.assertTrue(server['authorization_response_iss_parameter_supported'])
        self.assertNotIn('client_id_metadata_document_supported', server)
        with self.assertRaises(StoreError):
            with Store(self.config.db, ISSUER, ISSUER+'/other').transaction():
                pass
        missing = self.base/'missing.sqlite3'
        with self.assertRaises(StoreError):
            with Store(missing, ISSUER, self.config.resource).transaction():
                pass
        self.assertFalse(missing.exists())

    def test_pkce_client_redirect_resource_state_and_duplicate_validation(self):
        for field, value in [('code_challenge_method', 'plain'), ('code_challenge', ''),
                             ('resource', 'https://other.example/mcp'), ('client_id', 'unknown'),
                             ('redirect_uri', CALLBACK+'/changed'), ('scope', 'admin'), ('state', 'x'*513)]:
            verifier = secrets.token_urlsafe(48)
            params = dict(client_id=self.client_id, response_type='code', redirect_uri=CALLBACK,
                          scope='reader:mcp', resource=self.config.resource, state='state',
                          code_challenge=create_s256_code_challenge(verifier), code_challenge_method='S256')
            params[field] = value
            response = self.send('/oauth/authorize', query_string=params)
            self.assertEqual(response.status_code, 400, field)
            self.assertNotIn('Location', response.headers)
        self.assertEqual(self.send('/oauth/authorize?client_id=a&client_id=b').status_code, 400)
        for redirect in ('http://client.example/cb', 'https://client.example/cb#frag',
                         'https://user:password@client.example/cb', 'https://*.example/cb'):
            self.assertEqual(self.send('/oauth/register', method='POST', json={'redirect_uris':[redirect]}).status_code, 400)

    def test_invitation_expiry_csrf_cancel_and_single_use(self):
        _, csrf = self.start()
        _, invite = self.store.invite('004')
        self.assertEqual(self.send('/oauth/authorize', method='POST', data=dict(action='verify', csrf='wrong', invite=invite)).status_code, 403)
        stranger = self.app.test_client()
        self.assertEqual(self.send('/oauth/authorize', stranger, method='POST', data=dict(action='verify', csrf=csrf, invite=invite)).status_code, 403)
        self.assertEqual(self.send('/oauth/authorize', method='POST', data=dict(action='verify', csrf=csrf, invite='invalid')).status_code, 403)
        with self.store.transaction() as db:
            value = self.store.get(db, 'invite', digest(invite)); value['expires'] = now()-1
            self.store.put(db, 'invite', digest(invite), value)
        self.assertEqual(self.send('/oauth/authorize', method='POST', data=dict(action='verify', csrf=csrf, invite=invite)).status_code, 403)
        response = self.send('/oauth/authorize', method='POST', data=dict(action='deny', csrf=csrf))
        query = parse_qs(urlsplit(response.headers['Location']).query)
        self.assertEqual(query['error'], ['access_denied'])
        self.assertEqual(query['iss'], [ISSUER])
        token, consumed = self.issued()
        _, csrf = self.start()
        self.assertEqual(self.send('/oauth/authorize', method='POST', data=dict(action='verify', csrf=csrf, invite=consumed)).status_code, 403)
        self.assertEqual(self.store.access(token['access_token'], self.config.resource)['subject'], '004')

    def test_exchange_failures_and_code_replay(self):
        code, verifier, _ = self.authorization()
        for extra in (dict(code_verifier=secrets.token_urlsafe(48)), dict(redirect_uri=CALLBACK+'/evil'),
                      dict(client_id='unknown'), dict(resource='https://wrong.example/mcp')):
            response = self.exchange(code, extra.pop('code_verifier', verifier), **extra)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('access_token', response.json)
        response = self.exchange(code, verifier)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.exchange(code, verifier).status_code, 400)
        self.assertIsNone(self.store.access(response.json['access_token'], self.config.resource))

    def test_refresh_rotation_downscope_replay_and_independent_grants(self):
        first, _ = self.issued()
        second, _ = self.issued('op004')
        stranger = self.register()
        self.assertEqual(self.refresh(first['refresh_token'], client_id=stranger).status_code, 400)
        self.assertEqual(self.refresh(first['refresh_token'], scope='admin').status_code, 400)
        rotated = self.refresh(first['refresh_token'])
        self.assertEqual(rotated.status_code, 200)
        self.assertNotEqual(first['refresh_token'], rotated.json['refresh_token'])
        self.assertEqual(self.store.access(rotated.json['access_token'], self.config.resource)['subject'], '004')
        self.assertEqual(self.refresh(first['refresh_token']).status_code, 400)
        self.assertIsNone(self.store.access(rotated.json['access_token'], self.config.resource))
        self.assertIsNotNone(self.store.access(second['access_token'], self.config.resource))
        # Reloading the service cannot resurrect the revoked family.
        _, reopened = create_server(self.config, IdentityAdapter(str(self.ids)).lookup)
        self.assertIsNone(reopened.access(first['access_token'], self.config.resource))

    def test_expiry_scope_and_revocation_endpoint(self):
        first, _ = self.issued()
        second, _ = self.issued(scope='reader:mcp')
        self.assertNotIn('refresh_token', second)
        self.assertIsNone(self.store.access(first['access_token'], 'https://wrong.example/mcp'))
        stranger = self.register()
        self.assertEqual(self.send('/oauth/revoke', method='POST', data=dict(client_id=stranger, token=first['access_token'])).status_code, 200)
        self.assertIsNotNone(self.store.access(first['access_token'], self.config.resource))
        self.assertEqual(self.send('/oauth/revoke', method='POST', data=dict(client_id=self.client_id, token=first['access_token'])).status_code, 200)
        self.assertIsNone(self.store.access(first['access_token'], self.config.resource))
        self.assertEqual(self.refresh(first['refresh_token']).status_code, 400)
        self.assertEqual(self.send('/oauth/revoke', method='POST', data=dict(client_id=self.client_id, token='unknown')).status_code, 200)
        with self.store.transaction() as db:
            value = self.store.get(db, 'access', digest(second['access_token'])); value['expires'] = now()-1
            self.store.put(db, 'access', digest(second['access_token']), value)
        self.assertIsNone(self.store.access(second['access_token'], self.config.resource))

    def test_atomic_code_and_invitation_consumption(self):
        code, verifier, _ = self.authorization()
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda _: self.exchange(code, verifier).status_code, range(2)))
        self.assertEqual(sorted(responses), [200, 400])
        _, csrf = self.start()
        _, invite = self.store.invite('004')
        self.send('/oauth/authorize', method='POST', data=dict(csrf=csrf, action='verify', invite=invite))
        cookie = self.client.get_cookie(COOKIE, domain='oauth.example.test').value
        def approve(_):
            browser = self.app.test_client()
            browser.set_cookie(COOKIE, cookie, domain='oauth.example.test', secure=True)
            return self.send('/oauth/authorize', browser, method='POST', data=dict(csrf=csrf, action='approve')).status_code
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(approve, range(2)))
        self.assertEqual(sorted(responses), [302, 403])
        with self.store.transaction(False) as db:
            self.assertEqual(self.store.get(db, 'invite', digest(invite))['remaining'], 0)

    def test_no_plaintext_at_rest_and_safe_browser_headers(self):
        before = self.ids.read_bytes()
        token, invite = self.issued()
        raw = Path(self.config.db).read_bytes()
        for value in [invite, token['access_token'], token['refresh_token'], *self.identities.values()]:
            self.assertNotIn(value.encode(), raw)
        response = self.send('/.well-known/oauth-authorization-server')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(before, self.ids.read_bytes())
        self.assertEqual(self.send('/oauth/token', method='POST', headers={'Origin':'https://evil.example'}).status_code, 403)

    def test_refresh_concurrency_and_expired_codes(self):
        token, _ = self.issued()
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda _: self.refresh(token['refresh_token']).status_code, range(2)))
        self.assertEqual(sorted(responses), [200, 400])
        self.assertIsNone(self.store.access(token['access_token'], self.config.resource))
        code, verifier, _ = self.authorization()
        with self.store.transaction() as db:
            value = self.store.get(db, 'code', digest(code)); value['expires'] = now()-1
            self.store.put(db, 'code', digest(code), value)
        self.assertEqual(self.exchange(code, verifier).status_code, 400)

    def test_invitation_concurrency_between_two_browsers(self):
        _, invite = self.store.invite('004')
        browsers = [self.app.test_client(), self.app.test_client()]
        csrf_values = [self.start(browser)[1] for browser in browsers]
        for browser, csrf in zip(browsers, csrf_values):
            self.assertEqual(self.send('/oauth/authorize', browser, method='POST',
                data=dict(csrf=csrf, action='verify', invite=invite)).status_code, 200)
        def approve(index):
            return self.send('/oauth/authorize', browsers[index], method='POST',
                             data=dict(csrf=csrf_values[index], action='approve')).status_code
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(approve, range(2)))
        self.assertEqual(sorted(responses), [302, 403])

    def test_asgi_host_scope_transport_and_damaged_storage(self):
        async def legacy(scope, receive, send):
            await send({'type':'http.response.start', 'status':404, 'headers':[]})
            await send({'type':'http.response.body', 'body':b''})
        router = OAuthRouter(legacy, self.config, str(self.ids), '/mcp/'+'T'*24, 'http://127.0.0.1:8831')
        token, _ = self.issued()
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=router), base_url=ISSUER) as client:
                response = await client.post('/mcp', headers=[('Host','oauth.example.test'),('Host','evil.example')])
                self.assertEqual(response.status_code, 400)
                self.assertEqual((await client.post('/mcp', headers={'Origin':'https://evil.example'})).status_code, 403)
                with self.store.transaction() as db:
                    value = self.store.get(db, 'access', digest(token['access_token'])); value['scope'] = 'offline_access'
                    self.store.put(db, 'access', digest(token['access_token']), value)
                self.assertEqual((await client.post('/mcp', headers={'Authorization':'Bearer '+token['access_token']})).status_code, 403)
                Path(self.config.db).write_bytes(b'broken database')
                self.assertEqual((await client.post('/mcp', headers={'Authorization':'Bearer '+token['access_token']})).status_code, 503)
                self.assertEqual((await client.get('/.well-known/oauth-authorization-server')).status_code, 503)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=router, client=('203.0.113.2',123)),
                                         base_url='http://oauth.example.test') as client:
                self.assertEqual((await client.post('/mcp')).status_code, 400)
        asyncio.run(check())

    def test_cli_and_persistent_limits(self):
        env = dict(os.environ, READER_OAUTH_DB=self.config.db, READER_OAUTH_ISSUER=ISSUER,
                   READER_OAUTH_RESOURCE=self.config.resource)
        destination = self.base/'invitation.txt'
        def cli(*args):
            return subprocess.run([sys.executable, str(ROOT/'oauth_admin.py'), *args], env=env,
                                  capture_output=True, text=True)
        response = cli('invite', '--identities', str(self.ids), '--reader-id', 'op004', '--output', str(destination))
        self.assertEqual(response.returncode, 0, response.stderr)
        value = destination.read_text().strip()
        self.assertNotIn(value, response.stdout+response.stderr)
        self.assertNotIn(value, cli('invites').stdout)
        self.assertNotEqual(cli('invite', '--identities', str(self.ids), '--reader-id', 'op004', '--output', str(destination)).returncode, 0)
        self.assertEqual(destination.read_text().strip(), value)
        self.assertTrue(self.store.limited('test', 'source', 1, 10))
        reloaded = Store(self.config.db, ISSUER, self.config.resource)
        self.assertFalse(reloaded.limited('test', 'source', 1, 10))

    def test_backend_redirect_never_transmits_reader_key_to_another_service(self):
        captured = []
        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                captured.append(dict(self.headers)); self.send_response(200); self.end_headers()
            do_POST = do_GET
            def log_message(self, *args):
                pass
        sink = ThreadingHTTPServer(('127.0.0.1', 0), Sink)
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.1:'+str(sink.server_port)+'/capture')
                self.send_header('Content-Length', '0'); self.end_headers()
            do_POST = do_GET
            def log_message(self, *args):
                pass
        origin = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        for server in (sink, origin):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            core = self.base/'copy'
            shutil.copytree(ROOT, core, ignore=shutil.ignore_patterns('data', '书库', '__pycache__'))
            for name, value in (('regular', 'T'*24), ('url', 'G'*24)):
                (self.base/name).write_text(value); (self.base/name).chmod(0o600)
            env = dict(os.environ, READER_OAUTH_ENABLED='0', READER_API_URL='http://127.0.0.1:'+str(origin.server_port),
                READER_MCP_PATH_KEY_FILE=str(self.base/'regular'), READER_MCP_004_KEY_FILE=str(self.base/'url'),
                HTTP_PROXY='http://127.0.0.1:'+str(sink.server_port), NO_PROXY='')
            code = '''from types import SimpleNamespace
import urllib.request, urllib.error
import mcp_server
ctx=SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(scope={'reader_oauth':True})))
for data in (None,b'{}'):
    req=urllib.request.Request(mcp_server.API+'/api/test',data=data,headers={'X-Who-Token':'test-private-key'})
    try:
        mcp_server.backend_open(ctx,req)
        raise AssertionError('Redirect followed')
    except urllib.error.HTTPError as error:
        assert error.code==302
'''
            response = subprocess.run([sys.executable, '-B', '-c', code], env=env, cwd=core, capture_output=True, text=True)
            self.assertEqual(response.returncode, 0, response.stderr)
            self.assertEqual(captured, [])
        finally:
            for server in (origin, sink):
                server.shutdown(); server.server_close()


def port():
    with socket.socket() as stream:
        stream.bind(('127.0.0.1', 0))
        return stream.getsockname()[1]


async def http_mcp():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        core = base/'core'
        shutil.copytree(ROOT, core, ignore=shutil.ignore_patterns('data', '书库', '__pycache__'))
        data, books = core/'data', core/'书库'
        data.mkdir(); books.mkdir()
        ids = {ident: secrets.token_urlsafe(32) for ident in ('user', '004', 'op004', 'op001')}
        identities = data/'identities.json'
        identities.write_text(json.dumps(ids), encoding='utf-8')
        original_ids = identities.read_bytes()
        (books/'test.txt').write_text('第一章\n你好😀一起读书。\n第二段。', encoding='utf-8')
        for name, content in (('path-key', 'T'*24), ('gpt-key', 'G'*24)):
            (base/name).write_text(content); (base/name).chmod(0o600)
        config = Config(str(base/'state'/'oauth.sqlite3'), ISSUER, ISSUER+'/mcp')
        store = Store(config.db, ISSUER, config.resource); store.initialize()
        hp, mp = port(), port()
        env = dict(os.environ, READER_CLOUD='1', READER_PORT=str(hp), READER_MCP_PORT=str(mp),
            READER_API_URL=f'http://127.0.0.1:{hp}', READER_MCP_PUBLIC_HOST='legacy.example.test',
            READER_MCP_EXTRA_HOST='mcp.example.test', READER_MCP_PATH_KEY_FILE=str(base/'path-key'),
            READER_MCP_004_KEY_FILE=str(base/'gpt-key'), READER_OAUTH_ENABLED='1',
            READER_OAUTH_DB=config.db, READER_OAUTH_ISSUER=ISSUER, READER_OAUTH_RESOURCE=config.resource)
        web, mcp = f'http://127.0.0.1:{hp}', f'http://127.0.0.1:{mp}'
        log = open(base/'run.log', 'w+', encoding='utf-8')
        processes = [subprocess.Popen([sys.executable, '-B', str(core/name)], env=env, stdout=log, stderr=log)
                     for name in ('reader_server.py', 'mcp_server.py')]
        emitted = []
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                for _ in range(100):
                    try:
                        response = await client.post(mcp+'/mcp', headers={'Host':'oauth.example.test'})
                        if response.status_code == 401 and (await client.get(web+'/api/ping')).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if any(proc.poll() is not None for proc in processes):
                        raise AssertionError('Disposable server failed: '+(base/'run.log').read_text(encoding='utf-8'))
                    await asyncio.sleep(.1)
                else:
                    raise AssertionError('Disposable servers did not start')
                assert 'resource_metadata=' in response.headers['WWW-Authenticate']
                assert (await client.post(mcp+'/mcp')).status_code == 404
                for legacy in (ids['004'], ids['op001']):
                    assert (await client.post(mcp+'/mcp', headers={'Host':'oauth.example.test', 'Authorization':'Bearer '+legacy})).status_code == 401

                async def authorize(subject):
                    headers = {'Host':'oauth.example.test'}
                    registration = await client.post(mcp+'/oauth/register', headers=headers,
                        json={'redirect_uris':[CALLBACK], 'grant_types':['authorization_code','refresh_token']})
                    assert registration.status_code == 201
                    cid = registration.json()['client_id']
                    verifier = secrets.token_urlsafe(48)
                    params = dict(client_id=cid, response_type='code', redirect_uri=CALLBACK,
                        scope='reader:mcp offline_access', state='http-state', resource=config.resource,
                        code_challenge=create_s256_code_challenge(verifier), code_challenge_method='S256')
                    page = await client.get(mcp+'/oauth/authorize', params=params, headers=headers)
                    assert page.status_code == 200
                    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
                    # Simulate the loopback side of TLS termination; this is not public HTTPS validation.
                    headers['Cookie'] = page.headers['set-cookie'].split(';')[0]
                    _, invitation = store.invite(subject)
                    verified = await client.post(mcp+'/oauth/authorize', headers=headers,
                        data=dict(action='verify', csrf=csrf, invite=invitation))
                    assert verified.status_code == 200
                    confirmed = await client.post(mcp+'/oauth/authorize', headers=headers, data=dict(action='approve', csrf=csrf))
                    assert confirmed.status_code == 302
                    code = parse_qs(urlsplit(confirmed.headers['Location']).query)['code'][0]
                    issued = await client.post(mcp+'/oauth/token', headers={'Host':'oauth.example.test'},
                        data=dict(grant_type='authorization_code', client_id=cid, code=code,
                                  code_verifier=verifier, redirect_uri=CALLBACK, resource=config.resource))
                    assert issued.status_code == 200, issued.text
                    emitted.extend([invitation, code, *[issued.json()[key] for key in ('access_token','refresh_token')]])
                    return cid, issued.json()

                async def visit(path, headers, action=None):
                    async with httpx.AsyncClient(trust_env=False, headers=headers) as transport:
                        async with streamable_http_client(mcp+path, http_client=transport) as (read, write, _):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                assert len((await session.list_tools()).tools) == 15
                                async def call(name, arguments):
                                    result = await session.call_tool(name, arguments)
                                    assert not result.isError, result
                                    return json.loads(result.content[0].text)
                                listing = await call('list_books', {})
                                if action:
                                    await action(call, session)
                                return listing

                async def save_old(call, session):
                    await call('update_progress', dict(name='test.txt', offset=4))
                old = await visit('/mcp/'+'G'*24, {'Host':'mcp.example.test'}, save_old)
                assert old['viewer_id'] == '004'
                cid, token = await authorize('004')
                headers = {'Host':'oauth.example.test','Authorization':'Bearer '+token['access_token']}
                async def read_and_write(call, session):
                    progress = await call('get_reading_progress', dict(name='test.txt'))
                    assert progress['viewer_id'] == '004' and progress['progress']['offset'] == 4
                    await call('update_progress', dict(name='test.txt', offset=6))
                    highlight = await call('add_highlight', dict(name='test.txt', start=4, end=6, text='你好'))
                    comment = await call('add_comment', dict(name='test.txt', highlight_id=highlight['id'], text='OAuth 连续性测试'))
                    assert comment['annotation']['author_id'] == '004'
                    # Caller-supplied identity must not become an authenticated identity.
                    spoof = await session.call_tool('list_books', {'who':'user', 'viewer_id':'user'})
                    if not spoof.isError:
                        assert json.loads(spoof.content[0].text)['viewer_id'] == '004'
                listing = await visit('/mcp', headers, read_and_write)
                assert listing['viewer_id'] == '004'
                assert (await visit('/mcp', dict(headers, Authorization='bearer '+token['access_token'])))['viewer_id'] == '004'
                _, other = await authorize('op004')
                assert (await visit('/mcp', {'Host':'oauth.example.test','Authorization':'Bearer '+other['access_token']}))['viewer_id'] == 'op004'
                assert (await visit('/mcp/'+'T'*24, {'Authorization':'Bearer '+ids['op001']}))['viewer_id'] == 'op001'
                assert (await client.post(mcp+'/mcp/'+'T'*24, headers={'Authorization':'Bearer '+token['access_token']})).status_code == 401
                assert (await client.post(mcp+'/mcp/'+'G'*24, headers={'Host':'mcp.example.test','Authorization':'Bearer '+token['access_token']})).status_code == 401
                assert (await client.post(mcp+'/mcp/'+'G'*24, headers={'Host':'oauth.example.test'})).status_code == 404
                assert (await client.get(web+'/api/books', headers={'X-Who-Token':token['access_token']})).status_code == 401
                assert (await client.post(mcp+'/mcp?viewer_id=user', headers=headers)).status_code == 401
                revoked = await client.post(mcp+'/oauth/revoke', headers={'Host':'oauth.example.test'},
                    data=dict(client_id=cid, token=token['access_token']))
                assert revoked.status_code == 200
                assert (await client.post(mcp+'/mcp', headers=headers)).status_code == 401
                assert (await visit('/mcp', {'Host':'oauth.example.test','Authorization':'Bearer '+other['access_token']}))['viewer_id'] == 'op004'
                async def old_continuity(call, session):
                    assert (await call('get_reading_progress', {'name':'test.txt'}))['progress']['offset'] == 6
                await visit('/mcp/'+'G'*24, {'Host':'mcp.example.test'}, old_continuity)
                # Current identity removal is checked on every OAuth HTTP request.
                changed = dict(ids); del changed['op004']
                identities.write_text(json.dumps(changed), encoding='utf-8')
                assert (await client.post(mcp+'/mcp', headers={'Host':'oauth.example.test','Authorization':'Bearer '+other['access_token']})).status_code == 401
                identities.write_bytes(original_ids)
                assert identities.read_bytes() == original_ids
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=10)
            log.close()
        logged = (base/'run.log').read_bytes()
        for value in [*ids.values(), *emitted]:
            assert value.encode() not in logged, 'Credential leaked to test log'
        print('PASS OAuth real HTTP/MCP: 15 tools, 004/op004 separation, progress/comment continuity, legacy modes, revoke, no log credentials')


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Protocol))
    if not result.wasSuccessful():
        raise SystemExit(1)
    asyncio.run(http_mcp())
