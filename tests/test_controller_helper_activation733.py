"""Characterize managed-helper activation during an admitted stopped upgrade.

The fixture uses the real helper installer and update timer quiescer. Systemd
is intercepted at its command boundary; no live units or service are touched.
"""
from scripts.test_isolation import require_test_isolation
require_test_isolation('provisioning')

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from poolrotater_host.layout import atomic_json
from poolrotater_host.config import ConfigError
from poolrotater_host.managed_host import operations as managed
from poolrotater_host.provisioning import _write
from poolrotater_host.update_helpers import quiesce
from poolrotater_host.update_lifecycle import wait_stopped


class ManagedHelperActivationTests(unittest.TestCase):
    def cleanup_children(self, children):
        for child in children:
            if child.poll() is None:
                child.wait(timeout=3)

    def fixture(self, base):
        uid = 1000
        root = base / 'host'
        prefix = base / 'prefix'
        release = base / 'release'
        units = base / 'units'
        for path in (root / 'state', prefix / 'updates' / 'cache',
                     release / 'poolrotater_host', units):
            path.mkdir(parents=True, exist_ok=True)
        marker = {'format': 1, 'installation_id': 'a' * 36,
                  'generation': str(uuid.UUID('10000000-0000-4000-8000-000000000001'))}
        marker['installation_id'] = str(uuid.UUID('20000000-0000-4000-8000-000000000002'))
        atomic_json(root / '.poolrotater-host.json', marker)
        os.chown(root / '.poolrotater-host.json', uid, uid)
        owner_lock = root / 'state' / 'owner.lock'
        owner_lock.touch(mode=0o600)
        owner_lock.chmod(0o600)
        os.chown(owner_lock, uid, uid)
        atomic_json(prefix / 'updates' / 'cache' / 'policy.json',
                    {'format': 1, 'binding': {}})
        bootstrap = release / 'poolrotater_host' / 'managed_bootstrap.py'
        bootstrap.write_text('# isolated helper fixture\n')
        bootstrap.chmod(0o444)

        states = {'managed': 'inactive', 'updates': 'active', 'recovery': 'inactive'}
        children = []
        launch_helper_on_timer = [False]
        for kind, state in states.items():
            if kind == 'managed':
                # First-run ensure creates and registers the managed unit. The
                # admitted-upgrade case installs it once before quiescing.
                continue
            name = f'poolrotater-v7-{kind}-fixture'
            (units / (name + '.timer')).write_text(
                f'[Timer]\nUnit={name}.service\n')
            (units / (name + '.timer')).chmod(0o644)

        target = {'root': root, 'prefix': prefix}
        record = {'installation_id': marker['installation_id'],
                  'generation': marker['generation'], 'service_uid': uid,
                  'release': str(release), 'prefix': str(prefix),
                  'host_root': str(root), 'bundle_sha256': 'b' * 64}
        calls = []

        def systemctl(action, *args, **kwargs):
            calls.append((action, *args))
            if action == 'show':
                timer = args[0]
                kind = timer.removeprefix('poolrotater-v7-').split('-', 1)[0]
                return (states[kind] + '\n').encode()
            if action == 'stop':
                kind = args[0].removeprefix('poolrotater-v7-').split('-', 1)[0]
                states[kind] = 'inactive'
            elif action == 'enable' and args[0] == '--now':
                timer = args[1]
                kind = timer.removeprefix('poolrotater-v7-').split('-', 1)[0]
                states[kind] = 'active'
                if kind == 'managed' and launch_helper_on_timer[0]:
                    def service_uid():
                        os.setgroups([])
                        os.setgid(uid)
                        os.setuid(uid)
                    children.append(subprocess.Popen(
                        ['/usr/bin/python3', '-c', 'import time; time.sleep(1)'],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, preexec_fn=service_uid))
            elif action == 'start':
                kind = args[0].removeprefix('poolrotater-v7-').split('-', 1)[0]
                states[kind] = 'active'
            return b''

        return (uid, root, prefix, units, target, record, states, calls,
                systemctl, children, launch_helper_on_timer)

    def install_fixture_helper(self, fixture):
        uid, root, prefix, units, target, record, states, calls, systemctl, _, _ = fixture
        with patch('poolrotater_host.pilot_installation.paths', return_value=target), \
             patch('poolrotater_host.pilot_operations.UNIT_BASE', units), \
             patch('poolrotater_host.pilot_operations._systemctl', side_effect=systemctl), \
             patch('poolrotater_host.network.operations._checked_unit'), \
             patch('poolrotater_host.updates.agent.policy', return_value=None):
            managed.ensure('fixture', record)

    def update_job(self, fixture, *, phase='committing', after=None, attempts=0,
                   admitted=True):
        uid, root, prefix, units, target, record, states, calls, systemctl, children, launch = fixture
        now = time.time()
        after = dict(record if after is None else after)
        before = {**after, 'bundle_sha256': 'a' * 64}
        value = {'format': 1, 'id': str(uuid.uuid4()), 'phase': phase,
                 'before': before, 'after': after,
                 'commit_started_at': now - 2, 'commit_admitted_at': now - 1,
                 'automatic_recovery': {'format': 1, 'attempts': attempts}}
        if not admitted:
            value.pop('commit_admitted_at')
        if phase in ('recovering', 'retrying'):
            value['automatic_recovery']['last_attempt_at'] = now - 0.5
        _write(prefix / 'controller-update.json', value)
        self.assertEqual((prefix / 'controller-update.json').stat().st_mode & 0o777, 0o644)
        return value

    def test_admitted_upgrade_defers_helper_until_quiesced_configuration_is_ready(self):
        """The real process census stays clear after ensure inside commit."""
        with tempfile.TemporaryDirectory(dir='/runtime') as directory:
            fixture = self.fixture(Path(directory))
            uid, root, prefix, units, target, record, states, calls, systemctl, children, launch = fixture
            self.addCleanup(self.cleanup_children, children)
            # Establish the real helper unit through the normal manual path.
            self.install_fixture_helper(fixture)
            timer_text = (units / 'poolrotater-v7-managed-fixture.timer').read_text()
            self.assertIn('OnBootSec=90', timer_text)
            self.assertIn('OnUnitInactiveSec=15', timer_text)
            states['managed'] = 'inactive'
            calls.clear()
            launch[0] = True
            self.update_job(fixture)
            job = json.loads((prefix / 'controller-update.json').read_text())
            with patch('poolrotater_host.pilot_operations.UNIT_BASE', units), \
                 patch('poolrotater_host.pilot_operations._systemctl', side_effect=systemctl):
                with quiesce('fixture', target, job, lambda **values: job.update(values)):
                    self.assertEqual(states['managed'], 'inactive')
                    self.install_fixture_helper(fixture)
                    self.assertEqual(job['paused_helper_timers'][0]['was_active'], False)
                    self.assertEqual(states['managed'], 'inactive')
                    self.assertFalse(children)
                    self.assertFalse(any(c[:2] == ('enable', '--now')
                                         and c[2] == 'poolrotater-v7-managed-fixture.timer'
                                         for c in calls))
                    wait_stopped(root, uid, seconds=0)
                self.assertEqual(states['managed'], 'inactive')
            self.assertIn(('enable', 'poolrotater-v7-managed-fixture.timer'), calls)
            self.assertEqual(job['phase'], 'committing')

    def test_admitted_recovery_and_held_phases_durably_defer_activation(self):
        for phase in ('recovering', 'retrying', 'held'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(dir='/runtime') as directory:
                fixture = self.fixture(Path(directory))
                uid, root, prefix, units, target, record, states, calls, systemctl, children, launch = fixture
                self.addCleanup(self.cleanup_children, children)
                self.install_fixture_helper(fixture)
                states['managed'] = 'inactive'
                calls.clear()
                self.update_job(fixture, phase=phase, attempts=1)
                launch[0] = True
                self.install_fixture_helper(fixture)
                self.assertEqual(states['managed'], 'inactive')
                self.assertFalse(children)
                self.assertFalse(any(c[:2] == ('enable', '--now')
                                     and c[2] == 'poolrotater-v7-managed-fixture.timer'
                                     for c in calls))
                wait_stopped(root, uid, seconds=0)

    def test_originally_active_timer_is_restored_after_deferred_commit(self):
        with tempfile.TemporaryDirectory(dir='/runtime') as directory:
            fixture = self.fixture(Path(directory))
            uid, root, prefix, units, target, record, states, calls, systemctl, children, launch = fixture
            self.addCleanup(self.cleanup_children, children)
            self.install_fixture_helper(fixture)
            states['managed'] = 'active'
            calls.clear()
            self.update_job(fixture)
            job = json.loads((prefix / 'controller-update.json').read_text())
            with patch('poolrotater_host.pilot_operations.UNIT_BASE', units), \
                 patch('poolrotater_host.pilot_operations._systemctl', side_effect=systemctl):
                with quiesce('fixture', target, job, lambda **values: job.update(values)):
                    self.assertEqual(job['paused_helper_timers'][0]['was_active'], True)
                    self.install_fixture_helper(fixture)
                    self.assertEqual(states['managed'], 'inactive')
                    wait_stopped(root, uid, seconds=0)
                self.assertEqual(states['managed'], 'active')
            self.assertIn(('start', 'poolrotater-v7-managed-fixture.timer'), calls)

    def test_nonadmitted_terminal_or_mismatched_jobs_do_not_suppress_manual_activation(self):
        for phase, mismatched, admitted in (('complete', False, True),
                                            ('waiting_empty', False, True),
                                            ('held', False, False),
                                            ('committing', True, True)):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(dir='/runtime') as directory:
                fixture = self.fixture(Path(directory))
                uid, root, prefix, units, target, record, states, calls, systemctl, children, launch = fixture
                self.addCleanup(self.cleanup_children, children)
                self.install_fixture_helper(fixture)
                states['managed'] = 'inactive'
                calls.clear()
                after = {**record, 'bundle_sha256': 'c' * 64} if mismatched else None
                self.update_job(fixture, phase=phase, after=after, admitted=admitted)
                self.install_fixture_helper(fixture)
                self.assertEqual(states['managed'], 'active')
                self.assertIn(('enable', '--now', 'poolrotater-v7-managed-fixture.timer'), calls)

    def test_malformed_admitted_binding_fails_before_timer_activation(self):
        for corruption in ('generation', 'host_root'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory(dir='/runtime') as directory:
                fixture = self.fixture(Path(directory))
                uid, root, prefix, units, target, record, states, calls, systemctl, children, launch = fixture
                self.addCleanup(self.cleanup_children, children)
                self.install_fixture_helper(fixture)
                states['managed'] = 'inactive'
                calls.clear()
                malformed = self.update_job(fixture)
                if corruption == 'generation':
                    malformed['after'].pop('generation')
                else:
                    malformed['after']['host_root'] = str(Path(directory) / 'foreign-host')
                _write(prefix / 'controller-update.json', malformed)
                with self.assertRaisesRegex(ConfigError, 'update_guard_requires_review'):
                    self.install_fixture_helper(fixture)
                self.assertEqual(states['managed'], 'inactive')
                self.assertFalse(any(c[0] == 'enable' for c in calls))

    def test_unprotected_or_symlinked_native_job_fails_closed(self):
        for damage in ('world-writable', 'symlink'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory(dir='/runtime') as directory:
                fixture = self.fixture(Path(directory))
                uid, root, prefix, units, target, record, states, calls, systemctl, children, launch = fixture
                self.addCleanup(self.cleanup_children, children)
                self.install_fixture_helper(fixture)
                states['managed'] = 'inactive'
                calls.clear()
                self.update_job(fixture)
                job_path = prefix / 'controller-update.json'
                if damage == 'world-writable':
                    job_path.chmod(0o666)
                else:
                    external = prefix / 'external.json'
                    _write(external, json.loads(job_path.read_text()))
                    job_path.unlink()
                    job_path.symlink_to(external)
                with self.assertRaises(ConfigError):
                    self.install_fixture_helper(fixture)
                self.assertEqual(states['managed'], 'inactive')
                self.assertFalse(any(c[0] == 'enable' for c in calls))

    def test_manual_stopped_upgrade_keeps_the_existing_default_activation(self):
        """The ordinary ensure path continues to install and arm the helper."""
        with tempfile.TemporaryDirectory(dir='/runtime') as directory:
            fixture = self.fixture(Path(directory))
            self.install_fixture_helper(fixture)
            self.assertEqual(fixture[6]['managed'], 'active')
            self.assertIn(('enable', '--now', 'poolrotater-v7-managed-fixture.timer'), fixture[7])


if __name__ == '__main__':
    unittest.main()
