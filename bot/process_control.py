"""Tiny cross-process control bus for coordinated PoolRotater shutdowns."""
from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
import time


log = logging.getLogger("process_control")


class SharedProcessControl:
    """Publish and consume shutdown requests across room processes."""

    def __init__(self, cfg: dict):
        self.instance_name = str(
            cfg.get("discord_control_room_alias")
            or cfg.get("_instance_name")
            or "room1"
        ).strip().casefold()
        self.path = Path(str(
            cfg.get("process_control_db", "state/process_control.db")
        ))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._last_event_id = self._latest_event_id()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS process_control_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    requested_at REAL NOT NULL,
                    requested_by TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS process_control_cooldowns (
                    cooldown_key TEXT PRIMARY KEY,
                    claimed_at REAL NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _latest_event_id(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(event_id), 0) FROM process_control_events"
            ).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()

    def request_shutdown(self, target: str, *, requested_by: str = "") -> int:
        normalized = str(target or "").strip().casefold()
        if not normalized:
            raise ValueError("shutdown target may not be blank")
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO process_control_events(action, target, requested_at, requested_by) "
                "VALUES('shutdown', ?, ?, ?)",
                (normalized, time.time(), str(requested_by or "")[:200]),
            )
            event_id = int(cursor.lastrowid)
            # This is a command bus, not an audit log. Keep only a small tail.
            conn.execute(
                "DELETE FROM process_control_events WHERE event_id < ?",
                (max(0, event_id - 100),),
            )
            conn.commit()
            return event_id
        finally:
            conn.close()

    def pending_shutdown(self) -> dict | None:
        """Return a new shutdown event addressed to this room, if any."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT event_id, target, requested_at, requested_by "
                "FROM process_control_events WHERE event_id > ? "
                "ORDER BY event_id",
                (self._last_event_id,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        self._last_event_id = max(int(row[0]) for row in rows)
        for event_id, target, requested_at, requested_by in rows:
            if str(target).casefold() in {"all", self.instance_name}:
                return {
                    "event_id": int(event_id),
                    "target": str(target),
                    "requested_at": float(requested_at),
                    "requested_by": str(requested_by or ""),
                }
        return None

    def claim_cooldown(
        self, cooldown_key: str, cooldown_seconds: float, *, now: float | None = None,
    ) -> tuple[bool, float, float | None]:
        """Atomically claim a shared cooldown across every room process."""
        key = str(cooldown_key or "").strip().casefold()
        if not key:
            raise ValueError("cooldown key may not be blank")
        cooldown = max(0.0, float(cooldown_seconds))
        claimed_at = float(time.time() if now is None else now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT claimed_at FROM process_control_cooldowns WHERE cooldown_key = ?",
                (key,),
            ).fetchone()
            previous = float(row[0]) if row is not None else None
            remaining = max(0.0, cooldown - (claimed_at - previous)) if previous else 0.0
            if remaining > 0.0:
                conn.rollback()
                return False, remaining, None
            conn.execute(
                "INSERT INTO process_control_cooldowns(cooldown_key, claimed_at) VALUES(?, ?) "
                "ON CONFLICT(cooldown_key) DO UPDATE SET claimed_at = excluded.claimed_at",
                (key, claimed_at),
            )
            conn.commit()
            return True, 0.0, claimed_at
        finally:
            conn.close()

    def release_cooldown(self, cooldown_key: str, claim_time: float | None) -> None:
        """Release a failed reservation without erasing a newer claim."""
        if claim_time is None:
            return
        key = str(cooldown_key or "").strip().casefold()
        if not key:
            return
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM process_control_cooldowns "
                "WHERE cooldown_key = ? AND claimed_at = ?",
                (key, float(claim_time)),
            )
            conn.commit()
        finally:
            conn.close()
