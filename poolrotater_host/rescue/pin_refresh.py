"""Refresh a stale recovery runtime pin only across a complete verified lineage."""
from __future__ import annotations

import os
import math
from pathlib import Path
import re
import stat
import uuid

from ..layout import atomic_json, file_lease, LeaseBusy
from ..provisioning import _control, protected

_HISTORY_LIMIT = 256
_RECORD_LIMIT = 1024 * 1024
_IDENTITY_FIELDS = ('installation_id','generation','host_root','prefix','service_uid','execution')
_ACTIVE_CONTROLLER_PHASES = frozenset(('waiting_empty','locking','committing','recovering','retrying','held'))
_NO_COMMIT_FIELDS = ('commit_started_at','commit_admitted_at','services_stopped_at',
                     'startup_attempted_at','upgrade_transition','completed_at')


class _Refusal(Exception):
    def __init__(self, reason):
        self.reason = reason


def _need(value, reason='state_requires_review'):
    if not value:
        raise _Refusal(reason)


def _history(prefix, installation_id):
    from ..pilot_upgrade import _record, _transition

    directory = prefix / 'upgrade-history'
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return {}
    protected(directory)
    _need(stat.S_ISDIR(info.st_mode), 'upgrade_history_invalid')
    rows = {}
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= _HISTORY_LIMIT:
                raise _Refusal('upgrade_history_over_limit')
            match = re.fullmatch(r'([0-9a-f-]{36})\.json', entry.name)
            _need(match is not None, 'upgrade_history_invalid')
            try:
                identifier = str(uuid.UUID(match.group(1)))
            except (ValueError, AttributeError):
                raise _Refusal('upgrade_history_invalid') from None
            _need(identifier == match.group(1), 'upgrade_history_invalid')
            path = protected(directory / entry.name)
            row = _control(path, maximum=_RECORD_LIMIT)
            _need(type(row) is dict and set(row) == {'format','upgrade','transition'}
                  and type(row.get('format')) is int and row['format'] == 1,
                  'upgrade_history_invalid')
            upgrade = _record(row['upgrade'], prefix, installation_id)
            _need(upgrade['phase'] in ('committed','aborted'), 'upgrade_history_invalid')
            transition = _transition(upgrade, upgrade['phase'])
            _need(row['transition'] == transition and transition['transition_id'] == identifier,
                  'upgrade_history_invalid')
            _need(identifier not in rows, 'upgrade_history_ambiguous')
            rows[identifier] = {'upgrade':upgrade,'transition':transition}
    return rows


def _upgrade_lineage(prefix, current, old):
    from ..pilot_upgrade import NAME, _record, _transition
    from ..runtime_lifecycle import read_transition

    installation_id = current['installation_id']
    latest = _control(protected(prefix / NAME), optional=True, maximum=_RECORD_LIMIT)
    _need(latest is not None, 'upgrade_history_missing')
    latest = _record(latest, prefix, installation_id)
    _need(latest['phase'] == 'committed' and latest['after'] == current,
          'upgrade_history_current_mismatch')
    transition = _transition(latest, 'committed')
    _need(read_transition(prefix, installation_id) == transition,
          'upgrade_transition_unsettled')
    rows = _history(prefix, installation_id)
    visited = set()
    nodes = [current]
    cursor = latest
    found = old == current
    for _ in range(_HISTORY_LIMIT + 1):
        for registration in (cursor['before'],cursor['after']):
            if registration not in nodes:nodes.append(registration)
        if cursor['before'] == old:
            found = True
        predecessor = cursor.get('predecessor_transition')
        if predecessor is None:
            break
        predecessor_id = predecessor['transition_id']
        row = rows.get(predecessor_id)
        _need(row is not None and row['transition'] == predecessor,
              'upgrade_history_gap')
        _need(predecessor_id not in visited, 'upgrade_history_cycle')
        visited.add(predecessor_id)
        cursor = row['upgrade']
    else:
        raise _Refusal('upgrade_history_over_limit')
    _need(visited == set(rows), 'upgrade_history_ambiguous')
    return rows, nodes, found


def lineage_diagnostic(prefix, current, old):
    """Read-only finite diagnosis; never substitute digest equality for lineage.

    Call under the same protected installation/policy admission as pin refresh.
    Registration paths, inventories and environment details stay local. A
    matching bundle with different registration fields is diagnostic only.
    """
    from ..ownership_recovery import digest
    _need(type(current) is dict and type(old) is dict, 'recovery_policy_invalid')
    _need(all(old.get(key)==current.get(key) for key in _IDENTITY_FIELDS),
          'recovery_pin_identity_changed')
    rows,nodes,ancestor=_upgrade_lineage(prefix,current,old)
    matches=[]
    for node in nodes:
        if node.get('bundle_sha256')!=old.get('bundle_sha256'):continue
        matches.append({'exact_record':node==old,
                        'differing_fields':sorted(key for key in set(node)|set(old)
                                                  if node.get(key)!=old.get(key))})
    return {'read_only':True,'pin_refreshed':False,
            'status':'current' if old==current else 'ancestor' if ancestor else 'not-ancestor',
            'pinned_bundle_sha256':old.get('bundle_sha256'),
            'current_bundle_sha256':current.get('bundle_sha256'),
            'pinned_record_sha256':digest(old),'current_record_sha256':digest(current),
            'verified_history_count':len(rows),'matching_bundle_records':matches,
            'lineage_qualified':ancestor}


def _terminal_controller_job(prefix, current, nodes):
    job = _control(prefix / 'controller-update.json', optional=True, maximum=_RECORD_LIMIT)
    if job is None:
        return
    _need(type(job) is dict and type(job.get('format')) is int and job['format'] == 1,
          'controller_job_invalid')
    _need(type(job.get('id')) is str and str(uuid.UUID(job['id'])) == job['id'],
          'controller_job_invalid')
    if job.get('phase') not in ('complete','cancelled'):
        if job.get('phase') in _ACTIVE_CONTROLLER_PHASES:
            raise _Refusal('controller_job_active')
        raise _Refusal('controller_job_invalid')
    before, after = job.get('before'), job.get('after')
    _need(type(before) is dict and type(after) is dict, 'controller_job_invalid')
    for registration in (before, after):
        _need(all(registration.get(key) == current.get(key) for key in _IDENTITY_FIELDS),
              'controller_job_foreign')
    if job['phase'] == 'complete':
        terminal = after
        completed=job.get('completed_at')
        _need(type(completed) in (int,float) and math.isfinite(completed) and completed>0,
              'controller_job_unsettled')
    else:
        terminal = before
        _need(all(job.get(key) is None for key in _NO_COMMIT_FIELDS),
              'controller_job_unsettled')
    if job['phase'] == 'cancelled':
        if job.get('controller_update_drain') is not None:
            _need(job.get('drain_restoration_phase') == 'complete',
                  'controller_drain_unsettled')
        else:
            _need(job.get('drain_restoration_phase') in (None,'complete'),
                  'controller_drain_unsettled')
    _need(terminal in nodes, 'controller_job_lineage_mismatch')


def _protected_bytes(path, *, maximum=131072):
    path = protected(Path(path))
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum,
          'recovery_launcher_invalid')
    return path.read_bytes()


def _refresh_locked(instance, target, marker, current, policy_path):
    from ..runtime_lifecycle import validate_runtime, require_no_pending

    prefix = target['prefix']
    config = _control(protected(policy_path), private=True, maximum=512 * 1024)
    _need(type(config) is dict and type(config.get('format')) is int and config['format'] == 1
          and type(config.get('binding')) is dict and type(config.get('record')) is dict
          and type(config.get('enabled')) is bool and type(config.get('allow_recovery')) is bool,
          'recovery_policy_invalid')
    old = config['record']
    _need(all(old.get(key) == current.get(key) for key in
              ('installation_id','generation','host_root','prefix','service_uid','execution'))
          and old.get('execution') == 'pilot-live', 'recovery_pin_identity_changed')
    _need(config['binding'].get('installation_id') == marker['installation_id']
          and config['binding'].get('generation') == marker['generation']
          and type(config['binding'].get('host_id')) is str
          and bool(config['binding']['host_id']), 'recovery_pin_binding_changed')
    require_no_pending(prefix, marker['installation_id'])
    if old == current:
        return {'status':'current','pin_refreshed':False}
    validate_runtime(old, prefix)

    _rows, nodes, ancestor = _upgrade_lineage(prefix, current, old)
    _need(ancestor, 'recovery_pin_not_ancestor')
    _terminal_controller_job(prefix, current, nodes)

    from ..controller_update import GATE
    _need(not os.path.lexists(target['root'] / GATE), 'native_transition_unsettled')
    launcher = _protected_bytes(prefix / 'recovery-bootstrap.py')
    old_code = _protected_bytes(Path(old['release']) / 'poolrotater_host/recovery_bootstrap.py')
    new_code = _protected_bytes(Path(current['release']) / 'poolrotater_host/recovery_bootstrap.py')
    _need(launcher == old_code == new_code, 'recovery_launcher_changed')

    refreshed = {**config,'record':current}
    from ..layout import atomic_json
    atomic_json(policy_path, refreshed, mode=0o600)
    check = _control(protected(policy_path), private=True, maximum=512 * 1024)
    _need(check == refreshed, 'recovery_pin_write_unconfirmed')
    return {'status':'refreshed','pin_refreshed':True}


def refresh_pin(instance, record):
    """Atomically rebind only the recovery configuration after proven upgrade."""
    if os.geteuid() != 0:
        return {'status':'refused','reason':'administrator_required','pin_refreshed':False}
    if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') == '1':
        return {'status':'refused','reason':'offline_fixture_refused','pin_refreshed':False}
    try:
        from ..pilot_installation import paths, _administrative_binding
        target = paths(instance)
        prefix = target['prefix']
        recovery = prefix / 'recovery'
        if not os.path.lexists(recovery):
            return {'status':'absent','pin_refreshed':False}
        try: protected(recovery)
        except Exception:
            return {'status':'refused','reason':'recovery_directory_requires_review',
                    'pin_refreshed':False}
        policy_path = recovery / 'configuration.json'
        if not os.path.lexists(policy_path):
            return {'status':'absent','pin_refreshed':False}
        lock_path = recovery / 'policy.lock'
        lock = protected(lock_path)
        lock_info = lock.lstat()
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or lock_info.st_uid != 0 or lock_info.st_mode & 0o077):
            return {'status':'refused','reason':'policy_lock_invalid','pin_refreshed':False}
        try:
            # Direct stopped installer/upgrade paths serialize on this shared
            # prefix-parent lock rather than deployment-admission.lock.
            with file_lease(prefix.parent/'.install.lock',timeout=0):
                try:
                    with file_lease(lock_path, timeout=0):
                        target, marker, current = _administrative_binding(instance, execution='pilot-live')
                        if record != current:
                            return {'status':'deferred','reason':'registration_changed','pin_refreshed':False}
                        # Re-read mutable transition/job witnesses only while serialized
                        # with any other recovery policy edit.
                        try:
                            result = _refresh_locked(instance,target,marker,current,policy_path)
                        except _Refusal as error:
                            if error.reason in ('controller_job_active','controller_drain_unsettled',
                                                'native_transition_unsettled'):
                                return {'status':'deferred','reason':error.reason,'pin_refreshed':False}
                            return {'status':'refused','reason':error.reason,'pin_refreshed':False}
                        return result
                except LeaseBusy:
                    return {'status':'deferred','reason':'policy_lock_busy','pin_refreshed':False}
        except LeaseBusy:
            return {'status':'deferred','reason':'install_in_progress','pin_refreshed':False}
    except Exception:
        return {'status':'refused','reason':'state_requires_review','pin_refreshed':False}
