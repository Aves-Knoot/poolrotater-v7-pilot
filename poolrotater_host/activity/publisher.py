#!/usr/bin/env python3
"""Private hub-to-analytics file publisher; never a public HTTP endpoint.

The hub service identity owns its database and a dedicated setgid output
folder. Analytics receives read-only group access to this one snapshot rather
than access to host credentials or the hub databases.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from poolrotater_host.activity.repository import Repository
from poolrotater_host.network.store import Store

MAX_BYTES = 128 * 1024 * 1024


def checked_output(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=False) != path:
        raise ValueError('private_snapshot_path_must_be_absolute_without_symlinks')
    parent = path.parent
    info = parent.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o027 or not info.st_mode & stat.S_ISGID):
        raise ValueError('private_snapshot_directory_requires_owner_setgid_0750')
    if path.exists():
        old = path.lstat()
        if (not stat.S_ISREG(old.st_mode) or old.st_uid != os.geteuid()
                or old.st_nlink != 1 or stat.S_IMODE(old.st_mode) & 0o027):
            raise ValueError('private_snapshot_file_permissions_refused')
    return path, info


def publish_snapshot(store, output, *, owner_account_id=None, now=None):
    """Publish one complete private snapshot with authenticated owner binding.

    Return only a nonidentifying receipt. Named players and privacy IDs stay in
    the private file until the existing analytics exporter removes hidden rows
    and selects bounded public rankings. No sharing consent is changed here.
    """
    if owner_account_id is not None and (type(owner_account_id) is not int or owner_account_id <= 0):
        raise ValueError('invalid_owner_account_id')
    output, directory = checked_output(output)
    snapshot = Repository(store).snapshot(now=now, include_hidden_for_totals=True)
    snapshot['owner_host_ids'] = sorted(row['host_id'] for row in snapshot['hosts']
                                        if owner_account_id is not None and row['account_id'] == owner_account_id)
    payload = (json.dumps(snapshot, ensure_ascii=True, separators=(',', ':'), allow_nan=False)+'\n').encode()
    if len(payload) > MAX_BYTES:
        raise ValueError('private_snapshot_exceeds_export_bound')
    fd, temporary = tempfile.mkstemp(prefix='.' + output.name + '.', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
            if info.st_gid != directory.st_gid:
                raise ValueError('private_snapshot_setgid_inheritance_required')
        os.chmod(temporary, 0o640)
        # Check the destination again immediately before the atomic replace.
        checked_output(output)
        os.replace(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return {'ok': True, 'kind': 'private-network-statistics-snapshot', 'format': 1,
            'generated_at': snapshot['generated_at'], 'bytes': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
            'hosts': len(snapshot['hosts']),
            'enabled_hosts': sum(row['enabled'] for row in snapshot['hosts']),
            'counts': {key: len(snapshot[key]) for key in ('presence', 'joins', 'leaves', 'matches', 'achievements', 'baselines')},
            'public_http_exposure': False, 'sharing_choices_changed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hub-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--owner-account-id', type=int)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('run under the existing unprivileged hub service identity')
    print(json.dumps(publish_snapshot(Store(args.hub_root), args.output,
                                     owner_account_id=args.owner_account_id), sort_keys=True))


if __name__ == '__main__': main()
