"""Controllable loopback-only immutable catalog fixture; no production publisher."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import socket
import ssl
import threading
import time

from ..runtime_package import fixture_only
from ..catalog_trust import bounded_file
from ..catalog_export import MAX_FILE


class CatalogHTTPFixture:
    def __init__(self, repository):
        fixture_only()
        self.repository = repository
        self.requests = []
        self.bytes_sent = 0
        self.fault = None
        self.fault_path = 'bundle.tar.gz'
        self.etag_suffix = ''
        self.retry_after = '120'
        self.delay = .2

    @contextmanager
    def serve(self, *, tls=None):
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_GET(self):
                fault = fixture.fault if fixture.fault_path in self.path else None
                fixture.requests.append({'path': self.path, 'range': self.headers.get('Range'),
                                         'conditional': self.headers.get('If-None-Match'), 'fault': fault})
                if fault == 'timeout': time.sleep(fixture.delay)
                if fault in ('redirect', '429'):
                    self.send_response(302 if fault == 'redirect' else 429)
                    self.send_header('Content-Length', '0')
                    self.send_header('Retry-After', fixture.retry_after)
                    self.send_header('Location', 'https://external.invalid/')
                    self.end_headers(); return
                name = self.path.lstrip('/')
                if (not re.fullmatch(r'(metadata|targets)/[a-zA-Z0-9_.\-/]+', name)
                        or any(x in ('', '.', '..') for x in name.split('/'))):
                    self.send_error(400); return
                try:
                    with bounded_file(fixture.repository.folder/name, MAX_FILE) as f: data = f.read()
                except FileNotFoundError: self.send_error(404); return
                etag = '"' + hashlib.sha256(data).hexdigest() + fixture.etag_suffix + '"'
                if self.headers.get('If-None-Match') == etag:
                    self.send_response(304); self.send_header('ETag', etag); self.end_headers(); return
                start = 0
                value = self.headers.get('Range', '')
                if re.fullmatch(r'bytes=[0-9]+-', value) and self.headers.get('If-Range') == etag and fault != 'fallback':
                    start = int(value[6:-1])
                self.send_response(206 if start else 200)
                self.send_header('ETag', etag)
                self.send_header('Content-Length', str(len(data)-start))
                if start: self.send_header('Content-Range', f'bytes {start+(1 if fault=="bad-range" else 0)}-{len(data)-1}/{len(data)}')
                self.end_headers()
                try:
                    if fault == 'interrupt':
                        fixture.fault = None
                        part = data[start:start + max(1, (len(data)-start)//2)]
                        self.wfile.write(part); self.wfile.flush(); fixture.bytes_sent += len(part)
                        self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                    self.wfile.write(data[start:]); fixture.bytes_sent += len(data)-start
                except (BrokenPipeError, ConnectionResetError): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*map(str, tls)); server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try: yield ('https' if tls else 'http') + f'://127.0.0.1:{server.server_port}/'
        finally: server.shutdown(); server.server_close(); thread.join(timeout=5)
