"""Room-side transport views. Never open an authenticated connection here."""
from __future__ import annotations

import queue
import logging
from types import SimpleNamespace

from bot.chat_websocket import ChatSocketEvent
from bot.discord_control import DiscordControl, DiscordCommand
from bot.discord_modlog import DiscordModLog
from bot.hub import RefereeHub, HubDisconnected, HubInvocationUncertain
from .protocol import RemoteError, PeerClosed, UncertainResult


def call(peer, method, *args, **kwargs):
    try:
        return peer.call(method, *args, **kwargs)
    except RemoteError as exc:
        if exc.kind in ("HubDisconnected", "HubTransportError"):
            raise HubDisconnected(str(exc)) from exc
        if exc.kind == "HubInvocationUncertain":
            raise HubInvocationUncertain(str(exc)) from exc
        raise
    except (PeerClosed, UncertainResult) as exc:
        # Loss of a reply is not proof a command did not happen.
        raise HubInvocationUncertain(str(exc)) from exc


class Hub:
    def __init__(self, peer):
        self.peer = peer

    def invoke(self, method, args, **kwargs):
        # Keep the public playlist audit in the room's own log for existing
        # website/operations consumers. Never duplicate passwords or rosters.
        public = method in {"AddPlaylistItem", "EditCurrentPlaylistItem", "RemovePlaylistItem", "StartMatch", "CloseRoom"}
        log = logging.getLogger("hub")
        if public:
            log.debug("-> %s %s", method, args)
        result = call(self.peer, "hub.invoke", method, args, **kwargs)
        if public:
            log.debug("<- %s confirmed", method)
        return result

    def make_room(self, *args, **kwargs):
        # Keep the room-local creation/version marker used by existing ops
        # readers. The actual hub invocation now happens in the supervisor.
        logging.getLogger("hub").debug("-> MakeRoom %s", args[0] if args else kwargs.get("name", ""))
        return call(self.peer, "hub.make_room", *args, **kwargs)

    def is_connected(self):
        return not self.peer.closed.is_set() and call(self.peer, "hub.is_connected")

    def wait_connected(self, timeout=30):
        return call(self.peer, "hub.wait_connected", timeout)

    def invalidate_membership(self, rid):
        return call(self.peer, "hub.invalidate_membership", rid)

    def forget_missing_room(self, rid):
        return call(self.peer, "hub.forget_missing_room", rid)

    def stop(self):
        pass

    def __getattr__(self, name):
        wrapper = getattr(RefereeHub, name, None)
        if name.startswith("_") or name in ("connect", "stop") or not callable(wrapper):
            raise AttributeError(name)
        return wrapper.__get__(self, type(self))


class Api:
    METHODS = frozenset(("beatmap", "beatmapset", "chat_ack", "chat_join", "chat_messages",
        "chat_send", "room", "room_item_scores", "room_probe", "user", "user_top_scores",
        "usage_report", "configure_rate_limit", "configure_profile_cache", "invalidate_profile_cache"))

    def __init__(self, peer):
        self.peer = peer
        self.limiter = SimpleNamespace(usage_log_seconds=60)

    def __getattr__(self, name):
        if name not in self.METHODS:
            raise AttributeError(name)
        def invoke(*args, **kwargs):
            # These callbacks coordinate a desktop lazer client. Headless
            # rooms have no such client and legitimately return False here.
            # The account broker owns channel/room authority and HTTP pacing.
            kwargs.pop("before_request", None)
            try:
                return call(self.peer, "api." + name, *args, **kwargs)
            except RemoteError as exc:
                # Typed gate refusals establish no dispatch. Never infer this
                # from a generic OAuth error string or an uncertain RPC reply.
                if name in ('chat_ack', 'chat_messages') and exc.kind == 'ProviderAdmissionDeferred':
                    from bot.api import ApiAdmissionDeferred
                    raise ApiAdmissionDeferred(str(exc)) from exc
                raise
        return invoke


class Discord(DiscordControl):
    def __init__(self, peer, cfg):
        super().__init__(cfg, start=False)
        self.peer = peer

    def send_to_channel(self, *args, **kwargs):
        return call(self.peer, "discord.send_to_channel", *args, **kwargs)

    def react_to_map(self, *args, **kwargs):
        return call(self.peer, "discord.react_to_map", *args, **kwargs)

    def update_public_status(self, **kwargs):
        return call(self.peer, "discord.update_public_status", **kwargs)

    def flush(self, timeout=1.5):
        return True

    def stop(self, timeout=3):
        self._stop.set()


class ModLog(DiscordModLog):
    def __init__(self, peer, cfg):
        self.peer = peer
        self.cfg = cfg

    def submit_chat(self, **kwargs):
        return call(self.peer, "modlog.submit_chat", **kwargs)

    def submit_event(self, **kwargs):
        return call(self.peer, "modlog.submit_event", **kwargs)

    def stop(self, timeout=2):
        pass

    def _submit_content(self, content):
        return call(self.peer, "modlog.submit_content", content)


class Chat:
    def __init__(self, peer):
        self.peer = peer
        self.inboxes = {}
        self.enabled = True
        self.connected = False
        self.generation = 0
        self.last_error = ""

    def subscribe(self, channel_id, inbox):
        self.inboxes[int(channel_id)] = inbox
        result = call(self.peer, "chat.subscribe", int(channel_id))
        self.enabled, self.connected, self.generation = result

    def unsubscribe(self, channel_id, inbox):
        self.inboxes.pop(int(channel_id), None)
        call(self.peer, "chat.unsubscribe", int(channel_id))

    def event(self, value):
        value["messages"] = tuple(value.get("messages", ()))
        event = ChatSocketEvent(**value)
        self.generation = event.generation
        if event.kind in ("connected", "disconnected"):
            self.connected = event.kind == "connected"
        targets = [self.inboxes.get(event.channel_id)] if event.channel_id else list(self.inboxes.values())
        for inbox in targets:
            if inbox is not None:
                inbox.put_nowait(event)


class Siblings:
    def __init__(self, peer):
        self.peer = peer

    def autodiff_lobby_for(self, requester):
        return call(self.peer, "siblings.autodiff", {
            "ruleset": requester.cfg.get("ruleset_id", 0),
            "loved": bool(requester.cfg.get("loved_mode", False)),
        })
