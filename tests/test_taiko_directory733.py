"""Additive Taiko public directory keeps old consumer schemas intact."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import unittest
from tests.test_network_store import NetworkStoreTests
from tests.test_network_display import rich


class TaikoDirectory733Tests(unittest.TestCase):
    setUp=NetworkStoreTests.setUp
    enroll=NetworkStoreTests.enroll
    heartbeat=NetworkStoreTests.heartbeat

    def test_taiko_is_only_in_v3_while_standard_remains_in_all_versions(self):
        self.enroll()
        wire=self.heartbeat();wire.update(protocol_version=2,sent_at=990)
        first=wire['rooms'][0];first.update(state='playing',observed_at=990,display=rich())
        second=copy.deepcopy(first)
        second.update(alias='room2',osu_room_id=first['osu_room_id']+1,preset='taiko-auto')
        second['display']['settings'].update(ruleset_id=1,allowed_mods=[],required_mods=[],freestyle=False)
        wire['rooms'].append(second)
        self.store.heartbeat(self.token,wire,protocol_version=2)
        for version in (1,2,3):
            result=self.store.directory(protocol_version=version)
            aliases={row['alias'] for row in result['rooms']}
            self.assertEqual(aliases,{'room1','room2'} if version==3 else {'room1'})
        taiko=next(row for row in self.store.directory(protocol_version=3)['rooms'] if row['alias']=='room2')
        self.assertEqual(taiko['display']['settings']['ruleset_id'],1)
        self.assertTrue(taiko['display']['settings']['auto_difficulty'])
        self.assertEqual(taiko['current_map']['stars'],second['current_map']['stars'])
