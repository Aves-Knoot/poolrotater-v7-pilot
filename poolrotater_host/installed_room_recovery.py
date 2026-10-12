"""Explicit stopped, read-only upstream recovery; no room mutations."""
from .oauth import AuthError
from .provider_room_recovery import ProviderOwnershipReview, ProviderOwnershipRecovery


def execute(auth, action, *, journal_sha256=None, alias=None, room_id=None,
            confirm_lost_creation=False, transaction_id=None, decision=None):
    if action not in ('review', 'clear', 'resume'):
        raise AuthError('ownership_recovery_action_required')
    candidate = alias is not None or room_id is not None or confirm_lost_creation
    if (type(confirm_lost_creation) is not bool
            or (candidate and (action != 'clear' or alias is None or room_id is None
                               or not confirm_lost_creation))
            or (action == 'clear') != (journal_sha256 is not None)
            or (action == 'resume') != (transaction_id is not None and decision is not None)
            or (action != 'resume' and (transaction_id is not None or decision is not None))):
        raise AuthError('ownership_recovery_explicit_choices_required')
    if action == 'review': return ProviderOwnershipReview(auth).inspect()
    recovery = ProviderOwnershipRecovery(auth)
    if action == 'resume':
        return recovery.recover(transaction_id=transaction_id, action=decision)
    return recovery.apply(expected_journal_sha256=journal_sha256,
                          creation_candidates={alias: room_id} if candidate else None)


def run(root, release, action, **choices):
    from .installed_authorization import installed_provider_authentication
    from .provider_transport import TrustSnapshot
    try:
        auth = installed_provider_authentication(root, release, TrustSnapshot.system(prepared=True))
        return execute(auth, action, **choices)
    except AuthError as error:
        from .service_diagnostics import failure_code
        return {'ok':False, 'failure_code':failure_code(error), 'recovery_applied':False,
                'start_allowed':False, 'live_ready':False,
                'next_action':'retain_state; inspect stopped ownership review and any pending recovery transaction'}
