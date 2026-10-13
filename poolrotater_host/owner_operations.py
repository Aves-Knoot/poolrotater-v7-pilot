"""Installed stopped owner migration; no discovery, secret reuse or room start."""
import os
from pathlib import Path
import uuid

from .config import ConfigError
from .layout import file_lease
from .pilot_installation import administrative_binding, _stopped, require_no_service_processes
from . import pilot_operations as pilot
from .network import operations as network
from .ratings.operations import _identity
from .catalog_export import sha256


def command(instance, action, *, stream=None, snapshot_sha256=None):
    if action not in ('import', 'status', 'rollback', 'budget-prepare'):
        raise ConfigError('invalid owner administration action')
    if (stream is not None) != (action == 'import'):
        raise ConfigError('owner import requires explicit reviewed mappings on stdin')
    target, marker, record = administrative_binding(instance)
    pilot._service_command(target, record, ['--pilot-check'])
    from . import owner_migration as adoption
    if action == 'status':
        with _identity(record['service_uid']): result = adoption.status(target['root'])
        return {**result, 'protected_runtime_checked': True, 'actual_room_testing': False}
    with file_lease(target['prefix'] / 'network-admin.lock'):
        pilot._unit(instance, optional=True)
        network._checked_unit(instance, optional=True)
        for state in (pilot._service_state(instance), network._state(instance)):
            if (state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed')
                    or state['UnitFileState'] not in ('', 'disabled', 'not-found')):
                raise ConfigError('owner adoption requires disabled/stopped bot and network services')
        require_no_service_processes(record['service_uid'])
        with _stopped(target['root'], record['service_uid']):
            require_no_service_processes(record['service_uid'])
            with _identity(record['service_uid']):
                if action == 'import':
                    from .network.contracts import decode, fields
                    from .catalog_store import _hash
                    snapshot = target['prefix'] / 'owner-adoption'
                    adoption._offline_path(snapshot, directory=True)
                    _hash(snapshot_sha256)
                    if sha256(snapshot / adoption.MANIFEST) != snapshot_sha256:
                        raise ConfigError('independently reviewed owner snapshot digest mismatch')
                    review = decode(stream.read(256 * 1024 + 1), maximum=256 * 1024)
                    fields(review, ('mappings',))
                    if not isinstance(review['mappings'], dict):
                        raise ConfigError('reviewed owner mappings invalid')
                    result = adoption.import_snapshot(target['root'], snapshot,
                        mappings=review['mappings'], stopped=True, owner_lease_held=True)
                elif action == 'rollback':
                    transaction = uuid.uuid4().hex
                    directory = target['root'] / 'backups' / ('owner-rollback-' + transaction)
                    result = adoption.rollback_state(target['root'], directory,
                        stopped=True, owner_lease_held=True)
                    result.update(complete_owner_rollback=True, rollback_id=transaction)
                else:
                    captured = adoption.record(target['root'])
                    if captured is None or captured['phase'] != 'committed' or not captured['audit']['ok']:
                        raise ConfigError('owner pacing requires completed configuration adoption')
                    path = target['root'] / 'backups/owner-source-config.json'
                    if adoption._identity(path) != captured['outputs']['backups/owner-source-config.json']:
                        raise ConfigError('preserved owner source configuration changed')
                    from .layout import read_json
                    effective = adoption._config(read_json(path, maximum=1024**2))
                    if any(type(cfg.get('requests_per_minute')) is not int
                           or cfg['requests_per_minute'] != 50
                           or type(cfg.get('requests_burst_capacity')) is not int
                           or cfg['requests_burst_capacity'] != 60 for cfg in effective.values()):
                        raise ConfigError('owner pacing requires exact reviewed 50/60 source configuration')
                    from .operator_client import _load_provider_client, provider_client_fingerprint
                    from .pilot_identity import provider_execution
                    execution = provider_execution({'registration': record})
                    profile = _load_provider_client(target['root'], execution=execution)
                    fingerprint = provider_client_fingerprint(target['root'], profile, execution=execution)
                    proof = {key: marker[key] for key in ('installation_id', 'generation')}
                    proof.update(snapshot_sha256=captured['snapshot_sha256'], phase='committed',
                                 audit_ok=True, source_rpm=50, source_burst=60)
            if action == 'budget-prepare':
                from .provider_budget import configure
                def stopped_guard():
                    require_no_service_processes(record['service_uid'])
                    for state in (pilot._service_state(instance), network._state(instance)):
                        if (state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed')
                                or state['UnitFileState'] not in ('', 'disabled', 'not-found')):
                            raise ConfigError('owner pacing preparation requires stopped services')
                    if administrative_binding(instance)[2] != record:
                        raise ConfigError('registered owner runtime changed during pacing preparation')
                result = configure(target['prefix'], marker, fingerprint, proof, stopped_guard=stopped_guard)
                result.pop('policy_sha256', None)
    return {**result, 'protected_runtime_checked': True, 'actual_room_testing': False,
            'services_started': False, 'provider_dispatch': False}
