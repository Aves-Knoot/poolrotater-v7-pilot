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

    def inventory(self):
        """Explicit stopped discovery, never approval to clear or create.

        Two complete history traversals must agree while authority, local
        configuration and the ownership journal remain unchanged. Deleted API
        rows are outside this scope, so even an empty result grants no absence
        recovery authority.
        """
        import time
        from .layout import load
        from .ownership_recovery import digest
        from .owned_inventory import collect, MAX_SECONDS
        from .oauth import AuthError
        guard = getattr(self.auth, '_room_guard', self.auth._local_guard)
        with self.auth.verified_owner() as account:
            if guard is not None:guard()
            marker,config=load(self.root)
            aliases={room['alias'] for room in config['rooms']}
            before=self._read_journal(aliases)
            journal_digest=digest(before)
            binding={**{k:marker[k] for k in ('installation_id','generation')},
                     'client_id':self.auth.binding['client_id'],'user_id':account.user_id,
                     'client_profile_sha256':self.auth.binding['client_profile_sha256'],
                     'config_sha256':digest(config),'host_root':str(self.root)}
            started=time.monotonic();refreshed=False
            def fetch(endpoint):
                nonlocal refreshed
                now=time.monotonic()
                if now<started or now-started>=MAX_SECONDS:
                    raise AuthError('ownership_inventory_incomplete')
                if guard is not None:guard()
                token=self.auth.access_token()
                status,body=self.auth._request('GET',endpoint,token=token)
                if status==401 and not refreshed:
                    refreshed=True;self.auth.invalidate_access_token(token)
                    status,body=self.auth._request('GET',endpoint,token=self.auth.access_token())
                return status,body
            first=collect(fetch,account.user_id)
            second=collect(fetch,account.user_id)
            finished=time.monotonic()
            if finished<started or finished-started>=MAX_SECONDS:
                raise AuthError('ownership_inventory_incomplete')
            if first['rooms']!=second['rooms']:
                raise AuthError('ownership_inventory_changed')
            if self._read_journal(aliases)!=before or load(self.root)!=(marker,config):
                raise AuthError('ownership_journal_changed')
            if guard is not None:guard()
            evidence={'binding':binding,'journal_sha256':journal_digest,
                      'rooms_sha256':second['rooms_sha256'],'scope':second['scope']}
            return {**second,'execution':self.execution,'journal_sha256':journal_digest,
                    'journal_unchanged':True,'history_stable':True,'passes':2,
                    'requests':first['requests']+second['requests'],
                    'evidence_sha256':digest(evidence),
                    'next_action':'review_returned_ids; verify_actual_candidate_owner_and_end; '
                        'if_no_candidate_request_upstream_creation_outcome_including_deleted_rooms'}

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
