"""Exact archived committed chains; no inferred launch authority."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from poolrotater_host import worker_releases as releases
from poolrotater_host.config import ConfigError
from poolrotater_host.pilot_upgrade import _transition


class ChainTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.prefix=Path(tmp.name)
        installation=str(uuid.uuid4());generation=str(uuid.uuid4())
        self.registrations=[dict(format=1,installation_id=installation,generation=generation,
            prefix=str(self.prefix),execution='pilot-live',live_ready=False,service_uid=1000,
            host_root='/host',release=str(self.prefix/('release'+str(i))),environment='/environment',
            bundle_sha256=str(i)*64,environment_id='e'*64,release_snapshot={'files':{},'links':{}})for i in range(3)]
        self.first=self.upgrade(0,1);self.second=self.upgrade(1,2)
        self.second['predecessor_transition']=_transition(self.first,'committed')
        self.clean=dict(phase='stopped',operation='closed',room_id=None,ownership_unknown=False)
        self.receipt=dict(format=1,upgrade=self.first,transition=_transition(self.first,'committed'))
        def control(path,*args,**kwargs):
            if Path(path).name=='pilot-upgrade.json':return self.second
            if Path(path).parent.name=='upgrade-history':return self.receipt
            return None
        self.enterContext(patch.object(releases,'_control',side_effect=control))
        self.enterContext(patch.object(releases,'protected',side_effect=lambda p:p))
        self.validate=self.enterContext(patch.object(releases,'validate_runtime'))
        self.enterContext(patch('poolrotater_host.runtime_lifecycle.read_transition',side_effect=lambda *a:_transition(self.second,'committed')))

    def upgrade(self,a,b):
        before,after=self.registrations[a],self.registrations[b]
        return dict(format=1,phase='committed',transition_id=str(uuid.uuid4()),before=before,after=after,
            intent_before={**before},intent_after={**before,'bundle_sha256':after['bundle_sha256']},
            bootstrap_before=b'bootstrap'.hex(),bootstrap_after=b'new bootstrap'.hex(),unit_before=None,unit_after=None)

    def retired(self,row=None):
        return releases.retired_controller(self.registrations[2],Path(self.registrations[0]['release']),row or self.clean)

    def test_exact_two_upgrade_chain_validates_every_runtime_and_retires_closed_pin(self):
        self.assertTrue(self.retired())
        self.assertEqual({call.args[0]['release']for call in self.validate.call_args_list},
                         {r['release']for r in self.registrations})

    def test_missing_unknown_or_active_pin_never_retires(self):
        self.assertFalse(releases.retired_controller(self.registrations[2],Path('/unapproved'),self.clean))
        for change in ({'room_id':7},{'phase':'running'},{'ownership_unknown':True},{'operation':'close_pending'}):
            self.assertFalse(self.retired({**self.clean,**change}))

    def test_positively_closed_parked_pin_retires_without_rewriting_ownership(self):
        row={**self.clean,'phase':'parked'};before=copy.deepcopy(row)
        self.assertTrue(self.retired(row));self.assertEqual(row,before)
        self.assertFalse(self.retired({**row,'operation':'owned'}))

    def test_uncommitted_or_changed_history_refuses(self):
        for change in ({'phase':'prepared'},{'after':self.registrations[0]},
                       {'transition_id':str(uuid.uuid4())}):
            original=copy.deepcopy(self.receipt)
            self.receipt['upgrade']={**self.first,**change}
            with self.subTest(change=change),self.assertRaises(ConfigError):self.retired()
            self.receipt=original

    def test_different_current_transition_or_runtime_bytes_refuse(self):
        with patch('poolrotater_host.runtime_lifecycle.read_transition',return_value=None):
            with self.assertRaises(ConfigError):self.retired()
        self.validate.side_effect=ConfigError('protected runtime changed')
        with self.assertRaises(ConfigError):self.retired()

    def test_aborted_or_rollback_predecessor_never_establishes_ancestry(self):
        for change in ({'phase':'aborted'},{'operation':'rollback'}):
            self.second['predecessor_transition']={**_transition(self.first,'committed'),**change}
            # Aborted link must describe its selected before endpoint; this
            # deliberately inconsistent record is itself refused by _record.
            if change.get('phase')=='aborted':
                with self.assertRaises(ConfigError):self.retired()
            else:self.assertFalse(self.retired())
