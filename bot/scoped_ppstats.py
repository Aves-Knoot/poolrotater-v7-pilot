"""Ruleset-private PP and lobby-time tables, with account privacy still shared."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .ppstats import PPStatsStore


class _ScopedStatsConnection(sqlite3.Connection):
    suffix = ''

    def execute(self, sql, parameters=()):
        for name in ('autohost_pp_achievements', 'autohost_lobby_time',
                     'ix_autohost_pp_user', 'ix_autohost_pp_created',
                     'ix_autohost_lobby_username'):
            sql = re.sub(r'\b' + name + r'\b', name + self.suffix, sql)
        return super().execute(sql, parameters)


class _CatchStatsConnection(_ScopedStatsConnection):
    suffix = '_catch'


class _TaikoStatsConnection(_ScopedStatsConnection):
    suffix = '_taiko'


class ScopedPPStatsStore(PPStatsStore):
    """Base for scoped stores; Standard/Mania callers retain PPStatsStore."""
    ruleset_id = None
    connection_factory = sqlite3.Connection

    def __init__(self, path: str, *, reader=None):
        self.reader = reader
        self.path = str(path)
        if self.path != ':memory:':
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0, factory=self.connection_factory)
        db.execute('PRAGMA busy_timeout = 5000')
        return db


class CatchPPStatsStore(ScopedPPStatsStore):
    ruleset_id = 2
    connection_factory = _CatchStatsConnection

    def __init__(self, path: str, ruleset_id: int = 2, *, reader=None):
        if type(ruleset_id) is not int or ruleset_id != 2:
            raise ValueError('Unsupported PP stats ruleset')
        super().__init__(path, reader=reader)


class TaikoPPStatsStore(ScopedPPStatsStore):
    ruleset_id = 1
    connection_factory = _TaikoStatsConnection

    def __init__(self, path: str, ruleset_id: int = 1, *, reader=None):
        if type(ruleset_id) is not int or ruleset_id != 1:
            raise ValueError('Unsupported PP stats ruleset')
        super().__init__(path, reader=reader)
