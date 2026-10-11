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
from .layout import load, setup


def main(argv=None):
    parser = argparse.ArgumentParser(prog='poolrotater-host', description=__doc__)
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    download = commands.add_parser('catalog-download', help='verify a prepared catalog over explicitly trusted HTTPS')
    download.add_argument('--origin', required=True)
    download.add_argument('--cache', required=True, type=Path)
    download.add_argument('--trust-root', required=True, type=Path)
    download.add_argument('--trust-sha256', required=True)
    download.add_argument('--ca-file', required=True, type=Path)
    download.add_argument('--ca-sha256', required=True)
    download.add_argument('--destination', required=True, type=Path)
    download.add_argument('--mode', required=True, choices=('standard', 'mania4', 'mania7'))
    for action in ('install', 'check', 'status', 'support', 'unit', 'configure', 'authorize', 'cancel',
                   'recover', 'pacing-recover', 'catalog-import', 'catalog-recover', 'start', 'stop', 'remove'):
        command = commands.add_parser('pilot-' + action, help='explicit protected pilot ' + action)
        command.add_argument('--instance', required=True)
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
        if action == 'catalog-import':
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument('--repository', type=Path)
            source.add_argument('--origin')
            command.add_argument('--ca-file', type=Path)
            command.add_argument('--ca-sha256')
            command.add_argument('--trust-root', required=True, type=Path)
            command.add_argument('--trust-sha256', required=True)
            command.add_argument('--mode', required=True, action='append', choices=('standard', 'mania4', 'mania7'))
    for name in ('setup', 'check', 'status', 'config', 'runtime-config', 'simulate'):
        command = commands.add_parser(name)
        command.add_argument('--root', type=Path, required=True, help='explicit private host directory')
        if name == 'setup': command.add_argument('--preset', action='append', choices=PRESETS)
        if name == 'simulate': command.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    try:
        if args.command.startswith('pilot-'):
            from . import pilot_operations as pilot
            action = args.command.removeprefix('pilot-')
            if action == 'install':
                from .pilot_installation import install
                result = install(args.instance, args.bundle, args.sha256, presets=args.preset, **({'live_pilot': True} if args.live_pilot else {}))
            elif action in ('check', 'status', 'support', 'catalog-recover'):
                result = getattr(pilot, action.replace('-', '_'))(args.instance)
            elif action == 'unit': result = pilot.install_unit(args.instance)
            elif action == 'remove': result = pilot.remove_unit(args.instance)
            elif action in ('start', 'stop'): result = pilot.lifecycle(args.instance, action)
            elif action == 'catalog-import':
                if (args.origin is None) != (args.ca_file is None and args.ca_sha256 is None):
                    raise ConfigError('HTTPS catalog import requires explicit CA file and digest')
                repository = args.repository
                if args.origin is not None:
                    repository = pilot.https_repository(args.instance, args.origin, args.ca_file, args.ca_sha256)
                result = pilot.import_catalogs(args.instance, repository=repository,
                    trust_root=args.trust_root, trust_sha256=args.trust_sha256, modes=args.mode)
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
    except (ConfigError, OSError, ValueError, sqlite3.Error) as exc:
        # Deliberate config messages are safe; arbitrary OS/data errors can expose paths.
        detail = str(exc) if isinstance(exc, ConfigError) else type(exc).__name__
        print(json.dumps({'ok': False, 'error': detail}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
