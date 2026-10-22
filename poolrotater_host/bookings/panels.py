"""Secret-free owner Discord status cards for explicitly designated slots."""
import logging
import os
from pathlib import Path
import re
import time
import uuid

from .policy import enabled_aliases
from .presentation import purebooking_card

log=logging.getLogger(__name__)
MAINTENANCE='state/rolling/supervisor-maintenance.json'
BOOKINGS='state/bookings.sqlite3'
COALESCE_SECONDS=20


def _native_closed(room, journal):
    if (type(room) is not dict or type(room.get('config')) is not dict or
            room['config'].get('visibility')!='private' or room.get('desired')!='stopped' or
            room.get('phase') not in ('stopped','unobserved') or room.get('room_id') is not None or
            room.get('room_generation') is not None or room.get('pending') is not None or
            room.get('failure') is not None):
        return False
    if journal is None:
        return room['phase']=='unobserved'
    return (type(journal) is dict and journal.get('operation')=='closed' and
            journal.get('ownership_unknown') is False and journal.get('room_id') is None and
            not any(journal.get(key) for key in
                    ('pid','creation_candidate','target','error')))


def _lease_state(row,binding,alias):
    if (type(row) is not dict or type(row.get('lease')) is not dict or
            type(row.get('state')) is not dict or type(row.get('cleanup_complete')) is not bool):
        return None
    lease=row['lease']
    try:valid_id=type(lease.get('id')) is str and str(uuid.UUID(lease['id']))==lease['id']
    except (ValueError,AttributeError):valid_id=False
    if (not valid_id or lease.get('alias')!=alias or
            type(lease.get('session_generation')) is not str or
            re.fullmatch(r'[0-9a-f]{32}',lease['session_generation']) is None or
            any(lease.get(key)!=binding.get(key) for key in ('host_id','installation_id','generation'))):
        return None
    phase=row['state'].get('phase')
    if phase=='preparing':return 'preparing'
    if phase=='awaiting_claim':return 'reserved'
    if phase=='active':return 'active'
    if phase in ('closing','ended') and not row['cleanup_complete']:return 'closing'
    if phase=='ended' and row['cleanup_complete']:return 'ended'
    return None


class Publisher:
    """Coalesce local booking status into the existing per-alias Discord views."""
    def __init__(self,owner,clock=time.monotonic):
        self.owner=owner
        self.clock=clock
        self.last_sent={}
        self.pending={}
        self.last_aliases=frozenset()

    def _sessions(self,state):
        path=Path(self.owner.runtime)/BOOKINGS
        if not path.exists():return None
        coordinator=getattr(self.owner,'bookings',None)
        sessions=getattr(coordinator,'sessions',None)
        if sessions is not None:return sessions
        from .local import LocalSessions
        return LocalSessions(self.owner.runtime,state.binding)

    def _cards(self,aliases):
        root=Path(self.owner.runtime)
        state_obj=getattr(getattr(self.owner,'managed',None),'state',None)
        if state_obj is None:
            raise ValueError('managed native state unavailable')
        limits=state_obj.limits()
        capacity=(type(limits) is dict and type(limits.get('effective_limit')) is int and
                  type(limits.get('capacity_used')) is int and
                  limits['effective_limit']>0 and
                  limits['capacity_used']<limits['effective_limit'])
        maintenance=os.path.lexists(root/MAINTENANCE)
        registry=self.owner.registry.snapshot().get('rooms',{})
        sessions=self._sessions(state_obj)
        rows=sessions.leases(include_ended=True) if sessions is not None else []
        by_alias={}
        for row in rows:
            lease=row.get('lease') if type(row) is dict else None
            if type(lease) is not dict or lease.get('alias') not in aliases:
                continue
            if lease['alias'] in by_alias:
                return {alias:'unavailable' for alias in aliases}
            by_alias[lease['alias']]=row
        result={}
        for alias in aliases:
            try:
                room=state_obj.room(alias,private=True)
                if type(room) is not dict or room.get('config',{}).get('visibility')!='private':
                    result[alias]='unavailable';continue
                row=by_alias.get(alias)
                lease_phase=_lease_state(row,state_obj.binding,alias) if row is not None else None
                if row is not None and lease_phase is None:
                    result[alias]='unavailable';continue
                if lease_phase in ('preparing','reserved','active','closing'):
                    result[alias]=lease_phase;continue
                is_closed=_native_closed(room,registry.get(alias))
                if is_closed and capacity and not maintenance:
                    result[alias]='available'
                else:
                    result[alias]='unavailable'
            except Exception:
                result[alias]='unavailable'
        return result

    def _publish(self,alias,state):
        view=self.owner.discord.view_for(alias)
        values=purebooking_card({'label':'Private lobby','state':state,'fresh':True})
        # The alias-specific view preserves its configured public channel,
        # owner label, and instance identity. Room data always remains empty.
        values['spectator_count']=0
        return bool(view.update_public_status(**values))

    def tick(self):
        try:
            aliases=enabled_aliases(self.owner.runtime)
        except Exception:
            aliases=self.last_aliases
            states={alias:'unavailable' for alias in aliases}
        else:
            self.last_aliases=aliases
            if not aliases:
                self.pending.clear()
                return
            try:
                states=self._cards(aliases)
            except Exception:
                states={alias:'unavailable' for alias in aliases}
        now=self.clock()
        configs=getattr(self.owner,'configs',{})
        for alias in sorted(aliases):
            cfg=configs.get(alias,{})
            try:
                channel=int(cfg.get('discord_public_status_channel_id',0) or 0)
            except (TypeError,ValueError):
                channel=0
            if channel<=0:
                continue
            state=states.get(alias,'unavailable')
            prior=self.last_sent.get(alias)
            if prior is not None and prior['state']==state:
                self.pending.pop(alias,None)
                continue
            if prior is not None and now-prior['at']<COALESCE_SECONDS:
                self.pending[alias]={'state':state}
                continue
            queued=False
            try:
                queued=self._publish(alias,state)
            except Exception:
                log.warning('private booking panel update unavailable for %s',alias)
            if queued:
                self.last_sent[alias]={'state':state,'at':now}
                self.pending.pop(alias,None)
