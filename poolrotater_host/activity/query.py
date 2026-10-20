"""Personal totals from complete authenticated source evidence, without PP re-estimation."""
from ..network.contracts import require, fields, integer, text, base, verifier


def contract(value):
    fields(value,('protocol_version','user_ids','usernames','ruleset_id'));base(value)
    require(value['ruleset_id'] in (0,2,3) and type(value['ruleset_id']) is int,'invalid_activity_ruleset')
    require(type(value['user_ids']) is list and len(value['user_ids'])<=20,'activity_query_too_large')
    require(type(value['usernames']) is list and len(value['usernames'])<=10,'activity_query_too_large')
    for uid in value['user_ids']:integer(uid)
    for name in value['usernames']:text(name,80)
    return value


def union_seconds(intervals):
    total=0;end=None
    for start,finish in sorted(intervals):
        if end is None or start>end:total+=finish-start;end=finish
        elif finish>end:total+=finish-end;end=finish
    return float(total)


def player(snapshot, uid, *, ruleset_id=0):
    """Full identity and public totals. A hidden marker contains no leaked totals/name."""
    if uid in set(snapshot['hidden_user_ids']):return {'user_id':uid,'hidden':True}
    require(type(ruleset_id) is int and ruleset_id in (0,2,3),'invalid_activity_ruleset')
    allowed={0:('standard',),2:('catch',),3:('mania','mania4','mania7')}[ruleset_id]
    result={'user_id':uid,'username':str(uid),'hidden':False,'total_gain':0.0,'achievement_count':0,
            'best_gain':0.0,'total_lobby_seconds':0.0,'visit_count':0,'map_plays':0,
            'first_gain_at':None,'last_gain_at':None,'first_join_at':None,'last_join_at':None,'last_leave_at':None,
            'coverage':{'tracked_pp':True,'historical_time_overlaps_unknown':False},'host_ids':[]}
    baselines={};intervals=[];achievements={};maps=set();joins=set();names=[];hosts=set()
    def eligible(row):return row['user_id']==uid and row['mode'] in allowed
    for row in snapshot.get('baselines',[]):
        if not eligible(row):continue
        key=(row['source_id'],row['mode'])
        if key not in baselines or row['cutoff']>baselines[key]['cutoff']:baselines[key]=row
    cutoffs={}
    for row in baselines.values():
        result['total_lobby_seconds']+=row['total_seconds'];result['visit_count']+=row['visit_count']
        cutoffs[(row['host_id'],row['mode'])]=max(cutoffs.get((row['host_id'],row['mode']),0),row['cutoff'])
        names.append((row['cutoff'],row['username']));hosts.add(row['host_id'])
        for key in ('first_join_at','last_join_at','last_leave_at'):
            if row[key] is not None:
                result[key]=row[key] if result[key] is None else (min if key=='first_join_at' else max)(result[key],row[key])
    for row in snapshot['presence']:
        if not eligible(row):continue
        cutoff=cutoffs.get((row['host_id'],row['mode']),cutoffs.get((row['host_id'],'mania'),0) if ruleset_id==3 else 0)
        start=max(row['started_at'],cutoff);end=row['ended_at']
        if end>start:intervals.append((start,end));hosts.add(row['host_id']);names.append((end,row['username']))
    result['total_lobby_seconds']+=union_seconds(intervals)
    for row in snapshot['joins']:
        if not eligible(row):continue
        cutoff=cutoffs.get((row['host_id'],row['mode']),cutoffs.get((row['host_id'],'mania'),0) if ruleset_id==3 else 0)
        if row['occurred_at']<cutoff:continue
        joins.add((row['room_id'],row['occurred_at']));names.append((row['occurred_at'],row['username']));hosts.add(row['host_id'])
    result['visit_count']+=len(joins)
    for row in snapshot['matches']:
        if eligible(row):maps.add(row['match_key']);names.append((row['ended_at'],row['username']));hosts.add(row['host_id'])
    result['map_plays']=len(maps)
    for row in snapshot.get('achievements',[]):
        if not eligible(row):continue
        # Item identities trump source-specific achievement key and receive time.
        key=(row['room_id'],row['playlist_item_id'],uid) if row['playlist_item_id'] is not None else (row['achievement_key'],uid)
        old=achievements.get(key)
        if old is None or (row['host_id'],row['event_id'])<(old['host_id'],old['event_id']):achievements[key]=row
        names.append((row['created_at'],row['username']));hosts.add(row['host_id'])
    gains=[row for row in achievements.values() if row['profile_gain_pp']>0]
    result['total_gain']=sum(row['profile_gain_pp'] for row in gains)
    result['achievement_count']=len(gains);result['best_gain']=max((row['profile_gain_pp'] for row in gains),default=0)
    if gains:result['first_gain_at']=min(r['created_at'] for r in gains);result['last_gain_at']=max(r['created_at'] for r in gains)
    if names:result['username']=max(names,key=lambda item:item[0])[1]
    result['coverage']['historical_time_overlaps_unknown']=bool(baselines)
    result['host_ids']=sorted(hosts)[:100]
    result['coverage']['contributing_host_count']=len(hosts)
    result['coverage']['consenting_host_count']=sum(1 for h in snapshot['hosts'] if h['enabled'])
    result['coverage']['latest_received_at']=max((h['updated_at'] for h in snapshot['hosts'] if h['host_id'] in hosts and h['updated_at'] is not None),default=None)
    result['coverage']['opt_in_coverage_only']=True
    return result


def query(repository,token,value):
    contract(value)
    with repository.store.transaction() as db:
        host,_=repository.store._authenticate(db,verifier(token),'heartbeat')
    wanted=set(value['user_ids'])|repository.lookup_user_ids(value['usernames'])
    require(len(wanted)<=30,'activity_query_too_large')
    snapshot=repository.snapshot(user_ids=wanted)
    local={**snapshot}
    for collection in ('presence','joins','leaves','matches','achievements','baselines'):
        local[collection]=[row for row in snapshot.get(collection,()) if row['host_id']==host['id']]
    rows=[]
    for uid in sorted(wanted):
        row=player(snapshot,uid,ruleset_id=value['ruleset_id'])
        if not row['hidden']:
            own=player(local,uid,ruleset_id=value['ruleset_id'])
            row['requester_host_totals']={key:own[key] for key in ('total_gain','achievement_count','best_gain','total_lobby_seconds','visit_count','map_plays')}
        rows.append(row)
    return {'ok':True,'protocol_version':1,'ruleset_id':value['ruleset_id'],'generated_at':snapshot['generated_at'],
            'players':rows,'coverage':snapshot['coverage']}
