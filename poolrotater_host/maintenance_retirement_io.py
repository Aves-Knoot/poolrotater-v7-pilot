"""Retire obsolete automatic maintenance as evidence, never as a room closure.

The root-owned decision archives the original bytes. The native mailbox and
service receipt remain untouched until the normal new request/ack replaces
them. One atomic decision plus a job reference makes every crash point replayable.
Caller holds deployment/controller admission and the automatic-refresh fence.
"""
import json
import os
from pathlib import Path
import stat
import time
import uuid

from .config import ConfigError
from .controller_history import REFERENCE, read_terminal
from .layout import file_lease, unique_fields
from .maintenance_retirement import (
    _require, _time, committed_upgrade_witness, retirement_witness,
)
from .network.contracts import fingerprint
from .provisioning import _control, _write, protected

RETIREMENT = 'obsolete_maintenance_retirement'
FOLDER = 'controller-maintenance-retirements'
MAXIMUM = 2 * 1024 * 1024


def _path(target, job):
    try:
        _require(str(uuid.UUID(job['id'])) == job['id'])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ConfigError('maintenance_retirement_operation_identity_requires_review') from None
    return Path(target['prefix']) / FOLDER / (job['id'] + '.json')


def _pair_paths(target):
    from .operator_maintenance import NAME, RECEIPT
    return Path(target['prefix']) / NAME, Path(target['root']) / RECEIPT


def _decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique_fields,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, TypeError):
        raise ConfigError('maintenance_retirement_original_bytes_require_review') from None


def _original(path, owner, expected, *, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        st = os.fstat(stream.fileno())
        _require(stat.S_ISREG(st.st_mode) and st.st_uid == owner and st.st_nlink == 1
                 and st.st_size <= MAXIMUM // 4
                 and not stat.S_IMODE(st.st_mode) & (0o077 if private else 0o022),
                 'maintenance_retirement_original_custody_requires_review')
        raw = stream.read(MAXIMUM // 4 + 1)
    _require(len(raw) <= MAXIMUM // 4 and _decode(raw) == expected,
             'maintenance_retirement_original_changed')
    return raw.hex()


def _load(target, parent, job, reference):
    path = _path(target, job)
    protected(path.parent)
    _require(stat.S_ISDIR(path.parent.lstat().st_mode)
             and stat.S_IMODE(path.parent.lstat().st_mode) == 0o700,
             'maintenance_retirement_archive_custody_requires_review')
    record = _control(path, private=True, maximum=MAXIMUM)
    _require(type(reference) is dict and set(reference) == {'format', 'job_id', 'sha256'}
             and type(reference.get('format')) is int and reference['format'] == 1
             and reference.get('job_id') == job.get('id')
             and reference.get('sha256') == fingerprint(record),
             'maintenance_retirement_archive_reference_requires_review')
    fields = {'format', 'decision', 'parent', 'pending', 'terminal_reference', 'witness',
              'native_upgrade', 'mailbox_hex', 'receipt_hex', 'created_at'}
    _require(type(record) is dict and set(record) == fields
             and type(record.get('format')) is int and record['format'] == 1
             and record.get('decision') == 'retired_without_room_operations'
             and record.get('parent') == parent
             and record.get('terminal_reference') == job.get(REFERENCE)
             and type(record.get('pending')) is dict and _time(record.get('created_at')),
             'maintenance_retirement_archive_binding_requires_review')
    pending = record['pending']
    _require(all(pending.get(k) == job.get(k) for k in
                 ('id', 'before', 'process', 'rooms', 'queued_at', 'release_approval_required')),
             'maintenance_retirement_pending_update_requires_review')
    terminal = read_terminal(target, parent, record['terminal_reference'])
    try:
        mailbox = _decode(bytes.fromhex(record['mailbox_hex']))
        receipt = _decode(bytes.fromhex(record['receipt_hex']))
    except (ValueError, TypeError):
        raise ConfigError('maintenance_retirement_original_bytes_require_review') from None
    expected = retirement_witness(parent, terminal, pending, mailbox, receipt,
                                  old_process_absent=True)
    _require(record['witness'] == expected and type(record['native_upgrade']) is dict
             and record['native_upgrade'].get('transition_id') == terminal.get('upgrade_transition')
             and record['native_upgrade'].get('before_sha256') == terminal['before']['bundle_sha256']
             and record['native_upgrade'].get('after_sha256') == parent['bundle_sha256'],
             'maintenance_retirement_archive_proof_requires_review')
    return record, mailbox, receipt


def pair_matches(target, parent, job, mailbox, receipt):
    """Recognize only the exact archived pair, or its receipt awaiting a new ack."""
    reference = job.get(RETIREMENT)
    if reference is None:
        return False
    record, original, progress = _load(target, parent, job, reference)
    if receipt != progress:
        return False
    if mailbox == original:
        return True
    from .update_drain import RECORD, _request_id
    from .operator_maintenance import validate
    drain = job.get(RECORD)
    if (type(drain) is not dict or drain.get('round', 0) != 0
            or type(mailbox) is not dict
            or mailbox.get('id') != drain.get('request_id')
            or mailbox.get('id') != _request_id(job['id'], 0)
            or mailbox.get('rooms') != drain.get('rooms')
            or mailbox.get('mode') != 'empty'):
        return False
    validate(mailbox, parent)
    return True


def _old_process_absent(pid):
    _require(type(pid) is int and pid > 1)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def ensure_retired(target, parent, job, save, *, now=time.time):
    """Persist a decision before normal drain admission; never modify the pair."""
    from .update_drain import RECORD, _collect, _process_matches
    if job.get(RECORD) is not None or job.get(REFERENCE) is None:
        return False
    mailbox_path, receipt_path = _pair_paths(target)
    mailbox = _control(mailbox_path, optional=True)
    receipt = _control(receipt_path, parent['service_uid'], private=True, optional=True)
    if (type(mailbox) is not dict or type(receipt) is not dict
            or mailbox.get('parent_sha256') == parent['bundle_sha256']
            or receipt.get('phase') != 'draining'):
        return False
    with file_lease(Path(target['prefix']) / 'maintenance-admin.lock'):
        _require(str(target['prefix']) == parent['prefix']
                 and str(target['root']) == parent['host_root']
                 and _control(Path(target['prefix']) / 'installations' /
                              (parent['installation_id'] + '.json')) == parent
                 and _process_matches(job),
                 'maintenance_retirement_current_controller_requires_review')
        reference = job.get(RETIREMENT)
        if reference is not None:
            _require(pair_matches(target, parent, job, mailbox, receipt),
                     'maintenance_retirement_original_changed')
            return True
        terminal = read_terminal(target, parent, job[REFERENCE])
        absent = _old_process_absent(terminal.get('process', {}).get('pid'))
        witness = retirement_witness(parent, terminal, job, mailbox, receipt,
                                     old_process_absent=absent)
        native = committed_upgrade_witness(parent, terminal)
        at = now() if callable(now) else now
        _require(_time(at), 'maintenance_retirement_clock_requires_review')
        # Existing whole-host checks cover current ownership, fresh worker/roster
        # identity, stopped slots, managed operations and private booking guards.
        selected, *_ = _collect(target, parent, job, now=at)
        if not selected:
            return False
        _require(_control(mailbox_path) == mailbox
                 and _control(receipt_path, parent['service_uid'], private=True) == receipt,
                 'maintenance_retirement_original_changed')
        path = _path(target, job)
        if os.path.lexists(path):
            # A crash after decision persistence but before job-reference save
            # reuses this exact immutable decision instead of issuing another.
            existing = _control(path, private=True, maximum=MAXIMUM)
            reference = {'format': 1, 'job_id': job['id'], 'sha256': fingerprint(existing)}
            trial = {**job, RETIREMENT: reference}
            record, old_mailbox, old_receipt = _load(target, parent, trial, reference)
            _require(old_mailbox == mailbox and old_receipt == receipt
                     and record['native_upgrade'] == native and record['witness'] == witness,
                     'maintenance_retirement_archive_proof_requires_review')
        else:
            _require(not os.path.lexists(Path(target['root']) /
                                        'state/rolling/supervisor-maintenance.json'),
                     'maintenance_retirement_existing_gate_requires_review')
            raw_mailbox = _original(mailbox_path, 0, mailbox)
            raw_receipt = _original(receipt_path, parent['service_uid'], receipt, private=True)
            record = {'format': 1, 'decision': 'retired_without_room_operations',
                      'parent': parent, 'pending': job.copy(), 'terminal_reference': job[REFERENCE],
                      'witness': witness, 'native_upgrade': native,
                      'mailbox_hex': raw_mailbox, 'receipt_hex': raw_receipt, 'created_at': at}
            _require(len((json.dumps(record, indent=2, allow_nan=False) + '\n').encode()) <= MAXIMUM,
                     'maintenance_retirement_archive_size_requires_review')
            path.parent.mkdir(mode=0o700, exist_ok=True)
            protected(path.parent)
            _require(stat.S_ISDIR(path.parent.lstat().st_mode)
                     and stat.S_IMODE(path.parent.lstat().st_mode) == 0o700,
                     'maintenance_retirement_archive_custody_requires_review')
            _write(path, record, private=True)
            reference = {'format': 1, 'job_id': job['id'], 'sha256': fingerprint(record)}
        save(**{RETIREMENT: reference})
        job[RETIREMENT] = reference
        return True
