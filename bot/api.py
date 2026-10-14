"""osu!web API v2 client (chat, users, room scores).

All API v2 callers can share a small SQLite-backed rate limiter. This matters
when several lobby processes run on one VPS: osu!'s published guidance is to
keep sustained API usage at or below 60 requests/minute, so independent
process-local limiters are not sufficient for multi-room hosting.

Each process reserves its next request slot in the same SQLite database. The
actual network request happens after that slot is reached.
"""
from __future__ import annotations

import copy
import logging
import sqlite3
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import httpx

log = logging.getLogger("api")

API = "https://osu.ppy.sh/api/v2"


class ApiAdmissionDeferred(RuntimeError):
    """The provider gate refused a request before any exchange was dispatched."""


class RateLimiter:
    # Four-hour operations reports need a complete local request window. Keep
    # two extra hours so report scheduling jitter cannot truncate the sample.
    EVENT_RETENTION_SECONDS = 21600.0
    USAGE_WINDOWS = (
        ("5s", 5.0),
        ("30s", 30.0),
        ("1m", 60.0),
        ("5m", 300.0),
        ("10m", 600.0),
        ("30m", 1800.0),
        ("1h", 3600.0),
    )

    def __init__(self, per_minute: int = 60, state_db: str | None = None,
                 usage_log_seconds: float = 60.0, burst_capacity: int = 90):
        self.per_minute = max(int(per_minute), 1)
        self.interval = 60.0 / self.per_minute
        self.burst_capacity = max(int(burst_capacity), 1)
        self.burst_window = (self.burst_capacity - 1) * self.interval
        self._lock = threading.Lock()
        self._next = 0.0
        self.state_db = Path(state_db) if state_db else None
        self.usage_log_seconds = max(5.0, float(usage_log_seconds))
        self._last_usage_log = time.monotonic()
        self._usage_lock = threading.Lock()
        self._memory_events: deque[tuple[float, int | None]] = deque()

        if self.state_db is not None:
            self.state_db.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.state_db, timeout=20)
            conn.execute("PRAGMA busy_timeout=20000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_rate_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_request_events (
                    requested_at REAL NOT NULL,
                    status_code INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_request_events_time "
                "ON api_request_events(requested_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_rate_waiters (
                    reservation_key TEXT PRIMARY KEY,
                    ready_at REAL NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_rate_waiters_ready "
                "ON api_rate_waiters(ready_at)"
            )
            conn.commit()
            conn.close()

    def configure(self, per_minute: int, burst_capacity: int) -> None:
        """Apply a live limiter change without replacing the shared database."""
        per_minute = max(int(per_minute), 1)
        burst_capacity = max(int(burst_capacity), 1)
        with self._lock:
            self.per_minute = per_minute
            self.interval = 60.0 / per_minute
            self.burst_capacity = burst_capacity
            self.burst_window = (burst_capacity - 1) * self.interval
        log.info(
            "API limiter configured: sustained=%d/min burst=%d",
            per_minute,
            burst_capacity,
        )

    def record_request(self, status_code: int | None = None) -> None:
        """Record one real HTTP attempt, including retries and failures."""
        now = time.time()
        try:
            if self.state_db is None:
                with self._usage_lock:
                    self._memory_events.append((now, status_code))
                    cutoff = now - 7200.0
                    while self._memory_events and self._memory_events[0][0] < cutoff:
                        self._memory_events.popleft()
            else:
                conn = sqlite3.connect(self.state_db, timeout=20)
                try:
                    conn.execute("PRAGMA busy_timeout=20000")
                    conn.execute(
                        "INSERT INTO api_request_events(requested_at, status_code) "
                        "VALUES(?, ?)",
                        (now, status_code),
                    )
                    # Retain enough history for the four-hour operations report
                    # plus scheduling jitter; displayed live windows still top
                    # out at one hour.
                    conn.execute(
                        "DELETE FROM api_request_events WHERE requested_at < ?",
                        (now - self.EVENT_RETENTION_SECONDS,),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            # Instrumentation must never stop a lobby API request.
            log.warning("could not record API usage sample", exc_info=True)
            return

        self._maybe_log_usage()

    def usage_snapshot(self) -> list[dict[str, float | int | str]]:
        """Return shared rolling request totals and per-minute averages."""
        now = time.time()
        if self.state_db is None:
            with self._usage_lock:
                events = list(self._memory_events)
        else:
            conn = sqlite3.connect(self.state_db, timeout=20)
            try:
                conn.execute("PRAGMA busy_timeout=20000")
                events = [
                    (float(row[0]), row[1])
                    for row in conn.execute(
                        "SELECT requested_at, status_code FROM api_request_events "
                        "WHERE requested_at >= ?",
                        (now - 3600.0,),
                    )
                ]
            finally:
                conn.close()

        snapshot: list[dict[str, float | int | str]] = []
        for label, seconds in self.USAGE_WINDOWS:
            recent = [event for event in events if event[0] >= now - seconds]
            count = len(recent)
            snapshot.append({
                "label": label,
                "seconds": seconds,
                "count": count,
                "rpm": count * 60.0 / seconds,
                "rate_limited": sum(1 for event in recent if event[1] == 429),
            })
        return snapshot

    def usage_report(self) -> str:
        rows = self.usage_snapshot()
        lines = ["**osu! API usage · rolling averages**"]
        for row in rows:
            lines.append(
                f"{row['label']}: {row['count']} req · {row['rpm']:.1f}/min"
            )
        hour_429 = int(rows[-1]["rate_limited"])
        scope = "shared" if self.state_db is not None else "this process"
        queued, wait_seconds = self.queue_snapshot()
        lines.append(
            f"Limiter: {self.per_minute}/min sustained · burst "
            f"{self.burst_capacity} ({scope})"
        )
        lines.append(
            f"Queue: {queued} waiting request{'s' if queued != 1 else ''} · "
            f"head ready in {wait_seconds:.1f}s · 429s in 1h: {hour_429}"
        )
        return "\n".join(lines)

    def queue_snapshot(self) -> tuple[int, float]:
        """Return the number of delayed shared reservations and head wait."""
        if self.state_db is None:
            return 0, 0.0
        else:
            try:
                conn = sqlite3.connect(self.state_db, timeout=20)
                try:
                    conn.execute("PRAGMA busy_timeout=20000")
                    now = time.time()
                    row = conn.execute(
                        "SELECT COUNT(*), MIN(ready_at) FROM api_rate_waiters "
                        "WHERE ready_at > ?",
                        (now,),
                    ).fetchone()
                finally:
                    conn.close()
                queued = int(row[0] or 0) if row else 0
                wait_seconds = max(0.0, float(row[1]) - now) if row and row[1] else 0.0
            except Exception:
                return 0, 0.0
        return queued, wait_seconds

    def _maybe_log_usage(self) -> None:
        now = time.monotonic()
        with self._usage_lock:
            if now - self._last_usage_log < self.usage_log_seconds:
                return
            self._last_usage_log = now
        try:
            rows = self.usage_snapshot()
            metrics = " | ".join(
                f"{row['label']}={row['count']}req/{row['rpm']:.1f}rpm"
                for row in rows
            )
            log.info(
                "API usage rolling: %s | sustained=%d/min burst=%d | 429s_1h=%d",
                metrics,
                self.per_minute,
                self.burst_capacity,
                int(rows[-1]["rate_limited"]),
            )
        except Exception:
            log.warning("could not summarize API usage", exc_info=True)

    def _reserve_shared_slot(self) -> float:
        """Reserve one global request slot and return seconds to wait."""
        assert self.state_db is not None

        conn = sqlite3.connect(self.state_db, timeout=20)
        try:
            conn.execute("PRAGMA busy_timeout=20000")
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                "SELECT next_at FROM api_rate_state WHERE id=1"
            ).fetchone()

            now = time.time()
            next_at = float(row[0]) if row else now

            # If the wall clock moved a long way or an old state file was
            # restored from another machine, discard the stale reservation.
            stale_horizon = max(300.0, self.burst_window + 300.0)
            if next_at > now + stale_horizon or next_at < now - 300:
                next_at = now

            # GCRA-style reservation. Up to burst_capacity calls may pass at
            # once after an idle period. Every accepted reservation advances
            # the theoretical arrival time by one sustained-rate interval;
            # once burst credit is exhausted, later calls reserve future slots
            # and sleep instead of being discarded.
            earliest = next_at - self.burst_window
            slot = max(now, earliest)
            new_next = max(now, next_at) + self.interval

            conn.execute(
                """
                INSERT INTO api_rate_state(id, next_at)
                VALUES(1, ?)
                ON CONFLICT(id) DO UPDATE SET next_at=excluded.next_at
                """,
                (new_next,),
            )
            conn.execute(
                "DELETE FROM api_rate_waiters WHERE ready_at <= ? OR created_at < ?",
                (now, now - 600.0),
            )
            if slot > now:
                conn.execute(
                    "INSERT INTO api_rate_waiters(reservation_key, ready_at, created_at) "
                    "VALUES(?, ?, ?)",
                    (uuid.uuid4().hex, slot, now),
                )
            conn.commit()
        finally:
            conn.close()

        return max(0.0, slot - time.time())

    def wait(self, cancel_event=None) -> bool:
        def pause(seconds):
            if cancel_event is not None:
                return not cancel_event.wait(max(0, seconds))
            time.sleep(max(0, seconds))
            return True
        if cancel_event is not None and cancel_event.is_set():
            return False
        if self.state_db is None:
            # Single-process fallback with the same burst semantics.
            with self._lock:
                now = time.monotonic()
                earliest = self._next - self.burst_window
                if earliest > now:
                    if not pause(earliest - now):
                        return False
                    now = time.monotonic()
                self._next = max(now, self._next) + self.interval
            return True

        # The thread lock prevents two threads in the same process from
        # needlessly competing for SQLite's write lock. SQLite coordinates the
        # separate room processes.
        with self._lock:
            delay = self._reserve_shared_slot()

        if delay > 0:
            if not pause(delay):
                return False
            try:
                conn = sqlite3.connect(self.state_db, timeout=20)
                conn.execute("PRAGMA busy_timeout=20000")
                conn.execute(
                    "DELETE FROM api_rate_waiters WHERE ready_at <= ?",
                    (time.time(),),
                )
                conn.commit()
                conn.close()
            except Exception:
                log.debug("could not prune completed API queue entries", exc_info=True)
        return True


class OsuApi:
    def __init__(self, tokens, per_minute: int = 60,
                 rate_state_db: str | None = None,
                 usage_log_seconds: float = 60.0,
                 burst_capacity: int = 90,
                 profile_cache_seconds: float = 21600.0):
        self.tokens = tokens
        self.limiter = RateLimiter(
            per_minute,
            state_db=rate_state_db,
            usage_log_seconds=usage_log_seconds,
            burst_capacity=burst_capacity,
        )
        self.http = httpx.Client(
            timeout=httpx.Timeout(connect=10, read=20, write=10, pool=10))
        self._profile_cache_seconds = max(0.0, float(profile_cache_seconds))
        self._profile_cache_lock = threading.Lock()
        self._profile_cache: dict[tuple, tuple[float, object]] = {}

    def close(self) -> None:
        self.http.close()

    def _req(self, method: str, path: str, retries: int = 3, **kw):
        before_request = kw.pop("_before_request", None)
        for attempt in range(retries):
            self.limiter.wait()
            if before_request is not None:
                try:
                    before_request()
                except Exception:
                    log.exception("pre-request preparation failed for %s %s", method, path)
            try:
                r = self.http.request(
                    method, API + path,
                    headers={
                        "Authorization": f"Bearer {self.tokens.access_token()}",
                        "x-api-version": "20220705",
                        "Accept": "application/json",
                        "User-Agent": "poolrotater/0.1",
                    }, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                self.limiter.record_request(None)
                log.warning("%s %s network error: %s", method, path, e)
                time.sleep(1.5 * (attempt + 1))
                continue

            self.limiter.record_request(r.status_code)

            if r.status_code in (200, 201, 204):
                if r.status_code == 204 or not r.text:
                    return {}
                try:
                    return r.json()
                except Exception:
                    return {}
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 10 * (attempt + 1)))
                log.warning("rate limited, sleeping %.0fs", wait)
                time.sleep(min(wait, 120))
                continue
            if 500 <= r.status_code < 600:
                time.sleep(2 ** attempt)
                continue
            log.error("%s %s -> %s %s", method, path, r.status_code, r.text[:200])
            return None
        return None

    # ---------------------------------------------------------- chat

    def chat_ack(self):
        """Keep the authenticated user present in chat."""
        return self._req("POST", "/chat/ack")

    def chat_messages(
        self,
        channel_id: int,
        since: int | None = None,
        *,
        before_request=None,
    ):
        params = {"limit": 50}
        if since:
            params["since"] = since
        return self._req("GET", f"/chat/channels/{channel_id}/messages",
                         params=params, _before_request=before_request) or []

    def chat_send(
        self,
        channel_id: int,
        message: str,
        *,
        before_request=None,
    ):
        """Return the created message so the caller can ignore its own output."""
        return self._req("POST", f"/chat/channels/{channel_id}/messages",
                         json={"message": message, "is_action": False},
                         _before_request=before_request)

    def chat_join(self, channel_id: int, user_id: int):
        """Join/rejoin a public or multiplayer chat channel.

        Requires the chat.write_manage OAuth scope. Multiplayer room creation
        and a user's lazer client can change chat membership independently, so
        callers may safely use this to recover a later 403 from chat_send().
        """
        return self._req(
            "PUT",
            f"/chat/channels/{int(channel_id)}/users/{int(user_id)}",
        )

    # ---------------------------------------------------------- users

    def user(self, user_id: int, mode: str = "osu"):
        key = ("user", int(user_id), str(mode))
        cached = self._profile_cache_get(key)
        if cached is not None:
            return cached
        result = self._req("GET", f"/users/{user_id}/{mode}", params={"key": "id"})
        if isinstance(result, dict):
            self._profile_cache_put(key, result)
        return result

    def user_top_scores(self, user_id: int, mode: str = "osu", limit: int = 100):
        key = ("best", int(user_id), str(mode), int(limit))
        cached = self._profile_cache_get(key)
        if cached is not None:
            return cached
        result = self._req("GET", f"/users/{user_id}/scores/best",
                           params={"mode": mode, "limit": limit})
        if isinstance(result, list):
            self._profile_cache_put(key, result)
            return result
        return []

    def _profile_cache_get(self, key):
        with self._profile_cache_lock:
            cached = self._profile_cache.get(key)
            if cached is None:
                return None
            checked_at, value = cached
            if time.monotonic() - checked_at > self._profile_cache_seconds:
                self._profile_cache.pop(key, None)
                return None
            return copy.deepcopy(value)

    def _profile_cache_put(self, key, value) -> None:
        if self._profile_cache_seconds <= 0:
            return
        with self._profile_cache_lock:
            self._profile_cache[key] = (time.monotonic(), copy.deepcopy(value))
            if len(self._profile_cache) > 4096:
                oldest = min(self._profile_cache, key=lambda item: self._profile_cache[item][0])
                self._profile_cache.pop(oldest, None)

    def configure_profile_cache(self, seconds: float) -> None:
        """Apply a hot-reloaded profile/top-score cache lifetime."""
        seconds = max(0.0, float(seconds))
        with self._profile_cache_lock:
            self._profile_cache_seconds = seconds
            if seconds <= 0:
                self._profile_cache.clear()

    def invalidate_profile_cache(self, user_id: int) -> None:
        """Drop cached profile data after that user submits a room score."""
        user_id = int(user_id)
        with self._profile_cache_lock:
            for key in list(self._profile_cache):
                if len(key) > 1 and key[0] in {"user", "best"} and key[1] == user_id:
                    self._profile_cache.pop(key, None)

    # ---------------------------------------------------------- rooms

    def room(self, room_id: int):
        """Fetch the public room snapshot, including its playlist."""
        return self._req("GET", f"/rooms/{int(room_id)}")

    def room_probe(self, room_id: int, retries: int = 2):
        """Return (status, room) where status is ok/missing/unknown.

        _req() intentionally collapses 404 and several transient failures to
        None, which is fine for ordinary lookups but not for an unattended
        room-liveness watchdog. This probe preserves the important distinction:
        two real 404s can safely trigger room recreation, while a network/API
        hiccup must never destroy a healthy lobby.
        """
        path = f"/rooms/{int(room_id)}"
        for attempt in range(max(1, int(retries))):
            self.limiter.wait()
            try:
                r = self.http.request(
                    "GET", API + path,
                    headers={
                        "Authorization": f"Bearer {self.tokens.access_token()}",
                        "x-api-version": "20220705",
                        "Accept": "application/json",
                        "User-Agent": "poolrotater/0.1",
                    },
                )
            except (httpx.TransportError, httpx.TimeoutException) as e:
                self.limiter.record_request(None)
                log.warning("GET %s liveness probe network error: %s", path, e)
                time.sleep(1.0 * (attempt + 1))
                continue

            self.limiter.record_request(r.status_code)

            if r.status_code == 200:
                try:
                    return "ok", r.json()
                except Exception:
                    return "ok", {}
            if r.status_code == 404:
                return "missing", None
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 3 * (attempt + 1)))
                log.warning("room liveness probe rate limited, sleeping %.0fs", wait)
                time.sleep(min(wait, 15))
                continue
            if 500 <= r.status_code < 600:
                time.sleep(1.5 * (attempt + 1))
                continue

            log.warning(
                "room liveness probe GET %s -> %s %s",
                path, r.status_code, r.text[:160],
            )
            return "unknown", None

        return "unknown", None

    def room_item_scores(self, room_id: int, playlist_item_id: int):
        return self._req(
            "GET", f"/rooms/{room_id}/playlist/{playlist_item_id}/scores")

    def beatmap(self, beatmap_id: int):
        return self._req("GET", f"/beatmaps/{beatmap_id}")

    def beatmapset(self, beatmapset_id: int):
        """Fetch extended set metadata, including download availability."""
        return self._req("GET", f"/beatmapsets/{int(beatmapset_id)}")

    def usage_report(self) -> str:
        """Return compact shared API usage suitable for Discord."""
        return self.limiter.usage_report()

    def configure_rate_limit(self, per_minute: int, burst_capacity: int) -> None:
        """Apply hot-reloaded sustained/burst settings."""
        self.limiter.configure(per_minute, burst_capacity)
