"""Lost edit completions resolve only from full matching, catalog-verified truth."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import hashlib,json,unittest
from pathlib import Path
from unittest.mock import Mock
from bot.bot import LobbyBot
from bot.hub import HubInvocationUncertain,HubDisconnected
from bot.standard_pool import StandardPool
import test_standard_dt as fixtures

class UncertainStandardEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder=Path(__file__).resolve().parents[1]
        manifest=json.loads((folder/'catalog-manifest.json').read_text());files={entry['role']:entry for entry in manifest['files']}
        assert files['catalog']['tables']['beatmaps']>100000 and files['dt']['tables']['dt_maps']>100000
        for item in files.values():assert hashlib.sha256((folder/item['name']).read_bytes()).hexdigest()==item['sha256']
        cls.pool=StandardPool(str(folder/files['catalog']['name']),standard_dt_db=str(folder/files['dt']['name']),
            standard_catalog_binding={'source_sha256':files['catalog']['sha256'],'dt_sha256':files['dt']['sha256']},
            sr_min=3,sr_max=8.2,max_length=240,min_passcount=0,limit=400)
        cls.nm=cls.pool.get_variant_map(3185854,[]);assert cls.nm and not cls.nm.is_dt
        cls.old=next(m for m in cls.pool.maps if not m.is_dt and m.beatmap_id!=3185854)
        cls.dt=next(m for m in cls.pool.maps if m.is_dt and m.beatmap_id!=cls.old.beatmap_id)
        cls.other=next(m for m in cls.pool.maps if not m.is_dt and m.beatmap_id not in (cls.nm.beatmap_id,cls.old.beatmap_id))

    def bot(self):
        bot=fixtures.DTPlaylistTests().bot();bot.pool=self.pool
        bot._sync_countdown_wait_item=LobbyBot._sync_countdown_wait_item.__get__(bot,LobbyBot)
        bot.hub.edit_current_item=Mock(return_value={'id':200})
        bot._prefetch_map_pp=Mock();bot._notify_discord_lobby_event=Mock()
        bot.apply_map(self.old)
        bot.hub.edit_current_item.reset_mock()
        bot.hub.edit_current_item.side_effect=HubInvocationUncertain('invented completion lost')
        return bot

    def packet(self,bot,m,**changes):
        packet={'id':200,'beatmap_id':m.beatmap_id,'order':0,'ruleset_id':0,
                **bot._playlist_rules_for_map(m)}
        if m.is_dt:packet['star_rating']=m.sr
        return packet|changes

    def lose(self,bot,m):
        with self.assertRaises(HubInvocationUncertain):bot.apply_map(m)
        self.assertEqual(bot.current_map.beatmap_id,self.old.beatmap_id)
        self.assertTrue(bot._standard_edit_audit_pending())
        bot.hub.edit_current_item.assert_called_once()

    def reconcile(self,bot,packet):
        bot.api.room_probe=Mock(return_value=('ok',{'playlist':[packet]}))
        return LobbyBot.reconcile_playlist(bot,'lost completion regression',fetch_remote=True,ensure_depth=False)

    def test_verified_nm_snapshot_adopts_remote_edit_without_replay_and_resets_wait(self):
        bot=self.bot();bot._countdown_wait_requesters={99}
        self.lose(bot,self.nm)
        self.assertTrue(self.reconcile(bot,self.packet(bot,self.nm)))
        self.assertEqual(bot.current_map.beatmap_id,3185854)
        self.assertFalse(bot._standard_edit_audit_pending())
        self.assertFalse(bot._server_playlist_items[200]['standard_invalid'])
        self.assertFalse(bot._countdown_wait_requesters)
        bot.hub.edit_current_item.assert_called_once()

    def test_native_skip_removes_future_once_then_reconciles_applied_uncertain_edit(self):
        bot=self.bot();bot.queued=[(201,self.nm)]
        bot._map_is_downloadable=Mock(return_value=True)
        with self.assertRaises(HubInvocationUncertain):bot.skip_map()
        bot.hub.remove_playlist_item.assert_called_once_with(bot.room_id,201)
        bot.hub.edit_current_item.assert_called_once()
        self.assertEqual(bot.current_map.beatmap_id,self.old.beatmap_id)
        self.assertTrue(self.reconcile(bot,self.packet(bot,self.nm)))
        self.assertEqual(bot.current_map.beatmap_id,3185854)
        bot.hub.remove_playlist_item.assert_called_once()
        bot.hub.edit_current_item.assert_called_once()

    def test_verified_dt_snapshot_keeps_real_rating_and_mod_identity(self):
        bot=self.bot();self.lose(bot,self.dt)
        self.assertTrue(self.reconcile(bot,self.packet(bot,self.dt)))
        self.assertTrue(bot.current_map.is_dt);self.assertEqual(bot.current_map.sr,self.dt.sr)
        self.assertFalse(bot._standard_edit_audit_pending());bot.hub.edit_current_item.assert_called_once()

    def test_old_or_partial_snapshot_cannot_guess_that_edit_failed_or_promote(self):
        for partial in (False,True):
            with self.subTest(partial=partial):
                bot=self.bot();self.lose(bot,self.nm)
                item={'id':200,'beatmap_id':self.nm.beatmap_id} if partial else self.packet(bot,self.old)
                self.assertFalse(self.reconcile(bot,item));self.assertTrue(bot._standard_edit_audit_pending())
                self.assertEqual(bot.current_map.beatmap_id,self.old.beatmap_id)
                bot.hub.edit_current_item.assert_called_once()

    def test_event_alone_is_not_authoritative_then_full_snapshot_resolves(self):
        bot=self.bot();self.lose(bot,self.nm)
        bot._remember_playlist_item(self.packet(bot,self.nm))
        self.assertTrue(bot._standard_edit_audit_pending())
        self.assertTrue(self.reconcile(bot,self.packet(bot,self.nm)))
        self.assertEqual(bot.current_map.beatmap_id,self.nm.beatmap_id)

    def test_wrong_chart_ruleset_mods_speed_star_or_missing_catalog_still_refuse(self):
        for change in ({'beatmap_id':self.other.beatmap_id},{'beatmap_id':999999999},
                       {'ruleset_id':3},{'required_mods':[]},
                       {'required_mods':[{'acronym':'DT','settings':{'speed_change':1.25}}]},
                       {'star_rating':self.dt.sr+.2},{'freestyle':False}):
            with self.subTest(change=change):
                bot=self.bot();self.lose(bot,self.dt)
                self.assertFalse(self.reconcile(bot,self.packet(bot,self.dt,**change)))
                self.assertTrue(bot._standard_edit_audit_pending())
                self.assertEqual(bot.current_map.beatmap_id,self.old.beatmap_id)
                bot.hub.edit_current_item.assert_called_once()

    def test_disconnected_before_dispatch_does_not_invent_an_uncertain_edit(self):
        bot=self.bot();before=repr(bot._standard_playlist_edits)
        bot.hub.edit_current_item.side_effect=HubDisconnected('invented disconnected before send')
        with self.assertRaises(HubDisconnected):bot.apply_map(self.nm)
        self.assertEqual(repr(bot._standard_playlist_edits),before)
        self.assertTrue(self.reconcile(bot,self.packet(bot,self.old)))

    def test_definite_business_rejection_does_not_add_pending_revision(self):
        bot=self.bot();before=repr(bot._standard_playlist_edits)
        bot.hub.edit_current_item.side_effect=RuntimeError('invented rejected edit')
        with self.assertRaises(RuntimeError):bot.apply_map(self.nm)
        self.assertEqual(repr(bot._standard_playlist_edits),before)
        self.assertEqual(bot.current_map.beatmap_id,self.old.beatmap_id)

if __name__=='__main__':unittest.main()
