"""Prepared provider authority, separate from legacy offline authority.

Fixed upstream execution stays release-disabled. Invented provider qualification
uses a different execution binding in the same prepared namespace, and cannot be
reconstructed as fixed upstream authority. This class is never OfflineOAuth.
"""
import os

from .oauth import AuthError, OAuthAuthority
from .provider_contract import require_live_provider
from .provider_pacing import ProviderRequestGate
from .provider_preflight import FixedProviderTransport, PilotProviderTransport, LivePilotTransport
from .provider_transport import DeadlineFixtureTransport


class ProviderOAuth(OAuthAuthority):
    authority_prefix = 'provider-'

    def __init__(self, root, credentials, transport, *, expected_user_id=None,
                 redirect_uri=None, client_profile_sha256=None, **kwargs):
        if expected_user_id is None or redirect_uri is None or client_profile_sha256 is None:
            raise AuthError('provider_client_binding_required')
        super().__init__(root, credentials, transport, expected_user_id=expected_user_id,
                         redirect_uri=redirect_uri, client_profile_sha256=client_profile_sha256, **kwargs)

    def _require_transport(self, transport):
        if type(transport) in (FixedProviderTransport, PilotProviderTransport, LivePilotTransport):
            self.execution = transport.execution
        elif type(transport) is DeadlineFixtureTransport:
            if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1':
                raise AuthError('installed_offline_fixture_required')
            self.execution = 'provider-fixture'
        else:
            raise AuthError('qualified_provider_transport_required')

    def require_authorization_available(self):
        # Exact transport classification and persisted execution cannot be changed
        # by swapping a flag or replacing the transport after construction.
        if self.execution in ('provider-disabled', 'provider-pilot', 'provider-live-pilot'):
            require_live_provider(self.execution)
            if (type(self.transport) not in (FixedProviderTransport, PilotProviderTransport, LivePilotTransport)
                    or self.transport.execution != self.execution):
                raise AuthError('qualified_provider_transport_required')
        elif (self.execution != 'provider-fixture'
                or type(self.transport) is not DeadlineFixtureTransport
                or os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1'):
            raise AuthError('qualified_provider_transport_required')
        if self.binding['execution'] != self.execution:
            raise AuthError('qualified_provider_transport_required')
        self._require_gate()

    def _require_gate(self):
        gate = self.transport.gate
        if (type(gate) is not ProviderRequestGate or gate.execution != self.execution
                or gate.binding.get('execution') != self.binding['execution']
                or gate.binding.get('client_profile_sha256') != self.binding.get('client_profile_sha256')):
            raise AuthError('qualified_live_gate_required')
        gate.require_authority(self.root, self.credentials.client_id, user_id=self._account())
        return gate

    def authorization_status(self):
        result = super().authorization_status()
        return {**result, 'start_allowed': False, 'actual_provider_dispatch': False,
                'live_capable': self.execution == 'provider-live-pilot'}

    def pacing_snapshot(self):
        with self._transaction():
            return self._require_gate().snapshot()

    def recover_pacing(self, *, expected_state_sha256):
        # Local stopped ledger recovery remains possible with upstream disabled.
        with self._transaction():
            gate = self._require_gate()
            initial = gate._read()
            return gate._recover_locked(initial, expected_state_sha256)
