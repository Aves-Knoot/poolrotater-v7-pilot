"""Blocked native gameplay observations are distinct from worker liveness.

The four-room case uses native workers, socketpair RPC and invented TLS services.
The terminal matrix isolates the service loop with real processes/connections;
it does not qualify installed root registration or real provider gameplay.
"""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from contextlib import contextmanager
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from poolrotater_host.config import ConfigError
from poolrotater_host.provider_service import run_provider_service
from poolrotater_host.service_diagnostics import failure_details
from poolrotater_host.testing.fixtures import create_catalogs
from rolling.protocol import Peer
from rolling.state import Registry
from rolling.supervisor import Worker
from test_v7_provider_rooms import RoomCase
from test_v7_runtime import fixture_release


class NativeBlockedObservationTests(RoomCase):
    def setUp(self):
        super().setUp()
        # This liveness fixture invents only 5-star standard maps. Its catalog
        # is independent of the owner-tuned fresh-install fixed-room range.
        from poolrotater_host.layout import read_json, atomic_json
        host=read_json(self.root/'config/host.json')
        for room in host['rooms']:
            if room['preset']=='standard-fixed':room.update(sr_min=4.5,sr_max=5.5)
        atomic_json(self.root/'config/host.json',host)
        create_catalogs(self.root)
        self.release = fixture_release(self.root, 'v7.0.0')
        self.auth = self.authorized()
        self.backend, self.api, self.hub = self.adapters(self.auth)
        self.enterContext(patch('rolling.supervisor.require_immutable'))

    def test_four_native_api_queues_survive_stale_observations_without_empty_or_public_authority(self):
        release_mutex = threading.Event()
        mutex_held = threading.Event()
        all_waiting = threading.Event()
        lock = threading.Lock()
        waiting, delivered = set(), set()
        reply_marker = '.queue · .skip · .start'
        phase = {'blocked_ticks': 0, 'resumed': False}
        phases, workers = [], []
        original_messages = self.api.chat_messages

        def hold_actual_authorization_mutex():
            with self.auth._mutex:
                mutex_held.set()
                # A bounded fixture delay; never a real provider timeout.
                release_mutex.wait(25)

        def queued_poll(channel, *args, **kwargs):
            result = original_messages(channel, *args, **kwargs)
            with lock:
                if channel in delivered:
                    return result
                delivered.add(channel)
            # Native chat command handling and reply RPC, not a synthetic send.
            return [*(result or []), {'message_id': 50000 + channel,
                                     'sender_id': 7, 'content': '.help'}]

        holder = None
        original_tick = None

        def observer(name, supervisor, current_workers):
            nonlocal holder, original_tick
            phases.append(name)
            if name != 'running':
                return
            workers.extend(current_workers)
            self.assertEqual(len(workers), 4)
            self.assertEqual([w.status['ruleset'] for w in workers], [0, 0, 3, 3])
            for worker in workers:
                original_handler = worker.peer.handler
                def queued_rpc(method, *args, _worker=worker,
                               _handler=original_handler, **kwargs):
                    if method in ('api.chat_ack', 'api.chat_messages'):
                        with lock:
                            waiting.add(_worker.alias)
                            if len(waiting) == 4:
                                all_waiting.set()
                    return _handler(method, *args, **kwargs)
                worker.peer.handler = queued_rpc
            holder = threading.Thread(target=hold_actual_authorization_mutex,
                                      name='invented-api-mutex-delay', daemon=True)
            holder.start()
            self.assertTrue(mutex_held.wait(2))
            self.api.chat_messages = queued_poll
            original_tick = supervisor.tick
            started = time.monotonic()

            def tick():
                self.assertLess(time.monotonic() - started, 60, 'fixture did not resume')
                if all_waiting.is_set() and not release_mutex.is_set():
                    # All native main loops are inside their synchronous API
                    # RPC. Advance only observation age, without a 15s sleep.
                    with supervisor.status_lock:
                        for worker in workers:
                            worker.status_at = time.monotonic() - 20
                            self.assertIsNone(worker.process.poll())
                            self.assertFalse(worker.peer.closed.is_set())
                            self.assertFalse(worker.failure)
                            self.assertFalse(worker.fresh())
                            self.assertFalse(worker.idle_empty())
                        original_tick()
                        supervisor.write_status(force=True)
                        public = json.loads((supervisor.directory/'public-status.json').read_text())
                        registry = supervisor.registry.snapshot()['rooms']
                        for worker in workers:
                            row = public['rooms'][worker.alias]
                            self.assertEqual(row['state'], 'STALE')
                            self.assertFalse(row['healthy'])
                            self.assertIsNone(row['players'])
                            self.assertIsNone(row['current_map'])
                            self.assertFalse(row['queue_known'])
                            self.assertIsNone(worker.empty_since)
                            self.assertEqual(registry[worker.alias]['phase'], 'running')
                            self.assertFalse(registry[worker.alias]['ownership_unknown'])
                        self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'), 4)
                        self.assertNotIn('CloseRoom', self.backend.authenticated_calls)
                        phase['blocked_ticks'] += 1
                        # At least one whole service liveness pass runs with all
                        # observations stale before releasing the real mutex.
                        if phase['blocked_ticks'] >= 2:
                            release_mutex.set()
                    return
                original_tick()
                if release_mutex.is_set():
                    replies = [row for row in self.backend.api.messages
                               if reply_marker in str(row[1])]
                    if (len({row[0] for row in replies}) == 4
                            and all(worker.fresh() for worker in workers)):
                        self.assertTrue(all(worker.process.poll() is None for worker in workers))
                        phase['resumed'] = True
                        supervisor.stopping.set()
            supervisor.tick = tick

        try:
            run_provider_service(self.root, self.release, authentication=self.auth,
                                 api=self.api, hub=self.hub, observer=observer)
        finally:
            release_mutex.set()
            if holder is not None:
                holder.join(3)
                self.assertFalse(holder.is_alive())
        self.assertEqual(phases, ['running', 'stopped'])
        self.assertGreaterEqual(phase['blocked_ticks'], 2)
        self.assertTrue(phase['resumed'])
        self.assertEqual(len(delivered), 4)
        replies = [row for row in self.backend.api.messages if reply_marker in str(row[1])]
        self.assertEqual(len(replies), 4)
        self.assertEqual(len({row[0] for row in replies}), 4)
        self.assertFalse(self.backend.rooms)
        self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'), 4)
        self.assertTrue(all(worker.process.poll() is not None for worker in workers))


    def test_failed_candidate_activation_rolls_back_without_stopping_occupied_siblings(self):
        from rolling.release import build_manifest
        from poolrotater_host.layout import atomic_json
        bad=fixture_release(self.root,'v7.0.1')
        path=bad/'bot/bot.py'
        with path.open('a') as stream:
            stream.write("\ndef _fixture_constructor_failure(*args, **kwargs):\n    raise RuntimeError('invented candidate activation failure')\nLobbyBot.__init__ = _fixture_constructor_failure\n")
        manifest=build_manifest(bad,'v7.0.1',sys.executable)
        manifest['validation']={'tests':1,'offline_fixture':True}
        atomic_json(bad/'release.json',manifest)
        result={'recovered':False};phases=[]
        def observer(phase,supervisor,workers):
            phases.append(phase)
            if phase!='running':return
            original=list(workers);siblings=original[1:];root_pid=__import__('os').getpid()
            for index,w in enumerate(siblings):
                rid=w.status['room_id'];self.backend.rooms[rid]['players'].append({'user_id':7+index,'status':'idle'})
                self.backend.wire.route('UserJoined',{'room_id':rid,'user_id':7+index})
            native_wait=supervisor.wait_for
            def scheduled_recovery(predicate,seconds,worker=None):
                try:return native_wait(predicate,seconds,worker)
                except RuntimeError:
                    if worker is not None and worker.release==bad:time.sleep(1)
                    raise
            supervisor.wait_for=scheduled_recovery
            native_tick=supervisor.tick;requested=False;started=time.monotonic()
            def tick():
                nonlocal requested
                self.assertLess(time.monotonic()-started,90,'native rollback deadline')
                native_tick()
                if not requested and all(w.status.get('players')==1 for w in siblings):
                    supervisor.request(['room1'],bad);requested=True
                row=supervisor.registry.snapshot()['rooms']['room1']
                if requested and not supervisor.rollout_alias and 'previous release restored' in (row.get('error')or''):
                    current=supervisor.workers['room1']
                    self.assertIsNot(current,original[0]);self.assertEqual(current.release,self.release)
                    self.assertTrue(current.fresh());self.assertEqual(__import__('os').getpid(),root_pid)
                    self.assertTrue(all(supervisor.workers[w.alias]is w and w.process.poll()is None
                        and w.status['room_id']in self.backend.rooms for w in siblings))
                    result['recovered']=True;supervisor.request_shutdown()
            supervisor.tick=tick
        run_provider_service(self.root,self.release,authentication=self.auth,api=self.api,hub=self.hub,observer=observer)
        self.assertTrue(result['recovered']);self.assertEqual(phases,['running','stopped'])
        self.assertFalse(self.backend.rooms)


    def test_failed_worker_clean_close_parks_only_that_alias_and_preserves_occupied_siblings(self):
        phases=[];result={'isolated':False};injected=threading.Event()
        def observer(phase,supervisor,workers):
            phases.append(phase)
            if phase!='running':return
            original=list(workers);affected=original[0];siblings=original[1:]
            occupied=siblings[0];rid=occupied.status['room_id']
            self.backend.rooms[rid]['players'].append({'user_id':7,'status':'idle'})
            self.backend.wire.route('UserJoined',{'room_id':rid,'user_id':7})
            handler=affected.peer.handler
            def local_fault(method,*args,**kwargs):
                if method=='api.chat_messages' and not injected.is_set():
                    injected.set();raise ValueError('invented alias-local worker exception')
                return handler(method,*args,**kwargs)
            affected.peer.handler=local_fault
            native_tick=supervisor.tick;started=time.monotonic()
            def tick():
                self.assertLess(time.monotonic()-started,60,'alias cleanup did not settle')
                native_tick()
                row=supervisor.registry.snapshot()['rooms'][affected.alias]
                if row.get('phase')=='recovery_required':
                    raise AssertionError({'row':row,'failure':affected.failure,'exited':affected.exited,
                        'broker_uncertain':affected.broker.uncertain,'owned':list(affected.broker.hub.owned),
                        'process':affected.process.poll()})
                if row.get('phase')=='parked':
                    self.assertTrue(injected.is_set())
                    self.assertEqual(row['operation'],'closed');self.assertFalse(row['ownership_unknown'])
                    self.assertIsNotNone(affected.process.poll())
                    self.assertTrue(all(supervisor.workers[w.alias]is w and w.process.poll()is None
                        and w.status['room_id']in self.backend.rooms for w in siblings))
                    self.assertIn(7,{p['user_id']for p in self.backend.rooms[rid]['players']})
                    self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),4)
                    self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'),1)
                    result['isolated']=True;supervisor.request_shutdown()
            supervisor.tick=tick
        run_provider_service(self.root,self.release,authentication=self.auth,api=self.api,hub=self.hub,observer=observer)
        self.assertTrue(result['isolated']);self.assertEqual(phases,['running','stopped'])
        self.assertFalse(self.backend.rooms)


    def test_normal_shutdown_after_tick_parks_only_requested_alias_with_sibling_playing(self):
        from bot.process_control import SharedProcessControl
        phases=[];proof={'isolated':False}
        def observer(phase,supervisor,workers):
            phases.append(phase)
            if phase!='running':return
            affected,*siblings=list(workers)
            occupied=siblings[-1];rid=occupied.status['room_id']
            self.backend.rooms[rid]['players'].append({'user_id':7,'status':'idle'})
            self.backend.wire.route('UserJoined',{'room_id':rid,'user_id':7})
            native_tick=supervisor.tick;started=time.monotonic();requested=False;playing=False
            def tick():
                nonlocal requested,playing
                self.assertLess(time.monotonic()-started,60,'normal shutdown did not settle')
                native_tick()
                if not playing and occupied.status.get('players')==1:
                    self.backend.wire.route('MatchStarted',{'room_id':rid,
                        'playlist_item_id':self.backend.rooms[rid]['playlist'][0]['id']})
                    playing=True
                if not requested and occupied.status.get('state')=='PLAYING':
                    requested=True
                    SharedProcessControl(affected.broker.cfg).request_shutdown(affected.alias,requested_by='invented-operator')
                    self.assertTrue(affected.peer.closed.wait(20),'normal worker RPC did not disconnect')
                    self.assertFalse(affected.failure)
                    original_poll=affected.process.poll
                    def pending_exit():
                        affected.process.poll=original_poll
                        return None  # deterministic RPC-closed/process-not-yet-reaped window
                    affected.process.poll=pending_exit
                row=supervisor.registry.snapshot()['rooms'][affected.alias]
                if row.get('phase')=='parked':
                    self.assertTrue(requested)
                    self.assertEqual(row['operation'],'closed');self.assertFalse(row['ownership_unknown'])
                    self.assertIsNotNone(affected.process.poll())
                    self.assertTrue(all(supervisor.workers[w.alias]is w and w.process.poll()is None
                        and w.status['room_id']in self.backend.rooms for w in siblings))
                    self.assertEqual(occupied.status['state'],'PLAYING')
                    self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),4)
                    self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'),1)
                    proof['isolated']=True;supervisor.request_shutdown()
            supervisor.tick=tick
        run_provider_service(self.root,self.release,authentication=self.auth,api=self.api,hub=self.hub,observer=observer)
        self.assertTrue(proof['isolated']);self.assertEqual(phases,['running','stopped'])
        self.assertFalse(self.backend.rooms)


class TerminalServiceLivenessTests(unittest.TestCase):
    """Real child/socket terminal signals remain fail closed, regardless of age."""

    def exercise(self, cause, expected, *, grace=0):
        with tempfile.TemporaryDirectory(prefix='provider-liveness-') as temporary:
            root = Path(temporary)
            registry = Registry(root/'ownership.json')
            workers, remote_peers = {}, []
            for index in range(4):
                alias = 'room' + str(index + 1)
                process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
                parent, child = socket.socketpair()
                peer = Peer(parent, name=alias).start()
                remote_peers.append(Peer(child, name=alias+'-remote').start())
                broker = SimpleNamespace(generation=alias, fenced=False)
                worker = Worker(alias, root, {}, broker, process=process, peer=peer,
                                status_at=time.monotonic()-20)
                workers[alias] = worker
                registry.record(alias, phase='running', room_id=101+index,
                                operation='owned', ownership_unknown=False)
            supervisor = SimpleNamespace(configs=workers, workers=workers, handles=workers,
                                         registry=registry, stopping=threading.Event(),
                                         rollout_alias=None)
            supervisor.prepare = lambda alias, release: workers[alias]
            supervisor.start_release_for = lambda alias: root
            supervisor.activate = lambda worker: None
            supervisor.request_shutdown = lambda: supervisor.stopping.set()
            target = workers['room1']
            first_tick = True

            def tick():
                nonlocal first_tick
                if not first_tick:
                    if cause == 'persistent_connection':
                        return  # grace must expire without resetting on each pass
                    if cause == 'disconnect_then_unknown':
                        registry.record('room1',phase='recovery_required',operation='close_pending',ownership_unknown=True)
                        return
                    if cause in ('retirement','retirement_race','failure_exit_race',
                                 'normal_disconnect_race','normal_exit_race'):
                        if cause in ('failure_exit_race','normal_disconnect_race','normal_exit_race'):
                            self.assertTrue(all(w.process.poll()is None for a,w in workers.items()if a!='room1'))
                            if target.process.poll() is None:
                                target.process.terminate();target.process.wait(3)
                            registry.record('room1',phase='parked',operation='closed',room_id=None,ownership_unknown=False)
                        supervisor.stopping.set();return
                    self.fail('terminal event was ignored')
                first_tick = False
                if cause == 'death':
                    target.process.terminate()
                    target.process.wait(3)
                elif cause in ('connection','persistent_connection','disconnect_then_unknown'):
                    remote_peers[0].close()
                    self.assertTrue(target.peer.closed.wait(2))
                elif cause == 'failure':
                    target.failure = 'invented explicit worker exception'
                elif cause == 'failure_exit_race':
                    # Reproduce a worker failure + death after tick's last
                    # reconciliation, before the parent failure scan.
                    target.failure='invented local worker exception'
                    target.process.terminate();target.process.wait(3)
                    remote_peers[0].close();self.assertTrue(target.peer.closed.wait(2))
                elif cause in ('normal_disconnect_race','normal_exit_race'):
                    # A normal command shutdown has already closed its room,
                    # but RPC/process teardown occurs after native tick's poll.
                    self.assertFalse(target.failure)
                    registry.record('room1',phase='closing',operation='closed',room_id=None,ownership_unknown=False)
                    if cause == 'normal_exit_race':
                        target.process.terminate();target.process.wait(3)
                    remote_peers[0].close();self.assertTrue(target.peer.closed.wait(2))
                elif cause == 'ownership':
                    registry.record('room1', phase='recovery_required',
                                    operation='close_pending', ownership_unknown=True)
                elif cause == 'retirement_race':
                    original_poll = target.process.poll
                    def retire_after_fast_check():
                        supervisor.rollout_alias = 'room1'
                        target.process.poll = original_poll
                        target.broker.fenced = True
                        remote_peers[0].close()
                        self.assertTrue(target.peer.closed.wait(2))
                        return original_poll()
                    target.process.poll = retire_after_fast_check
                elif cause in ('retirement','retirement_sibling','retirement_recovery','fenced_without_roll'):
                    target.broker.fenced = True
                    if cause != 'fenced_without_roll':supervisor.rollout_alias = 'room1'
                    remote_peers[0].close()
                    self.assertTrue(target.peer.closed.wait(2))
                    self.assertIn(target.broker.generation,supervisor.handles)
                    registry.record('room1',phase='closing')
                    if cause == 'retirement_sibling':
                        remote_peers[1].close()
                        self.assertTrue(workers['room2'].peer.closed.wait(2))
                    elif cause == 'retirement_recovery':
                        registry.record('room1',phase='recovery_required',ownership_unknown=True)
                else:
                    raise AssertionError(cause)
            supervisor.prepare_startup_status = lambda:None
            supervisor.tick = tick
            observed = []

            @contextmanager
            def controller(*args, **kwargs):
                try:
                    yield supervisor
                finally:
                    for worker in workers.values():
                        worker.peer.close()
                        if worker.process.poll() is None:
                            worker.process.terminate()
                        worker.process.wait(3)
                    for peer in remote_peers:
                        peer.close()

            with patch('poolrotater_host.provider_service._authority'), \
                    patch('poolrotater_host.provider_service.WORKER_FAILURE_CLEANUP_SECONDS', grace), \
                    patch('poolrotater_host.provider_service.provider_host_controller', controller), \
                    patch('poolrotater_host.provider_usage.summary', return_value='invented count-only summary'):
                if expected is None:
                    run_provider_service(root, root, authentication=None, api=None, hub=None,
                                         observer=lambda name, *_: observed.append(name))
                else:
                    with self.assertRaises(ConfigError) as caught:
                        run_provider_service(root, root, authentication=None, api=None, hub=None,
                                             observer=lambda name, *_: observed.append(name))
                    self.assertEqual(str(caught.exception), expected)
                    self.assertEqual(failure_details(caught.exception), {'failure_code': expected})
            self.assertEqual(observed, ['running', 'stopped' if expected is None else 'failed'])
            self.assertTrue(all(worker.process.poll() is not None for worker in workers.values()))
            if cause == 'ownership':
                row = registry.snapshot()['rooms']['room1']
                self.assertEqual(row['operation'], 'close_pending')
                self.assertTrue(row['ownership_unknown'])
                self.assertEqual(row['room_id'], 101)

    def test_failed_worker_exit_after_tick_waits_for_next_native_reconciliation(self):
        self.exercise('failure_exit_race',None,grace=10)

    def test_normal_worker_disconnect_after_tick_preserves_siblings_until_reconciliation(self):
        self.exercise('normal_disconnect_race',None,grace=10)

    def test_normal_worker_exit_after_tick_preserves_siblings_until_reconciliation(self):
        self.exercise('normal_exit_race',None,grace=10)

    def test_unreconciled_disconnect_still_expires_without_worker_failure(self):
        self.exercise('persistent_connection','provider_worker_connection_closed',grace=.6)

    def test_unknown_ownership_is_not_hidden_by_disconnect_grace(self):
        self.exercise('disconnect_then_unknown','provider_worker_ownership_recovery_required',grace=10)

    def test_actual_process_exit_remains_terminal(self):
        self.exercise('death', 'provider_worker_exited')

    def test_actual_rpc_disconnect_remains_terminal(self):
        self.exercise('connection', 'provider_worker_connection_closed')

    def test_explicit_worker_failure_remains_terminal(self):
        self.exercise('failure', 'provider_worker_failed')

    def test_unknown_close_intent_remains_terminal_and_is_retained(self):
        self.exercise('ownership', 'provider_worker_ownership_recovery_required')

    def test_retiring_fenced_worker_rpc_closes_before_handle_removal(self):
        self.exercise('retirement',None)

    def test_retiring_alias_does_not_hide_sibling_disconnect(self):
        self.exercise('retirement_sibling','provider_worker_connection_closed')

    def test_retiring_alias_does_not_hide_unknown_ownership(self):
        self.exercise('retirement_recovery','provider_worker_ownership_recovery_required')

    def test_fencing_outside_roll_does_not_hide_disconnect(self):
        self.exercise('fenced_without_roll','provider_worker_connection_closed')

    def test_retirement_begins_between_fast_check_and_terminal_signal(self):
        self.exercise('retirement_race',None)
