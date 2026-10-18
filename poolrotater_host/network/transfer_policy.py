"""Explicit additive negotiation; old hubs retain their safe upload pacing."""
import time
from ..layout import load,read_json,atomic_json
from .contracts import fields,require,number,fingerprint

CAPABILITY={'ok':True,'format':1,'policy':'cpu-limited-v1','uncapped_authorized_transfers':True}


def enabled(client,*,clock=time.time,refresh=False):
    marker,_=load(client.root);path=client.root/'state/network-transfer-policy.json';now=clock()
    credential=client.credential()
    if credential is None:return False
    binding={**{k:marker[k] for k in ('installation_id','generation')},
             **{k:credential[k] for k in ('host_id','server_epoch')},'profile_sha256':fingerprint(client.profile())}
    if path.exists():
        state=read_json(path)
        fields(state,('format','binding','checked_at','uncapped'))
        require(state['format']==1 and type(state['uncapped']) is bool,'invalid_transfer_policy')
        number(state['checked_at'])
        if state['binding']==binding and 0<=now-state['checked_at']<=1200:return state['uncapped']
    if not refresh:return False
    allowed=False
    try:
        result=client.transport(client.profile()).request('GET','/v1/transfers/capabilities',token=credential['credential'])
        # Any unknown, older or unavailable response stays at predecessor pacing.
        allowed=(type(result) is dict and result==CAPABILITY
                 and type(result.get('format')) is int
                 and result.get('ok') is True and result.get('uncapped_authorized_transfers') is True)
    except Exception:pass
    atomic_json(path,{'format':1,'binding':binding,'checked_at':now,'uncapped':allowed})
    return allowed
