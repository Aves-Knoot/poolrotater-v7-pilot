"""Offline regression tests. No OAuth, osu!, Discord or desktop contact."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json
import os
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from bot.bot import LobbyBot
from bot.chat_websocket import ChatSocketEvent, ChatWebSocket
from bot.discord_control import (
    DiscordCommand,
    normalize_public_join_base_url,
    public_room_links,
)
from bot.hub import HubDisconnected
from bot.multiroom import ProcessLease, SharedReferee, SharedHeadlessApi, SharedDiscord
from run_multiroom import load_rooms, select_room_paths, Supervisor


class StubHub:
    def __init__(self):
        self._connect_generation = 1
        self.next_id = 100
        self.calls = []
        self.stopped = 0
        self.connected = True

    def make_room(self, *args):
        self.next_id += 1
        self.calls.append(("MakeRoom", args))
        return {"room_id": self.next_id, "chat_channel_id": self.next_id + 1000}

    def invoke(self, method, args, **kwargs):
        self.calls.append((method, args))
        return {"room_id": args[0]}

    def is_connected(self):
        return self.connected

    def wait_connected(self, timeout=30):
        return self.connected

    def connect(self):
        self.connected = True

    def stop(self):
        self.stopped += 1


class HubIsolationTests(unittest.TestCase):
    def setUp(self):
        self.hub = StubHub()
        self.shared = SharedReferee(self.hub, queue.Queue())
        self.a = self.shared.room_view("room1", queue.Queue())
        self.b = self.shared.room_view("room2", queue.Queue())
        self.ra = self.a.make_room("a", 1)["room_id"]
        self.rb = self.b.make_room("b", 2)["room_id"]

    def test_routes_only_to_owning_room_and_rejects_cross_room_calls(self):
        self.shared.route("MatchStarted", {"room_id": self.ra, "playlist_item_id": 1})
        self.assertEqual(self.a.events.get_nowait()[0], "MatchStarted")
        self.assertTrue(self.b.events.empty())
        with self.assertRaises(ValueError):
            self.a.start_match(self.rb)
        self.a.start_match(self.ra, countdown=10.0)
        self.assertEqual(self.hub.calls[-1], ("StartMatch", [self.ra, {"countdown": 10}]))
        self.assertIsInstance(self.hub.calls[-1][1][1]["countdown"], int)

    def test_close_and_hard_restart_leave_other_room_connected(self):
        self.a.close_room(self.ra)
        self.a.stop()
        self.assertEqual(self.hub.stopped, 0)
        new = self.a.make_room("replacement", 3)["room_id"]
        self.shared.route("UserJoined", {"room_id": self.ra, "user_id": 1})
        self.assertTrue(self.a.events.empty())
        self.shared.route("UserJoined", {"room_id": new, "user_id": 1})
        self.assertFalse(self.a.events.empty())
        self.b.start_match(self.rb)
        self.assertEqual(self.shared.routes[self.rb], self.b)

    def test_reconnect_broadcast_rejoins_once_each_generation(self):
        self.a.join_room(self.ra)  # already joined by MakeRoom
        self.assertFalse(any(c[0] == "JoinRoom" for c in self.hub.calls))
        self.hub._connect_generation += 1
        self.shared.route("HubReconnected", {"generation": 2})
        self.assertEqual(self.a.events.get_nowait()[0], "HubReconnected")
        self.assertEqual(self.b.events.get_nowait()[0], "HubReconnected")
        for _ in range(2):
            self.a.join_room(self.ra)
            self.b.join_room(self.rb)
        self.assertEqual([c for c in self.hub.calls if c[0] == "JoinRoom"],
                         [("JoinRoom", [self.ra]), ("JoinRoom", [self.rb])])

    def test_error2_can_invalidate_same_generation_and_retry(self):
        self.a.invalidate_membership(self.ra)
        self.a.join_room(self.ra)
        self.assertEqual(self.hub.calls[-1], ("JoinRoom", [self.ra]))

    def test_unknown_creation_and_close_outcomes_are_not_replayed(self):
        with self.assertRaises(RuntimeError):
            self.a.make_room("duplicate", 3)
        with patch.object(self.hub, "invoke", side_effect=HubDisconnected("unknown")):
            with self.assertRaises(HubDisconnected):
                self.a.close_room(self.ra)
        self.assertIn(self.ra, self.a.owned)
        self.a.forget_missing_room(self.ra)
        self.a.make_room("confirmed missing replacement", 3)
        self.shared.stopping.set()
        self.b.close_room(self.rb)
        with self.assertRaises(HubDisconnected):
            self.b.make_room("during shutdown", 1)

    def test_event_arriving_during_creation_is_routed_after_registration(self):
        self.a.close_room(self.ra)
        def make(*args):
            self.shared.events.put(("UserJoined", {"room_id": 500, "user_id": 1}))
            return {"room_id": 500}
        with patch.object(self.hub, "make_room", side_effect=make):
            self.shared.start_dispatch()
            try:
                self.a.make_room("early", 1)
                self.assertEqual(self.a.events.get(timeout=1)[1]["room_id"], 500)
            finally:
                self.shared.stop()
        self.assertEqual(self.hub.stopped, 1)

    def test_two_threads_serialize_hub_invocations(self):
        active = []
        overlap = []
        def invoke(*args, **kwargs):
            active.append(1)
            overlap.append(len(active))
            time.sleep(0.002)
            active.pop()
            return {}
        with patch.object(self.hub, "invoke", side_effect=invoke):
            threads = [threading.Thread(target=v.start_match, args=(rid,))
                       for v, rid in [(self.a, self.ra), (self.b, self.rb)] * 5]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(max(overlap), 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.calls = []
        self.responses = []
        mock_http = httpx.Client(transport=httpx.MockTransport(self.handle), trust_env=False)
        with patch("bot.api.httpx.Client", return_value=mock_http):
            self.api = SharedHeadlessApi(SimpleNamespace(access_token=lambda: "test-only"),
                                         per_minute=55, burst_capacity=1)
        self.api.limiter = SimpleNamespace(per_minute=55, wait=lambda **kw: True,
                                           record_request=Mock())
        self.api._pause = self.pause
        self.clock = patch("bot.multiroom.time.monotonic", side_effect=lambda: self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.api.close()

    def pause(self, seconds):
        self.now += max(0, seconds)
        return not self.api.cancel_pending.is_set()

    def handle(self, req):
        self.calls.append((req.method, req.url.path, self.now))
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return httpx.Response(200, json={"message_id": len(self.calls)})

    def test_chat_membership_never_puts_and_failures_are_not_empty_chat(self):
        self.api.chat_join(11, 1)
        self.assertEqual(self.calls[0][:2], ("GET", "/api/v2/chat/channels/11"))
        self.responses = [httpx.Response(404, json={"error": "unavailable"})]
        with self.assertLogs("multiroom", "WARNING") as logs:
            self.assertIsNone(self.api.chat_messages(12))
        self.assertIn("HTTP 404", " ".join(logs.output))

    def test_chat_ack_deduplicated_and_sends_share_global_spacing(self):
        self.api.chat_ack()
        self.api.chat_ack()
        self.assertEqual(len(self.calls), 1)
        callback = Mock()
        self.api.chat_send(11, "a", before_request=callback)
        self.api.chat_send(12, "b", before_request=callback)
        callback.assert_not_called()
        self.assertGreaterEqual(self.calls[-1][2] - self.calls[-2][2], 1.5)

    def test_all_requests_share_one_configurable_limiter(self):
        self.api.limiter.wait = Mock()
        for channel in [11, 12] * 3:
            self.api.chat_messages(channel)
        self.assertEqual(self.api.limiter.wait.call_count, 6)

    def test_profile_and_top_scores_are_cached_across_rooms(self):
        self.responses = [
            httpx.Response(200, json={"id": 7, "username": "test"}),
            httpx.Response(200, json=[{"pp": 100.0}]),
        ]
        self.assertEqual(self.api.user(7)["username"], "test")
        self.assertEqual(self.api.user(7)["username"], "test")
        self.assertEqual(len(self.api.user_top_scores(7)), 1)
        self.assertEqual(len(self.api.user_top_scores(7)), 1)
        self.assertEqual(len(self.calls), 2)
        self.api.configure_profile_cache(0)
        self.api.user(7)
        self.assertEqual(len(self.calls), 3)

    def test_profile_cache_can_be_invalidated_after_a_score(self):
        self.responses = [
            httpx.Response(200, json={"id": 7, "username": "before"}),
            httpx.Response(200, json={"id": 7, "username": "after"}),
        ]
        self.assertEqual(self.api.user(7)["username"], "before")
        self.api.invalidate_profile_cache(7)
        self.assertEqual(self.api.user(7)["username"], "after")
        self.assertEqual(len(self.calls), 2)

    def test_429_queues_and_respects_full_retry_after(self):
        self.responses = [httpx.Response(429, headers={"Retry-After": "130"}),
                          httpx.Response(200, json={"message_id": 1})]
        self.assertEqual(self.api.chat_send(11, "a")["message_id"], 1)
        self.api.chat_messages(12)
        self.assertGreaterEqual(self.calls[1][2] - self.calls[0][2], 130)

    def test_ambiguous_post_never_replayed(self):
        self.responses = [httpx.ReadTimeout("test")]
        self.assertIsNone(self.api.chat_send(11, "a"))
        self.assertEqual(len(self.calls), 1)

    def test_definite_401_refreshes_once_then_sends(self):
        self.api.tokens.invalidate_access_token=Mock()
        self.responses=[httpx.Response(401),httpx.Response(200,json={"message_id":42})]
        self.assertEqual(self.api.chat_send(11,"relay")["message_id"],42)
        self.api.tokens.invalidate_access_token.assert_called_once_with("test-only")
        self.assertEqual(len(self.calls),2)

    def test_repeated_401_does_not_loop_or_claim_missing_room(self):
        self.api.tokens.invalidate_access_token=Mock()
        self.responses=[httpx.Response(401),httpx.Response(401)]
        self.assertEqual(self.api.room_probe(101),("unknown",None))
        self.assertEqual(len(self.calls),2)
        self.api.tokens.invalidate_access_token.assert_called_once()

    def test_401_followed_by_ambiguous_post_does_not_replay_again(self):
        self.api.tokens.invalidate_access_token=Mock()
        self.responses=[httpx.Response(401),httpx.ReadTimeout("test")]
        self.assertIsNone(self.api.chat_send(11,"relay"))
        self.assertEqual(len(self.calls),2)

    def test_chat_authority_rechecked_after_pacing(self):
        authority=Mock(return_value=False)
        self.assertIsNone(self.api.chat_send(11,"late",_authority_check=authority))
        self.assertEqual(self.calls,[])
        authority.assert_called_once()

    def test_authentication_outage_defers_requests_without_killing_worker(self):
        self.api.tokens.access_token=Mock(side_effect=RuntimeError("fixture token outage"))
        self.assertEqual(self.api.room_probe(101),("unknown",None))
        self.assertEqual(self.calls,[])
        self.assertGreater(self.api._blocked_until,self.now)

    def test_room_probe_only_404_is_missing_and_shutdown_cancels_pending(self):
        for status, expected in [(403, "unknown"), (500, "unknown"), (404, "missing")]:
            self.responses = [httpx.Response(status)]
            self.assertEqual(self.api.room_probe(101)[0], expected)
        self.api.cancel_pending.set()
        self.assertEqual(self.api.room_probe(101)[0], "unknown")
        self.assertEqual(len(self.calls), 3)


class ConfigAndDiscordTests(unittest.TestCase):
    def test_room_count_selects_one_to_four_and_rejects_invalid(self):
        for count in range(1, 5):
            self.assertEqual(select_room_paths(room_count=count),
                             [f"instances/room{i}.json" for i in range(1, count + 1)])
        for count in (0, 5, True, 2.5, "3"):
            with self.assertRaises(ValueError):
                select_room_paths(room_count=count)
        with tempfile.TemporaryDirectory() as tmp:
            universal = Path(tmp) / "universal.json"
            universal.write_text(json.dumps({"room_count": 4}))
            self.assertEqual(len(select_room_paths(universal=universal)), 4)
            self.assertEqual(select_room_paths(["custom.json"], universal=universal), ["custom.json"])
            with self.assertRaises(ValueError):
                select_room_paths(["custom.json"], 3)

    def test_four_rooms_have_distinct_channels_and_routes(self):
        paths = []
        for i in range(1, 5):
            path = self.root / f"room{i}.json"
            path.write_text(json.dumps({"discord_control_enabled": False,
                                       "discord_control_channel_id": str(100 + i)}))
            paths.append(path)
        configs = load_rooms(paths)
        for cfg in configs:
            cfg["discord_control_enabled"] = True
        bridge = SharedDiscord(configs)
        for i, view in enumerate(bridge.views, 1):
            self.assertEqual(bridge.command_targets(".settings", 100 + i), [(view, ".settings")])
            self.assertEqual(bridge.command_targets(f".room{i} settings", 101), [(view, ".settings")])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = []
        for i in (1, 2):
            path = self.root / f"room{i}.json"
            path.write_text(json.dumps({"discord_control_guild_id": "50",
                "discord_control_channel_id": str(100 + i), "discord_control_enabled": False}))
            self.paths.append(path)
        (self.root / "universal.json").write_text(json.dumps({
            "lazer_coordinator_enabled": True, "lazer_chat_patrol_enabled": True,
            "requests_per_minute": 60, "requests_burst_capacity": 90}))

    def test_unsafe_legacy_settings_disabled_without_changing_json(self):
        before = [p.read_bytes() for p in self.paths]
        configs = load_rooms(self.paths)
        self.assertEqual([p.read_bytes() for p in self.paths], before)
        for cfg in configs:
            self.assertFalse(cfg["lazer_coordinator_enabled"])
            self.assertFalse(cfg["lazer_chat_patrol_enabled"])
            self.assertFalse(cfg["lazer_rejoin_on_hard_restart"])
            self.assertEqual(cfg["requests_per_minute"], 60)
            self.assertEqual(cfg["requests_burst_capacity"], 90)
            self.assertFalse(cfg["_supervisor_overrides"]["lazer_coordinator_enabled"])

    def test_duplicate_channels_and_aliases_rejected(self):
        self.paths[1].write_text(self.paths[0].read_text())
        with self.assertRaisesRegex(ValueError, "distinct Discord"):
            load_rooms(self.paths)
        with self.assertRaisesRegex(ValueError, "distinct room alias"):
            load_rooms([self.paths[0], self.paths[0]])

    def test_public_status_channels_are_distinct_and_not_control_channels(self):
        first = json.loads(self.paths[0].read_text())
        second = json.loads(self.paths[1].read_text())
        first["discord_control_enabled"] = True
        second["discord_control_enabled"] = True
        first["discord_public_status_channel_id"] = "901"
        second["discord_public_status_channel_id"] = "901"
        self.paths[0].write_text(json.dumps(first))
        self.paths[1].write_text(json.dumps(second))
        with self.assertRaisesRegex(ValueError, "distinct public Discord"):
            load_rooms(self.paths)

        second["discord_public_status_channel_id"] = "101"
        self.paths[1].write_text(json.dumps(second))
        with self.assertRaisesRegex(ValueError, "different from private"):
            load_rooms(self.paths)

    def test_one_discord_transport_routes_each_command_exactly_once(self):
        configs = load_rooms(self.paths)
        for cfg in configs:
            cfg["discord_control_enabled"] = True
        bridge = SharedDiscord(configs)
        self.assertEqual(bridge.command_targets(".settings", 101), [(bridge.views[0], ".settings")])
        self.assertEqual(bridge.command_targets(".settings", 102), [(bridge.views[1], ".settings")])
        self.assertEqual(bridge.command_targets(".room2 say hi", 101), [(bridge.views[1], ".say hi")])
        self.assertEqual(bridge.command_targets(".room1.shutdown", 102), [(bridge.views[0], ".shutdown this")])
        bridge.views[0].stop()
        old_view = bridge.views[0]
        self.assertFalse(bridge._stop.is_set())
        self.assertEqual(bridge.command_targets(".settings", 101), [])
        self.assertEqual(bridge.command_targets(".room2 settings", 101), [(bridge.views[1], ".settings")])
        # A stopped room still accepts supervisor-owned admin lifecycle
        # commands so it can be recreated without restarting sibling rooms.
        self.assertEqual(bridge.command_targets(".open", 101), [(bridge.views[0], ".open")])
        self.assertEqual(bridge.command_targets(".room1 reopen", 102), [(bridge.views[0], ".reopen")])

        command = DiscordCommand(1, "owner", "ADMIN", ".open", 101)
        bridge.views[0].enqueue_command(command)
        alias, queued = bridge.get_supervisor_command_nowait()
        self.assertEqual(alias, "room1")
        self.assertEqual(queued, command)
        self.assertTrue(bridge.views[0].commands.empty())

        shutdown = DiscordCommand(1, "owner", "ADMIN", ".shutdown all", 101)
        old_view.enqueue_command(shutdown)
        alias, queued = bridge.get_supervisor_command_nowait()
        self.assertEqual((alias, queued.content), ("room1", ".shutdown all"))

        fresh = bridge.reopen_view("room1")
        self.assertIsNot(fresh, old_view)
        self.assertFalse(fresh._stop.is_set())
        self.assertEqual(bridge.command_targets(".settings", 101), [(fresh, ".settings")])

    def test_room_view_queues_public_status_for_its_own_channel(self):
        configs = load_rooms(self.paths)
        configs[1].update(
            discord_public_status_channel_id="902",
            discord_public_status_channel_name="lobby-2-status",
            discord_public_status_label="Lobby 2",
        )
        bridge = SharedDiscord(configs)
        bridge._thread = Mock()
        self.assertTrue(
            bridge.views[1].update_public_status(
                is_open=True,
                lifecycle="Playing",
                room_id=123,
                player_count=4,
                spectator_count=1,
                sr_min=4.5,
                sr_max=5.5,
                map_label="Artist - Song [Insane] (5.00*)",
                map_url="https://osu.ppy.sh/b/1",
            )
        )
        outbound = SimpleNamespace(channel_id=902, public_status=bridge._take_status(902))
        self.assertEqual(outbound.channel_id, 902)
        self.assertEqual(outbound.public_status.instance, "room2")
        self.assertEqual(outbound.public_status.channel_base_name, "lobby-2-status")
        self.assertEqual(outbound.public_status.player_count, 4)
        self.assertEqual(
            bridge._safe_status_channel_name("🟢・lobby-2-status", False),
            "🔴・lobby-2-status",
        )

    def test_public_status_uses_safe_lazer_join_redirect(self):
        self.assertEqual(
            normalize_public_join_base_url(
                "https://poolrotator-91-107-237-182.nip.io/"
            ),
            "https://poolrotator-91-107-237-182.nip.io",
        )
        for unsafe in (
            "http://example.test",
            "https://user@example.test",
            "https://example.test/path",
            "javascript:alert(1)",
        ):
            self.assertEqual(normalize_public_join_base_url(unsafe), "")
        join_url, web_url = public_room_links(
            123, "https://poolrotator-91-107-237-182.nip.io"
        )
        self.assertEqual(
            join_url,
            "https://poolrotator-91-107-237-182.nip.io/room/123",
        )
        self.assertEqual(web_url, "https://osu.ppy.sh/multiplayer/rooms/123")

        configs = load_rooms(self.paths)
        configs[0].update(
            discord_public_status_channel_id="901",
            discord_public_status_channel_name="lobby-1-status",
            discord_public_status_label="Lobby 1",
        )
        bridge = SharedDiscord(configs)
        bridge._thread = Mock()
        with patch.dict(
            os.environ,
            {
                "POOLROTATOR_PUBLIC_JOIN_BASE_URL":
                    "https://poolrotator-91-107-237-182.nip.io"
            },
        ):
            self.assertTrue(
                bridge.views[0].update_public_status(
                    is_open=True, lifecycle="Waiting", room_id=123,
                )
            )
        status = bridge._take_status(901)
        self.assertEqual(
            status.join_base_url,
            "https://poolrotator-91-107-237-182.nip.io",
        )

    def test_permissions_are_still_role_based(self):
        bridge = SharedDiscord(load_rooms(self.paths))
        member = SimpleNamespace(id=42, guild=SimpleNamespace(owner_id=1), roles=[])
        self.assertIsNone(bridge.views[0]._role_for_member(member))
        member.roles = [SimpleNamespace(name="mods")]
        self.assertEqual(bridge.views[0]._role_for_member(member), "MOD")
        self.assertTrue(bridge.views[0]._role_allows_command("MOD", ".settings"))
        self.assertFalse(bridge.views[0]._role_allows_command("MOD", ".hardrestart"))
        self.assertFalse(bridge.views[0]._role_allows_command("MOD", ".open"))
        self.assertFalse(bridge.views[0]._role_allows_command("MOD", ".say hello"))
        member.roles = [SimpleNamespace(name="Top dev")]
        self.assertEqual(bridge.views[0]._role_for_member(member), "ADMIN")
        self.assertTrue(bridge.views[0]._role_allows_command("ADMIN", ".hardrestart"))
        self.assertTrue(bridge.views[0]._role_allows_command("ADMIN", ".open"))
        self.assertTrue(bridge.views[0]._role_allows_command("ADMIN", ".say hello"))
        member.roles = []
        member.id = 1
        self.assertEqual(bridge.views[0]._role_for_member(member), "ADMIN")

    def test_process_lease_released_and_second_owner_rejected(self):
        path = self.root / "account.lock"
        with ProcessLease(path):
            with self.assertRaises(RuntimeError):
                with ProcessLease(path):
                    self.fail("duplicate acquired")
        with ProcessLease(path):
            pass


class ControllerTests(unittest.TestCase):
    def test_close_when_empty_marks_only_confirmed_worker_for_reopen(self):
        bot = object.__new__(LobbyBot)
        bot._close_when_empty = True
        bot._supervisor_reopenable_stop = False
        bot.players = {}
        bot.room_id = 123
        bot.running = True
        bot.say = Mock()

        self.assertTrue(bot._close_if_armed_and_empty())
        self.assertTrue(bot._supervisor_reopenable_stop)
        self.assertFalse(bot.running)
        bot.say.assert_called_once_with("Lobby is empty — closing.")

    def test_chat_websocket_routes_only_matching_channel_and_adds_username(self):
        stream = ChatWebSocket(lambda: "test-only")
        first, second = queue.Queue(), queue.Queue()
        stream.subscribe(11, first)
        stream.subscribe(12, second)

        stream._route_messages({
            "messages": [
                {"channel_id": 12, "message_id": 3, "sender_id": 7,
                 "content": "second"},
                {"channel_id": 11, "message_id": 2, "sender_id": 7,
                 "content": "first"},
            ],
            "users": [{"id": 7, "username": "Player"}],
        }, generation=4)

        routed_first = first.get_nowait()
        routed_second = second.get_nowait()
        self.assertEqual(routed_first.generation, 4)
        self.assertEqual(routed_first.messages[0]["channel_id"], 11)
        self.assertEqual(routed_second.messages[0]["channel_id"], 12)
        self.assertEqual(routed_first.messages[0]["_sender_username"], "Player")

    def test_chat_websocket_rejects_non_ppy_token_destination(self):
        with self.assertRaises(ValueError):
            ChatWebSocket(
                lambda: "test-only",
                endpoint="wss://example.invalid/collect-token",
            )

    def test_headless_say_never_uses_lazer_or_retries_uncertain_delivery(self):
        bot = object.__new__(LobbyBot)
        bot.cfg = {"_headless_supervised": True}
        bot.channel_id, bot.room_id = 11, 101
        bot._last_sent = 0
        bot.api = SimpleNamespace(chat_send=Mock(return_value=None))
        bot.lazer_coordinator = Mock()
        bot._own_messages = set()
        bot._join_room_chat = Mock()
        bot.say("hello")
        self.assertEqual(bot.api.chat_send.call_count, 1)
        bot.lazer_coordinator.ensure_room.assert_not_called()
        bot._join_room_chat.assert_not_called()

    def test_startup_failure_cleans_own_room_without_stopping_sibling(self):
        api = SimpleNamespace(cancel_pending=threading.Event())
        supervisor = Supervisor([{}], [], api, StubHub(), queue.Queue())
        failed = SimpleNamespace(cfg={"_instance_name": "room1"}, room_id=101,
                                 running=True, run=Mock(side_effect=RuntimeError("test")), shutdown=Mock())
        sibling = SimpleNamespace(running=True)
        supervisor.bots = [failed, sibling]
        supervisor._worker(failed)
        failed.shutdown.assert_called_once()
        self.assertTrue(sibling.running)
        self.assertFalse(supervisor.shared.stopping.is_set())
        self.assertEqual(supervisor.shared.hub.stopped, 0)
        supervisor.request_stop()
        self.assertFalse(sibling.running)
        self.assertTrue(api.cancel_pending.is_set())

    def test_worker_parks_close_when_empty_room_only_after_confirmed_close(self):
        api = SimpleNamespace(cancel_pending=threading.Event())
        supervisor = Supervisor([{"_instance_name": "room1"}], [object()], api,
                                StubHub(), queue.Queue())
        confirmed = SimpleNamespace(
            cfg={"_instance_name": "room1"},
            room_id=None,
            running=False,
            run=Mock(),
            shutdown=Mock(),
            hub=SimpleNamespace(owned=set()),
            _supervisor_reopenable_stop=True,
        )
        supervisor._worker(confirmed)
        self.assertIn("room1", supervisor._reopenable_rooms)

        supervisor._reopenable_rooms.clear()
        uncertain = SimpleNamespace(
            cfg={"_instance_name": "room1"},
            room_id=None,
            running=False,
            run=Mock(),
            shutdown=Mock(),
            hub=SimpleNamespace(owned={999}),
            _supervisor_reopenable_stop=True,
        )
        with self.assertLogs("supervisor", "ERROR"):
            supervisor._worker(uncertain)
        self.assertNotIn("room1", supervisor._reopenable_rooms)

    def test_supervisor_reopens_only_a_confirmed_parked_room(self):
        api = SimpleNamespace(cancel_pending=threading.Event())
        cfg = {"_instance_name": "room1", "command_prefix": "."}
        supervisor = Supervisor([cfg], [object()], api, StubHub(), queue.Queue())
        command = DiscordCommand(1, "owner", "ADMIN", ".open", 101)

        replacement = SimpleNamespace(cfg=cfg)
        supervisor._make_bot = Mock(return_value=replacement)
        supervisor._launch_bot = Mock()

        # Refuse a second controller unless the prior close was positively
        # classified as a safe close-when-empty park.
        self.assertFalse(supervisor._reopen_room("room1", command))
        supervisor._make_bot.assert_not_called()

        supervisor._reopenable_rooms.add("room1")
        old_view = supervisor.discord.view_for("room1")
        old_view.stop()
        self.assertTrue(supervisor._reopen_room("room1", command))
        self.assertNotIn("room1", supervisor._reopenable_rooms)
        self.assertIsNot(supervisor.discord.view_for("room1"), old_view)
        supervisor._make_bot.assert_called_once_with(
            "room1",
            discord_view=supervisor.discord.view_for("room1"),
            fresh_pool=True,
        )
        supervisor._launch_bot.assert_called_once_with(replacement)


class ProductionControllerTests(unittest.TestCase):
    """Exercise the real LobbyBot lifecycle with HTTP/hub operations mocked."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        paths = []
        for i in (1, 2):
            path = root / f"room{i}.json"
            path.write_text(json.dumps({
                "ai_enabled": False, "discord_control_enabled": False,
                "discord_modlog_enabled": False, "admins": [1],
                "ratings_db": str(root / "ratings.db"),
                "process_control_db": str(root / "control.db"),
                "lazer_coordinator_db": str(root / "lazer.db"),
                "hard_restart_interval_seconds": 0,
            }))
            paths.append(path)
        (root / "universal.json").write_text("{}", encoding="utf-8")
        self.configs = load_rooms(paths)
        self.hub = StubHub()
        self.shared = SharedReferee(self.hub, queue.Queue())
        self.bridge = SharedDiscord(self.configs)
        self.api = SimpleNamespace(chat_join=Mock(return_value={}),
                                   chat_send=Mock(return_value={"message_id": 9000}),
                                   chat_ack=Mock(), chat_messages=Mock(return_value=[]),
                                   room_probe=Mock(),
                                   configure_rate_limit=Mock(),
                                   configure_profile_cache=Mock())
        self.bots = []
        for cfg, dc in zip(self.configs, self.bridge.views):
            cfg["_authenticated_user_id"] = 1
            events = queue.Queue()
            view = self.shared.room_view(cfg["_instance_name"], events)
            pool = SimpleNamespace(max_length=180, sr_min=4, sr_max=6, maps=[],
                                   limit=120, min_passcount=1000,
                                   max_map_age_label=lambda: "off")
            bot = LobbyBot(view, self.api, pool, cfg, events, discord_control=dc)
            self.addCleanup(bot.shutdown)
            bot._next_downloadable_map = Mock(return_value=SimpleNamespace(beatmap_id=1))
            bot.apply_map = Mock()
            bot.fill_queue = Mock()
            bot.create_room()
            self.bots.append(bot)

    def test_websocket_is_primary_with_reconcile_dedup_and_http_fallback(self):
        bot = self.bots[0]
        socket = SimpleNamespace(
            enabled=True,
            connected=True,
            subscribe=Mock(),
            unsubscribe=Mock(),
        )
        bot.chat_websocket = socket
        bot._attach_chat_websocket()
        socket.subscribe.assert_called_once_with(
            bot.channel_id, bot._chat_websocket_inbox
        )

        # Initial room/socket handoff performs exactly one history recovery.
        bot.poll_chat()
        self.api.chat_messages.assert_called_once()
        self.api.chat_messages.reset_mock()

        bot.handle_command = Mock()
        message = {
            "channel_id": bot.channel_id,
            "message_id": 77,
            "sender_id": 7,
            "content": ".help",
            "timestamp": "2026-09-11T00:00:00Z",
            "_sender_username": "Player",
        }
        bot._chat_websocket_inbox.put_nowait(
            ChatSocketEvent(
                "messages", 1, (message,), channel_id=bot.channel_id
            )
        )
        bot.poll_chat()
        self.api.chat_messages.assert_not_called()
        bot.handle_command.assert_called_once()

        # A periodic/reconnect reconciliation may overlap live delivery.
        # message_id deduplication prevents the command executing twice.
        bot._chat_websocket_inbox.put_nowait(
            ChatSocketEvent(
                "messages", 1, (message,), channel_id=bot.channel_id
            )
        )
        bot.poll_chat()
        bot.handle_command.assert_called_once()

        stale = dict(message, message_id=78)
        bot._chat_websocket_inbox.put_nowait(
            ChatSocketEvent(
                "messages", 1, (stale,), channel_id=bot.channel_id + 999
            )
        )
        bot.poll_chat()
        bot.handle_command.assert_called_once()

        socket.connected = False
        bot._last_chat_poll = 0
        self.api.chat_messages.return_value = []
        bot.poll_chat()
        self.api.chat_messages.assert_called_once()

    def test_real_hard_restart_and_shutdown_are_room_scoped(self):
        a, b = self.bots
        old, other = a.room_id, b.room_id
        self.assertTrue(a.hard_restart_room(announce=False))
        self.assertNotEqual(a.room_id, old)
        self.assertEqual(b.room_id, other)
        a.shutdown()
        self.assertTrue(b.running)
        self.assertFalse(self.bridge._stop.is_set())
        self.assertEqual(self.hub.stopped, 0)
        b.say("still here")
        b._last_sent = 0
        b._flush_room_chat()
        self.assertEqual(self.api.chat_send.call_args.args[0], b.channel_id)

    def test_fixed_difficulty_vote_shifts_range_and_owner_leave_resets_it(self):
        fixed = self.bots[0]
        fixed.manual_range = True
        fixed.cfg.update(
            auto_difficulty=False,
            sr_min=4.0,
            sr_max=6.0,
            max_length=180,
            difficulty_vote_step=0.20,
            difficulty_vote_max_steps=3,
        )
        fixed.pool.sr_min, fixed.pool.sr_max = 4.0, 6.0

        def set_window(lo, hi, length):
            fixed.pool.sr_min, fixed.pool.sr_max = lo, hi
            fixed.pool.max_length = length
            return 50

        fixed.pool.set_window = Mock(side_effect=set_window)
        fixed.players = {7: {"name": "voter", "status": "idle"}}
        fixed.say = Mock()

        fixed.cast_difficulty_vote(7, "harder")

        self.assertTrue(fixed.manual_range)
        self.assertFalse(fixed.cfg["auto_difficulty"])
        self.assertEqual((fixed.pool.sr_min, fixed.pool.sr_max), (4.2, 6.2))
        self.assertIn("fixed range 4.20-6.20", fixed.say.call_args.args[0])
        self.assertNotIn("https://", fixed.say.call_args.args[0])

        # Another player remains, so removing the voter restores the JSON
        # range and queues a quiet playlist rebuild after the active map.
        fixed.players[8] = {"name": "remaining", "status": "idle"}
        fixed.players.pop(7)
        fixed.state = "PLAYING"
        fixed._on_difficulty_voter_left(7)
        self.assertEqual((fixed.pool.sr_min, fixed.pool.sr_max), (4.0, 6.0))
        self.assertTrue(fixed._fixed_range_rebuild_pending)
        self.assertFalse(fixed._difficulty_bias_layers)

        fixed.state = "IDLE"
        fixed.perform_soft_restart = Mock(return_value=True)
        self.assertTrue(fixed._run_pending_fixed_range_rebuild())
        fixed.perform_soft_restart.assert_called_once_with(announce=False)
        self.assertFalse(fixed._fixed_range_rebuild_pending)

    def test_fixed_group_vote_lasts_until_its_final_supporter_leaves(self):
        fixed = self.bots[0]
        fixed.manual_range = True
        fixed.cfg.update(
            auto_difficulty=False,
            sr_min=4.0,
            sr_max=6.0,
            max_length=180,
            difficulty_vote_ratio=0.50,
            difficulty_vote_step=0.20,
            difficulty_vote_max_steps=3,
        )
        fixed.pool.sr_min, fixed.pool.sr_max = 4.0, 6.0

        def set_window(lo, hi, length):
            fixed.pool.sr_min, fixed.pool.sr_max = lo, hi
            fixed.pool.max_length = length
            return 50

        fixed.pool.set_window = Mock(side_effect=set_window)
        fixed.players = {
            7: {"name": "first", "status": "idle"},
            8: {"name": "second", "status": "idle"},
        }
        fixed.say = Mock()

        fixed.cast_difficulty_vote(7, "harder")
        fixed.cast_difficulty_vote(8, "harder")
        self.assertEqual((fixed.pool.sr_min, fixed.pool.sr_max), (4.2, 6.2))

        fixed.players.pop(7)
        fixed._on_difficulty_voter_left(7)
        self.assertEqual((fixed.pool.sr_min, fixed.pool.sr_max), (4.2, 6.2))
        self.assertEqual(len(fixed._difficulty_bias_layers), 1)

        fixed.players.pop(8)
        fixed._on_difficulty_voter_left(8)
        self.assertEqual((fixed.pool.sr_min, fixed.pool.sr_max), (4.0, 6.0))
        self.assertFalse(fixed._difficulty_bias_layers)
        self.assertTrue(fixed._fixed_range_rebuild_pending)
        fixed.perform_soft_restart = Mock(return_value=True)
        self.assertFalse(fixed._run_pending_fixed_range_rebuild())
        fixed.perform_soft_restart.assert_not_called()

    def test_all_automatic_bot_lines_reach_own_discord_channel(self):
        self.bridge._thread = Mock()
        texts = ["Welcome, player!", "Thanks for the feedback!", "Next: a map",
                 "While we count down, rate the last map with .good or .bad",
                 "Discord: https://discord.gg/example", "Match aborted."]
        for i, bot in enumerate(self.bots, 1):
            bot.cfg["discord_control_channel_id"] = 100 + i
            for text in texts:
                bot._last_sent = 0
                bot.say(text, automatic=True)
                outgoing = self.bridge.outbox.get_nowait()
                self.assertEqual(outgoing.channel_id, 100 + i)
                self.assertIn(text, outgoing.content)
        self.assertTrue(self.bridge.outbox.empty())

    def test_public_status_snapshot_updates_only_when_visible_state_changes(self):
        bot = self.bots[0]
        self.bridge._thread = Mock()
        bot.cfg.update(
            discord_public_status_channel_id=901,
            discord_public_status_channel_name="lobby-1-status",
            discord_public_status_label="Lobby 1",
        )
        bot.current_map = SimpleNamespace(
            label="Artist - Song [Insane] (5.00*)",
            url="https://osu.ppy.sh/b/1",
        )
        bot.players = {
            7: {"name": "Player", "status": "idle"},
            8: {"name": "Watcher", "status": "spectating"},
        }

        self.assertTrue(bot._update_discord_public_status())
        first = SimpleNamespace(public_status=self.bridge._take_status(901))
        self.assertEqual(first.public_status.player_count, 1)
        self.assertEqual(first.public_status.spectator_count, 1)
        self.assertFalse(bot._update_discord_public_status())
        self.assertTrue(self.bridge.outbox.empty())

        bot.state = "PLAYING"
        bot.playing_map = bot.current_map
        self.assertTrue(bot._update_discord_public_status())
        changed = SimpleNamespace(public_status=self.bridge._take_status(901))
        self.assertEqual(changed.public_status.lifecycle, "Playing")

    def test_discord_reply_stays_in_discord_but_automatic_side_effect_goes_to_lobby(self):
        bot = self.bots[0]
        self.bridge._thread = Mock()
        bot.cfg["discord_control_channel_id"] = 101
        bot._discord_command_reply_channel_id = 102
        self.api.chat_send.reset_mock()
        bot.say("Settings result")
        self.api.chat_send.assert_not_called()
        self.assertEqual(self.bridge.outbox.get_nowait().channel_id, 102)
        bot._last_sent = 0
        bot.say("Welcome, player!", automatic=True)
        self.api.chat_send.assert_called_once()
        self.assertEqual(self.bridge.outbox.get_nowait().channel_id, 101)

    def test_failed_discord_say_returns_delivery_notice_to_operator(self):
        bot=self.bots[0]
        self.bridge._thread=Mock()
        bot._discord_command_reply_channel_id=101
        self.api.chat_send.return_value=None
        bot.say("relay",lobby_only=True)
        bot._last_sent = 0
        bot._flush_room_chat()
        outgoing=self.bridge.outbox.get_nowait()
        self.assertEqual(outgoing.channel_id,101)
        self.assertIn("could not be confirmed",outgoing.content)

    def test_public_help_links_to_reference_instead_of_listing_every_command(self):
        bot = self.bots[0]
        bot.say = Mock()
        # This matrix repeats help past the per-player flood limit; command
        # throttling has its own coverage and should not hide formatting cases.
        bot._allow_public_command_burst = Mock(return_value=True)
        for prefix in (".", "!"):
            bot.prefix = prefix
            for loved in (False, True):
                bot.pool.loved_mode = loved
                for command in ("help", "commands"):
                    with self.subTest(prefix=prefix, loved=loved, command=command):
                        bot.say.reset_mock()
                        bot.handle_command(7, prefix + command)
                        bot.say.assert_called_once()
                        text = bot.say.call_args.args[0]
                        self.assertLess(len(text), 130)
                        self.assertIn(prefix + "queue", text)
                        self.assertIn(prefix + "skip", text)
                        self.assertIn("https://poolrotater.com/commands", text)
                        for omitted in ("ai", "discord", "farm", "pool", "pp", "stats"):
                            self.assertNotIn(prefix + omitted, text)

    def test_command_help_on_standard_room(self):
        bot = self.bots[0]
        bot.say = Mock()
        bot.handle_command(7, ".help .s")
        self.assertIn(".skip", bot.say.call_args.args[0])
        bot.handle_command(7, ".help jack")
        self.assertIn("Mania lobbies", bot.say.call_args.args[0])
        bot.handle_command(7, ".help anything")
        self.assertIn("Unknown command", bot.say.call_args.args[0])

    def test_mid_match_join_gets_one_short_orientation_message(self):
        bot = self.bots[0]
        bot.state = "PLAYING"
        bot.playing_map = SimpleNamespace(
            label="Artist - Map [Insane] (5.00*)", hit_length=120
        )
        bot._match_started_at = time.time() - 30
        bot.players = {7: {"name": "NewPlayer", "status": "idle"}}
        bot.say = Mock()

        self.assertTrue(bot._send_mid_match_join_intro(7))
        message = bot.say.call_args.args[0]
        self.assertIn("you're in next", message)
        self.assertIn("NewPlayer", message)
        self.assertIn(".time for remaining.", message)
        self.assertIn(".help", message)
        self.assertIn("1m", message)
        self.assertFalse(bot._send_mid_match_join_intro(7))
        bot.say.assert_called_once()

    def test_wait_stacks_once_per_player_and_remains_available_during_countdown(self):
        bot = self.bots[0]
        bot.state = "IDLE"
        bot.current_item_id = 123
        bot.players = {
            7: {"name": "One", "status": "idle"},
            8: {"name": "Two", "status": "idle"},
        }
        bot.say = Mock()

        bot._request_countdown_wait(7)
        bot._request_countdown_wait(7)
        bot._request_countdown_wait(8)
        self.assertEqual(bot._countdown_wait_requesters, {7, 8})
        self.assertEqual(bot._countdown_wait_delay_seconds, 20.0)
        self.assertEqual(bot.say.call_count, 2)

        bot._lock_countdown_wait_for_start()
        self.assertEqual(bot._countdown_wait_delay_seconds, 0.0)

        # Earlier requesters cannot add time twice, but a player who has not
        # used .wait on this map may cancel the active countdown once.
        bot.players[9] = {"name": "Three", "status": "idle"}
        bot.state = "STARTING"
        bot.starting_map = bot.current_map
        bot.starting_item_id = bot.current_item_id
        bot.ready = {7, 8, 9}
        bot._request_countdown_wait(7)
        bot._request_countdown_wait(9)
        self.assertEqual(bot.state, "IDLE")
        self.assertEqual(bot._countdown_wait_requesters, {7, 8, 9})
        self.assertEqual(bot._countdown_wait_delay_seconds, 10.0)
        self.assertEqual(bot.ready, {7, 8, 9})
        self.assertEqual(self.hub.calls[-1][0], "StopMatchCountdown")
        self.assertIn("Countdown cancelled", bot.say.call_args.args[0])
        self.assertEqual(bot.say.call_count, 3)

        # The cancellation grace begins immediately; it must not charge the
        # normal auto-start interval for a second time.
        self.assertAlmostEqual(
            time.time() - bot._idle_since,
            float(bot.cfg.get("auto_start_seconds", 45)),
            delta=1.0,
        )

        # Lazer's EditCurrentPlaylistItem keeps the same item id on an idle
        # skip. A different beatmap in that item is still a new map and must
        # get a fresh per-player .wait allowance.
        old_beatmap_id = int(bot.current_map.beatmap_id)
        bot.current_map = SimpleNamespace(beatmap_id=old_beatmap_id + 1)
        bot._request_countdown_wait(7)
        self.assertEqual(bot._countdown_wait_requesters, {7})
        self.assertEqual(bot._countdown_wait_delay_seconds, 10.0)
        self.assertEqual(bot.say.call_count, 4)

    def test_start_and_player_frustration_suppress_wait_for_exact_map(self):
        bot = self.bots[0]
        bot.state = "IDLE"
        bot.current_item_id = 321
        bot.players = {
            7: {"name": "One", "status": "idle"},
            8: {"name": "Two", "status": "idle"},
        }
        bot.say = Mock()

        bot._request_countdown_wait(7)
        self.assertEqual(bot._countdown_wait_delay_seconds, 10.0)
        bot.state = "STARTING"
        bot._start_from_command(8, privileged=False)
        self.assertEqual(bot._countdown_wait_delay_seconds, 0.0)
        bot._request_countdown_wait(8)
        self.assertEqual(bot.say.call_count, 1)

        # A new beatmap in the same lazer item resets the map-scoped lock.
        old_beatmap_id = int(bot.current_map.beatmap_id)
        bot.state = "IDLE"
        bot.current_map = SimpleNamespace(beatmap_id=old_beatmap_id + 1)
        bot._request_countdown_wait(7)
        self.assertEqual(bot._countdown_wait_delay_seconds, 10.0)

        bot._process_chat_messages([{
            "message_id": 123456,
            "sender_id": 8,
            "content": "start already please",
            "_sender_username": "Two",
        }])
        self.assertEqual(bot._countdown_wait_delay_seconds, 0.0)
        bot._request_countdown_wait(8)
        self.assertEqual(bot.say.call_count, 2)

    def test_spectator_frustration_does_not_disable_player_wait(self):
        bot = self.bots[0]
        bot.state = "IDLE"
        bot.current_item_id = 456
        bot.players = {
            7: {"name": "Player", "status": "idle"},
            8: {"name": "Spectator", "status": "spectating"},
        }
        bot.say = Mock()

        bot._process_chat_messages([{
            "message_id": 123457,
            "sender_id": 8,
            "content": "hurry up",
            "_sender_username": "Spectator",
        }])
        bot._request_countdown_wait(7)
        self.assertEqual(bot._countdown_wait_delay_seconds, 10.0)
        bot.say.assert_called_once()

    def test_wait_does_not_leave_starting_when_countdown_stop_fails(self):
        bot = self.bots[0]
        bot.state = "STARTING"
        bot.current_item_id = 654
        bot.starting_item_id = 654
        bot.starting_map = bot.current_map
        bot.players = {7: {"name": "Player", "status": "idle"}}
        bot.ready = {7}
        bot.say = Mock()
        bot._stop_match_countdown = Mock(return_value=False)

        bot._request_countdown_wait(7)

        self.assertEqual(bot.state, "STARTING")
        self.assertEqual(bot.starting_item_id, 654)
        self.assertEqual(bot.ready, {7})
        self.assertEqual(bot._countdown_wait_requesters, set())
        self.assertEqual(bot._countdown_wait_delay_seconds, 0.0)
        bot.say.assert_not_called()

    def test_active_chat_extension_ignores_start_pressure(self):
        bot = self.bots[0]
        now = time.time()
        bot._recent_human_chat.clear()
        bot._recent_human_chat.extend([
            (now - 1, "One", "nice map"),
            (now - 2, "Two", "this is fun"),
            (now - 3, "One", "good luck"),
            (now - 4, "Two", "lets go"),
            (now - 5, "One", "start already"),
        ])
        self.assertEqual(bot._active_chat_countdown_extension(), 5.0)

        bot._recent_human_chat.clear()
        bot._recent_human_chat.extend([
            (now - 1, "One", "start already"),
            (now - 2, "Two", "too slow"),
            (now - 3, "One", "hurry up"),
            (now - 4, "Two", "just start"),
        ])
        self.assertEqual(bot._active_chat_countdown_extension(), 0.0)

    def test_start_match_sends_integer_after_chat_extension(self):
        bot = self.bots[0]
        bot.state = "IDLE"
        bot.players = {7: {"name": "Player", "status": "idle"}}
        bot.current_map = SimpleNamespace(beatmap_id=1)
        bot.reconcile_playlist = Mock(return_value=True)
        bot._map_is_downloadable = Mock(return_value=True)
        bot.apply_pending_room_name_update = Mock()
        bot._active_chat_countdown_extension = Mock(return_value=5.0)

        bot.start_match()

        method, args = self.hub.calls[-1]
        self.assertEqual(method, "StartMatch")
        self.assertEqual(args[1]["countdown"], 20)
        self.assertIsInstance(args[1]["countdown"], int)
        self.assertEqual(bot._active_countdown_seconds, 20.0)

    def prepare_readiness_start(self, players=None, ready=None):
        bot = self.bots[0]
        bot.state = "IDLE"
        bot.players = players or {7: {"status": "ready"}, 8: {"status": "idle"}}
        bot.ready = {7} if ready is None else set(ready)
        bot.current_map = SimpleNamespace(beatmap_id=1)
        bot.reconcile_playlist = Mock(return_value=True)
        bot._map_is_downloadable = Mock(return_value=True)
        bot.apply_pending_room_name_update = Mock()
        bot._active_chat_countdown_extension = Mock(return_value=0)
        bot.say = Mock()
        return bot

    def test_one_ready_player_starts_with_afk_and_gets_only_first_grace(self):
        bot = self.prepare_readiness_start()
        self.assertTrue(bot.ready_threshold_met())
        bot.start_match()
        self.assertEqual(self.hub.calls[-1][1][1]["countdown"], 20)
        bot.set_state("IDLE")
        bot.current_map = SimpleNamespace(beatmap_id=2)
        bot.start_match()
        self.assertEqual(self.hub.calls[-1][1][1]["countdown"], 15)

    def test_several_unready_players_share_five_seconds(self):
        bot = self.prepare_readiness_start()
        bot.players.update({9: {"status": "idle"}, 10: {"status": "idle"}})
        bot.start_match()
        self.assertEqual(self.hub.calls[-1][1][1]["countdown"], 20)
        self.assertEqual(bot._unready_start_grace_users, {8, 9, 10})

    def test_all_ready_players_need_no_grace(self):
        bot = self.prepare_readiness_start(ready={7, 8})
        bot.start_match()
        self.assertEqual(self.hub.calls[-1][1][1]["countdown"], 15)
        self.assertEqual(bot._unready_start_grace_users, set())

    def test_zero_ready_multiplayer_never_sends_start(self):
        bot = self.prepare_readiness_start(ready=set())
        before = len(self.hub.calls)
        self.assertFalse(bot.ready_threshold_met())
        bot.start_match()
        self.assertEqual(len(self.hub.calls), before)
        self.assertEqual(bot.state, "IDLE")
        bot.say.assert_called_once()

    def test_public_and_privileged_start_hold_when_none_ready(self):
        for privileged in (False, True):
            bot = self.prepare_readiness_start(ready=set())
            bot.start_match = Mock()
            bot._suppress_countdown_wait = Mock()
            bot._start_from_command(7, privileged=privileged)
            bot.start_match.assert_not_called()
            bot._suppress_countdown_wait.assert_not_called()

    def test_manual_start_ignores_afk_but_includes_first_grace(self):
        bot = self.prepare_readiness_start()
        bot._start_from_command(7, privileged=False)
        self.assertEqual(bot.state, "STARTING")
        self.assertEqual(self.hub.calls[-1][1][1]["countdown"], 20)

    def test_spectators_do_not_trigger_multiplayer_grace(self):
        bot = self.prepare_readiness_start(players={7: {"status": "ready"},
                                                   8: {"status": "spectating"}})
        bot.start_match()
        self.assertEqual(self.hub.calls[-1][1][1]["countdown"], 15)

    def test_failed_preflight_does_not_spend_grace(self):
        bot = self.prepare_readiness_start()
        bot.reconcile_playlist.return_value = False
        bot.start_match()
        self.assertEqual(bot._unready_start_grace_users, set())
        bot.reconcile_playlist.return_value = True
        bot.start_match()
        self.assertEqual(self.hub.calls[-1][1][1]["countdown"], 20)

    def test_definite_start_rejection_does_not_spend_grace(self):
        bot = self.prepare_readiness_start()
        bot._referee_operation_with_rejoin = Mock(side_effect=ValueError("rejected"))
        with self.assertRaises(ValueError):
            bot.start_match()
        self.assertEqual(bot._unready_start_grace_users, set())
        self.assertEqual(bot.state, "IDLE")

    def test_new_unready_joiner_gets_grace_after_previous_player(self):
        bot = self.prepare_readiness_start()
        bot.start_match()
        bot.set_state("IDLE")
        bot.players[9] = {"status": "idle"}
        bot.start_match()
        self.assertEqual(self.hub.calls[-1][1][1]["countdown"], 20)
        self.assertEqual(bot._unready_start_grace_users, {8, 9})

    def prepare_empty_reset(self):
        bot = self.bots[0]
        bot.cfg.update(sr_min=4.5, sr_max=5.9, max_length=180,
                       auto_difficulty=True, auto_empty_reset_seconds=300)
        bot.manual_range = False
        bot.state = "IDLE"
        bot.players = {}
        bot.say = Mock()
        bot.reconcile_playlist = Mock()
        def set_window(lo, hi, length):
            bot.pool.sr_min, bot.pool.sr_max, bot.pool.max_length = lo, hi, length
            return 10
        bot.pool.set_window = Mock(side_effect=set_window)
        bot.pool.reset_session_pool = Mock(side_effect=set_window)
        bot.pool.source_mode_label = lambda: "variety 50/50"
        bot.pool.sr_min, bot.pool.sr_max = 7.0, 8.0
        bot._empty_since = 100.0
        return bot

    def test_empty_reset_at_five_minutes_rebuilds_once_without_closing(self):
        bot = self.prepare_empty_reset()
        room_id = bot.room_id
        bot._difficulty_bias_layers = [{"amount": 0.2, "supporters": set()}]
        with patch("bot.bot.time.monotonic", return_value=399):
            self.assertFalse(bot._maybe_reset_empty_autodiff())
            bot.pool.reset_session_pool.assert_not_called()
        with patch("bot.bot.time.monotonic", return_value=400):
            self.assertTrue(bot._maybe_reset_empty_autodiff())
            self.assertFalse(bot._maybe_reset_empty_autodiff())
        bot.pool.reset_session_pool.assert_called_once_with(4.5, 5.9, 180)
        self.assertEqual(bot.room_id, room_id)
        self.assertIn("(4.50-5.90*)", bot._current_room_name)
        self.assertFalse(bot._difficulty_bias_layers)
        self.assertTrue(bot.cfg["auto_difficulty"])
        self.assertFalse(bot.manual_range)
        bot.fill_queue.assert_called()

    def test_empty_timer_resets_on_rejoin_and_spectators_prevent_reset(self):
        bot = self.prepare_empty_reset()
        bot.players = {7: {"name": "spectator", "status": "spectating"}}
        with patch("bot.bot.time.monotonic", return_value=400):
            self.assertFalse(bot._maybe_reset_empty_autodiff())
            self.assertIsNone(bot._empty_since)
            bot.players.clear()
            self.assertFalse(bot._maybe_reset_empty_autodiff())
        with patch("bot.bot.time.monotonic", return_value=699):
            self.assertFalse(bot._maybe_reset_empty_autodiff())
        with patch("bot.bot.time.monotonic", return_value=700):
            self.assertTrue(bot._maybe_reset_empty_autodiff())

    def test_empty_reset_waits_for_idle_and_includes_unchanged_fixed_room(self):
        bot = self.prepare_empty_reset()
        with patch("bot.bot.time.monotonic", return_value=500):
            for state in ("PLAYING", "STARTING", "SCORING"):
                bot.state = state
                self.assertFalse(bot._maybe_reset_empty_autodiff())
            bot.state = "IDLE"
            bot.pool.reset_session_pool.assert_not_called()
            bot.manual_range = True
            bot.cfg["auto_difficulty"] = False
            self.assertTrue(bot._maybe_reset_empty_autodiff())
            self.assertFalse(bot._maybe_reset_empty_autodiff())
        bot.pool.reset_session_pool.assert_called_once_with(4.5, 5.9, 180)

    def test_empty_reset_uses_hotloaded_json_baseline_and_backs_off_on_failure(self):
        bot = self.prepare_empty_reset()
        bot._apply_live_config_from_disk({"sr_min": 3.5, "sr_max": 4.5,
                                          "auto_empty_reset_seconds": 60})
        self.assertEqual(bot.pool.sr_min, 7.0)
        bot.pool.reset_session_pool = Mock(return_value=0)
        with patch("bot.bot.time.monotonic", return_value=200):
            self.assertFalse(bot._maybe_reset_empty_autodiff())
            self.assertFalse(bot._maybe_reset_empty_autodiff())
        bot.pool.reset_session_pool.assert_called_once_with(3.5, 4.5, 180)
        bot.cfg["auto_empty_reset_seconds"] = 0
        with patch("bot.bot.time.monotonic", return_value=600):
            self.assertFalse(bot._maybe_reset_empty_autodiff())

    def test_empty_fixed_room_uses_same_delay_then_restores_json_range(self):
        bot = self.prepare_empty_reset()
        bot.manual_range = True
        bot.cfg["auto_difficulty"] = False
        bot.pool.sr_min, bot.pool.sr_max = 4.7, 6.1
        bot._difficulty_bias_layers = [{"amount": 0.2, "supporters": set()}]
        bot._fixed_difficulty_dirty = True

        with patch("bot.bot.time.monotonic", return_value=399):
            self.assertFalse(bot._maybe_reset_empty_autodiff())
        with patch("bot.bot.time.monotonic", return_value=400):
            self.assertTrue(bot._maybe_reset_empty_autodiff())

        bot.pool.reset_session_pool.assert_called_once_with(4.5, 5.9, 180)
        self.assertEqual((bot.pool.sr_min, bot.pool.sr_max), (4.5, 5.9))
        self.assertFalse(bot._difficulty_bias_layers)
        self.assertFalse(bot._fixed_difficulty_dirty)
        self.assertIn("Fixed difficulty restored", bot.say.call_args.args[0])

    def test_real_transport_recovery_rejoins_both_without_new_rooms(self):
        self.hub._connect_generation += 1
        for bot in self.bots:
            self.api.room_probe.return_value = ("ok", {"room_id": bot.room_id})
            bot.reconcile_playlist = Mock()
            bot._recover_hub_transport("offline reconnect simulation")
        self.assertEqual(len([c for c in self.hub.calls if c[0] == "MakeRoom"]), 2)
        self.assertEqual(len([c for c in self.hub.calls if c[0] == "JoinRoom"]), 2)

    def test_real_hot_reload_keeps_gui_disabled_and_allows_vote_step(self):
        bot = self.bots[0]
        data = json.loads(bot._config_path.read_text())
        data.update(lazer_coordinator_enabled=True, lazer_chat_patrol_enabled=True,
                    difficulty_vote_step=0.35)
        bot._config_path.write_text(json.dumps(data))
        bot._config_mtime_ns = None
        bot._reload_config_from_disk()
        self.assertFalse(bot.cfg["lazer_coordinator_enabled"])
        self.assertFalse(bot.cfg["lazer_chat_patrol_enabled"])
        self.assertEqual(bot.cfg["difficulty_vote_step"], 0.35)

    def test_real_discord_admin_relay_does_not_fetch_using_old_patrol_cursor(self):
        bot = self.bots[0]
        bot._discord_control_actors[-42] = {"name": "staff", "role": "ADMIN"}
        bot._discord_command_reply_channel_id = 101
        bot._fetch_chat_patrol_batch = Mock()
        bot._last_sent = 0
        bot.handle_command(-42, ".say hello lobby")
        bot._fetch_chat_patrol_batch.assert_not_called()
        self.assertEqual(self.api.chat_send.call_args.args[:2], (bot.channel_id, "hello lobby"))

    def test_discord_mod_cannot_relay_lobby_chat(self):
        bot = self.bots[0]
        bot._discord_control_actors[-42] = {"name": "staff", "role": "MOD"}
        bot._discord_command_reply_channel_id = 101
        self.api.chat_send.reset_mock()

        bot.handle_command(-42, ".say hello lobby")

        self.api.chat_send.assert_not_called()

    def test_public_command_flood_allows_burst_then_silently_blocks(self):
        bot = self.bots[0]
        bot.cfg.update(
            command_spam_burst=3,
            command_spam_window_seconds=10.0,
            command_spam_block_seconds=20.0,
        )
        bot.say = Mock()

        for _ in range(4):
            bot.handle_command(777, ".discord")

        self.assertEqual(bot.say.call_count, 3)
        self.assertGreater(bot._command_spam_block_until.get(777, 0.0), 0.0)

    def test_privileged_commands_are_not_public_spam_throttled(self):
        bot = self.bots[0]
        bot.cfg.update(
            command_spam_burst=1,
            command_spam_window_seconds=60.0,
            command_spam_block_seconds=60.0,
        )
        bot._discord_control_actors[-42] = {"name": "staff", "role": "MOD"}
        bot._discord_command_reply_channel_id = 101
        bot.say = Mock()

        bot.handle_command(-42, ".settings")
        bot.handle_command(-42, ".settings")

        self.assertGreaterEqual(bot.say.call_count, 2)

    def test_discord_who_lists_live_players_without_api_calls(self):
        bot = self.bots[0]
        bot._discord_control_actors[-42] = {"name": "staff", "role": "MOD"}
        bot._discord_command_reply_channel_id = 101
        bot.players = {
            7: {"name": "Zayoh", "status": "ready"},
            8: {"name": "Frogma", "status": "idle"},
            9: {"name": "Watcher", "status": "spectating"},
            1: {"name": "operator", "status": "idle"},
        }
        bot.say = Mock()
        self.api.configure_profile_cache.reset_mock()

        bot.handle_command(-42, ".who")

        report = bot.say.call_args.args[0]
        self.assertIn("Lobby 1 · 2 players", report)
        self.assertIn("Frogma, Zayoh", report)
        self.assertIn("Spectating · 1", report)
        self.assertIn("Watcher", report)
        self.assertNotIn("operator", report)
        self.api.configure_profile_cache.assert_not_called()

    def test_shared_set_writes_universal_and_second_room_hotloads(self):
        first, second = self.bots
        first.handle_admin_set(["set", "chatpoll", "4"])
        first.handle_admin_set(["set", "playingchatpoll", "10"])
        first.handle_admin_set(["set", "adaptivepoll", "on"])
        first.handle_admin_set(["set", "apilimit", "60"])
        first.handle_admin_set(["set", "apiburst", "90"])
        saved = json.loads(first._universal_config_path.read_text())
        self.assertEqual(saved["chat_poll_seconds"], 4.0)
        self.assertEqual(saved["requests_burst_capacity"], 90)
        second._last_config_check = 0
        second._reload_config_from_disk()
        self.assertEqual(second.cfg["chat_poll_seconds"], 4.0)
        self.assertEqual(second.cfg["chat_poll_playing_seconds"], 10.0)
        self.assertTrue(second.cfg["adaptive_chat_polling"])
        self.api.configure_rate_limit.assert_called_with(60, 90)

    def test_universal_playlist_audit_interval_hotloads_and_reverts(self):
        first, second = self.bots
        universal_path = first._universal_config_path
        def write_with_distinct_mtime(value):
            previous = universal_path.stat().st_mtime_ns
            universal_path.write_text(json.dumps(value))
            current = universal_path.stat().st_mtime_ns
            changed = max(current, previous + 1)
            os.utime(universal_path, ns=(changed, changed))

        saved = json.loads(universal_path.read_text())
        saved["playlist_audit_seconds"] = 30.0
        write_with_distinct_mtime(saved)

        second._last_config_check = 0
        second._reload_config_from_disk()
        self.assertEqual(second.cfg["playlist_audit_seconds"], 30.0)

        saved.pop("playlist_audit_seconds")
        write_with_distinct_mtime(saved)
        second._last_config_check = 0
        second._reload_config_from_disk()
        self.assertEqual(second.cfg["playlist_audit_seconds"], 8.0)

    def test_adaptive_polling_uses_fast_waiting_and_slower_playing_values(self):
        bot = self.bots[0]
        bot.cfg.update(chat_poll_seconds=5.0, chat_poll_playing_seconds=10.0,
                       adaptive_chat_polling=True)
        bot.state = "IDLE"
        self.assertEqual(bot._chat_poll_interval(), 5.0)
        bot.state = "PLAYING"
        self.assertEqual(bot._chat_poll_interval(), 10.0)
        bot.cfg["adaptive_chat_polling"] = False
        self.assertEqual(bot._chat_poll_interval(), 5.0)

    def test_explicit_download_availability_is_shared_persistently(self):
        first, second = self.bots
        metadata = {"beatmapset_id": 50, "beatmapset": {
            "id": 50, "availability": {"download_disabled": False},
            "beatmaps": [{"id": 700}, {"id": 701}],
        }}
        first.api.beatmap = Mock(return_value=metadata)
        first.api.beatmapset = Mock()
        candidate = SimpleNamespace(beatmap_id=700)
        self.assertTrue(first._map_is_downloadable(candidate))
        first.api.beatmap.assert_called_once()
        second.api.beatmap = Mock()
        second.api.beatmapset = Mock()
        self.assertTrue(second._map_is_downloadable(candidate, force=True))
        second.api.beatmap.assert_not_called()

    def test_download_cache_error_uses_api_and_still_rejects_disabled_set(self):
        bot = self.bots[0]
        bot.ratings.cached_download_availability = Mock(
            side_effect=sqlite3.OperationalError('unable to open database file'))
        bot.ratings.remember_download_availability = Mock(
            side_effect=sqlite3.OperationalError('unable to open database file'))
        bot.pool.exclude_beatmaps = Mock()
        bot.api.beatmap = Mock(return_value={'beatmapset_id': 50, 'beatmapset': {
            'id': 50, 'availability': {'download_disabled': True},
            'beatmaps': [{'id': 700}, {'id': 701}],
        }})
        self.assertFalse(bot._map_is_downloadable(SimpleNamespace(beatmap_id=700), force=True))
        bot.pool.exclude_beatmaps.assert_called_once_with([700, 701])
        self.assertFalse(bot._download_availability_cache[701][0])

    def test_download_cache_error_can_continue_with_available_api_result(self):
        bot = self.bots[0]
        bot.ratings.cached_download_availability = Mock(side_effect=sqlite3.OperationalError('cache unavailable'))
        bot.api.beatmap = Mock(return_value={'beatmapset': {
            'availability': {'download_disabled': False}}})
        self.assertTrue(bot._map_is_downloadable(SimpleNamespace(beatmap_id=700), force=True))
        bot.api.beatmap.assert_called_once_with(700)

    def test_force_during_cache_error_keeps_explicit_in_memory_quarantine(self):
        bot = self.bots[0]
        bot.ratings.cached_download_availability = Mock(side_effect=sqlite3.OperationalError('cache unavailable'))
        bot._download_availability_cache[700] = (False, 0, True)
        bot.api.beatmap = Mock(side_effect=RuntimeError('offline'))
        self.assertFalse(bot._map_is_downloadable(SimpleNamespace(beatmap_id=700), force=True))
        bot.api.beatmap.assert_not_called()

    def test_force_during_cache_error_keeps_quarantined_set_for_new_difficulty(self):
        bot = self.bots[0]
        bot.ratings.cached_download_availability = Mock(side_effect=sqlite3.OperationalError('cache unavailable'))
        bot._download_set_cache[50] = (False, 0, True)
        bot.api.beatmap = Mock(return_value={'beatmapset_id': 50})
        bot.api.beatmapset = Mock(side_effect=RuntimeError('offline'))
        self.assertFalse(bot._map_is_downloadable(SimpleNamespace(beatmap_id=701), force=True))
        bot.api.beatmapset.assert_not_called()

    def test_force_bypasses_unconfirmed_availability_when_cache_is_down(self):
        bot = self.bots[0]
        bot.pool.exclude_beatmaps = Mock()
        bot.ratings.cached_download_availability = Mock(side_effect=sqlite3.OperationalError('cache unavailable'))
        bot._download_availability_cache[700] = (True, time.time(), False)
        bot.api.beatmap = Mock(return_value={'beatmapset': {'availability': {'download_disabled': True}}})
        self.assertFalse(bot._map_is_downloadable(SimpleNamespace(beatmap_id=700), force=True))
        bot.api.beatmap.assert_called_once_with(700)

    def test_unavailable_set_records_per_pool_quarantine_event(self):
        first = self.bots[0]
        first.pool.exclude_beatmaps = Mock()
        first.pool.source_mode = "variety"
        metadata = {"beatmapset_id": 50, "beatmapset": {
            "id": 50, "availability": {"download_disabled": True},
            "beatmaps": [{"id": 700}, {"id": 701}],
        }}
        first.api.beatmap = Mock(return_value=metadata)
        candidate = SimpleNamespace(beatmap_id=700)

        self.assertFalse(first._map_is_downloadable(candidate))

        conn = sqlite3.connect(first.ratings.path)
        row = conn.execute(
            "SELECT room_alias,pool_source,excluded_count,reason "
            "FROM download_quarantine_events"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "room1")
        self.assertEqual(row[1], "variety")
        self.assertEqual(row[2:], (2, "download_disabled"))


if __name__ == "__main__":
    unittest.main()
