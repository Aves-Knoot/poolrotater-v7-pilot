import unittest
from types import SimpleNamespace

from poolrotater_host.bookings.worker import (
    SelectionError,
    apply_booking_settings,
    clear_selection,
    install_selection,
)


def map_item(beatmap_id, length):
    return SimpleNamespace(beatmap_id=beatmap_id, hit_length=length,
                           approach_rate=7.0, is_dt=False, clock_rate=1.0)


class WorkerPool:
    def __init__(self, maps):
        self.maps = list(maps)
        self._recent = []
        self._playlist_reserved = set()

    def next_map(self):
        return self.maps[0] if self.maps else None


class BookingWorkerTests(unittest.TestCase):
    def test_worker_startup_uses_fail_closed_selection_adapter(self):
        good, bad = map_item(1, 90), map_item(2, 240)
        pool = WorkerPool([good, bad])
        count = install_selection(pool, {
            'selection_filters': {
                'length': {'min': 60, 'max': 120, 'mode': 'hard'},
            },
        }, 'standard-fixed')
        self.assertEqual(count, 1)
        self.assertIs(pool.next_map(), good)
        clear_selection(pool)
        self.assertEqual([item.beatmap_id for item in pool.maps], [1, 2])

    def test_main_loop_settings_callback_commits_only_after_fresh_pool_passes_filter(self):
        original = WorkerPool([map_item(3, 80)])
        bot = SimpleNamespace(
            cfg={'ruleset_id': 0, 'sr_min': 3.5, 'sr_max': 4.5,
                 'auto_difficulty': False, '_supervisor_overrides': {}},
            pool=original,
            manual_range=True,
        )
        constructed = []

        def materialize(staged_cfg):
            constructed.append(staged_cfg)
            return WorkerPool([map_item(4, 90), map_item(5, 180)])

        result = apply_booking_settings(bot, {
            'selection_filters': {
                'length': {'min': 60, 'max': 120, 'mode': 'hard'},
            },
        }, 'standard-fixed', materialize=materialize)
        self.assertEqual(result['application'], 'next_map')
        self.assertIsNot(bot.pool, original)
        self.assertEqual(bot.pool.next_map().beatmap_id, 4)
        self.assertEqual(len(constructed), 1)
        self.assertEqual(constructed[0]['_supervisor_overrides']['selection_filters'],
                         bot._booking_session_settings['selection_filters'])

    def test_materializer_is_required_before_a_settings_change_can_be_applied(self):
        bot = SimpleNamespace(cfg={'ruleset_id': 0}, pool=WorkerPool([]))
        with self.assertRaises(SelectionError):
            apply_booking_settings(bot, {}, 'standard-fixed', materialize=None)

    def test_full_default_snapshot_keeps_live_receipt_for_live_only_change(self):
        bot = SimpleNamespace(
            cfg={'ruleset_id': 0, 'sr_min': 3.5, 'sr_max': 4.5,
                 'auto_difficulty': False, '_supervisor_overrides': {}},
            pool=WorkerPool([map_item(6, 90)]),
            manual_range=True,
        )
        result = apply_booking_settings(bot, {'countdown_seconds': 30}, 'standard-fixed',
                                        materialize=lambda _cfg: WorkerPool([map_item(7, 90)]))
        self.assertEqual(result['application'], 'live')


if __name__ == '__main__':
    unittest.main()
