"""A reviewed configuration never enables sharing or interrupts deployment."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import unittest
from unittest.mock import patch
from pathlib import Path
import uuid
from tests import test_activity_history_backfill as backfill
from pilot.install_history_review import install_review,admit
from poolrotater_host.activity import history
from poolrotater_host.layout import read_json,atomic_json


class HistoryReviewInstallationTests(unittest.TestCase):
    setUp=backfill.HistoryBackfillTests.setUp
    enable=backfill.HistoryBackfillTests.enable
    seed=backfill.HistoryBackfillTests.seed

    def manifest(self):return {'format':1,**self.value,'include_lobby_totals':True,'exclude_unreviewed_players':True}

    def test_install_and_replay_are_idempotent_and_do_not_capture_synchronously(self):
        self.seed();value=self.manifest()
        first=install_review(self.root,value);second=install_review(self.root,value)
        self.assertTrue(first['configuration_changed']);self.assertFalse(second['configuration_changed'])
        self.assertEqual(read_json(self.root/history.PLAN),value)
        self.assertFalse((self.root/history.RECEIPT).exists())
        self.assertFalse(first['live_rooms_affected']);self.assertFalse(first['sharing_changed'])

    def test_explicit_optout_is_not_enabled_and_no_plan_is_created(self):
        before=(self.root/'config/reporting.json').read_bytes()
        with self.assertRaisesRegex(Exception,'sharing_disabled'):install_review(self.root,self.manifest())
        self.assertEqual((self.root/'config/reporting.json').read_bytes(),before)
        self.assertFalse((self.root/history.PLAN).exists())

    def test_foreign_review_and_changed_existing_review_are_refused(self):
        self.seed();value=self.manifest()
        with self.assertRaisesRegex(Exception,'foreign_history_review'):
            install_review(self.root,{**value,'generation':str(uuid.uuid4())})
        install_review(self.root,value)
        with self.assertRaisesRegex(Exception,'existing_history_review_changed'):
            install_review(self.root,{**value,'public_room_ids':[71,72]})
        self.assertEqual(read_json(self.root/history.PLAN),value)

    def test_pending_controller_job_blocks_configuration_change(self):
        target={'prefix':self.root/'prefix'};parent={}
        with patch('poolrotater_host.operator_maintenance.require_idle'),patch('poolrotater_host.updates.native.require_idle_worker_deployment'),patch('poolrotater_host.provisioning._control',return_value={'phase':'waiting_empty'}):
            with self.assertRaisesRegex(ValueError,'pending_controller_update'):admit(target,parent)
        for phase in ('complete','cancelled'):
            with patch('poolrotater_host.operator_maintenance.require_idle'),patch('poolrotater_host.updates.native.require_idle_worker_deployment'),patch('poolrotater_host.provisioning._control',return_value={'phase':phase}):admit(target,parent)


if __name__=='__main__':unittest.main()
