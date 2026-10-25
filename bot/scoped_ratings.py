"""Explicit Catch feedback scope extending the unchanged legacy storage reader.

Legacy tables/readers stay unchanged. Optional Catch opinion tables and explicit
wire/readback conversions isolate its native NM domain. Standard/Mania keep the
legacy class directly.
"""
from __future__ import annotations
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable
from .ratings import RatingStore as LegacyRatingStore
log=logging.getLogger('ratings')


# Optional native Catch opinion tables; legacy tables/schema remain untouched.
CATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS map_ratings_catch (
    beatmap_id INTEGER NOT NULL,user_id INTEGER NOT NULL,rating INTEGER NOT NULL,
    created_at REAL,map_origin TEXT NOT NULL DEFAULT 'unknown',key_count INTEGER,
    PRIMARY KEY(beatmap_id,user_id));
CREATE INDEX IF NOT EXISTS ix_ratings_map_catch ON map_ratings_catch(beatmap_id);
CREATE INDEX IF NOT EXISTS ix_ratings_user_catch ON map_ratings_catch(user_id);
CREATE TABLE IF NOT EXISTS shadow_map_ratings_catch (
    beatmap_id INTEGER NOT NULL,user_id INTEGER NOT NULL,rating INTEGER NOT NULL,
    created_at REAL NOT NULL,PRIMARY KEY(beatmap_id,user_id));
"""
TAIKO_SCHEMA = CATCH_SCHEMA.replace('_catch', '_taiko')


class _CatchRatingConnection(sqlite3.Connection):
    @staticmethod
    def _sql(sql):
        for name in ('map_ratings','shadow_map_ratings'):
            sql=re.sub(r'\b'+name+r'\b',name+'_catch',sql)
        return sql

    def execute(self,sql,parameters=()):
        return super().execute(self._sql(sql),parameters)

    def executemany(self,sql,parameters):
        return super().executemany(self._sql(sql),parameters)


class _TaikoRatingConnection(_CatchRatingConnection):
    @staticmethod
    def _sql(sql):
        for name in ('map_ratings','shadow_map_ratings'):
            sql=re.sub(r'\b'+name+r'\b',name+'_taiko',sql)
        return sql


class ScopedRatingStore(LegacyRatingStore):
    def __init__(self,path='ratings.db',*,initialize=True,reader=None,ruleset_id=None):
        self.ruleset_id=ruleset_id
        self.reader=reader
        self._validate_reader_scope()
        if initialize and ruleset_id in (1, 2):
            # Fresh/stopped preparation only. Room workers pass initialize=False.
            LegacyRatingStore(path,initialize=True,reader=None)
            with sqlite3.connect(str(path),timeout=30) as db:
                db.executescript(CATCH_SCHEMA if ruleset_id == 2 else TAIKO_SCHEMA)
        super().__init__(path,initialize=initialize,reader=reader)

    def _validate_reader_scope(self):
        if self.reader is not None and (self.ruleset_id != getattr(self.reader, 'ruleset_id', None)):
            raise ValueError('rating_reader_scope_mismatch')


    def _rating_key(self, beatmap_id):
        from .rating_outbox import map_key
        return map_key(int(beatmap_id), self.ruleset_id)


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
        from .rating_outbox import storage_key
        origin = str(map_origin or "unknown")[:32]
        if self.ruleset_id in (1, 2):
            if context is None or context.get('ruleset_id') != self.ruleset_id:
                raise ValueError('rating_ruleset_identity_required')
        elif context is not None and context.get('ruleset_id') in (1, 2):
            raise ValueError('rating_scoped_store_required')
        public_bid = bid
        bid = storage_key(bid, context, map_origin=origin, key_count=key_count)
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
            capture(conn, beatmap_id=public_bid, user_id=uid, rating=val,
                    observed_at=now, map_origin=origin, key_count=keys, context=context)
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


    def shadow_rate(self, beatmap_id: int, user_id: int, rating: int) -> bool:
        """Record a fake/visual-only rating for a currently ignored user.

        This mirrors rate()'s one-vote-per-user-per-map behavior but never
        touches map_ratings, so it can never influence selection weighting.
        A shadow row from an older ignore episode is treated as historical and
        refreshed on the first new submission after the user is ignored again.
        """
        bid, uid, val = self._rating_key(beatmap_id), int(user_id), int(rating)
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


    def tally(self, beatmap_id: int) -> tuple[int, int]:
        self._validate_reader_scope()
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
            (self._rating_key(beatmap_id),),
        ).fetchone()
        conn.close()
        return int(r["up"] or 0), int(r["down"] or 0)


    def rater_ids_for_maps(self, beatmap_ids: Iterable[int]) -> set[int]:
        """Return real good/bad raters for any supplied completed map.

        Neutral rows are deliberately ignored: they are stored feedback, but
        do not suppress the periodic chat reminder. Rating-ignore policy is
        applied exactly as it is to public tallies and selection weighting.
        """
        ids = list(dict.fromkeys(self._rating_key(beatmap_id) for beatmap_id in beatmap_ids))
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
            (self._rating_key(beatmap_id), int(user_id)),
        ).fetchone()
        conn.close()
        if row:
            if int(row["rating"]) > 0:
                up += 1
            elif int(row["rating"]) < 0:
                down += 1
        return up, down


    def record_room_visibility(self, room_id: int, *, public: bool,
                               observed_at: float | None = None,
                               mode: str = "standard", preset: str | None = None,
                               room_alias: str | None = None) -> None:
        """Record authoritative provider/broker visibility, never inferred from a title."""
        if type(public) is not bool:
            raise ValueError("explicit_room_visibility_required")
        if preset:
            mode = {"mania-4k": "mania4", "mania-7k": "mania7", "catch-fixed": "catch",
                    "taiko-fixed": "taiko", "taiko-auto": "taiko"}.get(preset, "standard")
        if mode not in ("standard", "mania4", "mania7", "catch", "taiko"):
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


    def worst(self, min_votes: int = 4, ratio: float = 0.6) -> list[int]:
        """Maps the lobby dislikes, for pruning from the pool later."""
        conn = self._conn()
        from .rating_outbox import CATCH_KEY_BASE, TAIKO_KEY_BASE, MAX_PROVIDER_MAP_ID
        base = CATCH_KEY_BASE if self.ruleset_id == 2 else TAIKO_KEY_BASE if self.ruleset_id == 1 else None
        low,high = ((base+1,base+MAX_PROVIDER_MAP_ID) if base is not None
                    else (1,CATCH_KEY_BASE-1))
        rows = conn.execute(
            """
            SELECT beatmap_id, SUM(rating>0) up, SUM(rating<0) down,
                   COUNT(*) n
            FROM map_ratings
            WHERE beatmap_id BETWEEN ? AND ?
            GROUP BY beatmap_id
            HAVING n >= ?
            """,
            (low,high,min_votes),
        ).fetchall()
        conn.close()
        return [
            r["beatmap_id"]-base if base is not None else r["beatmap_id"]
            for r in rows
            if (r["down"] or 0) / max(r["n"], 1) >= ratio
        ]



    def _conn(self):
        # Retry only opening/configuring a connection, never application writes.
        # A root reader creating WAL/SHM can briefly leave them inaccessible
        # before SQLite fixes their ownership. Failed setup must also close its
        # connection so descriptors and locks cannot accumulate.
        for attempt in range(4):
            c = None
            try:
                c = sqlite3.connect(self.path, timeout=30,
                                    factory=(_CatchRatingConnection if self.ruleset_id==2 else
                                             _TaikoRatingConnection if self.ruleset_id==1 else sqlite3.Connection))
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

