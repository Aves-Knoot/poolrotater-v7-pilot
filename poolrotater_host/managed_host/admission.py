"""Positive evidence for a managed slot with no upstream room or worker.

A stopped heartbeat alone is insufficient. Match local intent, zero reservation,
the current controller observation and independent clean ownership journal.
These slots need no imaginary healthy worker to qualify an empty-host update.
"""
import os
import hashlib
import json
from .policy import NAME


def closed_slot(row,stored,intent,*,reserved=False):
    if reserved or type(row) is not dict or type(stored) is not dict or type(intent) is not dict:return False
    if (intent.get('pending') is not None or intent.get('room_id') is not None
            or intent.get('room_generation') is not None or intent.get('phase') not in ('stopped','unobserved')):return False
    pause=stored.get('managed_pause')
    if not (intent.get('desired')=='stopped' or intent.get('desired')=='running' and pause=='capacity'):return False
    identity=('pid','generation','room_id','release')
    if any(row.get(k)!=stored.get(k) for k in identity):return False
    for value in (row,stored):
        if (value.get('phase') not in ('parked','stopped') or value.get('operation')!='closed'
                or value.get('room_id') is not None or value.get('pid') is not None
                or value.get('ownership_unknown') is not False or value.get('creation_candidate')
                or value.get('error') or value.get('admission_locked') or value.get('target')):return False
    return True


def closed_aliases(target,parent,rows):
    if not os.path.lexists(target['root']/NAME):return frozenset()
    from ..ratings.operations import _identity
    from ..layout import load,read_json
    from ..reconciliation import _journal
    from ..diagnostic_logs.common import transaction
    from .policy import state
    with _identity(parent['service_uid']):
        _,host=load(target['root']);store=state(target['root'])
        aliases={r['alias'] for r in host['rooms']}
        if store.snapshot()['policy_version']!=1:return frozenset()
        journal=_journal(read_json(target['root']/'state/rolling/ownership.json'),aliases)
        with transaction(store.path) as db:
            intents={r['alias']:dict(r) for r in db.execute('SELECT alias,desired,phase,room_id,room_generation,pending FROM rooms')}
            reserved={r[0] for r in db.execute('SELECT alias FROM reservations')}
        if set(intents)!=aliases:return frozenset()
        return frozenset(alias for alias,row in rows.items() if alias in aliases and
            closed_slot(row,journal['rooms'].get(alias),intents[alias],reserved=alias in reserved))


def stopped_host(target,parent,*,reconcile=False):
    """Positive all-off admission under the independent owner lease.

    Unlike running-worker admission, this needs no fresh heartbeat or old PID.
    It requires a complete clean journal, zero reservations, settled config and
    deliberate off intent (or a zero effective ceiling). No provider requests.
    Reconciliation may release only generation-matching confirmed closures.
    """
    if not os.path.lexists(target['root']/NAME):return None
    from ..pilot_installation import _stopped
    from ..ratings.operations import _identity
    from ..layout import load,read_json
    from ..reconciliation import _journal
    from ..diagnostic_logs.common import transaction
    from .policy import state
    from .projection import document,read
    with _stopped(target['root'],parent['service_uid']):
        with _identity(parent['service_uid']):
            _,host=load(target['root']);aliases={r['alias'] for r in host['rooms']}
            journal=_journal(read_json(target['root']/'state/rolling/ownership.json'),aliases)
            if set(journal['rooms'])!=aliases:return None
            for row in journal['rooms'].values():
                if (row.get('phase') not in ('parked','stopped') or row.get('operation')!='closed'
                        or row.get('room_id') is not None or row.get('pid') is not None
                        or row.get('ownership_unknown') is not False or row.get('creation_candidate')
                        or row.get('target') or row.get('error')):return None
            store=state(target['root'])
            if reconcile:
                from .operations import settle_closed
                settle_closed(target['root'],journal)
            snapshot=store.snapshot();limits=store.limits()
            if (snapshot['policy_version']!=1 or limits['capacity_used']!=0
                    or {r['alias'] for r in snapshot['rooms']}!=aliases):return None
            if limits['effective_limit']>0 and any(r['desired']!='stopped' for r in snapshot['rooms']):return None
            if any(r['room_id'] is not None or r['room_generation'] is not None
                    or r['phase'] not in ('stopped','unobserved','waiting_boundary') for r in snapshot['rooms']):return None
            with transaction(store.path) as db:
                for row in db.execute('SELECT o.phase,o.revision_after FROM rooms r JOIN operations o ON r.pending=o.id'):
                    if row['phase']=='applying' or row['revision_after'] is not None:return None
            projection=document(store)
            if projection!=read(target['root']):return None
            return {'rooms':journal['rooms'],'projection':projection,
                    'intent_sequence':store.intent_sequence(),'effective_limit':limits['effective_limit']}


def stop_only_configuration(target,parent,job,proof,actual):
    """Rebase a queued job only across provable stop-intent edits.

    Settings, titles, account routing and arbitrary preference edits still need
    a new admission. Reconstruct original canonical bytes and compare their
    queued hashes; a revision label on its own grants no exception.
    """
    expected=job['configuration']
    if actual==expected:return True
    from ..provisioning import _control
    from .projection import NAME as PROJECTION
    before=job.get('managed_before');after=proof['projection']
    def digest(value):return hashlib.sha256((json.dumps(value,indent=2,allow_nan=False)+'\n').encode()).hexdigest()
    if (type(before) is not dict or set(before)!=set(after)
            or any(before[k]!=after[k] for k in after if k!='rooms')
            or set(before['rooms'])!=set(after['rooms']) or set(actual)!=set(expected)
            or digest(before)!=expected.get(PROJECTION)
            or digest(after)!=actual.get(PROJECTION)):return False
    changed=set()
    for alias,old in before['rooms'].items():
        new=after['rooms'][alias]
        if old==new:continue
        if (new['settings']!=old['settings'] or old['desired']!='running'
                or new['desired']!='stopped' or new['revision']!=old['revision']+1):return False
        changed.add(alias)
    for name,sha in expected.items():
        if actual[name]==sha or name==PROJECTION:continue
        alias=next((a for a in changed if name=='config/rooms/'+a+'.json'),None)
        if alias is None:return False
        value=_control(target['root']/name,parent['service_uid'],private=True)
        if value.get('managed_revision')!=after['rooms'][alias]['revision']:return False
        if digest({**value,'managed_revision':before['rooms'][alias]['revision']})!=sha:return False
    return True
