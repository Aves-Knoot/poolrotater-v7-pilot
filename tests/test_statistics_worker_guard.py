"""Player commands read complete aggregate replicas under the installed worker guard."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import sqlite3
from unittest.mock import Mock,patch
import unittest
from bot.bot import LobbyBot
from bot.ppstats import PPStatsStore
from poolrotater_host.activity.reader import Reader
from rolling.storage import install_guard
from tests import test_local_statistics_replica as replica_fixture


class StatisticsWorkerGuardTests(unittest.TestCase):
    setUpClass=classmethod(replica_fixture.ReplicaTests.setUpClass.__func__)
    tearDownClass=classmethod(replica_fixture.ReplicaTests.tearDownClass.__func__)
    setUp=replica_fixture.ReplicaTests.setUp
    stop=replica_fixture.ReplicaTests.stop
    enable=replica_fixture.ReplicaTests.enable
    presence=replica_fixture.ReplicaTests.presence
    _publish_presence=replica_fixture.ReplicaTests._publish_presence
    _fresh=replica_fixture.ReplicaTests._fresh
    _new_generation=replica_fixture.ReplicaTests._new_generation

    def test_named_stats_uses_complete_local_replica_inside_worker(self):
        self._publish_presence();self._new_generation()
        bot=LobbyBot.__new__(LobbyBot)
        bot.cfg={'ruleset_id':0,'_host_root':str(self.root),
                 'ratings_db':str(self.root/'state/controller-ratings.db')}
        bot.prefix='.';bot.players={8:{'name':'Requester'}}
        bot.say=Mock();bot._is_project_banned=Mock(return_value=False)
        bot.pp_stats=PPStatsStore(bot.cfg['ratings_db'],reader=Reader(self.root))
        with patch.object(sqlite3,'connect',sqlite3.connect):
            install_guard(bot.cfg)
            self.assertTrue(bot._show_player_stats(8,['.stats','Invented','Player']))
        self.assertIn('Invented Player',bot.say.call_args.args[0])
        self.assertIn('Playtime:',bot.say.call_args.args[0])
        self.assertNotIn('fetching',bot.say.call_args.args[0])

    def test_worker_can_resolve_all_supported_rulesets_without_fetching(self):
        self._publish_presence();self._new_generation()
        reader=Reader(self.root)
        with patch.object(sqlite3,'connect',sqlite3.connect):
            install_guard({'_host_root':str(self.root)})
            for mode in (0,3):
                with self.subTest(mode=mode):
                    self.assertEqual(reader.lookup('Invented Player',ruleset_id=mode),(7,'Invented Player'))
                    self.assertIs(reader.hidden(7,ruleset_id=mode),False)


class StatisticsStorageBoundaryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'host';self.root.mkdir(mode=0o700)
        (self.root/'state').mkdir(mode=0o700)
        self.base=self.root/'state/activity-replica-downloads';self.base.mkdir(mode=0o700)
        self.native=sqlite3.connect

    def database(self,digest='a'*64,name='replica.sqlite3'):
        parent=self.base/digest;parent.mkdir(mode=0o700,exist_ok=True)
        path=parent/name
        with self.native(path) as db:
            db.execute('CREATE TABLE value(n INTEGER)');db.execute('INSERT INTO value VALUES(7)')
        path.chmod(0o600);return path

    def open(self,path,query='mode=ro&immutable=1'):
        return sqlite3.connect(path.as_uri()+'?'+query,uri=True)

    def guard(self):
        return install_guard({'_host_root':str(self.root)})

    def test_replica_rotation_is_readable_without_restarting_worker(self):
        first=self.database()
        with patch.object(sqlite3,'connect',sqlite3.connect):
            self.guard()
            with self.open(first) as db:self.assertEqual(db.execute('SELECT n FROM value').fetchone(),(7,))
            # A separate agent publishes another complete generation later.
            second=self.database('b'*64)
            with self.open(second) as db:self.assertEqual(db.execute('SELECT n FROM value').fetchone(),(7,))

    def test_replica_writes_and_schema_changes_are_refused(self):
        path=self.database()
        with patch.object(sqlite3,'connect',sqlite3.connect):
            self.guard()
            for query in ('mode=rw&immutable=1','mode=ro','immutable=1'):
                with self.subTest(query=query),self.assertRaises(sqlite3.OperationalError):self.open(path,query)
            with self.assertRaises(sqlite3.OperationalError):sqlite3.connect(path)
            for sql in ('DELETE FROM value','CREATE TABLE other(n)','ATTACH ":memory:" AS other','PRAGMA user_version=3'):
                with self.subTest(sql=sql),self.open(path) as db:
                    with self.assertRaises(sqlite3.DatabaseError):db.execute(sql)

    def test_unrelated_staging_and_invalid_generation_paths_are_refused(self):
        paths=[self.database('a'*64,'replica-building.sqlite3'),self.database('wrong-generation')]
        outside=self.root/'state/other.sqlite3'
        with self.native(outside) as db:db.execute('CREATE TABLE value(n)')
        paths.append(outside)
        with patch.object(sqlite3,'connect',sqlite3.connect):
            self.guard()
            for path in paths:
                with self.subTest(path=path.name),self.assertRaises(sqlite3.OperationalError):self.open(path)

    def test_symlink_and_nonprivate_replica_are_refused(self):
        path=self.database();path.chmod(0o644)
        with patch.object(sqlite3,'connect',sqlite3.connect):
            self.guard()
            with self.assertRaises(sqlite3.OperationalError):self.open(path)
        path.chmod(0o600);other=self.base/('b'*64);other.symlink_to(path.parent,target_is_directory=True)
        with patch.object(sqlite3,'connect',sqlite3.connect):
            self.guard()
            with self.assertRaises(sqlite3.OperationalError):self.open(other/path.name)

    def test_no_host_root_does_not_admit_private_replica_directory(self):
        path=self.database()
        with patch.object(sqlite3,'connect',sqlite3.connect):
            install_guard({})
            with self.assertRaises(sqlite3.OperationalError):self.open(path)


class CatchReplicaTransportTests(unittest.TestCase):
    setUpClass=classmethod(replica_fixture.ReplicaTests.setUpClass.__func__)
    tearDownClass=classmethod(replica_fixture.ReplicaTests.tearDownClass.__func__)
    setUp=replica_fixture.ReplicaTests.setUp
    stop=replica_fixture.ReplicaTests.stop
    enable=replica_fixture.ReplicaTests.enable

    def test_actual_v2_https_worker_downloads_large_catch_pages_and_guarded_command(self):
        from poolrotater_host.config import defaults
        from poolrotater_host.layout import load,atomic_json
        from poolrotater_host.activity.replica import Replica
        from poolrotater_host.activity import Repository
        from poolrotater_host.network.contracts import canonical
        self.enable();marker,host=load(self.root)
        host['rooms']=defaults(['catch-fixed'])['rooms'];atomic_json(self.root/'config/host.json',host)
        source=Repository(self.store).snapshot();hid=self.client.credential()['host_id']
        source['presence']=[{'user_id':10000+i,'username':'Invented Catch '+str(i),'host_id':hid,
                            'event_id':str(i),'mode':'catch','room_id':99,'started_at':100,'ended_at':160}
                           for i in range(300)]
        with patch.object(self.server.statistics.repository,'snapshot',return_value=source):
            self.server.statistics.stamp=None
            value=Replica(self.root).sync(self.client,force=True)
        self.assertEqual(value['rows'],900)
        self.assertTrue(self.client.transport(self.client.profile()).last_worker_reaped)
        reader=Reader(self.root)
        with patch.object(sqlite3,'connect',sqlite3.connect):
            install_guard({'_host_root':str(self.root)})
            self.assertEqual(reader.lookup('Invented Catch 299',ruleset_id=2),(10299,'Invented Catch 299'))
            self.assertEqual(reader.user_stats(10299,ruleset_id=2)['total_lobby_seconds'],60)
        self.assertGreater(value['transferred'],65536)

    def test_catch_host_falls_back_safely_against_legacy_hub(self):
        import threading
        from poolrotater_host.config import defaults
        from poolrotater_host.layout import load,atomic_json
        from poolrotater_host.activity.replica import Replica
        from poolrotater_host.network.server import tls
        from tests.legacy_statistics_fixture import LegacyHubServer
        self.server.shutdown();self.server.server_close();self.thread.join(3)
        self.server=LegacyHubServer(('127.0.0.1',self.server.server_port),self.store,
            tls_context=tls(self.certs/'server.crt',self.certs/'server.key'))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.enable();_,host=load(self.root);host['rooms']=defaults(['catch-fixed'])['rooms']
        atomic_json(self.root/'config/host.json',host)
        result=Replica(self.root).sync(self.client,force=True)
        self.assertEqual(result['phase'],'legacy')
        self.assertIsNone(Reader(self.root).lookup('Not available',ruleset_id=2))
