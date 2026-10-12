#!/usr/bin/env python3
"""Serve the Pi camera viewer locally and relay the Pi's MJPEG stream.

Browsers block pages on one origin from pulling a stream off a LAN device,
so this serves index.html and proxies /stream.mjpg from the same origin.

Usage: python3 serve.py [--pi raspberrypi.local:8080] [--port 8790]
"""
import argparse
import http.server
import pathlib
import shutil
import socketserver
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent


def make_handler(pi):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path in ('/', '/index.html'):
                body = (HERE / 'index.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path in ('/stream.mjpg', '/status.json'):
                self.relay(f'http://{pi}{path}')
            else:
                self.send_error(404)

        def relay(self, url):
            try:
                upstream = urllib.request.urlopen(url, timeout=5)
            except OSError as e:
                self.send_error(502, f'Camera unreachable: {e}')
                return
            with upstream:
                self.send_response(200)
                self.send_header('Content-Type', upstream.headers.get('Content-Type', 'application/octet-stream'))
                self.send_header('Cache-Control', 'no-cache, private')
                self.end_headers()
                try:
                    shutil.copyfileobj(upstream, self.wfile, 64 * 1024)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Viewer closed the tab.

        def log_message(self, fmt, *args):
            pass

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pi', default='raspberrypi.local:8080', help='camera host:port')
    parser.add_argument('--port', type=int, default=8790, help='local port to serve on')
    args = parser.parse_args()
    server = Server(('127.0.0.1', args.port), make_handler(args.pi))
    print(f'Pi camera viewer: http://127.0.0.1:{args.port}/  (camera {args.pi})')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
