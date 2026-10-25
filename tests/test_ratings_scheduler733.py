from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import unittest
import time
from unittest.mock import patch

from poolrotater_host.layout import LeaseBusy, file_lease, read_json, atomic_json
from poolrotater_host.network.contracts import NetworkError
from poolrotater_host.network.contracts import fingerprint
from poolrotater_host.ratings.agent import Scheduler, FetchScheduler
from tests.test_v71_ratings_parity import V71RatingsParityTests as Fixtures


class RatingsScheduler733Tests(unittest.TestCase):
    setUp = Fixtures.setUp
    host = Fixtures.host
    enable = Fixtures.enable

    def scheduler(self, host):
        self.now = getattr(self, 'now', [1000.0])
        return Scheduler(host['sync'], clock=lambda: self.now[0], jitter=lambda *_: 0)

    def fetch_scheduler(self, host):
        self.now = getattr(self, 'now', [1000.0])
        return FetchScheduler(host['sync'], clock=lambda: self.now[0], jitter=lambda *_: 0)

    def test_upload_lock_contention_is_short_retry_with_safe_diagnostics(self):
        host = self.owner
        self.enable(host)
        scheduler = self.scheduler(host)
        with patch.object(host['sync'], 'scheduled_step', side_effect=LeaseBusy('private lock detail')):
            receipt = scheduler.tick()
        self.assertEqual(receipt['phase'], 'retrying')
        self.assertEqual(receipt['error'], 'hub-unavailable')
        self.assertLessEqual(receipt['next_attempt_at'] - receipt['attempted_at'], 8)
        self.assertGreaterEqual(receipt['next_attempt_at'] - receipt['attempted_at'], 5)
        diag = read_json(host['root'] / 'state/ratings-agent-diagnostics.json')
        self.assertEqual(diag['reason'], 'lease-contention')
        self.assertTrue(diag['lock_contention'])
        self.assertNotIn('private lock detail', str(diag))

    def test_snapshot_lock_contention_retries_and_accepted_upload_can_refresh(self):
        host = self.owner
        self.enable(host)
        scheduler = self.fetch_scheduler(host)
        with patch('poolrotater_host.ratings.reader.profile', return_value={'mode': 'network'}), \
             patch.object(host['sync'], 'fetch_snapshots', side_effect=LeaseBusy('private lock detail')):
            first = scheduler.tick()
            self.assertEqual(first['phase'], 'retrying')
            self.assertLessEqual(first['next_attempt_at'] - first['attempted_at'], 8)
            self.now[0] = first['attempted_at'] + 5
            recovered = {'phase': 'activated', 'downloaded_pages': 1, 'pending_pages': 0}
            with patch.object(host['sync'], 'fetch_snapshots', return_value=recovered) as fetch:
                second = scheduler.tick(uploaded_at=first['attempted_at'] + 1)
        self.assertEqual(second['phase'], 'activated')
        self.assertEqual(fetch.call_count, 1)

    def test_query_deferred_keeps_new_feedback_fast_path_and_reports_pending_age(self):
        host = self.owner
        self.enable(host)
        scheduler = self.scheduler(host)
        steps = []

        def step(now, **kwargs):
            steps.append(now)
            return {'phase': 'query-deferred', 'uploaded': 1, 'queried': 0}

        with patch.object(host['sync'], 'scheduled_step', side_effect=step), \
             patch.object(host['sync'], 'has_pending_feedback', return_value=False), \
             patch.object(host['sync'], 'feedback_highwater', return_value=0), \
             patch.object(host['sync'], 'advance_feedback_cursor', return_value=0), \
             patch.object(host['sync'], 'has_pending_feedback_after', return_value=True) as fresh:
            first = scheduler.tick()
            self.assertLess(first['next_attempt_at'] - first['attempted_at'], 30)
            self.now[0] = first['attempted_at'] + 6
            second = scheduler.tick()
        fresh.assert_called_once_with(0)
        self.assertEqual(len(steps), 2)
        fresh.assert_called_with(0)
        self.assertEqual(second['phase'], 'query-deferred')

    def test_query_retry_does_not_upload_another_old_backlog_batch(self):
        host=self.owner;self.enable(host);now=time.time();self.now=[now]
        with patch('bot.ratings.time.time',side_effect=[now-100+index/100 for index in range(105)]):
            for index in range(105):host['local'].rate(1000+index,5000+index,1)
        scheduler=self.scheduler(host)
        with patch.object(host['sync'],'_query',side_effect=NetworkError('hub_unavailable',503)):
            first=scheduler.tick()
            self.assertEqual(first['phase'],'query-deferred')
            batches=[request for request in host['network'].requests if request[1]=='/v1/events/batches']
            self.assertEqual(len(batches),1)
            self.assertEqual(len(batches[0][2]['events']),100)
            self.now[0]=first['next_attempt_at']+1
            second=scheduler.tick()
        self.assertEqual(second['phase'],'query-deferred')
        batches=[request for request in host['network'].requests if request[1]=='/v1/events/batches']
        self.assertEqual(len(batches),1)
        self.assertGreaterEqual(host['sync'].pending_feedback_age(self.now[0]),100)

    def test_new_vote_older_than_attempt_time_is_not_lost_behind_query_retry(self):
        host=self.owner;self.enable(host);self.now=[1000.0]
        for index in range(4):host['local'].rate(3000+index,7000+index,1)
        scheduler=self.scheduler(host);original=host['network'].request;inserted=[]
        def request(method,path,*,token,body=None):
            result=original(method,path,token=token,body=body)
            if path=='/v1/events/batches' and not inserted:
                # Simulate a wall-clock correction or delayed local capture:
                # sequence is newer although observed_at predates the attempt.
                with patch('bot.ratings.time.time',return_value=999.0):
                    host['local'].rate(3999,7999,-1)
                inserted.append(True)
            return result
        with patch('poolrotater_host.ratings.sync.UPLOAD_BATCH_EVENTS',2), \
             patch.object(host['network'],'request',side_effect=request), \
             patch.object(host['sync'],'_query',side_effect=[NetworkError('hub_unavailable',503),
                                                               NetworkError('hub_unavailable',503)]):
            first=scheduler.tick()
            self.assertEqual(first['phase'],'query-deferred')
            self.now[0]=first['attempted_at']+6
            second=scheduler.tick()
        self.assertEqual(second['phase'],'query-deferred')
        batches=[r for r in host['network'].requests if r[1]=='/v1/events/batches']
        self.assertEqual([len(r[2]['events']) for r in batches],[2,1])
        self.assertEqual(batches[1][2]['events'][0]['beatmap_id'],3999)

    def test_uncapped_old_backlog_resumes_after_reconciliation_recovers(self):
        host=self.owner;self.enable(host);self.now=[time.time()]
        for index in range(105):host['local'].rate(8000+index,11000+index,1)
        marker=host['sync'].marker;credential=host['network'].credential()
        binding={**{k:marker[k] for k in ('installation_id','generation')},
                 **{k:credential[k] for k in ('host_id','server_epoch')},
                 'profile_sha256':fingerprint(host['network'].profile())}
        atomic_json(host['root']/'state/network-transfer-policy.json',
                    {'format':1,'binding':binding,'checked_at':time.time(),'uncapped':True})
        scheduler=self.scheduler(host)
        with patch.object(host['sync'],'_query',side_effect=[NetworkError('hub_unavailable',503),
                                                               NetworkError('hub_unavailable',503),0,0]):
            first=scheduler.tick()
            self.assertEqual(first['phase'],'query-deferred')
            first_batches=[r for r in host['network'].requests if r[1]=='/v1/events/batches']
            self.assertEqual([len(r[2]['events']) for r in first_batches],[100])
            self.now[0]=first['next_attempt_at']+1
            second=scheduler.tick()
            self.assertEqual(second['phase'],'query-deferred')
            batches=[r for r in host['network'].requests if r[1]=='/v1/events/batches']
            self.assertEqual([len(r[2]['events']) for r in batches],[100])
            self.now[0]=second['next_attempt_at']+1
            third=scheduler.tick()
            self.assertEqual(third['phase'],'synchronized')
            self.now[0]=third['attempted_at']+1
            fourth=scheduler.tick()
        self.assertEqual(fourth['phase'],'synchronized')
        batches=[r for r in host['network'].requests if r[1]=='/v1/events/batches']
        self.assertEqual([len(r[2]['events']) for r in batches],[100,5])

    def test_fresh_vote_uploads_alone_while_reconciliation_is_deferred_with_legacy_budget(self):
        host=self.owner;self.enable(host);now=time.time();self.now=[now]
        with patch('bot.ratings.time.time',return_value=now-1):host['local'].rate(1,7,1)
        scheduler=self.scheduler(host)
        with patch.object(host['sync'],'_query',side_effect=NetworkError('hub_unavailable',503)):
            first=scheduler.tick()
            with patch('bot.ratings.time.time',return_value=first['attempted_at']+6):
                host['local'].rate(2,8,-1)
            self.now[0]=first['attempted_at']+6
            second=scheduler.tick()
        self.assertEqual(second['phase'],'query-deferred')
        batches=[request for request in host['network'].requests if request[1]=='/v1/events/batches']
        self.assertEqual([len(request[2]['events']) for request in batches],[1,1])
        self.assertEqual(batches[1][2]['events'][0]['beatmap_id'],2)
        self.assertTrue((host['root']/'state/ratings-delivery-budget.json').exists())

    def test_authority_and_corruption_errors_still_keep_review_fence(self):
        host = self.owner
        self.enable(host)
        scheduler = self.scheduler(host)
        with patch.object(host['sync'], 'scheduled_step', side_effect=NetworkError('authority_refused', 403)):
            receipt = scheduler.tick()
        self.assertEqual(receipt['phase'], 'authority-review-required')
        self.assertGreaterEqual(receipt['next_attempt_at'] - receipt['attempted_at'], 1800)

    def test_real_upload_lease_release_recovers_without_manual_action(self):
        host=self.owner;self.enable(host);self.now=[time.time()]
        scheduler=self.scheduler(host)
        with file_lease(host['sync'].lock_path):
            first=scheduler.tick()
        self.assertEqual(first['phase'],'retrying')
        self.now[0]=first['next_attempt_at']+1
        recovered=scheduler.tick()
        self.assertEqual(recovered['phase'],'synchronized')

    def test_real_snapshot_lease_release_recovers_without_manual_action(self):
        host=self.owner;self.enable(host);self.now=[time.time()]
        scheduler=self.fetch_scheduler(host)
        with file_lease(host['sync'].lock_path):
            first=scheduler.tick()
        self.assertEqual(first['phase'],'retrying')
        self.now[0]=first['next_attempt_at']+1
        with patch('poolrotater_host.ratings.reader.profile',return_value={'mode':'network'}):
            recovered=scheduler.tick()
        self.assertEqual(recovered['phase'],'activated')

    def test_optional_diagnostics_failure_does_not_mask_successful_sync(self):
        host=self.owner;self.enable(host);self.now=[time.time()]
        scheduler=self.scheduler(host);real_atomic=__import__('poolrotater_host.ratings.agent',fromlist=['atomic_json']).atomic_json
        def write(path,value,**kwargs):
            if path.name=='ratings-agent-diagnostics.json':raise TypeError('optional serialization failure')
            return real_atomic(path,value,**kwargs)
        with patch.object(host['sync'],'pending_feedback_age',side_effect=KeyError('optional field')), \
             patch('poolrotater_host.ratings.agent.atomic_json',side_effect=write):
            result=scheduler.tick()
        self.assertEqual(result['phase'],'synchronized')
        self.assertEqual(read_json(scheduler.path)['phase'],'synchronized')


if __name__ == '__main__':
    unittest.main()
