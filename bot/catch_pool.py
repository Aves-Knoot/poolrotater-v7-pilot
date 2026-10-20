"""Native NM Catch adapter over the accepted private C6 contract.

This is an offline research pool, not a signed/production catalog loader.
Unreviewed style/representation weights remain neutral.  The original selector
owns hard eligibility and deterministic cooldown relaxation.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

from .pool import MapPool, PoolMap
from .catch_catalog.c6_reader import Catalog, imported_rows
from .catch_catalog.c5_selection import Selector, rejection


@dataclass
class CatchMap(PoolMap):
    ruleset_id: int = 2
    native: bool = True
    file_md5: str = ''
    map_sha256: str = ''
    variant_id: str = ''
    calculator_id: str = ''
    beatmapset_id: int = 0
    mapper_id: int = 0
    song_key: str = ''
    provider_total_seconds: float | None = None
    provider_hit_seconds: float | None = None
    mandatory_scoring_seconds: float = 0.0
    audio_seconds: float | None = None
    covers: dict | None = None
    pp_scenarios: tuple = ()
    ranked_date: str | None = None
    status: str = ''
    selection_reference: dict | None = None

    @property
    def category(self):
        return 'catch-loved' if self.is_loved else 'catch-native-nm'


class CatchPool(MapPool):
    catch_mode = True
    research_only = True

    def __init__(self, db_path, *, purpose, catch_include_loved=False,
                 catch_min_length=0, seed=5, **kwargs):
        if type(catch_include_loved) is not bool:
            raise ValueError('Catch Loved inclusion requires an explicit boolean')
        if (type(catch_min_length) not in (int, float)
                or not math.isfinite(catch_min_length) or catch_min_length < 0):
            raise ValueError('Catch minimum length must be finite and nonnegative')
        # Counter aggregation is not qualified as a Catch filter/weight.
        if kwargs.get('min_passcount', 0) != 0 or kwargs.get('max_map_age_years', 0) != 0:
            raise ValueError('Catch pass-count/age filters are not qualified in C7')
        if kwargs.get('high_star_relax_enabled', False):
            raise ValueError('Catch hard constraints may not be relaxed')
        self.catch_include_loved = catch_include_loved
        self.catch_min_length = float(catch_min_length)
        path = Path(db_path)
        rows = self._load_rows(path, purpose)
        # Exhaust the independent reader before exposing any record to a pool.
        self._all_maps = {}
        selections = []
        for row in rows:
            item = self._from_row(row)
            self._all_maps[item.beatmap_id] = item
            selections.append(item.selection_reference)
        self._identities = {mid: (m.sr,m.file_md5,m.map_sha256,m.variant_id,m.calculator_id)
                            for mid,m in self._all_maps.items()}
        self.selector = Selector(selections, seed=seed, loved=catch_include_loved)
        self.last_selection = None
        self._base_random = self.selector.rng
        # Reuse the existing opt-in duration policy without changing reference
        # choices when it is disabled.  Weight buckets only within the final
        # hard-valid/cooldown-relaxed tier; no preference becomes a hard gate.
        class DurationRandom:
            def choices(_, options, weights, k):
                if self.real_max_length is not None:
                    preferred = [self._is_preferred_duration(self._all_maps[o['map_id']]) for o in options]
                    a = sum(w for w,p in zip(weights,preferred) if p)
                    b = sum(w for w,p in zip(weights,preferred) if not p)
                    if a and b:
                        share = self.preferred_length_share
                        weights = [w*(share/a if p else (1-share)/b) for w,p in zip(weights,preferred)]
                return self._base_random.choices(options,weights=weights,k=k)
        self.selector.rng = DurationRandom()
        kwargs.update(loved_mode=False, stream_enabled=False, min_passcount=0,
                      max_map_age_years=0, sparse_passcount_enabled=False)
        super().__init__(str(path), **kwargs)

    @staticmethod
    def _load_rows(path, purpose):
        if purpose != 'research':
            raise ValueError('Catch C6 catalogs are private offline research only')
        return Catalog(path, purpose=purpose).rows() if path.is_dir() else imported_rows(path)

    @staticmethod
    def _from_row(row):
        s, i, d, p = row['selection'], row['identity'], row['durations'], row['presentation']
        return CatchMap(
            beatmap_id=s['map_id'], artist=s['artist'], title=s['title'],
            version=s['version'], sr=s['stars'],
            hit_length=d['provider_total_seconds'] or d['provider_hit_seconds'] or s['duration'],
            farm_score=0., passcount=row['provider_metadata']['passcount'] or 0,
            is_loved=s['status'] == 'loved', map_origin='catch-native-nm',
            max_pp=s['ss_pp'] or 0., approach_rate=s['AR'],
            file_md5=i['file_md5'], map_sha256=s['map_sha256'],
            variant_id=s['variant_id'], calculator_id=s['calculator_id'],
            beatmapset_id=s['set_id'], mapper_id=s['mapper_id'], song_key=s['song_key'],
            provider_total_seconds=d['provider_total_seconds'],
            provider_hit_seconds=d['provider_hit_seconds'],
            mandatory_scoring_seconds=d['mandatory_scoring_seconds'],
            audio_seconds=d['audio_seconds'], covers=p['covers'],
            pp_scenarios=tuple(row['difficulty']['pp_scenarios']),
            ranked_date=s['ranked_date'], status=s['status'], selection_reference=s)

    def set_window(self, sr_min, sr_max, max_length):
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (sr_min,sr_max,max_length)):
            raise ValueError('Catch window must be finite')
        return super().set_window(sr_min,sr_max,max_length)

    def reload(self):
        if self.min_passcount != 0 or self.max_map_age_years != 0 or self.high_star_relax_enabled:
            raise ValueError('Catch counter/age/automatic-relaxation policy is unqualified')
        self.source_mode = 'variety'
        self.stream_enabled = False
        self.maps = [self._all_maps[s['map_id']] for s in self.selector.candidates(self.sr_min, self.sr_max)
                     if s['map_id'] not in self._runtime_excluded
                     and (not self.max_length or self._all_maps[s['map_id']].hit_length <= self.max_length)
                     and self._all_maps[s['map_id']].hit_length >= self.catch_min_length]
        return len(self.maps)

    def source_mode_label(self):
        return 'native Catch NM variety' + (' (Loved opt-in)' if self.catch_include_loved else '')

    def get_map(self, beatmap_id):
        m = self._all_maps.get(int(beatmap_id))
        if m is None or m.beatmap_id in self._runtime_excluded:
            return None
        return m if rejection(m.selection_reference, self.catch_include_loved) is None else None

    def get_variant_map(self, beatmap_id, mods):
        return self.get_map(beatmap_id) if mods == [] else None

    def next_map(self):
        # Prefer distinct visible maps. A finite sparse pool may repeat a
        # reserved map rather than leave the existing controller unable to
        # prequeue/start/advance. This relaxes only recency, never hard filters.
        available = [m.selection_reference for m in self.maps
                     if m.beatmap_id not in self._runtime_excluded]
        candidates = [s for s in available if s['map_id'] not in self._playlist_reserved]
        self.queue_repeat_unavoidable = bool(available and not candidates)
        candidates = candidates or available
        original_pool, original_stars = self.selector.pool, self.selector.stars
        try:
            self.selector.pool = sorted(candidates, key=lambda s: (s['stars'], s['map_id']))
            self.selector.stars = [s['stars'] for s in self.selector.pool]
            self.last_selection = self.selector.choose(self.sr_min, self.sr_max)
        finally:
            self.selector.pool, self.selector.stars = original_pool, original_stars
        if not self.last_selection['ok']:
            return None
        item = self._all_maps[self.last_selection['event']['map_id']]
        self._recent = (self._recent + [item.beatmap_id])[-20:]
        return item

    def set_variety_mode(self, *args):
        return self.reload()

    def set_farm_mode(self):
        raise ValueError('Catch style/farm labels are not qualified')

    set_stream_mode = set_farm_mode
    set_jump_mode = set_farm_mode

    def _selection_weight(self, item, good, bad):
        # C5/C6 baseline stays neutral; shared feedback is captured separately.
        return 1.

    def verify_variant(self, m, *, in_range=False):
        known = self.get_map(getattr(m, 'beatmap_id', 0))
        if (known is None or not isinstance(m, CatchMap) or m.ruleset_id != 2
                or not m.native or m.is_dt or m.is_mania or m.clock_rate != 1.
                or m.map_origin != 'catch-native-nm'
                or tuple(getattr(m,name) for name in ('sr','file_md5','map_sha256','variant_id','calculator_id'))
                   != self._identities.get(m.beatmap_id)
                or (in_range and (not self.sr_min <= m.sr <= self.sr_max
                                  or (self.max_length and m.hit_length > self.max_length)
                                  or m.hit_length < self.catch_min_length))):
            raise ValueError('unqualified/mismatched native Catch NM identity')
        return m


class ProductionCatchPool(CatchPool):
    """Signed-store production projection; never opens or relabels C6 research."""
    research_only = False

    def __init__(self, db_path, *, purpose, **kwargs):
        if purpose != 'production' or kwargs.get('catch_include_loved', False) is not False:
            raise ValueError('production Catch is native NM Ranked/Approved only')
        super().__init__(db_path, purpose=purpose, **kwargs)

    @staticmethod
    def _load_rows(path, purpose):
        if purpose != 'production':
            raise ValueError('production Catch purpose required')
        from .catch_catalog.production import rows
        return rows(path)
