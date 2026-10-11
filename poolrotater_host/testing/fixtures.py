"""Generate small deterministic catalogs with invented map/player data."""
import sqlite3
from contextlib import closing
from pathlib import Path
from ..config import contained
from ..layout import load


def create_catalogs(root: Path):
    _, config = load(root)
    for room in config['rooms']:
        path = contained(root, room['catalog'])
        if path.exists():
            continue  # Never overwrite an operator's installed catalog.
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        mania = room['preset'].startswith('mania-')
        keys = int(room['preset'][-2]) if mania else None
        with closing(sqlite3.connect(path)) as db, db:
            if mania:
                db.executescript('''CREATE TABLE maps(beatmap_id INTEGER PRIMARY KEY, artist TEXT, title TEXT,
                    version TEXT, sr REAL, hit_length INTEGER, passcount INTEGER, mode_int INTEGER,
                    key_count INTEGER, status TEXT, ranked_date TEXT);
                    CREATE TABLE mania_features(beatmap_id INTEGER PRIMARY KEY, jack_score REAL,
                    chordjack_score REAL, stream_score REAL, ln_score REAL, technical_score REAL);''')
                for i in range(1, 13):
                    db.execute('INSERT INTO maps VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (i, 'Fixture Artist', f'Fixture Song {i}', f'{keys}K', 3.5, 120, 20000, 3, keys, 'ranked', '2026-01-01'))
                    db.execute('INSERT INTO mania_features VALUES(?,?,?,?,?,?)', (i, .5, .5, .5, .1, .5))
            else:
                db.executescript('''CREATE TABLE beatmaps(beatmap_id INTEGER PRIMARY KEY, artist TEXT, title TEXT,
                    version TEXT, sr REAL, hit_length INTEGER, passcount INTEGER, status TEXT, ranked_date TEXT);
                    CREATE TABLE farm_report(beatmap_id INTEGER PRIMARY KEY, artist TEXT, title TEXT,
                    version TEXT, sr REAL, hit_length INTEGER, passcount INTEGER, farm_score REAL,
                    quadrant TEXT, forgiveness REAL, max_pp REAL, dominant_mods TEXT);''')
                for i in range(1, 13):
                    row = (i, 'Fixture Artist', f'Fixture Song {i}', 'Hard', 5.0, 120, 20000)
                    db.execute('INSERT INTO beatmaps VALUES(?,?,?,?,?,?,?,?,?)', (*row, 'ranked', '2026-01-01'))
                    db.execute('INSERT INTO farm_report VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (*row, 100-i, 'balanced', .5, 200, 'NM'))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    create_catalogs(parser.parse_args().root)
