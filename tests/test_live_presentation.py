"""Execute shared owner/community SSR using real staged maintained templates."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,json,re,shutil,subprocess,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def fixture(now=None):
 now=now or int(time.time());rooms=[]
 for i in range(1,5):
  rooms.append({'alias':f'room{i}','label':f'Owner {i}','room_id':100+i,'players':i,'state':'PLAYING','current_map':{'beatmap_id':i,'beatmapset_id':200+i,'title':f'Owner map {i}','artist':'Owner artist','total_length':150,'stars':4.5},'playback':{'started_at':now-20,'duration_seconds':150,'estimated_end_at':now+130},'queue_known':True,'queue':[],'settings':{'active_range':[4,5],'countdown_seconds':15,'required_mods':[]},'guide':'lobbies:start'})
 community=[]
 for i in range(2):
  community.append({'key':str(i+1)*24,'room_id':300+i,'title':'A very long public room title / '+'repeated words '*8,'operator':['Xikeon','Another host'][i],'preset':['standard-auto','mania-4k'][i],'state':'playing','players':2,'playing_players':1,'match_participants':1,'min_stars':4.,'max_stars':5.,'current_map':{'beatmap_id':9+i,'beatmapset_id':209+i,'title':'<script>safe map title</script>','artist':'Fixture artist','version':'Fixture difficulty','total_length':150,'stars':4.5},'playback':{'started_at':now-20,'duration_seconds':100,'estimated_end_at':now+80,'match_id':20+i,'beatmap_id':9+i,'clock_rate':1.5},'settings':{'ruleset_id':i*3,'keys':4 if i else None,'auto_difficulty':not bool(i),'selection_style':'variety','freestyle':False,'required_mods':['DT'],'allowed_mods':['NM','DT'],'countdown_seconds':15},'fresh_until':now+180,'hide_at':now+600})
 return {'status':{'schema':1,'available':True,'generated_at':now,'expires_at':now+60,'rooms':rooms,'service':{'online':True,'uptime_seconds':120}},'community':{'schema':1,'available':True,'generated_at':now,'expires_at':now+60,'rooms':community},'activity':{'samples':[]}}
def render(value,surface):
 with tempfile.TemporaryDirectory()as d:
  p=Path(d);feed=p/'community.json';feed.write_text(json.dumps(value['community']))
  for name in ('nowplaying.php','dashboard.php','home.php'):
   source=(ROOT/'website/live/template'/name).read_text().replace('/srv/poolrotator-wiki/public/community/rooms.json',str(feed));(p/name).write_text(source)
  shutil.copyfile(ROOT/'website/live/presentation.php',p/'presentation.php');shutil.copyfile(ROOT/'website/community/community.php',p/'community.php');shutil.copyfile(ROOT/'website/live/fixtures/render.php',p/'render.php')
  return subprocess.check_output(['/usr/bin/php8.3','-n',str(p/'render.php'),surface],input=json.dumps(value).encode(),stderr=subprocess.STDOUT).decode()
class LivePresentationTests(unittest.TestCase):
 def test_all_surfaces_share_owner_and_remote_cards_without_alias_collision(self):
  for surface in ('home','sidebar','dashboard'):
   html=render(fixture(),surface)
   self.assertEqual(html.count('data-ownership="owner"'),4);self.assertEqual(html.count('data-ownership="community"'),2)
   self.assertEqual(html.count('data-np-room="room1"'),1);self.assertIn('Hosted by Xikeon',html)
   self.assertIn('https://assets.ppy.sh/beatmaps/209/covers/cover.jpg',html)
   self.assertIn('aria-label="Estimated song progress"',html);self.assertRegex(html,r'Estimated 0:2[0-5] / 1:40')
   self.assertNotIn('<script>safe map title</script>',html);self.assertIn('&lt;script&gt;safe map title&lt;/script&gt;',html)
   if surface=='home':self.assertEqual(html.count('data-home-room='),4);self.assertIn('14</strong> people in public rooms',html)
   if surface=='dashboard':self.assertEqual(html.count('class="pr-browser-room"'),4);self.assertIn('Across owner and community rooms',html);self.assertIn('Players in room',html);self.assertIn('1 playing now',html);self.assertIn('1 players at match start',html)
 def test_stale_unavailable_and_legacy_no_timing_hide_progress_and_joins(self):
  for mode in ('stale','legacy','outage','map-change','complete'):
   value=fixture();r=value['community']['rooms'][0]
   if mode=='stale':r['fresh_until']=time.time()-1
   if mode=='legacy':r.pop('playback');r.pop('settings')
   if mode=='outage':value['community']['available']=False
   if mode=='map-change':r['current_map']['beatmap_id']=777
   if mode=='complete':r['state']='idle';r['playback']=None
   html=render(value,'dashboard');match=re.search(r'<article class="pr-community-room".*?</article>',html,re.S)
   if mode=='outage':self.assertIsNone(match);continue
   card=match[0];self.assertNotIn('Estimated 0:20',card)
   if mode=='stale':self.assertNotIn('osu://room/300',card);self.assertNotIn('209/covers',card);self.assertNotIn('safe map title',card)
 def test_inline_public_snapshot_excludes_private_and_unrecognized_fields(self):
  value=fixture();r=value['community']['rooms'][0];r['password']='DO-NOT-PUBLISH';r['settings']['oauth_token']='DO-NOT-PUBLISH';r['current_map']['player_id']='DO-NOT-PUBLISH';r['settings']['ruleset_id']={'nested':'DO-NOT-PUBLISH'};r['playback']['match_id']={'nested':'DO-NOT-PUBLISH'};r['current_map']['stars']={'nested':'DO-NOT-PUBLISH'};r['min_stars']={'nested':'DO-NOT-PUBLISH'}
  html=render(value,'dashboard');self.assertNotIn('DO-NOT-PUBLISH',html)
  r['visibility']='private';html=render(value,'dashboard');self.assertNotIn('osu://room/300',html)
 def test_paginated_feed_and_initial_markup_preserve_focus_keys(self):
  value=fixture();value['community']['more_available']=True;html=render(value,'dashboard')
  self.assertIn('Showing the first 100 public rooms; more rooms are available.',html)
  self.assertIn('data-live-key="111111111111111111111111"',html)
 def test_malformed_remote_row_does_not_break_owner_or_other_host_rendering(self):
  for key,value in [('state',{}),('title',[]),('operator',{}),('players',True),('fresh_until',[]),('current_map',{'beatmap_id':9,'artist':{}})]:
   data=fixture();data['community']['rooms'][0][key]=value;html=render(data,'dashboard')
   self.assertEqual(html.count('data-ownership="owner"'),4);self.assertEqual(html.count('data-ownership="community"'),1);self.assertIn('Hosted by Another host',html)
 def test_owner_join_browser_queues_and_settings_preserved(self):
  html=render(fixture(),'dashboard')
  for i in range(1,5):self.assertIn(f'osu://room/{100+i}',html);self.assertIn(f'https://poolrotator-91-107-237-182.nip.io/room/{100+i}',html)
  self.assertEqual(html.count('Observed queue'),4);self.assertEqual(html.count('data-setting="countdown_seconds"'),4)
 def test_owner_refresh_and_filters_have_explicit_remote_exclusion(self):
  source=(ROOT/'website/live/template/script.js').read_text();self.assertIn(".pr-now-playing[data-ownership=\"owner\"]",source)
  self.assertIn('window.prLivePresentation.update(card,room,valid,now())',source)
  self.assertIn(".pr-dash-room,[data-community-card]",source)
