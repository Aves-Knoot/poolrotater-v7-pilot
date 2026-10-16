"""Additive native-ledger evidence. No existing totals or tables are rewritten."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS activity_visibility (
 id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL,
 public INTEGER NOT NULL CHECK(public IN (0,1)), observed_at REAL NOT NULL,
 mode TEXT NOT NULL, room_alias TEXT);
CREATE INDEX IF NOT EXISTS activity_visibility_room_time ON activity_visibility(room_id,observed_at,id);
CREATE TABLE IF NOT EXISTS activity_presence (
 id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
 username TEXT NOT NULL,started_at REAL NOT NULL,ended_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS activity_joins (
 id INTEGER PRIMARY KEY AUTOINCREMENT,room_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
 username TEXT NOT NULL,occurred_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS activity_leaves (
 id INTEGER PRIMARY KEY AUTOINCREMENT,room_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
 username TEXT NOT NULL,occurred_at REAL NOT NULL);
'''


def prepare(conn):
    """Called by the coordinated ledger migration, never opportunistically by an agent."""
    for statement in SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)
