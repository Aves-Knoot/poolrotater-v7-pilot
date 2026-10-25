"""Pause only this installation's independent timers during an admitted switch.

Timers are not disabled, and an executing updater service is never stopped.
The durable original active states survive interrupted attempts. Native owner
leases and the service-UID process census remain authoritative throughout.
"""
from contextlib import contextmanager
import hashlib
import logging
import math
import os
from pathlib import Path
import re
import time
import uuid

from .config import ConfigError


_ADMITTED_UPDATE_PHASES = frozenset(('committing', 'recovering', 'retrying', 'held'))
_KNOWN_UPDATE_PHASES = _ADMITTED_UPDATE_PHASES | frozenset(
    ('waiting_empty', 'locking', 'held', 'complete', 'cancelled'))
_UPDATE_RUNTIME_FIELDS = ('installation_id', 'generation', 'prefix', 'host_root',
                          'service_uid', 'bundle_sha256')


def _valid_uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _valid_runtime_binding(value):
    if type(value) is not dict or any(key not in value for key in _UPDATE_RUNTIME_FIELDS):
        return False
    return (_valid_uuid(value['installation_id']) and _valid_uuid(value['generation'])
            and type(value['prefix']) is str and Path(value['prefix']).is_absolute()
            and '..' not in Path(value['prefix']).parts
            and Path(value['prefix']).as_posix() == value['prefix']
            and type(value['host_root']) is str and Path(value['host_root']).is_absolute()
            and '..' not in Path(value['host_root']).parts
            and Path(value['host_root']).as_posix() == value['host_root']
            and type(value['service_uid']) is int and value['service_uid'] > 0
            and type(value['bundle_sha256']) is str
            and re.fullmatch(r'[a-f0-9]{64}', value['bundle_sha256']) is not None)


def managed_update_in_progress(instance, target, record, marker):
    """Return true only for this installation's admitted successor transaction.

    This gates managed timer activation only. The native root-owned job remains
    the authority for updates, and all stopped-owner/process checks still run.
    """
    from .provisioning import _control, protected

    path = target['prefix'] / 'controller-update.json'
    if not os.path.lexists(path):
        return False
    value = _control(protected(path), maximum=2 * 1024 * 1024)
    if type(value) is not dict:
        raise ConfigError('update_guard_requires_review')
    phase = value.get('phase')
    if type(phase) is not str:
        raise ConfigError('update_guard_requires_review')
    if phase not in _KNOWN_UPDATE_PHASES:
        raise ConfigError('update_guard_requires_review')
    if phase == 'held' and 'commit_admitted_at' not in value:
        return False
    if phase not in _ADMITTED_UPDATE_PHASES:
        return False
    if (type(value.get('format')) is not int or value['format'] != 1
            or not _valid_uuid(value.get('id'))
            or not _valid_runtime_binding(value.get('after'))):
        raise ConfigError('update_guard_requires_review')
    after = value['after']
    now = time.time()
    timestamp = value.get('commit_admitted_at')
    if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
            or timestamp <= 0 or timestamp > now):
        raise ConfigError('update_guard_requires_review')
    if after['installation_id'] != marker.get('installation_id'):
        return False
    if after['generation'] != marker.get('generation'):
        return False
    if Path(after['prefix']) != target['prefix'] or Path(after['host_root']) != target['root']:
        raise ConfigError('update_guard_requires_review')
    if not _valid_runtime_binding(record):
        raise ConfigError('update_guard_requires_review')
    if record['installation_id'] != marker.get('installation_id'):
        return False
    if record['generation'] != marker.get('generation'):
        return False
    if (Path(record['prefix']) != target['prefix'] or Path(record['host_root']) != target['root']
            or record['service_uid'] != after['service_uid']):
        raise ConfigError('update_guard_requires_review')
    if after != record:
        return False
    return True


@contextmanager
def quiesce(instance,target,job,save):
    from .pilot_operations import UNIT_BASE,_systemctl
    from .provisioning import protected
    names={'poolrotater-v7-'+kind+'-'+instance+'.timer' for kind in ('managed','updates','recovery')}
    records=job.get('paused_helper_timers')
    if records is None:
        records=[]
        for name in sorted(names):
            path=UNIT_BASE/name
            if not os.path.lexists(path):continue
            raw=protected(path).read_bytes()
            units=[line for line in raw.decode('ascii').splitlines() if line.startswith('Unit=')]
            if units!=['Unit='+name.removesuffix('.timer')+'.service'] or b'[Timer]' not in raw:
                raise ConfigError('update_guard_requires_review')
            state=_systemctl('show',name,'--property=ActiveState','--value').decode('ascii').strip()
            if state not in ('active','inactive','failed'):raise ConfigError('update_guard_requires_review')
            records.append({'name':name,'sha256':hashlib.sha256(raw).hexdigest(),'was_active':state=='active'})
        save(paused_helper_timers=records)
    if not isinstance(records,list) or len(records)>3:raise ConfigError('update_guard_requires_review')
    seen=set()
    for row in records:
        if (type(row) is not dict or set(row)!= {'name','sha256','was_active'}
                or row['name'] not in names or row['name'] in seen or type(row['was_active']) is not bool):
            raise ConfigError('update_guard_requires_review')
        seen.add(row['name'])
        raw=protected(UNIT_BASE/row['name']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ConfigError('update_guard_requires_review')
    try:
        for row in records:_systemctl('stop',row['name'])
        save(helper_pause_active=True)
        yield
    finally:
        failed=False
        for row in records:
            if not row['was_active']:continue
            try:_systemctl('start',row['name'])
            except ConfigError:
                failed=True
                logging.getLogger(__name__).warning('controller helper timer restoration requires review')
        save(helper_pause_active=failed,helper_restore_required=failed)
