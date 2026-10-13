"""Map pools sourced from ranked Farm/Stream data or a separate Loved DB.

The pool consumes both ``farm_report`` (Farm Score) and ``stream_features``
(Stream Score) from the same farm_v2 database. Lobby-specific SR/length/pass
filters are applied dynamically. Farm entries whose dominant score bucket uses
DT or NC are excluded from the Farm axis.

Auto difficulty uses a continuous pp -> star-rating target rather than fixed
skill tiers. The normal curve stays simple through the vast majority of players:

    2000pp -> 4.0*
    3000pp -> 4.4*
    4000pp -> 4.8*
    5000pp -> 5.2*
    6000pp -> 5.6*
    7000pp -> 6.0*
    8000pp -> 6.4*

Above 8000pp the slope deliberately tapers to 0.2* per 1000pp. Very high PP
values otherwise extrapolate into tiny 8*+ farm pools where queueing can become
impossible. Admin/community bias is still applied on top, so genuinely elite
lobbies can push higher when they want to. The automatic range keeps a 3.0*
lower floor and does not use a hard upper star-rating ceiling.
"""
from __future__ import annotations

import logging
import math
import random
import re
import statistics
import unicodedata
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from .jump_catalog import JUMP_CATALOG

log = logging.getLogger("pool")


# ----------------------------------------------------- auto difficulty model

AUTO_SR_FLOOR = 3.0
AUTO_SR_HALF_WIDTH = 0.40
AUTO_SR_STEP = 0.05
AUTO_SR_TAPER_PP = 8000.0
AUTO_SR_NORMAL_SLOPE = 0.0004
AUTO_SR_HIGH_PP_SLOPE = 0.0002
# Automatic difficulty may keep extending the upper edge at elite PP, but
# its lower edge never rises above this value. Manual .range is intentionally
# unrestricted because this constant is only applied by auto_range_for_pp().
AUTO_SR_AUTO_MIN_CAP = 7.50

# ---------------------------------------------------------- player feedback

# Human .good/.bad feedback changes selection probability, never eligibility.
# One bad vote is intentionally mild; repeated bad votes compound sharply.
# Good votes accumulate gradually and are capped so popular maps do not take
# over the entire rotation.
RATING_GOOD_BOOST_PER_VOTE = 0.25
RATING_GOOD_MAX_FACTOR = 2.50
RATING_FIRST_BAD_FACTOR = 0.85
RATING_REPEAT_BAD_FACTOR = 0.35
RATING_MIN_WEIGHT = 0.03
PASSCOUNT_MAX_BOOST = 0.50


def target_sr_for_pp(pp: float, half_width: float = AUTO_SR_HALF_WIDTH,
                     step: float = AUTO_SR_STEP, bias: float = 0.0) -> float:
    """Return the continuous target star rating for a player's pp.

    Up through 8000pp the relationship is the original linear curve:
        base = 4.0 + (pp - 2000) * 0.0004

    Above 8000pp only the excess PP uses a gentler 0.0002 slope. This preserves
    the established behavior for ordinary lobbies while preventing one elite
    player's PP from jumping the requested farm pool into a nearly empty 8*+
    band. ``bias`` is applied after the taper, so admin/community difficulty
    preference still works normally.

    Targets are rounded to a small 0.05* step so tiny pp changes do not cause
    constant pool reloads. Only the lower edge is clamped; there is deliberately
    no hard upper SR ceiling.
    """
    pp = max(float(pp or 0.0), 0.0)
    half_width = max(float(half_width), 0.0)
    step = max(float(step), 0.0)
    bias = float(bias or 0.0)

    if pp <= AUTO_SR_TAPER_PP:
        base = 4.0 + (pp - 2000.0) * AUTO_SR_NORMAL_SLOPE
    else:
        at_taper = (
            4.0
            + (AUTO_SR_TAPER_PP - 2000.0) * AUTO_SR_NORMAL_SLOPE
        )
        base = at_taper + (pp - AUTO_SR_TAPER_PP) * AUTO_SR_HIGH_PP_SLOPE

    raw = base + bias

    if step > 0:
        raw = round(raw / step) * step

    # Keep the bottom of the selected window at or above the supported floor.
    # The upper end is intentionally uncapped.
    lo_target = AUTO_SR_FLOOR + half_width
    return max(raw, lo_target)


def auto_range_for_pp(pp: float, half_width: float = AUTO_SR_HALF_WIDTH,
                      step: float = AUTO_SR_STEP,
                      bias: float = 0.0) -> tuple[float, float]:
    """Return the automatic SR range centered on the pp-derived target.

    The upper edge remains uncapped. Once the natural lower edge would exceed
    7.50*, only the upper edge keeps climbing. This is deliberately an AutoDiff
    rule only; MapPool.set_range() still accepts arbitrary manual ranges.
    """
    target = target_sr_for_pp(
        pp, half_width=half_width, step=step, bias=bias)
    natural_lo = max(AUTO_SR_FLOOR, target - half_width)
    lo = min(AUTO_SR_AUTO_MIN_CAP, natural_lo)
    hi = target + half_width
    return round(lo, 2), round(hi, 2)


# Backwards-compatible name used by older bot.py versions.
def tier_for_pp(pp: float, half_width: float = AUTO_SR_HALF_WIDTH,
                bias: float = 0.0) -> tuple[float, float]:
    return auto_range_for_pp(pp, half_width=half_width, bias=bias)


# --------------------------------------------------------------- map records

@dataclass
class PoolMap:
    beatmap_id: int
    artist: str
    title: str
    version: str
    sr: float
    hit_length: int
    farm_score: float
    passcount: int
    quadrant: str = ""
    forgiveness: float = 0.0
    max_pp: float = 0.0
    stream_score: float = 0.0
    stream_bpm: float = 0.0
    is_farm: bool = False
    is_stream: bool = False
    is_loved: bool = False
    is_mania: bool = False
    is_mania_convert: bool = False
    mania_keys: int | None = None
    map_origin: str = "standard"
    mania_styles: tuple[str, ...] = ()
    mania_rank_score: float | None = None
    mania_confidence: float | None = None
    mania_farm_index: float | None = None
    mania_farm_confidence: float | None = None
    is_jump: bool = False
    jump_score: float = 0.0
    jump_relaxed_score: float = 0.0
    is_dt: bool = False
    clock_rate: float = 1.0
    source_sr: float | None = None
    source_hit_length: int | None = None
    dt_checksum: str | None = None
    dt_calculator: str | None = None
    approach_rate: float | None = None

    @property
    def label(self) -> str:
        suffix = ' +DT' if self.is_dt else ''
        return f"{self.artist} - {self.title} [{self.version}]{suffix} ({self.sr:.2f}*)"

    @property
    def url(self) -> str:
        return f"https://osu.ppy.sh/b/{self.beatmap_id}"

    @property
    def category(self) -> str:
        if self.is_mania:
            suffix = "-convert" if self.is_mania_convert else ""
            return f"mania-{int(self.mania_keys or 4)}k{suffix}"
        if self.is_loved:
            return "loved"
        if self.is_jump:
            return "jump"
        if self.is_farm and self.is_stream:
            return "farm+stream"
        if self.is_stream:
            return "stream"
        if self.is_farm:
            return "farm"
        return "fallback"


class MapPool:
    def __init__(self, db_path: str, sr_min=4.0, sr_max=5.5,
                 max_length=150, min_passcount=7_500, limit=250,
                 ratings_db: str | None = None,
                 stream_enabled: bool = True,
                 stream_share: float = 0.50,
                 stream_min_score: float = 0.50,
                 stream_min_bpm: float = 140.0,
                 stream_min_length: int = 45,
                 max_map_age_years: float = 0.0,
                 high_star_relax_enabled: bool = False,
                 high_star_relax_threshold: float = 7.0,
                 high_star_relax_min_maps: int = 20,
                 high_star_relax_length_step: int = 60,
                 high_star_relax_age_step_years: float = 2.0,
                 high_star_relax_max_steps: int = 2,
                 loved_mode: bool = False,
                 sparse_passcount_enabled: bool = False,
                 sparse_passcount_min_maps: int = 50,
                 sparse_passcount_floor: int = 1000,
                 ratings_reader=None):
        self.db_path = db_path
        self.loved_mode = bool(loved_mode)
        self.ratings_db = str(ratings_db) if ratings_db else None
        self.ratings_reader = ratings_reader
        self.sr_min, self.sr_max = float(sr_min), float(sr_max)
        # Loved rooms may explicitly use 0 for no length ceiling. Ranked
        # Farm/Stream rooms retain their historical positive ceiling.
        self.max_length = (
            max(0, int(max_length))
            if self.loved_mode else max(1, int(max_length))
        )
        self.min_passcount = int(min_passcount)
        self.sparse_passcount_enabled = sparse_passcount_enabled
        self.sparse_passcount_min_maps = sparse_passcount_min_maps
        self.sparse_passcount_floor = sparse_passcount_floor
        self._validate_passcount_policy()
        self.limit = max(1, int(limit))
        # Opt-in worker-local policy; legacy launchers and callers keep their
        # existing duration behavior until real_max_length is configured.
        self.real_max_length = None
        self.preferred_min_length = 90
        self.preferred_max_length = 150
        self.preferred_length_share = 0.80

        # Farm and Stream candidates live in the same farm_v2.db. Farm ranking is
        # evidence-based (farm_report); Stream ranking is structural
        # (stream_features). A 50/50 source split keeps both categories represented
        # without comparing their unrelated score scales directly.
        # Runtime source mode.  ``stream_enabled`` / ``stream_share`` remain
        # constructor-compatible with older room configs, but public commands can
        # switch between Farm-only, Stream-only, and a targeted Variety mix.
        configured_stream_share = min(1.0, max(0.0, float(stream_share)))
        if self.loved_mode:
            self.source_mode = "loved"
            self.farm_share = 0.0
            self.stream_share = 0.0
        elif not bool(stream_enabled) or configured_stream_share <= 0.0:
            self.source_mode = "farm"
            self.farm_share = 1.0
            self.stream_share = 0.0
        elif configured_stream_share >= 1.0:
            self.source_mode = "stream"
            self.farm_share = 0.0
            self.stream_share = 1.0
        else:
            self.source_mode = "variety"
            self.stream_share = configured_stream_share
            self.farm_share = 1.0 - configured_stream_share
        # Backwards-compatible flag for any older caller that inspects it.
        self.stream_enabled = self.source_mode in ("stream", "variety")

        self.stream_min_score = max(0.0, float(stream_min_score))
        self.stream_min_bpm = max(0.0, float(stream_min_bpm))
        self.stream_min_length = max(0, int(stream_min_length))
        # Zero disables the ranked-date filter. When enabled, maps with a
        # missing or unparsable ranked_date are deliberately ineligible.
        self.max_map_age_years = max(0.0, float(max_map_age_years or 0.0))
        self.high_star_relax_enabled = bool(high_star_relax_enabled)
        # Kept as a config/API compatibility field.  Sparse-pool fallback is
        # intentionally no longer gated by an arbitrary star threshold.
        self.high_star_relax_threshold = max(0.0, float(high_star_relax_threshold))
        self.high_star_relax_min_maps = max(1, int(high_star_relax_min_maps))
        self.high_star_relax_length_step = max(0, int(high_star_relax_length_step))
        self.high_star_relax_age_step_years = max(0.0, float(high_star_relax_age_step_years))
        self.high_star_relax_max_steps = max(0, int(high_star_relax_max_steps))
        self._high_star_relaxing = False

        self.maps: list[PoolMap] = []
        self._recent: list[int] = []
        # Selections protect the current/pre-download queue. Completed rounds
        # separately provide an approximately hour-long song-family cooldown.
        self._recent_songs: list[str] = []
        self._song_rounds: list[tuple[str, float]] = []
        # Beatmaps/sets proven unavailable for download at runtime are excluded
        # from future picks for the lifetime of this bot process.
        self._runtime_excluded: set[int] = set()
        self._round_history = []
        self._playlist_reserved = set()
        self._jump_fallback_active = False
        self.configure_jump_policy({})
        self.reload()

    def _has_table(self, conn, name: str) -> bool:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone())

    def _has_report(self, conn) -> bool:
        return self._has_table(conn, "farm_report")

    @staticmethod
    def _has_column(conn, table: str, column: str) -> bool:
        return any(
            str(row[1]).casefold() == str(column).casefold()
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )

    def max_map_age_cutoff(self, now: datetime | None = None) -> str | None:
        """Return the UTC ranked-date cutoff, or None when age filtering is off.

        Whole years use calendar subtraction so ``10 years`` means the same
        month/day ten years ago. A fractional remainder is converted using the
        mean Gregorian year length, which keeps values such as ``7.5`` useful
        without adding a third-party date dependency.
        """
        years = max(0.0, float(self.max_map_age_years or 0.0))
        if years <= 0.0:
            return None

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)

        whole_years = int(years)
        fractional_years = years - whole_years
        target_year = current.year - whole_years
        try:
            cutoff = current.replace(year=target_year)
        except ValueError:
            # Feb 29 minus a non-leap number of years becomes Feb 28.
            cutoff = current.replace(year=target_year, day=28)
        if fractional_years:
            cutoff -= timedelta(days=365.2425 * fractional_years)

        return cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")

    def max_map_age_label(self) -> str:
        years = max(0.0, float(self.max_map_age_years or 0.0))
        return "off" if years <= 0.0 else f"{years:g}y"

    def max_length_label(self) -> str:
        """Return the player-facing length ceiling label."""
        if self.loved_mode and int(self.max_length) <= 0:
            return "unlimited"
        return f"{int(self.max_length)}s"

    def _validate_passcount_policy(self):
        if type(self.sparse_passcount_enabled) is not bool:
            raise ValueError('sparse_passcount_enabled must be true/false')
        if type(self.sparse_passcount_min_maps) is not int or not 1 <= self.sparse_passcount_min_maps <= 5000:
            raise ValueError('sparse_passcount_min_maps must be an integer from 1 to 5000')
        if type(self.sparse_passcount_floor) is not int or not 1 <= self.sparse_passcount_floor <= 1000000:
            raise ValueError('sparse_passcount_floor must be an integer from 1 to 1000000')

    def _passcount_steps(self):
        """Configured baseline is never rewritten; the floor never raises it."""
        value = self.min_passcount
        yield value
        while value > self.sparse_passcount_floor:
            value = max(self.sparse_passcount_floor, value // 2)
            yield value

    def _passcount_fallback_enabled(self):
        return (self.sparse_passcount_enabled and not self.loved_mode
                and not getattr(self, 'mania_mode', False))

    def effective_min_passcount(self):
        if not self._passcount_fallback_enabled() or not self.maps:
            return self.min_passcount
        minimum = min(m.passcount for m in self.maps)
        return next((value for value in self._passcount_steps() if minimum >= value), self.min_passcount)

    def _preferred_passcount_candidates(self, candidates):
        if not self._passcount_fallback_enabled():
            return candidates
        for value in self._passcount_steps():
            preferred = [m for m in candidates if m.passcount >= value]
            if preferred:
                return preferred
        return candidates

    def _maybe_relax_passcount(self):
        """Fill thin Ranked pools, preserving stars/length/age/style boundaries.

        10k -> 5k -> 2.5k -> 1.25k -> 1k by default. Each query uses the same
        source and hard filters, and reloads always retry the original baseline.
        Recent history is selection policy, not catalog eligibility; confirmed
        unavailable maps do not count toward the target.
        """
        if not self._passcount_fallback_enabled() or hasattr(self, '_query_min_passcount'):
            return len(self.maps)
        target = min(self.sparse_passcount_min_maps, self.limit)
        def eligible_count(maps):
            return sum(m.beatmap_id not in self._runtime_excluded for m in maps)
        if eligible_count(self.maps) >= target:
            return len(self.maps)
        original = self.maps
        ordered = [m for m in original if m.beatmap_id not in self._runtime_excluded]
        seen = {m.beatmap_id for m in ordered}
        try:
            for value in list(self._passcount_steps())[1:]:
                self._query_min_passcount = value
                self.reload()
                for item in self.maps:
                    if (item.beatmap_id not in seen and item.beatmap_id not in self._runtime_excluded
                            and len(ordered) < self.limit):
                        ordered.append(item)
                        seen.add(item.beatmap_id)
                if eligible_count(ordered) >= target:
                    break
        except Exception:
            self.maps = original
            raise
        finally:
            self.__dict__.pop('_query_min_passcount', None)
        self.maps = ordered
        if self.effective_min_passcount() < self.min_passcount:
            log.info('sparse Ranked %.2f-%.2f* %s pool: %d eligible maps; passes %d -> %d (target %d), stars/length/age unchanged',
                     self.sr_min, self.sr_max, self.source_mode, eligible_count(ordered),
                     self.min_passcount, self.effective_min_passcount(), target)
        return len(self.maps)

    def _maybe_relax_high_star_window(self, current_count: int) -> int | None:
        """Append progressively less-relevant maps to a sparse SR window.

        The configured SR window is a hard boundary.  When the preferred
        candidates are exhausted, reloads may broaden length and ranked age,
        but the strict candidates remain first in the selection order.  This
        applies at every SR, rather than switching behavior at an arbitrary
        star rating.
        """
        if (
            self._high_star_relaxing
            or self.loved_mode
            or not self.high_star_relax_enabled
            or current_count >= self.high_star_relax_min_maps
            or self.high_star_relax_max_steps <= 0
        ):
            return None

        old_length = self.max_length
        old_age = self.max_map_age_years
        preferred = list(self.maps)

        def append_unique(existing, additions):
            seen = {int(item.beatmap_id) for item in existing}
            for item in additions:
                if int(item.beatmap_id) not in seen:
                    existing.append(item)
                    seen.add(int(item.beatmap_id))
            return existing

        self._high_star_relaxing = True
        try:
            for step in range(1, self.high_star_relax_max_steps + 1):
                self.max_length = old_length + self.high_star_relax_length_step * step
                if self.real_max_length is not None:
                    self.max_length = min(self.max_length, self.real_max_length)
                if old_age > 0.0:
                    self.max_map_age_years = (
                        old_age + self.high_star_relax_age_step_years * step
                    )
                self.reload()
                preferred = append_unique(preferred, self.maps)
                self.maps = preferred
                count = len(preferred)
                best_count = count
                if count >= self.high_star_relax_min_maps:
                    log.info(
                        "sparse %.2f-%.2f* pool expanded at step %d/%d: "
                        "length <=%ds, max_age=%s, %d ordered maps",
                        self.sr_min, self.sr_max, step,
                        self.high_star_relax_max_steps, self.max_length,
                        self.max_map_age_label(), count,
                    )
                    return count
            if best_count > 0:
                log.info(
                    "SR pool remained sparse after %d relaxation step(s): %d ordered maps",
                    self.high_star_relax_max_steps, best_count,
                )
                return best_count
        except sqlite3.Error:
            self.max_length = old_length
            self.max_map_age_years = old_age
            raise
        finally:
            self._high_star_relaxing = False
        return None

    @staticmethod
    def _merge_map(existing: PoolMap, incoming: PoolMap) -> PoolMap:
        """Merge category evidence for the same beatmap without losing ranking data."""
        if incoming.is_farm:
            existing.is_farm = True
            existing.farm_score = incoming.farm_score
            existing.quadrant = incoming.quadrant
            existing.forgiveness = incoming.forgiveness
            existing.max_pp = incoming.max_pp
        if incoming.is_stream:
            existing.is_stream = True
            if incoming.stream_score >= existing.stream_score:
                existing.stream_score = incoming.stream_score
                existing.stream_bpm = incoming.stream_bpm
        if incoming.is_loved:
            existing.is_loved = True
        return existing

    @staticmethod
    def _map_from_row(row) -> PoolMap:
        keys = set(row.keys())
        def value(name, default=None):
            return row[name] if name in keys and row[name] is not None else default

        return PoolMap(
            beatmap_id=int(value("beatmap_id", 0)),
            artist=value("artist", "?") or "?",
            title=value("title", "?") or "?",
            version=value("version", "?") or "?",
            sr=float(value("sr", 0.0) or 0.0),
            hit_length=int(value("hit_length", 0) or 0),
            farm_score=float(value("farm_score", 0.0) or 0.0),
            passcount=int(value("passcount", 0) or 0),
            quadrant=value("quadrant", "") or "",
            forgiveness=float(value("forgiveness", 0.0) or 0.0),
            max_pp=float(value("max_pp", 0.0) or 0.0),
            stream_score=float(value("stream_score", 0.0) or 0.0),
            stream_bpm=float(value("stream_bpm", 0.0) or 0.0),
            is_farm=bool(value("is_farm", 0)),
            is_stream=bool(value("is_stream", 0)),
            is_loved=bool(value("is_loved", 0)),
            approach_rate=value("ar", None),
        )

    def _balanced_candidates(
            self, farm_ranked: list[PoolMap], stream_ranked: list[PoolMap]
    ) -> list[PoolMap]:
        """Build one bounded live pool while reserving source slots for both axes."""
        merged: dict[int, PoolMap] = {}

        def absorb(rows: list[PoolMap]) -> list[PoolMap]:
            ranked: list[PoolMap] = []
            for item in rows:
                bid = int(item.beatmap_id)
                current = merged.get(bid)
                if current is None:
                    merged[bid] = item
                    current = item
                else:
                    self._merge_map(current, item)
                ranked.append(current)
            return ranked

        farm_ranked = absorb(farm_ranked)
        stream_ranked = absorb(stream_ranked)

        if self.source_mode == "farm":
            farm_target, stream_target = self.limit, 0
        elif self.source_mode == "stream":
            farm_target, stream_target = 0, self.limit
        else:
            farm_target = int(round(self.limit * self.farm_share))
            farm_target = min(self.limit, max(0, farm_target))
            stream_target = self.limit - farm_target

        selected: list[PoolMap] = []
        selected_ids: set[int] = set()

        def take(rows: list[PoolMap], count: int) -> None:
            if count <= 0:
                return
            added = 0
            for item in rows:
                bid = int(item.beatmap_id)
                if bid in selected_ids:
                    continue
                selected.append(item)
                selected_ids.add(bid)
                added += 1
                if added >= count or len(selected) >= self.limit:
                    return

        # Source quotas are intentionally based on where the item was ranked.
        # An overlap map can still report both labels, but it occupies only one
        # unique slot in the live pool.
        take(farm_ranked, farm_target)
        take(stream_ranked, stream_target)

        # Farm-only and Stream-only are hard source modes: do not back-fill from
        # the other category if the chosen source is sparse.  Variety is a target
        # mix, not a hard partition, so it may use spare maps from either side to
        # keep the live pool full in thin SR bands.
        if self.source_mode == "variety":
            i = 0
            while len(selected) < self.limit:
                if i < len(farm_ranked):
                    item = farm_ranked[i]
                    if item.beatmap_id not in selected_ids:
                        selected.append(item)
                        selected_ids.add(item.beatmap_id)
                        if len(selected) >= self.limit:
                            break
                if i < len(stream_ranked):
                    item = stream_ranked[i]
                    if item.beatmap_id not in selected_ids:
                        selected.append(item)
                        selected_ids.add(item.beatmap_id)
                        if len(selected) >= self.limit:
                            break
                i += 1
                if i >= len(farm_ranked) and i >= len(stream_ranked):
                    break

        return selected[:self.limit]

    def reload(self) -> int:
        query_min_passcount = getattr(self, '_query_min_passcount', self.min_passcount)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        farm_ranked: list[PoolMap] = []
        stream_ranked: list[PoolMap] = []
        source_parts: list[str] = []
        candidate_limit = max(self.limit * 4, 500)

        try:
            if self.source_mode == 'jump' and not self.loved_mode:
                if not self._has_table(conn, 'beatmaps'):
                    self.maps = []
                    return 0
                age = self.max_map_age_cutoff()
                age_clause = ' AND julianday(ranked_date)>=julianday(?)' if age else ''
                params = [self.sr_min, self.sr_max, self.max_length, query_min_passcount]
                if age:
                    params.append(age)
                rows = conn.execute(
                    'SELECT * FROM beatmaps WHERE LOWER(status)=\'ranked\' '
                    'AND sr BETWEEN ? AND ? AND hit_length<=? AND passcount>=?' + age_clause,
                    params).fetchall()
                maps = []
                for row in rows:
                    evidence = JUMP_CATALOG.get(int(row['beatmap_id']))
                    if evidence is None:
                        continue
                    item = self._map_from_row(row)
                    item.is_jump = True
                    item.jump_score, item.jump_relaxed_score = evidence[:2]
                    if item.jump_score <= 0 and item.jump_relaxed_score <= 0:
                        continue
                    item.quadrant = 'jump' if item.jump_score > 0 else 'relaxed jump'
                    maps.append(item)
                maps.sort(key=lambda m: (m.jump_score > 0, m.jump_score, m.jump_relaxed_score), reverse=True)
                maps = [m for m in maps if m.beatmap_id not in self._runtime_excluded]
                self.maps = self._bounded_jump_candidates(maps)
                # Jump fallback changes pattern evidence only, never hard bounds.
                return self._maybe_relax_passcount()
            if self.loved_mode:
                required = {
                    "beatmap_id", "beatmapset_id", "artist", "title",
                    "version", "sr", "hit_length", "passcount", "status",
                    "mode", "convert", "ranked_date", "download_disabled",
                    "playcount",
                }
                if not self._has_table(conn, "beatmaps"):
                    raise sqlite3.OperationalError(
                        "Loved mode requires a Loved database with beatmaps"
                    )
                columns = {
                    str(row[1]).casefold()
                    for row in conn.execute("PRAGMA table_info(beatmaps)")
                }
                missing = sorted(required - columns)
                if missing:
                    raise sqlite3.OperationalError(
                        "Loved database is missing columns: " + ", ".join(missing)
                    )

                age_cutoff = self.max_map_age_cutoff()
                age_clause = (
                    " AND b.ranked_date IS NOT NULL"
                    " AND julianday(b.ranked_date) >= julianday(?)"
                    if age_cutoff is not None else ""
                )
                length_clause = " AND b.hit_length <= ?" if self.max_length > 0 else ""
                params: list[object] = [
                    self.sr_min, self.sr_max, self.min_passcount,
                ]
                if self.max_length > 0:
                    params.append(self.max_length)
                if age_cutoff is not None:
                    params.append(age_cutoff)
                params.append(candidate_limit)

                loved_rows = conn.execute(f"""
                    SELECT b.beatmap_id, b.artist, b.title, b.version, b.sr,
                           b.hit_length, b.passcount,
                           0.0 AS farm_score, '' AS quadrant,
                           0.0 AS forgiveness, 0.0 AS max_pp,
                           0.0 AS stream_score, 0.0 AS stream_bpm,
                           0 AS is_farm, 0 AS is_stream, 1 AS is_loved
                    FROM beatmaps b
                    WHERE LOWER(COALESCE(b.status,'')) = 'loved'
                      AND LOWER(COALESCE(b.mode,'')) = 'osu'
                      AND COALESCE(b.convert,0) = 0
                      AND COALESCE(b.download_disabled,0) = 0
                      AND b.sr BETWEEN ? AND ?
                      AND COALESCE(b.passcount,0) >= ?
                      {length_clause}
                      {age_clause}
                    ORDER BY COALESCE(b.passcount,0) DESC,
                             COALESCE(b.playcount,0) DESC,
                             b.beatmap_id ASC
                    LIMIT ?
                """, params).fetchall()
                self.maps = [self._map_from_row(row) for row in loved_rows]
                log.info(
                    "Loved pool loaded: %d maps, %.2f-%.2f*, length=%s, "
                    ">=%d passes, max_age=%s",
                    len(self.maps), self.sr_min, self.sr_max,
                    self.max_length_label(), self.min_passcount,
                    self.max_map_age_label(),
                )
                return len(self.maps)

            has_report = self._has_report(conn)
            has_beatmaps = self._has_table(conn, "beatmaps")
            has_stream = self._has_table(conn, "stream_features")

            age_cutoff = self.max_map_age_cutoff()
            if age_cutoff is not None:
                if not has_beatmaps:
                    raise sqlite3.OperationalError(
                        "max map age requires the beatmaps table"
                    )
                if not self._has_column(conn, "beatmaps", "ranked_date"):
                    raise sqlite3.OperationalError(
                        "max map age requires beatmaps.ranked_date"
                    )
            age_clause = (
                " AND b.ranked_date IS NOT NULL"
                " AND julianday(b.ranked_date) >= julianday(?)"
                if age_cutoff is not None else ""
            )
            age_params = (age_cutoff,) if age_cutoff is not None else ()

            want_farm = self.source_mode in ("farm", "variety")
            want_stream = self.source_mode in ("stream", "variety")

            if want_farm and has_report:
                # Farm Score is authoritative for the Farm axis. DT and NC are
                # equivalent speed-up mod buckets for this purpose and are both
                # excluded, including combinations such as HDDT / HDNC.
                if has_beatmaps and has_stream:
                    farm_rows = conn.execute(f"""
                        SELECT f.beatmap_id, f.artist, f.title, f.version, f.sr,
                               f.hit_length, f.passcount, f.farm_score, f.quadrant,
                               f.forgiveness, f.max_pp,
                               COALESCE(sf.stream_score, 0.0) AS stream_score,
                               COALESCE(sf.median_stream_bpm, 0.0) AS stream_bpm,
                               1 AS is_farm,
                               CASE WHEN LOWER(COALESCE(b.status,''))='ranked'
                                          AND f.hit_length >= ?
                                          AND COALESCE(sf.stream_score,0) >= ?
                                          AND COALESCE(sf.median_stream_bpm,0) >= ?
                                    THEN 1 ELSE 0 END AS is_stream
                        FROM farm_report f
                        LEFT JOIN beatmaps b USING(beatmap_id)
                        LEFT JOIN stream_features sf USING(beatmap_id)
                        WHERE f.sr BETWEEN ? AND ?
                          AND LOWER(COALESCE(b.status,'')) = 'ranked'
                          AND f.hit_length <= ?
                          AND f.passcount >= ?
                          AND COALESCE(f.dominant_mods, '') NOT LIKE '%DT%'
                          AND COALESCE(f.dominant_mods, '') NOT LIKE '%NC%'
                          {age_clause}
                        ORDER BY f.farm_score DESC
                        LIMIT ?
                    """, (
                        self.stream_min_length, self.stream_min_score,
                        self.stream_min_bpm, self.sr_min, self.sr_max,
                        self.max_length, query_min_passcount, *age_params,
                        candidate_limit,
                    )).fetchall()
                elif has_beatmaps:
                    farm_rows = conn.execute(f"""
                        SELECT f.beatmap_id, f.artist, f.title, f.version, f.sr,
                               f.hit_length, f.passcount, f.farm_score, f.quadrant,
                               f.forgiveness, f.max_pp,
                               0.0 AS stream_score, 0.0 AS stream_bpm,
                               1 AS is_farm, 0 AS is_stream
                        FROM farm_report f
                        LEFT JOIN beatmaps b USING(beatmap_id)
                        WHERE f.sr BETWEEN ? AND ?
                          AND LOWER(COALESCE(b.status,'')) = 'ranked'
                          AND f.hit_length <= ?
                          AND f.passcount >= ?
                          AND COALESCE(f.dominant_mods, '') NOT LIKE '%DT%'
                          AND COALESCE(f.dominant_mods, '') NOT LIKE '%NC%'
                          {age_clause}
                        ORDER BY f.farm_score DESC
                        LIMIT ?
                    """, (
                        self.sr_min, self.sr_max, self.max_length,
                        query_min_passcount, *age_params, candidate_limit,
                    )).fetchall()
                else:
                    # A Farm report without the authoritative beatmap status
                    # table cannot prove Ranked eligibility. Refuse to guess.
                    farm_rows = []
                    log.error(
                        "ranked Farm mode requires beatmaps.status in %s",
                        self.db_path,
                    )
                farm_ranked = [self._map_from_row(r) for r in farm_rows]
                source_parts.append("farm")

            elif want_farm and has_beatmaps:
                # Compatibility fallback only for databases that genuinely have
                # no farm_report yet. These are not labelled Farm maps.
                fallback_rows = conn.execute(f"""
                    SELECT b.beatmap_id, b.artist, b.title, b.version, b.sr,
                           b.hit_length, b.passcount,
                           0.0 AS farm_score, '' AS quadrant,
                           0.0 AS forgiveness, 0.0 AS max_pp,
                           0.0 AS stream_score, 0.0 AS stream_bpm,
                           0 AS is_farm, 0 AS is_stream
                    FROM beatmaps b
                    WHERE b.sr BETWEEN ? AND ?
                      AND b.hit_length <= ?
                      AND b.passcount >= ?
                      AND LOWER(COALESCE(b.status,'')) = 'ranked'
                      {age_clause}
                    ORDER BY b.passcount DESC
                    LIMIT ?
                """, (
                    self.sr_min, self.sr_max, self.max_length,
                    query_min_passcount, *age_params, candidate_limit,
                )).fetchall()
                farm_ranked = [self._map_from_row(r) for r in fallback_rows]
                source_parts.append("popularity-fallback")

            if want_stream and has_beatmaps and has_stream:
                # Stream Score is a separate structural axis. It is deliberately
                # not blended numerically with Farm Score; the live pool reserves
                # source slots for each ranking instead.
                stream_rows = conn.execute(f"""
                    SELECT b.beatmap_id, b.artist, b.title, b.version, b.sr,
                           b.hit_length, b.passcount,
                           COALESCE(f.farm_score, 0.0) AS farm_score,
                           COALESCE(f.quadrant, '') AS quadrant,
                           COALESCE(f.forgiveness, 0.0) AS forgiveness,
                           COALESCE(f.max_pp, 0.0) AS max_pp,
                           sf.stream_score AS stream_score,
                           COALESCE(sf.median_stream_bpm, 0.0) AS stream_bpm,
                           CASE WHEN f.beatmap_id IS NOT NULL
                                      AND COALESCE(f.dominant_mods,'') NOT LIKE '%DT%'
                                      AND COALESCE(f.dominant_mods,'') NOT LIKE '%NC%'
                                THEN 1 ELSE 0 END AS is_farm,
                           1 AS is_stream
                    FROM beatmaps b
                    JOIN stream_features sf USING(beatmap_id)
                    LEFT JOIN farm_report f USING(beatmap_id)
                    WHERE LOWER(COALESCE(b.status,''))='ranked'
                      AND b.sr BETWEEN ? AND ?
                      AND b.hit_length BETWEEN ? AND ?
                      AND COALESCE(b.passcount,0) >= ?
                      AND sf.stream_score >= ?
                      AND COALESCE(sf.median_stream_bpm,0) >= ?
                      {age_clause}
                    ORDER BY sf.stream_score DESC,
                             sf.max_stream_notes DESC,
                             sf.long_stream_note_ratio DESC,
                             COALESCE(b.passcount,0) DESC
                    LIMIT ?
                """, (
                    self.sr_min, self.sr_max, self.stream_min_length,
                    self.max_length, query_min_passcount, self.stream_min_score,
                    self.stream_min_bpm, *age_params, candidate_limit,
                )).fetchall()
                stream_ranked = [self._map_from_row(r) for r in stream_rows]
                source_parts.append("stream")

            if not has_report and not has_beatmaps:
                log.error(
                    "farm database %s has neither farm_report nor beatmaps",
                    self.db_path,
                )
        finally:
            conn.close()

        if self._passcount_fallback_enabled():
            farm_ranked = [m for m in farm_ranked if m.beatmap_id not in self._runtime_excluded]
            stream_ranked = [m for m in stream_ranked if m.beatmap_id not in self._runtime_excluded]
        self.maps = self._balanced_candidates(farm_ranked, stream_ranked)

        self._maybe_relax_passcount()

        relaxed_count = (None if hasattr(self, '_query_min_passcount') else
                         self._maybe_relax_high_star_window(len(self.maps)))
        if relaxed_count is not None:
            return relaxed_count

        farm_count = sum(1 for m in self.maps if m.is_farm)
        stream_count = sum(1 for m in self.maps if m.is_stream)
        both_count = sum(1 for m in self.maps if m.is_farm and m.is_stream)
        source = "+".join(source_parts) if source_parts else "database"
        log.info(
            "pool loaded from %s: %d maps (farm=%d stream=%d both=%d), "
            "mode=%s, %.2f-%.2f*, <=%ds, >=%d passes, max_age=%s",
            source, len(self.maps), farm_count, stream_count, both_count,
            self.source_mode_label(), self.sr_min, self.sr_max,
            self.max_length,
            query_min_passcount if hasattr(self, '_query_min_passcount') else self.effective_min_passcount(),
            self.max_map_age_label(),
        )
        if farm_count:
            top_farm = [m for m in self.maps if m.is_farm][:5]
            log.info(
                "  top farm scores in live pool: %s",
                ", ".join(f"{m.farm_score:.2f}" for m in top_farm),
            )
        if stream_count:
            top_stream = sorted(
                (m for m in self.maps if m.is_stream),
                key=lambda m: m.stream_score,
                reverse=True,
            )[:5]
            log.info(
                "  top stream scores in live pool: %s",
                ", ".join(f"{m.stream_score:.2f}" for m in top_stream),
            )
        return len(self.maps)

    def source_mode_label(self) -> str:
        if self.loved_mode:
            return "loved"
        if self.source_mode == "farm":
            return "farm"
        if self.source_mode == "stream":
            return "stream"
        if self.source_mode == 'jump':
            return 'jump (relaxed)' if self._jump_fallback_active else 'jump'
        return (
            f"variety {self.farm_share * 100:.0f}/{self.stream_share * 100:.0f}"
        )

    def set_source_mix(self, farm_share: float, stream_share: float) -> int:
        """Atomically change future map-selection source targets.

        Shares are normalized weights, so callers may pass either 0.7/0.3 or
        70/30.  Pure 100/0 and 0/100 become hard Farm-only / Stream-only modes.
        Existing lazer playlist items are untouched; only later selections use
        the new source mode.  If the requested mode has zero eligible maps, the
        previous mode and live pool are restored.
        """
        if self.loved_mode:
            # Loved is a database boundary, not a source mix. Public source
            # commands must never make a Loved room query ranked Farm data.
            return 0

        farm_share = max(0.0, float(farm_share))
        stream_share = max(0.0, float(stream_share))
        total = farm_share + stream_share
        if total <= 0.0:
            return 0
        farm_share /= total
        stream_share /= total

        if stream_share <= 1e-9:
            new_mode = "farm"
            farm_share, stream_share = 1.0, 0.0
        elif farm_share <= 1e-9:
            new_mode = "stream"
            farm_share, stream_share = 0.0, 1.0
        else:
            new_mode = "variety"

        old_mode = self.source_mode
        old_farm_share = self.farm_share
        old_stream_share = self.stream_share
        old_stream_enabled = self.stream_enabled
        old_maps = self.maps

        self.source_mode = new_mode
        self.farm_share = farm_share
        self.stream_share = stream_share
        self.stream_enabled = new_mode != "farm"
        try:
            n = self.reload()
        except sqlite3.Error:
            n = 0

        if n <= 0:
            self.source_mode = old_mode
            self.farm_share = old_farm_share
            self.stream_share = old_stream_share
            self.stream_enabled = old_stream_enabled
            self.maps = old_maps
            log.warning(
                "source mode %s had no eligible maps; keeping %s",
                new_mode, old_mode,
            )
            return 0
        return n

    def set_farm_mode(self) -> int:
        return self.set_source_mix(1.0, 0.0)

    def set_jump_mode(self) -> int:
        if self.loved_mode or getattr(self, 'mania_mode', False):
            return 0
        old = (self.source_mode, self.stream_enabled, self.farm_share, self.stream_share, self.maps)
        self.source_mode = 'jump'
        self.stream_enabled = False
        self.farm_share = self.stream_share = 0.0
        try:
            count = self.reload()
        except sqlite3.Error:
            count = 0
        if not count:
            self.source_mode, self.stream_enabled, self.farm_share, self.stream_share, self.maps = old
        else:
            self._jump_fallback_active = False
        return count

    def set_stream_mode(self) -> int:
        return self.set_source_mix(0.0, 1.0)

    def set_variety_mode(self, farm_share: float = 0.50,
                         stream_share: float = 0.50) -> int:
        return self.set_source_mix(farm_share, stream_share)

    def set_max_map_age_years(self, years: float) -> int:
        """Atomically change the ranked-date limit for future selections.

        Existing lazer playlist items are owned by LobbyBot and are not
        touched here. A failed/empty reload restores both the previous limit
        and the previous known-good candidate list.
        """
        value = float(years)
        if value < 0.0:
            raise ValueError("max map age cannot be negative")

        old_value = self.max_map_age_years
        old_maps = self.maps
        self.max_map_age_years = value
        try:
            n = self.reload()
        except sqlite3.Error as e:
            self.max_map_age_years = old_value
            self.maps = old_maps
            log.error(
                "could not set max map age to %s: %s; keeping %s",
                "off" if value <= 0.0 else f"{value:g}y", e,
                "off" if old_value <= 0.0 else f"{old_value:g}y",
            )
            return 0

        if n <= 0:
            self.max_map_age_years = old_value
            self.maps = old_maps
            log.warning(
                "max map age %s left no eligible maps; keeping %s",
                "off" if value <= 0.0 else f"{value:g}y",
                "off" if old_value <= 0.0 else f"{old_value:g}y",
            )
            return 0
        return n

    def reset_session_pool(self, sr_min: float, sr_max: float,
                           max_length: int | float) -> int:
        """Restore variety and the baseline window in one atomic reload.

        A specialised source may have no maps at the baseline difficulty, so
        neither changing its window first nor changing its source first is
        sufficient. Loved remains a database boundary. Keep repeat history and
        unavailable-map exclusions across sessions.
        """
        updates = {}
        if getattr(self, "mania_mode", False):
            updates = {"mania_source_mode": "variety", "source_mode": "variety"}
        elif not self.loved_mode:
            updates = dict(source_mode="variety", farm_share=0.5,
                           stream_share=0.5, stream_enabled=True)
        previous = {key: getattr(self, key) for key in
                    (*updates, "sr_min", "sr_max", "max_length", "maps")}
        self.__dict__.update(updates)
        try:
            count = self.set_window(sr_min, sr_max, max_length)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self.__dict__.update(previous)
            log.warning("could not reset session pool; keeping current pool: %s", exc)
            return 0
        if count <= 0:
            self.__dict__.update(previous)
        return count

    def set_window(
            self, sr_min: float, sr_max: float, max_length: int | float
    ) -> int:
        """Atomically try an SR window plus a map-length ceiling.

        AutoDiff may temporarily allow longer maps before broadening the star
        range. A failed candidate restores SR, max length, and the prior live
        map list together, so an empty trial can never leak relaxed settings.
        """
        sr_min, sr_max = float(sr_min), float(sr_max)
        max_length = int(round(float(max_length)))
        max_length = max(0 if self.loved_mode else 1, max_length)
        if self.real_max_length is not None:
            max_length = min(max_length, self.real_max_length)
        if sr_min >= sr_max:
            log.warning("rejected invalid SR range %.2f-%.2f", sr_min, sr_max)
            return 0

        old_min, old_max = self.sr_min, self.sr_max
        old_length = self.max_length
        old_maps = self.maps

        self.sr_min, self.sr_max = sr_min, sr_max
        self.max_length = max_length

        try:
            n = self.reload()
        except sqlite3.Error as e:
            self.sr_min, self.sr_max = old_min, old_max
            self.max_length = old_length
            self.maps = old_maps
            log.error(
                "could not load candidate window %.2f-%.2f* <=%ds: %s; "
                "keeping %.2f-%.2f* <=%ds",
                sr_min, sr_max, max_length, e,
                old_min, old_max, old_length,
            )
            return 0

        if n <= 0:
            self.sr_min, self.sr_max = old_min, old_max
            self.max_length = old_length
            self.maps = old_maps
            log.warning(
                "no eligible maps in candidate window %.2f-%.2f* <=%ds; "
                "keeping previous %.2f-%.2f* <=%ds pool (%d maps)",
                sr_min, sr_max, max_length,
                old_min, old_max, old_length, len(old_maps),
            )
            return 0

        return n

    def set_range(self, sr_min: float, sr_max: float) -> int:
        """Try a new SR range without destroying a known-good live pool."""
        return self.set_window(sr_min, sr_max, self.max_length)

    def get_map(self, beatmap_id: int) -> PoolMap | None:
        """Resolve any known Farm or Stream beatmap regardless of active window.

        Playlist reconciliation may need a map after AutoDiff has changed the
        live pool. Stream-only maps therefore must be resolvable from beatmaps +
        stream_features, not only from farm_report.
        """
        beatmap_id = int(beatmap_id)

        for m in self.maps:
            if int(m.beatmap_id) == beatmap_id:
                return m

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if self.loved_mode:
                if not self._has_table(conn, "beatmaps"):
                    return None
                row = conn.execute("""
                    SELECT b.beatmap_id, b.artist, b.title, b.version, b.sr,
                           b.hit_length, b.passcount,
                           0.0 AS farm_score, '' AS quadrant,
                           0.0 AS forgiveness, 0.0 AS max_pp,
                           0.0 AS stream_score, 0.0 AS stream_bpm,
                           0 AS is_farm, 0 AS is_stream, 1 AS is_loved
                    FROM beatmaps b
                    WHERE b.beatmap_id = ?
                      AND LOWER(COALESCE(b.status,'')) = 'loved'
                      AND LOWER(COALESCE(b.mode,'')) = 'osu'
                      AND COALESCE(b.convert,0) = 0
                    LIMIT 1
                """, (beatmap_id,)).fetchone()
                return self._map_from_row(row) if row is not None else None

            row = None
            has_beatmaps = self._has_table(conn, "beatmaps")
            has_report = self._has_report(conn)
            has_stream = self._has_table(conn, "stream_features")

            if has_beatmaps:
                farm_join = "LEFT JOIN farm_report f USING(beatmap_id)" if has_report else ""
                stream_join = "LEFT JOIN stream_features sf USING(beatmap_id)" if has_stream else ""
                farm_fields = (
                    "COALESCE(f.farm_score,0.0) AS farm_score, "
                    "COALESCE(f.quadrant,'') AS quadrant, "
                    "COALESCE(f.forgiveness,0.0) AS forgiveness, "
                    "COALESCE(f.max_pp,0.0) AS max_pp, "
                    "CASE WHEN f.beatmap_id IS NOT NULL THEN 1 ELSE 0 END AS is_farm"
                    if has_report else
                    "0.0 AS farm_score, '' AS quadrant, 0.0 AS forgiveness, "
                    "0.0 AS max_pp, 0 AS is_farm"
                )
                stream_fields = (
                    "COALESCE(sf.stream_score,0.0) AS stream_score, "
                    "COALESCE(sf.median_stream_bpm,0.0) AS stream_bpm, "
                    "CASE WHEN LOWER(COALESCE(b.status,''))='ranked' "
                    f"AND b.hit_length >= {int(self.stream_min_length)} "
                    f"AND COALESCE(sf.stream_score,0) >= {float(self.stream_min_score)!r} "
                    f"AND COALESCE(sf.median_stream_bpm,0) >= {float(self.stream_min_bpm)!r} "
                    "THEN 1 ELSE 0 END AS is_stream"
                    if has_stream else
                    "0.0 AS stream_score, 0.0 AS stream_bpm, 0 AS is_stream"
                )
                sql = f"""
                    SELECT b.beatmap_id, b.artist, b.title, b.version, b.sr,
                           b.hit_length, b.passcount,
                           {farm_fields}, {stream_fields}
                    FROM beatmaps b
                    {farm_join}
                    {stream_join}
                    WHERE b.beatmap_id = ?
                      AND LOWER(COALESCE(b.status,'')) = 'ranked'
                    LIMIT 1
                """
                row = conn.execute(sql, (beatmap_id,)).fetchone()

        finally:
            conn.close()

        return self._map_from_row(row) if row is not None else None

    @staticmethod
    def _rating_weight(good: int, bad: int) -> float:
        """Return a strictly-positive selection weight from human feedback.

        Neutral/unrated maps have weight 1.0. One bad vote is only a mild
        penalty. The second and later bad votes compound much more strongly,
        which makes repeatedly disliked maps rare without ever banning them.
        Good votes provide a gradual boost capped at 2.5x.

        Examples with the default constants:
            0 good / 0 bad -> 1.000x
            1 good / 0 bad -> 1.250x
            3 good / 0 bad -> 1.750x
            6+ good/ 0 bad -> 2.500x
            0 good / 1 bad -> 0.850x
            0 good / 2 bad -> 0.297x
            0 good / 3 bad -> 0.104x
            0 good / 4 bad -> 0.036x

        Good and bad signals combine multiplicatively, so a mixed map can
        recover with enough positive feedback while repeated bad votes still
        matter. The floor guarantees every eligible map remains selectable.
        """
        good = max(0, int(good or 0))
        bad = max(0, int(bad or 0))

        good_factor = min(
            RATING_GOOD_MAX_FACTOR,
            1.0 + RATING_GOOD_BOOST_PER_VOTE * good,
        )

        if bad <= 0:
            bad_factor = 1.0
        else:
            bad_factor = RATING_FIRST_BAD_FACTOR * (
                RATING_REPEAT_BAD_FACTOR ** (bad - 1)
            )

        return max(RATING_MIN_WEIGHT, good_factor * bad_factor)

    def _rating_tallies(self, beatmap_ids: list[int]) -> dict[int, tuple[int, int]]:
        """Read current .good/.bad tallies for candidate maps.

        The selection path only runs when the bot needs another playlist item,
        so a small SQLite read here is cheap and means feedback can influence
        the very next maps without maintaining another cache. Any DB/read
        problem degrades safely to neutral weights.
        """
        if self.ratings_reader is not None:
            return self.ratings_reader.tallies(beatmap_ids)
        if not self.ratings_db or not beatmap_ids:
            return {}

        ids = sorted({int(x) for x in beatmap_ids})
        placeholders = ",".join("?" for _ in ids)

        try:
            conn = sqlite3.connect(self.ratings_db, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='map_ratings'"
                ).fetchone()
                if not has_table:
                    return {}

                has_ignore_table = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='rating_ignored_users'"
                ).fetchone()

                if has_ignore_table:
                    policy_cols = {
                        str(row["name"])
                        for row in conn.execute(
                            "PRAGMA table_info(rating_ignored_users)"
                        ).fetchall()
                    }
                    if {"is_ignored", "accept_after"} <= policy_cols:
                        rows = conn.execute(
                            f"""
                            SELECT mr.beatmap_id,
                                   SUM(CASE WHEN mr.rating > 0 THEN 1 ELSE 0 END) AS good,
                                   SUM(CASE WHEN mr.rating < 0 THEN 1 ELSE 0 END) AS bad
                            FROM map_ratings mr
                            LEFT JOIN rating_ignored_users rp
                                   ON rp.user_id = mr.user_id
                            WHERE mr.beatmap_id IN ({placeholders})
                              AND (
                                  rp.user_id IS NULL
                                  OR (
                                      COALESCE(rp.is_ignored, 1)=0
                                      AND rp.accept_after IS NOT NULL
                                      AND mr.created_at IS NOT NULL
                                      AND mr.created_at >= rp.accept_after
                                  )
                              )
                            GROUP BY mr.beatmap_id
                            """,
                            ids,
                        ).fetchall()
                    else:
                        # Transitional compatibility with the first reversible
                        # ignore-list build: rows present there are simply
                        # excluded until RatingStore performs its migration.
                        rows = conn.execute(
                            f"""
                            SELECT mr.beatmap_id,
                                   SUM(CASE WHEN mr.rating > 0 THEN 1 ELSE 0 END) AS good,
                                   SUM(CASE WHEN mr.rating < 0 THEN 1 ELSE 0 END) AS bad
                            FROM map_ratings mr
                            LEFT JOIN rating_ignored_users ri
                                   ON ri.user_id = mr.user_id
                            WHERE mr.beatmap_id IN ({placeholders})
                              AND ri.user_id IS NULL
                            GROUP BY mr.beatmap_id
                            """,
                            ids,
                        ).fetchall()
                else:
                    # Compatibility with an older ratings.db during startup;
                    # RatingStore creates the ignore table moments later.
                    rows = conn.execute(
                        f"""
                        SELECT beatmap_id,
                               SUM(CASE WHEN rating > 0 THEN 1 ELSE 0 END) AS good,
                               SUM(CASE WHEN rating < 0 THEN 1 ELSE 0 END) AS bad
                        FROM map_ratings
                        WHERE beatmap_id IN ({placeholders})
                        GROUP BY beatmap_id
                        """,
                        ids,
                    ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.warning(
                "could not read rating weights from %s: %s; using neutral picks",
                self.ratings_db, e,
            )
            return {}

        return {
            int(r["beatmap_id"]): (int(r["good"] or 0), int(r["bad"] or 0))
            for r in rows
        }

    @staticmethod
    def duration_policy_from_config(cfg):
        """Validate the complete opt-in policy, never partially accept it."""
        if 'real_max_length' not in cfg:
            return None
        values = {}
        for key, default in (('real_max_length', 240),
                             ('preferred_min_length', 90),
                             ('preferred_max_length', 150)):
            value = cfg.get(key, default)
            if type(value) is not int:
                raise ValueError(f'{key} must be an integer number of seconds')
            values[key] = value
        lo, hi, hard = (values['preferred_min_length'],
                        values['preferred_max_length'], values['real_max_length'])
        if not (30 <= hard <= 600 and 0 <= lo <= hi <= hard):
            raise ValueError('duration policy needs 0 <= preferred min <= preferred max <= hard max (30–600s)')
        share = cfg.get('preferred_length_share', 0.80)
        if isinstance(share, bool) or not isinstance(share, (int, float)) or not math.isfinite(share) or not 0 < share < 1:
            raise ValueError('preferred_length_share must be a finite number strictly between 0 and 1')
        values['preferred_length_share'] = float(share)
        return values

    def configure_duration_policy(self, cfg):
        """Atomically load policy and candidates; roll back an empty/failed trial."""
        policy = self.duration_policy_from_config(cfg)
        if policy is None:
            return len(self.maps)
        previous = dict(self.__dict__)
        self.__dict__.update(policy)
        self.max_length = policy['real_max_length']
        try:
            count = self.reload()
            if count <= 0:
                raise ValueError('duration policy left no eligible maps')
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(previous)
            raise
        return count

    def _is_preferred_duration(self, item: PoolMap) -> bool:
        if self.source_mode == 'jump':
            return self.jump_preferred_min_length <= item.hit_length < self.jump_preferred_max_length
        return self.preferred_min_length <= item.hit_length <= self.preferred_max_length

    def configure_jump_policy(self, cfg):
        """Separate, soft Jump preferences; never alter structural eligibility."""
        defaults = dict(jump_preferred_min_length=0, jump_preferred_max_length=90,
                        jump_preferred_length_share=.80, jump_preferred_ar_min=9.0,
                        jump_preferred_ar_max=10.0, jump_preferred_ar_boost=1.5)
        values = {key: cfg.get(key, default) for key, default in defaults.items()}
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{key} must be a finite number')
        if (type(values['jump_preferred_min_length']) is not int
                or type(values['jump_preferred_max_length']) is not int
                or not 0 <= values['jump_preferred_min_length'] <= values['jump_preferred_max_length'] <= 600
                or not 0 < values['jump_preferred_length_share'] < 1
                or not 0 <= values['jump_preferred_ar_min'] <= values['jump_preferred_ar_max'] <= 11
                or not 1 <= values['jump_preferred_ar_boost'] <= 3):
            raise ValueError('invalid Jump length/AR preference')
        self.__dict__.update(values)

    def _jump_weight(self, item):
        if self.source_mode != 'jump':
            return 1.0
        strength = item.jump_score or item.jump_relaxed_score
        weight = .5+max(0.0, min(1.0, strength))
        ar = item.approach_rate
        if isinstance(ar, (int, float)) and not isinstance(ar, bool) and math.isfinite(ar) and 0 <= ar <= 10:
            # DT preference must use perceived AR, not the source NM setting.
            if item.is_dt:
                preempt = 1200+120*(5-ar) if ar < 5 else 1200-150*(ar-5)
                preempt /= item.clock_rate
                ar = 5+(1200-preempt)/(120 if preempt > 1200 else 150)
            if self.jump_preferred_ar_min <= ar <= self.jump_preferred_ar_max:
                weight *= self.jump_preferred_ar_boost
        return weight

    def _bounded_jump_candidates(self, maps):
        """Honor pool_limit without permanently discarding every lower score.

        Preserve strong-before-relaxed tiers. When a tier exceeds capacity,
        sample without replacement; every qualifying member can enter a future
        reload. Actual pick weights/preferences still apply during selection.
        """
        strong = [m for m in maps if m.jump_score > 0]
        relaxed = [m for m in maps if m.jump_score <= 0 and m.jump_relaxed_score > 0]
        count = max(0, self.limit)
        if len(strong) >= count:
            return random.sample(strong, count) if len(strong) > count else strong
        remaining = count-len(strong)
        return strong+(random.sample(relaxed, remaining) if len(relaxed) > remaining else relaxed)

    def _duration_candidates(self, candidates):
        """Soft bucket bias inside an already eligible/fresh style tier.

        Never treat preferred_min_length as an eligibility minimum. Shorter
        and longer candidates share the occasional outside-range bucket.
        Keep human-feedback weighting inside the selected duration bucket.
        """
        if self.real_max_length is None and self.source_mode != 'jump':
            return candidates
        hard = self.real_max_length if self.real_max_length is not None else self.max_length
        candidates = [m for m in candidates if m.hit_length <= hard]
        preferred = [m for m in candidates if self._is_preferred_duration(m)]
        outside = [m for m in candidates if not self._is_preferred_duration(m)]
        if not preferred or not outside:
            return candidates
        share = self.jump_preferred_length_share if self.source_mode == 'jump' else self.preferred_length_share
        return preferred if random.random() < share else outside

    def _selection_weight(self, item: PoolMap, good: int, bad: int) -> float:
        """Return the final pick weight for one map.

        Subclasses may add bounded, mode-specific evidence while preserving
        the existing human-feedback factor and eligibility rules.
        """
        return self._rating_weight(good, bad)

    @staticmethod
    def _passcount_value(item) -> float:
        """Missing/invalid popularity evidence must never break selection."""
        try:
            value = float(item.passcount)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return 0.0
        return value if math.isfinite(value) and value > 0 else 0.0

    @classmethod
    def _passcount_reference(cls, candidates) -> float:
        return max((cls._passcount_value(item) for item in candidates), default=0.0)

    @classmethod
    def _passcount_weight(cls, item, maximum: float) -> float:
        if maximum <= 0 or not math.isfinite(maximum):
            return 1.0
        return 1.0 + PASSCOUNT_MAX_BOOST * min(cls._passcount_value(item) / maximum, 1.0)

    def _weighted_pick(self, candidates: list[PoolMap], *, passcount_max=None) -> PoolMap:
        """Choose among eligible candidates using style strength and feedback."""
        # Normalize against this eligible/fresh pool before duration, style,
        # NM/DT or native/convert sub-buckets. Recomputed on every selection;
        # never use an unrelated catalog-wide maximum or add an eligibility gate.
        if passcount_max is None:
            passcount_max = self._passcount_reference(candidates)
        candidates = self._preferred_passcount_candidates(candidates)
        candidates = self._duration_candidates(candidates)
        tallies = self._rating_tallies([m.beatmap_id for m in candidates])
        weights: list[float] = []

        for m in candidates:
            good, bad = tallies.get(int(m.beatmap_id), (0, 0))
            weights.append(self._selection_weight(m, good, bad)
                           * self._passcount_weight(m, passcount_max) * self._jump_weight(m))

        pick = random.choices(candidates, weights=weights, k=1)[0]
        good, bad = tallies.get(int(pick.beatmap_id), (0, 0))
        weight = (self._selection_weight(pick, good, bad) * self._passcount_weight(pick, passcount_max)
                  * self._jump_weight(pick))
        if good or bad:
            log.debug(
                "rating-weighted pick: %s | %d good / %d bad | weight %.3fx",
                pick.label, good, bad, weight,
            )
        return pick

    def exclude_beatmap(self, beatmap_id: int) -> None:
        beatmap_id = int(beatmap_id)
        if not hasattr(self, '_download_excluded'):
            self._download_excluded = set()
        self._download_excluded.add(beatmap_id)
        self._runtime_excluded.add(beatmap_id)
        log.warning("runtime-excluded beatmap %s from future picks", beatmap_id)

    def exclude_beatmaps(self, beatmap_ids) -> None:
        for beatmap_id in beatmap_ids or []:
            try:
                bid = int(beatmap_id)
                if not hasattr(self, '_download_excluded'):
                    self._download_excluded = set()
                self._download_excluded.add(bid)
                self._runtime_excluded.add(bid)
            except (TypeError, ValueError):
                continue
        if beatmap_ids:
            log.warning(
                "runtime-excluded %d beatmap(s) from an unavailable set",
                len(self._runtime_excluded),
            )

    def apply_operator_exclusions(self, ids):
        """Keep download quarantine separate so unbanning cannot clear it."""
        ids = set(ids)
        previous = getattr(self, '_operator_excluded', set())
        if ids == previous:
            return False
        self._runtime_excluded = ((self._runtime_excluded - previous) | ids
                                  | getattr(self, '_download_excluded', set()))
        self._operator_excluded = ids
        # Refresh even when additions leave no eligible maps. Never roll back
        # an operator ban just because a style/window has become empty.
        try:
            self.reload()
        except Exception:
            log.exception('Pool reload failed after exclusion update; bans remain enforced')
        return True

    @staticmethod
    def _song_key(m: PoolMap) -> str:
        """Return a normalized artist/title key for same-song spacing."""
        artist = " ".join(unicodedata.normalize('NFKC', m.artist or '').casefold().split())
        title = " ".join(unicodedata.normalize('NFKC', m.title or '').casefold().split())
        # Length/speed labels describe another edit of the same song. Preserve
        # remix names and other meaningful title text rather than guessing.
        original = title
        suffix = r"\s*\((?:tv (?:size|edit)|(?:cut|short) ver(?:sion)?\.?|sped up(?: & cut)? ver(?:sion)?\.?)\)$"
        while re.search(suffix, title):
            title = re.sub(suffix, '', title).strip()
        return f"{artist}\n{title or original}"

    def record_song_play(self, item: PoolMap) -> None:
        """Record a completed song, independently of speculative queue picks."""
        try:
            duration = float(item.hit_length)
        except (TypeError, ValueError, AttributeError):
            duration = 120.0
        if not math.isfinite(duration) or duration <= 0:
            duration = 120.0
        self._song_rounds = (getattr(self, '_song_rounds', []) + [
            (self._song_key(item), duration)
        ])[-120:]

    def _song_cooldown_rounds(self) -> int:
        """Estimate an hour in rounds, with 25 seconds for lobby/countdown time."""
        rounds = getattr(self, '_song_rounds', [])
        typical = statistics.median([duration for _key, duration in rounds[-20:]]) if rounds else 120.0
        return max(4, min(120, math.ceil(3600 / (typical + 25))))

    def _song_spacing_history(self) -> list[str]:
        rounds = getattr(self, '_song_rounds', [])
        played = [key for key, _duration in rounds[-self._song_cooldown_rounds():]]
        # Include picks that have not completed yet, so another difficulty
        # cannot slip into the current/pre-download queue before its first play.
        return played + self._recent_songs[-4:]

    def _space_song_candidates(self, candidates: list[PoolMap]) -> list[PoolMap]:
        """Prefer songs outside the cooldown; relax oldest history if necessary."""
        if not candidates:
            return []
        history = self._song_spacing_history()
        for depth in range(len(history), -1, -1):
            avoid = set(history[-depth:]) if depth else set()
            spaced = [item for item in candidates if self._song_key(item) not in avoid]
            if spaced:
                if depth < len(history):
                    log.debug('same-song cooldown relaxed to %d history entries for a small eligible pool', depth)
                return spaced
        return []

    def next_map(self) -> PoolMap | None:
        """Pick a map while spacing exact maps and same-song difficulties.

        Artist/title families are spaced by approximately an hour of completed
        rounds, plus the current/pre-download selections. Exact-map avoidance
        is relaxed first; small pools then relax the oldest song history only
        as needed. This is an estimated round cooldown, not a wall-clock timer.
        """
        if self.source_mode == 'jump' and not getattr(self, 'mania_mode', False):
            return self._next_jump_map()
        if not self.maps:
            return None

        exact_avoid = set(self._recent)
        song_history = self._song_spacing_history()
        song_avoid = set(song_history)

        # First choice: satisfy both exact-map history and same-song spacing.
        candidates = [
            m for m in self.maps
            if m.beatmap_id not in self._runtime_excluded
            and m.beatmap_id not in exact_avoid
            and self._song_key(m) not in song_avoid
        ]

        # If exact-map history is the only thing exhausting the pool, allow an
        # older exact map before sacrificing the much more visible song rule.
        if not candidates:
            candidates = [
                m for m in self.maps
                if m.beatmap_id not in self._runtime_excluded
                and self._song_key(m) not in song_avoid
            ]
            if candidates:
                log.warning(
                    "exact-map repeat avoidance relaxed because the active "
                    "pool is too small; song cooldown preserved"
                )

        # Only a small eligible pool may relax the oldest song history.
        if not candidates:
            candidates = self._space_song_candidates([
                m for m in self.maps if m.beatmap_id not in self._runtime_excluded
            ])

        if not candidates:
            candidates = [
                m for m in self.maps
                if m.beatmap_id not in self._runtime_excluded
            ]

        if not candidates:
            return None

        pick = self._weighted_pick(candidates)
        self._recent.append(pick.beatmap_id)
        self._recent_songs.append(self._song_key(pick))

        if len(self._recent) > max(len(self.maps) // 2, 5):
            self._recent.pop(0)
        if len(self._recent_songs) > 16:
            self._recent_songs = self._recent_songs[-16:]

        return pick

    def record_round(self, beatmap_id):
        self._round_history = (self._round_history + [int(beatmap_id)])[-10:]

    def reserve_playlist_maps(self, beatmap_ids):
        self._playlist_reserved = {int(bid) for bid in beatmap_ids}

    def _next_jump_map(self):
        blocked = set(self._round_history) | self._playlist_reserved | self._runtime_excluded
        fresh = [m for m in self.maps if m.beatmap_id not in blocked]
        strict = [m for m in fresh if m.jump_score > 0]
        candidates = strict or fresh
        self._jump_fallback_active = not bool(strict)
        if not candidates:
            available = [m for m in self.maps if m.beatmap_id not in
                         (self._playlist_reserved | self._runtime_excluded)]
            if not available:
                return None
            seen = {bid: i for i, bid in enumerate(self._round_history)}
            oldest = min(seen.get(m.beatmap_id, -1) for m in available)
            candidates = [m for m in available if seen.get(m.beatmap_id, -1) == oldest]
        pick = self._weighted_pick(self._space_song_candidates(candidates))
        self._recent = (self._recent + [pick.beatmap_id])[-max(len(self.maps) // 2, 5):]
        self._recent_songs = (self._recent_songs + [self._song_key(pick)])[-16:]
        return pick
