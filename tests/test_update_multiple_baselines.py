"""One signed release admits exact, independently checked predecessor archives."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,hashlib,unittest
from poolrotater_host.config import ConfigError
from poolrotater_host.updates.trust import verify
from tests import test_update_release_delivery as fixture

class MultipleBaselineTests(unittest.TestCase):
    setUp=fixture.ReleaseDeliveryTests.setUp
    serve=fixture.ReleaseDeliveryTests.serve
    client=fixture.ReleaseDeliveryTests.client
    envelope=fixture.ReleaseDeliveryTests.envelope
    def baseline(self):
        path=self.base/'second.zip';path.write_bytes(b'second');pin=hashlib.sha256(path.read_bytes()).hexdigest()
        self.specs[pin]=copy.deepcopy(self.specs[self.before_sha]);return path,pin
    def publish(self,extra):
        return self.publisher.publish(self.after,self.after_sha,before_bundle=self.before,
            before_sha256=self.before_sha,execution='offline-fixture',additional_baselines=extra)
    def test_two_old_hosts_select_same_artifact_without_new_client_code(self):
        path,pin=self.baseline();self.publish([(path,pin)])
        payload,_=verify(self.envelope(),self.root,now=self.now)
        self.assertEqual(payload['releases'][0]['from_sha256'],sorted([self.before_sha,pin]))
        self.serve()
        for i,baseline in enumerate((self.before_sha,pin)):
            client=self.client(str(i));value=client.check('pilot',baseline)
            self.assertEqual(value['available_version'],'v7.0.3')
            self.assertEqual(client.state()['available']['artifact']['sha256'],self.after_sha)
    def test_bad_extra_environment_refuses_before_any_publication(self):
        path,pin=self.baseline();self.specs[pin]['environment_id']='c'*64
        before=(self.custody/'state.json').read_bytes()
        with self.assertRaises(ConfigError):self.publish([(path,pin)])
        self.assertEqual((self.custody/'state.json').read_bytes(),before)
        self.assertFalse(list((self.served/'targets').iterdir()))
    def test_duplicate_malformed_and_oversized_lists_refuse_without_commit(self):
        for extra in ([(self.before,self.before_sha)],[('only-one',)],{},[(self.before,self.before_sha)]*16):
            with self.subTest(extra=extra),self.assertRaises(ConfigError):self.publish(extra)
        self.assertFalse((self.served/'metadata/releases.json').exists())
    def test_mixed_worker_and_controller_baselines_are_not_silently_reclassified(self):
        path,pin=self.baseline();self.specs[pin]['files']['payload/controller.py']='different'
        with self.assertRaisesRegex(ConfigError,'kind_changed'):self.publish([(path,pin)])
    def test_future_predecessor_cannot_authorize_a_downgrade(self):
        path,pin=self.baseline();self.specs[pin]['version']='v7.0.4'
        with self.assertRaisesRegex(ConfigError,'downgrade'):self.publish([(path,pin)])
