from pathlib import Path
import json,sys,unittest,time
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
sys.path.insert(0,str(ROOT/'candidate/source'))
FILES=['test_community_reply_latency', 'test_background_player_profiles', 'test_background_profile_rpc', 'test_custom_greetings', 'test_chat_admission_retry', 'test_room_chat_backpressure', 'test_chat_responsiveness', 'test_chat_keepalive_transport', 'test_provider_chat_recovery', 'test_update_chat_resilience', 'test_quieter_chat', 'test_deferred_statistics_reply', 'test_local_statistics_replica', 'test_network_statistics', 'test_activity_personal', 'test_player_profile_admission', 'test_profile_read_resilience', 'test_provider_api_nonblocking', 'test_provider_chat', 'test_headless_multiroom', 'test_community_live_chat_parity', 'test_runtime_defaults_preservation', 'test_closed_failed_update', 'test_rolling.ProtocolTests', 'test_rolling.BrokerTests']
def main():
 start=time.monotonic();suite=unittest.TestSuite()
 for name in FILES:suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 receipt={'ok':result.wasSuccessful(),'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'seconds':time.monotonic()-start,'modules':FILES,'frozen_source':True,'full_repository_suite':False}
 (ROOT/'regression-result.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
 raise SystemExit(0 if result.wasSuccessful()else 1)
if __name__=='__main__':main()
