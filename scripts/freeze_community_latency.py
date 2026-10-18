"""Freeze this isolated batch over the exact accepted predecessor; no publication."""
from pathlib import Path
import ast,hashlib,json,pprint,shutil,subprocess,sys,zipfile
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from scripts.prepare_v71_runtime import fingerprints,source_files
ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'inputs/v7.1.12-runtime.zip'
PIN='7400e3c6c53cc5596fd74929fdcf615aecce43fb7340f4c3409f2663b8acfe32'
OUT=ROOT/'candidate'
assert not OUT.exists(),'Candidate already frozen; review before rebuilding'
assert hashlib.sha256(BASE.read_bytes()).hexdigest()==PIN
OUT.mkdir();source=OUT/'source';source.mkdir();wheels=OUT/'wheelhouse';wheels.mkdir()
with zipfile.ZipFile(BASE) as archive:
 before=json.loads(archive.read('bundle.json'))
 for name,info in before['files'].items():
  data=archive.read(name);assert hashlib.sha256(data).hexdigest()==info['sha256']
  path=source/name.removeprefix('payload/') if name.startswith('payload/') else wheels/name.removeprefix('wheelhouse/')
  path.parent.mkdir(exist_ok=True,parents=True);path.write_bytes(data)
 original_files={name.removeprefix('payload/'):info['sha256'] for name,info in before['files'].items() if name.startswith('payload/')}
changed=[]
for folder in ('bot','poolrotater_host','rolling','deployment'):
 for p in (ROOT/folder).rglob('*'):
  if not p.is_file() or p.suffix=='.pyc':continue
  name=p.relative_to(ROOT).as_posix();data=p.read_bytes()
  if hashlib.sha256(data).hexdigest()!=original_files.get(name):
   if p.suffix=='.py':ast.parse(data,filename=name)
   target=source/name;target.parent.mkdir(exist_ok=True,parents=True);target.write_bytes(data);changed.append(name)
p=source/'poolrotater_host/__init__.py';s=p.read_text();assert "__version__ = '7.1.12'" in s;p.write_text(s.replace("__version__ = '7.1.12'","__version__ = '7.1.13'"))
p=source/'poolrotater_host/pilot_upgrade.py';s=p.read_text();old="('v7.1.11','v7.1.12')):";assert s.count(old)==1;p.write_text(s.replace(old,"('v7.1.11','v7.1.12'), ('v7.1.12','v7.1.13')):",1))
after=fingerprints(source_files(source),(source/'rolling/release.py').read_text())
with zipfile.ZipFile(BASE) as archive:
 old=fingerprints(before['files'],archive.read('payload/rolling/release.py').decode())
assert old['storage']==after['storage']
p=source/'poolrotater_host/integrated_upgrade.py';p.write_text(p.read_text()+'\n# Exact unchanged-native-storage local statistics transition.\nREVIEWED_TRANSITIONS += '+pprint.pformat(((old,after),),width=110,sort_dicts=True)+'\n')
# Preserve the exact accepted catalog dependency lock that is not packaged as runtime code.
shutil.copyfile(ROOT/'catalog-requirements-hashed.lock',source/'catalog-requirements-hashed.lock')
code='''import json,sys
from pathlib import Path
source=Path(sys.argv[1]);sys.path.insert(0,str(source))
from poolrotater_host.runtime_package import build_live_pilot,inspect
from poolrotater_host.updates.publisher import classify
from poolrotater_host.updates.approval import compatible
from poolrotater_host.pilot_upgrade import require_repair_compatible
from scripts.prepare_v71_runtime import fingerprints
out=source.parent
build=build_live_pilot(source,out/'wheelhouse',out/'runtime.zip',version='v7.1.13',catalog_cache=out/'wheelhouse')
new=inspect(out/'runtime.zip',build['sha256'],execution='pilot-live');old=inspect(Path(sys.argv[2]),sys.argv[3],execution='pilot-live')
def contract(spec):
 return {'version':spec['version'],'protocol':1,'storage_epoch':'poolrotater-6.7-v1','requirements':spec['environment_id'],'python_hash':spec['interpreter_sha256'],'environment_hash':spec['environment_id'],'migrations':[],**fingerprints(spec['files'],(source/'rolling/release.py').read_text())}
assert classify(old,new)=='controller'
compatible(contract(old),contract(new));require_repair_compatible(contract(old),contract(new))
for key in ('target','wheels','environment_id','interpreter_sha256'):assert old[key]==new[key]
assert contract(old)['storage']==contract(new)['storage']
result={'ok':True,'build':build,'before_sha256':sys.argv[3],'classification':'controller','storage_unchanged':True,'dependencies_unchanged':True,'reviewed_exact_predecessor_transition':True,'published':False,'queued':False,'activated':False}
(out/'freeze.json').write_text(json.dumps(result,indent=2)+'\\n');print(json.dumps(result))
'''
(OUT/'build.py').write_text(code)
subprocess.run([sys.executable,str(OUT/'build.py'),str(source),str(BASE),PIN],check=True)
(OUT/'changed-source.json').write_text(json.dumps({'overlay_files':sorted(changed)},indent=2)+'\n')
shutil.rmtree(wheels)
