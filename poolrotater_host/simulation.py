"""Bounded fake rounds using real legacy selection and local rating persistence."""
from pathlib import Path
from .catalogs import make_pool
from .config import ConfigError, contained
from .layout import load, owner_lease
from .testing.fakes import FakeReferee


def simulate(root: Path, rounds=3, *, hub=None):
    if type(rounds) is not int or not 1 <= rounds <= 100:
        raise ConfigError('rounds must be an integer in 1..100')
    from bot.ratings import RatingStore
    _, config = load(root)
    hub = FakeReferee() if hub is None else hub
    results = []
    with owner_lease(root):
        # Preflight every room before creating any fake rooms or ledger.
        pools = [(room, make_pool(root, room)) for room in config['rooms']]
        ratings = RatingStore(str(contained(root, 'state/simulation-ratings.db')))
        opened = []
        try:
            for room, pool in pools:
                rid = hub.create(room['alias']); opened.append(rid)
                played = []
                for _ in range(rounds):
                    item = pool.next_map()
                    if item is None: raise ConfigError('eligible pool exhausted')
                    hub.play(rid, item.beatmap_id)
                    pool.record_song_play(item); pool.record_round(item.beatmap_id)
                    ratings.rate(item.beatmap_id, 101, 1)
                    played.append(item.beatmap_id)
                results.append({'alias': room['alias'], 'preset': room['preset'], 'played': played, 'state': 'closed'})
        finally:
            for rid in opened: hub.close(rid)
    return {'ok': True, 'execution': 'offline', 'transport': 'fake', 'rooms': results,
            'rating_store': 'synthetic local simulation ledger; not exported', 'live_ready': False}
