"""Retain terminal controller evidence before another update replaces it.

An archived job is evidence, never authority to clear ownership or retire a
maintenance request. Recovery must independently validate its exact bindings.
"""
import os
from pathlib import Path
import re
import stat
import uuid

from .config import ConfigError
from .network.contracts import fingerprint
from .provisioning import _control, _write, protected


REFERENCE = 'previous_terminal_controller'


def terminal_reference(job):
    """An exact archive address; never discover evidence by scanning history."""
    if job is None:
        return None
    try:
        if type(job) is not dict or str(uuid.UUID(job['id'])) != job['id']:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ConfigError('controller history identity requires review') from None
    return {'format': 1, 'operation_id': job['id'], 'job_sha256': fingerprint(job)}


def read_terminal(target, parent, reference):
    """Read one root-custodied, content-addressed record without granting authority."""
    try:
        if (type(reference) is not dict
                or set(reference) != {'format', 'operation_id', 'job_sha256'}
                or type(reference['format']) is not int or reference['format'] != 1
                or str(uuid.UUID(reference['operation_id'])) != reference['operation_id']
                or type(reference['job_sha256']) is not str
                or re.fullmatch('[a-f0-9]{64}', reference['job_sha256']) is None):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ConfigError('controller history reference requires review') from None
    prefix = Path(target['prefix'])
    if str(prefix) != parent['prefix']:
        raise ConfigError('controller history prefix changed')
    folder = protected(prefix / 'controller-terminal-history')
    st = folder.lstat()
    if not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o700:
        raise ConfigError('controller history directory requires review')
    path = folder / (reference['operation_id'] + '-' + reference['job_sha256'] + '.json')
    value = _control(path, private=True, maximum=1024 * 1024)
    if (type(value) is not dict or set(value) != {'format', 'job_sha256', 'job'}
            or type(value.get('format')) is not int or value['format'] != 1
            or value.get('job_sha256') != reference['job_sha256']
            or type(value.get('job')) is not dict
            or value['job'].get('id') != reference['operation_id']
            or fingerprint(value['job']) != reference['job_sha256']):
        raise ConfigError('controller history receipt changed')
    job = value['job']
    # Reading an archive must not rewrite it or create a missing one.
    _validate_terminal(parent, job)
    return job


def _validate_terminal(parent, job, *, current=True):
    """Retention and recovery share custody, but only recovery needs currentness.

    A supported stopped upgrade can leave the previous terminal job behind.
    Archiving it must not block the next update. Merely retaining its bytes
    grants no authority: readers used for retirement still require the exact
    current endpoint, followed by independent native activation validation.
    """
    try:
        if (type(job) is not dict or type(job.get('format')) is not int or job.get('format') != 1
                or job.get('phase') not in ('complete', 'cancelled')
                or str(uuid.UUID(job['id'])) != job['id']):
            raise ValueError
        side = 'after' if job['phase'] == 'complete' else 'before'
        if current and job.get(side) != parent:
            raise ValueError
        for endpoint in ('before', 'after'):
            record = job[endpoint]
            if type(record) is not dict or any(record.get(k) != parent[k] for k in
                    ('installation_id', 'generation', 'prefix', 'host_root',
                     'execution', 'service_uid')):
                raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ConfigError('controller history terminal binding requires review') from None


def archive_terminal(target, parent, job):
    if job is None:
        return None
    _validate_terminal(parent, job, current=False)
    prefix = Path(target['prefix'])
    if str(prefix) != parent['prefix']:
        raise ConfigError('controller history prefix changed')
    folder = prefix / 'controller-terminal-history'
    folder.mkdir(mode=0o700, exist_ok=True)
    protected(folder)
    st = folder.lstat()
    if not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o700:
        raise ConfigError('controller history directory requires review')
    value = {'format': 1, 'job_sha256': fingerprint(job), 'job': job}
    path = folder / (job['id'] + '-' + value['job_sha256'] + '.json')
    if os.path.lexists(path):
        if _control(path, private=True, maximum=1024 * 1024) != value:
            raise ConfigError('controller history receipt changed')
    else:
        _write(path, value, private=True)
    return path
