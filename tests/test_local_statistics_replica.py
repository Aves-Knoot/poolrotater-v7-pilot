"""Real HTTPS replicas, both upgrade orders, all-player coverage and recovery."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
import gzip
import hashlib
import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests import test_reporting_integration as base_fixture
from tests.legacy_statistics_fixture import LegacyHubServer,LegacyReader,LegacyTransport
from poolrotater_host.activity import Repository,Collector
from poolrotater_host.activity.reader import Reader
from poolrotater_host.activity.replica import Replica,Publisher,manifest,page,POLICY_LEASE,INTERVAL
from poolrotater_host.network.server import HubServer,tls
from poolrotater_host.network.contracts import NetworkError,canonical,fingerprint
from poolrotater_host.layout import load,atomic_json,read_json
from poolrotater_host import reporting


class ReplicaTests(unittest.TestCase):
    setUpClass=classmethod(base_fixture.ReportingIntegrationTests.setUpClass.__func__)
    tearDownClass=classmethod(base_fixture.ReportingIntegrationTests.tearDownClass.__func__)
    def setUp(self):
        with patch('tests.test_reporting_integration.HubServer',HubServer):
            base_fixture.ReportingIntegrationTests.setUp(self)
    stop=base_fixture.ReportingIntegrationTests.stop
    enable=base_fixture.ReportingIntegrationTests.enable
    presence=base_fixture.ReportingIntegrationTests.presence

    def _fresh(self,network=None):return Replica(self.root).sync(network or self.client,force=True)

    def _publish_presence(self):
        self.enable();self.presence()
        for _ in range(5):self.reporter.step()

    def _new_generation(self):
        self.server.statistics.stamp=None
        return self._fresh()

    def test_complete_replica_supplies_both_modes_without_command_http(self):
        self._publish_presence();value=self._new_generation();self.assertEqual(value['phase'],'activated')
        reader=Reader(self.root)
        with patch.object(self.client,'transport',side_effect=AssertionError('command HTTP forbidden')):
            self.assertEqual(reader.lookup('Invented Player'),(7,'Invented Player'))
            self.assertGreater(reader.user_stats(7)['total_lobby_seconds'],0)
            self.assertEqual(reader.lookup('Invented Player',ruleset_id=3),(7,'Invented Player'))
            self.assertEqual(reader.user_stats(7,ruleset_id=3)['total_lobby_seconds'],0)
            self.assertEqual(reader.lookup_status('Never played'),'not_found')
        self.assertEqual(reader.status()['pending_queries'],0)

    def test_unchanged_replica_has_no_page_transfer(self):
        self._publish_presence();self._new_generation()
        value=self._fresh();self.assertEqual(value['phase'],'unchanged');self.assertEqual(value['transferred'],0)

    def test_snapshot_does_not_expire_after_two_minutes_when_policy_is_fresh(self):
        self._publish_presence();self._new_generation();now=time.time()
        replica=Replica(self.root,clock=lambda:now+121)
        with sqlite3.connect(replica.path) as db:db.execute('UPDATE replica_meta SET checked=?',(now+120,))
        self.assertGreater(Reader(self.root,clock=lambda:now+121).user_stats(7)['total_lobby_seconds'],0)
        self.assertIsNone(Reader(self.root,clock=lambda:now+121+POLICY_LEASE).user_stats(7))

    def test_privacy_change_revokes_old_snapshot_before_any_page_io(self):
        self._publish_presence();self._new_generation()
        repo=self.server.statistics.repository;token=self.client.credential()['credential'];now=time.time()
        self.ledger.set_stats_hidden(7,'Invented Player',True) if hasattr(self.ledger,'set_stats_hidden') else None
        from bot.ppstats import PPStatsStore
        PPStatsStore(str(self.root/'state/controller-ratings.db')).set_stats_hidden(7,'Invented Player',True)
        for _ in range(4):self.reporter.step()
        # Publishing a new policy with another public player ensures page I/O.
        self.ledger.add_room_player_time(room_id=99,user_id=8,username='Other Player',seconds=.1,left_at=time.time())
        for _ in range(4):self.reporter.step()
        real=self.client.transport
        class BrokenPages:
            def __init__(self,actual):self.actual=actual
            def request(inner,method,path,**kw):
                if '/replica/page?' in path:raise NetworkError('hub_unavailable',503)
                return inner.actual.request(method,path,**kw)
        with patch.object(self.client,'transport',side_effect=lambda p:BrokenPages(real(p))):
            with self.assertRaises(NetworkError):self._fresh()
        self.assertIsNone(Reader(self.root).user_stats(7))
        self._fresh();self.assertIsNone(Reader(self.root).lookup('Invented Player'))

    def test_outage_preserves_last_good_until_policy_lease_then_fails_closed(self):
        self._publish_presence();self._new_generation();reader=Reader(self.root)
        before=reader.user_stats(7)
        with patch.object(self.client,'transport',side_effect=NetworkError('hub_unavailable',503)):
            state=Replica(self.root,clock=lambda:time.time()+31).tick(self.client)
        self.assertEqual(state['phase'],'retrying');self.assertEqual(reader.user_stats(7)['total_lobby_seconds'],before['total_lobby_seconds'])
        self.assertIsNone(Reader(self.root,clock=lambda:time.time()+121).user_stats(7))

    def test_interrupted_import_resumes_and_preserves_previous_visible_transaction(self):
        self._publish_presence();self._new_generation();stop=threading.Event();stop.set()
        self.server.statistics.stamp=None
        old=Reader(self.root).user_stats(7)
        value=Replica(self.root).sync(self.client,force=True,stop=stop)
        self.assertEqual(value['phase'],'interrupted');self.assertEqual(Reader(self.root).user_stats(7)['total_lobby_seconds'],old['total_lobby_seconds'])
        self.assertEqual(self._fresh()['phase'],'activated')

    def test_old_reader_and_old_transport_work_against_new_hub(self):
        self._publish_presence()
        reader=LegacyReader(self.root);reader.request(username='Invented Player')
        actual=self.client.transport(self.client.profile())
        with patch.object(self.client,'transport',return_value=LegacyTransport(actual.origin,actual.trust)):
            reader.refresh(self.client)
        self.assertGreater(reader.user_stats(7)['total_lobby_seconds'],0)
        self.assertEqual(reader.lookup('Invented Player'),(7,'Invented Player'))

    def test_new_client_falls_back_against_exact_old_hub_and_later_adopts_new(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3)
        old=LegacyHubServer(('127.0.0.1',self.server.server_port),self.store,tls_context=tls(self.certs/'server.crt',self.certs/'server.key'))
        self.server=old;self.thread=threading.Thread(target=old.serve_forever,daemon=True);self.thread.start()
        self._publish_presence()
        self.assertEqual(self._fresh()['phase'],'legacy')
        from poolrotater_host.network.transfer_policy import enabled
        self.assertFalse(enabled(self.client,refresh=True))
        reader=Reader(self.root);reader.request(username='Invented Player');reader.refresh(self.client)
        self.assertGreater(reader.user_stats(7)['total_lobby_seconds'],0)
        old.shutdown();old.server_close();self.thread.join(3)
        new=HubServer(('127.0.0.1',old.server_port),self.store,tls_context=tls(self.certs/'server.crt',self.certs/'server.key'))
        self.server=new;self.thread=threading.Thread(target=new.serve_forever,daemon=True);self.thread.start()
        self.assertEqual(self._fresh()['phase'],'activated')
        self.assertTrue(enabled(self.client,clock=lambda:time.time()+1201,refresh=True))
        # A downgrade still sees an unmodified old-format cache and requests.
        legacy=LegacyReader(self.root);self.assertGreater(legacy.user_stats(7)['total_lobby_seconds'],0)

    def test_bulk_routes_do_not_consume_public_request_rate_budget(self):
        self.enable();self.server.requests.extend([time.monotonic()]*240)
        transport=self.client.transport(self.client.profile());token=self.client.credential()['credential']
        self.assertTrue(transport.request('GET','/v1/activity/replica/current',token=token)['ok'])
        with self.assertRaises(NetworkError) as cm:transport.request('GET','/v1/capabilities')
        self.assertEqual(cm.exception.status,429)
        with self.assertRaises(NetworkError):transport.request('GET','/v1/activity/replica/current',token='prn_'+'x'*43)

    def test_enrollment_binding_change_cannot_serve_other_hosts_projection(self):
        self._publish_presence();self._new_generation()
        binding=read_json(self.root/'state/activity-replica-binding.json');binding['host_id']='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        atomic_json(self.root/'state/activity-replica-binding.json',binding)
        with self.assertRaises(NetworkError):Reader(self.root).user_stats(7)

    def test_replica_failure_does_not_block_uploads_or_surface_raw_error(self):
        self.enable();self.presence()
        with patch('poolrotater_host.activity.replica.Replica.sync',side_effect=RuntimeError('invented-secret-canary')):
            for _ in range(5):self.reporter.step()
        self.assertEqual(len(Repository(self.store).snapshot()['presence']),1)
        receipt=read_json(self.root/'state/activity-replica-agent.json')
        self.assertNotIn('invented-secret-canary',json.dumps(receipt))

    def test_failed_same_policy_page_never_replaces_last_good_replica(self):
        self._publish_presence();self._new_generation();before=Reader(self.root).user_stats(7)
        self.server.statistics.stamp=None;actual=self.client.transport
        class Tampered:
            def __init__(inner,transport):inner.transport=transport
            def request(inner,method,path,**kw):
                result=inner.transport.request(method,path,**kw)
                if '/replica/page?' in path:result['rows'][0]['player']['total_gain']=99999
                return result
        with patch.object(self.client,'transport',side_effect=lambda p:Tampered(actual(p))):
            with self.assertRaises(NetworkError):self._fresh()
        self.assertEqual(Reader(self.root).user_stats(7)['total_gain'],before['total_gain'])
        self.assertEqual(self._fresh()['phase'],'activated')

    def test_validated_watermark_rollback_is_refused(self):
        self._publish_presence();self._new_generation()
        value=self.server.statistics.current(self.client.credential()['credential']);self.assertGreater(value['watermark'],0)
        value={**value,'watermark':value['watermark']-1};value['content_id']=fingerprint({k:v for k,v in value.items() if k!='content_id'})
        actual=self.client.transport
        class RolledBack:
            def __init__(inner,transport):inner.transport=transport
            def request(inner,method,path,**kw):
                return value if path=='/v1/activity/replica/current' else inner.transport.request(method,path,**kw)
        with patch.object(self.client,'transport',side_effect=lambda p:RolledBack(actual(p))):
            with self.assertRaises(NetworkError) as cm:self._fresh()
        self.assertEqual(cm.exception.code,'statistics_replica_rollback')
        self.assertGreater(Reader(self.root).user_stats(7)['total_lobby_seconds'],0)

    def test_local_consumption_optout_immediately_hides_retained_replica(self):
        self._publish_presence();self._new_generation()
        reporting.configure(self.root,{**self.config,'consume_statistics':False})
        self.assertIsNone(Reader(self.root).user_stats(7))
        self.assertEqual(Replica(self.root).status()['phase'],'unavailable')

    def test_unauthorized_reply_invalidates_previous_policy_lease_immediately(self):
        self._publish_presence();self._new_generation();actual=self.client.transport
        class Refused:
            def request(inner,*a,**kw):raise NetworkError('host_revoked',403)
        with patch.object(self.client,'transport',return_value=Refused()):
            with self.assertRaises(NetworkError):self._fresh()
        self.assertIsNone(Reader(self.root).user_stats(7))

    def test_import_does_not_take_a_large_command_database_write_lock(self):
        self._publish_presence();self._new_generation();self.server.statistics.stamp=None
        original=self.server.statistics.projection
        # A concurrent command cache writer remains admitted while the new,
        # independent SQLite projection is being populated.
        from poolrotater_host.activity import replica as module
        native=module.connection;observed=[]
        from contextlib import contextmanager
        @contextmanager
        def connection(path):
            with native(path) as db:
                if Path(path).name=='replica-building.sqlite3':
                    with native(self.root/'state/activity-cache.sqlite3') as other:
                        other.execute('SELECT COUNT(*) FROM replica_meta');observed.append(True)
                yield db
        with patch.object(module,'connection',connection):self._fresh()
        self.assertTrue(observed)

    def test_complete_inventory_exceeds_old_5000_row_cap_and_has_indexed_names(self):
        self.enable();source=Repository(self.store).snapshot();host=self.client.credential()['host_id']
        source['presence']=[{'user_id':10000+i,'username':'Invented '+str(i),'host_id':host,
                            'event_id':str(i),'mode':'standard','started_at':100,'ended_at':160}
                           for i in range(6001)]
        started=time.monotonic()
        with patch.object(self.server.statistics.repository,'snapshot',return_value=source):
            self.server.statistics.stamp=None;result=self._fresh()
        elapsed=time.monotonic()-started
        metadata=json.loads(sqlite3.connect(self.root/'state/activity-cache.sqlite3').execute('SELECT payload FROM replica_meta').fetchone()[0])['manifest']
        hostbase=self.store.root/'activity-replicas'/self.client.credential()['host_id']/metadata['content_id']
        compressed=sum(len(gzip.compress(canonical(read_json(hostbase/f"page-{r['index']}.json",maximum=512*1024)),compresslevel=1,mtime=0)) for r in metadata['pages'])
        print('REPLICA_FOOTPRINT '+json.dumps({'players':6001,'rows':result['rows'],'cache_bytes':(self.root/'state/activity-replica-downloads'/metadata['content_id']/'replica.sqlite3').stat().st_size,'uncompressed_page_bytes':sum(r['bytes'] for r in metadata['pages']),'compressed_page_bytes':compressed,'initial_sync_seconds':elapsed}))
        self.assertEqual(result['rows'],12002)
        reader=Reader(self.root);self.assertEqual(reader.lookup('Invented 6000'),(16000,'Invented 6000'))
        self.assertEqual(reader.user_stats(16000)['total_lobby_seconds'],60)
        self.assertEqual(reader.lookup('Invented 6000',ruleset_id=3),(16000,'Invented 6000'))


class ReplicaPolicyTests(unittest.TestCase):
    def test_units_limit_only_background_services(self):
        from poolrotater_host.network.operations import unit_text
        from poolrotater_host.network.hub_service import unit_text as hub
        from poolrotater_host.pilot_operations import unit_text as bot
        for name in ('owner','two'):
            text=unit_text(name);self.assertIn('Nice=17\nCPUQuota=50%',text);self.assertIn('Slice=poolrotater-v7-transfer.slice',text)
            self.assertNotIn('CPUQuota=50%',bot(name))
        self.assertIn('Nice=17\nCPUQuota=50%',hub())

    def test_new_parser_rejects_unknown_or_tampered_page_fields(self):
        from poolrotater_host.activity.replica import page
        value={'ok':True,'format':1,'index':0,'rows':[]};entry={'index':0,'sha256':fingerprint(value),'bytes':len(canonical(value)),'rows':0}
        self.assertEqual(page(value,entry),value)
        value['private']='invented-secret'
        with self.assertRaises(NetworkError):page(value,entry)


if __name__=='__main__':unittest.main()

class UncappedRatingTests(unittest.TestCase):
    def setUp(self):
        from tests import test_rating_sync as fixture
        fixture.RatingSyncTests.setUp(self)
        self.enable=lambda:fixture.RatingSyncTests.enable(self)

    def test_negotiated_new_hub_drains_backlog_without_quarantine_or_time_credits(self):
        from poolrotater_host.network.transfer_policy import CAPABILITY
        from poolrotater_host.ratings.agent import Scheduler
        self.enable()
        original=self.network.request
        def request(method,path,*,token,body=None):
            if path=='/v1/transfers/capabilities':return dict(CAPABILITY)
            return original(method,path,token=token,body=body)
        with patch.object(self.network,'request',side_effect=request):
            for i in range(601):self.local.rate(10000+i,20000+i,1)
            now=[1000]
            # Recreate scheduler each chunk to qualify durable restart behavior.
            receipts=[]
            for _ in range(7):
                receipts.append(Scheduler(self.sync,clock=lambda:now[0],jitter=lambda *_:0).tick())
            self.assertEqual(sum(r['uploaded'] for r in receipts),601)
            self.assertEqual(self.repo.diagnostics()['events'],{'accepted':601})
            self.assertFalse(self.sync.has_pending_feedback())
            self.assertFalse((self.root/'state/ratings-delivery-budget.json').exists())
            previous=receipts[-1]
            self.assertEqual(Scheduler(self.sync,clock=lambda:now[0],jitter=lambda *_:0).tick(),previous)

    def test_unrecognized_capability_never_enables_uncapped_uploads(self):
        from poolrotater_host.network.transfer_policy import enabled
        original=self.network.request
        def request(method,path,*,token,body=None):
            if path=='/v1/transfers/capabilities':return {'ok':True,'format':99,'uncapped_authorized_transfers':True}
            return original(method,path,token=token,body=body)
        with patch.object(self.network,'request',side_effect=request):self.assertFalse(enabled(self.network,refresh=True))
