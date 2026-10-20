"""Explicit addition preserves existing topology, tuning and shared identity."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import unittest
import uuid

from poolrotater_host.catch_pilot_configuration import project
from poolrotater_host.config import defaults, ConfigError
from poolrotater_host.owner_integrations import PATHS, ENVIRONMENT_FILE


class CatchConfigurationTransitionTests(unittest.TestCase):
    def setUp(self):
        self.marker={'installation_id':str(uuid.uuid4()),'generation':str(uuid.uuid4())}
        self.host=defaults(['standard-fixed','mania-7k','mania-4k'])
        for i,row in enumerate(self.host['rooms'],2):row['alias']='room'+str(i)
        self.host['execution']='pilot-live'
        self.gameplay={'format':1,**self.marker,'rooms':{r['alias']:{'preset':r['preset'],'settings':{
            'room_name':'Retain '+r['alias'],'countdown_seconds':23,'queue_ahead':2}}
            for r in self.host['rooms']}}
        aliases=[r['alias']for r in self.host['rooms']]
        channels={a:100000000000000001+i for i,a in enumerate(aliases)}
        self.integration={'format':1,**self.marker,'environment_file':ENVIRONMENT_FILE,'rooms':{
            a:{'settings':{'discord_control_enabled':True,'discord_control_channel_id':channels[a],
                           'discord_control_known_room_aliases':aliases,
                           'discord_control_channel_ids_by_room':dict(channels),
                           'discord_control_room_alias':a,'discord_control_primary_instance':'room2',
                           'discord_control_bot_token_env':'POOLROTATER_DISCORD_BOT_TOKEN',
                           'custom_join_greetings':{'123':'Welcome back!'}},
               'paths':{k:False for k in PATHS}}for a in aliases}}
        self.retained={'discord_control_channel_id':100000000000000009}

    def make(self,**kw):return project(self.marker,self.host,self.gameplay,self.integration,self.retained,**kw)

    def test_only_explicit_new_room_is_added_and_existing_objects_are_unchanged(self):
        before=copy.deepcopy((self.host,self.gameplay,self.integration,self.retained))
        result=self.make()
        self.assertEqual(result['host']['rooms'][:-1],self.host['rooms'])
        for key in self.host:
            if key!='rooms':self.assertEqual(result['host'][key],self.host[key])
        for alias,row in self.gameplay['rooms'].items():self.assertEqual(result['gameplay']['rooms'][alias],row)
        self.assertEqual((self.host,self.gameplay,self.integration,self.retained),before)
        self.assertEqual(result['host']['rooms'][-1]['preset'],'catch-fixed')
        self.assertFalse(result['provider_dispatch'])

    def test_catch_uses_operational_tuning_and_retained_channel_without_standard_filters(self):
        self.gameplay['rooms']['room2']['settings'].update(min_passcount=18000,stream_enabled=True,
            freestyle=True,required_mods=[],allowed_mods=[])
        result=self.make();cfg=result['gameplay']['rooms']['room1']['settings']
        self.assertEqual((cfg['countdown_seconds'],cfg['queue_ahead']),(23,2))
        self.assertEqual(cfg['min_passcount'],0)
        for field in ('auto_difficulty','freestyle','catch_include_loved','stream_enabled','mania_farm_enabled'):
            self.assertIs(cfg.get(field,False),False)
        row=result['integrations']['rooms']['room1']['settings']
        self.assertEqual(row['discord_control_channel_id'],self.retained['discord_control_channel_id'])
        self.assertEqual(row['custom_join_greetings'],{'123':'Welcome back!'})
        self.assertEqual(row['discord_control_primary_instance'],'room2')
        self.assertEqual(row['discord_control_bot_token_env'],'POOLROTATER_DISCORD_BOT_TOKEN')

    def test_only_required_alias_routing_fields_change_on_existing_discord_rooms(self):
        result=self.make()['integrations']['rooms']
        changed={'discord_control_known_room_aliases','discord_control_channel_ids_by_room'}
        for alias,old in self.integration['rooms'].items():
            self.assertEqual(result[alias]['paths'],old['paths'])
            for key,value in old['settings'].items():
                if key not in changed:self.assertEqual(result[alias]['settings'][key],value)
            self.assertEqual(result[alias]['settings']['discord_control_known_room_aliases'],['room1','room2','room3','room4'])

    def test_capacity_or_occupied_alias_is_not_replaced(self):
        with self.assertRaises(ConfigError):self.make(alias='room2')
        self.host['rooms'].append({**self.host['rooms'][-1],'alias':'other'})
        self.gameplay['rooms']['other']={'preset':'mania-4k','settings':{}}
        with self.assertRaises(ConfigError):self.make()

    def test_missing_discord_route_or_credentials_in_input_refuse(self):
        self.retained={}
        with self.assertRaisesRegex(ConfigError,'retained Discord'):self.make()
        self.retained={'access_token':'invented-not-a-real-token'}
        with self.assertRaisesRegex(ConfigError,'not credentials'):self.make()

    def test_no_integrations_or_overlay_does_not_invent_credentials(self):
        result=project(self.marker,self.host,None,None,{})
        self.assertIsNone(result['integrations'])
        self.assertEqual(len(result['gameplay']['rooms']),4)
        self.assertNotIn('token',repr(result['gameplay']).lower())


if __name__=='__main__':unittest.main()
