"""Managed tick integration for stale recovery-pin refresh; no live services."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from poolrotater_host import pilot_operations
from poolrotater_host.managed_host import operations


class ManagedRecoveryRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name)
        self.prefix=base/'prefix';self.root=base/'state'
        self.prefix.mkdir();self.root.mkdir()
        (self.prefix/'managed').mkdir()
        self.record={'format':1,'installation_id':'install-id','generation':'generation-id',
            'service_uid':954,'host_root':str(self.root),'prefix':str(self.prefix),
            'bundle_sha256':'a'*64}
        self.marker={'installation_id':'install-id','generation':'generation-id'}
        self.target={'prefix':self.prefix,'root':self.root,'unit':'pilot.service'}

    def test_pin_wrapper_absent_current_stale_and_bounded_fault(self):
        path=self.prefix/'recovery/configuration.json'
        with patch('poolrotater_host.provisioning._control') as control, \
             patch('poolrotater_host.provisioning.protected',side_effect=lambda value:Path(value)), \
             patch('poolrotater_host.rescue.operations.refresh_pin') as refresh:
            absent=operations._refresh_recovery_pin('first',self.record)
            self.assertEqual(absent,{'status':'absent','pin_refreshed':False})
            refresh.assert_not_called()

            path.parent.mkdir()
            path.touch()
            control.return_value={'format':1,'record':self.record}
            current=operations._refresh_recovery_pin('first',self.record)
            self.assertEqual(current,{'status':'current','pin_refreshed':False})
            refresh.assert_not_called()

            control.return_value={'format':1,'record':{'bundle_sha256':'old'}}
            refresh.return_value={'status':'refreshed','pin_refreshed':True}
            stale=operations._refresh_recovery_pin('first',self.record)
            self.assertEqual(stale,{'status':'refreshed','pin_refreshed':True})
            refresh.assert_called_once_with('first',self.record)

            control.side_effect=RuntimeError('DO_NOT_LOG_PRIVATE_POLICY')
            failed=operations._refresh_recovery_pin('first',self.record)
            self.assertEqual(failed,{'status':'refused','reason':'recovery_policy_unavailable',
                                     'pin_refreshed':False})
            self.assertNotIn('DO_NOT_LOG_PRIVATE_POLICY',repr(failed))

    def _tick_setup(self,job,*,recovery_result=None):
        import poolrotater_host.provisioning as provisioning
        import poolrotater_host.pilot_installation as pilot_installation
        import poolrotater_host.network.operations as network
        import poolrotater_host.managed_host.topology as topology
        import poolrotater_host.managed_host.catalog_rollout as catalog_rollout
        import poolrotater_host.ratings.operations as ratings_operations
        from poolrotater_host.managed_host import policy as managed_policy

        def control(path,*args,**kwargs):
            if Path(path)==self.root/managed_policy.NAME:
                return {'policy_version':1}
            if Path(path)==self.prefix/'controller-update.json':
                return job
            return None

        systemctl=Mock()
        caps={'max_concurrent_rooms':0,'provider_room_limit':0,'policy_version':1,'rooms':[]}
        patches=[
            patch.object(pilot_installation,'administrative_binding',
                         return_value=(self.target,self.marker,self.record)),
            patch.object(provisioning,'_control',side_effect=control),
            patch.object(provisioning,'protected',side_effect=lambda value:Path(value)),
            patch.object(operations,'file_lease',side_effect=lambda *a,**k:nullcontext()),
            patch.object(operations,'_refresh_recovery_pin',
                         return_value=recovery_result or {'status':'current','pin_refreshed':False}),
            patch.object(operations,'_desired',return_value=(caps,[],0)),
            patch.object(topology,'pending',return_value=False),
            patch.object(catalog_rollout,'prepare_pending',return_value=None),
            patch.object(pilot_operations,'_unit',return_value=object()),
            patch.object(pilot_operations,'_service_state',
                         return_value={'MainPID':123,'ActiveState':'active'}),
            patch.object(pilot_operations,'_systemctl',systemctl),
            patch.object(network,'_checked_unit',return_value=None),
            patch.object(network,'_state',return_value={'MainPID':1,'ActiveState':'active'}),
            patch.object(ratings_operations,'_identity',side_effect=lambda *a,**k:nullcontext()),
        ]
        return patches,systemctl

    def test_active_job_returns_before_pin_callback(self):
        job={'format':1,'phase':'committing','after':self.record}
        patches,systemctl=self._tick_setup(job)
        with patches[0],patches[1],patches[2],patches[3],patches[4] as refresh, \
             patches[5],patches[6],patches[7],patches[8],patches[9],patches[10],patches[11],patches[12]:
            result=operations.tick('first')
        self.assertEqual(result['phase'],'update-in-progress')
        refresh.assert_not_called()
        systemctl.assert_not_called()

    def test_running_controller_includes_pin_status_without_start_or_stop(self):
        job={'format':1,'phase':'complete','after':self.record}
        pin={'status':'current','pin_refreshed':False}
        patches,systemctl=self._tick_setup(job,recovery_result=pin)
        with patches[0],patches[1],patches[2],patches[3],patches[4] as refresh, \
             patches[5],patches[6],patches[7],patches[8],patches[9],patches[10],patches[11],patches[12]:
            result=operations.tick('first')
        self.assertEqual(result,{'ok':True,'phase':'controller-running','rooms_started':False,
                                 'recovery_pin':pin})
        refresh.assert_called_once_with('first',self.record)
        systemctl.assert_not_called()


if __name__ == '__main__':
    unittest.main()
