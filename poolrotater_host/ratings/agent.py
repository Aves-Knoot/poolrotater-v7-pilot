"""Durable independent ratings deadlines; shared service, separate health state."""
import os
import random
import sqlite3
import time

from ..config import ConfigError
from ..layout import contained, read_json, atomic_json, file_lease
from ..network.contracts import NetworkError, require, fields, integer, number

PHASES={'not-configured','sharing-disabled','synchronized','query-deferred','retrying','authority-review-required','local-review-required'}
ERRORS={None,'hub-unavailable','authority-refused','local-state-refused'}


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


class Scheduler:
    def __init__(self,sync,*,clock=time.time,jitter=random.uniform):
        self.sync=sync;self.clock=clock;self.jitter=jitter
        self.path=contained(sync.root,'state/ratings-agent.json')

    def tick(self):
        # Serializes persisted scheduling decisions. Synchronizer owns distinct
        # upload/network locks; no supervisor, provider, catalog or owner lock.
        with file_lease(contained(self.sync.root,'state/ratings-agent.lock')):
            now=self.clock();previous=None
            if os.path.lexists(self.path):
                previous=validate_receipt(read_json(self.path),self.sync.marker)
                # Backward wall-clock movement is bounded to one normal interval.
                if previous['attempted_at']<=now<previous['next_attempt_at']:
                    return previous
            phase='synchronized';error=None;delay=1800;failures=0;uploaded=queried=0
            try:
                result=self.sync.step();phase=result['phase']
                require(phase in PHASES,'invalid_ratings_sync_result')
                uploaded=result.get('uploaded',0);queried=result.get('queried',0)
                if phase in ('not-configured','sharing-disabled'):delay=60
                elif phase=='query-deferred':error='hub-unavailable'
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
            receipt={'format':1,'installation_id':self.sync.marker['installation_id'],
                     'generation':self.sync.marker['generation'],'phase':phase,'error':error,
                     'attempted_at':now,'next_attempt_at':now+delay,'failures':failures,
                     'uploaded':uploaded,'queried':queried,'live_rooms_affected':False}
            validate_receipt(receipt,self.sync.marker);atomic_json(self.path,receipt)
            return receipt


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

    def tick(self):
        with file_lease(contained(self.sync.root,'state/ratings-fetch-agent.lock')):
            now=self.clock();previous=None
            if os.path.lexists(self.path):
                previous=validate_fetch_receipt(read_json(self.path),self.sync.marker)
                if previous['attempted_at']<=now<previous['next_attempt_at']:return previous
            phase='independent';error=None;delay=60;failures=downloaded=pending=0
            try:
                from .reader import profile
                if profile(self.sync.root)['mode']=='network':
                    result=self.sync.fetch_snapshots();phase=result['phase']
                    require(phase in ('downloading','activated'),'invalid_ratings_fetch_result')
                    downloaded=result.get('downloaded_pages',0);pending=result.get('pending_pages',0)
                    delay=5 if phase=='downloading' else 1800
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
            validate_fetch_receipt(value,self.sync.marker);atomic_json(self.path,value);return value


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
            except (NetworkError,ConfigError,OSError,ValueError,KeyError,TypeError,sqlite3.Error):
                # Retain corrupt receipts for review, and keep the independent
                # heartbeat alive. No upload occurs when scheduling validation fails.
                receipt={'next_attempt_at':heartbeat.clock()+300}
            snapshot_due=heartbeat.clock()+60
            if snapshots is not None and not stop.is_set():
                try:snapshot_due=snapshots.tick()['next_attempt_at']
                except (NetworkError,ConfigError,OSError,ValueError,KeyError,TypeError,sqlite3.Error):
                    snapshot_due=heartbeat.clock()+300
            now=heartbeat.clock()
            stop.wait(max(1,min(60,heartbeat_due-now,receipt['next_attempt_at']-now,snapshot_due-now)))
