"""Offline v6.7 tests. Fake osu!/Discord transports; real JSON sockets/processes."""
from __future__ import annotations
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bot.hub import HubDisconnected, HubInvocationUncertain
from bot.multiroom import SharedReferee
from rolling.adapters import ModLog
from rolling.broker import Broker
from rolling.protocol import MAX_FRAME, Peer, PeerClosed, RemoteError, UncertainResult
from rolling.release import atomic_json, build_manifest, load_release, require_compatible, IncompatibleRelease, source_files
from rolling.state import Registry, schemas
from rolling.storage import guarded_connect
from rolling.supervisor import RollingSupervisor
from rolling.worker import Lifecycle
from run_multiroom import load_rooms

ROOT = Path(__file__).resolve().parents[1]


def _mania_fixture_db(keys, role):
    """Use legacy checked-in fixture DBs, or a checksum-validated full export."""
    legacy = ROOT / (f"catalog-data/mania_{keys}k.db" if role == "catalog" else
                     f"farm-data/mania_{keys}k_farm.db")
    if legacy.is_file():
        return legacy

    work_root = ROOT.parents[2]
    export = work_root / "validation" / "loved726" / "inputs" / f"mania{keys}"
    manifest_path = export / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise FileNotFoundError(f"full immutable mania{keys} export manifest is missing")
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("format") != 1 or manifest.get("export_schema") != 1 or
            manifest.get("mode") != f"mania{keys}" or manifest.get("keys") != keys or
            manifest.get("reader") != "poolrotater-selection-v1"):
        raise AssertionError(f"invalid full mania{keys} fixture export manifest")
    filename = "catalog.db" if role == "catalog" else "farm.db"
    entry = next((item for item in manifest.get("files", [])
                  if item.get("name") == filename and item.get("role") == role), None)
    if entry is None:
        raise AssertionError(f"manifest does not identify the full {role} export")
    path = export / filename
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"full immutable mania{keys} {role} database is missing")
    metadata = path.stat()
    if metadata.st_mode & 0o222:
        raise AssertionError(f"mania{keys} {role} fixture must remain immutable/read-only")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if metadata.st_size != entry.get("bytes") or digest != entry.get("sha256"):
        raise AssertionError(f"mania{keys} {role} fixture does not match its full-export manifest")
    return path


def seal(root, version="v6.7.0"):
    manifest = build_manifest(root, version, sys.executable)
    manifest["validation"] = {"at": 0, "tests": 1, "fixture": True}
    atomic_json(root / "release.json", manifest)
    return manifest


class ProtocolTests(unittest.TestCase):
    def peers(self, handler):
        a, b = socket.socketpair()
        left, right = Peer(a), Peer(b, handler=handler)
        self.addCleanup(left.close); self.addCleanup(right.close)
        left.start(); right.start()
        return left, right

    def test_bidirectional_requests_events_and_unicode(self):
        left, right = self.peers(lambda method, text: text.upper())
        self.assertEqual(left.call("upper", "osu! ★"), "OSU! ★")
        left.handler = lambda method: "back"
        right.handler = lambda method: right.call("callback")
        self.assertEqual(left.call("roundtrip"), "back")
        inbox = queue.Queue()
        left.on_event = lambda name, value: inbox.put((name, value))
        right.emit("status", {"version": "v6.7.1"})
        self.assertEqual(inbox.get(timeout=1), ("status", {"version": "v6.7.1"}))

    def test_timeout_is_not_replayed(self):
        calls = []
        def handle(method):
            calls.append(method); time.sleep(.1); return True
        left, _ = self.peers(handle)
        with self.assertRaises(UncertainResult):
            left.call("MakeRoom", timeout=.01)
        time.sleep(.15)
        self.assertEqual(calls, ["MakeRoom"])

    def test_remote_error_and_disconnection(self):
        def handle(method):
            raise HubInvocationUncertain("unknown creation")
        left, right = self.peers(handle)
        with self.assertRaises(RemoteError) as ctx:
            left.call("make")
        self.assertEqual(ctx.exception.kind, "HubInvocationUncertain")
        right.close()
        with self.assertRaises(PeerClosed):
            left.call("make", timeout=1)

    def test_oversized_frames_rejected(self):
        left, _ = self.peers(lambda *_: None)
        with self.assertRaises(ValueError):
            left.emit("big", "x" * MAX_FRAME)


class ManagedCacheConfigurationTests(unittest.TestCase):
    def hello(self, root, *, managed, cache):
        cfg={'_instance_name':'room1','_supervisor_overrides':{},'map_pp_cache_dir':cache,
             '_rolling_controller_release':str(root/'foreign-user-selected-controller')}
        if managed:cfg['_host_root']=str(root)
        supervisor=RollingSupervisor.__new__(RollingSupervisor)
        supervisor.runtime=root;supervisor.release=root/'controller'
        supervisor.configs={'room1':copy.deepcopy(cfg)}
        supervisor.config_loader=lambda:[copy.deepcopy(cfg)]
        supervisor.schema_baseline=None
        manifest={'protocol':1,'version':'v7.0.1','files':{}}
        supervisor.handles={'generation':SimpleNamespace(manifest=manifest,release=root/'release')}
        broker=SimpleNamespace(alias='room1',generation='generation',cfg=copy.deepcopy(cfg))
        value=supervisor.hello(broker,manifest)
        self.assertEqual(value['_rolling_controller_release'],str(supervisor.release))
        return value

    def test_managed_worker_uses_existing_imported_cache_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);cache=root/'state/controller-map-pp/osu'
            cache.mkdir(parents=True);(cache/'123.osu').write_text('invented cached map')
            result=self.hello(root,managed=True,cache=str(cache.parent))
            self.assertEqual(result['map_pp_cache_dir'],str(cache.parent))
            self.assertEqual(result['_supervisor_overrides']['map_pp_cache_dir'],str(cache.parent))
            self.assertEqual((cache/'123.osu').read_text(),'invented cached map')
            self.assertFalse((root/'state/map_pp').exists())

    def test_legacy_worker_keeps_versioned_cache_and_managed_foreign_path_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            result=self.hello(root,managed=False,cache='legacy-cache')
            self.assertEqual(result['map_pp_cache_dir'],str(root/'state/map_pp/v7.0.1'))
            with self.assertRaisesRegex(IncompatibleRelease,'managed PP cache path changed'):
                self.hello(root,managed=True,cache=str(root/'unreviewed-cache'))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ratings.db"
        with sqlite3.connect(self.path) as conn:
            conn.executescript("CREATE TABLE ratings (id INTEGER PRIMARY KEY, vote INTEGER); INSERT INTO ratings VALUES (1,1);")

    def test_existing_schema_and_data_writes_work(self):
        with guarded_connect(self.path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS ratings (id INTEGER PRIMARY KEY, vote INTEGER)")
            conn.execute("UPDATE ratings SET vote=-1 WHERE id=1")
            self.assertEqual(conn.execute("SELECT vote FROM ratings").fetchone()[0], -1)

    def test_destructive_and_additive_migrations_blocked(self):
        for sql in ("DROP TABLE ratings", "ALTER TABLE ratings ADD COLUMN x TEXT",
                    "CREATE TABLE new_schema (id INTEGER)", "PRAGMA user_version=9",
                    "PRAGMA writable_schema=ON", "ATTACH ':memory:' AS other"):
            with self.subTest(sql=sql), guarded_connect(self.path) as conn:
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(sql)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT * FROM ratings").fetchall(), [(1, 1)])

    def test_unknown_database_and_catalog_writes_blocked(self):
        with self.assertRaises(sqlite3.OperationalError):
            guarded_connect(self.path, allowed_paths=set())
        with self.assertRaises(sqlite3.OperationalError):
            guarded_connect(self.path.parent / "new.db")
        with guarded_connect(self.path, readonly_paths={str(self.path)}) as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM ratings")

    def test_schema_fingerprint_ignores_data_and_detects_migration(self):
        cfg = [{"ratings_db": str(self.path)}]
        before = schemas(cfg)
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO ratings VALUES (2, -1)")
        self.assertEqual(schemas(cfg), before)
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE ratings ADD COLUMN reason TEXT")
        self.assertNotEqual(schemas(cfg), before)

    def test_ownership_journal_blocks_crash_recreation(self):
        path = self.path.parent / "ownership.json"
        registry = Registry(path)
        registry.record("room1", operation="make_pending", ownership_unknown=True)
        with self.assertRaises(RuntimeError):
            Registry(path).require_clean_start()
        registry.record("room1", operation="owned", room_id=12, ownership_unknown=False)
        with self.assertRaises(RuntimeError):
            Registry(path).require_clean_start()
        registry.record("room1", operation="closed", room_id=None)
        Registry(path).require_clean_start()

    def test_concurrent_process_config_merges_do_not_lose_keys(self):
        path = self.path.parent / "universal.json"
        path.write_text("{}")
        code = """
import logging, sys
from pathlib import Path
from bot.bot import LobbyBot
logging.disable(logging.CRITICAL)
bot = LobbyBot.__new__(LobbyBot)
bot.cfg = {'_rolling_worker': True}
bot._universal_config_path = Path(sys.argv[1])
for i in range(20):
    assert bot._persist_universal_config_updates({sys.argv[2] + '_' + str(i): i})
"""
        processes = [subprocess.Popen([sys.executable, "-B", "-c", code, str(path), str(i)], cwd=ROOT) for i in range(4)]
        try:
            self.assertEqual([p.wait(timeout=15) for p in processes], [0, 0, 0, 0])
            self.assertEqual(len(json.loads(path.read_text())), 80)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.wait()


class FakeHub:
    def __init__(self):
        self._connect_generation = 1
        self.connected = True
        self.calls = []
        self.rooms = {}
        self.next_id = 100
        self.next_item = 200
        self.shared = None
        self.fail_make = False
        self.fail_close = False
        self.on_lock = None
        self.event_sequence = 0
        self.roster_revisions = {}
        self.pending_roster = {}

    def is_connected(self): return self.connected
    def wait_connected(self, timeout=30): return self.connected
    def connect(self): self.connected = True
    def stop(self): self.connected = False

    def provider_event(self, name, payload, generation=None):
        from poolrotater_host.provider_referee import _ProviderEvent
        generation = self._connect_generation if generation is None else generation
        self.event_sequence += 1
        item = _ProviderEvent(name, payload, generation, self.event_sequence)
        if name in ("UserJoined", "UserLeft", "UserKicked"):
            self.roster_revisions[generation] = self.roster_revisions.get(generation, 0) + 1
            self.pending_roster[item.sequence] = item
        return item

    def roster_progress(self):
        generation = self._connect_generation
        return (generation, self.roster_revisions.get(generation, 0),
                any(item.generation == generation for item in self.pending_roster.values()),
                self.event_sequence)

    def acknowledge_event(self, item):
        if self.pending_roster.get(item.sequence) is item:
            self.pending_roster.pop(item.sequence)

    def item(self, bid, mode=3):
        self.next_item += 1
        return {"id": self.next_item, "beatmap_id": bid, "ruleset_id": mode, "required_mods": [],
                "allowed_mods": [], "freestyle": False, "was_played": False, "order": 0}

    def make_room(self, name, bid, ruleset_id=0):
        self.calls.append(("MakeRoom", [name, bid]))
        if self.fail_make:
            raise HubInvocationUncertain("injected lost MakeRoom reply")
        self.next_id += 1
        row = {"room_id": self.next_id, "chat_channel_id": self.next_id + 1000,
               "players": [], "referees": [{"user_id": 99}], "password": "initial-private",
               "name": name, "playlist": [self.item(bid, ruleset_id)]}
        self.rooms[self.next_id] = row
        return copy.deepcopy(row)

    def invoke(self, method, args, **kwargs):
        self.calls.append((method, copy.deepcopy(args)))
        rid = int(args[0]); row = self.rooms[rid]
        if method == "CloseRoom":
            if self.fail_close:
                raise HubInvocationUncertain("injected lost CloseRoom reply")
            del self.rooms[rid]
        elif method == "JoinRoom":
            return copy.deepcopy(row)
        elif method == "ChangeRoomSettings":
            row.update(args[1])
            if args[1].get("password") and self.on_lock:
                self.on_lock(rid)
        elif method == "EditCurrentPlaylistItem":
            row["playlist"][0].update(args[1])
        elif method == "AddPlaylistItem":
            item = self.item(args[1]["beatmap_id"], args[1]["ruleset_id"])
            item["order"] = len(row["playlist"])
            row["playlist"].append(item)
            if self.shared:
                self.shared.route("PlaylistItemAdded", {"room_id": rid, "playlist_item": item})
        elif method == "RemovePlaylistItem":
            row["playlist"] = [p for p in row["playlist"] if p["id"] != args[1]["playlist_item_id"]]
        return None


class FakeApi:
    def __init__(self, hub):
        self.hub = hub
        self.cancel_pending = threading.Event()
        self.next_message = 0
        self.messages = []
    def beatmap(self, bid): return {"id": bid, "beatmapset_id": bid, "beatmapset": {"availability": {"download_disabled": False}}}
    def beatmapset(self, bid): return {"id": bid, "availability": {"download_disabled": False}}
    def chat_join(self, *a): return {"channel_id": a[0]}
    def chat_send(self, *a, **kw):
        self.next_message += 1
        self.messages.append(a)
        return {"message_id": self.next_message}
    def chat_messages(self, *a, **kw): return []
    def chat_ack(self): return None
    def room(self, rid): return copy.deepcopy(self.hub.rooms.get(rid))
    def room_probe(self, rid): return ("ok", self.room(rid)) if rid in self.hub.rooms else ("missing", None)
    def configure_rate_limit(self, *a): pass
    def configure_profile_cache(self, *a): pass
    def invalidate_profile_cache(self, *a): pass
    def user(self, uid, **kw): return {"id": uid, "username": "test-player", "statistics": {"pp": 100}}
    def user_top_scores(self, *a, **kw): return []


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        shared = SharedReferee(self.hub, queue.Queue())
        discord = Mock(); discord._channel_id.return_value = 12
        discord._channel_map.return_value = {"room1": 12, "room2": 13}
        self.owner = SimpleNamespace(shared=shared, discord=SimpleNamespace(view_for=lambda _: discord),
                                     modlogs={"room1": Mock()}, record=Mock(), room_closed=Mock(),
                                     api=FakeApi(self.hub), chat_websocket=None)
        self.b = Broker(self.owner, "room1", "g1", {})
        self.b.active = True
        self.rid = self.b.handle("hub.make_room", "test", 1)["room_id"]

    def test_cross_room_and_channel_and_private_methods_rejected(self):
        for name, args in (("hub.invoke", ("CloseRoom", [999])), ("api.chat_send", (999, "bad")),
                           ("hub.invoke", ("AddReferee", [self.rid, 1])), ("api._req", ("GET", "/me")),
                           ("discord.send_to_channel", ("bad", 999))):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.b.handle(name, *args)

    def test_room_stays_private_until_published(self):
        self.b.handle("hub.invoke", "ChangeRoomSettings", [self.rid, {"password": ""}])
        self.assertEqual(self.hub.rooms[self.rid]["password"], "initial-private")
        self.b.publish()
        self.assertEqual(self.hub.rooms[self.rid]["password"], "")

    def test_infrastructure_cannot_close_occupied_or_unknown_room(self):
        for known, players in ((True, {7}), (False, set())):
            self.b.known, self.b.players = known, players
            with self.assertRaises(HubDisconnected):
                self.b.close_room(self.rid)
        self.assertFalse(any(m in ("CloseRoom", "KickPlayer") for m, _ in self.hub.calls))
        self.assertIn(self.rid, self.hub.rooms)

    def test_countdown_cancel_reaches_only_owned_room(self):
        self.b.handle("hub.invoke", "StopMatchCountdown", [self.rid])
        self.assertEqual(self.hub.calls[-1], ("StopMatchCountdown", [self.rid]))
        with self.assertRaises(ValueError):
            self.b.handle("hub.invoke", "StopMatchCountdown", [999])

    def test_real_kick_event_removes_player_from_roster(self):
        self.owner.shared.route("UserJoined", {"room_id": self.rid, "user_id": 7})
        self.owner.shared.route("UserKicked", {"room_id": self.rid, "kicked_user_id": 7, "kicking_user_id": 99})
        self.assertTrue(self.b.empty())

    def test_targeted_discord_reply_can_return_to_configured_control_channel(self):
        self.b.handle("discord.send_to_channel", "reply", channel_id=13)
        self.b.discord.send_to_channel.assert_called_once_with("reply", channel_id=13)
        with self.assertRaises(ValueError):
            self.b.handle("discord.send_to_channel", "reply", channel_id=999)

    def test_disconnect_invalidates_empty_and_fresh_join_restores(self):
        self.assertTrue(self.b.empty())
        self.owner.shared.route("HubDisconnected", {})
        self.assertFalse(self.b.empty())
        self.hub._connect_generation += 1
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assertTrue(self.b.empty())

    def test_delayed_lifecycle_after_fresh_join_preserves_current_snapshot(self):
        retired = self.hub._connect_generation
        self.hub._connect_generation += 1
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        current = self.hub._connect_generation
        self.assertTrue(self.b.empty())

        # The receiver makes the new connection usable before the dispatcher
        # necessarily delivers the old disconnect and its new reconnect.
        self.owner.shared.route("HubDisconnected", {"generation": retired})
        self.owner.shared.route("HubReconnected", {"generation": current})
        self.assertTrue(self.b.known)
        self.assertTrue(self.b.empty())
        self.assertEqual(self.b.snapshot_generation, current)
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assertEqual(sum(m == "JoinRoom" for m, _ in self.hub.calls), 1)
        self.b.close_room(self.rid)
        self.assertEqual(sum(m == "MakeRoom" for m, _ in self.hub.calls), 1)
        self.assertEqual(sum(m == "CloseRoom" for m, _ in self.hub.calls), 1)
        self.assertNotIn(self.rid, self.hub.rooms)

    def test_delayed_lifecycle_never_replaces_players_with_cached_empty_roster(self):
        retired = self.hub._connect_generation
        self.hub._connect_generation += 1
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        current = self.hub._connect_generation
        self.assertEqual(self.b.hub.joined[self.rid][1]["players"], [])
        self.owner.shared.route("UserJoined", {"room_id": self.rid, "user_id": 7})
        self.owner.shared.route("HubDisconnected", {"generation": retired})
        self.owner.shared.route("HubReconnected", {"generation": current})
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])

        self.assertTrue(self.b.known)
        self.assertEqual(self.b.players, {7})
        self.assertFalse(self.b.empty())
        self.assertEqual(sum(m == "JoinRoom" for m, _ in self.hub.calls), 1)
        with self.assertRaises(HubDisconnected):
            self.b.close_room(self.rid)
        self.assertIn(self.rid, self.hub.rooms)
        self.assertFalse(any(m == "CloseRoom" for m, _ in self.hub.calls))

    def test_current_and_future_lifecycle_still_invalidate_roster(self):
        current = self.hub._connect_generation
        for name, generation in (("HubDisconnected", current),
                                 ("HubDisconnected", current + 1),
                                 ("HubReconnected", current + 1)):
            with self.subTest(name=name, generation=generation):
                self.b.snapshot(self.hub.rooms[self.rid])
                self.owner.shared.route(name, {"generation": generation})
                self.assertFalse(self.b.known)
                self.assertFalse(self.b.empty())
                with self.assertRaises(HubDisconnected):
                    self.b.close_room(self.rid)
        self.assertFalse(any(m == "CloseRoom" for m, _ in self.hub.calls))

    def test_untagged_and_malformed_lifecycle_still_invalidate_roster(self):
        for name in ("HubDisconnected", "HubReconnected"):
            for payload in ({}, None, {"generation": True}, {"generation": 0},
                            {"generation": -1}, {"generation": "1"},
                            {"generation": 1.0}, {"generation": None}):
                with self.subTest(name=name, payload=payload):
                    self.b.snapshot(self.hub.rooms[self.rid])
                    self.owner.shared.route(name, payload)
                    self.assertFalse(self.b.known)
                    self.assertFalse(self.b.empty())
                    with self.assertRaises(HubDisconnected):
                        self.b.close_room(self.rid)
        self.assertFalse(any(m == "CloseRoom" for m, _ in self.hub.calls))

    def test_delayed_lifecycle_cannot_restore_unknown_or_disconnected_roster(self):
        retired = self.hub._connect_generation
        self.hub._connect_generation += 1
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        current = self.hub._connect_generation
        for known, connected, snapshot in ((False, True, current),
                                           (True, False, current),
                                           (True, True, retired)):
            for name, generation in (("HubDisconnected", retired),
                                     ("HubReconnected", current)):
                with self.subTest(known=known, connected=connected,
                                  snapshot=snapshot, name=name):
                    self.b.snapshot(self.hub.rooms[self.rid])
                    self.b.known = known
                    self.b.snapshot_generation = snapshot
                    self.hub.connected = connected
                    self.owner.shared.route(name, {"generation": generation})
                    self.assertFalse(self.b.known)
                    self.assertFalse(self.b.empty())
                    with self.assertRaises(HubDisconnected):
                        self.b.close_room(self.rid)
        self.assertFalse(any(m == "CloseRoom" for m, _ in self.hub.calls))

    def provider_snapshot(self):
        previous = []
        while not self.b.outbox.empty():
            previous.append(self.b.outbox.get_nowait())
        self.hub.requires_event_provenance = True
        self.hub._connect_generation += 1
        self.hub.rooms[self.rid]["players"] = [{"user_id": 7}]
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assertTrue(self.b.known)
        self.assertEqual(self.b.players, {7})
        # A fresh JoinRoom now publishes its authoritative roster to the
        # worker before any subsequently tested deltas or lifecycle events.
        channel, (name, snapshot) = self.b.outbox.get_nowait()
        self.assertEqual((channel, name), ("hub", "RoomRosterSnapshot"))
        self.assertEqual(snapshot["players"], [{"user_id": 7}])
        self.assertNotIn("password", snapshot)
        self.assertTrue(self.b.outbox.empty())
        for event in previous:
            self.b.outbox.put_nowait(event)
        return self.hub._connect_generation

    def test_retired_provider_departures_reach_neither_mirror_nor_worker(self):
        from poolrotater_host.provider_referee import _ProviderEvent
        current = self.provider_snapshot()
        for name, identity in (("UserLeft", "user_id"), ("UserKicked", "kicked_user_id")):
            with self.subTest(name=name):
                before = self.b.outbox.qsize()
                payload = {"room_id": self.rid, identity: 7}
                self.owner.shared.route_event(self.hub.provider_event(name, payload, current - 1))
                self.assertTrue(self.b.known)
                self.assertEqual(self.b.players, {7})
                self.assertFalse(self.b.empty())
                self.assertEqual(self.b.outbox.qsize(), before)
                with self.assertRaises(HubDisconnected):
                    self.b.close_room(self.rid)
        self.assertFalse(any(m == "CloseRoom" for m, _ in self.hub.calls))

    def test_current_provider_departure_updates_mirror_and_worker(self):
        from poolrotater_host.provider_referee import _ProviderEvent
        current = self.provider_snapshot()
        payload = {"room_id": self.rid, "user_id": 7}
        self.owner.shared.route_event(self.hub.provider_event("UserLeft", payload, current))
        self.assertTrue(self.b.empty())
        self.assertEqual(self.b.outbox.get_nowait(), ("hub", ("UserLeft", payload)))
        self.b.close_room(self.rid)
        self.assertEqual(sum(m == "CloseRoom" for m, _ in self.hub.calls), 1)

    def test_remote_generation_cannot_override_private_provider_provenance(self):
        from poolrotater_host.provider_referee import _ProviderEvent
        current = self.provider_snapshot()
        payload = {"room_id": self.rid, "user_id": 7, "generation": current}
        self.owner.shared.route_event(self.hub.provider_event("UserLeft", payload, current - 1))
        self.assertTrue(self.b.known)
        self.assertEqual(self.b.players, {7})
        self.assertTrue(self.b.outbox.empty())
        self.owner.shared.route_event(self.hub.provider_event("HubDisconnected", {"generation": current}, current - 1))
        self.assertTrue(self.b.known)
        self.assertEqual(self.b.outbox.get_nowait(),
                         ("hub", ("HubDisconnected", {"generation": current - 1})))
        self.owner.shared.route_event(self.hub.provider_event("HubReconnected", {"generation": current - 1}, current))
        self.assertTrue(self.b.known)
        self.assertEqual(self.b.outbox.get_nowait(),
                         ("hub", ("HubReconnected", {"generation": current})))

    def test_unqualified_provider_events_invalidate_without_delivering_departure(self):
        from poolrotater_host.provider_referee import _ProviderEvent
        current = self.provider_snapshot()
        payload = {"room_id": self.rid, "user_id": 7}
        invalid = [("UserLeft", payload),
                   {"name": "UserLeft", "payload": payload, "generation": current}]
        invalid.extend(_ProviderEvent("UserLeft", payload, value, 1)
                       for value in (None, True, 0, -1, "2", 2.0, current + 1))
        invalid.extend((_ProviderEvent("Unknown", payload, current, 1),
                        _ProviderEvent("UserLeft", None, current, 1),
                        _ProviderEvent("UserLeft", {"room_id": self.rid, "user_id": float("nan")}, current, 1)))
        invalid.extend(_ProviderEvent("UserLeft", payload, current, value)
                       for value in (None, True, 0, -1, "1", 1.0))
        for item in invalid:
            with self.subTest(item=item):
                self.b.snapshot(self.hub.rooms[self.rid])
                self.b.hub.joined[self.rid] = (current, copy.deepcopy(self.hub.rooms[self.rid]))
                self.owner.shared.route_event(item)
                self.assertFalse(self.b.known)
                self.assertEqual(self.b.players, {7})
                self.assertFalse(self.b.empty())
                self.assertEqual(self.b.outbox.get_nowait(), ("hub", ("HubDisconnected", {})))
                self.assertTrue(self.b.outbox.empty())
                self.assertNotIn(self.rid, self.b.hub.joined)
                with self.assertRaises(HubDisconnected):
                    self.b.handle("hub.invoke", "StartMatch", [self.rid])
                with self.assertRaises(HubDisconnected):
                    self.b.close_room(self.rid)
        self.assertFalse(any(m == "CloseRoom" for m, _ in self.hub.calls))
        self.assertFalse(any(m == "StartMatch" for m, _ in self.hub.calls))
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assertTrue(self.b.known)
        self.assertEqual(self.b.players, {7})
        self.assertEqual(sum(m == "JoinRoom" for m, _ in self.hub.calls), 2)

    def test_provider_direct_route_cannot_bypass_event_provenance(self):
        self.provider_snapshot()
        self.owner.shared.route("UserLeft", {"room_id": self.rid, "user_id": 7})
        self.assertFalse(self.b.known)
        self.assertEqual(self.b.players, {7})
        self.assertEqual(self.b.outbox.get_nowait(), ("hub", ("HubDisconnected", {})))
        self.assertTrue(self.b.outbox.empty())

    def test_legacy_two_tuple_departure_delivery_remains_unchanged(self):
        payload = {"room_id": self.rid, "user_id": 7, "generation": -1}
        self.owner.shared.route_event(("UserJoined", payload))
        self.assertEqual(self.b.players, {7})
        self.owner.shared.route_event(("UserLeft", payload))
        self.assertTrue(self.b.empty())
        self.assertEqual(self.b.outbox.get_nowait(), ("hub", ("UserJoined", payload)))
        self.assertEqual(self.b.outbox.get_nowait(), ("hub", ("UserLeft", payload)))

    def drain_provider_events(self):
        while not self.owner.shared.events.empty():
            self.owner.shared.route_event(self.owner.shared.events.get_nowait())

    def assert_provider_fenced(self):
        self.assertFalse(self.b.known)
        self.assertFalse(self.b.empty())
        self.assertTrue(self.b.hub.roster_fenced)
        self.assertFalse(self.b.hub.is_connected())
        self.assertNotIn(self.rid, self.b.hub.joined)
        journal_calls = list(self.owner.record.mock_calls)
        for operation in ("StartMatch", "CloseRoom"):
            with self.subTest(operation=operation), self.assertRaises(HubDisconnected):
                # Explicit CloseRoom normally skips the infrastructure empty
                # check, but cannot skip the provider provenance fence.
                self.b.handle("hub.invoke", operation, [self.rid])
        self.assertEqual(self.owner.record.mock_calls, journal_calls)
        self.assertFalse(self.b.uncertain)

    def test_pending_provider_roster_prevents_cached_join_and_requires_fresh_drained_snapshot(self):
        current = self.provider_snapshot()
        item = self.hub.provider_event("UserLeft", {"room_id": self.rid, "user_id": 7})
        self.owner.shared.events.put_nowait(item)
        with self.assertRaises(HubDisconnected):
            self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assert_provider_fenced()
        self.assertEqual(self.b.hub.roster_cutoff, (current, item.sequence))
        self.drain_provider_events()
        delivered = list(self.b.outbox.queue)
        self.assertFalse(any(event[1][0] == "UserLeft" for event in delivered))
        self.assertFalse(self.hub.roster_progress()[2])

        before = sum(m == "JoinRoom" for m, _ in self.hub.calls)
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assertEqual(sum(m == "JoinRoom" for m, _ in self.hub.calls), before + 1)
        self.assertTrue(self.b.known)
        self.assertEqual(self.b.players, {7})
        self.assertFalse(self.b.hub.roster_fenced)
        self.assertTrue(self.b.hub.is_connected())
        with self.assertRaises(HubDisconnected):
            self.b.close_room(self.rid)

    def test_provider_roster_arriving_during_join_fences_the_returned_snapshot(self):
        current = self.provider_snapshot()
        self.b.hub.invalidate_membership(self.rid)
        original = self.hub.invoke
        pending = []
        def during_join(method, args, **kwargs):
            if method == "JoinRoom":
                item = self.hub.provider_event("UserKicked", {"room_id": self.rid, "kicked_user_id": 7})
                pending.append(item)
                self.owner.shared.events.put_nowait(item)
            return original(method, args, **kwargs)
        with patch.object(self.hub, "invoke", side_effect=during_join):
            with self.assertRaises(HubDisconnected):
                self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assert_provider_fenced()
        self.assertEqual(self.b.hub.roster_cutoff, (current, pending[0].sequence))
        self.drain_provider_events()
        self.assertFalse(any(event[1][0] == "UserKicked" for event in list(self.b.outbox.queue)))
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assertTrue(self.b.known)
        self.assertEqual(self.b.players, {7})
        self.assertFalse(self.b.hub.roster_fenced)

    def test_provider_out_of_order_receipt_ack_cannot_hide_a_popped_departure(self):
        self.provider_snapshot()
        self.hub.rooms[self.rid]["players"].append({"user_id": 8})
        departure = self.hub.provider_event("UserLeft", {"room_id": self.rid, "user_id": 7})
        arrival = self.hub.provider_event("UserJoined", {"room_id": self.rid, "user_id": 8})
        self.owner.shared.events.put_nowait(departure)
        self.owner.shared.events.put_nowait(arrival)
        # Dispatcher A has popped its receipt but has not acquired the shared
        # lock. A second consumer drains B and acknowledges it first.
        held = self.owner.shared.events.get_nowait()
        self.assertIs(held, departure)
        self.owner.shared.route_event(self.owner.shared.events.get_nowait())
        self.assertTrue(self.hub.roster_progress()[2])
        self.assertEqual(set(self.hub.pending_roster), {departure.sequence})
        with self.assertRaises(HubDisconnected):
            self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assert_provider_fenced()
        with self.assertRaises(HubDisconnected):
            self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assert_provider_fenced()

        self.owner.shared.route_event(held)
        self.assertFalse(self.hub.roster_progress()[2])
        self.assertFalse(any(event[1][0] == "UserLeft" for event in list(self.b.outbox.queue)))
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assertTrue(self.b.known)
        self.assertEqual(self.b.players, {7, 8})
        self.assertFalse(self.b.hub.roster_fenced)

    def test_provider_publication_defers_pending_roster_then_resumes_after_fresh_join(self):
        self.provider_snapshot()
        self.b.pending_public = True
        item = self.hub.provider_event("UserLeft", {"room_id": self.rid, "user_id": 7})
        self.owner.shared.events.put_nowait(item)
        self.b.publish()
        self.assertTrue(self.b.pending_public)
        self.assertTrue(self.b.private)
        self.assertEqual(self.hub.rooms[self.rid]["password"], "initial-private")
        self.assertFalse(any(m == "ChangeRoomSettings" for m, _ in self.hub.calls))

        with self.assertRaises(HubDisconnected):
            self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.b.publish()
        self.assertTrue(self.b.pending_public)
        self.assertEqual(self.hub.rooms[self.rid]["password"], "initial-private")
        self.drain_provider_events()
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assertTrue(self.b.known)
        self.assertEqual(self.b.players, {7})
        self.b.publish()
        self.assertFalse(self.b.pending_public)
        self.assertFalse(self.b.private)
        self.assertEqual(self.hub.rooms[self.rid]["password"], "")
        self.assertEqual(sum(m == "ChangeRoomSettings" for m, _ in self.hub.calls), 1)

    def test_direct_provider_room_join_cannot_bypass_pending_roster_fence(self):
        current = self.provider_snapshot()
        item = self.hub.provider_event("UserLeft", {"room_id": self.rid, "user_id": 7})
        self.owner.shared.events.put_nowait(item)
        with self.assertRaises(HubDisconnected):
            self.b.hub.join_room(self.rid)
        self.assert_provider_fenced()
        self.assertEqual(self.b.hub.roster_cutoff, (current, item.sequence))
        with self.assertRaises(HubDisconnected):
            self.b.hub.join_room(self.rid)
        self.assert_provider_fenced()
        self.drain_provider_events()
        self.assertFalse(any(event[1][0] == "UserLeft" for event in list(self.b.outbox.queue)))

        before = sum(m == "JoinRoom" for m, _ in self.hub.calls)
        result = self.b.hub.join_room(self.rid)
        self.assertEqual(sum(m == "JoinRoom" for m, _ in self.hub.calls), before + 1)
        self.assertEqual(result["players"], [{"user_id": 7}])
        self.assertFalse(self.b.hub.roster_fenced)
        self.assertFalse(self.b.known)
        # The broker independently rebuilds its unknown mirror from another
        # authoritative request instead of accepting the direct view's cache.
        self.b.handle("hub.invoke", "JoinRoom", [self.rid])
        self.assertTrue(self.b.known)
        self.assertEqual(self.b.players, {7})
        with self.assertRaises(HubDisconnected):
            self.b.close_room(self.rid)

    def test_reordered_current_roster_receipts_fence_before_departure_delivery(self):
        for later_name in ("UserJoined", "HubReconnected", "PlaylistItemAdded"):
            with self.subTest(later_name=later_name):
                current = self.provider_snapshot()
                self.b.outbox = queue.Queue(maxsize=1024)
                departure = self.hub.provider_event("UserLeft", {"room_id": self.rid, "user_id": 7})
                payload = ({"generation": current} if later_name == "HubReconnected" else
                           {"room_id": self.rid, "user_id": 7})
                later = self.hub.provider_event(later_name, payload)
                self.owner.shared.events.put_nowait(departure)
                self.owner.shared.events.put_nowait(later)
                held = self.owner.shared.events.get_nowait()
                self.owner.shared.route_event(self.owner.shared.events.get_nowait())
                self.assertTrue(self.hub.roster_progress()[2])
                self.assertEqual(self.b.players, {7})
                self.owner.shared.route_event(held)

                self.assertFalse(self.hub.roster_progress()[2])
                self.assert_provider_fenced()
                self.assertEqual(self.b.players, {7})
                delivered = list(self.b.outbox.queue)
                self.assertFalse(any(event[1][0] == "UserLeft" for event in delivered))
                self.assertTrue(any(event == ("hub", ("HubDisconnected", {})) for event in delivered))
                before = sum(m == "JoinRoom" for m, _ in self.hub.calls)
                self.b.handle("hub.invoke", "JoinRoom", [self.rid])
                self.assertEqual(sum(m == "JoinRoom" for m, _ in self.hub.calls), before + 1)
                self.assertTrue(self.b.known)
                self.assertEqual(self.b.players, {7})
                self.assertFalse(self.b.hub.roster_fenced)

                self.hub.rooms[self.rid]["players"] = []
                latest = self.hub.provider_event("UserLeft", {"room_id": self.rid, "user_id": 7})
                self.owner.shared.route_event(latest)
                self.assertTrue(self.b.empty())

    def test_missing_roster_is_not_empty(self):
        self.b.snapshot({"chat_channel_id": 1})
        self.assertFalse(self.b.empty())

    def test_join_mirror_updates_before_worker_event_delivery(self):
        self.owner.shared.route("UserJoined", {"room_id": self.rid, "user_id": 7})
        self.assertFalse(self.b.empty())
        self.owner.shared.route("UserLeft", {"room_id": self.rid, "user_id": 7})
        self.assertTrue(self.b.empty())

    def test_uncertain_close_never_replayed(self):
        self.hub.fail_close = True
        with self.assertRaises(HubInvocationUncertain):
            self.b.close_room(self.rid)
        with self.assertRaises(Exception):
            self.b.close_room(self.rid)
        self.assertTrue(self.b.uncertain)
        self.assertEqual(sum(c[0] == "CloseRoom" for c in self.hub.calls), 1)

    def test_uncertain_make_never_replayed(self):
        self.b.close_room(self.rid)
        self.hub.fail_make = True
        with self.assertRaises(HubInvocationUncertain): self.b.create("new", 1)
        with self.assertRaises(HubInvocationUncertain): self.b.create("new", 1)
        self.assertEqual(sum(c[0] == "MakeRoom" for c in self.hub.calls), 2)

    def test_fenced_and_quiesced_workers_cannot_mutate(self):
        self.b.quiesced = True
        with self.assertRaises(Exception): self.b.handle("hub.invoke", "StartMatch", [self.rid, {}])
        self.b.fenced = True
        with self.assertRaises(Exception): self.b.handle("api.chat_send", self.b.channel, "late")

    def test_fence_rechecked_after_waiting_for_shared_lock(self):
        errors = []
        self.b.close_room(self.rid)
        with self.owner.shared.lock:
            def create():
                try: self.b.handle("hub.make_room", "late", 1)
                except Exception as exc: errors.append(exc)
            thread = threading.Thread(target=create)
            thread.start()
            time.sleep(.03)
            self.b.fenced = True
        thread.join(timeout=1)
        self.assertTrue(errors)
        self.assertEqual(sum(m == "MakeRoom" for m, _ in self.hub.calls), 1)

    def test_missing_room_forget_independently_verified(self):
        with self.assertRaises(Exception): self.b.handle("hub.forget_missing_room", self.rid)
        del self.hub.rooms[self.rid]
        self.b.handle("hub.forget_missing_room", self.rid)
        self.assertFalse(self.b.hub.owned)

    def test_journal_failure_prevents_remote_mutations(self):
        self.owner.record.side_effect = OSError("disk full")
        with self.assertRaises(OSError): self.b.close_room(self.rid)
        self.assertIn(self.rid, self.hub.rooms)
        another = Broker(self.owner, "room1", "new", {})
        another.active = True
        with self.assertRaises(OSError): another.create("new", 1)
        self.assertEqual(sum(m == "MakeRoom" for m, _ in self.hub.calls), 1)
        self.assertFalse(any(m == "CloseRoom" for m, _ in self.hub.calls))

    def test_modlog_formatting_uses_room_configuration_without_thread(self):
        view = ModLog(Mock(), {"discord_modlog_include_instance_name": True, "_instance_name": "room1"})
        self.assertIn("room1", view.format_event(label="join", text="hello", room_id=1, room_name="x"))


class ProcessIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="rolling-test-")
        cls.root = Path(cls.temp.name)
        cls.releases = []
        for version in ("v6.7.0", "v6.7.1", "v6.7.2"):
            release = cls.root / version
            for name in source_files(ROOT):
                (release / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, release / name)
            # Exercise the real worker and LobbyBot, while making accidental
            # external connection attempts fail immediately in this test child.
            entry = release / "run_room_worker.py"
            text = entry.read_text().replace("from rolling.worker import run", """
def deny_network(*a, **kw):
    raise RuntimeError('external networking is disabled in this offline test')
socket.socket.connect = deny_network
from rolling.worker import run
""")
            entry.write_text(text)
            if version == "v6.7.1":
                with (release / "bot/bot.py").open("a") as f: f.write("\n# Independent room release fixture.\n")
            if version == "v6.7.2":
                with (release / "bot/bot.py").open("a") as f:
                    f.write("\ndef _injected_failure(*a, **kw):\n    raise RuntimeError('injected constructor failure')\nLobbyBot.__init__ = _injected_failure\n")
            seal(release, version)
            cls.releases.append(release)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        # These private, writable release fixtures exercise process/lifecycle
        # behavior. Historically this suite ran as root, which skips this gate
        # in RollingSupervisor.spawn. Reproduce that fixture boundary without
        # requiring root. The real guard remains unchanged and is exercised by
        # the v7 ReleaseBoundaryTests against a writable release.
        immutable = patch('rolling.supervisor.require_immutable')
        immutable.start()
        self.addCleanup(immutable.stop)
        self.runtime_tmp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.runtime_tmp.cleanup)
        self.runtime = Path(self.runtime_tmp.name)
        (self.runtime / "instances").mkdir()
        farm = self.runtime / "farm.db"
        with sqlite3.connect(farm) as db:
            db.execute("CREATE TABLE beatmaps (beatmap_id INTEGER PRIMARY KEY, artist TEXT, title TEXT, "
                       "version TEXT, sr REAL, hit_length INTEGER, passcount INTEGER, status TEXT, ranked_date TEXT)")
            db.executemany("INSERT INTO beatmaps VALUES (?, 'Artist', 'Title', 'Diff', 3.5, 120, 2000, 'ranked', '2026-01-01')",
                           [(i,) for i in range(1, 21)])
        configs = []
        for index in (1, 2, 3, 4):
            keys = 7 if index == 3 else 4
            cfg = {"ruleset_id": 0 if index < 3 else 3, "mania_keys": keys,
                   "farm_db": str(farm), "mania_db": str(_mania_fixture_db(keys, "catalog")),
                   "mania_farm_db": str(_mania_fixture_db(keys, "farm")), "pool_limit": 100,
                   "sr_min": 2, "sr_max": 5, "max_length": 300, "min_passcount": 0,
                   "max_map_age_years": 30, "auto_difficulty": index == 1, "loved_mode": False,
                   "room_name": "Offline test", "discord_control_enabled": False,
                   "log_file": str(self.runtime / "logs" / f"room{index}.log"),
                   "discord_modlog_enabled": False, "map_pp_display_enabled": False, "ai_enabled": False,
                   "chat_websocket_enabled": False, "hard_restart_interval_minutes": 0,
                   "allowed_mods": [], "required_mods": [], "freestyle": False,
                   "ratings_db": str(self.runtime / "state/ratings.db"),
                   "api_rate_db": str(self.runtime / "state/rate.db"),
                   "process_control_db": str(self.runtime / "state/process.db"),
                   "discord_control_room_alias": f"room{index}"}
            path = self.runtime / "instances" / f"room{index}.json"
            path.write_text(json.dumps(cfg)); configs.append(str(path))
        universal = self.runtime / "instances/universal.json"; universal.write_text("{}")
        cfgs = load_rooms(configs, str(universal))
        for cfg in cfgs: cfg["_authenticated_user_id"] = 99
        self.hub = FakeHub()
        self.supervisor = RollingSupervisor(cfgs, FakeApi(self.hub), self.hub, queue.Queue(), None,
                                             self.releases[0], self.runtime, health_seconds=.15, startup_timeout=35)
        self.hub.shared = self.supervisor.shared
        self.addCleanup(self.cleanup_supervisor)
        self.supervisor.shared.start_dispatch()

    def cleanup_supervisor(self):
        s = self.supervisor
        s.stopping.set()
        if s.rollout_thread: s.rollout_thread.join(timeout=10)
        for worker in list(s.handles.values()): s.dispose(worker)
        s.shared.stop()
        s.discord.stop()
        for modlog in s.modlogs.values(): modlog.stop(timeout=.1)

    def start(self, alias):
        worker = self.supervisor.prepare(alias, self.releases[0])
        try:
            self.supervisor.activate(worker)
        except Exception:
            logs = list((self.runtime / "logs/workers").glob("*.log"))
            for path in logs: print(path.read_text()[-8000:])
            raise
        return worker

    def test_owned_worker_sigterm_is_delivered_after_masked_spawn(self):
        import signal
        worker=self.start('room1')
        worker.signal_target.send(signal.SIGTERM)
        self.assertEqual(worker.process.wait(timeout=5),0)

    def test_private_maintenance_gates_preserve_rooms_and_cancel_on_late_join(self):
        workers=[self.start(alias) for alias in ('room1','room2','room3','room4')]
        path=self.runtime/'state/rolling/supervisor-maintenance.json'
        atomic_json(path,dict(format=1,phase='locking',nonce='a'*32,
                   expires_at=time.time()+60,bridge_release=str(self.releases[0])))
        self.supervisor.wait_for(lambda: all(w.status.get('maintenance',{}).get('locked')
                                 and w.broker.password for w in workers),15)
        before={w.alias:w.status['room_id'] for w in workers}
        w=workers[0]
        self.supervisor.shared.route('UserJoined',dict(room_id=w.status['room_id'],user_id=7,username='LatePlayer'))
        self.supervisor.wait_for(lambda: not w.status.get('maintenance',{}).get('locked')
                                 and w.broker.password=='',10,w)
        self.assertIn(7,w.broker.players)
        path.unlink()
        self.supervisor.wait_for(lambda: all(not x.status.get('maintenance',{}).get('locked')
                                 and x.broker.password=='' for x in workers),10)
        self.assertEqual({x.alias:x.status['room_id'] for x in workers},before)
        self.assertTrue(all(x.process.poll() is None for x in workers))
        self.assertFalse(any(call[0] in ('CloseRoom','KickPlayer') for call in self.hub.calls))
        self.supervisor.shared.route('UserLeft',dict(room_id=w.status['room_id'],user_id=7))

    def test_real_workers_mix_versions_and_leave_sibling_untouched(self):
        a, b = self.start("room1"), self.start("room2")
        mania7, mania4 = self.start("room3"), self.start("room4")
        sibling_pid, sibling_room = b.process.pid, b.status["room_id"]
        self.supervisor.schema_baseline = schemas(list(self.supervisor.configs.values()))
        self.supervisor.request(["room1"], self.releases[1])
        self.supervisor.rollout_alias = "room1"
        self.supervisor.roll("room1", self.releases[1])
        new = self.supervisor.workers["room1"]
        self.assertEqual(new.manifest["version"], "v6.7.1", self.supervisor.registry.snapshot())
        self.assertTrue(new.fresh())
        self.assertEqual(new.status["version"], "v6.7.1")
        self.assertEqual(b.process.pid, sibling_pid)
        self.assertIsNone(b.process.poll())
        self.assertIsNone(mania7.process.poll())
        self.assertIsNone(mania4.process.poll())
        self.assertEqual(len(self.hub.rooms), 4)
        self.assertIn(sibling_room, self.hub.rooms)
        self.assertFalse(any(m == "CloseRoom" and v[0] == sibling_room for m, v in self.hub.calls))
        self.assertEqual(self.supervisor.registry.snapshot()["rooms"]["room1"]["phase"], "running")
        self.supervisor.write_status(force=True)
        public=json.loads((self.supervisor.directory / "public-status.json").read_text())
        for alias in ("room1", "room2", "room3", "room4"):
            self.assertIsNotNone(public["rooms"][alias]["current_map"])
            self.assertTrue(public["rooms"][alias]["queue_known"])
            self.assertTrue(public["rooms"][alias]["queue"])
        self.assertFalse(any(m == "KickPlayer" for m, _ in self.hub.calls))

    def test_updated_worker_accepts_only_its_parent_controller_maintenance_gate(self):
        original = self.start('room1')
        sibling = self.start('room2')
        self.supervisor.schema_baseline = schemas(list(self.supervisor.configs.values()))
        self.supervisor.request(['room1'], self.releases[1])
        self.supervisor.rollout_alias = 'room1'
        self.supervisor.roll('room1', self.releases[1])
        updated = self.supervisor.workers['room1']
        self.assertEqual(updated.release, self.releases[1])
        self.assertNotEqual(updated.process.pid, original.process.pid)
        self.assertEqual(updated.broker.cfg['_rolling_controller_release'], str(self.releases[0]))
        before = {w.alias: (w.process.pid, w.status['room_id']) for w in (updated, sibling)}
        path = self.runtime / 'state/rolling/supervisor-maintenance.json'
        # A path naming the worker itself cannot impersonate its controller.
        atomic_json(path, dict(format=1, phase='locking', nonce='a'*32,
                   expires_at=time.time()+60, bridge_release=str(self.releases[1])))
        time.sleep(1)
        self.assertFalse(updated.status.get('maintenance', {}).get('locked'))
        atomic_json(path, dict(format=1, phase='locking', nonce='b'*32,
                   expires_at=time.time()+60, bridge_release=str(self.releases[0])))
        self.supervisor.wait_for(lambda: all(w.status.get('maintenance', {}).get('locked')
                                 and w.broker.password for w in (updated, sibling)), 15)
        path.unlink()
        self.supervisor.wait_for(lambda: all(not w.status.get('maintenance', {}).get('locked')
                                 and w.broker.password == '' for w in (updated, sibling)), 10)
        self.assertEqual({w.alias: (w.process.pid, w.status['room_id']) for w in (updated, sibling)}, before)
        self.assertTrue(all(w.process.poll() is None for w in (updated, sibling)))

    def test_join_during_lock_resumes_old_worker_without_closing(self):
        old = self.start("room1")
        rid = old.status["room_id"]
        self.supervisor.schema_baseline = schemas(list(self.supervisor.configs.values()))
        self.hub.on_lock = lambda rid: self.supervisor.shared.route("UserJoined", {"room_id": rid, "user_id": 7})
        self.supervisor.rollout_alias = "room1"
        self.supervisor.roll("room1", self.releases[1])
        self.assertIs(self.supervisor.workers["room1"], old)
        self.assertIsNone(old.process.poll())
        self.assertIn(rid, self.hub.rooms)
        self.assertEqual(self.hub.rooms[rid]["password"], "")
        self.assertFalse(old.status["paused"])
        self.assertFalse(any(m in ("CloseRoom", "KickPlayer") for m, _ in self.hub.calls))

    def test_discord_say_passes_through_real_worker_and_broker_once(self):
        from bot.discord_control import DiscordCommand
        worker=self.start("room1")
        message="offline relay regression"
        self.supervisor.discord.view_for("room1").enqueue_command(DiscordCommand(
            content=".say "+message,discord_user_id=42,username="fixture-admin",role="ADMIN",channel_id=12))
        self.supervisor.wait_for(lambda: any(m[1]==message for m in self.supervisor.api.messages),8,worker)
        sent=[m for m in self.supervisor.api.messages if m[1]==message]
        self.assertEqual(sent,[(worker.broker.channel,message)])
        self.assertIsNone(worker.process.poll())

    def test_native_discord_close_park_and_open_retire_only_current_view(self):
        from bot.discord_control import DiscordCommand
        old = self.start('room1'); sibling = self.start('room2')
        transport = self.supervisor.discord
        old_view = transport.view_for('room1'); sibling_view = transport.view_for('room2')
        # Enable only the invented command-routing views. Mutating the shared
        # startup config would correctly trip the pinned hello admission gate.
        old_view.cfg = copy.deepcopy(old_view.cfg); sibling_view.cfg = copy.deepcopy(sibling_view.cfg)
        old_view.cfg.update(discord_control_enabled=True, discord_control_channel_id=101)
        sibling_view.cfg.update(discord_control_enabled=True, discord_control_channel_id=102)
        old_view.enqueue_command(DiscordCommand(42, 'invented-admin', 'ADMIN', '.close', 101))
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            self.supervisor.tick()
            if self.supervisor.registry.snapshot()['rooms']['room1']['phase'] == 'parked': break
            time.sleep(.05)
        self.assertEqual(self.supervisor.registry.snapshot()['rooms']['room1']['phase'], 'parked')
        self.assertIsNotNone(old.process.poll())
        self.assertTrue(old_view._stop.is_set())
        self.assertFalse(transport._stop.is_set()); self.assertFalse(sibling_view._stop.is_set())
        self.assertEqual(transport.command_targets('.settings', 101), [])
        self.assertEqual(transport.command_targets('.open', 101), [(old_view, '.open')])
        old_view.enqueue_command(DiscordCommand(42, 'invented-admin', 'ADMIN', '.open', 101))
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            self.supervisor.tick()
            current = self.supervisor.workers.get('room1')
            if current is not old and self.supervisor.registry.snapshot()['rooms']['room1']['phase'] == 'running': break
            time.sleep(.05)
        self.assertTrue(current is not old, self.supervisor.registry.snapshot()['rooms']['room1'])
        self.assertEqual(self.supervisor.registry.snapshot()['rooms']['room1']['phase'], 'running')
        fresh = transport.view_for('room1')
        self.assertIsNot(fresh, old_view); self.assertFalse(fresh._stop.is_set())
        self.assertIs(current.broker.discord, fresh)
        old.broker.retire()  # delayed old generation cleanup must be harmless
        self.assertFalse(fresh._stop.is_set())
        self.assertIs(self.supervisor.discord, transport)
        self.assertIs(self.supervisor.workers['room2'], sibling)
        self.assertIsNone(sibling.process.poll())
        message = 'native reactivated Discord reply'
        fresh.enqueue_command(DiscordCommand(42, 'invented-admin', 'ADMIN', '.say '+message, 101))
        self.supervisor.wait_for(lambda:any(item[1] == message for item in self.supervisor.api.messages), 8, current)
        self.assertEqual([item for item in self.supervisor.api.messages if item[1] == message],
                         [(current.broker.channel, message)])

    def test_candidate_import_failure_keeps_old_room(self):
        old = self.start("room1")
        self.supervisor.schema_baseline = schemas(list(self.supervisor.configs.values()))
        with patch.object(self.supervisor, "prepare", side_effect=RuntimeError("bad import")) as prepare:
            self.supervisor.rollout_alias = "room1"
            self.supervisor.roll("room1", self.releases[1])
        prepare.assert_called_once()
        self.assertIsNone(old.process.poll())
        self.assertIn(old.status["room_id"], self.hub.rooms)

    def test_constructor_failure_rolls_back_after_confirmed_close(self):
        old = self.start("room1")
        self.supervisor.schema_baseline = schemas(list(self.supervisor.configs.values()))
        self.supervisor.rollout_alias = "room1"
        self.supervisor.roll("room1", self.releases[2])
        current = self.supervisor.workers["room1"]
        self.assertIsNot(current, old)
        self.assertEqual(current.manifest["version"], "v6.7.0")
        self.assertTrue(current.fresh(), self.supervisor.registry.snapshot())
        self.assertEqual(len(self.hub.rooms), 1)
        self.assertIn("previous release restored", self.supervisor.registry.snapshot()["rooms"]["room1"]["error"])

    def test_uncertain_candidate_make_does_not_attempt_rollback_creation(self):
        old = self.start("room1")
        s = self.supervisor
        s.schema_baseline = schemas(list(s.configs.values()))
        self.hub.fail_make = True
        s.rollout_alias = "room1"
        s.roll("room1", self.releases[1])
        self.assertEqual(sum(m == "MakeRoom" for m, _ in self.hub.calls), 2)
        state = s.registry.snapshot()["rooms"]["room1"]
        self.assertEqual(state["phase"], "recovery_required")
        self.assertTrue(state["ownership_unknown"])

    def test_uncertain_close_keeps_other_room_and_never_creates_replacement(self):
        old, sibling = self.start("room1"), self.start("room2")
        s = self.supervisor
        s.schema_baseline = schemas(list(s.configs.values()))
        self.hub.fail_close = True
        s.rollout_alias = "room1"
        s.roll("room1", self.releases[1])
        self.assertEqual(sum(m == "MakeRoom" for m, _ in self.hub.calls), 2)
        self.assertEqual(sum(m == "CloseRoom" for m, _ in self.hub.calls), 1)
        self.assertIsNone(sibling.process.poll())
        self.assertIn(sibling.status["room_id"], self.hub.rooms)
        self.assertEqual(s.registry.snapshot()["rooms"]["room1"]["phase"], "recovery_required")

    def test_occupied_or_stale_room_never_begins_handoff(self):
        old = self.start("room1")
        s = self.supervisor
        self.hub.shared.route("UserJoined", {"room_id": old.status["room_id"], "user_id": 7})
        with patch.object(s, "prepare") as prepare:
            s.roll("room1", self.releases[1])
            prepare.assert_not_called()
        old.broker.players.clear()
        old.status_at = 0
        with patch.object(s, "prepare") as prepare:
            s.roll("room1", self.releases[1])
            prepare.assert_not_called()

    def test_schema_or_shared_transport_change_refuses_rollout(self):
        old = self.start("room1")
        s = self.supervisor
        s.schema_baseline = schemas(list(s.configs.values()))
        with sqlite3.connect(s.configs["room1"]["ratings_db"]) as conn:
            conn.execute("CREATE TABLE unexpected (id INTEGER)")
        with self.assertRaises(IncompatibleRelease): s.request(["room1"], self.releases[1])
        changed = copy.deepcopy(old.manifest); changed["storage_epoch"] = "new"
        with self.assertRaises(IncompatibleRelease): require_compatible(old.manifest, changed)
        self.assertIsNone(old.process.poll())

    def test_clean_restart_keeps_each_rooms_pinned_release(self):
        self.supervisor.record("room1", phase="stopped", room_id=None, release=str(self.releases[1]))
        self.supervisor.record("room2", phase="stopped", room_id=None, release=str(self.releases[0]))
        self.assertEqual(self.supervisor.start_release_for("room1"), self.releases[1])

    def test_candidate_uses_latest_saved_preferences_and_refuses_shared_config_change(self):
        path = Path(self.supervisor.configs["room1"]["_config_path"])
        cfg = json.loads(path.read_text()); cfg["queue_ahead"] = 3
        path.write_text(json.dumps(cfg))
        prepared = self.supervisor.prepare("room1", self.releases[1])
        self.assertEqual(prepared.broker.cfg["queue_ahead"], 3)
        self.supervisor.dispose(prepared)
        cfg["spectator_url"] = "https://unexpected.example/"
        path.write_text(json.dumps(cfg))
        with self.assertRaises(RuntimeError): self.supervisor.prepare("room1", self.releases[1])
        self.assertFalse(self.hub.rooms)
        self.assertEqual(self.supervisor.start_release_for("room2"), self.releases[0])
        self.supervisor.record("room1", release=str(self.releases[2]), last_good_release=str(self.releases[1]))
        self.assertEqual(self.supervisor.start_release_for("room1"), self.releases[1])

    def test_seal_detects_tampering_and_permits_worker_only_change(self):
        first, second = [load_release(p) for p in self.releases[:2]]
        require_compatible(first, second)
        changed = copy.deepcopy(second); changed["shared"]["bot/hub.py"] = "different"
        with self.assertRaises(IncompatibleRelease): require_compatible(first, changed)
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            bad = Path(temporary) / "release"; shutil.copytree(self.releases[1], bad)
            with (bad / "bot/bot.py").open("a") as f: f.write("\n# tampered\n")
            with self.assertRaises(IncompatibleRelease): load_release(bad)


if __name__ == "__main__": unittest.main()
