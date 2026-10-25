"""Explicit stopped pilot repair upgrade; preserve all operator-owned data.

The new pinned installer supplies this administrator implementation. No account
or room request occurs. A runtime transition fences old bootstraps and a paired
upgrade journal fences the new bootstrap across partial writes and explicit resume.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

from .config import ConfigError
from .layout import file_lease
from .pilot_installation import (_admin, paths, _account, _quiescent,
                                  require_no_service_processes, RESERVE)
from .provisioning import (_control, _host_identity, _pointer, _provision,
                           _write, protected)
from .runtime_lifecycle import (REGISTRATION_KEYS, validate_runtime, read_transition,
                                transition_path)

NAME = 'pilot-upgrade.json'
FIELDS = {'format', 'phase', 'transition_id', 'before', 'after', 'intent_before',
          'intent_after', 'bootstrap_before', 'bootstrap_after', 'unit_before', 'unit_after'}
COORDINATED_SHARED = frozenset(('poolrotater_host/__init__.py',
    'poolrotater_host/runtime_config.py', 'poolrotater_host/provisioning.py',
    'poolrotater_host/runtime_lifecycle.py', 'poolrotater_host/service_bootstrap.py',
    'rolling/supervisor.py'))
CHAT_ACCESS_BROKER_REPAIR = (
    '2b5f1bc54a24b1967cd60e7517bf7e50367caeb0835911a628f4bc414f2a9f19',
    'ebb0b9190b3cc256873446379bdb3e0ab341e773fe361e2dfa011bdc2eabdf13',
)
CHAT_ACCESS_SHARED_REPAIRS = {
    'rolling/broker.py': CHAT_ACCESS_BROKER_REPAIR,
    'poolrotater_host/oauth.py': (
        '057557b712655c0df54be148356753c4ad62db5dfdd3904683b2eb6703be4ece',
        '64972f4a092e4ad5b6c686d00fd147e1c14c7d2a7462650f9b480b2a91ac3b30',
    ),
    'rolling/state.py': (
        '28cfbc256048954c2dc6f42009f67e251e2228ab2e2db09522e33df508c62bc2',
        '88b23d4f1fb76928d91015e576595a7a60f4d9a914a41d9a349fbf1a01e0d8aa',
    ),
}


def require_repair_compatible(before, after):
    """Stopped supervisor repair, distinct from occupied rolling promotion.

    Only this batch's explicit host/bootstrap components may change their shared
    fingerprint. Storage, protocol, interpreter and dependency contracts remain
    identical; neither path executes migrations. Full inventories are verified
    independently before this check.
    """
    v71 = after.get('version')=='v7.1.0' and before.get('version') in ('v7.0.6','v7.0.7','v7.0.8')
    if not v71 and (before.get('version'), after.get('version')) not in (('v7.3.0', 'v7.3.3'),('v7.3.1', 'v7.3.3'),('v7.3.2', 'v7.3.3'),('v7.2.12', 'v7.3.2'),('v7.2.13', 'v7.3.2'),('v7.2.14', 'v7.3.2'),('v7.3.0', 'v7.3.2'),('v7.3.1', 'v7.3.2'),('v7.2.12', 'v7.3.1'),('v7.2.13', 'v7.3.1'),('v7.2.14', 'v7.3.1'),('v7.3.0', 'v7.3.1'),('v7.2.14', 'v7.3.0'),('v7.2.13', 'v7.2.14'),('v7.3.0', 'v7.3.0'),('v7.2.12', 'v7.3.0'),('v7.2.13', 'v7.3.0'),('v7.2.12', 'v7.2.13'),('v7.2.6', 'v7.2.12'),('v7.2.7', 'v7.2.12'),('v7.2.8', 'v7.2.12'),('v7.2.9', 'v7.2.12'),('v7.2.10', 'v7.2.12'),('v7.2.11', 'v7.2.12'),('v7.2.6','v7.2.11'),('v7.2.7','v7.2.11'),('v7.2.8','v7.2.11'),('v7.2.9','v7.2.11'),('v7.2.10','v7.2.11'),('v7.2.6','v7.2.10'),('v7.2.7','v7.2.10'),('v7.2.8','v7.2.10'),('v7.2.9','v7.2.10'),('v7.2.6','v7.2.9'),('v7.2.7','v7.2.9'),('v7.2.8','v7.2.9'),('v7.2.6','v7.2.8'),('v7.2.8','v7.2.6'),('v7.2.7','v7.2.8'),('v7.2.8','v7.2.7'),('v7.2.6','v7.2.7'),('v7.2.7','v7.2.6'),('v7.2.5','v7.2.6'),('v7.2.6','v7.2.5'),('v7.2.3','v7.2.5'),('v7.2.5','v7.2.3'),('v7.2.4','v7.2.5'),('v7.2.5','v7.2.4'),('v7.2.3','v7.2.4'),('v7.2.4','v7.2.3'),('v7.2.2','v7.2.3'),('v7.2.3','v7.2.2'),('v7.2.1','v7.2.2'),('v7.2.2','v7.2.1'),('v7.1.20','v7.2.1'),('v7.2.0','v7.2.1'),('v7.2.1','v7.2.0'),('v7.2.1','v7.1.20'),('v7.1.20','v7.2.0'),('v7.2.0','v7.1.20'),('v7.1.16','v7.1.19'),('v7.1.17','v7.1.19'),('v7.1.11','v7.1.17'),('v7.1.15','v7.1.17'),('v7.1.16','v7.1.17'),('v7.1.13','v7.1.16'),
            ('v7.0.1','v7.0.1'), ('v7.0.1','v7.0.2'), ('v7.0.2','v7.0.2'),
            ('v7.0.2','v7.0.5'), ('v7.0.5','v7.0.5'), ('v7.0.1','v7.0.6'),
            ('v7.0.5','v7.0.6'), ('v7.0.6','v7.0.6'), ('v7.1.0','v7.1.1'),
            ('v7.1.0','v7.1.2'), ('v7.1.1','v7.1.2'),
            ('v7.1.2','v7.1.4'), ('v7.1.3','v7.1.4'),
            ('v7.1.1','v7.1.5'), ('v7.1.2','v7.1.5'),
            ('v7.1.3','v7.1.5'), ('v7.1.4','v7.1.5'),
            ('v7.1.1', 'v7.1.6'),
            ('v7.1.2', 'v7.1.6'),
            ('v7.1.3', 'v7.1.6'),
            ('v7.1.4', 'v7.1.6'),
            ('v7.1.5', 'v7.1.6'),
            ('v7.1.1', 'v7.1.7'),
            ('v7.1.2', 'v7.1.7'),
            ('v7.1.3', 'v7.1.7'),
            ('v7.1.4', 'v7.1.7'),
            ('v7.1.5', 'v7.1.7'),
            ('v7.1.6', 'v7.1.7'),
            ('v7.1.1', 'v7.1.8'),
            ('v7.1.2', 'v7.1.8'),
            ('v7.1.3', 'v7.1.8'),
            ('v7.1.4', 'v7.1.8'),
            ('v7.1.5', 'v7.1.8'),
            ('v7.1.6', 'v7.1.8'),
            ('v7.1.7', 'v7.1.8'),
            ('v7.1.1', 'v7.1.9'),
            ('v7.1.2', 'v7.1.9'),
            ('v7.1.3', 'v7.1.9'),
            ('v7.1.4', 'v7.1.9'),
            ('v7.1.5', 'v7.1.9'),
            ('v7.1.6', 'v7.1.9'),
            ('v7.1.7', 'v7.1.9'),
            ('v7.1.8', 'v7.1.9'),
            ('v7.1.1', 'v7.1.10'),
            ('v7.1.2', 'v7.1.10'),
            ('v7.1.3', 'v7.1.10'),
            ('v7.1.4', 'v7.1.10'),
            ('v7.1.5', 'v7.1.10'),
            ('v7.1.6', 'v7.1.10'),
            ('v7.1.7', 'v7.1.10'),
            ('v7.1.8', 'v7.1.10'),
            ('v7.1.9', 'v7.1.10'), ('v7.1.10','v7.1.11'), ('v7.1.11','v7.1.12'), ('v7.1.12','v7.1.13'), ('v7.1.13','v7.1.14'), ('v7.1.14','v7.1.15'), ('v7.1.11','v7.1.15')):
        raise ConfigError('pilot repair requires its reviewed runtime family')
    for key in ('protocol', 'storage_epoch', 'requirements', 'python_hash', 'environment_hash'):
        if key not in before or before[key] != after.get(key):
            raise ConfigError('pilot repair persistent/runtime contract changed: ' + key)
    if before.get('migrations') != [] or after.get('migrations') != []:
        raise ConfigError('pilot repair cannot execute migrations')
    if (type(before.get('shared')) is not dict or type(after.get('shared')) is not dict):
        raise ConfigError('pilot repair shared inventory changed')
    from .integrated_upgrade import admits
    if admits(before, after):
        # Exact inventory pairs are generated only in the reviewed frozen
        # artifact. The operator still supplies both trusted runtime digests;
        # _locked requires stopped services, no children and clean ownership.
        # M4 ledger preparation remains a separate explicit stopped operation.
        return
    if v71:
        raise ConfigError('v7.1 requires an exact reviewed stopped migration transition')
    if before.get('version') != after.get('version'):
        raise ConfigError('new controller family requires an exact reviewed transition')
    if before['shared'].keys() != after['shared'].keys():
        raise ConfigError('pilot repair shared inventory changed')
    if 'storage' not in before or before['storage'] != after.get('storage'):
        raise ConfigError('pilot repair persistent/runtime contract changed: storage')
    for key in before['shared']:
        # Only the exact published -> reviewed chat guard / OAuth diagnostics
        # are accepted here. Do not authorize arbitrary shared code changes or
        # relax occupied rolling compatibility. _locked requires stopped workers.
        if key in CHAT_ACCESS_SHARED_REPAIRS and (before['shared'][key], after['shared'][key]) == CHAT_ACCESS_SHARED_REPAIRS[key]:
            continue
        if key not in COORDINATED_SHARED and before['shared'][key] != after['shared'][key]:
            raise ConfigError('pilot repair unreviewed shared component changed')


def _code(path):
    protected(path)
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 131072:
        raise ConfigError('invalid upgrade code file')
    return path.read_bytes()


def _record(value, prefix, installation_id):
    try:
        if (type(value) is not dict or set(value) not in (FIELDS, FIELDS | {'predecessor_transition'}) or value['format'] != 1
                or type(value['format']) is not int
                or value['phase'] not in ('prepared', 'committed', 'aborted')
                or str(uuid.UUID(value['transition_id'])) != value['transition_id']): raise ValueError
        before, after = value['before'], value['after']
        for runtime in (before, after):
            if (type(runtime) is not dict or set(runtime) != REGISTRATION_KEYS
                    or runtime['installation_id'] != installation_id
                    or runtime['prefix'] != str(prefix) or runtime['execution'] != 'pilot-live'): raise ValueError
        for key in ('installation_id', 'generation', 'prefix', 'execution', 'service_uid', 'host_root'):
            if before[key] != after[key]: raise ValueError
        for side in ('before', 'after'):
            intent = value['intent_' + side]
            runtime = value[side]
            if (type(intent) is not dict or intent['execution'] != 'pilot-live'
                    or any(intent[k] != runtime[k] for k in ('installation_id', 'generation', 'host_root', 'service_uid', 'bundle_sha256'))): raise ValueError
            raw = bytes.fromhex(value['bootstrap_' + side])
            if not raw or len(raw) > 131072: raise ValueError
            unit = value['unit_' + side]
            if unit is not None and (type(unit) is not str or len(unit) > 16384): raise ValueError
        expected_intent = {**value['intent_before'], 'bundle_sha256': after['bundle_sha256']}
        if value['intent_after'] != expected_intent: raise ValueError
        previous = value.get('predecessor_transition')
        if previous is not None:
            if (type(previous) is not dict or set(previous) != {
                    'format','installation_id','generation','transition_id','operation','phase','before','after'}
                    or type(previous['format']) is not int or previous['format'] != 1
                    or previous['phase'] not in ('committed','aborted')
                    or previous['operation'] not in ('activate','rollback')
                    or previous['installation_id'] != installation_id
                    or previous['generation'] != before['generation']
                    or previous['transition_id'] == value['transition_id']
                    or previous['after' if previous['phase']=='committed' else 'before'] != before):
                raise ValueError
            if str(uuid.UUID(previous['transition_id'])) != previous['transition_id']: raise ValueError
        return value
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ConfigError('invalid or foreign pilot upgrade record') from None


def require_settled(prefix, installation_id):
    path = Path(prefix) / NAME
    value = _control(path, optional=True, maximum=512 * 1024)
    if value is None: return
    _record(value, Path(prefix), installation_id)
    if value['phase'] == 'prepared':
        raise ConfigError('pilot upgrade requires explicit complete or abort')


def _unit_state(target):
    from .pilot_operations import _service_state
    state = _service_state(target['root'].name)
    if (state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed')
            or state['UnitFileState'] not in ('', 'disabled', 'not-found')):
        raise ConfigError('pilot upgrade requires a disabled/stopped dedicated unit')


def _unit_bytes(target):
    from .pilot_operations import UNIT_BASE, unit_text
    path = UNIT_BASE / target['unit']
    if not os.path.lexists(path): return None
    text = _code(path).decode('ascii')
    new = unit_text(target['root'].name)
    environment = f"EnvironmentFile=-{target['root']}/secrets/integrations.env\n"
    bases = {new}
    if target['root'].name == 'owner':
        bases.add(new.replace('Nice=0\nMemoryMax=infinity\nMemorySwapMax=infinity\nTasksMax=256',
                              'Nice=15\nMemoryMax=1G\nMemorySwapMax=512M\nTasksMax=128'))
    approved = {body.replace('SendSIGKILL=yes\n', kill).replace(environment, env)
                for body in bases for kill in ('SendSIGKILL=yes\n', 'SendSIGKILL=no\n')
                for env in (environment, '')}
    if text not in approved:
        raise ConfigError('unrecognized pilot unit cannot be upgraded')
    return text


def _binding(instance):
    target = paths(instance); prefix = protected(target['prefix'])
    account = _account(target['user'], target['root'], create=False)
    marker = _host_identity(target['root'], account.pw_uid)
    pointer = _pointer(marker, prefix)
    if _control(target['root'] / 'state/runtime-installation.json', account.pw_uid, private=True) != pointer:
        raise ConfigError('pilot upgrade pointer changed')
    path = prefix / 'installations' / (marker['installation_id'] + '.json')
    record = _control(protected(path))
    if any(record.get(k) != v for k, v in {**pointer, 'host_root':str(target['root']),
                  'service_uid':account.pw_uid, 'execution':'pilot-live'}.items()):
        raise ConfigError('pilot upgrade requires the existing live-pilot identity')
    return target, marker, path, record


@contextmanager
def _admission_guard(root, uid, *, seconds=20, monotonic=time.monotonic, sleep=time.sleep):
    """Wait only before admission; never replay a yielded upgrade transaction.

    Independent read-only helpers can briefly assume the dedicated UID after a
    controller has closed. Retain the actual owner lease and process census;
    only positively identified cleanup contention is eligible for a bounded wait.
    """
    from .pilot_installation import CleanupPending
    deadline = monotonic() + seconds
    while True:
        admitted = False
        try:
            with _quiescent(root, uid):
                require_no_service_processes(uid)
                admitted = True
                yield
            return
        except CleanupPending as error:
            if admitted or error.code not in ('update_owner_lock_busy', 'update_children_still_running'):
                raise
            if monotonic() >= deadline:
                raise ConfigError('update_cleanup_timeout') from error
            sleep(min(.5, max(0, deadline - monotonic())))


@contextmanager
def _locked(instance):
    _admin()
    target, marker, path, current = _binding(instance)
    _unit_state(target)
    with file_lease(target['prefix'].parent / '.install.lock'), _admission_guard(target['root'], current['service_uid']):
        target, marker, path, current = _binding(instance)
        _unit_state(target)
        # A stopped controller can still own an awaiting-arrival reservation.
        # Root inspection must use the dedicated identity required by its
        # private ledger; an absent feature remains entirely inert.
        from .ratings.operations import _identity
        from .bookings.policy import holds_updates
        with _identity(current['service_uid']):
            if holds_updates(target['root']):
                raise ConfigError('private booking blocks stopped upgrade')
        yield target, marker, path, current


def _replace_code(path, raw):
    """Only checked root code/unit paths. Replace/fsync without editing in place."""
    _code(path); protected(path.parent)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.prv7-upgrade-', delete=False) as stream:
            name = Path(stream.name); stream.write(raw); os.fchmod(stream.fileno(), 0o444); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if name is not None: name.unlink(missing_ok=True)


def _bootstrap_record(raw): return {'format':1, 'sha256':hashlib.sha256(raw).hexdigest()}


def _transition(value, phase):
    return {'format':1, 'installation_id':value['before']['installation_id'],
            'generation':value['before']['generation'], 'transition_id':value['transition_id'],
            'operation':'activate', 'phase':phase, 'before':value['before'], 'after':value['after']}


def _archive_previous(prefix, installation_id, value, transition, current):
    """Retain the exact terminal pair before replacing the active upgrade ledger.

    An interrupted archive leaves the old ledger authoritative. Existing history
    must match byte-for-byte data; never overwrite a conflicting receipt.
    """
    if value is None: return
    _record(value, prefix, installation_id)
    if (value['phase'] not in ('committed', 'aborted')
            or transition != _transition(value, value['phase'])
            or current != value['after' if value['phase'] == 'committed' else 'before']):
        raise ConfigError('pilot upgrade previous journal and transition differ')
    parent = prefix / 'upgrade-history'
    parent.mkdir(mode=0o755, exist_ok=True)
    protected(parent)
    parent.chmod(0o755)  # Closed launch retirement reads receipts as the service UID.
    path = parent / (value['transition_id'] + '.json')
    receipt = {'format':1, 'upgrade':value, 'transition':transition}
    previous = _control(path, optional=True, maximum=1024*1024)
    if previous is not None and previous != receipt:
        raise ConfigError('pilot upgrade retained history changed')
    if previous is None: _write(path, receipt)


def _finish(target, marker, path, value, decision):
    prefix = target['prefix']
    _record(value, prefix, marker['installation_id'])
    chosen_phase = 'committed' if decision == 'complete' else 'aborted'
    if value['phase'] not in ('prepared', chosen_phase):
        raise ConfigError('upgrade decision differs from durable result')
    current = _control(path)
    if current not in (value['before'], value['after']): raise ConfigError('upgrade registration changed')
    intent = _control(prefix / 'pilot-install.json')
    if intent not in (value['intent_before'], value['intent_after']): raise ConfigError('upgrade intent changed')
    transition = read_transition(prefix, marker['installation_id'])
    predecessor = value.get('predecessor_transition')
    if transition is not None and transition not in (_transition(value, 'prepared'), _transition(value, chosen_phase), predecessor):
        raise ConfigError('upgrade transition changed')
    if transition == predecessor and transition is not None and (current != value['before'] or value['phase'] != 'prepared'):
        raise ConfigError('upgrade predecessor differs from registered starting point')
    if transition is None and (current != value['before'] or value['phase'] != 'prepared'):
        raise ConfigError('upgrade transition missing')
    raw = _code(prefix / 'bootstrap.py')
    choices = [bytes.fromhex(value['bootstrap_' + side]) for side in ('before','after')]
    if raw not in choices or _control(prefix / 'bootstrap.json') not in [_bootstrap_record(r) for r in choices]:
        raise ConfigError('upgrade bootstrap changed')
    from .pilot_operations import UNIT_BASE, _systemctl
    unit_path = UNIT_BASE / target['unit']
    actual_unit = _code(unit_path).decode('ascii') if os.path.lexists(unit_path) else None
    if actual_unit not in (value['unit_before'], value['unit_after']): raise ConfigError('upgrade unit changed')
    manifests = {}
    for side in ('before','after'):
        runtime = validate_runtime(value[side], prefix)
        manifests[side] = runtime['manifest']
        candidate_raw = _code(runtime['release'] / 'poolrotater_host/service_bootstrap.py')
        if candidate_raw != bytes.fromhex(value['bootstrap_' + side]):
            raise ConfigError('upgrade bootstrap differs from frozen release')
    from .updates.approval import require_upgrade
    require_upgrade(manifests['before'], manifests['after'], value['before'], value['after'], recovering=True)
    if decision == 'complete':
        from .catch_rollback import require_target
        require_target(target['root'],prefix,value['before']['service_uid'],manifests['before'],manifests['after'])
    if _control(prefix / NAME, maximum=512*1024) != value:
        raise ConfigError('upgrade journal changed')
    if transition is None or (predecessor is not None and transition == predecessor):
        _write(transition_path(prefix, marker['installation_id']), _transition(value, 'prepared'))
    side = 'after' if decision == 'complete' else 'before'
    if decision == 'complete':
        from .runtime_defaults import prepare as prepare_defaults
        prepare_defaults(target['root'],marker,uid=value['before']['service_uid'],
                         previous_release=value['before']['release'])
        from .managed_host.policy import prepare_data
        prepare_data(target['root'],marker,uid=value['before']['service_uid'],owner_lease_held=True)
        if manifests['after'].get('version')=='v7.1.0':
            from .activity_migration import prepare as prepare_activity
            prepare_activity(target['root'],marker,uid=value['before']['service_uid'],owner_lease_held=True)
        from .worker_deployment import retire_after_controller_upgrade
        retire_after_controller_upgrade(target, value['before'])
    desired_raw = bytes.fromhex(value['bootstrap_' + side])
    if raw != desired_raw: _replace_code(prefix / 'bootstrap.py', desired_raw)
    _write(prefix / 'bootstrap.json', _bootstrap_record(desired_raw))
    _write(path, value[side])
    _write(prefix / 'pilot-install.json', value['intent_' + side])
    desired_unit = value['unit_' + side]
    if desired_unit != actual_unit:
        if desired_unit is None: raise ConfigError('upgrade cannot remove an unexpected unit')
        _replace_code(unit_path, desired_unit.encode('ascii'))
    if desired_unit is not None: _systemctl('daemon-reload')
    _unit_state(target)
    if decision == 'complete':
        # The new bootstrap checks the upgrade fence until the final write.
        _write(transition_path(prefix, marker['installation_id']), _transition(value, chosen_phase))
        _write(prefix / NAME, {**value, 'phase':chosen_phase})
        from .managed_host.policy import prepare as prepare_managed
        prepare_managed(target['root'],marker,uid=value['after']['service_uid'])
        if os.path.lexists(prefix/'updates/configuration.json'):
            from .managed_host.operations import ensure as ensure_managed
            ensure_managed(target['root'].name,value['after'])
    else:
        # The old bootstrap knows the transition fence; release it last.
        _write(prefix / NAME, {**value, 'phase':chosen_phase})
        _write(transition_path(prefix, marker['installation_id']), _transition(value, chosen_phase))
    return {'ok':True, 'status':'upgraded' if decision == 'complete' else 'upgrade_aborted',
            'transition_id':value['transition_id'], 'release':value[side]['release'],
            'bundle_sha256':value[side]['bundle_sha256'], 'service_started':False,
            'operator_state_preserved':True, 'ownership_recovery_required_may_remain':True,
            'live_ready':False}


def upgrade(instance, bundle, expected_sha256, *, from_sha256, recover=None, transaction_id=None):
    for sha in (expected_sha256, from_sha256):
        if type(sha) is not str or not re.fullmatch('[a-f0-9]{64}', sha):
            raise ConfigError('explicit old and new runtime digests required')
    if expected_sha256 == from_sha256: raise ConfigError('upgrade requires a distinct replacement')
    if (recover not in (None, 'status', 'complete', 'abort')
            or ((recover in ('complete', 'abort')) != (transaction_id is not None))):
        raise ConfigError('explicit upgrade recovery decision and transaction required')
    with _locked(instance) as (target, marker, path, current):
        prefix = target['prefix']
        value = _control(prefix / NAME, optional=True, maximum=512*1024)
        if value is not None: _record(value, prefix, marker['installation_id'])
        if recover is not None:
            if value is None: raise ConfigError('pilot upgrade transaction missing')
            _record(value, prefix, marker['installation_id'])
            if ((recover != 'status' and value['transition_id'] != transaction_id) or value['before']['bundle_sha256'] != from_sha256
                    or value['after']['bundle_sha256'] != expected_sha256):
                raise ConfigError('pilot upgrade recovery transaction differs')
            if recover == 'status':
                transition = read_transition(prefix, marker['installation_id'])
                return {'ok':True, 'phase':value['phase'], 'transition_id':value['transition_id'],
                        'runtime_transition_phase':transition['phase'] if transition else 'absent',
                        'service_started':False, 'live_ready':False}
            from .runtime_package import inspect
            inspect(bundle, expected_sha256, execution='pilot-live')
            with file_lease(prefix / '.provision.lock'):
                return _finish(target, marker, path, value, recover)
        from .runtime_lifecycle import require_no_pending
        require_no_pending(prefix, marker['installation_id'])
        if (value is not None and value['phase'] == 'committed'
                and value['before']['bundle_sha256'] == from_sha256
                and value['after']['bundle_sha256'] == expected_sha256 and current == value['after']):
            with file_lease(prefix / '.provision.lock'):
                result = _finish(target, marker, path, value, 'complete')
            return {**result, 'status':'already_upgraded'}
        if current['bundle_sha256'] != from_sha256:
            raise ConfigError('pilot upgrade original runtime digest differs')
        validate_runtime(current, prefix)
        intent = _control(prefix / 'pilot-install.json')
        if any(intent.get(k) != current[k] for k in ('installation_id','generation','host_root','service_uid','execution','bundle_sha256')):
            raise ConfigError('pilot upgrade original intent differs')
        original_bootstrap = _code(prefix / 'bootstrap.py')
        if (_control(prefix / 'bootstrap.json') != _bootstrap_record(original_bootstrap)
                or original_bootstrap != _code(Path(current['release']) / 'poolrotater_host/service_bootstrap.py')):
            raise ConfigError('pilot upgrade original bootstrap differs')
        original_unit = _unit_bytes(target)
        predecessor = read_transition(prefix, marker['installation_id'])
        value_before_staging = value
        if predecessor is not None and (
                predecessor['phase'] not in ('committed','aborted')
                or predecessor['after' if predecessor['phase']=='committed' else 'before'] != current):
            raise ConfigError('pilot upgrade previous transition differs from registered runtime')
        staged = _provision(target['root'], prefix, Path(bundle), expected_sha256,
                            uid=current['service_uid'], execution='pilot-live', reserve=RESERVE, stage_bootstrap=True)
        if staged['status'] != 'candidate_staged': raise ConfigError('upgrade candidate was not staged')
        after = _control(prefix / 'candidates' / marker['installation_id'] / (expected_sha256 + '.json'))
        new = validate_runtime(after, prefix)
        from .updates.approval import require_upgrade
        before_manifest=validate_runtime(current,prefix)['manifest']
        require_upgrade(before_manifest,new['manifest'],current,after)
        from .catch_rollback import require_target
        require_target(target['root'],prefix,current['service_uid'],before_manifest,new['manifest'])
        replacement = _code(new['release'] / 'poolrotater_host/service_bootstrap.py')
        from .pilot_operations import unit_text
        value = {'format':1, 'phase':'prepared', 'transition_id':str(uuid.uuid4()),
                 'before':current, 'after':after, 'intent_before':intent,
                 'intent_after':{**intent, 'bundle_sha256':expected_sha256},
                 'bootstrap_before':original_bootstrap.hex(), 'bootstrap_after':replacement.hex(),
                 'unit_before':original_unit,
                 'unit_after':unit_text(instance) if original_unit is not None else None}
        if predecessor is not None: value['predecessor_transition'] = predecessor
        _record(value, prefix, marker['installation_id'])
        with file_lease(prefix / '.provision.lock'):
            if _control(path) != current: raise ConfigError('upgrade runtime changed during staging')
            previous_value = _control(prefix / NAME, optional=True, maximum=512*1024)
            if previous_value != value_before_staging:
                raise ConfigError('pilot upgrade predecessor journal changed during staging')
            if read_transition(prefix, marker['installation_id']) != predecessor:
                raise ConfigError('pilot upgrade predecessor transition changed during staging')
            _archive_previous(prefix, marker['installation_id'], previous_value, predecessor, current)
            _write(prefix / NAME, value)
            return _finish(target, marker, path, value, 'complete')
