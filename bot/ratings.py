"""Per-map feedback and silent performance evidence collected from the lobby.

This database is intentionally separate from farm.db. Human map feedback
(.good/.bad plus stored-neutral responses) and observed "this map just produced
unusually strong top plays" evidence are expensive live signals that should
survive rebuilding the farm-finder data.

Human votes stay in ``map_ratings``. Successful public vote-skips are kept
separately in ``map_skips`` so a skipped map is never silently converted into
a human ``.bad`` rating.

Automatically detected performance evidence goes into two separate tables:
``performance_evidence`` is one aggregate row per notable match, while
``performance_records`` stores the individual top-play results that caused the
match to qualify. Automatic evidence never pretends to be a human .good vote.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterable

log = logging.getLogger("ratings")

SCHEMA = """
CREATE TABLE IF NOT EXISTS map_ratings (
    beatmap_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    rating     INTEGER NOT NULL,   -- +1 good, 0 neutral, -1 bad
    created_at REAL,
    map_origin TEXT NOT NULL DEFAULT 'unknown',
    key_count  INTEGER,
    PRIMARY KEY (beatmap_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_ratings_map ON map_ratings(beatmap_id);
CREATE INDEX IF NOT EXISTS ix_ratings_user ON map_ratings(user_id);

-- Trust/moderation policy for human map feedback. Once a user has ever been
-- filtered, this row remains so old ratings stay retired even after feedback is
-- allowed again. While is_ignored=1 none of their real ratings count. When
-- is_ignored=0, only map_ratings created on/after accept_after count.
CREATE TABLE IF NOT EXISTS rating_ignored_users (
    user_id             INTEGER PRIMARY KEY,
    username            TEXT,
    ignored_at          REAL NOT NULL,
    ignored_by_user_id  INTEGER,
    ignored_by_username TEXT,
    is_ignored          INTEGER NOT NULL DEFAULT 1,
    accept_after        REAL,
    allowed_at          REAL,
    allowed_by_user_id  INTEGER,
    allowed_by_username TEXT
);

-- Feedback submitted while a user is shadow-ignored. These rows exist only so
-- the bot can behave exactly like a normal accepted rating in chat (including
-- duplicate/change handling and a plausible tally). They NEVER participate in
-- real map tallies or selection weighting.
CREATE TABLE IF NOT EXISTS shadow_map_ratings (
    beatmap_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    rating     INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (beatmap_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_shadow_ratings_user
    ON shadow_map_ratings(user_id);

CREATE TABLE IF NOT EXISTS map_skips (
    skip_key          TEXT PRIMARY KEY,
    beatmap_id        INTEGER NOT NULL,
    room_id           INTEGER,
    playlist_item_id  INTEGER,
    player_count      INTEGER NOT NULL,
    votes_cast        INTEGER NOT NULL,
    votes_required    INTEGER NOT NULL,
    skip_type         TEXT NOT NULL DEFAULT 'vote',
    actor_user_id     INTEGER,
    actor_username    TEXT,
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_map_skips_map ON map_skips(beatmap_id);
CREATE INDEX IF NOT EXISTS ix_map_skips_created ON map_skips(created_at);

-- Explicit osu! download-availability results. Positive results avoid a
-- beatmap + beatmapset REST lookup on every restart for months at a time.
CREATE TABLE IF NOT EXISTS download_availability_cache (
    beatmap_id    INTEGER PRIMARY KEY,
    beatmapset_id INTEGER,
    available     INTEGER NOT NULL,
    checked_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_download_availability_set
    ON download_availability_cache(beatmapset_id);
CREATE INDEX IF NOT EXISTS ix_download_availability_checked
    ON download_availability_cache(checked_at);

-- One row per explicit unavailable-set observation. The availability cache
-- remains the quarantine itself; these rows provide durable per-room/pool
-- counters without retaining player data or API response text.
CREATE TABLE IF NOT EXISTS download_quarantine_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    beatmap_id     INTEGER NOT NULL,
    beatmapset_id  INTEGER,
    room_alias     TEXT NOT NULL,
    pool_source    TEXT NOT NULL,
    excluded_count INTEGER NOT NULL,
    reason         TEXT NOT NULL,
    observed_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_download_quarantine_observed
    ON download_quarantine_events(observed_at);
CREATE INDEX IF NOT EXISTS ix_download_quarantine_pool
    ON download_quarantine_events(room_alias, pool_source);

CREATE TABLE IF NOT EXISTS performance_evidence (
    evidence_key      TEXT PRIMARY KEY,
    beatmap_id        INTEGER NOT NULL,
    room_id           INTEGER,
    playlist_item_id  INTEGER,
    created_at        REAL NOT NULL,

    participant_count INTEGER NOT NULL,
    scored_count      INTEGER NOT NULL,
    top100_count      INTEGER NOT NULL,
    top30_count       INTEGER NOT NULL,
    top20_count       INTEGER NOT NULL,
    top10_count       INTEGER NOT NULL,
    top1_count        INTEGER NOT NULL,
    best_rank         INTEGER,

    notable_ratio     REAL NOT NULL,
    evidence_score    REAL NOT NULL,
    trigger_reason    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_perf_evidence_map
    ON performance_evidence(beatmap_id);
CREATE INDEX IF NOT EXISTS ix_perf_evidence_created
    ON performance_evidence(created_at);

CREATE TABLE IF NOT EXISTS performance_records (
    evidence_key      TEXT NOT NULL,
    beatmap_id        INTEGER NOT NULL,
    room_id           INTEGER,
    playlist_item_id  INTEGER,
    user_id           INTEGER NOT NULL,
    top_rank          INTEGER NOT NULL,
    score_pp          REAL,
    profile_gain_pp   REAL,
    accuracy          REAL,
    created_at        REAL NOT NULL,
    PRIMARY KEY (evidence_key, user_id),
    FOREIGN KEY (evidence_key)
        REFERENCES performance_evidence(evidence_key)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_perf_records_map
    ON performance_records(beatmap_id);
CREATE INDEX IF NOT EXISTS ix_perf_records_rank
    ON performance_records(top_rank);

-- Durable AutoHost operating-history ledger. These tables are intentionally
-- raw/event-oriented so future reports can derive daily/monthly totals without
-- depending on old text logs.
CREATE TABLE IF NOT EXISTS autohost_rooms (
    room_id          INTEGER PRIMARY KEY,
    room_url         TEXT NOT NULL,
    initial_name     TEXT,
    last_name        TEXT,
    created_at       REAL NOT NULL,
    closed_at        REAL,
    close_reason     TEXT,
    last_activity_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_autohost_rooms_created
    ON autohost_rooms(created_at);

CREATE TABLE IF NOT EXISTS autohost_room_players (
    room_id          INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    username         TEXT,
    first_join_at    REAL NOT NULL,
    last_join_at     REAL NOT NULL,
    last_leave_at    REAL,
    join_count       INTEGER NOT NULL DEFAULT 1,
    total_seconds    REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (room_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_autohost_room_players_user
    ON autohost_room_players(user_id);

CREATE TABLE IF NOT EXISTS autohost_interactions (
    room_id              INTEGER NOT NULL,
    user_id              INTEGER NOT NULL,
    username             TEXT,
    chat_messages        INTEGER NOT NULL DEFAULT 0,
    commands_used        INTEGER NOT NULL DEFAULT 0,
    first_interaction_at REAL NOT NULL,
    last_interaction_at  REAL NOT NULL,
    PRIMARY KEY (room_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_autohost_interactions_user
    ON autohost_interactions(user_id);

CREATE TABLE IF NOT EXISTS autohost_matches (
    match_key         TEXT PRIMARY KEY,
    room_id           INTEGER NOT NULL,
    playlist_item_id  INTEGER,
    beatmap_id        INTEGER NOT NULL,
    map_label         TEXT,
    star_rating       REAL,
    started_at        REAL NOT NULL,
    ended_at          REAL,
    result            TEXT NOT NULL DEFAULT 'playing',
    participant_count INTEGER NOT NULL DEFAULT 0,
    scored_count      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_autohost_matches_room
    ON autohost_matches(room_id);
CREATE INDEX IF NOT EXISTS ix_autohost_matches_map
    ON autohost_matches(beatmap_id);
CREATE INDEX IF NOT EXISTS ix_autohost_matches_started
    ON autohost_matches(started_at);

CREATE TABLE IF NOT EXISTS autohost_countdown_stops (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id           INTEGER NOT NULL,
    playlist_item_id  INTEGER,
    reason            TEXT NOT NULL,
    gameplay_players  INTEGER NOT NULL,
    ready_players     INTEGER NOT NULL,
    minimum_players   INTEGER NOT NULL,
    occurred_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_countdown_stops_room
    ON autohost_countdown_stops(room_id);
CREATE INDEX IF NOT EXISTS ix_countdown_stops_occurred
    ON autohost_countdown_stops(occurred_at);

CREATE TABLE IF NOT EXISTS autohost_match_players (
    match_key         TEXT NOT NULL,
    room_id           INTEGER NOT NULL,
    playlist_item_id  INTEGER,
    beatmap_id        INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    username          TEXT,
    score_submitted   INTEGER NOT NULL DEFAULT 0,
    score_pp          REAL,
    accuracy          REAL,
    created_at        REAL NOT NULL,
    PRIMARY KEY (match_key, user_id),
    FOREIGN KEY (match_key)
        REFERENCES autohost_matches(match_key)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_autohost_match_players_user
    ON autohost_match_players(user_id);
CREATE INDEX IF NOT EXISTS ix_autohost_match_players_map
    ON autohost_match_players(beatmap_id);
"""


from poolrotater_host.activity.ledger import SCHEMA as ACTIVITY_SCHEMA
SCHEMA += ACTIVITY_SCHEMA


class RatingStore:
    def __init__(self, path: str = "ratings.db", *, initialize: bool = True, reader=None):
        self.path = str(path)
        self.reader=reader
        db_path = Path(self.path)
        if not initialize:
            # Coordinated v7 migrations prepare this ledger before workers run.
            # Existing workers validate it, rather than opportunistically DDL.
            if not db_path.is_file():
                raise ValueError("prepared_rating_ledger_required")
            conn = self._conn()
            try:
                from .rating_outbox import validate
                validate(conn)
                conn.execute("SELECT beatmap_id,user_id,rating,created_at,map_origin,key_count FROM map_ratings LIMIT 0")
                conn.execute("SELECT user_id,is_ignored,accept_after FROM rating_ignored_users LIMIT 0")
                conn.execute("SELECT beatmap_id,user_id,rating,created_at FROM shadow_map_ratings LIMIT 0")
            finally:
                conn.close()
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        # WAL lets multiple room processes read while another room performs a
        # short rating/evidence write. SQLite still has one writer at a time,
        # but our writes are tiny and busy_timeout handles brief contention.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(SCHEMA)

        # Non-destructive upgrades for databases created by older versions.
        skip_cols = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(map_skips)").fetchall()
        }
        if "skip_type" not in skip_cols:
            conn.execute(
                "ALTER TABLE map_skips "
                "ADD COLUMN skip_type TEXT NOT NULL DEFAULT 'vote'"
            )
        if "actor_user_id" not in skip_cols:
            conn.execute(
                "ALTER TABLE map_skips ADD COLUMN actor_user_id INTEGER"
            )
        if "actor_username" not in skip_cols:
            conn.execute(
                "ALTER TABLE map_skips ADD COLUMN actor_username TEXT"
            )

        rating_cols = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(map_ratings)").fetchall()
        }
        if "map_origin" not in rating_cols:
            conn.execute(
                "ALTER TABLE map_ratings "
                "ADD COLUMN map_origin TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "key_count" not in rating_cols:
            conn.execute("ALTER TABLE map_ratings ADD COLUMN key_count INTEGER")

        rating_policy_cols = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(rating_ignored_users)"
            ).fetchall()
        }
        if "is_ignored" not in rating_policy_cols:
            conn.execute(
                "ALTER TABLE rating_ignored_users "
                "ADD COLUMN is_ignored INTEGER NOT NULL DEFAULT 1"
            )
        if "accept_after" not in rating_policy_cols:
            conn.execute(
                "ALTER TABLE rating_ignored_users ADD COLUMN accept_after REAL"
            )
        if "allowed_at" not in rating_policy_cols:
            conn.execute(
                "ALTER TABLE rating_ignored_users ADD COLUMN allowed_at REAL"
            )
        if "allowed_by_user_id" not in rating_policy_cols:
            conn.execute(
                "ALTER TABLE rating_ignored_users "
                "ADD COLUMN allowed_by_user_id INTEGER"
            )
        if "allowed_by_username" not in rating_policy_cols:
            conn.execute(
                "ALTER TABLE rating_ignored_users "
                "ADD COLUMN allowed_by_username TEXT"
            )

        conn.commit()
        conn.close()

    def _conn(self):
        # Retry only opening/configuring a connection, never application writes.
        # A root reader creating WAL/SHM can briefly leave them inaccessible
        # before SQLite fixes their ownership. Failed setup must also close its
        # connection so descriptors and locks cannot accumulate.
        for attempt in range(4):
            c = None
            try:
                c = sqlite3.connect(self.path, timeout=30)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA busy_timeout=30000")
                c.execute("PRAGMA foreign_keys=ON")
                c.execute("PRAGMA synchronous=FULL")
                return c
            except sqlite3.OperationalError as exc:
                if c is not None:
                    c.close()
                code = getattr(exc, "sqlite_errorcode", 0) & 0xff
                if code != sqlite3.SQLITE_CANTOPEN or attempt == 3:
                    log.error("database connection setup failed: path=%s code=%s name=%s",
                              self.path, getattr(exc, "sqlite_errorcode", None),
                              getattr(exc, "sqlite_errorname", None))
                    raise
                time.sleep(0.05 * (2 ** attempt))
            except BaseException:
                if c is not None:
                    c.close()
                raise

    # --------------------------------------------- download availability cache

    def cached_download_availability(
        self, beatmap_id: int, max_age_seconds: float
    ) -> tuple[bool, float] | None:
        """Return a fresh explicit availability result, if one exists."""
        age = max(0.0, float(max_age_seconds))
        if age <= 0:
            return None
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT available, checked_at FROM download_availability_cache "
                "WHERE beatmap_id=? AND checked_at>=?",
                (int(beatmap_id), time.time() - age),
            ).fetchone()
            if row is None:
                return None
            return bool(row["available"]), float(row["checked_at"])
        finally:
            conn.close()

    def remember_download_availability(
        self,
        beatmap_ids: Iterable[int],
        *,
        beatmapset_id: int | None,
        available: bool,
        checked_at: float | None = None,
    ) -> None:
        """Upsert explicit availability for every known difficulty in a set."""
        ids = sorted({int(value) for value in beatmap_ids})
        if not ids:
            return
        when = time.time() if checked_at is None else float(checked_at)
        set_id = int(beatmapset_id) if beatmapset_id is not None else None
        conn = self._conn()
        try:
            conn.executemany(
                """
                INSERT INTO download_availability_cache(
                    beatmap_id, beatmapset_id, available, checked_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(beatmap_id) DO UPDATE SET
                    beatmapset_id=excluded.beatmapset_id,
                    available=excluded.available,
                    checked_at=excluded.checked_at
                """,
                [(bid, set_id, int(bool(available)), when) for bid in ids],
            )
            conn.commit()
        finally:
            conn.close()

    def record_download_quarantine(
        self,
        *,
        beatmap_id: int,
        beatmapset_id: int | None,
        room_alias: str,
        pool_source: str,
        excluded_count: int,
        reason: str = "download_disabled",
        observed_at: float | None = None,
    ) -> None:
        """Record a sanitized per-pool quarantine observation."""
        now = time.time() if observed_at is None else float(observed_at)
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO download_quarantine_events (
                    beatmap_id, beatmapset_id, room_alias, pool_source,
                    excluded_count, reason, observed_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    int(beatmap_id),
                    int(beatmapset_id) if beatmapset_id is not None else None,
                    str(room_alias or "unknown")[:32],
                    str(pool_source or "unknown")[:64],
                    max(1, int(excluded_count or 1)),
                    str(reason or "download_disabled")[:64],
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------- human ratings

    def rate(
        self,
        beatmap_id: int,
        user_id: int,
        rating: int,
        *,
        map_origin: str = "unknown",
        key_count: int | None = None,
        context: dict | None = None,
    ) -> bool:
        """Record a real human vote.

        Returns True if this is a change worth acknowledging in chat. Normally
        one identical vote per person/map is a no-op. The exception is a user
        who was previously filtered and then re-allowed: an old pre-cutoff row
        must be refreshed when they submit the same rating again, because that
        submission is genuinely NEW feedback and is allowed to count.
        """
        bid, uid, val = int(beatmap_id), int(user_id), int(rating)
        origin = str(map_origin or "unknown")[:32]
        keys = int(key_count) if key_count in (4, 7) else None
        conn = self._conn()
        try:
            # Serialize the read/modify/write decision across room processes.
            # Outbox capture shares this transaction, never another WAL file.
            conn.execute("BEGIN IMMEDIATE")
            prev = conn.execute(
                "SELECT rating, created_at FROM map_ratings WHERE beatmap_id=? AND user_id=?",
                (bid, uid),
            ).fetchone()
            policy = conn.execute(
                "SELECT is_ignored, accept_after FROM rating_ignored_users WHERE user_id=?",
                (uid,),
            ).fetchone()
            if policy is not None and int(policy["is_ignored"] or 0):
                # A moderator can ignore someone between the command's earlier
                # policy read and this transaction. Never export that input.
                conn.execute(
                    "INSERT INTO shadow_map_ratings(beatmap_id,user_id,rating,created_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(beatmap_id,user_id) DO UPDATE SET rating=excluded.rating,created_at=excluded.created_at",
                    (bid, uid, val, time.time()),
                )
                conn.commit()
                return False
            same_rating = bool(prev and int(prev["rating"]) == val)
            old_pre_cutoff = False
            if same_rating and policy is not None:
                cutoff, created = policy["accept_after"], prev["created_at"]
                old_pre_cutoff = cutoff is not None and (created is None or float(created) < float(cutoff))
            from .rating_outbox import capture, repeat_is_fresh
            fresh_after_conflict = same_rating and repeat_is_fresh(conn, bid, uid)
            if same_rating and not old_pre_cutoff and not fresh_after_conflict:
                conn.execute("UPDATE map_ratings SET map_origin=?,key_count=? WHERE beatmap_id=? AND user_id=?",
                             (origin, keys, bid, uid))
                conn.commit()
                return False
            now = time.time()
            conn.execute(
                "INSERT INTO map_ratings(beatmap_id,user_id,rating,created_at,map_origin,key_count) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(beatmap_id,user_id) DO UPDATE SET rating=excluded.rating,created_at=excluded.created_at,"
                "map_origin=excluded.map_origin,key_count=excluded.key_count",
                (bid, uid, val, now, origin, keys),
            )
            capture(conn, beatmap_id=bid, user_id=uid, rating=val,
                    observed_at=now, map_origin=origin, key_count=keys, context=context)
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def is_rating_ignored(self, user_id: int) -> bool:
        """Return True while this user's feedback is shadow-ignored."""
        conn = self._conn()
        row = conn.execute(
            """
            SELECT is_ignored
            FROM rating_ignored_users
            WHERE user_id=?
            """,
            (int(user_id),),
        ).fetchone()
        conn.close()
        return bool(row and int(row["is_ignored"] or 0))

    def set_rating_ignored(
        self,
        *,
        user_id: int,
        username: str | None = None,
        ignored: bool,
        actor_user_id: int | None = None,
        actor_username: str | None = None,
    ) -> bool:
        """Shadow-ignore or re-allow a user's feedback.

        Important policy semantics:
        - entering ignored state retires ALL existing real ratings immediately;
        - ratings submitted while ignored go to shadow_map_ratings, never the
          real map_ratings table;
        - leaving ignored state keeps all older real/shadow ratings retired and
          sets accept_after=now, so only newly submitted ratings count.

        The policy row is therefore deliberately kept after re-allowing a user.
        Returns True only when the live ignored/allowed state actually changed.
        """
        uid = int(user_id)
        now = time.time()
        conn = self._conn()
        existing = conn.execute(
            """
            SELECT user_id, is_ignored, accept_after
            FROM rating_ignored_users
            WHERE user_id=?
            """,
            (uid,),
        ).fetchone()

        clean_name = str(username) if username else None
        actor_uid = int(actor_user_id) if actor_user_id is not None else None
        actor_name = str(actor_username) if actor_username else None

        if ignored:
            if existing is not None and int(existing["is_ignored"] or 0) == 1:
                if clean_name:
                    conn.execute(
                        "UPDATE rating_ignored_users SET username=? WHERE user_id=?",
                        (clean_name, uid),
                    )
                    conn.commit()
                conn.close()
                return False

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO rating_ignored_users (
                        user_id, username, ignored_at,
                        ignored_by_user_id, ignored_by_username,
                        is_ignored, accept_after,
                        allowed_at, allowed_by_user_id, allowed_by_username
                    ) VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, NULL, NULL)
                    """,
                    (uid, clean_name, now, actor_uid, actor_name),
                )
            else:
                conn.execute(
                    """
                    UPDATE rating_ignored_users
                    SET username=COALESCE(?, username),
                        ignored_at=?,
                        ignored_by_user_id=?,
                        ignored_by_username=?,
                        is_ignored=1
                    WHERE user_id=?
                    """,
                    (clean_name, now, actor_uid, actor_name, uid),
                )

            conn.commit()
            conn.close()
            return True

        # "Allow" is meaningful only after a user has actually been filtered.
        if existing is None:
            conn.close()
            return False
        if int(existing["is_ignored"] or 0) == 0:
            if clean_name:
                conn.execute(
                    "UPDATE rating_ignored_users SET username=? WHERE user_id=?",
                    (clean_name, uid),
                )
                conn.commit()
            conn.close()
            return False

        # Keep the policy row forever. accept_after is the permanent cutoff:
        # old real ratings remain excluded even though future ratings are valid.
        conn.execute(
            """
            UPDATE rating_ignored_users
            SET username=COALESCE(?, username),
                is_ignored=0,
                accept_after=?,
                allowed_at=?,
                allowed_by_user_id=?,
                allowed_by_username=?
            WHERE user_id=?
            """,
            (clean_name, now, now, actor_uid, actor_name, uid),
        )
        conn.commit()
        conn.close()
        return True

    def rating_ignored_users(self) -> list[dict]:
        """Return only users currently in shadow-ignore state."""
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT user_id, username, ignored_at,
                   ignored_by_user_id, ignored_by_username,
                   accept_after, allowed_at,
                   allowed_by_user_id, allowed_by_username
            FROM rating_ignored_users
            WHERE is_ignored=1
            ORDER BY LOWER(COALESCE(username, '')), user_id
            """
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def rating_policy_for_user(self, user_id: int) -> dict | None:
        """Return the durable rating policy row, if this user has one."""
        conn = self._conn()
        row = conn.execute(
            """
            SELECT user_id, username, ignored_at,
                   ignored_by_user_id, ignored_by_username,
                   is_ignored, accept_after, allowed_at,
                   allowed_by_user_id, allowed_by_username
            FROM rating_ignored_users
            WHERE user_id=?
            """,
            (int(user_id),),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def rating_count_for_user(self, user_id: int) -> int:
        """Return the number of durable real map-rating rows for this user."""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM map_ratings WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        conn.close()
        return int(row["n"] or 0)

    def shadow_rating_count_for_user(self, user_id: int) -> int:
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM shadow_map_ratings WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        conn.close()
        return int(row["n"] or 0)

    def shadow_rate(self, beatmap_id: int, user_id: int, rating: int) -> bool:
        """Record a fake/visual-only rating for a currently ignored user.

        This mirrors rate()'s one-vote-per-user-per-map behavior but never
        touches map_ratings, so it can never influence selection weighting.
        A shadow row from an older ignore episode is treated as historical and
        refreshed on the first new submission after the user is ignored again.
        """
        bid, uid, val = int(beatmap_id), int(user_id), int(rating)
        conn = self._conn()
        prev = conn.execute(
            """
            SELECT rating, created_at
            FROM shadow_map_ratings
            WHERE beatmap_id=? AND user_id=?
            """,
            (bid, uid),
        ).fetchone()
        policy = conn.execute(
            """
            SELECT is_ignored, ignored_at
            FROM rating_ignored_users
            WHERE user_id=?
            """,
            (uid,),
        ).fetchone()

        same_rating = bool(prev and int(prev["rating"]) == val)
        old_ignore_episode = False
        if same_rating and policy is not None and int(policy["is_ignored"] or 0):
            ignored_at = policy["ignored_at"]
            created = prev["created_at"]
            old_ignore_episode = (
                ignored_at is not None
                and (created is None or float(created) < float(ignored_at))
            )

        if same_rating and not old_ignore_episode:
            conn.close()
            return False

        conn.execute(
            """
            INSERT INTO shadow_map_ratings (beatmap_id, user_id, rating, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(beatmap_id, user_id) DO UPDATE SET
                rating=excluded.rating,
                created_at=excluded.created_at
            """,
            (bid, uid, val, time.time()),
        )
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def _active_rating_policy_sql(alias: str = "mr", policy_alias: str = "rp") -> str:
        """SQL predicate selecting only feedback that is allowed to count."""
        return (
            f"({policy_alias}.user_id IS NULL OR ("
            f"COALESCE({policy_alias}.is_ignored, 1)=0 "
            f"AND {policy_alias}.accept_after IS NOT NULL "
            f"AND {alias}.created_at IS NOT NULL "
            f"AND {alias}.created_at >= {policy_alias}.accept_after))"
        )

    def tally(self, beatmap_id: int) -> tuple[int, int]:
        if self.reader is not None:
            return self.reader.tallies([int(beatmap_id)]).get(int(beatmap_id),(0,0))
        conn = self._conn()
        predicate = self._active_rating_policy_sql("mr", "rp")
        r = conn.execute(
            f"""
            SELECT SUM(mr.rating>0) up, SUM(mr.rating<0) down
            FROM map_ratings mr
            LEFT JOIN rating_ignored_users rp ON rp.user_id = mr.user_id
            WHERE mr.beatmap_id=? AND {predicate}
            """,
            (int(beatmap_id),),
        ).fetchone()
        conn.close()
        return int(r["up"] or 0), int(r["down"] or 0)

    def rater_ids_for_maps(self, beatmap_ids: Iterable[int]) -> set[int]:
        """Return real good/bad raters for any supplied completed map.

        Neutral rows are deliberately ignored: they are stored feedback, but
        do not suppress the periodic chat reminder. Rating-ignore policy is
        applied exactly as it is to public tallies and selection weighting.
        """
        ids = list(dict.fromkeys(int(beatmap_id) for beatmap_id in beatmap_ids))
        if not ids:
            return set()

        placeholders = ", ".join("?" for _ in ids)
        predicate = self._active_rating_policy_sql("mr", "rp")
        conn = self._conn()
        rows = conn.execute(
            f"""
            SELECT DISTINCT mr.user_id
            FROM map_ratings mr
            LEFT JOIN rating_ignored_users rp ON rp.user_id = mr.user_id
            WHERE mr.beatmap_id IN ({placeholders})
              AND mr.rating != 0
              AND {predicate}
            """,
            ids,
        ).fetchall()
        conn.close()
        return {int(row["user_id"]) for row in rows}

    def shadow_visible_tally(
        self, beatmap_id: int, user_id: int
    ) -> tuple[int, int]:
        """Return a plausible public tally for one shadow-ignored user's ACK.

        Real accepted votes form the base. The requesting user's shadow vote is
        then overlaid as one apparent vote. Other shadow users are intentionally
        not included so fake votes can never leak into any shared backend tally.
        """
        up, down = self.tally(int(beatmap_id))
        if self.reader is not None:
            # The canonical aggregate cannot identify a player's own opinion;
            # an extra apparent local/shadow vote would risk double counting.
            return up, down
        conn = self._conn()
        row = conn.execute(
            """
            SELECT rating
            FROM shadow_map_ratings
            WHERE beatmap_id=? AND user_id=?
            """,
            (int(beatmap_id), int(user_id)),
        ).fetchone()
        conn.close()
        if row:
            if int(row["rating"]) > 0:
                up += 1
            elif int(row["rating"]) < 0:
                down += 1
        return up, down

    def purge_ratings_for_user(self, user_id: int) -> dict[str, int]:
        """Permanently delete all real and shadow map ratings for one user.

        The rating-policy row is deliberately preserved. If the user is
        currently ignored they stay ignored; if they were re-allowed, their
        accept_after cutoff remains in force for future ratings.
        """
        uid = int(user_id)
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            real = conn.execute(
                "SELECT COUNT(*) AS n FROM map_ratings WHERE user_id=?",
                (uid,),
            ).fetchone()
            shadow = conn.execute(
                "SELECT COUNT(*) AS n FROM shadow_map_ratings WHERE user_id=?",
                (uid,),
            ).fetchone()
            conn.execute("DELETE FROM map_ratings WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM shadow_map_ratings WHERE user_id=?", (uid,))
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()
        return {
            "real": int(real["n"] or 0),
            "shadow": int(shadow["n"] or 0),
        }

    def record_skip(
        self,
        *,
        beatmap_id: int,
        room_id: int | None,
        playlist_item_id: int | None,
        player_count: int,
        votes_cast: int,
        votes_required: int,
        skip_type: str = "vote",
        actor_user_id: int | None = None,
        actor_username: str | None = None,
    ) -> bool:
        """Persist one successful vote-skip or privileged force-skip.

        Skips are deliberately separate from human good/bad ratings. The
        room+playlist item key makes the write idempotent if a live event is
        ever processed twice. Returns True when a row was newly inserted.
        """
        beatmap_id = int(beatmap_id)
        room_part = "none" if room_id is None else str(int(room_id))
        item_part = (
            "none" if playlist_item_id is None
            else str(int(playlist_item_id))
        )
        skip_type = str(skip_type or "vote").strip().casefold()
        if skip_type not in ("vote", "force"):
            skip_type = "vote"
        skip_key = f"{skip_type}:{room_part}:{item_part}:{beatmap_id}"

        conn = self._conn()
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO map_skips (
                skip_key, beatmap_id, room_id, playlist_item_id,
                player_count, votes_cast, votes_required,
                skip_type, actor_user_id, actor_username, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                skip_key,
                beatmap_id,
                int(room_id) if room_id is not None else None,
                int(playlist_item_id) if playlist_item_id is not None else None,
                max(0, int(player_count or 0)),
                max(0, int(votes_cast or 0)),
                max(0, int(votes_required or 0)),
                skip_type,
                (
                    int(actor_user_id)
                    if actor_user_id is not None else None
                ),
                (
                    str(actor_username).strip()
                    if actor_username else None
                ),
                time.time(),
            ),
        )
        inserted = cur.rowcount > 0
        conn.commit()
        conn.close()
        if inserted:
            log.debug(
                "saved map skip type=%s map=%s room=%s item=%s "
                "votes=%s/%s players=%s actor=%s",
                skip_type, beatmap_id, room_id, playlist_item_id,
                votes_cast, votes_required, player_count, actor_user_id,
            )
        return inserted

    # ----------------------------------------------------- activity history

    @staticmethod
    def _public_room_url(room_id: int) -> str:
        return f"https://osu.ppy.sh/multiplayer/rooms/{int(room_id)}"

    @staticmethod
    def _match_key(
        room_id: int,
        playlist_item_id: int | None,
        beatmap_id: int,
        started_at: float,
    ) -> str:
        if playlist_item_id is not None:
            return f"room:{int(room_id)}:item:{int(playlist_item_id)}"
        return (
            f"room:{int(room_id)}:map:{int(beatmap_id)}:"
            f"start:{int(started_at * 1000)}"
        )

    def record_room_visibility(self, room_id: int, *, public: bool,
                               observed_at: float | None = None,
                               mode: str = "standard", preset: str | None = None,
                               room_alias: str | None = None) -> None:
        """Record authoritative provider/broker visibility, never inferred from a title."""
        if type(public) is not bool:
            raise ValueError("explicit_room_visibility_required")
        if preset:
            mode = {"mania-4k": "mania4", "mania-7k": "mania7"}.get(preset, "standard")
        if mode not in ("standard", "mania4", "mania7"):
            raise ValueError("invalid_activity_mode")
        now = float(time.time() if observed_at is None else observed_at)
        conn = self._conn()
        try:
            previous = conn.execute("SELECT public,mode,room_alias,observed_at FROM activity_visibility WHERE room_id=? ORDER BY observed_at DESC,id DESC LIMIT 1", (int(room_id),)).fetchone()
            if previous and now < previous["observed_at"]:
                raise ValueError("activity_visibility_clock_rollback")
            if not previous or (previous["public"], previous["mode"], previous["room_alias"]) != (int(public), mode, room_alias):
                conn.execute("INSERT INTO activity_visibility(room_id,public,observed_at,mode,room_alias) VALUES(?,?,?,?,?)", (int(room_id),int(public),now,mode,room_alias))
                conn.commit()
        finally:
            conn.close()

    def record_room_player_leave(self, *, room_id: int, user_id: int,
                                 username: str | None = None,
                                 left_at: float | None = None) -> None:
        now = float(time.time() if left_at is None else left_at)
        conn = self._conn()
        try:
            conn.execute("INSERT INTO activity_leaves(room_id,user_id,username,occurred_at) VALUES(?,?,?,?)",
                         (int(room_id),int(user_id),str(username or user_id),now))
            conn.commit()
        finally:
            conn.close()

    def record_room_open(
        self,
        *,
        room_id: int,
        room_name: str | None,
        created_at: float | None = None,
    ) -> str:
        """Create/update the durable ledger row for one osu! multiplayer room."""
        now = float(created_at if created_at is not None else time.time())
        room_id = int(room_id)
        url = self._public_room_url(room_id)
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO autohost_rooms (
                room_id, room_url, initial_name, last_name, created_at,
                closed_at, close_reason, last_activity_at
            ) VALUES (?,?,?,?,?,NULL,NULL,?)
            ON CONFLICT(room_id) DO UPDATE SET
                room_url = excluded.room_url,
                initial_name = COALESCE(autohost_rooms.initial_name, excluded.initial_name),
                last_name = COALESCE(excluded.last_name, autohost_rooms.last_name),
                created_at = MIN(autohost_rooms.created_at, excluded.created_at),
                last_activity_at = MAX(autohost_rooms.last_activity_at, excluded.last_activity_at)
            """,
            (room_id, url, room_name, room_name, now, now),
        )
        conn.commit()
        conn.close()
        log.info("activity ledger opened room %s (%s)", room_id, url)
        return url

    def update_room_name(
        self, room_id: int, room_name: str, changed_at: float | None = None
    ) -> None:
        now = float(changed_at if changed_at is not None else time.time())
        conn = self._conn()
        conn.execute(
            """
            UPDATE autohost_rooms
            SET last_name=?, last_activity_at=MAX(last_activity_at, ?)
            WHERE room_id=?
            """,
            (str(room_name), now, int(room_id)),
        )
        conn.commit()
        conn.close()

    def record_room_close(
        self,
        room_id: int,
        reason: str,
        closed_at: float | None = None,
    ) -> None:
        now = float(closed_at if closed_at is not None else time.time())
        conn = self._conn()
        conn.execute(
            """
            UPDATE autohost_rooms
            SET closed_at=COALESCE(closed_at, ?),
                close_reason=COALESCE(close_reason, ?),
                last_activity_at=MAX(last_activity_at, ?)
            WHERE room_id=?
            """,
            (now, str(reason), now, int(room_id)),
        )
        conn.commit()
        conn.close()

    def record_room_player_join(
        self,
        *,
        room_id: int,
        user_id: int,
        username: str | None,
        joined_at: float | None = None,
    ) -> None:
        now = float(joined_at if joined_at is not None else time.time())
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO autohost_room_players (
                room_id, user_id, username, first_join_at, last_join_at,
                last_leave_at, join_count, total_seconds
            ) VALUES (?,?,?,?,?,NULL,1,0.0)
            ON CONFLICT(room_id, user_id) DO UPDATE SET
                username=excluded.username,
                last_join_at=excluded.last_join_at,
                join_count=autohost_room_players.join_count + 1
            """,
            (int(room_id), int(user_id), str(username or user_id), now, now),
        )
        conn.execute("INSERT INTO activity_joins(room_id,user_id,username,occurred_at) VALUES(?,?,?,?)",
                     (int(room_id),int(user_id),str(username or user_id),now))
        conn.execute(
            "UPDATE autohost_rooms SET last_activity_at=MAX(last_activity_at, ?) "
            "WHERE room_id=?",
            (now, int(room_id)),
        )
        conn.commit()
        conn.close()

    def add_room_player_time(
        self,
        *,
        room_id: int,
        user_id: int,
        username: str | None,
        seconds: float,
        left_at: float | None = None,
    ) -> None:
        seconds = max(0.0, float(seconds or 0.0))
        if seconds <= 0.0:
            return
        now = float(left_at if left_at is not None else time.time())
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO autohost_room_players (
                room_id, user_id, username, first_join_at, last_join_at,
                last_leave_at, join_count, total_seconds
            ) VALUES (?,?,?,?,?,?,0,?)
            ON CONFLICT(room_id, user_id) DO UPDATE SET
                username=excluded.username,
                last_leave_at=excluded.last_leave_at,
                total_seconds=autohost_room_players.total_seconds + excluded.total_seconds
            """,
            (
                int(room_id), int(user_id), str(username or user_id),
                now, now, now, seconds,
            ),
        )
        conn.execute("INSERT INTO activity_presence(room_id,user_id,username,started_at,ended_at) VALUES(?,?,?,?,?)",
                     (int(room_id),int(user_id),str(username or user_id),max(0.0,now-seconds),now))
        conn.execute(
            "UPDATE autohost_rooms SET last_activity_at=MAX(last_activity_at, ?) "
            "WHERE room_id=?",
            (now, int(room_id)),
        )
        conn.commit()
        conn.close()

    def record_interaction(
        self,
        *,
        room_id: int,
        user_id: int,
        username: str | None,
        is_command: bool,
        created_at: float | None = None,
    ) -> None:
        """Count human chat/command interaction without duplicating chat text."""
        now = float(created_at if created_at is not None else time.time())
        command_inc = 1 if is_command else 0
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO autohost_interactions (
                room_id, user_id, username, chat_messages, commands_used,
                first_interaction_at, last_interaction_at
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(room_id, user_id) DO UPDATE SET
                username=excluded.username,
                chat_messages=autohost_interactions.chat_messages + 1,
                commands_used=autohost_interactions.commands_used + excluded.commands_used,
                last_interaction_at=excluded.last_interaction_at
            """,
            (
                int(room_id), int(user_id), str(username or user_id),
                1, command_inc, now, now,
            ),
        )
        conn.execute(
            "UPDATE autohost_rooms SET last_activity_at=MAX(last_activity_at, ?) "
            "WHERE room_id=?",
            (now, int(room_id)),
        )
        conn.commit()
        conn.close()

    def record_countdown_stop(
        self,
        *,
        room_id: int,
        playlist_item_id: int | None,
        reason: str,
        gameplay_players: int,
        ready_players: int,
        minimum_players: int,
        occurred_at: float | None = None,
    ) -> None:
        """Persist one classified CountdownStopped event for operations."""
        now = time.time() if occurred_at is None else float(occurred_at)
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO autohost_countdown_stops (
                    room_id, playlist_item_id, reason, gameplay_players,
                    ready_players, minimum_players, occurred_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    int(room_id),
                    int(playlist_item_id)
                    if playlist_item_id is not None else None,
                    str(reason or "unknown")[:64],
                    max(0, int(gameplay_players)),
                    max(0, int(ready_players)),
                    max(0, int(minimum_players)),
                    now,
                ),
            )
            conn.execute(
                "UPDATE autohost_rooms "
                "SET last_activity_at=MAX(last_activity_at, ?) WHERE room_id=?",
                (now, int(room_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def record_match_start(
        self,
        *,
        room_id: int,
        playlist_item_id: int | None,
        beatmap_id: int,
        map_label: str | None,
        star_rating: float | None,
        participants: Iterable[dict],
        started_at: float | None = None,
    ) -> str:
        """Persist one server-confirmed MatchStarted plus its player snapshot."""
        now = float(started_at if started_at is not None else time.time())
        room_id = int(room_id)
        beatmap_id = int(beatmap_id)
        item_id = (
            int(playlist_item_id) if playlist_item_id is not None else None
        )
        players = []
        seen: set[int] = set()
        for raw in participants:
            uid = raw.get("user_id")
            if uid is None:
                continue
            uid = int(uid)
            if uid in seen:
                continue
            seen.add(uid)
            players.append((uid, str(raw.get("username") or uid)))

        key = self._match_key(room_id, item_id, beatmap_id, now)
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO autohost_matches (
                match_key, room_id, playlist_item_id, beatmap_id, map_label,
                star_rating, started_at, ended_at, result,
                participant_count, scored_count
            ) VALUES (?,?,?,?,?,?,?,NULL,'playing',?,0)
            ON CONFLICT(match_key) DO UPDATE SET
                map_label=COALESCE(excluded.map_label, autohost_matches.map_label),
                star_rating=COALESCE(excluded.star_rating, autohost_matches.star_rating),
                participant_count=MAX(autohost_matches.participant_count, excluded.participant_count)
            """,
            (
                key, room_id, item_id, beatmap_id, map_label,
                float(star_rating) if star_rating is not None else None,
                now, len(players),
            ),
        )
        for uid, username in players:
            conn.execute(
                """
                INSERT OR IGNORE INTO autohost_match_players (
                    match_key, room_id, playlist_item_id, beatmap_id, user_id,
                    username, score_submitted, score_pp, accuracy, created_at
                ) VALUES (?,?,?,?,?,?,0,NULL,NULL,?)
                """,
                (key, room_id, item_id, beatmap_id, uid, username, now),
            )
        conn.execute(
            "UPDATE autohost_rooms SET last_activity_at=MAX(last_activity_at, ?) "
            "WHERE room_id=?",
            (now, room_id),
        )
        conn.commit()
        conn.close()
        return key

    def record_match_end(
        self,
        *,
        match_key: str | None,
        room_id: int | None,
        playlist_item_id: int | None,
        result: str,
        ended_at: float | None = None,
    ) -> None:
        """Mark a started match completed/aborted/room_closed idempotently."""
        now = float(ended_at if ended_at is not None else time.time())
        conn = self._conn()
        if match_key:
            conn.execute(
                """
                UPDATE autohost_matches
                SET ended_at=COALESCE(ended_at, ?),
                    result=CASE WHEN result='playing' THEN ? ELSE result END
                WHERE match_key=?
                """,
                (now, str(result), str(match_key)),
            )
        elif room_id is not None and playlist_item_id is not None:
            conn.execute(
                """
                UPDATE autohost_matches
                SET ended_at=COALESCE(ended_at, ?),
                    result=CASE WHEN result='playing' THEN ? ELSE result END
                WHERE room_id=? AND playlist_item_id=?
                """,
                (now, str(result), int(room_id), int(playlist_item_id)),
            )
        if room_id is not None:
            conn.execute(
                "UPDATE autohost_rooms SET last_activity_at=MAX(last_activity_at, ?) "
                "WHERE room_id=?",
                (now, int(room_id)),
            )
        conn.commit()
        conn.close()

    def record_match_scores(
        self,
        *,
        match_key: str | None,
        scores: Iterable[dict],
    ) -> int:
        """Attach score submissions to a previously recorded match."""
        if not match_key:
            return 0
        conn = self._conn()
        match = conn.execute(
            "SELECT room_id, playlist_item_id, beatmap_id FROM autohost_matches "
            "WHERE match_key=?",
            (str(match_key),),
        ).fetchone()
        if not match:
            conn.close()
            return 0

        now = time.time()
        for raw in scores:
            uid = raw.get("user_id")
            if uid is None:
                continue
            uid = int(uid)
            username = str(raw.get("username") or uid)
            score_pp = raw.get("score_pp")
            accuracy = raw.get("accuracy")
            conn.execute(
                """
                INSERT INTO autohost_match_players (
                    match_key, room_id, playlist_item_id, beatmap_id, user_id,
                    username, score_submitted, score_pp, accuracy, created_at
                ) VALUES (?,?,?,?,?,?,1,?,?,?)
                ON CONFLICT(match_key, user_id) DO UPDATE SET
                    username=excluded.username,
                    score_submitted=1,
                    score_pp=excluded.score_pp,
                    accuracy=excluded.accuracy
                """,
                (
                    str(match_key), int(match["room_id"]),
                    match["playlist_item_id"], int(match["beatmap_id"]),
                    uid, username,
                    float(score_pp) if score_pp is not None else None,
                    float(accuracy) if accuracy is not None else None,
                    now,
                ),
            )
        row = conn.execute(
            "SELECT COUNT(*) FROM autohost_match_players "
            "WHERE match_key=? AND score_submitted=1",
            (str(match_key),),
        ).fetchone()
        scored = int(row[0] or 0)
        conn.execute(
            "UPDATE autohost_matches SET scored_count=? WHERE match_key=?",
            (scored, str(match_key)),
        )
        conn.commit()
        conn.close()
        return scored

    def worst(self, min_votes: int = 4, ratio: float = 0.6) -> list[int]:
        """Maps the lobby dislikes, for pruning from the pool later."""
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT beatmap_id, SUM(rating>0) up, SUM(rating<0) down,
                   COUNT(*) n
            FROM map_ratings
            GROUP BY beatmap_id
            HAVING n >= ?
            """,
            (min_votes,),
        ).fetchall()
        conn.close()
        return [
            r["beatmap_id"]
            for r in rows
            if (r["down"] or 0) / max(r["n"], 1) >= ratio
        ]

    # ------------------------------------------------ performance observations

    @staticmethod
    def _record_weight(rank: int) -> float:
        """Simple evidence weight retained in the DB for later analysis."""
        if rank <= 1:
            return 4.0
        if rank <= 10:
            return 3.0
        if rank <= 20:
            return 2.0
        if rank <= 30:
            return 1.0
        return 0.0

    def record_performance_evidence(
        self,
        *,
        beatmap_id: int,
        room_id: int | None,
        playlist_item_id: int | None,
        participant_count: int,
        scored_count: int,
        records: Iterable[dict],
        notable_ratio: float = 0.40,
        min_top20: int = 2,
        min_top30: int = 3,
    ) -> bool:
        """Silently persist a match when it looks like strong farm evidence.

        Default trigger rules intentionally match the lobby use-case:

        * any player gets a new #1;
        * at least two players get new top-20s;
        * at least three players get new top-30s; or
        * at least 40% of the match participants (minimum two people) get
          new top-30s.

        The aggregate match is stored once, plus the individual <= #30 records
        that contributed to the signal. Nothing is posted to lobby chat.

        Returns True if the match qualified and was written.
        """
        beatmap_id = int(beatmap_id)
        participant_count = max(int(participant_count or 0), 0)
        scored_count = max(int(scored_count or 0), 0)

        cleaned: list[dict] = []
        for rec in records:
            try:
                uid = int(rec["user_id"])
                rank = int(rec["rank"])
            except (KeyError, TypeError, ValueError):
                continue
            if rank < 1 or rank > 100:
                continue

            cleaned.append({
                "user_id": uid,
                "rank": rank,
                "pp": float(rec.get("pp") or 0.0),
                "gain": float(rec.get("gain") or 0.0),
                "accuracy": float(rec.get("accuracy") or 0.0),
            })

        if not cleaned:
            return False

        ranks = [r["rank"] for r in cleaned]
        top100 = len(ranks)
        top30 = sum(r <= 30 for r in ranks)
        top20 = sum(r <= 20 for r in ranks)
        top10 = sum(r <= 10 for r in ranks)
        top1 = sum(r <= 1 for r in ranks)
        best_rank = min(ranks)

        denom = max(participant_count, 1)
        ratio = top30 / denom
        evidence_score = sum(self._record_weight(r) for r in ranks)

        reasons: list[str] = []
        if top1 >= 1:
            reasons.append("top1")
        if top20 >= int(min_top20):
            reasons.append(f"{top20}_top20")
        if top30 >= int(min_top30):
            reasons.append(f"{top30}_top30")
        if top30 >= 2 and ratio >= float(notable_ratio):
            reasons.append(f"{ratio:.0%}_top30")

        if not reasons:
            return False

        now = time.time()
        room_part = "none" if room_id is None else str(int(room_id))
        item_part = "none" if playlist_item_id is None else str(int(playlist_item_id))
        evidence_key = f"{room_part}:{item_part}:{beatmap_id}"

        conn = self._conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO performance_evidence (
                evidence_key, beatmap_id, room_id, playlist_item_id, created_at,
                participant_count, scored_count,
                top100_count, top30_count, top20_count, top10_count, top1_count,
                best_rank, notable_ratio, evidence_score, trigger_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                evidence_key,
                beatmap_id,
                int(room_id) if room_id is not None else None,
                int(playlist_item_id) if playlist_item_id is not None else None,
                now,
                participant_count,
                scored_count,
                top100,
                top30,
                top20,
                top10,
                top1,
                best_rank,
                ratio,
                evidence_score,
                ",".join(reasons),
            ),
        )

        # Only <= #30 records are detailed; weaker top-100s still contribute to
        # top100_count above but are not individually stored as "notable".
        for rec in cleaned:
            if rec["rank"] > 30:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO performance_records (
                    evidence_key, beatmap_id, room_id, playlist_item_id,
                    user_id, top_rank, score_pp, profile_gain_pp,
                    accuracy, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    evidence_key,
                    beatmap_id,
                    int(room_id) if room_id is not None else None,
                    int(playlist_item_id) if playlist_item_id is not None else None,
                    rec["user_id"],
                    rec["rank"],
                    rec["pp"],
                    rec["gain"],
                    rec["accuracy"],
                    now,
                ),
            )

        conn.commit()
        conn.close()

        # Debug only: no lobby chat and normally no INFO console line.
        log.debug(
            "saved silent performance evidence map=%s participants=%s "
            "top30=%s best=#%s reasons=%s",
            beatmap_id, participant_count, top30, best_rank, ",".join(reasons),
        )
        return True
