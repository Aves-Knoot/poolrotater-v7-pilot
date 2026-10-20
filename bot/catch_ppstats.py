"""Native Catch PP/participation namespace over the unchanged legacy reader."""
from __future__ import annotations
import sqlite3
import time
import re
from pathlib import Path
from .ppstats import PPStatsStore as LegacyPPStatsStore

class _CatchStatsConnection(sqlite3.Connection):
    """Catch participation/PP totals remain separate; privacy stays global."""
    def execute(self, sql, parameters=()):
        for name in ("autohost_pp_achievements", "autohost_lobby_time",
                     "ix_autohost_pp_user", "ix_autohost_pp_created", "ix_autohost_lobby_username"):
            sql = re.sub(r"\b" + name + r"\b", name + "_catch", sql)
        return super().execute(sql, parameters)


class CatchPPStatsStore(LegacyPPStatsStore):
    def __init__(self, path: str, ruleset_id: int = 2, *, reader=None):
        if type(ruleset_id) is not int or ruleset_id != 2:
            raise ValueError("Unsupported PP stats ruleset")
        self.ruleset_id = ruleset_id
        self.reader = reader
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()


    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0,
                             factory=_CatchStatsConnection)
        db.execute("PRAGMA busy_timeout = 5000")
        return db

