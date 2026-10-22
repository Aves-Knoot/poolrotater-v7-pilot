"""Booking orchestration over the existing durable native room engine.

Only explicitly designated aliases with a matching private lease are admitted.
No provider calls, room creation retries or ownership inference live here.
"""
import copy
import json
import time
import uuid
from pathlib import Path

from ..diagnostic_logs.common import private_database, transaction
from ..layout import atomic_json
from ..network.contracts import NetworkError, canonical, require
from ..managed_host.contracts import TERMINAL
from ..managed_host.settings import FIELDS as NATIVE_FIELDS
from .local import LocalSessions
from .policy import enabled_aliases
from .settings import FIELDS, validate_player_patch

COMMAND_OUTCOME_SECONDS = 120


def operation_id(booking_id, stage):
    return str(uuid.uuid5(uuid.UUID(booking_id), 'booking-native:' + stage))


def overlay(root, configs):
    """Runtime-only worker binding; never edits operator defaults or grants."""
    aliases = enabled_aliases(root)
    if not aliases or not (Path(root) / 'state/bookings.sqlite3').exists():
        return configs
    from ..managed_host.policy import state
    sessions = LocalSessions(root, state(root).binding)
    leases = {r['lease']['alias']: r for r in sessions.leases() if not r['cleanup_complete']}
    result = copy.deepcopy(configs)
    for cfg in result:
        alias = cfg['_instance_name']
        if alias not in aliases or alias not in leases:
            continue
        row = leases[alias]; lease = row['lease']
        cfg['_booking_session'] = {k: lease[k] for k in ('id','alias','session_generation','preset')}
        # A reserved empty room is not eligible for discovery refresh or rollout.
        cfg['hard_restart_interval_seconds'] = 0
        cfg.setdefault('_supervisor_overrides', {})['hard_restart_interval_seconds'] = 0
        for key, value in row['settings'].items():
            cfg[key] = copy.deepcopy(value)
            cfg['_supervisor_overrides'][key] = copy.deepcopy(value)
        cfg['_supervisor_overrides']['_booking_session'] = copy.deepcopy(cfg['_booking_session'])
    return result


class Coordinator:
    def __init__(self, owner, clock=time.time):
        require(getattr(owner, 'managed', None) is not None, 'booking_native_management_required')
        self.owner = owner; self.root = Path(owner.runtime); self.clock = clock
        self.state = owner.managed.state
        self.sessions = LocalSessions(self.root, self.state.binding, clock=clock)
        self.path = private_database(self.root / 'state', 'booking-native.sqlite3')
        self.last_write = 0
        with transaction(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY, original TEXT NOT NULL, failure TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS dispatches(
                id TEXT PRIMARY KEY, booking TEXT NOT NULL, alias TEXT, generation TEXT NOT NULL,
                payload TEXT NOT NULL, phase TEXT NOT NULL, dispatched_at REAL)''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(dispatches)')}
            if 'dispatched_at' not in columns:
                db.execute('ALTER TABLE dispatches ADD COLUMN dispatched_at REAL')
            if 'alias' not in columns:
                db.execute('ALTER TABLE dispatches ADD COLUMN alias TEXT')
        # The sessions ledger is in a separate SQLite file; backfill through
        # its validated view rather than creating a cross-ledger SQL join.
        for booking in self.sessions.leases(include_ended=True):
            with transaction(self.path) as db:
                db.execute('UPDATE dispatches SET alias=? WHERE booking=? AND alias IS NULL',
                           (booking['lease']['alias'], booking['lease']['id']))

    def _original(self, lease):
        with transaction(self.path) as db:
            row = db.execute('SELECT original FROM sessions WHERE id=?',(lease['id'],)).fetchone()
            if row:
                return json.loads(row[0])
            native = self.state.room(lease['alias'], private=True)
            require(native['desired']=='stopped' and native['room_id'] is None and
                    native['pending'] is None and native['config'].get('visibility')=='private' and
                    native['phase'] in ('stopped','unobserved'), 'booking_slot_not_closed',409)
            original = copy.deepcopy(native['config'])
            db.execute('INSERT INTO sessions(id,original) VALUES(?,?)',
                       (lease['id'], canonical(original).decode()))
            return original

    def _fail(self, booking_id, code):
        with transaction(self.path) as db:
            db.execute('UPDATE sessions SET failure=? WHERE id=?',(code,booking_id))

    def _operation(self, lease, stage, action, patch=None):
        """Deterministic stage id means uncertainty never repeats a creation."""
        id = operation_id(lease['id'], stage)
        try:
            return self.state.operation(id)
        except NetworkError as exc:
            if exc.code!='managed_operation_not_found':raise
        native = self.state.room(lease['alias'], private=True)
        if native['pending'] is not None:
            return None
        value = {'protocol_version':1,'policy_version':1,'id':id,**self.state.binding,
                 'alias':lease['alias'],'actor':'private-booking','role':'FLEET_ADMIN',
                 'action':action,'expected_revision':native['revision'],
                 'expected_room_generation':native['room_generation'],'timing':'immediate',
                 'patch':patch or {},'expires_at':self.clock()+1800,'cancel_operation_id':None}
        return self.state.enqueue(value)

    def _stage(self, lease, stage, action, patch=None):
        receipt = self._operation(lease,stage,action,patch)
        if receipt and receipt['phase'] in TERMINAL and receipt['phase']!='applied':
            self._fail(lease['id'],'native_operation_requires_review')
        return bool(receipt and receipt['phase']=='applied')

    def _observation(self, row):
        lease = row['lease']; alias = lease['alias']; now = self.clock()
        journal = self.owner.registry.snapshot()['rooms'].get(alias,{})
        worker = self.owner.workers.get(alias)
        live = worker is not None and worker.process.poll() is None and worker.fresh()
        status = worker.status if live else {}
        detail = status.get('booking') or {}
        match = detail.get('match')
        bound = (detail.get('id')==lease['id'] and
                 detail.get('session_generation')==lease['session_generation'])
        known = journal.get('ownership_unknown') is False and journal.get('operation') in ('owned','closed')
        closed = known and journal.get('operation')=='closed' and journal.get('room_id') is None
        # An unused native slot is closed before this lease starts. That is not
        # closure of the new booking. A recorded open/close receipt binds it.
        if closed and row['state']['ready_at'] is None:
            try:opened=self.state.operation(operation_id(lease['id'],'open'))['phase']=='applied'
            except NetworkError:opened=False
            ended=any(r.get('action')=='end'and r['phase']=='applied'for r in row.get('operation_receipts',[]))
            closed=opened or ended or row['state']['phase']=='closing'
        owned = (known and live and status.get('room_id')==journal.get('room_id') and
                 journal.get('room_id') in worker.broker.hub.owned)
        progress=worker.broker.hub.roster_progress()if owned else None
        authoritative=set(worker.broker.players)-{self.owner.configs[alias].get('_authenticated_user_id')}if owned else set()
        roster_known = bool(owned and bound and detail.get('roster_known') is True and
                            worker.broker.known and not worker.broker.uncertain and
                            (progress is None or not progress[2]) and
                            {p['user_id']for p in detail.get('players',[])}==authoritative)
        return {'ready':bool(owned and bound and status.get('ready')),
                'room_id':journal.get('room_id'),
                'incarnation':journal.get('generation') if journal.get('room_id') else None,
                'roster_known':roster_known,'players':detail.get('players',[]) if roster_known else [],
                'match':match if roster_known else None,'closed':closed,
                'ownership_known':known,'updated_at':now}

    def _dispatch(self, row, command):
        lease = row['lease']; id=command['operation_id']; action=command['action']
        if action=='end':
            if self._stage(lease,'end:'+id,'close'):
                self.sessions.acknowledge(id,'applied',application={'application':'live'})
            return
        worker = self.owner.workers.get(lease['alias'])
        if worker is None or worker.process.poll() is not None or not worker.fresh():return
        binding=worker.status.get('booking')or{}
        if (not worker.status.get('ready')or binding.get('id')!=lease['id']or
                binding.get('session_generation')!=lease['session_generation']):return
        if not self.sessions.authorize(lease['id'],command['user_id'],action):
            self.sessions.acknowledge(id,'cancelled',error='booking_forbidden');return
        generation = worker.broker.generation
        with transaction(self.path) as db:
            previous=db.execute('SELECT * FROM dispatches WHERE id=?',(id,)).fetchone()
        if previous:
            age = self.clock() - (previous['dispatched_at'] or self.clock())
            if previous['generation']!=generation or age >= COMMAND_OUTCOME_SECONDS:
                self._ack_unknown(id)
            return  # RPC/worker uncertainty never authorizes a second write.
        with transaction(self.path) as db:
            # A dispatched command is a durable uncertainty boundary. Never
            # issue another gameplay/configuration RPC for this alias until its
            # receipt is terminal, even after a local process restart.
            active=db.execute('''SELECT id,dispatched_at FROM dispatches
                WHERE alias=? AND phase='dispatched' ORDER BY dispatched_at LIMIT 1''',
                              (lease['alias'],)).fetchone()
        if active:
            if self.clock() - (active['dispatched_at'] or self.clock()) >= COMMAND_OUTCOME_SECONDS:
                self._ack_unknown(active['id'])
            else:
                return
        with transaction(self.path) as db:
            db.execute('''INSERT INTO dispatches(id,booking,alias,generation,payload,phase,dispatched_at)
                VALUES(?,?,?,?,?,?,?)''',
                       (id,lease['id'],lease['alias'],generation,canonical(command).decode(),
                        'dispatched',self.clock()))
        try:
            worker.peer.call('booking-control',id,timeout=5)
        except Exception:
            # Delivery may have reached the worker before the transport failed.
            # The fixed failure is terminal so neither this worker nor a later
            # generation can repeat a gameplay mutation.
            self._ack_unknown(id)

    def _ack_unknown(self, operation_id):
        try:
            self.sessions.acknowledge(operation_id,'failed',error='worker_action_outcome_unknown')
        except NetworkError as exc:
            # The worker may have committed its receipt concurrently with a
            # transport timeout. Keep that authoritative receipt and do not
            # let the race take down the controller loop.
            if exc.code not in ('operation_receipt_conflict','invalid_operation_transition'):
                raise
        with transaction(self.path) as db:
            db.execute("UPDATE dispatches SET phase='failed' WHERE id=?",(operation_id,))

    def _one(self, row):
        lease = row['lease']; alias=lease['alias']; id=lease['id']
        if row['cleanup_complete']:return self._observation(row)
        if alias not in enabled_aliases(self.root):return self._observation(row)
        original=self._original(lease)
        observation=self._observation(row)
        state,effects=self.sessions.advance(id,observation)
        row=self.sessions.get(id)
        # Host-side lifecycle receipts are authoritative, including receipts
        # delivered asynchronously by the worker exchange. Release the local
        # per-alias dispatch gate only after such a terminal receipt exists.
        terminal = {r['operation_id']: r['phase'] for r in row.get('operation_receipts',[])
                    if r['phase'] in ('applied','failed','cancelled')}
        if terminal:
            with transaction(self.path) as db:
                for operation, phase in terminal.items():
                    db.execute("UPDATE dispatches SET phase=? WHERE id=? AND phase='dispatched'",
                               (phase,operation))
        with transaction(self.path) as db:
            failure=db.execute('SELECT failure FROM sessions WHERE id=?',(id,)).fetchone()[0]
        if state['phase']=='ended':
            # An external close can leave the managed desired state at running.
            # Stop that intent before restoring defaults so normal startup
            # reconciliation cannot reopen the room after this lease ends.
            if (observation['closed'] and observation['ownership_known'] and
                    observation['room_id'] is None and
                    self.state.room(alias,private=True)['desired']!='stopped'):
                if not self._stage(lease,'confirmed-room-closed','close'):
                    return observation
            # A native close may finish between polling turns. Once closure is
            # verified, resolve any end receipt before pending() hides it as
            # part of the ended lifecycle.
            if observation['closed'] and observation['ownership_known'] and observation['room_id'] is None:
                for receipt in row.get('operation_receipts',[]):
                    if receipt['action']=='end' and receipt['phase'] in ('pending','dispatched'):
                        self.sessions.acknowledge(receipt['operation_id'],'applied',
                                                  application={'application':'live'})
                for effect in effects:
                    if effect.get('type')=='close':
                        self.sessions.acknowledge_effect(id,effect['effect_id'])
            # Native settings restoration must complete before the hub releases capacity.
            if self._stage(lease,'restore','configure',original):
                self.sessions.complete_cleanup(id)
            return observation
        if failure:
            native=self.state.room(alias,private=True)
            journal=self.owner.registry.snapshot()['rooms'].get(alias,{})
            if (native['phase']in ('stopped','unobserved')and native['room_id']is None and
                    native['pending']is None and journal.get('operation')=='closed'and
                    journal.get('ownership_unknown')is False and journal.get('room_id')is None):
                # A refused preparation or independently settled native recovery
                # proves closure. Cleanup, not another MakeRoom, is the only next stage.
                observation={**observation,'closed':True,'ownership_known':True,'room_id':None,
                             'incarnation':None,'ready':False}
                self.sessions.advance(id,observation)
            return {**observation,'phase':'unavailable'}
        pending=self.sessions.pending(id)
        closing=state['phase']=='closing' or any(c['action']=='end'for c in pending)
        if closing:
            for effect in effects:
                if effect.get('type')=='close':
                    if self._stage(lease,'expiry:'+effect['effect_id'],'close'):
                        self.sessions.acknowledge_effect(id,effect['effect_id'])
            for command in pending:
                if command['action']=='end':self._dispatch(row,command)
            return observation
        patch={k:copy.deepcopy(v)for k,v in row['settings'].items()if k in NATIVE_FIELDS}
        patch.update(preset=lease['preset'],visibility='private',password=lease['password'])
        if not self._stage(lease,'prepare','configure',patch):return observation
        if not self._stage(lease,'open','open'):return observation
        # Governance changes are durable hub decisions. Their local role
        # mutation has already been admitted by LocalSessions; the worker's
        # normal authenticated roster reconciliation applies the consequence.
        # Gameplay/configuration changes use one in-flight command per alias.
        for command in pending:
            if command['action'] in ('grant','revoke'):
                self.sessions.acknowledge(command['operation_id'],'applied',
                                          application={'application':'live'})
                continue
            self._dispatch(row,command)
            break
        for effect in effects:
            if effect.get('type')=='warning':
                worker=self.owner.workers.get(alias)
                if worker and worker.fresh():
                    # Fixed warning code only; no user-provided account speech.
                    try:
                        worker.peer.call('booking-warning',effect['effect_id'],effect['code'],timeout=5)
                        self.sessions.acknowledge_effect(id,effect['effect_id'])
                    except Exception:pass
        return observation

    def tick(self):
        if self.clock()-self.last_write<1:return
        self.last_write=self.clock(); observations=[]
        for row in self.sessions.leases():
            try:
                value=self._one(row)
            except Exception:
                # Preserve lease/capacity on any validation or native uncertainty.
                value=self._observation(row)
            # Private telemetry is published after the strict lifecycle reducer;
            # map details never enter availability projections or timer evidence.
            from .contracts import map_info,queue_info
            worker=self.owner.workers.get(row['lease']['alias'])
            maps=worker.status.get('public',{})if worker is not None and value['ready']else{}
            try:
                value.update(current_map=map_info(maps.get('current_map')),
                             queue=queue_info(maps.get('queue',[])[:5]),
                             queue_known=bool(value['ready']and maps.get('queue_known')is True))
            except (NetworkError,TypeError,KeyError):
                value.update(current_map=None,queue=[],queue_known=False)
            with transaction(self.path)as db:
                saved=db.execute('SELECT failure FROM sessions WHERE id=?',(row['lease']['id'],)).fetchone()
            if saved and saved[0]:
                value.update(phase='unavailable',failure_code=saved[0])
            observations.append({'booking_id':row['lease']['id'],'observation':value})
        atomic_json(self.root/'state/booking-observations.json',{'format':1,'observations':observations})


def attach(owner):
    if not enabled_aliases(owner.runtime):return None
    return Coordinator(owner)
