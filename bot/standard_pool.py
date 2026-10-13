"""Mixed NoMod/verified 1.5x DT variants. Shared legacy launchers stay unchanged."""
import hashlib
import logging
import math
from pathlib import Path
import random
import sqlite3
import time

from .jump_catalog import JUMP_CATALOG
from .pool import MapPool, PoolMap

log = logging.getLogger('standard_pool')
DT_CALCULATOR = 'osu-tools-py-0.1.7/2026.702.1.0'


class StandardPool(MapPool):
    mixed_dt = True

    def __init__(self, db_path, *, standard_dt_db, dt_auto_threshold=5.5,
                 dt_pick_share=.25, standard_catalog_binding=None, **kwargs):
        if (isinstance(dt_auto_threshold, bool) or not isinstance(dt_auto_threshold, (int, float))
                or isinstance(dt_pick_share, bool) or not isinstance(dt_pick_share, (int, float))):
            raise ValueError('DT threshold and share must be numbers, not booleans or strings')
        self.standard_dt_db = str(standard_dt_db)
        if standard_catalog_binding is not None:
            if (type(standard_catalog_binding) is not dict
                    or set(standard_catalog_binding) != {'source_sha256', 'dt_sha256'}
                    or any(type(value) is not str or len(value) != 64
                           or any(c not in '0123456789abcdef' for c in value)
                           for value in standard_catalog_binding.values())):
                raise ValueError('Managed Standard needs explicit source and DT SHA256 identities')
            standard_catalog_binding = dict(standard_catalog_binding)
        self.standard_catalog_binding = standard_catalog_binding
        self._managed_hash_cache = {}
        self.dt_auto_threshold = float(dt_auto_threshold)
        self.dt_pick_share = float(dt_pick_share)
        if (not math.isfinite(self.dt_auto_threshold) or self.dt_auto_threshold < 0
                or not math.isfinite(self.dt_pick_share) or not 0 < self.dt_pick_share < 1):
            raise ValueError('DT needs finite nonnegative threshold and 0 < pick share < 1')
        self.dt_override = None
        self.dt_full = False
        self._dt_excluded = set()
        self._last_dt_pick = False
        self._dt_catalog_warned = False
        self._dt_valid_until = 0
        self._dt_refresh_after = 0
        self._dt_source_signature = None
        super().__init__(db_path, **kwargs)

    def dt_enabled(self):
        return (self.dt_override if self.dt_override is not None else
                (self.sr_min+self.sr_max)/2 > self.dt_auto_threshold)

    def _is_preferred_duration(self, item: PoolMap) -> bool:
        if self.source_mode == 'jump':
            return super()._is_preferred_duration(item)
        if not item.is_dt:
            return super()._is_preferred_duration(item)
        # Plain DT is always 1.5x. Match the catalog's ceil-rounded play time:
        # admit NM-preferred short charts without demoting existing DT picks.
        # The upper preference and hard cap still use actual playing seconds.
        minimum = math.ceil(self.preferred_min_length / 1.5)
        return minimum <= item.hit_length <= self.preferred_max_length

    def dt_policy_label(self):
        state = 'on' if self.dt_enabled() and self.dt_catalog_ready() else 'off'
        mode = 'auto' if self.dt_override is None else 'manual'
        if self.dt_full:
            return f'DT {state} (manual 100%; NoMod fallback)'
        return f'DT {state} ({mode}; up to {self.dt_pick_share:.0%} mixed picks)'

    def _managed_digest(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError('Managed catalog must be a regular file')
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = self._managed_hash_cache.get(str(path))
        if cached and cached[0] == signature:
            return cached[1]
        if stat.st_size > 512 * 1024**2:
            raise ValueError('Managed catalog exceeds size budget')
        digest = hashlib.sha256()
        with path.open('rb') as data:
            for chunk in iter(lambda: data.read(1024**2), b''):
                digest.update(chunk)
        after = path.stat()
        if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('Managed catalog changed during identity check')
        value = digest.hexdigest()
        self._managed_hash_cache[str(path)] = (signature, value)
        return value

    def _managed_ready(self):
        binding = self.standard_catalog_binding
        return (self._managed_digest(self.db_path) == binding['source_sha256']
                and self._managed_digest(self.standard_dt_db) == binding['dt_sha256'])

    def dt_catalog_ready(self):
        if time.time() > self._dt_valid_until:
            return False
        try:
            if self.standard_catalog_binding is not None:
                return self._managed_ready()
            source = Path(self.db_path).stat()
            return (source.st_size, source.st_mtime_ns) == self._dt_source_signature
        except (OSError, ValueError):
            return False

    def dt_catalog_refresh_due(self):
        """Thirty days schedules research refresh; it does not invalidate charts."""
        return self.dt_catalog_ready() and time.time() >= self._dt_refresh_after

    def set_dt_override(self, enabled, *, full=False):
        if enabled is not None and type(enabled) is not bool:
            raise ValueError('DT override must be true, false or automatic')
        if type(full) is not bool or (full and enabled is not True):
            raise ValueError('Full DT requires an explicit DT opt-in')
        old = self.dt_override, self.dt_full
        previous = self.maps[:]
        self.dt_override = enabled
        self.dt_full = full
        try:
            return self.reload()
        except Exception:
            if enabled is False:
                # Opt-out is sticky even if a catalog becomes unavailable.
                self.maps = [m for m in self.maps if not m.is_dt]
            else:
                self.dt_override, self.dt_full = old
                self.maps = previous
            raise

    def next_map(self, *, native_only=False):
        # Parent repeat/style fallback must never revive a disabled variant.
        self.maps = [m for m in self.maps if not m.is_dt or
                     (self.dt_enabled() and self.dt_catalog_ready()
                      and m.beatmap_id not in self._dt_excluded)]
        if not native_only and not self.dt_full:
            return super().next_map()
        maps = self.maps
        if native_only:
            choices = [m for m in maps if not m.is_dt and m.beatmap_id not in
                       (self._playlist_reserved | self._runtime_excluded)]
            fresh = [m for m in choices if m.beatmap_id not in
                     (set(self._recent) | set(self._round_history))]
            self.maps = fresh or choices
        else:
            # Full DT prefers fresh DT even when Jump's NM strict tier is
            # stronger. Never repeat/reserve a DT chart while fresh NM exists.
            blocked = (set(self._recent) | set(self._round_history)
                       | self._playlist_reserved | self._runtime_excluded)
            fresh = [m for m in maps if m.beatmap_id not in blocked]
            spaced = [m for m in fresh if self._song_key(m) not in
                      set(self._recent_songs[-4:])]
            choices = spaced or fresh
            dt = [m for m in choices if m.is_dt]
            self.maps = dt or choices or [m for m in maps if m.beatmap_id not in
                                         (self._playlist_reserved | self._runtime_excluded)]
        try:
            return super().next_map()
        finally:
            self.maps = maps

    def _dt_rows(self, *, beatmap_id=None):
        try:
            path = Path(self.standard_dt_db).resolve()
            conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)
            try:
                conn.row_factory = sqlite3.Row
                metadata = dict(conn.execute('SELECT key,value FROM metadata'))
                source = Path(self.db_path).stat()
                built_at = float(metadata['built_at'])
                if self.standard_catalog_binding is not None:
                    identity_valid = (metadata.get('format') == '2'
                                      and metadata.get('source_sha256') == self.standard_catalog_binding['source_sha256']
                                      and self._managed_ready())
                else:
                    identity_valid = (metadata.get('format') == '1'
                                      and int(metadata['source_size']) == source.st_size
                                      and int(metadata['source_mtime_ns']) == source.st_mtime_ns)
                if (not identity_valid or metadata.get('mods') != 'DT'
                        or metadata.get('clock_rate') != '1.5'
                        or metadata.get('calculator') != DT_CALCULATOR
                        or not math.isfinite(built_at) or built_at <= 0
                        or built_at > time.time()):
                    raise ValueError('DT evidence is invalid or belongs to another database/calculator')
                # Keep verified evidence usable while a replacement is prepared.
                # Managed content hashes, source identity and revision checks
                # remain mandatory; refresh age is not evidence invalidation.
                self._dt_valid_until = float('inf')
                self._dt_refresh_after = built_at+30*86400
                self._dt_source_signature = (source.st_size, source.st_mtime_ns)
                if beatmap_id is not None:
                    return conn.execute('SELECT * FROM dt_maps WHERE beatmap_id=?', (beatmap_id,)).fetchall()
                cutoff = self.max_map_age_cutoff()
                age = ' AND julianday(ranked_date)>=julianday(?)' if cutoff else ''
                params = [self.sr_min,self.sr_max,self.max_length,min(self.min_passcount,self.sparse_passcount_floor)]
                if cutoff:
                    params.append(cutoff)
                return conn.execute('SELECT * FROM dt_maps WHERE dt_sr BETWEEN ? AND ? '
                                    'AND hit_length<=? AND passcount>=?'+age, params).fetchall()
            finally:
                conn.close()
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
            self._dt_valid_until = 0
            if not self._dt_catalog_warned:
                log.warning('DT catalog unavailable; keeping NoMod only: %s', error)
                self._dt_catalog_warned = True
            return []

    def _dt_map(self, row):
        farm = row['farm_score'] > 0 and any(mod in row['dominant_mods'].upper() for mod in ('DT','NC'))
        stream = (row['stream_score'] >= self.stream_min_score and row['stream_bpm'] >= self.stream_min_bpm
                  and row['hit_length'] >= self.stream_min_length)
        jump = JUMP_CATALOG.get(row['beatmap_id'], (0,0))
        approach_rate = None
        if self.source_mode == 'jump':
            # Read original AR without changing the immutable DT schema/catalog.
            try:
                conn = sqlite3.connect(f'file:{Path(self.db_path).resolve()}?mode=ro', uri=True)
                try:
                    ar = conn.execute('SELECT ar FROM beatmaps WHERE beatmap_id=?', (row['beatmap_id'],)).fetchone()
                    approach_rate = ar[0] if ar else None
                finally:
                    conn.close()
            except sqlite3.Error:
                pass  # Older/synthetic catalogs without AR remain selectable.
        if (not math.isfinite(row['dt_sr']) or row['dt_sr'] <= 0
                or row['calculator'] != DT_CALCULATOR or len(row['checksum']) != 32
                or any(c not in '0123456789abcdef' for c in row['checksum'])):
            return None
        return PoolMap(row['beatmap_id'],row['artist'],row['title'],row['version'],row['dt_sr'],
                       row['hit_length'],row['farm_score'],row['passcount'],quadrant='DT',
                       stream_score=row['stream_score'],stream_bpm=row['stream_bpm'],
                       is_farm=farm,is_stream=stream,is_jump=bool(jump[0] or jump[1]),
                       jump_score=jump[0],jump_relaxed_score=jump[1],is_dt=True,clock_rate=1.5,
                       source_sr=row['nm_sr'],source_hit_length=row['source_hit_length'],
                       dt_checksum=row['checksum'],dt_calculator=row['calculator'],
                       approach_rate=approach_rate)

    def reload(self):
        super().reload()
        if hasattr(self, '_query_min_passcount') or not self.dt_enabled():
            return len(self.maps)
        nm = self.maps
        options = [self._dt_map(row) for row in self._dt_rows()]
        options = [m for m in options if m is not None and m.beatmap_id not in self._dt_excluded
                   and m.beatmap_id not in self._runtime_excluded]
        target = min(self.limit,self.sparse_passcount_min_maps)
        steps = list(self._passcount_steps()) if self.sparse_passcount_enabled else [self.min_passcount]
        selected = []
        seen = set()
        for minimum in steps:
            eligible = [m for m in options if m.passcount >= minimum]
            if self.source_mode == 'jump':
                ranked = self._bounded_jump_candidates([m for m in eligible if m.is_jump])
            else:
                farms = sorted((m for m in eligible if m.is_farm),key=lambda m:m.farm_score,reverse=True)
                streams = sorted((m for m in eligible if m.is_stream),key=lambda m:m.stream_score,reverse=True)
                ranked = self._balanced_candidates(farms,streams)
            for m in ranked:
                if m.beatmap_id not in seen and len(selected) < self.limit:
                    selected.append(m); seen.add(m.beatmap_id)
            if len(selected) >= target:
                break
        self.maps = nm+selected
        return len(self.maps)

    def _weighted_pick(self, candidates):
        nm = [m for m in candidates if not m.is_dt]
        dt = [m for m in candidates if m.is_dt and self.dt_enabled() and self.dt_catalog_ready()
              and m.beatmap_id not in self._dt_excluded]
        passcount_max = self._passcount_reference(nm + dt)
        # Ordinary mixing retains its no-consecutive-DT guard. Explicit 100%
        # removes that guard, with NM remaining a real fallback, not relabeling.
        choose_dt = bool(dt and (self.dt_full or not nm or
                         (not self._last_dt_pick and random.random() < self.dt_pick_share)))
        group = dt if choose_dt else nm
        if not group:
            return None
        item = super()._weighted_pick(group, passcount_max=passcount_max)
        self._last_dt_pick = item.is_dt
        return item

    def get_variant_map(self, beatmap_id, required_mods):
        mods = {str(m.get('acronym','') if isinstance(m,dict) else m).upper() for m in required_mods}
        if 'DT' in mods:
            rows = self._dt_rows(beatmap_id=int(beatmap_id))
            return self._dt_map(rows[0]) if rows else None
        if mods & {'NC','HT','DC'}:
            return None
        for m in self.maps:
            if m.beatmap_id == int(beatmap_id) and not m.is_dt:
                return m
        # Parent resolver checks self.maps first; hide DT variants so it cannot
        # accidentally bind an NM playlist item to the DT rating/duration.
        maps = self.maps
        self.maps = [m for m in maps if not m.is_dt]
        try:
            return super().get_map(beatmap_id)
        finally:
            self.maps = maps

    def get_map(self, beatmap_id):
        return self.get_variant_map(beatmap_id, [])

    def exclude_dt_beatmap(self, beatmap_id):
        self._dt_excluded.add(int(beatmap_id))
        self.maps = [m for m in self.maps if not (m.is_dt and m.beatmap_id == int(beatmap_id))]

    def reset_session_pool(self, *args):
        old = self.dt_override, self.dt_full
        self.dt_override = None
        self.dt_full = False
        try:
            count = super().reset_session_pool(*args)
        except Exception:
            self.dt_override, self.dt_full = old
            raise
        if not count:
            self.dt_override, self.dt_full = old
        return count
