"""Unsubmitted controller updates retry only on positively qualified baselines."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from poolrotater_host.updates import native


def _fixture(monkeypatch, tmp_path):
    root=tmp_path/'host';prefix=tmp_path/'prefix';root.mkdir();prefix.mkdir()
    installation=str(uuid.uuid4());generation=str(uuid.uuid4());host_id=str(uuid.uuid4())
    parent={'installation_id':installation,'generation':generation,'host_id':host_id,
        'prefix':str(prefix),'host_root':str(root),'service_uid':1000,'execution':'pilot-live',
        'bundle_sha256':'a'*64,'release':str(prefix/'releases'/'v7.3.0-a')}
    marker={'installation_id':installation,'generation':generation}
    target={'root':root,'prefix':prefix}
    old={'format':1,'id':str(uuid.uuid4()),'phase':'cancelled',
        'reason':'operator_cancelled_before_registration_change','before':parent,
        'after':{**parent,'bundle_sha256':'b'*64,'release':str(prefix/'releases'/'v7.3.1-b')},
        'rooms':['room1','room2'],'queued_at':90.0}
    operation={'id':str(uuid.uuid4()),'before_sha256':parent['bundle_sha256'],
        'effective_before_sha256':parent['bundle_sha256'],
        'release':{'kind':'controller','artifact':{'sha256':'c'*64}}}
    generation_worker='1'*32
    rows={
        'room1':{'phase':'running','healthy':True,'operation':'owned','ownership_unknown':False,
            'pid':501,'generation':generation_worker,'room_id':101,'release':'/retained/release',
            'error':None,'admission_locked':False,'roster_known':True,'observed_players':1,
            'heartbeat_age':1.0,'status':{'ready':True,'running':True,'room_id':101,
                'players':1,'state':'PLAYING'}},
        'room2':{'phase':'parked','healthy':False,'operation':'closed','ownership_unknown':False,
            'pid':None,'generation':'2'*32,'room_id':None,'release':'/retained/release',
            'error':None,'admission_locked':False,'roster_known':False,'observed_players':None,
            'heartbeat_age':None,'creation_candidate':None,'target':None,
            'status':{'ready':False,'running':False,'room_id':None,'players':0,'state':'IDLE'}}}
    journal={'format':1,'rooms':{alias:{k:row[k] for k in
        ('phase','operation','ownership_unknown','pid','generation','room_id','release','error')}
        for alias,row in rows.items()}}
    snapshot={'generated_at':100.0,'configuration_error':None,'rooms':rows}
    backend=native.Backend('invented',None)
    monkeypatch.setattr(backend,'binding',lambda:(target,marker,parent))
    monkeypatch.setattr(backend,'current',lambda:{'deployment_busy':False,
        'effective_sha256':parent['bundle_sha256'],'service_active':True,'managed_stopped':False})
    def control(path,*args,**kwargs):
        path=Path(path)
        if path.name=='controller-update.json':return old
        if path.name=='status.json':return snapshot
        if path.name=='ownership.json':return journal
        if kwargs.get('optional'):return None
        raise AssertionError('unexpected mandatory control read: '+str(path))
    monkeypatch.setattr(native,'_control',control)
    monkeypatch.setattr(native,'_host_configuration',lambda *_:{'rooms':[{'alias':'room1'},{'alias':'room2'}]})
    monkeypatch.setattr('poolrotater_host.ratings.operations._identity',lambda uid:nullcontext())
    monkeypatch.setattr('poolrotater_host.managed_host.admission.closed_aliases',lambda *_:{'room2'})
    monkeypatch.setattr('poolrotater_host.reconciliation._journal',lambda *_:journal)
    monkeypatch.setattr('poolrotater_host.runtime_lifecycle.require_no_pending',lambda *_:None)
    monkeypatch.setattr('poolrotater_host.runtime_lifecycle.validate_runtime',lambda *_:{'manifest':{}})
    monkeypatch.setattr(native,'require_registered_workers',lambda *_:None)
    monkeypatch.setattr(native.os.path,'lexists',lambda _:False)
    monkeypatch.setattr(native.time,'time',lambda:100.0)
    return backend,target,marker,parent,old,operation,rows,journal,snapshot


def test_unsubmitted_retry_allows_exactly_proven_closed_alias_and_terminal_predecessor(monkeypatch,tmp_path):
    backend,_,_,_,old,operation,_,_,_= _fixture(monkeypatch,tmp_path)
    assert backend.observe(operation)=={'phase':'absent'}
    assert backend.retryable(operation) is True


@pytest.mark.parametrize('phase',['waiting_empty','held','committing','unknown'])
def test_unrelated_nonterminal_or_unknown_native_job_is_not_absence(monkeypatch,tmp_path,phase):
    backend,_,_,_,old,operation,_,_,_= _fixture(monkeypatch,tmp_path)
    old.update(phase=phase,reason='operator_cancelled_before_registration_change')
    assert backend.observe(operation)['phase']=='conflict'
    assert backend.retryable(operation) is False


def test_cancelled_phase_alone_without_provenance_is_not_absence(monkeypatch,tmp_path):
    backend,_,_,_,old,operation,_,_,_= _fixture(monkeypatch,tmp_path)
    old['reason']='unknown'
    assert backend.observe(operation)['phase']=='conflict'
    assert backend.retryable(operation) is False


def test_terminal_cancel_with_incomplete_drain_restoration_is_not_absence(monkeypatch,tmp_path):
    backend,_,_,_,old,operation,_,_,_= _fixture(monkeypatch,tmp_path)
    old['controller_update_drain']={'request_id':str(uuid.uuid4())}
    old['drain_restoration_phase']='restoring'
    assert backend.observe(operation)['phase']=='conflict'
    assert backend.retryable(operation) is False


def test_terminal_cancel_with_admitted_or_stopped_commit_marker_is_not_absence(monkeypatch,tmp_path):
    backend,_,_,_,old,operation,_,_,_= _fixture(monkeypatch,tmp_path)
    old['commit_admitted_at']=99.0
    assert backend.observe(operation)['phase']=='conflict'
    assert backend.retryable(operation) is False


def test_pending_native_registration_blocks_terminal_classification(monkeypatch,tmp_path):
    backend,*rest=_fixture(monkeypatch,tmp_path)
    monkeypatch.setattr('poolrotater_host.runtime_lifecycle.require_no_pending',
        lambda *_:(_ for _ in ()).throw(native.ConfigError('runtime transition pending')))
    assert backend.observe(rest[4])['phase']=='conflict'
    assert backend.retryable(rest[4]) is False


def test_stopped_alias_without_authoritative_closed_proof_is_not_retryable(monkeypatch,tmp_path):
    backend,*rest=_fixture(monkeypatch,tmp_path)
    monkeypatch.setattr('poolrotater_host.managed_host.admission.closed_aliases',lambda *_:set())
    assert backend.observe(rest[4])['phase']=='absent'
    assert backend.retryable(rest[4]) is False


def test_running_identity_must_match_native_journal(monkeypatch,tmp_path):
    backend,*rest=_fixture(monkeypatch,tmp_path)
    operation=rest[4];journal=rest[6]
    journal['rooms']['room1']['generation']='9'*32
    assert backend.retryable(operation) is False


def test_running_release_must_be_registered(monkeypatch,tmp_path):
    backend,*rest=_fixture(monkeypatch,tmp_path)
    monkeypatch.setattr(native,'require_registered_workers',
        lambda *_:(_ for _ in ()).throw(native.ConfigError('update_unregistered_worker_release')))
    assert backend.retryable(rest[4]) is False


def test_unsubmitted_cancel_review_uses_only_outer_receipt_without_native_dispatch(monkeypatch,tmp_path):
    backend,_,_,_,old,operation,_,_,_=_fixture(monkeypatch,tmp_path)
    result=backend.recover(operation,'cancel')
    assert result=={'ok':True,'phase':'cancelled','unsubmitted':True,
        'upgrade_phase':'absent','registered_successor':False,'services_restarted':False}
    assert old['phase']=='cancelled'


def test_unsubmitted_review_api_reports_native_absence_and_no_registered_successor(monkeypatch,tmp_path):
    backend,_,_,_,_,operation,_,_,_=_fixture(monkeypatch,tmp_path)
    assert backend.recover(operation,'review')=={'ok':True,'phase':'absent','unsubmitted':True,
        'upgrade_phase':'absent','registered_successor':False,'services_restarted':False}


def test_unsubmitted_complete_is_not_claimed_without_native_commit(monkeypatch,tmp_path):
    backend,_,_,_,_,operation,_,_,_=_fixture(monkeypatch,tmp_path)
    with pytest.raises(Exception):backend.recover(operation,'complete')


def test_staging_ready_uses_verified_size_formula_and_archive_copy(monkeypatch,tmp_path):
    backend,target,_,parent,_,_,_,_,_=_fixture(monkeypatch,tmp_path)
    source=tmp_path/'verified.zip';source.write_bytes(b'x'*123)
    spec={'files':{'a':{'bytes':10}},'wheels':[{'expanded_bytes':20}]}
    item={'kind':'controller','from_sha256':[parent['bundle_sha256']],
        'execution':'pilot-live','artifact':{'sha256':'c'*64},
        'manifest_sha256':native.fingerprint(spec)}
    backend.client=SimpleNamespace(artifact=lambda selected:(source,item))
    monkeypatch.setattr('poolrotater_host.runtime_package.inspect',lambda *a,**k:spec)
    monkeypatch.setattr('poolrotater_host.pilot_installation.RESERVE',100)
    required=100+10+20+32*1024**2+source.stat().st_size
    monkeypatch.setattr(native.shutil,'disk_usage',lambda _:SimpleNamespace(free=required-1))
    assert backend.staging_ready(source,item) is False
    monkeypatch.setattr(native.shutil,'disk_usage',lambda _:SimpleNamespace(free=required))
    assert backend.staging_ready(source,item) is True


def test_staging_ready_rejects_wrong_artifact_or_baseline(monkeypatch,tmp_path):
    backend,target,_,parent,_,_,_,_,_=_fixture(monkeypatch,tmp_path)
    source=tmp_path/'verified.zip';source.write_bytes(b'x')
    item={'kind':'controller','from_sha256':['d'*64],'execution':'pilot-live',
        'artifact':{'sha256':'c'*64},'manifest_sha256':'e'*64}
    backend.client=SimpleNamespace(artifact=lambda selected:(source,selected))
    with pytest.raises(Exception):backend.staging_ready(source,item)


def test_staging_ready_uses_effective_baseline_when_parent_is_not_listed(monkeypatch,tmp_path):
    backend,_,_,parent,_,_,_,_,_=_fixture(monkeypatch,tmp_path)
    source=tmp_path/'verified.zip';source.write_bytes(b'x'*7)
    spec={'files':{},'wheels':[]};effective='d'*64
    item={'kind':'controller','from_sha256':[effective],'execution':'pilot-live',
        'artifact':{'sha256':'c'*64},'manifest_sha256':native.fingerprint(spec)}
    backend.client=SimpleNamespace(artifact=lambda selected:(source,item))
    monkeypatch.setattr(backend,'current',lambda:{'deployment_busy':False,
        'effective_sha256':effective,'service_active':True,'managed_stopped':False})
    monkeypatch.setattr('poolrotater_host.runtime_package.inspect',lambda *a,**k:spec)
    monkeypatch.setattr('poolrotater_host.pilot_installation.RESERVE',100)
    threshold=100+32*1024**2+source.stat().st_size
    monkeypatch.setattr(native.shutil,'disk_usage',lambda _:SimpleNamespace(free=threshold))
    assert parent['bundle_sha256'] not in item['from_sha256']
    assert backend.staging_ready(source,item) is True
