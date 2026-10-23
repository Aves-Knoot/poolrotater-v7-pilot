"""Durable private booking and host-operation ledger.

This module deliberately has no provider or network client.  The host-facing
methods consume already authenticated host identities supplied by the caller.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import uuid

from ..network.contracts import NetworkError, canonical, identifier, require, text
from ..config import PRESETS
from .contracts import SAFE_OPERATION_ERRORS, activity_info
from ..diagnostic_logs.common import check_database, private_database

PROTOCOL_VERSION = 1
SLOT_ID = re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')
ALIASES = {'room1', 'room2', 'room3', 'room4'}
ACTIVE = ('preparing', 'awaiting_claim', 'reserved', 'active', 'closing', 'unavailable', 'failed')
TERMINAL = ('ended',)
ACTIONS = {'configure', 'start', 'abort', 'skip', 'end', 'grant', 'revoke'}


def _now(value):
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 2**53 - 1, 'invalid_time')
    return float(value)


def _json(value):
    return json.loads(value) if value is not None else None


def _uuid():
    return str(uuid.uuid4())


def _creation_lease(value):
    """Project reserve-time data to the immutable host installation envelope."""
    booking_id = value.get('id') or value.get('booking_id')
    user_id = value.get('user_id') or value.get('owner_id')
    settings = value['settings']
    return {'protocol_version': PROTOCOL_VERSION, 'id': booking_id, 'booking_id': booking_id,
            'host_id': value['host_id'], 'installation_id': value['installation_id'],
            'generation': value['generation'], 'session_generation': value['session_generation'],
            'alias': value['alias'], 'user_id': user_id, 'owner_id': user_id,
            'designated_friend_id': value.get('designated_friend_id'), 'preset': value['preset'],
            'settings': _json(settings) if type(settings) is str else settings,
            'password': value['password']}


class Repository:
    """SQLite booking repository rooted in an application-owned private dir."""

    def __init__(self, root, clock=time.time, eligibility=None):
        self.root = Path(root)
        require(self.root.is_absolute(), 'private_booking_root_required')
        self.clock = clock
        self.eligibility = eligibility
        self.path = private_database(self.root, 'bookings.sqlite3')
        self._initialize()

    @contextmanager
    def _transaction(self):
        check_database(self.path)
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=2000')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self):
        # Schema changes are local to this opt-in feature's private ledger.
        with self._transaction() as db:
            schema = '''
                CREATE TABLE IF NOT EXISTS slots(
                    slot_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, alias TEXT NOT NULL,
                    label TEXT NOT NULL, presets TEXT NOT NULL, base_settings TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    UNIQUE(host_id,alias));
                CREATE TABLE IF NOT EXISTS bookings(
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, slot_id TEXT NOT NULL,
                    host_id TEXT NOT NULL, installation_id TEXT NOT NULL, generation TEXT NOT NULL,
                    alias TEXT NOT NULL, session_generation TEXT NOT NULL, preset TEXT NOT NULL, settings TEXT NOT NULL,
                    password TEXT NOT NULL, phase TEXT NOT NULL, revision INTEGER NOT NULL, role_revision INTEGER NOT NULL DEFAULT 0,
                    applied_settings TEXT,
                    created REAL NOT NULL, designated_friend_id INTEGER,
                    room_id INTEGER, incarnation TEXT, observation_sequence INTEGER NOT NULL DEFAULT -1,
                    observation_hash TEXT, observation TEXT, role_data TEXT NOT NULL DEFAULT '{}',
                    claim_deadline REAL, terminal_reason TEXT, lease_envelope TEXT,
                    FOREIGN KEY(slot_id) REFERENCES slots(slot_id));
                DROP INDEX IF EXISTS one_live_user;
                CREATE UNIQUE INDEX one_live_user ON bookings(user_id)
                    WHERE phase!='ended';
                DROP INDEX IF EXISTS one_live_slot;
                CREATE UNIQUE INDEX one_live_slot ON bookings(slot_id)
                    WHERE phase!='ended';
                CREATE TABLE IF NOT EXISTS requests(
                    user_id INTEGER NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    booking_id TEXT NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY(user_id,request_id));
                CREATE TABLE IF NOT EXISTS operations(
                    id TEXT PRIMARY KEY, booking_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                    request_id TEXT NOT NULL, payload_hash TEXT NOT NULL, action TEXT NOT NULL,
                    payload TEXT NOT NULL, expected_revision INTEGER NOT NULL, phase TEXT NOT NULL,
                    error TEXT, application TEXT, created REAL NOT NULL, command_sequence INTEGER NOT NULL DEFAULT 0,
                    envelope TEXT, host_receipt TEXT,
                    UNIQUE(booking_id,user_id,request_id),
                    FOREIGN KEY(booking_id) REFERENCES bookings(id));
                CREATE TABLE IF NOT EXISTS host_observations(
                    booking_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL, receipt TEXT NOT NULL,
                    FOREIGN KEY(booking_id) REFERENCES bookings(id));
                CREATE TABLE IF NOT EXISTS reserve_events(
                    user_id INTEGER NOT NULL, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS reserve_events_by_user ON reserve_events(user_id,at);
                CREATE TABLE IF NOT EXISTS no_show_cooldowns(
                    user_id INTEGER PRIMARY KEY, cooldown_until REAL NOT NULL);
            '''
            for statement in schema.split(';'):
                if statement.strip():
                    db.execute(statement)
            op_columns = {row['name'] for row in db.execute('PRAGMA table_info(operations)')}
            if 'command_sequence' not in op_columns:
                db.execute('ALTER TABLE operations ADD COLUMN command_sequence INTEGER NOT NULL DEFAULT 0')
            if 'envelope' not in op_columns:
                db.execute('ALTER TABLE operations ADD COLUMN envelope TEXT')
            if 'host_receipt' not in op_columns:
                db.execute('ALTER TABLE operations ADD COLUMN host_receipt TEXT')
            next_sequence = {}
            legacy = db.execute('SELECT * FROM operations ORDER BY created,id').fetchall()
            for op in legacy:
                sequence = op['command_sequence']
                if sequence <= 0:
                    sequence = next_sequence.get(op['booking_id'], 0) + 1
                next_sequence[op['booking_id']] = max(next_sequence.get(op['booking_id'], 0), sequence)
                if op['envelope'] is None or op['command_sequence'] <= 0:
                    booking = db.execute('SELECT * FROM bookings WHERE id=?', (op['booking_id'],)).fetchone()
                    roles = _json(booking['role_data']) if booking is not None else {}
                    envelope = {'protocol_version': PROTOCOL_VERSION, 'operation_id': op['id'],
                                'booking_id': op['booking_id'], 'host_id': booking['host_id'],
                                'installation_id': booking['installation_id'], 'generation': booking['generation'],
                                'session_generation': booking['session_generation'], 'alias': booking['alias'],
                                'request_id': op['request_id'], 'sequence': sequence,
                                'expected_revision': op['expected_revision'], 'user_id': op['user_id'],
                                'action': op['action'], 'payload': _json(op['payload']),
                                'role_revision': booking['role_revision'],
                                'role_snapshot': {uid: role for uid, role in roles.items() if role in ('owner', 'controller')}}
                    db.execute('UPDATE operations SET command_sequence=?,envelope=? WHERE id=?',
                               (sequence, canonical(envelope).decode(), op['id']))
            db.execute('DROP INDEX IF EXISTS operation_command_sequence')
            db.execute('CREATE UNIQUE INDEX operation_command_sequence ON operations(booking_id,command_sequence) WHERE command_sequence>0')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(bookings)')}
            if 'role_revision' not in columns:
                db.execute('ALTER TABLE bookings ADD COLUMN role_revision INTEGER NOT NULL DEFAULT 0')
            if 'applied_settings' not in columns:
                db.execute('ALTER TABLE bookings ADD COLUMN applied_settings TEXT')
            if 'lease_envelope' not in columns:
                db.execute('ALTER TABLE bookings ADD COLUMN lease_envelope TEXT')
            for row in db.execute("SELECT * FROM bookings WHERE lease_envelope IS NULL AND phase!='ended'").fetchall():
                request = db.execute('SELECT response FROM requests WHERE booking_id=? ORDER BY rowid LIMIT 1',
                                     (row['id'],)).fetchone()
                source = _json(request['response']) if request is not None else dict(row)
                db.execute('UPDATE bookings SET lease_envelope=? WHERE id=?',
                           (canonical(_creation_lease(source)).decode(), row['id']))
            db.execute("UPDATE bookings SET phase='unavailable' WHERE phase='failed'")
            # A terminal row means the host reported owned closure and
            # completed restoration. Scrub pre-existing ended rows on upgrade
            # too, so restart never rehydrates their creation password.
            for row in db.execute("SELECT id,revision FROM bookings WHERE phase='ended' AND (password!='' OR lease_envelope IS NOT NULL OR room_id IS NOT NULL OR incarnation IS NOT NULL OR role_data!='{}')").fetchall():
                self._scrub_terminal_booking(db, row['id'], row['revision'])

    def _scrub_terminal_booking(self, db, booking_id, revision):
        """Remove session access material while retaining request/receipt identity."""
        booking = db.execute('SELECT * FROM bookings WHERE id=? AND phase=?',
                             (booking_id, 'ended')).fetchone()
        require(booking is not None, 'booking_not_closed', 409)
        observation = _json(booking['observation']) or {}
        observation.update(roster=[], roster_known=False, room_id=None, incarnation=None,
                           current_map=None, queue=[], queue_known=False)
        db.execute('''UPDATE bookings SET password='',lease_envelope=NULL,room_id=NULL,
            incarnation=NULL,role_data='{}',observation=? WHERE id=?''',
                   (canonical(observation).decode(), booking_id))
        # Operation UUIDs, actors, payload hashes, phases, and receipts remain
        # useful for idempotency/audit. Their old envelopes and payloads are no
        # longer executable after terminal cleanup and may contain role IDs.
        db.execute("UPDATE operations SET payload='{}',envelope='{}' WHERE booking_id=?",
                   (booking_id,))
        terminal_receipt = {
            'id': booking_id, 'booking_id': booking_id,
            'protocol_version': PROTOCOL_VERSION, 'phase': 'ended',
            'revision': revision, 'password': None, 'join_url': None,
            'room_id': None, 'players': [], 'current_map': None,
            'queue': [], 'queue_known': False,
        }
        db.execute('UPDATE requests SET response=? WHERE booking_id=?',
                   (canonical(terminal_receipt).decode(), booking_id))

        # Exact exchange replay is retained while a lease can still be active.
        # Once confirmed cleanup retires that lease, its password/commands must
        # not survive in the cached response or be delivered after alias reuse.
        # Only retire the exact ended binding; never add new commands to an old
        # sequence or scrub an uncertain lease.
        reports_path = self.root / 'bookings-hosts.sqlite3'
        if os.path.lexists(reports_path):
            check_database(reports_path)
            attached = {row['name'] for row in db.execute('PRAGMA database_list')}
            if 'booking_reports' not in attached:
                db.execute('ATTACH DATABASE ? AS booking_reports', (str(reports_path),))
                db.execute('PRAGMA booking_reports.secure_delete=ON')
            columns = {row['name'] for row in db.execute(
                'PRAGMA booking_reports.table_info(reports)')}
            if columns:
                require({'host','response'} <= columns, 'booking_exchange_storage_uncertain', 503)
                for report in db.execute('SELECT host,response FROM booking_reports.reports').fetchall():
                    response = _json(report['response'])
                    require(type(response) is dict and type(response.get('leases')) is list
                            and type(response.get('commands')) is list,
                            'booking_exchange_storage_uncertain', 503)
                    retired = {**response,
                        'leases': [lease for lease in response['leases'] if lease.get('id') != booking_id],
                        'commands': [command for command in response['commands']
                                     if command.get('booking_id') != booking_id]}
                    if retired != response:
                        db.execute('UPDATE booking_reports.reports SET response=? WHERE host=?',
                                   (canonical(retired).decode(), report['host']))

        # Browser invite hashes live in their own private ledger. Attach it only
        # when it already exists; a hub without the portal must remain inert.
        browser_path = self.root / 'booking-browser.sqlite3'
        if os.path.lexists(browser_path):
            check_database(browser_path)
            attached = {row['name'] for row in db.execute('PRAGMA database_list')}
            if 'booking_browser' not in attached:
                db.execute('ATTACH DATABASE ? AS booking_browser', (str(browser_path),))
                db.execute('PRAGMA booking_browser.secure_delete=ON')
            columns = {row['name'] for row in db.execute(
                'PRAGMA booking_browser.table_info(join_invites)')}
            if columns:
                require('booking' in columns, 'booking_invite_storage_uncertain', 503)
                db.execute('DELETE FROM booking_browser.join_invites WHERE booking=?',
                           (booking_id,))

    @staticmethod
    def _booking(row):
        if row is None:
            return None
        data = dict(row)
        for key in ('settings', 'observation', 'role_data'):
            data[key] = _json(data[key])
        data['lease_envelope'] = _json(data['lease_envelope']) if data.get('lease_envelope') else None
        data['password'] = data['password']
        return data

    def _read_booking(self, db, booking_id):
        return self._booking(db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone())

    @staticmethod
    def _active(row):
        return row is not None and row['phase'] in ACTIVE

    def configure_slot(self, slot_id, host_id, alias, label, presets, base_settings, enabled=False):
        """Create/update a slot. This method is for explicit local administration."""
        require(type(slot_id) is str and SLOT_ID.fullmatch(slot_id), 'invalid_slot_id')
        identifier(host_id)
        require(alias in ALIASES, 'invalid_alias')
        text(label, 80)
        require(type(enabled) is bool, 'invalid_enabled')
        require(type(presets) in (list, tuple) and 1 <= len(presets) <= 5, 'invalid_presets')
        require(len(set(presets)) == len(presets) and all(p in PRESETS for p in presets), 'invalid_presets')
        require(type(base_settings) is dict, 'invalid_settings')
        normalized = self._validate_settings(base_settings, base_settings)
        require(bool(normalized), 'empty_settings')
        now = _now(self.clock())
        with self._transaction() as db:
            existing = db.execute('SELECT * FROM slots WHERE slot_id=?', (slot_id,)).fetchone()
            if existing and self._active(db.execute("SELECT * FROM bookings WHERE slot_id=? AND phase!='ended'", (slot_id,)).fetchone()):
                require((existing['host_id'], existing['alias']) == (host_id, alias), 'slot_in_use', 409)
            db.execute('''INSERT INTO slots(slot_id,host_id,alias,label,presets,base_settings,enabled)
                          VALUES(?,?,?,?,?,?,?) ON CONFLICT(slot_id) DO UPDATE SET
                          host_id=excluded.host_id,alias=excluded.alias,label=excluded.label,
                          presets=excluded.presets,base_settings=excluded.base_settings,enabled=excluded.enabled''',
                       (slot_id, host_id, alias, label, canonical(list(presets)).decode(),
                        canonical(normalized).decode(), int(enabled)))
        return {'slot_id': slot_id, 'host_id': host_id, 'alias': alias, 'label': label,
                'presets': list(presets), 'base_settings': normalized, 'enabled': enabled}

    @staticmethod
    def _validate_settings(value, current=None, preset=None):
        from .settings import validate_player_patch
        result = validate_player_patch(value, current=current, preset=preset)
        require(type(result) is dict, 'invalid_settings')
        return result

    def _eligibility(self, host_id, alias):
        if self.eligibility is None:
            raise NetworkError('host_ineligible', 503)
        try:
            value = self.eligibility(host_id, alias)
        except Exception:
            raise NetworkError('host_ineligible', 503) from None
        require(type(value) is dict and value.get('host_id') == host_id
                and type(value.get('supported')) is bool and type(value.get('fresh')) is bool, 'host_ineligible', 409)
        identifier(value.get('installation_id')); identifier(value.get('generation'))
        require(type(value.get('effective_limit')) is int and value['effective_limit'] >= 1
                and type(value.get('capacity_used')) is int and value['capacity_used'] >= 0,
                'host_ineligible', 409)
        ids = value.get('observed_booking_ids')
        supported_presets = value.get('supported_presets', [])
        require(type(ids) is list and all(type(x) is str for x in ids)
                and type(supported_presets) is list and all(x in PRESETS for x in supported_presets), 'host_ineligible', 409)
        return value

    def _project_slot(self, db, slot, now):
        booking = db.execute("SELECT * FROM bookings WHERE slot_id=? AND phase!='ended'", (slot['slot_id'],)).fetchone()
        state = ('unavailable' if booking['phase'] not in ('preparing', 'awaiting_claim', 'reserved', 'active', 'closing') else
                 'reserved' if booking['phase'] == 'awaiting_claim' else booking['phase']) if booking else 'available'
        fresh = False
        supported = False
        reason = 'host_unavailable'
        try:
            eligible = self._eligibility(slot['host_id'], slot['alias'])
            fresh = bool(eligible['fresh'])
            supported = bool(eligible['supported'])
            reason = None if fresh and supported else 'host_unavailable'
        except NetworkError:
            pass
        if booking is None:
            if not slot['enabled']:
                state, reason = 'unavailable', 'disabled'
            elif now < slot['cooldown_until']:
                state, reason = 'unavailable', 'cooldown'
            elif not fresh or not supported:
                state, reason = 'unavailable', 'host_unavailable' if not fresh else 'unsupported'
            else:
                try:
                    reservations = db.execute("SELECT id FROM bookings WHERE host_id=? AND phase!='ended'", (slot['host_id'],)).fetchall()
                    pending = sum(1 for item in reservations if item['id'] not in eligible['observed_booking_ids'])
                    if eligible['capacity_used'] + pending >= eligible['effective_limit']:
                        state, reason = 'unavailable', 'host_capacity'
                except (UnboundLocalError, NetworkError):
                    state, reason = 'unavailable', 'host_unavailable'
        elif booking['phase'] in ('unavailable', 'failed'):
            state = 'unavailable'
        return {'slot_id': slot['slot_id'], 'label': slot['label'], 'state': state,
                'fresh': fresh, 'supported_presets': [p for p in _json(slot['presets']) if p in eligible.get('supported_presets', _json(slot['presets']))] if fresh and supported and slot['enabled'] and state != 'unavailable' else [],
                **({'reason': reason} if reason else {})}

    def public_slots(self):
        now = _now(self.clock())
        with self._transaction() as db:
            rows = db.execute('SELECT * FROM slots ORDER BY slot_id').fetchall()
            return [self._project_slot(db, row, now) for row in rows]

    def public_assignments(self):
        """Internal read-only host/alias mapping paired with safe public slot docs."""
        now = _now(self.clock())
        with self._transaction() as db:
            rows = db.execute('SELECT * FROM slots ORDER BY slot_id').fetchall()
            return [{'host_id': row['host_id'], 'alias': row['alias'],
                     'slot': self._project_slot(db, row, now)} for row in rows]

    def reserve(self, user_id, request_id, slot_id, preset, settings, designated_friend_id=None):
        require(type(user_id) is int and user_id > 0, 'invalid_user')
        identifier(request_id)
        require(type(slot_id) is str and SLOT_ID.fullmatch(slot_id), 'invalid_slot_id')
        require(designated_friend_id is None or (type(designated_friend_id) is int and designated_friend_id > 0), 'invalid_friend')
        require(type(settings) is dict, 'invalid_settings')
        payload = {'slot_id': slot_id, 'preset': preset, 'settings': settings, 'designated_friend_id': designated_friend_id}
        payload_hash = hashlib.sha256(canonical(payload)).hexdigest()
        now = _now(self.clock())
        with self._transaction() as db:
            old_req = db.execute('SELECT * FROM requests WHERE user_id=? AND request_id=?', (user_id, request_id)).fetchone()
            if old_req:
                require(old_req['payload_hash'] == payload_hash, 'idempotency_conflict', 409)
                ended = db.execute("SELECT id,revision FROM bookings WHERE id=? AND phase='ended'",
                                   (old_req['booking_id'],)).fetchone()
                if ended is not None:
                    self._scrub_terminal_booking(db, ended['id'], ended['revision'])
                    old_req = db.execute('SELECT response FROM requests WHERE user_id=? AND request_id=?',
                                         (user_id, request_id)).fetchone()
                    return _json(old_req['response'])
                return _json(old_req['response'])
            cooldown = db.execute('SELECT cooldown_until FROM no_show_cooldowns WHERE user_id=?', (user_id,)).fetchone()
            require(cooldown is None or now >= cooldown['cooldown_until'], 'no_show_cooldown', 409)
            slot = db.execute('SELECT * FROM slots WHERE slot_id=?', (slot_id,)).fetchone()
            require(slot is not None, 'slot_not_found', 404)
            require(slot['enabled'], 'slot_unavailable', 409)
            require(now >= slot['cooldown_until'], 'slot_cooldown', 409)
            require(preset in _json(slot['presets']) and preset in PRESETS, 'unsupported_preset', 409)
            eligible = self._eligibility(slot['host_id'], slot['alias'])
            require(eligible['fresh'] and eligible['supported'], 'host_ineligible', 409)
            require(preset in eligible['supported_presets'], 'unsupported_preset', 409)
            existing = db.execute("SELECT * FROM bookings WHERE user_id=? AND phase!='ended'", (user_id,)).fetchone()
            require(existing is None, 'booking_already_active', 409)
            existing = db.execute("SELECT * FROM bookings WHERE slot_id=? AND phase!='ended'", (slot_id,)).fetchone()
            require(existing is None, 'slot_unavailable', 409)
            db.execute('DELETE FROM reserve_events WHERE at<?', (now - 60,))
            recent = db.execute('SELECT COUNT(*) AS n FROM reserve_events WHERE user_id=? AND at>?', (user_id, now - 60)).fetchone()['n']
            require(recent < 10, 'reservation_rate_limited', 429)
            reservations = db.execute("SELECT id FROM bookings WHERE host_id=? AND phase!='ended'", (slot['host_id'],)).fetchall()
            not_observed = sum(1 for r in reservations if r['id'] not in eligible['observed_booking_ids'])
            require(eligible['capacity_used'] + not_observed < eligible['effective_limit'], 'host_capacity', 409)
            base_settings = _json(slot['base_settings'])
            merged = dict(base_settings)
            merged.update(self._validate_settings(settings, current=base_settings, preset=preset))
            booking_id = _uuid()
            password = 'prb_' + secrets.token_urlsafe(24)
            generation = secrets.token_hex(16)
            role_data = {str(user_id): 'owner'}
            response = {'id': booking_id, 'protocol_version': PROTOCOL_VERSION,
                        'host_id': slot['host_id'], 'installation_id': eligible['installation_id'],
                        'generation': eligible['generation'], 'slot_id': slot_id, 'alias': slot['alias'],
                        'session_generation': generation, 'user_id': user_id,
                        'preset': preset, 'settings': merged, 'password': password,
                        'phase': 'preparing', 'revision': 0, 'created': now,
                        'designated_friend_id': designated_friend_id, 'players': [], 'operations': [],
                        'claim_deadline': None, 'cooldown_until': slot['cooldown_until']}
            lease_envelope = _creation_lease(response)
            db.execute('''INSERT INTO bookings(id,user_id,slot_id,host_id,installation_id,generation,alias,session_generation,preset,settings,password,
                          phase,revision,created,designated_friend_id,role_data,applied_settings,lease_envelope) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (booking_id, user_id, slot_id, slot['host_id'], eligible['installation_id'], eligible['generation'],
                        slot['alias'], generation, preset, canonical(merged).decode(), password, 'preparing', 0, now,
                        designated_friend_id, canonical(role_data).decode(), canonical(merged).decode(),
                        canonical(lease_envelope).decode()))
            db.execute('INSERT INTO reserve_events VALUES(?,?)', (user_id, now))
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?)', (user_id, request_id, payload_hash, booking_id, canonical(response).decode()))
            return response

    def _authorized_view(self, db, booking_id, user_id):
        row = db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
        require(row is not None, 'booking_not_found', 404)
        roles = _json(row['role_data'])
        ended = row['phase'] == 'ended'
        if ended:
            # Cleanup may erase the active role map. The original requester
            # retains only a read-only terminal receipt; controllers do not.
            require(user_id == row['user_id'], 'booking_forbidden', 403)
            role = 'owner'
        else:
            role = roles.get(str(user_id))
            require(role in ('owner', 'controller'), 'booking_forbidden', 403)
        operations = db.execute('SELECT id,action,phase,error,application FROM operations WHERE booking_id=? ORDER BY created,id', (booking_id,)).fetchall()
        observation = _json(row['observation']) or {}
        activity = {'condition': 'unknown', 'closes_at': None}
        activity_report = observation.get('activity')
        updated_at = observation.get('updated_at')
        now = _now(self.clock())
        if (not ended and row['phase'] == 'active' and activity_report is not None and
                type(updated_at) in (int, float) and 0 <= now - updated_at <= 30 and
                observation.get('roster_known') is True):
            activity = dict(activity_report)
        slot = db.execute('SELECT cooldown_until FROM slots WHERE slot_id=?', (row['slot_id'],)).fetchone()
        players = []
        if not ended:
            for player in observation.get('roster', []):
                uid = player['user_id']
                players.append({'user_id': uid, 'username': player['username'],
                                'role': 'owner' if uid == row['user_id'] else roles.get(str(uid), 'player')})
        join_url = f"https://osu.ppy.sh/multiplayer/rooms/{row['room_id']}" if row['room_id'] and not ended else None
        return {'id': row['id'], 'phase': row['phase'], 'revision': row['revision'], 'role': role,
                'slot_id': row['slot_id'], 'preset': row['preset'], 'room_id': None if ended else row['room_id'],
                'join_url': join_url, 'password': None if ended else row['password'],
                'claim_deadline': row['claim_deadline'], 'roster_known': False if ended else observation.get('roster_known', False), 'players': players,
                'activity': activity,
                'activity_updated_at': updated_at if not ended and type(updated_at) in (int, float) else None,
                'settings': _json(row['settings']), 'applied_settings': _json(row['applied_settings']) if row['applied_settings'] else {},
                'settings_revision':observation.get('settings_revision',0),
                'failure_code':observation.get('failure_code'),
                'current_map':None if ended else observation.get('current_map'),
                'queue':[]if ended else observation.get('queue',[]),'queue_known':False if ended else observation.get('queue_known',False),
                'operations': [dict(x) for x in operations], 'cooldown_until': slot['cooldown_until'] if slot else 0}

    def view(self, booking_id, user_id):
        identifier(booking_id)
        with self._transaction() as db:
            return self._authorized_view(db, booking_id, user_id)

    def submit(self, booking_id, user_id, request_id, action, payload, expected_revision):
        identifier(booking_id); identifier(request_id)
        require(type(user_id) is int and user_id > 0, 'invalid_user')
        require(action in ACTIONS and type(payload) is dict and len(canonical(payload)) <= 16384, 'invalid_action')
        require(type(expected_revision) is int and expected_revision >= 0, 'invalid_revision')
        request_hash = hashlib.sha256(canonical({'action': action, 'payload': payload, 'expected_revision': expected_revision})).hexdigest()
        now = _now(self.clock())
        with self._transaction() as db:
            prior = db.execute('SELECT * FROM operations WHERE booking_id=? AND user_id=? AND request_id=?',
                                (booking_id, user_id, request_id)).fetchone()
            if prior:
                row = db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
                require(row is not None, 'booking_not_found', 404)
                roles = _json(row['role_data'])
                # The owner may retry an operation after a lost reply even if
                # that operation ended the booking and cleanup erased roles.
                # A revoked controller cannot use an old request ID to regain
                # access to its prior response.
                require(user_id == row['user_id'] or
                        roles.get(str(user_id)) == 'controller',
                        'booking_forbidden', 403)
                require(prior['payload_hash'] == request_hash, 'idempotency_conflict', 409)
                return dict(prior)
            row = db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
            require(row is not None, 'booking_not_found', 404)
            require(row['phase'] in ACTIVE, 'booking_closed', 409)
            roles = _json(row['role_data'])
            role = roles.get(str(user_id))
            require(role in ('owner', 'controller'), 'booking_forbidden', 403)
            if action in ('end', 'grant', 'revoke'):
                require(role == 'owner', 'owner_required', 403)
            require(row['revision'] == expected_revision, 'revision_conflict', 409)
            if action not in ('end','revoke'):
                queued=db.execute("SELECT COUNT(*) FROM operations WHERE booking_id=? AND phase IN ('pending','dispatched')",(booking_id,)).fetchone()[0]
                require(queued<32,'booking_command_queue_full',429)
            operation_payload = payload
            if action == 'configure':
                require(bool(payload), 'empty_settings')
                slot = db.execute('SELECT * FROM slots WHERE slot_id=?', (row['slot_id'],)).fetchone()
                require(now >= slot['cooldown_until'], 'configure_cooldown', 409)
                pending = db.execute("SELECT 1 FROM operations WHERE booking_id=? AND action='configure' AND phase IN ('pending','dispatched')", (booking_id,)).fetchone()
                require(pending is None, 'configure_pending', 409)
                patch = self._validate_settings(payload, current=_json(row['settings']), preset=row['preset'])
                require(bool(patch), 'empty_settings')
                merged = dict(_json(row['settings'])); merged.update(patch)
                operation_payload = merged
                db.execute('UPDATE slots SET cooldown_until=? WHERE slot_id=?', (now + 10, row['slot_id']))
            elif action == 'grant':
                require(set(payload) == {'user_id'}, 'invalid_action_payload')
                friend = payload.get('user_id')
                require(type(friend) is int and friend > 0 and friend != row['user_id'], 'invalid_friend')
                require(roles.get(str(friend)) not in ('owner', 'controller', 'grant_pending'),
                        'invalid_controller', 409)
                roster_observation = _json(row['observation']) or {}
                roster = roster_observation.get('roster', [])
                require(roster_observation.get('roster_known') is True and now - roster_observation.get('updated_at', -1) <= 30
                        and 0 <= now - roster_observation.get('updated_at', -1), 'roster_unavailable', 409)
                require(any(type(p) is dict and p.get('user_id') == friend for p in roster), 'player_not_in_roster', 409)
                roles[str(friend)] = 'grant_pending'
            elif action == 'revoke':
                require(set(payload) == {'user_id'}, 'invalid_action_payload')
                friend = payload.get('user_id')
                require(type(friend) is int and friend > 0 and
                        roles.get(str(friend)) in ('controller', 'grant_pending'),
                        'invalid_controller', 409)
                target = str(friend)
                roles.pop(target, None)
                queued = db.execute("SELECT id,action,payload,user_id,phase FROM operations WHERE booking_id=? AND phase IN ('pending','dispatched')", (booking_id,)).fetchall()
                for queued_op in queued:
                    if queued_op['action'] == 'grant' and str(_json(queued_op['payload']).get('user_id')) == target:
                        db.execute("UPDATE operations SET phase='cancelled',error='role_revoked' WHERE id=?", (queued_op['id'],))
                    elif queued_op['user_id'] == friend:
                        db.execute("UPDATE operations SET phase='cancelled',error='role_revoked' WHERE id=?", (queued_op['id'],))
            elif action in ('start', 'abort', 'skip', 'end'):
                require(not payload, 'invalid_action_payload')
                if action == 'end':
                    db.execute("UPDATE operations SET phase='cancelled',error='booking_ending' WHERE booking_id=? AND phase IN ('pending','dispatched')", (booking_id,))
            else:
                raise NetworkError('invalid_action')

            role_revision = row['role_revision'] + int(action in ('end', 'grant', 'revoke'))
            command_sequence = db.execute('SELECT COALESCE(MAX(command_sequence),0)+1 FROM operations WHERE booking_id=?', (booking_id,)).fetchone()[0]
            role_snapshot = {uid: active_role for uid, active_role in roles.items()
                             if active_role in ('owner', 'controller')}
            envelope = {'protocol_version': PROTOCOL_VERSION, 'operation_id': _uuid(),
                        'booking_id': booking_id, 'host_id': row['host_id'],
                        'installation_id': row['installation_id'], 'generation': row['generation'],
                        'session_generation': row['session_generation'], 'alias': row['alias'],
                        'request_id': request_id, 'sequence': command_sequence,
                        'expected_revision': expected_revision, 'user_id': user_id,
                        'action': action, 'payload': operation_payload,
                        'role_revision': role_revision, 'role_snapshot': role_snapshot}
            if action=='configure':
                envelope['expected_settings_revision']=(_json(row['observation'])or{}).get('settings_revision',0)
            db.execute('''INSERT INTO operations(id,booking_id,user_id,request_id,payload_hash,action,payload,
                          expected_revision,phase,error,application,created,command_sequence,envelope,host_receipt)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)''',
                       (envelope['operation_id'], booking_id, user_id, request_id, request_hash, action,
                        canonical(operation_payload).decode(), expected_revision, 'pending', None, None, now,
                        command_sequence, canonical(envelope).decode()))
            if action == 'end':
                db.execute("UPDATE bookings SET phase='closing',revision=revision+1,role_revision=?,role_data=? WHERE id=?",
                           (role_revision, canonical(roles).decode(), booking_id))
            else:
                db.execute('UPDATE bookings SET revision=revision+1,role_revision=?,role_data=? WHERE id=?',
                           (role_revision, canonical(roles).decode(), booking_id))
            return dict(db.execute('SELECT * FROM operations WHERE id=?', (envelope['operation_id'],)).fetchone())

    def pending(self, host_id):
        identifier(host_id)
        with self._transaction() as db:
            rows = db.execute('''SELECT o.*,b.installation_id,b.generation,b.alias,b.session_generation,b.phase AS booking_phase
                                 FROM operations o JOIN bookings b ON b.id=o.booking_id
                                 WHERE b.host_id=? AND o.phase IN ('pending','dispatched') AND b.phase IN (%s)
                                 ORDER BY o.command_sequence,o.id LIMIT 32''' % ','.join('?' for _ in ACTIVE), (host_id, *ACTIVE)).fetchall()
            result = []
            for op in rows:
                booking = self._read_booking(db, op['booking_id'])
                roles = booking['role_data']
                try:
                    eligible = self._eligibility(host_id, booking['alias'])
                except NetworkError:
                    continue
                if not eligible['fresh'] or not eligible['supported']:
                    continue
                if not self._binding_matches(booking, host_id, eligible['installation_id'], eligible['generation']):
                    continue
                actor_role = roles.get(str(op['user_id']))
                if op['action'] in ('grant', 'revoke', 'end'):
                    authorized = actor_role == 'owner'
                else:
                    authorized = actor_role in ('owner', 'controller')
                if not authorized:
                    db.execute("UPDATE operations SET phase='cancelled',error='role_revoked' WHERE id=?", (op['id'],))
                    continue
                if op['action'] == 'grant' and roles.get(str(_json(op['payload']).get('user_id'))) != 'grant_pending':
                    db.execute("UPDATE operations SET phase='cancelled',error='role_revoked' WHERE id=?", (op['id'],))
                    continue
                envelope = _json(op['envelope'])
                require(type(envelope) is dict and envelope.get('operation_id') == op['id'], 'booking_operation_corrupt', 503)
                result.append(envelope)
                db.execute("UPDATE operations SET phase='dispatched' WHERE id=? AND phase='pending'", (op['id'],))
            return result

    @staticmethod
    def _binding_matches(booking, host_id, installation_id, generation):
        return (booking['host_id'], booking['installation_id'], booking['generation']) == (host_id, installation_id, generation)

    @staticmethod
    def _session_envelope(booking):
        if booking.get('lease_envelope') is not None:
            return copy.deepcopy(booking['lease_envelope'])
        return _creation_lease(booking)

    def leases(self, host_id, installation_id, generation):
        """Return active creation leases for the exact authenticated host generation."""
        identifier(host_id); identifier(installation_id); identifier(generation)
        with self._transaction() as db:
            rows = db.execute("SELECT * FROM bookings WHERE host_id=? AND installation_id=? AND generation=? AND phase!='ended' ORDER BY created,id",
                              (host_id, installation_id, generation)).fetchall()
            return [self._session_envelope(self._booking(row)) for row in rows]

    def role(self, booking_id, user_id):
        identifier(booking_id)
        require(type(user_id) is int and user_id > 0, 'invalid_user')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
            require(row is not None, 'booking_not_found', 404)
            if row['phase'] == 'ended':
                return 'player'
            roles = _json(row['role_data'])
            if roles.get(str(user_id)) in ('owner', 'controller'):
                return roles[str(user_id)]
            roster = (_json(row['observation']) or {}).get('roster', [])
            return 'player' if any(p['user_id'] == user_id for p in roster) else 'player'

    def active_for_user(self, user_id):
        require(type(user_id) is int and user_id > 0, 'invalid_user')
        with self._transaction() as db:
            rows = db.execute("SELECT * FROM bookings WHERE phase!='ended' ORDER BY created,id").fetchall()
            for row in rows:
                roles = _json(row['role_data'])
                if roles.get(str(user_id)) in ('owner', 'controller'):
                    return self._authorized_view(db, row['id'], user_id)
            return None

    def join_info(self, booking_id):
        """Internal minimal invite lookup; callers must enforce invite authorization."""
        identifier(booking_id)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
            require(row is not None, 'booking_not_found', 404)
            if row['phase'] == 'ended':
                return {'booking_id': booking_id, 'phase': 'ended', 'join_url': None, 'password': None}
            url = f"https://osu.ppy.sh/multiplayer/rooms/{row['room_id']}" if row['room_id'] else None
            return {'booking_id': booking_id, 'phase': row['phase'], 'join_url': url, 'password': row['password']}

    def observe(self, host_id, booking_id, observation):
        identifier(host_id); identifier(booking_id)
        require(type(observation) is dict, 'invalid_observation')
        required = {'sequence', 'phase', 'room_id', 'incarnation', 'roster', 'roster_known', 'updated_at',
                    'ready', 'closed', 'effective_settings', 'cleanup_complete', 'ownership_known',
                    'operation_receipts', 'installation_id', 'generation', 'session_generation', 'alias'}
        optional = {'terminal_reason', 'reason', 'activity'}
        optional.update(('claim_deadline','settings_revision','failure_code','current_map','queue','queue_known'))
        require(required <= set(observation) and not set(observation) - required - optional
                and not ({'terminal_reason', 'reason'} <= set(observation)), 'invalid_observation')
        require(type(observation['sequence']) is int and observation['sequence'] >= 0, 'invalid_sequence')
        require(observation['phase'] in ('preparing', 'awaiting_claim', 'reserved', 'active', 'closing', 'ended', 'failed', 'unavailable'), 'invalid_phase')
        require(observation['room_id'] is None or (type(observation['room_id']) is int and observation['room_id'] > 0), 'invalid_room_id')
        require(observation['incarnation'] is None or (type(observation['incarnation']) is str and 0 < len(observation['incarnation']) <= 128), 'invalid_incarnation')
        require(type(observation['roster']) is list and len(observation['roster']) <= 256 and type(observation['roster_known']) is bool, 'invalid_roster')
        updated_at = _now(observation['updated_at'])
        reported_claim_deadline = observation.get('claim_deadline')
        if reported_claim_deadline is not None:
            reported_claim_deadline = _now(reported_claim_deadline)
        from .contracts import FAILURES,map_info,queue_info
        settings_revision=observation.get('settings_revision',0)
        require(type(settings_revision)is int and 0<=settings_revision<=2**53-1,'invalid_settings_revision')
        require(observation.get('failure_code')is None or observation['failure_code']in FAILURES,'invalid_failure_code')
        if 'current_map'in observation:map_info(observation['current_map'])
        if 'queue'in observation:queue_info(observation['queue'])
        if 'queue_known'in observation:require(type(observation['queue_known'])is bool,'invalid_queue')
        if 'activity'in observation:activity_info(observation['activity'])
        require(type(observation['ready']) is bool and type(observation['closed']) is bool, 'invalid_observation')
        identifier(observation['installation_id']); identifier(observation['generation'])
        require(type(observation['session_generation']) is str and re.fullmatch(r'[a-f0-9]{32}', observation['session_generation'])
                and observation['alias'] in ALIASES, 'invalid_observation')
        seen_players = set()
        for player in observation['roster']:
            require(type(player) is dict and set(player) == {'user_id', 'username'}
                    and type(player['user_id']) is int and player['user_id'] > 0, 'invalid_roster')
            text(player['username'], 64)
            require(player['user_id'] not in seen_players, 'invalid_roster')
            seen_players.add(player['user_id'])
        require(type(observation['effective_settings']) is dict and type(observation['cleanup_complete']) is bool
                and type(observation['ownership_known']) is bool and type(observation['operation_receipts']) is list, 'invalid_observation')
        reason = observation.get('terminal_reason', observation.get('reason'))
        require(reason is None or reason in ('claim_timeout', 'inactive_timeout', 'host_closed'), 'invalid_terminal_reason')
        safe_receipts = []
        for item in observation['operation_receipts']:
            require(type(item) is dict and set(item) <= {'operation_id', 'phase', 'error', 'application'}
                    and {'operation_id', 'phase'} <= set(item), 'invalid_operation_receipt')
            identifier(item['operation_id'])
            require(item['phase'] in ('applied', 'failed', 'cancelled'), 'invalid_operation_receipt')
            error = item.get('error')
            require(error is None or (type(error) is str and error in SAFE_OPERATION_ERRORS), 'invalid_operation_receipt')
            application = item.get('application')
            require(application is None or application in ('live', 'next_map', 'recreate'), 'invalid_operation_receipt')
            safe_receipts.append({'operation_id': item['operation_id'], 'phase': item['phase'], 'error': error, 'application': application})
        sanitized = {key: observation[key] for key in required}
        for key in ('settings_revision','failure_code','current_map','queue','queue_known','activity'):
            if key in observation:sanitized[key]=observation[key]
        if 'claim_deadline' in observation:
            sanitized['claim_deadline'] = reported_claim_deadline
        sanitized['terminal_reason'] = reason
        sanitized['roster'] = [dict(user_id=p['user_id'], username=p['username']) for p in observation['roster']]
        # Validate against the booking's preset once its durable binding is loaded.
        require(len(canonical(sanitized)) <= 65536 and len(safe_receipts) <= 32, 'invalid_observation', 413)
        digest = None
        now = _now(self.clock())
        with self._transaction() as db:
            row = db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
            require(row is not None and row['host_id'] == host_id, 'booking_not_found', 404)
            require((row['installation_id'], row['generation'], row['session_generation'], row['alias']) ==
                    (observation['installation_id'], observation['generation'], observation['session_generation'], observation['alias']), 'host_generation_mismatch', 409)
            safe_settings = self._validate_settings(observation['effective_settings'], preset=row['preset'])
            require(safe_settings == observation['effective_settings'], 'invalid_effective_settings')
            sanitized['effective_settings'] = safe_settings
            sanitized['operation_receipts'] = safe_receipts
            digest = hashlib.sha256(canonical(sanitized)).hexdigest()
            prior = db.execute('SELECT * FROM host_observations WHERE booking_id=?', (booking_id,)).fetchone()
            if prior:
                if observation['sequence'] < prior['sequence']:
                    raise NetworkError('stale_observation', 409)
                if observation['sequence'] == prior['sequence']:
                    require(prior['payload_hash'] == digest, 'observation_conflict', 409)
                    return _json(prior['receipt'])
            current = self._booking(row)
            receipt = {'booking_id': booking_id, 'sequence': observation['sequence'], 'accepted': True, 'received_at': now}
            closure = observation['closed'] and observation['ownership_known'] and observation['room_id'] is None and observation['cleanup_complete']
            terminal = observation['phase'] in ('ended', 'failed') and closure
            if current['phase']in TERMINAL:
                require(terminal,'booking_closed',409)
                # Hosts retain a bounded terminal history to recover lost replies.
                # New report sequences may repeat confirmed cleanup, never reopen it.
                self._scrub_terminal_booking(db, booking_id, current['revision'])
                db.execute('INSERT OR REPLACE INTO host_observations VALUES(?,?,?,?)',
                           (booking_id,observation['sequence'],digest,canonical(receipt).decode()))
                return receipt
            previous_settings_revision=(current['observation']or{}).get('settings_revision',0)
            require(settings_revision>=previous_settings_revision,'stale_settings_revision',409)
            phase_map = {'failed': 'unavailable', 'unavailable': 'unavailable', 'ended': 'unavailable'}
            new_phase = 'ended' if terminal else phase_map.get(observation['phase'], observation['phase'])
            if current['phase'] == 'closing' and not terminal:
                new_phase = 'closing'
            elif current['phase'] == 'unavailable' and not terminal:
                same_known_room = (current['room_id'] is not None and observation['room_id'] == current['room_id']
                                   and observation['incarnation'] == current['incarnation'])
                if new_phase not in ('reserved', 'active') or not same_known_room:
                    new_phase = 'unavailable'
            else:
                rank = {'preparing': 0, 'awaiting_claim': 1, 'reserved': 1, 'active': 2}
                old_rank = rank.get(current['phase'])
                new_rank = rank.get(new_phase)
                if old_rank is not None and new_rank is not None and new_rank < old_rank:
                    new_phase = current['phase']
            claim_deadline = current['claim_deadline']
            if claim_deadline is None and observation['ready'] and observation['ownership_known'] and observation['room_id'] is not None and observation['incarnation'] is not None:
                require(0 <= now - updated_at <= 30, 'stale_observation', 409)
                claim_deadline = reported_claim_deadline if reported_claim_deadline is not None else updated_at + 600
                require(current['created']-30 <= claim_deadline-600 <= updated_at+30,
                        'invalid_claim_deadline')
                if new_phase == 'preparing':
                    new_phase = 'awaiting_claim'
            old_observation = current['observation'] or {}
            new_revision = current['revision']
            new_role_revision = row['role_revision']
            changed_visible = (new_phase != current['phase'] or observation['room_id'] != current['room_id']
                               or sanitized['roster'] != old_observation.get('roster', [])
                               or observation['roster_known'] != old_observation.get('roster_known', False)
                               or claim_deadline != current['claim_deadline']
                               or safe_settings != (current['applied_settings'] or {}))
            roles = current['role_data']
            for item in safe_receipts:
                op = db.execute('SELECT * FROM operations WHERE id=? AND booking_id=?', (item['operation_id'], booking_id)).fetchone()
                if op is None:
                    continue
                wire_receipt = canonical(item).decode()
                if op['phase'] == 'cancelled':
                    if op['host_receipt'] is None:
                        db.execute('UPDATE operations SET host_receipt=? WHERE id=?', (wire_receipt, op['id']))
                    else:
                        require(op['host_receipt'] == wire_receipt,
                                'operation_receipt_conflict', 409)
                    continue
                if op['phase'] in ('applied', 'failed'):
                    require((op['phase'], op['error'], op['application']) ==
                            (item['phase'], item['error'], item['application']), 'operation_receipt_conflict', 409)
                    if op['host_receipt'] is not None:
                        require(op['host_receipt'] == wire_receipt, 'operation_receipt_conflict', 409)
                    else:
                        db.execute('UPDATE operations SET host_receipt=? WHERE id=?', (wire_receipt, op['id']))
                    continue
                db.execute('UPDATE operations SET phase=?,error=?,application=?,host_receipt=? WHERE id=?',
                           (item['phase'], item['error'], item['application'], wire_receipt, op['id']))
                op_payload = _json(op['payload'])
                if op['action'] == 'grant':
                    uid = str(op_payload.get('user_id'))
                    if roles.get(uid) == 'grant_pending':
                        if item['phase'] == 'applied':
                            roles[uid] = 'controller'
                        else:
                            roles.pop(uid, None)
                        new_role_revision += 1
                changed_visible = True
            if changed_visible:
                new_revision += 1
            if terminal:
                # Configure cooldown belongs to the active room and ends with its verified cleanup.
                db.execute('UPDATE slots SET cooldown_until=0 WHERE slot_id=?', (current['slot_id'],))
                if reason == 'claim_timeout':
                    db.execute('INSERT INTO no_show_cooldowns(user_id,cooldown_until) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET cooldown_until=MAX(cooldown_until,excluded.cooldown_until)', (current['user_id'], now + 300))
                roles={}
                sanitized.update(roster=[],roster_known=False,current_map=None,queue=[],queue_known=False)
            new_room_id = observation['room_id'] if observation['room_id'] is not None else current['room_id']
            new_incarnation = observation['incarnation'] if observation['incarnation'] is not None else current['incarnation']
            db.execute('UPDATE bookings SET phase=?,revision=?,role_revision=?,room_id=?,incarnation=?,observation_sequence=?,observation_hash=?,observation=?,claim_deadline=?,terminal_reason=?,role_data=?,applied_settings=?,settings=? WHERE id=?',
                       (new_phase, new_revision, new_role_revision, new_room_id, new_incarnation, observation['sequence'], digest,
                        canonical(sanitized).decode(), claim_deadline, reason if terminal else None, canonical(roles).decode(),
                        canonical(safe_settings).decode(), canonical(safe_settings).decode(),booking_id))
            db.execute('INSERT OR REPLACE INTO host_observations VALUES(?,?,?,?)', (booking_id, observation['sequence'], digest, canonical(receipt).decode()))
            if terminal:
                self._scrub_terminal_booking(db, booking_id, new_revision)
            return receipt

    def observe_receipts(self, host_id, receipts):
        """Apply the host exchange's separate bounded terminal receipt queue.

        Receipt identities are checked against the immutable operation/session
        binding. Unknown receipts are stale queue entries and are ignored; a
        known operation from another host or session is rejected. A cancelled
        hub operation stays cancelled even if its already-dispatched worker
        later reports success.
        """
        identifier(host_id)
        require(type(receipts) is list and len(receipts) <= 32,
                'invalid_operation_receipts')
        now = _now(self.clock())
        safe = []
        for item in receipts:
            required = {'operation_id', 'booking_id', 'session_generation', 'alias', 'phase'}
            optional = {'error', 'application'}
            require(type(item) is dict and required <= set(item) and
                    not set(item) - required - optional,
                    'invalid_operation_receipt')
            identifier(item['operation_id']); identifier(item['booking_id'])
            require(type(item['session_generation']) is str and
                    re.fullmatch(r'[a-f0-9]{32}', item['session_generation']) is not None and
                    type(item['alias']) is str and item['alias'] in ALIASES,
                    'invalid_operation_receipt')
            require(item['phase'] in ('applied', 'failed', 'cancelled'),
                    'invalid_operation_receipt')
            error = item.get('error')
            require(error is None or (type(error) is str and error in SAFE_OPERATION_ERRORS),
                    'invalid_operation_receipt')
            application = item.get('application')
            require(application is None or application in ('live', 'next_map', 'recreate'),
                    'invalid_operation_receipt')
            safe.append({'operation_id': item['operation_id'], 'booking_id': item['booking_id'],
                         'session_generation': item['session_generation'], 'alias': item['alias'],
                         'phase': item['phase'], 'error': error, 'application': application})
        require(len(canonical(safe)) <= 32768, 'invalid_operation_receipts', 413)
        accepted = 0
        with self._transaction() as db:
            for item in safe:
                op = db.execute('''SELECT o.*,b.host_id,b.session_generation,b.alias,b.role_data,b.role_revision
                                   FROM operations o JOIN bookings b ON b.id=o.booking_id
                                   WHERE o.id=?''', (item['operation_id'],)).fetchone()
                if op is None:
                    continue
                require(op['host_id'] == host_id, 'host_generation_mismatch', 403)
                require((op['booking_id'], op['session_generation'], op['alias']) ==
                        (item['booking_id'], item['session_generation'], item['alias']),
                        'operation_receipt_binding_mismatch', 409)
                wire = {key: item[key] for key in
                        ('operation_id', 'phase', 'error', 'application')}
                pinned = canonical(wire).decode()
                if op['phase'] == 'cancelled':
                    if op['host_receipt'] is None:
                        db.execute('UPDATE operations SET host_receipt=? WHERE id=?',
                                   (pinned, op['id']))
                    else:
                        require(op['host_receipt'] == pinned,
                                'operation_receipt_conflict', 409)
                    continue
                if op['phase'] in ('applied', 'failed'):
                    require((op['phase'], op['error'], op['application']) ==
                            (item['phase'], item['error'], item['application']),
                            'operation_receipt_conflict', 409)
                    if op['host_receipt'] is not None:
                        require(op['host_receipt'] == pinned,
                                'operation_receipt_conflict', 409)
                    else:
                        db.execute('UPDATE operations SET host_receipt=? WHERE id=?',
                                   (pinned, op['id']))
                    continue
                require(op['phase'] in ('pending', 'dispatched'),
                        'invalid_operation_transition', 409)
                db.execute('UPDATE operations SET phase=?,error=?,application=?,host_receipt=? WHERE id=?',
                           (item['phase'], item['error'], item['application'], pinned, op['id']))
                if op['action'] == 'grant':
                    roles = _json(op['role_data'])
                    target = str(_json(op['payload']).get('user_id'))
                    if roles.get(target) == 'grant_pending':
                        if item['phase'] == 'applied':
                            roles[target] = 'controller'
                        else:
                            roles.pop(target, None)
                        db.execute('UPDATE bookings SET role_data=?,role_revision=role_revision+1,revision=revision+1 WHERE id=?',
                                   (canonical(roles).decode(), op['booking_id']))
                    else:
                        db.execute('UPDATE bookings SET revision=revision+1 WHERE id=?',
                                   (op['booking_id'],))
                else:
                    db.execute('UPDATE bookings SET revision=revision+1 WHERE id=?',
                               (op['booking_id'],))
                accepted += 1
        return {'accepted': accepted}
