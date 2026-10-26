"""Focused offline tests for future-only recovery pin refresh."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import json
import os
from contextlib import nullcontext
from pathlib import Path
import sys
import stat
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poolrotater_host import pilot_upgrade, runtime_lifecycle
from poolrotater_host.rescue import pin_refresh


def _registration(installation, generation, root, prefix, uid, label):
    bundle = (label[0].lower() * 64)
    return {'format':1,'installation_id':installation,'generation':generation,
        'prefix':str(prefix),'execution':'pilot-live','live_ready':False,'service_uid':uid,
        'host_root':str(root),'release':str(prefix/'releases'/('v7.3.3-'+bundle[:16])),
        'environment':str(prefix/'environments'/('e'*64)),'bundle_sha256':bundle,
        'environment_id':'e'*64,'release_snapshot':{'files':{},'links':{}}}


def _upgrade(before, after, *, predecessor=None):
    transition_id = str(uuid.uuid4())
    intent_before={'execution':'pilot-live','installation_id':before['installation_id'],
        'generation':before['generation'],'host_root':before['host_root'],
        'service_uid':before['service_uid'],'bundle_sha256':before['bundle_sha256']}
    return {'format':1,'phase':'committed','transition_id':transition_id,
        'before':before,'after':after,'intent_before':intent_before,
        'intent_after':{**intent_before,'bundle_sha256':after['bundle_sha256']},
        'bootstrap_before':b'bootstrap-before'.hex(),
        'bootstrap_after':b'bootstrap-after'.hex(),'unit_before':None,'unit_after':None,
        **({'predecessor_transition':predecessor} if predecessor is not None else {})}


class RecoveryPinRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base=Path(self.temporary.name)
        self.prefix=base/'prefix';self.root=base/'state'
        self.prefix.mkdir();self.root.mkdir()
        (self.prefix/'recovery').mkdir(mode=0o700)
        (self.prefix/'recovery/policy.lock').touch(mode=0o600)
        self.installation=str(uuid.uuid4());self.generation=str(uuid.uuid4())
        self.old=_registration(self.installation,self.generation,self.root,self.prefix,954,'a')
        self.mid=_registration(self.installation,self.generation,self.root,self.prefix,954,'b')
        self.current=_registration(self.installation,self.generation,self.root,self.prefix,954,'c')
        self.first=_upgrade(self.old,self.mid)
        first_transition=pilot_upgrade._transition(self.first,'committed')
        self.latest=_upgrade(self.mid,self.current,predecessor=first_transition)
        self.latest_transition=pilot_upgrade._transition(self.latest,'committed')
        self.history=self.prefix/'upgrade-history';self.history.mkdir(mode=0o755)
        self.history_path=self.history/(self.first['transition_id']+'.json')
        self.history_path.write_text('{}');self.history_path.chmod(0o600)
        self.row={'format':1,'upgrade':self.first,'transition':first_transition}
        self.launcher=b'unchanged protected recovery bootstrap\n'
        for record in (self.old,self.current):
            source=Path(record['release'])/'poolrotater_host'
            source.mkdir(parents=True)
            (source/'recovery_bootstrap.py').write_bytes(self.launcher)
        (self.prefix/'recovery-bootstrap.py').write_bytes(self.launcher)
        self.config={'format':1,'binding':{'installation_id':self.installation,
            'generation':self.generation,'host_id':'host-opaque'},'record':self.old,
            'enabled':False,'allow_recovery':False,'custom_policy':{'retain':17}}
        self.config_path=self.prefix/'recovery/configuration.json'
        self.config_path.write_text(json.dumps(self.config));self.config_path.chmod(0o600)
        self.target={'root':self.root,'prefix':self.prefix,'unit':'unused.service'}
        self.marker={'format':1,'installation_id':self.installation,'generation':self.generation}
        self.control_values={str(self.prefix/'pilot-upgrade.json'):self.latest,
            str(self.history_path):self.row,
            str(self.prefix/'controller-update.json'):None}

    def _control(self,path,*args,**kwargs):
        path=Path(path)
        if path==self.config_path:
            return json.loads(path.read_text())
        return self.control_values.get(str(path))

    def _locked_refresh(self):
        with patch.object(pin_refresh,'protected',side_effect=lambda path:Path(path)), \
             patch.object(pin_refresh,'_control',side_effect=self._control), \
             patch.object(runtime_lifecycle,'read_transition',return_value=self.latest_transition), \
             patch.object(runtime_lifecycle,'require_no_pending'), \
             patch.object(runtime_lifecycle,'validate_runtime',return_value={}), \
             patch.object(pin_refresh.os,'geteuid',return_value=0):
            return pin_refresh._refresh_locked('first',self.target,self.marker,self.current,self.config_path)

    def test_exact_committed_ancestor_refreshes_only_record_and_preserves_policy(self):
        result=self._locked_refresh()
        self.assertEqual(result,{'status':'refreshed','pin_refreshed':True})
        refreshed=json.loads(self.config_path.read_text())
        self.assertEqual(refreshed,{**self.config,'record':self.current})
        self.assertEqual((self.prefix/'recovery-bootstrap.py').read_bytes(),self.launcher)
        with patch.object(pin_refresh,'protected',side_effect=lambda path:Path(path)), \
             patch.object(pin_refresh,'_control',side_effect=self._control), \
             patch.object(runtime_lifecycle,'require_no_pending'), \
             patch.object(pin_refresh.os,'geteuid',return_value=0), \
             patch.object(pin_refresh,'_upgrade_lineage',side_effect=AssertionError('no-op must not walk history')), \
             patch.object(runtime_lifecycle,'validate_runtime',side_effect=AssertionError('current pin already verified')), \
             patch('poolrotater_host.layout.atomic_json') as atomic:
            result=pin_refresh._refresh_locked('first',self.target,self.marker,self.current,self.config_path)
        self.assertEqual(result,{'status':'current','pin_refreshed':False})
        atomic.assert_not_called()

    def test_history_gap_or_foreign_pin_refuses_without_write(self):
        before=self.config_path.read_bytes()
        self.latest['predecessor_transition']=dict(self.latest['predecessor_transition'])
        self.latest['predecessor_transition']['transition_id']=str(uuid.uuid4())
        self.latest_transition=pilot_upgrade._transition(self.latest,'committed')
        with self.assertRaises(pin_refresh._Refusal) as raised:
            self._locked_refresh()
        self.assertEqual(raised.exception.reason,'upgrade_history_gap')
        self.assertEqual(self.config_path.read_bytes(),before)

        self.latest['predecessor_transition']=pilot_upgrade._transition(self.first,'committed')
        foreign=_registration(str(uuid.uuid4()),self.generation,self.root,self.prefix,954,'d')
        self.config['record']=foreign
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)
        before=self.config_path.read_bytes()
        with self.assertRaises(pin_refresh._Refusal):
            self._locked_refresh()
        self.assertEqual(self.config_path.read_bytes(),before)

    def test_old_runtime_inventory_and_bootstrap_bytes_are_mandatory(self):
        before=self.config_path.read_bytes()
        def changed_inventory(record,prefix):
            if record==self.old:
                raise ValueError('inventory drift')
        with patch.object(pin_refresh,'protected',side_effect=lambda path:Path(path)), \
             patch.object(pin_refresh,'_control',side_effect=self._control), \
             patch.object(runtime_lifecycle,'read_transition',return_value=self.latest_transition), \
             patch.object(runtime_lifecycle,'require_no_pending'), \
             patch.object(runtime_lifecycle,'validate_runtime',side_effect=changed_inventory), \
             patch.object(pin_refresh.os,'geteuid',return_value=0):
            with self.assertRaises(ValueError):
                pin_refresh._refresh_locked('first',self.target,self.marker,self.current,self.config_path)
        self.assertEqual(self.config_path.read_bytes(),before)

        old_path=Path(self.old['release'])/'poolrotater_host/recovery_bootstrap.py'
        old_path.write_bytes(b'altered launcher')
        with self.assertRaises(pin_refresh._Refusal) as raised:
            self._locked_refresh()
        self.assertEqual(raised.exception.reason,'recovery_launcher_changed')
        self.assertEqual(self.config_path.read_bytes(),before)

    def test_active_controller_job_and_busy_lock_defer(self):
        active={'format':1,'id':str(uuid.uuid4()),'phase':'committing',
            'before':self.current,'after':self.current}
        with patch.object(pin_refresh,'_control',return_value=active):
            with self.assertRaises(pin_refresh._Refusal) as raised:
                pin_refresh._terminal_controller_job(self.prefix,self.current,set())
        self.assertEqual(raised.exception.reason,'controller_job_active')

        target=self.target
        lock=self.prefix/'recovery/policy.lock'
        install_lock=self.prefix.parent/'.install.lock'
        original_lstat=Path.lstat
        def lstat(path,*args,**kwargs):
            if Path(path)==lock:
                return SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_nlink=1,st_uid=0)
            return original_lstat(path,*args,**kwargs)
        def file_lease(path,timeout=0):
            if Path(path)==install_lock:return nullcontext()
            raise pin_refresh.LeaseBusy('busy')
        with patch.object(pin_refresh.os,'geteuid',return_value=0), \
             patch.dict(os.environ,{'POOLROTATER_OFFLINE_SANDBOX':'0'}), \
             patch('poolrotater_host.pilot_installation.paths',return_value=target), \
             patch.object(pin_refresh,'protected',side_effect=lambda path:Path(path)), \
             patch.object(Path,'lstat',autospec=True,side_effect=lstat), \
             patch.object(pin_refresh,'file_lease',side_effect=file_lease):
            result=pin_refresh.refresh_pin('first',self.current)
        self.assertEqual(result,{'status':'deferred','reason':'policy_lock_busy','pin_refreshed':False})

        admin=Mock()
        with patch.object(pin_refresh.os,'geteuid',return_value=0), \
             patch.dict(os.environ,{'POOLROTATER_OFFLINE_SANDBOX':'0'}), \
             patch('poolrotater_host.pilot_installation.paths',return_value=target), \
             patch('poolrotater_host.pilot_installation._administrative_binding',admin), \
             patch.object(pin_refresh,'protected',side_effect=lambda path:Path(path)), \
             patch.object(Path,'lstat',autospec=True,side_effect=lstat), \
             patch.object(pin_refresh,'file_lease',side_effect=pin_refresh.LeaseBusy('install busy')):
            result=pin_refresh.refresh_pin('first',self.current)
        self.assertEqual(result,{'status':'deferred','reason':'install_in_progress','pin_refreshed':False})
        admin.assert_not_called()

    def test_existing_unsafe_recovery_directory_is_not_reported_absent(self):
        target=self.target
        (self.prefix/'recovery').mkdir(mode=0o700,exist_ok=True)
        with patch.object(pin_refresh.os,'geteuid',return_value=0), \
             patch.dict(os.environ,{'POOLROTATER_OFFLINE_SANDBOX':'0'}), \
             patch('poolrotater_host.pilot_installation.paths',return_value=target), \
             patch.object(pin_refresh,'protected',side_effect=ValueError('unsafe private path')):
            result=pin_refresh.refresh_pin('first',self.current)
        self.assertEqual(result,{'status':'refused','reason':'recovery_directory_requires_review',
                                 'pin_refreshed':False})

    def test_completed_job_with_historical_drain_needs_no_cancel_restore_receipt(self):
        complete={'format':1,'id':str(uuid.uuid4()),'phase':'complete',
            'before':self.mid,'after':self.current,'completed_at':123.0,
            'controller_update_drain':{'format':1,'historical':True}}
        with patch.object(pin_refresh,'_control',return_value=complete):
            pin_refresh._terminal_controller_job(self.prefix,self.current,[self.mid,self.current])

        cancelled={'format':1,'id':str(uuid.uuid4()),'phase':'cancelled',
            'before':self.current,'after':self.mid,
            'controller_update_drain':{'format':1,'historical':True}}
        with patch.object(pin_refresh,'_control',return_value=cancelled):
            with self.assertRaises(pin_refresh._Refusal) as raised:
                pin_refresh._terminal_controller_job(self.prefix,self.current,[self.current])
        self.assertEqual(raised.exception.reason,'controller_drain_unsettled')


if __name__ == '__main__':
    unittest.main()
