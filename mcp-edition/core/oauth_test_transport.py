"""Loopback-only reverse proxy/callback for synthetic OAuth acceptance tests."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import ssl
import threading
from urllib.parse import urlsplit


def bridge(port, upstream, cert_path=None, key_path=None):
    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path == '/client-callback':
                body = b'<!doctype html><title>ReaderMate test callback</title><p>Authorization callback received.</p>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            self.forward()

        def do_POST(self):
            self.forward()

        def do_DELETE(self):
            self.forward()

        def do_HEAD(self):
            self.forward()

        def forward(self):
            if not hasattr(self.server, 'sanitized_requests'):
                self.server.sanitized_requests = []
            # This report excludes queries, headers containing credentials and request bodies.
            trace = self.server.sanitized_requests
            trace.append({'method':self.command,'path':urlsplit(self.path).path,
                'origin':self.headers.get('Origin'), 'has_cookie':'__Host-reader-oauth=' in self.headers.get('Cookie','')})
            del trace[:-100]
            if not self.path.startswith('/') or urlsplit(self.path).netloc:
                self.send_error(400); return
            length = int(self.headers.get('Content-Length', '0'))
            if length > 16384:
                self.send_error(413); return
            data = self.rfile.read(length) if length else None
            excluded = {'connection','keep-alive','transfer-encoding','content-length','upgrade','te','trailer'}
            headers = {key:value for key,value in self.headers.items() if key.lower() not in excluded}
            connection = http.client.HTTPConnection('127.0.0.1', upstream, timeout=30)
            try:
                connection.request(self.command, self.path, body=data, headers=headers)
                response = connection.getresponse(); body = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in excluded:
                        self.send_header(key, value)
                self.send_header('Content-Length', str(len(body))); self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)
            finally:
                connection.close()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Proxy)
    if cert_path:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == '__main__':
    import argparse
    import signal
    import time
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,required=True)
    parser.add_argument('--upstream',type=int,required=True)
    args=parser.parse_args()
    server=bridge(args.port,args.upstream)
    stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    stop.wait()
    server.shutdown();server.server_close()
