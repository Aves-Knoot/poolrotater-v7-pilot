"""Rescue route qualification through real TLS and the isolated transport child."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import time
import unittest
from tests import test_network_https as fixtures
from poolrotater_host.network.contracts import NetworkError
from poolrotater_host.rescue.repository import Repository


class RecoveryHTTPS(unittest.TestCase):
    setUpClass=classmethod(fixtures.NetworkHTTPSTests.setUpClass.__func__)
    tearDownClass=classmethod(fixtures.NetworkHTTPSTests.tearDownClass.__func__)
    setUp=fixtures.NetworkHTTPSTests.setUp
    stop=fixtures.NetworkHTTPSTests.stop

    def body(self):
        return {'protocol_version':1,'sequence':1,'sent_at':time.time(),
            **{k:self.binding[k] for k in ('installation_id','generation')},'receipt':None,
            'observation':{'phase':'absent','operation_id':None,'candidate_sha256':None,'job_sha256':None,
                'failure_stage':None,'failure_code':None,'service_state':'failed','startup_attempted':False}}

    def test_real_authenticated_inspection_does_not_need_room_or_network_service(self):
        enrolled=self.transport.request('POST','/v1/enrollments',body=self.enrollment)
        body=self.body()
        route='/v1/hosts/self/recovery'
        self.assertTrue(self.transport.request('POST',route,body=body,token=self.token)['ok'])
        repo=Repository(self.store)
        queued=repo.queue(enrolled['host_id'],'inspect')
        body.update(sequence=2)
        # Server minimum cadence is local and deterministic in this fixture.
        with repo.database() as db:db.execute('UPDATE reports SET received=received-10')
        response=self.transport.request('POST',route,body=body,token=self.token)
        self.assertEqual(response['request'],queued)
        self.assertTrue(self.transport.last_worker_reaped)

    def test_anonymous_recovery_and_public_command_submission_are_refused(self):
        with self.assertRaises(NetworkError):
            self.transport.request('POST','/v1/hosts/self/recovery',body=self.body())
        self.transport.request('POST','/v1/enrollments',body=self.enrollment)
        with self.assertRaises(NetworkError):
            self.transport.request('POST','/v1/hosts/self/recovery',token=self.token,
                body={**self.body(),'action':'complete-update','shell':'forbidden'})

    def test_unknown_credential_cannot_poll_for_recovery_requests(self):
        with self.assertRaises(NetworkError) as error:
            self.transport.request('POST','/v1/hosts/self/recovery',body=self.body(),token=self.token)
        self.assertEqual(error.exception.code,'credential_refused')


if __name__=='__main__':unittest.main()
