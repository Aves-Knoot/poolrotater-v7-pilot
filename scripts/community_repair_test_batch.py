from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import sys,unittest
sys.path.insert(0,'tests')
names=['test_profile_read_resilience','test_deferred_statistics_reply','test_closed_failed_update','test_community_live_chat_parity',
 'test_community_freemod_display','test_player_profile_admission','test_activity_commands','test_activity_personal','test_local_statistics_replica',
 'test_v7_runtime','test_provider_chat','test_provider_chat_recovery','test_chat_responsiveness','test_chat_admission_retry','test_v7_provider_api',
 'test_controller_commit','test_controller_retired_target','test_update_recovery','test_v710_reliability',
 'test_community_display_model','test_community_display_integration','test_network_display','test_network_projection',
 'test_live_presentation','test_community_dom']
if __name__ == "__main__":
 suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n)for n in names)
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 sys.exit(not result.wasSuccessful())
