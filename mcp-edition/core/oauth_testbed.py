"""Synthetic OAuth test services. Never reads production paths; refuses existing roots."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
MARKER = 'reader-mate-oauth-synthetic-v1'


def private_json(path, value):
    path = Path(path)
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
    path.chmod(0o600)


def free_port():
    with socket.socket() as stream:
        stream.bind(('127.0.0.1', 0))
        return stream.getsockname()[1]


def prepare(root, issuer, mcp_port=None, large=False, access_ttl=30):
    from oauth_server import Config
    from oauth_store import Store
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    core = root/'core'
    shutil.copytree(ROOT, core, ignore=shutil.ignore_patterns('data', '书库', '__pycache__'))
    manager = root/'token-mcp'
    shutil.copytree(ROOT.parent/'token-mcp', manager, ignore=shutil.ignore_patterns('__pycache__'))
    for directory in (core/'data', core/'书库', root/'state', root/'logs'):
        directory.mkdir(mode=0o700)
    identities = {subject:secrets.token_urlsafe(32) for subject in ('user','op001','op004','004')}
    private_json(core/'data'/'identities.json', identities)
    private_json(core/'data'/'.token-admin.json', {'salt':secrets.token_hex(16),'hash':secrets.token_hex(32)})
    keys = {name:secrets.token_urlsafe(18) for name in ('path-key','gpt-key')}
    for name, value in keys.items():
        (root/'state'/name).write_text(value, encoding='ascii')
        (root/'state'/name).chmod(0o600)
    (core/'书库'/'oauth-stage2.txt').write_text('第一章\n你好😀一起读书。\n第二章\n合成数据，仅供 OAuth 测试。\n', encoding='utf-8')
    if large:
        # Approximately two million UTF-16 units; constructed by the controller, outside a budget cgroup.
        (core/'书库'/'oauth-large.txt').write_text('第一章 合成大书\n'+('甲乙😀丙丁。\n'*250000), encoding='utf-8')
    config = Config(str(root/'state'/'oauth.sqlite3'), issuer, issuer+'/mcp', access_ttl=access_ttl)
    Store(config.db, issuer, config.resource).initialize()
    spec = dict(marker=MARKER, root=str(root), issuer=issuer, resource=config.resource,
                web_port=free_port(), mcp_port=mcp_port or free_port(), portal_port=free_port(),
                access_ttl=access_ttl, large=large)
    private_json(root/'fixture.json', spec)
    return spec


def load(root):
    root = Path(root).resolve()
    spec = json.loads((root/'fixture.json').read_text(encoding='utf-8'))
    if spec.get('marker') != MARKER or Path(spec['root']) != root:
        raise ValueError('Unknown fixture; refusing service operation')
    return spec


class Services:
    def __init__(self, root, oauth=True):
        self.spec = load(root)
        self.root = Path(root).resolve()
        self.oauth = oauth
        self.children, self.logs = [], []

    def start(self):
        spec, root = self.spec, self.root
        env = dict(os.environ, READER_CLOUD='1', READER_PORT=str(spec['web_port']),
            READER_MCP_PORT=str(spec['mcp_port']), READER_API_URL='http://127.0.0.1:'+str(spec['web_port']),
            READER_MCP_PUBLIC_HOST='legacy.stage2.example.test', READER_MCP_EXTRA_HOST='url.stage2.example.test',
            READER_MCP_PATH_KEY_FILE=str(root/'state'/'path-key'), READER_MCP_004_KEY_FILE=str(root/'state'/'gpt-key'),
            READER_OAUTH_ENABLED='1' if self.oauth else '0', READER_OAUTH_DB=str(root/'state'/'oauth.sqlite3'),
            READER_OAUTH_ISSUER=spec['issuer'], READER_OAUTH_RESOURCE=spec['resource'],
            READER_OAUTH_ACCESS_TTL=str(spec['access_ttl']), READER_CORE_DIR=str(root/'core'),
            READER_DATA_DIR=str(root/'core'/'data'), TOKEN_PORT=str(spec['portal_port']), TOKEN_HOST='127.0.0.1',
            READER_TOKEN='', READER_TTS='disabled', READER_TTS_KEY='')
        try:
            for name, script in (('web','core/reader_server.py'),('mcp','core/mcp_server.py'),('portal','token-mcp/server.py')):
                log = open(root/'logs'/(name+'.log'), 'ab', buffering=0)
                self.logs.append(log)
                child_env = dict(env)
                if name == 'web':
                    child_env.update(MALLOC_ARENA_MAX='1', MALLOC_TRIM_THRESHOLD_='131072')
                options = dict(creationflags=subprocess.CREATE_NO_WINDOW) if os.name == 'nt' else {}
                self.children.append(subprocess.Popen([sys.executable,'-B',str(root/script)], env=child_env,
                                                     stdout=log, stderr=log, **options))
        except BaseException:
            self.stop()
            raise
        private_json(root/'pids.json', {'launcher':os.getpid(),'children':[child.pid for child in self.children]})
        return self

    def stop(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
        for child in self.children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=10)
        for stream in self.logs:
            stream.close()
        self.children, self.logs = [], []

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create','serve'))
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--issuer')
    parser.add_argument('--mcp-port', type=int)
    parser.add_argument('--large', action='store_true')
    parser.add_argument('--access-ttl', type=int, default=30)
    parser.add_argument('--bridge-port', type=int)
    args = parser.parse_args()
    if args.action == 'create':
        if not args.issuer:
            parser.error('--issuer required for create')
        spec = prepare(args.root, args.issuer, args.mcp_port, args.large, args.access_ttl)
        print(json.dumps({key:spec[key] for key in ('issuer','resource','web_port','mcp_port','portal_port')}))
        return
    stop = False
    def requested(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, requested)
    signal.signal(signal.SIGINT, requested)
    with Services(args.root) as services:
        proxy = None
        if args.bridge_port:
            from oauth_test_transport import bridge
            proxy = bridge(args.bridge_port, services.spec['mcp_port'])
        try:
            while not stop and not (args.root/'STOP').exists():
                if any(child.poll() is not None for child in services.children):
                    raise SystemExit('A synthetic service stopped; inspect private fixture logs')
                time.sleep(.2)
        finally:
            if proxy:
                proxy.shutdown(); proxy.server_close()


if __name__ == '__main__':
    main()
