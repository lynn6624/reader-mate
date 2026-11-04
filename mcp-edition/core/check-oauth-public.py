#!/usr/bin/env python3
"""Credential-free OAuth smoke check. Run from the VPS and a client network."""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issuer', required=True, help='Actual HTTPS authorization server origin')
    parser.add_argument('--user-agent', default='python-httpx/0.28.1',
                        help='Request agent for a controlled comparison; defaults to the pinned SDK HTTP client')
    args = parser.parse_args()
    base = args.issuer
    if not args.user_agent or len(args.user_agent) > 256 or re.search(r'[\r\n\x00]', args.user_agent):
        parser.error('Invalid user agent')
    parts = urllib.parse.urlsplit(base)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment or re.search(r'[\s\\]', base)):
        parser.error('--issuer must be an HTTPS origin without credentials, path or parameters')
    try:
        parts.port
    except ValueError:
        parser.error('Invalid issuer port')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    checks = []
    for method, path, expected in (
            ('GET', '/.well-known/oauth-authorization-server', 200),
            ('GET', '/.well-known/oauth-protected-resource/mcp', 200),
            ('POST', '/oauth/register', 400),
            ('POST', '/mcp', 401)):
        row = dict(method=method, path=path, expected_status=expected, passed=False)
        request = urllib.request.Request(base + path, method=method,
            data=b'{}' if method == 'POST' else None,
            headers={'Content-Type': 'application/json', 'User-Agent': args.user_agent})
        try:
            try:
                response = opener.open(request, timeout=12)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                raw = response.read(65537)
                row.update(status=response.status, content_type=response.headers.get_content_type(),
                    cloudflare_challenge=response.headers.get('cf-mitigated') == 'challenge',
                    ray_id=response.headers.get('cf-ray'))
                try:
                    data = json.loads(raw) if len(raw) <= 65536 else None
                except (ValueError, UnicodeError):
                    data = None
                valid = isinstance(data, dict)
                if valid and path == '/oauth/register':
                    valid = data.get('error') == 'invalid_client_metadata'
                elif valid and path == '/mcp':
                    valid = 'resource_metadata="' + base + '/' in response.headers.get('WWW-Authenticate', '')
                elif valid and path.endswith('oauth-authorization-server'):
                    valid = (data.get('issuer') == base and data.get('registration_endpoint') == base + '/oauth/register'
                             and 'S256' in data.get('code_challenge_methods_supported', []))
                elif valid:
                    valid = data.get('resource') == base + '/mcp' and data.get('authorization_servers') == [base]
                row['passed'] = response.status == expected and valid and not row['cloudflare_challenge']
        except Exception as error:
            row['error_type'] = type(error).__name__
        checks.append(row)
    passed = all(row['passed'] for row in checks)
    print(json.dumps(dict(passed=passed, checks=checks, credentials_used=False,
                         clients_or_reader_grants_created=False), indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
