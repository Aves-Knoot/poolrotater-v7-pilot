"""Staff Discord -> authenticated stopped-host queue -> durable completion."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
import uuid
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'source'))
from bot.discord_control import DiscordCommand
from bot.multiroom import SharedDiscord
from poolrotater_host.discord_relay.bridge import Bridge,CommunityDiscord
from poolrotater_host.discord_relay.fleet import Fleet,operation_identity
from poolrotater_host.managed_host.state import State
from poolrotater_host.managed_host.hub import Repository
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.store import Store
from poolrotater_host.network.contracts import NetworkError,secret
from poolrotater_host.ratings.repository import prepare


class FleetTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.now=1000000;self.sequence=0
        initialize(self.root/'hub');self.store=Store(self.root/'hub',clock=lambda:self.now);prepare(self.store)
        binding={'operator_id':str(uuid.uuid4()),'account_id':99,'directory':True,
                 'installation_id':str(uuid.uuid4()),'generation':str(uuid.uuid4())}
        self.token=secret();invitation=self.store.invite(binding,label='Invented')['invitation']
        self.host=self.store.enroll({'protocol_version':1,'request_id':str(uuid.uuid4()),
                          'invitation':invitation,'credential':self.token,**binding})['host_id']
        self.binding={'host_id':self.host,**{k:binding[k] for k in ('installation_id','generation')}}
        self.state=State(self.root/'host',**self.binding,clock=lambda:self.now)
        self.state.seed([{'alias':'room'+str(i),'desired':'stopped','settings':{'preset':'standard-auto',
                         'sr_min':4.,'sr_max':5.,'countdown_seconds':20}} for i in range(1,5)])
        self.state.policy_applied(1);self.repo=Repository(self.store)
        self.fleet=Fleet(self.store,channel=9001,owner_host=self.host);self.exchange()
    def exchange(self,receipts=None):
        self.sequence+=1
        return self.repo.accept(self.token,{'protocol_version':1,**{k:self.binding[k] for k in ('installation_id','generation')},
              'sequence':self.sequence,'sent_at':self.now,'capabilities':self.state.snapshot(controller_phase='stopped'),
              'receipts':receipts or []})
    def cmd(self,content,*,message=100,channel=9001,role='ADMIN'):
        return DiscordCommand(777,'Untrusted display name',role,content,channel,message)
    def handle(self,action='open',*,message=100,tail='',host='owner',alias='room1',**kw):
        return self.fleet.handle(self.cmd(f'.fleet {action} {host} {alias} {tail}'.strip(),message=message,**kw))
    def test_all_stopped_controller_can_open_one_room_without_public_directory(self):
        self.assertEqual(self.store.directory()['rooms'],[])
        response=self.handle();self.assertIn('awaiting the host',response[0])
        request,=self.exchange()['requests'];self.assertEqual(request['action'],'open')
        self.assertEqual(request['alias'],'room1');self.assertEqual(request['expected_room_generation'],None)
        self.assertEqual(request['actor'],'discord:777');self.assertEqual(request['host_id'],self.host)
    def test_gateway_replay_returns_original_operation_after_room_revision_and_expiry_change(self):
        first=self.handle();request,=self.exchange()['requests'];self.state.receive(request)
        self.state.begin(request['id'],ready=True)
        result=self.state.finish(request['id'],'applied',result={'application':'lifecycle','revision':1,'room_id':None})
        self.exchange([result]);self.now+=1000
        replay=self.handle()
        self.assertIn(request['id'],replay[0]);self.assertIn('applied',replay[0])
        self.assertEqual(len(self.repo.inspect(self.host)['operations']),1)
    def test_edited_same_message_cannot_send_another_mutation(self):
        self.handle('open');response=self.handle('close')
        self.assertIn('managed_operation_conflict',response[0]);self.assertEqual(len(self.repo.inspect(self.host)['operations']),1)
    def test_roles_and_staff_channel_checked_before_queuing(self):
        for change in ({'role':'MOD'},{'role':'PLAYER'},{'channel':1005}):
            with self.subTest(change=change):self.assertIn('refused',self.handle(**change)[0])
        self.assertEqual(self.repo.inspect(self.host)['operations'],[])
    def test_large_requests_respect_reply_budget_without_starving_other_rooms(self):
        patch_value={'custom_join_greetings':{str(i):'hello '*120 for i in range(50)}}
        expected=set()
        for alias in ('room1','room2','room3','room4'):
            expected.add(self.repo.queue(self.host,alias,'configure',patch=patch_value)['request']['id'])
        seen=set();receipts=[]
        for _ in range(4):
            reply=self.exchange(receipts)
            from poolrotater_host.network.contracts import canonical
            self.assertLessEqual(len(canonical(reply)),65536)
            for request in reply['requests']:
                self.assertNotIn(request['id'],seen);seen.add(request['id'])
                self.state.receive(request);receipts.append(self.state.operation(request['id']))
        self.assertEqual(seen,expected)
    def test_single_undeliverable_request_is_refused_before_queue_commit(self):
        patch_value={'custom_join_greetings':{str(i):'a'*1000 for i in range(70)}}
        with self.assertRaises(NetworkError) as caught:self.repo.queue(self.host,'room1','configure',patch=patch_value)
        self.assertEqual(caught.exception.code,'request_too_large')
        self.assertEqual(self.repo.inspect(self.host)['operations'],[])

    def test_missing_gateway_identity_is_refused_instead_of_random_operation(self):
        self.assertIn('managed_message_identity_required',self.handle(message=0)[0])
        self.assertEqual(self.repo.inspect(self.host)['operations'],[])
    def test_private_configuration_and_error_never_echo_password(self):
        canary='never-echo-this-secret'
        response=self.handle('configure',tail=json.dumps({'visibility':'private','password':canary}))
        self.assertNotIn(canary,json.dumps(response));self.assertNotIn(canary,json.dumps(self.repo.inspect(self.host)))
        request,=self.exchange()['requests'];self.assertEqual(request['patch']['password'],canary)
        self.assertNotIn(canary,json.dumps(self.fleet.notices()))
        invalid=self.handle('configure',message=101,alias='room2',tail='{"password":"'+canary+'"')
        self.assertNotIn(canary,json.dumps(invalid))
    def test_staff_settings_inspection_shows_desired_and_effective_without_passwords(self):
        from poolrotater_host.managed_host import details
        document=details.snapshot(self.state);pin,pages=details.encode(document)
        for n in range(len(pages)):details.Repository(self.store).accept(self.token,details.page(document,n,pages,pin))
        output=self.handle('settings')
        self.assertIn('snapshot: current',output[0]);self.assertIn('countdown_seconds',output[0])
        self.assertIn('effective same',output[0])
        self.assertTrue(all(len(message)<=1800 for message in output))
        greeting=self.handle('greeting',tail='Frogma')
        self.assertIn('desired greeting',greeting[0])

    def test_explicit_stale_revision_and_none_generation_are_not_inferred(self):
        self.assertIn('configuration_revision_changed',self.handle(tail='--revision 9')[0])
        self.state.observe_owned('room1',uuid.uuid4().hex,123,adopt=True);self.exchange()
        self.assertIn('configuration_revision_changed',self.handle(tail='--generation none')[0])
        self.assertEqual(self.repo.inspect(self.host)['operations'],[])
    def test_explicit_immediate_action_warns_and_is_bound_to_only_one_target(self):
        self.handle('close',tail='--immediate')
        request,=self.exchange()['requests'];self.assertEqual(request['timing'],'immediate');self.assertEqual(request['alias'],'room1')
        self.assertEqual(self.state.limits()['max_concurrent_rooms'],4)
    def test_stale_capabilities_refuse_mutation_and_old_hosts_remain_unmanaged(self):
        self.now+=121;self.assertIn('managed_fresh_capabilities_required',self.handle()[0])
        self.assertEqual(self.repo.inspect(self.host)['operations'],[])
    def test_notifications_track_receipt_changes_across_bridge_restart_without_secret_payloads(self):
        self.handle();request,=self.exchange()['requests'];self.state.receive(request)
        self.exchange([self.state.operation(request['id'])]);notice,=self.fleet.notices()
        self.assertIn('received',notice['content']);self.fleet.acknowledge_notice(notice)
        self.assertEqual(Fleet(self.store,channel=9001,owner_host=self.host).notices(),[])
        self.state.begin(request['id'],ready=True)
        receipt=self.state.finish(request['id'],'applied',result={'application':'lifecycle','room_id':123,'revision':1})
        self.exchange([receipt]);notice,=self.fleet.notices()
        self.assertIn('applied',notice['content']);self.assertIn('/rooms/123',notice['content'])
    def test_queue_commit_then_error_retains_operation_and_eventual_notice(self):
        original=self.fleet.repo.queue
        def lost(*args,**kwargs):
            original(*args,**kwargs)
            raise OSError('invented error with secret-that-must-not-be-echoed')
        with patch.object(self.fleet.repo,'queue',side_effect=lost):response=self.handle()
        self.assertIn('requires review',response[0]);self.assertNotIn('secret-that',response[0])
        request,=self.exchange()['requests'];self.state.receive(request)
        self.exchange([self.state.operation(request['id'])])
        notice,=self.fleet.notices();self.assertIn(request['id'],notice['content'])

    def test_fleet_privacy_is_checked_for_both_ingress_and_actual_outbound_channel(self):
        control=CommunityDiscord({'discord_control_guild_id':555});control.fleet_channel=9001
        roles=[SimpleNamespace(id=i,name=name,managed=False,permissions=SimpleNamespace(administrator=False))
               for i,name in enumerate(('@everyone','Members','Top dev','mods'),1)]
        visible={'Top dev','mods'}
        # Current privacy policy requires complete bot/owner/overwrite evidence;
        # an incomplete cache is intentionally denied, not assumed private.
        channel=SimpleNamespace(id=9001,guild=SimpleNamespace(id=555,roles=roles,owner_id=777,me=SimpleNamespace(id=888)),overwrites={},
            permissions_for=lambda role:SimpleNamespace(view_channel=role.name in visible))
        self.assertTrue(control._channel_allows_command(channel));self.assertTrue(control._channel_allows_outbound(channel))
        visible.add('Members')
        self.assertFalse(control._channel_allows_command(channel));self.assertFalse(control._channel_allows_outbound(channel))
        channel.id=1005;self.assertTrue(control._channel_allows_command(channel))
        channel.id=9001;channel.guild=None;self.assertFalse(control._channel_allows_outbound(channel))

    def test_owner_supervisor_routes_close_to_durable_intent_and_retries_exactly(self):
        from rolling.supervisor import RollingSupervisor
        control=Mock();command=self.cmd('.close',channel=1001)
        owner=SimpleNamespace(configs={'room1':{}},discord=control,managed=SimpleNamespace(state=self.state),request_shutdown=Mock())
        for _ in range(2):
            control.get_supervisor_command_nowait.side_effect=[('room1',command),queue.Empty]
            RollingSupervisor._discord_commands(owner)
        receipt=self.state.operation(operation_identity(1001,100))
        self.assertEqual(receipt['phase'],'received')
        self.assertEqual(len(self.state.pending()),1);owner.request_shutdown.assert_not_called()
        view=SharedDiscord([{'_instance_name':'room1','managed_preset':'standard-auto'}]).view_for('room1')
        self.assertTrue(view._is_supervisor_command('.close'))

    def test_exact_label_owner_and_uuid_resolution_are_supported(self):
        for value in ('owner','Invented','invented',self.host):self.assertEqual(self.fleet.resolve(value),self.host)
    def test_no_shell_path_or_capacity_action_exists(self):
        for action in ('shell','set-limit','systemctl','exec'):
            self.assertIn('refused',self.handle(action,tail='rm -rf /')[0])
        self.assertIn('refused',self.handle('configure',tail='{"max_concurrent_rooms":999}')[0])
        self.assertEqual(self.state.limits()['max_concurrent_rooms'],4)
    def test_bridge_routes_staff_even_with_no_public_rooms_and_delivers_receipts_only_there(self):
        cfg={'channels':{'5':1005},'owner_channels':[1001,1002,1003,1004],
             'owner_host_id':self.host,'discord':{},'fleet_channel_id':9001,'feed':str(self.root/'missing')}
        control=CommunityDiscord({});control._thread=object();bridge=Bridge(self.store,cfg,discord=control,clock=lambda:self.now)
        bridge.next_discovery=self.now+600
        self.assertEqual(control.command_targets('.fleet open owner room1',9001),[(control,'.fleet open owner room1')])
        self.assertEqual(control.command_targets('.fleet open owner room1',1005),[])
        self.assertTrue(control._role_allows_command('ADMIN','.fleet open owner room1'))
        self.assertFalse(control._role_allows_command('MOD','.fleet open owner room1'))
        control.commands.put(self.cmd('.fleet open owner room1'));result=bridge.step()
        self.assertEqual(result['routable'],0)
        self.assertEqual(control.outbox.get_nowait().channel_id,9001)
        request,=self.exchange()['requests'];self.state.receive(request);self.exchange([self.state.operation(request['id'])])
        bridge.fleet_notices();notice=control.outbox.get_nowait()
        self.assertEqual(notice.channel_id,9001);self.assertIn('received',notice.content)
    def test_fleet_channel_cannot_share_a_public_lobby_panel(self):
        cfg={'channels':{'5':1005},'owner_channels':[1001,1002,1003,1004],
             'owner_host_id':self.host,'discord':{},'fleet_channel_id':1005}
        with self.assertRaises(NetworkError):Bridge(self.store,cfg)
    def test_owner_transport_does_not_also_deliver_fleet_as_gameplay(self):
        transport=SharedDiscord([{'_instance_name':'room1','discord_control_channel_id':9001}])
        self.assertEqual(transport.command_targets('.fleet open owner room1',9001),[])
    def test_local_lifecycle_gateway_replay_keeps_original_revision_and_global_shutdown_does_not_replay(self):
        oid=operation_identity(1001,101)
        first=self.state.local_lifecycle('room1','open',operation_id=oid,actor='discord:777')
        self.state.begin(oid,ready=True);self.state.finish(oid,'applied',result={'application':'lifecycle','revision':1})
        self.now+=1000
        self.assertEqual(self.state.local_lifecycle('room1','open',operation_id=oid,actor='discord:777')['phase'],'applied')
        shutdown=operation_identity(1001,102)
        self.assertTrue(self.state.operator_stop(operation_id=shutdown,actor='discord:777'))
        self.state.operator_start();self.assertEqual(self.state.room('room1')['desired'],'running')
        self.assertFalse(self.state.operator_stop(operation_id=shutdown,actor='discord:777'))
        self.assertEqual(self.state.room('room1')['desired'],'running')
        with self.assertRaises(NetworkError):self.state.local_lifecycle('room1','close',operation_id=shutdown,actor='discord:777')


if __name__=='__main__':unittest.main()
