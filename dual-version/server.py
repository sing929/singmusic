"""Loopback-only desktop UI preview. No model invocation endpoints."""
import argparse
import json
import mimetypes
import os
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
PORT = 18765
APP_ID = 'sing-studio-ui-v1'
ASSETS = {
    '/': ROOT / 'frontend/index.html',
    '/app.js': ROOT / 'app.js',
    '/local.js': ROOT / 'local.js',
    '/style.css': ROOT / 'style.css',
    '/audio/original.wav': WORKSPACE / 'local-test/samples/fishin-30s.wav',
    '/audio/remix.wav': WORKSPACE / 'local-test/outputs/ace/e0366613-1217-0663-b780-72e114dd1115.wav',
    '/audio/vocals.wav': WORKSPACE / 'local-test/outputs/uvr/fishin-30s_(Vocals)_UVR-MDX-NET-Inst_HQ_3.wav',
    '/audio/instrumental.wav': WORKSPACE / 'local-test/outputs/uvr/fishin-30s_(Instrumental)_UVR-MDX-NET-Inst_HQ_3.wav',
}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if self.headers.get('Host') not in (f'127.0.0.1:{PORT}', f'localhost:{PORT}'):
            self.send_error(403)
            return
        route = urlsplit(self.path).path
        if route == '/health':
            payload = json.dumps({'app': APP_ID, 'mode': 'ui-preview', 'generation_enabled': False}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if not head:
                self.wfile.write(payload)
            return
        path = ASSETS.get(route)
        if route.startswith('/assets/'):
            candidate=(ROOT/'frontend'/route.lstrip('/')).resolve()
            if candidate.is_relative_to((ROOT/'frontend/assets').resolve()) and candidate.suffix in ('.js','.css','.woff2','.png','.svg'):
                path=candidate
        if path is None or not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get('Range')
        if range_header and route.startswith('/audio/'):
            import re
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', range_header)
            if not match:
                self.send_error(416)
                return
            start = int(match[1])
            end = min(int(match[2]) if match[2] else end, end)
            if start > end:
                self.send_error(416)
                return
            status = 206
        self.send_response(status)
        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(end-start+1))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self' blob:; object-src 'none'; frame-ancestors 'none'")
        if route.startswith('/audio/'):
            self.send_header('Accept-Ranges', 'bytes')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if not head:
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end-start+1
                while remaining:
                    chunk = stream.read(min(262144, remaining))
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        break
                    remaining -= len(chunk)

def desktop():
    edge = Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
    if not edge.exists():
        raise RuntimeError('Microsoft Edge is required to display this local desktop preview.')
    subprocess.Popen([str(edge), f'--app=http://127.0.0.1:{PORT}', '--window-size=1440,1000',
        '--no-first-run', f'--user-data-dir={ROOT / "data" / "window-profile"}'])

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--desktop', action='store_true')
    args = parser.parse_args()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health', timeout=1) as response:
            existing = json.load(response)
        if existing.get('app') != APP_ID:
            raise RuntimeError('Port occupied by another application')
    except (OSError, ValueError):
        server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
        if args.desktop:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            desktop()
            while True:
                time.sleep(10)
        else:
            server.serve_forever()
    else:
        if args.desktop:
            desktop()
