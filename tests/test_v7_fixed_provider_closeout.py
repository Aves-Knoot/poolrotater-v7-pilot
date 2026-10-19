"""Dormant fixed-route continuity; all identities and I/O are invented.

Ordinary tests replace the protected registration reader explicitly. They do not
claim immutable installation qualification; the real UID/permission fixture and
donor checks supply that evidence. Independent wire fences and explicit I/O mocks
prevent provider traffic while dormant caller continuations are inspected.
"""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from poolrotater_host.authorization import OperatorAuthorizationSession
from poolrotater_host.installed_authorization import (
    _runtime_guard, configure_installed_provider_client,
    installed_provider_authentication, installed_provider_state,
)
from poolrotater_host.installed_operator_workflow import run
from poolrotater_host.layout import atomic_json, read_json, setup, owner_lease, file_lease
from poolrotater_host.oauth import AuthError, ClientCredentials, OfflineOAuth, diagnostic_lease
from poolrotater_host.operator_client import OperatorClient
from poolrotater_host.provider_preflight import FixedProviderTransport, ProviderConfiguration
from poolrotater_host.provider_rooms import ProviderRoomApi, ProviderRoomHub
from poolrotater_host.provider_transport import TrustSnapshot
from poolrotater_host.referee_wire import ChildSession
from poolrotater_host.testing.installed_authorization import (
    configure_installed_provider_fixture_client, installed_provider_fixture_authentication,
)
from poolrotater_host.testing.provider_https import certificates

SECRET = 'invented-client-secret'
PROFILE = 'secrets/provider-operator-client.json'


class FixedProviderCloseoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs = tempfile.TemporaryDirectory(prefix='fixed-closeout-ca-')
        certificates(cls.certs.name)
        cls.trust = TrustSnapshot((Path(cls.certs.name)/'ca.crt').read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls.certs.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='fixed-closeout-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'host'
        setup(self.root)
        self.release = self.root/'releases/invented'
        self.profile = OperatorClient(ClientCredentials(123, SECRET), 99,
                                     'http://127.0.0.1:51111/oauth/callback')
        self.enterContext(patch.dict(os.environ, {'POOLROTATER_PROVISIONING_SANDBOX':'1'}))
        self.registration = self.enterContext(patch(
            'poolrotater_host.installed_authorization.load_provisioned', return_value={}))

    def fixed(self):
        configure_installed_provider_client(self.root, self.release, self.profile)
        return installed_provider_authentication(self.root, self.release, self.trust,
                                                  initialize=True)

    def snapshot(self):
        return {str(path.relative_to(self.root)):path.read_bytes()
                for folder in ('secrets', 'state')
                for path in (self.root/folder).rglob('*.json')}

    def error(self, code, action):
        with self.assertRaises(AuthError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_fixed_authority_continuation_selects_fixed_target_without_fixture_backend(self):
        from poolrotater_host.provider_referee import referee_port
        auth = self.fixed()
        before = self.snapshot()
        # Only caller-level permission checks are replaced. The fixed wire's
        # independent release fence remains active throughout this test.
        with patch('poolrotater_host.provider_authorization.require_live_provider'), \
             patch('poolrotater_host.provider_rooms.require_live_provider'), \
             patch.dict('sys.modules', {'poolrotater_host.testing.room_service':None}), \
             patch('multiprocessing.get_context') as processes, \
             patch('socket.create_connection') as sockets:
            auth.require_authorization_available()
            self.assertIsNone(referee_port(auth))
            api, hub = ProviderRoomApi(auth), ProviderRoomHub(auth)
            self.assertIs(api.auth,auth)
            self.assertIs(hub.auth,auth)
            self.assertIsNone(api.backend)
            self.assertIsNone(hub.backend)
            self.error('live_provider_disabled',lambda:ChildSession(
                referee_port(auth),self.trust,'invented-bearer'))
            processes.assert_not_called()
            sockets.assert_not_called()
        self.assertEqual(self.snapshot(),before)

    def test_fixed_adapters_reject_supplied_backend_without_touching_it(self):
        auth = self.fixed()
        class UnavailableBackend:
            def __getattribute__(self,name):
                raise AssertionError('fixed adapter inspected a fixture backend')
        with patch('poolrotater_host.provider_authorization.require_live_provider'), \
             patch('poolrotater_host.provider_rooms.require_live_provider'):
            for constructor in (ProviderRoomApi,ProviderRoomHub):
                with self.subTest(constructor=constructor.__name__):
                    self.error('fixed_provider_backend_required',lambda:constructor(auth,UnavailableBackend()))

    def test_fixed_release_refuses_before_listener_child_socket_or_authority_mutation(self):
        from poolrotater_host.provider_referee import referee_port
        auth = self.fixed()
        before = self.snapshot()
        with patch('poolrotater_host.oauth_callback.HTTPServer') as listener, \
             patch('multiprocessing.get_context') as processes, \
             patch('socket.create_connection') as sockets:
            for action in (auth.require_authorization_available, lambda:referee_port(auth),
                           lambda:ProviderRoomApi(auth), lambda:ProviderRoomHub(auth),
                           lambda:auth.begin(self.profile.redirect_uri),
                           lambda:OperatorAuthorizationSession(auth,self.profile.redirect_uri).__enter__()):
                self.error('live_provider_disabled',action)
            listener.assert_not_called()
            processes.assert_not_called()
            sockets.assert_not_called()
        self.assertEqual(self.snapshot(),before)

    def test_fixed_branch_continuation_refuses_foreign_gate_profile_before_wire(self):
        from poolrotater_host.provider_referee import referee_port
        auth = self.fixed()
        auth.transport.gate.binding['client_profile_sha256'] = '0'*64
        before = self.snapshot()
        with patch('poolrotater_host.provider_authorization.require_live_provider'), \
             patch('multiprocessing.get_context') as processes:
            self.error('qualified_live_gate_required',lambda:referee_port(auth))
            processes.assert_not_called()
        self.assertEqual(self.snapshot(),before)

    def test_prepared_local_configure_and_status_do_not_need_fixture_environment(self):
        with patch.dict(os.environ,{'POOLROTATER_OFFLINE_SANDBOX':'',
                                  'POOLROTATER_PROVISIONING_SANDBOX':''}):
            configured = run(self.root,self.release,'configure',
                             credentials_stream=io.StringIO(json.dumps({
                                 'client_id':123,'client_secret':SECRET})),
                             expected_user_id=99,redirect_uri=self.profile.redirect_uri)
            before = self.snapshot()
            status = run(self.root,self.release,'status')
            self.assertEqual(status['execution'],'provider-disabled')
            self.assertFalse(status['live_ready'])
            self.assertFalse(status['start_allowed'])
            self.assertEqual(status['workers_started'],0)
            self.assertEqual(self.snapshot(),before)
            self.assertEqual(configured['provider_pacing']['counters'],status['provider_pacing']['counters'])
            self.assertTrue(self.registration.call_args_list)
            self.assertTrue(all(call.kwargs.get('prepared') is True
                                for call in self.registration.call_args_list))
            self.assertEqual((self.root/PROFILE).stat().st_mode & 0o777,0o600)

    def test_prepared_guard_failure_precedes_private_input_even_without_fixture_flags(self):
        class UnreadableInput:
            def read(self,*args):raise AssertionError('unprotected runtime consumed credentials')
        with patch.dict(os.environ,{'POOLROTATER_OFFLINE_SANDBOX':'',
                                  'POOLROTATER_PROVISIONING_SANDBOX':''}), \
             patch('poolrotater_host.installed_authorization.load_provisioned',side_effect=OSError('invented')):
            self.error('installed_runtime_binding_requires_review',lambda:run(
                self.root,self.release,'configure',credentials_stream=UnreadableInput(),
                expected_user_id=99,redirect_uri=self.profile.redirect_uri))
        self.assertFalse((self.root/PROFILE).exists())

    def test_active_installed_status_reads_without_owner_admission_or_state_changes(self):
        from poolrotater_host.config import ConfigError
        auth = self.fixed()
        before = self.snapshot()
        with owner_lease(self.root, account_id=99), \
             patch('socket.create_connection') as sockets:
            # Holding the exact lifetime lease reproduces the installed failure.
            result = run(self.root, self.release, 'status')
            self.assertTrue(result['ok'])
            self.assertFalse(result['authority_state_modified'])
            self.assertFalse(result['start_allowed'])
            self.assertFalse(result['actual_provider_dispatch'])
            sockets.assert_not_called()
            for mutation in ('cancel', 'pacing-recover'):
                arguments = {} if mutation == 'cancel' else {'pacing_state_sha256':'0'*64}
                with self.subTest(mutation=mutation), self.assertRaises(ConfigError):
                    run(self.root, self.release, mutation, **arguments)
            with self.assertRaises(ConfigError):
                installed_provider_state(self.root, self.release)
        self.assertEqual(before, self.snapshot())

    def test_active_diagnostic_serializes_against_token_refresh_without_exposing_values(self):
        auth = self.fixed()
        atomic_json(auth.token_path, {**auth.binding, 'access_token':'invented-access-secret',
            'refresh_token':'invented-refresh-secret', 'expires_at':9999999999,
            'user_id':99, 'phase':'refresh_pending', 'attempt':'invented-attempt'})
        result, errors = [], []
        entered = threading.Event()
        def read():
            entered.set()
            try: result.append(run(self.root, self.release, 'status'))
            except Exception as exc: errors.append(exc)
        with owner_lease(self.root, account_id=99):
            with file_lease(auth.lock_path):
                thread = threading.Thread(target=read)
                thread.start()
                self.assertTrue(entered.wait(1))
                record = read_json(auth.token_path)
                record['phase'] = 'ready'
                atomic_json(auth.token_path, record)
            thread.join(6)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]['token_phase'], 'ready')
        encoded = json.dumps(result)
        self.assertNotIn('invented-access-secret', encoded)
        self.assertNotIn('invented-refresh-secret', encoded)

    def test_diagnostic_missing_private_lock_or_ledger_never_initializes(self):
        auth = self.fixed()
        for name in ('secrets/provider-oauth.lock',
                     'state/provider-authority-pacing.lock',
                     'state/provider-authority-pacing.json',
                     'state/provider-authority-pacing-anchor.json'):
            path = self.root/name
            saved = path.read_bytes()
            path.unlink()
            before = self.snapshot()
            with self.subTest(name=name), self.assertRaises(AuthError):
                run(self.root, self.release, 'status')
            self.assertFalse(path.exists())
            self.assertEqual(before, self.snapshot())
            path.write_bytes(saved)
            path.chmod(0o600)

    def test_diagnostic_refuses_symlink_hardlink_and_nonprivate_lock(self):
        auth = self.fixed()
        saved = auth.lock_path.read_bytes()
        for case in ('symlink', 'hardlink', 'mode'):
            auth.lock_path.unlink()
            other = self.root/'secrets/invented-other-lock'
            other.write_bytes(saved)
            other.chmod(0o600)
            if case == 'symlink': auth.lock_path.symlink_to(other)
            elif case == 'hardlink': os.link(other, auth.lock_path)
            else:
                auth.lock_path.write_bytes(saved)
                auth.lock_path.chmod(0o644)
            with self.subTest(case=case):
                if case == 'symlink':
                    from poolrotater_host.config import ConfigError
                    with self.assertRaises(ConfigError): run(self.root,self.release,'status')
                else:
                    self.error('diagnostic_lock_requires_review',lambda:run(self.root,self.release,'status'))
            auth.lock_path.unlink()
            other.unlink()
            auth.lock_path.write_bytes(saved)
            auth.lock_path.chmod(0o600)

    def test_diagnostic_revalidates_runtime_and_profile_after_snapshot(self):
        auth = self.fixed()
        from poolrotater_host.config import ConfigError
        calls = 0
        def registration(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls >= 7: raise ConfigError('invented-private-detail')
            return {}
        with patch('poolrotater_host.installed_authorization.load_provisioned',side_effect=registration):
            self.error('installed_runtime_binding_requires_review',lambda:run(self.root,self.release,'status'))
        def changed(*args, **kwargs):
            record = read_json(self.root/PROFILE)
            record['client_secret'] = 'different-invented-secret'
            atomic_json(self.root/PROFILE, record)
            return {}
        from poolrotater_host.provider_pacing import ProviderRequestGate
        with patch.object(ProviderRequestGate,'diagnostic_snapshot',side_effect=changed):
            self.error('operator_client_changed',lambda:run(self.root,self.release,'status'))

    def test_shared_diagnostic_lock_has_bounded_busy_refusal(self):
        auth = self.fixed()
        with file_lease(auth.lock_path):
            self.error('diagnostic_snapshot_busy',lambda:diagnostic_lease(auth.lock_path,timeout=.03).__enter__())

    def test_active_diagnostic_waits_for_one_complete_pacing_update(self):
        from poolrotater_host.provider_pacing import ProviderRequestGate
        self.fixed()
        result, errors = [], []
        entered = threading.Event()
        original = ProviderRequestGate.diagnostic_snapshot
        def observe(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)
        def read():
            try: result.append(run(self.root,self.release,'status'))
            except Exception as exc: errors.append(exc)
        with owner_lease(self.root, account_id=99), \
             patch.object(ProviderRequestGate,'diagnostic_snapshot',side_effect=observe):
            with file_lease(self.root/ProviderRequestGate.lock_path):
                thread = threading.Thread(target=read)
                thread.start()
                self.assertTrue(entered.wait(2))
                self.assertEqual(result, [])
                state = read_json(self.root/ProviderRequestGate.state_path)
                state['counters'].update(attempted=3,dispatched=2,replies=2)
                atomic_json(self.root/ProviderRequestGate.state_path, state)
            thread.join(6)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]['provider_pacing']['counters']['attempted'],3)
        self.assertEqual(result[0]['provider_pacing']['counters']['dispatched'],2)
        self.assertEqual(result[0]['provider_pacing']['counters']['replies'],2)

    def test_diagnostic_refuses_lock_replacement_during_snapshot(self):
        auth = self.fixed()
        def replaced():
            with diagnostic_lease(auth.lock_path):
                auth.lock_path.unlink()
                auth.lock_path.write_bytes(b'')
                auth.lock_path.chmod(0o600)
        self.error('diagnostic_lock_changed', replaced)

    def test_prepared_local_entry_does_not_adopt_an_invented_profile(self):
        configure_installed_provider_fixture_client(self.root,self.release,self.profile)
        before = self.snapshot()
        with patch.dict(os.environ,{'POOLROTATER_OFFLINE_SANDBOX':'',
                                  'POOLROTATER_PROVISIONING_SANDBOX':''}):
            self.error('operator_client_binding_requires_review',lambda:run(self.root,self.release,'status'))
        self.assertEqual(self.snapshot(),before)

    def test_prepared_profile_change_remains_fenced_without_fixture_flags(self):
        self.fixed()
        profile = read_json(self.root/PROFILE)
        profile['client_secret'] = 'different-invented-secret'
        atomic_json(self.root/PROFILE,profile)
        before = self.snapshot()
        with patch.dict(os.environ,{'POOLROTATER_OFFLINE_SANDBOX':'',
                                  'POOLROTATER_PROVISIONING_SANDBOX':''}):
            self.error('provider_pacing_binding_mismatch',lambda:run(self.root,self.release,'status'))
        self.assertEqual(self.snapshot(),before)

    def test_original_fixture_guard_still_refuses_without_fixture_environment(self):
        with patch.dict(os.environ,{'POOLROTATER_OFFLINE_SANDBOX':'',
                                  'POOLROTATER_PROVISIONING_SANDBOX':''}):
            self.error('installed_offline_fixture_required',lambda:_runtime_guard(self.root,self.release))
            self.error('installed_offline_fixture_required',lambda:configure_installed_provider_fixture_client(
                self.root,self.release,self.profile))
            self.error('installed_offline_fixture_required',lambda:installed_provider_fixture_authentication(
                self.root,self.release,'https://127.0.0.1:51112',self.trust))
        self.assertFalse((self.root/PROFILE).exists())

    def test_fixed_profile_cannot_become_original_offline_oauth_authority(self):
        auth = self.fixed()
        before = self.snapshot()
        with patch.dict(os.environ,{'POOLROTATER_OFFLINE_SANDBOX':'',
                                  'POOLROTATER_PROVISIONING_SANDBOX':''}):
            self.error('offline_fixture_required',lambda:OfflineOAuth(
                self.root,self.profile.credentials,auth.transport))
        self.assertEqual(self.snapshot(),before)

    def test_slower_fixed_admission_retains_persistent_spacing_when_reconstructed(self):
        auth = self.fixed()
        auth.transport.gate.interval = 2.4
        transport = FixedProviderTransport(self.trust,ProviderConfiguration(),gate=auth.transport.gate)
        with patch('poolrotater_host.provider_preflight.require_live_provider'), \
             patch('poolrotater_host.provider_transport._owned_exchange') as wire:
            with transport.operation('GET','/api/v2/me',bearer='invented-bearer'):
                pass
            wire.assert_not_called()
        paced = read_json(self.root/auth.transport.gate.state_path)
        self.assertEqual(paced['phase'],'idle')
        self.assertEqual(paced['interval'],2.4)
        self.assertEqual(paced['counters']['attempted'],1)
        self.assertEqual(paced['counters']['dispatched'],1)
        self.assertEqual(paced['counters']['replies'],0)
        before = self.snapshot()
        reconstructed = installed_provider_state(self.root,self.release)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(reconstructed.pacing_snapshot()['counters'],paced['counters'])

    def test_fixed_transport_refuses_pacing_below_configured_minimum(self):
        auth = self.fixed()
        auth.transport.gate.interval = .6
        before = self.snapshot()
        self.error('invalid_provider_pacing',lambda:FixedProviderTransport(
            self.trust,ProviderConfiguration(),gate=auth.transport.gate))
        self.assertEqual(self.snapshot(),before)

    def test_operator_provider_actions_refuse_before_ca_discovery_or_private_notice(self):
        self.fixed()
        before = self.snapshot()
        with patch('poolrotater_host.provider_transport.TrustSnapshot.system') as trust, \
             patch('poolrotater_host.installed_operator_workflow.execute') as action:
            for requested in ('authorize','recover'):
                with self.subTest(action=requested):
                    self.error('live_provider_disabled',lambda:run(self.root,self.release,requested))
            trust.assert_not_called()
            action.assert_not_called()
        self.assertEqual(self.snapshot(),before)

    def test_dormant_operator_continuation_reconstructs_full_fixed_trust_adapter(self):
        self.fixed()
        before = self.snapshot()
        with patch('poolrotater_host.provider_contract.require_live_provider'), \
             patch('poolrotater_host.installed_operator_workflow.execute',return_value={'prepared':True}) as action, \
             patch('multiprocessing.get_context') as processes, \
             patch('socket.create_connection') as sockets:
            self.assertEqual(run(self.root,self.release,'authorize',trust=self.trust),{'prepared':True})
            authority = action.call_args.args[0]
            self.assertIs(type(authority.transport),FixedProviderTransport)
            self.assertIs(authority.transport.trust,self.trust)
            self.assertEqual(authority.binding['execution'],'provider-disabled')
            processes.assert_not_called()
            sockets.assert_not_called()
        self.assertEqual(self.snapshot(),before)

    def test_fixed_service_release_fence_precedes_factories_trust_and_native_start(self):
        from poolrotater_host.provider_service import run_fixed_provider_service
        before = self.snapshot()
        with patch('poolrotater_host.installed_authorization.installed_provider_authentication') as authority, \
             patch('poolrotater_host.provider_transport.TrustSnapshot.system') as trust, \
             patch('multiprocessing.get_context') as processes, \
             patch('socket.create_connection') as sockets:
            self.error('live_provider_disabled',lambda:run_fixed_provider_service(self.root,self.release))
            authority.assert_not_called()
            trust.assert_not_called()
            processes.assert_not_called()
            sockets.assert_not_called()
        self.assertEqual(self.snapshot(),before)

    def test_bootstrap_fixed_service_refuses_before_venv_exec_or_service_factory(self):
        from poolrotater_host import service_bootstrap
        with patch('sys.argv',['bootstrap.py','--root',str(self.root),'--provider-service']), \
             patch.object(service_bootstrap,'_protected',side_effect=lambda path:Path(path)), \
             patch.object(service_bootstrap,'_json',return_value={'format':1,'sha256':'invented-hash'}), \
             patch.object(service_bootstrap,'_hash',return_value='invented-hash'), \
             patch.object(service_bootstrap,'preflight',return_value={
                 'environment':Path('/invented-environment'),'release':self.release}), \
             patch('os.execve') as execute, \
             patch('poolrotater_host.provider_service.run_fixed_provider_service') as service:
            self.error('live_provider_disabled',service_bootstrap.main)
            execute.assert_not_called()
            service.assert_not_called()

    def test_shared_service_ticks_replacement_publication_and_reaps_after_health_failure(self):
        from poolrotater_host import provider_service
        from poolrotater_host.config import ConfigError
        from rolling.supervisor import RollingSupervisor
        events = []
        class Worker:
            def __init__(self,pid):
                self.healthy,self.exit_code = True,None
                self.failure = ''
                self.peer = SimpleNamespace(closed=threading.Event())
                self.process = SimpleNamespace(pid=pid,poll=lambda:self.exit_code)
                self.status = {'ready':True,'running':True,'room_id':101}
                self.empty_since = None
                self.broker = SimpleNamespace(
                    hub=SimpleNamespace(owned={101}),pending_public=True,generation=pid)
                self.broker.publish = self.publish
            def publish(self):
                self.broker.pending_public = False
                events.append(('published',self.process.pid))
            def fresh(self):return self.healthy
            def idle_empty(self):return False
        initial,replacement = Worker(111),Worker(222)
        supervisor = SimpleNamespace(configs={'room1':{}},manifest={'version':'invented'},
            workers={},handles={},stopping=SimpleNamespace(is_set=lambda:False),
            config_loader=lambda:events.append(('config_refresh',)),
            reload_deployment_settings=lambda:None,rollout_alias=None,targets={},
            schema_baseline='invented',registry=SimpleNamespace(snapshot=lambda:{
                'rooms':{'room1':{'phase':'running'}}}),
            _requests=lambda:None,_discord_commands=lambda:None,
            write_status=lambda:events.append(('status',replacement.broker.pending_public)))
        supervisor.prepare_startup_status = lambda:None
        supervisor.start_release_for = lambda alias:self.release
        supervisor._protected_requests = lambda:None
        supervisor.request_shutdown = lambda:events.append(('shutdown_requested',))
        supervisor.prepare = lambda alias,release:initial
        def activate(worker):
            supervisor.workers['room1'] = worker
            supervisor.handles[worker.broker.generation] = worker
        supervisor.activate = activate
        iterations = []
        def tick():
            iterations.append(True)
            if len(iterations) == 1:
                # A restart has replaced the original process with a ready room
                # whose public setting is held until the supervisor's tick.
                initial.healthy,initial.exit_code = False,0
                supervisor.workers['room1'] = replacement
                supervisor.handles = {replacement.broker.generation:replacement}
            else:
                replacement.healthy = False
                replacement.peer.closed.set()
            RollingSupervisor.tick(supervisor)
        supervisor.tick = tick
        @contextmanager
        def controller(*args,**kwargs):
            try:yield supervisor
            finally:
                for worker in (initial,replacement):worker.exit_code = 0
                events.append(('controller_cleanup',))
        def observe(phase,supervisor,workers):
            events.append(('observed',phase,tuple(worker.process.pid for worker in workers)))
        # The bounded event replaces only the sleep/signal timing. The actual
        # supervisor tick publishes the room, and the shared service must inspect
        # the current active worker rather than its retired startup process.
        bounded = SimpleNamespace(is_set=lambda:False,wait=lambda seconds:len(iterations)>=2)
        with patch.object(provider_service,'_authority'), \
             patch.object(provider_service,'WORKER_FAILURE_CLEANUP_SECONDS',0), \
             patch.object(provider_service,'provider_host_controller',controller), \
             patch.object(provider_service.threading,'Event',return_value=bounded), \
             patch.object(provider_service.signal,'signal',return_value=None):
            with self.assertRaisesRegex(ConfigError,'provider_worker_connection_closed'):
                provider_service.run_provider_service(self.root,self.release,
                    authentication=SimpleNamespace(execution='provider-fixture'),
                    api=object(),hub=object(),observer=observe)
        self.assertEqual(len(iterations),2)
        self.assertIn(('published',222),events)
        self.assertIn(('status',False),events)
        self.assertIn(('config_refresh',),events)
        self.assertLess(events.index(('published',222)),events.index(('controller_cleanup',)))
        self.assertIn(('observed','failed',(111,222)),events)
        self.assertEqual(initial.process.poll(),0)
        self.assertEqual(replacement.process.poll(),0)


if __name__ == '__main__':
    unittest.main()
