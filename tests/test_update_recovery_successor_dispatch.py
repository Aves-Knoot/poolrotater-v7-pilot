"""Operator completion must not render configuration using its predecessor SDK."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import uuid
import pytest
from poolrotater_host.updates import native,executor,recovery
from poolrotater_host.updates.contracts import UpdateError

def fixture(monkeypatch,tmp_path):
 backend=native.Backend('invented',None);target={'prefix':tmp_path};operation={'id':str(uuid.uuid4()),'release':{'kind':'controller','artifact':{'sha256':'a'*64}}}
 job={'id':operation['id'],'phase':'held','after':{'bundle_sha256':'a'*64}}
 monkeypatch.setattr(backend,'binding',lambda:(target,{},{}))
 monkeypatch.setattr(native,'protected',lambda p:p)
 monkeypatch.setattr(native,'_control',lambda *a,**k:job)
 calls=[]
 def execute(instance,target,value,*,decision):
  assert value is job and decision=='complete';calls.append('successor');job['phase']='complete'
 def review(instance,identity,decision):
  assert decision=='review','predecessor must never execute completion'
  calls.append('review');return {'ok':True,'phase':job['phase'],'services_restarted':False}
 monkeypatch.setattr(executor,'controller',execute);monkeypatch.setattr(recovery,'controller',review)
 return backend,operation,job,calls

def test_completion_uses_successor_before_readonly_predecessor_review(monkeypatch,tmp_path):
 backend,operation,_,calls=fixture(monkeypatch,tmp_path)
 assert backend.recover(operation,'complete')=={'ok':True,'phase':'complete'}
 assert calls==['successor','review']

@pytest.mark.parametrize('field',['id','artifact'])
def test_changed_native_operation_refuses_before_execution(monkeypatch,tmp_path,field):
 backend,operation,job,calls=fixture(monkeypatch,tmp_path)
 if field=='id':job['id']=str(uuid.uuid4())
 else:job['after']['bundle_sha256']='b'*64
 with pytest.raises(UpdateError,match='update_recovery_operation_changed'):backend.recover(operation,'complete')
 assert calls==[]

def test_failed_successor_does_not_claim_review_or_completion(monkeypatch,tmp_path):
 backend,operation,_,calls=fixture(monkeypatch,tmp_path)
 def fail(*a,**k):raise RuntimeError('fixture successor refused')
 monkeypatch.setattr(executor,'controller',fail)
 with pytest.raises(RuntimeError,match='fixture successor refused'):backend.recover(operation,'complete')
 assert calls==[]
