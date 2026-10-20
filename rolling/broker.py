"""Account-owner boundary and authoritative event mirror for a single worker."""
from __future__ import annotations

from dataclasses import asdict
import logging
import queue
import threading
import time

from bot.hub import HubDisconnected, HubInvocationUncertain
from .adapters import Api

log=logging.getLogger(__name__)

HUB_METHODS = frozenset(("JoinRoom", "CloseRoom", "ChangeRoomSettings", "EditCurrentPlaylistItem",
    "AddPlaylistItem", "RemovePlaylistItem", "StartMatch", "StopMatchCountdown", "AbortMatch", "InvitePlayer", "KickPlayer"))


class EventSink:
    """Updated synchronously inside SharedReferee's lock before queuing delivery."""
    def __init__(self, broker):
        self.broker = broker

    def put(self, item):
        self.broker.observe(*item)
        self.broker.enqueue("hub", item)


class InfrastructureCloseDeferred(HubDisconnected):
    """Roster/occupancy refused closure before any journal intent or dispatch."""


class Broker:
    def __init__(self, owner, alias, generation, cfg):
        self.private_pilot = getattr(owner, 'private_pilot', None)
        if self.private_pilot is not None and alias != self.private_pilot.alias:
            raise HubDisconnected('private pilot excludes this room alias')
        self.owner, self.alias, self.generation, self.cfg = owner, alias, generation, cfg
        self.peer = None
        self.active = False
        self.fenced = False
        self.quiesced = False
        self.private = True
        self.pending_public = False
        self.uncertain = False
        self.known = False
        self.players = set()
        self.epoch = 0
        self.channel = None
        self.snapshot_generation = None
        self.password = None
        self.outbox = queue.Queue(maxsize=1024)
        self.chat_inbox = queue.Queue(maxsize=1024)
        self.hub = owner.shared.room_view(alias, EventSink(self))
        self.hub.creation_observer = self.creation_observed
        self.hub.creation_begin = self.creation_begin
        self.hub.creation_committed = self.creation_committed
        self._creation_candidate = None
        self.discord = owner.discord.view_for(alias)
        self.modlog = owner.modlogs[alias]
        self.reporting_visibility(public=False)

    def reporting_visibility(self, public=False, room_id=None):
        # Optional reporting never changes room authority. Persist fail-closed
        # source context before recording a confirmed visibility transition.
        root=getattr(self.owner,'runtime',None)
        if root is None or not self.cfg.get('_host_root'):return
        try:
            from poolrotater_host.reporting_logging import witness,record_visibility
            from pathlib import Path
            from poolrotater_host.layout import atomic_json
            witness(root,self.alias,self.generation,room_id=room_id,public=public)
            if room_id is not None:
                mode='catch' if int(self.cfg.get('ruleset_id',0))==2 else 'standard' if int(self.cfg.get('ruleset_id',0))==0 else 'mania7' if int(self.cfg.get('mania_keys',4))==7 else 'mania4'
                record_visibility(Path(root)/self.cfg['ratings_db'],room_id=room_id,public=public,mode=mode,alias=self.alias)
            (Path(root)/'state'/('reporting-visibility-invalid-'+self.alias+'.json')).unlink(missing_ok=True)
        except Exception:
            try:
                from pathlib import Path
                from poolrotater_host.layout import atomic_json
                atomic_json(Path(root)/'state'/('reporting-visibility-invalid-'+self.alias+'.json'),{'format':1,'invalid':True})
            except Exception:pass
            import logging
            logging.getLogger(__name__).warning('reporting visibility unavailable',exc_info=True)

    def enqueue(self, name, value):
        if self.private_pilot is not None:
            from poolrotater_host.private_pilot import scrub
            value = scrub(value)
        try:
            self.outbox.put_nowait((name, value))
        except queue.Full:
            self.known = False
            self.fenced = True
            if self.peer:
                self.peer.close()

    def snapshot(self, result, *, private_creation=False):
        if self.private_pilot is not None:
            import re
            password = result.get('password')
            if (type(password) is not str or not re.fullmatch('[A-Za-z0-9_-]{8,128}', password)
                    or not private_creation and self.password != password):
                self.known = False
                self.uncertain = True
                raise HubInvocationUncertain('private pilot password state requires review')
        # Missing fields are unknown, NEVER evidence of an empty room.
        players = result.get("players")
        self.known = isinstance(players, list) and all(isinstance(p, dict) and type(p.get("user_id")) is int for p in players)
        self.players = {p["user_id"] for p in players} if self.known else set()
        self.channel = int(result["chat_channel_id"])
        self.password = result.get("password")
        self.snapshot_generation = self.hub._generation()
        self.epoch += 1
        rid=result.get("room_id")
        self.reporting_visibility(public=self.password=="" and not self.private and type(rid) is int,room_id=rid if type(rid) is int else None)

    def observe(self, name, payload):
        self.epoch += 1
        if name in ("HubDisconnected", "HubReconnected"):
            generation = payload.get("generation") if type(payload) is dict else None
            current = self.hub._generation()
            confirmed = (self.known and self.hub.is_connected()
                         and self.snapshot_generation == current)
            tagged = type(generation) is int and generation > 0
            # Delivery can lag behind an authoritative JoinRoom on the new
            # connection. Preserve that snapshot only for a provably retired
            # disconnect or its current reconnect; never recover from cache.
            stale = tagged and ((name == "HubDisconnected" and generation < current)
                                or (name == "HubReconnected" and generation == current))
            if not (confirmed and stale):
                self.known = False
        elif name in ("UserJoined", "UserLeft", "UserKicked"):
            uid = payload.get("kicked_user_id") if name == "UserKicked" else payload.get("user_id")
            if type(uid) is not int:
                self.known = False
            elif name == "UserJoined":
                self.players.add(uid)
            else:
                self.players.discard(uid)
        elif name == "RoomSettingsChanged" and isinstance(payload, dict):
            # Password updates are tracked by confirmed calls below. External
            # changes invalidate the empty-room dwell time via epoch.
            pass

    def empty(self):
        progress = self.hub.roster_progress()
        return (self.known and not self.players and not self.uncertain
                and self.hub.is_connected() and self.snapshot_generation == self.hub._generation()
                and (progress is None or not progress[2]))

    def journal(self, **values):
        self.owner.record(self.alias, **values)

    def creation_observed(self, room_id, connection_generation):
        """Persist unverified creation identity, never adopt ownership from it."""
        if (not self.active or self.fenced or not self.uncertain or self.hub.owned
                or type(room_id) is not int or not 0<room_id<10**19
                or type(connection_generation) is not int or not 0<connection_generation<2**63):
            raise HubInvocationUncertain('referee_creation_record_requires_review')
        value={'format':1,'room_id':room_id,'connection_generation':connection_generation,
               'observed_at':time.time()}
        self.journal(creation_candidate=value)
        self._creation_candidate=value
        log.warning('room creation candidate recorded before owner verification: alias=%s room_id=%d',self.alias,room_id)

    def create(self, *args, **kwargs):
        with self.owner.shared.lock:
            if not self.active or self.fenced or self.quiesced:
                raise HubDisconnected("worker lost mutation authority while waiting for the hub")
            if self.hub.owned or self.uncertain:
                raise HubInvocationUncertain("previous room ownership unresolved")
            chosen = None
            if self.private_pilot is not None:
                from poolrotater_host.private_pilot import desired_password
                chosen = desired_password(self.owner.runtime, self.private_pilot)
            if '_on_created' in kwargs:raise ValueError('reserved room creation argument')
        # The referee mutation/candidate write still run under RoomHub's lock.
        # Its independent HTTP confirmation must not hold the account-wide
        # routing lock through a persistent admission cooldown.
        try:result = self.hub.make_room(*args, **kwargs)
        except Exception as error:
            with self.owner.shared.lock:
                if self._creation_candidate is not None:
                    from poolrotater_host.service_diagnostics import failure_details
                    self.journal(creation_verification_failure=failure_details(error))
            raise
        with self.owner.shared.lock:
            if not self.active or self.fenced or self.quiesced:
                raise HubInvocationUncertain('worker lost authority during room verification')
            if self.private_pilot is not None:
                from poolrotater_host.private_pilot import capture_access, scrub
                if chosen is not None:
                    # The server first creates its ordinary private GUID room.
                    # Retain the known ID and never replay an uncertain setting.
                    capture_access(self.owner.runtime, self.private_pilot, room_id=int(result['room_id']), channel_id=self.channel, password=self.password)
                    self.uncertain = True
                    # Password outcome is uncertain; positive room ownership
                    # remains known and uses ordinary stopped recovery.
                    self.hub.change_room_settings(int(result['room_id']), password=chosen)
                    self.password = chosen
                    # RoomHub caches this exact authoritative creation object
                    # for same-generation JoinRoom; update only after ACK.
                    result["password"] = chosen
                capture_access(self.owner.runtime, self.private_pilot, room_id=int(result['room_id']), channel_id=self.channel, password=self.password)
                if chosen is not None:
                    self.journal(operation='owned', ownership_unknown=False)
                    self.uncertain = False
                return scrub(result)
            return result

    def creation_begin(self):
        """Recheck authority and write intent immediately before MakeRoom."""
        if not self.active or self.fenced or self.quiesced:
            raise HubDisconnected('worker lost authority before room creation')
        if self.hub.owned or self.uncertain:
            raise HubInvocationUncertain('previous room ownership unresolved')
        self.journal(operation='make_pending', ownership_unknown=True,
                     creation_candidate=None, creation_verification_failure=None)
        self.uncertain = True
        self._creation_candidate = None

    def creation_committed(self, result):
        """RoomHub holds the routing lock through snapshot/journal adoption."""
        if not self.active or self.fenced or self.quiesced:
            raise HubInvocationUncertain('worker lost authority during room verification')
        self.snapshot(result, private_creation=True)
        self.private = True
        self.pending_public = False
        self.journal(operation='owned', room_id=int(result['room_id']),
                     channel_id=self.channel, ownership_unknown=False,
                     creation_candidate=None, creation_verification_failure=None)
        self.uncertain = False
        self._creation_candidate = None

    def close_room(self, rid, *, require_empty=True):
        with self.owner.shared.lock:
            progress = self.hub.roster_progress()
            if self.hub.roster_fenced or (progress is not None and progress[2]):
                raise InfrastructureCloseDeferred("refusing close while roster ordering is unresolved")
            if require_empty and not self.empty():
                raise InfrastructureCloseDeferred("refusing infrastructure close: room is occupied or roster is unknown")
            self.journal(operation="close_pending", ownership_unknown=True)
            self.uncertain = True
            result = self.hub.close_room(rid)
            self.journal(operation="closed", room_id=None, channel_id=None, ownership_unknown=False)
            self.uncertain = False
            self.known = False
            self.reporting_visibility(public=False,room_id=rid)
            self.owner.room_closed(self.alias, rid)
            return result

    def publish(self):
        with self.owner.shared.lock:
            if self.private_pilot is not None:
                self.pending_public = False
                return
            if self.pending_public and self.hub.owned:
                progress = self.hub.roster_progress()
                if (progress is not None and self.owner.shared.hub.is_connected()
                        and (self.hub.roster_fenced or progress[2])):
                    return
                rid, = self.hub.owned
                try:
                    self.hub.change_room_settings(rid, password="")
                except HubDisconnected:
                    progress = self.hub.roster_progress()
                    if (progress is not None and self.owner.shared.hub.is_connected()
                            and (self.hub.roster_fenced or progress[2])):
                        return
                    raise
                self.password = ""
                self.private = False
                self.pending_public = False
                self.reporting_visibility(public=True,room_id=rid)

    def handle(self, method, *args, **kwargs):
        if method == "worker.hello":
            return self.owner.hello(self, *args)
        if not self.active or self.fenced:
            raise HubDisconnected("worker generation is inactive or fenced")
        if method == "siblings.autodiff":
            return self.owner.autodiff(self.alias, args[0])
        if method.startswith("hub."):
            name = method[4:]
            if name == "is_connected":
                return self.hub.is_connected()
            if name == "wait_connected":
                return self.hub.wait_connected(min(30, float(args[0])))
            if name == "refresh_ready":
                return self.hub.refresh_ready()
            if name == "make_room":
                if self.quiesced:
                    raise HubDisconnected("room is quiesced")
                return self.create(*args, **kwargs)
            if name in ("invalidate_membership", "forget_missing_room"):
                if int(args[0]) not in self.hub.owned:
                    raise ValueError("room ownership mismatch")
                if name == "forget_missing_room":
                    # Independently verify the worker's missing-room report.
                    probe = self.owner.api.room_probe(int(args[0]))
                    if not isinstance(probe, (list, tuple)) or probe[0] != "missing":
                        raise HubDisconnected("room absence not independently confirmed")
                    self.journal(operation="closed", room_id=None, ownership_unknown=False)
                self.known = False
                return getattr(self.hub, name)(*args)
            if name != "invoke" or not args or args[0] not in HUB_METHODS:
                raise ValueError("unsupported referee operation")
            operation, values = args
            with self.owner.shared.lock:
                if not self.active or self.fenced:
                    raise HubDisconnected("worker lost mutation authority while waiting for the hub")
                if not values or int(values[0]) not in self.hub.owned:
                    raise ValueError("room ownership mismatch")
                if self.quiesced:
                    raise HubDisconnected("room is quiesced")
                if operation == "CloseRoom":
                    # Explicit room commands retain their existing authority.
                    # Infrastructure/deployment callers use the guarded default.
                    return self.close_room(int(values[0]), require_empty=False)
                if operation == 'ChangeRoomSettings' and self.private_pilot is not None and 'password' in values[1]:
                    values = [values[0], {k:v for k,v in values[1].items() if k != 'password'}]
                    if not values[1]:return None
                if operation == "ChangeRoomSettings" and self.private and values[1].get("password") == "":
                    self.pending_public = self.private_pilot is None
                    values = [values[0], {k: v for k, v in values[1].items() if k != "password"}]
                    if not values[1]:
                        return None
                progress = self.hub.roster_progress() if operation == "JoinRoom" else None
                if operation == "JoinRoom" and progress is not None and (
                        not self.known or self.hub.roster_fenced or progress[2]):
                    self.hub.invalidate_membership(int(values[0]))
                cached = self.hub.joined.get(int(values[0]))
                fresh_join = operation == "JoinRoom" and (not cached or cached[0] != self.hub._generation())
                before = self.hub.roster_progress() if fresh_join else None
                result = self.hub.invoke(operation, values, **kwargs)
                if fresh_join:
                    self.snapshot(result)
                    after = self.hub.roster_progress()
                    if before is not None and (before[2] or after[2] or before[:2] != after[:2]):
                        self.known = False
                        self.hub.fence_roster(after)
                        self.enqueue("hub", ("HubDisconnected", {}))
                        raise HubDisconnected("JoinRoom overlapped undelivered roster events")
                    self.hub.roster_fenced = False
                    # Local ordered barrier, never an upstream event. Earlier
                    # queued deltas precede this fresh JoinRoom snapshot; later
                    # deltas follow it under the same shared routing lock.
                    # Cached JoinRoom replies MUST NOT emit a stale baseline.
                    self.enqueue("hub", ("RoomRosterSnapshot", {
                        "room_id": result["room_id"],
                        "chat_channel_id": result["chat_channel_id"],
                        "players": result.get("players"),
                        "referees": result.get("referees"),
                        "_worker_roster_barrier": self.epoch,
                    }))
                    # The RPC reply can overtake the outbox delivery thread.
                    # Tell a new worker which ordered barrier to await before
                    # completing recovery. Keep the cached provider reply clean.
                    result = dict(result, _worker_roster_barrier=self.epoch)
                if operation == "ChangeRoomSettings" and "password" in values[1]:
                    self.password = values[1]["password"]
                    self.reporting_visibility(public=self.password=="" and not self.private,room_id=int(values[0]))
                if self.private_pilot is not None:
                    from poolrotater_host.private_pilot import scrub
                    return scrub(result)
                return result
        if method.startswith("api.") and method[4:] in Api.METHODS:
            name = method[4:]
            if name in ("chat_join", "chat_send", "chat_messages"):
                if not args or int(args[0]) != self.channel or not self.hub.owned:
                    raise ValueError("chat channel ownership mismatch")
                if name != "chat_join" or getattr(self.owner.api, "chat_access_guard", False) is True:
                    channel = self.channel
                    kwargs["_authority_check"] = lambda: self.active and not self.fenced and self.channel == channel and bool(self.hub.owned)
            if name in ("room", "room_probe", "room_item_scores") and int(args[0]) not in self.hub.owned:
                raise ValueError("room API ownership mismatch")
            result = getattr(self.owner.api, name)(*args, **kwargs)
            if self.private_pilot is not None:
                from poolrotater_host.private_pilot import scrub
                return scrub(result)
            return result
        if method in ("discord.send_to_channel", "discord.react_to_map", "discord.update_public_status"):
            if self.private_pilot is not None and method == 'discord.update_public_status':
                from poolrotater_host.private_pilot import discord_status_enabled
                if not discord_status_enabled(self.owner.runtime,self.private_pilot):return False
            channel = kwargs.get("channel_id")
            position = 2 if method == "discord.react_to_map" else 1
            if len(args) > position:
                channel = args[position]
            allowed = {self.discord._channel_id(), *self.discord._channel_map().values()}
            if channel and int(channel) not in allowed:
                raise ValueError("Discord channel ownership mismatch")
            if method == "discord.update_public_status" and any(k in kwargs for k in (
                    "channel_base_name", "display_name", "instance")):
                raise ValueError("public status identity belongs to supervisor")
            return getattr(self.discord, method[8:])(*args, **kwargs)
        if method in ("modlog.submit_chat", "modlog.submit_event"):
            return getattr(self.modlog, method[7:])(*args, **kwargs)
        if method == "modlog.submit_content":
            return self.modlog._submit_content(*args)
        if method in ("chat.subscribe", "chat.unsubscribe"):
            if int(args[0]) != self.channel:
                raise ValueError("chat subscription ownership mismatch")
            chat = self.owner.chat_websocket
            if chat is None:
                return [False, False, 0]
            getattr(chat, method[5:])(int(args[0]), self.chat_inbox)
            return [chat.enabled, chat.connected, chat.generation]
        raise ValueError("unsupported broker request")

    def pump(self):
        """One pump per worker; a slow worker cannot hold the shared hub lock."""
        while self.peer and not self.peer.closed.is_set():
            try:
                name, value = self.outbox.get(timeout=.1)
                self.peer.emit(name, value)
                continue
            except queue.Empty:
                pass
            try:
                self.peer.emit("chat", asdict(self.chat_inbox.get_nowait()))
            except queue.Empty:
                pass
            try:
                if self.active and not self.quiesced and not self.fenced:
                    self.peer.emit("discord", asdict(self.discord.get_command_nowait()))
            except queue.Empty:
                pass

    def activate(self):
        if self.fenced:
            raise HubDisconnected('retired worker cannot reactivate')
        self.discord = self.owner.discord.activate_view(self.alias, self.generation)
        self.active = True

    def retire(self):
        self.active = False
        self.fenced = True
        self.owner.discord.retire_view(self.alias, self.generation, self.discord)
        if self.owner.chat_websocket and self.channel:
            self.owner.chat_websocket.unsubscribe(self.channel, self.chat_inbox)
        with self.owner.shared.lock:
            if not self.hub.owned and self.hub in self.owner.shared.views:
                self.owner.shared.views.remove(self.hub)
