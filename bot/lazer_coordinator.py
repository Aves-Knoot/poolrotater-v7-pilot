"""Cross-process ownership queue for one local osu!lazer client.

osu!lazer's release client is single-instance and refuses to join a room while
its multiplayer client is already in another one. Multiple PoolRotater room
processes therefore share this tiny SQLite queue. Before an osu! chat send, the
head waiter briefly owns lazer: it leaves the previous room with Escape+Enter,
opens ``osu://room/<id>``, waits for the join, then holds the room for a minimum
dwell period before another process may move the client.

This is intentionally Windows-only UI automation. It is not a remote-control
protocol and cannot move a lazer client running on a different computer.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Callable


log = logging.getLogger("lazer_coordinator")


class LazerCoordinator:
    """Fairly serialize room switches made by separate lobby processes."""

    def __init__(
        self,
        cfg: dict,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        launcher: Callable[[int], None] | None = None,
        leaver: Callable[[], bool] | None = None,
    ):
        self.cfg = cfg
        self._clock = clock
        self._sleep = sleeper
        self._launcher = launcher or self._default_launch_room
        self._leaver = leaver or self._default_leave_room
        self._local_lock = threading.Lock()
        self._warned_non_windows = False
        self.path = Path(str(
            cfg.get("lazer_coordinator_db", "state/lazer_coordinator.db")
        ))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS lazer_room_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    room_id INTEGER NOT NULL DEFAULT 0,
                    switched_at REAL NOT NULL DEFAULT 0,
                    ready_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 0,
                    visit_until REAL NOT NULL DEFAULT 0,
                    action_count INTEGER NOT NULL DEFAULT 0,
                    owner TEXT NOT NULL DEFAULT ''
                );
                INSERT OR IGNORE INTO lazer_room_state(id) VALUES(1);

                CREATE TABLE IF NOT EXISTS lazer_room_queue (
                    ticket INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_key TEXT NOT NULL UNIQUE,
                    room_id INTEGER NOT NULL,
                    instance_name TEXT NOT NULL,
                    requested_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_lazer_room_queue_time
                    ON lazer_room_queue(requested_at);
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(lazer_room_state)")
            }
            if "visit_until" not in columns:
                conn.execute(
                    "ALTER TABLE lazer_room_state "
                    "ADD COLUMN visit_until REAL NOT NULL DEFAULT 0"
                )
            if "action_count" not in columns:
                conn.execute(
                    "ALTER TABLE lazer_room_state "
                    "ADD COLUMN action_count INTEGER NOT NULL DEFAULT 0"
                )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.execute("PRAGMA busy_timeout=20000")
        return conn

    def _bool(self, key: str, default: bool) -> bool:
        value = self.cfg.get(key, default)
        return value if isinstance(value, bool) else default

    def _float(self, key: str, default: float, low: float, high: float) -> float:
        try:
            return min(high, max(low, float(self.cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    @property
    def enabled(self) -> bool:
        return self._bool("lazer_coordinator_enabled", os.name == "nt")

    def ensure_room(
        self,
        room_id: int,
        *,
        reason: str = "outgoing chat",
        force: bool = False,
    ) -> bool:
        """Wait for fair ownership and make ``room_id`` active in lazer.

        ``force`` reopens even when the database already names this room. It is
        used at room creation and after a failed chat send, where manual client
        movement may have made the shared state stale.
        """
        if not self.enabled:
            return False
        if not room_id:
            return False
        if os.name != "nt" and self._launcher == self._default_launch_room:
            if not self._warned_non_windows:
                log.warning(
                    "lazer coordination is enabled but this host is not Windows; "
                    "a local companion is required when lazer runs elsewhere"
                )
                self._warned_non_windows = True
            return False

        with self._local_lock:
            return self._ensure_room_locked(int(room_id), reason=reason, force=force)

    def _ensure_room_locked(self, room_id: int, *, reason: str, force: bool) -> bool:
        request_key = uuid.uuid4().hex
        instance = str(self.cfg.get("_instance_name", "room") or "room")[:80]
        timeout = self._float("lazer_switch_timeout_seconds", 45.0, 5.0, 300.0)
        poll = self._float("lazer_queue_poll_seconds", 0.10, 0.05, 1.0)
        deadline = self._clock() + timeout

        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO lazer_room_queue(request_key, room_id, instance_name, requested_at) "
                "VALUES(?, ?, ?, ?)",
                (request_key, room_id, instance, self._clock()),
            )
            conn.commit()
        finally:
            conn.close()

        try:
            while self._clock() < deadline:
                now = self._clock()
                conn = self._connect()
                plan: tuple[int, int, float, int] | None = None
                wait_for = poll
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    # A killed process must not block all future room movement.
                    conn.execute(
                        "DELETE FROM lazer_room_queue WHERE requested_at < ?",
                        (now - max(120.0, timeout * 2.0),),
                    )
                    head = conn.execute(
                        "SELECT request_key, room_id FROM lazer_room_queue "
                        "ORDER BY ticket LIMIT 1"
                    ).fetchone()
                    state = conn.execute(
                        "SELECT room_id, ready_at, lease_until, generation, "
                        "visit_until, action_count "
                        "FROM lazer_room_state WHERE id=1"
                    ).fetchone()
                    current_room = int(state[0] or 0)
                    ready_at = float(state[1] or 0.0)
                    lease_until = float(state[2] or 0.0)
                    generation = int(state[3] or 0)
                    visit_until = float(state[4] or 0.0)
                    action_count = int(state[5] or 0)
                    dwell = self._float(
                        "lazer_minimum_room_dwell_seconds", 2.0, 0.0, 60.0
                    )
                    max_visit = max(dwell, self._float(
                        "lazer_visit_max_seconds", 10.0, 1.0, 60.0
                    ))
                    try:
                        max_actions = min(50, max(
                            1, int(self.cfg.get("lazer_visit_max_actions", 8))
                        ))
                    except (TypeError, ValueError):
                        max_actions = 8
                    head_is_self = bool(
                        head and str(head[0]) == request_key
                    )
                    same_ready = bool(
                        current_room == room_id and now >= ready_at and not force
                    )
                    # Closely-spaced actions from the room already displayed in
                    # lazer may bypass an older waiter. This is deliberately
                    # bounded by both a quiet lease, a visit deadline, and an
                    # action cap so a busy room cannot starve the other room.
                    continue_batch = bool(
                        same_ready
                        and now < lease_until
                        and now < visit_until
                        and action_count < max_actions
                    )

                    if continue_batch:
                        new_lease = min(
                            visit_until,
                            max(lease_until, now + dwell),
                        )
                        conn.execute(
                            "UPDATE lazer_room_state SET lease_until=?, "
                            "action_count=? WHERE id=1",
                            (new_lease, action_count + 1),
                        )
                        conn.execute(
                            "DELETE FROM lazer_room_queue WHERE request_key=?",
                            (request_key,),
                        )
                        conn.commit()
                        log.debug(
                            "lazer kept room %s for batched action %d/%d (%s)",
                            room_id, action_count + 1, max_actions, reason,
                        )
                        return True
                    if not head_is_self:
                        wait_for = poll
                    elif same_ready:
                        # No older waiter remains. Start a fresh bounded batch
                        # without pointlessly leaving and reopening this room.
                        conn.execute(
                            "UPDATE lazer_room_state SET lease_until=?, "
                            "visit_until=?, action_count=1 WHERE id=1",
                            (now + dwell, now + max_visit),
                        )
                        conn.execute(
                            "DELETE FROM lazer_room_queue WHERE request_key=?",
                            (request_key,),
                        )
                        conn.commit()
                        log.debug(
                            "lazer already assigned to room %s for %s",
                            room_id, reason,
                        )
                        return True
                    elif current_room != room_id and now < lease_until:
                        wait_for = min(max(lease_until - now, poll), 1.0)
                    elif current_room == room_id and now < ready_at and not force:
                        wait_for = min(max(ready_at - now, poll), 1.0)
                    else:
                        confirm_delay = self._float(
                            "lazer_leave_confirm_delay_seconds", 0.5, 0.0, 5.0
                        ) if current_room else 0.0
                        leave_delay = self._float(
                            "lazer_leave_delay_seconds", 1.0, 0.0, 10.0
                        ) if current_room else 0.0
                        join_delay = self._float(
                            "lazer_join_delay_seconds",
                            float(self.cfg.get("lazer_rejoin_delay_seconds", 5.0) or 5.0),
                            0.0,
                            30.0,
                        )
                        generation += 1
                        ready_at = now + confirm_delay + leave_delay + join_delay
                        lease_until = ready_at + dwell
                        visit_until = ready_at + max_visit
                        conn.execute(
                            """
                            UPDATE lazer_room_state
                            SET room_id=?, switched_at=?, ready_at=?, lease_until=?,
                                generation=?, visit_until=?, action_count=1,
                                owner=?
                            WHERE id=1
                            """,
                            (
                                room_id, now, ready_at, lease_until,
                                generation, visit_until,
                                f"{instance}:{os.getpid()}",
                            ),
                        )
                        conn.execute(
                            "DELETE FROM lazer_room_queue WHERE request_key=?",
                            (request_key,),
                        )
                        conn.commit()
                        plan = (current_room, room_id, leave_delay, generation)
                finally:
                    if conn.in_transaction:
                        conn.rollback()
                    conn.close()

                if plan is not None:
                    previous_room, target_room, leave_delay, generation = plan
                    try:
                        if previous_room:
                            left = self._leaver()
                            if not left:
                                log.warning(
                                    "could not confirm a lazer window for room switch "
                                    "%s -> %s; opening target URI anyway",
                                    previous_room, target_room,
                                )
                            if leave_delay > 0:
                                self._sleep(leave_delay)
                        self._launcher(target_room)
                        join_delay = self._float(
                            "lazer_join_delay_seconds",
                            float(self.cfg.get("lazer_rejoin_delay_seconds", 5.0) or 5.0),
                            0.0,
                            30.0,
                        )
                        if join_delay > 0:
                            self._sleep(join_delay)
                        log.info(
                            "lazer assigned to room %s for %s (previous=%s, "
                            "minimum_dwell=%.1fs)",
                            target_room,
                            reason,
                            previous_room or "none",
                            self._float(
                                "lazer_minimum_room_dwell_seconds", 2.0, 0.0, 60.0
                            ),
                        )
                        return True
                    except Exception:
                        log.exception("could not move lazer to room %s", target_room)
                        self._mark_failed(target_room, generation)
                        return False

                self._sleep(max(0.01, wait_for))

            log.error(
                "timed out after %.1fs waiting to assign lazer to room %s for %s",
                timeout, room_id, reason,
            )
            return False
        finally:
            try:
                conn = self._connect()
                conn.execute(
                    "DELETE FROM lazer_room_queue WHERE request_key=?",
                    (request_key,),
                )
                conn.commit()
                conn.close()
            except Exception:
                log.debug("could not clean stale lazer queue ticket", exc_info=True)

    def _mark_failed(self, room_id: int, generation: int) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT room_id, generation FROM lazer_room_state WHERE id=1"
            ).fetchone()
            if row and int(row[0]) == room_id and int(row[1]) == generation:
                conn.execute(
                    "UPDATE lazer_room_state SET room_id=0, ready_at=0, "
                    "lease_until=0, visit_until=0, action_count=0, "
                    "owner='' WHERE id=1"
                )
            conn.commit()
        finally:
            conn.close()

    def snapshot(self) -> dict[str, float | int | str]:
        """Return coordinator state for diagnostics and tests."""
        conn = self._connect()
        try:
            state = conn.execute(
                "SELECT room_id, ready_at, lease_until, generation, owner, "
                "visit_until, action_count "
                "FROM lazer_room_state WHERE id=1"
            ).fetchone()
            queued = int(conn.execute(
                "SELECT COUNT(*) FROM lazer_room_queue"
            ).fetchone()[0])
        finally:
            conn.close()
        return {
            "room_id": int(state[0] or 0),
            "ready_at": float(state[1] or 0.0),
            "lease_until": float(state[2] or 0.0),
            "generation": int(state[3] or 0),
            "owner": str(state[4] or ""),
            "visit_until": float(state[5] or 0.0),
            "action_count": int(state[6] or 0),
            "queued": queued,
        }

    @staticmethod
    def _default_launch_room(room_id: int) -> None:
        if os.name != "nt" or not hasattr(os, "startfile"):
            raise OSError("osu! room URI launching is only available on Windows")
        os.startfile(f"osu://room/{int(room_id)}")  # type: ignore[attr-defined]

    @staticmethod
    def _post_virtual_key(user32, hwnd: int, virtual_key: int) -> bool:
        """Post one complete key press to a specific native window."""
        scan = int(user32.MapVirtualKeyW(int(virtual_key), 0))
        down_lparam = 1 | (scan << 16)
        up_lparam = down_lparam | (1 << 30) | (1 << 31)
        down = user32.PostMessageW(hwnd, 0x0100, virtual_key, down_lparam)
        up = user32.PostMessageW(hwnd, 0x0101, virtual_key, up_lparam)
        return bool(down and up)

    def _post_leave_confirmation(self, user32, hwnd: int) -> bool:
        """Open lazer's leave dialog with Escape, then confirm it with Enter."""
        if not self._post_virtual_key(user32, hwnd, 0x1B):  # VK_ESCAPE
            return False
        delay = self._float(
            "lazer_leave_confirm_delay_seconds", 0.5, 0.0, 5.0
        )
        if delay > 0:
            self._sleep(delay)
        return self._post_virtual_key(user32, hwnd, 0x0D)  # VK_RETURN

    def _default_leave_room(self) -> bool:
        """Send Escape+Enter directly to the release-client lazer window."""
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int
        ]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        candidates: list[int] = []

        def visit(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if "osu!" not in title.value.casefold():
                return True

            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = kernel32.OpenProcess(0x1000, False, pid.value)
            if not process:
                return True
            try:
                size = wintypes.DWORD(2048)
                path = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(
                    process, 0, path, ctypes.byref(size)
                ):
                    executable = Path(path.value).name.casefold()
                    if executable in {"osu!.exe", "osu.exe"}:
                        candidates.append(hwnd)
            finally:
                kernel32.CloseHandle(process)
            return True

        callback = enum_proc(visit)
        user32.EnumWindows(callback, 0)
        if not candidates:
            return False

        return self._post_leave_confirmation(user32, candidates[0])
