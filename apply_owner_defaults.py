"""Apply the reviewed owner gameplay profile to an explicitly bound stopped two-room host.

Invoke with sudo /usr/bin/python3.12 -I -S apply_owner_defaults.py --release PATH --expected-user-id ID.
Never starts/stops services or changes code, tokens, enrollment or journals.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import uuid

sys.dont_write_bytecode = True
PROFILE_SHA = 'cbe7bccfe11f7fe54dcc81161f92ebf4cdec8600b20179f56e63dc9375fe72d7'
ENTRY_SHA = 'de5933d829e10cc48949153e09ce0c5e31b30ffc9b5231c23211a865f2981600'
RUNTIMES = {
    '199e4e228e833d6ce051d76b47817e558f261d0224d2cd9c88e3d7eadf0b7b81',
    '54b662a08bbfda597ae8db59f21e78a50f1dbf3fe266e7a8f9ca7991c2cef78f',
    '53a1e98f0057a965cdbbb4d1afa4d1434296f91a7492e0d704fdf409717fae64',
}
TOPOLOGY = [('room1', 'standard-auto'), ('room2', 'mania-4k')]


def require_stopped(states):
    for state in states:
        if (state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed')
                or state['UnitFileState'] not in ('', 'disabled', 'not-found')):
            raise ValueError('bot and network must already be stopped and disabled; no service was stopped')


def project(host, profile, expected_user_id=36451):
    """Pure, narrow projection. Preserve every non-range host field."""
    if [(r['alias'], r['preset']) for r in host['rooms']] != TOPOLOGY:
        raise ValueError('expected two: Standard Auto room1 and Mania 4K room2')
    if host['account']['expected_user_id'] not in (None, expected_user_id):
        raise ValueError('host account does not match explicit expected operator')
    result = copy.deepcopy(host)
    patches = {}
    for room in result['rooms']:
        preset = profile['presets'][room['preset']]
        room.update(sr_min=preset['sr_min'], sr_max=preset['sr_max'])
        patches[room['alias']] = copy.deepcopy(preset['settings'])
        if 'room_name' in patches[room['alias']]:
            raise ValueError('profile must preserve operator titles')
    return result, {'rooms': patches}


def require_account(marker, account, expected_user_id=36451):
    if (type(account.get('user_id')) is not int or account['user_id'] != expected_user_id
            or account.get('format') != 1 or account.get('execution') != 'provider-live-pilot'
            or any(account.get(k) != marker[k] for k in ('installation_id', 'generation'))):
        raise ValueError('verified local provider account must match explicit operator and installation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True, type=Path)
    parser.add_argument('--instance', default='two')
    parser.add_argument('--expected-user-id', required=True, type=int)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('administrator required')
    release = args.release.absolute()
    import re
    if not re.fullmatch('[a-z][a-z0-9-]{0,15}', args.instance) or args.expected_user_id <= 0:
        raise ValueError('invalid explicit instance/account binding')
    if release.parent != Path('/opt/poolrotater-v7-pilot') / args.instance / 'releases':
        raise ValueError('release does not belong to the explicit installation')
    # Verify bootstrap custody and its independently pinned digest before import.
    entry = release / 'poolrotater_host/pilot_admin_entry.py'
    import stat
    for path in (entry, *entry.parents):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unprotected installed bootstrap')
    if hashlib.sha256(entry.read_bytes()).hexdigest() != ENTRY_SHA:
        raise ValueError('unsupported administrative bootstrap')
    bootstrap = runpy.run_path(str(entry), run_name='verified_bootstrap')
    if bootstrap['verified_release'](args.instance) != release:
        raise ValueError('release is not the registered current runtime')
    profile_bytes = Path(__file__).with_name('owner-defaults.json').read_bytes()
    if hashlib.sha256(profile_bytes).hexdigest() != PROFILE_SHA:
        raise ValueError('settings profile checksum mismatch')
    profile = json.loads(profile_bytes)
    sys.path.insert(0, str(release))
    from poolrotater_host.pilot_installation import administrative_binding, _stopped, require_no_service_processes
    from poolrotater_host import pilot_operations as pilot
    from poolrotater_host.network import operations as network
    from poolrotater_host.layout import atomic_json, file_lease, load, read_json
    from poolrotater_host.config import validate
    from poolrotater_host.owner_settings import load_overrides, validate_record, RECORD
    from poolrotater_host.room_settings_operations import configure
    from poolrotater_host.ratings.operations import _identity
    target, marker, record = administrative_binding(args.instance)
    if record['bundle_sha256'] not in RUNTIMES:
        raise ValueError('only registered dev10/dev11/dev12 runtimes supported')
    root, uid = target['root'], record['service_uid']
    with file_lease(target['prefix'] / 'network-admin.lock'):
        pilot._unit('two', optional=True)
        network._checked_unit('two', optional=True)
        require_stopped((pilot._service_state(args.instance), network._state(args.instance)))
        require_no_service_processes(uid)
        with _stopped(root, uid):
            require_no_service_processes(uid)
            with _identity(uid):
                _, before = load(root)
                # New hosts may leave host.expected_user_id null; the completed
                # local authorization record is the actual account binding.
                # Read identity state only, never OAuth credentials or tokens.
                require_account(marker, read_json(root / 'state/provider-oauth-account.json'), args.expected_user_id)
                after, patch = project(before, profile, args.expected_user_id)
                validate(after)
                old = load_overrides(root, marker, before)
                merged = {r['alias']: {'preset': r['preset'], 'settings': {
                    **old.get(r['alias'], {}).get('settings', {}), **patch['rooms'][r['alias']]}}
                    for r in after['rooms']}
                # Validate both complete outputs before writing either file.
                validate_record({'format': 1, **{k: marker[k] for k in ('installation_id', 'generation')},
                                 'rooms': merged}, marker, after)
                backup = root / 'config' / ('tuning-backup-' + str(uuid.uuid4()))
                backup.mkdir(mode=0o700)
                atomic_json(backup / 'host.json', before)
                if (root / RECORD).exists():
                    atomic_json(backup / 'gameplay.json', read_json(root / RECORD))
                # These are separate atomic config writes, not a two-file transaction.
                # An interrupted run remains stopped and can be rerun after review.
                result = configure(root, patch)
                atomic_json(root / 'config/host.json', after)
                _, saved = load(root)
                actual = load_overrides(root, marker, saved)
                if saved != after or actual != merged:
                    raise ValueError('saved defaults verification failed; retain backup and inspect before start')
    return {'ok': True, 'instance': args.instance, 'scope': 'saved_gameplay_defaults',
            'rooms': [{'alias': r['alias'], 'preset': r['preset'],
                       'sr_min': r['sr_min'], 'sr_max': r['sr_max']} for r in after['rooms']],
            'changed_settings': result['changed_settings'], 'backup': str(backup),
            'protected_runtime_checked': True, 'provider_dispatch': False,
            'services_started': False, 'takes_effect': 'next_start'}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), sort_keys=True))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'services_started': False,
                          'next_action': 'keep_stopped; inspect configuration and backup before starting'}))
        raise SystemExit(2)
