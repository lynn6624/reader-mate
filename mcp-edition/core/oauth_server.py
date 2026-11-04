"""Reusable Authlib authorization server. The identity adapter supplies only subject lookup."""
from dataclasses import dataclass
from functools import wraps
import base64
import hashlib
import json
import hmac
import logging
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from authlib.integrations.flask_oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import OAuth2Error, OAuth2Request, grants
from authlib.oauth2.rfc6749.requests import BasicOAuth2Payload
from authlib.oauth2.rfc7636 import CodeChallenge
from flask import Flask, g, jsonify, make_response, render_template_string, request
from werkzeug.exceptions import HTTPException

from oauth_store import Store, StoreError, digest, now, secret

# Authlib's DEBUG records can include generated tokens. Never propagate them.
authlog = logging.getLogger('authlib')
authlog.handlers = [logging.NullHandler()]
authlog.propagate = False
authlog.setLevel(logging.CRITICAL + 1)

SCOPE = 'reader:mcp'
SCOPES = (SCOPE, 'offline_access')
COOKIE = '__Host-reader-oauth'
AUTH_PARAMS = ('client_id', 'response_type', 'redirect_uri', 'resource', 'scope', 'state',
               'code_challenge', 'code_challenge_method')


def browser_origin_allowed(origin, issuer, path, method):
    # Chrome can redact a form navigation's Origin to "null" under no-referrer.
    # Only this cookie + independently verified CSRF endpoint permits that value.
    return origin in (None, issuer) or (origin == 'null' and path == '/oauth/authorize' and method == 'POST')


def https_uri(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    try:
        value.encode('ascii')
        parts = urlsplit(value)
        reserved = {'code', 'state', 'iss', 'error', 'error_description'}
        return (parts.scheme == 'https' and bool(parts.hostname)
                and bool(re.fullmatch(r'[A-Za-z0-9.:\[\]-]+', parts.netloc)) and not parts.username
                and not parts.password and not parts.fragment and '#' not in value
                and not re.search(r'[\s\\\x00-\x1f\x7f*]', value)
                and not reserved.intersection(key for key, _ in parse_qsl(parts.query))
                and (parts.port is None or 1 <= parts.port <= 65535))
    except (ValueError, UnicodeError):
        return False


@dataclass(frozen=True)
class Config:
    db: str
    issuer: str
    resource: str
    access_ttl: int = 900
    refresh_ttl: int = 2592000

    def __post_init__(self):
        parts = urlsplit(self.issuer)
        if (not https_uri(self.issuer) or parts.path or parts.query or self.issuer.endswith('/')
                or self.resource != self.issuer + '/mcp' or not os.path.isabs(self.db)
                or not 30 <= self.access_ttl <= 3600 or not 3600 <= self.refresh_ttl <= 2592000):
            raise StoreError('Invalid OAuth configuration: HTTPS origin, /mcp resource and private absolute DB required')

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('READER_OAUTH_DB', ''), os.environ.get('READER_OAUTH_ISSUER', ''),
                   os.environ.get('READER_OAUTH_RESOURCE', ''),
                   int(os.environ.get('READER_OAUTH_ACCESS_TTL', '900')),
                   int(os.environ.get('READER_OAUTH_REFRESH_TTL', '2592000')))


class Client:
    def __init__(self, value):
        self.value = value
        self.client_id = value['client_id']

    def get_client_id(self):
        return self.client_id

    def get_default_redirect_uri(self):
        return self.value['redirect_uris'][0]

    def check_redirect_uri(self, uri):
        return uri in self.value['redirect_uris']

    def get_allowed_scope(self, scope):
        return ' '.join(s for s in (scope or SCOPE).split() if s in SCOPES)

    def check_client_secret(self, value):
        return False

    def check_endpoint_auth_method(self, method, endpoint):
        return method == 'none'

    def check_response_type(self, value):
        return value == 'code'

    def check_grant_type(self, value):
        return value in self.value['grant_types']


class Credential:
    def __init__(self, key, value):
        self.key, self.value = key, value
        self.code_challenge = value.get('challenge')
        self.code_challenge_method = 'S256'

    def get_redirect_uri(self):
        return self.value['redirect_uri']

    def get_scope(self):
        return self.value['scope']

    def check_client(self, client):
        return self.value['client_id'] == client.client_id


class StoredRequest(OAuth2Request):
    def __init__(self, uri, params):
        super().__init__('GET', uri)
        self.payload = BasicOAuth2Payload(params)

    @property
    def args(self):
        return self.payload.data

    @property
    def form(self):
        return self.payload.data


class Server(AuthorizationServer):
    def create_oauth2_request(self, value):
        return value if isinstance(value, OAuth2Request) else super().create_oauth2_request(value)


STYLE = '''body{margin:0;padding:20px;background:#f5f2ec;color:#292e2c;font-family:system-ui,sans-serif;line-height:1.65}
main{box-sizing:border-box;max-width:36rem;margin:2rem auto;padding:24px;background:white;border:1px solid #e5dfd4;border-radius:12px}
h1{margin-top:0;font-size:1.6rem}p{overflow-wrap:anywhere}label{display:block}input[type=password]{box-sizing:border-box;width:100%;min-height:44px;padding:8px;margin-top:6px;font:inherit}
button{min-height:44px;margin:12px 8px 0 0;padding:8px 18px;font:inherit;border:1px solid #365c59;border-radius:6px;background:#365c59;color:white;cursor:pointer}
button[value=deny]{background:white;color:#454b48;border-color:#b7beb9}'''
STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()

PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>小书桌授权</title>
<style>{{ style|safe }}</style></head>
<body><main><h1>连接小书桌</h1>
<p>回调地址：<b>{{ redirect }}</b></p><p>客户端标识：{{ client }}</p>
<p>权限：读取共享书籍、管理自己的进度和批注、提交建议；仍受原有读者权限限制。</p>
{% if offline %}<p>允许客户端在你离线时刷新本次授权，最多 30 天。</p>{% endif %}
<form method="post" action="/oauth/authorize">
<input type="hidden" name="csrf" value="{{ csrf }}">
{% if subject %}<p>将绑定读者：<b>{{ subject }}</b>（{{ nickname }}）</p>
<button name="action" value="approve">确认授权</button>
{% else %}<label>邀请码 <input name="invite" type="password" autocomplete="off" required maxlength="128"></label>
<button name="action" value="verify">验证邀请码</button>{% endif %}
<button name="action" value="deny" formnovalidate>取消</button></form></main></body></html>'''


def create_server(config, lookup_subject):
    store = Store(config.db, config.issuer, config.resource)
    # Validate existing state at startup; only CLI init may create a database.
    with store.transaction(False):
        pass
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=16384, OAUTH2_SCOPES_SUPPORTED=list(SCOPES))

    def query_client(ident):
        value = store.get(g.db, 'client', ident)
        return Client(value) if value else None

    def save_token(token, oauth_request):
        old = oauth_request.authorization_code or oauth_request.refresh_token
        grant_id = old.value['grant']
        grant = store.active_grant(g.db, grant_id)
        if not grant:
            raise StoreError('OAuth grant unavailable')
        token['resource'] = config.resource
        token['expires_in'] = min(config.access_ttl, grant['expires'] - now())
        value = dict(grant=grant_id, client_id=grant['client_id'], scope=token['scope'],
                     expires=now()+token['expires_in'])
        store.put(g.db, 'access', digest(token['access_token']), value)
        if token.get('refresh_token'):
            store.put(g.db, 'refresh', digest(token['refresh_token']),
                      dict(value, expires=grant['expires'], used=False))
        store.audit(g.db, 'token_issued', grant['subject'], grant_id)

    server = Server(app, query_client, save_token)

    def generate_token(grant_type, client, user=None, scope=None, expires_in=None, include_refresh_token=True):
        value = dict(access_token=secret('rma_'), token_type='Bearer', expires_in=config.access_ttl,
                     scope=scope or SCOPE)
        if (include_refresh_token and client.check_grant_type('refresh_token')
                and getattr(g, 'refresh_consented', False)):
            value['refresh_token'] = secret('rmr_')
        return value

    server.register_token_generator('default', generate_token)

    class CodeGrant(grants.AuthorizationCodeGrant):
        TOKEN_ENDPOINT_AUTH_METHODS = ['none']

        def save_authorization_code(self, code, oauth_request):
            params = oauth_request.payload.data
            grant_id = oauth_request.user
            store.put(g.db, 'code', digest(code), dict(grant=grant_id, client_id=oauth_request.client.client_id,
                      redirect_uri=params['redirect_uri'], challenge=params['code_challenge'],
                      scope=oauth_request.scope, expires=now()+60, used=False))

        def query_authorization_code(self, code, client):
            value = store.get(g.db, 'code', digest(code))
            if not value or value['client_id'] != client.client_id:
                return None
            if value['used']:
                store.revoke(g.db, value['grant'], 'code_replayed')
                return None
            if value['expires'] <= now() or not store.active_grant(g.db, value['grant']):
                return None
            return Credential(digest(code), value)

        def delete_authorization_code(self, value):
            value.value['used'] = True
            store.put(g.db, 'code', value.key, value.value)

        def authenticate_user(self, value):
            grant = store.active_grant(g.db, value.value['grant'])
            # Older grants retain their original consent; a new policy cannot upgrade them.
            g.refresh_consented = bool(grant and grant.get(
                'refresh_consented', 'offline_access' in grant['scope'].split()))
            return grant['subject'] if grant and lookup_subject(grant['subject']) else None

    class RefreshGrant(grants.RefreshTokenGrant):
        TOKEN_ENDPOINT_AUTH_METHODS = ['none']
        INCLUDE_NEW_REFRESH_TOKEN = True

        def authenticate_refresh_token(self, token):
            value = store.get(g.db, 'refresh', digest(token))
            # Check client before detecting replay: strangers cannot revoke this family.
            if not value or value['client_id'] != self.request.client.client_id:
                return None
            if value['used']:
                store.revoke(g.db, value['grant'], 'refresh_replayed')
                return None
            if value['expires'] <= now() or not store.active_grant(g.db, value['grant']):
                return None
            return Credential(digest(token), value)

        authenticate_user = CodeGrant.authenticate_user

        def revoke_old_credential(self, value):
            value.value['used'] = True
            store.put(g.db, 'refresh', value.key, value.value)

    server.register_grant(CodeGrant, [CodeChallenge(required=True)])
    server.register_grant(RefreshGrant)

    def error(name='invalid_request', status=400):
        return jsonify(error=name), status

    @app.before_request
    def transport_and_limits():
        if request.host != urlsplit(config.issuer).netloc or request.scheme != 'https':
            return error('invalid_request')
        if request.method == 'POST' and not browser_origin_allowed(
                request.headers.get('Origin'), config.issuer, request.path, request.method):
            return error('invalid_request', 403)
        for values in (request.args, request.form):
            if any(len(values.getlist(key)) != 1 for key in values):
                return error()
        if request.method == 'POST' and request.query_string:
            return error()
        category, count, total, period = ('request', 120, 600, 60)
        if request.path == '/oauth/register':
            category, count, total, period = ('register', 10, 50, 3600)
        elif request.path == '/oauth/authorize':
            category, count, total, period = ('authorize', 30, 120, 600)
        if not store.limited(category, request.remote_addr or '', count, total, period):
            return error('temporarily_unavailable', 429)

    @app.after_request
    def protect_response(response):
        # Chrome checks a form's cross-origin 302 destination against this policy.
        # Only a redirect already validated against the client's registration is added.
        form_targets = "'self'"
        if getattr(g, 'form_redirect_origin', None):
            form_targets += ' ' + g.form_redirect_origin
        response.headers.update({'Cache-Control': 'no-store', 'Pragma': 'no-cache',
            'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; style-src 'sha256-"+STYLE_HASH+"'; form-action "+form_targets+"; frame-ancestors 'none'; base-uri 'none'",
            'X-Frame-Options': 'DENY'})
        return response

    @app.errorhandler(Exception)
    def unavailable(exc):
        # No tracebacks or attacker-supplied request values in application logs.
        return error('invalid_request', exc.code) if isinstance(exc, HTTPException) else error('temporarily_unavailable', 503)

    def atomic(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with store.transaction() as db:
                g.db = db
                return function(*args, **kwargs)
        return wrapped

    def oauth_request(params):
        return StoredRequest(config.issuer + '/oauth/authorize', params)

    def validate_authorize(params):
        scope = params.get('scope', SCOPE)
        if (params.get('resource') != config.resource or params.get('code_challenge_method') != 'S256'
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', params.get('code_challenge', ''))
                or not params.get('redirect_uri') or not SCOPE in scope.split()
                or not set(scope.split()) <= set(SCOPES) or len(params.get('state', '')) > 512):
            raise ValueError('Invalid authorization request')
        return server.get_consent_grant(oauth_request(params))

    def issuer_response(response):
        if response.headers.get('Location'):
            parts = urlsplit(response.headers['Location'])
            query = parse_qsl(parts.query, keep_blank_values=True) + [('iss', config.issuer)]
            response.headers['Location'] = urlunsplit(parts._replace(query=urlencode(query)))
        return response

    @app.get('/.well-known/oauth-authorization-server')
    def metadata():
        return jsonify(issuer=config.issuer, authorization_endpoint=config.issuer+'/oauth/authorize',
            token_endpoint=config.issuer+'/oauth/token', registration_endpoint=config.issuer+'/oauth/register',
            revocation_endpoint=config.issuer+'/oauth/revoke', response_types_supported=['code'],
            grant_types_supported=['authorization_code', 'refresh_token'], scopes_supported=list(SCOPES),
            token_endpoint_auth_methods_supported=['none'], revocation_endpoint_auth_methods_supported=['none'],
            code_challenge_methods_supported=['S256'], authorization_response_iss_parameter_supported=True)

    @app.get('/.well-known/oauth-protected-resource')
    @app.get('/.well-known/oauth-protected-resource/mcp')
    def resource_metadata():
        return jsonify(resource=config.resource, authorization_servers=[config.issuer],
                       scopes_supported=[SCOPE], bearer_methods_supported=['header'])

    @app.post('/oauth/register')
    @atomic
    def register():
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate registration field')
                result[key] = value
            return result
        try:
            if request.mimetype != 'application/json':
                return error('invalid_client_metadata')
            body = json.loads(request.get_data(), object_pairs_hook=unique)
            redirects = body.get('redirect_uris')
            grant_types = body.get('grant_types', ['authorization_code'])
            if (not isinstance(redirects, list) or not 1 <= len(redirects) <= 5
                    or not all(https_uri(uri) for uri in redirects)
                    or len(set(redirects)) != len(redirects)
                    or body.get('token_endpoint_auth_method', 'none') != 'none'
                    or body.get('response_types', ['code']) != ['code']
                    or not isinstance(grant_types, list) or 'authorization_code' not in grant_types
                    or not set(grant_types) <= {'authorization_code', 'refresh_token'}):
                return error('invalid_client_metadata')
        except (ValueError, TypeError, AttributeError):
            return error('invalid_client_metadata')
        if store.count(g.db, 'client') >= 1024:
            return error('temporarily_unavailable', 503)
        client = dict(client_id=secret('client_'), client_id_issued_at=now(), redirect_uris=redirects,
                      grant_types=grant_types, response_types=['code'], token_endpoint_auth_method='none')
        store.put(g.db, 'client', client['client_id'], client)
        store.audit(g.db, 'client_registered', object_id=client['client_id'])
        return jsonify(client), 201

    @app.route('/oauth/authorize', methods=['GET', 'POST'])
    @atomic
    def authorize():
        csrf = secret()
        if request.method == 'GET':
            # Unknown extensions cannot inject identity or plaintext credentials into state.
            params = {key: request.args[key] for key in AUTH_PARAMS if key in request.args}
            try:
                validated_grant = validate_authorize(params)
            except (ValueError, OAuth2Error):
                return error()
            if store.count(g.db, 'transaction') >= 2048:
                return error('temporarily_unavailable', 503)
            cookie = secret()
            key = digest(cookie)
            value = dict(params=params, csrf=digest(csrf), expires=now()+600, used=False, invite=None)
        else:
            cookie = request.cookies.get(COOKIE, '')
            key = digest(cookie)
            value = store.get(g.db, 'transaction', key)
            supplied = request.form.get('csrf', '')
            if (not value or value['used'] or value['expires'] <= now()
                    or not hmac.compare_digest(digest(supplied), value['csrf'])):
                return error('invalid_request', 403)
            csrf = supplied
            params = value['params']
            try:
                validated_grant = validate_authorize(params)
            except (ValueError, OAuth2Error):
                return error()
            action = request.form.get('action')
            if action == 'deny':
                value['used'] = True
                store.put(g.db, 'transaction', key, value)
                store.audit(g.db, 'authorization_denied', object_id=params['client_id'])
                return issuer_response(server.create_authorization_response(
                    validated_grant.request, grant_user=None, grant=validated_grant))
            if action == 'verify':
                invite_key = digest(request.form.get('invite', ''))
                invitation = store.get(g.db, 'invite', invite_key)
                if (not invitation or invitation['revoked'] or invitation['expires'] <= now()
                        or invitation['remaining'] <= 0 or not lookup_subject(invitation['subject'])):
                    store.audit(g.db, 'invitation_rejected')
                    return error('access_denied', 403)
                value['invite'] = invite_key
            elif action == 'approve' and value['invite']:
                invitation = store.get(g.db, 'invite', value['invite'])
                if (not invitation or invitation['revoked'] or invitation['expires'] <= now()
                        or invitation['remaining'] <= 0 or not lookup_subject(invitation['subject'])):
                    return error('access_denied', 403)
                grant_id = secret('grant_')
                grant = dict(id=grant_id, subject=invitation['subject'], client_id=params['client_id'],
                    resource=config.resource, scope=params.get('scope', SCOPE), created=now(),
                    expires=now()+config.refresh_ttl, revoked=0, invite_id=invitation['id'],
                    refresh_consented=validated_grant.request.client.check_grant_type('refresh_token'))
                store.put(g.db, 'grant', grant_id, grant)
                response = issuer_response(server.create_authorization_response(
                    validated_grant.request, grant_user=grant_id, grant=validated_grant))
                if response.status_code != 302:
                    raise StoreError('Authorization failed')
                invitation['remaining'] -= 1
                value['used'] = True
                store.put(g.db, 'invite', value['invite'], invitation)
                store.put(g.db, 'transaction', key, value)
                store.audit(g.db, 'grant_authorized', grant['subject'], grant_id)
                response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='Lax')
                return response
            else:
                return error()
        target = urlsplit(params['redirect_uri'])
        g.form_redirect_origin = target.scheme + '://' + target.netloc
        store.put(g.db, 'transaction', key, value)
        invitation = store.get(g.db, 'invite', value['invite']) if value['invite'] else None
        subject = invitation['subject'] if invitation else ''
        response = make_response(render_template_string(PAGE, csrf=csrf, subject=subject,
            nickname=lookup_subject(subject) if subject else '', redirect=params['redirect_uri'],
            client=params['client_id'], style=STYLE,
            offline=validated_grant.request.client.check_grant_type('refresh_token')))
        response.set_cookie(COOKIE, cookie, max_age=600, secure=True, httponly=True, samesite='Lax', path='/')
        return response

    @app.post('/oauth/token')
    @atomic
    def token_endpoint():
        if request.mimetype != 'application/x-www-form-urlencoded' or request.form.get('resource') != config.resource:
            return error('invalid_target')
        return server.create_token_response()

    @app.post('/oauth/revoke')
    @atomic
    def revoke():
        if request.mimetype != 'application/x-www-form-urlencoded' or request.headers.get('Authorization'):
            return error('invalid_request')
        client = query_client(request.form.get('client_id', ''))
        if not client:
            return error('invalid_client', 401)
        supplied = request.form.get('token', '')
        value = store.get(g.db, 'access', digest(supplied)) or store.get(g.db, 'refresh', digest(supplied))
        if value and value['client_id'] == client.client_id:
            store.revoke(g.db, value['grant'])
        return '', 200

    return app, store
