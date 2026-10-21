"""Operator-local OAuth transactions shared by distinct prepared authorities.

Decoded JWT claims are hints until the provider accepts the SAME bearer at /me.
No signature validation, env-secret loading or enabled upstream execution is
implemented here. See docs/OAUTH.md for the upstream contract assumption.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import hmac
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

from .config import ConfigError, contained
from .layout import account_lease, atomic_json, checked_root, file_lease, load, owner_lease, read_json, unique_fields

SCOPES = ('public', 'identify', 'chat.read', 'chat.write', 'chat.write_manage', 'multiplayer.write_manage')
TOKEN_PHASES = {'unverified', 'ready', 'refresh_pending', 'reauthorization_required', 'revoke_pending', 'revoked'}
PENDING_PHASES = {'waiting', 'exchange_pending', 'verifying', 'complete', 'cancelled', 'failed'}


class AuthError(ConfigError):
    """Stable error codes; never expose server bodies, credentials or callbacks."""
    def __init__(self, code, *, http_status=None):
        self.code = code
        if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
            raise ValueError('invalid diagnostic HTTP status')
        self.http_status = http_status
        suffix = '' if http_status is None else f' [HTTP {http_status}]'
        super().__init__('OAuth: ' + code + suffix)


@contextmanager
def diagnostic_lease(path, *, timeout=5):
    """Shared, bounded read lease on an existing private transaction lock.

    Diagnostics never create locks or acquire the lifetime owner/account lease.
    Writers still hold their existing exclusive OAuth/pacing locks. Refuse a
    replaced lock rather than treating two unrelated inodes as one authority.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 5:
        raise AuthError('invalid_diagnostic_timeout')
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        initial = os.fstat(fd)
        if (not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.geteuid()
                or initial.st_nlink != 1 or stat.S_IMODE(initial.st_mode) & 0o077):
            raise AuthError('diagnostic_lock_requires_review')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AuthError('diagnostic_snapshot_busy') from None
                time.sleep(max(0, min(.02, deadline - time.monotonic())))
        def current():
            now = os.stat(path, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (initial.st_dev, initial.st_ino):
                raise AuthError('diagnostic_lock_changed')
            if (not stat.S_ISREG(now.st_mode) or now.st_uid != os.geteuid()
                    or now.st_nlink != 1 or stat.S_IMODE(now.st_mode) & 0o077):
                raise AuthError('diagnostic_lock_requires_review')
        current()
        yield
        current()
    except AuthError:
        raise
    except Exception:
        raise AuthError('diagnostic_lock_requires_review') from None
    finally:
        if fd is not None: os.close(fd)


def _secret(value, maximum=16384):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not re.fullmatch(r'[!-~]+', value):
        raise AuthError('invalid_secret_format')
    return value


def _positive(value):
    if type(value) is not int or value <= 0:
        raise AuthError('invalid_identity')
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise AuthError('invalid_time')
    return value


def redirect_parts(uri):
    try:
        parts = urlsplit(uri)
        port = parts.port
    except (ValueError, TypeError):
        raise AuthError('invalid_loopback_redirect') from None
    if (parts.scheme != 'http' or parts.hostname != '127.0.0.1' or not port
            or parts.username is not None or parts.password is not None or parts.query or parts.fragment
            or parts.netloc != f'127.0.0.1:{port}'
            or not re.fullmatch(r'/[A-Za-z0-9_/-]+', parts.path) or '//' in parts.path
            or any(p in {'.', '..'} for p in parts.path.split('/'))):
        raise AuthError('invalid_loopback_redirect')
    return parts


def claims_hint(token):
    """Bounded parsing only. A hint cannot itself authenticate anything."""
    try:
        pieces = _secret(token).split('.')
        if len(pieces) != 3 or not all(re.fullmatch(r'[A-Za-z0-9_-]+', p) for p in pieces):
            raise ValueError
        payload = base64.b64decode(pieces[1] + '=' * (-len(pieces[1]) % 4), altchars=b'-_', validate=True)
        claims = json.loads(payload, object_pairs_hook=unique_fields,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(claims, dict): raise ValueError
        sub = claims['sub']
        if not isinstance(sub, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', sub): raise ValueError
        scopes = claims['scopes']
        if not isinstance(scopes, list) or len(scopes) > 32 or not all(isinstance(s, str) and len(s) <= 80 for s in scopes):
            raise ValueError
        aud = claims['aud']
        if isinstance(aud, str): aud = [aud]
        if not isinstance(aud, list) or len(aud) != 1 or not isinstance(aud[0], str): raise ValueError
        return {'user_id': int(sub), 'client_id': aud[0], 'scopes': scopes, 'exp': _number(claims['exp'])}
    except Exception:
        raise AuthError('unsupported_token_claims') from None


@dataclass(frozen=True)
class ClientCredentials:
    client_id: int
    client_secret: str = field(repr=False)

    def __post_init__(self):
        _positive(self.client_id); _secret(self.client_secret, 8192)


@dataclass(frozen=True)
class VerifiedAccount:
    user_id: int
    username: str
    scopes: tuple[str, ...]


class OAuthAuthority:
    """Single installation authority, durable transactions and a lifetime owner.

    Access tokens can be obtained only while verified_owner is held. Refreshes
    serialize across request threads and reread the persisted token every time.
    """
    execution = 'offline-fixture'
    authority_prefix = ''

    def _require_transport(self, transport):
        raise AuthError('invalid_authority_type')

    def require_authorization_available(self):
        """Release permission check, called before listener or durable intent."""

    def __init__(self, root, credentials, transport, *, clock=time.time, local_guard=None,
                 expected_user_id=None, redirect_uri=None, client_profile_sha256=None):
        self._require_transport(transport)
        if not isinstance(credentials, ClientCredentials): raise AuthError('invalid_credentials')
        if local_guard is not None and not callable(local_guard): raise AuthError('invalid_local_guard')
        if expected_user_id is not None: _positive(expected_user_id)
        if redirect_uri is not None: redirect_parts(redirect_uri)
        if client_profile_sha256 is not None and (not isinstance(client_profile_sha256, str)
                or not re.fullmatch('[a-f0-9]{64}', client_profile_sha256)):
            raise AuthError('invalid_client_profile_binding')
        self._expected_user_id = expected_user_id
        self._redirect_uri = redirect_uri
        self.root = checked_root(Path(root))
        self.credentials = credentials
        self.transport = transport
        self.clock = clock
        self._local_guard = local_guard
        marker, _ = load(self.root)
        self.binding = {'format': 1, 'execution': self.execution, **{k: marker[k] for k in ('installation_id', 'generation')},
                        'client_id': credentials.client_id}
        if client_profile_sha256 is not None: self.binding['client_profile_sha256'] = client_profile_sha256
        prefix = self.authority_prefix
        self.token_path = contained(self.root, f'secrets/{prefix}osu-token.json')
        self.pending_path = contained(self.root, f'secrets/{prefix}oauth-pending.json')
        self.candidate_path = contained(self.root, f'secrets/{prefix}oauth-candidate.json')
        self.account_path = contained(self.root, f'state/{prefix}oauth-account.json')
        self.lock_path = contained(self.root, f'secrets/{prefix}oauth.lock')
        self._mutex = threading.RLock()
        self._active = None
        self._verified = False
        self._accepted = None
        self._rejected = set()
        if hasattr(transport, 'require_authority'):
            transport.require_authority(self.root, credentials.client_id, user_id=self._account())

    def _current(self):
        marker, config = load(self.root)
        if any(marker[k] != self.binding[k] for k in ('installation_id', 'generation')):
            raise AuthError('installation_changed')
        return config

    def _read(self, path, kind, *, optional=False):
        self._current()
        try: record = read_json(path)
        except FileNotFoundError:
            if optional: return None
            raise AuthError('credentials_missing') from None
        except Exception: raise AuthError('unsafe_or_invalid_local_state') from None
        base = set(self.binding)
        fields = {'account': {'user_id'}, 'token': {'access_token', 'refresh_token', 'expires_at', 'user_id', 'phase', 'attempt'},
                  'pending': {'state', 'redirect_uri', 'issued_at', 'expires_at', 'phase', 'attempt'}}[kind]
        if not isinstance(record, dict) or set(record) != base | fields or any(record[k] != v or type(record[k]) is not type(v) for k, v in self.binding.items()):
            raise AuthError('local_binding_mismatch')
        if kind == 'account': _positive(record['user_id'])
        elif kind == 'token':
            _secret(record['access_token']); _secret(record['refresh_token'], 8192)
            _number(record['expires_at']); _secret(record['attempt'], 128)
            if record['user_id'] is not None: _positive(record['user_id'])
            if not isinstance(record['phase'], str) or record['phase'] not in TOKEN_PHASES: raise AuthError('invalid_token_phase')
        else:
            _secret(record['state'], 128); _secret(record['attempt'], 128); redirect_parts(record['redirect_uri'])
            if self._redirect_uri is not None and record['redirect_uri'] != self._redirect_uri:
                raise AuthError('client_redirect_mismatch')
            _number(record['issued_at']); _number(record['expires_at'])
            if not isinstance(record['phase'], str) or record['phase'] not in PENDING_PHASES or not 0 < record['expires_at'] - record['issued_at'] <= 600:
                raise AuthError('invalid_authorization_state')
        return record

    def _write(self, path, record):
        # Validate any destination before atomic replacement; never repair bad state silently.
        kind = 'token' if path in (self.token_path, self.candidate_path) else 'pending' if path == self.pending_path else 'account'
        self._read(path, kind, optional=True)
        try: atomic_json(path, record)
        except Exception: raise AuthError('local_persistence_failed') from None

    def _account(self):
        config = self._current()
        record = self._read(self.account_path, 'account', optional=True)
        expected = config['account']['expected_user_id']
        if self._expected_user_id is not None:
            if expected is not None and expected != self._expected_user_id:
                raise AuthError('expected_account_mismatch')
            expected = self._expected_user_id
        if record and expected is not None and record['user_id'] != expected:
            raise AuthError('expected_account_mismatch')
        return record['user_id'] if record else expected

    @contextmanager
    def _transaction(self):
        with self._mutex:
            if self._active is not None: raise AuthError('account_in_use')
            with owner_lease(self.root, account_id=self._account()), file_lease(self.lock_path, timeout=5):
                if self._local_guard is not None: self._local_guard()
                yield

    def _admission(self, method, path, *, form=None, token=None):
        if hasattr(self.transport, 'operation'):
            return self.transport.operation(method, path, form=form, bearer=token)
        return nullcontext(self.transport)

    def _request(self, method, path, *, form=None, token=None, transport=None):
        try: result = (transport or self.transport).request(method, path, form=form, bearer=token)
        except Exception: raise AuthError('transport_reply_uncertain') from None
        if not isinstance(result, tuple) or len(result) != 2 or type(result[0]) is not int:
            raise AuthError('invalid_provider_reply')
        return result

    def begin(self, redirect_uri, *, reauthorize=False, replace_pending=True):
        self.require_authorization_available()
        redirect_parts(redirect_uri)
        if self._redirect_uri is not None and redirect_uri != self._redirect_uri:
            raise AuthError('client_redirect_mismatch')
        if type(reauthorize) is not bool: raise AuthError('invalid_reauthorization_choice')
        if type(replace_pending) is not bool: raise AuthError('invalid_replacement_choice')
        with self._transaction():
            current = self._read(self.token_path, 'token', optional=True)
            self._read(self.candidate_path, 'token', optional=True)
            if current and not reauthorize: raise AuthError('explicit_reauthorization_required')
            previous = self._read(self.pending_path, 'pending', optional=True)
            if previous and previous['phase'] in {'waiting', 'exchange_pending', 'verifying'} and not replace_pending:
                raise AuthError('explicit_attempt_replacement_required')
            now = _number(self.clock())
            pending = {**self.binding, 'state': secrets.token_urlsafe(32), 'attempt': secrets.token_hex(16),
                       'redirect_uri': redirect_uri, 'issued_at': now, 'expires_at': now + 600, 'phase': 'waiting'}
            self._write(self.pending_path, pending)
            return 'https://osu.ppy.sh/oauth/authorize?' + urlencode({'client_id': self.credentials.client_id,
                    'redirect_uri': redirect_uri, 'response_type': 'code', 'scope': ' '.join(SCOPES), 'state': pending['state']})

    def cancel(self, *, expected_state=None):
        with self._transaction():
            pending = self._read(self.pending_path, 'pending')
            if expected_state is not None and not hmac.compare_digest(_secret(expected_state, 128), pending['state']):
                raise AuthError('authorization_attempt_changed')
            if pending['phase'] == 'waiting':
                pending['phase'] = 'cancelled'; self._write(self.pending_path, pending)

    def authorization_status(self):
        """Local phases only: no HTTP, token values, callbacks or account names."""
        with self._transaction():
            return self._authorization_phases()

    def _authorization_phases(self):
        """Checked fields only; caller must hold an OAuth transaction/read lease."""
        pending = self._read(self.pending_path, 'pending', optional=True)
        token = self._read(self.token_path, 'token', optional=True)
        candidate = self._read(self.candidate_path, 'token', optional=True)
        recoverable = bool(pending and pending['phase'] in {'exchange_pending', 'verifying'}
                           and candidate and candidate['attempt'] == pending['attempt'])
        return {'execution': self.execution, 'live_ready': False,
                'pending_phase': pending['phase'] if pending else 'absent',
                'token_phase': token['phase'] if token else 'absent',
                'candidate_recovery_available': recoverable,
                'callback_expired': bool(pending and pending['phase'] == 'waiting'
                                         and not pending['issued_at'] <= self.clock() < pending['expires_at'])}

    def callback_retry_available(self, expected_state=None):
        with self._transaction():
            pending = self._read(self.pending_path, 'pending')
            return (pending['phase'] == 'waiting' and pending['issued_at'] <= self.clock() < pending['expires_at']
                    and (expected_state is None or hmac.compare_digest(_secret(expected_state, 128), pending['state'])))

    def complete(self, target, host, *, expected_state=None):
        """Validate a callback once, consume its state durably, then exchange."""
        self.require_authorization_available()
        with self._transaction():
            pending = self._read(self.pending_path, 'pending')
            if expected_state is not None and not hmac.compare_digest(_secret(expected_state, 128), pending['state']):
                raise AuthError('authorization_attempt_changed')
            if pending['phase'] != 'waiting': raise AuthError('callback_already_consumed')
            if not pending['issued_at'] <= self.clock() < pending['expires_at']:
                pending['phase'] = 'cancelled'; self._write(self.pending_path, pending)
                raise AuthError('authorization_expired')
            expected = redirect_parts(pending['redirect_uri'])
            try:
                if not isinstance(target, str) or len(target) > 8192: raise ValueError
                parts = urlsplit(target)
                pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=5)
                values = dict(pairs)
                if (parts.scheme or parts.netloc or parts.fragment or parts.path != expected.path or host != expected.netloc
                        or len(values) != len(pairs) or set(values) - {'code', 'state', 'error', 'error_description', 'error_uri'}
                        or ('code' in values) == ('error' in values)):
                    raise ValueError
                state = _secret(values.get('state'), 128)
                if not hmac.compare_digest(state, pending['state']): raise ValueError
                if 'code' in values: _secret(values['code'], 4096)
            except Exception: raise AuthError('invalid_callback') from None
            # Detect a broken destination before burning a single-use code.
            self._read(self.token_path, 'token', optional=True)
            self._read(self.candidate_path, 'token', optional=True)
            if 'error' in values:
                pending['phase'] = 'failed'; self._write(self.pending_path, pending)
                raise AuthError('authorization_denied')
            form = {'client_id': self.credentials.client_id,
                    'client_secret': self.credentials.client_secret, 'code': values['code'],
                    'grant_type': 'authorization_code', 'redirect_uri': pending['redirect_uri']}
            with self._admission('POST', '/oauth/token', form=form) as permit:
                if not pending['issued_at'] <= self.clock() < pending['expires_at']:
                    pending['phase'] = 'cancelled'; self._write(self.pending_path, pending)
                    raise AuthError('authorization_expired')
                if hasattr(permit, 'check'): permit.check()
                pending['phase'] = 'exchange_pending'; self._write(self.pending_path, pending)
                status, body = self._request('POST', '/oauth/token', form=form, transport=permit)
            if status != 200: raise AuthError('authorization_exchange_failed')
            candidate = self._response(body, pending['attempt'], self._account())
            self._write(self.candidate_path, candidate)
            pending['phase'] = 'verifying'; self._write(self.pending_path, pending)
            return self._promote(pending, candidate)

    def _response(self, body, attempt, user_id):
        if not isinstance(body, dict) or body.get('token_type') != 'Bearer': raise AuthError('invalid_token_response')
        expires = body.get('expires_in')
        if type(expires) is not int or not 0 < expires <= 366 * 86400: raise AuthError('invalid_token_response')
        try:
            access = _secret(body.get('access_token')); refresh = _secret(body.get('refresh_token'), 8192)
        except AuthError: raise AuthError('invalid_token_response') from None
        return {**self.binding, 'access_token': access, 'refresh_token': refresh, 'expires_at': self.clock() + expires,
                'user_id': user_id, 'phase': 'unverified', 'attempt': attempt}

    def _verify(self, record):
        hint = claims_hint(record['access_token'])
        expected = self._account()
        if hint['client_id'] != str(self.credentials.client_id): raise AuthError('token_client_mismatch')
        if any(uid is not None and uid != hint['user_id'] for uid in (expected, record['user_id'], self._active)):
            raise AuthError('expected_account_mismatch')
        if not set(SCOPES) <= set(hint['scopes']): raise AuthError('required_scopes_missing')
        if min(record['expires_at'], hint['exp']) <= self.clock(): raise AuthError('token_expired')
        status, body = self._request('GET', '/api/v2/me', token=record['access_token'])
        if status == 401: raise AuthError('access_token_rejected')
        if status == 403: raise AuthError('account_permission_denied')
        if status != 200: raise AuthError('identity_temporarily_unavailable')
        if (not isinstance(body, dict) or type(body.get('id')) is not int or body['id'] != hint['user_id']
                or not isinstance(body.get('username'), str) or not 1 <= len(body['username']) <= 128
                or type(body.get('is_restricted')) is not bool or body['is_restricted']):
            raise AuthError('provider_identity_mismatch_or_restricted')
        if hasattr(self.transport, 'bind_verified'): self.transport.bind_verified(body['id'])
        record.update(user_id=body['id'], phase='ready', expires_at=min(record['expires_at'], hint['exp']))
        return VerifiedAccount(body['id'], body['username'], tuple(hint['scopes']))

    def _promote(self, pending, candidate):
        if candidate['attempt'] != pending['attempt']: raise AuthError('authorization_attempt_mismatch')
        hint = claims_hint(candidate['access_token'])
        with account_lease(hint['user_id']) if self._account() is None else nullcontext():
            account = self._verify(candidate)
            self._write(self.candidate_path, candidate)
            self._write(self.account_path, {**self.binding, 'user_id': account.user_id})
            self._write(self.token_path, candidate)
            pending['phase'] = 'complete'; self._write(self.pending_path, pending)
            return account

    def recover_authorization(self):
        self.require_authorization_available()
        with self._transaction():
            pending = self._read(self.pending_path, 'pending')
            if pending['phase'] not in {'exchange_pending', 'verifying'}: raise AuthError('authorization_recovery_unavailable')
            candidate = self._read(self.candidate_path, 'token', optional=True)
            if not candidate or candidate['attempt'] != pending['attempt']:
                raise AuthError('authorization_reply_lost_reauthorize')
            return self._promote(pending, candidate)

    def _refresh(self, record):
        if record['phase'] not in {'ready', 'unverified'}: raise AuthError('credential_recovery_required')
        previous = record['phase']
        form = {'client_id': self.credentials.client_id, 'client_secret': self.credentials.client_secret,
                'grant_type': 'refresh_token', 'refresh_token': record['refresh_token']}
        with self._admission('POST', '/oauth/token', form=form) as permit:
            if hasattr(permit, 'check'): permit.check()
            record['phase'] = 'refresh_pending'; self._write(self.token_path, record)
            status, body = self._request('POST', '/oauth/token', form=form, transport=permit)
        if status != 200:
            if status in {400, 401, 403, 429}:
                record['phase'] = previous if status == 429 else 'reauthorization_required'
                self._write(self.token_path, record)
            raise AuthError('refresh_rate_limited' if status == 429 else 'refresh_failed_recovery_required')
        fresh = self._response(body, secrets.token_hex(16), record['user_id'])
        self._write(self.token_path, fresh)  # Persist rotated refresh BEFORE verification/use.
        return fresh

    def _ensure(self, *, verify=False):
        record = self._read(self.token_path, 'token')
        if record['phase'] not in {'ready', 'unverified'}: raise AuthError('credential_recovery_required')
        if record['user_id'] != self._active: raise AuthError('expected_account_mismatch')
        hint = claims_hint(record['access_token'])
        refreshed = False
        if min(record['expires_at'], hint['exp']) <= self.clock() + 30 or record['access_token'] in self._rejected:
            record = self._refresh(record)
            refreshed = True
        if verify or record['phase'] == 'unverified' or record['access_token'] != self._accepted:
            try: account = self._verify(record)
            except AuthError as exc:
                if exc.code != 'access_token_rejected': raise
                if refreshed:
                    record['phase'] = 'reauthorization_required'; self._write(self.token_path, record)
                    raise
                # One rejected bearer may be refreshed; a second rejection is terminal.
                record = self._refresh(record)
                try: account = self._verify(record)
                except AuthError as second:
                    if second.code == 'access_token_rejected':
                        record['phase'] = 'reauthorization_required'; self._write(self.token_path, record)
                    raise
            self._write(self.token_path, record)
            self._accepted = record['access_token']
        else:
            if hint['client_id'] != str(self.credentials.client_id) or hint['user_id'] != self._active or not set(SCOPES) <= set(hint['scopes']):
                raise AuthError('persisted_token_authority_mismatch')
            account = VerifiedAccount(record['user_id'], '', tuple(hint['scopes']))
        return record['access_token'], account

    def _prepare_owner_verification(self):
        """Local-only startup seam, under the owner and OAuth leases."""

    @contextmanager
    def verified_owner(self):
        self.require_authorization_available()
        with self._mutex:
            if self._active is not None: raise AuthError('account_in_use')
            uid = self._account()
            if uid is None: raise AuthError('verified_account_binding_missing')
        with owner_lease(self.root, account_id=uid):
            try:
                with self._mutex, file_lease(self.lock_path, timeout=5):
                    if self._local_guard is not None: self._local_guard()
                    pending = self._read(self.pending_path, 'pending', optional=True)
                    if pending and pending['phase'] in {'waiting', 'exchange_pending', 'verifying'}:
                        # An older ready token (or partially promoted new token)
                        # must not bypass explicit authorization recovery.
                        raise AuthError('unfinished_authorization_requires_resolution')
                    self._prepare_owner_verification()
                    self._active = uid
                    _, account = self._ensure(verify=True)
                    self._verified = True
                yield account
            finally:
                with self._mutex:
                    self._active = None
                    self._verified = False

    def access_token(self):
        self.require_authorization_available()
        with self._mutex:
            if not self._verified: raise AuthError('verified_owner_required')
            with file_lease(self.lock_path, timeout=5): return self._ensure()[0]

    def invalidate_access_token(self, rejected):
        with self._mutex:
            if not self._verified: raise AuthError('verified_owner_required')
            self._rejected = {_secret(rejected)}

    def revoke(self):
        self.require_authorization_available()
        with self._transaction():
            record = self._read(self.token_path, 'token')
            if record['phase'] == 'revoked': return
            if record['phase'] not in {'ready', 'unverified', 'reauthorization_required'}:
                raise AuthError('credential_recovery_required')
            with self._admission('DELETE', '/api/v2/oauth/tokens/current', token=record['access_token']) as permit:
                if hasattr(permit, 'check'): permit.check()
                record['phase'] = 'revoke_pending'; self._write(self.token_path, record)
                status, _ = self._request('DELETE', '/api/v2/oauth/tokens/current', token=record['access_token'], transport=permit)
            # Only 204 proves recursive provider revocation. 401 cannot prove the refresh is revoked.
            if status != 204: raise AuthError('revocation_unconfirmed')
            record['phase'] = 'revoked'; self._write(self.token_path, record)


class OfflineOAuth(OAuthAuthority):
    """Original isolated fixture authority; provider authority is a sibling."""

    def _require_transport(self, transport):
        if (os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1'
                or getattr(transport, 'offline_fixture', False) is not True
                or getattr(getattr(transport, 'gate', None), 'execution', 'offline-fixture') != 'offline-fixture'):
            raise AuthError('offline_fixture_required')
