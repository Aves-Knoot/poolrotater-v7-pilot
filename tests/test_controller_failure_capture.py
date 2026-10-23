"""Original internal validator failures survive cleanup without leaking locals."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from poolrotater_host import provider_service as service
from poolrotater_host.config import ConfigError
from poolrotater_host.network.contracts import NetworkError
from poolrotater_host.service_diagnostics import controller_failure, failure_details


class FailureCaptureTests(unittest.TestCase):
    def test_internal_validation_code_is_preserved_without_becoming_an_http_error(self):
        for code in ('unsafe_log_database', 'room_ownership_requires_review',
                     'invalid_managed_generation', 'foreign_managed_room_projection'):
            with self.subTest(code=code):
                self.assertEqual(failure_details(NetworkError(code,409)), {'failure_code':code})

    def test_unknown_private_error_text_and_dynamic_exception_names_do_not_escape(self):
        error=type('invented_private_type_canary', (Exception,), {})('invented-private-message-canary')
        detail=controller_failure(error, '/invented/registered/release', 'not_a_component')
        self.assertEqual(detail['failure_code'],'service_failed')
        self.assertEqual(detail['exception_type'],'Exception')
        self.assertEqual(detail['component'],'startup')
        self.assertNotIn('canary',json.dumps(detail))
        self.assertEqual(failure_details(NetworkError('invented-private-message-canary')),
                         {'failure_code':'service_failed'})

    def test_only_registered_source_coordinates_are_recorded_without_locals(self):
        with tempfile.TemporaryDirectory()as temp:
            release=Path(temp)
            filename=str(release/'poolrotater_host/managed_host/native.py')
            code=compile("def fail():\n secret='invented-private-local-canary'\n raise Error('unsafe_log_database')\nfail()\n",filename,'exec')
            try:exec(code,{'Error':NetworkError})
            except NetworkError as error:detail=controller_failure(error,release,'managed_rooms')
            self.assertEqual(detail['failure_code'],'unsafe_log_database')
            self.assertEqual(detail['component'],'managed_rooms')
            self.assertEqual(detail['frames'][-1],{'file':'poolrotater_host/managed_host/native.py','function':'fail','line':3})
            self.assertNotIn(str(release),json.dumps(detail))
            self.assertNotIn('canary',json.dumps(detail))
            self.assertLessEqual(len(detail['frames']),12)

    def test_external_traceback_paths_are_not_exported(self):
        try:raise NetworkError('room_ownership_requires_review')
        except NetworkError as error:detail=controller_failure(error,'/invented/registered/release','workers')
        self.assertEqual(detail['frames'],[])

    def test_invalid_component_container_cannot_break_failure_capture(self):
        self.assertEqual(controller_failure(NetworkError('unsafe_log_database'),'/release',[])['component'],'startup')

    def test_native_loop_marks_each_stage_and_does_not_swallow_validation_errors(self):
        from rolling.supervisor import RollingSupervisor
        stages={'configuration':'config_loader','deployment_settings':'reload_deployment_settings',
            'protected_requests':'_protected_requests','bookings':'bookings','managed_rooms':'managed',
            'booking_panels':'booking_panels','rollout_requests':'_requests',
            'discord_commands':'_discord_commands','status':'write_status'}
        with tempfile.TemporaryDirectory()as temp:
            for component,attribute in stages.items():
                owner=SimpleNamespace(config_loader=lambda:None,config_error=None,
                    reload_deployment_settings=lambda:None,_protected_requests=lambda:None,
                    protected_parent=None,bookings=None,managed=None,booking_panels=None,
                    workers={},schema_baseline={},_requests=lambda:None,
                    _discord_commands=lambda:None,write_status=lambda:None,runtime=Path(temp))
                error=NetworkError('unsafe_log_database')
                def fail():raise error
                setattr(owner,attribute,SimpleNamespace(tick=fail)if attribute in('bookings','managed','booking_panels')else fail)
                with self.subTest(component=component),self.assertRaises(NetworkError)as caught:
                    RollingSupervisor.tick(owner)
                self.assertIs(caught.exception,error)
                self.assertEqual(owner._tick_component,component)

    def test_failure_callback_precedes_explicit_shutdown_and_transport_cleanup(self):
        events=[]
        owner=SimpleNamespace(request_shutdown=lambda:events.append('shutdown'))
        @contextmanager
        def provider(*args,**kwargs):
            try:yield owner
            finally:events.append('cleanup')
        original=NetworkError('room_ownership_requires_review')
        with patch.object(service,'provider_host_controller',provider):
            with self.assertRaises(NetworkError)as caught:
                with service._managed_service_controller(_on_failure=lambda error,owner:events.append('capture')):
                    raise original
        self.assertIs(caught.exception,original)
        self.assertEqual(events,['capture','shutdown','cleanup'])

    def test_diagnostic_failure_does_not_mask_original_ownership_error(self):
        events=[]
        owner=SimpleNamespace(request_shutdown=lambda:events.append('shutdown'))
        @contextmanager
        def provider(*args,**kwargs):
            try:yield owner
            finally:events.append('cleanup')
        def broken(error,owner):raise OSError('invented-disk-failure')
        with patch.object(service,'provider_host_controller',provider):
            with self.assertRaisesRegex(NetworkError,'room_ownership_requires_review'):
                with service._managed_service_controller(_on_failure=broken):
                    raise NetworkError('room_ownership_requires_review')
        self.assertEqual(events,['shutdown','cleanup'])

    def test_main_loop_persists_original_code_and_component_before_teardown(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);(root/'state').mkdir()
            phases=[];evidence_at_cleanup=[]
            owner=SimpleNamespace(configs={},stopping=threading.Event())
            owner.prepare_startup_status=lambda:None
            owner.request_shutdown=owner.stopping.set
            def tick():
                owner._tick_component='managed_rooms'
                raise NetworkError('unsafe_log_database')
            owner.tick=tick
            @contextmanager
            def provider(*args,**kwargs):
                try:yield owner
                finally:evidence_at_cleanup.append(json.loads((root/'state/controller-failure.json').read_text()))
            with patch.object(service,'provider_host_controller',provider),patch.object(service,'_authority'),\
                    patch('poolrotater_host.provider_usage.summary',return_value='invented usage'):
                with self.assertRaisesRegex(NetworkError,'unsafe_log_database'):
                    service.run_provider_service(root,root/'release',authentication=object(),api=object(),hub=object(),
                        observer=lambda phase,*args:phases.append(phase))
            self.assertEqual(phases,['running','failed'])
            self.assertEqual(len(evidence_at_cleanup),1)
            self.assertEqual(evidence_at_cleanup[0]['failure_code'],'unsafe_log_database')
            self.assertEqual(evidence_at_cleanup[0]['component'],'managed_rooms')
            self.assertTrue(owner.stopping.is_set())

    def test_cleanup_error_does_not_replace_original_failure_in_service_record(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);(root/'state').mkdir()
            owner=SimpleNamespace(configs={},stopping=threading.Event(),manifest={'version':'v7.3.1'})
            owner.prepare_startup_status=lambda:None;owner.request_shutdown=owner.stopping.set
            def tick():
                owner._tick_component='managed_rooms'
                raise NetworkError('unsafe_log_database')
            owner.tick=tick
            hub=SimpleNamespace(client=SimpleNamespace(_session=object()))
            auth=SimpleNamespace(execution='provider-live-pilot',binding={'installation_id':'invented','generation':'invented'})
            @contextmanager
            def provider(*args,**kwargs):
                try:yield owner
                finally:
                    hub.client._session=None
                    raise ConfigError('provider_cleanup_incomplete')
            with patch.object(service,'provider_host_controller',provider),patch.object(service,'_authority'),\
                    patch('poolrotater_host.provider_usage.summary',return_value='invented usage'):
                with self.assertRaisesRegex(ConfigError,'provider_cleanup_incomplete'):
                    service.run_provider_service(root,root/'release',authentication=auth,api=object(),hub=hub)
            observed=json.loads((root/'state/provider-service.json').read_text())
            self.assertEqual(observed['failure_code'],'unsafe_log_database')
            self.assertEqual(observed['phase'],'failed')
            self.assertTrue(observed['workers_reaped'])
            self.assertTrue(observed['referee_child_reaped'])


if __name__=='__main__':unittest.main()
