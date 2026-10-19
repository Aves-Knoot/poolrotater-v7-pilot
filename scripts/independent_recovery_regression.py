from pathlib import Path
import json,sys,unittest,time
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
sys.path.insert(0,str(ROOT/'candidate/source'))
FILES=['test_failed_service_recovery', 'test_independent_recovery', 'test_recovery_successor_dispatch', 'test_recovery_https', 'test_update_recovery', 'test_update_agent', 'test_update_native_status', 'test_update_retention', 'test_update_configuration', 'test_update_approval', 'test_update_telemetry', 'test_update_https', 'test_update_launcher', 'test_network_operations', 'test_network_admin', 'test_network_client', 'test_network_store', 'test_network_installed', 'test_reporting_backup', 'test_reporting_worker_boundary', 'test_reporting_integration', 'test_pilot_upgrade', 'test_closed_failed_update']
def main():
 start=time.monotonic();suite=unittest.TestSuite()
 for name in FILES:suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 receipt={'ok':result.wasSuccessful(),'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'seconds':time.monotonic()-start,'modules':FILES,'frozen_source':True,'full_repository_suite':False}
 (ROOT/'independent-recovery-regression.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
 raise SystemExit(0 if result.wasSuccessful()else 1)
if __name__=='__main__':main()
