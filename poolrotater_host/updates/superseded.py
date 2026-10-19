"""Explicitly retire old scheduler intent after a proved later native upgrade.

This changes scheduler history only. It never cancels a native transaction,
adopts ownership, stops a service, or calls a provider.
"""
from .contracts import fingerprint, require, version


def qualify(operation, parent, manifest, job, upgrade, rows, journal):
    require(operation['release']['kind'] == 'controller', 'update_superseded_controller_required')
    require(job is not None and job.get('id') != operation['id']
            and job.get('phase') == 'complete' and job.get('after') == parent,
            'update_superseded_committed_successor_required')
    require(upgrade is not None and upgrade.get('phase') == 'committed'
            and upgrade.get('before') == job.get('before') and upgrade.get('after') == parent,
            'update_superseded_upgrade_receipt_required')
    require(job.get('queued_at', 0) > operation['queued_at']
            and job.get('completed_at', 0) >= job['queued_at'],
            'update_superseded_chronology_required')
    require(parent['bundle_sha256'] not in (operation['before_sha256'],
            operation['release']['artifact']['sha256'])
            and version(manifest['version']) > version(operation['release']['version']),
            'update_superseded_newer_registration_required')
    require(set(rows) == set(job['rooms']) == set(journal.get('rooms', {})) and bool(rows),
            'update_superseded_topology_required')
    for alias, row in rows.items():
        stored = journal['rooms'][alias]
        require(row.get('release') == parent['release'] == stored.get('release')
                and row.get('phase') == stored.get('phase') == 'running'
                and row.get('operation') == stored.get('operation') == 'owned'
                and row.get('ownership_unknown') is False and stored.get('ownership_unknown') is False
                and not row.get('error') and not stored.get('error')
                and row.get('room_id') is not None and row['room_id'] == stored.get('room_id')
                and not row.get('target') and not stored.get('target')
                and not stored.get('creation_candidate'), 'update_superseded_owned_workers_required')
    return {'ok': True, 'phase': 'superseded', 'installed_version': manifest['version'],
            'successor_operation_id': job['id'], 'successor_sha256': parent['bundle_sha256'],
            'native_evidence_sha256': fingerprint({'job': job, 'upgrade': upgrade, 'parent': parent}),
            'services_restarted': False, 'live_rooms_affected': False}


def review(instance, operation):
    from ..pilot_installation import administrative_binding
    from ..provisioning import _control
    from ..layout import file_lease
    from ..runtime_lifecycle import validate_runtime
    from ..pilot_upgrade import require_settled
    from ..operator_maintenance import require_idle
    from .native import require_idle_worker_deployment
    from .recovery import _fresh_running
    target, _, _ = administrative_binding(instance)
    with file_lease(target['prefix']/'deployment-admission.lock'), \
         file_lease(target['prefix']/'controller-update-admin.lock'):
        locked, _, parent = administrative_binding(instance)
        require(locked == target, 'update_superseded_binding_changed')
        runtime = validate_runtime(parent, target['prefix'])
        require_settled(target['prefix'], parent['installation_id'])
        require_idle(target, parent)
        require_idle_worker_deployment(target, parent)
        rows = _fresh_running(target, parent)
        job = _control(target['prefix']/'controller-update.json', optional=True)
        upgrade = _control(target['prefix']/'pilot-upgrade.json', optional=True, maximum=512*1024)
        journal = _control(target['root']/'state/rolling/ownership.json', parent['service_uid'], private=True)
        return qualify(operation, parent, runtime['manifest'], job, upgrade, rows, journal)
