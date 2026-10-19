"""Freeze one stopped-repair runtime from exact owner15 and operator13 bytes."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
import ast,hashlib,json,pprint,shutil,zipfile
from scripts.prepare_v71_runtime import fingerprints,source_files
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'candidate'
assert not OUT.exists();OUT.mkdir();source=OUT/'source';source.mkdir();wheels=OUT/'wheelhouse';wheels.mkdir()
for name in ('bot','rolling','poolrotater_host','deployment'):
 shutil.copytree(ROOT/name,source/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
for name in ('run_bot.py','run_multiroom.py','run_rolling.py','run_room_worker.py','offline_host_controller.py','check_protected_runtime.py','requirements.txt','requirements-hashed.lock','catalog-requirements-hashed.lock'):
 shutil.copyfile(ROOT/name,source/name)
bases=[]
for version in ('v7.1.13','v7.1.15'):
 path=ROOT/'inputs'/(version+'-runtime.zip')
 with zipfile.ZipFile(path)as z:
  spec=json.loads(z.read('bundle.json'));assert spec['version']==version
  bases.append((path,spec))
  if version=='v7.1.15':
   for n in z.namelist():
    if n.startswith('wheelhouse/'):(wheels/Path(n).name).write_bytes(z.read(n))
after=fingerprints(source_files(source),(source/'rolling/release.py').read_text())
with zipfile.ZipFile(bases[0][0])as z:before=fingerprints(bases[0][1]['files'],z.read('payload/rolling/release.py').decode())
assert before['storage']==after['storage']
p=source/'poolrotater_host/integrated_upgrade.py'
p.write_text(p.read_text()+'\n# Exact operator13 -> explicit stopped16 repair; storage/dependencies unchanged.\nREVIEWED_TRANSITIONS += '+pprint.pformat(((before,after),),width=110,sort_dicts=True)+'\n')
for p in source.rglob('*.py'):ast.parse(p.read_text(),filename=str(p))
import sys;sys.path.insert(0,str(source))
from poolrotater_host.runtime_package import build_live_pilot,inspect
from poolrotater_host.pilot_upgrade import require_repair_compatible
from poolrotater_host.updates.publisher import classify
from poolrotater_host.updates.approval import compatible
build=build_live_pilot(source,wheels,OUT/'runtime.zip',version='v7.1.16',catalog_cache=wheels)
spec=inspect(OUT/'runtime.zip',build['sha256'],execution='pilot-live')
def contract(s):return {'version':s['version'],'protocol':1,'storage_epoch':'poolrotater-6.7-v1','requirements':s['environment_id'],'python_hash':s['interpreter_sha256'],'environment_hash':s['environment_id'],'migrations':[],**fingerprints(s['files'],(source/'rolling/release.py').read_text())}
old=bases[0][1];compatible(contract(old),contract(spec));require_repair_compatible(contract(old),contract(spec))
for k in ('target','wheels','environment_id','interpreter_sha256'):assert old[k]==spec[k]
result={'ok':True,'build':build,'before_sha256':hashlib.sha256(bases[0][0].read_bytes()).hexdigest(),'version':'v7.1.16','storage_unchanged':True,'dependencies_unchanged':True,'classification':classify(old,spec),'exact_stopped_upgrade_qualified':True}
(OUT/'freeze.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
shutil.rmtree(wheels)
