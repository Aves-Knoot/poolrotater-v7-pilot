"""Durable independent ratings deadlines; shared service, separate health state."""
import os
import random
import sqlite3
import time

from ..config import ConfigError
from ..layout import contained, read_json, atomic_json, file_lease, LeaseBusy
from ..network.contracts import NetworkError, require, fields, integer, number

PHASES={'not-configured','sharing-disabled','synchronized','query-deferred','retrying','authority-review-required','local-review-required'}
ERRORS={None,'hub-unavailable','authority-refused','local-state-refused'}
CONTENTION_RETRY_SECONDS=8


def validate_receipt(value,marker):
    fields(value,('format','installation_id','generation','phase','error','attempted_at',
                  'next_attempt_at','failures','uploaded','queried','live_rooms_affected'))
    require(type(value['format']) is int and value['format']==1
            and all(value[k]==marker[k] for k in ('installation_id','generation')),'invalid_ratings_agent_receipt')
    require(value['phase'] in PHASES and value['error'] in ERRORS
            and value['live_rooms_affected'] is False,'invalid_ratings_agent_receipt')
    for key in ('attempted_at','next_attempt_at'):number(value[key])
    require(value['attempted_at']+5<=value['next_attempt_at']<=value['attempted_at']+1980,'invalid_ratings_agent_receipt')
    integer(value['failures'],0,8);integer(value['uploaded'],0,500);integer(value['queried'],0,100)
    return value


def validate_feedback_cursor(value,marker):
    fields(value,('format','installation_id','generation','sequence'))
    require(type(value['format']) is int and value['format']==1
            and all(value[k]==marker[k] for k in ('installation_id','generation')),
            'invalid_ratings_feedback_cursor')
    integer(value['sequence'],0,2**53-1)
    return value


class Scheduler:
    def __init__(self,sync,*,clock=time.time,jitter=random.uniform):
        self.sync=sync;self.clock=clock;self.jitter=jitter
        self.path=contained(sync.root,'state/ratings-agent.json')
        self.cursor_path=contained(sync.root,'state/ratings-agent-cursor.json')

    def tick(self):
        # Serializes persisted scheduling decisions. Synchronizer owns distinct
        # upload/network locks; no supervisor, provider, catalog or owner lock.
        with file_lease(contained(self.sync.root,'state/ratings-agent.lock')):
            now=self.clock();previous=None;fresh_after_sequence=None;reconcile_only=False
            cursor=None;cursor_error=False;sequence_fresh=False
            cursor_supported=(getattr(self.sync,'feedback_highwater',None) is not None
                              and getattr(self.sync,'has_pending_feedback_after',None) is not None
                              and getattr(self.sync,'advance_feedback_cursor',None) is not None)
            if cursor_supported:
                try:
                    if os.path.lexists(self.cursor_path):
                        cursor=validate_feedback_cursor(read_json(self.cursor_path),self.sync.marker)['sequence']
                    else:
                        # Existing queued events at first start are backlog. A
                        # later sequence is an event observed during service.
                        cursor=self.sync.feedback_highwater()
                        atomic_json(self.cursor_path,{'format':1,
                            'installation_id':self.sync.marker['installation_id'],
                            'generation':self.sync.marker['generation'],'sequence':cursor})
                except (NetworkError,ConfigError,OSError,ValueError,TypeError,KeyError,sqlite3.Error):
                    cursor_error=True
            if os.path.lexists(self.path):
                previous=validate_receipt(read_json(self.path),self.sync.marker)
                # Backward wall-clock movement is bounded to one normal interval.
                fresh=getattr(self.sync,'has_fresh_feedback',None)
                if cursor_supported and not cursor_error and cursor is not None:
                    try:sequence_fresh=self.sync.has_pending_feedback_after(cursor)
                    except (NetworkError,ConfigError,OSError,ValueError,TypeError,KeyError,sqlite3.Error):
                        cursor_error=True
                fresh_vote=(previous['phase'] in ('synchronized','query-deferred')
                            and now>=previous['attempted_at']+5
                            and (sequence_fresh or (not cursor_supported and previous['attempted_at']<=now
                                 and fresh is not None and fresh(previous['attempted_at'],now))))
                pending=getattr(self.sync,'has_pending_feedback',None)
                # A successful reconciliation can itself be the only completed
                # operation in its tick. Resume remaining backlog promptly on
                # hubs that explicitly authorize uncapped transfers; old hubs
                # remain governed by DeliveryBudget and its cadence.
                draining=(previous['phase']=='synchronized'
                          and pending is not None and pending())
                if previous['phase']=='query-deferred' and previous['attempted_at']<=now:
                    if fresh_vote:fresh_after_sequence=cursor if sequence_fresh else None
                    elif now<previous['next_attempt_at']:return previous
                    else:reconcile_only=True
                elif previous['attempted_at']<=now<previous['next_attempt_at']:
                    if fresh_vote:fresh_after_sequence=cursor if sequence_fresh else None
                    elif not draining:return previous
            phase='synchronized';error=None;delay=1800;failures=0;uploaded=queried=0;contention=False
            try:
                if cursor_error:raise ConfigError('ratings_feedback_cursor_requires_review')
                scheduled=getattr(self.sync,'scheduled_step',None)
                if reconcile_only and getattr(self.sync,'reconcile_only',None) is not None:
                    result=self.sync.reconcile_only()
                elif scheduled is not None and (fresh_after_sequence is not None):
                    result=scheduled(now,fresh_after_sequence=fresh_after_sequence)
                else:
                    result=scheduled(now) if scheduled is not None else self.sync.step()
                phase=result['phase']
                require(phase in PHASES,'invalid_ratings_sync_result')
                uploaded=result.get('uploaded',0);queried=result.get('queried',0)
                if phase in ('not-configured','sharing-disabled'):delay=60
                elif phase=='query-deferred':error='hub-unavailable';delay=15
            except LeaseBusy:
                # A sibling worker briefly owns one of the ratings/network
                # leases. This is normal contention, never a local review hold.
                phase='retrying';error='hub-unavailable';delay=CONTENTION_RETRY_SECONDS
                failures=min(8,(previous['failures'] if previous else 0)+1)
                contention=True
            except NetworkError as exc:
                failures=min(8,(previous['failures'] if previous else 0)+1)
                if exc.status in (401,403):
                    phase='authority-review-required';error='authority-refused';delay=1800
                elif exc.status==409 or exc.code.startswith(('ratings_','rating_','foreign_','invalid_','network_')):
                    phase='local-review-required';error='local-state-refused';delay=1800
                else:
                    phase='retrying';error='hub-unavailable';delay=min(300,5*2**(failures-1))
            except (ConfigError,OSError,ValueError,KeyError,TypeError,sqlite3.Error):
                phase='local-review-required';error='local-state-refused';delay=1800
                failures=min(8,(previous['failures'] if previous else 0)+1)
            if cursor_supported and cursor is not None and not cursor_error:
                try:
                    next_cursor=self.sync.advance_feedback_cursor(cursor)
                    atomic_json(self.cursor_path,{'format':1,
                        'installation_id':self.sync.marker['installation_id'],
                        'generation':self.sync.marker['generation'],'sequence':next_cursor})
                except (NetworkError,ConfigError,OSError,ValueError,TypeError,KeyError,sqlite3.Error):
                    pass
            delay=max(5,min(1980,delay+self.jitter(0,min(180,delay*.1))))
            receipt={'format':1,'installation_id':self.sync.marker['installation_id'],
                     'generation':self.sync.marker['generation'],'phase':phase,'error':error,
                     'attempted_at':now,'next_attempt_at':now+delay,'failures':failures,
                     'uploaded':uploaded,'queried':queried,'live_rooms_affected':False}
            validate_receipt(receipt,self.sync.marker);atomic_json(self.path,receipt)
            reason=('lease-contention' if contention
                    else 'post-upload-query-deferred' if phase=='query-deferred'
                    else 'authority-review' if phase=='authority-review-required'
                    else 'local-review' if phase=='local-review-required'
                    else 'transport-retry' if phase=='retrying'
                    else 'idle' if phase in ('not-configured','sharing-disabled')
                    else 'sync-complete')
            self._diagnostics(now,receipt,reason,contention)
            return receipt

    def _diagnostics(self, now, receipt, reason, contention):
        """Safe operational hints live outside legacy strict receipt schemas."""
        age=None
        age_reader=getattr(self.sync,'pending_feedback_age',None)
        if age_reader is not None:
            try:age=age_reader(now)
            except (NetworkError,OSError,ValueError,TypeError,KeyError,sqlite3.Error,ConfigError):pass
        if age is not None and (type(age) is not int or age<0 or age>2**53-1):age=None
        try:
            atomic_json(contained(self.sync.root,'state/ratings-agent-diagnostics.json'),{
                'format':1,'installation_id':self.sync.marker['installation_id'],
                'generation':self.sync.marker['generation'],'attempted_at':now,
                'reason':reason,'lock_contention':bool(contention),
                'retry_deadline':receipt['next_attempt_at'],
                'pending_event_age_seconds':age,'uploaded':receipt['uploaded'],
                'queried':receipt['queried']})
        except (OSError,ConfigError,TypeError,ValueError):
            # Diagnostics never become a prerequisite for synchronization.
            pass


FETCH_PHASES={'independent','downloading','activated','retrying','authority-review-required','local-review-required'}


def validate_fetch_receipt(value,marker):
    fields(value,('format','installation_id','generation','phase','error','attempted_at',
                  'next_attempt_at','failures','downloaded_pages','pending_pages','live_rooms_affected'))
    require(type(value['format']) is int and value['format']==1
            and all(value[k]==marker[k] for k in ('installation_id','generation')),'invalid_ratings_fetch_receipt')
    require(value['phase'] in FETCH_PHASES and value['error'] in ERRORS
            and value['live_rooms_affected'] is False,'invalid_ratings_fetch_receipt')
    for key in ('attempted_at','next_attempt_at'):number(value[key])
    require(value['attempted_at']+5<=value['next_attempt_at']<=value['attempted_at']+1980,'invalid_ratings_fetch_receipt')
    integer(value['failures'],0,8);integer(value['downloaded_pages'],0,2);integer(value['pending_pages'],0,200)
    return value


class FetchScheduler:
    """Bootstrap/cache catch-up has its own deadline and never accelerates uploads."""
    def __init__(self,sync,*,clock=time.time,jitter=random.uniform):
        self.sync=sync;self.clock=clock;self.jitter=jitter
        self.path=contained(sync.root,'state/ratings-fetch-agent.json')

    def tick(self, *, uploaded_at=None):
        with file_lease(contained(self.sync.root,'state/ratings-fetch-agent.lock')):
            now=self.clock();previous=None
            if os.path.lexists(self.path):
                previous=validate_fetch_receipt(read_json(self.path),self.sync.marker)
                if previous['attempted_at']<=now<previous['next_attempt_at']:
                    transient=previous['phase']=='retrying' and previous['error']=='hub-unavailable'
                    if not ((previous['phase']=='downloading' and getattr(self.sync,'uncapped_transfers',lambda:False)()) or (uploaded_at is not None and previous['phase'] in ('activated','retrying')
                            and (previous['phase']!='retrying' or transient)
                            and previous['attempted_at'] < uploaded_at <= now
                            and now >= previous['attempted_at'] + 5)):
                        return previous
            phase='independent';error=None;delay=60;failures=downloaded=pending=0;contention=False
            try:
                from .reader import profile
                if profile(self.sync.root)['mode']=='network':
                    result=self.sync.fetch_snapshots();phase=result['phase']
                    require(phase in ('downloading','activated'),'invalid_ratings_fetch_result')
                    downloaded=result.get('downloaded_pages',0);pending=result.get('pending_pages',0)
                    delay=5 if phase=='downloading' else 1800
            except LeaseBusy:
                phase='retrying';error='hub-unavailable';delay=CONTENTION_RETRY_SECONDS
                failures=min(8,(previous['failures'] if previous else 0)+1)
                contention=True
            except NetworkError as exc:
                failures=min(8,(previous['failures'] if previous else 0)+1)
                if exc.status in (401,403):
                    phase='authority-review-required';error='authority-refused';delay=1800
                elif exc.status==409 or exc.code.startswith(('ratings_','rating_','foreign_','invalid_','network_')):
                    phase='local-review-required';error='local-state-refused';delay=1800
                else:
                    phase='retrying';error='hub-unavailable';delay=min(300,5*2**(failures-1))
            except (ConfigError,OSError,ValueError,KeyError,TypeError,sqlite3.Error):
                phase='local-review-required';error='local-state-refused';delay=1800
                failures=min(8,(previous['failures'] if previous else 0)+1)
            delay=max(5,min(1980,delay+self.jitter(0,min(180,delay*.1))))
            value={'format':1,'installation_id':self.sync.marker['installation_id'],
                   'generation':self.sync.marker['generation'],'phase':phase,'error':error,
                   'attempted_at':now,'next_attempt_at':now+delay,'failures':failures,
                   'downloaded_pages':downloaded,'pending_pages':pending,'live_rooms_affected':False}
            validate_fetch_receipt(value,self.sync.marker);atomic_json(self.path,value)
            reason=('lease-contention' if contention
                    else 'authority-review' if phase=='authority-review-required'
                    else 'local-review' if phase=='local-review-required'
                    else 'transport-retry' if phase=='retrying'
                    else 'snapshot-downloading' if phase=='downloading'
                    else 'snapshot-activated' if phase=='activated' else 'independent')
            try:
                atomic_json(contained(self.sync.root,'state/ratings-fetch-agent-diagnostics.json'),{
                    'format':1,'installation_id':self.sync.marker['installation_id'],
                    'generation':self.sync.marker['generation'],'attempted_at':now,
                    'reason':reason,'lock_contention':bool(contention),
                    'retry_deadline':value['next_attempt_at'],
                    'downloaded_pages':downloaded,'pending_pages':pending})
            except (OSError,ConfigError,TypeError,ValueError):
                # Snapshot activation and its receipt remain authoritative.
                pass
            return value


def run_combined(heartbeat,ratings,stop,*,snapshots=None):
    """One optional network service; ratings failure cannot replace heartbeat health."""
    with file_lease(contained(heartbeat.client.root,'state/network-agent.lock')):
        heartbeat_due=0
        while not stop.is_set():
            now=heartbeat.clock()
            if now>=heartbeat_due:
                result=heartbeat.step();heartbeat_due=now+result['retry_seconds']
            if stop.is_set():break
            try:receipt=ratings.tick()
            except LeaseBusy:
                receipt={'next_attempt_at':heartbeat.clock()+CONTENTION_RETRY_SECONDS}
            except (NetworkError,ConfigError,OSError,ValueError,KeyError,TypeError,sqlite3.Error):
                # Retain corrupt receipts for review, and keep the independent
                # heartbeat alive. No upload occurs when scheduling validation fails.
                receipt={'next_attempt_at':heartbeat.clock()+300}
            snapshot_due=heartbeat.clock()+60;fetch_draining=False
            if snapshots is not None and not stop.is_set():
                try:
                    uploaded_at = (receipt.get('attempted_at') if receipt.get('uploaded', 0) > 0 else None)
                    fetched=snapshots.tick(uploaded_at=uploaded_at);snapshot_due=fetched['next_attempt_at']
                    fetch_draining=(fetched['phase']=='downloading' and getattr(snapshots.sync,'uncapped_transfers',lambda:False)())
                except LeaseBusy:
                    snapshot_due=heartbeat.clock()+CONTENTION_RETRY_SECONDS
                except (NetworkError,ConfigError,OSError,ValueError,KeyError,TypeError,sqlite3.Error):
                    snapshot_due=heartbeat.clock()+300
            now=heartbeat.clock()
            # Lightweight local wake-up checks, not new HTTP polls. Persisted
            # idle/retry/authority deadlines still decide actual dispatches.
            pending=getattr(getattr(ratings,'sync',None),'has_pending_feedback',None)
            upload_draining=(receipt.get('phase')=='synchronized' and receipt.get('uploaded',0)>0
                             and pending is not None and pending())
            stop.wait(0 if fetch_draining or upload_draining else max(1,min(5,heartbeat_due-now,receipt['next_attempt_at']-now,snapshot_due-now)))
