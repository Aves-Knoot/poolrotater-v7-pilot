"""Community cards keep authoritative timing while labelling 1x freemod estimates."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,unittest
from website.community.display_model import playback

class FreemodPlaybackTests(unittest.TestCase):
 def setUp(self):
  self.current={'beatmap_id':123,'total_length':120};self.match={'beatmap_id':123,'playlist_item_id':5,'started_at':970.,'clock_rate':None,'participants':None,'clock_skew_seconds':0}
  self.settings={'freestyle':True,'required_mods':[]}
 def run_playback(self,**kw):return playback('playing',self.current,self.match,now=1000,observed_at=999,fresh=True,settings=self.settings,**kw)
 def test_freemod_nominal_estimate_uses_real_match_start(self):
  p=self.run_playback();self.assertTrue(p['nominal_speed']);self.assertEqual(p['duration_seconds'],120);self.assertEqual(p['started_at'],970)
 def test_forced_dt_uses_actual_speed_instead_of_nominal(self):
  self.settings={'freestyle':False,'required_mods':['DT']};self.match['clock_rate']=1.5
  p=self.run_playback();self.assertFalse(p['nominal_speed']);self.assertEqual(p['duration_seconds'],80)
 def test_unknown_required_speed_and_incomplete_evidence_stay_unavailable(self):
  self.settings['required_mods']=['DT'];self.assertIsNone(self.run_playback())
  self.settings['required_mods']=[];self.match['beatmap_id']=999;self.assertIsNone(self.run_playback())
 def test_nominal_freemod_does_not_invent_missing_start_or_stale_progress(self):
  self.match['started_at']=None;self.assertIsNone(self.run_playback())
  self.match['started_at']=1006;self.assertIsNone(self.run_playback())
 def test_existing_strict_call_without_settings_keeps_unknown_speed_unavailable(self):
  self.assertIsNone(playback('playing',self.current,self.match,now=1000,observed_at=999,fresh=True))
