"""Current controller/worker observations and explicit operator next actions."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from poolrotater_host.operator_status import summary


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.target={'root':Path('/host'),'prefix':Path('/runtime')}
        self.parent={'service_uid':1000,'prefix':'/runtime','installation_id':'installation',
            'release':'/runtime/releases/v7.1.5-'+'a'*16,'bundle_sha256':'a'*64}
        self.service={'ActiveState':'active','MainPID':123}
        self.native={'generated_at':time.time(),'supervisor_version':'v7.1.5','rooms':{
            'room1':{'version':'v7.1.5','phase':'running','healthy':True,'status':{'players':2}}}}
        self.job=None;self.progress=None;self.update=None
        def control(path,*args,**kwargs):
            name=Path(path).name
            if name=='status.json':return self.native
            if name=='controller-update.json':return self.update
            if name=='operator-maintenance.json':return self.job if Path(path).parent.name=='runtime' else self.progress
            return None
        self.enterContext(patch('poolrotater_host.operator_status._control',side_effect=control))
        self.enterContext(patch('poolrotater_host.worker_deployment.read_job',return_value=None))

    def result(self):
        return summary(self.target,self.parent,self.service,current_observation={'phase':'running','version':'v7.1.5'})

    def test_current_and_installed_versions_and_lifetime_counter_scope(self):
        result=self.result()
        self.assertEqual(result['running_controller_version'],'v7.1.5')
        self.assertEqual(result['installed_controller_version'],'v7.1.5')
        self.assertTrue(result['workers']['room1']['healthy'])
        self.assertIn('historical',result['pacing_counter_scope'])

    def test_stale_failed_or_unmatched_service_never_claims_healthy_workers(self):
        self.native['generated_at']=time.time()-20
        self.assertFalse(self.result()['workers']['room1']['healthy'])
        self.assertIsNone(self.result()['running_controller_version'])
        self.native['generated_at']=time.time();self.service={'ActiveState':'failed','MainPID':0}
        self.assertFalse(self.result()['workers']['room1']['observation_current'])

    def test_controller_target_blocking_reason_and_all_empty_requirement(self):
        self.update={'phase':'waiting_empty','reason':'waiting_for_all_rooms_empty',
            'after':{'release':'/runtime/releases/v7.1.6-'+'b'*16}}
        queued=self.result()['queued_update']
        self.assertEqual(queued['target_version'],'v7.1.6')
        self.assertEqual(queued['blocking_reason'],'waiting_for_all_rooms_empty')
        self.assertTrue(queued['all_rooms_empty_required'])

    def test_maintenance_restart_review_and_completion_next_steps(self):
        self.job={'id':'operation','parent_sha256':'a'*64,'mode':'one-map','phase':'requested'}
        self.progress={'id':'operation','phase':'requires_review','rooms':{},'controller_pid':999}
        self.assertFalse(self.result()['maintenance']['progress_current'])
        self.assertIn('cancel_retained',self.result()['operator_next_action'])
        self.progress.update(phase='complete',controller_pid=123)
        self.assertIn('stop_service',self.result()['operator_next_action'])

    def test_worker_queue_names_approved_target_and_retains_per_room_ownership(self):
        self.native['rooms']['room1'].update(operation='owned',ownership_unknown=False,room_id=101)
        job={'id':'job','candidate_sha256':'b'*64,'rooms':['room1']}
        def control(path,*args,**kwargs):
            name=Path(path).name
            if name=='status.json':return self.native
            if name=='b'*64+'.json':return {'candidate':{'release':'/runtime/releases/v7.1.8-'+'b'*16}}
            return None
        with patch('poolrotater_host.worker_deployment.read_job',return_value=job),patch('poolrotater_host.operator_status._control',side_effect=control):
            result=self.result()
        self.assertEqual(result['queued_update']['target_version'],'v7.1.8')
        self.assertFalse(result['queued_update']['all_rooms_empty_required'])
        self.assertEqual(result['workers']['room1']['operation'],'owned')
        self.assertFalse(result['workers']['room1']['ownership_unknown'])
