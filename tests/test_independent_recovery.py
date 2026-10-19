"""Real hub/durable agent exchanges with bot and network processes absent."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from poolrotater_host.layout import atomic_json, read_json
from poolrotater_host.network.contracts import NetworkError, secret, fingerprint
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.store import Store
from poolrotater_host.rescue.agent import Agent
from poolrotater_host.rescue.repository import Repository
from poolrotater_host.rescue import operations


class IndependentRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.now = 1000.0
        initialize(self.root / 'hub')
        self.store = Store(self.root / 'hub', clock=lambda: self.now)
        self.identity = {k: str(uuid.uuid4()) for k in ('operator_id', 'installation_id', 'generation')}
        self.identity.update(account_id=7, directory=True)
        invitation = self.store.invite(self.identity, label='Invented operator')
        self.token = secret()
        enrolled = self.store.enroll({'protocol_version': 1, 'request_id': str(uuid.uuid4()),
            **self.identity, 'invitation': invitation['invitation'], 'credential': self.token})
        self.host = enrolled['host_id']
        self.binding = {k: self.identity[k] for k in ('installation_id', 'generation')}
        self.binding['host_id'] = self.host
        self.repository = Repository(self.store)
        self.directory = self.root / 'local'
        self.directory.mkdir(mode=0o700)
        self.observation = {'phase': 'held', 'operation_id': str(uuid.uuid4()), 'candidate_sha256': 'a'*64,
            'job_sha256': 'b'*64, 'failure_stage': 'network_restore',
            'failure_code': 'update_local_io_failed', 'service_state': 'failed', 'startup_attempted': True}
        self.backend = Mock()
        self.backend.observe.side_effect = lambda: deepcopy(self.observation)
        self.agent = Agent(self.directory, self.binding, self.backend,
                           lambda body: self.repository.accept(self.token, body), clock=lambda: self.now)

    def step(self):
        self.now += 30
        return self.agent.step()

    def queue(self, action='complete-update'):
        return self.repository.queue(self.host, action,
            expected_job_sha256=self.observation['job_sha256'])

    def test_remote_inspect_and_completion_with_no_bot_or_network_process(self):
        self.assertEqual(self.step()['phase'], 'connected')
        command = self.queue()
        self.assertEqual(self.step()['phase'], 'complete')
        self.backend.perform.assert_called_once_with(command)
        self.assertEqual(self.step()['phase'], 'connected')
        reported = self.repository.local(self.host)
        self.assertEqual(reported['requests'][0]['receipt']['result'], 'complete')
        self.assertEqual(reported['report']['observation']['failure_stage'], 'network_restore')
        self.assertNotIn('recovery', self.store.directory())

    def test_lost_reply_is_replayed_and_action_dispatched_once(self):
        self.step()
        command = self.queue('inspect')
        sent = []
        def lose(body):
            sent.append(deepcopy(body))
            reply = self.repository.accept(self.token, body)
            if len(sent) == 1:
                raise TimeoutError('invented lost response')
            return reply
        self.agent.transport = lose
        self.assertFalse(self.step()['ok'])
        self.backend.perform.assert_not_called()
        self.assertTrue(self.step()['ok'])
        self.assertEqual(sent[0], sent[1])
        self.backend.perform.assert_called_once_with(command)
        self.step()
        self.backend.perform.assert_called_once()

    def test_crash_after_dispatch_does_not_run_same_request_twice(self):
        self.step()
        self.queue()
        self.backend.perform.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.step()
        self.backend.perform.side_effect = None
        self.assertEqual(self.step()['phase'], 'connected')
        self.assertEqual(self.backend.perform.call_count, 1)
        saved = self.repository.local(self.host)['requests'][0]['receipt']
        self.assertEqual(saved, {'id': saved['id'], 'result': 'uncertain', 'error': 'rescue_outcome_uncertain'})

    def test_raw_error_never_leaves_local_coordinator(self):
        self.step()
        self.queue()
        self.backend.perform.side_effect = RuntimeError('Bearer invented-secret https://private/path')
        self.assertEqual(self.step()['error'], 'rescue_update_requires_review')
        self.step()
        raw = str(self.repository.local(self.host))
        self.assertNotIn('invented-secret', raw)
        self.assertNotIn('https://private', raw)

    def test_stale_or_changed_operation_cannot_be_queued(self):
        self.step()
        with self.assertRaises(NetworkError):
            self.repository.queue(self.host, 'complete-update', expected_job_sha256='c'*64)
        self.now += 181
        with self.assertRaises(NetworkError):
            self.queue()
        self.backend.perform.assert_not_called()

    def test_cross_host_revoked_or_wrong_generation_cannot_exchange(self):
        self.step()
        value = self.repository.local(self.host)['report']
        for field in ('installation_id', 'generation'):
            bad = deepcopy(value)
            bad[field] = str(uuid.uuid4())
            with self.subTest(field=field), self.assertRaises(NetworkError):
                self.repository.accept(self.token, bad)
        self.store.revoke(self.host)
        self.assertFalse(self.step()['ok'])
        self.backend.perform.assert_not_called()

    def test_replay_equivocation_and_unknown_receipt_refused(self):
        self.step()
        value = self.repository.local(self.host)['report']
        self.repository.accept(self.token, value)
        bad = deepcopy(value)
        bad['observation']['service_state'] = 'active'
        with self.assertRaises(NetworkError):
            self.repository.accept(self.token, bad)
        value.update(sequence=value['sequence']+1, sent_at=self.now)
        value['receipt'] = {'id': str(uuid.uuid4()), 'result': 'complete', 'error': None}
        self.now += 10
        with self.assertRaises(NetworkError):
            self.repository.accept(self.token, value)

    def test_arbitrary_action_private_fields_and_shell_payload_refused(self):
        self.step()
        with self.assertRaises(NetworkError):
            self.queue('shell')
        value = self.repository.local(self.host)['report']
        for name in ('token', 'path', 'players', 'command'):
            bad = deepcopy(value)
            bad['observation'][name] = 'secret'
            with self.subTest(name=name), self.assertRaises(NetworkError):
                self.repository.accept(self.token, bad)

    def test_expired_hub_response_cannot_invoke_backend(self):
        self.step()
        command = self.queue()
        self.now = command['expires_at'] + 1
        self.agent.transport = lambda body: {'ok': True, 'sequence': body['sequence'],
            'received_at': self.now, 'request': command}
        self.assertEqual(self.step()['error'], 'rescue_request_expired')
        self.backend.perform.assert_not_called()

    def test_cross_host_hub_command_rejected_locally(self):
        self.step()
        command = self.queue()
        command['host_id'] = str(uuid.uuid4())
        self.agent.transport = lambda body: {'ok': True, 'sequence': body['sequence'],
            'received_at': self.now, 'request': command}
        self.assertEqual(self.step()['error'], 'rescue_binding_changed')
        self.backend.perform.assert_not_called()

    def test_outage_longer_than_envelope_freshness_keeps_recovery_receipt(self):
        self.step()
        self.queue()
        self.step()
        receipt = read_json(self.directory/'state.json')['receipt']
        self.agent.transport = Mock(side_effect=TimeoutError)
        self.step()
        previous = read_json(self.directory/'state.json')['pending']['sequence']
        self.now += 301
        self.agent.transport = lambda body: self.repository.accept(self.token, body)
        self.step()
        self.assertGreater(self.repository.local(self.host)['report']['sequence'], previous)
        self.assertEqual(self.repository.local(self.host)['requests'][0]['receipt'], receipt)

    def test_non_update_crash_can_request_only_exact_failed_service_proof(self):
        self.observation.update(phase='service_failed',operation_id=None)
        self.step()
        command=self.queue('start-failed-service')
        self.assertEqual(self.step()['phase'],'complete')
        self.backend.perform.assert_called_once_with(command)
        self.step()
        self.assertEqual(self.repository.local(self.host)['requests'][0]['receipt']['result'],'complete')

    def test_failed_service_request_cannot_bypass_pending_update(self):
        self.step()
        with self.assertRaises(NetworkError):self.queue('start-failed-service')
        self.backend.perform.assert_not_called()


class LocalRecoveryGuards(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = {'prefix': self.root, 'root': self.root/'host'}
        self.pinned = {'installation_id': str(uuid.uuid4()), 'generation': str(uuid.uuid4()),
            'prefix': str(self.root), 'host_root': str(self.root/'host'),
            'service_uid': 1000, 'execution': 'pilot-live'}
        (self.root/'recovery').mkdir()
        self.enterContext(patch.object(operations, 'target_for', return_value=self.target))
        self.backend = operations.Backend('invented', self.pinned)
        self.job = {'id': str(uuid.uuid4()), 'phase': 'held', 'release_approval_required': True,
            'commit_admitted_at': 900, 'before': dict(self.pinned),
            'after': {**self.pinned, 'bundle_sha256': 'c'*64}, 'rooms': ['room1']}
        self.backend.job = lambda: deepcopy(self.job)
        self.policy = self.enterContext(patch.object(operations, 'policy_allowed'))
        self.clean = self.enterContext(patch.object(self.backend, 'require_clean_closed'))
        self.enterContext(patch('poolrotater_host.pilot_operations._service_state',
                               return_value={'MainPID': 0, 'ActiveState': 'failed'}))
        self.recover = self.enterContext(patch('poolrotater_host.updates.executor.controller',
                                             side_effect=lambda *a,**kw:self.job.update(phase='complete')))
        self.enterContext(patch.object(operations.time, 'time', return_value=1000))
        from poolrotater_host.provisioning import _control
        self.enterContext(patch.object(operations, '_control', side_effect=lambda path,*a,**kw:
            _control(path, __import__('os').geteuid(), **kw)))
        self.command = {'action': 'complete-update', 'operation_id': self.job['id'],
            'candidate_sha256': 'c'*64, 'job_sha256': fingerprint(self.job)}

    def test_clean_approved_stopped_update_continues_once(self):
        self.backend.perform(self.command)
        self.clean.assert_called_once()
        self.assertEqual(self.recover.call_args.args[:2], ('invented', self.target))
        self.assertEqual(self.recover.call_args.kwargs, {'decision': 'complete'})

    def test_unknown_room_ownership_prevents_all_native_actions(self):
        self.clean.side_effect = NetworkError('rescue_ownership_requires_review')
        with self.assertRaises(NetworkError):
            self.backend.perform(self.command)
        self.recover.assert_not_called()
        self.assertFalse((self.root/'recovery/attempts.json').exists())

    def test_changed_job_candidate_or_binding_prevents_recovery(self):
        for change in ({'job_sha256': 'd'*64}, {'candidate_sha256': 'd'*64},
                       {'operation_id': str(uuid.uuid4())}):
            with self.subTest(change=change), self.assertRaises(NetworkError):
                self.backend.perform({**self.command, **change})
        self.recover.assert_not_called()

    def test_operator_pause_or_update_opt_out_prevents_recovery(self):
        self.policy.side_effect = NetworkError('rescue_policy_disabled')
        with self.assertRaises(NetworkError):
            self.backend.perform(self.command)
        self.recover.assert_not_called()

    def test_attempt_budget_survives_new_request_and_coordinator_recreation(self):
        self.recover.side_effect = OSError('invented transient failure')
        for index in range(3):
            with patch.object(operations.time, 'time', return_value=1000+61*index):
                with self.assertRaises(OSError):
                    self.backend.perform(self.command)
        with patch.object(operations.time, 'time', return_value=1300):
            with self.assertRaisesRegex(NetworkError, 'rescue_attempt_limit'):
                self.backend.perform(self.command)
        self.assertEqual(self.recover.call_count, 3)

    def test_no_commit_or_unsigned_job_cannot_be_used_as_remote_start(self):
        for change in ({'commit_admitted_at': None}, {'release_approval_required': False}, {'phase':'waiting_empty'}):
            job = {**self.job, **change}
            with patch.object(self.backend, 'job', return_value=job):
                with self.subTest(change=change), self.assertRaises(NetworkError):
                    self.backend.perform({**self.command, 'job_sha256': fingerprint(job)})
        self.recover.assert_not_called()

    def test_automatic_never_retries_generic_guards_or_expired_intent(self):
        for change in ({'failure_code': 'update_guard_requires_review'}, {'commit_admitted_at': 1}):
            with patch.object(self.backend, 'job', return_value={**self.job, **change}):
                if change.get('commit_admitted_at') == 1:
                    with patch.object(operations.time, 'time', return_value=4000):
                        self.assertEqual(self.backend.automatic(), 'requires-review')
                else:
                    self.assertEqual(self.backend.automatic(), 'requires-review')
        self.recover.assert_not_called()

    def test_clean_closed_guard_reads_real_journal_and_refuses_every_uncertainty(self):
        from contextlib import nullcontext
        self.enterContext(patch('poolrotater_host.ratings.operations._identity',side_effect=lambda uid:nullcontext()))
        from poolrotater_host.layout import setup
        host = self.target['root']
        setup(host,['standard-auto'])
        rolling = host/'state/rolling'
        rolling.mkdir(mode=0o700)
        row = {'phase':'parked','operation':'closed','ownership_unknown':False,
               'room_id':None,'channel_id':None,'pid':None,'target':None,'error':None}
        def write(value):atomic_json(rolling/'ownership.json',{'format':1,'rooms':{'room1':value}})
        original = operations.Backend.require_clean_closed
        write(row)
        original(self.backend,self.job)
        for change in ({'ownership_unknown':True},{'operation':'make_pending'},
                       {'room_id':123},{'pid':42},{'phase':'running'},
                       {'creation_candidate':{'format':1,'room_id':123,'connection_generation':1,'observed_at':1000}},
                       {'target':'unreviewed'},{'error':'unknown'}):
            write({**row,**change})
            with self.subTest(change=change),self.assertRaises(Exception):
                original(self.backend,self.job)

    def test_pause_preserves_reporting_but_prevents_mutations(self):
        config=self.root/'recovery/configuration.json'
        atomic_json(config,{'format':1,'enabled':True,'allow_recovery':True})
        with patch('poolrotater_host.pilot_installation.paths',return_value=self.target), \
             patch.object(operations,'protected',side_effect=lambda p:p):
            operations.pause('invented')
        state=read_json(config)
        self.assertTrue(state['enabled']);self.assertFalse(state['allow_recovery'])

    def test_diagnostics_report_transition_state_without_configuration_values(self):
        from poolrotater_host.controller_update import _config_hashes
        host=self.target['root'];(host/'config').mkdir(parents=True)
        atomic_json(host/'config/example.json',{'private':'invented-do-not-share'})
        self.job['configuration']=_config_hashes(host)
        (self.root/'releases').mkdir()
        for key in ('before','after'):
            release=self.root/'releases'/key;release.mkdir();atomic_json(release/'release.json',{'version':'v7.1.14'})
            self.job[key]['release']=str(release)
        folder=self.root/'installations';folder.mkdir()
        atomic_json(folder/(self.pinned['installation_id']+'.json'),self.job['before'])
        atomic_json(self.root/'pilot-upgrade.json',{'before':self.job['before'],'after':self.job['after'],'phase':'committed'})
        atomic_json(host/'config/example.json',{'private':'another-do-not-share'})
        with patch.object(operations,'protected',side_effect=lambda p:p):
            frame=self.backend.inspect()
        self.assertEqual(frame['registration'],'before');self.assertEqual(frame['upgrade'],'committed')
        self.assertEqual(frame['configuration'],'changed')
        self.assertNotIn('do-not-share',str(frame));self.assertNotIn('example.json',str(frame))


if __name__ == '__main__':
    unittest.main()
