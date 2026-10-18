"""One player request eventually gets one answer; no HTTP or privacy bypass."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import unittest
from unittest.mock import Mock,patch
from bot.bot import LobbyBot

class DeferredStatisticsTests(unittest.TestCase):
 def bot(self):
  b=LobbyBot.__new__(LobbyBot);b.cfg={'ruleset_id':0};b.prefix='.';b.room_id=10;b.channel_id=20
  b.players={8:{'name':'Requester'}};b.say=Mock();b._is_project_banned=Mock(return_value=False)
  b.pp_stats=Mock();b.pp_stats.reader=Mock();b.pp_stats.find_user_by_username.return_value=None
  b.pp_stats.shared_username.return_value=None;b.pp_stats.reader.lookup_status.return_value='pending'
  b.pp_stats.stats_hidden.return_value=False;b.pp_stats.shared_stats_hidden.return_value=False
  b.pp_stats.user_stats.return_value={'total_gain':12.,'achievement_count':2,'best_gain':8.,'total_lobby_seconds':120}
  return b
 def queue(self,b):
  with patch('bot.bot.time.monotonic',return_value=100):self.assertFalse(b._show_player_stats(8,['.stats','Remote','User']))
 def test_cold_name_posts_automatically_after_background_cache_fill(self):
  b=self.bot();self.queue(b);self.assertEqual(b.say.call_count,1);self.assertIn('fetching stats',b.say.call_args.args[0])
  with patch('bot.bot.time.monotonic',return_value=102):b._flush_stats_replies()
  self.assertEqual(b.say.call_count,1)
  b.pp_stats.shared_username.return_value=(9,'Remote User')
  with patch('bot.bot.time.monotonic',return_value=104):b._flush_stats_replies()
  self.assertEqual(b.say.call_count,2);self.assertIn('Playtime: 0:02',b.say.call_args.args[0]);self.assertFalse(b._pending_stats_replies)
  with patch('bot.bot.time.monotonic',return_value=106):b._flush_stats_replies()
  self.assertEqual(b.say.call_count,2)
 def test_privacy_is_rechecked_before_posting(self):
  b=self.bot();self.queue(b);b.pp_stats.shared_username.return_value=(9,'Remote User');b.pp_stats.shared_stats_hidden.return_value=True
  with patch('bot.bot.time.monotonic',return_value=102):b._flush_stats_replies()
  self.assertIn('has hidden',b.say.call_args.args[0]);b.pp_stats.user_stats.assert_not_called();self.assertFalse(b._pending_stats_replies)
 def test_departure_rejoin_room_change_and_channel_change_cancel(self):
  for change in ('leave','rejoin','room','channel','banned'):
   b=self.bot();self.queue(b)
   if change=='leave':b.players.clear()
   if change=='rejoin':b.players[8]=dict(b.players[8])
   if change=='room':b.room_id=11
   if change=='channel':b.channel_id=21
   if change=='banned':b._is_project_banned.return_value=True
   b.pp_stats.shared_username.return_value=(9,'Remote User')
   with patch('bot.bot.time.monotonic',return_value=102):b._flush_stats_replies()
   self.assertEqual(b.say.call_count,1);self.assertFalse(b._pending_stats_replies)
 def test_duplicate_request_does_not_extend_deadline_or_repeat_ack(self):
  b=self.bot();self.queue(b)
  with patch('bot.bot.time.monotonic',return_value=120):b._show_player_stats(8,['.stats','Remote','User'])
  self.assertEqual(b.say.call_count,1);self.assertEqual(next(iter(b._pending_stats_replies.values()))['expires_at'],190)
 def test_expiry_is_bounded_and_does_not_fabricate_totals(self):
  b=self.bot();self.queue(b)
  with patch('bot.bot.time.monotonic',return_value=191):b._flush_stats_replies()
  self.assertIn('unavailable right now',b.say.call_args.args[0]);self.assertNotIn('0.0pp',b.say.call_args.args[0]);self.assertFalse(b._pending_stats_replies)
 def test_negative_lookup_finishes_automatically(self):
  b=self.bot();self.queue(b);b.pp_stats.reader.lookup_status.return_value='not_found'
  with patch('bot.bot.time.monotonic',return_value=102):b._flush_stats_replies()
  self.assertIn('no shared AutoHost stats',b.say.call_args.args[0]);self.assertFalse(b._pending_stats_replies)
 def test_queue_is_bounded(self):
  b=self.bot()
  for n in range(34):
   with patch('bot.bot.time.monotonic',return_value=100):b._queue_stats_reply(8,['.stats',str(n)],str(n),announce=False)
  self.assertEqual(len(b._pending_stats_replies),32)
