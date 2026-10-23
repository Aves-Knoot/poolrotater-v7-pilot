"""Private host-side booking lease, role, command, and effect ledger."""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from ..diagnostic_logs.common import private_database, transaction
from ..network.contracts import NetworkError, canonical, identifier, require
from ..config import PRESETS
from .lifecycle import advance as advance_lifecycle, initial as initial_lifecycle
from .settings import validate_player_patch

PROTOCOL_VERSION = 1
ACTION_SET = {'configure', 'start', 'abort', 'skip', 'end', 'grant', 'revoke'}
ROLES = {'owner', 'controller', 'player', 'grant_pending'}
ALIAS_RE = re.compile(r'room[1-4]\Z')
HEX32_RE = re.compile(r'[a-f0-9]{32}\Z')
MAX_JSON = 65536
MAX_PENDING_COMMANDS = 32


def _now(value):
    require(type(value) in (int, float) and math.isfinite(value) and
            0 <= value <= 2**53 - 1, 'invalid_time')
    return float(value)


def _json(value):
    return json.loads(value) if value is not None else None


def _digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _uuid(value, code='invalid_booking_command'):
    try:
        require(type(value) is str and str(uuid.UUID(value)) == value, code)
    except (ValueError, AttributeError):
        raise NetworkError(code) from None
    return value


def _positive_id(value, code='invalid_booking_command'):
    require(type(value) is int and 0 < value <= 2**63 - 1, code)
    return value


class LocalSessions:
    """SQLite-backed host ledger. It performs no provider, fleet, or service I/O."""

    def __init__(self, root, binding, clock=time.time):
        self.clock = clock
        self.binding = self._validate_binding(binding)
        self.path = private_database(Path(root) / 'state', 'bookings.sqlite3')
        with transaction(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS host_bookings(
                booking_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, installation_id TEXT NOT NULL,
                generation TEXT NOT NULL, alias TEXT NOT NULL, session_generation TEXT NOT NULL,
                lease TEXT NOT NULL, lifecycle_state TEXT NOT NULL, roles TEXT NOT NULL,
                settings TEXT NOT NULL, base_settings TEXT NOT NULL, preset TEXT NOT NULL,
                cleanup_complete INTEGER NOT NULL DEFAULT 0, latest_roster TEXT NOT NULL DEFAULT '[]',
                roster_known INTEGER NOT NULL DEFAULT 0, roster_updated_at REAL,
                last_sequence INTEGER NOT NULL DEFAULT -1, configure_cooldown_until REAL NOT NULL DEFAULT 0,
                last_local_sequence INTEGER NOT NULL DEFAULT -1,
                settings_revision INTEGER NOT NULL DEFAULT 0,
                audit TEXT NOT NULL DEFAULT '[]')''')
            db.execute('''CREATE TABLE IF NOT EXISTS host_commands(
                operation_id TEXT PRIMARY KEY, booking_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                user_id INTEGER NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL, phase TEXT NOT NULL, error TEXT, application TEXT,
                effective_settings TEXT, acknowledgement_hash TEXT, created REAL NOT NULL,
                origin TEXT NOT NULL DEFAULT 'hub', request_id TEXT, envelope TEXT,
                UNIQUE(booking_id, sequence),
                FOREIGN KEY(booking_id) REFERENCES host_bookings(booking_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS host_command_refusals(
                operation_id TEXT NOT NULL, payload_hash TEXT NOT NULL, booking_id TEXT,
                user_id INTEGER, request_id TEXT, action TEXT, origin TEXT NOT NULL,
                receipt TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(operation_id,payload_hash))''')
            db.execute('''CREATE TABLE IF NOT EXISTS host_effects(
                effect_id TEXT PRIMARY KEY, booking_id TEXT NOT NULL, effect TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                FOREIGN KEY(booking_id) REFERENCES host_bookings(booking_id))''')
            booking_columns = {row[1] for row in db.execute('PRAGMA table_info(host_bookings)')}
            if 'last_local_sequence' not in booking_columns:
                db.execute('ALTER TABLE host_bookings ADD COLUMN last_local_sequence INTEGER NOT NULL DEFAULT -1')
            if 'settings_revision' not in booking_columns:
                db.execute('ALTER TABLE host_bookings ADD COLUMN settings_revision INTEGER NOT NULL DEFAULT 0')
            command_columns = {row[1] for row in db.execute('PRAGMA table_info(host_commands)')}
            for name, declaration in (('origin', "TEXT NOT NULL DEFAULT 'hub'"),
                                      ('request_id', 'TEXT'), ('envelope', 'TEXT')):
                if name not in command_columns:
                    db.execute(f'ALTER TABLE host_commands ADD COLUMN {name} {declaration}')

    @staticmethod
    def _validate_binding(value):
        require(type(value) is dict and set(value) == {
            'host_id', 'installation_id', 'generation'
        }, 'invalid_booking_binding')
        for key in ('host_id', 'installation_id', 'generation'):
            identifier(value[key])
        return copy.deepcopy(value)

    @staticmethod
    def _lease(lease, base_settings):
        require(type(lease) is dict and len(canonical(lease)) <= MAX_JSON,
                'invalid_booking_lease')
        required = {'id', 'user_id', 'designated_friend_id', 'host_id',
                    'installation_id', 'generation', 'alias', 'session_generation',
                    'preset', 'settings', 'password'}
        require(required <= set(lease), 'invalid_booking_lease')
        _uuid(lease['id'], 'invalid_booking_lease')
        _positive_id(lease['user_id'], 'invalid_booking_lease')
        friend = lease['designated_friend_id']
        require(friend is None or (_positive_id(friend, 'invalid_booking_lease') != lease['user_id']),
                'invalid_booking_lease')
        for key in ('host_id', 'installation_id', 'generation'):
            identifier(lease[key])
        require(type(lease['alias']) is str and ALIAS_RE.fullmatch(lease['alias']) is not None,
                'invalid_booking_lease')
        require(type(lease['session_generation']) is str and
                HEX32_RE.fullmatch(lease['session_generation']) is not None,
                'invalid_booking_lease')
        require(type(lease['preset']) is str and lease['preset'] in PRESETS,
                'invalid_booking_lease')
        require(type(lease['password']) is str and 1 <= len(lease['password']) <= 256,
                'invalid_booking_lease')
        require(type(lease['settings']) is dict and type(base_settings) is dict,
                'invalid_booking_lease')
        return copy.deepcopy(lease), copy.deepcopy(base_settings)

    @staticmethod
    def _booking(row):
        if row is None:
            return None
        return {
            'lease': _json(row['lease']),
            'state': _json(row['lifecycle_state']),
            'roles': {int(uid): role for uid, role in _json(row['roles']).items()
                      if role in ('owner', 'controller', 'player')},
            'settings': _json(row['settings']),
            'base_settings': _json(row['base_settings']),
            'cleanup_complete': bool(row['cleanup_complete']),
            'settings_revision': row['settings_revision'],
            'operation_receipts': [],
        }

    @staticmethod
    def _row(db, booking_id):
        return db.execute('SELECT * FROM host_bookings WHERE booking_id=?',
                          (booking_id,)).fetchone()

    @staticmethod
    def _require_queue_space(db, booking_id):
        count=db.execute('''SELECT COUNT(*) FROM host_commands WHERE booking_id=?
            AND phase IN ('pending','dispatched')''',(booking_id,)).fetchone()[0]
        require(count<MAX_PENDING_COMMANDS,'booking_command_queue_full',409)

    def install(self, lease, base_settings):
        lease, base_settings = self._lease(lease, base_settings)
        require((lease['host_id'], lease['installation_id'], lease['generation']) ==
                (self.binding['host_id'], self.binding['installation_id'], self.binding['generation']),
                'host_generation_mismatch', 409)
        require(bool(base_settings), 'invalid_booking_settings')
        base_settings = validate_player_patch(base_settings, current=base_settings,
                                              preset=lease['preset'])
        patch = validate_player_patch(lease['settings'], current=base_settings,
                                      preset=lease['preset'])
        settings = dict(base_settings); settings.update(patch)
        require(bool(settings), 'invalid_booking_settings')
        lease['settings'] = copy.deepcopy(settings)
        booking_id = lease['id']
        binding = {
            'booking_id': booking_id,
            'session_generation': lease['session_generation'],
            'host_id': lease['host_id'],
            'installation_id': lease['installation_id'],
            'generation': lease['generation'],
            'alias': lease['alias'],
        }
        state = initial_lifecycle(binding, lease['user_id'], lease['designated_friend_id'])
        now = _now(self.clock())
        with transaction(self.path) as db:
            existing = self._row(db, booking_id)
            if existing is not None:
                require(_json(existing['lease']) == lease and
                        _json(existing['base_settings']) == base_settings,
                        'booking_lease_conflict', 409)
                return self._booking(existing)
            alias_row = db.execute('''SELECT * FROM host_bookings
                WHERE host_id=? AND alias=? ORDER BY rowid DESC LIMIT 1''',
                                   (lease['host_id'], lease['alias'])).fetchone()
            if alias_row is not None:
                prior_state = _json(alias_row['lifecycle_state'])
                require(bool(alias_row['cleanup_complete']) and prior_state['phase'] == 'ended',
                        'booking_alias_in_use', 409)
            roles = {str(lease['user_id']): 'owner'}
            db.execute('''INSERT INTO host_bookings(
                booking_id,host_id,installation_id,generation,alias,session_generation,
                lease,lifecycle_state,roles,settings,base_settings,preset,cleanup_complete,
                latest_roster,roster_known,roster_updated_at,last_sequence,configure_cooldown_until,audit)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,'[]',0,NULL,-1,0,'[]')''',
                       (booking_id, lease['host_id'], lease['installation_id'], lease['generation'],
                        lease['alias'], lease['session_generation'], canonical(lease).decode(),
                        canonical(state).decode(), canonical(roles).decode(), canonical(settings).decode(),
                        canonical(base_settings).decode(), lease['preset']))
            return self._booking(self._row(db, booking_id))

    def get(self, booking_id):
        _uuid(booking_id)
        with transaction(self.path) as db:
            row = self._row(db, booking_id)
            require(row is not None, 'booking_not_found', 404)
            result = self._booking(row)
            receipts = db.execute('''SELECT * FROM host_commands WHERE booking_id=? ORDER BY rowid''',
                                  (booking_id,)).fetchall()
            refusals = db.execute('''SELECT receipt FROM host_command_refusals
                                     WHERE booking_id=? AND NOT EXISTS(
                                       SELECT 1 FROM host_commands c
                                       WHERE c.operation_id=host_command_refusals.operation_id)
                                     ORDER BY created''', (booking_id,)).fetchall()
            result['operation_receipts'] = [self._receipt(item) for item in receipts]
            result['operation_receipts'].extend(_json(item['receipt']) for item in refusals)
            return result

    def receipts(self, limit=32):
        """Return the newest hub-origin command receipts for the next exchange."""
        require(type(limit) is int and 1 <= limit <= 32, 'invalid_receipt_limit')
        with transaction(self.path) as db:
            rows = db.execute('''SELECT * FROM host_commands WHERE origin='hub'
                AND phase IN ('applied','failed','cancelled') ORDER BY rowid DESC LIMIT ?''',
                              (limit,)).fetchall()
            result = [self._receipt(row) for row in reversed(rows)]
            remaining = limit - len(result)
            if remaining:
                refusals = db.execute('''SELECT receipt FROM host_command_refusals
                    WHERE origin='hub' AND NOT EXISTS(
                      SELECT 1 FROM host_commands c
                      WHERE c.operation_id=host_command_refusals.operation_id)
                    ORDER BY created DESC LIMIT ?''', (remaining,)).fetchall()
                result.extend(_json(row['receipt']) for row in reversed(refusals))
            return result

    def leases(self, include_ended=False):
        with transaction(self.path) as db:
            rows = db.execute('SELECT * FROM host_bookings ORDER BY rowid').fetchall()
            result = []
            for row in rows:
                state = _json(row['lifecycle_state'])
                if not include_ended and state['phase'] == 'ended' and row['cleanup_complete']:
                    continue
                result.append(self._booking(row))
            return result

    @staticmethod
    def _command(value):
        expected = {'protocol_version', 'operation_id', 'booking_id', 'host_id',
                    'installation_id', 'generation', 'alias', 'session_generation',
                    'sequence', 'user_id', 'action', 'payload'}
        optional = {'request_id', 'expected_revision', 'role_revision', 'role_snapshot',
                    'expected_settings_revision'}
        optional.add('origin')
        require(type(value) is dict and expected <= set(value) and
                not set(value) - expected - optional,
                'invalid_booking_command')
        require(type(value['protocol_version']) is int and
                value['protocol_version'] == PROTOCOL_VERSION, 'unsupported_protocol', 409)
        for key in ('operation_id', 'booking_id', 'host_id', 'installation_id', 'generation'):
            _uuid(value[key])
        require(type(value['alias']) is str and ALIAS_RE.fullmatch(value['alias']) is not None,
                'invalid_booking_command')
        require(type(value['session_generation']) is str and
                HEX32_RE.fullmatch(value['session_generation']) is not None,
                'invalid_booking_command')
        require(type(value['sequence']) is int and value['sequence'] >= 0,
                'invalid_booking_sequence')
        _positive_id(value['user_id'])
        require(type(value['action']) is str and value['action'] in ACTION_SET and
                type(value['payload']) is dict and
                len(canonical(value['payload'])) <= MAX_JSON, 'invalid_booking_command')
        require(value.get('origin', 'hub') == 'hub', 'invalid_booking_command')
        if 'request_id' in value:
            _uuid(value['request_id'])
        for key in ('expected_revision', 'role_revision'):
            if key in value:
                require(type(value[key]) is int and value[key] >= 0,
                        'invalid_booking_command')
        if 'expected_settings_revision' in value:
            require(type(value['expected_settings_revision']) is int and
                    value['expected_settings_revision'] >= 0,
                    'invalid_booking_command')
        if 'role_snapshot' in value:
            snapshot = value['role_snapshot']
            require(type(snapshot) is dict and len(snapshot) <= 256 and
                    all(type(uid) in (str, int) and str(uid).isdigit() and
                        role in ('owner', 'controller', 'player')
                        for uid, role in snapshot.items()), 'invalid_booking_command')
        return value

    @staticmethod
    def _role_allows(role, action):
        if action in ('end', 'grant', 'revoke'):
            return role == 'owner'
        if action in ('configure', 'start', 'abort', 'skip'):
            return role in ('owner', 'controller')
        if action in ('dot_command', 'player_command', 'vote'):
            return role in ('owner', 'controller', 'player')
        return False

    @staticmethod
    def _receipt(row):
        if row is None:
            return None
        return {
            'operation_id': row['operation_id'], 'booking_id': row['booking_id'],
            'sequence': row['sequence'] if row['origin'] == 'hub' else -row['sequence'] - 1,
            'origin': row['origin'], 'request_id': row['request_id'],
            'user_id': row['user_id'],
            'action': row['action'], 'phase': row['phase'], 'error': row['error'],
            'application': _json(row['application']),
            'effective_settings': _json(row['effective_settings']),
        }

    def request_local(self, booking_id, user_id, request_id, action, payload):
        """Queue an authorized in-game control in its own sequence namespace."""
        _uuid(booking_id); _positive_id(user_id, 'invalid_user'); _uuid(request_id)
        local_actions = {'configure', 'start', 'abort', 'skip', 'end'}
        require(type(action) is str and action in local_actions and type(payload) is dict,
                'invalid_local_action')
        payload_hash = _digest({'booking_id': booking_id, 'user_id': user_id,
                                'request_id': request_id, 'action': action,
                                'payload': payload})
        now = _now(self.clock())
        with transaction(self.path) as db:
            prior = db.execute('''SELECT * FROM host_commands WHERE booking_id=?
                AND origin='local' AND user_id=? AND request_id=?''',
                               (booking_id, user_id, request_id)).fetchone()
            if prior is not None:
                require(prior['payload_hash'] == payload_hash, 'idempotency_conflict', 409)
                return self._receipt(prior)
            row = self._row(db, booking_id)
            require(row is not None, 'booking_not_found', 404)
            require(not row['cleanup_complete'] and
                    _json(row['lifecycle_state'])['phase'] not in ('closing', 'ended'),
                    'booking_closed', 409)
            pending_end = db.execute('''SELECT 1 FROM host_commands WHERE booking_id=?
                AND action='end' AND phase IN ('pending','dispatched','applied') LIMIT 1''',
                                     (booking_id,)).fetchone()
            require(pending_end is None or action == 'end', 'booking_closing', 409)
            roles = _json(row['roles'])
            require(self._role_allows(roles.get(str(user_id)), action), 'booking_forbidden', 403)
            if action in ('start', 'abort', 'skip', 'end'):
                require(not payload, 'invalid_action_payload')
            else:
                require(bool(payload), 'empty_settings')
                require(now >= row['configure_cooldown_until'], 'configure_cooldown', 409)
                pending_configure = db.execute('''SELECT 1 FROM host_commands WHERE booking_id=?
                    AND action='configure' AND phase IN ('pending','dispatched') LIMIT 1''',
                                                (booking_id,)).fetchone()
                require(pending_configure is None, 'configure_pending', 409)
                patch = validate_player_patch(payload, current=_json(row['settings']),
                                              preset=row['preset'])
                require(bool(patch), 'empty_settings')
                merged = dict(_json(row['settings'])); merged.update(patch)
                payload = merged
            if action!='end':
                self._require_queue_space(db,booking_id)
            sequence = row['last_local_sequence'] + 1
            operation_id = str(uuid.uuid4())
            envelope = {
                'protocol_version': PROTOCOL_VERSION,
                'operation_id': operation_id, 'booking_id': booking_id,
                'host_id': row['host_id'], 'installation_id': row['installation_id'],
                'generation': row['generation'], 'alias': row['alias'],
                'session_generation': row['session_generation'],
                'sequence': sequence, 'user_id': user_id, 'action': action,
                'payload': payload, 'origin': 'local', 'request_id': request_id,
                'expected_settings_revision': row['settings_revision'],
            }
            if action == 'end':
                db.execute('''UPDATE host_commands SET phase='cancelled',error='booking_ending'
                    WHERE booking_id=? AND phase IN ('pending','dispatched')''', (booking_id,))
            db.execute('''INSERT INTO host_commands(operation_id,booking_id,sequence,user_id,action,
                payload,payload_hash,phase,created,origin,request_id,envelope)
                VALUES(?,?,?,?,?,?,?,?,?,'local',?,?)''',
                       (operation_id, booking_id, -sequence - 1, user_id, action,
                        canonical(payload).decode(), payload_hash, 'pending', now,
                        request_id, canonical(envelope).decode()))
            cooldown = now + 10 if action == 'configure' else row['configure_cooldown_until']
            db.execute('''UPDATE host_bookings SET last_local_sequence=?,configure_cooldown_until=?
                WHERE booking_id=?''', (sequence, cooldown, booking_id))
            return self._receipt(db.execute('SELECT * FROM host_commands WHERE operation_id=?',
                                            (operation_id,)).fetchone())

    @staticmethod
    def _refusal_receipt(command, payload_hash, code, created):
        operation_id = command.get('operation_id') if type(command) is dict else None
        booking_id = command.get('booking_id') if type(command) is dict else None
        user_id = command.get('user_id') if type(command) is dict else None
        request_id = command.get('request_id') if type(command) is dict else None
        action = command.get('action') if type(command) is dict else None
        sequence = command.get('sequence') if type(command) is dict else None
        if type(code) is not str or re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code) is None:
            code = 'booking_command_refused'
        return {
            'operation_id': operation_id, 'booking_id': booking_id,
            'sequence': sequence, 'origin': 'hub', 'request_id': request_id,
            'user_id': user_id, 'action': action, 'phase': 'failed',
            'error': code, 'application': None, 'effective_settings': None,
        }

    def _persist_refusal(self, command, payload_hash, code):
        try:
            operation_id = _uuid(command.get('operation_id'))
        except (AttributeError, NetworkError):
            raise NetworkError(code) from None
        booking_id = command.get('booking_id')
        if type(booking_id) is not str:
            booking_id = None
        else:
            try:
                _uuid(booking_id)
            except NetworkError:
                booking_id = None
        user_id = command.get('user_id')
        if type(user_id) is not int or user_id <= 0:
            user_id = None
        request_id = command.get('request_id')
        try:
            if request_id is not None:
                _uuid(request_id)
        except NetworkError:
            request_id = None
        action = command.get('action')
        if type(action) is not str or len(action) > 64:
            action = None
        now = _now(self.clock())
        receipt = self._refusal_receipt(command, payload_hash, code, now)
        with transaction(self.path) as db:
            prior = db.execute('''SELECT receipt FROM host_command_refusals
                WHERE operation_id=? AND payload_hash=?''', (operation_id, payload_hash)).fetchone()
            if prior is not None:
                return _json(prior['receipt'])
            receipt.update(operation_id=operation_id, booking_id=booking_id,
                           user_id=user_id, request_id=request_id, action=action)
            db.execute('''INSERT INTO host_command_refusals(
                operation_id,payload_hash,booking_id,user_id,request_id,action,origin,receipt,created)
                VALUES(?,?,?,?,?,?, 'hub',?,?)''',
                       (operation_id, payload_hash, booking_id, user_id, request_id, action,
                        canonical(receipt).decode(), now))
            return receipt

    def receive(self, command):
        # Hash and cache terminal refusals too: a malformed or unauthorized
        # delivered command must not poison every retry of the exchange body.
        try:
            raw_digest = _digest(command)
            require(len(canonical(command)) <= MAX_JSON, 'invalid_booking_command')
            operation_id = _uuid(command.get('operation_id'))
        except (NetworkError, AttributeError, TypeError, ValueError):
            raise NetworkError('invalid_booking_command') from None
        with transaction(self.path) as db:
            prior_refusal = db.execute('''SELECT receipt FROM host_command_refusals
                WHERE operation_id=? AND payload_hash=?''', (operation_id, raw_digest)).fetchone()
            if prior_refusal is not None:
                return _json(prior_refusal['receipt'])
        try:
            normalized = self._command(command)
            return self._receive_validated(normalized, raw_digest)
        except NetworkError as exc:
            return self._persist_refusal(command, raw_digest, exc.code)

    def _receive_validated(self, command, digest):
        command = {**command, 'origin': 'hub',
                   'expected_settings_revision': command.get('expected_settings_revision', 0)}
        envelope = copy.deepcopy(command)
        now = _now(self.clock())
        with transaction(self.path) as db:
            prior = db.execute('SELECT * FROM host_commands WHERE operation_id=?',
                               (command['operation_id'],)).fetchone()
            if prior is not None:
                require(prior['payload_hash'] == digest, 'idempotency_conflict', 409)
                return self._receipt(prior)
            row = self._row(db, command['booking_id'])
            require(row is not None, 'booking_not_found', 404)
            lease = _json(row['lease'])
            expected = (lease['id'], lease['host_id'], lease['installation_id'], lease['generation'],
                        lease['alias'], lease['session_generation'])
            actual = (command['booking_id'], command['host_id'], command['installation_id'],
                      command['generation'], command['alias'], command['session_generation'])
            require(actual == expected, 'host_generation_mismatch', 409)
            require(not row['cleanup_complete'] and
                    _json(row['lifecycle_state'])['phase'] not in ('closing', 'ended'),
                    'booking_closed', 409)
            pending_end = db.execute('''SELECT 1 FROM host_commands WHERE booking_id=?
                AND action='end' AND phase IN ('pending','dispatched','applied') LIMIT 1''',
                                     (command['booking_id'],)).fetchone()
            require(pending_end is None or command['action'] == 'end', 'booking_closing', 409)
            require(command['sequence'] > row['last_sequence'], 'stale_booking_command', 409)
            roles = _json(row['roles'])
            user_role = roles.get(str(command['user_id']))
            snapshot = command.get('role_snapshot')
            if snapshot is not None:
                snapshot_role = snapshot.get(str(command['user_id']),
                                             snapshot.get(command['user_id']))
                require(snapshot_role == user_role, 'booking_role_changed', 409)
            require(self._role_allows(user_role, command['action']), 'booking_forbidden', 403)
            require(command['expected_settings_revision'] == row['settings_revision'],
                    'settings_revision_conflict', 409)
            if command['action'] not in ('end','revoke'):
                self._require_queue_space(db,command['booking_id'])
            payload = command['payload']
            if command['action'] in ('start', 'abort', 'skip', 'end'):
                require(not payload, 'invalid_action_payload')
            elif command['action'] == 'configure':
                require(bool(payload), 'empty_settings')
                require(now >= row['configure_cooldown_until'], 'configure_cooldown', 409)
                pending = db.execute('''SELECT 1 FROM host_commands WHERE booking_id=?
                    AND action='configure' AND phase IN ('pending','dispatched') LIMIT 1''',
                                       (command['booking_id'],)).fetchone()
                require(pending is None, 'configure_pending', 409)
                patch = validate_player_patch(payload, current=_json(row['settings']), preset=row['preset'])
                require(bool(patch), 'empty_settings')
                merged = dict(_json(row['settings'])); merged.update(patch)
                payload = merged
                command = {**command, 'payload': payload}
            elif command['action'] == 'grant':
                require(set(payload) == {'user_id'}, 'invalid_action_payload')
                target = _positive_id(payload['user_id'])
                require(target != lease['user_id'], 'invalid_controller', 409)
                roster = _json(row['latest_roster'])
                roster_fresh = (row['roster_known'] and row['roster_updated_at'] is not None and
                                0 <= now - row['roster_updated_at'] <= 30)
                require(roster_fresh and any(p['user_id'] == target for p in roster),
                        'player_not_in_roster', 409)
                require(roles.get(str(target)) not in ('owner', 'controller', 'grant_pending'),
                        'invalid_controller', 409)
                roles[str(target)] = 'grant_pending'
                db.execute('UPDATE host_bookings SET roles=? WHERE booking_id=?',
                           (canonical(roles).decode(), command['booking_id']))
            elif command['action'] == 'revoke':
                require(set(payload) == {'user_id'}, 'invalid_action_payload')
                target = _positive_id(payload['user_id'])
                require(roles.get(str(target)) in ('controller', 'grant_pending'),
                        'invalid_controller', 409)
                roles.pop(str(target), None)
                db.execute('UPDATE host_bookings SET roles=? WHERE booking_id=?',
                           (canonical(roles).decode(), command['booking_id']))
                # Revoke access at receipt time and cancel every command from that user.
                db.execute('''UPDATE host_commands SET phase='cancelled',error='revoked'
                    WHERE booking_id=? AND user_id=? AND phase IN ('pending','dispatched')''',
                           (command['booking_id'], target))
                db.execute('''UPDATE host_commands SET phase='cancelled',error='revoked'
                    WHERE booking_id=? AND action='grant' AND phase IN ('pending','dispatched')
                    AND json_extract(payload,'$.user_id')=?''', (command['booking_id'], target))

            if command['action'] == 'end':
                db.execute('''UPDATE host_commands SET phase='cancelled',error='booking_ending'
                    WHERE booking_id=? AND phase IN ('pending','dispatched')''',
                           (command['booking_id'],))

            db.execute('''INSERT INTO host_commands(operation_id,booking_id,sequence,user_id,action,
                payload,payload_hash,phase,created,origin,request_id,envelope)
                VALUES(?,?,?,?,?,?,?,?,?,'hub',?,?)''',
                       (command['operation_id'], command['booking_id'], command['sequence'],
                        command['user_id'], command['action'], canonical(payload).decode(),
                        digest, 'pending', now, command.get('request_id'), canonical(envelope).decode()))
            values = {'last_sequence': command['sequence']}
            if command['action'] == 'configure':
                values['configure_cooldown_until'] = now + 10
            db.execute('UPDATE host_bookings SET last_sequence=?,configure_cooldown_until=? WHERE booking_id=?',
                       (values['last_sequence'], values.get('configure_cooldown_until', row['configure_cooldown_until']),
                        command['booking_id']))
            return self._receipt(db.execute('SELECT * FROM host_commands WHERE operation_id=?',
                                            (command['operation_id'],)).fetchone())

    def pending(self, booking_id):
        _uuid(booking_id)
        now = _now(self.clock())
        with transaction(self.path) as db:
            booking = self._row(db, booking_id)
            require(booking is not None, 'booking_not_found', 404)
            rows = db.execute('''SELECT * FROM host_commands WHERE booking_id=?
                AND phase IN ('pending','dispatched') ORDER BY rowid''', (booking_id,)).fetchall()
            result = []
            roles = _json(booking['roles'])
            lifecycle_phase = _json(booking['lifecycle_state'])['phase']
            for row in rows:
                if lifecycle_phase in ('closing', 'ended') or booking['cleanup_complete']:
                    db.execute("UPDATE host_commands SET phase='cancelled',error='booking_closed' WHERE operation_id=?",
                               (row['operation_id'],))
                    continue
                if not self._role_allows(roles.get(str(row['user_id'])), row['action']):
                    db.execute("UPDATE host_commands SET phase='cancelled',error='role_revoked' WHERE operation_id=?",
                               (row['operation_id'],))
                    continue
                envelope = _json(row['envelope']) if row['envelope'] is not None else {}
                expected_settings_revision = envelope.get('expected_settings_revision', 0)
                if expected_settings_revision != booking['settings_revision']:
                    db.execute("UPDATE host_commands SET phase='failed',error='settings_revision_conflict' WHERE operation_id=?",
                               (row['operation_id'],))
                    continue
                payload = _json(row['payload'])
                if row['action'] == 'grant':
                    target = payload.get('user_id')
                    roster = _json(booking['latest_roster'])
                    roster_live = (booking['roster_known'] and booking['roster_updated_at'] is not None and
                                   0 <= now - booking['roster_updated_at'] <= 30 and
                                   any(player['user_id'] == target for player in roster))
                    if roles.get(str(target)) != 'grant_pending' or not roster_live:
                        db.execute("UPDATE host_commands SET phase='cancelled',error='roster_or_role_changed' WHERE operation_id=?",
                                   (row['operation_id'],))
                        continue
                if row['action'] in ('grant', 'revoke', 'end') and roles.get(str(row['user_id'])) != 'owner':
                    db.execute("UPDATE host_commands SET phase='cancelled',error='owner_required' WHERE operation_id=?",
                               (row['operation_id'],))
                    continue
                if row['envelope'] is not None:
                    envelope = _json(row['envelope'])
                else:
                    envelope = {
                        'protocol_version': PROTOCOL_VERSION,
                        'operation_id': row['operation_id'], 'booking_id': booking_id,
                        'host_id': booking['host_id'], 'installation_id': booking['installation_id'],
                        'generation': booking['generation'], 'alias': booking['alias'],
                        'session_generation': booking['session_generation'],
                        'sequence': row['sequence'] if row['origin'] == 'hub' else -row['sequence'] - 1,
                        'user_id': row['user_id'], 'action': row['action'],
                        'payload': payload, 'origin': row['origin'], 'request_id': row['request_id'],
                        'expected_settings_revision': expected_settings_revision,
                    }
                result.append(envelope)
                if row['phase'] == 'pending':
                    db.execute("UPDATE host_commands SET phase='dispatched' WHERE operation_id=?",
                               (row['operation_id'],))
            return result

    def acknowledge(self, operation_id, phase, error=None, application=None,
                    effective_settings=None):
        _uuid(operation_id)
        require(phase in ('applied', 'failed', 'cancelled'), 'invalid_operation_phase')
        require(error is None or (type(error) is str and len(error) <= 160 and
                                  not any(ord(ch) < 32 or ord(ch) == 127 for ch in error)),
                'invalid_operation_error')
        require(application is None or (type(application) is dict and
                                        len(canonical(application)) <= 16384),
                'invalid_operation_application')
        require(effective_settings is None or (type(effective_settings) is dict and
                                               len(canonical(effective_settings)) <= 16384),
                'invalid_booking_settings')
        ack = {'phase': phase, 'error': error, 'application': application,
               'effective_settings': effective_settings}
        ack_hash = _digest(ack)
        now = _now(self.clock())
        with transaction(self.path) as db:
            row = db.execute('SELECT * FROM host_commands WHERE operation_id=?',
                             (operation_id,)).fetchone()
            require(row is not None, 'operation_not_found', 404)
            if row['phase'] == 'cancelled' and row['acknowledgement_hash'] is None:
                # Cancellation remains authoritative when a dispatched native
                # action returns late. Pin the late result for idempotency, but
                # never turn the cancelled operation back into an applied one.
                db.execute('UPDATE host_commands SET acknowledgement_hash=? WHERE operation_id=?',
                           (ack_hash, operation_id))
                return self._receipt(row)
            if row['phase'] in ('applied', 'failed', 'cancelled'):
                require(row['acknowledgement_hash'] == ack_hash, 'operation_receipt_conflict', 409)
                return self._receipt(row)
            require(row['phase'] in ('pending', 'dispatched'), 'invalid_operation_transition', 409)
            booking = self._row(db, row['booking_id'])
            roles = _json(booking['roles'])
            payload = _json(row['payload'])
            if phase == 'applied' and row['action'] == 'grant':
                target = payload.get('user_id')
                if roles.get(str(target)) == 'grant_pending':
                    roles[str(target)] = 'controller'
                    db.execute('UPDATE host_bookings SET roles=? WHERE booking_id=?',
                               (canonical(roles).decode(), row['booking_id']))
            elif phase in ('failed', 'cancelled') and row['action'] == 'grant':
                target = payload.get('user_id')
                if roles.get(str(target)) == 'grant_pending':
                    roles.pop(str(target), None)
                    db.execute('UPDATE host_bookings SET roles=? WHERE booking_id=?',
                               (canonical(roles).decode(), row['booking_id']))
            if phase == 'applied' and row['action'] == 'configure':
                committed = effective_settings if effective_settings is not None else payload
                normalized = validate_player_patch(committed, current=_json(booking['settings']),
                                                   preset=booking['preset'])
                settings = dict(_json(booking['settings'])); settings.update(normalized)
                db.execute('UPDATE host_bookings SET settings=?,settings_revision=settings_revision+1 WHERE booking_id=?',
                           (canonical(settings).decode(), row['booking_id']))
            db.execute('''UPDATE host_commands SET phase=?,error=?,application=?,effective_settings=?,
                acknowledgement_hash=? WHERE operation_id=?''',
                       (phase, error, canonical(application).decode() if application is not None else None,
                        canonical(effective_settings).decode() if effective_settings is not None else None,
                        ack_hash, operation_id))
            if row['action'] == 'end' and phase == 'applied':
                audit = _json(booking['audit'])
                audit.append({'operation_id': operation_id, 'action': 'end', 'at': now,
                              'phase': 'applied'})
                db.execute('UPDATE host_bookings SET audit=? WHERE booking_id=?',
                           (canonical(audit[-128:]).decode(), row['booking_id']))
            result = db.execute('SELECT * FROM host_commands WHERE operation_id=?',
                                (operation_id,)).fetchone()
            return self._receipt(result)

    def advance(self, booking_id, observation):
        _uuid(booking_id)
        now = _now(self.clock())
        with transaction(self.path) as db:
            row = self._row(db, booking_id)
            require(row is not None, 'booking_not_found', 404)
            require(not row['cleanup_complete'], 'booking_cleanup_complete', 409)
            state, new_effects = advance_lifecycle(_json(row['lifecycle_state']), observation, now)
            # Lifecycle state and the UUID outbox entries commit together.
            db.execute('UPDATE host_bookings SET lifecycle_state=? WHERE booking_id=?',
                       (canonical(state).decode(), booking_id))
            if (observation['roster_known'] and observation['ready'] and
                    observation['ownership_known'] and observation['room_id'] is not None and
                    observation['incarnation'] is not None and
                    0 <= now - observation['updated_at'] <= 30):
                db.execute('''UPDATE host_bookings SET latest_roster=?,roster_known=1,
                    roster_updated_at=? WHERE booking_id=?''',
                           (canonical(observation['players']).decode(), observation['updated_at'], booking_id))
                roles = _json(row['roles'])
                for player in observation['players']:
                    user_key = str(player['user_id'])
                    if roles.get(user_key) not in ('owner', 'controller', 'grant_pending'):
                        roles[user_key] = 'player'
                db.execute('UPDATE host_bookings SET roles=? WHERE booking_id=?',
                           (canonical(roles).decode(), booking_id))
            for effect in new_effects:
                effect_id = str(uuid.uuid4())
                db.execute('INSERT INTO host_effects VALUES(?,?,?,0,?)',
                           (effect_id, booking_id, canonical(effect).decode(), now))
            effects = db.execute('''SELECT effect_id,effect FROM host_effects
                WHERE booking_id=? AND acknowledged=0 ORDER BY created,effect_id''',
                                 (booking_id,)).fetchall()
            return state, [{'effect_id': item['effect_id'], **_json(item['effect'])}
                           for item in effects]

    def acknowledge_effect(self, booking_id, effect_id):
        _uuid(booking_id); _uuid(effect_id)
        with transaction(self.path) as db:
            row = db.execute('SELECT * FROM host_effects WHERE booking_id=? AND effect_id=?',
                             (booking_id, effect_id)).fetchone()
            require(row is not None, 'booking_effect_not_found', 404)
            db.execute('UPDATE host_effects SET acknowledged=1 WHERE effect_id=?', (effect_id,))
            return {'booking_id': booking_id, 'effect_id': effect_id, 'acknowledged': True}

    def complete_cleanup(self, booking_id):
        _uuid(booking_id)
        with transaction(self.path) as db:
            row = self._row(db, booking_id)
            require(row is not None, 'booking_not_found', 404)
            state = _json(row['lifecycle_state'])
            require(state['phase'] == 'ended', 'booking_not_closed', 409)
            if row['cleanup_complete']:
                return self._booking(row)
            lease = _json(row['lease'])
            lease.pop('password', None)
            lease['settings'] = _json(row['base_settings'])
            audit = _json(row['audit'])
            audit.append({'event': 'cleanup_complete', 'at': _now(self.clock())})
            db.execute('''UPDATE host_bookings SET lease=?,settings=base_settings,roles='{}',
                cleanup_complete=1,latest_roster='[]',roster_known=0,roster_updated_at=NULL,
                audit=? WHERE booking_id=?''',
                       (canonical(lease).decode(), canonical(audit[-128:]).decode(), booking_id))
            db.execute('''UPDATE host_commands SET phase='cancelled',error='cleanup_complete'
                WHERE booking_id=? AND phase IN ('pending','dispatched')''', (booking_id,))
            return self._booking(self._row(db, booking_id))

    def authorize(self, booking_id, user_id, action):
        _uuid(booking_id); _positive_id(user_id, 'invalid_user')
        require(type(action) is str and 0 < len(action) <= 64, 'invalid_action')
        with transaction(self.path) as db:
            row = self._row(db, booking_id)
            require(row is not None, 'booking_not_found', 404)
            pending_end = db.execute('''SELECT 1 FROM host_commands WHERE booking_id=?
                AND action='end' AND phase IN ('pending','dispatched','applied') LIMIT 1''',
                                     (booking_id,)).fetchone()
            if (row['cleanup_complete'] or pending_end is not None or
                    _json(row['lifecycle_state'])['phase'] in ('closing', 'ended')):
                return False
            role = _json(row['roles']).get(str(user_id))
            return self._role_allows(role, action)
