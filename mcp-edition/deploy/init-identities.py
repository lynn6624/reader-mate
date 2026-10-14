"""Run on the VPS only. Refuses to replace existing credentials; prints no secrets."""
import json, os, pathlib, secrets
path = pathlib.Path(__file__).resolve().parents[1] / 'core' / 'data' / 'identities.json'
path.parent.mkdir(parents=True, exist_ok=True)
with path.open('x', encoding='utf8') as f:
    os.chmod(path, 0o600)
    json.dump({who: secrets.token_urlsafe(32) for who in ('user',)}, f, indent=2)
print('Created identities.json. Retrieve each credential privately; never commit it.')
