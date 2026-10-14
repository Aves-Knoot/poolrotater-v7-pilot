"""Best-effort Discord webhook transcript for lobby activity.

The lobby state machine must never wait for Discord.  This module therefore
puts small, already-sanitized messages on a single background worker queue.
The webhook address is intentionally read from an environment variable rather
than a room JSON file, so it never ends up in source control or chat logs.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


log = logging.getLogger("discord_modlog")


@dataclass(frozen=True, slots=True)
class LobbyLogEvent:
    webhook_url: str
    content: str


class DiscordModLog:
    """Mirror lobby chat and major lobby activity to an optional webhook."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        # Discord has its own rate limits; this larger buffer lets a lively
        # lobby absorb a short burst without touching the referee loop.
        self._queue: queue.Queue[LobbyLogEvent | None] = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker,
            name="poolrotater-discord-modlog",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _safe_text(value: object, limit: int) -> str:
        """Keep webhook text compact and prevent accidental Discord pings."""
        text = str(value or "").replace("\r", " ").replace("\n", " ")
        text = " ".join(text.split())
        text = text.replace("@", "@\u200b")
        return text[:limit]

    @staticmethod
    def _valid_webhook_url(value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        host = (parsed.hostname or "").casefold()
        allowed_host = host in {"discord.com", "discordapp.com"}
        return (
            parsed.scheme == "https"
            and allowed_host
            and parsed.path.startswith("/api/webhooks/")
        )

    def _webhook_url(self) -> str | None:
        if not bool(self.cfg.get("discord_modlog_enabled", True)):
            return None
        env_name = str(
            self.cfg.get(
                "discord_modlog_webhook_env",
                "POOLROTATER_DISCORD_MODLOG_WEBHOOK",
            )
            or ""
        ).strip()
        if not env_name:
            return None
        url = os.environ.get(env_name, "").strip()
        if not url:
            return None
        if not self._valid_webhook_url(url):
            log.warning(
                "Discord lobby log disabled: %s is not a valid Discord webhook URL",
                env_name,
            )
            return None
        return url

    def _submit_content(self, content: str) -> bool:
        webhook_url = self._webhook_url()
        if not webhook_url:
            return False
        try:
            self._queue.put_nowait(LobbyLogEvent(webhook_url, content[:2000]))
            return True
        except queue.Full:
            log.warning("Discord lobby log queue is full; dropped one notification")
            return False

    def _instance_prefix(self) -> str:
        if not bool(self.cfg.get("discord_modlog_include_instance_name", False)):
            return ""
        name = self._safe_text(
            self.cfg.get("discord_modlog_room_label")
            or self.cfg.get("_instance_name")
            or "",
            24,
        )
        return f"[{name}] " if name else ""

    def submit_chat(
        self,
        *,
        role: str,
        username: str,
        user_id: int,
        text: str,
        is_command: bool,
        room_id: int | None,
        room_name: str,
    ) -> bool:
        """Queue an incoming player-chat line without blocking the lobby."""
        return self._submit_content(self.format_chat(
            role=role,
            username=username,
            user_id=user_id,
            text=text,
            is_command=is_command,
            room_id=room_id,
            room_name=room_name,
        ))

    def format_chat(
        self,
        *,
        role: str,
        username: str,
        user_id: int,
        text: str,
        is_command: bool,
        room_id: int | None,
        room_name: str,
    ) -> str:
        """Build a safe human-readable transcript line for any Discord sink."""
        actor = self._safe_text(username, 80) or str(user_id)
        message = self._safe_text(text, 1200).replace("`", "'")
        prefix = self._instance_prefix()
        if is_command:
            return f"{prefix}⌘ {actor}: {message}"
        return f"{prefix}{actor}: {message}"

    def submit_event(
        self,
        *,
        label: str,
        text: str,
        room_id: int | None,
        room_name: str,
    ) -> bool:
        """Queue a concise bot/lobby event without blocking the state machine."""
        return self._submit_content(self.format_event(
            label=label,
            text=text,
            room_id=room_id,
            room_name=room_name,
        ))

    def format_event(
        self,
        *,
        label: str,
        text: str,
        room_id: int | None,
        room_name: str,
    ) -> str:
        """Build a safe event line for any Discord sink."""
        event_label = self._safe_text(label, 40).upper() or "LOBBY"
        message = self._safe_text(text, 1400).replace("`", "'")
        prefix = self._instance_prefix()
        if event_label == "MAP":
            return f"{prefix}🎵 {message}"
        if event_label == "MATCH START":
            return f"{prefix}▶️ {message.removeprefix('Started: ').strip()}"
        if event_label == "MATCH END":
            return f"{prefix}🔚 {message.removeprefix('Finished: ').strip()}"
        if event_label == "MATCH ABORTED":
            return f"{prefix}🆎 {message}"
        if event_label in ("JOIN", "LEAVE"):
            return f"{prefix}↳ {message}"
        if event_label in ("BOT", "OSU BOT"):
            actor = self._safe_text(self.cfg.get("_authenticated_username"), 80)
            return f"{prefix}{actor or 'PoolRotater'}: {message}"
        return f"{prefix}⚙ {event_label}: {message}"

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.0, float(timeout)))

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                break
            try:
                timeout = max(
                    1.0,
                    min(
                        30.0,
                        float(self.cfg.get("discord_modlog_timeout_seconds", 5.0)),
                    ),
                )
                with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
                    response = client.post(
                        event.webhook_url,
                        json={"content": event.content, "allowed_mentions": {"parse": []}},
                        headers={"User-Agent": "poolrotater-discord-modlog/0.1"},
                    )
                    response.raise_for_status()
                log.info("sent Discord lobby-log notification")
            except Exception as exc:
                # Do not include the URL in errors: it contains the webhook secret.
                log.warning("could not send Discord lobby-log notification: %s", type(exc).__name__)
