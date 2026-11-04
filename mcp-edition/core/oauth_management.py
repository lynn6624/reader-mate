"""Lightweight administrator adapter; no OAuth web framework imports or identity writes."""
import os
import re
from urllib.parse import urlsplit

import member_store
from oauth_store import Store, StoreError, now


def configured():
    return os.environ.get('READER_OAUTH_MANAGEMENT', '0') == '1'


def settings():
    issuer = os.environ.get('READER_OAUTH_ISSUER', '')
    resource = os.environ.get('READER_OAUTH_RESOURCE', '')
    path = os.environ.get('READER_OAUTH_DB', '')
    parts = urlsplit(issuer)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment
            or not re.fullmatch(r'[A-Za-z0-9.:\[\]-]+', parts.netloc)
            or resource != issuer+'/mcp' or not os.path.isabs(path)):
        raise StoreError('OAuth management configuration unavailable')
    return issuer, resource, path


def store():
    issuer, resource, path = settings()
    return Store(path, issuer, resource)


def allowed_origin(origin):
    issuer, _, _ = settings()
    expected = os.environ.get('TOKEN_PUBLIC_ORIGIN', issuer)
    return origin is None or origin == expected


def eligible(doc, subject):
    token = doc.get(subject) if isinstance(subject, str) else None
    return isinstance(token, str) and bool(token) and any(
        member_store.authenticate(doc, token, channel) == subject for channel in ('mcp', 'mcp-url'))


def overview(identities, page=0):
    if type(page) is not int or not 0 <= page <= 10000:
        raise ValueError('Invalid page')
    doc = member_store.read(identities)
    subjects = [{'id':ident, 'nickname':profile['nickname']}
                for ident, profile in member_store.profiles(doc).items() if eligible(doc, ident)]
    value = store()
    with value.transaction(False) as db:
        import json
        def rows(kind):
            return [json.loads(row[0]) for row in db.execute(
                'SELECT value FROM objects WHERE kind=? ORDER BY rowid DESC LIMIT 50 OFFSET ?',
                (kind, page*50))]
        invites = rows('invite')
        invite_total, grant_total = value.count(db, 'invite'), value.count(db, 'grant')
        grants = []
        for row in rows('grant'):
            client = value.get(db, 'client', row['client_id'])
            grants.append(dict(row, redirect_uris=client['redirect_uris'] if client else []))
    # Store rows contain public fields only; credential tables are never queried here.
    return dict(resource=settings()[1], subjects=subjects, page=page,
                invite_total=invite_total, grant_total=grant_total,
                invites=sorted(invites, key=lambda x:x['expires'], reverse=True),
                grants=sorted(grants, key=lambda x:x['created'], reverse=True))


def invite(identities, body):
    subject, ttl, uses = body.get('reader_id'), body.get('ttl', 86400), body.get('uses', 1)
    if not eligible(member_store.read(identities), subject):
        raise ValueError('Reader unavailable')
    if type(ttl) is not int or type(uses) is not int:
        raise ValueError('Invalid invitation limits')
    ident, secret = store().invite(subject, ttl=ttl, uses=uses)
    return dict(invite_id=ident, reader_id=subject, invitation=secret, resource=settings()[1],
                expires=now()+ttl, uses=uses)


def revoke(kind, ident):
    prefix = 'grant_' if kind == 'grant' else 'inv_'
    if not isinstance(ident, str) or not re.fullmatch(prefix+r'[A-Za-z0-9_-]{43}', ident):
        raise ValueError('Invalid public identifier')
    value = store()
    with value.transaction() as db:
        if kind == 'grant':
            if not value.get(db, 'grant', ident):
                raise KeyError('Unknown grant')
            value.revoke(db, ident, 'admin_grant_revoked')
        else:
            found = next(((key, row) for key, row in value.rows(db, 'invite') if row['id']==ident), None)
            if not found:
                raise KeyError('Unknown invitation')
            key, row = found
            if not row['revoked']:
                row['revoked'] = now()
                value.put(db, 'invite', key, row)
                value.audit(db, 'admin_invite_revoked', row['subject'], row['id'])
