"""Ruleset-specific recall scopes over the unchanged preference storage.

Existing installed update guards pin the storage module. Standard and Mania
retain that exact implementation; Taiko and Catch use distinct scopes through
the same preference function and lifecycle conditions.
"""
import logging
import math
import sqlite3
from .player_preferences import (DifficultyRecall, preference,
                                 preference_scope as legacy_scope,
                                 map_type_label as legacy_label)
log=logging.getLogger('bot')


def preference_scope(cfg,pool):
    if cfg.get('ruleset_id',0)==1:
        return 'taiko:ranked'
    if cfg.get('ruleset_id',0)==2:
        return 'catch:loved' if getattr(pool, 'catch_include_loved', False) else 'catch:ranked'
    return legacy_scope(cfg,pool)


def map_type_label(item):
    if getattr(item,'ruleset_id',None)==1:
        return 'Taiko convert' if getattr(item,'is_taiko_convert',False) else 'Taiko'
    return legacy_label(item)


class ModeDifficultyRecall(DifficultyRecall):
    def _save_departing_preference(self, uid, kicked=False):
        if self.cfg.get('ruleset_id',0) not in (1,2):
            return super()._save_departing_preference(uid,kicked=kicked)
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

    def _restore_solo_difficulty(self, uid):
        if self.cfg.get('ruleset_id',0) not in (1,2):
            return super()._restore_solo_difficulty(uid)
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


def prepare(path):
    """Stopped preparation of the unchanged preference table before worker guards.

    UID zero is a read-only lookup, never a player row or a saved preference.
    """
    preference(str(path),'taiko:ranked',0)
