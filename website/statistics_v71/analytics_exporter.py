#!/usr/bin/env python3
"""Read-only bot analytics. Publish allowlisted aggregates, never raw logs/chat."""
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
import ast, json, math, os, re, sqlite3, subprocess, time
from contextlib import contextmanager
import sys
sys.path.insert(0, "/usr/local/lib/poolrotator-ops")
try:
    from sqlite_readonly import readonly_database
except ModuleNotFoundError:
    from deployment.sqlite_readonly import readonly_database

# Mutable observations/config/catalogs are independent of the active release.
from owner_source import selected
from network_statistics import compose as compose_network_statistics, read_snapshot as read_network_statistics
SOURCE = selected()
APP = SOURCE.root
OPS = Path('/var/lib/poolrotator-ops/snapshots')
DB = SOURCE.ledger
PUBLIC = Path('/srv/poolrotator-wiki/public')
STATE = Path('/var/lib/poolrotator-wiki-analytics')
TTL = 600
LABELS = ['Standard automatic', 'Standard fixed', 'Mania 7K', 'Mania 4K']
TYPES = ['standard', 'standard', 'mania_7k', 'mania_4k']
GUIDES = ['standard_auto','standard_fixed','mania_7k','mania_4k']
LOG_PREFIX = r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[room[1-4]\] '
PERIODS = {'all': 0, '7d': 7*86400, '14d': 14*86400, '30d': 30*86400}
SCOPES = ['all', 'standard', 'mania_7k', 'mania_4k']

def number(value, default=0):
    try:
        n=float(value)
        return n if math.isfinite(n) else default
    except (ValueError, TypeError): return default

def clean(value, limit=240):
    return re.sub(r'[\x00-\x1f\x7f]', '', str(value or ''))[:limit]

def epoch(value):
    try: return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (ValueError, TypeError): return 0

def read_json(path, default=None):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return default

def runtime_rooms(now, service):
    """Prefer version-pinned worker observations; never infer health from a PID."""
    path=APP/'state/rolling/public-status.json'
    if not path.exists():return None  # Legacy service fallback only.
    data=read_json(path,{})
    if not isinstance(data,dict):data={}
    fresh=-5<=now-number(data.get('generated_at'))<=20
    rows=data.get('rooms') if isinstance(data.get('rooms'),dict) else {}
    result={}
    def public_map(value):
        if not isinstance(value,dict) or type(value.get('beatmap_id')) is not int or value['beatmap_id']<=0:return None
        return dict(beatmap_id=value['beatmap_id'],label=clean(value.get('label')),stars=number(value.get('stars'),None),hit_length=number(value.get('hit_length'),None))
    for i in range(4):
        alias='room'+str(i+1);row=rows.get(alias,{})
        if not isinstance(row,dict):row={}
        valid=bool(service.get('online') and fresh and row.get('healthy') is True and
                   type(row.get('room_id')) is int and row['room_id']>0 and
                   row.get('state') in ('IDLE','STARTING','PLAYING','SCORING') and
                   -5<=now-number(row.get('observed_at'))<=20 and
                   type(row.get('players')) is int and 0<=row['players']<=512)
        offline=service.get('online') is False or (fresh and row.get('state')=='OFFLINE')
        result[alias]=dict(valid=valid,room_id=row.get('room_id') if valid else None,
            state=row['state'] if valid else 'OFFLINE' if offline else 'STALE',
            players=row['players'] if valid else None,observed_at=row.get('observed_at') if valid else None,
            current_map=public_map(row.get('current_map')) if valid else None,
            queue=[m for v in (row.get('queue') if isinstance(row.get('queue'),list) else [])[:8] if (m:=public_map(v))] if valid else [],
            queue_known=bool(valid and row.get('queue_known')),match_started_at=number(row.get('match_started_at'),None) if valid else None)
    return result

def atomic(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=True, separators=(',',':'), allow_nan=False)+'\n')
    tmp.chmod(mode); os.replace(tmp,path)

@contextmanager
def readonly(path):
    with readonly_database(path, timeout=2) as conn:
        conn.row_factory=sqlite3.Row
        yield conn

@contextmanager
def snapshot_db(path):
    # Copy a consistent ledger into private memory, then release the live DB
    # before aggregating. Long Python work must never hold a rollback-journal
    # read lock that could delay the bot's score/visibility writes.
    if os.environ.get('POOLROTATOR_WIKI_REQUIRE_READONLY')=='1' and not os.statvfs(path).f_flag & os.ST_RDONLY:
        raise OSError('Ledger must be mounted read-only')
    target=sqlite3.connect(':memory:')
    try:
        with readonly_database(path, timeout=2) as source:
            source.backup(target,pages=128,sleep=0.01)
        target.row_factory=sqlite3.Row
        target.execute('PRAGMA query_only=ON')
        yield target
    finally:
        target.close()

def service_status(now):
    result=subprocess.run(['systemctl','show',SOURCE.service,'--property=ActiveState,SubState,NRestarts,ExecMainStartTimestampMonotonic'],
                          capture_output=True,text=True,timeout=4,check=True)
    props=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    online=props.get('ActiveState')=='active' and props.get('SubState')=='running'
    start=number(props.get('ExecMainStartTimestampMonotonic'))/1e6
    uptime=max(0,number(Path('/proc/uptime').read_text().split()[0])-start) if online and start else 0
    return {'online':online,'uptime_seconds':round(uptime),'started_at':round(now-uptime) if uptime else None,
            'restarts':int(number(props.get('NRestarts')))}

def room_type(row):
    title=(str(row['initial_name'] or '')+' '+str(row['last_name'] or '')).upper()
    if row['ruleset_id']==3 or re.search(r'\b[47]K\b',title):
        if re.search(r'\b7K\b',title):return 'mania_7k'
        if re.search(r'\b4K\b',title):return 'mania_4k'
        return 'mania_other'
    # This is the same legacy default used by the bot's PP statistics.
    return 'standard'

def log_timestamp(line):
    try:return time.mktime(time.strptime(line[:19],'%Y-%m-%d %H:%M:%S'))
    except (ValueError,OverflowError):return 0

def playlist(lines, map_by_id, map_by_label, expected_room=None):
    """Only public playlist operations/labels. Never parse chat/MakeRoom payloads.

    A queued label or successful operation confirms a future map. Promotion
    consumes the matching future entry. Ambiguous removals hide the queue.
    Freeze the pre-countdown map because lazer can promote before MatchStarted.
    """
    current=None; frozen=None; future=[]; pending={}; current_room=expected_room
    state=None; observed=0; queue_known=False
    def map_info(bid, label=None):
        info=dict(map_by_id.get(bid, {'beatmap_id':bid,'label':'Map #'+str(bid)}))
        if label:info['label']=clean(label)
        return info
    for line in lines:
        ts=log_timestamp(line)
        if not ts:continue
        observed=max(observed,ts)
        created=re.search(LOG_PREFIX+r'bot INFO room (\d+) created\b',line)
        if created:
            current_room=int(created[1]); current=None; frozen=None; future=[];pending={};queue_known=False
        request=re.search(LOG_PREFIX+r'hub DEBUG -> (AddPlaylistItem|EditCurrentPlaylistItem|RemovePlaylistItem|StartMatch|CloseRoom) (\[.*\])$',line)
        if request:
            try:
                if len(request[2])>8000:continue
                args=ast.literal_eval(request[2]); room_id=args[0]
                if current_room and room_id!=current_room:continue
                method=request[1];payload=args[1] if len(args)>1 and isinstance(args[1],dict) else {}
                if method in ('AddPlaylistItem','EditCurrentPlaylistItem'):
                    bid=payload.get('beatmap_id')
                    if type(bid) is int and bid>0:pending[method]=map_info(bid)
                elif method=='RemovePlaylistItem':pending[method]=True
                elif method=='StartMatch':pending[method]=dict(current) if current else None
                elif method=='CloseRoom':pending[method]=True
            except (ValueError,SyntaxError,IndexError,TypeError):pass
        response=re.search(LOG_PREFIX+r'hub DEBUG <- (AddPlaylistItem|EditCurrentPlaylistItem|RemovePlaylistItem|StartMatch|CloseRoom) ',line)
        if response:
            method=response[1]; value=pending.pop(method,None)
            if method=='AddPlaylistItem' and value:
                future.append(value);queue_known=True
            elif method=='EditCurrentPlaylistItem' and value:current=value
            elif method=='RemovePlaylistItem' and value:
                # Current configuration has one future entry. For a deeper
                # queue an unlogged item-to-map mapping is not safe to guess.
                queue_known=len(future)<=1;future=[]
            elif method=='StartMatch':frozen=value
            elif method=='CloseRoom' and value:current=None;frozen=None;future=[];queue_known=False
        queued=re.search(LOG_PREFIX+r'bot DEBUG queued ahead silently: (.*)$',line)
        if queued and future:future[-1]['label']=clean(queued[1])
        corrected=re.search(LOG_PREFIX+r'bot (?:INFO|WARNING) playlist (?:current corrected|audit repaired current item).*?: (?:item )?\S+ -> (\d+) \((.*)\)$',line)
        if corrected:
            label=corrected[2];match=next((i for i,m in enumerate(future) if m['label']==label),None)
            if match is not None:current=future[match];future=future[match+1:];queue_known=True
            else:
                current=dict(map_by_label.get(label, {'beatmap_id':None,'label':clean(label)}))
                # The log supplied no map-to-item proof for remaining entries.
                future=[];queue_known=False
        transition=re.search(LOG_PREFIX+r'bot INFO state (?:IDLE|STARTING|PLAYING|SCORING) -> (IDLE|STARTING|PLAYING|SCORING)',line)
        if transition:
            state=transition[1]
            if state in ('IDLE','SCORING'):frozen=None
    shown=frozen if state in ('STARTING','PLAYING') and frozen else current
    upcoming=list(future)
    if frozen and current and current.get('beatmap_id')!=frozen.get('beatmap_id') and state in ('STARTING','PLAYING'):
        upcoming.insert(0,current)
    return {'room_id':current_room,'state':state,'current_map':shown,'queue':upcoming[:8] if queue_known else [],
            'queue_known':queue_known,'observed_at':observed}

def tail_lines(path, limit=1500000):
    try:
        with path.open('rb') as f:
            size=path.stat().st_size; offset=max(0,size-limit);f.seek(offset);data=f.read(limit)
        lines=data.decode('utf8',errors='replace').splitlines()
        return lines[1:] if offset else lines
    except OSError:return []

def public_settings(config, universal, room):
    cfg={**universal, **config, **(config.get('mania_settings',{}) if isinstance(config.get('mania_settings'),dict) else {})}
    mania=config.get('mania_settings',{}) if isinstance(config.get('mania_settings'),dict) else {}
    lo=number(mania.get('sr_min',cfg.get('sr_min')),None); hi=number(mania.get('sr_max',cfg.get('sr_max')),None)
    title=room['last_name'] if room else ''
    match=re.search(r'\((\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\*\)',str(title or ''))
    result={'difficulty':'Automatic' if cfg.get('auto_difficulty',False) else 'Fixed with player votes',
            'configured_range':[lo,hi], 'active_range':[float(match[1]),float(match[2])] if match else None}
    for key in ['queue_ahead','countdown_seconds','auto_start_seconds','min_players','max_length']:
        value=mania.get(key,cfg.get(key))
        if type(value) in (int,float) and math.isfinite(value) and 0<=value<100000:result[key]=value
    for key in ['freestyle','voteskip_enabled','user_commands_enabled','mania_farm_enabled','mania_style_fallback_enabled']:
        if isinstance(cfg.get(key),bool):result[key]=cfg[key]
    for key in ['required_mods','allowed_mods']:
        if isinstance(cfg.get(key),list):result[key]=[m for m in cfg[key] if isinstance(m,str) and re.fullmatch(r'[A-Z0-9]{1,6}',m)][:16]
    return result

def enrich_map(info, mode, cache):
    if not info or not str(info.get('label','')).startswith('Map #'):return info
    bid=info.get('beatmap_id')
    if type(bid) is not int or bid<=0:return info
    key=(bid,mode)
    if key not in cache:
        catalogs=[('farm.db','beatmaps'),('loved.db','beatmaps')] if mode=='standard' else [('mania_7k.db','maps'),('mania.db','maps')]
        if mode=='all':catalogs=[('farm.db','beatmaps'),('loved.db','beatmaps'),('mania_7k.db','maps'),('mania.db','maps')]
        cache[key]=None
        for filename,table in catalogs:
            try:
                with readonly(SOURCE.catalog(filename)) as conn:
                    row=conn.execute('SELECT artist,title,version,sr FROM '+table+' WHERE beatmap_id=? LIMIT 1',(bid,)).fetchone()
                if row and row['title']:
                    sr=number(row['sr'],None)
                    label=clean(row['artist'])+' - '+clean(row['title'])+' ['+clean(row['version'])+']'
                    if sr is not None:label+=' ('+format(sr,'.2f')+'*)'
                    cache[key]={'label':clean(label),'stars':sr};break
            except (OSError,ValueError,sqlite3.Error):continue
    return {**info,**(cache[key] or {})}

def empty_player(uid):
    return {'user_id':uid,'username':'Player #'+str(uid),'seconds':0,'maps':0,'scores':0,'pp':0,
            'unique':set(),'days':set(),'accuracy_sum':0,'accuracy_n':0,'best_gain':None,'best_score':None,
            'top_plays':0,'visits':0,'time_by_type':{}}

def metrics(conn, rooms, now, active_ids, network_snapshot=None, views_out=None):
    # A single read transaction includes privacy and stats consistently. A
    # failed privacy query aborts named publication rather than treating it public.
    hidden={r[0] for r in conn.execute('SELECT user_id FROM autohost_stats_privacy WHERE hidden!=0')}
    groups={mode:{period:defaultdict(empty_player) for period in PERIODS} for mode in SCOPES}
    names={};name_dates={};first=now
    def player(mode,period,uid):
        group=groups[mode][period]
        if uid not in group:group[uid]=empty_player(uid)
        return group[uid]
    def name(uid,value,at):
        if value and at>=name_dates.get(uid,0):names[uid]=clean(value,64);name_dates[uid]=at
    def targets(mode,at):
        for scope in (['all',mode] if mode in SCOPES and mode!='all' else ['all']):
            for period,seconds in PERIODS.items():
                if seconds==0 or at>=now-seconds:yield scope,period
    current_live=defaultdict(lambda:defaultdict(float))
    room_time=defaultdict(lambda:defaultdict(float))
    for row in conn.execute('SELECT * FROM autohost_room_players'):
        uid=row['user_id'];room=rooms.get(row['room_id']);mode=room['type'] if room else 'standard'
        seconds=max(0,number(row['total_seconds']))
        if row['room_id'] in active_ids and number(row['last_join_at'])>number(row['last_leave_at']):
            live=max(0,now-number(row['last_join_at']));seconds+=live;current_live[uid][mode]+=live
        room_time[uid][mode]+=seconds
    mania_totals={}
    for table,mode in [('autohost_lobby_time','standard'),('autohost_lobby_time_mania','mania')]:
        for row in conn.execute('SELECT * FROM '+table):
            uid=row['user_id'];name(uid,row['username'],number(row['updated_at']));first=min(first,number(row['first_join_at'],now))
            live=sum(v for m,v in current_live[uid].items() if (m=='standard')==(mode=='standard'))
            seconds=max(0,number(row['total_seconds']))+live
            p=player('all','all',uid);p['seconds']+=seconds;p['visits']+=int(number(row['visit_count']));p['time_by_type'][mode]=round(seconds,1)
            if mode=='standard':q=player('standard','all',uid);q['seconds']=seconds;q['visits']=int(number(row['visit_count']))
            else:mania_totals[uid]=seconds
    for uid,types in room_time.items():
        for mode in ['mania_7k','mania_4k']:
            if types[mode]>0:
                p=player(mode,'all',uid);p['seconds']=types[mode]
                player('all','all',uid)['time_by_type'][mode]=round(types[mode],1)
    maps={};labels={}
    for row in conn.execute('SELECT * FROM autohost_matches ORDER BY started_at'):
        bid=row['beatmap_id']; info={'beatmap_id':bid,'label':clean(row['map_label']) or 'Map #'+str(bid),'stars':number(row['star_rating'],None)}
        maps[bid]=info;labels[info['label']]=info
    for row in conn.execute('SELECT * FROM autohost_match_players'):
        uid=row['user_id'];at=number(row['created_at']);name(uid,row['username'],at);first=min(first,at)
        mode=rooms.get(row['room_id'],{}).get('type','standard')
        for scope,period in targets(mode,at):
            p=player(scope,period,uid);p['maps']+=1;p['scores']+=int(bool(row['score_submitted']));p['unique'].add(row['beatmap_id']);p['days'].add(int(at//86400))
            if row['score_submitted'] and row['accuracy'] is not None:
                acc=number(row['accuracy'],-1)
                if 0<=acc<=100:p['accuracy_sum']+=acc;p['accuracy_n']+=1
    for table,fallback_mode in [('autohost_pp_achievements','standard'),('autohost_pp_achievements_mania','mania_other')]:
        for row in conn.execute('SELECT * FROM '+table):
            uid=row['user_id'];at=number(row['created_at']);name(uid,row['username'],at);first=min(first,at)
            mode=rooms.get(row['room_id'],{}).get('type',fallback_mode)
            achievement={'beatmap_id':row['beatmap_id'],'label':maps.get(row['beatmap_id'],{}).get('label','Map #'+str(row['beatmap_id'])),
                         'gain':round(max(0,number(row['profile_gain_pp'])),2),'score_pp':round(max(0,number(row['score_pp'])),2),
                         'accuracy':number(row['accuracy'],None),'top_rank':int(number(row['top_rank'])),'at':at}
            for scope,period in targets(mode,at):
                p=player(scope,period,uid);p['pp']+=max(0,number(row['profile_gain_pp']));p['top_plays']+=1
                if not p['best_gain'] or achievement['gain']>p['best_gain']['gain']:p['best_gain']=achievement
                if not p['best_score'] or achievement['score_pp']>p['best_score']['score_pp']:p['best_score']=achievement
    output, views = compose_network_statistics(groups,names,hidden,network_snapshot,set(rooms),now)
    if views_out is not None: views_out.update(views)
    return output,maps,labels,first

def history_seed():
    rows=[]
    for path in sorted(OPS.glob('health-*.json')):
        item=read_json(path,{})
        if not isinstance(item,dict):continue
        t=epoch(item.get('generated_at'));service=item.get('system',{}).get('service',{})
        if not t:continue
        online=(service.get('active_state')=='active' and service.get('sub_state')=='running') if service.get('active_state') in ['active','inactive','failed','activating','deactivating'] and service.get('sub_state') else None
        by={r.get('alias'):r for r in item.get('rooms',[]) if isinstance(r,dict)}
        counts=[]
        for i in range(4):
            value=by.get('room'+str(i+1),{}).get('occupants')
            counts.append(value if online and type(value) is int and 0<=value<=100 else None)
        rows.append([round(t),*counts,1 if online else 0 if online is False else None])
    return rows

def room_observation_valid(candidate, active_ids, observed_at, observed_now):
    # The analytics run can take tens of seconds. Compare a log event with the
    # clock taken after reading the tails, not the run's initial timestamp.
    return candidate in active_ids and 0 <= observed_now-observed_at <= TTL

def history_room_states(history, saved, current_time, current_states):
    """Align compact room-state evidence with the existing count history.

    1 means an open room (including zero players), 0 means confirmed offline,
    and null means unavailable evidence. Older rows have no per-room state;
    derive only what their counts and sampled service state actually prove.
    """
    by_time={r[0]:r for r in saved if isinstance(r,list) and len(r)==5 and
             type(r[0]) is int and all(type(v) is int and v in (0,1) or v is None for v in r[1:])}
    by_time[current_time]=[current_time,*current_states]
    aligned=[]
    for row in history:
        timestamp=row[0]
        if timestamp in by_time:aligned.append(by_time[timestamp]);continue
        aligned.append([timestamp,*[1 if type(row[i]) is int and row[i]>=0 else
                      0 if row[5]==0 else None for i in range(1,5)]])
    return aligned

def run(*, public=PUBLIC, state=STATE, now=None, publish_status=True):
    realtime=now is None
    now=time.time() if realtime else now
    shell={'schema':1,'generated_at':round(now),'expires_at':round(now)+TTL}
    snapshot=read_json(OPS/'latest.json',{})
    if not isinstance(snapshot,dict):snapshot={}
    snapshot_fresh=-30<=now-epoch(snapshot.get('generated_at'))<=TTL
    try:service=service_status(now)
    except (OSError,ValueError,subprocess.SubprocessError):service={'online':None,'uptime_seconds':None,'started_at':None,'restarts':None}
    runtime=runtime_rooms(now,service)
    stats={**shell,'available':False,'scopes':{}}
    status={**shell,'service':service,'rooms':[],'available':False}
    rooms={};active_ids=set()
    try:
        with snapshot_db(DB) as conn:
            for row in conn.execute('SELECT r.*,rr.ruleset_id FROM autohost_rooms r LEFT JOIN autohost_room_rulesets rr USING(room_id)'):
                rooms[row['room_id']]={**dict(row),'type':room_type(row)}
            latest={r.get('alias'):r for r in snapshot.get('rooms',[]) if isinstance(r,dict)}
            # Determine current IDs from local creation logs and cross-check
            # against the room ledger before treating anyone as currently present.
            tails={}; ids={}
            for i in range(4):
                alias='room'+str(i+1);lines=tail_lines(SOURCE.room_log(alias));tails[alias]=lines
                created=[int(m[1]) for line in lines if (m:=re.search(LOG_PREFIX+r'bot INFO room (\d+) created\b',line))]
                candidate=runtime[alias]['room_id'] if runtime is not None else created[-1] if created else latest.get(alias,{}).get('room_id')
                ids[alias]=candidate
                row=rooms.get(candidate)
                if service['online'] and (runtime[alias]['valid'] if runtime is not None else snapshot_fresh) and row and row['closed_at'] is None:active_ids.add(candidate)
            observed_now=time.time() if realtime else now
            network_views={}
            scopes,map_by_id,map_by_label,first=metrics(conn,rooms,now,active_ids,read_network_statistics(now),network_views)
            catalog_cache={}
            for view_scopes in [scopes,*[host['scopes'] for host in network_views.get('hosts',{}).values()]]:
                for mode,periods in view_scopes.items():
                    for group in periods.values():
                        for player_row in group['leaders']:
                            for key in ['best_gain','best_score']:
                                if player_row[key]:player_row[key]=enrich_map(player_row[key],mode,catalog_cache)
            stats={**shell,'available':True,'tracking_since':round(first),'scopes':scopes,**network_views}
            universal=read_json(SOURCE.config('universal'),{})
            for i in range(4):
                alias='room'+str(i+1);candidate=ids[alias];row=rooms.get(candidate)
                parsed=runtime[alias] if runtime is not None else playlist(tails[alias],map_by_id,map_by_label,candidate)
                valid=bool(parsed['valid'] and candidate in active_ids) if runtime is not None else room_observation_valid(candidate,active_ids,parsed['observed_at'],observed_now)
                phase=parsed['state'] if runtime is not None else (parsed['state'] or latest.get(alias,{}).get('state') or 'UNKNOWN') if valid else 'OFFLINE' if service['online'] is False or row and row['closed_at'] else 'STALE'
                occupants=None
                if valid:
                    occupants=parsed['players'] if runtime is not None else conn.execute('SELECT COUNT(*) FROM autohost_room_players WHERE room_id=? AND last_join_at>COALESCE(last_leave_at,0)',(candidate,)).fetchone()[0]
                # A closed room has no observation of an empty *open* room.
                # Keep it out of the occupancy series and record it offline.
                cfg=read_json(SOURCE.config(alias),{})
                # A frozen MatchStarted ledger row is the strongest current-map
                # evidence during gameplay, even when playlist promotion raced.
                current=parsed['current_map'] if valid else None
                if valid and phase=='PLAYING' and runtime is None:
                    playing=conn.execute('SELECT beatmap_id,map_label,star_rating FROM autohost_matches WHERE room_id=? AND result="playing" ORDER BY started_at DESC LIMIT 1',(candidate,)).fetchone()
                    if playing:current={'beatmap_id':playing[0],'label':clean(playing[1]),'stars':number(playing[2],None)}
                current=enrich_map(current,TYPES[i],catalog_cache)
                parsed['queue']=[enrich_map(m,TYPES[i],catalog_cache) for m in parsed['queue']]
                status['rooms'].append({'alias':alias,'label':LABELS[i],'type':TYPES[i],'room_id':candidate if valid else None,
                    'state':phase,'players':occupants,'observed_at':parsed['observed_at'] if valid else None,
                    'current_map':current,'queue':parsed['queue'] if valid else [],'queue_known':bool(valid and parsed['queue_known']),
                    'settings':public_settings(cfg if isinstance(cfg,dict) else {},universal if isinstance(universal,dict) else {},row),
                    'guide':'lobbies:'+GUIDES[i]})
            status['available']=True
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error) as exc:
        import traceback
        print('Analytics source unavailable: '+type(exc).__name__+' '+str(getattr(exc,'sqlite_errorname',''))+' '+str([(f.name,f.lineno) for f in traceback.extract_tb(exc.__traceback__)]))
        stats={**shell,'available':False,'scopes':{}}
        status['rooms']=[]
        # Do not retain named statistics when privacy cannot be verified.
    if not status['rooms']:
        status['rooms']=[{'alias':'room'+str(i+1),'label':LABELS[i],'type':TYPES[i],'state':'STALE','players':None,
                         'room_id':None,'observed_at':None,'current_map':None,'queue':[],'queue_known':False,'settings':{},'guide':'lobbies:'+GUIDES[i]} for i in range(4)]
    history=read_json(state/'observations.json')
    if not isinstance(history,list):history=history_seed()
    history=[r for r in history if isinstance(r,list) and len(r)==6 and now-31*86400<=number(r[0])<=now+30]
    history.append([round(now),*[r['players'] for r in status['rooms']],1 if service['online'] else 0 if service['online'] is False else None])
    # Retain aggregate observations for 31 days; never touch production logs.
    history=list({row[0]:row for row in history}.values());history.sort(key=lambda r:r[0])
    current_states=[1 if type(r['players']) is int and r.get('room_id') else
                    0 if service['online'] is False or r['state']=='OFFLINE' else None
                    for r in status['rooms']]
    saved_states=read_json(state/'room-states.json',[])
    if not isinstance(saved_states,list):saved_states=[]
    room_states=history_room_states(history,saved_states,round(now),current_states)
    atomic(state/'observations.json',history,0o600)
    atomic(state/'room-states.json',room_states,0o600)
    known=[r for r in history if r[0]>=now-86400 and r[5] is not None]
    status['availability']={'window_seconds':86400,'samples':len(known),'online_samples':sum(r[5] for r in known),
                            'percent':round(100*sum(r[5] for r in known)/len(known),2) if known else None}
    status['history_since']=history[0][0] if history else None
    activity={**shell,'samples':history,'room_states':room_states,'room_labels':LABELS}
    atomic(public/'statistics.json',stats)
    if publish_status:atomic(public/'status.json',status)
    atomic(public/'activity.json',activity)
    return stats,status,activity

if __name__=='__main__':run(publish_status=False)
