"""Explicit stopped M4 administration; no implicit service shutdown or startup."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading

from ..config import ConfigError
from ..layout import file_lease
from ..pilot_installation import administrative_binding, _stopped, require_no_service_processes
from .. import pilot_operations as pilot
from ..network import operations as network
from .migration import migrate_ledger, local_status


@contextmanager
def _identity(uid):
    # All privileged checks are finished before touching SQLite. Root must not
    # create WAL/SHM that race dedicated-user writers after the migration.
    if (os.geteuid() != 0 or type(uid) is not int or uid <= 0
            or threading.active_count() != 1 or len(list(Path('/proc/self/task').iterdir())) != 1):
        raise ConfigError('rating administration requires a single-threaded administrator')
    gid, groups = os.getegid(), os.getgroups()
    mask = os.umask(0o077)
    try:
        os.setgroups([]); os.setegid(uid); os.seteuid(uid)
        yield
    finally:
        os.seteuid(0); os.setegid(gid); os.setgroups(groups); os.umask(mask)


def command(instance, action, stream=None):
    if action not in ('migrate','seed','recover-stream','review-epoch','reader-configure','configure','sync','fetch','status','support'): raise ConfigError('invalid rating administration action')
    if (stream is not None)!=(action in ('configure','reader-configure','review-epoch')):raise ConfigError('rating configure requires explicit stdin')
    target, _, record = administrative_binding(instance)
    pilot._service_command(target, record, ['--pilot-check'])
    if action not in ('migrate','seed','recover-stream','reader-configure','review-epoch'):
        from ..network.contracts import decode, canonical
        arguments=['--ratings-action',action];data=None
        if stream is not None:
            data=canonical(decode(stream.read(1025),maximum=1024));arguments.append('--ratings-input')
        return pilot._service_command(target,record,arguments,input_data=data,timeout=60)
    # Serialize agent start/remove and establish actual process absence, not just
    # a stale MainPID field. The owner lease excludes lobby/rolling activity.
    with file_lease(target['prefix']/'network-admin.lock'):
        pilot._unit(instance, optional=True)
        network._checked_unit(instance, optional=True)
        for state in (pilot._service_state(instance), network._state(instance)):
            if (state['MainPID'] or state['ActiveState'] not in ('inactive','failed')
                    or state['UnitFileState'] not in ('','disabled','not-found')):
                raise ConfigError('rating administration requires disabled/stopped instance and network services')
        require_no_service_processes(record['service_uid'])
        with _stopped(target['root'], record['service_uid']):
            require_no_service_processes(record['service_uid'])
            with _identity(record['service_uid']):
                if action=='migrate':result=migrate_ledger(target['root'])
                elif action=='seed':
                    from .seed import seed_ledger
                    result=seed_ledger(target['root'])
                elif action=='reader-configure':
                    from .reader import configure
                    from .sync import Synchronizer
                    from ..network.contracts import decode,fields
                    value=decode(stream.read(1025),maximum=1024);fields(value,('mode',))
                    result=configure(Synchronizer(target['root']),**value)
                elif action=='review-epoch':
                    from .sync import Synchronizer
                    from ..network.contracts import decode,fields
                    value=decode(stream.read(1025),maximum=1024)
                    fields(value,('expected_profile_sha256','expected_new_epoch','acknowledge_quarantined_backlog'))
                    result=Synchronizer(target['root']).review_canonical_restore(**value)
                else:
                    from .sync import Synchronizer
                    result=Synchronizer(target['root']).recover_stream()
    return {**result, 'protected_runtime_checked': True, 'actual_room_testing': False,
            'services_started': False, 'provider_dispatch': False}
