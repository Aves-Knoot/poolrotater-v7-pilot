#!/usr/bin/env python3
"""Fast, read-only current-room projection. No score aggregation or osu API."""
from pathlib import Path
import stat
import importlib.machinery,importlib.util,json,os,re,sqlite3,time
analytics_path=Path(__file__).with_name('analytics.py')
loader=importlib.machinery.SourceFileLoader('wiki_analytics',str(analytics_path if analytics_path.is_file() else '/usr/local/sbin/poolrotator-wiki-analytics'))
spec=importlib.util.spec_from_loader(loader.name,loader);a=importlib.util.module_from_spec(spec);loader.exec_module(a)
CACHE=Path('/var/lib/poolrotator-wiki-live/maps.json');PEAK=Path('/var/lib/poolrotator-wiki-live/peak-players.json');TTL=35
DISPLAY=Path('/var/lib/poolrotator-wiki-live/display.db')

def display_row(mode,bid,*,owner_uid=0):
 if mode not in ('standard','mania_4k','mania_7k','catch') or type(bid)is not int or bid<=0:return None
 for path in (DISPLAY,*DISPLAY.parents):
  if path.is_symlink():raise ValueError('display index symlink refused')
 info=DISPLAY.lstat()
 if not stat.S_ISREG(info.st_mode) or info.st_uid!=owner_uid or info.st_nlink!=1 or info.st_mode&0o222 or info.st_size>64*1024**2:
  raise ValueError('display index must be protected immutable public metadata')
 conn=sqlite3.connect(DISPLAY.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=1);conn.row_factory=sqlite3.Row
 try:
  conn.execute('PRAGMA query_only=ON');conn.execute('PRAGMA trusted_schema=OFF')
  return conn.execute('SELECT beatmapset_id,title,artist,version,total_length,hit_length,sr FROM display_maps WHERE mode=? AND beatmap_id=?',(mode,bid)).fetchone()
 finally:conn.close()

def valid_peak(value,now):
 return (isinstance(value,dict) and value.get('schema')==1 and
         type(value.get('count')) is int and 0<=value['count']<=2048 and
         type(value.get('observed_at')) is int and type(value.get('tracking_since')) is int and
         1600000000<=value['tracking_since']<=value['observed_at']<=now+30)

def four_room_total(rooms):
 if len(rooms)!=4 or any(not isinstance(r,dict) for r in rooms) or {r.get('alias') for r in rooms}!={'room1','room2','room3','room4'}:return None
 counts=[0 if r.get('state')=='OFFLINE' and r.get('players') is None else r.get('players') for r in rooms]
 return sum(counts) if all(type(n) is int and 0<=n<=512 for n in counts) else None

def tracked_peak(path,samples,rooms,now):
 """Keep a durable high-water mark; never invent a total from partial rooms."""
 stamp=int(now)
 if path.exists():
  old=a.read_json(path,None)
  if not valid_peak(old,now):
   print('Invalid peak record retained for operator review; public peak withheld')
   return None
 else:
  old=None
  for row in samples:
   if (isinstance(row,list) and len(row)==6 and type(row[0]) is int and
       1600000000<=row[0]<=now+30 and row[5]==1 and
       all(type(n) is int and 0<=n<=512 for n in row[1:5])):
    count=sum(row[1:5])
    if old is None or count>old['count']:
     old=dict(schema=1,count=count,observed_at=row[0],tracking_since=min(old['tracking_since'],row[0]) if old else row[0])
    else:old['tracking_since']=min(old['tracking_since'],row[0])
 total=four_room_total(rooms)
 value=old.copy() if old else None
 if total is not None and (value is None or total>value['count']):
  value=dict(schema=1,count=total,observed_at=stamp,tracking_since=value['tracking_since'] if value else stamp)
 if value is not None and (value!=old or not path.exists()):
  try:a.atomic(path,value,0o600)
  except OSError:
   print('Could not persist peak record; retaining previous verified peak')
   return old if path.exists() else None
 return value

def catalog_stamp(path):
 # Check both pending-journal formats; never omit uncheckpointed changes.
 stamps=[]
 for file in [path,Path(str(path)+'-wal'),Path(str(path)+'-journal')]:
  try:
   s=file.stat();stamps.append((s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))
  except FileNotFoundError:stamps.append(None)
 return stamps

def catalog_row(path,query,bid):
 try:
  with a.readonly(path) as conn:
   return conn.execute(query,(bid,)).fetchone()
 except sqlite3.OperationalError as error:
  if 'readonly' not in str(error).lower() and 'unable to open' not in str(error).lower():raise
  before=catalog_stamp(path)
  if not before[0] or any(s and s[1] for s in before[1:]):raise
  # A read-only WAL catalog may have no shared-index files after the bot closes
  # its last connection. For a stable, fully checkpointed lookup, immutable
  # avoids creating any cache files. Reject the result if any source changed.
  conn=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=2)
  conn.row_factory=sqlite3.Row
  try:
   conn.execute('PRAGMA query_only=ON');row=conn.execute(query,(bid,)).fetchone()
  finally:conn.close()
  if catalog_stamp(path)!=before:raise sqlite3.OperationalError('Catalog changed during lookup')
  return row

def map_metadata(info,mode,cache,now):
 if not info or type(info.get('beatmap_id')) is not int:return info
 bid=info['beatmap_id'];key=mode+':'+str(bid);saved=cache.get(key)
 if not isinstance(saved,dict) or now-saved.get('cached_at',0)>(86400 if saved.get('beatmapset_id') else 20):
  saved={'cached_at':now}
  catalogs=[('catch.db','catch_maps')] if mode=='catch' else [('farm.db','beatmaps'),('loved.db','beatmaps')] if mode=='standard' else ([('mania_7k.db','maps'),('mania.db','maps'),('farm.db','beatmaps')] if mode=='mania_7k' else [('mania.db','maps'),('mania_7k.db','maps'),('farm.db','beatmaps')])
  for filename,table in catalogs:
   try:
    row=(display_row(mode,bid) if a.SOURCE.owner else None if mode=='catch' else catalog_row(a.SOURCE.catalog(filename),'SELECT beatmapset_id,title,artist,version,total_length,hit_length,sr FROM '+table+' WHERE beatmap_id=? LIMIT 1',bid))
    if row:
     saved.update({k:a.clean(row[k],180) for k in ['title','artist','version']})
     for field in ['beatmapset_id','total_length','hit_length']:
      n=a.number(row[field],None)
      if n is not None and 0<n<10000000:saved[field]=int(n)
     saved['stars']=a.number(row['sr'],None);break
   except (OSError,ValueError,sqlite3.Error):continue
  cache[key]=saved
 meta={k:v for k,v in saved.items() if k!='cached_at'}
 result={**meta,**info}
 if result.get('title') and str(result.get('label','')).startswith('Map #'):result['label']=a.clean(result.get('artist','')+' - '+result['title']+' ['+result.get('version','')+']')
 return result

def playback(room,match,now):
 if room['state']!='PLAYING' or not match or match['result']!='playing' or match['ended_at'] is not None:return None
 current=room['current_map'] or {}
 if current.get('beatmap_id')!=match['beatmap_id']:return None
 start=a.number(match['started_at'],None);length=a.number(current.get('total_length') if room.get('type')=='catch' else current.get('total_length') or current.get('hit_length'),None)
 if start is None or start>now+5 or length is None or not 1<=length<=3600:return None
 mods=room['settings'].get('required_mods',[]);rate=1.5 if 'DT' in mods or 'NC' in mods else .75 if 'HT' in mods or 'DC' in mods else 1
 # Catalog duration includes breaks; allow a little time for the lead-in/out.
 duration=round(length/rate+5,1)
 return dict(started_at=start,duration_seconds=duration,estimated_end_at=round(start+duration,1),approximate=True)

def run(public=a.PUBLIC,cache_path=CACHE,now=None,peak_path=PEAK):
 now=time.time() if now is None else now
 if os.environ.get('POOLROTATOR_WIKI_REQUIRE_READONLY')=='1' and not os.statvfs(a.DB).f_flag & os.ST_RDONLY:raise OSError('Ledger must be mounted read-only')
 data=dict(schema=1,generated_at=round(now,3),expires_at=round(now+TTL,3),refresh_seconds=10,available=False,rooms=[])
 try:service=a.service_status(now)
 except Exception:service=dict(online=None,uptime_seconds=None,started_at=None,restarts=None)
 data['service']=service
 runtime=a.runtime_rooms(now,service)
 old=a.read_json(public/'status.json',{})
 for key in ['availability','history_since']:
  if isinstance(old,dict) and key in old:data[key]=old[key]
 # The five-minute history export remains the source for availability statistics.
 activity=a.read_json(public/'activity.json',{})
 samples=activity.get('samples',[]) if isinstance(activity,dict) else []
 known=[r for r in samples if isinstance(r,list) and len(r)==6 and now-86400<=a.number(r[0])<=now and r[5] in (0,1)]
 if known:data['availability']=dict(window_seconds=86400,samples=len(known),online_samples=sum(r[5] for r in known),percent=round(100*sum(r[5] for r in known)/len(known),2))
 if samples:data['history_since']=samples[0][0]
 snapshot=a.read_json(a.OPS/'latest.json',{});snapshot=snapshot if isinstance(snapshot,dict) else {}
 latest={r.get('alias'):r for r in snapshot.get('rooms',[]) if isinstance(r,dict)}
 cached=a.read_json(cache_path,{})
 cached=cached if isinstance(cached,dict) else {}
 tails={};ids={};rows={};matches={};counts={};map_ids={};map_labels={}
 universal=a.read_json(a.SOURCE.config('universal'),{});universal=universal if isinstance(universal,dict) else {}
 try:
  for i in range(4):
   alias='room'+str(i+1);lines=a.tail_lines(a.SOURCE.room_log(alias),220000);tails[alias]=lines
   created=[int(m[1]) for line in lines if (m:=re.search(a.LOG_PREFIX+r'bot INFO room (\d+) created\b',line))]
   candidate=runtime[alias]['room_id'] if runtime is not None else created[-1] if created else latest.get(alias,{}).get('room_id')
   ids[alias]=candidate if type(candidate) is int and candidate>0 else None
  with a.readonly(a.DB) as conn:
   for alias,candidate in ids.items():
    if candidate is None:continue
    row=conn.execute('SELECT * FROM autohost_rooms WHERE room_id=?',(candidate,)).fetchone();rows[alias]=dict(row) if row else None
    counts[alias]=conn.execute('SELECT COUNT(*) FROM autohost_room_players WHERE room_id=? AND last_join_at>COALESCE(last_leave_at,0)',(candidate,)).fetchone()[0]
    recent=conn.execute('SELECT beatmap_id,map_label,star_rating,started_at,ended_at,result FROM autohost_matches WHERE room_id=? ORDER BY started_at DESC LIMIT 12',(candidate,)).fetchall()
    matches[alias]=dict(recent[0]) if recent else None
    for match in recent:
     info=dict(beatmap_id=match['beatmap_id'],label=a.clean(match['map_label']),stars=a.number(match['star_rating'],None))
     map_ids[match['beatmap_id']]=info
     if match['map_label']:map_labels[match['map_label']]=info
  # Release production read transaction before log parsing/catalog lookups.
  for i in range(4):
   alias='room'+str(i+1);candidate=ids[alias];row=rows.get(alias);parsed=runtime[alias] if runtime is not None else a.playlist(tails[alias],map_ids,map_labels,candidate);match=matches.get(alias)
   same_activation=bool(row and (row['created_at']>=(service.get('started_at') or 0)-30 or (a.epoch(snapshot.get('generated_at'))>=(service.get('started_at') or 0) and latest.get(alias,{}).get('room_id')==candidate)))
   valid=bool(parsed['valid'] and row and row['closed_at'] is None) if runtime is not None else bool(service['online'] and row and row['closed_at'] is None and same_activation)
   phase=parsed['state'] if runtime is not None else (parsed['state'] or latest.get(alias,{}).get('state') or 'STALE') if valid else 'OFFLINE' if service['online'] is False or row and row['closed_at'] else 'STALE'
   current=parsed['current_map'] if valid else None
   if runtime is None and valid and match and match['result']=='playing' and match['ended_at'] is None:
    phase='PLAYING';current=dict(beatmap_id=match['beatmap_id'],label=a.clean(match['map_label']),stars=a.number(match['star_rating'],None))
   elif runtime is None and phase=='PLAYING' and match and match['ended_at'] is not None:phase='SCORING'
   cfg=a.read_json(a.SOURCE.config(alias),{});cfg=cfg if isinstance(cfg,dict) else {}
   mode='catch' if cfg.get('ruleset_id')==2 or (row and row.get('ruleset_id')==2) else a.TYPES[i]
   room=dict(alias=alias,label='Catch fixed' if mode=='catch' else a.LABELS[i],type=mode,room_id=candidate if valid else None,state=phase,players=counts.get(alias) if valid else 0 if row and row['closed_at'] else None,observed_at=parsed['observed_at'] or None,current_map=map_metadata(current,mode,cached,now),queue=[map_metadata(m,mode,cached,now) for m in parsed['queue']] if valid else [],queue_known=bool(valid and parsed['queue_known']),settings=a.public_settings(cfg,universal,row),guide='lobbies:'+('catch' if mode=='catch' else a.GUIDES[i]))
   if runtime is not None:
    room['players']=parsed['players'] if valid else None
    if valid and current and parsed.get('match_started_at'):
     match=dict(result='playing',ended_at=None,beatmap_id=current['beatmap_id'],started_at=parsed['match_started_at'])
   if runtime is not None and parsed.get('private'):
    room.update(label='Lobby '+str(i+1),type='unknown',settings={},guide='',players=0)
   elif runtime is not None and parsed.get('intentionally_stopped'):
    room['players']=0;room['intentionally_stopped']=True
   room['playback']=playback(room,match,now);data['rooms'].append(room)
  data['available']=True
 except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):
  data['rooms']=[]
 if not data['rooms']:
  for i in range(4):
   alias='room'+str(i+1);cfg=a.read_json(a.SOURCE.config(alias),{})
   catch=isinstance(cfg,dict) and cfg.get('ruleset_id')==2
   data['rooms'].append(dict(alias=alias,label='Catch fixed' if catch else a.LABELS[i],type='catch' if catch else a.TYPES[i],room_id=None,state='STALE',players=None,observed_at=None,current_map=None,queue=[],queue_known=False,settings={},guide='lobbies:'+('catch' if catch else a.GUIDES[i]),playback=None))
 # Repeat the saved-intent privacy boundary after every fallback path. Even a
 # failed ledger read must not decorate a private slot from native settings.
 mask=a.managed_public_mask()
 if mask is not None:
  for room in data['rooms']:
   hidden=mask.get(room['alias'],{'private':True})
   if hidden.get('private') or hidden.get('intentionally_stopped'):
    room.update(room_id=None,state='OFFLINE',players=0,observed_at=None,current_map=None,queue=[],queue_known=False,playback=None)
    if hidden.get('private'):room.update(label='Lobby '+room['alias'][-1],type='unknown',settings={},guide='')
    else:room['intentionally_stopped']=True
 data['peak_players']=tracked_peak(peak_path,samples,data['rooms'] if data['available'] else [],now)
 a.atomic(public/'status.json',data)
 cached=dict(sorted(cached.items(),key=lambda p:p[1].get('cached_at',0),reverse=True)[:256]);a.atomic(cache_path,cached,0o600)
 return data
if __name__=='__main__':run()
