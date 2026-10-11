"""Invented OAuth HTTP server/client, restricted to loopback in scripts/offline."""
import base64
from collections import deque
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import secrets
import socket
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

from poolrotater_host.oauth import AuthError, SCOPES


def synthetic_token(user_id=99, client_id=123, scopes=SCOPES, *, expires_at=None, **claims):
    payload = {'sub': str(user_id), 'aud': [str(client_id)], 'scopes': list(scopes) if isinstance(scopes, (tuple, list)) else scopes,
               'exp': expires_at or time.time() + 3600, 'jti': secrets.token_hex(12), **claims}
    encode = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')
    return encode({'alg': 'synthetic-fixture'}) + '.' + encode(payload) + '.invented'


class LoopbackTransport:
    offline_fixture = True

    def __init__(self, origin):
        parts = urlsplit(origin)
        if (os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1' or parts.scheme != 'http'
                or parts.hostname != '127.0.0.1' or not parts.port or parts.netloc != f'127.0.0.1:{parts.port}'
                or parts.path or parts.query or parts.fragment):
            raise AuthError('offline_loopback_transport_required')
        self.port = parts.port

    def request(self, method, path, *, form=None, bearer=None):
        if (method, path) not in {('POST', '/oauth/token'), ('GET', '/api/v2/me'), ('DELETE', '/api/v2/oauth/tokens/current')}:
            raise AuthError('fixture_endpoint_not_allowed')
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        headers = {'Accept': 'application/json'}
        if bearer is not None: headers['Authorization'] = 'Bearer ' + bearer
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(65537)
            if len(data) > 65536: raise AuthError('fixture_reply_too_large')
            # Do not follow redirects, consult proxies, or retry mutations.
            return response.status, json.loads(data) if data else None
        finally: connection.close()


class OAuthProvider:
    """Faults can replace a response after the provider has performed its mutation."""
    def __init__(self):
        self.codes = {}
        self.access = {}
        self.refresh = {}
        self.calls = []
        self.faults = {}
        self.server = None
        self.thread = None

    def code(self, value='invented-code', *, user_id=99, client_id=123, scopes=SCOPES, restricted=False):
        self.codes[value] = (user_id, client_id, tuple(scopes), restricted)
        return value

    def fault(self, path, status, body=None, *, after=False):
        self.faults.setdefault(path, deque()).append((status, body, after))

    def _issue(self, identity):
        user_id, client_id, scopes, restricted = identity
        access = synthetic_token(user_id, client_id, scopes)
        refresh = 'invented-refresh-' + secrets.token_hex(16)
        self.access[access] = identity
        self.refresh[refresh] = (identity, access)
        return 200, {'token_type': 'Bearer', 'access_token': access, 'refresh_token': refresh, 'expires_in': 3600}

    def handle(self, method, path, form, bearer, headers):
        self.calls.append({'method': method, 'path': path, 'form': form, 'bearer': bearer, 'headers': dict(headers)})
        fault = self.faults.get(path)
        fault = fault.popleft() if fault else None
        if fault and not fault[2]: return fault[:2]
        if method == 'POST' and path == '/oauth/token':
            identity = None
            if form.get('client_id') == '123' and form.get('client_secret') == 'invented-client-secret':
                if form.get('grant_type') == 'authorization_code': identity = self.codes.pop(form.get('code'), None)
                elif form.get('grant_type') == 'refresh_token':
                    pair = self.refresh.pop(form.get('refresh_token'), None)
                    if pair:
                        identity, old = pair
                        self.access.pop(old, None)
            result = self._issue(identity) if identity else (400, {'error': 'invalid_grant', 'echo': 'invented-sensitive-error'})
        elif method == 'GET' and path == '/api/v2/me':
            identity = self.access.get(bearer)
            result = (200, {'id': identity[0], 'username': 'Synthetic operator', 'is_restricted': identity[3]}) if identity else (401, {})
        elif method == 'DELETE' and path == '/api/v2/oauth/tokens/current':
            if self.access.pop(bearer, None):
                self.refresh = {r: value for r, value in self.refresh.items() if value[1] != bearer}
                result = 204, None
            else: result = 401, {}
        else: result = 404, {}
        return fault[:2] if fault else result

    def __enter__(self):
        if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1': raise AuthError('offline_fixture_required')
        provider = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def respond(self):
                count = int(self.headers.get('Content-Length', '0'))
                if not 0 <= count <= 32768: self.send_error(400); return
                form = dict(parse_qsl(self.rfile.read(count).decode(), strict_parsing=True)) if count else {}
                bearer = self.headers.get('Authorization', '').removeprefix('Bearer ') or None
                status, body = provider.handle(self.command, self.path, form, bearer, self.headers)
                if status == 'disconnect':
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                data = json.dumps(body).encode() if body is not None else b''
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)
            do_POST = do_GET = do_DELETE = respond
        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.transport = LoopbackTransport('http://127.0.0.1:' + str(self.server.server_port))
        return self

    def __exit__(self, *args):
        self.server.shutdown(); self.thread.join(timeout=3); self.server.server_close()
