#!/usr/bin/env python3
"""Guarded, reversible website-only installation. Default checks without writes.

This deliberately never starts/restarts any service and does not touch bot state.
Use --root for donor/staging qualification. Live --apply is a maintainer action.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

SOURCE = Path(__file__).resolve().parent
TARGETS = {
    'network_statistics.py': '/usr/local/lib/poolrotator-ops/network_statistics.py',
    'analytics_exporter.py': '/usr/local/sbin/poolrotator-wiki-analytics',
    'analytics.php': '/srv/poolrotator-wiki/dokuwiki/lib/tpl/poolrotator/analytics.php',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def atomic(path, payload, mode):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def locations(root):
    baseline = json.loads((SOURCE/'baseline.json').read_text())
    result = []
    for name, relative in TARGETS.items():
        lexical = root/relative.lstrip('/')
        # The existing DokuWiki root is an intentionally maintained symlink.
        # Freeze its real path once and reject any nonregular target.
        target = lexical.resolve(strict=False)
        if root != Path('/') and not target.is_relative_to(root.resolve()):
            raise ValueError('target escapes staging root')
        if target.exists() and not stat.S_ISREG(target.lstat().st_mode):
            raise ValueError('target is not a regular file')
        before = baseline.get(name, {}).get('before_sha256')
        after = digest(SOURCE/name)
        observed = digest(target)
        if observed not in (before, after):
            raise ValueError('website baseline changed: ' + name)
        if not target.parent.is_dir():
            raise ValueError('target parent missing: ' + name)
        result.append((name, target, before, after))
    return result


def install(root, backup, apply=False):
    root = root.resolve()
    rows = locations(root)
    report = {'format': 1, 'applied': False, 'services_restarted': False,
              'bot_state_modified': False, 'files': []}
    if apply and backup.exists():
        raise ValueError('backup destination exists; retain previous receipt')
    if apply:
        backup.mkdir(parents=True, mode=0o700)
        backup.chmod(0o700)
    installed = []
    try:
        for name, target, before, after in rows:
            current = digest(target)
            if current not in (before, after):
                raise ValueError('website changed during preparation')
            changed = current != after
            previous = target.read_bytes() if target.exists() else None
            mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
            record = {'name': name, 'path': str(target), 'before_sha256': current,
                      'after_sha256': after, 'mode': mode, 'changed': changed}
            if apply and changed:
                if previous is not None:
                    atomic(backup/(name+'.before'), previous, 0o600)
                if digest(target) != current:
                    raise ValueError('website changed before commit')
                atomic(target, (SOURCE/name).read_bytes(), mode)
                installed.append((target, previous, mode, after))
            report['files'].append(record)
        report['applied'] = bool(apply)
        if apply:
            atomic(backup/'receipt.json', (json.dumps(report, indent=2)+'\n').encode(), 0o600)
        return report
    except Exception:
        # Roll back only bytes still belonging to this transition. Never undo
        # somebody else's edit made after our write.
        for target, previous, mode, after in reversed(installed):
            if digest(target) != after:
                continue
            if previous is None: target.unlink()
            else: atomic(target, previous, mode)
        raise


def rollback(backup):
    report = json.loads((backup/'receipt.json').read_text())
    for row in report['files']:
        if row['changed'] and digest(Path(row['path'])) != row['after_sha256']:
            raise ValueError('website changed after installation; refusing rollback')
    for row in reversed(report['files']):
        if not row['changed']: continue
        target = Path(row['path'])
        if row['before_sha256'] is None: target.unlink()
        else:
            payload = (backup/(row['name']+'.before')).read_bytes()
            if hashlib.sha256(payload).hexdigest() != row['before_sha256']:
                raise ValueError('backup digest mismatch')
            atomic(target, payload, row['mode'])
    return {'ok': True, 'rolled_back': True, 'services_restarted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/'))
    parser.add_argument('--backup', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    if args.root == Path('/') and os.geteuid() != 0:
        parser.error('live website check/install requires root')
    result = rollback(args.backup) if args.rollback else install(args.root, args.backup, args.apply)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__': main()
