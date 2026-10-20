#!/usr/bin/env python3
"""Small, isolated five-minute sampler for public occupancy graphs.

This process reads only the already-published owner status and community room
directory. It has no database, credentials, network client, or exporter imports.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import stat
import sys
import time

sys.path.insert(0, "/usr/local/lib/poolrotator-ops")

try:
    from network_statistics import live_occupancy
except ModuleNotFoundError:  # package import in tests
    from .network_statistics import live_occupancy

LABELS = ['Standard automatic', 'Standard fixed', 'Mania 7K', 'Mania 4K']
ALIASES = tuple(f'room{i}' for i in range(1, 5))
BUCKET = 300
RETENTION = 31 * 86400
OWNER_TTL = 600
COMMUNITY_TTL = 60
FUTURE_TOLERANCE = 30
MAX_CANONICAL_BYTES = 8 * 1024 * 1024
MAX_POINTS = 12000
CANONICAL_FORMAT = 1
PUBLIC = Path('/srv/poolrotator-wiki/public')
STATE = Path('/var/lib/poolrotator-wiki-analytics')


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _read(path, max_bytes=2 * 1024 * 1024):
    try:
        if path.is_symlink():
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                return None
            raw = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > max_bytes:
            return None
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            return None
        value = json.loads(raw)
        return value
    except (OSError, ValueError, TypeError):
        return None


def _read_canonical(path):
    """Read canonical history strictly; corruption must stop publication."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError('cannot inspect canonical graph history') from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError('canonical graph history is a symlink')
    try:
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_CANONICAL_BYTES):
            raise ValueError('unsafe canonical graph history')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(MAX_CANONICAL_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) > MAX_CANONICAL_BYTES or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError('canonical graph history changed while reading')
        value = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError('cannot read canonical graph history') from exc
    _validate_canonical(value)
    return value


def _valid_rows_strict(rows, width, max_count, *, service_index=None):
    if not isinstance(rows, list) or len(rows) > MAX_POINTS:
        raise ValueError('invalid canonical graph series')
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != width or type(row[0]) is not int:
            raise ValueError('invalid canonical graph row')
        stamp = row[0]
        if stamp in seen:
            raise ValueError('duplicate canonical graph timestamp')
        seen.add(stamp)
        for i, value in enumerate(row[1:], 1):
            if value is None:
                continue
            upper = 1 if service_index == i else max_count
            if type(value) is not int or value < 0 or (upper is not None and value > upper):
                raise ValueError('invalid canonical graph value')
        result.append(row)
    if result != sorted(result, key=lambda row: row[0]):
        raise ValueError('unsorted canonical graph series')
    return result


def _validate_canonical(value):
    if (not isinstance(value, dict) or type(value.get('format')) is not int
            or value.get('format') != CANONICAL_FORMAT):
        raise ValueError('unsupported canonical graph history')
    samples = _valid_rows_strict(value.get('samples'), 6, 512, service_index=5)
    states = _valid_rows_strict(value.get('room_states'), 5, 1)
    totals = _valid_rows_strict(value.get('network_totals'), 2, 13312)
    provenance = value.get('provenance')
    if not isinstance(provenance, list) or len(provenance) > MAX_POINTS:
        raise ValueError('invalid canonical graph provenance')
    seen = set()
    for row in provenance:
        if not isinstance(row, dict) or type(row.get('timestamp')) is not int:
            raise ValueError('invalid graph provenance row')
        stamp = row['timestamp']
        if stamp in seen:
            raise ValueError('duplicate graph provenance timestamp')
        seen.add(stamp)
        if type(row.get('sample_captured')) is not bool or type(row.get('network_captured')) is not bool:
            raise ValueError('invalid graph provenance flags')
        for key in ('owner_generated_at', 'community_generated_at'):
            observed_time = row.get(key)
            if observed_time is not None and not _finite(observed_time):
                raise ValueError('invalid graph provenance time')
        ids, observed = row.get('room_ids'), row.get('room_observed_at')
        if (not isinstance(ids, list) or len(ids) != 4 or not isinstance(observed, list) or len(observed) != 4
                or any(v is not None and (type(v) is not int or v <= 0) for v in ids)
                or any(v is not None and not _finite(v) for v in observed)):
            raise ValueError('invalid graph provenance room details')
    if seen != {row[0] for row in samples}:
        raise ValueError('graph provenance does not match samples')
    if 'pending_public' not in value:
        raise ValueError('canonical graph history has no publication state')


def _feed_fresh(value, now, ttl):
    if not isinstance(value, dict) or value.get('available') is not True:
        return False
    generated, expires = value.get('generated_at'), value.get('expires_at')
    return (_finite(generated) and _finite(expires) and generated <= now + FUTURE_TOLERANCE
            and 0 < expires - generated <= ttl and now < expires)


def _owner_observations(owner, now):
    """Return counts/states/service-known if the feed proves each room value."""
    unknown = ([None] * 4, [None] * 4, None)
    if not _feed_fresh(owner, now, OWNER_TTL):
        return unknown
    service = owner.get('service')
    online = service.get('online') if isinstance(service, dict) else None
    online = online if type(online) is bool else None
    rooms = owner.get('rooms')
    if not isinstance(rooms, list) or len(rooms) != 4:
        return unknown
    by_alias = {}
    for room in rooms:
        if not isinstance(room, dict) or room.get('alias') not in ALIASES or room['alias'] in by_alias:
            return unknown
        by_alias[room['alias']] = room
    if set(by_alias) != set(ALIASES):
        return unknown
    counts, states = [], []
    for alias in ALIASES:
        room = by_alias[alias]
        state = room.get('state')
        if online is False or state == 'OFFLINE':
            counts.append(None)
            states.append(0)
            continue
        observed = room.get('observed_at')
        identity = room.get('room_id')
        players = room.get('players')
        fresh_observation = (_finite(observed) and -FUTURE_TOLERANCE <= now - observed <= OWNER_TTL)
        valid = (online is True and fresh_observation and type(identity) is int and identity > 0
                 and state in ('IDLE', 'STARTING', 'PLAYING', 'SCORING')
                 and type(players) is int and 0 <= players <= 512)
        if valid:
            counts.append(players)
            states.append(1)
        else:
            counts.append(None)
            states.append(None)
    return counts, states, online


def graph_room_states(history, saved, now):
    """Preserve valid state rows and derive only missing timestamps from counts."""
    by_time = {row[0]: row for row in _valid_history(saved, now, 5, 5, 1).values()}
    for row in history:
        stamp = row[0]
        if stamp not in by_time:
            by_time[stamp] = [stamp, *[
                1 if type(row[i]) is int and row[i] >= 0 else 0 if row[5] == 0 else None
                for i in range(1, 5)]]
    return [by_time[stamp] for stamp in sorted(by_time)]


def merge_samples(existing, candidates, now):
    """Merge bounded graph rows by timestamp, retaining at most 31 days."""
    rows = _valid_history(existing, now, 6, 6, 512)
    for row in candidates if isinstance(candidates, list) else ():
        accepted = _valid_history([row], now, 6, 6, 512)
        rows.update(accepted)
    return [rows[t] for t in sorted(rows)]


def _valid_history(rows, now, length, width, max_count=None):
    if not isinstance(rows, list):
        return {}
    result = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != width or type(row[0]) is not int:
            continue
        if not now - RETENTION <= row[0] <= now + FUTURE_TOLERANCE:
            continue
        valid = True
        for index, value in enumerate(row[1:], 1):
            if value is None:
                continue
            limit = 1 if width == 6 and index == 5 else max_count
            if type(value) is not int or value < 0 or (limit is not None and value > limit):
                valid = False
                break
        if valid:
            result[row[0]] = row
    return result


def _atomic(path, payload, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=True, separators=(',', ':'), allow_nan=False) + '\n'
    if len(raw.encode('utf8')) > MAX_CANONICAL_BYTES:
        raise ValueError('graph output exceeds bounded size')
    tmp = path.with_name(path.name + '.tmp.' + secrets.token_hex(8))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dirfd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _network_observation(owner, community, now):
    """Apply room identity/time checks before using network_statistics contract."""
    counts, states, online = _owner_observations(owner, now)
    if online is None or any(v is None and s is None for v, s in zip(counts, states)):
        return None
    try:
        return live_occupancy(owner, community, now)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _pruned(rows, now):
    return [row for row in rows if now - RETENTION <= row[0] <= now + FUTURE_TOLERANCE]


def _legacy_series(state, now):
    samples = list(_valid_history(_read(state / 'observations.json'), now, 6, 6, 512).values())
    samples.sort(key=lambda row: row[0])
    room_states = list(_valid_history(_read(state / 'room-states.json'), now, 5, 5, 1).values())
    room_states.sort(key=lambda row: row[0])
    network_totals = list(_valid_history(_read(state / 'network-occupancy.json'), now, 2, 2, 13312).values())
    network_totals.sort(key=lambda row: row[0])
    room_states = graph_room_states(samples, room_states, now)
    provenance = [_provenance(row[0], None, None, sample_captured=False, network_captured=False)
                  for row in samples]
    return {'format': CANONICAL_FORMAT, 'samples': samples, 'room_states': room_states,
            'network_totals': network_totals, 'provenance': provenance, 'pending_public': None}


def _provenance(timestamp, owner, community, *, sample_captured, network_captured):
    rooms = owner.get('rooms') if isinstance(owner, dict) else None
    by_alias = {row.get('alias'): row for row in rooms if isinstance(row, dict)} if isinstance(rooms, list) else {}
    return {'timestamp': timestamp,
            'sample_captured': bool(sample_captured), 'network_captured': bool(network_captured),
            'owner_generated_at': owner.get('generated_at') if isinstance(owner, dict) and _finite(owner.get('generated_at')) else None,
            'community_generated_at': community.get('generated_at') if isinstance(community, dict) and _finite(community.get('generated_at')) else None,
            'room_ids': [by_alias.get(alias, {}).get('room_id') if type(by_alias.get(alias, {}).get('room_id')) is int
                         and by_alias.get(alias, {}).get('room_id') > 0 else None for alias in ALIASES],
            'room_observed_at': [by_alias.get(alias, {}).get('observed_at') if _finite(by_alias.get(alias, {}).get('observed_at'))
                                 else None for alias in ALIASES]}


def _mark_provenance(history, timestamp, owner, community, *, sample_captured=None, network_captured=None):
    rows = {row['timestamp']: row for row in history['provenance']}
    row = rows.get(timestamp)
    fresh = _provenance(timestamp, owner, community,
                        sample_captured=bool(sample_captured), network_captured=bool(network_captured))
    if row is None:
        row = fresh
    else:
        row.update({key: value for key, value in fresh.items()
                    if key not in ('sample_captured', 'network_captured')})
        if sample_captured is not None:
            row['sample_captured'] = bool(sample_captured)
        if network_captured is not None:
            row['network_captured'] = bool(network_captured)
    rows[timestamp] = row
    history['provenance'] = [rows[key] for key in sorted(rows)]


def _validate_pending(value):
    if value is None:
        return
    if (not isinstance(value, dict) or type(value.get('generated_at')) is not int
            or type(value.get('expires_at')) is not int or value['expires_at'] <= value['generated_at']):
        raise ValueError('invalid pending graph publication')
    current = value.get('network_current')
    if current is not None and (not isinstance(current, dict)
            or any(type(current.get(key)) is not int or current[key] < 0
                   for key in ('total', 'owner', 'community', 'community_rooms'))):
        raise ValueError('invalid pending network observation')


def _combined_totals(history):
    """Project unavailable community as zero using recorded owner observations.

    Raw history stays unchanged. Existing known network totals always win, and
    owner-unknown periods remain unknown. Do not extend network coverage into
    times before combined sampling began.
    """
    totals = {row[0]: list(row) for row in history['network_totals']}
    if not totals:
        return []
    since = min(totals)
    states = {row[0]: row[1:] for row in history['room_states']}
    by_bucket = {stamp // BUCKET: stamp for stamp in totals}
    for row in history['samples']:
        stamp, *values = row
        if stamp // BUCKET < since // BUCKET:
            continue
        counts, online = values[:4], values[4]
        room_states = states.get(stamp, [None] * 4)
        if online == 0:
            owner_total = 0
        elif online == 1 and all(state == 0 or (state == 1 and type(count) is int)
                                 for count, state in zip(counts, room_states)):
            owner_total = sum(count for count, state in zip(counts, room_states) if state == 1)
        else:
            continue
        network_stamp = by_bucket.get(stamp // BUCKET, stamp)
        if network_stamp not in totals or totals[network_stamp][1] is None:
            totals[network_stamp] = [network_stamp, owner_total]
            by_bucket[stamp // BUCKET] = network_stamp
    return [totals[stamp] for stamp in sorted(totals)]


def _history_activity(history, pending):
    samples = history['samples']
    totals = _combined_totals(history)
    return {'schema': 1, 'generated_at': pending['generated_at'], 'expires_at': pending['expires_at'],
            'samples': samples, 'room_states': history['room_states'], 'room_labels': LABELS,
            'history_since': samples[0][0] if samples else None,
            'network_totals': totals, 'network_coverage_since': totals[0][0] if totals else None,
            'network_current': pending['network_current'],
            'network_scope': 'listed_public_rooms_including_spectators',
            'network_unavailable_community': 'zero'}


def _publish_history(history, activity, state, public):
    _atomic(state / 'observations.json', history['samples'], 0o600)
    _atomic(state / 'room-states.json', history['room_states'], 0o600)
    _atomic(state / 'network-occupancy.json', history['network_totals'], 0o600)
    _atomic(public / 'activity.json', activity)


def _run_locked(*, public, state, now):
    canonical_path = state / 'graph-history.json'
    history = _read_canonical(canonical_path)
    if history is None:
        history = _legacy_series(state, now)
    _validate_pending(history.get('pending_public'))

    # A committed pending publication always wins over newer feed contents.
    # It is replayed in full before this run considers a new observation.
    if history['pending_public'] is not None:
        pending = history['pending_public']
        activity = _history_activity(history, pending)
        _publish_history(history, activity, state, public)
        history['pending_public'] = None
        _atomic(canonical_path, history, 0o600)
        return activity

    # Legacy migration retains every valid point and its original timestamp.
    # Retention uses the real clock, not the aligned graph bucket.
    history['samples'] = _pruned(history['samples'], now)
    history['room_states'] = _pruned(history['room_states'], now)
    history['network_totals'] = _pruned(history['network_totals'], now)
    sample_times = {row[0] for row in history['samples']}
    history['provenance'] = [row for row in history['provenance'] if row['timestamp'] in sample_times]

    bucket = int(now // BUCKET) * BUCKET
    bucket_end = bucket + BUCKET
    owner = _read(public / 'status.json')
    community = _read(public / 'community' / 'rooms.json')
    counts, states, online = _owner_observations(owner, now)
    sample_in_bucket = next((row for row in history['samples'] if bucket <= row[0] < bucket_end), None)
    new_sample = sample_in_bucket is None
    if new_sample:
        sample_in_bucket = [bucket, *counts, 1 if online is True else 0 if online is False else None]
        history['samples'].append(sample_in_bucket)
        history['samples'].sort(key=lambda row: row[0])
        # The feed explicitly distinguishes an individually closed room from
        # unavailable evidence while the controller itself remains online.
        history['room_states'].append([bucket, *states])
        history['room_states'].sort(key=lambda row: row[0])
        history['room_states'] = graph_room_states(history['samples'], history['room_states'], now)
        _mark_provenance(history, bucket, owner, community, sample_captured=True, network_captured=False)

    if not any(bucket <= row[0] < bucket_end for row in history['network_totals']):
        current_network = _network_observation(owner, community, now)
        network_stamp = sample_in_bucket[0]
        history['network_totals'].append([network_stamp, current_network['total'] if current_network else None])
        history['network_totals'].sort(key=lambda row: row[0])
        _mark_provenance(history, network_stamp, owner, community,
                         sample_captured=True if new_sample else None, network_captured=True)

    # Missing states at pre-existing count timestamps are derived solely from
    # that count and its service marker; current feed state cannot rewrite them.
    history['room_states'] = graph_room_states(history['samples'], history['room_states'], now)
    history['provenance'].sort(key=lambda row: row['timestamp'])
    # Bound every private series and its provenance before the canonical commit.
    for key in ('samples', 'room_states', 'network_totals', 'provenance'):
        if len(history[key]) > MAX_POINTS:
            raise ValueError('graph history exceeds bounded point count')

    pending = {'generated_at': int(now), 'expires_at': int(now) + OWNER_TTL,
               'network_current': _network_observation(owner, community, now)}
    history['pending_public'] = pending
    _atomic(canonical_path, history, 0o600)
    activity = _history_activity(history, pending)
    _publish_history(history, activity, state, public)
    history['pending_public'] = None
    _atomic(canonical_path, history, 0o600)
    return activity


def run(*, public=PUBLIC, state=STATE, now=None):
    now = time.time() if now is None else float(now)
    if not math.isfinite(now):
        raise ValueError('now must be finite')
    state = Path(state)
    public = Path(public)
    for directory in (state, public):
        if any(parent.is_symlink() for parent in (directory, *directory.parents)):
            raise ValueError('graph directory path contains a symlink')
    state.mkdir(parents=True, exist_ok=True)
    if not state.is_dir() or not public.is_dir():
        raise ValueError('unsafe graph directories')
    lock_path = state / 'graph.lock'
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_CLOEXEC', 0), 0o600)
    try:
        lock_stat = os.fstat(fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise ValueError('unsafe graph lock')
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        return _run_locked(public=public, state=state, now=now)
    finally:
        os.close(fd)


if __name__ == '__main__':
    sys.exit(0 if run() is not None else 0)
