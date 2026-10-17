"""Invalid native identities and bounded evidence-bound update recovery."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from contextlib import nullcontext
import json
import sqlite3
import unittest
from unittest.mock import patch
from tests import test_activity_collector as capture_tests
from tests import test_activity_history_backfill as history_tests
from tests import test_update_recovery as recovery_tests
from tests import test_operator_status as status_tests
from poolrotater_host import update_lifecycle as lifecycle
from poolrotater_host.config import ConfigError
from poolrotater_host.pilot_installation import CleanupPending
from poolrotater_host.layout import read_json,atomic_json
from poolrotater_host.activity.collector import Collector,provider_identity
from poolrotater_host.updates.recovery import _fresh_running as native_fresh_running


class IdentityBoundaryTests(unittest.TestCase):
    setUp=capture_tests.ActivityCollectorTests.setUp
    public=capture_tests.ActivityCollectorTests.public
    presence=capture_tests.ActivityCollectorTests.presence
    join=capture_tests.ActivityCollectorTests.join
    events=capture_tests.ActivityCollectorTests.events
    consent=capture_tests.ActivityCollectorTests.consent
    ack=capture_tests.ActivityCollectorTests.ack

    def native_dump(self):
        with sqlite3.connect(self.ledger)as db:return '\n'.join(db.iterdump())

    def test_internal_privacy_identity_does_not_poison_capture_or_change_native_records(self):
        self.consent();self.public();self.join()
        with sqlite3.connect(self.ledger)as db:
            db.executemany('INSERT INTO autohost_stats_privacy VALUES(?,?,111)',[(-885287630797963264,0),(0,1),(2**53,0),(9,1)])
        before=self.native_dump();result=self.collector.collect()
        self.assertTrue(result['privacy_safe']);self.assertEqual(self.native_dump(),before)
        self.assertEqual({e['payload']['user_id']for e in self.events()},{2,9})
        self.assertEqual(self.collector.status()['identity_exclusions_last_scan']['privacy'],3)
        self.ack();self.collector.collect();self.assertEqual(self.events(),[])

    def test_internal_presence_join_and_match_rows_advance_without_blocking_valid_player(self):
        self.consent();self.public();self.join();self.presence(start=101,end=110)
        key=self.native_match()
        with sqlite3.connect(self.ledger)as db:
            # Direct native data models retained legacy/internal rows too.
            for table in ('activity_joins','activity_presence','autohost_match_players'):
                db.execute('UPDATE '+table+' SET user_id=-99')
        self.join(user=3);self.presence(start=101,end=110,user=3)
        before=self.native_dump();self.collector.collect()
        self.assertEqual(self.native_dump(),before)
        self.assertEqual({e['payload']['user_id']for e in self.events()},{3})
        status=self.collector.status()['identity_exclusions_last_scan']
        for kind in ('join','presence','match'):self.assertEqual(status[kind],1)
        self.ack();self.collector.collect();self.assertEqual(self.events(),[])

    def native_match(self):
        key=self.native.record_match_start(room_id=1,playlist_item_id=3,beatmap_id=4,map_label='Invented',star_rating=4,participants=[{'user_id':2,'username':'Invented'}],started_at=101)
        self.native.record_match_end(match_key=key,room_id=1,playlist_item_id=3,result='completed',ended_at=110)
        return key

    def test_malformed_valid_player_preference_fences_old_outbox_until_repaired(self):
        self.consent();self.public();self.join()
        with sqlite3.connect(self.ledger)as db:db.execute('INSERT INTO autohost_stats_privacy VALUES(2,0,111)')
        self.collector.collect()
        with sqlite3.connect(self.ledger)as db:db.execute("UPDATE autohost_stats_privacy SET hidden=2")
        result=self.collector.collect();self.assertTrue(result['privacy_review_required'])
        self.assertEqual(self.events(),[]);self.assertTrue(self.collector.status()['privacy_review_required'])
        self.assertEqual(Collector(self.root,self.ledger,marker=self.marker).batch()['events'],[])
        with sqlite3.connect(self.ledger)as db:db.execute('UPDATE autohost_stats_privacy SET hidden=1,updated_at=112')
        self.collector.collect();self.assertEqual([e['kind']for e in self.events()],['privacy'])
        self.assertTrue(self.events()[0]['payload']['hidden'])
        self.assertFalse(self.collector.status()['privacy_review_required'])

    def test_correcting_malformed_choice_requeues_same_previously_unsent_public_preference(self):
        self.consent();self.public();self.join()
        with sqlite3.connect(self.ledger)as db:db.execute('INSERT INTO autohost_stats_privacy VALUES(2,0,111)')
        self.collector.collect()
        with sqlite3.connect(self.ledger)as db:db.execute('UPDATE autohost_stats_privacy SET hidden=3')
        self.collector.collect();self.assertEqual(self.events(),[])
        with sqlite3.connect(self.ledger)as db:db.execute('UPDATE autohost_stats_privacy SET hidden=0')
        self.collector.collect()
        self.assertEqual([e['kind']for e in self.events()],['privacy'])
        self.assertFalse(self.events()[0]['payload']['hidden'])

    def test_identity_is_exact_positive_wire_integer(self):
        for value in (-1,0,True,2.0,'2',2**53,None):self.assertFalse(provider_identity(value))
        for value in (1,2**53-1):self.assertTrue(provider_identity(value))

class HistoryIdentityTests(unittest.TestCase):
    setUp=history_tests.HistoryBackfillTests.setUp
    enable=history_tests.HistoryBackfillTests.enable
    seed=history_tests.HistoryBackfillTests.seed
    plan=history_tests.HistoryBackfillTests.plan
    backup_plan=history_tests.HistoryBackfillTests.backup_plan

    def test_internal_legacy_baseline_excluded_but_valid_history_still_queued(self):
        self.seed();self.plan()
        with sqlite3.connect(self.root/'state/controller-ratings.db')as db:
            db.execute("INSERT INTO autohost_stats_privacy VALUES(-44,'Internal',0,95)")
            db.execute("INSERT INTO autohost_lobby_time VALUES(-44,'Internal',99,1,20,80,90,90)")
            db.execute("INSERT INTO autohost_room_players VALUES(71,-44,'Internal',20,80,90,1,99)")
        self.assertEqual(history_tests.history.step(self.root)['phase'],'queued')
        events=Collector(self.root).batch()['events']
        self.assertEqual([e['payload']['user_id']for e in events],[9])
        self.assertEqual(history_tests.history.step(self.root)['phase'],'queued')

    def test_current_malformed_privacy_cannot_be_overridden_by_frozen_public_history(self):
        self.seed();self.backup_plan()
        with sqlite3.connect(self.root/'state/controller-ratings.db')as db:
            db.execute("INSERT INTO autohost_stats_privacy VALUES(9,'Historical Player',3,95)")
        with self.assertRaisesRegex(Exception,'privacy_preferences_require_review'):history_tests.history.step(self.root)
        self.assertEqual(Collector(self.root).batch()['events'],[])


class CleanupTests(unittest.TestCase):
    def test_only_identified_delays_retry_and_each_retry_rechecks_ownership(self):
        receipt={};ticks=[0.0]
        def sleep(seconds):ticks[0]+=seconds
        with patch('poolrotater_host.pilot_installation._stopped',return_value=nullcontext())as stopped,patch(
            'poolrotater_host.pilot_installation.require_no_service_processes',side_effect=[
                CleanupPending('update_children_still_running','invented'),None])as processes:
            lifecycle.wait_stopped(None,1000,seconds=2,monotonic=lambda:ticks[0],sleep=sleep,save=lambda **v:receipt.update(v))
        self.assertEqual(stopped.call_count,2);self.assertEqual(processes.call_count,2)
        self.assertEqual(receipt['cleanup_observations'],2)

    def test_unknown_ownership_and_unreadable_processes_are_never_retried(self):
        for error in (ConfigError('unknown ownership'),OSError('unreadable proc')):
            with patch('poolrotater_host.pilot_installation._stopped',side_effect=error)as stopped:
                with self.assertRaises(type(error)):lifecycle.wait_stopped(None,1000,sleep=lambda _:self.fail('must not wait'))
                self.assertEqual(stopped.call_count,1)

    def test_persistent_cleanup_delay_is_bounded_and_reports_safe_code(self):
        ticks=[0.0]
        with patch('poolrotater_host.pilot_installation._stopped',side_effect=CleanupPending('update_owner_lock_busy','secret must not leave')):
            with self.assertRaisesRegex(ConfigError,'update_cleanup_timeout')as caught:
                lifecycle.wait_stopped(None,1000,seconds=1,monotonic=lambda:ticks[0],sleep=lambda d:ticks.__setitem__(0,ticks[0]+d))
        self.assertEqual(ticks[0],1);self.assertEqual(lifecycle.diagnostic(caught.exception),'update_cleanup_timeout')
        self.assertNotIn('secret',lifecycle.diagnostic(caught.exception))


class AutomaticRecoveryTests(unittest.TestCase):
    setUp=recovery_tests.ControllerRecoveryTests.setUp
    def authorize(self):
        import time
        self.job.update(phase='committing',commit_admitted_at=time.time(),automatic_recovery={'format':1,'attempts':0})
        atomic_json(self.target['prefix']/self.native.NAME,self.job)
    def run_auto(self):return recovery_tests.recovery.controller('fixture',self.id,'complete',automatic=True)
    def test_interrupted_stopped_transaction_finishes_once(self):
        self.authorize();self.assertEqual(self.run_auto()['phase'],'complete');self.assertEqual(self.lifecycle.call_count,1)
        self.assertEqual(self.run_auto()['phase'],'complete');self.assertEqual(self.lifecycle.call_count,1)
    def test_healthy_successor_is_observed_even_after_start_intent(self):
        self.authorize();self.current=self.after;self.upgrade={'phase':'committed','transition_id':'kept'}
        self.service.update(MainPID=222,ActiveState='active')
        job=read_json(self.target['prefix']/self.native.NAME);job['startup_attempted_at']=1;atomic_json(self.target['prefix']/self.native.NAME,job)
        self.assertEqual(self.run_auto()['phase'],'complete');self.lifecycle.assert_not_called();self.ready.assert_called_once()
    def test_unknown_start_outcome_is_not_replayed(self):
        self.authorize();self.current=self.after;self.upgrade={'phase':'committed','transition_id':'kept'}
        job=read_json(self.target['prefix']/self.native.NAME);job['startup_attempted_at']=1;atomic_json(self.target['prefix']/self.native.NAME,job)
        result=self.run_auto();self.assertEqual(result['phase'],'held');self.lifecycle.assert_not_called()
        self.assertEqual(result['failure_code'],'update_start_outcome_requires_review')
    def test_legacy_held_job_is_not_given_automatic_authority(self):
        result=self.run_auto();self.assertEqual(result['phase'],'held');self.lifecycle.assert_not_called();self.finish.assert_not_called()
    def test_recovery_attempt_budget_is_durable(self):
        self.authorize()
        from poolrotater_host.update_lifecycle import wait_stopped
        with patch('poolrotater_host.update_lifecycle.wait_stopped',side_effect=ConfigError('update_cleanup_timeout')):
            for attempt in range(1,4):
                result=self.run_auto();job=read_json(self.target['prefix']/self.native.NAME)
                self.assertEqual(job['automatic_recovery']['attempts'],attempt)
                self.assertEqual(result['phase'],'retrying' if attempt<3 else 'held')
        self.lifecycle.assert_not_called();self.finish.assert_not_called()


class OperationalStateTests(unittest.TestCase):
    setUp=status_tests.StatusTests.setUp
    result=status_tests.StatusTests.result
    def test_offline_held_update_is_an_outage_not_waiting_for_empty_rooms(self):
        self.service.update(ActiveState='inactive',MainPID=0)
        self.update={'phase':'held','commit_admitted_at':1,'failure_stage':'cleanup','failure_code':'update_cleanup_timeout','after':{}}
        result=self.result();self.assertEqual(result['hosting_state'],'update_outage')
        self.assertIn('outage',result['operator_next_action'])
        self.assertEqual(result['queued_update']['failure_code'],'update_cleanup_timeout')
    def test_unreviewed_failure_text_is_not_exposed(self):
        self.update={'phase':'held','failure_stage':'secret-value','failure_code':'secret-value','after':{}}
        self.assertNotIn('failure_code',self.result()['queued_update']);self.assertNotIn('failure_stage',self.result()['queued_update'])

class OperationsProjectionTests(unittest.TestCase):
    def test_waiting_disabled_maintenance_outage_and_stalled_are_distinct(self):
        from poolrotater_host.operations_health import project
        service={'ActiveState':'active','MainPID':22};now=10000
        self.assertEqual(project(service,{'phase':'waiting_empty'},None,now=now)['hosting'],'waiting_for_empty_rooms')
        self.assertEqual(project(service,None,{'at':now,'phase':'disabled'},now=now)['reporting'],'disabled_by_policy')
        service={'ActiveState':'inactive','MainPID':0}
        for phase in ('committing','recovering','retrying'):
            job={'phase':phase,'commit_admitted_at':9999,'stage_at':9999,'stage':'cleanup'}
            self.assertEqual(project(service,job,None,now=now)['hosting'],'updating')
            self.assertEqual(project(service,job,None,now=now+2000)['hosting'],'update_stalled')
        job={'phase':'held','commit_admitted_at':9999}
        self.assertEqual(project(service,job,None,now=now)['hosting'],'update_outage')
        self.assertEqual(project(service,None,{'at':1,'phase':'synchronized'},now=now)['reporting'],'stalled')
        self.assertEqual(project(service,None,{'at':now,'phase':'partial','capture_errors':['activity-privacy-review-required']},now=now)['reporting'],'capture_blocked')
    def test_safe_diagnostics_keep_known_recovery_refusal_and_not_arbitrary_exception_text(self):
        from poolrotater_host.network.contracts import NetworkError
        self.assertEqual(lifecycle.diagnostic(NetworkError('update_recovery_configuration_changed')),'update_recovery_configuration_changed')
        self.assertNotIn('secret',lifecycle.diagnostic(NetworkError('secret-canary')))

class AdmissionUncertaintyTests(unittest.TestCase):
    def test_nonidle_fenced_worker_is_detected_instead_of_waiting_forever(self):
        from pathlib import Path
        import time
        from poolrotater_host import controller_update as update
        now=time.time();value={'generated_at':now,'rooms':{
            'room1':{'status':{'state':'SCORING','paused':True,'maintenance':{'uncertain':True}}},
            'room2':{'status':{'state':'IDLE','maintenance':{'uncertain':False}}}}}
        with patch.object(update,'_control',return_value=value):
            self.assertTrue(update._uncertain_maintenance({'root':Path('/invented')},{'service_uid':1000},{'rooms':['room1','room2']},now=now))
            self.assertFalse(update._uncertain_maintenance({'root':Path('/invented')},{'service_uid':1000},{'rooms':['room1','room2']},now=now+10))
            self.assertFalse(update._uncertain_maintenance({'root':Path('/invented')},{'service_uid':1000},{'rooms':['room1']},now=now))
    def test_stale_or_untyped_flags_cannot_be_treated_as_current_uncertainty(self):
        from pathlib import Path
        from poolrotater_host import controller_update as update
        target={'root':Path('/invented')};parent={'service_uid':1000};job={'rooms':['room1']}
        for value in (None,{}, {'generated_at':10,'rooms':{'room1':{'status':{'maintenance':{'uncertain':'true'}}}}},
            {'generated_at':float('nan'),'rooms':{'room1':{'status':{'maintenance':{'uncertain':True}}}}}):
            with patch.object(update,'_control',return_value=value):self.assertFalse(update._uncertain_maintenance(target,parent,job,now=10))

    def test_timer_holds_fresh_uncertainty_before_any_stop_gate_or_upgrade(self):
        from pathlib import Path
        import time
        from poolrotater_host import controller_update as update
        now=time.time();parent={'service_uid':1000};target={'root':Path('/invented'),'prefix':Path('/code')}
        job={'phase':'waiting_empty','before':parent,'after':{},'process':'same-process','configuration':{'keep':'bytes'},'rooms':['room1']}
        native={'generated_at':now,'rooms':{'room1':{'status':{'state':'SCORING','maintenance':{'uncertain':True}}}}}
        def control(path,*a,**kw):return job if Path(path).name==update.NAME else native
        with patch.object(update,'_binding',return_value=(target,{},None,parent)),patch.object(update,'_control',side_effect=control), \
            patch.object(update,'_process',return_value='same-process'),patch.object(update,'_config_hashes',return_value=job['configuration']), \
            patch.object(update,'validate_runtime',return_value={'manifest':{}}),patch.object(update,'file_lease',return_value=nullcontext()), \
            patch('poolrotater_host.pilot_operations._service_state',return_value={'MainPID':123}), \
            patch('poolrotater_host.updates.approval.require_upgrade'),patch.object(update,'_write'), \
            patch.object(update,'_rows',side_effect=AssertionError('uncertain admission must not become empty waiting')), \
            patch.object(update,'_gate')as gate,patch.object(update,'_commit')as commit,patch.object(update,'disarm'):
            result=update.step('fixture')
        self.assertEqual(result['phase'],'held');self.assertEqual(result['failure_stage'],'admission')
        self.assertEqual(result['failure_code'],'update_maintenance_outcome_requires_review')
        gate.assert_not_called();commit.assert_not_called()

class SuccessorObservationTests(unittest.TestCase):
    setUp=recovery_tests.ControllerRecoveryTests.setUp
    def test_cold_successor_status_can_arrive_after_provider_ready_without_restarting_it(self):
        from poolrotater_host.updates.contracts import UpdateError
        self.current=self.after;self.service.update(MainPID=222,ActiveState='active')
        self.fresh.side_effect=[UpdateError('update_recovery_fresh_workers_required'),{'ready':True}]
        with patch.object(recovery_tests.recovery.time,'sleep'):
            recovery_tests.recovery.wait_for_workers('fixture',self.target,self.current)
        self.assertEqual(self.fresh.call_count,2);self.lifecycle.assert_not_called()
    def test_changed_process_during_observation_is_not_adopted(self):
        from poolrotater_host.updates.contracts import UpdateError
        self.current=self.after;self.service.update(MainPID=222,ActiveState='active')
        self.fresh.side_effect=UpdateError('update_recovery_fresh_workers_required')
        with patch.object(recovery_tests.recovery.time,'sleep',side_effect=lambda _:self.service.update(MainPID=333)):
            with self.assertRaisesRegex(Exception,'existing_controller_requires_review'):
                recovery_tests.recovery.wait_for_workers('fixture',self.target,self.current)
        self.lifecycle.assert_not_called()
    def test_missing_fresh_observations_have_a_hard_bound(self):
        from poolrotater_host.updates.contracts import UpdateError
        self.service.update(MainPID=222,ActiveState='active');ticks=[0.0]
        self.fresh.side_effect=UpdateError('update_recovery_fresh_workers_required')
        with patch.object(recovery_tests.recovery.time,'monotonic',side_effect=lambda:ticks[0]),patch.object(
            recovery_tests.recovery.time,'sleep',side_effect=lambda d:ticks.__setitem__(0,ticks[0]+d)):
            with self.assertRaisesRegex(Exception,'fresh_workers_required'):
                recovery_tests.recovery.wait_for_workers('fixture',self.target,self.after,seconds=1)
        self.assertEqual(ticks[0],1);self.lifecycle.assert_not_called()

    def test_real_missing_status_guard_is_observed_again_without_starting_another_process(self):
        self.service.update(MainPID=222,ActiveState='active');calls=[]
        def observe(target,current):
            calls.append(1)
            if len(calls)==1:return native_fresh_running(target,current)
            return {'room1':{'ready':True}}
        self.fresh.side_effect=observe
        with patch('poolrotater_host.ratings.operations._identity',side_effect=lambda _:nullcontext()),patch(
                'poolrotater_host.layout.load',return_value=({}, {'rooms':[{'alias':'room1'}]})),patch.object(recovery_tests.recovery.time,'sleep'):
            recovery_tests.recovery.wait_for_workers('fixture',self.target,self.current)
        self.assertEqual(len(calls),2);self.lifecycle.assert_not_called()

    def test_other_update_refusals_are_not_retried(self):
        from poolrotater_host.updates.contracts import UpdateError
        self.service.update(MainPID=222,ActiveState='active')
        self.fresh.side_effect=UpdateError('update_recovery_room_topology_changed')
        with patch.object(recovery_tests.recovery.time,'sleep')as sleep:
            with self.assertRaisesRegex(UpdateError,'room_topology_changed'):
                recovery_tests.recovery.wait_for_workers('fixture',self.target,self.current)
        self.assertEqual(self.fresh.call_count,1);sleep.assert_not_called();self.lifecycle.assert_not_called()
