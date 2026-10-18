"""Public map metadata and timing, independent of gameplay and OAuth.

Lookups use a protected central display index. No network/API access, operator
catalog copying or long-lived cache is involved. Every refresh observes an
atomic index replacement, so changed catalogs cannot leave stale metadata.
"""
from pathlib import Path
import math
import sqlite3
import stat

MODES = {'standard-auto': 'standard', 'standard-fixed': 'standard',
         'mania-4k': 'mania_4k', 'mania-7k': 'mania_7k'}
EXCLUDED_MAPS = frozenset({1549496})
MAX_INDEX_BYTES = 64 * 1024**2


def finite_number(value, minimum=0, maximum=2**53-1):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def index_identity(path, owner_uid):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('display index symlink refused')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_nlink != 1
            or info.st_mode & 0o222 or not 0 < info.st_size <= MAX_INDEX_BYTES):
        raise ValueError('display index must be protected immutable public metadata')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class DisplayIndex:
    """One read-only connection per refresh; no retained inode or result cache."""
    def __init__(self, path, *, owner_uid=0):
        self.path = Path(path).absolute()
        self.owner_uid = owner_uid
        self.identity = index_identity(self.path, owner_uid)
        self.connection = sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1',
                                          uri=True, timeout=1)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute('PRAGMA query_only=ON')
            self.connection.execute('PRAGMA trusted_schema=OFF')
            if index_identity(self.path, owner_uid) != self.identity:
                raise ValueError('display index changed during open')
        except Exception:
            self.connection.close()
            raise

    def lookup(self, preset, beatmap_id):
        if preset not in MODES or type(beatmap_id) is not int or beatmap_id <= 0 or beatmap_id in EXCLUDED_MAPS:
            return None
        row = self.connection.execute(
            'SELECT beatmapset_id,title,artist,version,total_length,hit_length,sr '
            'FROM display_maps WHERE mode=? AND beatmap_id=?',
            (MODES[preset], beatmap_id)).fetchone()
        if index_identity(self.path, self.owner_uid) != self.identity:
            raise ValueError('display index changed during lookup')
        return dict(row) if row else None

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def map_display(current_map, preset, lookup=None):
    if current_map is None:
        return None
    result = dict(current_map)
    # Published snapshots are observations, not permission to override central
    # duration/art. Preserve observed title/stars for the selected mod variant.
    metadata = None
    if lookup is not None:
        try:
            metadata = lookup(preset, result['beatmap_id'])
        except (OSError, ValueError, sqlite3.Error):
            pass
    if not isinstance(metadata, dict):
        return result
    for field in ('artist', 'version'):
        value = metadata.get(field)
        if type(value) is str and len(value) <= 180 and not any(ord(c) < 32 or ord(c) == 127 for c in value):
            result[field] = value
    for field in ('beatmapset_id', 'total_length', 'hit_length'):
        value = metadata.get(field)
        ceiling = 10**7 if field == 'beatmapset_id' else 86400
        if type(value) is int and 0 < value <= ceiling:
            result[field] = value
    if result.get('beatmapset_id'):
        result['cover_url'] = 'https://assets.ppy.sh/beatmaps/' + str(result['beatmapset_id']) + '/covers/cover.jpg'
    result['metadata_source'] = 'central-display-index'
    return result


def playback(state, current_map, match, *, now, observed_at, fresh, settings=None):
    """Estimate only actual matching PLAYING evidence; no selected-time fallback.

    Hub v2 supplies a bounded heartbeat clock correction for this projection.
    A further future skew over five seconds, impossible ordering, or elapsed
    time beyond duration plus the bounded observation window withdraws timing.
    Counts describe participants observed at match start, never current players.
    """
    if not fresh or state != 'playing' or not current_map or not isinstance(match, dict):
        return None
    raw_start = match.get('started_at')
    skew = match.get('clock_skew_seconds')
    if not finite_number(skew, -30, 30) or not finite_number(raw_start):
        return None
    if not finite_number(observed_at):
        return None
    start = raw_start + skew
    observed_at += skew
    rate = match.get('clock_rate')
    # Freemod has no single player clock. Show a clearly marked nominal 1x
    # estimate from the actual match start, never from selection/countdown time.
    nominal = (rate is None and isinstance(settings, dict)
               and settings.get('freestyle') is True
               and settings.get('required_mods') == [])
    if nominal:
        rate = 1.0
    length = current_map.get('total_length')
    if (type(match.get('playlist_item_id')) is not int or match['playlist_item_id'] <= 0
            or match.get('beatmap_id') != current_map.get('beatmap_id')
            or not finite_number(now) or not finite_number(observed_at)
            or not finite_number(start) or start > now + 5 or start > observed_at + 5
            or not finite_number(rate, .1, 3)
            or not finite_number(length, 1, 86400)):
        return None
    duration = round(length / rate, 3)
    # A long-stale playing observation can only show a bounded end estimate;
    # it cannot continue advertising a song indefinitely after its duration.
    if now - start > duration + 180:
        return None
    return {'started_at': start, 'duration_seconds': duration,
            'estimated_end_at': round(start + duration, 3), 'approximate': True,
            'estimated': True, 'match_id': match['playlist_item_id'],
            'beatmap_id': match['beatmap_id'], 'clock_rate': rate, 'nominal_speed': nominal}


def public_settings(settings):
    """Render-safe projection of an already validated closed public contract."""
    if settings is None:
        return None
    return {key: settings[key] for key in ('ruleset_id', 'keys', 'auto_difficulty',
            'selection_style', 'allowed_mods', 'required_mods', 'freestyle',
            'countdown_seconds')}
