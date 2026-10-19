from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from unittest.mock import patch
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import read_json,atomic_json
from poolrotater_host.runtime_config import RuntimeConfigBridge,STATE_FILE
from tests.test_v7_runtime import NativeCase


class ProfileSchemaUpgradeTests(NativeCase):
    def predecessor(self):
        bridge=RuntimeConfigBridge(self.root,account_id=99);bridge.materialize()
        state=read_json(self.root/STATE_FILE)
        for alias,base in state['bases'].items():
            base.pop('background_player_profiles')
            p=self.room_path(alias);data=read_json(p);data.pop('background_player_profiles')
            data['countdown_seconds']=27;data['queue_ahead']=3;data['moderators']=[117]
            atomic_json(p,data)
        atomic_json(self.root/STATE_FILE,state)
        return bridge

    def test_cached_predecessor_adds_only_managed_flag_and_retains_preferences(self):
        bridge=self.predecessor();before=(self.root/'config/host.json').read_bytes()
        bridge.materialize()
        for alias in read_json(self.root/STATE_FILE)['bases']:
            data=read_json(self.room_path(alias))
            self.assertIs(data['background_player_profiles'],False)
            self.assertEqual(data['countdown_seconds'],27)
            self.assertEqual(data['queue_ahead'],3)
            self.assertEqual(data['moderators'],[117])
        self.assertEqual((self.root/'config/host.json').read_bytes(),before)
        before={p:p.read_bytes()for p in (self.root/'config/rooms').glob('*.json')}
        bridge.materialize();self.assertTrue(all(p.read_bytes()==v for p,v in before.items()))

    def test_conflicting_or_mistyped_new_local_preference_is_not_erased(self):
        bridge=self.predecessor()
        for value in (True,0,'false'):
            p=self.room_path();data=read_json(p);data['background_player_profiles']=value;atomic_json(p,data)
            before=p.read_bytes()
            with self.subTest(value=value),self.assertRaises(ConfigError):bridge.materialize()
            self.assertEqual(p.read_bytes(),before)

    def test_unknown_schema_expansion_keeps_refusing(self):
        bridge=self.predecessor()
        import poolrotater_host.runtime_config as runtime
        original=runtime.render
        def changed(*args,**kwargs):
            data=original(*args,**kwargs)
            for room in data['rooms']:room['settings']['unreviewed_transport_policy']=True
            return data
        with patch.object(runtime,'render',changed),self.assertRaises(ConfigError):bridge.materialize()
