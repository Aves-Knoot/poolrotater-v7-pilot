"""Optional Discord bot bridge for staff control of PoolRotater.

A Discord webhook can only send messages.  Receiving commands requires a
normal Discord *bot* account, never automation of a person's Discord account.
This module owns that client in a background thread and hands authorized
commands back to LobbyBot through a queue.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit


log = logging.getLogger("discord_control")


def normalize_public_join_base_url(value: object) -> str:
    """Return a safe HTTPS origin, or an empty string to use osu! web."""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    hostname = str(parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[a-z0-9.-]+", hostname)
    ):
        return ""
    return f"https://{hostname}"


def public_room_links(room_id: int, join_base_url: str = "") -> tuple[str, str]:
    """Return the preferred Lazer launcher URL and official web fallback."""
    room_id = max(0, int(room_id or 0))
    web_url = f"https://osu.ppy.sh/multiplayer/rooms/{room_id}"
    base = normalize_public_join_base_url(join_base_url)
    return (f"{base}/room/{room_id}" if base else web_url), web_url


# Discord moderators are intentionally limited to the operational controls
# advertised as moderator commands by LobbyBot.  Admin-only commands are
# rejected at the Discord edge so they never enter the lobby command queue.
_DISCORD_MOD_COMMANDS = frozenset({
    "kick", "ban", "unban", "bans",
    "help", "commands", "pp",
    "settings", "start", "abort", "forceskip", "softrestart",
    "usercommands", "silence", "voteskip", "ppannounce", "aichat",
    "range", "auto", "length", "maxlength", "maxage", "countdown",
    "autostart", "minplayers", "queueahead", "difficultyvote", "diffvote",
    "overview", "activity", "players", "ratings", "retention", "mapstats",
    "who", "apiusage", "lazerstatus",
})


_SUPERVISOR_COMMANDS = frozenset({"open", "reopen"})


@dataclass(frozen=True, slots=True)
class DiscordCommand:
    discord_user_id: int
    username: str
    role: str
    content: str
    channel_id: int


@dataclass(frozen=True, slots=True)
class DiscordOutboundMessage:
    channel_id: int
    content: str = ""
    map_message: bool = False
    map_key: str = ""
    reaction: str = ""
    public_status: "DiscordPublicStatus | None" = None


@dataclass(frozen=True, slots=True)
class DiscordPublicStatus:
    channel_id: int
    instance: str
    channel_base_name: str
    display_name: str
    is_open: bool
    lifecycle: str
    room_id: int = 0
    room_name: str = ""
    player_count: int = 0
    spectator_count: int = 0
    sr_min: float = 0.0
    sr_max: float = 0.0
    map_label: str = ""
    map_url: str = ""
    join_base_url: str = ""
    rename_channel: bool = True


class DiscordControl:
    """Receive staff commands from one configured Discord guild/channel."""

    def __init__(self, cfg: dict, *, start: bool = True):
        self.cfg = cfg
        self.commands: queue.Queue[DiscordCommand] = queue.Queue(maxsize=100)
        self.outbox: queue.Queue[DiscordOutboundMessage] = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._outbox_task_started = False
        self._status_pending = {}
        self._status_inflight = set()
        self._status_lock = threading.Lock()
        if start:
            self.start()

    def _take_status(self, channel_id):
        with self._status_lock:
            return self._status_pending.pop(channel_id, None)

    def start(self) -> None:
        cfg = self.cfg

        if not bool(cfg.get("discord_control_enabled", True)):
            return
        token = self._token()
        if not token:
            return
        if not self._channel_id():
            log.warning(
                "Discord control is disabled: set discord_control_channel_id "
                "in the room config"
            )
            return
        self._write_health_state("starting")
        self._thread = threading.Thread(
            target=self._run,
            args=(token,),
            name="poolrotator-discord-control",
            daemon=True,
        )
        self._thread.start()

    def _token(self) -> str:
        env_name = str(
            self.cfg.get(
                "discord_control_bot_token_env",
                "POOLROTATER_DISCORD_BOT_TOKEN",
            )
            or ""
        ).strip()
        return os.environ.get(env_name, "").strip() if env_name else ""

    def _channel_id(self) -> int:
        """Return this room's outbound Discord channel."""
        instance = self._instance_name()
        mapped = self._channel_map().get(instance)
        if mapped:
            return mapped
        try:
            return max(0, int(self.cfg.get("discord_control_channel_id", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _instance_name(self) -> str:
        return str(
            self.cfg.get("discord_control_room_alias")
            or self.cfg.get("_instance_name")
            or "room1"
        ).strip().casefold()

    def _channel_map(self) -> dict[str, int]:
        """Return configured room alias -> Discord channel mappings."""
        raw = self.cfg.get("discord_control_channel_ids_by_room", {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, int] = {}
        for alias, channel_id in raw.items():
            name = str(alias or "").strip().casefold()
            try:
                parsed = max(0, int(channel_id or 0))
            except (TypeError, ValueError):
                continue
            if name and parsed:
                out[name] = parsed
        return out

    def _channel_ids(self) -> set[int]:
        mapped = set(self._channel_map().values())
        legacy = self._channel_id()
        if legacy:
            mapped.add(legacy)
        return mapped

    def _guild_id(self) -> int:
        try:
            return max(0, int(self.cfg.get("discord_control_guild_id", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _public_status_channel_id(self) -> int:
        try:
            return max(
                0,
                int(self.cfg.get("discord_public_status_channel_id", 0) or 0),
            )
        except (TypeError, ValueError):
            return 0

    def _public_status_channel_name(self) -> str:
        configured = str(
            self.cfg.get("discord_public_status_channel_name", "") or ""
        ).strip()
        return configured or f"{self._instance_name()}-status"

    def _public_status_display_name(self) -> str:
        configured = str(
            self.cfg.get("discord_public_status_label", "") or ""
        ).strip()
        if configured:
            return configured
        instance = self._instance_name()
        matched = re.fullmatch(r"room(\d+)", instance)
        return f"Lobby {matched.group(1)}" if matched else instance.title()

    def _public_join_base_url(self) -> str:
        configured = os.environ.get(
            "POOLROTATOR_PUBLIC_JOIN_BASE_URL",
            self.cfg.get("discord_public_join_base_url", ""),
        )
        return normalize_public_join_base_url(configured)

    def _route_command(
        self, content: str, channel_id: int | None = None
    ) -> str | None:
        """Route one shared-channel command to exactly one room process.

        Existing unqualified commands remain assigned to ``room1`` by default.
        Staff can target another process with ``.room2 settings``. A room using
        a dedicated Discord channel can opt back into unqualified commands with
        ``discord_control_accept_untargeted`` in that room's JSON.
        """
        prefix = str(self.cfg.get("command_prefix", ".") or ".")
        if not content.startswith(prefix):
            return None
        instance = self._instance_name()
        known = {
            str(value).strip().casefold()
            for value in self.cfg.get(
                "discord_control_known_room_aliases", ["room1", "room2"]
            ) or []
            if str(value).strip()
        }
        if instance:
            known.add(instance)

        body = content[len(prefix):].strip()
        first, separator, remainder = body.partition(" ")
        target = first.casefold()
        if target in known:
            if target != instance or not separator or not remainder.strip():
                return None
            command = remainder.strip()
            if command.casefold() == "shutdown":
                command = "shutdown this"
            return prefix + command

        # Also accept `.room2.settings` for staff who prefer one token.
        for alias in known:
            dotted = alias + "."
            if body.casefold().startswith(dotted):
                if alias != instance:
                    return None
                command = body[len(dotted):].strip()
                if command.casefold() == "shutdown":
                    command = "shutdown this"
                return prefix + command if command else None

        if bool(self.cfg.get("discord_control_accept_untargeted", False)):
            return content
        # With one channel per room, an unqualified command belongs to the room
        # mapped to the channel it was typed in. Every process receives the
        # Gateway event, but exactly one accepts it.
        if channel_id is not None:
            channel_defaults = {
                mapped_channel: alias
                for alias, mapped_channel in self._channel_map().items()
            }
            channel_instance = channel_defaults.get(int(channel_id))
            if channel_instance is not None:
                return content if instance == channel_instance else None
        primary = str(
            self.cfg.get("discord_control_primary_instance", "room1") or "room1"
        ).strip().casefold()
        return content if instance == primary else None

    @staticmethod
    def _role_names(values: object) -> set[str]:
        """Return non-empty case-insensitive Discord role names."""
        return {
            str(value).strip().casefold()
            for value in values or []
            if str(value).strip()
        }

    def _role_for_member(self, member: Any) -> str | None:
        """Map the server owner and configured Discord role names to bot roles."""
        member_role_names = {
            str(getattr(role, "name", "")).strip().casefold()
            for role in getattr(member, "roles", [])
            if str(getattr(role, "name", "")).strip()
        }
        admin_roles = self._role_names(
            self.cfg.get("discord_control_admin_role_names", ["Top dev"])
        )
        mod_roles = self._role_names(
            self.cfg.get("discord_control_moderator_role_names", ["mods"])
        )
        guild = getattr(member, "guild", None)
        is_server_owner = bool(
            guild is not None
            and int(getattr(guild, "owner_id", 0) or 0) == int(member.id)
        )
        if is_server_owner or member_role_names & admin_roles:
            return "ADMIN"
        if member_role_names & mod_roles:
            return "MOD"
        return None

    def _command_name(self, content: str) -> str:
        prefix = str(self.cfg.get("command_prefix", ".") or ".")
        text = str(content or "").strip()
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
        return text.partition(" ")[0].casefold()

    def _role_allows_command(self, role: str, content: str) -> bool:
        """Return whether a Discord role may enqueue this routed command."""
        normalized = str(role or "").upper()
        if normalized == "ADMIN":
            return True
        if normalized != "MOD":
            return False
        return self._command_name(content) in _DISCORD_MOD_COMMANDS

    def _is_supervisor_command(self, content: str) -> bool:
        """Return whether this command must be handled above LobbyBot.

        ``.open`` / ``.reopen`` have to work while a room worker is stopped,
        so there is no LobbyBot queue available to consume them.  ``.shutdown
        all`` is also supervisor-owned so an otherwise-idle supervisor can be
        terminated even when every room is parked.
        """
        name = self._command_name(content)
        if name in _SUPERVISOR_COMMANDS:
            return True
        if name != "shutdown":
            return False
        prefix = str(self.cfg.get("command_prefix", ".") or ".")
        text = str(content or "").strip()
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
        parts = text.split()
        return len(parts) == 2 and parts[0].casefold() == "shutdown" and parts[1].casefold() == "all"

    def _status_rename_spacing_seconds(self) -> float:
        """Minimum spacing between channel-rename attempts across all rooms."""
        try:
            value = float(
                self.cfg.get("discord_status_rename_spacing_seconds", 5.0) or 5.0
            )
        except (TypeError, ValueError):
            value = 5.0
        return max(0.0, min(60.0, value))

    def get_command_nowait(self) -> DiscordCommand:
        return self.commands.get_nowait()

    def enqueue_command(self, command: DiscordCommand) -> None:
        """Queue a routed command for this controller.

        Shared multi-room views override this for supervisor-owned lifecycle
        commands while ordinary single-room Discord control keeps the legacy
        LobbyBot queue behavior.
        """
        self.commands.put_nowait(command)

    def command_targets(self, content: str, channel_id: int):
        routed = self._route_command(content, channel_id=channel_id)
        return [(self, routed)] if routed is not None else []

    def send_to_channel(
        self,
        content: str,
        channel_id: int | None = None,
        *,
        map_message: bool = False,
        map_key: str = "",
    ) -> bool:
        """Queue a plain Discord message without blocking the lobby thread."""
        target = int(channel_id or self._channel_id() or 0)
        text = str(content or "").strip()
        if not self._thread or not target or not text:
            return False
        try:
            self.outbox.put_nowait(DiscordOutboundMessage(
                target,
                text[:2000],
                map_message=bool(map_message),
                map_key=str(map_key or ""),
            ))
            return True
        except queue.Full:
            log.warning("Discord outbound queue full; dropped one message")
            return False

    def react_to_map(
        self, emoji: str, map_key: str, channel_id: int | None = None
    ) -> bool:
        """React to a map record previously posted in this channel."""
        target = int(channel_id or self._channel_id() or 0)
        reaction = str(emoji or "").strip()
        key = str(map_key or "").strip()
        if not self._thread or not target or not reaction or not key:
            return False
        try:
            self.outbox.put_nowait(DiscordOutboundMessage(
                target, map_key=key, reaction=reaction[:100]
            ))
            return True
        except queue.Full:
            log.warning("Discord outbound queue full; dropped one reaction")
            return False

    def update_public_status(
        self,
        *,
        is_open: bool,
        lifecycle: str,
        room_id: int = 0,
        room_name: str = "",
        player_count: int = 0,
        spectator_count: int = 0,
        sr_min: float = 0.0,
        sr_max: float = 0.0,
        map_label: str = "",
        map_url: str = "",
        channel_id: int | None = None,
        channel_base_name: str | None = None,
        display_name: str | None = None,
        instance: str | None = None,
        rename_channel: bool = True,
    ) -> bool:
        """Queue one public lobby-status update without blocking the bot."""
        target = int(channel_id or self._public_status_channel_id() or 0)
        if not self._thread or not target:
            return False
        status = DiscordPublicStatus(
            channel_id=target,
            instance=str(instance or self._instance_name()),
            channel_base_name=str(
                channel_base_name or self._public_status_channel_name()
            ),
            display_name=str(display_name or self._public_status_display_name()),
            is_open=bool(is_open),
            lifecycle=str(lifecycle or ("Open" if is_open else "Offline")),
            room_id=max(0, int(room_id or 0)),
            room_name=str(room_name or ""),
            player_count=max(0, int(player_count or 0)),
            spectator_count=max(0, int(spectator_count or 0)),
            sr_min=float(sr_min or 0.0),
            sr_max=float(sr_max or 0.0),
            map_label=str(map_label or ""),
            map_url=str(map_url or ""),
            join_base_url=self._public_join_base_url(),
            rename_channel=bool(rename_channel),
        )
        with self._status_lock:
            self._status_pending[target] = status
        return True

    def _status_state_file(self) -> Path:
        return Path(
            str(
                self.cfg.get(
                    "discord_public_status_state_file",
                    "state/discord_public_status.json",
                )
                or "state/discord_public_status.json"
            )
        )

    def _health_state_file(self) -> Path:
        """Return the local-only Discord connection heartbeat path."""
        return Path(
            str(
                self.cfg.get(
                    "discord_control_health_state_file",
                    "state/discord_control_health.json",
                )
                or "state/discord_control_health.json"
            )
        )

    def _write_health_state(self, status: str, detail: str = "") -> None:
        """Atomically publish a secret-free connection state for local ops."""
        path = self._health_state_file()
        temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
        payload = {
            "schema": 1,
            "status": str(status or "unknown").strip().casefold(),
            "connected": str(status or "").strip().casefold() == "connected",
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "updated_at_epoch": time.time(),
            "pid": os.getpid(),
        }
        if detail:
            # Only callers' fixed status labels / exception class names belong
            # here; never write exception text or Discord identifiers.
            payload["detail"] = str(detail)[:80]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o640)
            os.replace(temporary, path)
        except OSError as exc:
            log.warning(
                "could not save Discord control health state (%s)",
                type(exc).__name__,
            )
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _load_status_message_ids(self) -> dict[str, int]:
        path = self._status_state_file()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        result: dict[str, int] = {}
        for channel_id, message_id in raw.items():
            try:
                result[str(int(channel_id))] = int(message_id)
            except (TypeError, ValueError):
                continue
        return result

    def _save_status_message_ids(self, values: dict[str, int]) -> None:
        path = self._status_state_file()
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(values, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as exc:
            log.warning("could not save Discord public-status message ID: %s", exc)

    @staticmethod
    def _safe_status_channel_name(base_name: str, is_open: bool) -> str:
        base = str(base_name or "lobby-status").strip()
        base = re.sub(r"^[🟢🔴]\s*[・·|_-]*\s*", "", base)
        base = re.sub(r"\s+", "-", base).strip("-・") or "lobby-status"
        return f"{'🟢' if is_open else '🔴'}・{base}"[:100]

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._write_health_state("stopping")
        client = self._client
        loop = getattr(client, "loop", None) if client is not None else None
        if loop is not None and loop.is_running():
            try:
                import asyncio
                asyncio.run_coroutine_threadsafe(client.close(), loop)
            except Exception:
                log.debug("could not request Discord control shutdown", exc_info=True)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(0.0, float(timeout)))

    def flush(self, timeout: float = 1.5) -> bool:
        """Wait briefly for already-queued Discord work to be delivered."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        def pending():
            with self._status_lock:
                return bool(self.outbox.unfinished_tasks or self._status_pending or self._status_inflight)
        while pending() and time.monotonic() < deadline:
            if not self._thread or not self._thread.is_alive():
                break
            time.sleep(0.05)
        return not pending()

    def _run(self, token: str) -> None:
        try:
            import discord
        except ImportError:
            self._write_health_state("failed", "ImportError")
            log.error(
                "Discord control needs discord.py; run: pip install -r requirements.txt"
            )
            return

        parent = self

        class ControlClient(discord.Client):
            async def on_ready(self) -> None:
                log.info("Discord control connected as %s", self.user)
                parent._write_health_state("connected")
                if not parent._outbox_task_started:
                    parent._outbox_task_started = True
                    self.status_tasks = {}
                    self.rename_tasks = {}
                    self.rename_targets = {}
                    self.rename_after = {}
                    self.rename_rate_lock = asyncio.Lock()
                    self.rename_next_at = 0.0
                    self.status_message_ids = parent._load_status_message_ids()
                    self.outbox_task = self.loop.create_task(self._send_outbox())
                    self.status_dispatch_task = self.loop.create_task(self._status_dispatch())
                    self.health_task = self.loop.create_task(self._health_heartbeat())

            async def on_resumed(self) -> None:
                log.info("Discord control connection resumed")
                parent._write_health_state("connected")

            async def on_disconnect(self) -> None:
                log.warning(
                    "Discord control disconnected; waiting for automatic reconnect"
                )
                parent._write_health_state("disconnected")

            async def _health_heartbeat(self) -> None:
                while not parent._stop.is_set() and not self.is_closed():
                    if self.is_ready():
                        parent._write_health_state("connected")
                    await asyncio.sleep(30.0)

            async def _send_outbox(self) -> None:
                """Deliver main-thread lobby output in the configured channel."""
                map_messages: dict[tuple[int, str], Any] = {}
                while not parent._stop.is_set() and not self.is_closed():
                    try:
                        outbound = parent.outbox.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.15)
                        continue
                    try:
                        channel = self.get_channel(outbound.channel_id)
                        if channel is None:
                            channel = await self.fetch_channel(outbound.channel_id)
                        if outbound.reaction:
                            map_post = map_messages.get(
                                (outbound.channel_id, outbound.map_key)
                            )
                            if map_post is None:
                                log.debug(
                                    "dropped map reaction with no map post: %s",
                                    outbound.reaction,
                                )
                                continue
                            await map_post.add_reaction(outbound.reaction)
                            continue
                        posted = await channel.send(
                            outbound.content,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        if outbound.map_message:
                            map_messages[(
                                outbound.channel_id, outbound.map_key
                            )] = posted
                            while len(map_messages) > 128:
                                del map_messages[next(iter(map_messages))]
                    except Exception as exc:
                        log.warning("could not send Discord lobby message: %s", exc)
                    finally:
                        parent.outbox.task_done()

            async def _rename_status_channel(self, channel_id):
                # Separate task: a channel-rename 429 must not hold up cards,
                # other rooms, transcript delivery, or staff responses.
                while not parent._stop.is_set():
                    desired = self.rename_targets.get(channel_id)
                    delay = self.rename_after.get(channel_id, 0) - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(min(delay, 1.0))
                        continue
                    if not desired:
                        return
                    channel = self.get_channel(channel_id)
                    if channel is None:
                        channel = await self.fetch_channel(channel_id)
                    if str(getattr(channel, "name", "")) == desired:
                        self.rename_targets.pop(channel_id, None)
                        return
                    try:
                        # Discord applies route-level limits across channel edits.
                        # Serialize rename attempts and leave a small gap so three
                        # rooms opening together do not make the third one eat a
                        # long rate-limit penalty.
                        async with self.rename_rate_lock:
                            delay = self.rename_next_at - time.monotonic()
                            if delay > 0:
                                await asyncio.sleep(delay)
                            self.rename_next_at = (
                                time.monotonic()
                                + parent._status_rename_spacing_seconds()
                            )
                            await channel.edit(
                                name=desired,
                                reason="PoolRotater public lobby status",
                            )
                    except Exception as exc:
                        # Keep the historical long per-channel cooldown after a
                        # successful rename, but do not strand a failed startup
                        # rename for ten minutes. Respect a retry_after supplied
                        # by discord.py when one is available.
                        try:
                            retry_after = float(getattr(exc, "retry_after", 0.0) or 0.0)
                        except (TypeError, ValueError):
                            retry_after = 0.0
                        retry_delay = retry_after or max(
                            15.0, parent._status_rename_spacing_seconds()
                        )
                        self.rename_after[channel_id] = (
                            time.monotonic() + min(600.0, retry_delay)
                        )
                        log.warning(
                            "status channel rename delayed for %s (%s)",
                            channel_id,
                            type(exc).__name__,
                        )
                    else:
                        self.rename_after[channel_id] = time.monotonic() + 600.0
                        if self.rename_targets.get(channel_id) == desired:
                            self.rename_targets.pop(channel_id, None)
                            return

            async def _send_status(self, status):
                channel = self.get_channel(status.channel_id)
                if channel is None:
                    channel = await self.fetch_channel(status.channel_id)
                if status.rename_channel:
                    desired = parent._safe_status_channel_name(
                        status.channel_base_name, status.is_open
                    )
                    self.rename_targets[status.channel_id] = desired
                    task = self.rename_tasks.get(status.channel_id)
                    if task is None or task.done():
                        self.rename_tasks[status.channel_id] = self.loop.create_task(
                            self._rename_status_channel(status.channel_id))
                colour = 0x2ECC71 if status.is_open else 0xE74C3C
                state_word = "Open" if status.is_open else "Closed"
                join_url = web_url = None
                if status.is_open and status.room_id:
                    join_url, web_url = public_room_links(
                        status.room_id, status.join_base_url
                    )
                embed = discord.Embed(
                    title=(
                        f"{'🟢' if status.is_open else '🔴'} "
                        f"{status.display_name} — {state_word}"
                    ),
                    description=status.room_name or None,
                    colour=colour,
                    timestamp=datetime.now(timezone.utc),
                    url=join_url,
                )
                total = status.player_count + status.spectator_count
                occupancy = f"{total} total"
                if status.spectator_count:
                    occupancy += (
                        f" ({status.player_count} playing, "
                        f"{status.spectator_count} spectating)"
                    )
                embed.add_field(
                    name="Players", value=occupancy, inline=True
                )
                embed.add_field(
                    name="Star rating",
                    value=f"{status.sr_min:.2f}–{status.sr_max:.2f}★",
                    inline=True,
                )
                embed.add_field(
                    name="Status", value=status.lifecycle, inline=True
                )
                if status.map_label:
                    map_value = status.map_label
                    if status.map_url:
                        map_value = f"[{map_value}]({status.map_url})"
                    embed.add_field(
                        name="Current map",
                        value=map_value[:1024],
                        inline=False,
                    )
                if status.is_open and status.room_id:
                    embed.add_field(
                        name="Join",
                        value=(
                            f"[Join {status.display_name} in osu!lazer]"
                            f"({join_url}) · [View on osu! web]({web_url})"
                        ),
                        inline=False,
                    )
                embed.set_footer(
                    text=f"PoolRotater public status · {status.instance}"
                )

                key = str(status.channel_id)
                message_id = self.status_message_ids.get(key)
                posted = None
                if message_id:
                    try:
                        partial = channel.get_partial_message(message_id)
                        posted = await partial.edit(
                            content=None,
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.NotFound:
                        self.status_message_ids.pop(key, None)
                    except Exception as exc:
                        log.warning(
                            "could not edit Discord public status message "
                            "in channel %s: %s",
                            status.channel_id,
                            exc,
                        )
                if posted is None and key not in self.status_message_ids:
                    posted = await channel.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    self.status_message_ids[key] = int(posted.id)
                    parent._save_status_message_ids(self.status_message_ids)

            async def _status_worker(self, channel_id):
                while not parent._stop.is_set():
                    status = parent._take_status(channel_id)
                    if status is None:
                        return
                    with parent._status_lock:
                        parent._status_inflight.add(channel_id)
                    try:
                        await self._send_status(status)
                    except Exception as exc:
                        log.warning("could not update public status for %s (%s)", channel_id, type(exc).__name__)
                        with parent._status_lock:
                            parent._status_pending.setdefault(channel_id, status)
                    finally:
                        with parent._status_lock:
                            parent._status_inflight.discard(channel_id)
                    # Coalesce intermediate snapshots while one is in flight.
                    for _ in range(75):
                        await asyncio.sleep(0.2)
                        with parent._status_lock:
                            pending = parent._status_pending.get(channel_id)
                        if pending is not None and pending.is_open != status.is_open:
                            break

            async def _status_dispatch(self):
                while not parent._stop.is_set():
                    with parent._status_lock:
                        channels = list(parent._status_pending)
                    for channel_id in channels:
                        task = self.status_tasks.get(channel_id)
                        if task is None or task.done():
                            self.status_tasks[channel_id] = self.loop.create_task(self._status_worker(channel_id))
                    await asyncio.sleep(0.2)

            async def close(self):
                tasks = list(getattr(self, "status_tasks", {}).values()) + list(getattr(self, "rename_tasks", {}).values())
                tasks += [t for t in (getattr(self, "outbox_task", None), getattr(self, "status_dispatch_task", None), getattr(self, "health_task", None)) if t]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                await super().close()
            async def on_message(self, message) -> None:
                if parent._stop.is_set() or message.author.bot or not message.guild:
                    return
                configured_guild = parent._guild_id()
                if configured_guild and int(message.guild.id) != configured_guild:
                    return
                if int(message.channel.id) not in parent._channel_ids():
                    return
                original_content = str(message.content or "").strip()
                targets = parent.command_targets(original_content, int(message.channel.id))
                if not targets:
                    return
                if len(targets) != 1:
                    log.error("ambiguous Discord command route; not executing")
                    return
                target, content = targets[0]
                role = target._role_for_member(message.author)
                if role is None:
                    try:
                        await message.reply(
                            "You need Discord moderator permissions to control the lobby.",
                            mention_author=False,
                        )
                    except Exception:
                        pass
                    return
                if not target._role_allows_command(role, content):
                    # A red X means the Discord message was received but this
                    # role is not authorized to execute that command.  Most
                    # importantly, moderators cannot relay arbitrary lobby chat
                    # or invoke admin lifecycle controls such as .close.
                    try:
                        await message.add_reaction("❌")
                    except Exception:
                        pass
                    return
                try:
                    target.enqueue_command(
                        DiscordCommand(
                            discord_user_id=int(message.author.id),
                            username=str(message.author.display_name),
                            role=role,
                            content=content[:1000],
                            channel_id=int(message.channel.id),
                        )
                    )
                    try:
                        await message.add_reaction("✅")
                    except Exception:
                        pass
                except queue.Full:
                    log.warning("Discord control queue full; dropped one command")

        intents = discord.Intents.default()
        intents.message_content = True
        client = ControlClient(intents=intents)
        self._client = client
        try:
            client.run(token, log_handler=None)
        except Exception as exc:
            if not self._stop.is_set():
                self._write_health_state("failed", type(exc).__name__)
                log.error("Discord control stopped: %s", exc)
        else:
            if not self._stop.is_set():
                self._write_health_state("failed", "UnexpectedExit")
                log.error("Discord control stopped without a shutdown request")
