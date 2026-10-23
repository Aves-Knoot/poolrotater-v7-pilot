"""Independent enrolled-host booking exchange; never controls native rooms."""
import copy
import os
import re
import threading
import time
from pathlib import Path

from ..config import ConfigError, PRESETS
from ..layout import LeaseBusy, atomic_json, contained, file_lease, read_json
from ..network.contracts import NetworkError, canonical, identifier, require
from .contracts import FAILURES, PATH, map_info as contract_map_info, queue_info as contract_queue_info
from .lifecycle import activity_snapshot
from .local import LocalSessions

FORMAT = 1
DIAGNOSTICS = {'idle', 'disabled', 'not-configured', 'not-enrolled', 'retrying',
               'connected', 'requires-review', 'authority-refused', 'hub-upgrade-required'}
ERRORS = {None, 'local-state-refused', 'hub-unavailable', 'authority-refused',
          'unsupported-capability', 'policy-preparation-required'}
APPLICATIONS = {'live', 'next_map', 'recreate'}
RECEIPT_ERRORS = {
    'booking_forbidden', 'booking_role_changed', 'booking_closed', 'booking_closing',
    'configure_cooldown', 'configure_pending', 'invalid_action_payload',
    'invalid_booking_settings', 'operation_cancelled', 'worker_action_outcome_unknown',
    'native_operation_requires_review', 'booking_command_queue_full', 'host_operation_failed',
    'settings_revision_conflict',
}
MAX_ACTIVE = 4
MAX_ENDED_REPORTS = 64


def _fixed_error(value):
    return value if type(value) is str and value in RECEIPT_ERRORS else 'host_operation_failed'


def _application(value):
    if type(value) is str and value in APPLICATIONS:
        return value
    if type(value) is dict and value.get('application') in APPLICATIONS:
        return value['application']
    return None


class Agent:
    def __init__(self, client, store=None, clock=time.time, observation_provider=None):
        self.client = client
        self.root = Path(client.root)
        self.store = store
        self.clock = clock
        self.observation_provider = observation_provider
        self.failures = 0
        self.path = contained(self.root, 'state/booking-agent.json')
        self.diagnostic_path = contained(self.root, 'state/booking-agent-status.json')
        self.lock_path = contained(self.root, 'state/booking-agent.lock')

    def _local_status(self):
        status = self.client.status()
        if not status.get('configured'):
            return None, None, None
        if status.get('enrolled') is not True:
            return status, 'not-enrolled', None
        require(status.get('account_binding_matches') is True, 'network_account_mismatch', 409)
        require(status.get('pending_operation') is None, 'network_pending_requires_resume', 409)
        profile = self.client.profile()
        credential = self.client.credential()
        require(credential is not None and credential['credential_expires_at'] > self.clock(),
                'credential_expired', 401)
        require(self.client.pending(profile) is None, 'network_pending_requires_resume', 409)

        from .policy import enabled_aliases
        aliases = enabled_aliases(self.root)
        if not aliases:
            return status, None, None
        managed_state = getattr(self.client, 'managed_state', None)
        if managed_state is None:
            from ..managed_host.policy import state as open_managed_state
            managed_state = open_managed_state(self.root)
        binding = managed_state.binding
        require(binding.get('host_id') == credential['host_id'] and
                binding.get('installation_id') == credential['installation_id'] and
                binding.get('generation') == credential['generation'],
                'host_identity_mismatch', 409)
        status_store = self.store
        if status_store is None:
            status_store = LocalSessions(self.root, binding, clock=self.clock)
        require(getattr(status_store, 'binding', None) == binding, 'host_identity_mismatch', 409)
        return status, (managed_state, binding, aliases), status_store

    def _supported_presets(self):
        injected = getattr(self.client, 'supported_presets', None)
        if injected is not None:
            require(type(injected) in (list, tuple, set, frozenset) and
                    all(type(value) is str and value in PRESETS for value in injected),
                    'booking_catalog_unavailable', 503)
            return sorted(set(injected))
        catalog_store = getattr(self.client, 'catalog_store', None)
        if catalog_store is None:
            from ..catalog_store import CatalogStore
            catalog_store = CatalogStore.open(self.root)
        inspection = catalog_store.inspect()
        manifests = inspection.get('manifests') if type(inspection) is dict else None
        require(type(manifests) is dict, 'booking_catalog_unavailable', 503)
        modes = set(manifests)
        mode_for = {
            'standard-auto': 'standard', 'standard-fixed': 'standard',
            'standard-loved': 'loved', 'catch-fixed': 'catch',
            'mania-4k': 'mania4', 'mania-7k': 'mania7',
        }
        return sorted(preset for preset in PRESETS if mode_for.get(preset) in modes)

    def _room_and_base(self, managed_state, alias, booking_id=None):
        room = managed_state.room(alias, private=True)
        config = room.get('config')
        require(type(config) is dict and config.get('visibility') == 'private',
                'booking_room_not_private', 409)
        require(room.get('pending') is None and room.get('failure') is None and
                room.get('pending_operation') is None and room.get('failure_code') is None,
                'booking_room_uncertain', 409)
        phase = room.get('phase')
        existing_exact = False
        existing_base = None
        if booking_id is not None:
            try:
                existing = self.store.get(booking_id)
                saved = existing['lease']
                incoming = self._current_lease
                existing_exact = all(saved.get(key) == incoming.get(key) for key in
                                     ('id', 'host_id', 'installation_id', 'generation',
                                      'alias', 'session_generation'))
                if existing_exact:
                    existing_base = copy.deepcopy(existing['base_settings'])
                    if existing['cleanup_complete']:
                        return room, existing_base
            except NetworkError:
                existing_exact = False
        if existing_exact:
            require(phase in ('running', 'stopped', 'unobserved'), 'booking_room_uncertain', 409)
            if phase == 'running':
                require(room.get('desired') == 'running' and room.get('room_id') is not None,
                        'booking_room_in_use', 409)
            else:
                require(room.get('desired') == 'stopped' and room.get('room_id') is None,
                        'booking_room_in_use', 409)
        else:
            require(room.get('desired') == 'stopped' and phase in ('stopped', 'unobserved')
                    and room.get('room_id') is None, 'booking_room_in_use', 409)
        from .settings import FIELDS
        if existing_exact:
            return room, existing_base
        preset = self._current_lease['preset']
        if config.get('preset') != preset:
            from .settings import inventory
            defaults = inventory(preset)['defaults']
            return room, {key: value for key, value in defaults.items() if key in FIELDS}
        return room, {key: value for key, value in config.items() if key in FIELDS}

    @property
    def _setting_fields(self):
        from .settings import FIELDS
        return FIELDS

    def _install_lease(self, lease, managed_state, aliases, supported_presets, store):
        require(type(lease) is dict, 'invalid_booking_lease')
        try:
            prior = store.get(lease.get('id'))
        except NetworkError:
            prior = None
        if prior is not None:
            saved = prior['lease']
            exact_keys = ('id', 'user_id', 'designated_friend_id', 'host_id',
                          'installation_id', 'generation', 'alias',
                          'session_generation', 'preset')
            exact = all(saved.get(key) == lease.get(key) for key in exact_keys)
            if exact and not prior['cleanup_complete']:
                exact = saved.get('password') == lease.get('password')
            require(exact, 'booking_lease_conflict', 409)
            # A central replay of a locally installed lease never reconfigures
            # the native room or rewrites the durable local settings base.
            return
        require(lease.get('alias') in aliases, 'booking_policy_refused', 409)
        require(lease.get('preset') in supported_presets, 'booking_catalog_unavailable', 409)
        require(lease.get('host_id') == store.binding['host_id'] and
                lease.get('installation_id') == store.binding['installation_id'] and
                lease.get('generation') == store.binding['generation'],
                'host_generation_mismatch', 409)
        self.store = store
        self._current_lease = lease
        with file_lease(self.root / 'state/booking-admission.lock'):
            maintenance = self.root / 'state/rolling/supervisor-maintenance.json'
            require(not os.path.lexists(maintenance), 'booking_host_updating', 409)
            room, base_settings = self._room_and_base(managed_state, lease['alias'])
            if base_settings:
                store.install(lease, base_settings)

    def _observations(self):
        if self.observation_provider is not None:
            value = self.observation_provider()
        else:
            path = contained(self.root, 'state/booking-observations.json')
            if not path.exists():
                return {}
            value = read_json(path, maximum=256 * 1024)
        if type(value) is dict:
            require(set(value) == {'format', 'observations'} and value['format'] == FORMAT,
                    'booking_observation_state_invalid')
            value = value['observations']
        require(type(value) is list and len(value) <= 8, 'booking_observation_state_invalid')
        result = {}
        for item in value:
            require(type(item) is dict and set(item) == {'booking_id', 'observation'},
                    'booking_observation_state_invalid')
            identifier(item['booking_id'])
            require(type(item['observation']) is dict and item['booking_id'] not in result,
                    'booking_observation_state_invalid')
            result[item['booking_id']] = copy.deepcopy(item['observation'])
        return result

    def _operation_receipts(self, local_row):
        result = []
        for item in local_row.get('operation_receipts', [])[-32:]:
            if item.get('origin', 'hub') != 'hub' or item.get('phase') not in ('applied', 'failed', 'cancelled'):
                continue
            receipt = {'operation_id': item['operation_id'], 'phase': item['phase']}
            if item.get('error') is not None:
                receipt['error'] = _fixed_error(item['error'])
            application = _application(item.get('application'))
            if application is not None:
                receipt['application'] = application
            result.append(receipt)
        return result

    def _reported_observation(self, local_row, supplied, sequence, now):
        lease = local_row['lease']
        state = local_row['state']
        cleanup_complete = local_row['cleanup_complete']
        supplied = supplied or {}
        ended = state['phase'] == 'ended'
        roster = supplied.get('roster', supplied.get('players', []))
        require(type(roster) is list, 'booking_observation_state_invalid')
        observation = {
            'sequence': sequence,
            'phase': 'reserved' if state['phase'] == 'awaiting_claim' else state['phase'],
            'room_id': supplied.get('room_id'),
            'incarnation': supplied.get('incarnation'),
            'roster': copy.deepcopy(roster),
            'roster_known': supplied.get('roster_known', ended),
            'updated_at': supplied.get('updated_at', now),
            'ready': supplied.get('ready', not ended and state['ready_at'] is not None),
            'closed': supplied.get('closed', ended),
            'effective_settings': copy.deepcopy(supplied.get(
                'effective_settings', {} if cleanup_complete else local_row['settings'])),
            'cleanup_complete': cleanup_complete,
            'settings_revision': local_row.get('settings_revision', 0),
            'ownership_known': supplied.get('ownership_known', ended),
            'operation_receipts': self._operation_receipts(local_row),
            'installation_id': lease['installation_id'],
            'generation': lease['generation'],
            'session_generation': lease['session_generation'],
            'alias': lease['alias'],
        }
        if not ended and supplied.get('phase') == 'unavailable':
            observation['phase'] = 'unavailable'
        if not ended and 'current_map' in supplied:
            observation['current_map'] = contract_map_info(supplied['current_map'])
        if not ended and 'queue' in supplied:
            observation['queue'] = contract_queue_info(supplied['queue'])
        if not ended and 'queue_known' in supplied:
            require(type(supplied['queue_known']) is bool, 'booking_invalid_queue')
            observation['queue_known'] = supplied['queue_known']
        if not ended and supplied:
            lifecycle_source = dict(supplied)
            lifecycle_source.setdefault('updated_at', now)
            observation['activity'] = activity_snapshot(
                state, self._lifecycle_observation(lifecycle_source), now)
        if not ended and 'failure_code' in supplied:
            failure_code = supplied['failure_code']
            require(failure_code is None or failure_code in FAILURES,
                    'booking_invalid_failure')
            if failure_code is not None:
                observation['failure_code'] = failure_code
        if state.get('claim_deadline') is not None:
            observation['claim_deadline'] = state['claim_deadline']
        # Do not let the provider file claim local cleanup succeeded.
        if ended:
            observation.update(room_id=None, incarnation=None, ready=False, closed=True,
                               roster=[], roster_known=True, ownership_known=True,
                               current_map=None, queue=[], queue_known=False,
                               updated_at=now)
        reason = state.get('terminal_reason')
        if reason is not None:
            observation['terminal_reason'] = reason
        return observation

    @staticmethod
    def _lifecycle_observation(value):
        require(type(value) is dict, 'booking_observation_state_invalid')
        result = {
            'ready': value.get('ready', False),
            'room_id': value.get('room_id'),
            'incarnation': value.get('incarnation'),
            'roster_known': value.get('roster_known', False),
            'players': copy.deepcopy(value.get('players', value.get('roster', []))),
            'match': copy.deepcopy(value.get('match')),
            'closed': value.get('closed', False),
            'ownership_known': value.get('ownership_known', False),
            'updated_at': value.get('updated_at'),
        }
        require(result['updated_at'] is not None, 'booking_observation_state_invalid')
        return result

    def _report_rows(self, store, observations, sequence, now):
        rows = store.leases(include_ended=True)
        active = [row for row in rows if row['state']['phase'] != 'ended' or not row['cleanup_complete']]
        ended = [row for row in rows if row['state']['phase'] == 'ended' and row['cleanup_complete']]
        ended = ended[-MAX_ENDED_REPORTS:]
        require(len(active) <= MAX_ACTIVE, 'booking_capacity_refused', 409)
        if ended:
            batch_size = min(4, len(ended))
            offset = (sequence * batch_size) % len(ended)
            ended_batch = [ended[(offset + index) % len(ended)] for index in range(batch_size)]
        else:
            ended_batch = []
        selected = active + ended_batch
        reported = []
        for row in selected:
            if row['cleanup_complete']:
                supplied = observations.get(row['lease']['id'])
            else:
                supplied = observations.get(row['lease']['id'])
                if supplied is not None:
                    # Controller-published native observations feed the pure reducer;
                    # this exchange agent never applies a native action.
                    store.advance(row['lease']['id'], self._lifecycle_observation(supplied))
                    row = store.get(row['lease']['id'])
            if supplied is None and row['state']['phase'] != 'ended':
                continue
            normalized = self._reported_observation(row, supplied, sequence, now)
            reported.append({'booking_id': row['lease']['id'], 'observation': normalized})
        return reported

    def _load_exchange(self, binding):
        if not self.path.exists():
            return {'sequence': 0, 'pending': None}
        value = read_json(self.path, maximum=512 * 1024)
        require(type(value) is dict and set(value) == {'sequence', 'pending'} and
                type(value['sequence']) is int and value['sequence'] >= 0,
                'booking_agent_state_invalid')
        pending = value['pending']
        if pending is not None:
            required = {'protocol_version', 'host_id', 'installation_id', 'generation',
                        'sequence', 'sent_at', 'capabilities', 'observations', 'receipts'}
            require(type(pending) is dict and set(pending) == required and
                    type(pending['protocol_version']) is int and pending['protocol_version'] == 1 and
                    all(pending.get(key) == binding[key] for key in binding) and
                    type(pending['sequence']) is int and 0 <= pending['sequence'] < value['sequence'] and
                    type(pending['sent_at']) in (int, float) and pending['sent_at'] >= 0 and
                    type(pending['capabilities']) is dict and set(pending['capabilities']) == {
                        'version', 'enabled_aliases', 'supported_presets', 'active_booking_ids'} and
                    type(pending['observations']) is list and len(pending['observations']) <= 8 and
                    type(pending['receipts']) is list and len(pending['receipts']) <= 32 and
                    len(canonical(pending)) <= 65536,
                    'booking_agent_state_invalid')
        return value

    def _capabilities(self, aliases, supported, store):
        rows = store.leases(include_ended=True)
        active = sorted({row['lease']['id'] for row in rows if not row['cleanup_complete']})
        require(len(active) <= MAX_ACTIVE, 'booking_capacity_refused', 409)
        return {'version': 1, 'enabled_aliases': sorted(aliases),
                'supported_presets': supported, 'active_booking_ids': active}

    @staticmethod
    def _exchange_receipts(store):
        result = []
        for item in store.receipts(limit=32):
            if item.get('origin') != 'hub' or item.get('phase') not in ('applied', 'failed', 'cancelled'):
                continue
            booking_id = item.get('booking_id')
            try:
                lease = store.get(booking_id)['lease']
            except NetworkError:
                # A malformed refusal may name no locally installed booking;
                # it cannot be safely associated with a central operation.
                continue
            receipt = {
                'operation_id': item['operation_id'], 'booking_id': booking_id,
                'session_generation': lease['session_generation'], 'alias': lease['alias'],
                'phase': item['phase'],
            }
            if item.get('error') is not None:
                receipt['error'] = _fixed_error(item['error'])
            application = _application(item.get('application'))
            if application is not None:
                receipt['application'] = application
            result.append(receipt)
        return result

    def _exchange(self, pending):
        profile = self.client.profile()
        credential = self.client.credential()
        require(credential is not None, 'credential_expired', 401)
        response = self.client.transport(profile).request('POST', PATH, body=pending,
                                                          token=credential['credential'])
        require(type(response) is dict and set(response) == {'ok', 'sequence', 'leases', 'commands'} and
                response['ok'] is True and type(response['sequence']) is int and
                response['sequence'] == pending['sequence'] and
                type(response['leases']) is list and len(response['leases']) <= 4 and
                type(response['commands']) is list and len(response['commands']) <= 32,
                'booking_exchange_reply_mismatch', 503)
        return response

    def _clear_expired_exchange(self):
        """Forget only an exchange explicitly rejected as stale and unaccepted."""
        if not self.path.exists():
            return
        value = read_json(self.path, maximum=512 * 1024)
        pending = value.get('pending') if type(value) is dict else None
        if (type(value) is dict and set(value) == {'sequence', 'pending'} and
                type(value['sequence']) is int and type(pending) is dict and
                type(pending.get('sequence')) is int and
                value['sequence'] == pending['sequence'] + 1):
            atomic_json(self.path, {'sequence': value['sequence'], 'pending': None})

    def _apply_response(self, response, managed_state, aliases, supported, store):
        for lease in response['leases']:
            self._install_lease(lease, managed_state, aliases, supported, store)
        for command in response['commands']:
            require(type(command) is dict, 'invalid_booking_command')
            normalized = dict(command)
            normalized.setdefault('host_id', store.binding['host_id'])
            store.receive(normalized)

    def step(self):
        now = self.clock()
        phase = 'idle'; error = None; delay = 60
        try:
            status, context, store = self._local_status()
            if status is None:
                phase = 'not-configured'; delay = 300
            elif context == 'not-enrolled':
                phase = 'not-enrolled'; delay = 300
            elif context is None:
                phase = 'disabled'; delay = 300
            else:
                managed_state, binding, aliases = context
                supported = self._supported_presets()
                require(bool(supported), 'booking_catalog_unavailable', 503)
                exchange_state = self._load_exchange(binding)
                if exchange_state['pending'] is None:
                    sequence = exchange_state['sequence']
                    observations = self._observations()
                    reported = self._report_rows(store, observations, sequence, now)
                    pending = {'protocol_version': 1, **binding, 'sequence': sequence,
                               'sent_at': now,
                               'capabilities': self._capabilities(aliases, supported, store),
                               'observations': reported,
                               'receipts': self._exchange_receipts(store)}
                    require(len(canonical(pending)) <= 65536,
                            'booking_exchange_too_large', 413)
                    exchange_state = {'sequence': sequence + 1, 'pending': pending}
                    atomic_json(self.path, exchange_state)
                else:
                    pending = exchange_state['pending']
                response = self._exchange(pending)
                self._apply_response(response, managed_state, aliases, supported, store)
                atomic_json(self.path, {'sequence': exchange_state['sequence'], 'pending': None})
                phase = 'connected'; self.failures = 0
                active_rows = store.leases()
                has_pending = any(item['phase'] in ('pending', 'dispatched')
                                  for row in active_rows
                                  for item in store.get(row['lease']['id'])['operation_receipts'])
                delay = 5 if active_rows or has_pending else 60
        except LeaseBusy:
            phase = 'retrying'; delay = 5
        except NetworkError as exc:
            self.failures = min(self.failures + 1, 8)
            if exc.code == 'booking_exchange_expired' and exc.status == 409:
                self._clear_expired_exchange()
                phase = 'retrying'; error = None; delay = 5
            elif exc.code == 'booking_host_updating' and exc.status == 409:
                phase = 'retrying'; error = 'policy-preparation-required'; delay = 5
            elif exc.code in ('host_revoked', 'credential_expired', 'credential_retired',
                            'credential_refused', 'scope_refused'):
                phase = 'authority-refused'; error = 'authority-refused'; delay = 300
            elif exc.code == 'route_not_found' and exc.status == 404:
                phase = 'hub-upgrade-required'; error = 'unsupported-capability'; delay = 300
            elif exc.status >= 500 or exc.code in ('hub_deadline_exceeded', 'hub_transport_failed',
                                                    'hub_rate_limited'):
                phase = 'retrying'; error = 'hub-unavailable'
                delay = min(300, 5 * 2 ** (self.failures - 1))
            else:
                phase = 'requires-review'; error = 'local-state-refused'; delay = 300
        except Exception:
            phase = 'requires-review'; error = 'local-state-refused'; delay = 300
        # Never include remote error text, ids, settings, or credentials in diagnostics.
        receipt = {'format': FORMAT, 'phase': phase, 'error': error,
                   'attempted_at': now, 'retry_seconds': delay,
                   'live_rooms_affected': False}
        try:
            atomic_json(self.diagnostic_path, receipt)
        except (OSError, ConfigError):
            pass
        return receipt

    def run(self, stop):
        try:
            os.setpriority(os.PRIO_PROCESS, 0, max(19, os.getpriority(os.PRIO_PROCESS, 0)))
        except (AttributeError, OSError):
            pass
        with file_lease(self.lock_path):
            while not stop.is_set():
                stop.wait(self.step()['retry_seconds'])


def start(client, stop):
    from .policy import enabled_aliases
    try:
        if not enabled_aliases(client.root):
            return None
    except Exception:
        # Without a verified explicit alias, the exchange agent stays inert.
        return None
    thread = threading.Thread(target=Agent(client).run, args=(stop,),
                              name='booking-exchange-agent', daemon=True)
    thread.start()
    return thread


def diagnostics(root):
    path = Path(root) / 'state/booking-agent-status.json'
    if not path.exists():
        return {'phase': 'not-observed', 'error': None}
    value = read_json(path)
    expected = {'format', 'phase', 'error', 'attempted_at', 'retry_seconds', 'live_rooms_affected'}
    require(type(value) is dict and set(value) == expected and type(value['format']) is int and
            value['format'] == FORMAT and type(value['phase']) is str and
            value['phase'] in DIAGNOSTICS and
            (value['error'] is None or type(value['error']) is str) and value['error'] in ERRORS and
            value['live_rooms_affected'] is False,
            'booking_agent_diagnostics_invalid')
    return {key: value[key] for key in ('phase', 'error', 'attempted_at', 'retry_seconds')}
