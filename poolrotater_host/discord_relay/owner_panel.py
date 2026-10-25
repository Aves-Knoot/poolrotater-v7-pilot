"""Owner presentation parity from a public local feed; never gameplay authority."""
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from .model import channel_name,room_kind

FEED=Path('/srv/poolrotator-wiki/public/status.json')


def presentation(cfg,runtime=None):
    """Only active broker-owned room fields can override presentation defaults."""
    result=dict(cfg)
    if (type(runtime) is dict and set(runtime)=={'ruleset_id','mania_keys','auto_difficulty','loved_mode'}
            and type(runtime['ruleset_id']) is int and runtime['ruleset_id'] in (0,1,2,3)
            and type(runtime['mania_keys']) is int and runtime['mania_keys'] in (4,7)
            and type(runtime['auto_difficulty']) is bool and type(runtime['loved_mode']) is bool):
        result.update(runtime)
    return result


def preset(cfg):
    return ('standard-loved' if cfg.get('ruleset_id',0)==0 and cfg.get('loved_mode') is True else
            ('taiko-auto' if cfg.get('auto_difficulty') else 'taiko-fixed') if cfg.get('ruleset_id')==1 else 'catch-fixed' if cfg.get('ruleset_id')==2 else
            'mania-4k' if cfg.get('ruleset_id')==3 and cfg.get('mania_keys')==4 else
            'mania-7k' if cfg.get('ruleset_id')==3 and cfg.get('mania_keys')==7 else 'standard-fixed')


def identity(cfg):
    alias=str(cfg.get('_instance_name',''));match=re.fullmatch('room([1-4])',alias)
    if Path(cfg.get('_host_root','')).name!='owner' or not match:return None
    slot=int(match[1]);kind=room_kind(preset(cfg),{'auto_difficulty':bool(cfg.get('auto_difficulty',False))})
    return channel_name(slot,preset(cfg),settings={'auto_difficulty':bool(cfg.get('auto_difficulty',False))}),f'Lobby {slot} · '+kind.replace('-', ' ').title()


def finite(value,minimum=0,maximum=86400):
    return type(value) in (int,float) and math.isfinite(value) and minimum<=value<=maximum


def decorate(cfg,values,*,path=FEED,now=None):
    """A missing/malformed/delayed display feed never interrupts a command RPC."""
    result={**values,'image_url':'','extra_fields':()};now=time.time()if now is None else now
    if identity(cfg)is None:return dict(values)
    details=[]
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb')as f:
            s=os.fstat(f.fileno())
            if not stat.S_ISREG(s.st_mode)or s.st_size>2*1024**2:return result
            raw=f.read(2*1024**2+1)
        feed=json.loads(raw)
        if (feed.get('available')is not True or not finite(feed.get('generated_at'),0,2**53)
            or not 0<=now-feed['generated_at']<120 or not finite(feed.get('expires_at'),0,2**53)
            or feed['expires_at']<=now or type(feed.get('rooms'))is not list):return result
        room=next((r for r in feed['rooms']if r.get('alias')==cfg['_instance_name']and r.get('room_id')==values.get('room_id')),None)
        if room is None or not values.get('is_open'):return result
        settings=room.get('settings')or{}
        freestyle=settings.get('freestyle')
        count=settings.get('countdown_seconds')
        if type(freestyle)is bool and finite(count,0,600):
            details.append(('Room settings',f"Freemod: {'on'if freestyle else'off'} · Countdown: {count:g}s · Minimum players: {settings.get('min_players',1)}"))
        current=room.get('current_map')or{};bid=current.get('beatmap_id');setid=current.get('beatmapset_id')
        # Prevent the previous map's artwork from decorating a new pick.
        match=re.fullmatch(r'https://osu\.ppy\.sh/(?:b/|beatmaps/)([1-9][0-9]*)',str(values.get('map_url','')))
        if not match or type(bid)is not int or int(match[1])!=bid:
            result['extra_fields']=tuple(details);return result
        if type(setid)is int and 0<setid<=10**7:
            result['image_url']=f'https://assets.ppy.sh/beatmaps/{setid}/covers/cover.jpg'
        queue=room.get('queue')
        if room.get('queue_known')is True and isinstance(queue,list)and queue and isinstance(queue[0],dict):
            label=queue[0].get('label')or queue[0].get('title')
            if isinstance(label,str)and label.strip():details.append(('Next map',label[:1024]))
        result['extra_fields']=tuple(details)
    except (OSError,ValueError,KeyError,TypeError,AttributeError):pass
    return result
