"""Read-only catalog validation and reuse of the existing selection engines."""
from __future__ import annotations
import sqlite3
from contextlib import closing
from pathlib import Path
from .config import ConfigError, PRESETS, contained

PRIVATE_TABLES = {'scores', 'users', 'players', 'map_ratings', 'shadow_map_ratings',
                  'performance_records', 'autohost_match_players', 'tokens'}


def inspect_catalog(path: Path, preset: str) -> dict:
    if (not path.is_file() and not (preset == 'catch-fixed' and path.is_dir())) or path.is_symlink():
        raise ConfigError('catalog missing or not a regular local file')
    spec = PRESETS[preset]
    if spec['ruleset'] == 2:
        if path.is_file():
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                production=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catch_maps'").fetchone()
            if production:
                from bot.catch_catalog.production import rows
                return {'maps':sum(1 for _ in rows(path)),'tables':['catch_maps','metadata'],
                        'research_only':False,'bytes':path.stat().st_size}
        from bot.catch_catalog.c6_reader import imported_rows, Catalog
        rows = Catalog(path, purpose='research').rows() if path.is_dir() else imported_rows(path)
        count = sum(1 for _ in rows)
        return {'maps': count, 'tables': ['maps', 'metadata'], 'research_only': True}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ConfigError('catalog integrity check failed')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if PRIVATE_TABLES & tables or any(n.startswith('autohost_') for n in tables):
                raise ConfigError('catalog includes private player/runtime tables')
            table = 'beatmaps' if spec['ruleset'] == 0 else 'maps'
            expected = {'beatmap_id', 'artist', 'title', 'version', 'sr', 'hit_length', 'passcount', 'status'}
            if spec['ruleset'] == 3:
                expected |= {'mode_int', 'key_count'}
            columns = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
            if not expected <= columns:
                raise ConfigError('catalog schema does not match preset')
            if spec['ruleset'] == 0 and 'farm_report' not in tables:
                raise ConfigError('standard catalog lacks prepared farm features')
            if spec['ruleset'] == 3:
                invalid = db.execute('SELECT COUNT(*) FROM maps WHERE mode_int IS NULL OR mode_int!=3 '
                                     'OR key_count IS NULL OR key_count!=?', (spec['keys'],)).fetchone()[0]
                if invalid:
                    raise ConfigError('native Mania catalog contains wrong ruleset/key count')
            count = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            if not count:
                raise ConfigError('catalog contains no maps')
            return {'maps': count, 'tables': sorted(tables), 'bytes': path.stat().st_size}
    except sqlite3.Error as exc:
        raise ConfigError('catalog is corrupt or incompatible') from exc


def make_pool(root: Path, room: dict, *, ratings: Path | None = None, resolved=None):
    if PRESETS[room['preset']]['ruleset'] == 2:
        from .layout import load
        from .catalog_store import resolved_catalogs, mode_for, CatalogStore
        _, config = load(root)
        if resolved is None:
            resolved = resolved_catalogs(root)
        if resolved is not None:
            from bot.catch_pool import ProductionCatchPool
            from .ratings.reader import for_settings, profile
            from .catalog_exclusions import ExclusionPolicy, attach_policy
            if not isinstance(resolved,dict) or 'catch' not in resolved.get('bundles',{}):
                raise ConfigError('invalid managed/live Catch catalog reference')
            folder, manifest = resolved['bundles']['catch']
            rp=profile(root)
            reader=for_settings({'ruleset_id':2, 'ratings_reader_mode':rp['mode'],
                                 'ratings_reader_root':str(root), 'ratings_reader_epoch':rp['server_epoch']})
            pool=ProductionCatchPool(folder/'catalog.db',purpose='production',
                                     sr_min=room['sr_min'],sr_max=room['sr_max'],max_length=240,
                                     min_passcount=0,ratings_db=str(ratings) if ratings else None,
                                     ratings_reader=reader)
            return attach_policy(pool,ExclusionPolicy(CatalogStore.open(root),'catch',manifest['content_id']))
        if config['execution'] != 'offline':
            raise ConfigError('Catch research-only catalogs cannot activate in managed/live hosts')
        from bot.catch_pool import CatchPool
        path = contained(root, room['catalog'])
        return CatchPool(path, purpose='research', sr_min=room['sr_min'], sr_max=room['sr_max'],
                         max_length=240, min_passcount=0)
    from .ratings.reader import profile,NetworkReader
    from .ratings import reader as ratings_reader
    read_profile=profile(root)
    if read_profile['mode']=='network':
        from .network.contracts import require
        require(ratings_reader.NETWORK_CONSUMPTION_QUALIFIED,'ratings_shadow_qualification_required')
        reader=NetworkReader(root,server_epoch=read_profile['server_epoch'])
    else:reader=None
    # Imports are deferred: setup/config/status do not need the legacy runtime.
    from bot.pool import MapPool
    from bot.mania_pool import ManiaPool
    if resolved is None:
        from .catalog_store import resolved_catalogs
        resolved = resolved_catalogs(root)
    if resolved is not None:
        from .catalog_store import settings_for
        from bot.standard_pool import StandardPool
        cfg = settings_for(room, resolved)
        spec = PRESETS[room['preset']]
        options = dict(sr_min=room['sr_min'], sr_max=room['sr_max'], max_length=240,
                       min_passcount=1000, max_map_age_years=0, limit=200,
                       ratings_db=str(ratings) if ratings is not None else None,ratings_reader=reader)
        if spec['ruleset'] == 3:
            pool = ManiaPool(cfg['mania_db'], mania_keys=spec['keys'],
                             mania_farm_enabled=cfg['mania_farm_enabled'], mania_farm_db=cfg['mania_farm_db'], **options)
        elif cfg['standard_dt_db']:
            pool = StandardPool(cfg['farm_db'], standard_dt_db=cfg['standard_dt_db'],
                                standard_catalog_binding=cfg['standard_catalog_binding'], **options)
        else:
            pool = MapPool(cfg['farm_db'], **options)
        from .catalog_exclusions import ExclusionPolicy, attach_policy
        from .catalog_store import CatalogStore, mode_for
        mode = mode_for(room)
        pool = attach_policy(pool, ExclusionPolicy(CatalogStore.open(root), mode, resolved['bundles'][mode][1]['content_id']))
        if not pool.maps: raise ConfigError('no eligible maps in managed difficulty range or exclusion policy')
        return pool
    path = contained(root, room['catalog'])
    inspect_catalog(path, room['preset'])
    spec = PRESETS[room['preset']]
    options = dict(sr_min=room['sr_min'], sr_max=room['sr_max'], max_length=240,
                   min_passcount=1000, max_map_age_years=0, limit=200,
                   ratings_db=str(ratings) if ratings is not None else None,ratings_reader=reader)
    if spec['ruleset'] == 3:
        pool = ManiaPool(str(path), mania_keys=spec['keys'], mania_farm_enabled=False, mania_farm_db='', **options)
    else:
        pool = MapPool(str(path), **options)
    if not pool.maps:
        raise ConfigError('no eligible maps in configured difficulty range')
    return pool
