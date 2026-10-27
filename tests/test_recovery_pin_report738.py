"""Read-only pin reporting shares exact installation and policy admission."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import tempfile,unittest
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from poolrotater_host.layout import file_lease,LeaseBusy
from poolrotater_host.rescue import pin_refresh

class PinReport(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.prefix=Path(temporary.name)/'two';(self.prefix/'recovery').mkdir(parents=True)
        self.record={'bundle_sha256':'a'*64};self.old={'bundle_sha256':'b'*64}
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        def patched(name,**kw):return self.stack.enter_context(patch(name,**kw))
        patched('poolrotater_host.pilot_installation.paths',return_value={'prefix':self.prefix})
        self.binding=patched('poolrotater_host.pilot_installation._administrative_binding',return_value=({}, {},self.record))
        patched('poolrotater_host.rescue.pin_refresh.protected',side_effect=lambda path:Path(path))
        patched('poolrotater_host.rescue.pin_refresh._control',return_value={'record':self.old})
        self.walk=patched('poolrotater_host.rescue.pin_refresh.lineage_diagnostic',return_value={'read_only':True,'status':'not-ancestor'})
    def test_exact_report_under_both_leases_never_writes(self):
        def report(prefix,current,old):
            for path in (prefix.parent/'.install.lock',prefix/'recovery/policy.lock'):
                with self.assertRaises(LeaseBusy):
                    with file_lease(path):pass
            self.assertEqual((current,old),(self.record,self.old))
            return {'read_only':True,'status':'not-ancestor'}
        self.walk.side_effect=report
        with patch.object(pin_refresh,'atomic_json',side_effect=AssertionError('must not write')):
            self.assertEqual(pin_refresh.diagnose_pin('two',self.record)['status'],'not-ancestor')
    def test_changed_registration_refuses_before_walking_history(self):
        self.binding.return_value=({}, {},{'bundle_sha256':'c'*64})
        result=pin_refresh.diagnose_pin('two',self.record)
        self.assertEqual(result['reason'],'recovery_registration_changed');self.walk.assert_not_called()
    def test_busy_install_deferred_without_stale_unlocked_read(self):
        with file_lease(self.prefix.parent/'.install.lock'):
            result=pin_refresh.diagnose_pin('two',self.record)
        self.assertEqual(result['status'],'deferred');self.walk.assert_not_called()
