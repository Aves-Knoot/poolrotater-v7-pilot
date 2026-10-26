"""Referee hub client (SignalR).

signalrcore is thread-based, not asyncio. Rather than bridging the two, hub
callbacks simply push onto a thread-safe queue that the bot's main loop
drains. Keeps the concurrency story boring, which matters for something meant
to run unattended.

Method and event names come from IRefereeHubServer / IRefereeHubClient.
Payload field names come from the published JSON Schemas, so they should be
exact -- but this API is explicitly marked unstable in the docs, so treat a
sudden flood of errors as "the contract moved" rather than "my code broke".
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from signalrcore.hub_connection_builder import HubConnectionBuilder

log = logging.getLogger("hub")


class HubTransportError(RuntimeError):
    """Base class for transient/unknown-outcome referee transport failures."""


class HubDisconnected(HubTransportError):
    """The SignalR transport is not currently connected."""


class HubOperationDeferred(HubDisconnected):
    """A finite local guard refused an operation before downstream invocation.

    Only guards outside the transport may raise this type. It must never wrap
    a send failure, lost reply, server error, or arbitrary disconnected error.
    Keep the existing messages for callers that also check their meaning.
    """

    REASONS = {
        "shared referee operation lock timed out": "shared_referee_operation_busy",
        "shared referee transport is disconnected": "referee_not_connected",
        "room roster requires a fresh authoritative JoinRoom": "roster_snapshot_pending",
        "room is quiesced": "room_quiesced",
    }

    def __init__(self, message):
        if type(message) is not str or message not in self.REASONS:
            raise ValueError("invalid local referee deferral")
        self.reason = self.REASONS[message]
        super().__init__(message)


class HubInvocationUncertain(HubTransportError):
    """A hub command may or may not have reached the server. Never blind-retry."""


_TRANSPORT_CLOSED = object()

# Server -> client events we care about. The rest exist but are noise for a
# rotating farm lobby (teams, rolls, bans, countdown chatter).
# Rate-changing mods alter song length, so every player in a room must share
# them. osu! therefore rejects them as FREE mods -- they can only be required
# for the whole lobby. Sending one in allowed_mods returns
# "Error 12: Invalid mods. Details: Invalid free mods were selected: DT".
RATE_CHANGING = {"DT", "NC", "HT", "DC"}

# Mods that make no sense in a multiplayer farm context or are outright
# rejected: autoplay/cinema/relax variants.
NEVER_FREE = RATE_CHANGING | {"AT", "CN", "RX", "AP", "TP"}


def validate_mods(allowed: list[str], required: list[str]) -> None:
    """Fail fast, before a room exists.

    Getting this wrong costs a room slot: MakeRoom succeeds, then the first
    EditCurrentPlaylistItem is rejected and the room has to be torn down.
    """
    bad = [m for m in (allowed or []) if m.upper() in NEVER_FREE]
    if bad:
        raise SystemExit(
            f"Invalid allowed_mods: {', '.join(bad)}.\n"
            f"Rate-changing mods ({', '.join(sorted(RATE_CHANGING))}) cannot "
            f"be free mods -- every player must share the same song speed.\n"
            f"Move them to required_mods to apply them to the whole lobby, "
            f"or drop them.\n"
            f"Note: with a rate-changing mod required, your pool's star "
            f"ratings are still NOMOD values -- lower sr_min/sr_max to suit."
        )
    overlap = set(m.upper() for m in (allowed or [])) & \
              set(m.upper() for m in (required or []))
    if overlap:
        raise SystemExit(
            f"Mods in both allowed_mods and required_mods: "
            f"{', '.join(sorted(overlap))}. Pick one.")


EVENTS = [
    "MatchStarted", "MatchCompleted", "MatchAborted",
    "UserJoined", "UserLeft", "UserKicked",
    "UserStatusChanged",          # idle / ready / playing / finished_play
    "PlaylistItemAdded", "PlaylistItemChanged", "PlaylistItemRemoved",
    "RoomSettingsChanged",
    "CountdownStarted", "CountdownStopped",
]


class RefereeHub:
    def __init__(self, spectator_url: str, token_provider, events: queue.Queue):
        self.url = spectator_url.rstrip("/") + "/referee"
        self.token_provider = token_provider
        self.events = events
        self.conn = None
        self._connected = threading.Event()
        self._results: dict[str, queue.Queue] = {}
        self._connect_generation = 0
        self._ever_connected = False
        self._stopping = False
        self._stop_event = threading.Event()
        self._manager = None
        self._send_lock = threading.RLock()
        self._results_lock = threading.Lock()
        self._last_received = 0.0

    def connect(self, timeout: float = 30.0) -> None:
        if self._manager and self._manager.is_alive():
            raise RuntimeError("referee connection already started")
        self._stopping = False
        self._stop_event.clear()
        self._manager = threading.Thread(target=self._connection_loop,
                                         name="referee-connection", daemon=True)
        self._manager.start()
        if not self.wait_connected(timeout):
            self.stop()
            raise HubDisconnected(f"Hub did not connect within {timeout}s")

    def _build_connection(self):
        # Own reconnects here. signalrcore's reconnect state can remain stuck
        # after DNS failures, without delivering on_close/on_open callbacks.
        conn = (
            HubConnectionBuilder()
            .with_url(self.url, options={
                # Called on every (re)connect, so a rotated token is picked up
                # automatically rather than the connection dying after an hour.
                "access_token_factory": self.token_provider,
                "verify_ssl": True,
            })
            .build()
        )
        def current(callback):
            def guarded(*args):
                if self.conn is conn and not self._stopping:
                    return callback(*args)
            return guarded
        conn.on_open(current(self._on_open))
        conn.on_close(current(self._on_close))
        conn.on_error(current(self._on_error))
        # Include protocol pings/completions, not just gameplay events, in the
        # receive watchdog. This is not an HTTP request or a gameplay command.
        receive = conn.on_message
        def on_message(*args, **kwargs):
            if self.conn is not conn or self._stopping:
                return
            self._last_received = time.monotonic()
            return receive(*args, **kwargs)
        conn.on_message = on_message
        for name in EVENTS:
            conn.on(name, current(self._make_handler(name)))
        return conn

    def _connection_loop(self):
        from signalrcore.messages.ping_message import PingMessage
        backoff = 2.0
        while not self._stop_event.is_set():
            conn = None
            try:
                conn = self._build_connection()
                self.conn = conn
                log.info("connecting to %s", self.url)
                generation_before_start = self._connect_generation
                conn.start()
                deadline = time.monotonic() + 30
                while not self._stop_event.wait(0.2):
                    if self._connect_generation > generation_before_start:
                        break
                    if time.monotonic() >= deadline:
                        raise HubDisconnected("referee handshake timed out")
                last_ping = time.monotonic()
                while not self._stop_event.wait(0.2):
                    if not self.is_connected():
                        raise HubDisconnected("referee socket disconnected")
                    now = time.monotonic()
                    if now - self._last_received > 60:
                        raise HubDisconnected("no referee traffic for 60 seconds")
                    if now - last_ping >= 15:
                        with self._send_lock:
                            conn.transport.send(PingMessage())
                        last_ping = now
                    if now - deadline > 30:
                        backoff = 2.0
            except (Exception, SystemExit) as exc:
                if not self._stopping:
                    log.warning("referee transport unavailable (%s); reconnect in %.1fs",
                                type(exc).__name__, backoff)
            finally:
                self._on_close()
                # Retire old callbacks before closing; never open a replacement
                # until disposal returns. If disposal hangs, shutdown stays
                # bounded and we do NOT create a second controller.
                if self.conn is conn:
                    self.conn = None
                if conn is not None:
                    try:
                        # close() alone can leave recv() blocked in another
                        # thread. Shutdown the socket before the library join.
                        import socket
                        transport = getattr(conn, "transport", None)
                        client = getattr(transport, "_client", None)
                        sock = getattr(client, "sock", None)
                        if sock is not None:
                            try:
                                sock.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                        conn.stop()
                    except Exception:
                        log.warning("referee transport disposal failed", exc_info=True)
            if self._stop_event.wait(backoff):
                break
            backoff = min(30.0, backoff * 2)

    def _on_open(self) -> None:
        reconnected = self._ever_connected
        self._ever_connected = True
        self._connect_generation += 1
        self._last_received = time.monotonic()
        self._connected.set()
        if reconnected:
            log.warning("hub reconnected (generation %d)", self._connect_generation)
            try:
                self.events.put_nowait(("HubReconnected", {
                    "generation": self._connect_generation,
                }))
            except Exception:
                pass
        else:
            log.info("hub connected")

    def _on_close(self) -> None:
        was_connected = self._connected.is_set()
        self._connected.clear()
        if self._stopping and was_connected:
            log.info("hub connection closed")
        elif was_connected:
            log.warning("hub connection closed; automatic reconnect will be attempted")
            try:
                self.events.put_nowait(("HubDisconnected", {}))
            except Exception:
                pass

        # Any in-flight command now has an unknown outcome. Wake invoke()
        # immediately instead of letting it sit for 15 seconds and tear down
        # the entire lobby on timeout.
        with self._results_lock:
            pending = list(self._results.values())
        for q in pending:
            try:
                q.put_nowait(_TRANSPORT_CLOSED)
            except Exception:
                pass

    def is_connected(self) -> bool:
        transport = getattr(self.conn, "transport", None)
        if transport is not None and not transport.is_connected():
            self._on_close()
        return self._connected.is_set()

    def wait_connected(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        while not self._stopping:
            if self.is_connected():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._stop_event.wait(min(0.1, remaining))
        return False

    @staticmethod
    def describe(msg) -> str:
        """Pull readable text out of a CompletionMessage.

        signalrcore's error callback hands over the raw object, whose repr is
        just a memory address -- which hides the one piece of information that
        actually matters. The hub returns business-logic errors as strings
        here (see ThrowHelper in the docs), so this is where the real reason
        for a failed invocation lives.
        """
        for attr in ("error", "result", "invocation_id"):
            val = getattr(msg, attr, None)
            if val:
                return f"{attr}={val!r}"
        return repr(msg)

    def _on_error(self, msg) -> None:
        log.error("hub error: %s", self.describe(msg))
        # Unblock any invoke() waiting on a result -- otherwise a rejected
        # call just sits until its timeout with no explanation.
        with self._results_lock:
            pending = list(self._results.values())
        for q in pending:
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    def _make_handler(self, name: str):
        def handler(args):
            payload = args[0] if isinstance(args, list) and args else args
            log.debug("event %s: %s", name, payload)
            self.events.put((name, payload))
        return handler

    def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()
        self._on_close()
        # Disposal belongs to the manager; stop must never join a library
        # receiver forever (especially from a signal handler/shutdown path).
        if self._manager and self._manager is not threading.current_thread():
            self._manager.join(timeout=1.0)

    # ------------------------------------------------------- invocation

    def invoke(self, method: str, args: list, timeout: float = 15.0):
        """Invoke a hub method and wait for its result.

        A timeout or transport drop is deliberately reported as
        HubInvocationUncertain. The command may already have reached osu!, so
        callers must reconcile server state instead of blindly sending it a
        second time.
        """
        if self.conn is None:
            raise HubDisconnected(f"{method}: hub has not been connected")

        # The account manager owns reconnects. Fail promptly; the room worker
        # must rejoin/reconcile before issuing mutations on a new generation.
        if self._stopping or not self.is_connected():
            raise HubDisconnected(f"{method}: referee hub is disconnected")

        key = f"{method}-{time.time()}"
        result_q: queue.Queue = queue.Queue(maxsize=1)
        with self._results_lock:
            self._results[key] = result_q
        generation = self._connect_generation
        log.debug("-> %s %s", method, args)
        try:
            def _deliver(message):
                try:
                    result_q.put_nowait(message)
                except queue.Full:
                    # A disconnect/error sentinel may already have woken the
                    # waiter. Never block signalrcore's callback thread.
                    pass

            try:
                with self._send_lock:
                    if self._stopping or not self.is_connected():
                        raise HubDisconnected("referee disconnected before send")
                    self.conn.send(method, args, _deliver)
            except Exception as e:
                self._on_close()
                raise HubInvocationUncertain(
                    f"{method}: send failed; server outcome unknown: {e}") from e

            try:
                msg = result_q.get(timeout=timeout)
            except queue.Empty as e:
                self._on_close()
                raise HubInvocationUncertain(
                    f"{method}: no completion within {timeout}s; server outcome "
                    "unknown") from e
        finally:
            with self._results_lock:
                self._results.pop(key, None)

        if msg is _TRANSPORT_CLOSED:
            raise HubInvocationUncertain(
                f"{method}: connection closed before completion; server outcome "
                "unknown")

        # If the transport reconnected while the command was in flight, even a
        # missing/late completion cannot safely be retried. A real completion
        # received on the original generation is still accepted below.
        err = getattr(msg, "error", None)
        if err:
            raise RuntimeError(f"{method} failed: {err}")
        result = getattr(msg, "result", None)
        log.debug("<- %s %s", method, result)
        if generation != self._connect_generation:
            log.warning(
                "%s completed across a hub reconnect (generation %d -> %d)",
                method, generation, self._connect_generation,
            )
        return result

    # ------------------------------------------------ typed conveniences

    def join_room(self, room_id: int):
        return self.invoke("JoinRoom", [int(room_id)])

    def make_room(self, name: str, beatmap_id: int, ruleset_id: int = 0):
        return self.invoke("MakeRoom", [{
            "name": name,
            "beatmap_id": int(beatmap_id),
            "ruleset_id": int(ruleset_id),
        }])

    def change_room_settings(self, room_id: int, name=None, password=None,
                             match_type=None):
        """NOTE the password semantics: null keeps the existing password,
        empty string clears it. Passing None to 'remove' the password would
        silently leave the room locked and unjoinable."""
        body = {}
        if name is not None:
            body["name"] = name
        if password is not None:
            body["password"] = password
        if match_type is not None:
            body["type"] = match_type
        return self.invoke("ChangeRoomSettings", [int(room_id), body])

    def edit_current_item(self, room_id: int, beatmap_id: int,
                          allowed_mods=None, required_mods=None,
                          freestyle=None, ruleset_id: int = 0):
        body = {"beatmap_id": int(beatmap_id), "ruleset_id": int(ruleset_id)}
        if required_mods is not None:
            body["required_mods"] = [{"acronym": m} for m in required_mods]
        if allowed_mods is not None:
            body["allowed_mods"] = [{"acronym": m} for m in allowed_mods]
        if freestyle is not None:
            body["freestyle"] = bool(freestyle)
        return self.invoke("EditCurrentPlaylistItem", [int(room_id), body])

    def add_playlist_item(self, room_id: int, beatmap_id: int,
                          allowed_mods=None, required_mods=None,
                          freestyle: bool = False, ruleset_id: int = 0):
        """Queue an extra map so players can pre-download it.

        Confirmed against AddPlaylistItemRequest's published schema: all five
        fields are REQUIRED (unlike EditCurrentPlaylistItemRequest, where
        everything is optional). No position/order field -- items append and
        the server sequences them, so queue order follows call order.
        """
        body = {
            "beatmap_id": int(beatmap_id),
            "ruleset_id": int(ruleset_id),
            "required_mods": [{"acronym": m} for m in (required_mods or [])],
            "allowed_mods": [{"acronym": m} for m in (allowed_mods or [])],
            "freestyle": bool(freestyle),
        }
        return self.invoke("AddPlaylistItem", [int(room_id), body])

    def remove_playlist_item(self, room_id: int, item_id: int):
        return self.invoke("RemovePlaylistItem",
                           [int(room_id), {"playlist_item_id": int(item_id)}])

    def start_match(self, room_id: int, countdown: int | None = None):
        # SignalR's StartMatch contract expects an integer.  Enforce this at
        # the transport boundary as a final guard against callers serializing
        # an integer-valued duration as a JSON floating-point number.
        normalized = None if countdown is None else max(1, int(round(float(countdown))))
        return self.invoke("StartMatch", [int(room_id), {"countdown": normalized}])

    def abort_match(self, room_id: int):
        return self.invoke("AbortMatch", [int(room_id)])

    def close_room(self, room_id: int):
        return self.invoke("CloseRoom", [int(room_id)])

    def invite(self, room_id: int, user_id: int):
        return self.invoke("InvitePlayer", [int(room_id), int(user_id)])

    def kick(self, room_id: int, user_id: int):
        return self.invoke("KickPlayer", [int(room_id), int(user_id)])
