"""Preparation only: fixed provider settings and explicit operator declarations.

No secret discovery, network, account verification, token/ledger write, browser,
callback listener or room worker. An operator declaration is not provider evidence.
The prepared transport deliberately cannot be used as an OfflineOAuth fixture.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import math
import time

from .oauth import AuthError, SCOPES
from .operator_client import OperatorClient
from .provider_contract import PROVIDER_ORIGIN, prepare, require_live_provider
from .provider_transport import TrustSnapshot, _Permit


@dataclass(frozen=True)
class ProviderConfiguration:
    request_timeout_seconds: float = 5.
    minimum_interval_seconds: float = 1.2

    def __post_init__(self):
        for value, low, high, code in (
                (self.request_timeout_seconds, 1, 5, 'invalid_provider_timeout'),
                (self.minimum_interval_seconds, 1.2, 60, 'invalid_provider_pacing')):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise AuthError(code)

    @classmethod
    def from_mapping(cls, value):
        if (type(value) is not dict or set(value) != {
                'execution', 'request_timeout_seconds', 'minimum_interval_seconds'}
                or value['execution'] != 'disabled'):
            raise AuthError('invalid_provider_configuration')
        return cls(value['request_timeout_seconds'], value['minimum_interval_seconds'])

    def summary(self):
        self.__post_init__()
        return {'execution': 'disabled', 'origin': PROVIDER_ORIGIN,
                'request_timeout_seconds': self.request_timeout_seconds,
                'minimum_interval_seconds': self.minimum_interval_seconds,
                'automatic_retries': False, 'live_ready': False}


@dataclass(frozen=True)
class OperatorDeclaration:
    client_owner_user_id: int
    registered_redirect_uri: str
    requested_scopes: tuple = SCOPES

    def check(self, profile):
        if type(profile) is not OperatorClient: raise AuthError('invalid_client_input')
        # Revalidate snapshots, including objects bypassing frozen dataclass setters.
        profile.__post_init__(); profile.credentials.__post_init__()
        if (type(self.client_owner_user_id) is not int
                or self.client_owner_user_id != profile.expected_user_id):
            raise AuthError('operator_client_owner_mismatch')
        if (type(self.registered_redirect_uri) is not str
                or self.registered_redirect_uri != profile.redirect_uri):
            raise AuthError('operator_registered_redirect_mismatch')
        if type(self.requested_scopes) is not tuple or self.requested_scopes != SCOPES:
            raise AuthError('operator_requested_scopes_mismatch')


def operator_preflight(profile, declaration, configuration, trust):
    """Redacted local consistency result; never claims verified ownership/scopes."""
    if type(declaration) is not OperatorDeclaration: raise AuthError('invalid_operator_declaration')
    declaration.check(profile)
    if type(configuration) is not ProviderConfiguration: raise AuthError('invalid_provider_configuration')
    if type(trust) is not TrustSnapshot: raise AuthError('provider_trust_refused')
    return {'configuration': configuration.summary(), 'trust': trust.summary(),
            'requested_scopes': list(SCOPES), 'operator_declaration_consistent': True,
            'provider_client_ownership_verified': False, 'provider_identity_verified': False,
            'granted_scopes_verified': False, 'authorization_started': False,
            'start_allowed': False, 'live_ready': False}


class FixedProviderTransport:
    """Dormant adapter with no URL/headers/enable flag; no constructor effects.

    The installed prepared factory supplies a distinct persistent account/profile
    gate. Upstream operations remain release-disabled, and this adapter is never
    accepted as an offline OAuth fixture. Invented TLS qualifies shared machinery.
    """
    offline_fixture = False
    execution = 'provider-disabled'

    def __init__(self, trust, configuration=None, *, gate=None, state_only=False):
        self.configuration = ProviderConfiguration() if configuration is None else configuration
        if type(self.configuration) is not ProviderConfiguration:
            raise AuthError('invalid_provider_configuration')
        self.configuration.__post_init__()
        if type(state_only) is not bool: raise AuthError('invalid_provider_configuration')
        if state_only:
            if trust is not None or gate is not None: raise AuthError('provider_trust_refused')
        else:
            if type(trust) is not TrustSnapshot: raise AuthError('provider_trust_refused')
            trust.context()
        if gate is not None:
            from .provider_pacing import PersistentRequestGate, ProviderRequestGate
            if type(gate) not in (PersistentRequestGate, ProviderRequestGate): raise AuthError('invalid_provider_pacing')
            if (type(gate.interval) not in (int, float) or not math.isfinite(gate.interval)
                    or not self.configuration.minimum_interval_seconds <= gate.interval <= 60):
                raise AuthError('invalid_provider_pacing')
        self.trust, self.gate = trust, gate

    @property
    def timeout(self):
        """Expose the same bounded request deadline as the fixture adapter."""
        return self.configuration.request_timeout_seconds

    @classmethod
    def local_state(cls, configuration=None):
        """Explicit dormant local setup/status; no ambient trust or TLS required."""
        return cls(None, configuration, state_only=True)

    def require_authority(self, root, client_id, *, user_id=None):
        if self.gate is not None:
            from .provider_pacing import ProviderRequestGate
            if type(self.gate) is not ProviderRequestGate or self.gate.execution != self.execution:
                raise AuthError('qualified_live_gate_required')
            self.gate.require_authority(root, client_id, user_id=user_id)

    def bind_verified(self, user_id):
        from .provider_pacing import ProviderRequestGate
        if type(self.gate) is not ProviderRequestGate or self.gate.execution != self.execution:
            raise AuthError('qualified_live_gate_required')
        self.gate.bind_verified(user_id)

    def _qualified_gate(self):
        from .provider_pacing import ProviderRequestGate
        if (type(self.gate) is not ProviderRequestGate or self.gate.execution != self.execution
                or type(self.gate.interval) not in (int, float) or not math.isfinite(self.gate.interval)
                or not self.configuration.minimum_interval_seconds <= self.gate.interval <= 60):
            raise AuthError('qualified_live_gate_required')
        return self.gate

    @contextmanager
    def operation(self, method, path, *, form=None, bearer=None):
        # Fence BEFORE admission counters, durable intent, request preparation,
        # secret parsing, process creation, DNS or provider I/O.
        require_live_provider(self.execution)
        self.configuration.__post_init__()
        if type(self.trust) is not TrustSnapshot: raise AuthError('provider_trust_refused')
        self._qualified_gate()
        deadline = time.monotonic() + self.configuration.request_timeout_seconds
        request = prepare(method, path, form=form, bearer=bearer)
        with self.gate.admission(deadline):
            permit = _Permit(None, self.trust, self.gate, request, deadline)
            try: yield permit
            finally: permit.active = False

    def request(self, method, path, *, form=None, bearer=None):
        with self.operation(method, path, form=form, bearer=bearer) as permit:
            return permit.request(method, path, form=form, bearer=bearer)

    @contextmanager
    def api_operation(self, method, path, *, bearer, params=None, json_body=None):
        # Keep the new API entry fenced before preparation, counters or I/O too.
        require_live_provider(self.execution)
        from .provider_api_contract import prepare_api
        self.configuration.__post_init__()
        if type(self.trust) is not TrustSnapshot: raise AuthError('provider_trust_refused')
        self._qualified_gate()
        deadline = time.monotonic() + self.configuration.request_timeout_seconds
        request = prepare_api(method, path, bearer=bearer, params=params, json_body=json_body)
        with self.gate.admission(deadline):
            permit = _Permit(None, self.trust, self.gate, request, deadline)
            try: yield permit
            finally: permit.active = False


class PilotProviderTransport(FixedProviderTransport):
    """Candidate-only fixed adapter; the same immutable release fence applies.

    Its persisted client/OAuth/pacing/ownership execution differs from both
    disabled preparation and invented TLS qualification. There is no enable flag.
    """
    execution = 'provider-pilot'


class LivePilotTransport(FixedProviderTransport):
    """Fixed origin, one-room release authority; never a fixture adapter."""
    execution = 'provider-live-pilot'
