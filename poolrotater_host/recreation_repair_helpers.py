"""Finite helper pause/restore for an admitted stopped recreation repair.

The caller owns root deployment/network admission and validates native update
transactions before pausing. This module never stops the room controller, kills
an executing helper, clears ownership or starts rooms. Snapshots must be saved in
the repair's protected receipt before pause, so interrupted work is recoverable.
"""
import time
from .config import ConfigError
from .pilot_installation import paths

TIMERS=('updates','controller-update','managed','recovery')


def names(instance):
    paths(instance)  # Validate the instance before constructing unit names.
    return {key:'poolrotater-v7-'+key+'-'+instance for key in TIMERS}


def _valid(value, *, timer=False):
    if (type(value)is not dict or not {'LoadState','ActiveState','MainPID','UnitFileState'}<=set(value)
            or value['LoadState'] not in ('loaded','not-found')
            or value['ActiveState'] not in ('active','activating','inactive','failed','deactivating')
            or type(value['MainPID'])is not int or value['MainPID']<0
            or value['UnitFileState'] not in ('enabled','disabled','static','','not-found')):
        raise ConfigError('repair_helper_state_requires_review')
    if (value['LoadState']=='not-found'and (value['MainPID'] or value['ActiveState']!='inactive')
            or value['LoadState']=='loaded'and value['UnitFileState']in ('','not-found')):
        raise ConfigError('repair_helper_state_requires_review')
    if timer and value['MainPID']!=0:raise ConfigError('repair_helper_state_requires_review')
    return dict(value)


class Helpers:
    def __init__(self,instance,operations,*,clock=time.monotonic,sleep=time.sleep):
        self.instance=instance;self.ops=operations;self.units=names(instance)
        self.clock=clock;self.sleep=sleep
    def snapshot(self):
        bot=_valid(self.ops.state('poolrotater-v7-pilot-'+self.instance+'.service'))
        if bot['MainPID'] or bot['ActiveState'] not in ('inactive','failed'):
            raise ConfigError('repair_requires_stopped_bot')
        timers={key:_valid(self.ops.state(name+'.timer'),timer=True)for key,name in self.units.items()}
        network=_valid(self.ops.state('poolrotater-v7-network-'+self.instance+'.service'))
        return {'format':1,'instance':self.instance,'bot':bot,'timers':timers,'network':network}
    def validate(self,snapshot):
        if (type(snapshot)is not dict or set(snapshot)!={'format','instance','bot','timers','network'}
                or type(snapshot['format'])is not int or snapshot['format']!=1
                or snapshot['instance']!=self.instance
                or type(snapshot['timers'])is not dict or set(snapshot['timers'])!=set(TIMERS)):
            raise ConfigError('repair_helper_snapshot_requires_review')
        _valid(snapshot['bot']);_valid(snapshot['network'])
        for row in snapshot['timers'].values():_valid(row,timer=True)
        if snapshot['bot']['MainPID'] or snapshot['bot']['ActiveState']not in ('inactive','failed'):
            raise ConfigError('repair_requires_stopped_bot')
    def pause(self,snapshot,*,timeout=120):
        self.validate(snapshot)
        if type(timeout)not in (int,float) or not 0<timeout<=300:
            raise ConfigError('repair_helper_wait_refused')
        for key,name in self.units.items():
            state=_valid(self.ops.state(name+'.timer'),timer=True)
            if state['LoadState']!='not-found':self.ops.systemctl('stop',name+'.timer')
        started=self.clock()
        while True:
            running=[]
            for key,name in self.units.items():
                state=_valid(self.ops.state(name+'.service'))
                if state['MainPID'] or state['ActiveState']not in ('inactive','failed'):running.append(key)
            if not running:break
            now=self.clock()
            if now<started or now-started>=timeout:
                raise ConfigError('repair_wait_for_executing_helpers')
            self.sleep(min(1,timeout-(now-started)))
        # Stop the network last, after all admitted root helpers have finished.
        name='poolrotater-v7-network-'+self.instance+'.service'
        state=_valid(self.ops.state(name))
        if state['LoadState']!='not-found':self.ops.systemctl('stop',name)
        state=_valid(self.ops.state(name))
        if state['MainPID'] or state['ActiveState']not in ('inactive','failed'):
            raise ConfigError('repair_network_cleanup_requires_review')
        return {'helpers_paused':True,'network_stopped':True,'rooms_stopped':False}
    def _enablement(self,unit,old,current):
        wanted='disabled'if old['LoadState']=='not-found'else old['UnitFileState']
        if current['UnitFileState']==wanted:return
        if wanted not in ('enabled','disabled')or current['UnitFileState']not in ('enabled','disabled'):
            raise ConfigError('repair_helper_enablement_requires_review')
        self.ops.systemctl('enable'if wanted=='enabled'else 'disable',unit)
        if _valid(self.ops.state(unit),timer=unit.endswith('.timer'))['UnitFileState']!=wanted:
            raise ConfigError('repair_helper_enablement_requires_review')
    def restore(self,snapshot,*,allow_reconciliation=True):
        self.validate(snapshot)
        if type(allow_reconciliation)is not bool:raise ConfigError('repair_helper_restore_refused')
        restored=[];deferred=[]
        # Network is independent; restore only its previous active preference.
        network='poolrotater-v7-network-'+self.instance+'.service'
        old=snapshot['network'];current=_valid(self.ops.state(network))
        if current['LoadState']!='not-found':self._enablement(network,old,current)
        elif old['LoadState']!='not-found':raise ConfigError('repair_helper_disappeared')
        if old['ActiveState']in ('active','activating'):
            if current['LoadState']=='not-found':raise ConfigError('repair_helper_disappeared')
            self.ops.systemctl('start',network);restored.append('network')
        elif current['MainPID'] or current['ActiveState']not in ('inactive','failed'):
            self.ops.systemctl('stop',network)
        for key,name in self.units.items():
            unit=name+'.timer';old=snapshot['timers'][key]
            current=_valid(self.ops.state(unit),timer=True)
            if current['LoadState']=='not-found':
                if old['LoadState']!='not-found':raise ConfigError('repair_helper_disappeared')
                continue
            self._enablement(unit,old,current)
            active=old['ActiveState']in ('active','activating')
            if active and (key!='managed'or allow_reconciliation):
                self.ops.systemctl('start',unit);restored.append(key)
            else:
                self.ops.systemctl('stop',unit)
                if active and key=='managed':deferred.append(key)
        return {'restored':restored,'restoration_deferred':deferred,'rooms_started':False}
