"""Plan retirement of an obsolete automatic maintenance pair from exact proof.

This module does not remove files, change ownership, or issue room operations.
The future I/O coordinator must authenticate custody, registration, process
absence, current ownership and leases before it can consume this witness.
"""
import math
from pathlib import Path
import uuid

from .config import ConfigError
from .network.contracts import fingerprint


def _require(value, code='maintenance_retirement_binding_requires_review'):
    if not value:
        raise ConfigError(code)


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def committed_upgrade_witness(parent, terminal):
    """Bind terminal evidence to the exact committed native activation.

    This is read-only. A terminal controller job is insufficient on its own:
    the native journal, activation receipt and both immutable runtime inventories
    must agree. Older ancestry is not inferred from versions or directory scans.
    """
    from .pilot_upgrade import _record, _transition
    from .provisioning import _control, protected
    from .runtime_lifecycle import read_transition, validate_runtime
    _require(type(parent) is dict and type(terminal) is dict
             and terminal.get('phase') == 'complete' and terminal.get('after') == parent,
             'maintenance_retirement_terminal_update_requires_review')
    prefix = Path(parent['prefix'])
    upgrade = _record(_control(protected(prefix / 'pilot-upgrade.json'), maximum=512 * 1024),
                      prefix, parent['installation_id'])
    activation = read_transition(prefix, parent['installation_id'])
    _require(upgrade.get('phase') == 'committed' and upgrade.get('after') == parent
             and upgrade.get('before') == terminal.get('before')
             and upgrade.get('transition_id') == terminal.get('upgrade_transition')
             and activation == _transition(upgrade, 'committed'),
             'maintenance_retirement_native_upgrade_requires_review')
    # Validate executable inventories too; journal agreement is not a substitute.
    validate_runtime(parent, prefix)
    validate_runtime(upgrade['before'], prefix)
    return {'format': 1, 'transition_id': upgrade['transition_id'],
            'upgrade_sha256': fingerprint(upgrade), 'activation_sha256': fingerprint(activation),
            'before_sha256': upgrade['before']['bundle_sha256'],
            'after_sha256': parent['bundle_sha256']}


def retirement_witness(parent, terminal, pending, mailbox, receipt, *, old_process_absent):
    """Validate a completed-update witness without manufacturing a closure.

    ``old_process_absent`` is an independently acquired local observation, not
    a value accepted from the network. A positive return is still only a plan.
    """
    from .operator_maintenance import validate
    from .update_drain import _request_id, _validate_drain, _validate_maintenance_receipt
    _require(old_process_absent is True, 'maintenance_retirement_old_process_not_absent')
    _require(all(type(x) is dict for x in (parent, terminal, pending, mailbox, receipt)),
             'maintenance_retirement_record_shape_requires_review')
    _require(terminal.get('phase') == 'complete' and terminal.get('after') == parent
             and terminal.get('release_approval_required') is True,
             'maintenance_retirement_terminal_update_requires_review')
    _require(pending.get('phase') == 'waiting_empty' and pending.get('before') == parent
             and pending.get('release_approval_required') is True,
             'maintenance_retirement_pending_update_requires_review')
    _require(not any(pending.get(k) is not None for k in
                    ('commit_started_at', 'commit_admitted_at', 'services_stopped_at',
                     'startup_attempted_at', 'upgrade_transition')),
             'maintenance_retirement_commit_already_started')
    try:
        _require(str(uuid.UUID(terminal['id'])) == terminal['id'])
        _require(str(uuid.UUID(pending['id'])) == pending['id'])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ConfigError('maintenance_retirement_operation_identity_requires_review') from None
    _require(terminal['id'] != pending['id'])
    aliases = terminal.get('rooms')
    _require(type(aliases) is list and aliases and all(type(a) is str for a in aliases)
             and len(set(aliases)) == len(aliases),
             'maintenance_retirement_room_topology_requires_review')
    before = terminal.get('before')
    _require(type(before) is dict and before.get('bundle_sha256') != parent.get('bundle_sha256'))
    _require(all(before.get(k) == parent.get(k) and parent.get(k) is not None for k in
                 ('installation_id', 'generation', 'prefix', 'host_root', 'service_uid', 'execution')))
    _require(mailbox.get('parent_sha256') == before.get('bundle_sha256')
             and mailbox.get('phase') == 'requested' and mailbox.get('mode') == 'empty')
    validate(mailbox, before)
    _validate_maintenance_receipt(receipt, before, mailbox)
    _require(receipt.get('phase') in ('requested', 'draining') and not receipt.get('failures'),
             'maintenance_retirement_outcome_requires_review')
    _require(type(receipt.get('rooms')) is dict and receipt['rooms']
             and all(x in ('waiting_empty', 'closed') for x in receipt['rooms'].values()),
             'maintenance_retirement_outcome_requires_review')
    drain = terminal.get('controller_update_drain')
    _require(type(drain) is dict)
    _validate_drain(drain, before, terminal)
    _require(mailbox['id'] == drain['request_id'] ==
             _request_id(terminal['id'], drain.get('round', 0)))
    _require(mailbox['rooms'] == drain['rooms'] and set(receipt['rooms']) == set(drain['rooms'])
             and pending.get('rooms') == terminal.get('rooms'))
    process = terminal.get('process')
    _require(type(process) is dict and type(process.get('pid')) is int and process['pid'] > 1
             and receipt.get('controller_pid') == process['pid'])
    new_process = pending.get('process')
    _require(new_process is None or type(new_process) is dict
             and type(new_process.get('pid')) is int and new_process['pid'] > 1
             and new_process['pid'] != process['pid'])
    proof = terminal.get('precommit_controller_failure_proof')
    _require(terminal.get('admission_kind') == 'confirmed_closed_controller_failure'
             and type(proof) is dict and proof.get('process') == process
             and proof.get('closed_aliases') == sorted(terminal['rooms'])
             and terminal.get('precommit_controller_failure_proof_sha256') == fingerprint(proof),
             'maintenance_retirement_closed_controller_proof_requires_review')
    failure = proof.get('provider_service_failure')
    _require(type(failure) is dict and failure.get('pid') == process['pid']
             and failure.get('workers_reaped') is True and failure.get('referee_child_reaped') is True)
    invocation = failure.get('systemd_invocation')
    _require(type(invocation) is dict and invocation.get('exec_main_pid') == process['pid']
             and invocation.get('cgroup_empty') is True)
    completed = terminal.get('completed_at')
    queued = pending.get('queued_at')
    observed = receipt.get('updated_at')
    _require(all(_time(x) for x in (completed, queued, observed))
             and mailbox['created_at'] <= observed <= completed <= queued,
             'maintenance_retirement_time_order_requires_review')
    return {'format': 1, 'kind': 'obsolete-automatic-maintenance',
            'installation_id': parent['installation_id'], 'generation': parent['generation'],
            'current_parent_sha256': parent['bundle_sha256'],
            'terminal_job_id': terminal['id'], 'terminal_job_sha256': fingerprint(terminal),
            'pending_job_id': pending['id'], 'request_id': mailbox['id'],
            'mailbox_sha256': fingerprint(mailbox), 'receipt_sha256': fingerprint(receipt),
            'closed_controller_proof_sha256': fingerprint(proof),
            'retired_controller_pid': process['pid'], 'room_operations_authorized': False,
            'ownership_changes_authorized': False}
