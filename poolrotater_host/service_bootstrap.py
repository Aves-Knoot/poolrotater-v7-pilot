"""Standalone fixture bootstrap. Start with system Python -I -S, before the venv."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


def _protected(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts: raise ValueError('noncanonical path')
    for item in (path, *path.parents):
        mode = item.lstat()
        if (stat.S_ISLNK(mode.st_mode) or mode.st_uid != 0 or mode.st_mode & 0o022
                or os.access(item, os.W_OK)):
            raise ValueError('unprotected path')
    return path


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result: raise ValueError('duplicate field')
        result[key] = value
    return result


def _json(path, owner, *, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding='utf-8') as stream:
        mode = os.fstat(stream.fileno())
        if (not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1 or mode.st_uid != owner
                or mode.st_size > 2 * 1024**2 or mode.st_mode & (0o077 if private else 0o022)):
            raise ValueError('unsafe control file')
        return json.load(stream, object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))


def _hash(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def _code(record, prefix):
    release = _protected(Path(record['release']))
    if release.parent != prefix / 'releases': raise ValueError('foreign release')
    snapshot = record['release_snapshot']
    if set(snapshot) != {'files', 'links'} or snapshot['links'] != {}: raise ValueError('invalid code snapshot')
    actual = {}
    for path in release.rglob('*'):
        _protected(path)
        if path.is_dir(): continue
        mode = path.lstat()
        if not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1: raise ValueError('unsafe code file')
        actual[path.relative_to(release).as_posix()] = _hash(path)
    if actual != snapshot['files']: raise ValueError('code inventory changed')
    return release


def preflight(root, prefix, *, prepared=False):
    """No imports from the selected release until ALL its code hashes pass."""
    root, prefix = Path(root).absolute(), _protected(Path(prefix))
    for path in (root, root / 'state'):
        mode = path.lstat()
        if (not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.geteuid()
                or stat.S_IMODE(mode.st_mode) != 0o700): raise ValueError('private host required')
    for path in (root, *root.parents):
        if path.is_symlink(): raise ValueError('linked operator root')
    marker = _json(root / '.poolrotater-host.json', os.geteuid(), private=True)
    pointer = _json(root / 'state/runtime-installation.json', os.geteuid(), private=True)
    expected = {'format': 1, 'installation_id': marker['installation_id'],
                'generation': marker['generation'], 'prefix': str(prefix)}
    if (pointer != expected or type(pointer['format']) is not int
            or not isinstance(marker['installation_id'], str)
            or not re.fullmatch('[a-f0-9-]{36}', marker['installation_id'])):
        raise ValueError('foreign runtime pointer')
    registration = _protected(prefix / 'installations' / (marker['installation_id'] + '.json'))
    record = _json(registration, 0)
    if (any(record.get(k) != v for k, v in {**expected, 'service_uid': os.geteuid(), 'host_root': str(root)}.items())
            or record.get('execution') not in ('offline-fixture', 'pilot-candidate', 'pilot-qualification', 'pilot-live') or record.get('live_ready') is not False):
        raise ValueError('foreign protected registration')
    transition = _json(prefix / 'transitions' / (marker['installation_id'] + '.json'), 0) if (
        prefix / 'transitions' / (marker['installation_id'] + '.json')).exists() else None
    if transition and transition.get('phase') == 'prepared': raise ValueError('pending transition')
    release = _code(record, prefix)
    sys.path.insert(0, str(release))
    from poolrotater_host.pilot_identity import require_entry
    require_entry(record['execution'], prepared=prepared)
    from poolrotater_host.provisioning import load_provisioned
    if prepared: return load_provisioned(root, release=release, prepared=True)
    return load_provisioned(root, release=release)


def main():
    if os.geteuid() == 0:
        raise ValueError('isolated service fixture required')
    prefix = _protected(Path(__file__).absolute().parent)
    _protected(Path(__file__).absolute())
    record = _json(_protected(prefix / 'bootstrap.json'), 0)
    if record != {'format': 1, 'sha256': _hash(__file__)}: raise ValueError('bootstrap changed')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--pilot-check', action='store_true', help='check the protected pilot candidate without upstream I/O')
    mode.add_argument('--service', action='store_true')
    mode.add_argument('--provider-service', action='store_true',
                      help='prepared fixed provider service; upstream execution is disabled')
    mode.add_argument('--network-action', choices=('configure','enroll','resume','rotate','leave','reset','status','support'),
                      help='independent installed network operation; never provider authorization')
    mode.add_argument('--network-agent',action='store_true',help='independent optional heartbeat agent')
    mode.add_argument('--ratings-action',choices=('configure','sync','fetch','status','support'))
    parser.add_argument('--ratings-input',action='store_true',help='explicit bounded ratings JSON on stdin')
    parser.add_argument('--network-input',action='store_true',help='explicit bounded network JSON on stdin')
    mode.add_argument('--ownership-recovery', choices=('review', 'clear', 'resume'),
                      help='explicit stopped room ownership recovery; no room mutations')
    mode.add_argument('--ownership-local-status', action='store_true',
                      help='read local journal and recovery receipt without provider requests')
    mode.add_argument('--calculator-check', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--probe-candidate', type=Path, help=argparse.SUPPRESS)
    mode.add_argument('--authorization-fixture', choices=('status', 'authorize', 'cancel', 'timeout',
                      'candidate', 'recover', 'lost-reply', 'waiting-kill', 'promotion-kill', 'pacing-recover'),
                      help=argparse.SUPPRESS)
    mode.add_argument('--operator-authorization', choices=('configure', 'status', 'authorize',
                      'cancel', 'recover', 'pacing-recover'),
                      help='prepared installed operator workflow; live authorization is disabled')
    mode.add_argument('--operator-authorization-fixture', choices=('configure', 'status', 'authorize',
                      'cancel', 'timeout', 'candidate', 'recover', 'lost-reply', 'pacing-recover'),
                      help=argparse.SUPPRESS)
    mode.add_argument('--provider-room-fixture', choices=('run', 'reconnect', 'lost-create', 'lost-close-recover', 'service'),
                      help=argparse.SUPPRESS)
    parser.add_argument('--credentials-stdin', action='store_true',
                        help='read explicit private client JSON on stdin for operator configure')
    parser.add_argument('--expected-user-id', type=int)
    parser.add_argument('--redirect-uri')
    parser.add_argument('--operator-mode', choices=('local', 'ssh'), default='local')
    parser.add_argument('--callback-timeout', type=float, default=60)
    parser.add_argument('--pacing-state-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--reauthorize', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--replace-pending', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--journal-sha256')
    parser.add_argument('--room-alias')
    parser.add_argument('--created-room-id', type=int)
    parser.add_argument('--confirm-lost-creation', action='store_true')
    parser.add_argument('--transaction-id')
    parser.add_argument('--decision', choices=('complete', 'abort'))
    parser.add_argument('--verified-stage', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    prepared = bool(args.operator_authorization or args.provider_service or args.pilot_check or args.ownership_recovery or args.ownership_local_status
                    or args.network_action or args.network_agent or args.ratings_action)
    if args.ratings_input != (args.ratings_action=='configure'):
        raise ValueError('ratings private input requires explicit configure action')
    if args.network_input != (args.network_action in ('configure','enroll','reset')):
        raise ValueError('network private input requires explicit network action')
    if not args.ownership_recovery and any((args.journal_sha256, args.room_alias,
            args.created_room_id, args.confirm_lost_creation, args.transaction_id, args.decision)):
        raise ValueError('ownership choices require explicit recovery')
    if not prepared and (os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1'
            or os.environ.get('POOLROTATER_PROVISIONING_SANDBOX') != '1'):
        raise ValueError('isolated service fixture required')
    configure_operator = args.operator_authorization == 'configure'
    if (configure_operator != args.credentials_stdin
            or configure_operator != (args.expected_user_id is not None)
            or configure_operator != (args.redirect_uri is not None)):
        raise ValueError('operator configure requires explicit stdin and profile metadata')
    new_attempt = (args.operator_authorization == 'authorize'
                   or (args.operator_authorization_fixture is not None
                       and args.operator_authorization_fixture not in ('configure', 'status', 'recover', 'pacing-recover'))
                   or (args.authorization_fixture is not None
                       and args.authorization_fixture not in ('status', 'recover', 'pacing-recover')))
    if (args.reauthorize or args.replace_pending) and not new_attempt:
        raise ValueError('authorization choices require a new fixture attempt')
    pacing_recovery = 'pacing-recover' in (args.authorization_fixture,
                                         args.operator_authorization, args.operator_authorization_fixture)
    if pacing_recovery != (args.pacing_state_sha256 is not None):
        raise ValueError('explicit pacing state required only for pacing recovery')
    if (args.operator_mode != 'local' or args.callback_timeout != 60) and not (
            args.operator_authorization or args.operator_authorization_fixture):
        raise ValueError('operator options require the operator workflow')
    runtime = preflight(args.root, prefix, prepared=prepared)
    if args.provider_service:
        # Verified code, then the immutable release fence, before venv exec,
        # provider/private inputs, TLS, signals, processes or room activity.
        from poolrotater_host.provider_contract import require_live_provider
        from poolrotater_host.pilot_identity import provider_execution
        require_live_provider(provider_execution(runtime))
    if args.calculator_check and os.environ.get('POOLROTATER_CALCULATOR_QUALIFICATION') != '1':
        raise ValueError('dedicated native calculator fixture required')
    if args.check or args.pilot_check:
        if args.pilot_check and runtime['registration']['execution'] not in ('pilot-candidate', 'pilot-live'):
            raise ValueError('pilot check requires a fresh candidate installation')
        live = runtime['registration']['execution'] == 'pilot-live'
        if live:
            from poolrotater_host.provider_contract import require_live_provider
            from poolrotater_host.pilot_identity import provider_execution
            require_live_provider(provider_execution(runtime))
        print(json.dumps({'ok': True, 'uid': os.geteuid(), 'version': runtime['manifest']['version'],
                          'provider_dispatch_enabled': live, 'live_ready': False}))
        return
    if not args.verified_stage:
        env = {'PATH': str(runtime['environment'] / 'bin') + ':/usr/lib/dotnet:/usr/bin:/bin',
               'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'MALLOC_ARENA_MAX': '2',
               'DOTNET_ROOT': '/usr/lib/dotnet', 'DOTNET_CLI_TELEMETRY_OPTOUT': '1',
               'DOTNET_GCHeapHardLimit': '0x8000000'}
        if args.provider_service:
            # Keep only explicitly configured owner integration credentials.
            # The fixed systemd EnvironmentFile is separately provisioned;
            # never discover production files or inherit arbitrary variables.
            from poolrotater_host.owner_integrations import environment_names
            for name in environment_names(args.root):
                if name in os.environ:
                    env[name] = os.environ[name]
        if not prepared:
            env.update(POOLROTATER_OFFLINE_SANDBOX='1', POOLROTATER_PROVISIONING_SANDBOX='1')
        if args.calculator_check: env['POOLROTATER_CALCULATOR_QUALIFICATION'] = '1'
        command = [str(runtime['environment'] / 'bin/python'), '-I', '-B', str(Path(__file__).absolute()),
                   '--root', str(args.root), '--verified-stage']
        if args.service: command += ['--service']
        elif args.provider_service: command += ['--provider-service']
        elif args.network_agent:command += ['--network-agent']
        elif args.ratings_action:
            command += ['--ratings-action',args.ratings_action]
            if args.ratings_input:command += ['--ratings-input']
        elif args.network_action:
            command += ['--network-action',args.network_action]
            if args.network_input:command += ['--network-input']
        elif args.ownership_local_status: command += ['--ownership-local-status']
        elif args.ownership_recovery:
            command += ['--ownership-recovery', args.ownership_recovery]
            for option, value in (('--journal-sha256', args.journal_sha256),
                    ('--room-alias', args.room_alias), ('--created-room-id', args.created_room_id),
                    ('--transaction-id', args.transaction_id), ('--decision', args.decision)):
                if value is not None: command += [option, str(value)]
            if args.confirm_lost_creation: command += ['--confirm-lost-creation']
        elif args.calculator_check: command += ['--calculator-check']
        elif args.operator_authorization or args.operator_authorization_fixture:
            if args.operator_authorization:
                command += ['--operator-authorization', args.operator_authorization]
            else:
                command += ['--operator-authorization-fixture', args.operator_authorization_fixture]
            command += ['--operator-mode', args.operator_mode,
                        '--callback-timeout', str(args.callback_timeout)]
            if args.credentials_stdin:
                command += ['--credentials-stdin', '--expected-user-id', str(args.expected_user_id),
                            '--redirect-uri', args.redirect_uri]
            if args.reauthorize: command += ['--reauthorize']
            if args.replace_pending: command += ['--replace-pending']
            if args.pacing_state_sha256 is not None:
                command += ['--pacing-state-sha256', args.pacing_state_sha256]
        elif args.provider_room_fixture:
            command += ['--provider-room-fixture', args.provider_room_fixture]
        elif args.authorization_fixture:
            command += ['--authorization-fixture', args.authorization_fixture]
            if args.reauthorize: command += ['--reauthorize']
            if args.replace_pending: command += ['--replace-pending']
            if args.pacing_state_sha256 is not None:
                command += ['--pacing-state-sha256', args.pacing_state_sha256]
        else: command += ['--probe-candidate', str(args.probe_candidate)]
        os.execve(command[0], command, env)
    if args.network_action:
        from poolrotater_host.network.installed import run as network_action
        print(json.dumps(network_action(args.root,runtime['release'],args.network_action,
                         stream=sys.stdin.buffer if args.network_input else None)))
        return
    if args.ratings_action:
        from poolrotater_host.ratings.installed import run as ratings_action
        print(json.dumps(ratings_action(args.root,runtime['release'],args.ratings_action,
                         stream=sys.stdin.buffer if args.ratings_input else None)))
        return
    if args.network_agent:
        from poolrotater_host.network.installed import serve as network_agent
        network_agent(args.root,runtime['release']);return
    if args.ownership_local_status:
        from poolrotater_host.installed_room_recovery import local_status
        print(json.dumps(local_status(args.root)))
        return
    if args.ownership_recovery:
        from poolrotater_host.installed_room_recovery import run as recover_rooms
        print(json.dumps(recover_rooms(args.root, runtime['release'], args.ownership_recovery,
            journal_sha256=args.journal_sha256, alias=args.room_alias, room_id=args.created_room_id,
            confirm_lost_creation=args.confirm_lost_creation,
            transaction_id=args.transaction_id, decision=args.decision)))
        return
    if args.operator_authorization:
        from poolrotater_host.installed_operator_workflow import run as operator_authorization
        def private_notice(session):
            print(json.dumps({'authorization_url': session.authorization_url,
                              'instructions': session.instructions()}), flush=True)
        print(json.dumps(operator_authorization(args.root, runtime['release'], args.operator_authorization,
                         credentials_stream=sys.stdin.buffer if args.credentials_stdin else None,
                         expected_user_id=args.expected_user_id, redirect_uri=args.redirect_uri,
                         mode=args.operator_mode, timeout=args.callback_timeout, notice=private_notice,
                         reauthorize=args.reauthorize, replace_pending=args.replace_pending,
                         pacing_state_sha256=args.pacing_state_sha256)))
        return
    if args.provider_service:
        from poolrotater_host.provider_service import run_fixed_provider_service
        run_fixed_provider_service(args.root, runtime['release'])
        return
    if args.operator_authorization_fixture:
        from poolrotater_host.testing.installed_operator_workflow import run as operator_fixture
        print(json.dumps(operator_fixture(args.root, runtime['release'], scenario=args.operator_authorization_fixture,
                         mode=args.operator_mode, reauthorize=args.reauthorize,
                         replace_pending=args.replace_pending, pacing_state_sha256=args.pacing_state_sha256)))
        return
    if args.authorization_fixture:
        from poolrotater_host.testing.installed_authorization import run as authorization
        print(json.dumps(authorization(args.root, runtime['release'], scenario=args.authorization_fixture,
                         reauthorize=args.reauthorize, replace_pending=args.replace_pending,
                         pacing_state_sha256=args.pacing_state_sha256)))
        return
    if args.provider_room_fixture:
        if args.provider_room_fixture == 'service':
            from poolrotater_host.testing.provider_service import run as provider_service
            provider_service(args.root, runtime['release'])
            return
        from poolrotater_host.testing.installed_rooms import run as rooms
        print(json.dumps(rooms(args.root, runtime['release'], scenario=args.provider_room_fixture)))
        return
    from poolrotater_host.testing.service import run, probe_candidate, calculator_check
    if args.service: run(args.root, runtime['release'])
    elif args.calculator_check: print(json.dumps(calculator_check(args.root)))
    else: print(json.dumps(probe_candidate(args.root, runtime['release'], args.probe_candidate)))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        # Control file values, token bodies and exception strings never reach stderr.
        label = ('network service' if '--network-agent' in sys.argv or '--network-action' in sys.argv else
                 'pilot service' if '--provider-service' in sys.argv else 'offline service')
        print(label + ' refused: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(2)
