"""Local privacy boundary. Construct wire fields; never upload raw room status."""
import os
import stat

DISPLAY_OBSERVATION_SECONDS = 120

from ..layout import contained
from .contracts import decode, room, versions, number, require, NetworkError


def verified_visibility(worker, row):
    """Display-only grace; control freshness and ownership checks stay strict."""
    if not (worker and worker.display_fresh() and worker.status.get('ready') is True
            and worker.status.get('running') is True
            and row.get('room_id') == worker.status.get('room_id')
            and type(row.get('room_id')) is int and row['room_id'] > 0):
        return 'unknown'
    broker = worker.broker
    if broker.uncertain or not broker.known or broker.password is None:
        return 'unknown'
    return 'public' if not broker.private and broker.password == '' else 'private'


def read_status(runtime):
    """Read only the owned, bounded rolling status file; its writer uses 0640."""
    path = contained(runtime, 'state/rolling/status.json')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) in (0o600, 0o640)
                and info.st_size <= 65536, 'unsafe_network_observation')
        return decode(handle.read(65537))


def project(snapshot, config, *, now, code_version, catalog_version='unavailable'):
    """Unknown/private/malformed/stale rooms contribute no identifying fields."""
    result = {'code_version': code_version, 'catalog_version': catalog_version, 'rooms': []}
    versions(result); number(now)
    if type(snapshot) is not dict or type(snapshot.get('rooms')) is not dict:
        return result
    generated = snapshot.get('generated_at')
    if type(generated) not in (int, float) or not 0 <= now-generated < DISPLAY_OBSERVATION_SECONDS:
        return result
    for settings in config['rooms']:
        alias = settings['alias']; row = snapshot['rooms'].get(alias)
        if type(row) is not dict or row.get('network_visibility') != 'public':
            continue
        status = row.get('status'); age = row.get('heartbeat_age')
        if (type(status) is not dict or type(age) not in (int, float) or not 0 <= age < DISPLAY_OBSERVATION_SECONDS
                or age + (now-generated) >= DISPLAY_OBSERVATION_SECONDS
                or status.get('ready') is not True or status.get('running') is not True
                or row.get('room_id') != status.get('room_id')):
            continue
        public = status.get('public'); details = status.get('network')
        if type(public) is not dict or type(details) is not dict:
            continue
        state = {'IDLE': 'idle', 'STARTING': 'starting', 'PLAYING': 'playing',
                 'SCORING': 'idle'}.get(status.get('state'))
        if state is None:continue
        if status.get('paused') is True:state = 'draining'
        current = public.get('current_map')
        if current is not None:
            if type(current) is not dict:continue
            label = current.get('label')
            current = {'beatmap_id': current.get('beatmap_id'), 'title': label[:160] if type(label) is str else label, 'stars': current.get('stars')}
        observation = {'alias': alias, 'osu_room_id': row.get('room_id'), 'preset': settings['preset'],
            'state': state, 'occupants': status.get('players'), 'visibility': 'public',
            'title': details.get('title')[:160] if type(details.get('title')) is str else details.get('title'), 'min_stars': details.get('min_stars'), 'max_stars': details.get('max_stars'),
            'observed_at': generated-age, 'code_version': status.get('version'),
            'catalog_version': details.get('catalog_version',catalog_version), 'current_map': current}
        try:room(observation)
        except NetworkError:continue
        if observation['osu_room_id'] in {r['osu_room_id'] for r in result['rooms']}:continue
        from .display import display as validate_display
        import copy
        proposed = copy.deepcopy(details.get("display"))
        if type(proposed) is not dict: proposed = None
        if proposed is not None:
            try: validate_display(proposed)
            except (NetworkError, KeyError, TypeError): proposed = None
        if proposed is not None:
            # A paused/transitioning/mismatched map cannot retain old progress.
            if state != "playing" or not current:
                proposed["match"] = None
                proposed["playing_players"] = None
            elif proposed.get("match") and proposed["match"].get("beatmap_id") != current["beatmap_id"]:
                proposed["match"] = None
        observation["display"] = proposed
        from .display import room_v2
        try: room_v2(observation)
        except NetworkError: observation["display"] = None
        result['rooms'].append(observation)
    pinned={r['catalog_version'] for r in result['rooms']}
    if len(pinned)==1:result['catalog_version']=pinned.pop()
    return result
