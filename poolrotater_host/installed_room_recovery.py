"""Explicit stopped, read-only upstream recovery; no room mutations."""
from .oauth import AuthError
from .provider_room_recovery import ProviderOwnershipReview, ProviderOwnershipRecovery


def local_status(root):
    """Stopped local authority only; no OAuth, provider listing or diagnostic adoption."""
    from .config import ConfigError
    from .layout import load, owner_lease, read_json
    from .reconciliation import _journal, journal_path
    from .ownership_recovery import require_settled, RECOVERY_PATH, MAXIMUM, validate_record
    from .config import contained
    import os
    try:
        with owner_lease(root):
            marker, config = load(root)
            phase = 'absent'
            try: receipt = read_json(contained(root, RECOVERY_PATH), maximum=MAXIMUM)
            except FileNotFoundError: receipt = None
            if receipt is not None:
                validate_record(receipt, root=root, marker=marker, uid=os.geteuid())
                phase = receipt['phase']
            require_settled(root)
            try:
                if not os.path.lexists(contained(root, 'state/rolling/ownership.json')):
                    if receipt is not None or os.path.lexists(contained(root, 'state/provider-room-binding.json')):
                        raise AuthError('ownership_journal_missing')
                    raise FileNotFoundError
                journal = _journal(read_json(journal_path(root)), {r['alias'] for r in config['rooms']})
            except FileNotFoundError: journal = {'rooms': {}}
            unresolved = any(s.get('room_id') is not None or s.get('ownership_unknown')
                             or s.get('operation') in ('make_pending', 'close_pending')
                             for s in journal['rooms'].values())
            return {'room_phase': 'recovery_required' if unresolved else 'stopped',
                    'ownership_recovery_phase': phase, 'ownership_status_source': 'journal_and_receipt',
                    'live_ready': False, 'start_allowed': False}
    except (ConfigError, AuthError):
        return {'room_phase': 'recovery_required', 'ownership_recovery_phase': 'requires_review',
                'ownership_status_source': 'journal_and_receipt', 'failure_code': 'ownership_local_state_requires_review',
                'live_ready': False, 'start_allowed': False}


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
    stage = 'authority'
    try:
        auth = installed_provider_authentication(root, release, TrustSnapshot.system(prepared=True))
        stage = 'review' if action == 'review' else 'recovery'
        return execute(auth, action, **choices)
    except AuthError as error:
        from .service_diagnostics import failure_code
        code = failure_code(error)
        if code == 'service_failed':
            code = {'authority': 'ownership_review_authority_refused',
                    'review': 'ownership_review_provider_refused',
                    'recovery': 'ownership_recovery_refused'}[stage]
        return {'ok':False, 'failure_code':code, 'failure_stage':stage, 'recovery_applied':False,
                'start_allowed':False, 'live_ready':False,
                'next_action':'retain_state; inspect stopped ownership review and any pending recovery transaction'}
