"""Read-only, fail-closed booking eligibility from fresh host reports."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

from ..config import PRESETS
from ..diagnostic_logs.common import check_database
from ..managed_host.contracts import capabilities as validate_native_capabilities
from ..network.contracts import NetworkError, canonical, identifier, require

FRESH_SECONDS = 120
ALIASES = {'room1', 'room2', 'room3', 'room4'}


def _json(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeError):
        raise NetworkError('booking_eligibility_unavailable', 503) from None
    return value


def _timestamp(value):
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 2**53 - 1,
            'booking_eligibility_unavailable', 503)
    return float(value)


def _readonly(path):
    path = Path(path)
    require(path.is_absolute() and path.exists(), 'booking_eligibility_unavailable', 503)
    try:
        check_database(path)
        uri = 'file:' + quote(str(path), safe='/') + '?mode=ro'
        db = sqlite3.connect(uri, uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA busy_timeout=1000')
        return db
    except (OSError, sqlite3.Error, NetworkError):
        raise NetworkError('booking_eligibility_unavailable', 503) from None


class Eligibility:
    """Read fresh, generation-bound booking and managed-room reports.

    This reader never creates or mutates a database. The caller owns report
    ingestion and explicit local alias policy.
    """

    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock
        self.network_path = Path(store.path)
        self.root = Path(store.root)
        self.managed_path = self.root / 'managed-rooms.sqlite3'
        self.bookings_path = self.root / 'bookings-hosts.sqlite3'

    def _fresh(self, stamp, now):
        return 0 <= now - _timestamp(stamp) <= FRESH_SECONDS

    def __call__(self, host_id, alias):
        identifier(host_id)
        require(alias in ALIASES, 'invalid_alias')
        now = _timestamp(self.clock())
        auth = _readonly(self.network_path)
        try:
            host = auth.execute('SELECT id,installation_id,generation,revoked FROM hosts WHERE id=?', (host_id,)).fetchone()
        except sqlite3.Error:
            auth.close()
            raise NetworkError('booking_eligibility_unavailable', 503) from None
        auth.close()
        require(host is not None and not host['revoked'], 'host_ineligible', 409)
        installation_id, generation = host['installation_id'], host['generation']
        identifier(installation_id); identifier(generation)

        managed = _readonly(self.managed_path)
        try:
            report = managed.execute('SELECT * FROM reports WHERE host=?', (host_id,)).fetchone()
        except sqlite3.Error:
            managed.close()
            raise NetworkError('booking_eligibility_unavailable', 503) from None
        managed.close()
        require(report is not None and self._fresh(report['received'], now), 'host_ineligible', 409)
        native = _json(report['capabilities'])
        try:
            validate_native_capabilities(native)
        except NetworkError:
            raise NetworkError('host_ineligible', 409) from None
        require(native['host_id'] == host_id and native['installation_id'] == installation_id
                and native['generation'] == generation, 'host_ineligible', 409)
        controller_phase = native['controller_phase']
        stopped_controller_safe = (
            controller_phase == 'stopped' and native['capacity_used'] == 0 and
            all(room['desired'] == 'stopped' and room['phase'] in ('stopped', 'unobserved')
                and room['room_id'] is None and room['pending_operation'] is None
                and room['failure_code'] is None for room in native['rooms']))
        controller_eligible = controller_phase == 'running' or stopped_controller_safe

        booking_db = _readonly(self.bookings_path)
        try:
            report = booking_db.execute('SELECT * FROM reports WHERE host=?', (host_id,)).fetchone()
        except sqlite3.Error:
            booking_db.close()
            raise NetworkError('booking_eligibility_unavailable', 503) from None
        booking_db.close()
        require(report is not None and self._fresh(report['received'], now), 'host_ineligible', 409)
        payload = _json(report['payload'])
        require(type(payload) is dict and len(canonical(payload)) <= 262144 and set(payload) == {
            'protocol_version', 'host_id', 'installation_id', 'generation', 'sequence', 'sent_at',
            'capabilities', 'observations', 'receipts'}, 'host_ineligible', 409)
        require(payload['protocol_version'] == 1 and payload['host_id'] == host_id
                and payload['installation_id'] == installation_id and payload['generation'] == generation
                and type(payload['sequence']) is int and payload['sequence'] == report['sequence']
                and self._fresh(payload['sent_at'], now), 'host_ineligible', 409)
        pin = hashlib.sha256(canonical(payload)).hexdigest()
        require(type(report['pin']) is str and report['pin'] == pin, 'host_ineligible', 409)
        caps = payload['capabilities']
        require(type(caps) is dict and set(caps) == {'version', 'enabled_aliases', 'supported_presets', 'active_booking_ids'}
                and caps['version'] == 1, 'host_ineligible', 409)
        enabled_aliases = caps['enabled_aliases']
        supported_presets = caps['supported_presets']
        active_ids = caps['active_booking_ids']
        require(type(enabled_aliases) is list and len(enabled_aliases) <= 4
                and all(type(item) is str and item in ALIASES for item in enabled_aliases)
                and len(set(enabled_aliases)) == len(enabled_aliases), 'host_ineligible', 409)
        require(type(supported_presets) is list and 1 <= len(supported_presets) <= len(PRESETS)
                and all(type(item) is str and item in PRESETS for item in supported_presets)
                and len(set(supported_presets)) == len(supported_presets), 'host_ineligible', 409)
        require(type(active_ids) is list and len(active_ids) <= 64
                and all(type(item) is str for item in active_ids)
                and len(set(active_ids)) == len(active_ids), 'host_ineligible', 409)
        for item in active_ids:
            identifier(item)
        observations = payload['observations']
        require(type(observations) is list and len(observations) <= 64, 'host_ineligible', 409)

        native_room = next((room for room in native['rooms'] if room['alias'] == alias), None)
        supported = controller_eligible and alias in enabled_aliases and native_room is not None
        if supported:
            supported = native_room['preset'] in PRESETS and native_room['preset'] in supported_presets
            supported = supported and native_room['pending_operation'] is None and native_room['failure_code'] is None
            stopped = (native_room['desired'] == 'stopped' and native_room['phase'] in ('stopped', 'unobserved')
                       and native_room['room_id'] is None and native_room['visibility'] == 'private')
            running = (native_room['desired'] == 'running' and native_room['phase'] == 'running'
                       and native_room['visibility'] == 'private' and native_room['room_id'] is not None)
            if running:
                # A running private room is acceptable only as the same currently
                # owned lease reported by the booking host. It cannot be adopted.
                active = set(active_ids)
                running = any(type(item) is dict and set(item) == {'booking_id', 'observation'}
                              and item['booking_id'] in active and type(item['observation']) is dict
                              and item['observation'].get('room_id') == native_room['room_id']
                              and item['observation'].get('ownership_known') is True
                              and item['observation'].get('cleanup_complete') is False
                              and item['observation'].get('closed') is not True
                              and item['observation'].get('phase') in ('reserved', 'awaiting_claim', 'active', 'closing')
                              and item['observation'].get('alias', alias) == alias
                              for item in observations)
            supported = supported and (stopped or running)

        limit = min(native['max_concurrent_rooms'], native['provider_room_limit'])
        return {'supported': bool(supported), 'fresh': True, 'host_id': host_id,
                'installation_id': installation_id, 'generation': generation,
                'effective_limit': limit, 'capacity_used': native['capacity_used'],
                'observed_booking_ids': active_ids, 'supported_presets': supported_presets,
                'enabled_aliases': enabled_aliases}
