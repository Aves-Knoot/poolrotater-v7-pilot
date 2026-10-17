"""Recognize a manual repair without offering it to other channel followers."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import hashlib
import json
import unittest
from unittest.mock import patch
from scripts.register_release_without_promotion import register
from poolrotater_host.config import ConfigError
from poolrotater_host.updates.trust import verify
from test_update_release_delivery import ReleaseDeliveryTests, spec


class RegistrationTests(unittest.TestCase):
    setUp=ReleaseDeliveryTests.setUp
    envelope=ReleaseDeliveryTests.envelope
    client=ReleaseDeliveryTests.client
    serve=ReleaseDeliveryTests.serve

    def prepare(self):
        for row in self.specs.values():row['execution']='pilot-live'
        self.publisher.publish(self.after,self.after_sha,before_bundle=self.before,
                               before_sha256=self.before_sha,execution='pilot-live')
        self.manual=self.base/'manual.zip';self.manual.write_bytes(b'invented-manual-repair')
        self.manual_sha=hashlib.sha256(self.manual.read_bytes()).hexdigest()
        self.specs[self.manual_sha]=spec('v7.0.4','manual')
        self.specs[self.manual_sha]['execution']='pilot-live'
        self.enterContext(patch('poolrotater_host.runtime_package.inspect',
            side_effect=lambda path,sha,**kw:self.specs[sha]))
        self.channels={'pilot':self.after_sha}

    def register(self):
        return register(self.publisher,self.manual,self.manual_sha,before_bundle=self.after,
                        before_sha256=self.after_sha,expected_channels=self.channels)

    def test_signed_registration_preserves_channel_and_recognizes_manual_install_without_downgrade(self):
        self.prepare();self.serve();old=self.client('old');new=self.client('new')
        self.assertEqual(old.check('pilot',self.before_sha)['available_version'],'v7.0.3')
        result=self.register();payload,_=verify(self.envelope(),self.root,now=self.now)
        self.assertTrue(result['channel_targets_unchanged']);self.assertEqual(payload['channels'],self.channels)
        self.assertEqual(len(payload['releases']),2)
        self.assertEqual(old.check('pilot',self.before_sha)['available_version'],'v7.0.3')
        self.assertIsNone(new.check('pilot',self.manual_sha)['available_version'])

    def test_registration_retry_is_idempotent(self):
        self.prepare();self.register();before=self.envelope();result=self.register()
        self.assertTrue(result['already_registered']);self.assertEqual(self.envelope(),before)

    def test_changed_channel_guard_refuses_without_publishing_or_promoting(self):
        self.prepare();before=self.envelope();self.channels={'pilot':self.before_sha}
        with self.assertRaises(ConfigError):self.register()
        self.assertEqual(self.envelope(),before)
        self.assertFalse((self.served/'targets'/(self.manual_sha+'.runtime.zip')).exists())

    def test_artifact_tampering_refuses_before_signed_metadata_changes(self):
        self.prepare();before=self.envelope();self.manual.write_bytes(b'changed')
        with self.assertRaises(ConfigError):self.register()
        self.assertEqual(self.envelope(),before)
