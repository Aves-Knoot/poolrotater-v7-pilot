"""Offline readiness checks; credentials are never read or refreshed."""
import os
from pathlib import Path
import platform
import shutil
import sys
import sqlite3
from .catalogs import make_pool
from .config import ConfigError
from .layout import load


def check(root: Path) -> dict:
    marker, config = load(root)
    problems = []
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        problems.append('reference platform requires Linux x86-64')
    if sys.version_info < (3, 12):
        problems.append('Python 3.12 or later is required')
    free = shutil.disk_usage(root).free
    if free < 256 * 1024**2:
        problems.append('less than 256 MiB free for offline state; stop optional writes')
    rooms = []
    for room in config['rooms']:
        try:
            pool = make_pool(root, room)
            rooms.append({'alias': room['alias'], 'preset': room['preset'], 'eligible_maps': len(pool.maps), 'ok': True})
        except (ConfigError, OSError, ValueError, sqlite3.Error) as exc:
            # Do not include paths/SQL/credential contents in diagnostic output.
            rooms.append({'alias': room['alias'], 'preset': room['preset'], 'ok': False, 'error': type(exc).__name__})
            problems.append(f'{room["alias"]}: missing, incompatible or empty catalog; install a compatible catalog or adjust the range')
    return {'ok': not problems, 'execution': 'offline', 'installation_id': marker['installation_id'],
            'rooms': rooms, 'problems': problems, 'free_bytes': free,
            'live_ready': False, 'authentication': 'not checked; offline foundation',
            'network': 'disabled', 'service_installed': False}
