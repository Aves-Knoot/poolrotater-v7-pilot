"""Fixed private password projection for the existing diagnostic redactor."""
import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
from ..diagnostic_logs.common import check_database


def values(root):
    path=Path(root)/'state/managed-host.sqlite3'
    if not os.path.lexists(path):return ()
    check_database(path);found=set()
    def add(value):
        if type(value) is dict:
            if type(value.get('password')) is str and value['password']:found.add(value['password'])
            patch=value.get('patch')
            if type(patch) is dict and type(patch.get('password')) is str and patch['password']:found.add(patch['password'])
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        for row in db.execute('SELECT config,effective FROM rooms'):
            for raw in row:add(json.loads(raw))
        for row in db.execute('SELECT payload,previous_config FROM operations ORDER BY created DESC LIMIT 512'):
            for raw in row:
                if raw is not None:add(json.loads(raw))
    return tuple(found)
