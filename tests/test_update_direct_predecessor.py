"""Signed direct paths take precedence over optional sorted predecessors."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,hashlib,unittest
from tests import test_update_release_delivery as fixture

class DirectPredecessorTests(unittest.TestCase):
    setUp=fixture.ReleaseDeliveryTests.setUp
    serve=fixture.ReleaseDeliveryTests.serve
    client=fixture.ReleaseDeliveryTests.client
    envelope=fixture.ReleaseDeliveryTests.envelope
    def head(self,direct):
        # Force intermediate hash to sort before the installed baseline.
        for i in range(100):
            data=('intermediate-'+str(i)).encode();pin=hashlib.sha256(data).hexdigest()
            if pin<self.before_sha:break
        else:raise AssertionError('fixture hash ordering')
        middle=self.base/'middle.zip';middle.write_bytes(data)
        self.specs[pin]=fixture.spec('v7.0.3','middle')
        self.publisher.publish(middle,pin,before_bundle=self.before,before_sha256=self.before_sha,execution='offline-fixture')
        self.specs[self.after_sha]=fixture.spec('v7.0.4','head')
        self.publisher.publish(self.after,self.after_sha,before_bundle=middle,before_sha256=pin,execution='offline-fixture',
                               additional_baselines=[(self.before,self.before_sha)]if direct else [])
        self.serve();return pin
    def test_direct_signed_baseline_wins_over_lexicographically_first_intermediate(self):
        self.head(True);client=self.client()
        self.assertEqual(client.check('pilot',self.before_sha)['available_version'],'v7.0.4')
        self.assertEqual(client.state()['available']['artifact']['sha256'],self.after_sha)
        self.assertEqual(client.download()['phase'],'downloaded')
    def test_required_intermediate_is_preserved(self):
        pin=self.head(False);client=self.client()
        self.assertEqual(client.check('pilot',self.before_sha)['available_version'],'v7.0.3')
        self.assertEqual(client.state()['available']['artifact']['sha256'],pin)
    def test_retained_explicit_effective_baseline_can_use_direct_path(self):
        self.head(True);client=self.client()
        self.assertEqual(client.check('pilot','f'*64,baselines=(self.before_sha,))['available_version'],'v7.0.4')
