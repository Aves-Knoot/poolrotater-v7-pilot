"""Explicit fleet-wide prerequisite gate before any catalog activation queue.

The complete enrolled-host inventory is an operator-reviewed input. Stale,
missing, revoked, changed inventory, mixed controller/worker versions and failed
controllers all hold the whole batch. Each host then switches independently at
its existing empty-room boundary; no global synchronized empty window.
"""
import json
import re
import uuid
from ..diagnostic_logs.common import transaction
from ..network.contracts import require,identifier
from .contracts import digest

def review(hosts,observations,*,expected_hosts,required_version,now):
    require(type(expected_hosts) is list and expected_hosts and len(set(expected_hosts))==len(expected_hosts),
            'catalog_fleet_inventory_refused')
    for host in expected_hosts:identifier(host)
    require(type(required_version) is str and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',required_version) is not None,
            'catalog_fleet_version_refused')
    require(tuple(map(int,required_version[1:].split('.')))>=(7,2,14),'catalog_fleet_version_refused')
    require({h['id'] for h in hosts if not h['revoked']}==set(expected_hosts),'catalog_fleet_inventory_changed')
    by_id={h['id']:h for h in hosts};plans=[]
    for hid in expected_hosts:
        host=by_id[hid];report=observations.get(hid)
        require(report is not None and 0<=now-report['received']<=180 and host.get('received') is not None
                and 0<=now-host['received']<=180,'catalog_fleet_host_stale')
        heartbeat=json.loads(host['heartbeat']);caps=report['capabilities']
        require(heartbeat['code_version']==required_version and caps['controller_phase']=='running'
                and caps['installation_id']==host['installation_id'] and caps['generation']==host['generation'],
                'catalog_fleet_version_not_ready')
        active={r['alias']:r for r in heartbeat['rooms']}
        for row in caps['rooms']:
            require(row['pending_operation'] is None and row['failure_code'] is None,'catalog_fleet_operation_pending')
            if row['desired']=='running':
                observed=active.get(row['alias'])
                require(row['phase']=='running' and observed is not None and observed['code_version']==required_version
                        and type(observed.get('observed_at')) in (int,float) and 0<=now-observed['observed_at']<=180,
                        'catalog_fleet_worker_not_ready')
            plans.append((hid,row))
    return plans

def queue(repository,*,expected_hosts,required_version,mode,content_id,batch_id):
    """Administrator-side API; deterministic IDs identify partial delivery.

    Gate precedes the first queue call. A partial batch requires review, not a
    blind loop with new IDs. Individual queue() still checks fresh
    generation/revision, preventing a topology change from targeting new rooms.
    """
    from ..catalog_store import mode_for
    identifier(batch_id)
    require(mode in ('standard','mania4','mania7','catch','loved') and type(content_id) is str
            and re.fullmatch('[a-f0-9]{64}',content_id) is not None,'catalog_fleet_target_refused')
    now=repository.store.clock()
    with repository.store.transaction() as db:
        hosts=[dict(r) for r in db.execute('SELECT id,revoked,received,heartbeat,installation_id,generation FROM hosts')]
    with transaction(repository.path) as db:
        reports={r['host']:{'received':r['received'],'capabilities':json.loads(r['capabilities'])}
                 for r in db.execute('SELECT host,received,capabilities FROM reports')}
    plan=review(hosts,reports,expected_hosts=expected_hosts,required_version=required_version,now=now)
    requests=[]
    for hid,row in plan:
        if mode_for({'preset':row['preset']})!=mode:continue
        # Explicitly stopped rooms share the desired catalog reference, but must
        # not be reopened or needlessly acquire a live room mutation request.
        if row['desired']!='running':continue
        operation=str(uuid.uuid5(uuid.UUID(batch_id),hid+'/'+row['alias']))
        requests.append(repository.queue(hid,row['alias'],'catalog',patch={'mode':mode,'content_id':content_id},
            timing='when_empty',actor='catalog-fleet:'+batch_id,expected_revision=row['revision'],
            expected_room_generation=row['room_generation'],expires_seconds=7*86400,operation_id=operation))
    return {'ok':True,'batch_id':batch_id,'mode':mode,'content_id':content_id,
            'required_version':required_version,'gated_hosts':expected_hosts,'requests':requests}
