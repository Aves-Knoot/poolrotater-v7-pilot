"""Private-room gate for an owner-authorized coordinated supervisor upgrade.

No API polling, kicks, or closures. Missing/expired requests release the gate.
Only the worker main loop may change settings; unknown outcomes stay paused.
"""
import json
import math
from pathlib import Path
import re
import secrets
import time


class MaintenanceGate:
    def __init__(self, path='state/rolling/supervisor-maintenance.json', release=None):
        self.path = Path(path)
        self.release = Path(release).resolve() if release else None
        self.nonce = None
        self.blocked_nonce = None
        self.room_id = None
        self.locked = False
        self.uncertain = False
        self.error = None
        self.checked_at = 0

    def status(self):
        return dict(nonce=self.nonce, locked=self.locked,
                    uncertain=self.uncertain, error=self.error)

    def read_request(self):
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 8192:
                return None
            value = json.loads(self.path.read_text())
            deadline = value.get('expires_at')
            if (value.get('format') != 1 or value.get('phase') not in ('locking','committing')
                or not re.fullmatch(r'[a-f0-9]{32}', str(value.get('nonce', '')))
                or type(deadline) not in (int,float) or not math.isfinite(deadline)
                or deadline <= time.time() or self.release is None
                or value.get('bridge_release') != str(self.release)):
                return None
            return value
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def release_gate(self, bot):
        if self.uncertain:
            return  # Never guess that an unconfirmed mutation failed.
        if self.locked:
            try:
                if bot.room_id != self.room_id or not bot.hub.is_connected():
                    raise RuntimeError('maintenance room identity/connection changed')
                bot.hub.change_room_settings(self.room_id, password='')
                self.locked = False
            except Exception:
                self.uncertain = True
                self.error = 'maintenance unlock outcome requires reconciliation'

    def tick(self, bot):
        # Paused workers still drain referee events; a late join must be visible.
        if self.locked or self.uncertain:
            import queue
            try:
                while True:
                    name, payload = bot.events.get_nowait()
                    bot.handle_event(name, payload)
            except queue.Empty:
                pass
        if time.monotonic()-self.checked_at < .5:
            return self.locked or self.uncertain
        self.checked_at = time.monotonic()
        request = self.read_request()
        if self.uncertain:
            return True
        if not request and not self.locked:
            return False
        safe = (bot._room_ready and bot.running and bot.state == 'IDLE'
                and not bot.players and not bot._hub_rejoin_pending
                and bot.events.empty() and bot.hub.is_connected())
        if self.locked:
            if (not request or request['nonce'] != self.nonce or not safe
                    or bot.room_id != self.room_id):
                self.blocked_nonce = self.nonce
                self.release_gate(bot)
            return self.locked or self.uncertain
        if not request or request['nonce'] == self.blocked_nonce or not safe:
            return False
        self.nonce = request['nonce']
        self.room_id = bot.room_id
        try:
            bot.hub.change_room_settings(self.room_id, password=secrets.token_urlsafe(24))
            self.locked = True
            bot._flush_all_lobby_time()
        except Exception:
            self.uncertain = True
            self.error = 'maintenance lock outcome requires reconciliation'
        return True
