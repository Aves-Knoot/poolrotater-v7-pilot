"""Small, process-wide custom greeting helpers for known players."""
from __future__ import annotations

import threading
import time


class GreetingCooldown:
    """Claim a per-player greeting slot across every room in this process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_sent: dict[int, float] = {}

    def claim(self, user_id: int, cooldown_seconds: float, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else float(now)
        cooldown = max(0.0, float(cooldown_seconds))
        with self._lock:
            previous = self._last_sent.get(int(user_id))
            if previous is not None and now - previous < cooldown:
                return False
            self._last_sent[int(user_id)] = now
            return True


JOIN_GREETING_COOLDOWN = GreetingCooldown()


def configured_greeting(cfg: dict, user_id: int, username: str) -> str | None:
    """Find a greeting by osu! user id first, then case-insensitive username."""
    greetings = cfg.get("custom_join_greetings", {})
    if not isinstance(greetings, dict):
        return None

    value = greetings.get(str(int(user_id)))
    if value is None:
        wanted = str(username or "").casefold()
        for key, candidate in greetings.items():
            if str(key).casefold() == wanted:
                value = candidate
                break
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:450] if value else None
