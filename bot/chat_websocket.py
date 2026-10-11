"""Account-level osu! notification WebSocket used for real-time lobby chat.

osu! sends incoming chat through one authenticated notification socket.  This
module owns that socket, subscribes with ``chat.start``, and routes
``chat.message.new`` batches to room-specific thread-safe queues.  It never
executes lobby code on the socket thread.

HTTP remains authoritative for sends, keepalive ACKs, and occasional history
reconciliation.  If the socket cannot connect, LobbyBot automatically falls
back to its existing HTTP polling path.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
import json
import logging
import queue
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

log = logging.getLogger("chat_websocket")


class ChatProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatSocketEvent:
    """A socket lifecycle or message event delivered to one lobby thread."""

    kind: str
    generation: int
    messages: tuple[dict, ...] = ()
    channel_id: int | None = None


class ChatWebSocket:
    """Maintain one reconnecting osu! notification socket per OAuth account."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        endpoint: str = "wss://notify.ppy.sh",
        enabled: bool = True,
        reconnect_max_seconds: float = 30.0,
        invalidate_token: Callable[[], None] | None = None,
    ):
        self.token_provider = token_provider
        self.endpoint = str(endpoint or "wss://notify.ppy.sh").strip()
        self.enabled = bool(enabled and self.endpoint)
        parsed_endpoint = urlsplit(self.endpoint)
        endpoint_host = (parsed_endpoint.hostname or "").casefold()
        if self.enabled and (
            parsed_endpoint.scheme.casefold() != "wss"
            or not (endpoint_host == "ppy.sh" or endpoint_host.endswith(".ppy.sh"))
        ):
            raise ValueError(
                "chat_websocket_endpoint must be an official wss://*.ppy.sh URL"
            )
        self.reconnect_max_seconds = max(1.0, float(reconnect_max_seconds))
        self.invalidate_token = invalidate_token

        self._routes: dict[int, set[queue.Queue]] = defaultdict(set)
        self._routes_lock = threading.RLock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._last_event_at = 0.0
        self._last_error = ""
        self._last_401_refresh_at = float("-inf")

    @property
    def connected(self) -> bool:
        return self.enabled and self._connected.is_set()

    @property
    def generation(self) -> int:
        with self._generation_lock:
            return self._generation

    @property
    def last_error(self) -> str:
        return self._last_error

    def start(self) -> None:
        if not self.enabled:
            log.info("real-time osu! chat WebSocket disabled; using HTTP polling")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="osu-chat-websocket",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                pass
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        self._connected.clear()

    def subscribe(self, channel_id: int, inbox: queue.Queue) -> None:
        channel_id = int(channel_id)
        with self._routes_lock:
            self._routes[channel_id].add(inbox)
        # A room created after the account socket connected still needs one
        # history reconciliation before it begins consuming live events.
        if self.connected:
            self._put(inbox, ChatSocketEvent("connected", self.generation))
        log.info("real-time chat route added for channel %s", channel_id)

    def unsubscribe(self, channel_id: int, inbox: queue.Queue) -> None:
        channel_id = int(channel_id)
        with self._routes_lock:
            targets = self._routes.get(channel_id)
            if targets is None:
                return
            targets.discard(inbox)
            if not targets:
                self._routes.pop(channel_id, None)
        log.info("real-time chat route removed for channel %s", channel_id)

    def _put(self, inbox: queue.Queue, event: ChatSocketEvent) -> None:
        try:
            inbox.put_nowait(event)
        except queue.Full:
            # Production inboxes are deliberately unbounded.  Keep this guard
            # for tests or custom integrations without killing the socket.
            log.error("real-time chat inbox full; event dropped")

    def _targets(self, channel_id: int | None = None) -> list[queue.Queue]:
        with self._routes_lock:
            if channel_id is not None:
                return list(self._routes.get(int(channel_id), ()))
            return list({target for targets in self._routes.values() for target in targets})

    def _broadcast(self, kind: str, generation: int) -> None:
        event = ChatSocketEvent(kind, generation)
        for inbox in self._targets():
            self._put(inbox, event)

    def _route_messages(self, data: dict, generation: int) -> None:
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list):
            return

        users: dict[int, str] = {}
        for user in data.get("users") or []:
            if not isinstance(user, dict):
                continue
            try:
                uid = int(user.get("id"))
            except (TypeError, ValueError):
                continue
            username = str(user.get("username") or "").strip()
            if username:
                users[uid] = username

        by_channel: dict[int, list[dict]] = defaultdict(list)
        for raw in raw_messages:
            if not isinstance(raw, dict):
                continue
            try:
                channel_id = int(raw.get("channel_id"))
            except (TypeError, ValueError):
                continue
            message = dict(raw)
            try:
                sender_id = int(message.get("sender_id"))
            except (TypeError, ValueError):
                sender_id = 0
            if sender_id in users:
                message["_sender_username"] = users[sender_id]
            by_channel[channel_id].append(message)

        for channel_id, messages in by_channel.items():
            messages.sort(key=lambda item: int(item.get("message_id", 0) or 0))
            event = ChatSocketEvent(
                "messages", generation, tuple(messages), channel_id
            )
            for inbox in self._targets(channel_id):
                self._put(inbox, event)

    def _handle_payload(self, payload: str, generation: int) -> bool:
        try:
            message = json.loads(payload)
        except (TypeError, ValueError):
            log.warning("ignored invalid JSON from osu! chat WebSocket")
            return True
        if not isinstance(message, dict):
            return True
        if message.get("error"):
            log.warning("osu! chat WebSocket error event: %s", message["error"])
            return False

        event = str(message.get("event") or "")
        data = message.get("data")
        if event == "chat.message.new" and isinstance(data, dict):
            self._last_event_at = time.monotonic()
            self._route_messages(data, generation)
        elif event in ("chat.channel.join", "chat.channel.part") and isinstance(data, dict):
            try:
                channel_id = int(data.get("channel_id"))
            except (TypeError, ValueError):
                return True
            kind = "channel_join" if event.endswith("join") else "channel_part"
            lifecycle = ChatSocketEvent(
                kind, generation, channel_id=channel_id
            )
            for inbox in self._targets(channel_id):
                self._put(inbox, lifecycle)
        return True

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception:
            log.exception("osu! chat WebSocket worker stopped unexpectedly")
        finally:
            self._loop = None
            self._connected.clear()

    async def _sleep_until_retry(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    async def _run(self) -> None:
        try:
            import aiohttp
        except ImportError:
            self.enabled = False
            self._last_error = "aiohttp is not installed"
            log.error(
                "real-time osu! chat unavailable: install requirements.txt; "
                "falling back to HTTP polling"
            )
            return

        self._loop = asyncio.get_running_loop()
        delay = 1.0
        while not self._stop.is_set():
            generation = self.generation
            try:
                token = self.token_provider()
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(
                        self.endpoint,
                        headers={"Authorization": f"Bearer {token}"},
                        heartbeat=25.0,
                        autoping=True,
                        max_msg_size=4 * 1024 * 1024,
                    ) as socket:
                        await socket.send_json({"event": "chat.start"})
                        with self._generation_lock:
                            self._generation += 1
                            generation = self._generation
                        self._last_error = ""
                        self._last_event_at = time.monotonic()
                        self._connected.set()
                        self._broadcast("connected", generation)
                        log.info(
                            "real-time osu! chat connected (generation %d)",
                            generation,
                        )
                        delay = 1.0
                        connected_at = time.monotonic()

                        while not self._stop.is_set():
                            # Reconnect shortly before the normal 24-hour token
                            # lifetime. TokenManager refreshes within its final
                            # five minutes, so the next handshake gets a fresh
                            # bearer token without a blind refresh.
                            if time.monotonic() - connected_at >= 86160.0:
                                log.info("renewing osu! chat WebSocket authentication")
                                break
                            try:
                                incoming = await socket.receive(timeout=5.0)
                            except asyncio.TimeoutError:
                                continue
                            if incoming.type == aiohttp.WSMsgType.TEXT:
                                if not self._handle_payload(incoming.data, generation):
                                    raise ChatProtocolError(
                                        "server rejected the chat WebSocket command"
                                    )
                            elif incoming.type in (
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                error = socket.exception()
                                if error:
                                    raise error
                                break

                        if not socket.closed:
                            try:
                                await socket.send_json({"event": "chat.end"})
                            except Exception:
                                pass
                            await socket.close()
            except aiohttp.WSServerHandshakeError as exc:
                self._last_error = f"HTTP {exc.status} WebSocket handshake"
                now = time.monotonic()
                if (
                    exc.status == 401
                    and self.invalidate_token is not None
                    and now - self._last_401_refresh_at >= 300.0
                ):
                    try:
                        self.invalidate_token()
                        self._last_401_refresh_at = now
                    except Exception:
                        log.debug("could not invalidate rejected OAuth token", exc_info=True)
                log.warning(
                    "osu! chat WebSocket handshake failed: HTTP %s; "
                    "HTTP polling remains active",
                    exc.status,
                )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                ChatProtocolError,
            ) as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                if not self._stop.is_set():
                    log.warning(
                        "osu! chat WebSocket disconnected (%s); HTTP polling "
                        "remains active",
                        type(exc).__name__,
                    )
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                if not self._stop.is_set():
                    log.exception("unexpected osu! chat WebSocket failure")
            finally:
                was_connected = self._connected.is_set()
                self._connected.clear()
                if was_connected:
                    self._broadcast("disconnected", generation)

            if self._stop.is_set():
                break
            await self._sleep_until_retry(delay)
            delay = min(self.reconnect_max_seconds, delay * 2.0)
