"""Actual HTTPS ingestion + durable installed-client reporting, no provider I/O."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import io
import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from bot.ratings import RatingStore
from poolrotater_host.layout import setup,load,atomic_json,read_json
from poolrotater_host.network.client import Client
from poolrotater_host.network.contracts import NetworkError
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.store import Store
from poolrotater_host.network.server import HubServer,tls
from poolrotater_host.provider_transport import TrustSnapshot
from poolrotater_host.testing.provider_https import certificates
from poolrotater_host.activity import Collector,Repository
from poolrotater_host.activity.reader import Reader
from poolrotater_host.diagnostic_logs import HubLogs
from poolrotater_host import reporting,reporting_logging
import threading


class ReportingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.certs=Path(cls.tmp.name);certificates(cls.certs)
        cls.trust=TrustSnapshot((cls.certs/'ca.crt').read_bytes())
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);base=Path(self.tmp.name)
        self.root=base/'host';setup(self.root)
        hub=base/'hub';initialize(hub);self.store=Store(hub)
        self.server=HubServer(('127.0.0.1',0),self.store,tls_context=tls(self.certs/'server.crt',self.certs/'server.key'))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.addCleanup(self.stop)
        self.client=Client(self.root);self.client.configure(url='https://127.0.0.1:'+str(self.server.server_port),trust=self.trust,operator_id=str(uuid.uuid4()),account_id=123,directory=True)
        p=self.client.profile();binding={k:p[k] for k in ('installation_id','generation','operator_id','account_id','directory')}
        self.client.enroll(self.store.invite(binding,label='Invented Operator')['invitation'])
        self.ledger=RatingStore(str(self.root/'state/controller-ratings.db'))
        self.reporter=reporting.Reporter(self.client)
        self.config=dict(share_statistics=True,consume_statistics=True,operational_logs=True,public_chat=False)
    def stop(self):self.server.shutdown();self.server.server_close();self.thread.join(3)
    def enable(self):
        reporting.configure(self.root,self.config)
        self.reporter.step();self.reporter.step()
    def presence(self):
        now=time.time();self.ledger.record_room_visibility(99,public=True,observed_at=now-1,mode='standard')
        self.ledger.add_room_player_time(room_id=99,user_id=7,username='Invented Player',seconds=.2,left_at=now)
    def test_real_https_consents_capture_upload_and_local_query_cache(self):
        # Legacy query fallback is separate from complete periodic replica tests.
        self.enterContext(patch("poolrotater_host.activity.replica.Replica.tick",return_value={"phase":"legacy"}))
        self.enable();self.presence();reader=Reader(self.root);reader.request(user_id=7)
        local=reporting.logs(self.root);local.append('lifecycle info retained',room='room1',version='7.1.0')
        for _ in range(7):self.reporter.step()
        self.assertEqual(len(Repository(self.store).snapshot()['presence']),1)
        self.assertGreater(reader.user_stats(7)['total_lobby_seconds'],0)
        self.assertEqual(Collector(self.root).status()['pending_events'],0)
        self.assertIn('lifecycle info retained',str(HubLogs(self.store).search()))
        self.assertFalse(reporting.support(self.root)['provider_dispatch'])

    def test_reviewed_history_captures_uploads_and_resolves_name_over_real_https(self):
        self.enterContext(patch("poolrotater_host.activity.replica.Replica.tick",return_value={"phase":"legacy"}))
        from bot.ppstats import PPStatsStore
        from poolrotater_host.activity import history
        self.enable()
        marker,_=load(self.root)
        store=PPStatsStore(str(self.root/'state/controller-ratings.db'))
        self.ledger.record_room_open(room_id=71,room_name='Invented public historical room',created_at=20)
        self.ledger.record_room_player_join(room_id=71,user_id=9,username='Historical Player',joined_at=20)
        with store._connect() as db:
            db.execute("INSERT INTO autohost_lobby_time VALUES(9,'Historical Player',306467,123,20,80,90,90)")
        atomic_json(self.root/history.PLAN,{'format':1,**marker,'public_room_ids':[71],
            'public_history_confirmed':True,'include_lobby_totals':True,'exclude_unreviewed_players':True})
        for _ in range(6):self.reporter.step()
        self.assertEqual(len(Repository(self.store).snapshot()['baselines']),1)
        reader=Reader(self.root);self.assertIsNone(reader.lookup('Historical Player'))
        for _ in range(3):self.reporter.step()
        self.assertEqual(reader.lookup('Historical Player'),(9,'Historical Player'))
        self.assertEqual(reader.user_stats(9)['total_lobby_seconds'],306467)
        self.assertEqual(read_json(self.root/history.RECEIPT)['phase'],'queued')
        self.assertEqual(Collector(self.root).status()['pending_events'],0)

    def test_refused_history_review_does_not_block_ordinary_activity_or_logs(self):
        from poolrotater_host.activity import history
        self.enable();self.presence()
        atomic_json(self.root/history.PLAN,{'format':1,'unreviewed':True})
        reporting.logs(self.root).append('diagnostic during history refusal',room='room1')
        receipts=[self.reporter.step() for _ in range(6)]
        self.assertTrue(all('activity-history-review-required' in r['capture_errors'] for r in receipts))
        self.assertEqual(len(Repository(self.store).snapshot()['presence']),1)
        self.assertIn('diagnostic during history refusal',str(HubLogs(self.store).search()))
        self.assertIn('activity-history-review-required',reporting.support(self.root)['capture_errors'])
    def test_outage_preserves_unsent_and_replays_once(self):
        self.enable();self.presence()
        with patch.object(self.client,'transport',side_effect=NetworkError('hub_unavailable',503)):
            with self.assertRaises(NetworkError):self.reporter.step()
        self.assertTrue(Collector(self.root).batch()['events'])
        for _ in range(5):self.reporter.step()
        self.assertEqual(len(Repository(self.store).snapshot()['presence']),1)
        self.reporter=reporting.Reporter(self.client)
        for _ in range(4):self.reporter.step()
        self.assertEqual(len(Repository(self.store).snapshot()['presence']),1)
    def test_optout_purges_and_withdraws_public_data(self):
        self.enable();self.presence()
        for _ in range(4):self.reporter.step()
        self.assertTrue(Repository(self.store).snapshot()['presence'])
        reporting.configure(self.root,dict.fromkeys(reporting.CHOICES,False))
        for _ in range(4):self.reporter.step()
        self.assertEqual(Repository(self.store).snapshot()['presence'],[])
        self.assertFalse(Collector(self.root).batch()['events'])
    def test_defaults_on_and_foreign_profile_refused(self):
        self.assertTrue(all(reporting.profile(self.root)[k] for k in reporting.CHOICES))
        p=reporting.profile(self.root);p['generation']=str(uuid.uuid4());atomic_json(self.root/'config/reporting.json',p)
        with self.assertRaises(NetworkError):reporting.profile(self.root)
    def test_explicit_optout_survives_default_policy_and_agent_restart(self):
        reporting.configure(self.root,dict.fromkeys(reporting.CHOICES,False))
        self.reporter=reporting.Reporter(self.client)
        for _ in range(3):self.reporter.step()
        self.assertFalse(any(reporting.profile(self.root)[k] for k in reporting.CHOICES))
        self.assertEqual(reporting.support(self.root)['agent_phase'],'disabled')

    def test_missing_profile_syncs_new_defaults_without_manual_enable(self):
        self.assertFalse((self.root/'config/reporting.json').exists())
        for _ in range(3):self.reporter.step()
        self.presence()
        for _ in range(5):self.reporter.step()
        self.assertEqual(len(Repository(self.store).snapshot()['presence']),1)
        self.assertTrue(reporting.profile(self.root)['public_chat'])

    def test_enrolled_rating_defaults_enable_and_explicit_modes_survive(self):
        import os
        from poolrotater_host.ratings.sync import Synchronizer
        from poolrotater_host.ratings.migration import migrate_ledger
        from poolrotater_host.ratings import reader
        (self.root/'state/controller-ratings.db').chmod(0o600)
        previous=os.umask(0o077)
        try:migrate_ledger(self.root)
        finally:os.umask(previous)
        sync=Synchronizer(self.root,network=self.client)
        self.assertTrue(sync.ensure_defaults())
        self.assertTrue(sync.profile()['share_ratings'])
        self.assertEqual(reader.profile(self.root)['mode'],'network')
        sync.configure(share_ratings=False)
        self.assertTrue(sync.ensure_defaults())
        self.assertFalse(sync.profile()['share_ratings'])
        reader.configure(sync,mode='independent')
        self.assertEqual(reader.profile(self.root)['mode'],'independent')

    def test_local_support_does_not_expose_credentials(self):
        self.enable();value=reporting.support(self.root)
        self.assertNotIn(self.client.credential()['credential'],json.dumps(value))
        self.assertNotIn('provider-osu-token',json.dumps(value))
    def test_corrupted_support_receipt_cannot_emit_arbitrary_private_text(self):
        self.enable()
        path=self.root/'state/reporting-agent.json';state=read_json(path);state['at']='invented-private-text-canary'
        atomic_json(path,state)
        with self.assertRaises(NetworkError) as exc:reporting.support(self.root)
        self.assertNotIn('invented-private-text-canary',str(exc.exception))

    def test_unknown_host_cannot_ingest_or_search_logs_remotely(self):
        from poolrotater_host.network.transport import Transport
        transport=self.client.transport(self.client.profile())
        with self.assertRaises(NetworkError):transport.request('POST','/v1/logs/chunks',body={},token='prn_'+'x'*43)
        with self.assertRaises(NetworkError):transport.request('GET','/v1/logs/search')
    def test_full_activity_queue_drains_without_losing_native_cursor(self):
        self.enable()
        for _ in range(4):self.presence()
        with patch('poolrotater_host.activity.collector.MAX_OUTBOX_EVENTS',1):
            receipts=[self.reporter.step() for _ in range(9)]
        self.assertTrue(any('activity-capacity-paused' in r['capture_errors'] for r in receipts))
        self.assertEqual(len(Repository(self.store).snapshot()['presence']),4)
        self.assertFalse(Collector(self.root).batch()['events'])

    def test_activity_capture_failure_does_not_block_diagnostic_delivery(self):
        self.enable();reporting.logs(self.root).append('diagnostic during ledger outage',room='room1')
        with patch.object(Collector,'collect',side_effect=OSError('invented privacy source unavailable')):
            receipts=[self.reporter.step() for _ in range(4)]
        self.assertTrue(all(r['phase']=='partial' for r in receipts))
        self.assertIn('diagnostic during ledger outage',str(HubLogs(self.store).search()))
        self.assertEqual(reporting.support(self.root)['capture_errors'],['activity-capture-unavailable'])

    def test_public_chat_requires_separate_choice(self):
        self.enable();local=reporting.logs(self.root)
        self.assertFalse(local.append('Invented public chat',room='room1',kind='chat'))
        self.assertTrue(local.append('ordinary INFO lifecycle',room='room1'))
        self.assertTrue(local.append('private room operational',room='room1',private=True))
    def test_protected_installed_reporting_dispatch(self):
        from poolrotater_host.network.installed import execute
        result=execute(self.client,'reporting-configure',io.BytesIO(json.dumps(self.config).encode()))
        self.assertTrue(result['share_statistics'])
        self.assertTrue(execute(self.client,'reporting-support')['ok'])
    def test_source_sink_scrubs_known_secret_and_preserves_traceback(self):
        self.enable();canary='opaque-invented-provider-credential'
        atomic_json(self.root/'secrets/provider-osu-token.json',{'access_token':canary})
        reporting_logging.witness(self.root,'room1',1,room_id=99,public=True)
        logger=logging.getLogger();before=list(logger.handlers);level=logger.level;logger.setLevel(logging.INFO)
        try:
            reporting_logging.install(self.root,alias='room1',version='7.1.0')
            try:raise RuntimeError('traceback '+canary)
            except RuntimeError:logger.exception('lifecycle failure '+canary)
            for h in logger.handlers:
                if h not in before:h.flush()
            raw=(self.root/'logs/reporting-room1.jsonl').read_text()
            self.assertNotIn(canary,raw);self.assertIn('Traceback',raw);self.assertIn('REDACTED',raw)
            local=reporting.logs(self.root)
            for source in local.sources.values():local.collect(source)
            self.assertIsNotNone(local.batch())
        finally:
            for h in list(logger.handlers):
                if h not in before:logger.removeHandler(h);h.close()
            logger.setLevel(level)
    def test_private_diagnostics_survive_later_room_visibility_changes(self):
        self.enable();reporting_logging.witness(self.root,'room1',1,room_id=99,public=False)
        logger=logging.getLogger();before=list(logger.handlers);level=logger.level;logger.setLevel(logging.INFO)
        try:
            reporting_logging.install(self.root,alias='room1',version='7.1.0');logger.info('private lifecycle excluded')
            reporting_logging.witness(self.root,'room1',1,room_id=99,public=True);logger.info('public lifecycle included')
            for h in logger.handlers:
                if h not in before:h.flush()
            local=reporting.logs(self.root)
            for source in local.sources.values():local.collect(source)
            batch=local.batch();self.assertIsNotNone(batch)
            from poolrotater_host.diagnostic_logs.common import unpack
            payload=str(unpack(batch['payload']))
            self.assertIn('private lifecycle excluded',payload);self.assertIn('public lifecycle included',payload)
        finally:
            for h in list(logger.handlers):
                if h not in before:logger.removeHandler(h);h.close()
            logger.setLevel(level)

if __name__=='__main__':unittest.main()
