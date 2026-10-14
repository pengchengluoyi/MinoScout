"""Controlled local fixture, not production authentication. No request dedup."""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class DemoServer(ThreadingHTTPServer):
    def __init__(self, address):
        super().__init__(address, Handler)
        self.lock = threading.Lock()
        self.submissions = 0
        self.interactions = 0
        self.accepted = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Fixture inputs never enter HTTP access logs.

    def reply(self, payload, code=200, mime='application/json'):
        content = json.dumps(payload).encode() if mime == 'application/json' else payload
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/state':
            with self.server.lock:
                state = {key: getattr(self.server, key) for key in ('submissions', 'interactions', 'accepted')}
            self.reply(state)
        elif path in {'/', '/login', '/previous'}:
            self.reply(Path(__file__).with_name('index.html').read_bytes(), mime='text/html; charset=utf-8')
        else:
            self.reply({'error': 'not_found'}, 404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length > 16384:
            return self.reply({'error': 'request_too_large'}, 413)
        try:
            body = json.loads(self.rfile.read(length) or '{}')
        except (ValueError, TypeError):
            return self.reply({'error': 'invalid_json'}, 400)
        path = urlsplit(self.path).path
        if path == '/interaction':
            with self.server.lock:
                self.server.interactions += 1
            return self.reply({'ok': True})
        if path == '/reset':
            # Baseline reset never erases the lifetime submission counter.
            return self.reply({'reset': True})
        if path != '/submit':
            return self.reply({'error': 'not_found'}, 404)
        with self.server.lock:
            self.server.submissions += 1
        time.sleep(max(0, min(12, float(body.get('delay_ms', 0)) / 1000)))
        if not body.get('username'):
            return self.reply({'ok': False, 'message': '账号不能为空'})
        if not body.get('consent'):
            return self.reply({'ok': False, 'message': '请勾选同意'})
        if body.get('username') != 'demo' or body.get('password') != 'demo-valid':
            return self.reply({'ok': False, 'message': '账号或密码错误'})
        with self.server.lock:
            self.server.accepted += 1
        self.reply({'ok': True, 'identity': 'demo-user', 'message': '登录成功'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = DemoServer(('127.0.0.1', args.port))
    print(f'Controlled demo: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
