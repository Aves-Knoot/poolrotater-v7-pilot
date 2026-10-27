"""Real atomic receipts retain intent across interruption and fence repeat start."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from poolrotater_host.config import ConfigError
from poolrotater_host.recreation_repair_receipt import Receipt,validate
from poolrotater_host.recreation_repair_helpers import Helpers
import test_recreation_repair_helpers738 as helper_tests


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.prefix=Path(temp.name)
        self.current={k:'invented-'+k for k in ('installation_id','generation','prefix','host_root','execution')}
        self.current.update(service_uid=1000,bundle_sha256='a'*64)
        self.helpers=Helpers('two',helper_tests.Ops());self.snapshot=self.helpers.snapshot()
        self.receipt=Receipt(self.prefix,'two',self.current,'b'*64,self.helpers)
    def ready(self):
        value=self.receipt.begin(self.snapshot)
        for phase in ('helpers_paused','upgraded','ownership_review','ready'):
            value=self.receipt.advance(value,phase)
        return value
    def test_restart_retains_original_helpers_even_when_currently_paused(self):
        first=self.receipt.begin(self.snapshot);self.helpers.pause(self.snapshot)
        restarted=Receipt(self.prefix,'two',self.current,'b'*64,self.helpers)
        second=restarted.begin(self.helpers.snapshot())
        self.assertEqual(first,second);self.assertEqual(second['helpers'],self.snapshot)
    def test_start_marker_is_durable_and_cannot_be_admitted_twice(self):
        value=self.ready()
        started=self.receipt.advance(value,'startup_attempted',journal_sha256='c'*64)
        restarted=Receipt(self.prefix,'two',{**self.current,'bundle_sha256':'b'*64},'b'*64,self.helpers)
        resumed=restarted.begin(self.snapshot)
        self.assertTrue(resumed['startup_attempted']);self.assertEqual(started,resumed)
        with self.assertRaises(ConfigError):restarted.advance(resumed,'startup_attempted')
        with self.assertRaises(ConfigError):restarted.advance(resumed,'ready')
    def test_stale_report_cannot_overwrite_an_admitted_start(self):
        value=self.ready();self.receipt.advance(value,'startup_attempted',journal_sha256='c'*64)
        with self.assertRaises(ConfigError):self.receipt.advance(value,'requires_review')
    def test_unknown_creation_never_implies_ready(self):
        value=self.receipt.begin(self.snapshot)
        value=self.receipt.advance(value,'helpers_paused')
        value=self.receipt.advance(value,'ownership_review')
        value=self.receipt.advance(value,'requires_upstream')
        with self.assertRaises(ConfigError):self.receipt.advance(value,'startup_attempted',journal_sha256='c'*64)
        self.assertFalse(value['startup_attempted'])
    def test_other_binding_bundle_or_mutated_original_preferences_refuse(self):
        value=self.receipt.begin(self.snapshot)
        for changed in ({**self.current,'generation':'foreign'},
                        {**self.current,'bundle_sha256':'d'*64}):
            with self.assertRaises(ConfigError):validate(value,'two',changed,'b'*64)
        changed=copy.deepcopy(value);changed['helpers']['network']['UnitFileState']='disabled'
        with self.assertRaises(ConfigError):validate(changed,'two',self.current,'b'*64)
    def test_start_requires_fresh_review_digest_before_any_durable_admission(self):
        value=self.ready()
        with self.assertRaises(ConfigError):self.receipt.advance(value,'startup_attempted')
        self.assertEqual(self.receipt.begin(self.snapshot),value)
    def test_failed_atomic_write_cannot_be_reported_as_admitted(self):
        value=self.ready()
        with patch('poolrotater_host.recreation_repair_receipt.atomic_json',side_effect=OSError('injected disk failure')):
            with self.assertRaises(OSError):self.receipt.advance(value,'startup_attempted',journal_sha256='c'*64)
        self.assertFalse(self.receipt.begin(self.snapshot)['startup_attempted'])

    def test_two_concurrent_continuations_cannot_both_admit_start(self):
        import concurrent.futures
        value=self.ready()
        def claim(_):
            try:self.receipt.advance(value,'startup_attempted',journal_sha256='c'*64);return True
            except ConfigError:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2)as executor:
            results=list(executor.map(claim,range(2)))
        self.assertEqual(sum(results),1)
        self.assertTrue(self.receipt.begin(self.snapshot)['startup_attempted'])

    def test_finished_predecessor_is_archived_before_new_repair(self):
        value=self.ready();value=self.receipt.advance(value,'startup_attempted',journal_sha256='c'*64)
        value=self.receipt.advance(value,'complete')
        next_receipt=Receipt(self.prefix,'two',{**self.current,'bundle_sha256':'b'*64},'d'*64,self.helpers)
        next_value=next_receipt.begin(self.snapshot)
        self.assertEqual(next_value['phase'],'prepared');self.assertFalse(next_value['startup_attempted'])
        from poolrotater_host.layout import read_json
        archive=self.prefix/'recreation-repair-history'/(value['operation_id']+'.json')
        self.assertEqual(read_json(archive),value)
    def test_incomplete_repair_cannot_be_retargeted_to_a_different_bundle(self):
        self.receipt.begin(self.snapshot)
        other=Receipt(self.prefix,'two',self.current,'d'*64,self.helpers)
        with self.assertRaises(ConfigError):other.begin(self.snapshot)
    def test_load_validates_existing_receipt_without_current_stopped_snapshot(self):
        value=self.ready()
        self.assertEqual(self.receipt.load(),value)
