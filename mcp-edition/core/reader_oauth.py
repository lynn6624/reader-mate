"""Reader-Mate identity adapter and dedicated-host ASGI dispatcher."""
import ipaddress
import re
from urllib.parse import urlsplit

from starlette.middleware.wsgi import WSGIMiddleware
from starlette.responses import JSONResponse

import member_store
from oauth_server import Config, SCOPE, create_server
from oauth_store import StoreError


class IdentityAdapter:
    def __init__(self, identities_file):
        self.path = identities_file

    def credential(self, subject):
        if not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', subject):
            return None
        try:
            doc = member_store.read(self.path)
            token = doc.get(subject)
            if (isinstance(token, str) and
                    (member_store.authenticate(doc, token, 'mcp') == subject or
                     member_store.authenticate(doc, token, 'mcp-url') == subject)):
                return token, member_store.profiles(doc)[subject]['nickname']
        except member_store.StoreError:
            pass
        return None

    def lookup(self, subject):
        result = self.credential(subject)
        return result[1] if result else None


class OAuthRouter:
    def __init__(self, legacy, config, identities_file, internal_path, api_url):
        self.legacy, self.config, self.internal_path = legacy, config, internal_path
        self.identity = IdentityAdapter(identities_file)
        parts = urlsplit(config.issuer)
        self.hosts = {parts.netloc}
        if parts.port is None:
            self.hosts.add(parts.hostname + ':443')
        api = urlsplit(api_url)
        if (api.scheme != 'http' or api.hostname not in ('127.0.0.1', 'localhost', '::1')
                or api.username or api.password or api.path or api.query or api.fragment):
            raise StoreError('OAuth requires a trusted loopback reader API')
        self.flask, self.store = create_server(config, self.identity.lookup)
        self.authorization = WSGIMiddleware(self.flask)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.legacy(scope, receive, send)
        raw_hosts = [value for key, value in scope['headers'] if key.lower() == b'host']
        try:
            hosts = [value.decode('ascii').lower() for value in raw_hosts]
        except UnicodeError:
            hosts = []
        if not any(host in self.hosts for host in hosts):
            if any(key.lower() == b'authorization' and value.lower().startswith(b'bearer rma_')
                   for key, value in scope['headers']):
                # Do not reinterpret an OAuth credential through a legacy URL gateway.
                return await self.failure(scope, receive, send, 401, 'invalid_token')
            return await self.legacy(scope, receive, send)
        if len(hosts) != 1:
            return await self.failure(scope, receive, send, 400, 'invalid_request')
        # Listener is loopback-only. Forwarded headers are never trusted.
        try:
            trusted_proxy = ipaddress.ip_address((scope.get('client') or ('',))[0]).is_loopback
        except ValueError:
            trusted_proxy = False
        if scope.get('scheme') != 'https' and not trusted_proxy:
            return await self.failure(scope, receive, send, 400, 'invalid_request')
        origins = [value.decode('ascii', 'replace') for key, value in scope['headers'] if key.lower() == b'origin']
        if origins and origins != [self.config.issuer]:
            return await self.failure(scope, receive, send, 403, 'invalid_request')
        scope = dict(scope, scheme='https', headers=[(key, value) for key, value in scope['headers']
                                                   if key.lower() != b'host'])
        scope['headers'].append((b'host', urlsplit(self.config.issuer).netloc.encode('ascii')))
        path = scope.get('path', '')
        if path.startswith('/oauth/') or path.startswith('/.well-known/'):
            return await self.authorization(scope, receive, send)
        if path != '/mcp':
            return await self.failure(scope, receive, send, 404, 'not_found')
        authorizations = [value.decode('ascii', 'replace') for key, value in scope['headers']
                          if key.lower() == b'authorization']
        if not authorizations and not scope.get('query_string'):
            return await self.failure(scope, receive, send, 401, 'authorization_required')
        if len(authorizations) != 1 or not authorizations[0].lower().startswith('bearer ') or scope.get('query_string'):
            return await self.failure(scope, receive, send, 401, 'invalid_token')
        try:
            grant = self.store.access(authorizations[0][7:], self.config.resource)
            identity = self.identity.credential(grant['subject']) if grant else None
        except StoreError:
            return await self.failure(scope, receive, send, 503, 'temporarily_unavailable')
        if not identity:
            return await self.failure(scope, receive, send, 401, 'invalid_token')
        if SCOPE not in grant['scope'].split():
            return await self.failure(scope, receive, send, 403, 'insufficient_scope')
        # Only the server chooses this credential; it never appears in a client response.
        scope['headers'] = [(key, value) for key, value in scope['headers']
                            if key.lower() not in (b'authorization', b'x-who-token')]
        scope['headers'].append((b'authorization', b'Bearer ' + identity[0].encode('utf-8')))
        scope.update(path=self.internal_path, raw_path=self.internal_path.encode('ascii'), reader_oauth=True)

        async def protected_send(message):
            if message['type'] == 'http.response.start':
                message = dict(message, headers=list(message.get('headers', [])) + [(b'cache-control', b'no-store')])
            await send(message)
        await self.legacy(scope, receive, protected_send)

    async def failure(self, scope, receive, send, status, error):
        headers = {'Cache-Control': 'no-store'}
        if status in (401, 403):
            headers['WWW-Authenticate'] = ('Bearer resource_metadata="' + self.config.issuer +
                '/.well-known/oauth-protected-resource/mcp", scope="' + SCOPE + '"')
            if error != 'authorization_required':
                headers['WWW-Authenticate'] += ', error="' + error + '"'
        await JSONResponse({'error': error}, status_code=status, headers=headers)(scope, receive, send)
