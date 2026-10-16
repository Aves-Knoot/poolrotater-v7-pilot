"""Source-side private diagnostic sink; raw native logging remains unchanged."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import time

from .layout import atomic_json

_CHAT=re.compile(r'(?i)(^CHAT\b|command from|public command suppressed|chat silenced|AI request queued|sending (?:chat|message)|(?:received|sent) (?:chat|message))')
_ALIAS=re.compile(r'room[0-9]+\Z')


class ConsentFilter(logging.Filter):
    def __init__(self,root):super().__init__();self.root=root;self.until=0;self.value=None
    def filter(self,record):
        if time.monotonic()>=self.until:
            from .reporting import profile
            try:self.value=profile(self.root)
            except Exception:self.value=None
            self.until=time.monotonic()+1
        if not self.value or not self.value['operational_logs']:return False
        if _CHAT.search(record.getMessage()) and not self.value['public_chat']:return False
        return True


def witness(root,alias,generation,*,room_id,public):
    """Called only after authoritative broker state changes, before public use."""
    if not _ALIAS.fullmatch(alias):return
    root=Path(root)
    atomic_json(root/'state'/('reporting-visibility-'+alias+'.json'),{
        'format':1,'worker_generation':generation,'room_id':room_id,'public':bool(public),'at':time.time()})


def install(root,*,alias=None,version='unknown'):
    from .diagnostic_logs import SanitizedJSONFormatter
    from .reporting import CachedSecrets
    root=Path(root)
    if alias is not None and not _ALIAS.fullmatch(alias):return
    name='controller' if alias is None else alias
    path=root/'logs'/('reporting-'+name+'.jsonl')
    logger=logging.getLogger()
    if any(getattr(h,'_reporting_sink',None)==str(path) for h in logger.handlers):return
    def context(record):
        tag=alias if alias else record.threadName if _ALIAS.fullmatch(record.threadName) else None
        kind='command' if 'command from' in record.getMessage() or 'public command suppressed' in record.getMessage() else 'chat' if _CHAT.search(record.getMessage()) else 'operational'
        # Diagnostics cover private rooms too. The independent statistics and
        # directory visibility witnesses retain their original privacy rules.
        return {'room':tag,'private':False,'kind':kind,'version':version}
    handler=RotatingFileHandler(path,maxBytes=8*1024**2,backupCount=4,encoding='utf-8',delay=True)
    handler._reporting_sink=str(path);handler.setLevel(logging.DEBUG)
    handler.addFilter(ConsentFilter(root))
    handler.setFormatter(SanitizedJSONFormatter(secret_provider=CachedSecrets(root),context_provider=context,version=version))
    logger.addHandler(handler)


def record_visibility(path,*,room_id,public,mode,alias):
    """Append native evidence without depending on optional rating contribution."""
    import sqlite3
    from .ratings.migration import _private
    path=_private(Path(path))
    conn=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=.2)
    try:
        conn.execute('BEGIN IMMEDIATE')
        now=time.time()
        previous=conn.execute('SELECT public,mode,room_alias,observed_at FROM activity_visibility WHERE room_id=? ORDER BY observed_at DESC,id DESC LIMIT 1',(room_id,)).fetchone()
        if previous and now<previous[3]:raise ValueError('activity_visibility_clock_rollback')
        if previous is None or previous[:3]!=(int(public),mode,alias):
            conn.execute('INSERT INTO activity_visibility(room_id,public,observed_at,mode,room_alias) VALUES(?,?,?,?,?)',(room_id,int(public),now,mode,alias))
        conn.commit()
    finally:conn.close()
