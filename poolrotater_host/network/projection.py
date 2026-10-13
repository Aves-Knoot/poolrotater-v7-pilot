"""Local privacy boundary. Construct wire fields; never upload raw room status."""
import os
import stat

from ..layout import contained
from .contracts import decode, room, versions, number, require, NetworkError


def verified_visibility(worker, row):
    """Only a fresh, matching room with a confirmed empty password is public."""
    if not (worker and worker.fresh() and worker.status.get('ready') is True
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
    if type(generated) not in (int, float) or not 0 <= now-generated < 15:
        return result
    for settings in config['rooms']:
        alias = settings['alias']; row = snapshot['rooms'].get(alias)
        if type(row) is not dict or row.get('network_visibility') != 'public' or row.get('healthy') is not True:
            continue
        status = row.get('status'); age = row.get('heartbeat_age')
        if (type(status) is not dict or type(age) not in (int, float) or not 0 <= age < 15
                or status.get('ready') is not True or status.get('running') is not True
                or row.get('room_id') != status.get('room_id')):
            continue
        public = status.get('public'); details = status.get('network')
        if type(public) is not dict or type(details) is not dict:
            continue
        state = {'IDLE': 'idle', 'STARTING': 'starting', 'PLAYING': 'playing'}.get(status.get('state'))
        if state is None:continue
        if status.get('paused') is True:state = 'draining'
        current = public.get('current_map')
        if current is not None:
            if type(current) is not dict:continue
            current = {'beatmap_id': current.get('beatmap_id'), 'title': current.get('label'), 'stars': current.get('stars')}
        observation = {'alias': alias, 'osu_room_id': row.get('room_id'), 'preset': settings['preset'],
            'state': state, 'occupants': status.get('players'), 'visibility': 'public',
            'title': details.get('title'), 'min_stars': details.get('min_stars'), 'max_stars': details.get('max_stars'),
            'observed_at': generated-age, 'code_version': status.get('version'),
            'catalog_version': details.get('catalog_version',catalog_version), 'current_map': current}
        try:room(observation)
        except NetworkError:continue
        if observation['osu_room_id'] in {r['osu_room_id'] for r in result['rooms']}:continue
        result['rooms'].append(observation)
    pinned={r['catalog_version'] for r in result['rooms']}
    if len(pinned)==1:result['catalog_version']=pinned.pop()
    return result
