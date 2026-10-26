"""One account-level connection, with room-scoped hub and Discord views.

No desktop automation. Room controllers keep independent state and queues;
only the supervisor may connect/disconnect the account or close shared HTTP.
"""
from __future__ import annotations

import copy
import logging
import math
import os
from pathlib import Path
import queue
import threading
import time

from .api import OsuApi
from .discord_control import DiscordControl
from .hub import HubDisconnected, HubTransportError, HubInvocationUncertain, HubOperationDeferred

log = logging.getLogger("multiroom")


class ProcessLease:
    """OS-released lock; a crash cannot leave a permanently stale lock."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if not self.file.tell():
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError(
                f"Another launcher owns {self.path.name}. Stop it before starting this one."
            ) from exc
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()
            self.file = None


class SharedReferee:
    def __init__(self, hub, upstream_events):
        self.hub = hub
        self.events = upstream_events
        # Serialise invocations: the existing SignalR error callback is not
        # invocation-specific. Also protect make->register from early events.
        self.lock = threading.RLock()
        self.routes = {}
        self.views = []
        self.stopping = threading.Event()
        self.dispatch_stop = threading.Event()
        self.thread = None
        self.shutdown_deadline = None
        self.close_attempted = set()
        self._provider_sequence_generation = 0
        self._provider_sequence = 0

    def begin_shutdown(self, seconds=45.0):
        if self.shutdown_deadline is None:
            seconds = float(seconds)
            if not math.isfinite(seconds):
                seconds = 45.0
            self.shutdown_deadline = time.monotonic() + min(120.0, max(1.0, seconds))
        self.stopping.set()

    def remaining_shutdown(self):
        return max(0.0, self.shutdown_deadline - time.monotonic()) if self.shutdown_deadline is not None else 15.0

    def room_view(self, alias, events):
        view = RoomHub(self, alias, events)
        self.views.append(view)
        return view

    def start_dispatch(self):
        self.thread = threading.Thread(target=self._dispatch, name="referee-events", daemon=True)
        self.thread.start()

    def _dispatch(self):
        while not self.dispatch_stop.is_set():
            try:
                self.route_event(self.events.get(timeout=0.2))
            except queue.Empty:
                pass
            except Exception:
                log.exception("could not route referee event")

    def _provider_events(self):
        return getattr(self.hub, "requires_event_provenance", False) is True

    def _invalidate_provider_events(self):
        # Unqualified local provenance cannot establish membership or roster
        # authority, even if the underlying connection remains available.
        for view in self.views:
            if view.owned or view.creating:
                view.fence_roster(view.roster_progress())
        self._route("HubDisconnected", {})

    def route_event(self, item):
        """Admit local provider provenance before mirror or worker delivery."""
        if not self._provider_events():
            return self.route(*item)
        from poolrotater_host.provider_referee import _ProviderEvent, _ROSTER_EVENTS
        from poolrotater_host.referee_protocol import encoded
        from .hub import EVENTS
        with self.lock:
            try:
                current = getattr(self.hub, "_connect_generation", 0)
                valid = (type(current) is int and current > 0
                         and type(item) is _ProviderEvent and type(item.name) is str
                         and item.name in set(EVENTS) | {"HubDisconnected", "HubReconnected"}
                         and type(item.payload) is dict and type(item.generation) is int
                         and 0 < item.generation <= current
                         and type(item.sequence) is int and item.sequence > 0)
                if valid:
                    try:
                        encoded(item.payload, 32768)
                    except Exception:
                        valid = False
                if not valid:
                    self._invalidate_provider_events()
                    return
                lifecycle = item.name in ("HubDisconnected", "HubReconnected")
                if item.generation < current and not lifecycle:
                    return
                previous = 0
                if item.generation == current:
                    if self._provider_sequence_generation != current:
                        self._provider_sequence_generation = current
                        self._provider_sequence = 0
                    previous = self._provider_sequence
                    # Admission order and exact receipt ACK are separate: a
                    # synchronous drain can overtake a dispatcher-held event.
                    # Every event kind advances this local ordering evidence.
                    self._provider_sequence = max(previous, item.sequence)
                if item.name in _ROSTER_EVENTS:
                    rid = item.payload.get("room_id")
                    try:
                        target = self.routes.get(int(rid)) if rid is not None else None
                    except (TypeError, ValueError):
                        target = None
                    if target and target.roster_cutoff is not None:
                        generation, sequence = target.roster_cutoff
                        if item.generation == generation and item.sequence <= sequence:
                            return
                    if item.generation == current and item.sequence <= previous:
                        self._invalidate_provider_events()
                        return
                payload = {"generation": item.generation} if lifecycle else item.payload
                self._route(item.name, payload)
            finally:
                if type(item) is _ProviderEvent and type(item.sequence) is int:
                    acknowledge = getattr(self.hub, "acknowledge_event", None)
                    if callable(acknowledge):
                        acknowledge(item)

    def route(self, name, payload):
        with self.lock:
            if self._provider_events():
                self._invalidate_provider_events()
                return
            self._route(name, payload)

    def _route(self, name, payload):
        if name in ("HubDisconnected", "HubReconnected"):
            targets = [v for v in self.views if v.owned]
        else:
            rid = payload.get("room_id") if isinstance(payload, dict) else None
            try:
                target = self.routes.get(int(rid)) if rid is not None else None
            except (TypeError, ValueError):
                target = None
            targets = [target] if target else []
            if not targets:
                for pending in self.views:
                    if pending.creation_pending_id is not None and pending.creation_pending_id == rid:
                        if len(pending.creation_events) >= 128:
                            pending.creation_events_overflow = True
                        else:
                            pending.creation_events.append((name, copy.deepcopy(payload)))
                        return
                log.debug("unrouted/stale event %s (room %s)", name, rid)
        for target in targets:
            target.events.put((name, copy.deepcopy(payload)))

    def stop(self):
        self.stopping.set()
        self.dispatch_stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.hub.stop()


class RoomHub:
    def __init__(self, shared, alias, events):
        self.shared, self.alias, self.events = shared, alias, events
        self.owned = set()
        self.joined = {}
        self.roster_fenced = False
        self.roster_cutoff = None
        self.creation_observer = None
        self.creation_begin = None
        self.creation_committed = None
        self.creation_pending_id = None
        self.creation_events = []
        self.creation_events_overflow = False
        self.creating = False

    def is_connected(self):
        return self.shared.hub.is_connected() and not self.roster_fenced

    def wait_connected(self, timeout=30):
        return self.shared.hub.wait_connected(timeout)

    def stop(self):
        # A room owns its room(s), NOT the account-level websocket.
        return None

    def connect(self, *args, **kwargs):
        raise RuntimeError("Only the supervisor may open the referee connection")

    def _generation(self):
        return getattr(self.shared.hub, "_connect_generation", 0)

    def roster_progress(self):
        if not self.shared._provider_events():
            return None
        inspect = getattr(self.shared.hub, "roster_progress", None)
        progress = inspect() if callable(inspect) else None
        if (type(progress) is tuple and len(progress) == 4
                and type(progress[0]) is int and progress[0] > 0
                and type(progress[1]) is int and progress[1] >= 0
                and type(progress[2]) is bool
                and type(progress[3]) is int and progress[3] >= 0):
            return progress
        return (self._generation(), 0, True, 0)

    def fence_roster(self, progress=None):
        self.roster_fenced = True
        self.joined.clear()
        if progress is not None:
            self.roster_cutoff = (progress[0], progress[3])

    def make_room(self, *args, **kwargs):
        with self.shared.lock:
            if self.creating:
                raise HubInvocationUncertain('previous room creation is still being verified')
            self.creating = True
        try:
            return self._make_room(*args, **kwargs)
        finally:
            with self.shared.lock:
                self.creating = False
                self.creation_pending_id = None
                self.creation_events = []
                self.creation_events_overflow = False

    def _make_room(self, *args, **kwargs):
        split = getattr(self.shared.hub, 'split_creation_verification', False) is True
        with self.shared.lock:
            if self.shared.stopping.is_set():
                raise HubDisconnected("supervisor is stopping")
            if self.owned:
                raise RuntimeError("Refusing to create replacement before old room is closed")
            if self.creation_begin is not None:
                self.creation_begin()
            generation = self._generation()
            if split:
                result, generation = self.shared.hub.begin_make_room(*args, _on_created=self.creation_observer, **kwargs)
                self.creation_pending_id = result['room_id']
            elif getattr(self.shared.hub,'creation_observer_supported',False) is True:
                result = self.shared.hub.make_room(*args,_on_created=self.creation_observer,**kwargs)
            else:
                result = self.shared.hub.make_room(*args, **kwargs)
            if not split:
                return self._adopt_created(result, generation)
        result = self.shared.hub.finish_make_room(result, generation,
            cancelled=self.shared.stopping.is_set)
        with self.shared.lock:
            return self._adopt_created(result, generation)

    def _adopt_created(self, result, generation):
        # Caller holds the shared routing lock. A candidate is never adopted
        # after shutdown/generation change, even if its HTTP read succeeded.
        if self.shared.stopping.is_set() or generation != self._generation():
            raise HubInvocationUncertain('referee_generation_changed')
        if self.creation_events_overflow or self.roster_fenced:
            raise HubInvocationUncertain('referee_snapshot_requires_review')
        if not self.shared.hub.is_connected():
            raise HubInvocationUncertain('referee_generation_changed')
        rid = int(result["room_id"])
        self.owned.add(rid)
        self.shared.routes[rid] = self
        self.joined[rid] = (generation, result)
        try:
            if self.creation_committed is not None:
                self.creation_committed(result)
        except Exception:
            self.owned.discard(rid)
            self.shared.routes.pop(rid, None)
            self.joined.pop(rid, None)
            raise
        # Candidate events have no mutation authority. Only after independent
        # verification and the broker snapshot may they reach the room mirror.
        for item in self.creation_events:
            self.events.put(item)
        self.creation_events = []
        log.info("%s owns room %s on the shared connection", self.alias, rid)
        return result

    def refresh_ready(self):
        check = getattr(self.shared.hub, 'refresh_ready', None)
        return check() if callable(check) else True

    def invoke(self, method, args, **kwargs):
        # Never sit forever behind a room operation during shutdown.
        limit = self.shared.remaining_shutdown() if self.shared.stopping.is_set() else 15.0
        if not self.shared.lock.acquire(timeout=limit):
            raise HubOperationDeferred("shared referee operation lock timed out")
        try:
            if not args or int(args[0]) not in self.owned:
                raise ValueError(f"{self.alias} cannot invoke {method} on another room")
            rid = int(args[0])
            if self.shared.stopping.is_set():
                if method not in ("JoinRoom", "CloseRoom"):
                    raise HubDisconnected("supervisor is stopping; mutation cancelled")
                remaining = self.shared.remaining_shutdown()
                if remaining <= 0:
                    raise HubDisconnected("shutdown deadline expired")
                kwargs["timeout"] = min(float(kwargs.get("timeout", 5)), remaining)
            if not self.shared.hub.is_connected():
                raise HubOperationDeferred("shared referee transport is disconnected")
            progress = self.roster_progress()
            if method != "JoinRoom" and (self.roster_fenced or (progress is not None and progress[2])):
                raise HubOperationDeferred("room roster requires a fresh authoritative JoinRoom")
            if method == "JoinRoom":
                cached = self.joined.get(rid)
                # Stale queued lifecycle events must not invoke JoinRoom twice
                # on a healthy membership in the same connection generation.
                if cached and cached[0] == self._generation() and not self.roster_fenced and (
                        progress is None or not progress[2]):
                    return cached[1]
                self.joined.pop(rid, None)
            elif method == "CloseRoom":
                if rid in self.shared.close_attempted:
                    raise HubDisconnected("CloseRoom outcome unknown; manual verification required")
                cached = self.joined.get(rid)
                if not cached or cached[0] != self._generation():
                    self.invoke("JoinRoom", [rid], **kwargs)
                # Once submitted, even a timeout must not lead to duplicate
                # close attempts in worker and supervisor cleanup.
                self.shared.close_attempted.add(rid)
            else:
                cached = self.joined.get(rid)
                if not cached or cached[0] != self._generation():
                    raise HubDisconnected("room must rejoin the new referee connection before mutation")
            generation = self._generation()
            result = self.shared.hub.invoke(method, args, **kwargs)
            if method == "JoinRoom":
                after = self.roster_progress()
                if progress is not None and (progress[2] or after[2] or progress[:2] != after[:2]):
                    self.fence_roster(after)
                    self.events.put(("HubDisconnected", {}))
                    raise HubDisconnected("JoinRoom overlapped undelivered roster events")
                self.joined[rid] = (generation, result)
                self.roster_fenced = False
            elif method == "CloseRoom":
                self.owned.discard(rid)
                self.shared.close_attempted.discard(rid)
                self.joined.pop(rid, None)
                self.shared.routes.pop(rid, None)
            return result
        finally:
            self.shared.lock.release()

    def join_room(self, rid):
        return self.invoke("JoinRoom", [int(rid)])

    def close_room(self, rid):
        if self.shared.stopping.is_set() and not self.is_connected():
            self.wait_connected(min(5, self.shared.remaining_shutdown()))
        return self.invoke("CloseRoom", [int(rid)])

    def forget_missing_room(self, rid):
        """Only after LobbyBot's repeated authoritative room-missing checks."""
        with self.shared.lock:
            self.owned.discard(int(rid))
            self.joined.pop(int(rid), None)
            self.shared.routes.pop(int(rid), None)

    def invalidate_membership(self, rid):
        with self.shared.lock:
            self.joined.pop(int(rid), None)

    def __getattr__(self, name):
        # Reuse the existing typed wrappers with this proxy as `self`, so their
        # final invoke() is scoped/serialised above, never bypassing the guard.
        from .hub import RefereeHub
        method = getattr(RefereeHub, name, None)
        if name.startswith("_") or not callable(method):
            raise AttributeError(name)
        return method.__get__(self, type(self))


class SharedHeadlessApi(OsuApi):
    def __init__(self, tokens, **kwargs):
        super().__init__(tokens, **kwargs)
        self._chat_lock = threading.Lock()
        self._ack_lock = threading.Lock()
        self._last_chat_at = 0.0
        self._last_ack_at = 0.0
        self._request_lock = threading.RLock()
        self._blocked_until = 0.0
        self._last_request_at = 0.0
        self._status_by_path = {}
        self.cancel_pending = threading.Event()

    def _pause(self, seconds):
        return not self.cancel_pending.wait(max(0.0, seconds))

    def _req(self, method, path, retries=3, **kw):
        # Only definite rate-limit/authentication rejections are retried. Keep at most one
        # pending call per room thread, not an unbounded background queue.
        with self._request_lock:
            auth_retried = False
            for _ in range(max(1, retries)):
                self._status_by_path.pop((method, path), None)
                result = self._request_once(method, path, **kw)
                if self._status_by_path.get((method, path)) == 401 and not auth_retried:
                    self.tokens.invalidate_access_token(self._last_request_token)
                    auth_retried = True
                    continue
                if self._status_by_path.get((method, path)) != 429:
                    return result
            return None

    def _request_once(self, method, path, **kw):
        """Exact status logging, no GUI callbacks, conservative account pacing.

        POST outcomes are never retried after ambiguous network/5xx failures.
        A 429 establishes a shared, cancellable cooldown for all rooms.
        """
        import httpx
        from .api import API
        kw.pop("_before_request", None)
        authority = kw.pop("_authority_check", None)
        with self._request_lock:
            now = time.monotonic()
            if not self._pause(self._blocked_until - now):
                return None
            # The shared RateLimiter owns both the sustained rate and burst
            # allowance. Its values can be hot-reloaded from universal.json.
            if not self.limiter.wait(cancel_event=self.cancel_pending):
                return None
            if self.cancel_pending.is_set():
                return None
            self._last_request_at = time.monotonic()
            try:
                token = self.tokens.access_token()
            except Exception as exc:
                # An authentication outage must not terminate room workers or
                # be mistaken for confirmation that their rooms disappeared.
                self._blocked_until = time.monotonic() + 30
                log.warning("OAuth unavailable (%s); deferring account requests", type(exc).__name__)
                return None
            try:
                if authority is not None and not authority():
                    return None
                self._last_request_token = token
                response = self.http.request(method, API + path, headers={
                    "Authorization": f"Bearer {token}",
                    "x-api-version": "20220705", "Accept": "application/json",
                    "User-Agent": "poolrotater-headless/1.0",
                }, **kw)
            except httpx.TransportError as exc:
                self.limiter.record_request(None)
                log.warning("%s %s network failure (%s); not blind-retrying", method, path, type(exc).__name__)
                return None
            status = response.status_code
            self.limiter.record_request(status)
            key = (method, path)
            previous = self._status_by_path.get(key)
            self._status_by_path[key] = status
            if len(self._status_by_path) > 512:
                self._status_by_path.pop(next(iter(self._status_by_path)))
            if status >= 400 or previous != status:
                log.log(logging.WARNING if status >= 400 else logging.INFO,
                        "%s %s -> HTTP %s", method, path, status)
            if status == 429:
                from email.utils import parsedate_to_datetime
                raw = response.headers.get("Retry-After", "60")
                try:
                    delay = float(raw)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(raw).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        delay = 60.0
                if not math.isfinite(delay):
                    delay = 60.0
                self._blocked_until = time.monotonic() + max(1, delay)
                log.warning("account HTTP cooldown for %.1fs", max(1, delay))
                return None
            if not 200 <= status < 300:
                return None
            if status == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                log.warning("%s %s returned invalid JSON", method, path)
                return None

    def chat_join(self, channel_id, user_id):
        # MakeRoom/JoinRoom establish membership. Generic PUT channel-join is
        # rejected for lazer multiplayer channels; only check access here.
        for attempt in range(3):
            result = self._req("GET", f"/chat/channels/{int(channel_id)}")
            if result is not None:
                return result
            if attempt < 2 and not self._pause(2):
                break
        log.error("chat access unavailable for channel %s after bounded checks; keep lazer closed", channel_id)
        return None

    def chat_ack(self):
        with self._ack_lock:
            if time.monotonic() - self._last_ack_at < 30:
                return {}
            result = self._req("POST", "/chat/ack")
            if result is not None:
                self._last_ack_at = time.monotonic()
            return result

    def chat_send(self, channel_id, message, *, before_request=None, _authority_check=None):
        with self._chat_lock:
            if not self._pause(self._last_chat_at + 1.5 - time.monotonic()):
                return None
            result = self._req("POST", f"/chat/channels/{int(channel_id)}/messages",
                               json={"message": message, "is_action": False},
                               _authority_check=_authority_check)
            self._last_chat_at = time.monotonic()
            return result

    def chat_messages(self, channel_id, since=None, *, before_request=None, _authority_check=None):
        params = {"limit": 50}
        if since:
            params["since"] = since
        return self._req("GET", f"/chat/channels/{int(channel_id)}/messages", params=params,
                         _authority_check=_authority_check)

    def room_probe(self, rid):
        # The production helper bypasses _req. Keep probes inside our shared
        # request pacing too; never turn a permission/network failure into a
        # confirmed missing room.
        path = f"/rooms/{int(rid)}"
        with self._request_lock:
            result = self._req("GET", path)
            status = self._status_by_path.get(("GET", path))
        return ("ok", result) if result is not None else (
            ("missing", None) if status == 404 else ("unknown", None))


class DiscordRoomView(DiscordControl):
    def __init__(self, cfg, transport):
        super().__init__(cfg, start=False)
        self.transport = transport

    def _is_supervisor_command(self,content):
        if self.cfg.get('managed_preset') and self._command_name(content)=='close':return True
        return super()._is_supervisor_command(content)

    def send_to_channel(self, content, channel_id=None, **kwargs):
        relay = getattr(self.transport, "remote_relay", None)
        if relay and channel_id and int(channel_id) in relay.channels(self._instance_name()):
            return relay.reply(self._instance_name(), int(channel_id), content)
        return self.transport.send_to_channel(content, channel_id or self._channel_id(), **kwargs)

    def react_to_map(self, emoji, map_key, channel_id=None):
        return self.transport.react_to_map(emoji, map_key, channel_id or self._channel_id())

    def _public_status_channel_name(self):
        from poolrotater_host.discord_relay.owner_panel import identity
        name = identity(self.cfg)
        return name[0] if name else super()._public_status_channel_name()

    def _public_status_display_name(self):
        from poolrotater_host.discord_relay.owner_panel import identity
        name = identity(self.cfg)
        return name[1] if name else super()._public_status_display_name()

    def update_public_status(self, **kwargs):
        from poolrotater_host.discord_relay.owner_panel import decorate,identity,presentation
        # The supervisor view may outlive a target-room recreation. Keep its
        # channel/permissions fixed, while naming the active worker's mode.
        current=presentation(self.cfg,kwargs.pop('_runtime_settings',None))
        name=identity(current)
        kwargs = decorate(current, kwargs)
        kwargs.update(
            channel_id=self._public_status_channel_id(),
            channel_base_name=name[0] if name else self._public_status_channel_name(),
            display_name=name[1] if name else self._public_status_display_name(),
            instance=self._instance_name(),
        )
        return self.transport.update_public_status(**kwargs)

    def enqueue_command(self, command):
        # Room lifecycle commands that must work while LobbyBot is stopped are
        # consumed by the supervisor instead of this room's command queue.
        with self.transport._view_lock:
            if self._is_supervisor_command(command.content):
                return self.transport.enqueue_supervisor_command(
                    self._instance_name(), command
                )
            if self._stop.is_set():
                return False
            return super().enqueue_command(command)

    def flush(self, timeout=1.5):
        # The shared transport remains alive until every room has shut down,
        # so this room must not wait on unrelated rooms' queued messages.
        return True

    def stop(self, timeout=3):
        self._stop.set()


class SharedDiscord(DiscordControl):
    def __init__(self, configs):
        # All channel mappings and permissions are validated before starting.
        super().__init__(configs[0], start=False)
        self.views = [DiscordRoomView(cfg, self) for cfg in configs]
        self.supervisor_commands = queue.Queue(maxsize=100)
        self._view_lock = threading.RLock()
        self._view_generations = {}

    def view_for(self, alias):
        normalized = str(alias or "").strip().casefold()
        for view in self.views:
            if view._instance_name() == normalized:
                return view
        raise KeyError(f"unknown Discord room alias: {alias}")

    def reopen_view(self, alias):
        """Replace a stopped room view with a fresh command queue/state."""
        normalized = str(alias or "").strip().casefold()
        with self._view_lock:
            for index, old in enumerate(self.views):
                if old._instance_name() != normalized:
                    continue
                fresh = DiscordRoomView(old.cfg, self)
                self.views[index] = fresh
                self._view_generations.pop(normalized, None)
                return fresh
        raise KeyError(f"unknown Discord room alias: {alias}")

    @staticmethod
    def _discard_room_commands(view):
        # Lifecycle requests have a separate shared supervisor queue. Never
        # drain that queue or another room's commands when retiring a worker.
        while True:
            try: view.commands.get_nowait()
            except queue.Empty: return

    def activate_view(self, alias, generation):
        """Bind one rolling generation without replacing the shared transport."""
        if not isinstance(generation, str) or not generation or len(generation) > 128:
            raise ValueError('explicit Discord worker generation required')
        normalized = str(alias or '').strip().casefold()
        with self._view_lock:
            view = self.view_for(normalized)
            if self._view_generations.get(normalized) != generation:
                self._discard_room_commands(view)
                if view._stop.is_set():
                    fresh = DiscordRoomView(view.cfg, self)
                    self.views[self.views.index(view)] = fresh
                    view = fresh
                self._view_generations[normalized] = generation
                view._stop.clear()
            return view

    def retire_view(self, alias, generation, view):
        """A late old-generation cleanup cannot disable its replacement."""
        normalized = str(alias or '').strip().casefold()
        with self._view_lock:
            if (self._view_generations.get(normalized) != generation
                    or self.view_for(normalized) is not view):
                return False
            view.stop()
            self._discard_room_commands(view)
            self._view_generations.pop(normalized)
            return True

    def enqueue_supervisor_command(self, alias, command):
        self.supervisor_commands.put_nowait((str(alias).casefold(), command))

    def get_supervisor_command_nowait(self):
        return self.supervisor_commands.get_nowait()

    def command_targets(self, content, channel_id):
        # The independent hub bridge owns staff fleet administration even when
        # this supervisor is offline. Do not also relay it as a player command.
        parts=content.split(maxsplit=1)
        if parts and parts[0].casefold()=='.fleet':return []
        results = []
        with self._view_lock:
            for view in self.views:
                if not view.cfg.get("discord_control_enabled", True):
                    continue
                routed = view._route_command(content, channel_id=channel_id)
                if routed is not None:
                    # A parked room intentionally stops its LobbyBot Discord
                    # view. Lifecycle commands still reach the supervisor.
                    if view._stop.is_set() and not view._is_supervisor_command(routed):
                        continue
                    results.append((view, routed))
        return results
