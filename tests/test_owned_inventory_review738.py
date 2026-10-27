"""Installed stopped discovery binds stable history without clearing uncertainty."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,json
from unittest.mock import patch
from poolrotater_host.layout import read_json,atomic_json,load
from poolrotater_host.oauth import AuthError
from poolrotater_host.provider_room_recovery import ProviderOwnershipReview
from poolrotater_host.installed_room_recovery import execute
from poolrotater_host.owned_inventory import is_history_path,HISTORY_PATH
import test_v7_provider_rooms as rooms
import test_owned_inventory738 as history


class InventoryReview(rooms.RoomCase):
    def setUp(self):
        super().setUp();auth=self.authorized();self.bind(auth)
        self.journal(room1={'room_id':None,'phase':'recovery_required','operation':'make_pending',
                            'ownership_unknown':True})
        self.before=read_json(self.root/'state/rolling/ownership.json')
        self.provider.history_rooms=[history.room(101)]
    def inspect_history(self):return execute(self.authority(),'inventory')
    def test_stable_ended_candidate_is_reported_with_binding_but_never_adopted(self):
        report=self.inspect_history()
        self.assertEqual(report['passes'],2);self.assertEqual(report['requests'],4)
        self.assertEqual(report['rooms'][0]['room_id'],101)
        self.assertTrue(report['history_stable']and report['journal_unchanged'])
        self.assertFalse(report['absence_recovery_qualified']or report['recovery_applied']or report['start_allowed'])
        self.assertEqual(len(report['evidence_sha256']),64)
        self.assertEqual(read_json(self.root/'state/rolling/ownership.json'),self.before)
        self.assertFalse((self.root/'state/ownership-recovery.json').exists())
        raw=json.dumps(report)
        for value in ('private room title','not-for-the-report','access_token','client_secret'):
            self.assertNotIn(value,raw)
        calls=[r for r in self.provider.calls if is_history_path(r['path'])]
        self.assertTrue(all(r['method']=='GET'for r in calls))
    def test_empty_complete_history_never_resolves_unknown_creation(self):
        self.provider.history_rooms=[];report=self.inspect_history()
        self.assertEqual(report['rooms'],[]);self.assertEqual(report['requests'],2)
        self.assertFalse(report['absence_recovery_qualified']or report['recovery_applied'])
        self.assertIn('upstream_creation_outcome',report['next_action'])
        self.assertEqual(read_json(self.root/'state/rolling/ownership.json'),self.before)
    def test_changed_history_between_complete_passes_refuses(self):
        starts=[]
        def change(path):
            if path==HISTORY_PATH:
                starts.append(path)
                if len(starts)==2:self.provider.history_rooms=[history.room(102)]
        self.provider.on_read=change
        with self.assertRaises(AuthError)as error:self.inspect_history()
        self.assertEqual(error.exception.code,'ownership_inventory_changed')
        self.assertEqual(read_json(self.root/'state/rolling/ownership.json'),self.before)
    def test_changed_journal_during_inventory_refuses(self):
        def change(path):
            if path==HISTORY_PATH:
                changed=copy.deepcopy(self.before);changed['rooms']['room1']['error']='new_failure'
                atomic_json(self.root/'state/rolling/ownership.json',changed)
        self.provider.on_read=change
        with self.assertRaises(AuthError)as error:self.inspect_history()
        self.assertEqual(error.exception.code,'ownership_journal_changed')
    def test_changed_configuration_during_inventory_refuses(self):
        def change(path):
            if path==HISTORY_PATH:
                marker,config=load(self.root);config['rooms'][0]['sr_min']=3.0
                atomic_json(self.root/'config/host.json',config)
        self.provider.on_read=change
        with self.assertRaises(AuthError)as error:self.inspect_history()
        self.assertIn(error.exception.code,('ownership_journal_changed','authorization_binding_changed','provider_room_binding_changed'))
    def test_inventory_rejects_recovery_choices_before_authentication(self):
        for choices in ({'journal_sha256':'a'*64},{'alias':'room1','room_id':101,'confirm_lost_creation':True},
                        {'transaction_id':'invented','decision':'complete'}):
            with self.subTest(choices=choices),self.assertRaises(AuthError)as error:
                execute(None,'inventory',**choices)
            self.assertEqual(error.exception.code,'ownership_recovery_explicit_choices_required')

    def test_whole_review_budget_refuses_before_history_dispatch(self):
        with patch('poolrotater_host.owned_inventory.MAX_SECONDS',0):
            with self.assertRaises(AuthError)as error:self.inspect_history()
        self.assertEqual(error.exception.code,'ownership_inventory_incomplete')
        self.assertFalse(any(is_history_path(r['path'])for r in self.provider.calls))
        self.assertEqual(read_json(self.root/'state/rolling/ownership.json'),self.before)
