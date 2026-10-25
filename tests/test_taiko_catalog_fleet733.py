"""Taiko catalog modes remain closed, generation-bound managed operations."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from contextlib import contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from poolrotater_host.managed_host.contracts import request, require_catalog_client_version
from poolrotater_host.managed_host import catalog_fleet
from poolrotater_host.network.contracts import NetworkError


class ManagedTaikoCatalogContractTests(unittest.TestCase):
    def catalog_request(self, mode='taiko'):
        return {
            'protocol_version':1,'policy_version':1,'id':str(uuid.uuid4()),
            'host_id':str(uuid.uuid4()),'installation_id':str(uuid.uuid4()),
            'generation':str(uuid.uuid4()),'alias':'room1','actor':'catalog-fleet:test',
            'role':'FLEET_ADMIN','action':'catalog','expected_revision':4,
            'expected_room_generation':'a'*32,'timing':'when_empty',
            'patch':{'mode':mode,'content_id':'b'*64},'expires_at':2000,
            'cancel_operation_id':None}

    def test_taiko_is_accepted_without_widening_catalog_request_fields(self):
        value=self.catalog_request()
        self.assertIs(request(value),value)
        value['patch']['path']='/tmp/catalog.db'
        with self.assertRaises(NetworkError):request(value)

    def test_unsupported_catalog_mode_is_still_rejected(self):
        with self.assertRaises(NetworkError):request(self.catalog_request('unsupported'))


class FakeCatalogRepository:
    def __init__(self, root, *, desired='running'):
        self.root=Path(root)
        self.host_db=self.root/'hosts.sqlite3'
        self.report_db=self.root/'reports.sqlite3'
        self.path=self.report_db
        self.calls=[]
        self.store=self
        self.clock=lambda:1000
        host_id=str(uuid.uuid4());installation_id=str(uuid.uuid4());generation=str(uuid.uuid4())
        self.host_id=host_id
        heartbeat={'code_version':'v7.3.3','rooms':([
            {'alias':'room1','code_version':'v7.3.3','observed_at':990}]
            if desired=='running' else [])}
        capabilities={'controller_phase':'running','installation_id':installation_id,
            'generation':generation,'rooms':[{'alias':'room1','desired':desired,
            'revision':9,'room_generation':'c'*32,'preset':'taiko-auto',
            'phase':'running' if desired=='running' else 'stopped',
            'pending_operation':None,'failure_code':None}]}
        with sqlite3.connect(self.host_db) as db:
            db.execute('CREATE TABLE hosts(id TEXT,revoked INTEGER,received REAL,heartbeat TEXT,installation_id TEXT,generation TEXT)')
            db.execute('INSERT INTO hosts VALUES(?,?,?,?,?,?)',
                (host_id,0,990,json.dumps(heartbeat),installation_id,generation))
        with sqlite3.connect(self.report_db) as db:
            db.execute('CREATE TABLE reports(host TEXT,received REAL,capabilities TEXT)')
            db.execute('INSERT INTO reports VALUES(?,?,?)',
                (host_id,990,json.dumps(capabilities)))

    @contextmanager
    def transaction(self):
        connection=sqlite3.connect(self.host_db)
        connection.row_factory=sqlite3.Row
        try:yield connection
        finally:connection.close()

    def queue(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        return {'ok':True,'id':kwargs['operation_id'],'action':args[2]}


class TaikoCatalogFleetTests(unittest.TestCase):
    @contextmanager
    def reports_transaction(self,path):
        connection=sqlite3.connect(path)
        connection.row_factory=sqlite3.Row
        try:yield connection
        finally:connection.close()

    def run_fleet(self,desired='running'):
        with tempfile.TemporaryDirectory() as directory:
            repository=FakeCatalogRepository(directory,desired=desired)
            with patch.object(catalog_fleet,'transaction',self.reports_transaction):
                result=catalog_fleet.queue(repository,expected_hosts=[repository.host_id],
                    required_version='v7.3.3',mode='taiko',content_id='d'*64,
                    batch_id=str(uuid.uuid4()))
            return result,repository.calls

    def test_taiko_catalog_rollout_targets_only_matching_live_rooms(self):
        result,calls=self.run_fleet()
        self.assertEqual(result['mode'],'taiko')
        self.assertEqual(result['content_id'],'d'*64)
        self.assertEqual(len(calls),1)
        args,kwargs=calls[0]
        self.assertEqual(args[:3],(result['gated_hosts'][0],'room1','catalog'))
        self.assertEqual(kwargs['patch'],{'mode':'taiko','content_id':'d'*64})
        self.assertEqual(kwargs['timing'],'when_empty')

    def test_stopped_taiko_slot_is_not_reopened_by_fleet_rollout(self):
        result,calls=self.run_fleet(desired='stopped')
        self.assertEqual(result['mode'],'taiko')
        self.assertEqual(calls,[])

    def test_fleet_gate_rejects_taiko_below_minimum_but_leaves_legacy_modes_open(self):
        with self.assertRaises(NetworkError) as caught:
            require_catalog_client_version('taiko','v7.2.14')
        self.assertEqual(caught.exception.code,'catalog_client_version_unsupported')
        self.assertIsNone(require_catalog_client_version('standard','v7.2.14'))


class TaikoHubVersionGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=1_000_000
        from poolrotater_host.managed_host.state import State
        from poolrotater_host.managed_host.hub import Repository
        from poolrotater_host.network.hub_layout import initialize
        from poolrotater_host.network.store import Store
        from poolrotater_host.network.contracts import secret
        from poolrotater_host.ratings.repository import prepare
        hub=self.root/'hub';initialize(hub);self.store=Store(hub,clock=lambda:self.now);prepare(self.store)
        self.installation_id=str(uuid.uuid4());self.generation=str(uuid.uuid4())
        identity={'operator_id':str(uuid.uuid4()),'account_id':99,'directory':True,
            'installation_id':self.installation_id,'generation':self.generation}
        invitation=self.store.invite(identity,label='invented catalog test')['invitation']
        self.token=secret()
        enrolled=self.store.enroll({'protocol_version':1,'request_id':str(uuid.uuid4()),
            'invitation':invitation,'credential':self.token,**identity})
        self.host_id=enrolled['host_id']
        self.state=State(self.root/'host',host_id=self.host_id,
            installation_id=self.installation_id,generation=self.generation,clock=lambda:self.now)
        self.state.seed([{'alias':f'room{i}','settings':{'preset':'standard-auto',
            'visibility':'public','sr_min':4.0,'sr_max':5.0,'countdown_seconds':20}}
            for i in range(1,5)])
        self.state.policy_applied(1);self.repository=Repository(self.store)
        self.repository.accept(self.token,{'protocol_version':1,
            'installation_id':self.installation_id,'generation':self.generation,'sequence':1,
            'sent_at':self.now,'capabilities':self.state.snapshot(controller_phase='running'),'receipts':[]})

    def heartbeat(self,version,sequence=1):
        self.store.heartbeat(self.token,{'protocol_version':1,
            'installation_id':self.installation_id,'generation':self.generation,'sequence':sequence,
            'code_version':version,'catalog_version':'a'*64,'rooms':[]})

    def queue_taiko(self,operation_id=None):
        return self.repository.queue(self.host_id,'room1','catalog',patch={'mode':'taiko',
            'content_id':'b'*64},expires_seconds=600,operation_id=operation_id)

    def test_hub_rejects_old_taiko_client_and_accepts_current_version(self):
        self.heartbeat('v7.2.14')
        with self.assertRaises(NetworkError) as caught:self.queue_taiko()
        self.assertEqual(caught.exception.code,'catalog_client_version_unsupported')
        self.now+=5
        self.heartbeat('v7.3.3',sequence=2)
        result=self.queue_taiko()
        self.assertEqual(result['request']['patch']['mode'],'taiko')

    def test_existing_taiko_operation_retry_survives_stale_heartbeat(self):
        self.heartbeat('v7.3.3')
        operation_id=str(uuid.uuid4());first=self.queue_taiko(operation_id)
        self.now+=181
        replay=self.queue_taiko(operation_id)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['request'],first['request'])


class TaikoCatalogCliTests(unittest.TestCase):
    def test_explicit_download_and_import_choices_accept_taiko(self):
        from poolrotater_host import cli, pilot_operations
        with patch('poolrotater_host.catalog_download.download_catalog',return_value={'ok':True}) as download, \
             redirect_stdout(io.StringIO()):
            result=cli.main(['catalog-download','--origin','https://catalog.invalid/',
                '--cache','/tmp/cache','--trust-root','/tmp/root.json','--trust-sha256','a'*64,
                '--ca-file','/tmp/ca.pem','--ca-sha256','b'*64,'--destination','/tmp/catalog',
                '--mode','taiko'])
        self.assertEqual(result,0)
        self.assertEqual(download.call_args.kwargs['mode'],'taiko')
        with patch.object(pilot_operations,'import_catalogs',return_value={'ok':True}) as imported, \
             redirect_stdout(io.StringIO()):
            result=cli.main(['pilot-catalog-import','--instance','qa-host',
                '--repository','/tmp/repository','--trust-root','/tmp/root.json',
                '--trust-sha256','a'*64,'--mode','taiko'])
        self.assertEqual(result,0)
        self.assertEqual(imported.call_args.kwargs['modes'],['taiko'])


if __name__=='__main__':unittest.main()
