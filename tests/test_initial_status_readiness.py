"""A cold catalog audit must finish before old updaters observe readiness."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch
from rolling.supervisor import RollingSupervisor
from poolrotater_host.provider_service import run_provider_service

class StartupStatusTests(unittest.TestCase):
    def supervisor(self, *, private=False):
        events=[];worker=SimpleNamespace(process=SimpleNamespace(pid=123,poll=lambda:None))
        supervisor=SimpleNamespace(schema_baseline=None,configs={'room1':{'catalog_db':'invented.db'}},
            workers={'room1':worker},handles={'room1':worker},stopping=threading.Event())
        if private:supervisor.configs['room2']={'catalog_db':'unselected.db'}
        supervisor.write_status=lambda **kw:events.append(('status',kw,supervisor.schema_baseline))
        supervisor.prepare_startup_status=lambda:RollingSupervisor.prepare_startup_status(supervisor)
        return supervisor,worker,events

    def test_slow_schema_audit_finishes_before_fresh_status_publication(self):
        supervisor,_,events=self.supervisor();elapsed=[0]
        def audit(configs):
            elapsed[0]+=93;events.append(('audit',elapsed[0]));return {'invented.db':'schema-identity'}
        with patch('rolling.supervisor.schemas',side_effect=audit)as schemas:
            supervisor.prepare_startup_status();supervisor.prepare_startup_status()
        self.assertEqual(schemas.call_count,1)
        self.assertEqual(events,[('audit',93),('status',{'force':True},{'invented.db':'schema-identity'}),
                                  ('status',{'force':True},{'invented.db':'schema-identity'})])

    def test_failed_schema_audit_cannot_publish_success(self):
        supervisor,_,events=self.supervisor()
        with patch('rolling.supervisor.schemas',side_effect=ValueError('invented invalid catalog')):
            with self.assertRaises(ValueError):supervisor.prepare_startup_status()
        self.assertEqual(events,[]);self.assertIsNone(supervisor.schema_baseline)

    def test_private_subset_publishes_status_without_claiming_full_host_schema_baseline(self):
        supervisor,_,events=self.supervisor(private=True)
        with patch('rolling.supervisor.schemas')as schemas:supervisor.prepare_startup_status()
        schemas.assert_not_called();self.assertIsNone(supervisor.schema_baseline)
        self.assertEqual(events,[('status',{'force':True},None)])

    def test_service_announces_running_only_after_schema_and_status(self):
        supervisor,worker,events=self.supervisor()
        supervisor.start_release_for=lambda _:Path('/invented-release')
        supervisor.prepare=lambda *_:worker
        supervisor.activate=lambda _:events.append(('activated',))
        supervisor.tick=lambda:supervisor.stopping.set()
        supervisor.request_shutdown=lambda:supervisor.stopping.set()
        @contextmanager
        def controller(*a,**kw):
            try:yield supervisor
            finally:worker.process.poll=lambda:0
        def observe(phase,*_):events.append(('phase',phase))
        with patch('poolrotater_host.provider_service._authority'),patch(
                'poolrotater_host.provider_service.provider_host_controller',controller),patch(
                'poolrotater_host.provider_usage.summary',return_value='invented'),patch(
                'rolling.supervisor.schemas',return_value={'db':'checked'}):
            run_provider_service(Path('/invented'),Path('/invented-release'),
                                 authentication=None,api=None,hub=None,observer=observe)
        self.assertLess(events.index(('status',{'force':True},{'db':'checked'})),events.index(('phase','running')))
        self.assertIn(('phase','stopped'),events)

if __name__=='__main__':unittest.main()
