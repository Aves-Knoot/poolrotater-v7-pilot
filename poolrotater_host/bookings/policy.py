"""Explicit local designation of private slots; absent means disabled."""
import json
import os
from pathlib import Path
import sqlite3
import stat
from urllib.parse import quote

from ..diagnostic_logs.common import check_database
from ..layout import atomic_json, checked_root, load as load_host, read_json
from ..network.contracts import fields, require

NAME='config/private-bookings.json'
ALIASES=frozenset(('room1','room2','room3','room4'))
BOOKINGS_DB='state/bookings.sqlite3'
MANAGED_DB='state/managed-host.sqlite3'


def _policy(value):
    fields(value,('format','enabled','aliases'))
    require(type(value['aliases']) is list and len(value['aliases'])<=4 and
            all(type(alias) is str and alias in ALIASES for alias in value['aliases']),
            'booking_policy_refused')
    require(type(value['format']) is int and value['format']==1 and
            type(value['enabled']) is bool and
            len(set(value['aliases']))==len(value['aliases']) and
            (value['enabled'] or not value['aliases']),'booking_policy_refused')
    return {'format':1,'enabled':value['enabled'],'aliases':list(value['aliases'])}


def _config_directory(root):
    path=Path(root)/'config'
    try:mode=path.lstat()
    except OSError:raise ValueError('booking_policy_directory_required') from None
    require(stat.S_ISDIR(mode.st_mode) and mode.st_uid==os.geteuid() and
            not mode.st_mode&0o077,'booking_policy_directory_refused')
    return path

def load(root):
    path=Path(root)/NAME
    if not os.path.lexists(path):return {'format':1,'enabled':False,'aliases':[]}
    return _policy(read_json(path,maximum=2048))

def enabled_aliases(root):
    value=load(root);return frozenset(value['aliases'])if value['enabled']else frozenset()


def _readonly_rows(root, relative, table, columns):
    path=Path(root)/relative
    if not path.exists():return None
    check_database(path)
    uri='file:'+quote(str(path),safe='/')+'?mode=ro'
    try:
        db=sqlite3.connect(uri,uri=True,timeout=1)
        db.row_factory=sqlite3.Row
        try:
            actual={row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            require(set(columns)<=actual,'booking_local_state_uncertain',409)
            return [dict(row) for row in db.execute(f'SELECT {",".join(columns)} FROM {table}')]
        finally:db.close()
    except (sqlite3.Error,OSError):
        raise ValueError('booking_local_state_uncertain') from None


def _active_aliases(root):
    rows=_readonly_rows(root,BOOKINGS_DB,'host_bookings',('alias','cleanup_complete'))
    if rows is None:return set()
    result=set()
    for row in rows:
        require(row['alias'] in ALIASES and row['cleanup_complete'] in (0,1),
                'booking_local_state_uncertain',409)
        if row['cleanup_complete']==0:result.add(row['alias'])
    return result


def native_binding(root):
    """Read existing authority without migrations, credential access or writes.

    Worker SQLite guards keep managed intent read-only. Constructing State here
    would incorrectly enter its schema/rebinding transaction from a worker.
    """
    marker,_=load_host(root)
    rows=_readonly_rows(root,MANAGED_DB,'meta',('key','value'))
    row=next((r for r in rows or [] if r['key']=='binding'),None)
    require(row is not None,'booking_native_state_unavailable',409)
    try:value=json.loads(row['value'])
    except (TypeError,ValueError):raise ValueError('booking_native_state_uncertain') from None
    require(type(value)is dict and set(value)=={'host_id','installation_id','generation'},
            'booking_native_state_uncertain',409)
    from ..network.contracts import identifier
    for key in value:identifier(value[key])
    require(all(value[k]==marker[k]for k in ('installation_id','generation')),
            'booking_native_state_uncertain',409)
    return value


def _native_rooms(root):
    marker,host=load_host(root)
    expected={room['alias'] for room in host['rooms']}
    rows=_readonly_rows(root,MANAGED_DB,'rooms',
                        ('alias','desired','config','phase','room_id','room_generation','pending','failure'))
    require(rows is not None,'booking_native_state_unavailable',409)
    # The read-only view is bound to this installed root. Never initialize or
    # rebind the native managed database as a side effect of policy changes.
    binding_rows=_readonly_rows(root,MANAGED_DB,'meta',('key','value'))
    binding_row=next((row for row in binding_rows or [] if row['key']=='binding'),None)
    require(binding_row is not None,'booking_native_state_unavailable',409)
    try:binding=json.loads(binding_row['value'])
    except (TypeError,ValueError):raise ValueError('booking_native_state_uncertain') from None
    require(type(binding) is dict and binding.get('installation_id')==marker['installation_id'] and
            binding.get('generation')==marker['generation'],'booking_native_state_uncertain',409)
    result={}
    for row in rows:
        require(row['alias'] in expected and row['alias'] in ALIASES and
                row['alias'] not in result,'booking_native_state_uncertain',409)
        try:row['config']=json.loads(row['config'])
        except (TypeError,ValueError):raise ValueError('booking_native_state_uncertain') from None
        require(type(row['config']) is dict,'booking_native_state_uncertain',409)
        result[row['alias']]=row
    return marker,expected,result


def _confirmed_unused(root, aliases):
    marker,host_aliases,rooms=_native_rooms(root)
    require(set(aliases)<=host_aliases and set(aliases)<=set(rooms),
            'booking_alias_unknown',409)
    journal_path=Path(root)/'state/rolling/ownership.json'
    journal=None
    if os.path.lexists(journal_path):
        from ..reconciliation import _journal
        journal=_journal(read_json(journal_path,maximum=65536),host_aliases)['rooms']
    for alias in aliases:
        room=rooms[alias]
        require(room['config'].get('visibility')=='private' and room['desired']=='stopped' and
                room['phase'] in ('stopped','unobserved') and room['room_id'] is None and
                room['room_generation'] is None and room['pending'] is None and
                room['failure'] is None,'booking_slot_not_closed',409)
        owner=journal.get(alias) if journal is not None else None
        if owner is not None:
            require(owner.get('operation')=='closed' and owner.get('ownership_unknown') is False and
                    owner.get('room_id') is None and not any(owner.get(key) for key in
                    ('pid','creation_candidate','target','error')),
                    'booking_slot_not_closed',409)
        else:
            require(room['phase']=='unobserved','booking_slot_not_closed',409)


def configure(root, value):
    """Atomically enable only unused private aliases; never starts native work."""
    root=checked_root(Path(root))
    _config_directory(root)
    requested=_policy(value)
    current=load(root)
    removed=set(current['aliases'])-set(requested['aliases'])
    # An active or uncertain lease always holds its alias, even if the global
    # booking policy was previously disabled or the request disables it now.
    active=_active_aliases(root)
    held=active if not requested['enabled'] else removed&active
    require(not held,'booking_slot_in_use',409)
    added=set(requested['aliases'])-set(current['aliases'])
    if requested['enabled'] and added:
        _confirmed_unused(root,added)
    atomic_json(root/NAME,requested)
    restart=bool(requested['enabled'] and not current['enabled'])
    return {'enabled':requested['enabled'],'aliases':requested['aliases'],
            'network_agent_restart_required':restart,
            'restart_note':('The already-running network agent must be restarted once to begin booking exchanges.'
                            if restart else None)}

def holds_updates(root):
    # Unknown local state holds ordinary updates; it never grants lifecycle authority.
    path=Path(root)/'state/bookings.sqlite3'
    if not os.path.lexists(path):return False
    try:
        return bool(_active_aliases(root))
    except Exception:return True

def startup_leases(root):
    """Exact durable work identity for the existing stopped-host reconciler."""
    if not enabled_aliases(root)or not (Path(root)/'state/bookings.sqlite3').exists():return []
    from .local import LocalSessions
    from ..managed_host.policy import state
    native=state(root);store=LocalSessions(root,native.binding)
    result=[]
    for row in store.leases():
        lease=row['lease'];room=native.room(lease['alias'],private=True)
        require(lease['alias']in enabled_aliases(root)and room['config'].get('visibility')=='private',
                'booking_policy_refused',409)
        if not row['cleanup_complete']:
            result.append(lease['id']+':'+lease['session_generation'])
    return sorted(result)
