"""Explicit offline commands. Live start, authorization and installation are absent."""
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
    for name in ('setup', 'check', 'status', 'config', 'runtime-config', 'simulate'):
        command = commands.add_parser(name)
        command.add_argument('--root', type=Path, required=True, help='explicit private host directory')
        if name == 'setup': command.add_argument('--preset', action='append', choices=PRESETS)
        if name == 'simulate': command.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    try:
        if args.command == 'setup':
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
