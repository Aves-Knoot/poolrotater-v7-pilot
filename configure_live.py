#!/usr/bin/python3.12
"""Private local operator setup; no secrets in argv, files, logs or environment."""
import argparse
import getpass
import json
from pathlib import Path
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--release', required=True, type=Path)
    parser.add_argument('--expected-user-id', required=True, type=int)
    parser.add_argument('--redirect-uri', required=True)
    args = parser.parse_args()
    if (not re.fullmatch('[a-z][a-z0-9-]{0,15}', args.instance)
            or args.expected_user_id <= 0 or not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ValueError('private interactive operator terminal required')
    expected = Path('/opt/poolrotater-v7-pilot') / args.instance / 'releases'
    if args.release.parent != expected or not re.fullmatch('v7\\.0\\.1-[a-f0-9]{16}', args.release.name):
        raise ValueError('use the exact installed live pilot release')
    entry = args.release / 'poolrotater_host/pilot_admin_entry.py'
    command = ['sudo', '-n', '/usr/bin/python3.12', '-I', '-S', str(entry), '--instance', args.instance]
    # Installed root bootstrap verifies all code/dependencies and service UID
    # before prompting. Configure repeats its checks before reading the pipe.
    checked = subprocess.run([*command, 'check'], capture_output=True, timeout=30)
    if checked.returncode: raise ValueError('protected installed check failed')
    report = json.loads(checked.stdout)
    if report.get('execution') != 'pilot-live' or report.get('ok') is not True:
        raise ValueError('separate live artifact required')
    client_id = int(input('Your own OAuth client ID: ').strip())
    if client_id <= 0: raise ValueError('positive client ID required')
    client_secret = getpass.getpass('Your own OAuth client secret (hidden): ')
    if not client_secret or len(client_secret) > 8192: raise ValueError('invalid client secret')
    private = json.dumps({'client_id': client_id, 'client_secret': client_secret}).encode()
    configured = subprocess.run([*command, 'configure', '--credentials-stdin',
        '--expected-user-id', str(args.expected_user_id), '--redirect-uri', args.redirect_uri],
        input=private, capture_output=True, timeout=90)
    if configured.returncode: raise ValueError('protected operator setup refused')
    # Output is from the checked installed diagnostic, never the input payload.
    print(json.dumps(json.loads(configured.stdout), indent=2))


if __name__ == '__main__':
    try: main()
    except Exception:
        print('private operator configuration failed; inspect protected status', file=sys.stderr)
        raise SystemExit(2)
