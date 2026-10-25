"""Local lifecycle operations must not masquerade as hub-delivered receipts."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from pathlib import Path
import concurrent.futures
import json
import threading
import tempfile
import unittest
import uuid

from poolrotater_host.managed_host.state import State
from poolrotater_host.managed_host.hub import Repository
from poolrotater_host.network.contracts import NetworkError, secret
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.store import Store


class ManagedLocalReceiptProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=1_800_000_000
        self.host_id=str(uuid.uuid4());self.installation_id=str(uuid.uuid4())
        self.generation=str(uuid.uuid4())
        self.binding={'host_id':self.host_id,'installation_id':self.installation_id,
                      'generation':self.generation}
        self.state=State(self.root/'host',**self.binding,clock=lambda:self.now)
        self.state.seed([{'alias':'room'+str(index),'settings':{'preset':'standard-auto',
            'visibility':'public','sr_min':4.0,'sr_max':5.0}} for index in range(1,5)])
        self.state.policy_applied(1)
        self._setup_hub()

    def _setup_hub(self):
        hub_root=self.root/'hub';initialize(hub_root)
        self.network=Store(hub_root,clock=lambda:self.now)
        from poolrotater_host.ratings.repository import prepare
        prepare(self.network)
        self.token=secret()
        identity={'operator_id':str(uuid.uuid4()),'account_id':99,'directory':True,
            'installation_id':self.installation_id,'generation':self.generation}
        invite=self.network.invite(identity,label='Fixture managed host')['invitation']
        self.host_id=self.network.enroll({'protocol_version':1,'request_id':str(uuid.uuid4()),
            'invitation':invite,'credential':self.token,**identity})['host_id']
        self.binding['host_id']=self.host_id
        # Reopen the host ledger with the hub-issued authenticated binding.
        import shutil
        shutil.rmtree(self.root/'host')
        self.state=State(self.root/'host',**self.binding,clock=lambda:self.now)
        self.state.seed([{'alias':'room'+str(index),'settings':{'preset':'standard-auto',
            'visibility':'public','sr_min':4.0,'sr_max':5.0}} for index in range(1,5)])
        self.state.policy_applied(1)
        self.hub=Repository(self.network)
        self.sequence=0

    def _exchange(self):
        body=self.state.exchange(controller_phase='running')
        self.hub.accept(self.token,body)
        self.state.acknowledge_exchange(body)
        self.sequence=body['sequence']
        return body

    def _local_open(self,operation_id=None,*,actor='controller-update-drain-restore'):
        return self.state.local_lifecycle('room1','open',
            operation_id=operation_id or str(uuid.uuid4()),actor=actor)

    def _remote_open(self,operation_id=None,*,actor='hub-administrator',timing='immediate'):
        self._exchange()
        return self.hub.queue(self.host_id,'room1','open',timing=timing,
            actor=actor,operation_id=operation_id)

    def test_local_open_and_exact_retry_never_enter_hub_receipt_outbox(self):
        operation_id=str(uuid.uuid4())
        first=self._local_open(operation_id)
        retry=self._local_open(operation_id)
        self.assertEqual(retry,first)
        self.assertEqual(self.state.exchange(controller_phase='running')['receipts'],[])

    def test_local_open_progress_and_terminal_receipt_never_enter_hub_outbox(self):
        operation_id=str(uuid.uuid4())
        self._local_open(operation_id)
        # Neither the initial local receipt nor later lifecycle progress belongs
        # to the exchange receipt channel consumed by the authenticated hub.
        self.state.begin(operation_id,ready=True)
        self.state.finish(operation_id,'applied',result={
            'application':'lifecycle','room_id':None,'revision':1})
        self.assertEqual(self.state.exchange(controller_phase='running')['receipts'],[])

    def test_remote_operation_progress_still_reaches_hub_and_is_acknowledged(self):
        queued=self._remote_open()
        request=queued['request']
        self.assertEqual(self.state.receive(request)['phase'],'received')
        self.state.begin(request['id'],ready=True)
        receipt=self.state.finish(request['id'],'applied',result={
            'application':'lifecycle','room_id':None,'revision':1})
        outgoing=self.state.exchange(controller_phase='running')
        self.assertEqual(outgoing['receipts'],[receipt])
        self.hub.accept(self.token,outgoing)
        self.state.acknowledge_exchange(outgoing)
        persisted=self.hub.inspect(self.host_id)['operations']
        self.assertEqual(persisted[0]['receipt'],receipt)

    def test_local_then_remote_uuid_collision_is_refused(self):
        from poolrotater_host.diagnostic_logs.common import transaction
        operation_id=str(uuid.uuid4())
        self._exchange()
        actor='controller-update-drain-restore'
        remote=self.hub.queue(self.host_id,'room1','open',timing='when_empty',
            actor=actor,operation_id=operation_id)['request']
        self.assertEqual(self._local_open(operation_id,actor=actor)['id'],remote['id'])
        with transaction(self.state.path) as db:
            local=json.loads(db.execute('SELECT payload FROM operations WHERE id=?',
                (operation_id,)).fetchone()['payload'])
        self.assertEqual(local,remote)
        with self.assertRaises(NetworkError) as caught:self.state.receive(remote)
        self.assertEqual(caught.exception.code,'managed_operation_conflict')

    def test_remote_then_local_uuid_collision_is_refused(self):
        operation_id=str(uuid.uuid4())
        actor='controller-update-drain-restore'
        queued=self._remote_open(operation_id,actor=actor,timing='when_empty')
        self.state.receive(queued['request'])
        expected={'protocol_version':1,'policy_version':1,'id':operation_id,**self.binding,
            'alias':'room1','actor':actor,'role':'FLEET_ADMIN','action':'open',
            'expected_revision':0,'expected_room_generation':None,'timing':'when_empty',
            'patch':{},'expires_at':self.now+600,'cancel_operation_id':None}
        self.assertEqual(queued['request'],expected)
        with self.assertRaises(NetworkError) as caught:self._local_open(operation_id,actor=actor)
        self.assertEqual(caught.exception.code,'managed_operation_conflict')

    def test_local_terminal_retry_from_prior_enrollment_is_refused(self):
        operation_id=str(uuid.uuid4())
        self._local_open(operation_id)
        self.state.begin(operation_id,ready=True)
        self.state.finish(operation_id,'applied',result={
            'application':'lifecycle','room_id':None,'revision':1})
        self.state.rebind(str(uuid.uuid4()))
        with self.assertRaises(NetworkError) as caught:self._local_open(operation_id)
        self.assertEqual(caught.exception.code,'managed_operation_conflict')

    def test_hub_terminal_retry_from_prior_enrollment_is_refused_as_local(self):
        operation_id=str(uuid.uuid4());actor='controller-update-drain-restore'
        queued=self._remote_open(operation_id,actor=actor,timing='when_empty')
        request=queued['request'];self.state.receive(request)
        self.state.begin(operation_id,ready=True)
        self.state.finish(operation_id,'applied',result={
            'application':'lifecycle','room_id':None,'revision':1})
        self.state.rebind(str(uuid.uuid4()))
        with self.assertRaises(NetworkError) as caught:self._local_open(operation_id,actor=actor)
        self.assertEqual(caught.exception.code,'managed_operation_conflict')

    def test_stale_state_object_refreshes_binding_before_retry(self):
        operation_id=str(uuid.uuid4())
        self._local_open(operation_id)
        stale=State(self.root/'host',**self.binding,clock=lambda:self.now)
        new_host_id=str(uuid.uuid4());self.state.rebind(new_host_id)
        with self.assertRaises(NetworkError) as caught:
            stale.local_lifecycle('room1','open',operation_id=operation_id,
                actor='controller-update-drain-restore')
        self.assertEqual(caught.exception.code,'managed_operation_conflict')
        self.assertEqual(stale.binding['host_id'],new_host_id)

    def test_stale_local_enqueue_does_not_create_hub_delivery(self):
        from poolrotater_host.diagnostic_logs.common import transaction
        operation_id=str(uuid.uuid4());enqueue=self.state.enqueue
        def race_revision(value):
            with transaction(self.state.path) as db:
                db.execute("UPDATE rooms SET revision=revision+1 WHERE alias='room1'")
            return enqueue(value)
        self.state.enqueue=race_revision
        with self.assertRaises(NetworkError) as caught:self._local_open(operation_id)
        self.assertEqual(caught.exception.code,'configuration_revision_changed')
        self.assertNotIn(operation_id,{item['id'] for item in
            self.state.exchange(controller_phase='running')['receipts']})

    def test_concurrent_local_and_exact_remote_uuid_admission_has_one_origin(self):
        operation_id=str(uuid.uuid4());actor='controller-update-drain-restore'
        row=self.state.room('room1')
        remote={'protocol_version':1,'policy_version':1,'id':operation_id,**self.binding,
            'alias':'room1','actor':actor,'role':'FLEET_ADMIN','action':'open',
            'expected_revision':row['revision'],'expected_room_generation':row['room_generation'],
            'timing':'when_empty','patch':{},'expires_at':self.now+600,
            'cancel_operation_id':None}
        barrier=threading.Barrier(2)
        def local():
            barrier.wait(timeout=2)
            try:return ('local',self._local_open(operation_id,actor=actor))
            except NetworkError as error:return ('conflict',error.code)
        def hub():
            barrier.wait(timeout=2)
            try:return ('hub',self.state.receive(remote))
            except NetworkError as error:return ('conflict',error.code)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(local),pool.submit(hub)]
            outcomes=[future.result(timeout=5) for future in futures]
        self.assertEqual(sum(kind in ('local','hub') for kind,_ in outcomes),1)
        self.assertEqual(sum(kind=='conflict' and code=='managed_operation_conflict'
                             for kind,code in outcomes),1)
        receipt_ids={item['id'] for item in self.state.exchange(controller_phase='running')['receipts']}
        if any(kind=='local' for kind,_ in outcomes):self.assertNotIn(operation_id,receipt_ids)
        else:self.assertIn(operation_id,receipt_ids)

    def test_hub_keeps_rejecting_unrequested_receipts(self):
        bogus={'id':str(uuid.uuid4()),'alias':'room1','phase':'applied',
            'failure_code':None,'result':None}
        body={'protocol_version':1,'installation_id':self.installation_id,
            'generation':self.generation,'sequence':1,'sent_at':self.now,
            'capabilities':self.state.snapshot(controller_phase='running'),'receipts':[bogus]}
        with self.assertRaises(NetworkError) as caught:self.hub.accept(self.token,body)
        self.assertEqual(caught.exception.code,'unknown_managed_receipt')

    def test_hub_rejects_local_receipt_without_weakening_provenance_validation(self):
        operation_id=str(uuid.uuid4());self._exchange();self._local_open(operation_id)
        outgoing=self.state.exchange(controller_phase='running')
        self.assertEqual(outgoing['receipts'],[])
        response=self.hub.accept(self.token,outgoing)
        self.assertEqual(response['requests'],[])


if __name__=='__main__':unittest.main()
