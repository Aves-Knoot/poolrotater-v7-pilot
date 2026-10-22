"""One central Discord connection for community panels and room commands.

Runs as the hub identity, uses the existing guild permissions, and never holds
community OAuth credentials. Existing owner Discord transport keeps slots1–4.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import queue
import re
import signal
import threading
import time
import urllib.request
from bot.discord_control import DiscordControl
from ..network.store import Store
from ..network.contracts import require
from ..layout import atomic_json, read_json
from .repository import Repository
from .model import COMMANDS, channel_name

log=logging.getLogger('community_discord')

def is_fleet(content):
    parts=content.split(maxsplit=1)
    return bool(parts and parts[0].casefold()=='.fleet')


class CommunityDiscord(DiscordControl):
    def __init__(self,cfg):
        super().__init__(cfg,start=False);self.routes={};self.route_lock=threading.Lock();self.fleet_channel=0

    def _channel_ids(self):
        with self.route_lock:return set(self.routes)|({self.fleet_channel} if self.fleet_channel else set())

    def command_targets(self,content,channel_id):
        if is_fleet(content):
            return [(self,content)] if channel_id==self.fleet_channel and self.fleet_channel and len(content)<=1000 else []
        with self.route_lock:known=channel_id in self.routes
        if not known or not content.startswith('.') or len(content)>1000:return []
        return [(self,content)]

    def _role_allows_command(self,role,content):
        parts=content[1:].split(maxsplit=1) if content.startswith('.') else []
        if parts and parts[0].casefold()=='fleet':return bool(self.fleet_channel and role=='ADMIN')
        return bool(parts and parts[0].casefold() in COMMANDS and super()._role_allows_command(role,content))

    def _private_fleet_channel(self,channel):
        if int(getattr(channel,'id',0))!=self.fleet_channel:return True
        try:
            guild=channel.guild
            if not guild or int(guild.id)!=self._guild_id():return False
            staff=self._role_names(self.cfg.get('discord_control_admin_role_names',['Top dev']))
            staff|=self._role_names(self.cfg.get('discord_control_moderator_role_names',['mods']))
            roles=list(guild.roles)
            if not roles:return False
            own_bot_id=int(guild.me.id)
            for role in roles:
                # Discord's own managed bot role needs access too. Qualify its
                # platform binding, never a matching display name or another bot.
                own_bot_role=(getattr(role,'managed',False) is True
                              and getattr(getattr(role,'tags',None),'bot_id',None)==own_bot_id)
                if (channel.permissions_for(role).view_channel and not role.permissions.administrator
                        and str(role.name).casefold() not in staff and not own_bot_role):return False
            # Role checks do not include individual member overrides. Do not
            # infer staff membership from an incomplete member cache: use staff
            # roles for viewing grants, except the guild owner and this bot.
            role_ids={int(role.id) for role in roles}
            implicit_staff={int(guild.owner_id),int(guild.me.id)}
            for subject,permissions in channel.overwrites.items():
                if (permissions.view_channel is True and int(subject.id) not in role_ids
                        and int(subject.id) not in implicit_staff):return False
            return True
        except (AttributeError,TypeError,ValueError):return False

    def _channel_allows_command(self,channel):return self._private_fleet_channel(channel)
    def _channel_allows_outbound(self,channel):return self._private_fleet_channel(channel)


def card(room,slot,feed=None,*,now=None,booking=None,website_origin='https://poolrotater.com'):
    if booking is not None:
        from ..bookings.presentation import purebooking_card
        return purebooking_card(booking, website_origin)
    now=time.time() if now is None else now
    display=room.get('display') or {};settings=display.get('settings') or {}
    fresh=room.get('freshness')=='fresh';current=room.get('current_map') or {}
    name=channel_name(slot,room['preset'],operator=room['operator'],settings=settings)
    details=[]
    if settings:
        details.append(('Room settings',f"Freemod: {'on' if settings.get('freestyle') else 'off'} · Style: {settings.get('selection_style','variety')} · Countdown: {settings.get('countdown_seconds',0):g}s"))
    image=''
    # Reuse website artwork; playback progress belongs on the live website.
    if isinstance(feed,dict) and feed.get('room_id')==room['osu_room_id'] and feed.get('fresh_until',0)>now:
        m=feed.get('current_map') or {};u=m.get('cover_url','')
        if re.fullmatch(r'https://assets\.ppy\.sh/beatmaps/[1-9][0-9]*/covers/cover\.jpg',u):image=u
    lifecycle=room['state'].title() if fresh else 'Status delayed'
    return {'is_open':fresh,'lifecycle':lifecycle,'room_id':room['osu_room_id'] if fresh else 0,
            'room_name':room['title'],'player_count':room.get('occupants') or 0,
            'sr_min':room['min_stars'],'sr_max':room['max_stars'],
            'map_label':current.get('title',''),'map_url':'https://osu.ppy.sh/b/'+str(current['beatmap_id']) if current else '',
            'instance':f'community-slot-{slot}','channel_base_name':name,'display_name':f"{room['operator']} · {name.split('-',2)[-1]}",
            'image_url':image,'extra_fields':details}


class Bridge:
    def __init__(self,store,cfg,*,discord=None,clock=time.time):
        self.store=store;self.repo=Repository(store);self.cfg=cfg;self.clock=clock
        self.discord=discord or CommunityDiscord(cfg['discord']);self.channels={int(k):v for k,v in cfg['channels'].items()}
        require(all(5<=k<=9999 and type(v)is int and v>0 for k,v in self.channels.items())
                and len(set(self.channels.values()))==len(self.channels),'invalid_channel_configuration')
        self.owner_host=cfg['owner_host_id'];self.reserved=set(cfg['owner_channels'])
        require(not self.reserved.intersection(self.channels.values()),'owner_channel_conflict')
        fleet_channel=cfg.get('fleet_channel_id',0)
        require(type(fleet_channel) is int and 0<=fleet_channel<2**63,'invalid_channel_configuration')
        require(not fleet_channel or fleet_channel not in self.reserved|set(self.channels.values()),'fleet_channel_must_be_separate')
        self.discord.fleet_channel=fleet_channel
        from .fleet import Fleet
        self.fleet=Fleet(store,channel=fleet_channel,owner_host=self.owner_host) if fleet_channel else None
        self.next_discovery=0;self.last_rooms={};self.last_cards={};self.stop=threading.Event()
        self.observation_path=store.root/'discord-bridge-observations.json'
        if self.observation_path.exists():
            try:
                saved=read_json(self.observation_path,maximum=2*1024**2)
                require(saved.get('format')==1 and type(saved.get('rooms'))is list and len(saved['rooms'])<=2048)
                self.last_rooms={(r['host_id'],r['alias']):r for r in saved['rooms']}
            except Exception:log.warning('Previous Discord observations unavailable; waiting for fresh status')

    def discover(self):
        """GET only. Names are for discovery; durable routes use immutable IDs."""
        token=self.discord._token();guild=self.discord._guild_id()
        req=urllib.request.Request(f'https://discord.com/api/v10/guilds/{guild}/channels',
            headers={'Authorization':'Bot '+token,'User-Agent':'PoolRotater-community/1'})
        with urllib.request.urlopen(req,timeout=15) as response:
            raw=response.read(1024*1024+1);require(len(raw)<=1024*1024,'invalid_discord_channels')
            rows=json.loads(raw)
        require(type(rows)is list,'invalid_discord_channels')
        proposed=dict(self.channels)
        for row in rows:
            match=re.fullmatch(r'lobby[-_]([0-9]{1,4})[-_]community',row.get('name',''))
            if not match or row.get('type')!=0:continue
            slot=int(match[1]);channel=int(row['id'])
            if slot<5:continue
            require(channel not in self.reserved,'owner_channel_conflict')
            require(channel!=self.discord.fleet_channel,'fleet_channel_must_be_separate')
            require(slot not in proposed or proposed[slot]==channel,'discord_slot_conflict')
            require(channel not in proposed.values() or proposed.get(slot)==channel,'discord_channel_conflict')
            proposed[slot]=channel
        self.channels=proposed

    def _booking_assignments(self):
        """Read explicitly configured private slots without creating a ledger."""
        path=self.store.root/'bookings.sqlite3'
        if not os.path.lexists(path):return {}
        try:
            from ..bookings.eligibility import Eligibility
            from ..bookings.repository import Repository as BookingLedger
            ledger=BookingLedger(self.store.root,clock=self.clock,
                                 eligibility=Eligibility(self.store,clock=self.clock))
            assignments=ledger.public_assignments()
            return {(row['host_id'],row['alias']):row['slot'] for row in assignments
                    if row['host_id']!=self.owner_host}
        except Exception:
            # An unreadable opt-in ledger must not leave public room details or
            # command routes exposed while its private alias policy is unknown.
            log.warning('Private booking assignments unavailable; suppressing community room details')
            return None

    def step(self):
        now=self.clock()
        if now>=self.next_discovery:
            self.next_discovery=now+60
            try:self.discover()
            except Exception:log.warning('Discord channel discovery delayed; retained assignments unchanged')
        rooms=[];offset=0
        while True:
            page=self.store.directory(offset=offset,limit=100,protocol_version=2);rooms.extend(page['rooms'])
            offset=page['next_offset']
            if offset is None:break
        community=[r for r in rooms if r['host_id']!=self.owner_host]
        booking_assignments=self._booking_assignments()
        booking_identities=[] if booking_assignments is None else [
            {'host_id':host,'alias':alias} for host,alias in booking_assignments
            if not any(r['host_id']==host and r['alias']==alias for r in community)]
        slots=self.repo.assign([*community,*booking_identities],self.channels,owner_host=self.owner_host)
        current={(r['host_id'],r['alias']):r for r in community}
        # Retain the identity of offline hosts so the slot stays clearly labelled.
        self.last_rooms.update(current)
        atomic_json(self.observation_path,{"format":1,"rooms":list(self.last_rooms.values())})
        feed={}
        try:
            p=Path(self.cfg['feed']);require(not p.is_symlink() and p.stat().st_size<=2*1024**2)
            v=json.loads(p.read_bytes())
            if v.get('expires_at',0)>now:feed={r['room_id']:r for r in v.get('rooms',[])}
        except Exception:pass
        routes={}
        for row in slots:
            key=(row['host'],row['alias']);room=current.get(key);last=self.last_rooms.get(key)
            if row['channel'] is None:continue
            channel=int(row['channel']);require(channel not in self.reserved,'owner_channel_conflict')
            booking_slot=(booking_assignments or {}).get(key) if booking_assignments is not None else None
            if booking_slot is not None:
                values=card({},row['slot'],now=now,booking=booking_slot)
                values['channel_id']=channel
                signature=json.dumps(values,sort_keys=True)
                if self.last_cards.get(channel)!=signature:
                    if self.discord.update_public_status(**values):self.last_cards[channel]=signature
                continue
            if booking_assignments is None:
                values=card({},row['slot'],now=now,booking={'label':'Private lobby',
                             'state':'unavailable','fresh':False})
                values['channel_id']=channel
                signature=json.dumps(values,sort_keys=True)
                if self.last_cards.get(channel)!=signature:
                    if self.discord.update_public_status(**values):self.last_cards[channel]=signature
                continue
            if last is None:continue
            if room is None:last={**last,'freshness':'stale','state':'unavailable','occupants':None,'current_map':None}
            else:last=room
            if room and room['freshness']=='fresh' and room['age_seconds']<120:routes[channel]=key
            values=card(last,row['slot'],feed.get(last['osu_room_id']),now=now)
            values['channel_id']=channel
            # Publish changed panels only; unchanged cards are coalesced.
            signature=json.dumps(values,sort_keys=True)
            if self.last_cards.get(channel)!=signature:
                if self.discord.update_public_status(**values):self.last_cards[channel]=signature
        with self.discord.route_lock:self.discord.routes=routes
        for _ in range(32):
            try:cmd=self.discord.get_command_nowait()
            except queue.Empty:break
            if is_fleet(cmd.content):
                if self.fleet is not None:
                    for message in self.fleet.handle(cmd):self.discord.send_to_channel(message,self.fleet.channel)
                continue
            key=routes.get(cmd.channel_id)
            if key is None:self.discord.send_to_channel('This room is unavailable; no command was sent.',cmd.channel_id);continue
            try:
                self.repo.queue(*key,{'discord_user_id':cmd.discord_user_id,'username':cmd.username[:80],'role':cmd.role},channel_id=cmd.channel_id,content=cmd.content)
            except Exception:
                self.discord.send_to_channel('Command not sent: the host is updating, disconnected, or not yet on the Discord relay release.',cmd.channel_id)
        for reply in self.repo.take_replies()+self.repo.take_notices():self.discord.send_to_channel(reply['content'],int(reply['channel']))
        self.fleet_notices()
        return {'ok':True,'assigned':len(slots),'available_channels':len(self.channels),'routable':len(routes),'waiting':len(set(current)-{(s['host'],s['alias'])for s in slots})}

    def fleet_notices(self):
        if self.fleet is None:return
        for value in self.fleet.notices():
            if self.discord.send_to_channel(value['content'],value['channel']):self.fleet.acknowledge_notice(value)

    def run(self):
        self.discord.start();next_panels=0
        try:
            while not self.stop.wait(1):
                try:
                    # Command delivery doesn't wait for the panel refresh interval.
                    if not self.discord.commands.empty() or self.clock()>=next_panels:
                        result=self.step();next_panels=self.clock()+20
                        atomic_json(self.store.root/'discord-bridge-health.json',{**result,'at':self.clock()})
                    else:
                        for reply in self.repo.take_replies()+self.repo.take_notices():self.discord.send_to_channel(reply['content'],int(reply['channel']))
                        self.fleet_notices()
                except Exception:log.warning('Discord bridge refresh delayed; gameplay unaffected')
        finally:self.discord.stop()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--config',required=True);args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(name)s %(levelname)s %(message)s')
    cfg=json.loads(Path(args.config).read_bytes());bridge=Bridge(Store(Path(args.root)),cfg)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:bridge.stop.set())
    bridge.run()


if __name__=='__main__':main()
