"""Standalone root administration bootstrap; invoke with system Python -I -S.

Only verified registered code may be imported. The first stage verifies the
entire pinned dependency environment before entering its interpreter.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
sys.dont_write_bytecode = True


def _protected(path):
    for item in (path, *path.parents):
        st = item.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError('unprotected administrative code')
    return path


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result: raise ValueError('duplicate administrative record field')
        result[key] = value
    return result


def _json(path):
    _protected(path)
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 2 * 1024**2:
        raise ValueError('invalid administrative record')
    return json.loads(path.read_bytes(), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def verified_release(instance):
    import re
    if type(instance) is not str or not re.fullmatch('[a-z][a-z0-9-]{0,15}', instance):
        raise ValueError('invalid administrative instance')
    release = _protected(Path(__file__).absolute().parent.parent)
    prefix = _protected(Path('/opt/poolrotater-v7-pilot') / instance)
    intent = _json(prefix / 'pilot-install.json')
    import uuid
    if str(uuid.UUID(intent['installation_id'])) != intent['installation_id']:
        raise ValueError('invalid administrative installation identity')
    record = _json(prefix / 'installations' / (intent['installation_id'] + '.json'))
    if (record['execution'] not in ('pilot-candidate', 'pilot-live') or record['prefix'] != str(prefix)
            or record['release'] != str(release) or release.parent != prefix / 'releases'):
        raise ValueError('foreign administrative release')
    snapshot = record['release_snapshot']
    if set(snapshot) != {'files', 'links'} or snapshot['links']:
        raise ValueError('invalid administrative code inventory')
    files = {}
    for path in release.rglob('*'):
        _protected(path)
        if path.is_dir(): continue
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ValueError('invalid administrative code type')
        with path.open('rb') as stream:
            files[path.relative_to(release).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    if files != snapshot['files']: raise ValueError('administrative code changed')
    return release


def main():
    if os.geteuid() != 0: raise ValueError('administrator required')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--verified-stage', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('action', choices=('check', 'status', 'support', 'unit', 'configure', 'authorize',
        'cancel', 'recover', 'pacing-recover', 'catalog-import', 'catalog-recover', 'start', 'stop', 'remove',
        'ownership-review', 'ownership-clear', 'ownership-resume'))
    args, rest = parser.parse_known_args()
    release = verified_release(args.instance)
    sys.path.insert(0, str(release))
    from poolrotater_host.pilot_installation import administrative_binding
    _, _, record = administrative_binding(args.instance)
    python = Path(record['environment']) / 'bin/python'
    if not args.verified_stage:
        command = [str(python), '-I', '-B', str(Path(__file__).absolute()), '--verified-stage',
                   '--instance', args.instance, args.action, *rest]
        env = {'PATH': '/usr/sbin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
               'PYTHONDONTWRITEBYTECODE': '1', 'MALLOC_ARENA_MAX': '2'}
        os.execve(command[0], command, env)
    if Path(sys.executable).absolute() != python:
        raise ValueError('administration requires the registered interpreter')
    from poolrotater_host.cli import main as cli
    return cli(['pilot-' + args.action, '--instance', args.instance, *rest])


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception:
        print('protected pilot administration refused', file=sys.stderr)
        raise SystemExit(2)
