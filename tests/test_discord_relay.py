from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
from poolrotater_host.network.contracts import NetworkError,secret
from poolrotater_host.network.store import Store
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.discord_relay.repository import Repository
from poolrotater_host.discord_relay.model import command,channel_name,PATH
from poolrotater_host.discord_relay.host import HostRelay
from poolrotater_host.discord_relay.bridge import CommunityDiscord,card,Bridge
from bot.discord_control import DiscordCommand
from bot.multiroom import SharedDiscord
from tests.test_network_store import identity,room


class RelayTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.now=1000
        hub=self.root/'hub';initialize(hub);self.store=Store(hub,clock=lambda:self.now);self.repo=Repository(self.store)
        self.binding=identity();self.token=secret()
        self.host=self.store.enroll({'protocol_version':1,'request_id':str(uuid.uuid4()),'invitation':self.store.invite(self.binding,label='Xikeon')['invitation'],'credential':self.token,**self.binding})['host_id']
        self.owner=str(uuid.uuid4());self.publish()
        self.repo.assign(self.store.directory()['rooms'],{5:1005,6:1006},owner_host=self.owner)
        self.body={'protocol_version':1,**{k:self.binding[k]for k in ('installation_id','generation')},'sent_at':self.now,'receipts':[],'replies':[]}
        self.repo.accept(self.token,self.body)
        self.actor={'discord_user_id':777,'username':'Tester','role':'ADMIN'}

    def publish(self):
        self.store.heartbeat(self.token,{'protocol_version':1,**{k:self.binding[k]for k in ('installation_id','generation')},'sequence':int(self.now),'code_version':'v7.2.7','catalog_version':'a'*64,'rooms':[room(self.now)]})

    def enqueue(self,content='.settings'):
        return self.repo.queue(self.host,'room1',self.actor,channel_id=1005,content=content)

    def error(self,fn,*args,**kwargs):
        with self.assertRaises(NetworkError):fn(*args,**kwargs)

    def test_semantic_names_owner_and_community_and_duplicate_labels(self):
        self.assertEqual(channel_name(1,'catch-fixed'),'l1-catch-setdiff')
        self.assertEqual(channel_name(2,'standard-fixed'),'l2-standard-setdiff')
        self.assertEqual(channel_name(3,'mania-7k'),'l3-7k-mania-setdiff')
        self.assertEqual(channel_name(5,'standard-auto',operator='Xikeon'),'l5-xikeon-standard-autodiff')
        self.assertNotEqual(channel_name(5,'mania-4k',operator='same'),channel_name(6,'mania-4k',operator='same'))

    def test_owner_slots_reserved_and_capacity_extension(self):
        rows=self.store.directory()['rooms'];rows.append({**rows[0],'host_id':self.owner,'alias':'room2'})
        slots=self.repo.assign(rows,{5:1005},owner_host=self.owner)
        self.assertEqual(len(slots),1);self.assertEqual(slots[0]['slot'],5)
        new={**rows[0],'host_id':str(uuid.uuid4())}
        self.assertEqual(len(self.repo.assign([new],{5:1005},owner_host=self.owner)),1)
        extended=self.repo.assign([new],{5:1005,6:1006},owner_host=self.owner)
        self.assertEqual(len(extended),2);self.assertEqual(extended[1]['slot'],6)

    def test_assignments_survive_restart_room_recreation_and_missing_host(self):
        a=self.repo.assign([],{5:1005,6:1006},owner_host=self.owner)
        b=Repository(self.store).assign([{**self.store.directory()['rooms'][0],'osu_room_id':99999}],{5:1005,6:1006},owner_host=self.owner)
        self.assertEqual(a,b)

    def test_duplicate_channel_and_owner_slot_refused(self):
        self.error(self.repo.assign,[],{4:1004},owner_host=self.owner)
        self.error(self.repo.assign,[],{5:1005,6:1005},owner_host=self.owner)

    def test_command_bound_to_identity_room_and_channel(self):
        request=self.enqueue();value=self.repo.accept(self.token,self.body)['requests'][0]
        self.assertEqual(value['id'],request);self.assertEqual(value['room_id'],12345)
        self.assertEqual(value['generation'],self.binding['generation'])
        self.error(self.repo.queue,self.host,'room1',self.actor,channel_id=1006,content='.settings')

    def test_response_loss_never_redelivers_mutation(self):
        self.enqueue('.say hello');self.assertEqual(len(self.repo.accept(self.token,self.body)['requests']),1)
        self.assertEqual(self.repo.accept(self.token,self.body)['requests'],[])
        self.now+=46;self.assertEqual(len(self.repo.take_notices()),1);self.assertEqual(self.repo.take_notices(),[])

    def test_expired_command_never_delivered(self):
        self.enqueue();self.now+=46;self.body['sent_at']=self.now
        self.assertEqual(self.repo.accept(self.token,self.body)['requests'],[])

    def test_foreign_binding_token_revocation_and_stale_rooms(self):
        self.error(self.repo.accept,self.token,{**self.body,'generation':str(uuid.uuid4())})
        self.error(self.repo.accept,secret(),self.body)
        self.now+=121;self.error(self.enqueue)
        self.store.revoke(self.host);self.error(self.repo.accept,self.token,{**self.body,'sent_at':self.now})

    def test_no_account_process_or_shell_commands(self):
        for text in ('.','.shutdown all','.open','.reopen','.password secret','curl evil','.settings\n.say bad'):
            with self.subTest(text=text):self.error(self.enqueue,text)

    def test_real_discord_snowflakes_are_admitted(self):
        rid=self.enqueue();value=json.loads(self._request(rid)['payload'])
        value.update(channel_id=1562177940551114814,discord_user_id=734567890123456789)
        self.assertEqual(command(value)['channel_id'],1562177940551114814)

    def test_moderator_cannot_say_or_override_admin(self):
        actor={**self.actor,'role':'MOD'}
        self.error(self.repo.queue,self.host,'room1',actor,channel_id=1005,content='.say hi')
        self.repo.queue(self.host,'room1',actor,channel_id=1005,content='.settings')

    def test_old_host_without_relay_reports_clear_refusal(self):
        self.now+=61;self.publish();self.error(self.enqueue)

    def test_receipts_replies_are_host_bound_idempotent_and_redacted(self):
        rid=self.enqueue();self.repo.accept(self.token,self.body)
        reply={'id':str(uuid.uuid4()),'request_id':rid,'content':'password=not-for-discord'}
        value={**self.body,'receipts':[{'id':rid,'result':'dispatched'}],'replies':[reply]}
        self.repo.accept(self.token,value);self.repo.accept(self.token,value)
        messages=self.repo.take_replies();self.assertEqual(len(messages),1)
        self.assertNotIn('not-for-discord',messages[0]['content']);self.assertEqual(self.repo.take_replies(),[])
        self.error(self.repo.accept,self.token,{**value,'replies':[{**reply,'request_id':str(uuid.uuid4())}]})

    def test_refused_delivery_emits_one_notice(self):
        rid=self.enqueue();self.repo.accept(self.token,self.body)
        self.repo.accept(self.token,{**self.body,'receipts':[{'id':rid,'result':'refused'}]})
        self.assertEqual(len(self.repo.take_notices()),1);self.assertEqual(self.repo.take_notices(),[])

    def test_discord_permissions_and_non_command_filtering(self):
        c=CommunityDiscord({'discord_control_channel_id':1005});c.routes={1005:(self.host,'room1')}
        self.assertEqual(c.command_targets('.settings',1006),[])
        self.assertEqual(c.command_targets('ordinary conversation',1005),[])
        self.assertTrue(c._role_allows_command('MOD','.settings'))
        self.assertFalse(c._role_allows_command('MOD','.say hello'))
        self.assertFalse(c._role_allows_command('ADMIN','.shutdown all'))

    def make_host(self):
        runtime=self.root/'host';(runtime/'state').mkdir(parents=True);(runtime/'config').mkdir()
        cfg={'_instance_name':'room1','discord_control_enabled':True}
        d=SharedDiscord([cfg]);d.remote_relay=None
        worker=SimpleNamespace(fresh=lambda:True,status={'ready':True,'running':True,'room_id':12345},broker=SimpleNamespace(known=True,uncertain=False,hub=SimpleNamespace(owned={12345})))
        owner=SimpleNamespace(runtime=runtime,discord=d,workers={'room1':worker},stopping=threading.Event(),rollout_alias=None)
        cred={'host_id':self.host,**{k:self.binding[k]for k in ('installation_id','generation')},'credential':self.token,'credential_expires_at':5000}
        transport=SimpleNamespace(request=lambda method,path,**kw:self.repo.accept(kw['token'],kw['body']))
        client=SimpleNamespace(marker={k:self.binding[k]for k in ('installation_id','generation')},status=lambda:{'enrolled':True},profile=lambda:{'account_id':123},check_account=lambda _:None,credential=lambda:cred,transport=lambda _:transport)
        relay=HostRelay(owner,client=client,clock=lambda:self.now);d.remote_relay=relay
        return owner,relay,cred

    def test_host_end_to_end_receipt_reply_and_no_duplicate_execution(self):
        owner,relay,cred=self.make_host();rid=self.enqueue('.settings')
        self.assertEqual(relay.step(),'connected')
        cmd=owner.discord.view_for('room1').get_command_nowait();self.assertEqual(cmd.channel_id,1005)
        self.assertEqual(relay.dispatch({**json.loads(self._request(rid)['payload'])},cred),'dispatched')
        with self.assertRaises(queue.Empty):owner.discord.view_for('room1').get_command_nowait()
        self.assertTrue(owner.discord.view_for('room1').send_to_channel('Settings response',1005))
        relay.step();relay.step();self.assertEqual(len(self.repo.take_replies()),1)
        with relay.database() as db:self.assertEqual(db.execute('SELECT ack FROM requests').fetchone()[0],1)

    def _request(self,rid):
        with self.repo.database() as db:return dict(db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone())

    def test_host_refuses_room_refresh_expiry_uncertainty_and_stopping(self):
        owner,relay,cred=self.make_host()
        for mutation in ('refresh','expiry','uncertain','stopping'):
            self.now+=6;self.publish();self.repo.accept(self.token,{**self.body,'sent_at':self.now})
            rid=self.enqueue();value=self.repo.accept(self.token,{**self.body,'sent_at':self.now})['requests'][0]
            if mutation=='refresh':value['room_id']=54321
            if mutation=='expiry':value['expires_at']=self.now-1
            if mutation=='uncertain':owner.workers['room1'].broker.uncertain=True
            if mutation=='stopping':owner.stopping.set()
            self.assertEqual(relay.dispatch(value,cred),'refused')
            owner.workers['room1'].broker.uncertain=False;owner.stopping.clear()
        with self.assertRaises(queue.Empty):owner.discord.view_for('room1').get_command_nowait()

    def test_optout_and_corrupt_state_do_not_touch_rooms(self):
        owner,relay,cred=self.make_host()
        (owner.runtime/'config/discord-relay.json').write_text('{"format":1,"enabled":false}');(owner.runtime/'config/discord-relay.json').chmod(0o600)
        self.assertEqual(relay.step(),'disabled')
        with patch.object(relay,'database',side_effect=OSError):self.assertEqual(relay.channels('room1'),set())
        self.assertEqual(set(owner.workers),{'room1'})

    def test_owner_type_labels_use_actual_mania_keys_and_reserved_slots(self):
        for slot,ruleset,keys,label in [(1,2,None,'catch'),(2,0,None,'standard'),(3,3,7,'7k-mania'),(4,3,4,'4k-mania')]:
            cfg={'_host_root':'/var/lib/poolrotater-v7-pilot/owner','_instance_name':f'room{slot}','ruleset_id':ruleset,'mania_keys':keys,'auto_difficulty':False}
            self.assertEqual(SharedDiscord([cfg]).view_for(f'room{slot}')._public_status_channel_name(),f'l{slot}-{label}-setdiff')

    def test_card_matches_map_art_and_timing_and_withdraws_stale(self):
        r=self.store.directory(protocol_version=2)['rooms'][0]
        r['state']='playing';r['display']={'settings':{'freestyle':True,'countdown_seconds':15,'selection_style':'variety'}}
        f={'room_id':12345,'fresh_until':1100,'current_map':{'cover_url':'https://assets.ppy.sh/beatmaps/123/covers/cover.jpg'},'playback':{'beatmap_id':100,'started_at':990,'duration_seconds':90}}
        c=card(r,5,f,now=1000);self.assertTrue(c['image_url']);self.assertEqual(len(c['extra_fields']),2)
        c=card({**r,'freshness':'stale'},5,f,now=1000);self.assertFalse(c['is_open']);self.assertEqual(len(c['extra_fields']),1)
        self.assertEqual(card(r,5,{**f,'fresh_until':900},now=1000)['image_url'],'')

    def test_bridge_rejects_reserved_owner_channel(self):
        self.error(Bridge,self.store,{'channels':{'5':1005},'owner_channels':[1005],'owner_host_id':self.owner,'discord':{}})


class RelayHTTPSTests(unittest.TestCase):
    from tests.test_network_https import NetworkHTTPSTests as _HTTP
    setUpClass=classmethod(_HTTP.setUpClass.__func__)
    tearDownClass=classmethod(_HTTP.tearDownClass.__func__)
    setUp=_HTTP.setUp
    stop=_HTTP.stop

    def test_real_tls_authenticated_poll_dispatch_receipt_and_reply(self):
        import time
        host=self.transport.request('POST','/v1/enrollments',body=self.enrollment)['host_id']
        self.transport.request('POST','/v1/hosts/self/heartbeat',token=self.token,body={'protocol_version':1,**{k:self.binding[k]for k in ('installation_id','generation')},'sequence':0,'code_version':'v7.2.7','catalog_version':'a'*64,'rooms':[room(time.time())]})
        repo=Repository(self.store);repo.assign(self.store.directory()['rooms'],{5:1005},owner_host=str(uuid.uuid4()))
        body={'protocol_version':1,**{k:self.binding[k]for k in ('installation_id','generation')},'sent_at':time.time(),'receipts':[],'replies':[]}
        self.assertEqual(self.transport.request('POST',PATH,token=self.token,body=body)['requests'],[])
        rid=repo.queue(host,'room1',{'discord_user_id':777,'username':'Invented','role':'ADMIN'},channel_id=1005,content='.settings')
        response=self.transport.request('POST',PATH,token=self.token,body=body);self.assertEqual(response['requests'][0]['id'],rid)
        self.assertTrue(self.transport.last_worker_reaped)
        response=self.transport.request('POST',PATH,token=self.token,body={**body,'receipts':[{'id':rid,'result':'dispatched'}],'replies':[{'id':str(uuid.uuid4()),'request_id':rid,'content':'Native settings reply'}]})
        self.assertEqual(response['requests'],[]);self.assertEqual(repo.take_replies()[0]['content'],'Native settings reply')

class BridgeTests(RelayTests):
    # Select only the bridge cases below when running; avoid duplicating base cases.
    def test_panel_offline_assignment_persists_across_bridge_restart(self):
        cfg={'channels':{'5':1005,'6':1006},'owner_channels':[1001,1002,1003,1004],
             'owner_host_id':self.owner,'discord':{'discord_control_channel_id':1005},'feed':str(self.root/'missing-feed')}
        d=CommunityDiscord(cfg['discord']);d._thread=object()
        bridge=Bridge(self.store,cfg,discord=d,clock=lambda:self.now)
        with patch.object(bridge,'discover'):
            self.assertEqual(bridge.step()['assigned'],1)
        self.assertTrue(d._take_status(1005).is_open)
        self.store.revoke(self.host)
        d2=CommunityDiscord(cfg['discord']);d2._thread=object()
        second=Bridge(self.store,cfg,discord=d2,clock=lambda:self.now)
        with patch.object(second,'discover'):second.step()
        status=d2._take_status(1005);self.assertFalse(status.is_open);self.assertEqual(status.room_id,0)
        self.assertIn('xikeon',status.channel_base_name);self.assertEqual(d2.routes,{})

    def test_dynamic_discovery_adds_new_numbered_channels_without_reassigning(self):
        cfg={'channels':{'5':1005},'owner_channels':[1001,1002,1003,1004],
             'owner_host_id':self.owner,'discord':{'discord_control_channel_id':1005,'discord_control_guild_id':555},'feed':str(self.root/'missing-feed')}
        d=CommunityDiscord(cfg['discord']);bridge=Bridge(self.store,cfg,discord=d,clock=lambda:self.now)
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps([{'name':'l5-xikeon-standard-autodiff','type':0,'id':'1005'},
            {'name':'lobby-13-community','type':0,'id':'1013'},{'name':'lobby-1-community','type':0,'id':'1099'},
            {'name':'lobby-14-community','type':2,'id':'1014'}]).encode()
        with patch.object(d,'_token',return_value='invented'),patch('urllib.request.urlopen',return_value=response):bridge.discover()
        self.assertEqual(bridge.channels,{5:1005,13:1013})
