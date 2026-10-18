"""Real .stats command path with cold caches, visibility gates and missing hub."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import tempfile
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock
from bot.bot import LobbyBot
from bot.ppstats import PPStatsStore
from poolrotater_host.activity.reader import Reader


class ActivityCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);(self.root/'state').mkdir()
        self.reader=Reader(self.root)
        self.bot=LobbyBot.__new__(LobbyBot)
        b=self.bot;b.cfg={'ruleset_id':0};b.prefix='.';b.players={7:{'name':'Local'},8:{'name':'Requester'}}
        b._is_admin=Mock(return_value=False);b._is_moderator=Mock(return_value=False)
        b._is_project_banned=Mock(return_value=False);b._allow_public_command_burst=Mock(return_value=True)
        b.say=Mock();b.pp_stats=PPStatsStore(str(self.root/'native.sqlite'),reader=self.reader)
        with b.pp_stats._connect() as db:
            db.execute("INSERT INTO autohost_lobby_time VALUES(7,'Local',60,1,1,1,61,61)")

    def test_self_lookup_uses_local_history_immediately_and_queues_background_fetch(self):
        self.bot.handle_command(7,'.stats')
        self.assertNotIn('shared stats',self.bot.say.call_args.args[0])
        self.assertIn('0:01',self.bot.say.call_args.args[0])
        self.assertEqual(self.reader.status()['pending_queries'],1)

    def test_unknown_remote_username_returns_useful_syncing_reply_not_worker_exception(self):
        self.bot.handle_command(8,'.stats Remote User')
        self.assertIn('fetching stats for Remote User',self.bot.say.call_args.args[0])
        self.assertEqual(self.reader.status()['pending_queries'],1)

    def test_other_user_unknown_network_privacy_fails_closed(self):
        self.bot.handle_command(8,'.stats Local')
        message=self.bot.say.call_args.args[0]
        self.assertIn('fetching stats for Local',message);self.assertNotIn('Playtime:',message)

    def test_completed_miss_does_not_keep_claiming_stats_are_syncing(self):
        self.bot.handle_command(8,'.stats Remote User')
        with sqlite3.connect(self.reader.path) as db:
            db.execute('INSERT INTO lookups VALUES(?,?,?)',('remote user',0,self.reader.clock()))
            db.execute('DELETE FROM queries')
        self.bot.handle_command(8,'.stats Remote User')
        message=self.bot.say.call_args.args[0]
        self.assertIn('no shared AutoHost stats are available',message)
        self.assertNotIn('syncing',message)
        self.assertEqual(self.reader.status()['pending_queries'],0)

    def test_local_hide_still_wins_without_hub_and_self_lookup_remains_available(self):
        self.bot.pp_stats.set_stats_hidden(7,'Local',True)
        self.bot.handle_command(8,'.stats Local');self.assertIn('has hidden',self.bot.say.call_args.args[0])
        self.bot.handle_command(7,'.stats');self.assertIn('Playtime:',self.bot.say.call_args.args[0])

    def test_missing_reader_retains_existing_local_behavior(self):
        self.bot.pp_stats.reader=None
        self.bot.handle_command(8,'.stats Local')
        self.assertIn('Playtime:',self.bot.say.call_args.args[0]);self.assertNotIn('Shared',self.bot.say.call_args.args[0])

if __name__=='__main__':unittest.main()
