"""Durable root repair intent and at-most-once startup admission.

This is coordination evidence, not ownership authority. The native stopped
review/transaction and lifecycle checks remain mandatory. No result here can
clear an unknown creation or authorize a provider mutation by itself.
"""
import hashlib
import json
import re
import uuid
from .config import ConfigError
from .layout import atomic_json,read_json,file_lease

FIELDS=('installation_id','generation','prefix','host_root','service_uid','execution')
PHASES=('prepared','helpers_paused','upgraded','ownership_review','requires_upstream',
        'ready','startup_attempted','complete','requires_review')
NAME='recreation-repair.json'


def _need(value):
    if not value:raise ConfigError('repair_receipt_requires_review')


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def validate(value,instance,current,candidate):
    _need(type(value)is dict and set(value)=={'format','operation_id','instance','binding',
        'before_sha256','candidate_sha256','helpers','phase','startup_attempted',
        'journal_sha256','helpers_sha256'})
    _need(type(value['format'])is int and value['format']==1 and value['instance']==instance
          and value['phase']in PHASES and type(value['startup_attempted'])is bool)
    try:_need(str(uuid.UUID(value['operation_id']))==value['operation_id'])
    except (ValueError,TypeError,AttributeError):raise ConfigError('repair_receipt_requires_review')from None
    _need(value['binding']=={k:current[k]for k in FIELDS})
    for field in ('before_sha256','candidate_sha256','helpers_sha256'):
        _need(type(value[field])is str and re.fullmatch('[a-f0-9]{64}',value[field])is not None)
    _need(value['candidate_sha256']==candidate and current['bundle_sha256']in
          (value['before_sha256'],candidate))
    _need(value['helpers_sha256']==_digest(value['helpers']))
    _need(value['journal_sha256']is None or type(value['journal_sha256'])is str and
          re.fullmatch('[a-f0-9]{64}',value['journal_sha256'])is not None)
    _need(not value['startup_attempted']or value['phase']in ('startup_attempted','complete','requires_review'))
    _need(value['phase']!='startup_attempted'or value['startup_attempted'])
    _need(not value['startup_attempted']or value['journal_sha256']is not None)
    return value


class Receipt:
    def __init__(self,prefix,instance,current,candidate,helpers):
        self.path=prefix/NAME;self.instance=instance;self.current=current;self.candidate=candidate
        self.helpers=helpers
    def begin(self,snapshot):
        with file_lease(self.path.parent/'recreation-repair-receipt.lock',timeout=1):
            return self._begin_locked(snapshot)
    def _begin_locked(self,snapshot):
        self.helpers.validate(snapshot)
        _need(type(self.candidate)is str and re.fullmatch('[a-f0-9]{64}',self.candidate)is not None)
        try:prior=read_json(self.path,maximum=65536)
        except FileNotFoundError:prior=None
        if prior is not None:
            validate(prior,self.instance,self.current,self.candidate)
            self.helpers.validate(prior['helpers'])
            # The saved original preferences win over the currently paused state.
            return prior
        value={'format':1,'operation_id':str(uuid.uuid4()),'instance':self.instance,
               'binding':{k:self.current[k]for k in FIELDS},
               'before_sha256':self.current['bundle_sha256'],'candidate_sha256':self.candidate,
               'helpers':snapshot,'helpers_sha256':_digest(snapshot),'phase':'prepared',
               'startup_attempted':False,'journal_sha256':None}
        atomic_json(self.path,value)
        return value
    def advance(self,expected,phase,*,journal_sha256=None):
        with file_lease(self.path.parent/'recreation-repair-receipt.lock',timeout=1):
            return self._advance_locked(expected,phase,journal_sha256=journal_sha256)
    def _advance_locked(self,expected,phase,*,journal_sha256=None):
        _need(phase in PHASES)
        actual=read_json(self.path,maximum=65536)
        validate(actual,self.instance,self.current,self.candidate)
        _need(actual==expected)
        # Once a startup was admitted, no route returns to a pre-start phase.
        _need(not actual['startup_attempted']or phase in ('complete','requires_review'))
        allowed={
            'prepared':{'helpers_paused','requires_review'},
            'helpers_paused':{'upgraded','ownership_review','requires_review'},
            'upgraded':{'ownership_review','requires_review'},
            'ownership_review':{'requires_upstream','ready','requires_review'},
            'requires_upstream':{'ownership_review','requires_review'},
            'ready':{'startup_attempted','complete','requires_review'},
            'startup_attempted':{'complete','requires_review'},
            'complete':set(),'requires_review':set(),
        }
        _need(phase in allowed[actual['phase']])
        value={**actual,'phase':phase}
        if journal_sha256 is not None:value['journal_sha256']=journal_sha256
        if phase=='startup_attempted':value['startup_attempted']=True
        validate(value,self.instance,self.current,self.candidate)
        atomic_json(self.path,value)
        return value
