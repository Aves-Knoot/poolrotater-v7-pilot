"""Private isolated/prepared client setup; explicit inputs, never discovered.

No ambient environment, credential path, terminal, provider or network is read.
The first profile is write-once and bound to this host generation. Changing it or
adopting pre-existing authority needs a later explicit migration contract.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import re

from .config import contained, validate
from .layout import atomic_json, file_lease, load, owner_lease, read_json, unique_fields
from .oauth import AuthError, ClientCredentials, redirect_parts

PROFILE_PATH = 'secrets/operator-client.json'
PROVIDER_PROFILE_PATH = 'secrets/provider-operator-client.json'
MAX_INPUT = 16384
MAX_REDIRECT = 2048
AUTHORITY_PATHS = ('secrets/osu-token.json', 'secrets/oauth-pending.json',
                   'secrets/oauth-candidate.json', 'state/oauth-account.json',
                   'state/provider-pacing.json', 'state/provider-pacing-anchor.json',
                   PROVIDER_PROFILE_PATH, 'secrets/provider-osu-token.json',
                   'secrets/provider-oauth-pending.json', 'secrets/provider-oauth-candidate.json',
                   'state/provider-oauth-account.json', 'state/provider-authority-pacing.json',
                   'state/provider-authority-pacing-anchor.json',
                   'state/rolling/ownership.json', 'state/ownership-recovery.json',
                   'state/provider-room-binding.json')


def _client_id(value):
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,18}', value):
        value = int(value)
    if type(value) is not int or not 0 < value < 10**19:
        raise AuthError('invalid_client_input')
    return value


def credentials_from_environment(config, environment):
    """Read ONLY the reserved names from a caller-supplied mapping.

    The caller chooses a source. This helper never reads os.environ or returns
    raw input in errors. Command arguments and shell histories are not inputs.
    """
    try:
        config = validate(config)
        if not isinstance(environment, Mapping): raise ValueError
        account = config['account']
        return ClientCredentials(_client_id(environment[account['client_id_env']]),
                                 environment[account['client_secret_env']])
    except Exception:
        raise AuthError('invalid_client_input') from None


def credentials_from_stream(stream):
    """Bounded JSON from an explicitly supplied binary/text stream; never stdin.

    Exactly client_id and client_secret are accepted. The caller owns the stream
    and its privacy; no pathname is followed and the stream is never closed here.
    """
    try:
        raw = stream.read(MAX_INPUT + 1)
        if not isinstance(raw, (str, bytes)) or len(raw) > MAX_INPUT: raise ValueError
        if isinstance(raw, bytes): raw = raw.decode('utf-8')
        if len(raw.encode('utf-8')) > MAX_INPUT: raise ValueError
        value = json.loads(raw, object_pairs_hook=unique_fields,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or set(value) != {'client_id', 'client_secret'}: raise ValueError
        return ClientCredentials(_client_id(value['client_id']), value['client_secret'])
    except Exception:
        raise AuthError('invalid_client_input') from None


@dataclass(frozen=True)
class OperatorClient:
    credentials: ClientCredentials
    expected_user_id: int
    redirect_uri: str

    def __post_init__(self):
        if not isinstance(self.credentials, ClientCredentials): raise AuthError('invalid_client_input')
        _client_id(self.credentials.client_id)
        if type(self.expected_user_id) is not int or not 0 < self.expected_user_id < 10**19:
            raise AuthError('invalid_expected_account')
        if not isinstance(self.redirect_uri, str) or len(self.redirect_uri) > MAX_REDIRECT:
            raise AuthError('invalid_loopback_redirect')
        redirect_parts(self.redirect_uri)


def _offline():
    if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1':
        raise AuthError('offline_fixture_required')


def _binding(root, *, execution='offline-fixture'):
    try: marker, config = load(root)
    except Exception: raise AuthError('operator_client_layout_requires_review') from None
    return {'format': 1, 'execution': execution,
            'installation_id': marker['installation_id'], 'generation': marker['generation'],
            'client_id_env': config['account']['client_id_env'],
            'client_secret_env': config['account']['client_secret_env']}, config


def _record(binding, profile):
    record = {**binding, 'client_id': profile.credentials.client_id,
              'client_secret': profile.credentials.client_secret,
              'expected_user_id': profile.expected_user_id, 'redirect_uri': profile.redirect_uri}
    if len((json.dumps(record, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()) > MAX_INPUT:
        raise AuthError('operator_client_input_too_large')
    return record


def _read(root, *, optional=False, execution='offline-fixture', profile_path=PROFILE_PATH):
    binding, config = _binding(root, execution=execution)
    try:
        record = read_json(contained(root, profile_path), maximum=MAX_INPUT)
    except FileNotFoundError:
        if optional: return None
        raise AuthError('operator_client_missing') from None
    except Exception:
        raise AuthError('unsafe_operator_client_state') from None
    try:
        fields = set(binding) | {'client_id', 'client_secret', 'expected_user_id', 'redirect_uri'}
        if not isinstance(record, dict) or set(record) != fields: raise ValueError
        if any(type(record[k]) is not type(v) or record[k] != v for k, v in binding.items()): raise ValueError
        profile = OperatorClient(ClientCredentials(record['client_id'], record['client_secret']),
                                 record['expected_user_id'], record['redirect_uri'])
        expected = config['account']['expected_user_id']
        if expected is not None and expected != profile.expected_user_id: raise ValueError
        return profile
    except Exception:
        raise AuthError('operator_client_binding_requires_review') from None


def load_operator_client(root):
    _offline()
    return _read(root)


def same_client(left, right):
    """Compare private snapshots without emitting them or early secret equality."""
    return (left.credentials.client_id == right.credentials.client_id
            and hmac.compare_digest(left.credentials.client_secret, right.credentials.client_secret)
            and left.expected_user_id == right.expected_user_id
            and left.redirect_uri == right.redirect_uri)


def operator_client_fingerprint(root, profile, *, execution='offline-fixture'):
    """Private binding used by authority records; never a diagnostics field."""
    binding, _ = _binding(root, execution=execution)
    return hashlib.sha256(json.dumps(_record(binding, profile), sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _configure_client(root, profile, *, local_guard=None, execution='offline-fixture',
                      profile_path=PROFILE_PATH, lock_path='secrets/oauth.lock'):
    """Stopped, serialized, idempotent enrollment. No implicit repair or rotation."""
    if execution not in ('provider-disabled', 'provider-pilot', 'provider-live-pilot'): _offline()
    if not isinstance(profile, OperatorClient): raise AuthError('invalid_client_input')
    if local_guard is not None and not callable(local_guard): raise AuthError('invalid_local_guard')
    # Validate layout before constructing paths or creating locks.
    _, config = _binding(root, execution=execution)
    expected = config['account']['expected_user_id']
    if expected is not None and expected != profile.expected_user_id:
        raise AuthError('expected_account_mismatch')
    with owner_lease(root, account_id=profile.expected_user_id), file_lease(
            contained(root, lock_path), timeout=5):
        if local_guard is not None: local_guard()
        binding, config = _binding(root, execution=execution)
        expected = config['account']['expected_user_id']
        if expected is not None and expected != profile.expected_user_id:
            raise AuthError('expected_account_mismatch')
        current = _read(root, optional=True, execution=execution, profile_path=profile_path)
        if current is not None:
            if not same_client(current, profile):
                raise AuthError('operator_client_change_requires_migration')
            return current
        # lexists includes dangling links and other unsafe legacy authority.
        authority_paths = AUTHORITY_PATHS if execution == 'offline-fixture' else (PROFILE_PATH, *AUTHORITY_PATHS)
        if any(os.path.lexists(contained(root, name)) for name in authority_paths):
            raise AuthError('unbound_authority_requires_migration')
        record = _record(binding, profile)
        try: atomic_json(contained(root, profile_path), record)
        except Exception: raise AuthError('operator_client_persistence_failed') from None
        return profile


def configure_operator_client(root, profile, *, local_guard=None):
    return _configure_client(root, profile, local_guard=local_guard)


def _configure_provider_client(root, profile, *, local_guard=None, execution='provider-disabled'):
    if execution not in ('provider-disabled', 'provider-fixture', 'provider-pilot', 'provider-live-pilot'):
        raise AuthError('invalid_provider_execution')
    return _configure_client(root, profile, local_guard=local_guard, execution=execution,
                             profile_path=PROVIDER_PROFILE_PATH, lock_path='secrets/provider-oauth.lock')


def _load_provider_client(root, *, execution='provider-disabled'):
    if execution not in ('provider-disabled', 'provider-pilot', 'provider-live-pilot'): _offline()
    if execution not in ('provider-disabled', 'provider-fixture', 'provider-pilot', 'provider-live-pilot'):
        raise AuthError('invalid_provider_execution')
    return _read(root, execution=execution, profile_path=PROVIDER_PROFILE_PATH)


def load_provider_client(root):
    return _load_provider_client(root)


def provider_client_fingerprint(root, profile, *, execution='provider-disabled'):
    if execution not in ('provider-disabled', 'provider-fixture', 'provider-pilot', 'provider-live-pilot'):
        raise AuthError('invalid_provider_execution')
    return operator_client_fingerprint(root, profile, execution=execution)


def provider_client_diagnostic(root):
    load_provider_client(root)
    return {'operator_client_configured': True, 'execution': 'provider-disabled',
            'credential_rotation_available': False, 'live_ready': False, 'start_allowed': False}


def operator_client_diagnostic(root):
    """A redacted presence check, never credentials, account IDs or callback URIs."""
    load_operator_client(root)
    return {'operator_client_configured': True, 'execution': 'offline-fixture',
            'credential_rotation_available': False, 'live_ready': False}
