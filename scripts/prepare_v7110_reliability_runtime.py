"""Freeze the isolated reliability work over the exact accepted v7.1.9 artifact."""
from pathlib import Path
import ast
import hashlib
import json
import pprint
import shutil
import subprocess
import sys
import zipfile
from scripts.prepare_v71_runtime import fingerprints,source_files
from scripts.prepare_v719_history_runtime import BASELINES as EARLIER

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'validation/v7.1.10-reliability/release'
WORK=ROOT/'validation/v7.1.10-reliability/work'
BASE=ROOT/'validation/v7.1.9-history-stats/release/runtime.zip'
PIN='6e0d7260a803cdded105b086a04c1740e1dd2e389d6bd85aa6ad552fbedcc601'
BASELINES=((BASE,PIN),*EARLIER)
OVERLAYS=('poolrotater_host/controller_update.py','poolrotater_host/update_lifecycle.py',
 'poolrotater_host/pilot_installation.py','poolrotater_host/updates/recovery.py',
 'poolrotater_host/activity/collector.py','poolrotater_host/operator_status.py',
 'poolrotater_host/operations_health.py','poolrotater_host/reporting.py')


def main():
 from scripts.test_isolation import require_test_isolation
 require_test_isolation('ordinary')
 assert not OUT.exists(),'inspect the existing freeze before rebuilding'
 assert hashlib.sha256(BASE.read_bytes()).hexdigest()==PIN
 OUT.mkdir();source=OUT/'source';wheels=OUT/'wheelhouse';source.mkdir();wheels.mkdir()
 with zipfile.ZipFile(BASE)as archive:
  before=json.loads(archive.read('bundle.json'))
  for name,info in before['files'].items():
   data=archive.read(name);assert hashlib.sha256(data).hexdigest()==info['sha256']
   path=(source/name.removeprefix('payload/'))if name.startswith('payload/')else(wheels/name.removeprefix('wheelhouse/'))
   path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
 for name in OVERLAYS:
  data=(WORK/name).read_bytes();ast.parse(data,filename=name);(source/name).write_bytes(data)
 init=source/'poolrotater_host/__init__.py';text=init.read_text();assert text.count("__version__ = '7.1.9'")==1
 init.write_text(text.replace("__version__ = '7.1.9'","__version__ = '7.1.10'"))
 upgrade=source/'poolrotater_host/pilot_upgrade.py';text=upgrade.read_text();anchor="('v7.1.8', 'v7.1.9')):"
 assert text.count(anchor)==1
 pairs=',\n            '.join(repr((f'v7.1.{i}','v7.1.10'))for i in range(1,10))
 upgrade.write_text(text.replace(anchor,"('v7.1.8', 'v7.1.9'),\n            "+pairs+"):",1))
 after=fingerprints(source_files(source),(source/'rolling/release.py').read_text());transitions=[]
 for path,pin in BASELINES:
  assert hashlib.sha256(path.read_bytes()).hexdigest()==pin
  with zipfile.ZipFile(path)as archive:
   manifest=json.loads(archive.read('bundle.json'));old=fingerprints(manifest['files'],archive.read('payload/rolling/release.py').decode())
  assert old['storage']==after['storage'];transitions.append((old,after))
 file=source/'poolrotater_host/integrated_upgrade.py'
 file.write_text(file.read_text()+'\n# Exact unchanged-storage reliability transitions from accepted v7.1.1-v7.1.9.\nREVIEWED_TRANSITIONS += '+pprint.pformat(tuple(transitions),width=110,sort_dicts=True)+'\n')
 pins={row['name']:row for row in before['wheels']}
 trust=ast.parse((source/'poolrotater_host/catalog_trust.py').read_text())
 names=next(ast.literal_eval(n.value)for n in trust.body if isinstance(n,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='PINS'for t in n.targets))
 (source/'catalog-requirements-hashed.lock').write_text(''.join(name+'=='+names[name]+' --hash=sha256:'+pins[name]['sha256']+'\n'for name in sorted(names)))
 # A fresh interpreter ensures the candidate inspector, not the dirty checkout,
 # checks every compatibility grant and generated payload member.
 code='''import json,sys
from pathlib import Path
source=Path(sys.argv[1]);sys.path.insert(0,str(source))
from poolrotater_host.runtime_package import build_live_pilot,inspect
from poolrotater_host.updates.publisher import classify
from poolrotater_host.updates.approval import compatible
from poolrotater_host.pilot_upgrade import require_repair_compatible
from scripts.prepare_v71_runtime import fingerprints
out=source.parent
build=build_live_pilot(source,out/'wheelhouse',out/'runtime.zip',version='v7.1.10',catalog_cache=out/'wheelhouse')
new=inspect(out/'runtime.zip',build['sha256'],execution='pilot-live')
def contract(spec):
 return {'version':spec['version'],'protocol':1,'storage_epoch':'poolrotater-6.7-v1','requirements':spec['environment_id'],'python_hash':spec['interpreter_sha256'],'environment_hash':spec['environment_id'],'migrations':[],**fingerprints(spec['files'],(source/'rolling/release.py').read_text())}
baselines=[]
for path,pin in json.loads(sys.argv[2]):
 old=inspect(Path(path),pin,execution='pilot-live');assert classify(old,new)=='controller'
 compatible(contract(old),contract(new));require_repair_compatible(contract(old),contract(new))
 for key in ('target','wheels','environment_id','interpreter_sha256'):assert old[key]==new[key]
 assert contract(old)['storage']==contract(new)['storage']
 baselines.append({'version':old['version'],'sha256':pin,'classification':'controller','signed_admission_compatible':True})
old=inspect(Path(sys.argv[3]),sys.argv[4],execution='pilot-live')
changes=sorted(name for name in new['files'] if new['files'][name]!=old['files'].get(name))
allowed={'payload/'+name for name in json.loads(sys.argv[5])}|{'payload/poolrotater_host/__init__.py','payload/poolrotater_host/integrated_upgrade.py','payload/poolrotater_host/pilot_upgrade.py'}
assert set(changes)==allowed
result={'ok':True,'build':build,'before_sha256':sys.argv[4],'baselines':baselines,'classification':'controller','changed_files':changes,'storage_unchanged':True,'dependencies_unchanged':True}
(out/'freeze.json').write_text(json.dumps(result,indent=2)+'\\n');print(json.dumps(result))
'''
 (OUT/'build.py').write_text(code)
 subprocess.run([sys.executable,str(OUT/'build.py'),str(source),json.dumps([(str(p),h)for p,h in BASELINES]),str(BASE),PIN,json.dumps(OVERLAYS)],check=True)
 shutil.rmtree(wheels)

if __name__=='__main__':main()
