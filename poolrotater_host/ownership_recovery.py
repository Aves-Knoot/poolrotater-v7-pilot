"""Offline-only, stopped recovery of known ended rooms; no upstream mutations."""
import copy
import hashlib
import json
import os
import re
import uuid

from .config import ConfigError, contained
from .layout import atomic_json, checked_root, load, read_json
from .oauth import AuthError
from .reconciliation import OfflineOwnershipReview, _journal, journal_path

RECOVERY_PATH = 'state/ownership-recovery.json'
MAXIMUM = 196608


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _bounded(value, maximum):
    if len((json.dumps(value, indent=2, allow_nan=False)+'\n').encode()) > maximum:
        raise AuthError('ownership_recovery_record_too_large')


def cleared(before):
    """Only previously identified owned/closing rooms can become locally closed."""
    after = copy.deepcopy(before)
    count = 0
    for state in after['rooms'].values():
        if state.get('room_id') is not None:
            if (state.get('operation') not in ('owned', 'close_pending')
                    or (state.get('ownership_unknown', False) and state['operation'] != 'close_pending')):
                raise AuthError('ownership_recovery_unresolved')
            state.update(room_id=None, channel_id=None, ownership_unknown=False,
                         operation='closed', phase='stopped', pid=None)
            count += 1
        elif state.get('ownership_unknown') or state.get('operation') in ('make_pending', 'close_pending'):
            raise AuthError('ownership_recovery_unresolved')
    if not count: raise AuthError('ownership_recovery_no_known_rooms')
    return after


def validate_record(value, *, root, marker, uid):
    """Pure structural/binding validation, also used by the root switch guard."""
    try:
        if (not isinstance(value, dict) or set(value) != {'format', 'execution', 'live_ready',
                'transaction_id', 'phase', 'binding', 'before', 'after', 'before_sha256', 'after_sha256'}
                or type(value['format']) is not int or value['format'] != 1
                or value['execution'] not in ('offline-fixture', 'provider-fixture', 'provider-disabled', 'provider-pilot', 'provider-live-pilot')
                or value['live_ready'] is not False
                or value['phase'] not in ('prepared', 'committed', 'aborted')):
            raise ValueError
        uuid.UUID(value['transaction_id'])
        b = value['binding']
        fields = {'host_root', 'uid', 'installation_id', 'generation', 'client_id', 'user_id', 'config_sha256'}
        if value['execution'] != 'offline-fixture': fields |= {'execution', 'client_profile_sha256'}
        if (not isinstance(b, dict) or set(b) != fields or b['host_root'] != str(root)
                or type(b['uid']) is not int or b['uid'] != uid
                or any(b[k] != marker[k] for k in ('installation_id', 'generation'))
                or any(type(b[k]) is not int or b[k] <= 0 for k in ('client_id', 'user_id'))
                or not isinstance(b['config_sha256'], str)
                or not re.fullmatch('[a-f0-9]{64}', b['config_sha256'])):
            raise ValueError
        if value['execution'] != 'offline-fixture':
            if b['execution'] != value['execution']: raise ValueError
            if not re.fullmatch('[a-f0-9]{64}', b['client_profile_sha256']): raise ValueError
        aliases = set(value['before']['rooms'])
        if _journal(value['before'], aliases) != value['before']: raise ValueError
        if _journal(value['after'], aliases) != cleared(value['before']): raise ValueError
        for journal in (value['before'], value['after']): _bounded(journal, 65536)
        if any(value[k+'_sha256'] != digest(value[k]) for k in ('before', 'after')): raise ValueError
        return value
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ConfigError('ownership recovery record is invalid or belongs to another installation') from None


def require_settled(root):
    """Run inside the owner lease, before any stores, workers or room creation."""
    root = checked_root(root)
    marker, _ = load(root)
    try: value = read_json(contained(root, RECOVERY_PATH), maximum=MAXIMUM)
    except FileNotFoundError: return
    validate_record(value, root=root, marker=marker, uid=os.geteuid())
    if value['execution'] != 'offline-fixture':
        from .provider_room_state import binding, require_namespace
        require_namespace(root, binding(root, value['binding'], value['binding']['user_id']))
    if value['phase'] == 'prepared': raise ConfigError('ownership recovery requires explicit complete or abort')


class OfflineOwnershipRecovery(OfflineOwnershipReview):
    """Explicit transactions only. Diagnostic review JSON is never authority."""
    def _binding(self, account):
        marker, config = load(self.root)
        if any(marker[k] != self.auth.binding[k] for k in ('installation_id', 'generation')):
            raise AuthError('ownership_recovery_binding_changed')
        return {'host_root': str(self.root), 'uid': os.geteuid(),
                **{k: marker[k] for k in ('installation_id', 'generation')},
                'client_id': self.auth.binding['client_id'], 'user_id': account.user_id,
                'config_sha256': digest(config)}

    def _read(self):
        marker, _ = load(self.root)
        try: value = read_json(contained(self.root, RECOVERY_PATH), maximum=MAXIMUM)
        except FileNotFoundError: return None
        return validate_record(value, root=self.root, marker=marker, uid=os.geteuid())

    def _ledger(self):
        _, config = load(self.root)
        try: return _journal(read_json(journal_path(self.root)),
                             {r['alias'] for r in config['rooms']})
        except FileNotFoundError: raise AuthError('ownership_journal_missing') from None

    def _eligible(self, account, before):
        report = self._inspect(account, journal=before)
        if (report['untracked_unconfirmed_room_ids'] or any(r['result'] not in
                ('confirmed_ended', 'local_clean') for r in report['rooms'])):
            raise AuthError('ownership_recovery_unresolved')
        return cleared(before)

    def _write(self, path, value):
        # Keep the ordinary bounded readers applicable after replacing a compact file.
        maximum = MAXIMUM if path.name == 'ownership-recovery.json' else 65536
        _bounded(value, maximum)
        try: atomic_json(path, value)
        except (OSError, ConfigError): raise AuthError('ownership_recovery_write_uncertain') from None

    def _result(self, record):
        return {'format': 1, 'execution': self.execution, 'live_ready': False,
                'transaction_id': record['transaction_id'], 'phase': record['phase'],
                'recovery_applied': record['phase'] == 'committed', 'start_allowed': False}

    def apply(self, *, expected_journal_sha256):
        """Expected digest selects local state; fresh provider reads authorize it."""
        if not isinstance(expected_journal_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_journal_sha256):
            raise AuthError('ownership_recovery_expected_digest_required')
        with self.auth.verified_owner() as account:
            previous = self._read()
            if previous and previous['phase'] == 'prepared': raise AuthError('ownership_recovery_pending')
            binding = self._binding(account)
            before = self._ledger()
            if digest(before) != expected_journal_sha256: raise AuthError('ownership_journal_changed')
            _bounded(before, 65536)
            after = self._eligible(account, before)
            _bounded(after, 65536)
            if self._binding(account) != binding or self._ledger() != before:
                raise AuthError('ownership_recovery_binding_changed')
            record = {'format': 1, 'execution': self.execution, 'live_ready': False,
                      'transaction_id': str(uuid.uuid4()), 'phase': 'prepared', 'binding': binding,
                      'before': before, 'after': after, 'before_sha256': digest(before), 'after_sha256': digest(after)}
            path = contained(self.root, RECOVERY_PATH)
            self._write(path, record)
            # Close the gap after persisting intent; never overwrite intervening state.
            return self._finish(account, record, action='complete')

    def recover(self, *, transaction_id, action):
        if action not in ('complete', 'abort'): raise AuthError('ownership_recovery_action_required')
        with self.auth.verified_owner() as account:
            record = self._read()
            if not record or record['phase'] != 'prepared' or record['transaction_id'] != transaction_id:
                raise AuthError('ownership_recovery_transaction_required')
            return self._finish(account, record, action=action)

    def _finish(self, account, record, *, action):
        if self._binding(account) != record['binding']: raise AuthError('ownership_recovery_binding_changed')
        current = self._ledger()
        if current not in (record['before'], record['after']): raise AuthError('ownership_journal_changed')
        if self._read() != record: raise AuthError('ownership_recovery_record_changed')
        if action == 'complete': self._eligible(account, record['before'])
        if (self._binding(account) != record['binding'] or self._ledger() != current
                or self._read() != record): raise AuthError('ownership_recovery_binding_changed')
        target = record['after'] if action == 'complete' else record['before']
        if current != target:
            self._write(journal_path(self.root), target)
        if (self._binding(account) != record['binding'] or self._ledger() != target
                or self._read() != record): raise AuthError('ownership_recovery_binding_changed')
        record = {**record, 'phase': 'committed' if action == 'complete' else 'aborted'}
        self._write(contained(self.root, RECOVERY_PATH), record)
        return self._result(record)
