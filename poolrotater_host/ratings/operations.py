"""Explicit stopped M4 administration; no implicit service shutdown or startup."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading

from ..config import ConfigError
from ..layout import file_lease, contained, read_json
from ..pilot_installation import administrative_binding, _stopped, require_no_service_processes
from .. import pilot_operations as pilot
from ..network import operations as network
from .migration import migrate_ledger, local_status
from ..network.contracts import fields, integer, number, require, NetworkError


_AGENT_REASONS={'lease-contention','post-upload-query-deferred','authority-review',
                'local-review','transport-retry','idle','sync-complete'}
_FETCH_REASONS={'lease-contention','authority-review','local-review','transport-retry',
                'snapshot-downloading','snapshot-activated','independent'}


def _optional_agent_diagnostics(root, marker):
    """Read only the fixed, nonsecret scheduler fields; malformed files are optional."""
    try:
        value=read_json(contained(root,'state/ratings-agent-diagnostics.json'))
        fields(value,('format','installation_id','generation','attempted_at','reason',
                      'lock_contention','retry_deadline','pending_event_age_seconds','uploaded','queried'))
        require(type(value['format']) is int and value['format']==1
                and all(value[k]==marker[k] for k in ('installation_id','generation')),
                'invalid_ratings_agent_diagnostics')
        number(value['attempted_at']);number(value['retry_deadline'])
        require(value['attempted_at']+5<=value['retry_deadline']<=value['attempted_at']+1980,
                'invalid_ratings_agent_diagnostics')
        require(value['reason'] in _AGENT_REASONS and type(value['lock_contention']) is bool,
                'invalid_ratings_agent_diagnostics')
        age=value['pending_event_age_seconds']
        require(age is None or (type(age) is int and 0<=age<=2**53-1),
                'invalid_ratings_agent_diagnostics')
        integer(value['uploaded'],0,500);integer(value['queried'],0,100)
        return {'available':True,'reason':value['reason'],'lock_contention':value['lock_contention'],
                'attempted_at':value['attempted_at'],'retry_deadline':value['retry_deadline'],
                'pending_event_age_seconds':age,'uploaded':value['uploaded'],'queried':value['queried']}
    except (NetworkError,ConfigError,OSError,ValueError,TypeError,KeyError):
        return {'available':False}


def _optional_fetch_diagnostics(root, marker):
    try:
        value=read_json(contained(root,'state/ratings-fetch-agent-diagnostics.json'))
        fields(value,('format','installation_id','generation','attempted_at','reason',
                      'lock_contention','retry_deadline','downloaded_pages','pending_pages'))
        require(type(value['format']) is int and value['format']==1
                and all(value[k]==marker[k] for k in ('installation_id','generation')),
                'invalid_ratings_fetch_diagnostics')
        number(value['attempted_at']);number(value['retry_deadline'])
        require(value['attempted_at']+5<=value['retry_deadline']<=value['attempted_at']+1980,
                'invalid_ratings_fetch_diagnostics')
        require(value['reason'] in _FETCH_REASONS and type(value['lock_contention']) is bool,
                'invalid_ratings_fetch_diagnostics')
        integer(value['downloaded_pages'],0,2);integer(value['pending_pages'],0,200)
        return {'available':True,'reason':value['reason'],'lock_contention':value['lock_contention'],
                'attempted_at':value['attempted_at'],'retry_deadline':value['retry_deadline'],
                'downloaded_pages':value['downloaded_pages'],'pending_pages':value['pending_pages']}
    except (NetworkError,ConfigError,OSError,ValueError,TypeError,KeyError):
        return {'available':False}


def scheduler_diagnostics(root, marker):
    return {'upload':_optional_agent_diagnostics(root,marker),
            'snapshot':_optional_fetch_diagnostics(root,marker)}


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
    target, marker, record = administrative_binding(instance)
    pilot._service_command(target, record, ['--pilot-check'])
    if action not in ('migrate','seed','recover-stream','reader-configure','review-epoch'):
        from ..network.contracts import decode, canonical
        arguments=['--ratings-action',action];data=None
        if stream is not None:
            data=canonical(decode(stream.read(1025),maximum=1024));arguments.append('--ratings-input')
        result=pilot._service_command(target,record,arguments,input_data=data,timeout=60)
        if action in ('status','support'):
            try:
                with _identity(record['service_uid']):
                    result={**result,'scheduler_diagnostics':scheduler_diagnostics(target['root'],marker)}
            except (ConfigError,OSError,ValueError,TypeError):
                result={**result,'scheduler_diagnostics':{'upload':{'available':False},
                                                          'snapshot':{'available':False}}}
        return result
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
