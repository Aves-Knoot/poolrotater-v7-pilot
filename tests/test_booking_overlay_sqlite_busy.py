"""Real SQLite contention cannot tear down an already healthy supervisor."""

from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import copy
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from poolrotater_host.bookings.native import overlay
from poolrotater_host.bookings.local import LocalSessions
from poolrotater_host.diagnostic_logs.common import transaction, DatabaseBusy
from poolrotater_host.layout import atomic_json, load, setup
from poolrotater_host.managed_host.state import State
from poolrotater_host.network.contracts import NetworkError
from rolling.supervisor import RollingSupervisor


def populated_root(path):
    setup(path)
    marker,_=load(path)
    binding={'host_id':str(uuid.uuid4()),'installation_id':marker['installation_id'],
             'generation':marker['generation']}
    State(path,**binding)
    atomic_json(path/'config/private-bookings.json',
        {'format':1,'enabled':True,'aliases':['room1']})
    sessions=LocalSessions(path,binding)
    lease={'id':str(uuid.uuid4()),'user_id':777,'designated_friend_id':None,
        **binding,'alias':'room1','session_generation':'a'*32,'preset':'standard-fixed',
        'settings':{'sr_min':3.5,'sr_max':4.5},'password':'fixture-private-password'}
    sessions.install(lease,{'sr_min':3.5,'sr_max':4.5,'countdown_seconds':20})
    return binding,lease


def configs():
    return [{'_instance_name':'room1','preset':'standard-fixed','min_stars':2.0,
             'max_stars':5.0},
            {'_instance_name':'room2','preset':'mania-4k','min_stars':3.0,
             'max_stars':4.0}]


def lock(path,mode='IMMEDIATE'):
    db=sqlite3.connect(path,timeout=0,isolation_level=None)
    db.execute('BEGIN '+mode)
    return db


class BookingOverlaySQLiteBusyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'host'
        self.binding,self.lease=populated_root(self.root)

    def test_overlay_reads_binding_and_leases_under_real_immediate_writer(self):
        before=configs()
        dbpath=self.root/'state/managed-host.sqlite3'
        booking_path=self.root/'state/bookings.sqlite3'
        managed_writer=lock(dbpath,'IMMEDIATE')
        booking_writer=lock(booking_path,'IMMEDIATE')
        try:
            # The old State()/LocalSessions() path required BEGIN IMMEDIATE
            # and failed on the actual SQLite busy result, not a mocked exception.
            try:
                with transaction(dbpath):
                    pass
            except sqlite3.OperationalError as error:
                self.assertEqual(getattr(error,'sqlite_errorcode',0)&0xff,sqlite3.SQLITE_BUSY)
            else:
                self.fail('fixture writer did not reproduce the old write-lock conflict')

            result=overlay(self.root,before)
            self.assertEqual(result[0]['_booking_session'],{
                'id':self.lease['id'],'alias':'room1','session_generation':'a'*32,
                'preset':'standard-fixed'})
            self.assertEqual((result[0]['sr_min'],result[0]['sr_max']),(3.5,4.5))
            self.assertEqual(result[1],before[1])
            self.assertIsNone(before[0].get('_booking_session'))
        finally:
            booking_writer.rollback();booking_writer.close()
            managed_writer.rollback();managed_writer.close()


    def test_supervisor_defers_exclusive_read_contention_and_recovers_after_release(self):
        baseline=configs();current=copy.deepcopy(baseline)
        current[0]['_booking_session']={'id':'live-session','alias':'room1'}
        match={'state':'PLAYING','room_id':901,'beatmap_id':12345}
        worker=type('Worker',(),{})()
        worker.status={'public':{'current_map':match}}
        supervisor=object.__new__(RollingSupervisor)
        supervisor.config_loader=lambda:overlay(self.root,baseline)
        supervisor.config_error=None
        supervisor.configs={cfg['_instance_name']:copy.deepcopy(cfg) for cfg in current}
        supervisor.workers={'room1':worker}

        writer=lock(self.root/'state/managed-host.sqlite3','EXCLUSIVE')
        try:
            supervisor._refresh_configuration()
            self.assertEqual(supervisor.config_error,'local_database_busy')
            self.assertEqual(supervisor.configs['room1'],current[0])
            self.assertEqual(supervisor.configs['room2'],current[1])
            self.assertEqual(supervisor.workers['room1'].status['public']['current_map'],match)
        finally:
            writer.rollback();writer.close()

        supervisor._refresh_configuration()
        self.assertIsNone(supervisor.config_error)
        refreshed=overlay(self.root,baseline)
        self.assertEqual(refreshed[0]['_booking_session']['alias'],'room1')
        self.assertEqual(supervisor.configs['room1'],current[0])
        self.assertEqual(supervisor.workers['room1'].status['public']['current_map'],match)


    def test_sqlite_busy_is_typed_but_bad_binding_and_schema_still_refuse(self):
        writer=lock(self.root/'state/managed-host.sqlite3','EXCLUSIVE')
        try:
            with self.assertRaises(DatabaseBusy) as error:
                overlay(self.root,configs())
            self.assertEqual(str(error.exception),'local_database_busy')
        finally:
            writer.rollback();writer.close()

        # A malformed binding remains an authority error, never a busy deferral.
        from poolrotater_host.bookings.policy import native_binding
        with transaction(self.root/'state/managed-host.sqlite3') as db:
            db.execute("UPDATE meta SET value='{}' WHERE key='binding'")
        with self.assertRaises(NetworkError) as error:
            native_binding(self.root)
        self.assertNotIsInstance(error.exception,DatabaseBusy)

        # Missing persisted tables are also a schema refusal, not transient
        # contention; the retry path never upgrades them into a busy result.
        dbpath=self.root/'state/bookings.sqlite3'
        with transaction(dbpath) as db:
            db.execute('DROP TABLE host_bookings')
        with self.assertRaises(NetworkError) as error:
            overlay(self.root,configs())
        self.assertNotEqual(getattr(error.exception,'code',None),'local_database_busy')


if __name__=='__main__':
    unittest.main()
