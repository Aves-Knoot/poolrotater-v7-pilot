"""Bounded reviewed history backfill on the independent reporting agent.

No provider requests, native ledger writes, room operations or consent changes.
An installation-bound public-room review is required; new sharing defaults do
not constitute a review of pre-telemetry private/public history.
"""
from pathlib import Path
import hashlib
import os
import re
import stat

from ..layout import load, contained, read_json, atomic_json
from ..network.contracts import fields, identifier, fingerprint, require
from .collector import Collector
from .storage import connection

PLAN = 'config/activity-history.json'
RECEIPT = 'state/activity-history-progress.json'
_VERIFIED_SOURCES = {}


def review(root, value):
    required=('format','installation_id','generation','public_room_ids',
              'public_history_confirmed','include_lobby_totals','exclude_unreviewed_players')
    optional=tuple(k for k in ('source','source_sha256') if k in value) if type(value) is dict else ()
    fields(value, (*required,*optional))
    marker,_=load(root)
    require(type(value['format']) is int and value['format']==1,'invalid_history_review')
    for key in ('installation_id','generation'):
        identifier(value[key]);require(value[key]==marker[key],'foreign_history_review')
    rooms=value['public_room_ids']
    require(type(rooms) is list and 1<=len(rooms)<=5000
            and all(type(room) is int and 0<room<2**63 for room in rooms)
            and len(set(rooms))==len(rooms),'invalid_history_public_rooms')
    require(value['public_history_confirmed'] is True
            and type(value['include_lobby_totals']) is bool
            and type(value['exclude_unreviewed_players']) is bool,'explicit_history_confirmation_required')
    source=value.get('source','current')
    require(source in ('current','migration-backup'),'invalid_history_source')
    if source=='migration-backup':
        require(type(value.get('source_sha256')) is str and re.fullmatch('[a-f0-9]{64}',value['source_sha256']),
                'history_source_digest_required')
    else:require('source_sha256' not in value,'unexpected_history_source_digest')
    return {**value,'public_room_ids':sorted(rooms)}


def source(root,value):
    """Only the fixed native migration backup can replace the current ledger."""
    if value.get('source','current')=='current':return root/'state/controller-ratings.db'
    path=contained(root,'backups/ratings-before-v71-activity.sqlite')
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.geteuid()
            and not info.st_mode&0o077 and 0<info.st_size<=2*1024**3,'unsafe_history_source')
    for suffix in ('-wal','-journal'):
        side=Path(str(path)+suffix)
        if os.path.lexists(side):
            side_info=side.lstat()
            require(stat.S_ISREG(side_info.st_mode) and side_info.st_nlink==1
                    and side_info.st_size==0,'history_source_requires_checkpoint')
    stamp=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    key=(str(path),value['source_sha256'])
    if _VERIFIED_SOURCES.get(key)!=stamp:
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        after=path.lstat()
        require(stamp==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
                and digest==value['source_sha256'],'history_source_digest_mismatch')
        if len(_VERIFIED_SOURCES)>=32:_VERIFIED_SOURCES.clear()
        _VERIFIED_SOURCES[key]=stamp
    return path


def step(root, *, limit=200):
    """Called under reporting.lock. Queue chunks; normal uploads drain them.

    The completed receipt is consent-bound. Re-enabling sharing does not silently
    replay an earlier opt-in epoch's historical review.
    """
    root=Path(root);path=contained(root,PLAN)
    if not os.path.lexists(path):return None
    value=review(root,read_json(path,maximum=256*1024))
    ledger=source(root,value)
    collector=Collector(root,ledger_path=ledger,privacy_path=root/'state/controller-ratings.db')
    consent=collector.consent()
    require(consent and consent['enabled'],'statistics_sharing_disabled')
    signature=fingerprint(value)
    receipt_path=contained(root,RECEIPT)
    if os.path.lexists(receipt_path):
        previous=read_json(receipt_path)
        fields(previous,('format','review_sha256','consent_id','phase','historical_events_queued','provider_dispatch'))
        require(previous['format']==1 and previous['review_sha256']==signature,'history_review_changed_requires_review')
        require(previous['consent_id']==consent['consent_id'],'history_consent_changed_requires_review')
        require(previous['phase'] in ('queued','capturing','paused')
                and type(previous['historical_events_queued']) is int and previous['historical_events_queued']>=0
                and previous['provider_dispatch'] is False,'invalid_history_progress')
        if previous['phase']=='queued':return previous
        queued=previous['historical_events_queued']
    else:queued=0
    # Bind before the first chunk too: a crash between enqueue and receipt must
    # not admit a different manifest on the next attempt.
    with connection(collector.path) as db:
        key='history_agent_review_'+consent['consent_id']
        row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        require(row is None or row[0]==signature,'history_review_changed_requires_review')
        db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',(key,signature))
    result=collector.import_history(public_room_ids=value['public_room_ids'],
        public_history_confirmed=True,include_lobby_totals=value['include_lobby_totals'],
        exclude_unreviewed_players=value['exclude_unreviewed_players'],limit=limit)
    phase='queued' if result['history_scan_complete'] else 'paused' if result['history_capture_paused'] else 'capturing'
    receipt={'format':1,'review_sha256':signature,'consent_id':consent['consent_id'],
             'phase':phase,'historical_events_queued':queued+result['historical_events_queued'],'provider_dispatch':False}
    atomic_json(receipt_path,receipt)
    return receipt
