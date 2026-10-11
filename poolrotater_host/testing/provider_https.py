"""Invented loopback TLS provider. Test CA/key never leave the private fixture."""
from collections import deque
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from ..oauth import AuthError
from ..provider_contract import OWNED_PATH, exchange_fixture, prepare
from .oauth import OAuthProvider


def certificates(folder):
    if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1': raise AuthError('offline_fixture_required')
    folder = Path(folder)
    def run(*args):
        subprocess.run(['/usr/bin/openssl', *args], cwd=folder, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    run('req', '-config', '/dev/null', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
        '-subj', '/CN=Invented offline CA', '-keyout', 'ca.key', '-out', 'ca.crt',
        '-addext', 'basicConstraints=critical,CA:TRUE')
    for name, san in (('server', 'IP:127.0.0.1'), ('wrong-host', 'DNS:localhost')):
        run('req', '-config', '/dev/null', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=Invented offline provider',
            '-keyout', name+'.key', '-out', name+'.csr')
        (folder/(name+'.ext')).write_text('subjectAltName='+san+'\nextendedKeyUsage=serverAuth\n')
        run('x509', '-req', '-in', name+'.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key',
            '-CAcreateserial', '-days', '2', '-extfile', name+'.ext', '-out', name+'.crt')
    for path in folder.iterdir(): path.chmod(0o600)


class TLSFixtureTransport:
    offline_fixture = True

    def __init__(self, origin, context, *, timeout=2):
        parts = urlsplit(origin)
        if (os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1' or parts.scheme != 'https'
                or parts.hostname != '127.0.0.1' or not parts.port
                or parts.netloc != f'127.0.0.1:{parts.port}' or parts.path or parts.query or parts.fragment
                or context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname):
            raise AuthError('offline_verified_tls_required')
        self.port, self.context, self.timeout = parts.port, context, timeout

    def request(self, method, path, *, form=None, bearer=None):
        request = prepare(method, path, form=form, bearer=bearer)
        connection = http.client.HTTPSConnection('127.0.0.1', self.port, context=self.context, timeout=self.timeout)
        return exchange_fixture(connection, request, timeout=self.timeout)


class TLSProvider(OAuthProvider):
    def __init__(self, folder, *, wrong_host=False):
        super().__init__()
        self.folder = Path(folder); self.wrong_host = wrong_host
        self.wires = {}; self.rooms = []; self.details = {}; self.on_read = None
        self.api_handler = None
        self.referee_handler = None

    def wire(self, path, status, body=b'', *, headers=None, chunked=False, delay=0):
        self.wires.setdefault(path, deque()).append((status, body, headers, chunked, delay))

    def handle(self, method, path, form, bearer, headers):
        if self.api_handler is not None:
            result = self.api_handler(method, path, form, bearer, headers)
            if result is not None: return result
        if method == 'GET' and (path == OWNED_PATH or path.startswith('/api/v2/rooms/')):
            self.calls.append({'method': method, 'path': path, 'form': form, 'bearer': bearer, 'headers': dict(headers)})
            if self.on_read: self.on_read(path)
            if bearer not in self.access: return 401, {}
            return (200, self.rooms) if path == OWNED_PATH else self.details.get(path.rsplit('/', 1)[1], (404, {}))
        return super().handle(method, path, form, bearer, headers)

    def __enter__(self):
        if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1': raise AuthError('offline_fixture_required')
        provider = self
        class Handler(BaseHTTPRequestHandler):
            rbufsize = 0
            def log_message(self, *args): pass
            def respond(self):
                if (not provider.wires.get(self.path) and provider.referee_handler is not None
                        and provider.referee_handler.handle(self)):
                    self.close_connection = True
                    return
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 32768: self.send_error(400); return
                raw = self.rfile.read(length)
                if raw and self.headers.get('Content-Type') == 'application/json':
                    form = json.loads(raw.decode('utf-8'))
                else:
                    form = dict(parse_qsl(raw.decode(), strict_parsing=True)) if raw else {}
                bearer = self.headers.get('Authorization', '').removeprefix('Bearer ') or None
                if provider.wires.get(self.path):
                    status, data, headers, chunked, delay = provider.wires[self.path].popleft()
                else:
                    status, body = provider.handle(self.command, self.path, form, bearer, self.headers)
                    if status == 'disconnect':
                        self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                    data = json.dumps(body).encode() if body is not None else b''
                    headers, chunked, delay = None, False, 0
                try:
                    self.send_response(status)
                    for name, value in (headers if headers is not None else [('Content-Type', 'application/json'),
                            ('Transfer-Encoding', 'chunked')] if chunked else [('Content-Type', 'application/json'),
                            ('Content-Length', str(len(data)))]): self.send_header(name, value)
                    self.end_headers()
                    if delay: time.sleep(delay)
                    if chunked:
                        for start in range(0, len(data), 1024):
                            part = data[start:start+1024]
                            self.wfile.write(f'{len(part):x}\r\n'.encode()+part+b'\r\n')
                        self.wfile.write(b'0\r\n\r\n')
                    else: self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError): pass
                self.close_connection = True
            do_POST = do_GET = do_DELETE = do_PUT = respond
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        name = 'wrong-host' if self.wrong_host else 'server'
        context.load_cert_chain(self.folder/(name+'.crt'), self.folder/(name+'.key'))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        client = ssl.create_default_context(cafile=str(self.folder/'ca.crt'))
        self.transport = TLSFixtureTransport('https://127.0.0.1:'+str(self.server.server_port), client)
        return self

    def __exit__(self, *args):
        if self.referee_handler is not None: self.referee_handler.stop()
        return super().__exit__(*args)
