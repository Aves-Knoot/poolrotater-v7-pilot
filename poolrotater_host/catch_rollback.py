"""Refuse legacy code rollback once native Catch state has been initialized.

The supported post-pilot rollback removes the Catch configuration/catalog while
retaining context-aware code and history. Older v7.1 readers cannot consume
pending context-format feedback or Catch activity; never silently drop it.
"""
import sqlite3
from pathlib import Path
from .config import ConfigError
from .ratings.operations import _identity
from .layout import load


def require_target(root,prefix,uid,before,after):
    from .updates.contracts import version
    if version(before['version']) < (7,2,0) or version(after['version']) >= (7,2,0):return
    with _identity(uid):
        _,host=load(root)
        initialized=any(r['preset']=='catch-fixed'for r in host['rooms'])
        path=Path(root)/'state/controller-ratings.db'
        if path.exists():
            with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)as db:
                initialized=initialized or bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='map_ratings_catch' AND type='table'").fetchone())
    if initialized or (Path(prefix)/'catch-pilot-transition-private.json').exists():
        raise ConfigError('Catch state requires a compatible controller; use Catch configuration rollback and retain history')
