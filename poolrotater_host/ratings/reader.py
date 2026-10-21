"""Canonical-only reader. Independent mode retains the original local readers."""
import os
from pathlib import Path

from ..layout import load,contained,read_json,atomic_json,file_lease
from ..network.contracts import require,fields,identifier
from .snapshots import Cache
from bot.rating_outbox import map_key

# Development shadow matrix and protected donor core checks precede this gate.
# Candidate installation/agent qualification still gates technical acceptance.
# Immutable packaged source carries this gate; a mutable host file cannot open it.
NETWORK_CONSUMPTION_QUALIFIED=True


def profile(root):
    marker,_=load(root);path=contained(root,'config/ratings-reader.json')
    if not os.path.lexists(path):
        from .sync import Synchronizer
        if not (Path(root)/'config/network.json').exists():return {'mode':'independent','server_epoch':None}
        sync=Synchronizer(root)
        if sync.network.credential() is None:return {'mode':'independent','server_epoch':None}
        if not sync.ensure_defaults():return {'mode':'independent','server_epoch':None}
        settings=sync.profile();credential=sync.network.credential()
        require(credential is not None and all(settings[k]==credential[k] for k in ('host_id','server_epoch')),
                'ratings_restored_binding_requires_review')
        return {'mode':'network','server_epoch':settings['server_epoch']}
    value=read_json(path);fields(value,('format','installation_id','generation','mode','server_epoch'))
    require(type(value['format']) is int and value['format']==1
            and all(value[k]==marker[k] for k in ('installation_id','generation'))
            and value['mode'] in ('independent','network'),'invalid_ratings_reader_profile')
    if value['mode']=='network':identifier(value['server_epoch'])
    else:require(value['server_epoch'] is None,'invalid_ratings_reader_profile')
    from ..managed_host.policy import enabled
    if enabled(root) and value['mode']=='independent':
        from .sync import Synchronizer
        sync=Synchronizer(root);credential=sync.network.credential()
        if credential is not None and sync.ensure_defaults():
            value={**value,'mode':'network','server_epoch':sync.profile()['server_epoch']}
    return value


def configure(sync,*,mode):
    require(mode in ('independent','network'),'invalid_ratings_reader_mode')
    from ..managed_host.policy import require_enabled_settings
    require_enabled_settings(sync.root,{'consume_ratings':mode=='network'})
    with file_lease(contained(sync.root,'state/ratings-reader.lock')):
        epoch=None
        if mode=='network':
            require(NETWORK_CONSUMPTION_QUALIFIED,'ratings_shadow_qualification_required')
            # Verify the enrolled binding through the opt-in profile, but this
            # separate choice does not turn rating sharing on implicitly.
            settings=sync.profile();credential=sync.network.credential()
            require(credential is not None and all(settings[k]==credential[k] for k in ('host_id','server_epoch')),
                    'ratings_restored_binding_requires_review')
            epoch=settings['server_epoch']
        marker=sync.marker
        atomic_json(contained(sync.root,'config/ratings-reader.json'),{'format':1,
                    **{k:marker[k] for k in ('installation_id','generation')},'mode':mode,'server_epoch':epoch})
    return {'ok':True,'rating_mode':mode,'provider_dispatch':False,'local_votes_added_to_network':False}


class NetworkReader:
    source='network'
    def __init__(self,root,*,server_epoch,ruleset_id=None):
        self.ruleset_id=ruleset_id
        identifier(server_epoch);self.cache=Cache(root);self.epoch=server_epoch;self.status={'phase':'cold','watermark':0}

    def tallies(self,beatmap_ids):
        counts,status=self.cache.read();self.status=status
        if status.get('server_epoch') not in (None,self.epoch):
            self.status={'phase':'epoch-review-required','watermark':status['watermark']};return {}
        return {int(bid):counts[map_key(int(bid), self.ruleset_id)] for bid in set(beatmap_ids) if map_key(int(bid), self.ruleset_id) in counts}


def for_settings(settings):
    mode=settings.get('ratings_reader_mode','independent')
    require(mode in ('independent','network'),'invalid_ratings_reader_mode')
    if mode=='independent':return None
    require(NETWORK_CONSUMPTION_QUALIFIED,'ratings_shadow_qualification_required')
    return NetworkReader(Path(settings['ratings_reader_root']),server_epoch=settings['ratings_reader_epoch'],ruleset_id=settings.get('ruleset_id'))
