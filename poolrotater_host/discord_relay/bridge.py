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


class CommunityDiscord(DiscordControl):
    def __init__(self,cfg):
        super().__init__(cfg,start=False);self.routes={};self.route_lock=threading.Lock()

    def _channel_ids(self):
        with self.route_lock:return set(self.routes)

    def command_targets(self,content,channel_id):
        with self.route_lock:known=channel_id in self.routes
        if not known or not content.startswith('.') or len(content)>1000:return []
        return [(self,content)]

    def _role_allows_command(self,role,content):
        parts=content[1:].split(maxsplit=1) if content.startswith('.') else []
        return bool(parts and parts[0].casefold() in COMMANDS and super()._role_allows_command(role,content))


def card(room,slot,feed=None,*,now=None):
    now=time.time() if now is None else now
    display=room.get('display') or {};settings=display.get('settings') or {}
    fresh=room.get('freshness')=='fresh';current=room.get('current_map') or {}
    name=channel_name(slot,room['preset'],operator=room['operator'],settings=settings)
    details=[]
    if settings:
        details.append(('Room settings',f"Freemod: {'on' if settings.get('freestyle') else 'off'} · Style: {settings.get('selection_style','variety')} · Countdown: {settings.get('countdown_seconds',0):g}s"))
    image=''
    # Public website projection supplies central qualified metadata/timing.
    if isinstance(feed,dict) and feed.get('room_id')==room['osu_room_id'] and feed.get('fresh_until',0)>now:
        m=feed.get('current_map') or {};u=m.get('cover_url','')
        if re.fullmatch(r'https://assets\.ppy\.sh/beatmaps/[1-9][0-9]*/covers/cover\.jpg',u):image=u
        playback=feed.get('playback')
        if fresh and room['state']=='playing' and isinstance(playback,dict) and playback.get('beatmap_id')==current.get('beatmap_id'):
            duration=playback.get('duration_seconds');start=playback.get('started_at')
            if type(duration)in(int,float) and type(start)in(int,float) and duration>0 and 0<=now-start<=duration+30:
                elapsed=max(0,min(duration,now-start));details.append(('Map progress',f'{int(elapsed)//60}:{int(elapsed)%60:02d} / {int(duration)//60}:{int(duration)%60:02d} · approximately {max(0,int(duration-elapsed))}s left'))
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
            require(slot not in proposed or proposed[slot]==channel,'discord_slot_conflict')
            require(channel not in proposed.values() or proposed.get(slot)==channel,'discord_channel_conflict')
            proposed[slot]=channel
        self.channels=proposed

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
        slots=self.repo.assign(community,self.channels,owner_host=self.owner_host)
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
            if last is None or row['channel'] is None:continue
            channel=int(row['channel']);require(channel not in self.reserved,'owner_channel_conflict')
            if room is None:last={**last,'freshness':'stale','state':'unavailable','occupants':None,'current_map':None}
            else:last=room
            if room and room['freshness']=='fresh' and room['age_seconds']<120:routes[channel]=key
            values=card(last,row['slot'],feed.get(last['osu_room_id']),now=now)
            values['channel_id']=channel
            # Progress is refreshed every20s, unchanged idle cards are coalesced.
            signature=json.dumps(values,sort_keys=True)
            if self.last_cards.get(channel)!=signature:
                if self.discord.update_public_status(**values):self.last_cards[channel]=signature
        with self.discord.route_lock:self.discord.routes=routes
        for _ in range(32):
            try:cmd=self.discord.get_command_nowait()
            except queue.Empty:break
            key=routes.get(cmd.channel_id)
            if key is None:self.discord.send_to_channel('This room is unavailable; no command was sent.',cmd.channel_id);continue
            try:
                self.repo.queue(*key,{'discord_user_id':cmd.discord_user_id,'username':cmd.username[:80],'role':cmd.role},channel_id=cmd.channel_id,content=cmd.content)
            except Exception:
                self.discord.send_to_channel('Command not sent: the host is updating, disconnected, or not yet on the Discord relay release.',cmd.channel_id)
        for reply in self.repo.take_replies()+self.repo.take_notices():self.discord.send_to_channel(reply['content'],int(reply['channel']))
        return {'ok':True,'assigned':len(slots),'available_channels':len(self.channels),'routable':len(routes),'waiting':len(set(current)-{(s['host'],s['alias'])for s in slots})}

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
                except Exception:log.warning('Discord bridge refresh delayed; gameplay unaffected')
        finally:self.discord.stop()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--config',required=True);args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(name)s %(levelname)s %(message)s')
    cfg=json.loads(Path(args.config).read_bytes());bridge=Bridge(Store(Path(args.root)),cfg)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:bridge.stop.set())
    bridge.run()


if __name__=='__main__':main()
