"""Canonical intent ordering; client observation clocks never order opinions.

Deduplication and schema validation precede this decision in the repository.
Current revisions are logical server revisions, not SQLite rowids or timestamps.
"""


def decide(current, event, predecessor=None):
    """Return (phase, reason) without editing event, state or an old rejection.

    A predecessor may authorize a sequential offline change only when that exact
    accepted intent is still current, belongs to this stream/key and precedes
    the new sequence. A superseded same-value event cannot bridge an unknown old
    revision into permission to overwrite a competing host's newer opinion.
    """
    if current is not None and current['suppressed']:
        return 'permanently_rejected', 'key_suppressed'
    if event.get('kind')=='historical_seed' and current is not None:
        # Observation time never gives a historical import authority to replace
        # any canonical opinion, including neutral or a deletion tombstone.
        return 'superseded','historical_seed_obsolete'
    revision = current['revision'] if current is not None else 0
    known = event['base_revision']
    if known == revision or (current is None and known is None):
        return 'accepted', 'known_revision'
    if (current is not None and predecessor is not None
            and predecessor['phase'] == 'accepted'
            and predecessor['event_id'] == event['predecessor'] == current['event_id']
            and predecessor['revision'] == current['revision']
            and predecessor['installation_id'] == event['installation_id']
            and predecessor['generation'] == event['generation']
            and predecessor['beatmap_id'] == event['beatmap_id']
            and _scope(predecessor) == _scope(event)
            and predecessor['user_id'] == event['user_id']
            and predecessor['sequence'] < event['sequence']):
        return 'accepted', 'current_predecessor'
    if current is not None and current['rating'] == event['rating']:
        return 'superseded', 'same_current_value'
    return 'conflict', 'canonical_revision_changed'


def _scope(event):
    mode=event.get('context',{}).get('ruleset_id')
    return mode if mode in (1,2) else None
