#!/usr/bin/python3.12
"""Credential-free protected pilot installer. Run explicitly as root with -I -S.

The operator supplies an independently reviewed runtime ZIP SHA-256. No wheel,
source or dependency is downloaded, and no service or account authorization starts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import zipfile


def main():
    if os.geteuid() != 0: raise ValueError('administrator required')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--preset', action='append')
    parser.add_argument('--live-pilot', action='store_true', help='require the separately reviewed one-room live artifact')
    parser.add_argument('--upgrade-from-sha256', help='explicit existing runtime digest; preserve the same instance/state')
    upgrade = parser.add_mutually_exclusive_group()
    upgrade.add_argument('--upgrade-recover', choices=('complete', 'abort'))
    upgrade.add_argument('--upgrade-status', action='store_true')
    parser.add_argument('--transaction-id')
    args = parser.parse_args()
    if (args.upgrade_from_sha256 is not None and (not args.live_pilot or args.preset is not None)
            or (args.upgrade_from_sha256 is None and (args.upgrade_recover or args.upgrade_status or args.transaction_id))
            or bool(args.upgrade_recover) != bool(args.transaction_id)):
        raise ValueError('upgrade requires explicit live artifact and existing digest; no preset changes')
    import re
    if not re.fullmatch('[a-f0-9]{64}', args.sha256): raise ValueError('explicit trusted digest required')
    with tempfile.TemporaryDirectory(prefix='prv7-install-') as directory:
        stage = Path(directory); pinned = stage / 'runtime.zip'
        fd = os.open(args.bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source, pinned.open('xb') as target:
            st = os.fstat(source.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 128 * 1024**2:
                raise ValueError('bounded regular artifact required')
            digest = hashlib.sha256(); count = 0
            while chunk := source.read(1024**2):
                count += len(chunk)
                if count > 128 * 1024**2: raise ValueError('artifact exceeds quota')
                digest.update(chunk); target.write(chunk)
            target.flush(); os.fsync(target.fileno())
        if digest.hexdigest() != args.sha256: raise ValueError('artifact differs from approved digest')
        with zipfile.ZipFile(pinned) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if len(names) != len(set(names)) or sum(item.file_size for item in entries) > 128 * 1024**2:
                raise ValueError('ambiguous or oversized artifact')
            for item in entries:
                path = PurePosixPath(item.filename)
                if (path.is_absolute() or any(p in ('', '.', '..') for p in item.filename.split('/'))
                        or str(path) != item.filename or not stat.S_ISREG(item.external_attr >> 16)
                        or item.flag_bits & 1 or item.file_size > 64 * 1024**2):
                    raise ValueError('unsafe artifact member')
            for item in entries:
                if not item.filename.startswith('payload/'): continue
                destination = stage / item.filename
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.open(item) as source, destination.open('xb') as target:
                    import shutil
                    shutil.copyfileobj(source, target, length=1024**2)
        sys.path.insert(0, str(stage / 'payload'))
        if args.upgrade_from_sha256 is not None:
            from poolrotater_host.pilot_upgrade import upgrade
            result = upgrade(args.instance, pinned, args.sha256, from_sha256=args.upgrade_from_sha256,
                             recover='status' if args.upgrade_status else args.upgrade_recover,
                             transaction_id=args.transaction_id)
        else:
            from poolrotater_host.pilot_installation import install
            result = install(args.instance, pinned, args.sha256, presets=args.preset, live_pilot=args.live_pilot)
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try: main()
    except Exception:
        print('protected pilot installation refused', file=sys.stderr)
        raise SystemExit(2)
