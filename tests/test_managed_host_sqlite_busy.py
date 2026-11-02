"""SQLite contention defers managed observations without stopping the supervisor."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import sqlite3
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from poolrotater_host.diagnostic_logs.common import DatabaseBusy
from poolrotater_host.layout import load, setup
from poolrotater_host.managed_host.native import Coordinator
from poolrotater_host.managed_host.state import State
from rolling.supervisor import RollingSupervisor


def locked(path, mode):
    db=sqlite3.connect(path,timeout=0,isolation_level=None)
    if db.execute('PRAGMA journal_mode').fetchone()[0].lower()!='delete':
        db.close();raise AssertionError('contention regression requires native DELETE journal mode')
    db.execute('BEGIN '+mode)
    return db


class ManagedHostSQLiteBusyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'host';setup(self.root)
        marker,_=load(self.root)
        self.state=State(self.root,host_id=str(uuid.uuid4()),
            installation_id=marker['installation_id'],generation=marker['generation'])
        self.rows={}
        self.registry=SimpleNamespace(snapshot=lambda:{'rooms':self.rows})
        self.workers={}
        self.owner=SimpleNamespace(runtime=self.root,workers=self.workers,
            rollout_alias=None,targets={},registry=self.registry,configs={})
        self.coordinator=Coordinator(self.owner,self.state)
        self.owner.managed=self.coordinator
        self.status_writes=[]
        self.supervisor=object.__new__(RollingSupervisor)
        self.supervisor.config_loader=None
        self.supervisor.runtime=self.root
        self.supervisor.registry=self.registry
        self.supervisor.workers=self.workers
        self.supervisor.rollout_alias=None;self.supervisor.targets={}
        self.supervisor.empty_seconds=5;self.supervisor.schema_baseline={}
        self.supervisor.bookings=None;self.supervisor.managed=self.coordinator
        self.supervisor.booking_panels=None
        self.supervisor._refresh_configuration=lambda:None
        self.supervisor.reload_deployment_settings=lambda:None
        self.supervisor._protected_requests=lambda:None
        self.supervisor._requests=lambda:None
        self.supervisor._discord_commands=lambda:None
        self.supervisor.write_status=lambda:self.status_writes.append(True)
        # Model two already healthy siblings with an in-progress match. Their
        # identities are already durably observed, so this tick is read-only.
        for index,alias in enumerate(('room1','room2'),start=1):
            room_id=700+index;generation=f'{index:032x}'
            broker=SimpleNamespace(generation=generation,hub=SimpleNamespace(owned={room_id}))
            worker=SimpleNamespace(alias=alias,broker=broker,process=SimpleNamespace(poll=lambda:None),
                status={'public':{'current_map':{'state':'PLAYING','beatmap_id':900+index}}},
                idle_empty=lambda:False,fresh=lambda:False)
            self.workers[alias]=worker
            self.rows[alias]={'phase':'running','operation':'owned','ownership_unknown':False,
                              'room_id':room_id}
            self.coordinator._observations[alias]=(generation,room_id,'owned',False)

    def status_supervisor(self,rows=None):
        supervisor=object.__new__(RollingSupervisor)
        supervisor.directory=self.root/'state/rolling';supervisor.directory.mkdir(parents=True,exist_ok=True)
        supervisor.manifest={'version':'v7.3.9'}
        status_rows=rows or {}
        supervisor.registry=SimpleNamespace(snapshot=lambda:{'rooms':{alias:dict(row)
            for alias,row in status_rows.items()}})
        supervisor.workers={};supervisor.managed=self.coordinator
        supervisor.empty_seconds=5;supervisor.last_status_write=0;supervisor.config_error=None
        supervisor.private_pilot=None
        return supervisor

    def test_reserved_writer_does_not_block_read_snapshot_and_supervisor_keeps_siblings(self):
        path=self.root/'state/managed-host.sqlite3'
        writer=locked(path,'IMMEDIATE')
        try:
            # A normal reserved writer lock permits a query-only deferred read.
            self.assertEqual(self.state.pending(),[])
            # Capacity and status observations use query-only deferred reads,
            # which can coexist with a RESERVED writer in DELETE journal mode.
            self.supervisor.tick()
            self.assertEqual(self.status_writes,[True])
            self.assertEqual(set(self.workers),{'room1','room2'})
            self.assertEqual([w.status['public']['current_map']['state']
                              for w in self.workers.values()],['PLAYING','PLAYING'])
            self.assertEqual(self.coordinator.state_retry_at,0)
        finally:
            writer.rollback();writer.close()

        # Once contention ends, the ordinary next tick reads state and keeps
        # publishing status. No room operation was queued or replayed.
        self.coordinator.state_retry_at=0
        self.supervisor.tick()
        self.assertEqual(self.status_writes,[True,True])
        self.assertEqual(self.state.pending(),[])

    def test_exclusive_reader_contention_is_typed_and_deferred(self):
        path=self.root/'state/managed-host.sqlite3'
        writer=locked(path,'EXCLUSIVE')
        try:
            with self.assertRaises(DatabaseBusy) as raised:
                self.state.pending()
            self.assertEqual(str(raised.exception),'local_database_busy')
            self.supervisor.tick()
            self.assertEqual(self.status_writes,[True])
            self.assertEqual([w.status['public']['current_map']['state']
                              for w in self.workers.values()],['PLAYING','PLAYING'])
        finally:
            writer.rollback();writer.close()

        self.coordinator.state_retry_at=0
        self.supervisor.tick()
        self.assertEqual(self.status_writes,[True,True])

    def test_failed_owned_observation_retries_after_writer_releases(self):
        self.state.seed([{'alias':a,'settings':{'preset':'standard-fixed'}}
                         for a in ('room1','room2')])
        self.coordinator._observations.clear()
        writer=locked(self.state.path,'IMMEDIATE')
        try:
            self.supervisor.tick()
            self.assertNotIn('room1',self.coordinator._observations)
            self.assertEqual(self.state.room('room1')['phase'],'unobserved')
            self.assertEqual(self.status_writes,[True])
        finally:
            writer.rollback();writer.close()
        self.coordinator.state_retry_at=0
        self.supervisor.tick()
        self.assertIn('room1',self.coordinator._observations)
        self.assertEqual(self.state.room('room1')['room_id'],701)
        self.assertEqual(self.state.room('room2')['room_id'],702)
        self.assertEqual(self.status_writes,[True,True])

    def test_nonbusy_schema_errors_are_not_reclassified_as_deferrals(self):
        with sqlite3.connect(self.state.path) as db:
            db.execute('DROP TABLE operations')
        with self.assertRaises(sqlite3.OperationalError) as raised:
            self.state.pending()
        self.assertNotIsInstance(raised.exception,DatabaseBusy)

    def test_status_publication_reads_during_reserved_writer_without_stopping_workers(self):
        supervisor=self.status_supervisor();writer=locked(self.state.path,'IMMEDIATE')
        try:
            self.assertTrue(supervisor.write_status(force=True))
            self.assertTrue((supervisor.directory/'status.json').is_file())
            self.assertTrue((supervisor.directory/'public-status.json').is_file())
            self.assertEqual(supervisor.workers,{})
        finally:
            writer.rollback();writer.close()

    def test_exclusive_writer_defers_whole_status_snapshot_without_overwriting_authority(self):
        supervisor=self.status_supervisor();status=supervisor.directory/'status.json'
        public=supervisor.directory/'public-status.json'
        status.write_text('{"previous":"private-status"}\n');public.write_text('{"previous":"public-status"}\n')
        before=(status.read_bytes(),public.read_bytes());supervisor.last_status_write=123.0
        writer=locked(self.state.path,'EXCLUSIVE')
        try:
            self.assertFalse(supervisor.write_status(force=True))
            self.assertEqual((status.read_bytes(),public.read_bytes()),before)
            self.assertEqual(supervisor.last_status_write,123.0)
            self.assertEqual(supervisor.workers,{})
        finally:
            writer.rollback();writer.close()
        self.assertTrue(supervisor.write_status(force=True))


    def test_pure_managed_getters_use_typed_reads_and_real_schema_errors_still_escape(self):
        writer=locked(self.state.path,'EXCLUSIVE')
        try:
            readers=(lambda:self.state.room('room1'),self.state.limits,self.state.snapshot,
                     self.state.intent_sequence,
                     lambda:self.state.operation('00000000-0000-4000-8000-000000000000'))
            for read in readers:
                with self.assertRaises(DatabaseBusy):read()
        finally:
            writer.rollback();writer.close()
        with sqlite3.connect(self.state.path) as db:db.execute('DROP TABLE rooms')
        with self.assertRaises(sqlite3.OperationalError) as raised:self.state.snapshot()
        self.assertNotIsInstance(raised.exception,DatabaseBusy)


if __name__=='__main__':
    unittest.main()
