"""Conservative solo difficulty recall; additive storage in ratings.db."""
import logging
from contextlib import closing
import math
import sqlite3
import time

log = logging.getLogger("bot")


def preference_scope(cfg, pool):
    if int(cfg.get("ruleset_id", 0)) == 3:
        return "mania:" + str(cfg.get("mania_keys", 4))
    return "standard:loved" if getattr(pool, "loved_mode", False) else "standard:ranked"


def preference(db, scope, uid, window=None):
    """Read/upsert independently of existing ratings schemas and connections."""
    with closing(sqlite3.connect(db, timeout=5)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS player_difficulty_preferences (
            scope TEXT NOT NULL, user_id INTEGER NOT NULL,
            sr_min REAL NOT NULL, sr_max REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY (scope, user_id))""")
        if window is not None:
            lo, hi = map(float, window)
            if not (math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi):
                raise ValueError("Invalid difficulty preference")
            conn.execute("""INSERT INTO player_difficulty_preferences VALUES (?,?,?,?,?)
                ON CONFLICT(scope,user_id) DO UPDATE SET
                sr_min=excluded.sr_min, sr_max=excluded.sr_max, updated_at=excluded.updated_at""",
                (scope, int(uid), lo, hi, time.time()))
        return conn.execute("SELECT sr_min,sr_max FROM player_difficulty_preferences WHERE scope=? AND user_id=?",
                            (scope, int(uid))).fetchone()


def map_type_label(m):
    if getattr(m, "is_mania", False):
        styles = [s for s in getattr(m, "mania_styles", ())
                  if s in ("jack", "chordjack", "stream", "ln", "technical")]
        # Two tags are enough for the short PP line; .map retains full details.
        return "/".join(styles[:2]) or "4K"
    if getattr(m, "is_loved", False):
        return "loved"
    labels = [name for name in ("farm", "stream") if getattr(m, "is_" + name, False)]
    return "+".join(labels) or "unclassified"


class DifficultyRecall:
    def _occupants(self):
        return set(self.players) - self.non_gameplay_user_ids()

    def _note_solo_difficulty_choice(self, uid):
        """Call only after an accepted explicit change, never an ordinary join."""
        self._preference_played = None
        self._preference_started = None
        self._preference_choice = int(uid) if self._occupants() == {int(uid)} else None
        self._preference_window = None

    def _note_preference_match(self):
        self._preference_played = None
        self._preference_started = None
        uid = getattr(self, "_preference_choice", None)
        window = (float(self.pool.sr_min), float(self.pool.sr_max))
        if uid is None or self._occupants() != {uid} or uid not in self.playing_player_ids:
            return
        if getattr(self, "_auto_retune_pending", False):
            return
        chosen = getattr(self, "_preference_window", None)
        if chosen is not None and chosen != window:
            return
        m = self.playing_map
        # Do not qualify a stale queued chart outside the selected window.
        if m is None or not window[0] <= float(m.sr) <= window[1]:
            return
        self._preference_window = window
        self._preference_started = (uid, window)

    def _complete_preference_match(self):
        started = getattr(self, "_preference_started", None)
        if started and self._occupants() == {started[0]}:
            self._preference_played = started
        self._preference_started = None

    def _save_departing_preference(self, uid, kicked=False):
        if uid not in self.players:
            return
        played = getattr(self, "_preference_played", None)
        if (not kicked and self._occupants() == {uid} and played
                and played[0] == uid and getattr(self, "_preference_choice", None) == uid
                and played[1] == (float(self.pool.sr_min), float(self.pool.sr_max))):
            try:
                preference(self.ratings.path, preference_scope(self.cfg, self.pool), uid, played[1])
                log.info("saved solo difficulty preference for %s: %.2f-%.2f", uid, *played[1])
            except (sqlite3.Error, OSError, ValueError):
                log.exception("could not save solo difficulty preference")
        if getattr(self, "_preference_choice", None) == uid:
            self._preference_choice = None
            self._preference_played = None
        self._release_recalled_difficulty()

    def _release_recalled_difficulty(self):
        if getattr(self, "_recalled_window", None) is None:
            return
        self._recalled_window = None
        self._clear_community_difficulty("solo recall ended")
        if self._fixed_difficulty_mode():
            self._apply_fixed_community_range("solo recall ended")
            self._fixed_range_rebuild_pending = True
        else:
            self.mark_auto_retune_pending()

    def _restore_solo_difficulty(self, uid):
        if getattr(self, "state", None) != "IDLE" or self._occupants() != {uid}:
            return None
        try:
            window = preference(self.ratings.path, preference_scope(self.cfg, self.pool), uid)
            if not window or not all(math.isfinite(x) for x in window) or not 0 <= window[0] < window[1]:
                return None
            old = (self.pool.sr_min, self.pool.sr_max)
            if self.pool.set_range(*window) <= 0:
                return None
            self._recalled_window = tuple(window)
            # Refresh the visible map before telling the player their range is ready.
            refreshed = False
            try:
                refreshed = self.perform_soft_restart(announce=False)
            finally:
                if not refreshed:
                    self._recalled_window = None
                    self.pool.set_range(*old)
            if not refreshed:
                return None
            self._clear_community_difficulty("restored solo preference")
            self._recalled_window = tuple(window)
            self._auto_retune_pending = False
            self._fixed_range_rebuild_pending = False
            self.update_room_name_for_range(*window)
            self._note_solo_difficulty_choice(uid)
            return window
        except (sqlite3.Error, OSError, ValueError):
            log.exception("could not restore solo difficulty preference")
            return None
