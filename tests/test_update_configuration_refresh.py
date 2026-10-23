"""Settings drift must restart admission, never reset settings or stop players."""
from contextlib import nullcontext
import copy
import json
import os
from pathlib import Path
import uuid
import unittest

import pytest

from poolrotater_host import controller_update as update
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import atomic_json, load, read_json, setup


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / 'host'
    setup(root, ['standard-fixed'])
    marker, _ = load(root)
    prefix = tmp_path / 'prefix'
    prefix.mkdir(mode=0o700)
    target = {'root': root, 'prefix': prefix, 'unit': 'invented.service'}
    parent = {**marker, 'service_uid': os.geteuid(), 'bundle_sha256': 'a' * 64,
              'release': str(prefix / 'old')}
    job = {'format': 1, 'id': str(uuid.uuid4()), 'phase': 'waiting_empty',
           'before': parent, 'after': {}, 'configuration': update._config_hashes(root),
           'rooms': ['room1'], 'process': {'pid': 123, 'start': 'invented', 'boot': 'invented'},
           'empty_since': 50, 'locked_since': 70, 'reason': 'observing_continuous_empty_window'}
    def save(**values):
        job.update(values)
        atomic_json(prefix / update.NAME, job)
    save()
    def control(path, *args, **kwargs):
        path = Path(path)
        if not path.exists() and kwargs.get('optional'):
            return None
        return read_json(path)
    monkeypatch.setattr(update, '_control', control)
    monkeypatch.setattr(update, '_write', atomic_json)
    monkeypatch.setattr('poolrotater_host.ratings.operations._identity', lambda uid: nullcontext())
    return target, parent, job, save


def change_settings(root, **patch):
    path = root / 'config/host.json'
    value = read_json(path)
    value['rooms'][0].update(patch)
    atomic_json(path, value)
    return path.read_bytes()


def test_changed_settings_refresh_baseline_without_editing_them(host):
    target, parent, job, save = host
    saved = change_settings(target['root'], sr_min=2.5, sr_max=3.5)
    original = copy.deepcopy(job['configuration'])
    assert update._refresh_waiting_configuration(target, parent, job, save)
    assert job['phase'] == 'waiting_empty'
    assert job['reason'] == 'configuration_refreshed_waiting_for_fresh_empty_window'
    assert job['configuration'] == update._config_hashes(target['root']) != original
    assert job['empty_since'] is None and job['locked_since'] is None
    assert job['configuration_baseline_revision'] == 1
    assert (target['root'] / 'config/host.json').read_bytes() == saved


def test_locking_change_releases_only_existing_gate_and_restarts_window(host, monkeypatch):
    target, parent, job, save = host
    job.update(phase='locking', nonce='a' * 32)
    change_settings(target['root'], sr_min=3.0)
    gates = []
    monkeypatch.setattr(update, '_gate', lambda *args: gates.append(args[-1]))
    assert update._refresh_waiting_configuration(target, parent, job, save)
    assert gates == [None]
    assert job['phase'] == 'waiting_empty' and job['empty_since'] is None


def test_invalid_current_configuration_waits_without_accepting_or_overwriting_it(host):
    target, parent, job, save = host
    original = copy.deepcopy(job['configuration'])
    saved = change_settings(target['root'], sr_min=9.0, sr_max=2.0)
    assert update._refresh_waiting_configuration(target, parent, job, save)
    assert job['phase'] == 'waiting_empty'
    assert job['reason'] == 'waiting_for_valid_current_configuration'
    assert job['configuration'] == original
    assert (target['root'] / 'config/host.json').read_bytes() == saved


@pytest.mark.parametrize('witness', ['pilot-upgrade.json', 'managed-preparation'])
def test_prepared_migration_never_rebases_its_pinned_configuration(host, witness):
    target, parent, job, save = host
    change_settings(target['root'], sr_min=2.5)
    name = witness if witness != 'managed-preparation' else 'managed-preparation-' + job['id'] + '.json'
    atomic_json(target['prefix'] / name, {'phase': 'prepared'})
    before = copy.deepcopy(job)
    with pytest.raises(ConfigError, match='already_prepared'):
        update._refresh_waiting_configuration(target, parent, job, save)
    assert job == before


def test_snapshot_rejects_identity_change_and_does_not_materialize(host):
    target, parent, _, _ = host
    before = {p.relative_to(target['root']).as_posix(): p.read_bytes()
              for p in target['root'].rglob('*') if p.is_file()}
    with pytest.raises(ConfigError, match='binding_changed'):
        update._configuration_snapshot(target, {**parent, 'generation': str(uuid.uuid4())})
    assert update._configuration_snapshot(target, parent)['rooms'] == ['room1']
    after = {p.relative_to(target['root']).as_posix(): p.read_bytes()
             for p in target['root'].rglob('*') if p.is_file()}
    assert before == after


def test_changing_during_read_defers_the_snapshot(host, monkeypatch):
    target, parent, _, _ = host
    snapshots = iter([{'cfg': 'a'}, {'cfg': 'b'}])
    monkeypatch.setattr(update, '_config_hashes', lambda root: next(snapshots))
    assert update._configuration_snapshot(target, parent) is None


def test_clean_shutdown_can_preserve_the_last_saved_player_preferences(host):
    target, parent, job, save = host
    job.update(phase='committing', commit_admitted_at=100)
    saved = change_settings(target['root'], sr_min=2.5)
    update._refresh_closed_configuration(target, parent, job, save)
    assert job['configuration'] == update._config_hashes(target['root'])
    assert job['phase'] == 'committing'
    assert (target['root'] / 'config/host.json').read_bytes() == saved


@pytest.mark.parametrize('key', ['commit_started_at', 'commit_admitted_at',
                                  'services_stopped_at', 'upgrade_transition', 'startup_attempted_at'])
def test_only_a_never_admitted_settings_hold_is_retryable(key):
    job = {'phase': 'held', 'reason': 'configuration_changed_since_queue'}
    assert update.configuration_retryable(job)
    assert not update.configuration_retryable({**job, key: 123})
    assert not update.configuration_retryable({**job, 'reason': 'ownership_journal_changed_before_commit'})


def test_running_controller_step_keeps_occupied_workers_and_rechecks_new_settings(host, monkeypatch):
    target, parent, job, _ = host
    saved = change_settings(target['root'], sr_min=2.5)
    monkeypatch.setattr(update, '_binding', lambda instance: (target, {}, None, parent))
    monkeypatch.setattr(update, '_process', lambda pid: job['process'])
    monkeypatch.setattr('poolrotater_host.pilot_operations._service_state', lambda instance:
                        {'MainPID': 123, 'ActiveState': 'active'})
    def forbidden(*args, **kwargs):
        pytest.fail('changed settings must not stop services or enter empty admission in this tick')
    monkeypatch.setattr(update, '_commit', forbidden)
    monkeypatch.setattr(update, '_rows', forbidden)
    monkeypatch.setattr('poolrotater_host.pilot_operations._systemctl', forbidden)
    result = update.step('invented')
    assert result['phase'] == 'waiting_empty'
    assert read_json(target['prefix'] / update.NAME)['configuration'] == update._config_hashes(target['root'])
    assert (target['root'] / 'config/host.json').read_bytes() == saved


def test_an_old_configuration_hold_returns_to_admission_without_stopping_rooms(host, monkeypatch):
    target, parent, job, save = host
    save(phase='held', reason='configuration_changed_since_queue')
    change_settings(target['root'], sr_min=2.5)
    monkeypatch.setattr(update, '_binding', lambda instance: (target, {}, None, parent))
    monkeypatch.setattr(update, '_process', lambda pid: job['process'])
    monkeypatch.setattr('poolrotater_host.pilot_operations._service_state', lambda instance:
                        {'MainPID': 123, 'ActiveState': 'active'})
    monkeypatch.setattr(update, 'disarm', lambda instance: pytest.fail('retryable hold must retain its timer'))
    assert update.step('invented')['phase'] == 'waiting_empty'


def test_updater_observes_only_qualified_successors_as_configuration_retryable(host, monkeypatch):
    from poolrotater_host.updates import native
    target, parent, job, save = host
    save(phase='held', reason='configuration_changed_since_queue',
         after={'release': str(target['prefix'] / 'new'), 'bundle_sha256': 'b' * 64})
    backend = native.Backend('invented', None)
    monkeypatch.setattr(backend, 'binding', lambda: (target, {}, parent))
    monkeypatch.setattr(native, 'protected', lambda path: path)
    version = ['v7.3.1']
    def control(path, *args, **kwargs):
        if Path(path).name == 'release.json':return {'version': version[0]}
        if Path(path).name == update.NAME:return job
        return {}
    monkeypatch.setattr(native, '_control', control)
    operation = {'id': job['id'], 'release': {'kind': 'controller', 'artifact': {'sha256': 'b' * 64}}}
    assert backend.observe(operation)['phase'] == 'waiting_empty'
    version[0] = 'v7.2.13'
    assert backend.observe(operation)['phase'] == 'held'
    version[0] = 'v7.3.1'
    job['commit_admitted_at'] = 100
    assert backend.observe(operation)['phase'] == 'held'


class AgentProgressTests(unittest.TestCase):
    from tests import test_update_agent as fixture
    setUp = fixture.UpdateAgentTests.setUp
    setup_agent = fixture.UpdateAgentTests.setup_agent
    publish = fixture.UpdateAgentTests.publish
    serve = fixture.UpdateAgentTests.serve
    client = fixture.UpdateAgentTests.client

    def test_confirmed_native_progress_clears_the_old_generic_hold_error(self):
        agent = self.setup_agent(automatic_download=True, automatic_install=True)
        self.assertEqual(agent.step()['phase'], 'waiting_empty')
        state = agent.state()
        state.update(phase='held', error='update_installation_requires_review')
        atomic_json(agent.path, state)
        self.native.update(phase='waiting_empty')
        result = agent.step()
        self.assertEqual(result['phase'], 'waiting_empty')
        self.assertIsNone(result['error'])
        self.backend.queue.assert_called_once()
