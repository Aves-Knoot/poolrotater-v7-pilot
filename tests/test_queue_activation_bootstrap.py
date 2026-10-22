"""Pinned SDK extraction and fixed download boundaries for one-time queue admission."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import hashlib,importlib.machinery,importlib.util,stat,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch

class BootstrapTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[1]/'pilot/activate_update_queue.py.template'
        loader=importlib.machinery.SourceFileLoader('queue_bootstrap_fixture',str(path))
        spec=importlib.util.spec_from_loader(loader.name,loader);self.module=importlib.util.module_from_spec(spec);loader.exec_module(self.module)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def archive(self,name='payload/example.py',body=b'example',mode=stat.S_IFREG|0o600):
        path=self.root/'runtime.zip'
        with zipfile.ZipFile(path,'w')as z:
            info=zipfile.ZipInfo(name);info.external_attr=mode<<16;z.writestr(info,body)
        self.module.SHA256=hashlib.sha256(path.read_bytes()).hexdigest();return path
    def test_only_pinned_https_github_destinations_are_admitted(self):
        for url in ('https://github.com/a','https://release-assets.githubusercontent.com/a'):self.module.allowed(url)
        for url in ('http://github.com/a','https://example.com/a','https://u:p@github.com/a','https://github.com:444/a'):
            with self.subTest(url=url),self.assertRaises(ValueError):self.module.allowed(url)
    def test_changed_bytes_fail_before_extraction(self):
        archive=self.archive();self.module.SHA256='0'*64
        with self.assertRaisesRegex(ValueError,'digest_mismatch'):self.module.unpack(archive,self.root/'out')
        self.assertFalse((self.root/'out').exists())
    def test_parent_escape_and_symlink_members_are_refused(self):
        for name,mode in (('../outside',stat.S_IFREG|0o600),('payload/link',stat.S_IFLNK|0o700)):
            with self.subTest(name=name):
                archive=self.archive(name=name,mode=mode)
                with self.assertRaisesRegex(ValueError,'archive_refused'):self.module.unpack(archive,self.root/'out')
    def test_valid_payload_is_extracted_without_running_it(self):
        archive=self.archive(body=b'raise RuntimeError("not executed")')
        self.module.unpack(archive,self.root/'out');self.assertTrue((self.root/'out/payload/example.py').is_file())
    def test_expanded_quota_is_checked_before_writes(self):
        archive=self.archive(body=b'x'*100)
        with patch.object(self.module,'LIMIT',99),self.assertRaisesRegex(ValueError,'archive_refused'):self.module.unpack(archive,self.root/'out')

    def test_sealed_sdk_can_be_read_after_uid_switch_but_is_not_writable(self):
        archive=self.archive();stage=self.root/'stage';stage.mkdir(mode=0o700)
        self.module.unpack(archive,stage);self.module.seal_payload(stage)
        self.assertEqual(stage.stat().st_mode&0o777,0o711)
        self.assertEqual((stage/'payload').stat().st_mode&0o777,0o755)
        self.assertEqual((stage/'payload/example.py').stat().st_mode&0o777,0o444)
        self.assertFalse(archive.stat().st_mode&0o022)
