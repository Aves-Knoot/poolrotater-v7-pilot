"""Kit entry reuses validated archive primitives and exposes only safe errors."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import hashlib,json,os,stat,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pilot import recreation_repair as entry,repair_and_start as primitives
from poolrotater_host.config import ConfigError

class Kit(unittest.TestCase):
    def test_verified_public_sdk_is_readable_without_exposing_other_files(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);source=root/'payload';nested=source/'package'
            nested.mkdir(parents=True,mode=0o700);code=nested/'module.py';code.write_bytes(b'public code')
            entry.readable_public_sdk(root,source,primitives)
            for path in (root,source,nested):self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o755)
            self.assertEqual(stat.S_IMODE(code.stat().st_mode),0o444)
            self.assertEqual(code.read_bytes(),b'public code')

    def test_public_sdk_refuses_siblings_links_and_multiple_file_custody(self):
        for kind in ('sibling','symlink','hardlink'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory()as folder:
                root=Path(folder);source=root/'payload';source.mkdir(mode=0o700)
                outside=root/'outside';outside.write_bytes(b'private sentinel');outside.chmod(0o600)
                if kind=='symlink':
                    outside.rename(root.parent/(root.name+'-sentinel'))
                    sentinel=root.parent/(root.name+'-sentinel')
                    (source/'link').symlink_to(sentinel)
                elif kind=='hardlink':
                    sentinel=root.parent/(root.name+'-sentinel');outside.rename(sentinel)
                    os.link(sentinel,source/'linked')
                else:sentinel=outside
                try:
                    with self.assertRaises(primitives.RepairError):
                        entry.readable_public_sdk(root,source,primitives)
                    self.assertEqual(sentinel.read_bytes(),b'private sentinel')
                    self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode),0o600)
                finally:
                    if sentinel.parent!=root:sentinel.unlink()

    def test_environment_comes_from_verified_registration_and_exact_supported_bytes(self):
        release={'runtime_sha256':'a'*64,'supported_predecessors':[{'sha256':'b'*64}]}
        record={'execution':'pilot-live','bundle_sha256':'b'*64,
                'environment':'/verified/instance/environments/sealed'}
        with patch('poolrotater_host.pilot_installation.administrative_binding',
                   return_value=({}, {}, record))as binding:
            self.assertEqual(entry.registered_environment('fixture',release,primitives),
                (Path(record['environment'])/'bin/python',Path(record['environment'])))
            binding.assert_called_once_with('fixture')
        for change in ({'execution':'pilot-qualification'},{'bundle_sha256':'c'*64}):
            with self.subTest(change=change),patch(
                    'poolrotater_host.pilot_installation.administrative_binding',
                    return_value=({}, {}, {**record,**change})):
                with self.assertRaises(primitives.RepairError):
                    entry.registered_environment('fixture',release,primitives)

    def test_failed_inventory_verification_never_supplies_an_interpreter(self):
        with patch('poolrotater_host.pilot_installation.administrative_binding',
                   side_effect=ConfigError('protected runtime environment changed')):
            with self.assertRaises(ConfigError):
                entry.registered_environment('fixture',{},primitives)

    def test_verified_stage_requires_registered_prefix_executable_and_enabled_site_packages(self):
        environment=Path('/verified/environment');python=environment/'bin/python'
        for executable,prefix,isolated,no_site,bytecode,expected in (
            (str(python),str(environment),1,0,True,True),
            ('/usr/bin/python3.12',str(environment),1,0,True,False),
            (str(python),'/usr',1,0,True,False),
            (str(python),str(environment),0,0,True,False),
            (str(python),str(environment),1,1,True,False),
            (str(python),str(environment),1,0,False,False)):
            with self.subTest(executable=executable,prefix=prefix,isolated=isolated,
                              no_site=no_site,bytecode=bytecode), \
                    patch.object(entry.sys,'executable',executable), \
                    patch.object(entry.sys,'prefix',prefix), \
                    patch.object(entry.sys,'flags',SimpleNamespace(isolated=isolated,no_site=no_site)), \
                    patch.object(entry.sys,'dont_write_bytecode',bytecode):
                self.assertEqual(bool(entry.interpreter_matches(python,environment)),expected)

    def test_failure_details_keep_static_frames_without_messages_or_locals(self):
        try:
            try:raise RuntimeError('secret=private-canary')
            except RuntimeError as original:raise ConfigError('provider response private-canary')from original
        except ConfigError as error:
            detail=entry.failure_detail(error)
        self.assertEqual([row['type']for row in detail],['ConfigError','RuntimeError'])
        self.assertNotIn('private-canary',json.dumps(detail))
        self.assertNotIn('secret=',json.dumps(detail))
        error=ConfigError('install the hash-pinned optional catalog verification dependencies')
        self.assertEqual(entry.failure_code(error),'repair_registered_dependencies_unavailable')

    def test_reused_kit_verifier_accepts_only_explicit_version_and_all_exact_bytes(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);runtime=b'invented runtime';(root/'runtime.zip').write_bytes(runtime)
            digest=hashlib.sha256(runtime).hexdigest()
            manifest={'runtime_version':'v7.3.8','runtime_sha256':digest,
                      'files':{'runtime.zip':{'bytes':len(runtime),'sha256':digest}}}
            (root/'release.json').write_text(json.dumps(manifest))
            self.assertEqual(primitives.verify_kit(root,expected_version=entry.EXPECTED_VERSION),manifest)
            with self.assertRaises(primitives.RepairError):primitives.verify_kit(root)
            (root/'runtime.zip').write_bytes(b'changed')
            with self.assertRaises(primitives.RepairError):primitives.verify_kit(root,expected_version=entry.EXPECTED_VERSION)
    def test_safe_console_codes_never_echo_provider_or_filesystem_messages(self):
        self.assertEqual(entry.failure_code(ConfigError('repair_original_backup_missing')),'repair_original_backup_missing')
        for error in (RuntimeError('secret=private-canary'),ConfigError('provider response private-canary'),
                      OSError('private-canary')):
            self.assertNotIn('private-canary',entry.failure_code(error))
    def test_legacy_verification_default_remains_dynamic(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);data=b'fixture';(root/'runtime.zip').write_bytes(data)
            digest=hashlib.sha256(data).hexdigest()
            (root/'release.json').write_text(json.dumps({'runtime_version':'invented',
                'runtime_sha256':digest,'files':{'runtime.zip':{'bytes':len(data),'sha256':digest}}}))
            with patch.object(primitives,'EXPECTED_VERSION','invented'):
                self.assertEqual(primitives.verify_kit(root)['runtime_version'],'invented')
