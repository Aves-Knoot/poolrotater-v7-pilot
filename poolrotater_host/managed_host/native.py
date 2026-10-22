"""Controller adapter: reuse native workers, hotload and verified room closure."""
import copy
import os
from pathlib import Path
import threading
import time
from ..config import PRESETS
from ..layout import load
from . import settings
from .engine import Engine,Deferred,BeforeMutationError
from .projection import export


def apply_pool_styles(pool,cfg):
    styles=cfg.get('selection_styles')
    if styles is None and 'selection_style' in cfg:styles=[cfg['selection_style']]
    if styles is None:return
    if cfg.get('ruleset_id')==3:
        count=pool.set_mania_source(styles or ['variety'])
    elif cfg.get('ruleset_id',0)==0 and not getattr(pool,'loved_mode',False):
        count=pool.set_style_selection([] if styles==['variety'] else styles)
    else:count=len(pool.maps)
    if not count:raise ValueError('managed_style_has_no_eligible_maps')


def apply_worker_settings(bot,patch):
    """Called only from the worker's main-loop lifecycle queue."""
    mode=('mania-'+str(bot.cfg.get('mania_keys',4))+'k' if bot.cfg.get('ruleset_id')==3 else
          'catch-fixed' if bot.cfg.get('ruleset_id')==2 else
          'standard-loved' if bot.cfg.get('loved_mode') else
          'standard-auto' if bot.cfg.get('auto_difficulty') else 'standard-fixed')
    settings.validate(patch,current={k:v for k,v in bot.cfg.items() if k in settings.FIELDS}|{'preset':mode})
    if any(k in settings.RECREATE for k in patch):raise ValueError('managed_settings_requires_recreation')
    # The controller already materialized the validated persistent defaults.
    # Force the existing guarded reload on this main-loop turn, not an RPC thread.
    bot._last_config_check=0;bot._config_mtime_ns=None
    bot._reload_config_from_disk()
    checks={k:v for k,v in patch.items() if k!='custom_join_greeting_changes'}
    for key,wanted in checks.items():
        actual=bot.cfg.get(key)
        if key in ('admins','moderators','allowed_mods','required_mods','selection_styles'):
            if type(actual) is not list:return False
            if key=='moderators':wanted=[uid for uid in wanted if uid not in bot.cfg.get('admins',[])]
            if set(actual)!=set(wanted):return False
        elif actual!=wanted:return False
    if 'custom_join_greeting_changes' in patch:
        # Entry edits are expanded from persistent host defaults by the parent.
        # The worker's effective defaults may differ (legacy empty overrides or
        # unrelated entries), so acknowledge the requested edits themselves.
        from bot.custom_greetings import DEFAULT_JOIN_GREETINGS
        greetings=bot.cfg.get('custom_join_greetings',DEFAULT_JOIN_GREETINGS)
        if type(greetings) is not dict:return False
        for key,wanted in patch['custom_join_greeting_changes'].items():
            if (key in greetings if wanted is None else greetings.get(key)!=wanted):return False
    # Unrelated tuning must retain a player's temporary pool selection. Only an
    # explicit style command can replace it. Retire the opposite persisted form
    # because the generic native reload intentionally retains absent cfg keys.
    if 'selection_styles' in patch:
        bot.cfg.pop('selection_style',None);apply_pool_styles(bot.pool,bot.cfg)
    elif 'selection_style' in patch:
        bot.cfg.pop('selection_styles',None);apply_pool_styles(bot.pool,bot.cfg)
    return True

class NativeAdapter:
    def __init__(self,coordinator):self.coordinator=coordinator;self.owner=coordinator.owner;self.state=coordinator.state;self.admitted=None
    def preflight(self,value):
        alias=value['alias']
        if alias not in self.owner.configs:raise BeforeMutationError('room_not_found')
        row=self.state.room(alias,private=True)
        if value['action']=='catalog':
            from ..catalog_store import mode_for
            if mode_for({'preset':row['config']['preset']})!=value['patch']['mode']:
                raise BeforeMutationError('catalog_unavailable')
        limits=self.state.limits()
        needs_open=value['action'] in ('open','restart') or value['action']=='catalog' and row['desired']=='running' or value['action']=='configure' and row['desired']=='running' and settings.application(value['patch'])=='recreate'
        if needs_open and (limits['effective_limit']==0 or limits['capacity_used']>limits['effective_limit']
                or row['room_id'] is None and limits['capacity_used']>=limits['effective_limit']):
            raise BeforeMutationError('host_capacity_reached')
        if value['action']=='configure':
            worker=self.owner.workers.get(alias)
            if settings.application(value['patch'])!='recreate' and (self.owner.config_loader is None
                    or worker is not None and worker.process.poll() is None and worker.status.get('managed_control_version')!=1):
                raise BeforeMutationError('unsupported_capability')
            try:settings.validate(value['patch'],current=row['config'])
            except (ValueError,TypeError):raise BeforeMutationError('invalid_settings') from None
            candidate=settings.merge(row['config'],value['patch'])
            if (settings.application(value['patch'])!='live'):
                # Read-only candidate validation against the complete qualified
                # catalog bundle. Never shrink pools or copy mutable research DBs.
                from ..catalog_store import resolved_catalogs,settings_for
                room={'alias':alias,'preset':candidate['preset']}
                marker,host=load(self.owner.runtime)
                from .projection import configuration
                effective=configuration(self.owner.runtime,marker,host)
                selected=next(r for r in effective['rooms'] if r['alias']==alias);selected['preset']=candidate['preset']
                try:
                    resolved=resolved_catalogs(self.owner.runtime,configuration=effective)
                    cfg=copy.deepcopy(self.owner.configs[alias]);spec=PRESETS[candidate['preset']]
                    cfg.update(ruleset_id=spec['ruleset'],mania_keys=spec['keys'] or 4,loved_mode=candidate['preset']=='standard-loved')
                    cfg.update({k:v for k,v in candidate.items() if k not in ('preset','visibility','password')})
                    if resolved is not None:cfg.update(settings_for(room,resolved))
                    from rolling.worker import make_worker_pool
                    pool=make_worker_pool(cfg)
                    if not pool.maps:raise ValueError('empty candidate pool')
                except Exception:raise BeforeMutationError('catalog_unavailable') from None
        row=self.owner.registry.snapshot()['rooms'].get(alias,{})
        if row.get('ownership_unknown') or row.get('phase')=='recovery_required':raise BeforeMutationError('room_ownership_requires_review')
    def ready(self,value):
        if value['action']=='catalog':
            from .catalog_rollout import ready
            try:
                if not ready(self.owner.protected_parent['prefix'],value):return False
            except Exception:raise BeforeMutationError('catalog_unavailable') from None
        if self.owner.stopping.is_set() or self.owner.rollout_alias or self.owner.targets:return False
        if os.path.lexists(Path(self.owner.runtime)/'state/rolling/supervisor-maintenance.json'):return False
        worker=self.owner.workers.get(value['alias'])
        if worker is None or worker.process.poll() is not None:
            row=self.owner.registry.snapshot()['rooms'].get(value['alias'],{})
            return not row.get('room_id') and not row.get('ownership_unknown')
        if not worker.fresh() or not worker.status.get('ready'):return False
        if value['action']=='open':return True  # already owned is a verified no-op
        if value['action']=='configure' and settings.application(value['patch'])!='recreate':
            return worker.status.get('managed_control_version')==1
        if value['timing']=='immediate':return True
        return (worker.idle_empty() and worker.empty_since is not None
                and time.monotonic()-worker.empty_since>=self.owner.empty_seconds)
    def admit(self,value):
        with self.owner.status_lock:
            if (self.owner.rollout_alias or self.owner.stopping.is_set()
                    or os.path.lexists(Path(self.owner.runtime)/'state/rolling/supervisor-maintenance.json')):return False
            self.owner.rollout_alias=value['alias'];self.admitted=value['alias'];return True
    def release(self):
        with self.owner.status_lock:
            if self.admitted is not None and self.owner.rollout_alias==self.admitted:
                self.owner.rollout_alias=None
            self.admitted=None
    def apply(self,value):
        alias=value['alias'];action=value['action'];owner=self.owner
        worker=owner.workers.get(alias)
        running=worker is not None and worker.process.poll() is None
        revision=self.state.room(alias)['revision']
        if action=='open' and running and worker.broker.hub.owned:
            return {'application':'lifecycle','room_id':worker.status.get('room_id'),'revision':revision}
        recreate=action in ('restart','catalog') or action=='configure' and settings.application(value['patch'])=='recreate'
        if action=='catalog' and not running and self.state.room(alias)['desired']=='stopped':
            return {'application':'recreate','revision':revision,'room_id':None,
                    'catalog_content_id':value['patch']['content_id']}
        if action=='configure' and not running and self.state.room(alias)['desired']=='stopped':
            # Saved defaults on an intentionally stopped room do not grant room
            # creation, even when the patch would recreate a running room.
            owner.config_loader()
            return {'application':settings.application(value['patch']),'revision':revision,'room_id':None}
        if action=='configure' and not recreate:
            if self.state.room(alias)['room_generation']!=value['expected_room_generation']:
                raise RuntimeError('managed_room_replaced_before_apply')
            if owner.config_loader is None:raise RuntimeError('managed_settings_dispatch_uncertain')
            # Materialize only changed defaults; existing bridge preserves player
            # preferences and unrelated configuration using its original bases.
            owner.config_loader()
            if running:
                if worker.status.get('managed_control_version')!=1:raise RuntimeError('managed_settings_dispatch_uncertain')
                worker.peer.call('managed-settings',value['id'],value['patch'],timeout=10)
                owner.wait_for(lambda: (worker.status.get('control') or {}).get('id')==value['id'],45,worker)
                ack=worker.status['control']
                if not ack.get('ok'):raise RuntimeError('managed_settings_acknowledgment_uncertain')
            return {'application':settings.application(value['patch']),'revision':revision,
                    'room_id':worker.status.get('room_id') if running else None}
        candidate=None;closed=False
        try:
            if action!='close':
                release=worker.release if worker is not None else owner.start_release_for(alias)
                try:candidate=owner.prepare(alias,release)
                except Exception:raise BeforeMutationError('catalog_unavailable') from None
                if action=='catalog':
                    actual=candidate.broker.cfg.get('_host_catalog_reference',{}).get('modes',{}).get(value['patch']['mode'])
                    if actual!=value['patch']['content_id']:raise BeforeMutationError('catalog_unavailable')
            if running:
                if value['timing']!='immediate':
                    from rolling.supervisor import HandoffDeferred
                    try:owner.control(worker,'prepare')
                    except HandoffDeferred:raise Deferred() from None
                with owner.shared.lock:
                    if value['timing']!='immediate' and not worker.idle_empty():raise Deferred()
                    if worker.broker.uncertain or len(worker.broker.hub.owned)!=1:
                        raise BeforeMutationError('room_ownership_requires_review')
                    if self.state.room(alias)['room_generation']!=value['expected_room_generation']:
                        raise RuntimeError('managed_room_replaced_before_apply')
                    rid,=worker.broker.hub.owned
                    from rolling.broker import InfrastructureCloseDeferred
                    try:worker.broker.close_room(rid,require_empty=value['timing']!='immediate')
                    except InfrastructureCloseDeferred:raise Deferred() from None
                    closed=True
                    worker.broker.quiesced=True
                owner.dispose(worker);owner.record(alias,phase='parked',pid=None,error=None)
            if action=='close':return {'application':'lifecycle','room_id':None,'revision':revision}
            owner.activate(candidate);candidate=None
            current=owner.workers[alias]
            result={'application':'recreate' if recreate else 'lifecycle','room_id':current.status.get('room_id'),'revision':revision}
            if action=='catalog':
                result['catalog_content_id']=value['patch']['content_id']
                result['catalog_set_id']=current.broker.cfg['_host_catalog_set']
            return result
        finally:
            if candidate is not None:owner.dispose(candidate)
            if running and not closed and getattr(worker,'status',{}).get('paused'):
                try:owner.control(worker,'resume')
                except Exception:pass

class Coordinator:
    def __init__(self,owner,state):
        self.owner=owner;self.state=state;self.engine=Engine(state,NativeAdapter(self));self.thread=None
        self._observations={};self.capacity_retry_at={};self.state.recover_applying();export(self.state)
    def desired_running(self,alias):return self.state.room(alias)['desired']=='running'
    def startup_allowed(self,alias):
        limits=self.state.limits()
        return self.desired_running(alias) and limits['capacity_used']<limits['effective_limit']
    def creation_begin(self,broker):
        self.state.reserve(broker.alias,broker.generation)
        self.state.mark_dispatched(broker.alias,broker.generation)
    def creation_candidate(self,broker,room_id):self.state.uncertain_creation(broker.alias,broker.generation,room_id)
    def owned(self,broker,room_id):self.state.observe_owned(broker.alias,broker.generation,room_id)
    def closed(self,broker,room_id):self.state.confirmed_closed(broker.alias,broker.generation,room_id)
    def park(self,alias):
        stored=self.owner.registry.snapshot()['rooms'].get(alias,{})
        if stored.get('room_id') or stored.get('ownership_unknown') or stored.get('operation') in ('make_pending','close_pending'):
            raise RuntimeError('managed_park_requires_closed_ownership')
        self.owner.record(alias,phase='parked',operation='closed',room_id=None,ownership_unknown=False,
                          pid=None,managed_pause='intent' if not self.desired_running(alias) else 'capacity')
    def capacity_action(self):
        limits=self.state.limits();rows=self.owner.registry.snapshot()['rooms']
        if limits['over_capacity_draining']:
            for alias,worker in sorted(self.owner.workers.items(),reverse=True):
                if (worker.process.poll() is None and worker.fresh() and worker.idle_empty()
                        and worker.empty_since is not None and time.monotonic()-worker.empty_since>=self.owner.empty_seconds
                        and rows.get(alias,{}).get('operation')=='owned' and not worker.broker.uncertain):
                    return ('drain',alias)
        elif limits['capacity_used']<limits['effective_limit']:
            for alias in self.owner.configs:
                row=rows.get(alias,{})
                if (row.get('managed_pause')=='capacity' and row.get('phase')=='parked'
                        and self.desired_running(alias) and time.monotonic()>=self.capacity_retry_at.get(alias,0)):
                    return ('resume',alias)
        return None
    def reconcile_capacity(self,action,alias):
        adapter=self.engine.adapter;value={'alias':alias}
        if not adapter.admit(value):return
        worker=self.owner.workers.get(alias);paused=False;closed=False
        try:
            if action=='resume':
                if not self.startup_allowed(alias):return
                self.owner.activate(self.owner.prepare(alias,self.owner.start_release_for(alias)))
                self.owner.record(alias,managed_pause=None)
                return
            if not self.state.limits()['over_capacity_draining']:return
            if worker is None or not worker.fresh() or not worker.idle_empty():return
            from rolling.supervisor import HandoffDeferred
            try:self.owner.control(worker,'prepare');paused=True
            except HandoffDeferred:return
            with self.owner.shared.lock:
                if not worker.idle_empty() or not self.state.limits()['over_capacity_draining']:return
                if worker.broker.uncertain or len(worker.broker.hub.owned)!=1:return
                rid,=worker.broker.hub.owned
                from rolling.broker import InfrastructureCloseDeferred
                try:worker.broker.close_room(rid,require_empty=True)
                except InfrastructureCloseDeferred:return
                closed=True;worker.broker.quiesced=True
            self.owner.dispose(worker)
            self.owner.record(alias,phase='parked',operation='closed',room_id=None,pid=None,
                              ownership_unknown=False,managed_pause='capacity',error=None)
        except Exception:
            # Preserve authoritative uncertainty. A failed preparation has not
            # closed any room and can retry; a dispatched close requires review.
            self.capacity_retry_at[alias]=time.monotonic()+60
            if worker is not None and worker.broker.uncertain:
                self.owner.record(alias,phase='recovery_required',error='managed_capacity_closure_requires_review')
        finally:
            if paused and not closed and worker is not None:
                try:self.owner.control(worker,'resume')
                except Exception:pass
            adapter.release()
    def tick(self):
        for alias,worker in self.owner.workers.items():
            state=self.owner.registry.snapshot()['rooms'].get(alias,{})
            identity=(worker.broker.generation,state.get('room_id'),state.get('operation'),state.get('ownership_unknown'))
            if self._observations.get(alias)==identity:continue
            self._observations[alias]=identity
            if (state.get('operation')=='owned' and not state.get('ownership_unknown')
                    and state.get('room_id') in worker.broker.hub.owned):
                self.state.observe_owned(alias,worker.broker.generation,state['room_id'],adopt=True)
        if self.thread is not None and self.thread.is_alive():return
        if self.owner.rollout_alias or self.owner.targets:return
        capacity=None if self.state.pending() else self.capacity_action()
        if not self.state.pending() and capacity is None:return
        # Reserve the same parent lifecycle gate used by native rollouts/Discord.
        def execute():
            os.setpriority(os.PRIO_PROCESS,0,max(15,os.getpriority(os.PRIO_PROCESS,0)))
            try:
                if capacity is not None:self.reconcile_capacity(*capacity)
                else:self.engine.step()
            finally:self.engine.adapter.release()
        # NativeAdapter.ready observes this coordinator's own gate as clear;
        # the worker is assigned only once its operation is admitted below.
        self.thread=threading.Thread(target=execute,daemon=True,name='managed-room-operation');self.thread.start()


def attach(owner):
    from .policy import enabled,state as open_state
    if not enabled(owner.runtime):return None
    state=open_state(owner.runtime)
    from .policy import seed
    seed(owner.runtime,state)
    state.reconcile_closed_journal(owner.registry.snapshot())
    return Coordinator(owner,state)
