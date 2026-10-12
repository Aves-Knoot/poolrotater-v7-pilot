"""Closed diagnostic vocabulary; never forward private exception text."""
FAILURE_CODES = frozenset(('referee_snapshot_requires_review',
    'referee_room_owner_requires_review', 'referee_generation_changed',
    'ownership_listing_unavailable', 'ownership_listing_refused',
    'provider_existing_rooms_require_review', 'referee_unauthorized',
    'referee_rate_limited', 'referee_unavailable', 'service_failed',
    'ownership_recovery_unresolved', 'ownership_creation_candidate_required',
    'ownership_creation_candidate_refused', 'ownership_recovery_expected_digest_required',
    'ownership_recovery_explicit_choices_required', 'ownership_journal_changed',
    'ownership_room_evidence_refused', 'ownership_listing_unavailable',
    'ownership_recovery_pending', 'ownership_recovery_write_uncertain',
    'ownership_recovery_binding_changed', 'ownership_recovery_transaction_required'))


def failure_code(error):
    seen=set();pending=[error]
    while pending and len(seen)<32:
        item=pending.pop()
        if id(item) in seen:continue
        seen.add(id(item))
        if (type(getattr(item,'args',None)) is tuple and len(item.args)==1
                and type(item.args[0]) is str and item.args[0] in FAILURE_CODES):
            return item.args[0]
        pending.extend(e for e in (item.__cause__,item.__context__) if isinstance(e,BaseException))
    return 'service_failed'
