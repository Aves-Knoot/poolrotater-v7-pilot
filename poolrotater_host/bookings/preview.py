"""Bounded, read-only previews over caller-trusted native catalog pools."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path
import threading
import time

from ..config import ConfigError, PRESETS
from .selection import PRESETS as SELECTION_PRESETS
from .selection import SelectionError, preview as summarize
from .settings import FIELDS, inventory, validate_player_patch

_EXPORT_MODES = {
    'standard-auto': 'standard', 'standard-fixed': 'standard',
    'standard-loved': 'loved', 'mania-4k': 'mania4',
    'mania-7k': 'mania7', 'catch-fixed': 'catch',
}
_MAX_POOL_LIMIT = 500_000


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False, default=str).encode('utf-8')


def _fstat_identity(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ConfigError('preview bundle file ownership or type changed')
        return (info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)
    finally:
        os.close(fd)


def _directory_identity(path):
    info = Path(path).lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise ConfigError('preview bundle directory ownership or type changed')
    return (info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


class PreviewProvider:
    """Cache configured native pools briefly and summarize without picking.

    ``configs(preset)`` must return an already-qualified, trusted native room
    configuration. ``pool_factory(config)`` must construct the same read-only
    native pool used by a room worker. Cache keys include both that complete
    base configuration and the effective player settings.
    """

    def __init__(self, configs, pool_factory, *, clock=time.monotonic,
                 ttl_seconds=60.0, max_entries=2):
        if not callable(configs) or not callable(pool_factory):
            raise TypeError('preview provider requires trusted config and pool factories')
        if (type(ttl_seconds) not in (int, float) or ttl_seconds <= 0
                or type(max_entries) is not int or not 1 <= max_entries <= 32):
            raise ValueError('preview cache bounds are invalid')
        self.configs = configs
        self.pool_factory = pool_factory
        self.clock = clock
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = max_entries
        self._cache = {}
        self._lock = threading.RLock()

    def __call__(self, preset, settings):
        return self.preview(preset, settings)

    def _prepare(self, preset, settings):
        if preset not in SELECTION_PRESETS or preset not in PRESETS:
            raise SelectionError('unsupported preview preset')
        base = self.configs(preset)
        if type(base) is not dict:
            raise SelectionError('trusted preview configuration is unavailable')
        base = copy.deepcopy(base)
        base_digest = hashlib.sha256(_canonical(base)).hexdigest()
        current = copy.deepcopy(inventory(preset)['defaults'])
        current.update({key: copy.deepcopy(value) for key, value in base.items() if key in FIELDS})
        current['preset'] = preset
        try:
            patch = validate_player_patch(settings, current=current, preset=preset)
        except Exception as exc:
            raise SelectionError('Preview settings are not valid for this preset.') from exc
        effective = copy.deepcopy(current)
        effective.update(copy.deepcopy(patch))
        staged = base
        staged.update(copy.deepcopy(effective))
        staged['_supervisor_overrides'] = {
            **copy.deepcopy(staged.get('_supervisor_overrides') or {}),
            **copy.deepcopy(effective),
        }
        return staged, effective, base_digest

    def _pool(self, preset, staged, effective, base_digest):
        settings_digest = hashlib.sha256(_canonical(effective)).hexdigest()
        key = (preset, base_digest, settings_digest)
        now = self.clock()
        with self._lock:
            self._expire(now)
            cached = self._cache.get(key)
            if cached is not None:
                cached['expires'] = now + self.ttl_seconds
                cached['used'] = now
                return cached['pool']

            # A configured pool can hold a complete catalog. Free its LRU
            # predecessor before materializing another one.
            if len(self._cache) >= self.max_entries:
                oldest = min(self._cache, key=lambda item: self._cache[item]['used'])
                del self._cache[oldest]

            try:
                pool = self.pool_factory(copy.deepcopy(staged))
                if pool is None or not isinstance(getattr(pool, 'maps', None), list):
                    raise SelectionError('native preview pool is unavailable')
                if callable(getattr(pool, 'configure_duration_policy', None)):
                    pool.configure_duration_policy(staged)
                if callable(getattr(pool, 'configure_jump_policy', None)):
                    pool.configure_jump_policy(staged)
                from ..managed_host.native import apply_pool_styles
                apply_pool_styles(pool, staged)
                from .selection import install_selection
                install_selection(pool, effective, preset)
                pool.maps = pool._booking_selection_adapter._eligible(pool.maps)
            except SelectionError:
                raise
            except Exception as exc:
                raise SelectionError('A qualified preview pool could not be prepared.') from exc
            self._cache[key] = {'pool': pool, 'expires': now + self.ttl_seconds,
                                'used': now}
            return pool

    def _expire(self, now):
        for key in [key for key, item in self._cache.items() if item['expires'] <= now]:
            del self._cache[key]

    def preview(self, preset, settings):
        staged, effective, base_digest = self._prepare(preset, settings)
        pool = self._pool(preset, staged, effective, base_digest)
        filters = effective.get('selection_filters', {})
        mods = effective.get('required_mods', ())
        candidates = getattr(pool, 'maps', ())
        if (not candidates and getattr(pool, 'mania_mode', False)
                and getattr(pool, 'mania_style_fallback_enabled', False)
                and getattr(pool, 'mania_source_mode', '') != 'variety'
                and callable(getattr(pool, '_rows', None))):
            # This read-only SQL fallback mirrors Mania's native exhausted-style
            # path. It never calls next_map or alters cached selector history.
            rows = pool._rows(ignore_style=True)
            candidates = [pool._mania_map(row) for row in rows
                          if pool._composition_guard_values(dict(row), relaxed=True)]
        return summarize(candidates, filters, preset, mods)


class ExportPreviewProvider(PreviewProvider):
    """Native previews backed by a caller-configured export directory.

    The directory must contain clean `standard`, `mania4`, `mania7`, `catch`,
    and/or `loved` CatalogBundle exports. Each bundle is inspected and matched
    to its fixed preset mode before any native pool is created. Request data
    cannot supply or select a filesystem path.
    """

    def __init__(self, export_root, *, pool_factory=None, clock=time.monotonic,
                 ttl_seconds=60.0, max_entries=2):
        root = Path(export_root).absolute()
        if root.is_symlink() or not root.is_dir():
            raise ConfigError('preview export root must be a configured directory')
        root_info = root.stat()
        if root_info.st_uid != os.geteuid() or root_info.st_mode & 0o022:
            raise ConfigError('preview export root must be privately owned')
        self.export_root = root
        self._root_identity = _directory_identity(root)
        self._manifest_lock = threading.RLock()
        self._bundle_cache = {}
        self._config_cache = {}
        self._config_ttl = float(ttl_seconds)
        self._clock = clock
        self._base_pool_cache = {}
        super().__init__(self._config_for_preset,
                         pool_factory or _native_pool_factory,
                         clock=clock, ttl_seconds=ttl_seconds,
                         max_entries=max_entries)

    def _bundle(self, preset):
        if not self._root_valid():
            raise SelectionError('qualified preview catalog is unavailable')
        mode = _EXPORT_MODES.get(preset)
        if mode is None:
            raise SelectionError('unsupported preview preset')
        folder = self.export_root / mode
        if folder.is_symlink() or not folder.is_dir():
            raise SelectionError('qualified preview catalog is unavailable')
        try:
            directory_identity = _directory_identity(folder)
        except ConfigError as exc:
            raise SelectionError('qualified preview catalog is unavailable') from exc
        now = self._clock()
        cached = self._bundle_cache.get(mode)
        if (cached is not None and cached['expires'] > now
                and self._bundle_identity_matches(folder, cached)):
            return folder, cached['manifest'], cached['identity']
        from ..catalog_export import inspect_bundle
        try:
            from ..catalog_export import read_manifest
            header = read_manifest(folder / 'manifest.json')
            names = {'manifest.json', header['archive']['name']}
            names.update(entry['name'] for entry in header['files'])
            if {path.name for path in folder.iterdir()} != names:
                raise ConfigError('unexpected preview export contents')
            before = self._identity(folder, names)
            manifest = inspect_bundle(folder)
        except Exception as exc:
            raise SelectionError('qualified preview catalog is unavailable') from exc
        if manifest.get('mode') != mode:
            raise SelectionError('qualified preview catalog does not match this preset')
        identity = self._identity(folder, names)
        if before != identity:
            raise SelectionError('qualified preview catalog changed during validation')
        self._bundle_cache[mode] = {'manifest': manifest, 'identity': identity,
                                    'expires': now + self._config_ttl}
        return folder, manifest, identity

    @staticmethod
    def _identity(folder, names):
        return (('.', _directory_identity(folder)), *tuple(
            (name, _fstat_identity(folder / name)) for name in sorted(names)))

    @staticmethod
    def _bundle_identity_matches(folder, cached):
        try:
            expected = dict(cached['identity'])
            if _directory_identity(folder) != expected.pop('.'):
                return False
            if {path.name for path in folder.iterdir()} != set(expected):
                return False
            return all(_fstat_identity(folder / name) == identity
                       for name, identity in expected.items())
        except (OSError, ConfigError, TypeError, ValueError):
            return False

    def _config_for_preset(self, preset):
        with self._manifest_lock:
            cached = self._config_cache.get(preset)
            now = self._clock()
            if (cached is not None and cached['expires'] > now
                    and self._cached_identity_matches(preset, cached)):
                return copy.deepcopy(cached['config'])
            folder, manifest, identity = self._bundle(preset)
            config = self._build_config(preset, folder, manifest)
            self._config_cache[preset] = {'config': copy.deepcopy(config),
                                          'identity': identity,
                                          'expires': now + self._config_ttl}
            return config

    def _cached_identity_matches(self, preset, cached):
        mode = _EXPORT_MODES.get(preset)
        folder = self.export_root / mode if mode else None
        if not self._root_valid() or folder is None or folder.is_symlink() or not folder.is_dir():
            return False
        try:
            expected = dict(cached['identity'])
            if _directory_identity(folder) != expected.pop('.'):
                return False
            if {path.name for path in folder.iterdir()} != set(expected):
                return False
            return all(_fstat_identity(folder / name) == identity
                       for name, identity in expected.items())
        except (OSError, ConfigError, TypeError, ValueError):
            return False

    def _root_valid(self):
        try:
            return (not self.export_root.is_symlink()
                    and _directory_identity(self.export_root) == self._root_identity)
        except (OSError, ConfigError):
            return False

    @staticmethod
    def _build_config(preset, folder, manifest):
        mode = manifest['mode']
        cfg = {
            '_preview_catalog_mode': mode,
            '_preview_content_id': manifest['content_id'],
            'ruleset_id': PRESETS[preset]['ruleset'],
            'sr_min': PRESETS[preset]['sr_min'],
            'sr_max': PRESETS[preset]['sr_max'],
            'auto_difficulty': PRESETS[preset]['auto_difficulty'],
            'max_length': 240,
            'min_passcount': 0,
            'pool_limit': _MAX_POOL_LIMIT,
            'ratings_db': None,
            'stream_enabled': True,
            'stream_share': 0.5,
            'allowed_mods': [],
            'required_mods': [],
            'freestyle': False,
            '_host_managed_catalogs': True,
            '_instance_name': 'booking-preview',
        }
        if mode == 'standard':
            files = {entry['name']: entry['sha256'] for entry in manifest['files']}
            cfg['farm_db'] = str(folder / 'catalog.db')
            if 'dt' in manifest['capabilities']['companions']:
                cfg.update(standard_dt_db=str(folder / 'dt.db'),
                           standard_catalog_binding={
                               'source_sha256': files['catalog.db'],
                               'dt_sha256': files['dt.db'],
                           })
        elif mode == 'loved':
            cfg.update(loved_db=str(folder / 'catalog.db'), loved_mode=True,
                       stream_enabled=False, stream_share=0)
        elif mode == 'catch':
            cfg.update(catch_catalog=str(folder / 'catalog.db'),
                       catch_include_loved=False, catch_min_length=0,
                       stream_enabled=False, stream_share=0)
        else:
            companions = set(manifest['capabilities']['companions'])
            cfg.update(mania_db=str(folder / 'catalog.db'),
                       mania_keys=manifest['keys'],
                       mania_source_mode='variety',
                       mania_farm_enabled='farm' in companions,
                       mania_farm_db=str(folder / 'farm.db') if 'farm' in companions else '',
                       mania_convert_db=str(folder / 'converts.db') if 'converts' in companions else '',
                       mania_convert_validation_db=str(folder / 'converts.db') if 'converts' in companions else None,
                       stream_enabled=False, stream_share=0)
        return cfg

    def preview(self, preset, settings):
        staged, effective, base_digest = self._prepare(preset, settings)
        now = self.clock()
        key = (preset, base_digest)
        with self._lock:
            self._expire(now)
            for expired in [item_key for item_key, entry in self._base_pool_cache.items()
                            if entry['expires'] <= now]:
                del self._base_pool_cache[expired]
            cached = self._base_pool_cache.get(key)
            if cached is None:
                # Base pools can retain the full catalog too. Evict before a
                # factory call so peak memory never includes max+1 pools.
                if len(self._base_pool_cache) >= self.max_entries:
                    oldest = min(self._base_pool_cache,
                                 key=lambda item: self._base_pool_cache[item]['used'])
                    del self._base_pool_cache[oldest]
                base_cfg = copy.deepcopy(staged)
                base_cfg.update(sr_min=0.0, sr_max=100.0, max_length=3600,
                                min_passcount=0, pool_limit=_MAX_POOL_LIMIT,
                                selection_styles=['variety'], allowed_mods=[],
                                required_mods=[], freestyle=False,
                                selection_filters={}, dt_mode='auto')
                for field in ('real_max_length', 'preferred_min_length',
                              'preferred_max_length', 'preferred_length_share'):
                    base_cfg.pop(field, None)
                try:
                    base_pool = self.pool_factory(copy.deepcopy(base_cfg))
                except Exception as exc:
                    raise SelectionError('A qualified preview pool could not be prepared.') from exc
                if base_pool is None or not isinstance(getattr(base_pool, 'maps', None), list):
                    raise SelectionError('native preview pool is unavailable')
                cached = {'pool': base_pool, 'expires': now + self.ttl_seconds,
                          'used': now}
                self._base_pool_cache[key] = cached
            else:
                cached['expires'] = now + self.ttl_seconds
                cached['used'] = now
            try:
                pool = self._configure_pool(copy.deepcopy(cached['pool']), staged,
                                            effective, preset)
            except SelectionError:
                raise
            except Exception as exc:
                raise SelectionError('A qualified preview pool could not be prepared.') from exc
        candidates = getattr(pool, 'maps', ())
        if (not candidates and getattr(pool, 'mania_mode', False)
                and getattr(pool, 'mania_style_fallback_enabled', False)
                and getattr(pool, 'mania_source_mode', '') != 'variety'
                and callable(getattr(pool, '_rows', None))):
            rows = pool._rows(ignore_style=True)
            candidates = [pool._mania_map(row) for row in rows
                          if pool._composition_guard_values(dict(row), relaxed=True)]
        return summarize(candidates, effective.get('selection_filters', {}),
                         preset, effective.get('required_mods', ()))

    @staticmethod
    def _configure_pool(pool, staged, effective, preset):
        if hasattr(pool, 'sr_min'):
            pool.sr_min = float(effective.get('sr_min', pool.sr_min))
            pool.sr_max = float(effective.get('sr_max', pool.sr_max))
        duration_reader = getattr(pool, 'duration_policy_from_config', None)
        policy = duration_reader(staged) if callable(duration_reader) else None
        if policy:
            pool.real_max_length = policy['real_max_length']
            pool.preferred_min_length = policy['preferred_min_length']
            pool.preferred_max_length = policy['preferred_max_length']
            pool.preferred_length_share = policy['preferred_length_share']
        maximum = effective.get('max_length', getattr(pool, 'max_length', 3600))
        if policy:
            maximum = min(maximum, policy['real_max_length'])
        pool.max_length = int(maximum)
        jump = getattr(pool, 'configure_jump_policy', None)
        if callable(jump):
            jump(staged)
        pool.reload()
        from ..managed_host.native import apply_pool_styles
        apply_pool_styles(pool, staged)
        from .selection import install_selection
        install_selection(pool, effective, preset)
        pool.maps = pool._booking_selection_adapter._eligible(pool.maps)
        return pool


def _native_pool_factory(cfg):
    """Use only the pinned native catalog readers and stdlib pool modules."""
    shared = {
        'sr_min': cfg['sr_min'], 'sr_max': cfg['sr_max'],
        'max_length': cfg['max_length'], 'min_passcount': cfg['min_passcount'],
        'limit': cfg['pool_limit'], 'ratings_db': None,
        'stream_enabled': cfg.get('stream_enabled', True),
        'stream_share': cfg.get('stream_share', 0.5),
        'max_map_age_years': 0,
    }
    if cfg['ruleset_id'] == 2:
        from bot.catch_pool import ProductionCatchPool
        return ProductionCatchPool(cfg['catch_catalog'], purpose='production',
                                   catch_include_loved=False,
                                   catch_min_length=cfg.get('catch_min_length', 0), **shared)
    if cfg['ruleset_id'] == 3:
        from bot.mania_pool import ManiaPool
        return ManiaPool(cfg['mania_db'], mania_keys=cfg['mania_keys'],
                         mania_source_mode=cfg.get('mania_source_mode', 'variety'),
                         mania_farm_enabled=cfg.get('mania_farm_enabled', False),
                         mania_farm_db=cfg.get('mania_farm_db', ''),
                         mania_convert_db=cfg.get('mania_convert_db', ''),
                         mania_convert_validation_db=cfg.get('mania_convert_validation_db'),
                         **shared)
    if cfg.get('loved_mode'):
        from bot.pool import MapPool
        return MapPool(cfg['loved_db'], loved_mode=True, **shared)
    if cfg.get('standard_dt_db'):
        from bot.standard_pool import StandardPool
        return StandardPool(cfg['farm_db'], standard_dt_db=cfg['standard_dt_db'],
                            standard_catalog_binding=cfg['standard_catalog_binding'], **shared)
    from bot.pool import MapPool
    return MapPool(cfg['farm_db'], **shared)
