"""Returned creation identity survives failed verification; mutations never replay."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from unittest.mock import patch

from bot.hub import HubInvocationUncertain
from bot.bot import LobbyBot
from bot.multiroom import SharedReferee
from poolrotater_host import provider_rooms as rooms
from poolrotater_host.layout import file_lease, read_json
from poolrotater_host.oauth import AuthError
from poolrotater_host.ownership_recovery import digest,identified
from poolrotater_host.provider_pacing import ProviderAdmissionDeferred
from poolrotater_host.provider_room_recovery import ProviderOwnershipRecovery,ProviderOwnershipReview
from poolrotater_host.reconciliation import _journal
from poolrotater_host.service_diagnostics import failure_details
from tests.test_v7_provider_rooms import RoomCase


class RefreshAdmissionTests(unittest.TestCase):
    def test_lost_worker_authority_refuses_before_any_creation_dispatch(self):
        hub=Mock();hub._connect_generation=1
        shared=SharedReferee(hub,queue.Queue());view=shared.room_view('invented',queue.Queue())
        view.creation_begin=Mock(side_effect=HubInvocationUncertain('worker authority lost'))
        with self.assertRaises(HubInvocationUncertain):view.make_room('invented',1)
        hub.make_room.assert_not_called();self.assertFalse(view.owned)
        self.assertFalse(view.creating)

    def test_invalid_provenance_during_creation_fences_the_candidate_before_adoption(self):
        hub=SimpleNamespace(_connect_generation=1)
        shared=SharedReferee(hub,queue.Queue());view=shared.room_view('invented',queue.Queue())
        view.creating=True;view.creation_pending_id=123
        shared._invalidate_provider_events()
        self.assertTrue(view.roster_fenced)
        with self.assertRaisesRegex(HubInvocationUncertain,'referee_snapshot_requires_review'):
            view._adopt_created({'room_id':123},1)
        self.assertFalse(view.owned)

    def test_candidate_event_buffer_and_generation_fences_prevent_adoption(self):
        hub=SimpleNamespace(_connect_generation=1)
        shared=SharedReferee(hub,queue.Queue())
        view=shared.room_view('invented',queue.Queue())
        view.creation_pending_id=123
        for _ in range(129):shared.route('UserJoined',{'room_id':123,'user_id':7})
        self.assertEqual(len(view.creation_events),128)
        self.assertTrue(view.creation_events_overflow);self.assertTrue(view.events.empty())
        with self.assertRaisesRegex(HubInvocationUncertain,'referee_snapshot_requires_review'):
            view._adopt_created({'room_id':123},1)
        self.assertFalse(view.owned);self.assertFalse(shared.routes)
        view.creation_events_overflow=False;hub._connect_generation=2
        with self.assertRaisesRegex(HubInvocationUncertain,'referee_generation_changed'):
            view._adopt_created({'room_id':123},1)
        self.assertFalse(view.owned)

    def test_automatic_refresh_leaves_old_room_and_status_untouched_and_retries_later(self):
        bot=object.__new__(LobbyBot);bot.room_id=123
        bot.hub=SimpleNamespace(refresh_ready=Mock(return_value=False),close_room=Mock())
        bot._update_discord_public_status=Mock();bot.say=Mock();bot.create_room=Mock()
        self.assertFalse(bot.hard_restart_room(close_reason='automatic_hard_restart'))
        self.assertFalse(bot.hard_restart_room(close_reason='automatic_hard_restart'))
        bot.hub.refresh_ready.assert_called_once()
        bot.hub.close_room.assert_not_called();bot._update_discord_public_status.assert_not_called()
        bot.say.assert_not_called();bot.create_room.assert_not_called()
        self.assertEqual(bot.room_id,123)

    def test_worker_advisory_remains_compatible_with_old_controller_but_does_not_hide_auth_errors(self):
        from rolling.adapters import Hub
        from rolling.protocol import RemoteError
        peer=Mock();hub=Hub(peer)
        peer.call.side_effect=RemoteError('ValueError','unsupported referee operation')
        self.assertTrue(hub.refresh_ready())
        peer.call.side_effect=RemoteError('AuthError','operator_client_changed')
        with self.assertRaises(RemoteError):hub.refresh_ready()


def candidate(rid=101):
    return {'format':1,'room_id':rid,'connection_generation':1,'observed_at':time.time()}


class VerificationTests(RoomCase):
    def setUp(self):
        super().setUp();self.auth=self.authorized()
        self.backend,self.api,self.hub=self.adapters(self.auth)

    def test_persistent_admission_cooldown_waits_without_consuming_read_attempts_or_recreating(self):
        now=[100.]
        clock=SimpleNamespace(monotonic=lambda:now[0],sleep=lambda delay:now.__setitem__(0,now[0]+delay))
        with self.auth.verified_owner():
            self.hub.connect()
            value=self.hub.client.make_room('invented',1)
            def delay():return max(0.,160.-now[0])
            with patch.object(rooms,'time',clock),patch.object(self.auth.transport.gate,'read_delay',side_effect=delay),patch.object(rooms,'_fetch',return_value=(200,self.evidence(active=True))) as reads:
                self.assertEqual(self.hub.finish_make_room(value,self.hub._connect_generation)['room_id'],101)
            self.assertGreaterEqual(now[0],159.5);self.assertEqual(reads.call_count,1)
        self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),1)

    def test_unending_cooldown_is_bounded_without_dispatch_or_lost_candidate(self):
        now=[100.];seen=[]
        clock=SimpleNamespace(monotonic=lambda:now[0],sleep=lambda delay:now.__setitem__(0,now[0]+delay))
        with self.auth.verified_owner():
            self.hub.connect()
            with patch.object(rooms,'time',clock),patch.object(rooms,'OWNER_ADMISSION_BUDGET',2.),patch.object(self.auth.transport.gate,'read_delay',return_value=60.),patch.object(rooms,'_fetch') as reads:
                with self.assertRaisesRegex(HubInvocationUncertain,'referee_room_owner_requires_review'):
                    self.hub.make_room('invented',1,_on_created=lambda rid,generation:seen.append(rid))
                reads.assert_not_called()
        self.assertEqual(now[0],102.);self.assertEqual(seen,[101])
        self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),1)

    def test_shutdown_cancels_wait_before_read(self):
        with self.auth.verified_owner():
            self.hub.connect();value=self.hub.client.make_room('invented',1)
            with patch.object(rooms,'_fetch') as reads:
                with self.assertRaisesRegex(HubInvocationUncertain,'referee_generation_changed'):
                    self.hub.finish_make_room(value,self.hub._connect_generation,cancelled=lambda:True)
                reads.assert_not_called()

    def test_referee_disconnect_before_or_during_confirmation_cannot_adopt_candidate(self):
        with self.auth.verified_owner():
            self.hub.connect();value=self.hub.client.make_room('invented',1)
            with patch.object(self.hub.client,'is_connected',return_value=False),patch.object(rooms,'_fetch') as reads:
                with self.assertRaisesRegex(HubInvocationUncertain,'referee_generation_changed'):
                    self.hub.finish_make_room(value,self.hub._connect_generation)
                reads.assert_not_called()
            with patch.object(self.hub.client,'is_connected',side_effect=[True,False]),patch.object(rooms,'_fetch',return_value=(200,self.evidence(active=True))):
                with self.assertRaisesRegex(HubInvocationUncertain,'referee_generation_changed'):
                    self.hub.finish_make_room(value,self.hub._connect_generation)

    def test_refresh_readiness_does_not_reset_or_reserve_the_persistent_gate(self):
        gate=self.auth.transport.gate
        with self.auth.verified_owner():
            self.hub.connect()
            with gate.admission(time.monotonic()+5):
                gate.exchange_started();gate.uncertain()
            before=gate._read();calls=len(self.provider.calls)
            self.assertFalse(self.hub.refresh_ready())
            self.assertGreater(gate.read_delay(),55.)
            self.assertEqual(gate._read(),before)
            self.assertEqual(len(self.provider.calls),calls)

    def test_busy_admission_lease_defers_refresh_without_changing_ledger(self):
        gate=self.auth.transport.gate
        with self.auth.verified_owner():
            self.hub.connect();before=gate._read()
            with file_lease(self.root/gate.lock_path):
                self.assertFalse(self.hub.refresh_ready())
            self.assertEqual(gate._read(),before)

    def test_split_confirmation_preserves_sibling_routing_and_buffers_candidate_events(self):
        entered=threading.Event();release=threading.Event();results=[];errors=[]
        with self.auth.verified_owner():
            self.hub.connect();shared=SharedReferee(self.hub,self.hub.client.events)
            a=shared.room_view('a',queue.Queue());b=shared.room_view('b',queue.Queue())
            sibling=b.make_room('sibling',1)['room_id']
            committed=[];a.creation_committed=lambda value:committed.append(value['room_id'])
            original=self.hub.finish_make_room
            def blocked(snapshot,generation,**kwargs):
                entered.set()
                if not release.wait(8):raise AssertionError('fixture confirmation not released')
                return original(snapshot,generation,**kwargs)
            def create():
                try:results.append(a.make_room('candidate',1))
                except Exception as error:errors.append(error)
            with patch.object(self.hub,'finish_make_room',side_effect=blocked):
                thread=threading.Thread(target=create);thread.start()
                try:
                    self.assertTrue(entered.wait(5));self.assertFalse(a.owned)
                    self.assertFalse(committed)
                    self.assertTrue(shared.lock.acquire(timeout=.5));shared.lock.release()
                    rid=a.creation_pending_id;self.assertIsNotNone(rid)
                    # Use the admitted router, not fabricated provider provenance.
                    # The wire receives and stamps each invented service event.
                    self.backend.wire.route('UserJoined',{'room_id':sibling,'user_id':77})
                    self.backend.wire.route('UserJoined',{'room_id':rid,'user_id':88})
                    until=time.monotonic()+3
                    while b.events.empty() or not a.creation_events:
                        self.assertLess(time.monotonic(),until)
                        shared.route_event(self.hub.client.events.get(timeout=1))
                    self.assertEqual(b.events.get_nowait()[1]['user_id'],77)
                    self.assertTrue(a.events.empty())
                    release.set();thread.join(6)
                    self.assertFalse(thread.is_alive());self.assertFalse(errors,errors)
                    self.assertEqual(committed,[rid]);self.assertEqual(results[0]['room_id'],rid)
                    self.assertEqual(a.events.get_nowait()[1]['user_id'],88)
                    self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),2)
                finally:
                    release.set();thread.join(8)

    def test_real_tls_temporary_503_then_success_never_repeats_make(self):
        seen=[];original=rooms._fetch
        self.provider.wire('/api/v2/rooms/101',503,b'{"private":"invented-provider-canary"}')
        def fetch(auth,path):
            self.assertEqual(seen,[101]);return original(auth,path)
        with self.auth.verified_owner():
            self.hub.connect()
            with patch.object(rooms,'_fetch',side_effect=fetch) as reads:
                value=self.hub.make_room('invented',1,_on_created=lambda rid,generation:seen.append(rid))
            self.assertEqual(reads.call_count,2)
        self.assertEqual(value['room_id'],101)
        self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),1)

    def test_forbidden_read_keeps_safe_status_and_candidate_without_retry(self):
        seen=[]
        self.provider.wire('/api/v2/rooms/101',403,b'{"private":"invented-provider-canary"}')
        original=rooms._fetch
        with self.auth.verified_owner():
            self.hub.connect()
            with patch.object(rooms,'_fetch',wraps=original) as reads,self.assertLogs(rooms.log,level='WARNING') as logs:
                with self.assertRaises(HubInvocationUncertain) as error:
                    self.hub.make_room('invented',1,_on_created=lambda rid,generation:seen.append(rid))
            self.assertEqual(reads.call_count,1)
        self.assertEqual(seen,[101]);self.assertEqual(failure_details(error.exception),{
            'failure_code':'referee_room_owner_requires_review','failure_http_status':403})
        self.assertNotIn('invented-provider-canary',str(logs.output)+str(error.exception))
        self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),1)

    def test_transient_transport_and_admission_retry_only_reads(self):
        for first in (ProviderAdmissionDeferred('provider_pacing_deferred'),AuthError('provider_deadline_exceeded')):
            with self.subTest(code=first.code),self.auth.verified_owner():
                self.hub.connect()
                # Validate a join snapshot directly so every trial uses one
                # already-created room rather than another room mutation.
                value=self.hub.client.make_room('invented',1)
                with patch.object(rooms,'_fetch',side_effect=[first,(200,self.evidence(value['room_id'],active=True))]) as reads:
                    self.hub._checked_snapshot(value,self.hub._connect_generation)
                self.assertEqual(reads.call_count,2)

    def test_authority_cleanup_halt_foreign_owner_and_malformed_evidence_never_retry(self):
        failures=[AuthError('provider_child_cleanup_uncertain'),AuthError('operator_client_changed'),
            AuthError('provider_pacing_recovery_required'),AuthError('provider_pacing_deferred')]
        with self.auth.verified_owner():
            self.hub.connect();value=self.hub.client.make_room('invented',1)
            for error in failures:
                with self.subTest(code=error.code),patch.object(rooms,'_fetch',side_effect=error) as reads:
                    with self.assertRaises(HubInvocationUncertain):self.hub._checked_snapshot(value,self.hub._connect_generation)
                    self.assertEqual(reads.call_count,1)
            for evidence in ({**self.evidence(active=True),'user_id':100},self.evidence(),{'private':'invented-provider-canary'}):
                with patch.object(rooms,'_fetch',return_value=(200,evidence)) as reads:
                    with self.assertRaises(HubInvocationUncertain):self.hub._checked_snapshot(value,self.hub._connect_generation)
                    self.assertEqual(reads.call_count,1)
        self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),1)

    def test_repeated_transient_refusal_is_bounded_and_does_not_recreate(self):
        seen=[]
        with self.auth.verified_owner():
            self.hub.connect()
            with patch.object(rooms,'_fetch',return_value=(503,{})) as reads:
                with self.assertRaises(HubInvocationUncertain):
                    self.hub.make_room('invented',1,_on_created=lambda rid,generation:seen.append(rid))
                self.assertEqual(reads.call_count,rooms.OWNER_READ_ATTEMPTS)
        self.assertEqual(seen,[101]);self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),1)

    def test_temporarily_inactive_own_room_must_become_active_before_adoption(self):
        with self.auth.verified_owner():
            self.hub.connect();value=self.hub.client.make_room('invented',1)
            pending={**self.evidence(active=True),'active':False}
            with patch.object(rooms,'_fetch',side_effect=[(200,pending),(200,self.evidence(active=True))]) as reads:
                result=self.hub._checked_snapshot(value,self.hub._connect_generation)
                self.assertEqual(reads.call_count,2);self.assertEqual(result['room_id'],101)

    def test_unusable_snapshot_keeps_candidate_without_granting_authority(self):
        seen=[];self.backend.fault('MakeRoom','no-referee')
        with self.auth.verified_owner():
            self.hub.connect()
            with patch.object(rooms,'_fetch') as fetch:
                with self.assertRaisesRegex(HubInvocationUncertain,'referee_snapshot_requires_review'):
                    self.hub.make_room('invented',1,_on_created=lambda rid,generation:seen.append(rid))
                fetch.assert_not_called()
        self.assertEqual(seen,[101]);self.assertEqual(len(self.backend.rooms),1)

    def test_candidate_write_failure_never_proceeds_or_repeats_make(self):
        with self.auth.verified_owner():
            self.hub.connect()
            with patch.object(rooms,'_fetch') as fetch:
                with self.assertRaisesRegex(HubInvocationUncertain,'referee_creation_record_requires_review') as caught:
                    self.hub.make_room('invented',1,_on_created=lambda *args:(_ for _ in ()).throw(OSError('private canary')))
                fetch.assert_not_called()
        self.assertNotIn('private canary',str(caught.exception))
        self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),1)


class RecoveryTests(RoomCase):
    def setUp(self):
        super().setUp();self.auth=self.authorized();self.bind(self.auth)
        self.state={'room_id':None,'operation':'make_pending','ownership_unknown':True,
            'phase':'recovery_required','creation_candidate':candidate(),
            'creation_verification_failure':{'failure_code':'referee_room_owner_requires_review','failure_http_status':503}}
        self.journal(room1=self.state);self.before=read_json(self.root/'state/rolling/ownership.json')
        self.provider.details['101']=(200,self.evidence())

    def apply(self):
        return ProviderOwnershipRecovery(self.authority()).apply(expected_journal_sha256=digest(self.before))

    def test_legacy_incident_with_parked_sibling_and_ended_unlisted_replacement_uses_explicit_id(self):
        closed={'room_id':None,'channel_id':None,'phase':'parked','operation':'closed','ownership_unknown':False}
        lost={'room_id':None,'channel_id':None,'phase':'recovery_required','operation':'make_pending','ownership_unknown':True}
        self.journal(room1=closed,room2=lost)
        self.before=read_json(self.root/'state/rolling/ownership.json')
        self.provider.rooms=[]
        self.provider.details['4716142']=(200,self.evidence(4716142))
        result=ProviderOwnershipRecovery(self.authority()).apply(
            expected_journal_sha256=digest(self.before),creation_candidates={'room2':4716142})
        self.assertTrue(result['recovery_applied'])
        after=read_json(self.root/'state/rolling/ownership.json')['rooms']
        self.assertEqual(after['room1'],closed)
        self.assertEqual(after['room2']['operation'],'closed')
        self.assertFalse(after['room2']['ownership_unknown'])
        receipt=read_json(self.root/'state/ownership-recovery.json')
        self.assertEqual(receipt['before'],self.before)
        self.assertEqual(receipt['creation_candidates'],{'room2':4716142})

    def test_saved_reply_identity_and_fresh_ended_owner_checks_clear_without_external_room_id(self):
        report=ProviderOwnershipReview(self.authority()).inspect()['rooms'][0]
        self.assertEqual(report['result'],'unknown_creation')
        self.assertEqual(report['creation_candidate_room_id'],101)
        self.assertEqual(report['creation_candidate_result'],'confirmed_ended')
        result=self.apply();self.assertTrue(result['recovery_applied'])
        receipt=read_json(self.root/'state/ownership-recovery.json')
        self.assertEqual(receipt['before'],self.before)
        after=receipt['after']['rooms']['room1']
        self.assertEqual(after['operation'],'closed');self.assertFalse(after['ownership_unknown'])
        self.assertIsNone(after['creation_candidate']);self.assertIsNone(after['creation_verification_failure'])
        self.assertFalse(any(row['method'] not in ('GET','POST')for row in self.provider.calls))

    def test_active_unconfirmed_foreign_or_missing_candidate_cannot_clear(self):
        for status,evidence in ((200,self.evidence(active=True)),(404,{}),(503,{}),
                (200,{**self.evidence(),'user_id':100})):
            self.provider.details['101']=(status,evidence)
            with self.subTest(status=status,evidence=evidence),self.assertRaises(AuthError):self.apply()
            self.assertEqual(read_json(self.root/'state/rolling/ownership.json'),self.before)
            self.assertFalse((self.root/'state/ownership-recovery.json').exists())

    def test_old_ended_room_cannot_substitute_for_saved_replacement(self):
        self.state['creation_candidate']=candidate(102);self.journal(room1=self.state)
        self.before=read_json(self.root/'state/rolling/ownership.json')
        self.provider.details['102']=(200,self.evidence(102,active=True))
        with self.assertRaises(AuthError):self.apply()
        with self.assertRaisesRegex(AuthError,'ownership_creation_candidate_refused'):
            identified(self.before,{'room1':101})

    def test_missing_legacy_identity_remains_unknown(self):
        state={k:v for k,v in self.state.items() if k!='creation_candidate'}
        self.journal(room1=state);self.before=read_json(self.root/'state/rolling/ownership.json')
        with self.assertRaises(AuthError):self.apply()
        report=ProviderOwnershipReview(self.authority()).inspect()['rooms'][0]
        self.assertNotIn('creation_candidate_room_id',report)

    def test_malformed_or_cross_alias_candidate_refuses_before_recovery(self):
        for change in ({'room_id':True},{'connection_generation':0},{'observed_at':float('inf')},{'format':True}):
            invalid=copy.deepcopy(self.before);invalid['rooms']['room1']['creation_candidate'].update(change)
            with self.subTest(change=change),self.assertRaises(AuthError):_journal(invalid,{'room1'})
        invalid=copy.deepcopy(self.before);invalid['rooms']['room2']={'room_id':101,'operation':'owned'}
        with self.assertRaises(AuthError):_journal(invalid,{'room1','room2'})

    def test_saved_candidate_keeps_the_capped_listing_fence(self):
        recovery=ProviderOwnershipRecovery(self.authority())
        report={'listing_at_limit':True,'untracked_unconfirmed_room_ids':[],
            'rooms':[{'result':'confirmed_ended'}]}
        with patch.object(recovery,'_inspect',return_value=report):
            with self.assertRaisesRegex(AuthError,'ownership_recovery_unresolved'):
                recovery.apply(expected_journal_sha256=digest(self.before))
        self.assertEqual(read_json(self.root/'state/rolling/ownership.json'),self.before)

    def test_interrupted_saved_candidate_recovery_resumes_without_losing_original_evidence(self):
        recovery=ProviderOwnershipRecovery(self.authority());write=recovery._write
        def interrupt(path,value):
            if path.name=='ownership-recovery.json' and value['phase']=='committed':
                raise OSError('invented lost final commit reply')
            return write(path,value)
        with patch.object(recovery,'_write',side_effect=interrupt):
            with self.assertRaises(OSError):recovery.apply(expected_journal_sha256=digest(self.before))
        receipt=read_json(self.root/'state/ownership-recovery.json')
        self.assertEqual(receipt['phase'],'prepared');self.assertEqual(receipt['before'],self.before)
        resumed=ProviderOwnershipRecovery(self.authority()).recover(transaction_id=receipt['transaction_id'],action='complete')
        self.assertEqual(resumed['phase'],'committed')
        self.assertEqual(read_json(self.root/'state/rolling/ownership.json'),receipt['after'])
        self.assertEqual(read_json(self.root/'state/ownership-recovery.json')['before'],self.before)
