"""Offline selection reference; finite relaxation never changes hard constraints."""
import bisect,math,random,statistics
from collections import Counter
from .errors import PolicyError

def rejection(m,loved=False):
 if not m.get('verified'):return 'unverified_file'
 if not m.get('native')or m.get('mods')!=[]or m.get('clock_rate')!=1:return 'unsupported_variant'
 if m.get('deleted')or m.get('scoreable')is not True:return 'unavailable'
 if m.get('status')not in ({'ranked','approved','loved'}if loved else{'ranked','approved'}):return 'status_policy'
 if not math.isfinite(m['stars'])or m['max_combo']<=0 or m['duration']<=0:return 'no_mandatory_scoring_objects'
 return None

def feedback_weight(feedback):
 if not feedback or feedback.get('unique_voters',0)<5:return 1.
 good,bad=feedback.get('good',0),feedback.get('bad',0);unique=feedback['unique_voters']
 if good<0 or bad<0 or good+bad!=unique:raise PolicyError('feedback requires deduplicated one-vote-per-voter counts')
 return max(.75,min(1.25,1+.25*(good-bad)/(unique+20)))

class Selector:
 def __init__(self,maps,seed=5,loved=False):
  self.pool=sorted([m for m in maps if rejection(m,loved)is None],key=lambda m:(m['stars'],m['map_id']));self.stars=[m['stars']for m in self.pool];self.rng=random.Random(seed);self.history=[];self.now=0.
 def candidates(self,lo,hi):
  if not math.isfinite(lo)or not math.isfinite(hi)or lo>hi:raise PolicyError('invalid finite star range')
  return self.pool[bisect.bisect_left(self.stars,lo):bisect.bisect_right(self.stars,hi)]
 def choose(self,lo,hi):
  candidates=self.candidates(lo,hi)
  if not candidates:return {'ok':False,'reason':'pool_exhausted','hard_filters_relaxed':False,'candidates':0,'relaxations':[]}
  recent_maps={x['map_id']for x in self.history[-20:]};songs={x['song_key']for x in self.history if self.now-x['at']<3600};mappers={x['mapper_id']for x in self.history[-3:]};chosen=None
  for level in range(4):
   options=[m for m in candidates if(level>=3 or m['map_id']not in recent_maps)and(level>=2 or m['song_key']not in songs)and(level>=1 or m['mapper_id']not in mappers)]
   if options:break
  # All accepted candidates have positive probability; provisional scores are neutral.
  weights=[feedback_weight(m.get('feedback'))for m in options];chosen=self.rng.choices(options,weights=weights,k=1)[0]
  event={'map_id':chosen['map_id'],'song_key':chosen['song_key'],'mapper_id':chosen['mapper_id'],'at':self.now,'stars':chosen['stars'],'duration':chosen.get('provider_duration')or chosen['duration'],'spike_ratio':chosen.get('spike_ratio'),'repeat_map':chosen['map_id']in recent_maps,'repeat_song_within_hour':chosen['song_key']in songs,'repeat_mapper_within_three':chosen['mapper_id']in mappers,'relaxations':['mapper','song','map'][:level]}
  self.history.append(event);self.now+=max(1,event['duration'])+25
  return {'ok':True,'event':event,'hard_filters_relaxed':False,'candidates':len(candidates)}

def qualify_simulation(maps):
 scenarios=[];totaldraws=0;allvalid=True
 for loved in(False,True):
  for lo,hi in((0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,10),(10,14),(80,81),(95,97)):
   for seed in(5,11,29):
    selector=Selector(maps,seed,loved);results=[selector.choose(lo,hi)for _ in range(100)];events=[r['event']for r in results if r['ok']];totaldraws+=len(events)
    allvalid=allvalid and all(lo<=e['stars']<=hi for e in events)and all(not r['hard_filters_relaxed']for r in results)
    scenarios.append({'loved':loved,'range':[lo,hi],'seed':seed,'candidate_maps':len(selector.candidates(lo,hi)),'draws':len(events),'exhausted':sum(not r['ok']for r in results),'distinct_maps':len({e['map_id']for e in events}),'map_repeats':sum(e['repeat_map']for e in events),'song_repeats_within_hour':sum(e['repeat_song_within_hour']for e in events),'mapper_repeats_within_three':sum(e['repeat_mapper_within_three']for e in events),'relaxations':dict(Counter(x for e in events for x in e['relaxations'])),'median_duration_seconds':statistics.median(e['duration']for e in events)if events else None,'mean_spike_ratio':statistics.fmean(e['spike_ratio']for e in events if e['spike_ratio']is not None)if any(e['spike_ratio']is not None for e in events)else None})
 # Every map is admitted or explicitly excluded; every admitted map is in its narrow window.
 admissions=[];selector=Selector(maps,7,True)
 for m in maps:
  reason=rejection(m,True);candidates=selector.candidates(max(0,m['stars']-.001),m['stars']+.001)if reason is None else []
  reachable=any(c['map_id']==m['map_id']for c in candidates)
  admissions.append({'map_id':m['map_id'],'reason':reason,'eligible':reason is None,'positive_probability':reachable})
  allvalid=allvalid and(reachable if reason is None else not reachable)
 return {'schema':'c5-simulation-1','ok':allvalid,'full_catalog_maps':len(maps),'admissions':admissions,'scenarios':scenarios,'draws':totaldraws,'difficulty_overshoots':0 if allvalid else None,'map_cooldown_plays':20,'song_cooldown_estimated_seconds':3600,'relaxation_order':['mapper','song','map'],'representation_weights_enabled':False,'style_weights_enabled':False,'Catch_feedback_events':0,'unreviewed_weight':1.,'starvation_claim':'all hard-valid candidates have positive probability; finite draws cannot prove equal coverage','actual_room_operations':False}
