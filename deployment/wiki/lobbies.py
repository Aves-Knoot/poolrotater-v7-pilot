#!/usr/bin/env python3
"""Publish only invite/room links. Never write to or import the bot runtime."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import sqlite3
import subprocess
import time
import sys
sys.path.insert(0, "/usr/local/lib/poolrotator-ops")
from sqlite_readonly import readonly_database

SNAPSHOT = Path('/var/lib/poolrotator-ops/snapshots/latest.json')
from owner_source import selected
SOURCE = selected()
CONFIG = SOURCE.config('room1') if SOURCE.owner else Path('/opt/poolrotator/instances/universal.json')
LEDGER = SOURCE.ledger
OUTPUT = Path('/srv/poolrotator-wiki/public/lobbies.json')
TTL = 600
ROOMS = (
    ('room1', 'Standard automatic', 'lobbies:standard_auto'),
    ('room2', 'Standard fixed', 'lobbies:standard_fixed'),
    ('room3', 'Mania 7K', 'lobbies:mania_7k'),
    ('room4', 'Mania 4K', 'lobbies:mania_4k'),
)


def valid_invite(value):
    return isinstance(value, str) and bool(re.fullmatch(
        r'https://(?:discord\.gg/|discord\.com/invite/)[A-Za-z0-9_-]{2,100}', value))



def room_descriptors():
    """Configured mode controls labels; never infer it from a numbered slot."""
    result=[]
    for alias,label,guide in ROOMS:
        if SOURCE.owner:
            try:
                if json.loads(SOURCE.config(alias).read_text()).get('ruleset_id')==2:
                    label,guide='Catch fixed','lobbies:catch'
            except (OSError,ValueError,AttributeError):pass
        result.append((alias,label,guide))
    return result


def build(snapshot, invite, ledger, *, now=None, service_active=False):
    """Fail closed for stale, stopped, invalid, or ledger-closed rooms."""
    now = time.time() if now is None else now
    generated = 0
    try:
        generated = datetime.fromisoformat(snapshot['generated_at'].replace('Z', '+00:00')).timestamp()
    except (KeyError, ValueError, TypeError, AttributeError):
        pass
    service = snapshot.get('system', {}).get('service', {})
    fresh = (service_active and -30 <= now - generated <= TTL
             and service.get('active_state') == 'active'
             and service.get('sub_state') == 'running')
    raw_rooms = snapshot.get('rooms', [])
    observed = {r.get('alias'): r.get('room_id') for r in raw_rooms if isinstance(r, dict)}
    rooms = []
    for alias, label, guide in room_descriptors():
        room_id = observed.get(alias)
        if not fresh or type(room_id) is not int or not 0 < room_id <= 9223372036854775807:
            room_id = None
        if room_id is not None:
            row = ledger.execute('SELECT closed_at FROM autohost_rooms WHERE room_id=?', (room_id,)).fetchone()
            if row is None or row[0] is not None:
                room_id = None
        rooms.append({'alias': alias, 'label': label, 'guide': guide, 'room_id': room_id})
    return {'schema': 1, 'discord_url': invite if valid_invite(invite) else None,
            'observed_at': generated, 'expires_at': generated + TTL, 'rooms': rooms}


def main():
    invite = None
    try:
        invite = json.loads(CONFIG.read_text()).get('discord_invite_url')
    except (OSError, ValueError):
        pass
    if not valid_invite(invite):
        try:
            invite = json.loads(OUTPUT.read_text()).get('discord_url')
        except (OSError, ValueError):
            pass
    try:
        snapshot = json.loads(SNAPSHOT.read_text())
        active = subprocess.run(['systemctl', 'is-active', '--quiet', SOURCE.service],
                                timeout=3, check=False).returncode == 0
        # mode=ro plus systemd's read-only mount guarantees no bot DB writes.
        with readonly_database(LEDGER, timeout=1) as conn:
            result = build(snapshot, invite, conn, service_active=active)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error, subprocess.SubprocessError):
        result = {'schema': 1, 'discord_url': invite if valid_invite(invite) else None,
                  'observed_at': 0, 'expires_at': 0,
                  'rooms': [{'alias': a, 'label': l, 'guide': g, 'room_id': None} for a, l, g in room_descriptors()]}
    OUTPUT.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix('.tmp')
    with temp.open('w') as file:
        json.dump(result, file, separators=(',', ':'))
        file.write('\n')
    temp.chmod(0o644)
    os.replace(temp, OUTPUT)


if __name__ == '__main__':
    main()
