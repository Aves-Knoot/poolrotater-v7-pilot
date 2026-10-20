"""Read-only, compact Discord reports from PoolRotater's activity database."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class DiscordReports:
    """Staff-only reports. Privacy preferences do not limit these reports."""

    def __init__(self, path: str, ruleset_id: int | None = None):
        self.path = str(path)
        if ruleset_id not in (None, 0, 2, 3):
            raise ValueError("Unsupported report ruleset")
        self.ruleset_id = ruleset_id

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(Path(self.path).resolve().as_uri() + "?mode=ro", uri=True)
        if self.ruleset_id is None:
            return conn
        # Temporary views scope all existing report SQL without altering the
        # production schema or any stored rows. Legacy unlabelled rooms are osu.
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if self.ruleset_id == 2:
            # Old/unlabelled ledgers are Standard evidence, never Catch data.
            # Cold Catch reports must be empty instead of adopting old tables.
            from .rating_outbox import CATCH_KEY_BASE, MAX_PROVIDER_MAP_ID
            for table in ('autohost_matches','autohost_room_players'):
                if table in tables:
                    if 'autohost_room_rulesets' in tables:
                        conn.execute(f'CREATE TEMP VIEW {table} AS SELECT t.* FROM main.{table} t JOIN main.autohost_room_rulesets rr ON rr.room_id=t.room_id WHERE rr.ruleset_id=2')
                    else:
                        conn.execute(f'CREATE TEMP VIEW {table} AS SELECT * FROM main.{table} WHERE 0')
            for table in ('autohost_lobby_time','autohost_pp_achievements'):
                if table+'_catch' in tables:
                    conn.execute(f'CREATE TEMP VIEW {table} AS SELECT * FROM main.{table}_catch')
                elif table in tables:
                    conn.execute(f'CREATE TEMP VIEW {table} AS SELECT * FROM main.{table} WHERE 0')
            if 'map_ratings_catch' in tables:
                conn.execute(f'CREATE TEMP VIEW map_ratings AS SELECT beatmap_id-{CATCH_KEY_BASE} AS beatmap_id,user_id,rating,created_at,map_origin,key_count FROM main.map_ratings_catch WHERE beatmap_id>{CATCH_KEY_BASE} AND beatmap_id<={CATCH_KEY_BASE+MAX_PROVIDER_MAP_ID}')
            elif 'map_ratings' in tables:
                conn.execute('CREATE TEMP VIEW map_ratings AS SELECT * FROM main.map_ratings WHERE 0')
            return conn
        if "autohost_room_rulesets" in tables:
            for table in ("autohost_matches", "autohost_room_players"):
                if table in tables:
                    conn.execute(f"CREATE TEMP VIEW {table} AS SELECT t.* FROM main.{table} t LEFT JOIN main.autohost_room_rulesets rr ON rr.room_id=t.room_id WHERE COALESCE(rr.ruleset_id,0)={self.ruleset_id}")
            if self.ruleset_id == 2 and 'map_ratings' in tables:
                from .rating_outbox import CATCH_KEY_BASE, MAX_PROVIDER_MAP_ID
                conn.execute(f'CREATE TEMP VIEW map_ratings AS SELECT beatmap_id-{CATCH_KEY_BASE} AS beatmap_id,user_id,rating,created_at,map_origin,key_count FROM main.map_ratings WHERE beatmap_id>{CATCH_KEY_BASE} AND beatmap_id<={CATCH_KEY_BASE+MAX_PROVIDER_MAP_ID}')
            elif "map_ratings" in tables and "autohost_matches" in tables:
                if self.ruleset_id == 3:
                    conn.execute("CREATE TEMP VIEW map_ratings AS SELECT r.* FROM main.map_ratings r WHERE r.beatmap_id IN (SELECT beatmap_id FROM autohost_matches)")
                else:
                    # Keep historical ratings predating the match ledger.
                    conn.execute("CREATE TEMP VIEW map_ratings AS SELECT r.* FROM main.map_ratings r WHERE r.beatmap_id NOT IN (SELECT m.beatmap_id FROM main.autohost_matches m JOIN main.autohost_room_rulesets rr ON rr.room_id=m.room_id WHERE rr.ruleset_id=3 AND m.beatmap_id IS NOT NULL) AND r.beatmap_id < 4503599627370496")
        if self.ruleset_id == 2 and "autohost_lobby_time_catch" in tables:
            conn.execute("CREATE TEMP VIEW autohost_lobby_time AS SELECT * FROM main.autohost_lobby_time_catch")
        if self.ruleset_id == 3 and "autohost_lobby_time_mania" in tables:
            conn.execute("CREATE TEMP VIEW autohost_lobby_time AS SELECT * FROM main.autohost_lobby_time_mania")
        return conn

    @staticmethod
    def period(raw: str | None) -> tuple[str, float | None]:
        value = (raw or "7d").casefold()
        days = {"24h": 1, "1d": 1, "7d": 7, "30d": 30, "all": None}
        if value not in days:
            raise ValueError("use 24h, 7d, 30d, or all")
        return value, None if days[value] is None else time.time() - days[value] * 86400

    def report(self, name: str, period: str | None = None) -> str:
        label, cutoff = self.period(period)
        conn = self._conn()
        match_where = "" if cutoff is None else " WHERE started_at >= ?"
        args: tuple[float, ...] = () if cutoff is None else (cutoff,)
        try:
            if name == "activity":
                row = conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT room_id), COUNT(DISTINCT beatmap_id), "
                    "ROUND(AVG(participant_count), 1), SUM(result = 'completed'), "
                    f"SUM(result = 'aborted') FROM autohost_matches{match_where}",
                    args,
                ).fetchone()
                return (
                    f"**Activity · {label}**\n"
                    f"Matches: {row[0]:,} across {row[1]:,} rooms | maps: {row[2]:,}\n"
                    f"Avg players: {row[3] or 0} | completed: {row[4] or 0:,} | "
                    f"aborted: {row[5] or 0:,}"
                )

            if name == "players":
                if cutoff is None:
                    rows = conn.execute(
                        "SELECT username, total_seconds, visit_count "
                        "FROM autohost_lobby_time "
                        "ORDER BY total_seconds DESC LIMIT 12"
                    ).fetchall()
                else:
                    # Room sessions are the finest-grained stored time data.
                    rows = conn.execute(
                        "SELECT username, SUM(total_seconds), SUM(join_count) "
                        "FROM autohost_room_players "
                        "WHERE last_join_at >= ? "
                        "GROUP BY user_id "
                        "ORDER BY SUM(total_seconds) DESC LIMIT 12",
                        args,
                    ).fetchall()
                lines = [
                    f"{index}. {username or 'Unknown'} — {seconds / 3600:.1f}h · "
                    f"{visits} visits"
                    for index, (username, seconds, visits) in enumerate(rows, start=1)
                ]
                return f"**Top player time · {label}**\n" + (
                    "\n".join(lines) or "No activity recorded."
                )

            if name == "ratings":
                rating_where = "" if cutoff is None else " WHERE created_at >= ?"
                row = conn.execute(
                    "SELECT COUNT(*), SUM(rating = 1), SUM(rating = -1), "
                    "SUM(rating = 0), COUNT(DISTINCT user_id), COUNT(DISTINCT beatmap_id) "
                    f"FROM map_ratings{rating_where}",
                    args,
                ).fetchone()
                return (
                    f"**Ratings · {label}**\n"
                    f"{row[0]:,} votes from {row[4]:,} players on {row[5]:,} maps\n"
                    f"👍 {row[1] or 0:,} · 👎 {row[2] or 0:,} · 😐 {row[3] or 0:,}"
                )

            if name == "retention":
                lifetime = conn.execute(
                    "SELECT COUNT(*), SUM(visit_count >= 2), SUM(visit_count >= 3), "
                    "ROUND(AVG(visit_count), 1) FROM autohost_lobby_time"
                ).fetchone()
                if cutoff is None:
                    return (
                        "**Retention · all**\n"
                        f"Players: {lifetime[0]:,} | 2+ visits: {lifetime[1] or 0:,} "
                        f"({(lifetime[1] or 0) / lifetime[0]:.1%})\n"
                        f"3+ visits: {lifetime[2] or 0:,} "
                        f"({(lifetime[2] or 0) / lifetime[0]:.1%}) | "
                        f"avg visits: {lifetime[3] or 0}"
                    )

                active, new, returning = conn.execute(
                    "SELECT "
                    "SUM(last_join_at >= ?), "
                    "SUM(first_join_at >= ?), "
                    "SUM(last_join_at >= ? AND first_join_at < ?) "
                    "FROM autohost_lobby_time",
                    (cutoff, cutoff, cutoff, cutoff),
                ).fetchone()
                if not lifetime[0]:
                    return f"**Retention · {label}**\nNo player history recorded."
                active = active or 0
                new = new or 0
                returning = returning or 0
                returning_rate = returning / active if active else 0.0
                return (
                    f"**Retention · {label}**\n"
                    f"Active: {active:,} | new: {new:,} | "
                    f"returning: {returning:,} ({returning_rate:.1%} of active)\n"
                    f"All-time 2+ visits: {lifetime[1] or 0:,} / {lifetime[0]:,} "
                    f"({(lifetime[1] or 0) / lifetime[0]:.1%})"
                )

            if name == "mapstats":
                rows = conn.execute(
                    "SELECT map_label, COUNT(*), ROUND(AVG(participant_count), 1) "
                    f"FROM autohost_matches{match_where} "
                    "GROUP BY beatmap_id ORDER BY COUNT(*) DESC LIMIT 8",
                    args,
                ).fetchall()
                lines = [
                    f"{index}. {str(map_name or 'Unknown')[:90]} — {plays} plays · "
                    f"{players or 0} avg"
                    for index, (map_name, plays, players) in enumerate(rows, start=1)
                ]
                return f"**Most played maps · {label}**\n" + (
                    "\n".join(lines) or "No matches recorded."
                )

            raise ValueError("unknown report")
        finally:
            conn.close()
