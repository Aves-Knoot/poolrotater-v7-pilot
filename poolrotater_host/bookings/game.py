"""Session-only native worker controls; permanent moderation remains native."""
import copy
import time
import uuid
from pathlib import Path
from ..network.contracts import NetworkError, require
from .local import LocalSessions
from .settings import validate_player_patch

WARNINGS={
 'claim_required':'Private booking: the requester or designated friend must join within one minute.',
 'inactivity_warning':'Private booking: this room will close soon without gameplay.'}
READ_COMMANDS=frozenset(('help','help2','commands','info','queue','q','now','np','time','t','who',
 'stats','good','g','bad','b','rating','ratings','discord','website','hidden','show','lang','language',
 'skip','s','easier','harder','replay','r'))


def local(bot, *, allow_cleanup=False):
    value=bot.cfg.get('_booking_session')
    require(type(value)is dict and set(value)=={'id','alias','session_generation','preset'},'booking_binding_refused')
    from .policy import native_binding
    root=Path(bot.cfg['_host_root']);store=LocalSessions(root,native_binding(root))
    row=store.get(value['id']);lease=row['lease']
    require(all(value[k]==lease[k]for k in ('id','alias','session_generation','preset')),
            'booking_binding_refused',409)
    require((lease['host_id'],lease['installation_id'],lease['generation']) ==
            (store.binding['host_id'],store.binding['installation_id'],store.binding['generation']),
            'booking_binding_refused',409)
    require(allow_cleanup or not row['cleanup_complete'],'booking_closed',409)
    return store,row


def command(bot,user_id,cmd,parts,*,privileged=False):
    """Return True when booking policy handled/refused this dot command."""
    if not bot.cfg.get('_booking_session') or privileged:return False
    try:
        store,row=local(bot);id=row['lease']['id'];role=row['roles'].get(user_id,'player')
        actor=role in ('owner','controller')
        if cmd in ('start','abort','forceskip','skip','s','end','endbooking') and actor:
            action={'forceskip':'skip','s':'skip','endbooking':'end'}.get(cmd,cmd)
            require(len(parts)==1,'invalid_action_payload')
            result=store.request_local(id,user_id,str(uuid.uuid4()),action,{})
            bot.say('Private lobby control queued.' if result['phase']in ('pending','dispatched')else'Private lobby control refused.')
            return True
        patch=None
        if actor and cmd in ('range','sr'):
            require(len(parts)==3,'booking_settings_refused')
            patch={'sr_min':float(parts[1]),'sr_max':float(parts[2]),'auto_difficulty':False}
        elif actor and cmd in ('easier','harder'):
            require(len(parts)==1,'booking_settings_refused');delta=-0.2 if cmd=='easier'else 0.2
            patch={'sr_min':max(0,bot.pool.sr_min+delta),'sr_max':max(0.1,bot.pool.sr_max+delta)}
        elif actor and cmd in ('dt','nm'):
            require(len(parts)<=2,'booking_settings_refused')
            patch={'dt_mode':'off'if cmd=='nm'else parts[1].lower()if len(parts)==2 else'mixed'}
        elif actor and cmd in ('variety','farm','stream','jump','jack','chordjack','ln','technical','rice','noodle','style'):
            style=parts[1].lower()if cmd=='style'and len(parts)==2 else cmd
            require(len(parts)<=2,'booking_settings_refused')
            patch={'selection_styles':[style]}
        elif actor and cmd in ('countdown','autostart','minplayers','length'):
            require(len(parts)==2,'booking_settings_refused')
            key={'countdown':'countdown_seconds','autostart':'auto_start_seconds','minplayers':'min_players','length':'real_max_length'}[cmd]
            patch={key:int(parts[1])}
        if patch is not None:
            validate_player_patch(patch,current=row['settings'],preset=row['lease']['preset'])
            store.request_local(id,user_id,str(uuid.uuid4()),'configure',patch)
            bot.say('Private lobby settings queued; existing maps stay in the queue.')
            return True
        if not actor and not bot.cfg.get('user_commands_enabled',True) and cmd not in ('hidden','show','lang','language'):return True
        if cmd in READ_COMMANDS:return False
        bot.say('This command is unavailable in a private booking. The owner and controllers can use the website settings panel.')
        return True
    except (NetworkError,ValueError,TypeError,KeyError):
        bot.say('Private lobby control could not be applied. Check your session role, settings and cooldown.')
        return True


def execute(bot,operation_id,materialize):
    store,row=local(bot);booking_id=row['lease']['id']
    command=next((c for c in store.pending(booking_id)if c['operation_id']==operation_id),None)
    if command is None:return False
    action=command['action'];uid=command['user_id']
    require(store.authorize(booking_id,uid,action),'booking_forbidden',403)
    application={'application':'live'};effective=None
    configure_commit_attempted=False
    try:
        if action=='configure':
            from .worker import apply_booking_settings
            effective={**row['settings'],**command['payload']}
            def commit_settings(committed, receipt):
                nonlocal configure_commit_attempted
                configure_commit_attempted=True
                current=next((item for item in store.pending(booking_id)
                              if item['operation_id']==operation_id),None)
                if current is None or not store.authorize(booking_id,uid,'configure'):
                    return False
                result=store.acknowledge(operation_id,'applied',application=receipt,
                                         effective_settings=committed)
                return result.get('phase')=='applied'

            apply_booking_settings(bot,effective,row['lease']['preset'],
                                   materialize=materialize,before_commit=commit_settings)
            # The callback durably acknowledges the settings before the
            # candidate pool and worker config become visible.
            return True
        elif action=='start':bot._start_from_command(uid,privileged=True)
        elif action=='skip':bot.force_skip(uid)
        elif action=='abort':
            bot._explicit_abort_pending=True
            bot.hub.abort_match(bot.room_id)
        elif action in ('grant','revoke'):
            pass  # The durable ledger rechecks the roster and commits the role.
        else:raise NetworkError('booking_action_refused')
        store.acknowledge(operation_id,'applied',application=application,effective_settings=effective)
        return True
    except Exception:
        if action=='configure':
            if configure_commit_attempted:
                # The callback already found or attempted a durable terminal
                # receipt. Never replace a cancellation/applied result with a
                # second acknowledgement after a build or ledger race.
                return False
            try:
                current=next((item for item in store.pending(booking_id)
                              if item['operation_id']==operation_id),None)
                receipt=next((item for item in store.get(booking_id).get('operation_receipts',[])
                              if item.get('operation_id')==operation_id),None)
                if (receipt is not None and receipt.get('phase') in ('applied','failed','cancelled')):
                    return False
                if current is None or not store.authorize(booking_id,uid,'configure'):
                    return False
                store.acknowledge(operation_id,'failed',error='worker_action_outcome_unknown')
            except Exception:
                pass
            return False
        # Do not retry an uncertain gameplay write. Fixed error only, never tokens/body.
        try:store.acknowledge(operation_id,'failed',error='worker_action_outcome_unknown')
        except Exception:pass
        return False


def observation(bot):
    binding=bot.cfg.get('_booking_session')
    if not binding:return None
    players=[]
    for uid,info in bot.players.items():
        if type(uid)is not int or uid<=0 or type(info)is not dict:return None
        username=info.get('name')or str(uid)
        if type(username)is not str or not 0<len(username)<=64 or any(ord(c)<32 or ord(c)==127 for c in username):return None
        players.append({'user_id':uid,'username':username})
    known=(not bot._hub_rejoin_pending and
           getattr(bot,'_last_referee_roster_barrier',0)>=getattr(bot,'_required_referee_roster_barrier',0))
    return {'id':binding['id'],'session_generation':binding['session_generation'],
            'roster_known':known,'players':players,'match':copy.deepcopy(getattr(bot,'_booking_match',None))}


class BookingEventsMixin:
    def _update_discord_public_status(self, *, force=False, is_open=None,
                                      lifecycle=None, rename_channel=True):
        if not self.cfg.get('_booking_session'):
            return super()._update_discord_public_status(
                force=force, is_open=is_open, lifecycle=lifecycle,
                rename_channel=rename_channel)
        control=getattr(self,'discord_control',None)
        if control is None or not hasattr(control,'update_public_status'):
            return False
        from .presentation import purebooking_card
        state='unavailable';fresh=False
        try:
            _,row=local(self,allow_cleanup=True)
            phase=row['state']['phase']
            if phase=='ended':
                state='available' if row['cleanup_complete'] else 'closing'
            elif phase=='preparing':state='preparing'
            elif phase=='awaiting_claim':state='reserved'
            elif phase=='active':state='active'
            elif phase=='closing':state='closing'
            fresh=True
        except (NetworkError,ValueError,TypeError,KeyError):
            pass
        card=purebooking_card({'label':'Private lobby','state':state,'fresh':fresh})
        card['spectator_count']=0
        instance=str(self.cfg.get('_instance_name') or 'room1')
        channel_base=str(self.cfg.get('discord_public_status_channel_name') or
                         f'{instance}-status')
        display=str(self.cfg.get('discord_public_status_label') or instance.title())
        card.update(instance=instance,channel_base_name=channel_base,
                    display_name=display,rename_channel=bool(rename_channel))
        snapshot=tuple(sorted(card.items()))
        if not force and snapshot==getattr(self,'_last_booking_discord_status',None):
            return False
        queued=control.update_public_status(**card)
        if queued:self._last_booking_discord_status=snapshot
        return queued

    def _notify_discord_lobby_event(self, label, text, *, automatic=False, map_key=''):
        if self.cfg.get('_booking_session'):
            # Public lobby event posts can reveal private join IDs, maps, or
            # player activity. Operational activity collection remains separate.
            return None
        return super()._notify_discord_lobby_event(
            label,text,automatic=automatic,map_key=map_key)

    def handle_event(self,name,payload):
        before=getattr(self,'_match_started_at',0)
        prior=copy.deepcopy(getattr(self,'_booking_match',None))
        result=super().handle_event(name,payload)
        if not self.cfg.get('_booking_session'):return result
        if payload.get('room_id',self.room_id)!=self.room_id:return result
        now=time.time()
        item=payload.get('playlist_item_id')
        event_started=payload.get('started_at',payload.get('match_started_at'))
        item_matches=item is None or item==self.playing_item_id
        started_matches=(event_started is None or event_started==self._match_started_at)
        if (name=='MatchStarted'and self._match_started_at!=before and self.playing_map is not None
                and item_matches and started_matches and bool(getattr(self,'players',{}))):
            duration=float(self.playing_map.hit_length)
            if 0<duration<=86400:
                self._booking_match={'id':str(self.playing_item_id)+':'+str(self._match_started_at),
                    'started_at':self._match_started_at,'expected_end':self._match_started_at+duration,
                    'ended_at':None,'completed':False,'aborted':False}
        elif name in ('MatchCompleted','MatchAborted')and prior and prior['ended_at']is None:
            prior_item,_,prior_started=prior['id'].partition(':')
            if item is not None and str(item)!=prior_item:return result
            if event_started is not None and event_started!=prior['started_at']:return result
            # Only a completed provider match renews gameplay. Aborts never do.
            self._booking_match={**prior,'ended_at':now,'completed':name=='MatchCompleted','aborted':name=='MatchAborted'}
        return result
