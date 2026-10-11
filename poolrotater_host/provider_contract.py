"""Fixed provider wire contract; only a protected live-pilot release may execute.

Candidate, rehearsal and qualification authority remains fenced independently of
URLs, environment flags and credentials. Transport children repeat admission.
"""
from dataclasses import dataclass, field
import http.client
import json
import math
import os
import re
import ssl
import time
from urllib.parse import urlencode

from .layout import unique_fields
from .oauth import AuthError, _positive, _secret, redirect_parts

PROVIDER_ORIGIN = 'https://osu.ppy.sh'
API_VERSION = '20220705'
ROOM_LIMIT = 250
OWNED_PATH = '/api/v2/rooms?' + urlencode(
    {'mode': 'owned', 'type_group': 'realtime', 'limit': ROOM_LIMIT, 'sort': 'created'})


def require_live_provider(execution=None):
    """Artifact-specific fence. Legacy/candidate/qualification never dispatch."""
    from .live_release import require
    require(execution)


@dataclass(frozen=True)
class PreparedRequest:
    method: str
    path: str
    headers: dict = field(repr=False)
    body: bytes | None = field(repr=False)
    maximum_reply: int


def prepare(method, path, *, form=None, bearer=None):
    """No arbitrary URL, caller headers, room mutation, or token grant is accepted."""
    if not isinstance(method, str) or not isinstance(path, str): raise AuthError('provider_endpoint_refused')
    detail = isinstance(path, str) and re.fullmatch(r'/api/v2/rooms/[1-9][0-9]{0,18}', path)
    if (method, path) not in {('POST', '/oauth/token'), ('GET', '/api/v2/me'),
                             ('DELETE', '/api/v2/oauth/tokens/current'), ('GET', OWNED_PATH)} and not (method == 'GET' and detail):
        raise AuthError('provider_endpoint_refused')
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity',
               'Connection': 'close', 'x-api-version': API_VERSION}
    body = None
    if method == 'POST':
        if not isinstance(form, dict) or bearer is not None:
            raise AuthError('provider_form_refused')
        grant = form.get('grant_type')
        fields = {'client_id', 'client_secret', 'grant_type'}
        if grant == 'authorization_code': fields |= {'code', 'redirect_uri'}
        elif grant == 'refresh_token': fields |= {'refresh_token'}
        else: raise AuthError('provider_grant_refused')
        if set(form) != fields: raise AuthError('provider_form_refused')
        _positive(form['client_id']); _secret(form['client_secret'], 8192)
        if grant == 'authorization_code':
            _secret(form['code'], 4096); redirect_parts(form['redirect_uri'])
        else: _secret(form['refresh_token'], 8192)
        body = urlencode(form).encode('ascii')
        if len(body) > 32768: raise AuthError('provider_request_too_large')
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    else:
        if form is not None: raise AuthError('provider_form_refused')
        headers['Authorization'] = 'Bearer ' + _secret(bearer)
    return PreparedRequest(method, path, headers, body, 1024**2 if detail or path == OWNED_PATH else 65536)


def exchange_fixture(connection, request, *, timeout=2, on_status=None):
    """One checked HTTPS exchange; no proxies, redirects, or mutation retries.

Only literal loopback connections in the isolated development namespace execute.
The fixture supplies its own trusted CA; hostname verification remains mandatory.
Socket timeouts bound individual I/O. A body deadline also rejects slow streaming;
this is not a whole-operation DNS/process deadline for a future live adapter.
"""
    if (os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1'
            or type(connection) is not http.client.HTTPSConnection
            or connection.host != '127.0.0.1' or connection._tunnel_host is not None
            or connection._context.verify_mode != ssl.CERT_REQUIRED
            or not connection._context.check_hostname):
        raise AuthError('offline_verified_tls_required')
    return _exchange_checked(connection, request, timeout=timeout, on_status=on_status)


def exchange_upstream(connection, request, *, timeout=5, on_status=None, execution=None):
    """Dormant fixed upstream route using the same qualified wire parser.

    The release fence is checked before connection/request inspection or I/O.
    Independent origin/TLS checks remain even after future fence implementation.
    No caller URL, tunnel, proxy, alternate port or insecure context is accepted.
    """
    require_live_provider(execution)
    if (type(connection) is not http.client.HTTPSConnection
            or connection.host != 'osu.ppy.sh' or connection.port != 443
            or connection._tunnel_host is not None
            or connection._context.verify_mode != ssl.CERT_REQUIRED
            or not connection._context.check_hostname
            or connection._context.minimum_version < ssl.TLSVersion.TLSv1_2):
        raise AuthError('fixed_provider_required')
    from .referee_contract import PreparedRefereeRequest
    if type(request) is PreparedRefereeRequest: raise AuthError('invalid_prepared_request')
    return _exchange_checked(connection, request, timeout=timeout, on_status=on_status)


def _exchange_checked(connection, request, *, timeout, on_status):
    """Shared bounded HTTP/JSON parser after an entry-specific origin fence."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 5:
        raise AuthError('invalid_provider_timeout')
    # Revalidate even a manually constructed or mutated request before sending.
    if not isinstance(request, PreparedRequest): raise AuthError('invalid_prepared_request')
    try:
        from .provider_api_contract import PreparedApiRequest, revalidate_api
        from .referee_contract import PreparedRefereeRequest, prepare_referee
        api_request = type(request) is PreparedApiRequest
        if api_request:
            expected = revalidate_api(request)
        elif type(request) is PreparedRefereeRequest:
            expected = prepare_referee(request.headers['Authorization'].removeprefix('Bearer '))
        elif type(request) is not PreparedRequest:
            raise ValueError
        elif request.method == 'POST':
            from urllib.parse import parse_qsl
            pairs = parse_qsl(request.body.decode('ascii'), strict_parsing=True)
            form = dict(pairs)
            if len(form) != len(pairs): raise ValueError
            form['client_id'] = int(form['client_id'])
            expected = prepare(request.method, request.path, form=form)
        else:
            expected = prepare(request.method, request.path,
                               bearer=request.headers['Authorization'].removeprefix('Bearer '))
        if request != expected: raise ValueError
    except Exception: raise AuthError('invalid_prepared_request') from None
    connection.timeout = timeout
    response = None
    try:
        connection.request(request.method, request.path, body=request.body, headers=request.headers)
        response = connection.getresponse()
        if on_status is not None: on_status(response.status, response.headers)
        if 300 <= response.status < 400: raise AuthError('provider_redirect_refused')
        # Discard error bodies: definite 429/401/etc reach the durable auth state
        # machine even if the provider's error page is HTML or contains secrets.
        success = (200, 201, 204) if api_request else (200, 204)
        if response.status not in success: return response.status, None
        lengths = response.headers.get_all('Content-Length', [])
        transfers = response.headers.get_all('Transfer-Encoding', [])
        encodings = response.headers.get_all('Content-Encoding', [])
        if (len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers)
                or (transfers and transfers != ['chunked'])
                or any(not re.fullmatch(r'[0-9]{1,10}', v) for v in lengths)
                or (lengths and int(lengths[0]) > request.maximum_reply)
                or len(encodings) > 1 or (encodings and encodings[0].lower() != 'identity')):
            raise AuthError('provider_framing_refused')
        if response.status == 204:
            empty_allowed = request.method in ('POST', 'PUT') if api_request else request.method == 'DELETE'
            if not empty_allowed or transfers or (lengths and int(lengths[0]) != 0):
                raise AuthError('invalid_provider_reply')
            return 204, None
        if request.method == 'DELETE': raise AuthError('invalid_provider_reply')
        content_types = response.headers.get_all('Content-Type', [])
        if len(content_types) != 1 or not re.fullmatch(
                r'application/json(?:\s*;\s*charset=utf-8)?', content_types[0], re.I):
            raise AuthError('provider_content_type_refused')
        deadline = time.monotonic() + timeout
        chunks = []; count = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise AuthError('provider_reply_deadline')
            if connection.sock is not None: connection.sock.settimeout(remaining)
            chunk = response.read1(min(16384, request.maximum_reply + 1 - count))
            if not chunk: break
            chunks.append(chunk); count += len(chunk)
            if count > request.maximum_reply: raise AuthError('provider_reply_too_large')
        if lengths and count != int(lengths[0]): raise AuthError('provider_reply_truncated')
        def finite(raw):
            value = float(raw)
            if not math.isfinite(value): raise ValueError
            return value
        value = json.loads(b''.join(chunks).decode('utf-8'), object_pairs_hook=unique_fields, parse_float=finite,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, (dict, list)): raise AuthError('invalid_provider_reply')
        return response.status, value
    except AuthError: raise
    except Exception: raise AuthError('provider_transport_uncertain') from None
    finally:
        if response is not None: response.close()
        connection.close()
