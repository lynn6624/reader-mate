"""Local OAuth administration. Never changes identities.json or existing MCP keys."""
import argparse
import json
import os
from pathlib import Path
import sys

from oauth_server import Config
from oauth_store import Store, StoreError, now
from reader_oauth import IdentityAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('init')
    invite = actions.add_parser('invite')
    invite.add_argument('--identities', required=True, type=Path)
    invite.add_argument('--reader-id', required=True)
    invite.add_argument('--output', required=True, type=Path, help='New private file; never overwritten')
    invite.add_argument('--ttl', type=int, default=86400)
    invite.add_argument('--uses', type=int, default=1)
    actions.add_parser('grants')
    actions.add_parser('invites')
    revoke = actions.add_parser('revoke')
    revoke.add_argument('--grant-id', required=True)
    revoke_invite = actions.add_parser('revoke-invite')
    revoke_invite.add_argument('--invite-id', required=True)
    actions.add_parser('audit')
    actions.add_parser('prune')
    args = parser.parse_args()
    config = Config.from_env()
    store = Store(config.db, config.issuer, config.resource)
    if args.action == 'init':
        store.initialize()
        print('OAuth store initialized; no reader credentials changed.')
    elif args.action == 'invite':
        if not IdentityAdapter(str(args.identities)).lookup(args.reader_id):
            raise StoreError('Reader identity unavailable or not permitted for MCP')
        # Reserve the destination before creating the invitation; no terminal output of secrets.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            ident, value = store.invite(args.reader_id, args.ttl, args.uses)
            stream.write(value + '\n')
        print(json.dumps({'invite_id': ident, 'reader_id': args.reader_id}))
    elif args.action == 'prune':
        print(json.dumps({'removed': store.prune()}))
    else:
        with store.transaction() as db:
            if args.action == 'revoke':
                if not store.get(db, 'grant', args.grant_id):
                    raise StoreError('Unknown grant ID')
                store.revoke(db, args.grant_id)
                print('Authorization revoked; reader identity and legacy credentials preserved.')
            elif args.action == 'revoke-invite':
                found = False
                for key, value in store.rows(db, 'invite'):
                    if value['id'] == args.invite_id:
                        value['revoked'] = now()
                        store.put(db, 'invite', key, value)
                        store.audit(db, 'invite_revoked', value['subject'], value['id'])
                        found = True
                if not found:
                    raise StoreError('Unknown invitation ID')
                print('Invitation revoked; existing grants preserved.')
            elif args.action == 'grants':
                print(json.dumps([value for _, value in store.rows(db, 'grant')], ensure_ascii=False))
            elif args.action == 'invites':
                print(json.dumps([value for _, value in store.rows(db, 'invite')], ensure_ascii=False))
            elif args.action == 'audit':
                rows = db.execute('SELECT at,event,subject,object_id FROM audit ORDER BY seq DESC LIMIT 100').fetchall()
                print(json.dumps([dict(zip(('at', 'event', 'subject', 'object_id'), row)) for row in rows], ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (StoreError, OSError, ValueError):
        print('OAuth administration failed; check configuration, private paths and public object IDs.', file=sys.stderr)
        raise SystemExit(1)
