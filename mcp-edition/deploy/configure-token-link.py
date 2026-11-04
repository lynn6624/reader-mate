#!/usr/bin/env python3
"""Set the deployment-specific Token portal link without changing reader data."""
import argparse
import html
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


def configured_page(raw, origin):
    parsed = urlsplit(origin)
    host = (parsed.hostname or '').lower()
    if (re.search(r'[\s\\]', origin) or parsed.scheme != 'https' or not host or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or any(host == domain or host.endswith('.' + domain)
                   for domain in ('example.com', 'example.net', 'example.org'))):
        raise ValueError('Use the actual HTTPS Token origin, without credentials or URL parameters')
    # Validate the port as well; urlsplit otherwise accepts malformed ports.
    parsed.port
    link = html.escape(origin.rstrip('/') + '/', quote=True).encode('utf-8')
    pattern = rb'(<a\b(?=[^>]*\bclass="tokenLink")[^>]*\bhref=")([^"]*)(")'
    updated, count = re.subn(pattern, lambda m: m[1] + link + m[3], raw)
    if count != 1:
        raise ValueError('Expected exactly one Token management link; no file changed')
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html', type=Path, required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--check', action='store_true', help='Verify without writing')
    args = parser.parse_args()
    raw = args.html.read_bytes()
    updated = configured_page(raw, args.origin)
    if args.check:
        raise SystemExit(0 if raw == updated else 'Token link does not match the configured origin')
    if raw == updated:
        return
    metadata = args.html.stat()
    fd, temporary = tempfile.mkstemp(prefix=args.html.name + '.', dir=args.html.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if hasattr(os, 'chown'):
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.chmod(temporary, metadata.st_mode & 0o777)
        os.replace(temporary, args.html)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    main()
