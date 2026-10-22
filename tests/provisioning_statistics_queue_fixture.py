"""Real root/service UID ownership checks on the native running-upgrade queue."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('provisioning')
import hashlib,json,os,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from poolrotater_host import controller_update as update
from poolrotater_host.layout import setup,load,atomic_json
from poolrotater_host.managed_host import policy,projection
from poolrotater_host.ratings.operations import _identity
from poolrotater_host.config import ConfigError


class ManagedQueueIdentityTests(unittest.TestCase):
    def setUp(self):
        name=uuid.uuid4().hex;self.root=Path('/state')/name;self.root.mkdir(mode=0o700);os.chown(self.root,1000,1000)
        self.prefix=Path('/runtime')/name;self.prefix.mkdir(mode=0o755)
        with _identity(1000):
            setup(self.root);self.marker,host=load(self.root)
            state=policy.state(self.root);policy.seed(self.root,state);projection.export(state)
            (self.root/'state/rolling').mkdir(mode=0o700)
            atomic_json(self.root/'state/rolling/status.json',{'rooms':{r['alias']:{}for r in host['rooms']}})
            self.expected=projection.read(self.root,self.marker)
        self.parent={**self.marker,'service_uid':1000,'bundle_sha256':'a'*64,
                     'execution':'pilot-live','host_root':str(self.root),'prefix':str(self.prefix),'release':str(self.prefix/'old')}
        self.bundle=self.prefix/'input.zip';self.bundle.write_bytes(b'invented artifact')
        self.pin=hashlib.sha256(self.bundle.read_bytes()).hexdigest()
        candidate={**self.parent,'bundle_sha256':self.pin,'release':str(self.prefix/'new')}
        folder=self.prefix/'candidates'/self.marker['installation_id'];folder.mkdir(parents=True)
        atomic_json(folder/(self.pin+'.json'),candidate)
        self.target={'prefix':self.prefix,'root':self.root}

    def queue(self):
        with patch('poolrotater_host.pilot_installation.administrative_binding',return_value=(self.target,self.marker,self.parent)), \
             patch('poolrotater_host.pilot_operations._service_state',return_value={'MainPID':123,'ActiveState':'active','UnitFileState':'disabled'}), \
             patch('poolrotater_host.network.operations._state',return_value={'ActiveState':'inactive','UnitFileState':'disabled'}), \
             patch('poolrotater_host.updates.native.require_registered_workers'), \
             patch.object(update,'_provision',return_value={'release':str(self.prefix/'new')}), \
             patch.object(update,'validate_runtime',return_value={'manifest':{}}), \
             patch('poolrotater_host.updates.approval.require_approved'), \
             patch.object(update,'_process',return_value={'pid':123,'start':'invented','boot':'invented'}):
            return update.queue('invented',self.bundle,self.pin,from_sha256='a'*64,
                                deployment_id=str(uuid.uuid4()),release_approval_required=True)

    def test_service_owned_projection_is_read_without_chown_or_permission_change(self):
        path=self.root/projection.NAME;before=path.stat()
        with self.assertRaisesRegex(ConfigError,'private, owned'):
            projection.read(self.root,self.marker) # The original root caller fails here.
        result=self.queue();self.assertEqual(result['phase'],'waiting_empty')
        job=json.loads((self.prefix/'controller-update.json').read_text())
        self.assertEqual(job['managed_before'],self.expected)
        after=path.stat();self.assertEqual((before.st_uid,before.st_mode,before.st_ino),(after.st_uid,after.st_mode,after.st_ino))
        self.assertEqual(os.geteuid(),0)

    def test_foreign_owner_is_refused_and_root_identity_is_restored(self):
        path=self.root/projection.NAME;os.chown(path,2000,2000)
        with self.assertRaises((ConfigError,OSError)):self.queue()
        self.assertFalse((self.prefix/'controller-update.json').exists());self.assertEqual(os.geteuid(),0)

    def test_nonprivate_projection_is_refused(self):
        with _identity(1000):(self.root/projection.NAME).chmod(0o644)
        with self.assertRaises((ConfigError,OSError)):self.queue()
        self.assertFalse((self.prefix/'controller-update.json').exists())

if __name__=='__main__':unittest.main()

class BootstrapServiceIdentityTests(unittest.TestCase):
    def test_public_sdk_imports_work_after_switch_and_archive_stays_private(self):
        import importlib.machinery,importlib.util,tempfile
        path=Path(__file__).resolve().parents[1]/'pilot/activate_update_queue.py.template'
        loader=importlib.machinery.SourceFileLoader('bootstrap_uid_fixture',str(path));spec=importlib.util.spec_from_loader(loader.name,loader)
        module=importlib.util.module_from_spec(spec);loader.exec_module(module)
        with tempfile.TemporaryDirectory()as tmp:
            stage=Path(tmp);payload=stage/'payload';payload.mkdir(mode=0o700)
            code=payload/'example.py';code.write_text('value = 42\n');code.chmod(0o600)
            archive=stage/'runtime.zip';archive.write_bytes(b'private transport archive');archive.chmod(0o600)
            with _identity(1000),self.assertRaises(PermissionError):code.read_text()
            module.seal_payload(stage)
            with _identity(1000):
                imported={};exec(compile(code.read_bytes(),str(code),'exec'),imported);self.assertEqual(imported['value'],42)
                with self.assertRaises(PermissionError):code.write_text('changed')
                with self.assertRaises(PermissionError):archive.read_bytes()
                with self.assertRaises(PermissionError):(payload/'injected.py').write_text('changed')
            self.assertEqual(os.geteuid(),0)
