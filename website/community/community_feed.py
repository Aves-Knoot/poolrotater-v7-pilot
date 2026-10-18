"""Credential-free community directory, independent of the four owner feeds."""
import hashlib,http.client,json,math,os,re,ssl,sqlite3,stat,tempfile,time,uuid
from pathlib import Path
try:
    from .display_model import DisplayIndex, map_display, playback, public_settings
except ImportError:  # Isolated (-I) installed publisher: no sys.path mutation.
    import importlib.util
    _display_path=Path(__file__).with_name('display_model.py')
    _display_spec=importlib.util.spec_from_file_location('community_display_model',_display_path)
    _display=importlib.util.module_from_spec(_display_spec)
    _display_spec.loader.exec_module(_display)
    DisplayIndex,map_display,playback,public_settings=(_display.DisplayIndex,_display.map_display,_display.playback,_display.public_settings)
ORIGIN='https://91.107.237.182:9444'
HOST='91.107.237.182';PORT=9444;RESOURCE='/v1/rooms?limit=100'
MAX_BYTES=262144;FETCH_SECONDS=4;CACHE_SECONDS=60;FRESH_SECONDS=180;HIDE_SECONDS=600
TOP={'ok','protocol_version','server_epoch','generated_at','rooms','next_offset'}
ROW={'alias','osu_room_id','preset','state','occupants','visibility','title','min_stars','max_stars','observed_at','code_version','catalog_version','current_map','host_id','operator','age_seconds','freshness','join_url','distribution'}
PRESETS={'standard-auto','standard-fixed','mania-4k','mania-7k'}
STATES={'idle','starting','playing','draining','unavailable'}
def require(value):
    if not value:raise ValueError('invalid public directory')
def integer(v,minimum=0,maximum=2**53-1):require(type(v)is int and minimum<=v<=maximum)
def number(v,minimum=0,maximum=2**53-1):require(type(v)in(int,float) and math.isfinite(v) and minimum<=v<=maximum)
def text(v,maximum=160):require(type(v)is str and 0<len(v)<=maximum and not any(ord(c)<32 or ord(c)==127 for c in v))
def identity(v):
    require(type(v)is str and len(v)==36)
    try:require(str(uuid.UUID(v))==v)
    except Exception:raise ValueError('invalid public identity') from None


class DirectoryV2Unavailable(ValueError):
    """Only the explicit legacy unknown-route reply permits downgrade."""


def validate_display(display,row):
    require(type(display)is dict and set(display)=={'settings','playing_players','match'})
    settings=display['settings']
    require(type(settings)is dict and set(settings)=={'ruleset_id','keys','auto_difficulty','selection_style','allowed_mods','required_mods','freestyle','countdown_seconds'})
    integer(settings['ruleset_id'],0,3)
    require(settings['keys']is None or type(settings['keys'])is int and settings['keys']in(4,7))
    require(settings['keys']is None or settings['ruleset_id']==3)
    require(type(settings['auto_difficulty'])is bool and type(settings['freestyle'])is bool)
    text(settings['selection_style'],80);number(settings['countdown_seconds'],0,600)
    for key in('allowed_mods','required_mods'):
        mods=settings[key];require(type(mods)is list and len(mods)<=32 and all(type(mod)is str for mod in mods) and len(set(mods))==len(mods))
        for mod in mods:require(type(mod)is str and re.fullmatch('[A-Z0-9]{1,16}',mod))
    if display['playing_players']is not None:integer(display['playing_players'],0,128)
    match=display['match']
    if match is not None:
        require(type(match)is dict and set(match)=={'playlist_item_id','beatmap_id','started_at','clock_rate','participants','clock_skew_seconds'})
        integer(match['playlist_item_id'],1);integer(match['beatmap_id'],1);number(match['started_at'])
        if match['clock_rate']is not None:number(match['clock_rate'],.25,3)
        if match['participants']is not None:integer(match['participants'],0,128)
        if match['clock_skew_seconds']is not None:number(match['clock_skew_seconds'],-30,30)

def project(value,*,now,owner_room_ids=(),owner_host_id=None,metadata_lookup=None):
    number(now);require(type(value)is dict and set(value)==TOP and value['ok']is True and type(value['protocol_version'])is int and value['protocol_version']in(1,2))
    identity(value['server_epoch']);number(value['generated_at']);require(-30<=now-value['generated_at']<=CACHE_SECONDS)
    require(type(value['rooms'])is list and len(value['rooms'])<=100)
    if value['next_offset']is not None:integer(value['next_offset'],1,1000000)
    if owner_host_id is not None:identity(owner_host_id)
    known=set(owner_room_ids)
    for n in known:integer(n,1)
    seen=set();rooms=[]
    for row in value['rooms']:
        require(type(row)is dict and set(row)==(ROW if value['protocol_version']==1 else ROW|{'display'}));identity(row['host_id']);text(row['operator'],80);text(row['title'])
        require(type(row['alias'])is str and re.fullmatch('[a-z][a-z0-9_-]{0,23}',row['alias']))
        integer(row['osu_room_id'],1);require(row['osu_room_id']not in seen);seen.add(row['osu_room_id'])
        require(row['visibility']=='public' and row['preset']in PRESETS and row['state']in STATES)
        require(row['freshness']in('fresh','stale') and row['distribution']=='self-reported')
        number(row['min_stars'],0,20);number(row['max_stars'],row['min_stars'],20);number(row['observed_at']);number(row['age_seconds']);require(row['observed_at']<=value['generated_at']+30)
        require(type(row['code_version'])is str and re.fullmatch('[A-Za-z0-9.+-]{1,64}',row['code_version']))
        require(row['catalog_version']=='unavailable' or type(row['catalog_version'])is str and re.fullmatch('[a-f0-9]{64}',row['catalog_version']))
        if row['occupants']is not None:integer(row['occupants'],0,128)
        if row['current_map']is not None:
            m=row['current_map'];require(type(m)is dict and set(m)=={'beatmap_id','title','stars'});integer(m['beatmap_id'],1);text(m['title']);number(m['stars'],0,20)
        display=row.get('display')
        if display is not None:validate_display(display,row)
        # Supplied links are never used; an invalid type also fails closed.
        require(row['join_url']is None or type(row['join_url'])is str and len(row['join_url'])<=200)
        age=max(row['age_seconds']+max(0,now-value['generated_at']),now-row['observed_at'],0)
        if age>=HIDE_SECONDS or row['osu_room_id']in known or row['host_id']==owner_host_id:continue
        fresh=age<FRESH_SECONDS and row['freshness']=='fresh' and row['state']!='unavailable'
        rid=row['osu_room_id'];expiry=now+max(0,FRESH_SECONDS-age)
        current=map_display(row['current_map'],row['preset'],metadata_lookup)if fresh else None
        estimate=playback(row['state'],current,display.get('match')if display else None,now=now,observed_at=row['observed_at'],fresh=fresh,settings=display['settings'] if display else None)
        rooms.append({'key':hashlib.sha256((row['host_id']+':'+row['alias']).encode()).hexdigest()[:24],
          'room_id':rid,'title':row['title'],'operator':row['operator'],'preset':row['preset'],'state':row['state']if fresh else'unavailable',
          'players':row['occupants']if fresh else None,'current_map':current,
          'ownership':'community','settings':public_settings(display['settings'])if fresh and display else None,
          'playing_players':display['playing_players']if fresh and display else None,
          'match_participants':display['match']['participants']if estimate and display.get('match')else None,'playback':estimate,
          'min_stars':row['min_stars'],'max_stars':row['max_stars'],'fresh_until':expiry if fresh else 0,'hide_at':now+HIDE_SECONDS-age,
          'self_reported':True,'browser_url':'https://osu.ppy.sh/multiplayer/rooms/'+str(rid)if fresh else None,'join_url':'osu://room/'+str(rid)if fresh else None})
    return {'schema':1,'available':True,'generated_at':now,'expires_at':now+CACHE_SECONDS,'source':'self-reported community rooms','rooms':rooms,'more_available':value['next_offset']is not None}

def decode(raw):
    require(type(raw)is bytes and len(raw)<=MAX_BYTES)
    def pairs(items):
        result={}
        for k,v in items:require(k not in result);result[k]=v
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('non-finite JSON')))

def fetch_directory(ca_file,ca_sha256,*,connection_factory=http.client.HTTPSConnection,resource=RESOURCE):
    require(resource in('/v1/rooms?limit=100','/v2/rooms?limit=100'))
    require(type(ca_sha256)is str and re.fullmatch('[a-f0-9]{64}',ca_sha256))
    fd=os.open(ca_file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb')as stream:
        s=os.fstat(stream.fileno());require(stat.S_ISREG(s.st_mode) and s.st_nlink==1 and not s.st_mode&0o022 and 100<=s.st_size<=65536)
        ca=stream.read(65537);require(hashlib.sha256(ca).hexdigest()==ca_sha256)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=ca.decode('ascii'))
    deadline=time.monotonic()+FETCH_SECONDS;connection=connection_factory(HOST,PORT,timeout=2,context=context)
    try:
        connection.request('GET',resource,headers={'Accept':'application/json','User-Agent':'PoolRotater-public-directory/1','Cache-Control':'no-cache'})
        require(time.monotonic()<deadline)
        if connection.sock is not None:connection.sock.settimeout(max(.01,min(2,deadline-time.monotonic())))
        response=connection.getresponse()
        if response.status==404 and resource.startswith('/v2/'):
            require((response.getheader('Content-Type')or'').split(';')[0].strip()=='application/json')
            raw=response.read(MAX_BYTES+1);require(time.monotonic()<deadline);error=decode(raw)
            if type(error)is dict and set(error)=={'ok','error'} and error['ok']is False and error['error']=='route_not_found':raise DirectoryV2Unavailable()
        require(response.status==200)
        require((response.getheader('Content-Type')or'').split(';')[0].strip()=='application/json')
        length=response.getheader('Content-Length')
        if length is not None:require(length.isdigit() and int(length)<=MAX_BYTES)
        chunks=[];size=0
        while True:
            require(time.monotonic()<deadline)
            if connection.sock is not None:connection.sock.settimeout(max(.01,min(2,deadline-time.monotonic())))
            raw=response.read(min(16384,MAX_BYTES+1-size));size+=len(raw);require(size<=MAX_BYTES)
            if not raw:break
            chunks.append(raw)
        return decode(b''.join(chunks))
    finally:connection.close()

def unavailable(now):return {'schema':1,'available':False,'generated_at':now,'expires_at':now+CACHE_SECONDS,'source':'self-reported community rooms','rooms':[],'more_available':False}
def publish(destination,fetch,*,now=None,owner_room_ids=(),owner_host_id=None,metadata_lookup=None):
    """A refresh failure withdraws cached remote links; owner feed is untouched."""
    now=time.time()if now is None else now
    try:value=project(fetch(),now=now,owner_room_ids=owner_room_ids,owner_host_id=owner_host_id,metadata_lookup=metadata_lookup)
    except Exception:value=unavailable(now)
    destination=Path(destination)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent,prefix='.community-',mode='w',delete=False)as f:
            temporary=Path(f.name);json.dump(value,f,ensure_ascii=False,allow_nan=False,separators=(',',':'));f.write('\n');f.flush();os.fsync(f.fileno())
        temporary.chmod(0o644);os.replace(temporary,destination)
        fd=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return value


def owner_ids(value):
    """Deduplicate from public owner projection only, including dated IDs."""
    require(type(value)is dict and value.get('schema')==1 and type(value.get('rooms'))is list and len(value['rooms'])==4)
    require({r.get('alias') for r in value['rooms'] if type(r)is dict}=={'room1','room2','room3','room4'})
    result=[]
    for row in value['rooms']:
        if row.get('room_id')is not None:integer(row['room_id'],1);result.append(row['room_id'])
    require(len(result)==len(set(result)));return result

def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ca-file',required=True);parser.add_argument('--ca-sha256',required=True)
    parser.add_argument('--owner-feed',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--display-index',help='protected central map display index (read-only)')
    parser.add_argument('--owner-host-id',help='public hub host identity, independently bound to the owner feed')
    args=parser.parse_args()
    if args.owner_host_id is not None:identity(args.owner_host_id)
    def fetch():
        fd=os.open(args.owner_feed,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb')as stream:
            st=os.fstat(stream.fileno());require(stat.S_ISREG(st.st_mode) and st.st_nlink==1 and st.st_size<=MAX_BYTES)
            ids=owner_ids(decode(stream.read(MAX_BYTES+1)))
        try:directory=fetch_directory(args.ca_file,args.ca_sha256,resource='/v2/rooms?limit=100')
        except DirectoryV2Unavailable:directory=fetch_directory(args.ca_file,args.ca_sha256)
        return directory,ids
    # Capture IDs from the same refresh, rather than private network bindings.
    now=time.time()
    try:value,ids=fetch();fetch_value=lambda:value
    except Exception:fetch_value=lambda:(_ for _ in()).throw(ValueError('directory unavailable'));ids=()
    index=None
    try:
        if args.display_index:
            try:index=DisplayIndex(args.display_index)
            except (OSError,ValueError,sqlite3.Error)as error:
                # Index unavailability must not withdraw otherwise valid rooms.
                print('Community map metadata unavailable: '+type(error).__name__)
        publish(args.output,fetch_value,now=now,owner_room_ids=ids,owner_host_id=args.owner_host_id,metadata_lookup=index.lookup if index else None)
    finally:
        if index:index.close()
if __name__=='__main__':main()
