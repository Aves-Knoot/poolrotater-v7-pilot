"""Root rescue entry, pinned separately from the rotating registration pointer."""
import sys
sys.dont_write_bytecode = True
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat


def protected(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('recovery protected path required')
    return path


def read(path):
    protected(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024**2:
        raise ValueError('recovery record refused')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate recovery field')
            result[key] = value
        return result
    return json.loads(path.read_bytes(), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def verify(record, prefix):
    release = protected(Path(record['release']))
    if (record['prefix'] != str(prefix) or release.parent != prefix / 'releases'
            or record['execution'] not in ('pilot-live', 'pilot-candidate')):
        raise ValueError('recovery code identity changed')
    files = {}
    for path in release.rglob('*'):
        protected(path)
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('recovery code type refused')
        with path.open('rb') as stream:
            files[path.relative_to(release).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    if record['release_snapshot'] != {'files': files, 'links': {}}:
        raise ValueError('recovery code changed')
    return release


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--instance', required=True)
    parser.add_argument('--action', choices=('tick', 'install', 'status', 'pause', 'resume'), default='tick')
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch('[a-z][a-z0-9-]{0,15}', args.instance):
        raise ValueError('recovery administrator and instance required')
    prefix = protected(Path('/opt/poolrotater-v7-pilot') / args.instance)
    entry = Path(__file__).absolute()
    if args.action == 'install':
        if entry.parent.parent.parent != prefix / 'releases':
            raise ValueError('recovery install entry refused')
        intent = read(prefix / 'pilot-install.json')
        folder = prefix / 'candidates' / intent['installation_id']
        paths = [prefix / 'installations' / (intent['installation_id'] + '.json')]
        if folder.exists():
            candidates = list(protected(folder).glob('*.json'))
            if len(candidates) > 128:
                raise ValueError('recovery candidate budget exceeded')
            paths += candidates
        records = [read(path) for path in paths]
        matches = [r for r in records if r.get('release') == str(entry.parent.parent)]
        if not matches or any(r != matches[0] for r in matches):
            raise ValueError('recovery registered candidate required')
        record = matches[0]
    else:
        if entry != prefix / 'recovery-bootstrap.py':
            raise ValueError('recovery launcher location refused')
        config = read(prefix / 'recovery/configuration.json')
        record = config['record']
    release = verify(record, prefix)
    sys.path.insert(0, str(release))
    from poolrotater_host.runtime_lifecycle import validate_runtime
    runtime = validate_runtime(record, prefix)
    python = runtime['environment'] / 'bin/python'
    if Path(sys.executable).absolute() != python:
        command = [str(python), '-I', '-B', str(entry), '--instance', args.instance, '--action', args.action]
        os.chdir('/tmp')
        os.execve(command[0], command, {'PATH': '/usr/sbin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
                                      'PYTHONDONTWRITEBYTECODE': '1', 'MALLOC_ARENA_MAX': '2'})
    from poolrotater_host.rescue import operations
    if args.action == 'install':
        result = operations.install(args.instance, record)
    elif args.action == 'tick':
        result = operations.tick(args.instance, record)
    elif args.action == 'status':
        result = operations.Backend(args.instance, record).observe()
    elif args.action == 'pause':
        operations.pause(args.instance)
        result = {'ok': True, 'allow_recovery': False}
    else:
        from poolrotater_host.layout import atomic_json, file_lease
        with file_lease(prefix / 'recovery/policy.lock'):
            path = prefix / 'recovery/configuration.json'
            config = read(path)
            atomic_json(path, {**config, 'allow_recovery': True})
        result = {'ok': True, 'allow_recovery': True}
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"ok":false,"error":"recovery_bootstrap_refused"}')
        raise SystemExit(2)
