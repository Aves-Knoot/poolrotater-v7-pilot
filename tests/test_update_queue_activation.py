"""Unsubmitted scheduler intent is distinct from uncertain native dispatch."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,unittest
from poolrotater_host.update_queue_activation import unsubmitted
from poolrotater_host.network.contracts import NetworkError

class QueuePreflightEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.parent={'bundle_sha256':'a'*64}
        self.operation={'id':'operation','queued_at':200,'before_sha256':'a'*64,'effective_before_sha256':'a'*64,
                        'release':{'kind':'controller','artifact':{'sha256':'b'*64}}}
        self.job={'id':'older','phase':'complete','after':self.parent,'queued_at':100}
    def test_missing_or_older_completed_job_is_unsubmitted(self):
        unsubmitted(self.operation,self.parent,None);unsubmitted(self.operation,self.parent,self.job)
    def test_matching_operation_can_never_be_retired_as_unsubmitted(self):
        with self.assertRaises(NetworkError):unsubmitted(self.operation,self.parent,{**self.job,'id':'operation'})
    def test_active_uncertain_newer_or_foreign_jobs_are_refused(self):
        for patch in ({'phase':'held'},{'phase':'committing'},{'phase':'waiting_empty'},{'queued_at':201},{'after':{'bundle_sha256':'c'*64}}):
            with self.subTest(patch=patch),self.assertRaises(NetworkError):unsubmitted(self.operation,self.parent,{**self.job,**patch})
    def test_controller_or_effective_baseline_change_is_refused(self):
        for patch in ({'before_sha256':'c'*64},{'effective_before_sha256':'c'*64},{'release':{'kind':'worker'}}):
            with self.subTest(patch=patch),self.assertRaises(NetworkError):unsubmitted({**self.operation,**patch},self.parent,self.job)
    def test_older_cancellation_must_belong_to_current_parent(self):
        unsubmitted(self.operation,self.parent,{'id':'older','phase':'cancelled','before':self.parent,'queued_at':100})
        with self.assertRaises(NetworkError):unsubmitted(self.operation,self.parent,{'id':'older','phase':'cancelled','before':{},'queued_at':100})
