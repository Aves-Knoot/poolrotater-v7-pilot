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
    _,snapshot=Cache(sync.root).read()
    result.update(rating_mode=profile(sync.root)['mode'],network_consumption_qualified=NETWORK_CONSUMPTION_QUALIFIED,
                  snapshot_phase=snapshot['phase'],snapshot_watermark=snapshot['watermark'])
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
