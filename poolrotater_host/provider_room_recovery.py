"""Provider-bound stopped ownership review and existing durable recovery engine."""
from .provider_rooms import _authority
from .provider_room_state import binding, require_namespace
from .reconciliation import OfflineOwnershipReview
from .ownership_recovery import OfflineOwnershipRecovery


class ProviderOwnershipReview(OfflineOwnershipReview):
    def __init__(self, authentication):
        _authority(authentication)
        self.auth = authentication
        self.root = authentication.root
        self.execution = authentication.execution
        require_namespace(self.root, binding(self.root, self.auth.binding, self.auth._account()))

    def _inspect(self, account, *, journal=None):
        guard = getattr(self.auth, '_room_guard', self.auth._local_guard)
        if guard is not None: guard()
        result = super()._inspect(account, journal=journal)
        if guard is not None: guard()
        return result

    def _read_journal(self, aliases):
        # Only a completely untouched provider namespace is an absent journal.
        # Missing pieces of a previous creation or recovery remain refused.
        if not require_namespace(self.root, binding(self.root, self.auth.binding, self.auth._account())):
            return {'format': 1, 'rooms': {}}
        return super()._read_journal(aliases)


class ProviderOwnershipRecovery(OfflineOwnershipRecovery, ProviderOwnershipReview):
    def __init__(self, authentication):
        ProviderOwnershipReview.__init__(self, authentication)

    def _binding(self, account):
        guard = getattr(self.auth, '_room_guard', self.auth._local_guard)
        if guard is not None: guard()
        return {**super()._binding(account), 'execution': self.execution,
                'client_profile_sha256': self.auth.binding['client_profile_sha256']}
