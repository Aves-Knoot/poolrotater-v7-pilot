from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from bot.multiroom import SharedDiscord
from poolrotater_host.discord_relay.owner_panel import decorate,identity

class OwnerPanelTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.path=Path(t.name)/'status.json'
  self.cfg={'_host_root':'/var/lib/poolrotater-v7-pilot/owner','_instance_name':'room1','ruleset_id':2,'auto_difficulty':False,'discord_public_status_channel_id':1001}
  self.values={'is_open':True,'lifecycle':'Playing','room_id':123,'map_url':'https://osu.ppy.sh/b/456'}
  self.feed={'available':True,'generated_at':990,'expires_at':1100,'rooms':[{'alias':'room1','room_id':123,'state':'PLAYING','settings':{'freestyle':False,'countdown_seconds':20,'min_players':1},'current_map':{'beatmap_id':456,'beatmapset_id':789},'playback':{'started_at':980,'duration_seconds':100},'queue_known':True,'queue':[{'label':'Next invented map'}]}]}
  self.write()
 def write(self):self.path.write_text(json.dumps(self.feed))
 def panel(self):return decorate(self.cfg,self.values,path=self.path,now=1000)
 def test_owner_title_semantics_and_community_preservation(self):
  self.assertEqual(identity(self.cfg),('l1-catch-setdiff','Lobby 1 · Catch Setdiff'))
  other={**self.cfg,'_host_root':'/var/lib/poolrotater-v7-pilot/two'}
  self.assertIsNone(identity(other));self.assertEqual(decorate(other,self.values,path=self.path,now=1000),self.values)
 def test_art_settings_next_without_public_map_progress(self):
  r=self.panel();self.assertTrue(r['image_url'].endswith('/789/covers/cover.jpg'))
  fields=dict(r['extra_fields']);self.assertIn('Freemod: off',fields['Room settings']);self.assertNotIn('Map progress',fields);self.assertEqual(fields['Next map'],'Next invented map')
 def test_delayed_or_expired_feed_never_reuses_old_art_or_progress(self):
  for changes in ({'generated_at':700},{'expires_at':999},{'available':False}):
   with self.subTest(changes=changes):
    old=copy.deepcopy(self.feed);self.feed.update(changes);self.write();self.assertEqual(self.panel()['extra_fields'],());self.assertEqual(self.panel()['image_url'],'');self.feed=old
 def test_room_and_map_refresh_withdraw_old_metadata(self):
  self.feed['rooms'][0]['room_id']=999;self.write();self.assertEqual(self.panel()['image_url'],'')
  self.feed['rooms'][0]['room_id']=123;self.feed['rooms'][0]['current_map']['beatmap_id']=999;self.write()
  r=self.panel();self.assertEqual(r['image_url'],'');self.assertNotIn('Map progress',dict(r['extra_fields']))
 def test_idle_does_not_show_previous_progress(self):
  self.feed['rooms'][0]['state']='IDLE';self.write();self.assertNotIn('Map progress',dict(self.panel()['extra_fields']))
 def test_mod_adjusted_duration_does_not_expose_public_map_progress(self):
  self.feed['rooms'][0]['playback']['duration_seconds']=60;self.write();self.assertNotIn('Map progress',dict(self.panel()['extra_fields']))
 def test_invalid_timing_and_nonfinite_are_suppressed(self):
  for value in (float('nan'),-1,100000):
   self.feed['rooms'][0]['playback']['duration_seconds']=value;self.write();self.assertNotIn('Map progress',dict(self.panel()['extra_fields']))
 def test_missing_malformed_symlink_and_closed_are_safe(self):
  self.path.unlink();self.assertEqual(self.panel()['extra_fields'],())
  self.path.write_text('{');self.assertEqual(self.panel()['extra_fields'],())
  self.path.unlink();other=self.path.parent/'elsewhere';other.write_text('private-value');self.path.symlink_to(other)
  self.assertEqual(self.panel()['extra_fields'],());self.path.unlink();self.write()
  self.values['is_open']=False;self.assertEqual(self.panel()['extra_fields'],())
 def test_existing_message_route_and_channel_are_preserved(self):
  shared=SharedDiscord([self.cfg]);shared._thread=object();view=shared.view_for('room1')
  original=dict(self.cfg)
  import poolrotater_host.discord_relay.owner_panel as module
  real=module.decorate
  with patch.object(module,'decorate',side_effect=lambda cfg,values:real(cfg,values,path=self.path,now=1000)):
   self.assertTrue(view.update_public_status(**self.values))
  status=shared._take_status(1001)
  self.assertEqual(status.channel_id,1001);self.assertEqual(status.instance,'room1');self.assertEqual(status.channel_base_name,'l1-catch-setdiff')
  self.assertEqual(status.display_name,'Lobby 1 · Catch Setdiff');self.assertTrue(status.image_url);self.assertEqual(dict(self.cfg),original)
 def test_playing_progress_refresh_is_bounded_and_never_calls_provider(self):
  from bot.bot import LobbyBot,PLAYING
  from types import SimpleNamespace
  from unittest.mock import Mock
  control=SimpleNamespace(update_public_status=Mock(return_value=True))
  map=SimpleNamespace(label='Invented',url='https://osu.ppy.sh/b/456')
  bot=SimpleNamespace(discord_control=control,room_id=123,_room_ready=True,running=True,
    state=PLAYING,gameplay_player_ids=lambda:[],non_gameplay_user_ids=lambda:set(),players={},
    playing_map=map,current_map=map,starting_map=None,pool=SimpleNamespace(sr_min=3,sr_max=4),
    cfg=self.cfg,_last_discord_public_status=None)
  with patch('bot.bot.time.monotonic',side_effect=[1,2,21]):
   self.assertTrue(LobbyBot._update_discord_public_status(bot))
   self.assertFalse(LobbyBot._update_discord_public_status(bot))
   self.assertTrue(LobbyBot._update_discord_public_status(bot))
  self.assertEqual(control.update_public_status.call_count,2)
