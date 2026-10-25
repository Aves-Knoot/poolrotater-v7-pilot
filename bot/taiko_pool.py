"""Production Taiko NM selector backed by the compact verified catalog."""
from __future__ import annotations

from dataclasses import dataclass
import math
import sqlite3
from pathlib import Path

from .pool import MapPool, PoolMap
from poolrotater_host.catalog_taiko import (
    CALCULATOR, DIFFICULTY_VERSION, OFFICIAL_COMMIT, MAP_COLUMNS,
)


@dataclass
class TaikoMap(PoolMap):
    ruleset_id: int = 1
    is_taiko_convert: bool = False
    native: bool = True
    file_md5: str = ''
    source_sha256: str = ''
    taiko_nm_variant: str = ''
    calculator_id: str = ''
    difficulty_version: int = 0
    source_commit: str = ''
    beatmapset_id: int = 0
    background_url: str | None = None
    bpm_min: float | None = None
    bpm_max: float | None = None
    hit_count: int = 0
    big_hits: int = 0
    centre_hits: int = 0
    rim_hits: int = 0
    peak_hits_per_second: float = 0.0
    mean_hits_per_second: float = 0.0
    colour_change_fraction: float = 0.0
    maximum_mono_streak: int = 0
    status: str = ''

    @property
    def category(self):
        return 'taiko-convert-nm' if self.is_taiko_convert else 'taiko-native-nm'

    @property
    def native_nm_variant(self):
        return self.taiko_nm_variant

    @property
    def source_md5(self):
        return self.file_md5

    @property
    def original_mode(self):
        return 0 if self.is_taiko_convert else 1

    @property
    def pp_scenarios(self):
        return ((100, self.max_pp),)


class TaikoPool(MapPool):
    """Taiko ruleset selector for official NM charts and verified converts only."""
    taiko_mode = True

    def __init__(self, db_path, *, purpose='production', taiko_min_length=0,
                 taiko_include_converts=True, seed=5, **kwargs):
        if purpose != 'production':
            raise ValueError('Taiko production purpose is required')
        if type(taiko_include_converts) is not bool:
            raise ValueError('Taiko conversion inclusion requires an explicit boolean')
        if (type(taiko_min_length) not in (int, float) or not math.isfinite(taiko_min_length)
                or taiko_min_length < 0):
            raise ValueError('Taiko minimum length must be finite and nonnegative')
        if kwargs.get('loved_mode', False) or kwargs.get('stream_enabled', True) is not True:
            # Source modes carry no meaning for Taiko. Avoid accidental style/farm options.
            if kwargs.get('loved_mode', False):
                raise ValueError('Taiko Loved selection is not enabled')
        # These Standard population filters do not have qualified Taiko inputs;
        # override their inherited defaults below instead of leaking them.
        self.taiko_min_length = float(taiko_min_length)
        self.taiko_include_converts = taiko_include_converts
        self._all_taiko_maps = self._read_catalog(db_path)
        from .taiko_difficulty import read_calibration
        self._taiko_calibration = read_calibration(db_path)
        self._identities = {
            m.beatmap_id: (m.sr, m.file_md5, m.source_sha256, m.taiko_nm_variant,
                           m.calculator_id, m.difficulty_version, m.source_commit,
                           m.ruleset_id, m.map_origin, m.is_taiko_convert, m.native)
            for m in self._all_taiko_maps.values()
        }
        # MapPool's shared vote machinery, range setters, exclusions, song-spacing
        # history and sparse-pool handling remain in use. Taiko ignores unqualified
        # Standard pass-count/farm/stream policy and reloads from this projection.
        kwargs.update(loved_mode=False, stream_enabled=True, stream_share=0.5,
                      min_passcount=0, max_map_age_years=0, high_star_relax_enabled=False,
                      sparse_passcount_enabled=False)
        kwargs.pop("seed", None)
        super().__init__(str(db_path), **kwargs)
        self.source_mode = 'taiko-variety'
        self._taiko_rating_store = None
        if self.ratings_db and self.ratings_reader is None:
            from .scoped_ratings import ScopedRatingStore
            self._taiko_rating_store = ScopedRatingStore(
                self.ratings_db, initialize=False, ruleset_id=1)

    @staticmethod
    def _read_catalog(db_path):
        path = Path(db_path).resolve()
        if not path.is_file() or path.is_symlink():
            raise ValueError('Taiko catalog database is unavailable')
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        columns = tuple((r['name'], r['type'].upper()) for r in db.execute('PRAGMA table_info(taiko_maps)'))
        if columns != MAP_COLUMNS:
            db.close()
            raise ValueError('Taiko catalog schema mismatch')
        maps = {}
        try:
            for row in db.execute('SELECT * FROM taiko_maps ORDER BY beatmap_id'):
                item = dict(row)
                if (item['status'] not in ('ranked','approved') or item['original_mode'] not in (0,1)
                        or item['is_convert'] != int(item['original_mode'] == 0)
                        or item['calculator_id'] != CALCULATOR
                        or item['difficulty_version'] != DIFFICULTY_VERSION
                        or item['source_commit'] != OFFICIAL_COMMIT):
                    raise ValueError('Taiko catalog contains an unqualified row')
                converted = bool(item['is_convert'])
                m = TaikoMap(
                    beatmap_id=item['beatmap_id'], beatmapset_id=item['beatmapset_id'],
                    artist=item['artist'], title=item['title'], version=item['version'], sr=item['sr'],
                    hit_length=item['hit_length'], farm_score=0.0, passcount=0,
                    max_pp=item['max_pp'], is_taiko_convert=converted,
                    is_mania_convert=False,
                    map_origin='taiko-convert-nm' if converted else 'taiko-native-nm',
                    ruleset_id=1, native=not converted, file_md5=item['source_md5'],
                    source_sha256=item['source_sha256'], taiko_nm_variant=item['taiko_nm_variant'],
                    calculator_id=item['calculator_id'], difficulty_version=item['difficulty_version'],
                    source_commit=item['source_commit'], background_url=item['background_url'],
                    bpm_min=item['bpm_min'], bpm_max=item['bpm_max'], hit_count=item['hit_count'],
                    big_hits=item['big_hits'], centre_hits=item['centre_hits'], rim_hits=item['rim_hits'],
                    peak_hits_per_second=item['peak_hits_per_second'],
                    mean_hits_per_second=item['mean_hits_per_second'],
                    colour_change_fraction=item['colour_change_fraction'],
                    maximum_mono_streak=item['maximum_mono_streak'], status=item['status'],
                )
                if item['beatmap_id'] in maps:
                    raise ValueError('duplicate Taiko beatmap identity')
                maps[item['beatmap_id']] = m
        finally:
            db.close()
        if not maps:
            raise ValueError('Taiko catalog has no qualified maps')
        return maps

    def reload(self):
        maps = []
        excluded = getattr(self, '_runtime_excluded', set())
        for item in self._all_taiko_maps.values():
            if item.beatmap_id in excluded:
                continue
            if item.is_taiko_convert and not self.taiko_include_converts:
                continue
            if not self.sr_min <= item.sr <= self.sr_max:
                continue
            if self.max_length and item.hit_length > self.max_length:
                continue
            if item.hit_length < self.taiko_min_length:
                continue
            maps.append(item)
        self.maps = maps[:self.limit]
        self.source_mode = 'taiko-variety'
        return len(self.maps)

    def target_sr_for_pp(self, taiko_total_pp, half_width=0.4, bias=0.0, step=0.05):
        from .taiko_difficulty import calibrated_target_sr
        return calibrated_target_sr(taiko_total_pp, self._taiko_calibration,
                                   half_width=half_width, bias=bias, step=step)

    def auto_range_for_pp(self, taiko_total_pp, half_width=0.4, step=0.05, bias=0.0):
        from .taiko_difficulty import range_for_pp
        return range_for_pp(taiko_total_pp, self._taiko_calibration,
                            half_width=half_width, step=step, bias=bias)

    def _rating_tallies(self, beatmap_ids):
        if self.ratings_reader is not None:
            return self.ratings_reader.tallies(beatmap_ids)
        store = self._taiko_rating_store
        if store is None or not beatmap_ids:
            return {}
        raw_ids = sorted({int(value) for value in beatmap_ids})
        key_to_id = {store._rating_key(value): value for value in raw_ids}
        placeholders = ','.join('?' for _ in key_to_id)
        conn = None
        try:
            conn = store._conn()
            policy = store._active_rating_policy_sql('mr', 'rp')
            rows = conn.execute(
                f"""SELECT mr.beatmap_id,
                           SUM(CASE WHEN mr.rating > 0 THEN 1 ELSE 0 END) AS good,
                           SUM(CASE WHEN mr.rating < 0 THEN 1 ELSE 0 END) AS bad
                    FROM map_ratings mr
                    LEFT JOIN rating_ignored_users rp ON rp.user_id=mr.user_id
                    WHERE mr.beatmap_id IN ({placeholders}) AND {policy}
                    GROUP BY mr.beatmap_id""",
                tuple(key_to_id)).fetchall()
            return {key_to_id[int(row['beatmap_id'])]:
                    (int(row['good'] or 0), int(row['bad'] or 0)) for row in rows
                    if int(row['beatmap_id']) in key_to_id}
        except (sqlite3.Error, KeyError, TypeError, ValueError):
            return {}
        finally:
            if conn is not None:
                conn.close()

    def source_mode_label(self):
        return 'Taiko NM variety' + (' with qualified converts' if self.taiko_include_converts else ' native only')

    def get_map(self, beatmap_id):
        try:
            map_id = int(beatmap_id)
        except (TypeError, ValueError):
            return None
        item = self._all_taiko_maps.get(map_id)
        if item is None or item.beatmap_id in getattr(self, '_runtime_excluded', set()):
            return None
        if item.is_taiko_convert and not self.taiko_include_converts:
            return None
        return item

    def get_variant_map(self, beatmap_id, mods):
        # Only the verified official Taiko NM result exists. No Standard stars,
        # DT/NC, rate-changing mod, or generic variant fallback is permitted.
        if mods not in ([], (), None):
            return None
        return self.get_map(beatmap_id)

    def verify_variant(self, item, *, in_range=False):
        known = self.get_map(getattr(item, 'beatmap_id', 0))
        identity = self._identities.get(getattr(item, 'beatmap_id', 0))
        actual = tuple(getattr(item, name, None) for name in (
            'sr','file_md5','source_sha256','taiko_nm_variant','calculator_id',
            'difficulty_version','source_commit','ruleset_id','map_origin',
            'is_taiko_convert','native'))
        if (known is None or not isinstance(item, TaikoMap) or actual != identity
                or item.clock_rate != 1.0 or item.is_dt or item.ruleset_id != 1
                or (in_range and (not self.sr_min <= item.sr <= self.sr_max
                                  or (self.max_length and item.hit_length > self.max_length)
                                  or item.hit_length < self.taiko_min_length))):
            raise ValueError('unqualified or mismatched Taiko NM identity')
        return item

    def set_farm_mode(self):
        raise ValueError('Taiko style/farm modes are not qualified')

    set_stream_mode = set_farm_mode
    set_jump_mode = set_farm_mode
    set_variety_mode = reload

