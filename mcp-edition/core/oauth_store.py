"""Private OAuth state, independent of reader data. No plaintext credentials at rest."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time


class StoreError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def secret(prefix=''):
    return prefix + secrets.token_urlsafe(32)


def now():
    return int(time.time())


def private(path, directory=False):
    path = Path(path)
    if path.is_symlink() or not (path.is_dir() if directory else path.is_file()):
        raise StoreError('OAuth private storage unavailable')
    if os.name != 'nt' and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise StoreError('OAuth storage must be owner-only')


class Store:
    def __init__(self, path, issuer, resource):
        self.path = Path(path).absolute()
        self.binding = {'version': 1, 'issuer': issuer, 'resource': resource}

    def initialize(self):
        """Explicit administrator operation only; never overwrite an existing database."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private(self.path.parent, True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
                PRAGMA max_page_count=16384;
                CREATE TABLE objects(kind TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
                                     PRIMARY KEY(kind,id));
                CREATE TABLE audit(seq INTEGER PRIMARY KEY, at INTEGER NOT NULL,
                                   event TEXT NOT NULL, subject TEXT, object_id TEXT);
                CREATE TABLE rate(id TEXT PRIMARY KEY, count INTEGER, expires INTEGER);
            ''')
            self.put(db, 'meta', 'binding', self.binding)
            db.commit()

    @contextmanager
    def transaction(self, write=True):
        db = None
        try:
            private(self.path.parent, True)
            private(self.path)
            # mode=rw refuses to silently recreate missing security state.
            db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)
            db.execute('PRAGMA secure_delete=ON')
            db.execute('PRAGMA max_page_count=16384')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if self.get(db, 'meta', 'binding') != self.binding:
                raise StoreError('OAuth database configuration mismatch')
            yield db
            db.commit()
        except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
            raise StoreError('OAuth private storage unavailable') from None
        finally:
            if db is not None:
                db.close()  # also rolls back an incomplete operation

    @staticmethod
    def get(db, kind, key):
        row = db.execute('SELECT value FROM objects WHERE kind=? AND id=?', (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def put(db, kind, key, value):
        db.execute('INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value',
                   (kind, key, json.dumps(value, ensure_ascii=False, separators=(',', ':'))))

    @staticmethod
    def delete(db, kind, key):
        db.execute('DELETE FROM objects WHERE kind=? AND id=?', (kind, key))

    @staticmethod
    def count(db, kind):
        return db.execute('SELECT COUNT(*) FROM objects WHERE kind=?', (kind,)).fetchone()[0]

    @staticmethod
    def rows(db, kind):
        return [(key, json.loads(value)) for key, value in db.execute(
            'SELECT id,value FROM objects WHERE kind=?', (kind,))]

    @staticmethod
    def audit(db, event, subject='', object_id=''):
        # Only server-generated labels and public identifiers, never request data.
        db.execute('INSERT INTO audit(at,event,subject,object_id) VALUES(?,?,?,?)',
                   (now(), event, subject, object_id))
        db.execute('DELETE FROM audit WHERE seq <= (SELECT COALESCE(MAX(seq),0)-10000 FROM audit)')

    def revoke(self, db, grant_id, event='grant_revoked'):
        grant = self.get(db, 'grant', grant_id)
        if grant and not grant.get('revoked'):
            grant['revoked'] = now()
            self.put(db, 'grant', grant_id, grant)
            self.audit(db, event, grant['subject'], grant_id)

    def active_grant(self, db, grant_id):
        grant = self.get(db, 'grant', grant_id)
        return grant if grant and not grant.get('revoked') and grant['expires'] > now() else None

    def access(self, token, resource):
        if not isinstance(token, str) or not token.startswith('rma_') or len(token) != 47:
            return None
        with self.transaction(False) as db:
            value = self.get(db, 'access', digest(token))
            grant = self.active_grant(db, value['grant']) if value and value['expires'] > now() else None
            if not grant or grant['resource'] != resource:
                return None
            return dict(grant, scope=value['scope'])

    def invite(self, subject, ttl=86400, uses=1):
        if not 1 <= ttl <= 2592000 or not 1 <= uses <= 100:
            raise StoreError('Invalid invitation lifetime or uses')
        value, ident = secret('rmi_'), secret('inv_')
        with self.transaction() as db:
            if self.count(db, 'invite') >= 4096:
                raise StoreError('Invitation quota reached; prune expired state')
            self.put(db, 'invite', digest(value), dict(id=ident, subject=subject, expires=now()+ttl,
                                                      remaining=uses, revoked=0))
            self.audit(db, 'invite_created', subject, ident)
        return ident, value

    def limited(self, category, source, per_source, total, period=60):
        """Persistent limits. Ignore client-controlled forwarded IP headers."""
        stamp = now()
        allowed = True
        with self.transaction() as db:
            db.execute('DELETE FROM rate WHERE expires<=?', (stamp,))
            for identity, limit in (('global', total), (digest(source), per_source)):
                key = category + ':' + str(stamp // period) + ':' + identity
                db.execute('INSERT INTO rate VALUES(?,1,?) ON CONFLICT(id) DO UPDATE SET count=count+1',
                           (key, (stamp // period + 1)*period))
                count = db.execute('SELECT count FROM rate WHERE id=?', (key,)).fetchone()[0]
                allowed = allowed and count <= limit
        return allowed

    def prune(self):
        """Keep refresh/code replay tombstones until their grant expires."""
        stamp = now()
        removed = 0
        with self.transaction() as db:
            for kind in ('transaction', 'invite', 'code', 'access', 'refresh', 'grant'):
                for key, value in self.rows(db, kind):
                    expires = value.get('expires', 0)
                    if kind in ('code', 'access', 'refresh'):
                        grant = self.get(db, 'grant', value['grant'])
                        expires = max(expires, grant['expires'] if grant else 0)
                    if expires <= stamp:
                        self.delete(db, kind, key)
                        removed += 1
        return removed
