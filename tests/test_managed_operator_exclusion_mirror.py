"""Private local chart IDs reach every versioned signed reader scope."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,json,os,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from poolrotater_host.catalog_operator_exclusions import read_policy,merge_policy
from poolrotater_host.catalog_publication import _default_feed,_cumulative,_legacy_feed,_scoped_feed
from poolrotater_host.catalog_export import MODES,CORE_MODES
from poolrotater_host.config import ConfigError
from poolrotater_host.catalog_service import units,EXCLUSIONS,EXCLUSIONS_TIMER,REFRESH,UNIT

class OperatorExclusionMirror(unittest.TestCase):
 def write(self,folder,value):
  path=folder/'policy.json';path.write_text(json.dumps(value));path.chmod(0o600);return path
 def test_all_sixteen_chart_ids_without_private_evidence(self):
  with tempfile.TemporaryDirectory() as temporary:
   p=self.write(Path(temporary),{'schema':1,'exclusions':[{'beatmap_id':i,'evidence':'private chat canary','password':'secret canary','ruleset_id':3} for i in range(1,17)]})
   data=read_policy(p,os.geteuid());self.assertEqual(data['ids'],list(range(1,17)))
   self.assertNotIn('canary',json.dumps(data));self.assertEqual(len(data['source_sha256']),64)
   feed=merge_policy(_default_feed(),data['ids'])
   for bid in range(1,17):
    self.assertEqual({m for e in feed['events'] if e['id']==bid for m in e['modes']},set(MODES))
   self.assertNotIn('canary',json.dumps(feed))
 def test_repeat_is_idempotent_and_removal_never_unblocks_signed_history(self):
  feed=merge_policy(_default_feed(),[1,2,1549496]);saved=_cumulative(_default_feed(),feed)
  self.assertEqual(merge_policy(saved,[1,2,1549496]),saved)
  self.assertEqual(merge_policy(saved,[1]),saved)
  new=merge_policy(saved,[1,2,3]);self.assertEqual(new['revision'],saved['revision']+1)
  self.assertTrue(any(e['id']==1549496 for e in new['events']))
 def test_existing_set_bindings_and_reasons_are_retained(self):
  before=_default_feed();event={'kind':'beatmapset','id':987,'modes':['standard'],'action':'exclude','reason':'corrupt',
   'memberships':{'standard':[{'content_id':'a'*64,'beatmap_ids':[1,2]}]}}
  before['events'].append(event);after=merge_policy(before,[2]);self.assertIn(event,after['events']);self.assertEqual(before['revision'],1)
 def test_legacy_catch_and_loved_targets_preserve_scope_compatibility(self):
  feed=_cumulative(None,merge_policy(_default_feed(),list(range(1,17))))
  for selected,modes in ((_legacy_feed(feed),set(CORE_MODES)),(_scoped_feed(feed,set(CORE_MODES)|{'catch'}),set(CORE_MODES)|{'catch'}),(_scoped_feed(feed,{'loved'}),{'loved'})):
   self.assertTrue(all(set(e['modes'])<=modes for e in selected['events']))
   for bid in range(1,17):self.assertEqual({m for e in selected['events'] if e['id']==bid for m in e['modes']},modes)
 def test_malformed_private_policy_refuses_without_partial_selection(self):
  bad=[{'schema':True,'exclusions':[]},{'schema':1,'exclusions':[{'beatmap_id':True}]},
       {'schema':1,'exclusions':[{'beatmap_id':-1}]},{'schema':1,'exclusions':[{'beatmap_id':1}]*257},
       {'schema':1,'exclusions':[],'other_secret':'hidden'}]
  with tempfile.TemporaryDirectory() as temporary:
   for value in bad:
    with self.subTest(value=value),self.assertRaises(ConfigError):read_policy(self.write(Path(temporary),value),os.geteuid())
 def test_symlink_multiple_links_and_writable_source_refuse(self):
  with tempfile.TemporaryDirectory() as temporary:
   root=Path(temporary);p=self.write(root,{'schema':1,'exclusions':[]});link=root/'link';link.symlink_to(p)
   with self.assertRaises(ConfigError):read_policy(link,os.geteuid())
   link.unlink();os.link(p,link)
   with self.assertRaises(ConfigError):read_policy(p,os.geteuid())
   link.unlink();p.chmod(0o666)
   with self.assertRaises(ConfigError):read_policy(p,os.geteuid())
 def test_periodic_sync_is_offline_bounded_and_does_not_restart_listeners(self):
  text=units()[EXCLUSIONS]
  for token in (' sync-exclusions','PrivateNetwork=yes','User=root','Nice=17','CPUQuota=50%'):self.assertIn(token,text)
  self.assertNotIn('systemctl',text);self.assertIn('OnUnitInactiveSec=5min',units()[EXCLUSIONS_TIMER])
 def test_publishers_can_read_private_owner_source_without_listener_capabilities(self):
  profiles=units()
  for name in (REFRESH,EXCLUSIONS):
   with self.subTest(name=name):
    self.assertIn('CapabilityBoundingSet=CAP_DAC_READ_SEARCH\n',profiles[name])
    self.assertIn('AmbientCapabilities=\n',profiles[name])
    self.assertNotIn('CAP_DAC_OVERRIDE',profiles[name])
  self.assertIn('CapabilityBoundingSet=\n',profiles[UNIT])
  self.assertNotIn('CAP_DAC_READ_SEARCH',profiles[UNIT])
 def test_invalid_ids_and_budget_cannot_replace_current_feed(self):
  current=_default_feed();before=copy.deepcopy(current)
  for ids in ([True],[-1],list(range(1,258))):
   with self.subTest(ids=ids),self.assertRaises(ConfigError):merge_policy(current,ids)
  self.assertEqual(current,before)

if __name__=='__main__':unittest.main()
