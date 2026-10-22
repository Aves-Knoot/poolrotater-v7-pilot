"""Controller switch preserves prior room/network service state and configuration.

Systemd and stopped upgrade are invented; native guards are separately qualified
by installed donor tests. No provider authority or credentials are loaded.
"""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from poolrotater_host import controller_update as update
from poolrotater_host import pilot_operations as pilot
from poolrotater_host.network import operations as network


class ControllerCommitTests(unittest.TestCase):
    def exercise(self, *, network_active, enabled, refuse_network=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'config').mkdir()
            config=root/'config/retained.json';config.write_text('{"configured":"keep"}\n')
            target={'root':root,'unit':'invented-owner.service'}
            parent={'service_uid':1000,'bundle_sha256':'a'*64}
            states={'bot':True,'network':network_active,'bot_enabled':enabled,'network_enabled':enabled}
            job={'network_active':network_active,'network_enabled':enabled,'service_enabled':enabled,
                 'archive':'invented.zip','after':{'bundle_sha256':'b'*64,'service_uid':1000},'configuration':update._config_hashes(root)}
            events=[];receipt={};upgraded=False
            def systemctl(action,*args,**kwargs):
                events.append((action,*args))
                if action=='stop':states['bot']=False
                elif action in('enable','disable'):
                    field='network_enabled' if 'network' in args[0] else 'bot_enabled'
                    states[field]=action=='enable'
            def network_unit(instance,action):
                if refuse_network:raise RuntimeError('invented stop refusal')
                if action=='start':
                    self.assertTrue(upgraded);self.assertTrue(states['bot'])
                states['network']=action=='start'
            def no_processes(uid):
                self.assertFalse(states['bot']);self.assertFalse(states['network'])
            def upgrade(*args,**kwargs):
                nonlocal upgraded
                no_processes(1000);upgraded=True
                return {'transition_id':'invented'}
            def lifecycle(instance,action,**kwargs):
                self.assertEqual(action,'start');self.assertTrue(upgraded)
                states['bot']=True;return {'ok':True}
            with patch.object(network,'_checked_unit'),patch.object(network,'unit',side_effect=network_unit), \
                    patch.object(pilot,'_systemctl',side_effect=systemctl), \
                    patch.object(pilot,'lifecycle',side_effect=lifecycle), \
                    patch('poolrotater_host.pilot_installation._stopped',return_value=nullcontext()), \
                    patch('poolrotater_host.pilot_installation.require_no_service_processes',side_effect=no_processes), \
                    patch('poolrotater_host.pilot_upgrade.upgrade',side_effect=upgrade), \
                    patch('poolrotater_host.managed_host.upgrade.prepare'), \
                    patch('poolrotater_host.ratings.operations._identity',return_value=nullcontext()), \
                    patch('poolrotater_host.updates.recovery.wait_for_workers'), \
                    patch.object(update,'_gate'),patch.object(update,'disarm'):
                if refuse_network:
                    with self.assertRaisesRegex(RuntimeError,'stop refusal'):
                        update._commit('invented',target,parent,job,lambda **v:receipt.update(v))
                    self.assertTrue(states['bot']);self.assertFalse(upgraded)
                    self.assertFalse(any(e[0]=='stop' for e in events));self.assertEqual(receipt['stage'],'network_stop');self.assertIn('commit_admitted_at',receipt)
                else:
                    update._commit('invented',target,parent,job,lambda **v:receipt.update(v))
                    self.assertEqual(states,{'bot':True,'network':network_active,
                        'bot_enabled':enabled,'network_enabled':enabled})
                    self.assertEqual(receipt['phase'],'complete')
                self.assertEqual(config.read_text(),'{"configured":"keep"}\n')

    def test_active_and_inactive_agent_and_enablement_are_preserved(self):
        for active in (False,True):
            for enabled in (False,True):
                with self.subTest(active=active,enabled=enabled):
                    self.exercise(network_active=active,enabled=enabled)

    def test_network_stop_refusal_never_stops_rooms_or_runs_upgrade(self):
        self.exercise(network_active=True,enabled=False,refuse_network=True)


class SignedControllerCommitTests(unittest.TestCase):
    def test_queue_refreshes_registration_after_waiting_for_native_admission(self):
        from poolrotater_host.config import ConfigError
        with tempfile.TemporaryDirectory() as directory:
            target = {'prefix': Path(directory)}
            before = {'bundle_sha256': 'a' * 64}
            successor = {'bundle_sha256': 'b' * 64}
            with patch('poolrotater_host.pilot_installation.administrative_binding',
                       side_effect=[(target, {}, before), (target, {}, successor)]) as binding, \
                 patch.object(update, 'file_lease', return_value=nullcontext()), \
                 patch.object(update, '_control', return_value=None), \
                 patch.object(update, '_provision') as provision:
                with self.assertRaisesRegex(ConfigError, 'starting digest differs'):
                    update.queue('invented', Path('unused.zip'), 'c' * 64,
                                 from_sha256='a' * 64)
                self.assertEqual(binding.call_count, 2)
                provision.assert_not_called()

    def test_expired_signed_approval_refuses_before_stopping_any_service(self):
        from poolrotater_host.config import ConfigError
        with tempfile.TemporaryDirectory()as directory:
            target={'prefix':Path(directory)};parent={}
            job={'release_approval_required':True,'after':{}}
            with patch.object(update,'validate_runtime',return_value={'manifest':{}}), \
                 patch('poolrotater_host.updates.approval.require_approved',side_effect=ConfigError('expired'))as check, \
                 patch.object(network,'_checked_unit')as unit, \
                 patch.object(network,'unit')as mutate_network, \
                 patch.object(pilot,'_systemctl')as mutate_bot:
                with self.assertRaisesRegex(ConfigError,'expired'):
                    update._commit('fixture',target,parent,job,lambda **kw:self.fail('no phase mutation'))
                check.assert_called_once();unit.assert_not_called()
                mutate_network.assert_not_called();mutate_bot.assert_not_called()
