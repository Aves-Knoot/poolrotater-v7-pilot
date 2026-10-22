"""Fail closed on schema changes in a rolling candidate's SQLite connections.

This protects normal sqlite3-based application code, not hostile Python code.
Approved releases are still trusted software. Initial coordinated startup uses
the existing store initializers; rolling candidates may only reuse that schema.
"""
from __future__ import annotations

from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

_CONNECT = sqlite3.connect
_CREATE = {sqlite3.SQLITE_CREATE_TABLE: "table", sqlite3.SQLITE_CREATE_INDEX: "index",
           sqlite3.SQLITE_CREATE_TRIGGER: "trigger", sqlite3.SQLITE_CREATE_VIEW: "view"}
_DENY = {getattr(sqlite3, name) for name in (
    "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW",
    "SQLITE_ALTER_TABLE", "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_CREATE_VTABLE", "SQLITE_DROP_VTABLE")}


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
    readonly = {str(Path(cfg[key]).resolve()) for key in ("farm_db", "loved_db", "mania_db", "mania_farm_db", "catch_catalog")
                if cfg.get(key)}
    if cfg.get('_host_root'):readonly.add(managed)
    statistics_root = (Path(cfg['_host_root']).absolute()/'state/activity-replica-downloads'
                       if cfg.get('_host_root') else None)
    sqlite3.connect = lambda database, *a, **kw: guarded_connect(
        database, *a, allowed_paths=allowed, readonly_paths=readonly,
        statistics_root=statistics_root, **kw)
