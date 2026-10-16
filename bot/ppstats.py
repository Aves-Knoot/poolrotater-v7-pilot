"""Persistent AutoHost PP-gain and lobby-time tracking.

This table intentionally lives beside map ratings/performance evidence in the
same SQLite database, but it is independent from both.  A .bad/.good vote is a
preference signal; a performance-evidence row is a map-quality signal; these
rows answer a different question: "how much profile PP did this player gain
from scores made in this AutoHost?"  The lobby-time table beside it tracks how
long each player has actually spent present in AutoHost rooms.

Tracking begins when this module is first deployed.  Existing
performance_records are not backfilled because those records are intentionally
sparse and would undercount historical gains. Stats lookup is public by default;
players can persistently opt out of other-player lookups with .hidden and reverse
that choice with .show.
"""
from __future__ import annotations

import sqlite3
import time
import re
from pathlib import Path


class _ManiaStatsConnection(sqlite3.Connection):
    """Namespace mode-specific totals; privacy remains account-wide.

    Only fixed internal SQL identifiers are rewritten, never parameter values.
    Existing standard tables and callers keep their original names/schema.
    """
    def execute(self, sql, parameters=()):
        for name in ("autohost_pp_achievements", "autohost_lobby_time",
                     "ix_autohost_pp_user", "ix_autohost_pp_created", "ix_autohost_lobby_username"):
            sql = re.sub(r"\b" + name + r"\b", name + "_mania", sql)
        return super().execute(sql, parameters)


class PPStatsStore:
    def __init__(self, path: str, ruleset_id: int = 0, *, reader=None):
        if ruleset_id not in (0, 3):
            raise ValueError("Unsupported PP stats ruleset")
        self.ruleset_id = ruleset_id
        self.reader = reader
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0,
                             factory=_ManiaStatsConnection if self.ruleset_id == 3 else sqlite3.Connection)
        db.execute("PRAGMA busy_timeout = 5000")
        return db

    def _ensure_schema(self) -> None:
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS autohost_room_rulesets (room_id INTEGER PRIMARY KEY, ruleset_id INTEGER NOT NULL)")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS autohost_pp_achievements (
                    achievement_key  TEXT PRIMARY KEY,
                    user_id          INTEGER NOT NULL,
                    username         TEXT,
                    beatmap_id       INTEGER NOT NULL,
                    room_id          INTEGER,
                    playlist_item_id INTEGER,
                    score_pp         REAL NOT NULL,
                    profile_gain_pp  REAL NOT NULL,
                    top_rank         INTEGER NOT NULL,
                    accuracy         REAL,
                    created_at       REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_autohost_pp_user
                ON autohost_pp_achievements(user_id)
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_autohost_pp_created
                ON autohost_pp_achievements(created_at)
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS autohost_lobby_time (
                    user_id          INTEGER PRIMARY KEY,
                    username         TEXT,
                    total_seconds    REAL NOT NULL DEFAULT 0.0,
                    visit_count      INTEGER NOT NULL DEFAULT 0,
                    first_join_at    REAL,
                    last_join_at     REAL,
                    last_leave_at    REAL,
                    updated_at       REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_autohost_lobby_username
                ON autohost_lobby_time(username COLLATE NOCASE)
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS autohost_stats_privacy (
                    user_id       INTEGER PRIMARY KEY,
                    username      TEXT,
                    hidden        INTEGER NOT NULL DEFAULT 0,
                    updated_at    REAL NOT NULL
                )
                """
            )

    def record_room_mode(self, room_id: int) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO autohost_room_rulesets(room_id,ruleset_id) VALUES (?,?) ON CONFLICT(room_id) DO UPDATE SET ruleset_id=excluded.ruleset_id", (int(room_id), self.ruleset_id))

    @staticmethod
    def _achievement_key(
        room_id: int | None,
        playlist_item_id: int | None,
        beatmap_id: int,
        user_id: int,
        created_at: float,
    ) -> str:
        # A real playlist item id is the preferred durable match identity.
        # The timestamp fallback is only for abnormal score reports where the
        # referee event did not provide an item id.
        if playlist_item_id is not None:
            return (
                f"room:{int(room_id or 0)}:"
                f"item:{int(playlist_item_id)}:"
                f"user:{int(user_id)}"
            )
        return (
            f"room:{int(room_id or 0)}:"
            f"map:{int(beatmap_id)}:"
            f"user:{int(user_id)}:"
            f"time:{int(created_at)}"
        )

    def record_achievements(
        self,
        *,
        room_id: int | None,
        playlist_item_id: int | None,
        beatmap_id: int,
        achievements: list[dict],
    ) -> int:
        """Insert every detected top-100 event for one completed match.

        INSERT OR IGNORE makes this idempotent if score reporting is retried.
        Rows are retained even when the calculated profile gain is zero;
        user_stats() separately counts/sums only positive profile gains. This
        keeps the backend as the complete top-100 event history while .stats
        remains an honest "PP actually gained" total.
        Returns the number of newly inserted top-100 events.
        """
        if not beatmap_id or not achievements:
            return 0

        now = time.time()
        rows = []
        for a in achievements:
            user_id = int(a["user_id"])
            gain = float(a.get("gain") or 0.0)
            rank = int(a.get("rank") or 0)
            if rank < 1 or rank > 100:
                continue

            rows.append(
                (
                    self._achievement_key(
                        room_id, playlist_item_id, beatmap_id, user_id, now
                    ),
                    user_id,
                    str(a.get("name") or user_id),
                    int(beatmap_id),
                    int(room_id) if room_id is not None else None,
                    int(playlist_item_id)
                    if playlist_item_id is not None
                    else None,
                    float(a.get("pp") or 0.0),
                    gain,
                    rank,
                    float(a.get("accuracy") or 0.0),
                    now,
                )
            )

        if not rows:
            return 0

        inserted = 0
        with self._connect() as db:
            for row in rows:
                cur = db.execute(
                    """
                    INSERT OR IGNORE INTO autohost_pp_achievements (
                        achievement_key,
                        user_id,
                        username,
                        beatmap_id,
                        room_id,
                        playlist_item_id,
                        score_pp,
                        profile_gain_pp,
                        top_rank,
                        accuracy,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                inserted += max(0, int(cur.rowcount or 0))
        return inserted

    def find_user_by_username(self, username: str) -> tuple[int, str] | None:
        """Resolve a previously seen osu! username to its AutoHost user id.

        The lobby-time table stores the latest username seen for each user and is
        the primary source.  The achievement table is a fallback for older rows
        that may predate lobby-time tracking.  Matching is exact but
        case-insensitive so lookups do not require exact capitalization.
        """
        wanted = str(username or "").strip()
        if not wanted:
            return None

        with self._connect() as db:
            row = db.execute(
                """
                SELECT user_id, username
                FROM autohost_lobby_time
                WHERE username = ? COLLATE NOCASE
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (wanted,),
            ).fetchone()
            if row is None:
                row = db.execute(
                    """
                    SELECT user_id, username
                    FROM autohost_pp_achievements
                    WHERE username = ? COLLATE NOCASE
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (wanted,),
                ).fetchone()

        if row is None:
            return None
        return int(row[0]), str(row[1] or row[0])

    def set_stats_hidden(
        self,
        user_id: int,
        username: str | None,
        hidden: bool,
    ) -> None:
        """Persist whether other players may look up this user's stats."""
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO autohost_stats_privacy (
                    user_id, username, hidden, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    hidden = excluded.hidden,
                    updated_at = excluded.updated_at
                """,
                (
                    int(user_id),
                    str(username or user_id),
                    1 if hidden else 0,
                    now,
                ),
            )

    def stats_hidden(self, user_id: int) -> bool:
        """Return True only when the player explicitly opted out.

        No row means public, preserving the requested default for both old and
        newly seen players.
        """
        with self._connect() as db:
            row = db.execute(
                """
                SELECT hidden
                FROM autohost_stats_privacy
                WHERE user_id = ?
                LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
        return bool(row and int(row[0] or 0))

    def username_for_user_id(self, user_id: int) -> str | None:
        """Return the most recently stored username for an AutoHost user."""
        with self._connect() as db:
            row = db.execute(
                """
                SELECT username
                FROM autohost_lobby_time
                WHERE user_id = ?
                LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
            if row is None or not row[0]:
                row = db.execute(
                    """
                    SELECT username
                    FROM autohost_pp_achievements
                    WHERE user_id = ? AND username IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (int(user_id),),
                ).fetchone()

        return str(row[0]) if row and row[0] else None

    def user_stats(self, user_id: int) -> dict:
        """Return persistent AutoHost gain and lobby-time totals."""
        with self._connect() as db:
            pp_row = db.execute(
                """
                SELECT
                    COALESCE(SUM(
                        CASE WHEN profile_gain_pp > 0
                             THEN profile_gain_pp ELSE 0 END
                    ), 0.0),
                    COALESCE(SUM(
                        CASE WHEN profile_gain_pp > 0 THEN 1 ELSE 0 END
                    ), 0),
                    COALESCE(MAX(
                        CASE WHEN profile_gain_pp > 0
                             THEN profile_gain_pp ELSE 0 END
                    ), 0.0),
                    MIN(CASE WHEN profile_gain_pp > 0 THEN created_at END),
                    MAX(CASE WHEN profile_gain_pp > 0 THEN created_at END)
                FROM autohost_pp_achievements
                WHERE user_id = ?
                """,
                (int(user_id),),
            ).fetchone()
            time_row = db.execute(
                """
                SELECT
                    COALESCE(total_seconds, 0.0),
                    COALESCE(visit_count, 0),
                    first_join_at,
                    last_join_at,
                    last_leave_at
                FROM autohost_lobby_time
                WHERE user_id = ?
                """,
                (int(user_id),),
            ).fetchone()

        result = {
            "total_gain": float(pp_row[0] or 0.0),
            "achievement_count": int(pp_row[1] or 0),
            "best_gain": float(pp_row[2] or 0.0),
            "first_gain_at": pp_row[3],
            "last_gain_at": pp_row[4],
            "total_lobby_seconds": float(time_row[0] or 0.0) if time_row else 0.0,
            "visit_count": int(time_row[1] or 0) if time_row else 0,
            "first_join_at": time_row[2] if time_row else None,
            "last_join_at": time_row[3] if time_row else None,
            "last_leave_at": time_row[4] if time_row else None,
        }

        if self.reader is not None:
            try:
                shared = self.reader.user_stats(int(user_id), ruleset_id=self.ruleset_id)
            except Exception:
                shared = None
            if shared and not shared.get("hidden"):
                own = shared.get("requester_host_totals", {})
                result["local_tracked_gain"] = result["total_gain"]
                result["local_tracked_lobby_seconds"] = result["total_lobby_seconds"]
                result["local_unreconciled_gain"] = max(0, result["total_gain"] - own.get("total_gain", 0))
                result["local_unreconciled_lobby_seconds"] = max(0, result["total_lobby_seconds"] - own.get("total_lobby_seconds", 0))
                # Pending local intervals/legacy PP may overlap evidence from
                # other hosts. Preserve them separately; never invent an exact
                # combined sum from scalar cumulative totals.
                for key in ("total_gain", "achievement_count", "best_gain", "total_lobby_seconds", "visit_count",
                            "first_gain_at", "last_gain_at", "first_join_at", "last_join_at", "last_leave_at"):
                    result[key] = shared.get(key, result.get(key))
                result["network_statistics"] = True
                result["network_statistics_fetched_at"] = shared.get("network_statistics_fetched_at")
                result["network_coverage"] = shared.get("coverage", {})
            else:
                result["network_statistics_pending"] = shared is None
                result["network_statistics_hidden"] = bool(shared and shared.get("hidden"))
        return result

    def shared_stats_hidden(self, user_id: int):
        """Tri-state network visibility; None means cache pending, never public proof."""
        if self.reader is None:
            return None
        return self.reader.hidden(int(user_id), ruleset_id=self.ruleset_id)

    def shared_username(self, username: str):
        if self.reader is None:
            return None
        return self.reader.lookup(username, ruleset_id=self.ruleset_id)

    def lifetime_lobby_summary(
        self,
        *,
        live_seconds_by_user: dict[int, float] | None = None,
        live_names_by_user: dict[int, str] | None = None,
        leaderboard_limit: int = 3,
    ) -> dict:
        """Return lifetime public AutoHost totals for the lobby command.

        ``map_plays`` intentionally counts one row per player per started map,
        not one row per multiplayer match. Therefore two players entering the
        same map contributes two map plays, exactly matching the public stat
        shown by ``.lobby``.

        Aggregate totals include hidden users because they are anonymous. The
        named playtime leaderboard excludes anyone who opted out with
        ``.hidden`` so the leaderboard cannot bypass stats privacy. Live,
        unflushed lobby time may be supplied by the bot and is added to both
        the aggregate total and the leaderboard.
        """
        live_seconds_by_user = {
            int(uid): max(0.0, float(seconds or 0.0))
            for uid, seconds in (live_seconds_by_user or {}).items()
        }
        live_names_by_user = {
            int(uid): str(name)
            for uid, name in (live_names_by_user or {}).items()
            if name
        }
        limit = max(0, int(leaderboard_limit or 0))

        with self._connect() as db:
            # Map-play history began later than the original lobby-time
            # tracker, so these two lifetime metrics intentionally come from
            # different ledgers:
            #
            # * map_plays: one autohost_match_players row per player-map play;
            # * players/hours/top playtime: the complete autohost_lobby_time
            #   history, including users from before detailed match tracking
            #   was introduced.
            #
            # Do NOT restrict lobby-time rows to users present in the newer
            # match ledger. Doing so undercounts both lifetime players and
            # player-hours and can also hide legitimate leaderboard entries.
            try:
                map_plays = int(
                    db.execute(
                        "SELECT COUNT(*) FROM autohost_match_players mp "
                        "LEFT JOIN autohost_room_rulesets rr ON rr.room_id=mp.room_id "
                        "WHERE COALESCE(rr.ruleset_id,0)=?", (self.ruleset_id,)
                    ).fetchone()[0]
                    or 0
                )
            except sqlite3.OperationalError:
                map_plays = 0

            try:
                player_rows = db.execute(
                    """
                    SELECT
                        lt.user_id,
                        COALESCE(lt.username, CAST(lt.user_id AS TEXT))
                            AS username,
                        COALESCE(lt.total_seconds, 0.0) AS total_seconds,
                        COALESCE(priv.hidden, 0) AS hidden
                    FROM autohost_lobby_time lt
                    LEFT JOIN autohost_stats_privacy priv
                        ON priv.user_id = lt.user_id
                    """
                ).fetchall()
            except sqlite3.OperationalError:
                player_rows = []

            pp_row = db.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN profile_gain_pp > 0
                         THEN profile_gain_pp ELSE 0 END
                ), 0.0)
                FROM autohost_pp_achievements
                """
            ).fetchone()

        total_seconds = 0.0
        leaderboard_rows: list[tuple[str, float]] = []
        seen_users: set[int] = set()

        for row in player_rows:
            uid = int(row[0])
            seen_users.add(uid)
            username = live_names_by_user.get(uid) or str(row[1] or uid)
            seconds = max(0.0, float(row[2] or 0.0))
            seconds += live_seconds_by_user.get(uid, 0.0)
            total_seconds += seconds

            hidden = bool(int(row[3] or 0))
            if not hidden:
                leaderboard_rows.append((username, seconds))

        # record_lobby_join normally creates the persistent row immediately,
        # but include a live player defensively if that tiny DB write failed or
        # is momentarily unavailable. This keeps the live .lobby total honest
        # without double-counting users already present in the table.
        for uid, seconds in live_seconds_by_user.items():
            if uid in seen_users:
                continue
            seen_users.add(uid)
            seconds = max(0.0, float(seconds or 0.0))
            total_seconds += seconds
            username = live_names_by_user.get(uid) or str(uid)
            leaderboard_rows.append((username, seconds))

        unique_players = len(seen_users)

        leaderboard_rows.sort(
            key=lambda item: (-float(item[1]), item[0].casefold())
        )

        return {
            "total_lobby_seconds": total_seconds,
            "unique_players": unique_players,
            "map_plays": map_plays,
            "total_pp_gain": float(pp_row[0] or 0.0),
            "top_playtime": leaderboard_rows[:limit],
        }

    def record_lobby_join(
        self,
        user_id: int,
        username: str | None,
        joined_at: float | None = None,
    ) -> None:
        """Record that a player entered an AutoHost lobby.

        Actual elapsed time is added when the player leaves (or when the bot
        cleanly shuts down / hard-restarts the room). Keeping join-count and
        elapsed-time writes separate prevents duplicate UserJoined events from
        inflating playtime.
        """
        now = float(joined_at if joined_at is not None else time.time())
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO autohost_lobby_time (
                    user_id, username, total_seconds, visit_count,
                    first_join_at, last_join_at, last_leave_at, updated_at
                ) VALUES (?, ?, 0.0, 1, ?, ?, NULL, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    visit_count = autohost_lobby_time.visit_count + 1,
                    first_join_at = COALESCE(
                        autohost_lobby_time.first_join_at,
                        excluded.first_join_at
                    ),
                    last_join_at = excluded.last_join_at,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), str(username or user_id), now, now, now),
            )

    def add_lobby_time(
        self,
        user_id: int,
        username: str | None,
        seconds: float,
        left_at: float | None = None,
    ) -> None:
        """Add one completed slice of lobby-presence time for a player."""
        seconds = max(0.0, float(seconds or 0.0))
        if seconds <= 0.0:
            return
        now = float(left_at if left_at is not None else time.time())
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO autohost_lobby_time (
                    user_id, username, total_seconds, visit_count,
                    first_join_at, last_join_at, last_leave_at, updated_at
                ) VALUES (?, ?, ?, 0, NULL, NULL, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    total_seconds = autohost_lobby_time.total_seconds
                                    + excluded.total_seconds,
                    last_leave_at = excluded.last_leave_at,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), str(username or user_id), seconds, now, now),
            )
