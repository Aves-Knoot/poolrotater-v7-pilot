"""Explicit host commands; catalog download never authorizes or starts rooms."""
import argparse
import json
import logging
from pathlib import Path
import sys
import sqlite3
from . import __version__
from .checks import check
from .config import ConfigError, PRESETS
from .catalog_export import MODES as CATALOG_MODES
from .layout import load, setup
from .network.contracts import NetworkError


class _PrivateTestParser(argparse.ArgumentParser):
    def error(self, message):
        # An unsupported password argument must not be echoed by argparse.
        self.exit(2, 'private test arguments refused; use bounded stdin\n')


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else list(argv)
    parser_class = (_PrivateTestParser if any(value.startswith('pilot-private-test-')
                    for value in arguments) else argparse.ArgumentParser)
    parser = parser_class(prog='poolrotater-host', description=__doc__)
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True, parser_class=parser_class)
    download = commands.add_parser('catalog-download', help='verify a prepared catalog over explicitly trusted HTTPS')
    download.add_argument('--origin', required=True)
    download.add_argument('--cache', required=True, type=Path)
    download.add_argument('--trust-root', required=True, type=Path)
    download.add_argument('--trust-sha256', required=True)
    download.add_argument('--ca-file', required=True, type=Path)
    download.add_argument('--ca-sha256', required=True)
    download.add_argument('--destination', required=True, type=Path)
    download.add_argument('--mode', required=True, choices=tuple(sorted(CATALOG_MODES)))
    for action in ('install', 'check', 'code-proof', 'status', 'support', 'unit', 'configure', 'authorize', 'cancel',
                   'recover', 'pacing-recover', 'catalog-import', 'catalog-recover',
                   'catalog-trust-replace', 'catalog-trust-recover', 'start', 'stop', 'remove',
                   'owner-import', 'owner-status', 'owner-rollback', 'owner-budget-prepare',
                   'private-test-configure', 'private-test-clear', 'private-test-status',
                   'ownership-review', 'ownership-clear', 'ownership-resume',
                   'network-configure','network-enroll','network-resume','network-rotate','network-leave','network-reset',
                   'network-reporting-configure','network-reporting-support','network-reporting-sync',
                   'network-status','network-support','network-unit','network-start','network-stop','network-remove',
                   'ratings-migrate','ratings-seed','ratings-recover-stream','ratings-status','ratings-support','ratings-configure',
                   'ratings-reader-configure','ratings-review-epoch','ratings-sync','ratings-fetch',
                   'room-settings-configure','room-settings-status',
                   'managed-status','managed-limit','managed-request','managed-bookings',
                   'catch-pilot-prepare','catch-pilot-status','catch-pilot-resume','catch-pilot-rollback',
                   'worker-stage','worker-queue','worker-cancel','worker-status',
                   'maintenance-request','maintenance-cancel','maintenance-status',
                   'update-configure','update-check','update-download','update-apply','update-pause','update-resume',
                   'update-status','update-tick','update-recover','update-unit','update-start','update-stop','update-cleanup',
        'release-init','release-publish','release-promote','release-renew','release-resume','release-status'):
        command = commands.add_parser('pilot-' + action, help='explicit protected pilot ' + action)
        command.add_argument('--instance', required=True)
        if action in ('managed-limit','managed-request','managed-bookings'):
            command.add_argument('--managed-stdin',required=True,action='store_true')
        if action in ('maintenance-request','maintenance-cancel'):
            command.add_argument('--operation-id',required=True)
        if action == 'maintenance-request':
            command.add_argument('--mode',required=True,choices=('empty','one-map'))
            command.add_argument('--room',action='append')
        if action == 'worker-stage':
            command.add_argument('--bundle', required=True, type=Path)
            command.add_argument('--sha256', required=True)
        if action == 'worker-queue':
            command.add_argument('--sha256', required=True)
            command.add_argument('--room', action='append')
        if action.startswith('release-'):
            command.add_argument('--custody',required=True,type=Path)
            if action=='release-init':command.add_argument('--served',required=True,type=Path)
            if action=='release-publish':
                command.add_argument('--bundle',required=True,type=Path);command.add_argument('--sha256',required=True)
                command.add_argument('--before-bundle',required=True,type=Path);command.add_argument('--before-sha256',required=True)
                command.add_argument('--channel',choices=('pilot','stable'),default='pilot')
            if action=='release-promote':
                command.add_argument('--sha256',required=True);command.add_argument('--channel',required=True,choices=('pilot','stable'))
        if action=='update-recover':
            command.add_argument('--operation-id')
            command.add_argument('--decision',choices=('review','retry','cancel','complete','rollback','supersede'))
        if action == 'update-configure':
            command.add_argument('--updates-stdin', required=True, action='store_true')
        if action == 'private-test-configure':
            command.add_argument('--private-test-stdin', required=True, action='store_true')
        if action == 'start':
            command.add_argument('--preserve-managed-intent',action='store_true',help=argparse.SUPPRESS)
        if action == 'owner-import':
            command.add_argument('--owner-review-stdin', required=True, action='store_true')
            command.add_argument('--snapshot-sha256', required=True)
        if action in ('ratings-configure','ratings-reader-configure','ratings-review-epoch'):command.add_argument('--ratings-stdin',required=True,action='store_true')
        if action in ('room-settings-configure','catch-pilot-prepare'):command.add_argument('--settings-stdin',required=True,action='store_true')
        if action == 'install':
            command.add_argument('--bundle', required=True, type=Path)
            command.add_argument('--sha256', required=True)
            command.add_argument('--preset', action='append', choices=PRESETS)
            command.add_argument('--live-pilot', action='store_true')
        if action == 'configure':
            command.add_argument('--credentials-stdin', required=True, action='store_true')
            command.add_argument('--expected-user-id', required=True, type=int)
            command.add_argument('--redirect-uri', required=True)
        if action in ('configure', 'authorize'):
            command.add_argument('--operator-mode', choices=('local', 'ssh'), default='local')
        if action == 'authorize':
            command.add_argument('--reauthorize', action='store_true')
            command.add_argument('--replace-pending', action='store_true')
        if action == 'pacing-recover': command.add_argument('--pacing-state-sha256', required=True)
        if action == 'ownership-clear':
            command.add_argument('--journal-sha256', required=True)
            command.add_argument('--room-alias')
            command.add_argument('--created-room-id', type=int)
            command.add_argument('--confirm-lost-creation', action='store_true')
        if action == 'ownership-resume':
            command.add_argument('--transaction-id', required=True)
            command.add_argument('--decision', choices=('complete', 'abort'), required=True)
        if action == 'catalog-trust-recover': command.add_argument('--transaction-id', required=True)
        if action == 'catalog-trust-replace':
            command.add_argument('--previous-trust-sha256', required=True)
            command.add_argument('--confirm-replacement', required=True, action='store_true')
        if action in ('catalog-import', 'catalog-trust-replace'):
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument('--repository', type=Path)
            source.add_argument('--origin')
            command.add_argument('--ca-file', type=Path)
            command.add_argument('--ca-sha256')
            command.add_argument('--trust-root', required=True, type=Path)
            command.add_argument('--trust-sha256', required=True)
            if action == 'catalog-import':
                command.add_argument('--mode', required=True, action='append', choices=tuple(sorted(CATALOG_MODES)))
    for name in ('setup', 'check', 'status', 'config', 'runtime-config', 'simulate'):
        command = commands.add_parser(name)
        command.add_argument('--root', type=Path, required=True, help='explicit private host directory')
        if name == 'setup': command.add_argument('--preset', action='append', choices=PRESETS)
        if name == 'simulate': command.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args(arguments)
    logging.basicConfig(level=logging.ERROR)
    try:
        if args.command.startswith('pilot-'):
            from . import pilot_operations as pilot
            action = args.command.removeprefix('pilot-')
            if action == 'install':
                from .pilot_installation import install
                result = install(args.instance, args.bundle, args.sha256, presets=args.preset, **({'live_pilot': True} if args.live_pilot else {}))
            elif action in ('check', 'code-proof', 'status', 'support', 'catalog-recover'):
                result = getattr(pilot, action.replace('-', '_'))(args.instance)
            elif action == 'unit': result = pilot.install_unit(args.instance)
            elif action == 'remove': result = pilot.remove_unit(args.instance)
            elif action.startswith('maintenance-'):
                from .operator_maintenance import command as maintenance_command
                result=maintenance_command(args.instance,action.removeprefix('maintenance-'),
                    operation_id=getattr(args,'operation_id',None),mode=getattr(args,'mode',None),
                    rooms=getattr(args,'room',None))
            elif action.startswith('worker-'):
                from .worker_operations import command as worker_command
                result=worker_command(args.instance,action.removeprefix('worker-'),
                    bundle=getattr(args,'bundle',None),sha256=getattr(args,'sha256',None),
                    rooms=getattr(args,'room',None))
            elif action.startswith('release-'):
                from .updates.authority import command as release_command
                result=release_command(action.removeprefix('release-'),custody=args.custody,
                    served=getattr(args,'served',None),bundle=getattr(args,'bundle',None),sha256=getattr(args,'sha256',None),
                    before_bundle=getattr(args,'before_bundle',None),before_sha256=getattr(args,'before_sha256',None),
                    channel=getattr(args,'channel',None))
            elif action.startswith('update-'):
                from .updates.operations import command as update_command
                result=update_command(args.instance,action.removeprefix('update-'),
                    stream=sys.stdin.buffer if action=='update-configure' else None,
                    operation_id=getattr(args,'operation_id',None),decision=getattr(args,'decision',None))
            elif action.startswith('catch-pilot-'):
                from .catch_pilot_operations import command as catch_command
                result=catch_command(args.instance,action.removeprefix('catch-pilot-'),
                    stream=sys.stdin.buffer if action=='catch-pilot-prepare' else None)
            elif action.startswith('managed-'):
                from .managed_host.operations import command as managed_command
                result=managed_command(args.instance,action.removeprefix('managed-'),
                    stream=sys.stdin.buffer if action in ('managed-limit','managed-request','managed-bookings') else None)
            elif action.startswith('room-settings-'):
                from .room_settings_operations import command as settings_command
                result=settings_command(args.instance,action.removeprefix('room-settings-'),
                    stream=sys.stdin.buffer if action=='room-settings-configure' else None)
            elif action.startswith('ratings-'):
                from .ratings import operations
                operation=action.removeprefix('ratings-')
                if operation in ('configure','reader-configure','review-epoch'):result=operations.command(args.instance,operation,stream=sys.stdin.buffer)
                else:result=operations.command(args.instance,operation)
            elif action.startswith('network-'):
                from .network import operations
                operation=action.removeprefix('network-')
                if operation in ('unit','start','stop','remove'):result=operations.unit(args.instance,operation)
                else:result=operations.command(args.instance,operation,
                    stream=sys.stdin.buffer if operation in ('configure','enroll','reset','reporting-configure') else None)
            elif action.startswith('ownership-'):
                result = pilot.ownership(args.instance, action.removeprefix('ownership-'),
                    journal_sha256=getattr(args, 'journal_sha256', None),
                    alias=getattr(args, 'room_alias', None), room_id=getattr(args, 'created_room_id', None),
                    confirm_lost_creation=getattr(args, 'confirm_lost_creation', False),
                    transaction_id=getattr(args, 'transaction_id', None), decision=getattr(args, 'decision', None))
            elif action in ('start', 'stop'):
                if getattr(args,'preserve_managed_intent',False):
                    result=pilot.lifecycle(args.instance,action,preserve_managed_intent=True)
                else:result=pilot.lifecycle(args.instance,action)
            elif action.startswith('private-test-'):
                from .private_pilot_operations import command as private_test_command
                result = private_test_command(args.instance, action.removeprefix('private-test-'),
                    stream=sys.stdin.buffer if action == 'private-test-configure' else None)
            elif action.startswith('owner-'):
                from .owner_operations import command as owner_command
                result = owner_command(args.instance, action.removeprefix('owner-'),
                    stream=sys.stdin.buffer if action == 'owner-import' else None,
                    snapshot_sha256=getattr(args, 'snapshot_sha256', None))
            elif action == 'catalog-trust-recover':
                result = pilot.recover_catalog_trust(args.instance, transaction_id=args.transaction_id)
            elif action in ('catalog-import', 'catalog-trust-replace'):
                if (args.origin is None) != (args.ca_file is None and args.ca_sha256 is None):
                    raise ConfigError('HTTPS catalog import requires explicit CA file and digest')
                repository = args.repository
                if args.origin is not None:
                    repository = pilot.https_repository(args.instance, args.origin, args.ca_file, args.ca_sha256)
                if action == 'catalog-import':
                    result = pilot.import_catalogs(args.instance, repository=repository,
                        trust_root=args.trust_root, trust_sha256=args.trust_sha256, modes=args.mode)
                else:
                    result = pilot.replace_catalog_trust(args.instance, repository=repository,
                        trust_root=args.trust_root, trust_sha256=args.trust_sha256,
                        previous_trust_sha256=args.previous_trust_sha256,
                        confirm_replacement=args.confirm_replacement)
            else:
                result = pilot.operator(args.instance, action,
                    credentials_stream=sys.stdin.buffer if action == 'configure' else None,
                    expected_user_id=getattr(args, 'expected_user_id', None),
                    redirect_uri=getattr(args, 'redirect_uri', None),
                    mode=getattr(args, 'operator_mode', 'local'),
                    pacing_state_sha256=getattr(args, 'pacing_state_sha256', None),
                    notice=(lambda value: print(json.dumps(value), flush=True)) if action == 'authorize' else None,
                    reauthorize=getattr(args, 'reauthorize', False),
                    replace_pending=getattr(args, 'replace_pending', False))
        elif args.command == 'catalog-download':
            from .catalog_download import download_catalog
            result = download_catalog(url=args.origin, cache=args.cache,
                                      trust_root=args.trust_root, trust_sha256=args.trust_sha256,
                                      ca_file=args.ca_file, ca_sha256=args.ca_sha256,
                                      destination=args.destination, mode=args.mode)
        elif args.command == 'setup':
            result = setup(args.root, args.preset)
        elif args.command == 'check':
            result = check(args.root)
        elif args.command == 'runtime-config':
            from .runtime_config import render
            result = render(args.root)
        elif args.command in ('status', 'config'):
            marker, config = load(args.root)
            result = config if args.command == 'config' else {
                'version': __version__, 'installation_id': marker['installation_id'],
                'execution': 'offline', 'rooms': config['rooms'], 'live_ready': False,
                'network': 'disabled', 'service_installed': False}
        else:
            from .simulation import simulate
            result = simulate(args.root, args.rounds)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result.get('ok', True) else 3
    except (ConfigError, OSError, ValueError, sqlite3.Error, NetworkError) as exc:
        # Deliberate config messages are safe; arbitrary OS/data errors can expose paths.
        detail = exc.code if isinstance(exc,NetworkError) else str(exc) if isinstance(exc, ConfigError) else type(exc).__name__
        print(json.dumps({'ok': False, 'error': detail}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
