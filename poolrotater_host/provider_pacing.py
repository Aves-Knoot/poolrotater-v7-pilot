"""Private persistent pacing for one offline installation/client/account.

The gate grants request admission only, never OAuth, account or room authority.
All installed processes share a private file lease. Other roots/machines do not.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

from .config import contained
from .layout import atomic_json, checked_root, file_lease, load, owner_lease, read_json
from .oauth import AuthError, _positive, diagnostic_lease
from .provider_contract import PROVIDER_ORIGIN

COUNTERS = {'attempted', 'dispatched', 'replies', 'rate_limited', 'uncertain', 'deferred'}
STATE_PATH = 'state/provider-pacing.json'
ANCHOR_PATH = 'state/provider-pacing-anchor.json'
LOCK_PATH = 'state/provider-pacing.lock'
PROVIDER_STATE_PATH = 'state/provider-authority-pacing.json'
PROVIDER_ANCHOR_PATH = 'state/provider-authority-pacing-anchor.json'
PROVIDER_LOCK_PATH = 'state/provider-authority-pacing.lock'
MAX_COUNTER = 2**63 - 1


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def clock_sample():
    return {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'monotonic': time.monotonic(), 'wall': time.time()}


def _clock(value):
    if not isinstance(value, dict) or set(value) != {'boot_id', 'monotonic', 'wall'}:
        raise AuthError('provider_pacing_clock_refused')
    try:
        if str(uuid.UUID(value['boot_id'])) != value['boot_id']: raise ValueError
        for key in ('monotonic', 'wall'):
            number = value[key]
            if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1e12: raise ValueError
    except Exception: raise AuthError('provider_pacing_clock_refused') from None
    return dict(value)


def clock_matches(before, after):
    return (before['boot_id'] == after['boot_id'] and after['monotonic'] >= before['monotonic']
            and abs((after['wall'] - before['wall']) - (after['monotonic'] - before['monotonic'])) <= 5)


class PersistentRequestGate:
    execution = 'offline-fixture'
    state_path = STATE_PATH
    anchor_path = ANCHOR_PATH
    lock_path = LOCK_PATH
    minimum_spacing = .05

    def _extra_binding(self):
        return {}

    def __init__(self, root, client_id, *, minimum_interval=.05, clock=clock_sample):
        self._initialize_binding(root, client_id, minimum_interval, clock)
        try:
            with self._lease(5):
                anchor = self._anchor(optional=True)
                state = self._read(optional=True)
                if anchor is None and state is None:
                    anchor = {**self.binding, 'ledger_id': str(uuid.uuid4())}
                    atomic_json(contained(self.root, self.anchor_path), anchor)
                    now = self._now()
                    state = {'ledger_id': anchor['ledger_id'], 'user_id': None, 'phase': 'idle',
                             'clock': now, 'interval': self.interval, 'next': now['monotonic'],
                             'blocked': now['monotonic'], 'halted': False, 'counters': {k: 0 for k in COUNTERS}}
                    atomic_json(contained(self.root, self.state_path), state)
                elif anchor is None or state is None:
                    raise AuthError('provider_pacing_state_missing')
                self._read()
        except AuthError: raise
        except Exception: raise AuthError('provider_pacing_initialization_uncertain') from None

    def _initialize_binding(self, root, client_id, minimum_interval, clock):
        """Only validate/bind memory; ledger creation remains in __init__."""
        self._offline(); _positive(client_id)
        if type(minimum_interval) not in (float, int) or not self.minimum_spacing <= minimum_interval <= 60:
            raise AuthError('invalid_provider_pacing')
        self.root = checked_root(Path(root)); marker, _ = load(self.root)
        self.binding = {'format': 1, 'execution': self.execution, 'root': str(self.root),
                        'uid': os.geteuid(), 'installation_id': marker['installation_id'],
                        'generation': marker['generation'], 'client_id': client_id, 'provider': PROVIDER_ORIGIN,
                        **self._extra_binding()}
        self.interval, self.clock = float(minimum_interval), clock
        self._pid = os.getpid(); self._held = None
        self._needs_outcome = False; self._outcome_saved = False

    @staticmethod
    def _offline():
        if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1': raise AuthError('offline_fixture_required')

    def _current(self):
        self._offline()
        if os.getpid() != self._pid: raise AuthError('provider_gate_process_changed')
        marker, config = load(self.root)
        if any(marker[k] != self.binding[k] for k in ('installation_id', 'generation')):
            raise AuthError('provider_pacing_binding_mismatch')
        return config

    def _lease(self, timeout):
        self._current()
        return file_lease(contained(self.root, self.lock_path), timeout=max(0, min(5, timeout)))

    def _now(self):
        try: return _clock(self.clock())
        except Exception: raise AuthError('provider_pacing_clock_refused') from None

    def _anchor(self, *, optional=False):
        try: value = read_json(contained(self.root, self.anchor_path), maximum=4096)
        except FileNotFoundError:
            if optional: return None
            raise AuthError('provider_pacing_state_missing') from None
        except Exception: raise AuthError('provider_pacing_state_refused') from None
        if (not isinstance(value, dict) or set(value) != {*self.binding, 'ledger_id'}
                or any(type(value[k]) is not type(v) or value[k] != v for k, v in self.binding.items())):
            raise AuthError('provider_pacing_binding_mismatch')
        try:
            if str(uuid.UUID(value['ledger_id'])) != value['ledger_id']: raise ValueError
        except Exception: raise AuthError('provider_pacing_state_refused') from None
        return value

    def _read(self, *, optional=False):
        config = self._current()
        try: value = read_json(contained(self.root, self.state_path), maximum=16384)
        except FileNotFoundError:
            if optional: return None
            raise AuthError('provider_pacing_state_missing') from None
        except Exception: raise AuthError('provider_pacing_state_refused') from None
        anchor = self._anchor()
        try:
            if (not isinstance(value, dict) or set(value) != {'ledger_id','user_id','phase','clock','interval','next','blocked','halted','counters'}
                    or value['ledger_id'] != anchor['ledger_id'] or value['phase'] not in ('idle', 'reserved')
                    or type(value['halted']) is not bool): raise ValueError
            _clock(value['clock'])
            if value['user_id'] is not None: _positive(value['user_id'])
            for key in ('next', 'blocked', 'interval'):
                if type(value[key]) not in (float, int) or not math.isfinite(value[key]) or not 0 <= value[key] <= 1e12:
                    raise ValueError
            if not self.minimum_spacing <= value['interval'] <= 60: raise ValueError
            if (not isinstance(value['counters'], dict) or set(value['counters']) != COUNTERS
                    or any(type(n) is not int or not 0 <= n <= MAX_COUNTER for n in value['counters'].values())): raise ValueError
        except Exception: raise AuthError('provider_pacing_state_refused') from None
        expected = config['account']['expected_user_id']
        if expected is not None and value['user_id'] is not None and expected != value['user_id']:
            raise AuthError('provider_pacing_account_mismatch')
        return value

    def _save(self, value):
        if self._held is None or self._read() != self._held: raise AuthError('provider_pacing_state_changed')
        if len(json.dumps(value, allow_nan=False).encode()) > 16384: raise AuthError('provider_pacing_state_refused')
        try: atomic_json(contained(self.root, self.state_path), value)
        except Exception: raise AuthError('provider_pacing_write_uncertain') from None
        previous = self._held
        self._held = deepcopy(value)
        self._usage_saved(previous, value)

    def _usage_saved(self, before, after):
        pass  # Legacy fixture pacing has no operational provider telemetry.

    def _usage_admission(self):
        pass

    def _usage_wait(self, seconds, *, elapsed=None):
        pass

    def _wait_seconds(self, value, now):
        return max(value['next'], value['blocked']) - now['monotonic']

    def _next_time(self, value, now):
        return now['monotonic'] + value['interval']

    @staticmethod
    def _count(value, key): value['counters'][key] = min(MAX_COUNTER, value['counters'][key] + 1)

    def require_authority(self, root, client_id, *, user_id=None):
        if checked_root(Path(root)) != self.root or client_id != self.binding['client_id']:
            raise AuthError('provider_pacing_binding_mismatch')
        if user_id is not None: _positive(user_id)
        with self._lease(5):
            value = self._read()
            if user_id is not None and value['user_id'] not in (None, user_id):
                raise AuthError('provider_pacing_account_mismatch')

    def bind_verified(self, user_id):
        _positive(user_id)
        with self._lease(5):
            value = self._read(); self._healthy(value)
            if value['user_id'] not in (None, user_id): raise AuthError('provider_pacing_account_mismatch')
            expected = self._current()['account']['expected_user_id']
            if expected is not None and expected != user_id: raise AuthError('provider_pacing_account_mismatch')
            if value['user_id'] == user_id: return
            self._held = deepcopy(value)
            try:
                value['user_id'] = user_id; self._save(value)
            finally: self._held = None

    def _healthy(self, value):
        if value['phase'] != 'idle': raise AuthError('provider_pacing_recovery_required')
        if not clock_matches(value['clock'], self._now()): raise AuthError('provider_pacing_clock_changed')

    @contextmanager
    def admission(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise AuthError('provider_deadline_exceeded')
        try:
            lease = self._lease(remaining)
            lease.__enter__()
        except AuthError: raise
        except Exception: raise AuthError('provider_pacing_busy') from None
        reserved = False
        try:
            value = self._read(); self._healthy(value); now = self._now()
            self._held = deepcopy(value); self._count(value, 'attempted')
            self._usage_admission()
            wait = self._wait_seconds(value, now)
            if value['halted'] or wait >= deadline - time.monotonic():
                self._count(value, 'deferred'); self._save(value)
                raise AuthError('provider_pacing_deferred')
            if wait > 0:
                self._usage_wait(wait)
                wait_started = time.monotonic()
                try: time.sleep(wait)
                finally: self._usage_wait(wait, elapsed=max(0., time.monotonic() - wait_started))
            if time.monotonic() >= deadline: raise AuthError('provider_deadline_exceeded')
            now = self._now()
            if not clock_matches(value['clock'], now): raise AuthError('provider_pacing_clock_changed')
            value['clock'] = now; value['interval'] = max(value['interval'], self.interval)
            value['next'] = self._next_time(value, now); value['phase'] = 'reserved'
            self._count(value, 'dispatched'); self._save(value); reserved = True
            self._needs_outcome = False; self._outcome_saved = False
            yield
        finally:
            try:
                if reserved and (not self._needs_outcome or self._outcome_saved):
                    value = deepcopy(self._held); value['phase'] = 'idle'; self._save(value)
            finally:
                self._held = None; lease.__exit__(None, None, None)

    def replied(self, code, delay=0):
        self._needs_outcome = True
        if self._held is None or self._held['phase'] != 'reserved': raise AuthError('provider_pacing_not_admitted')
        if type(code) is not int or not 100 <= code <= 599 or (delay is not None and
                (type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 86400)):
            raise AuthError('provider_pacing_outcome_refused')
        value = deepcopy(self._held); now = self._now()
        if not clock_matches(value['clock'], now): raise AuthError('provider_pacing_clock_changed')
        value['clock'] = now; self._count(value, 'replies')
        if code == 429:
            self._count(value, 'rate_limited')
            if delay is None: value['halted'] = True
            else: value['blocked'] = max(value['blocked'], now['monotonic'] + delay)
        self._usage_last_status = code
        self._save(value); self._outcome_saved = True

    def exchange_started(self, *, kind='http'):
        if self._held is None or self._held['phase'] != 'reserved': raise AuthError('provider_pacing_not_admitted')
        self._needs_outcome = True

    def uncertain(self):
        self._needs_outcome = True
        value = deepcopy(self._held)
        if value is None or value['phase'] != 'reserved': raise AuthError('provider_pacing_not_admitted')
        self._count(value, 'uncertain')
        now = self._now()
        if not clock_matches(value['clock'], now): raise AuthError('provider_pacing_clock_changed')
        value['clock'] = now
        value['blocked'] = max(value['blocked'], now['monotonic'] + 60)
        self._save(value); self._outcome_saved = True

    def snapshot(self):
        with self._lease(5):
            value = self._read()
            return self._snapshot_fields(value)

    def _snapshot_fields(self, value):
        return {'counters': dict(value['counters']), 'phase': value['phase'], 'halted': value['halted'],
                'account_bound': value['user_id'] is not None, 'state_sha256': digest(value),
                'requires_recovery': value['phase'] != 'idle' or not clock_matches(value['clock'], self._now()),
                'live_ready': False}

    def recover(self, *, expected_state_sha256, local_guard=None):
        """Explicit stopped rate-reservation/clock recovery; no token/room writes.

        Preserve full stored relative waits and any indefinite halt. Unknown
        reservations add >=60 s; lost upstream response delays are not knowable.
        Recovery never replays an OAuth mutation or establishes provider identity.
        """
        self._offline()
        if local_guard is not None and not callable(local_guard):
            raise AuthError('invalid_local_guard')
        try:
            initial = self._read()
            with owner_lease(self.root, account_id=initial['user_id']):
                if local_guard is not None: local_guard()
                return self._recover_locked(initial, expected_state_sha256)
        except AuthError: raise
        except Exception: raise AuthError('provider_pacing_recovery_refused') from None

    def _recover_locked(self, initial, expected_state_sha256):
        with self._lease(0):
            value = self._read()
            if value != initial or digest(value) != expected_state_sha256: raise AuthError('provider_pacing_state_changed')
            now = self._now()
            if value['phase'] == 'idle' and clock_matches(value['clock'], now):
                raise AuthError('provider_pacing_recovery_unavailable')
            self._held = deepcopy(value)
            try:
                remaining = max(60., value['next'] - value['clock']['monotonic'], value['blocked'] - value['clock']['monotonic'])
                value.update(phase='idle', clock=now, next=now['monotonic'] + value['interval'],
                             blocked=now['monotonic'] + remaining)
                self._save(value)
            finally: self._held = None
        return self.snapshot()


class ProviderRequestGate(PersistentRequestGate):
    """Distinct prepared authority ledger; fixture evidence cannot become live."""
    state_path = PROVIDER_STATE_PATH
    anchor_path = PROVIDER_ANCHOR_PATH
    lock_path = PROVIDER_LOCK_PATH

    def _offline(self):
        # Private fixed preparation is local filesystem work, never admission to
        # upstream execution. Invented qualification still requires isolation.
        if self.execution not in ('provider-disabled', 'provider-pilot', 'provider-live-pilot'):
            super()._offline()

    def __init__(self, root, client_id, *, execution, client_profile_sha256,
                 minimum_interval=1.2, clock=clock_sample, budget_loader=None):
        self._provider_binding(execution, client_profile_sha256)
        self._bind_budget(budget_loader)
        if self._budget is not None: minimum_interval = max(minimum_interval, self._budget['minimum_interval_seconds'])
        super().__init__(root, client_id, minimum_interval=minimum_interval, clock=clock)

    def _bind_budget(self, loader):
        if loader is not None and not callable(loader): raise AuthError('invalid_provider_budget_loader')
        self._budget_loader = loader
        self._budget = None if loader is None else loader()

    def _current(self):
        config = super()._current()
        if self._budget_loader is not None and self._budget_loader() != self._budget:
            raise AuthError('provider_budget_changed')
        return config

    def _wait_seconds(self, value, now):
        if self._budget is None: return super()._wait_seconds(value, now)
        # Exact existing owner GCRA tolerance; blocked/halt/crash fences retain
        # precedence. The unchanged `next` field is theoretical arrival time.
        interval = max(value['interval'], self.interval)
        earliest = value['next'] - (self._budget['burst_capacity'] - 1) * interval
        return max(earliest, value['blocked']) - now['monotonic']

    def _next_time(self, value, now):
        if self._budget is None: return super()._next_time(value, now)
        return max(value['next'], now['monotonic']) + value['interval']

    def _provider_binding(self, execution, client_profile_sha256):
        import re
        if execution not in ('provider-disabled', 'provider-fixture', 'provider-pilot', 'provider-live-pilot'):
            raise AuthError('invalid_provider_execution')
        if (not isinstance(client_profile_sha256, str)
                or not re.fullmatch('[a-f0-9]{64}', client_profile_sha256)):
            raise AuthError('invalid_client_profile_binding')
        self.execution = execution
        self.minimum_spacing = .05 if execution == 'provider-fixture' else 1.2
        self.client_profile_sha256 = client_profile_sha256
        self._usage_started_at = None
        self._usage_last_status = None
        self._usage_kind = 'http'

    @classmethod
    def diagnostic_snapshot(cls, root, client_id, *, execution, client_profile_sha256,
                            user_id, minimum_interval=1.2, budget_loader=None):
        """Read an existing checked ledger; never return an admission authority.

        The caller holds the OAuth read lease, stabilizing the account/profile
        while this shared pacing lease serializes against request outcomes.
        Missing authority is refused, never initialized or repaired.
        """
        gate = object.__new__(cls)
        gate._provider_binding(execution, client_profile_sha256)
        gate._bind_budget(budget_loader)
        if gate._budget is not None: minimum_interval = max(minimum_interval, gate._budget['minimum_interval_seconds'])
        gate._initialize_binding(root, client_id, minimum_interval, clock_sample)
        if user_id is not None: _positive(user_id)
        with diagnostic_lease(contained(gate.root, gate.lock_path)):
            value = gate._read()
            if user_id is not None and value['user_id'] not in (None, user_id):
                raise AuthError('provider_pacing_account_mismatch')
            return gate._snapshot_fields(value)

    def _extra_binding(self):
        return {'client_profile_sha256': self.client_profile_sha256}

    def _snapshot_fields(self, value):
        result = super()._snapshot_fields(value)
        result['budget'] = ({'source':'default-strict','algorithm':'strict-spacing',
                             'minimum_interval_seconds':max(value['interval'],self.interval),'burst_capacity':1}
                            if self._budget is None else
                            {'source':'protected-owner-adoption','algorithm':'gcra-owner',
                             'minimum_interval_seconds':max(value['interval'],self.interval),
                             'burst_capacity':self._budget['burst_capacity']})
        return result

    def _usage_admission(self):
        self._usage_started_at = None
        self._usage_last_status = None
        self._usage_kind = 'http'

    def _usage_emit(self, event, before, after, **kwargs):
        # The observer cannot escape into authority/dispatch control flow.
        try:
            from .provider_usage import record
            record(self.root, ledger=after['ledger_id'], execution=self.execution,
                   before=before['counters'], after=after['counters'], event=event,
                   sequence=after['counters']['dispatched'] or None, kind=self._usage_kind, **kwargs)
        except Exception:
            try:
                from .provider_usage import _failure
                _failure(self.root)
            except Exception: pass

    def _usage_saved(self, before, after):
        if before['counters'] == after['counters']: return
        event = ('replied' if after['counters']['replies'] > before['counters']['replies'] else
                 'uncertain' if after['counters']['uncertain'] > before['counters']['uncertain'] else 'counters')
        duration = (max(0., time.monotonic() - self._usage_started_at)
                    if self._usage_started_at is not None else None)
        self._usage_emit(event, before, after, status=self._usage_last_status, duration=duration)

    def exchange_started(self, *, kind='http'):
        super().exchange_started(kind=kind)
        self._usage_kind = kind
        self._usage_started_at = time.monotonic()
        self._usage_emit('started', self._held, self._held)

    def _usage_wait(self, seconds, *, elapsed=None):
        try:
            from .provider_usage import record
            record(self.root, ledger=self._held['ledger_id'], execution=self.execution,
                   before=self._held['counters'], after=self._held['counters'],
                   event='wait' if elapsed is None else 'wait_finished',
                   sequence=self._held['counters']['attempted'] + 1, delay=seconds, elapsed=elapsed)
        except Exception:
            try:
                from .provider_usage import _failure
                _failure(self.root)
            except Exception: pass
