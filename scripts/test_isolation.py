"""Verify the kernel boundary before importing any development test code.

The wrappers mount a read-only namespace inventory captured outside bubblewrap.
Environment flags cannot supply this inventory or satisfy the namespace checks.
"""
import json
import os
from pathlib import Path
import re
import stat

ANCHOR = '/etc/poolrotater-test-boundary.json'
NAMESPACES = ('pid', 'mnt', 'net', 'ipc', 'uts')
PROVISIONING_CAPS = sum(1 << bit for bit in (0, 1, 6, 7))


def inventory(mode):
    return {'format': 1, 'mode': mode,
            'namespaces': {name: os.readlink('/proc/self/ns/' + name) for name in NAMESPACES}}


def require_test_isolation(expected_mode=None):
    try:
        path = Path(ANCHOR)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_size > 4096:
            raise ValueError
        if not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise ValueError
        value = json.loads(path.read_bytes())
        if (set(value) != {'format', 'mode', 'namespaces'} or type(value['format']) is not int or value['format'] != 1
                or value['mode'] not in ('ordinary', 'provisioning')
                or (expected_mode is not None and value['mode'] != expected_mode)
                or set(value['namespaces']) != set(NAMESPACES)
                or any(type(value['namespaces'][name]) is not str or not re.fullmatch(
                    re.escape(name)+r':\[[0-9]+\]',value['namespaces'][name]) for name in NAMESPACES)):
            raise ValueError
        if any(os.readlink('/proc/self/ns/' + name) == value['namespaces'][name]
               for name in NAMESPACES):
            raise ValueError
        status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                      if ':' in line)
        capabilities = int(status['CapEff'].strip(), 16)
        if value['mode'] == 'ordinary':
            if os.getuid() != 1000 or os.geteuid() != 1000 or capabilities:
                raise ValueError
        elif os.geteuid() not in (0, 1000) or capabilities & ~PROVISIONING_CAPS:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('development tests require scripts/offline or scripts/offline-provisioning; '
                           'fixture environment flags are not isolation') from None
    return value['mode']


def anchor_fd(mode):
    descriptor = os.memfd_create('poolrotater-test-boundary', os.MFD_CLOEXEC)
    os.write(descriptor, json.dumps(inventory(mode)).encode())
    os.fchmod(descriptor, 0o444)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor
