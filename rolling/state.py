"""Durable ownership journal. Never assume a missing heartbeat means no room."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import threading
import time

from deployment.sqlite_readonly import readonly_database

from .release import atomic_json


class Registry:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"format": 1, "rooms": {}}
        if self.data.get("format") != 1 or not isinstance(self.data.get("rooms"), dict):
            raise ValueError("unsupported ownership journal; no rooms were started")

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def record(self, alias, **values):
        with self.lock:
            new = copy.deepcopy(self.data)
            new["rooms"].setdefault(alias, {}).update(values, updated_at=time.time())
            atomic_json(self.path, new)
            self.data = new

    def require_clean_start(self):
        unresolved = [alias for alias, state in self.snapshot()["rooms"].items()
                      if state.get("room_id") or state.get("ownership_unknown")
                      or state.get("operation") in ("make_pending", "close_pending")]
        if unresolved:
            raise RuntimeError("Ownership requires reconciliation before startup: " + ", ".join(unresolved))


def schemas(configs):
    """Read schema identities, not contents; never execute migrations."""
    paths = {str(Path(value).resolve()) for cfg in configs for key, value in cfg.items()
             if key.endswith("_db") and isinstance(value, str) and value}
    result = {}
    for path in sorted(paths):
        p = Path(path)
        if not p.exists():
            result[path] = None
            continue
        with readonly_database(p, timeout=2) as conn:
            rows = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        result[path] = hashlib.sha256(json.dumps([version, rows], separators=(",", ":")).encode()).hexdigest()
    return result
