"""Sealed installed ratings controls, independent from provider authorization."""
import os

from ..layout import contained, read_json
from ..network.installed import guard
from ..network.contracts import NetworkError, require, decode, fields
from .migration import local_status
from .sync import Synchronizer

ACTIONS=('configure','sync','fetch','status','support')


def diagnostics(sync):
    from .reader import profile,NETWORK_CONSUMPTION_QUALIFIED
    from .snapshots import Cache
    result=local_status(sync.root)
    result.update(sharing_configured=os.path.lexists(sync.profile_path), share_ratings=False,
                  aggregate_inclusion_confirmed=False, live_rooms_affected=False)
    if result['sharing_configured']:result['share_ratings']=sync.profile(allow_review=True)['share_ratings']
    counts,snapshot=Cache(sync.root).read()
    reader=profile(sync.root)
    result.update(rating_mode=reader['mode'],network_consumption_qualified=NETWORK_CONSUMPTION_QUALIFIED,
                  snapshot_phase=snapshot['phase'],snapshot_watermark=snapshot['watermark'])
    events=result.get('events',{})
    complete=snapshot['phase'] in ('current','last-good')
    result['progress']={
        'locally_captured_events':sum(events.values()),
        'queued_upload_events':sum(events.get(phase,0) for phase in ('pending','retryable')),
        'centrally_acknowledged_events':sum(events.get(phase,0) for phase in ('accepted','already_accepted')),
        'nonaccepted_outcomes':{phase:count for phase,count in events.items()
            if phase not in ('pending','retryable','accepted','already_accepted')},
        'verified_shared_snapshot_available':complete,
        'verified_shared_snapshot_maps':len(counts) if complete else 0,
        'configured_selection_source':reader['mode'],
        'shared_snapshot_eligible_for_configured_reader':bool(complete and
            reader['mode']=='network' and NETWORK_CONSUMPTION_QUALIFIED and
            snapshot.get('server_epoch')==reader['server_epoch']),
        'runtime_reader_observation_verified':False,
        'acknowledgment_proves_aggregate_inclusion':False}
    if not result.get('capture_enabled'):result['next_action']='ratings-migrate-while-stopped; review-if-recovery-required'
    elif not result['share_ratings']:result['next_action']='obtain-central-contribute-grant-before-enabling-sharing'
    elif result['progress']['queued_upload_events']:result['next_action']='inspect-ratings-agent-and-await-scheduled-upload'
    elif not complete:result['next_action']='ratings-fetch-for-verified-snapshot; upload-ack-is-separate'
    else:result['next_action']='inspect-verified-snapshot-and-configured-reader; confirm-real-vote-inclusion-separately'
    path=contained(sync.root,'state/ratings-agent.json')
    if os.path.lexists(path):
        from .agent import validate_receipt
        value=validate_receipt(read_json(path),sync.marker)
        result.update(agent_phase=value['phase'],agent_error=value['error'],
                      last_attempt_at=value['attempted_at'],next_attempt_at=value['next_attempt_at'],
                      consecutive_failures=value['failures'])
    path=contained(sync.root,'state/ratings-fetch-agent.json')
    if os.path.lexists(path):
        from .agent import validate_fetch_receipt
        value=validate_fetch_receipt(read_json(path),sync.marker)
        result.update(fetch_agent_phase=value['phase'],fetch_agent_error=value['error'],
                      fetch_next_attempt_at=value['next_attempt_at'],fetch_pending_pages=value['pending_pages'])
    return result


def execute(sync,action,stream=None):
    require(action in ACTIONS,'invalid_ratings_action')
    require((stream is not None)==(action=='configure'),'explicit_rating_sharing_required')
    if action=='configure':
        value=decode(stream.read(1025),maximum=1024);fields(value,('share_ratings',))
        return sync.configure(**value)
    if action=='sync':return sync.step()
    if action=='fetch':return sync.fetch_snapshots()
    return diagnostics(sync)


def run(root,release,action,stream=None):
    # Must run before private stdin, SQLite or any hub request.
    guard(root,release)
    try:
        return {**execute(Synchronizer(root),action,stream),
                'protected_runtime_checked':True,'actual_room_testing':False,'provider_dispatch':False}
    except NetworkError:
        # Remote/private messages are deliberately absent from installed output.
        return {'ok':False,'error':'ratings-operation-refused','protected_runtime_checked':True,
                'actual_room_testing':False,'provider_dispatch':False,'live_rooms_affected':False}
