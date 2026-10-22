"""Fail closed on schema changes in a rolling candidate's SQLite connections.

This protects normal sqlite3-based application code, not hostile Python code.
Approved releases are still trusted software. Initial coordinated startup uses
the existing store initializers; rolling candidates may only reuse that schema.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sqlite3
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

_CONNECT = sqlite3.connect
_CREATE = {sqlite3.SQLITE_CREATE_TABLE: "table", sqlite3.SQLITE_CREATE_INDEX: "index",
           sqlite3.SQLITE_CREATE_TRIGGER: "trigger", sqlite3.SQLITE_CREATE_VIEW: "view"}
_DENY = {getattr(sqlite3, name) for name in (
    "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW",
    "SQLITE_ALTER_TABLE", "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_CREATE_VTABLE", "SQLITE_DROP_VTABLE")}
_BOOKING_ALIASES = frozenset(('room1', 'room2', 'room3', 'room4'))
_BOOKING_BINDING_FIELDS = frozenset(('id', 'alias', 'session_generation', 'preset'))


def _booking_session_database(cfg):
    """Return only the controller-prepared ledger for a bound private worker."""
    value = cfg.get('_booking_session')
    if type(value) is not dict or set(value) != _BOOKING_BINDING_FIELDS:
        return None
    booking_id = value.get('id')
    alias = value.get('alias')
    generation = value.get('session_generation')
    preset = value.get('preset')
    try:
        if type(booking_id) is not str or str(uuid.UUID(booking_id)) != booking_id:
            return None
    except (ValueError, AttributeError):
        return None
    if (type(alias) is not str or alias not in _BOOKING_ALIASES
            or cfg.get('_instance_name') != alias
            or type(generation) is not str or re.fullmatch('[a-f0-9]{32}', generation) is None):
        return None
    from poolrotater_host.config import PRESETS
    if type(preset) is not str or preset not in PRESETS:
        return None
    root = cfg.get('_host_root')
    if type(root) is not str or not Path(root).is_absolute():
        return None
    host_root = Path(root)
    state = host_root / 'state'
    database = state / 'bookings.sqlite3'
    try:
        if (any(item.is_symlink() for item in (host_root, *host_root.parents, state, database))
                or not state.is_dir() or not database.is_file()):
            return None
        state_info = state.lstat()
        database_info = database.lstat()
        if (state_info.st_uid != os.geteuid() or stat.S_IMODE(state_info.st_mode) & 0o077
                or not stat.S_ISREG(database_info.st_mode) or database_info.st_uid != os.geteuid()
                or database_info.st_nlink != 1 or stat.S_IMODE(database_info.st_mode) & 0o077):
            return None
    except OSError:
        return None
    return str(database.resolve())


def guarded_connect(database, *args, allowed_paths=None, readonly_paths=(),
                    statistics_root=None, **kwargs):
    raw = str(database)
    filename = unquote(urlsplit(raw).path) if raw.startswith("file:") else raw
    path = str(Path(filename).resolve())
    replica = False
    if statistics_root is not None and raw.startswith('file:') and kwargs.get('uri') is True:
        # The agent atomically switches completed replicas while workers run.
        # Admit their exact closed filenames dynamically, never the staging DB,
        # arbitrary siblings, symlinks, or a writable SQLite connection.
        candidate = Path(filename).absolute()
        base = Path(statistics_root)
        replica = (candidate.name == 'replica.sqlite3'
                   and candidate.parent.parent == base
                   and re.fullmatch('[a-f0-9]{64}', candidate.parent.name) is not None
                   and parse_qs(urlsplit(raw).query) == {'mode': ['ro'], 'immutable': ['1']})
        if replica:
            from poolrotater_host.ratings.migration import _private
            try:
                _private(base, directory=True)
                _private(candidate.parent, directory=True)
                _private(candidate)
            except (OSError, ValueError):
                raise sqlite3.OperationalError('rolling worker statistics replica refused') from None
    if allowed_paths is not None and path not in allowed_paths and not replica:
        raise sqlite3.OperationalError("rolling worker cannot open an undeclared database")
    if not Path(path).is_file():
        raise sqlite3.OperationalError("rolling worker cannot create a database")
    conn = _CONNECT(database, *args, **kwargs)
    try:
        existing = set(conn.execute("SELECT type,name FROM sqlite_master").fetchall())
        conn.enable_load_extension(False)
        readonly = replica or path in readonly_paths

        def authorize(action, first, second, database_name, trigger):
            if action in _DENY:
                return sqlite3.SQLITE_DENY
            if action in _CREATE and (_CREATE[action], first) not in existing:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_PRAGMA and first.lower() in (
                    "writable_schema", "schema_version", "user_version", "application_id") and second is not None:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and str(second).lower() == "load_extension":
                return sqlite3.SQLITE_DENY
            if readonly and action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn
    except BaseException:
        conn.close()
        raise


def install_guard(cfg):
    allowed = {str(Path(value).resolve()) for key, value in cfg.items()
               if key.endswith("_db") and isinstance(value, str) and value}
    if cfg.get('ruleset_id') == 2 and cfg.get('catch_catalog'):
        candidate = Path(cfg['catch_catalog']).resolve()
        if candidate.is_file(): allowed.add(str(candidate))
    if cfg.get('_host_root'):
        # This exact cache has a reviewed schema prepared by the controller or
        # independent reporting agent. Workers may enqueue lookups, never create
        # arbitrary databases or run migrations through the rolling guard.
        allowed.add(str((Path(cfg['_host_root'])/'state/activity-cache.sqlite3').resolve()))
        # Diagnostic redaction reads only recognized private password fields.
        # Workers never write command intent, revisions or capacity authority.
        managed=str((Path(cfg['_host_root'])/'state/managed-host.sqlite3').resolve())
        allowed.add(managed)
        # A bound private-booking worker receives one pre-created local ledger.
        # Its path is fixed beneath the installed host root; user-selected DB
        # paths and workers without an exact lease binding remain denied.
        booking_database = _booking_session_database(cfg)
        if booking_database is not None:
            allowed.add(booking_database)
    readonly = {str(Path(cfg[key]).resolve()) for key in ("farm_db", "loved_db", "mania_db", "mania_farm_db", "catch_catalog")
                if cfg.get(key)}
    if cfg.get('_host_root'):readonly.add(managed)
    statistics_root = (Path(cfg['_host_root']).absolute()/'state/activity-replica-downloads'
                       if cfg.get('_host_root') else None)
    sqlite3.connect = lambda database, *a, **kw: guarded_connect(
        database, *a, allowed_paths=allowed, readonly_paths=readonly,
        statistics_root=statistics_root, **kw)
