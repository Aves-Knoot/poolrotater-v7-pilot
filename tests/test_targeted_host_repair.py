"""Incident regressions: definitive chat denial, installed relay, mixed snapshots."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
from types import SimpleNamespace

from bot.api import ApiAdmissionDeferred,ApiChatDeliveryRefused
from poolrotater_host.oauth import AuthError
from poolrotater_host.provider_api import ProviderChatDeliveryRefused
from poolrotater_host.network.contracts import NetworkError
from poolrotater_host.activity.replica import Publisher,INTERVAL
from rolling.adapters import Api
from rolling.protocol import RemoteError
from rolling.supervisor import RollingSupervisor
from tests.test_v7_provider_rooms import RoomCase
from tests.test_room_chat_backpressure import RoomChatBackpressureTests
from tests.test_local_statistics_replica import ReplicaTests


class ChatDenialWireTests(RoomCase):
    def test_ack_and_read_denials_are_nonfatal_but_channel_authority_stays_strict(self):
        from poolrotater_host.provider_api import ProviderKeepaliveUnavailable,ProviderChatReadUnavailable
        auth=self.authorized();backend,api,hub=self.adapters(auth)
        with auth.verified_owner():
            self.provider.wire('/api/v2/chat/ack',403,b'private')
            with self.assertRaises(ProviderKeepaliveUnavailable):api.chat_ack()
            self.provider.wire('/api/v2/chat/channels/1101/messages?limit=50',403,b'private')
            with self.assertRaises(ProviderChatReadUnavailable):api.chat_messages(1101)
            self.provider.wire('/api/v2/chat/channels/1101',403,b'private')
            with self.assertRaises(AuthError):api.chat_join(1101,99)

    def test_actual_403_is_definitive_chat_only_without_replay_or_uncertainty(self):
        auth=self.authorized();backend,api,hub=self.adapters(auth)
        self.provider.wire('/api/v2/chat/channels/1101/messages',403,b'private provider text')
        with auth.verified_owner(),patch.object(api.client,'_req',wraps=api.client._req)as requests:
            with self.assertRaises(ProviderChatDeliveryRefused)as caught:api.chat_send(1101,'one message')
        self.assertEqual(caught.exception.http_status,403)
        self.assertNotIn('private provider text',str(caught.exception))
        requests.assert_called_once()
        from poolrotater_host.layout import read_json
        value=read_json(auth.transport.gate.root/auth.transport.gate.state_path)
        self.assertEqual(value['counters']['uncertain'],0)
        self.assertFalse(backend.rooms)


class ChatDenialBotTests(unittest.TestCase):
    bot=RoomChatBackpressureTests.bot
    clock=RoomChatBackpressureTests.clock

    def test_definitive_denial_keeps_gameplay_and_drops_only_rejected_message(self):
        b=self.bot();b.state='PLAYING';b.running=True
        b.api.chat_send.side_effect=[ApiChatDeliveryRefused('HTTP 403'),{'message_id':99}]
        with self.clock(100):b.say('announcement',automatic=True)
        self.assertEqual(b.state,'PLAYING');self.assertTrue(b.running)
        self.assertFalse(getattr(b,'_pending_room_chat',None))
        b._notify_discord_lobby_event.assert_not_called()
        with self.clock(102):b.say('new command reply')
        self.assertEqual(b.api.chat_send.call_count,1)
        with self.clock(131):b._flush_room_chat()
        self.assertEqual([c.args[1]for c in b.api.chat_send.call_args_list],['announcement','new command reply'])

    def test_queued_denial_is_removed_and_does_not_advance_send_success(self):
        b=self.bot();b.api.chat_send.side_effect=[ApiAdmissionDeferred('busy'),ApiChatDeliveryRefused('HTTP 403')]
        with self.clock(100):b.say('queued')
        with self.clock(102):b._flush_room_chat()
        self.assertFalse(b._pending_room_chat);self.assertEqual(b._last_sent,0)

    def test_rpc_converts_only_typed_chat_denial(self):
        peer=Mock();peer.call.side_effect=RemoteError('ProviderChatDeliveryRefused','HTTP 403')
        with self.assertRaises(ApiChatDeliveryRefused):Api(peer).chat_send(1101,'x')
        with self.assertRaises(RemoteError):Api(peer).chat_join(1101,99)
        peer.call.side_effect=RemoteError('AuthError','OAuth: provider_api_unavailable [HTTP 403]')
        with self.assertRaises(RemoteError):Api(peer).chat_send(1101,'x')


class RelayLifecycleTests(unittest.TestCase):
    def test_relay_lifecycle_is_idempotent_and_uses_same_controller(self):
        owner=SimpleNamespace(discord=SimpleNamespace())
        with patch('poolrotater_host.discord_relay.host.HostRelay')as relay:
            RollingSupervisor.start_discord_relay(owner)
            RollingSupervisor.start_discord_relay(owner)
            relay.assert_called_once_with(owner);relay.return_value.start.assert_called_once()
            RollingSupervisor.stop_discord_relay(owner);relay.return_value.stop.assert_called_once()


class InstalledRelayLifecycleTests(RoomCase):
    def test_actual_installed_provider_context_starts_and_stops_relay(self):
        from tests.catalog_fixture_ranges import create_catalogs
        from test_v7_runtime import fixture_release
        from poolrotater_host.provider_rooms import provider_host_controller
        create_catalogs(self.root);release=fixture_release(self.root,'v7.0.0')
        auth=self.authorized();backend,api,hub=self.adapters(auth)
        with patch('rolling.supervisor.require_immutable'):
            with provider_host_controller(self.root,release,authentication=auth,api=api,hub=hub)as owner:
                relay=owner.discord.remote_relay
                self.assertTrue(relay.thread.is_alive())
                deadline=time.monotonic()+5
                while not (self.root/'state/discord-relay.json').exists()and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue((self.root/'state/discord-relay.json').exists())
        self.assertFalse(relay.thread.is_alive())
        self.assertFalse(backend.rooms)


class StatisticsRepairTests(unittest.TestCase):
    setUpClass=classmethod(ReplicaTests.setUpClass.__func__)
    tearDownClass=classmethod(ReplicaTests.tearDownClass.__func__)
    setUp=ReplicaTests.setUp
    stop=ReplicaTests.stop
    enable=ReplicaTests.enable
    presence=ReplicaTests.presence
    _publish_presence=ReplicaTests._publish_presence

    def clear_pointers(self,pub,token):
        host,_,_,_=pub.authority(token)
        for family in ('activity-replicas','activity-replicas-v2'):
            (pub.store.root/family/host/'current.json').unlink(missing_ok=True)
        pub.stamp=None

    def test_mixed_v1_v2_uses_verified_disk_generations_without_rebuild_thrashing(self):
        self._publish_presence();pub=self.server.statistics;token=self.client.credential()['credential']
        self.clear_pointers(pub,token)
        with patch.object(pub,'_project',wraps=pub._project)as builds:
            v1=pub.current(token);v2=pub.current(token,include_catch=True)
            self.assertEqual(builds.call_count,2)
            for _ in range(4):
                self.assertEqual(pub.current(token),v1)
                self.assertEqual(pub.current(token,include_catch=True),v2)
            self.assertEqual(builds.call_count,2)

    def test_background_cold_build_does_not_hold_request_or_block_capabilities(self):
        self._publish_presence();pub=self.server.statistics;token=self.client.credential()['credential']
        self.clear_pointers(pub,token)
        pub.background=True;entered=threading.Event();release=threading.Event();original=pub._project
        def blocked(*args,**kwargs):
            entered.set();release.wait(10);return original(*args,**kwargs)
        try:
            with patch.object(pub,'_project',side_effect=blocked):
                start=time.monotonic()
                with self.assertRaises(NetworkError)as caught:pub.current(token)
                self.assertEqual(caught.exception.status,503)
                self.assertLess(time.monotonic()-start,1)
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.client.transport(self.client.profile()).request('GET','/v2/capabilities')['ok'])
                release.set()
                deadline=time.monotonic()+5
                while pub._worker is not None and time.monotonic()<deadline:time.sleep(.01)
                self.assertIsNone(pub._worker)
                self.assertTrue(pub.current(token)['ok'])
        finally:release.set();pub.close()

    def test_stale_same_policy_remains_available_during_refresh_but_new_policy_does_not(self):
        self._publish_presence();pub=self.server.statistics;token=self.client.credential()['credential']
        old=pub.current(token);pub.background=True
        host,epoch,policy,watermark=pub.authority(token)
        with patch.object(pub,'_schedule')as queued,patch.object(pub.store,'clock',return_value=old['generated_at']+INTERVAL+1):
            self.assertEqual(pub.current(token),old);queued.assert_called_once()
        with patch.object(pub,'authority',return_value=(host,epoch,'f'*64,watermark)),patch.object(pub,'_schedule'):
            with self.assertRaises(NetworkError):pub.current(token)


class HelperPauseTests(unittest.TestCase):
    def test_pause_restores_original_active_timers_even_on_upgrade_failure(self):
        from poolrotater_host.update_helpers import quiesce
        with tempfile.TemporaryDirectory()as directory:
            units=Path(directory)
            for kind in ('managed','updates','recovery'):
                name='poolrotater-v7-'+kind+'-first'
                (units/(name+'.timer')).write_text('[Timer]\nUnit='+name+'.service\n')
            calls=[];job={}
            def systemctl(*args):
                calls.append(args)
                return b'active\n'if args[0]=='show'else b''
            with patch('poolrotater_host.pilot_operations.UNIT_BASE',units),patch('poolrotater_host.pilot_operations._systemctl',side_effect=systemctl),patch('poolrotater_host.provisioning.protected',side_effect=lambda p:p):
                with self.assertRaisesRegex(RuntimeError,'fixture failure'):
                    with quiesce('first',{},job,lambda **v:job.update(v)):raise RuntimeError('fixture failure')
            self.assertEqual(len([c for c in calls if c[0]=='stop']),3)
            self.assertEqual(len([c for c in calls if c[0]=='start']),3)
            self.assertTrue(all(c[1].endswith('.timer')for c in calls))
            self.assertFalse(job['helper_pause_active'])

# Imported helpers are not additional suites in this incident batch.
del ReplicaTests, RoomChatBackpressureTests, RoomCase
