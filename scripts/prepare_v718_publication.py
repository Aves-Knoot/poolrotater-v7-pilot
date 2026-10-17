"""Build the maintenance kit from the frozen runtime and audited v7.1.7 kit."""
import ast,hashlib,json,shutil,zipfile
from pathlib import Path
from scripts.prepare_pilot_release import scan,zip_tree
from scripts.test_isolation import require_test_isolation
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'validation/v7.1.8-maintenance/publication'
BASE=ROOT/'validation/v7.1.7-timeout/publication/poolrotater-7.1.7-live-pilot.zip'
BASE_SHA='6b52201e20072c81376e8c1391ea3a9135a235a1fa4a419622e0426560ae7dd9'
DOCS=('MAINTENANCE_OPERATOR_GUIDE.md','V7_1_8_RELEASE_NOTES.md','V7_1_8_OPERATOR_GUIDE.md')
TESTS=('tests/test_operator_maintenance.py','tests/test_operator_status.py','tests/test_maintenance_upgrade_chain.py',
 'tests/test_maintenance_process_startup.py','tests/maintenance_full_catalog_fixture.py',
 'tests/maintenance_installed_upgrade_fixture.py','tests/maintenance_direct_upgrade_fixture.py')
def sha(raw):return hashlib.sha256(raw).hexdigest()
def main():
 require_test_isolation('ordinary');assert not OUT.exists();assert sha(BASE.read_bytes())==BASE_SHA
 frozen=json.loads((OUT.parent/'release/freeze.json').read_text());runtime=OUT.parent/'release/runtime.zip';pin=frozen['build']['sha256']
 assert sha(runtime.read_bytes())==pin
 OUT.mkdir(parents=True);kit=OUT/'kit';kit.mkdir()
 with zipfile.ZipFile(BASE)as z:
  old=json.loads(z.read('release.json'))
  for info in z.infolist():
   name=info.filename;assert not name.startswith('/')and '..'not in Path(name).parts
   if name in ('runtime.zip','release.json','SHA256SUMS.txt'):continue
   p=kit/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(name))
 shutil.copyfile(runtime,kit/'runtime.zip')
 helper=(kit/'repair_and_start.py').read_text();assert helper.count("EXPECTED_VERSION = 'v7.1.7'")==1
 (kit/'repair_and_start.py').write_text(helper.replace("EXPECTED_VERSION = 'v7.1.7'","EXPECTED_VERSION = 'v7.1.8'"))
 for name in DOCS:shutil.copyfile(ROOT/'docs'/name,kit/name)
 (kit/'README.md').write_text('# v7.1.8 maintenance and recovery hardening\n\nRead V7_1_8_OPERATOR_GUIDE.md and MAINTENANCE_OPERATOR_GUIDE.md first. Preserve the existing instance, settings, OAuth, catalogs, enrollment, ratings and history. Existing opted-in running hosts update naturally when all configured rooms are empty. Publication never starts a stopped host. Use the current pinned repair launcher for a stopped recovery-required host; older launchers retain their original pins.\n\n'+(kit/'README.md').read_text())
 release={**old,'version':'7.1.8','runtime_version':'v7.1.8','runtime_sha256':pin,
  'kind':'qualified-maintenance-recovery-controller-update','predecessor_sha256':frozen['before_sha256'],
  'supported_predecessors':frozen['baselines'],'storage_unchanged':True,'dependencies_unchanged':True,
  'actual_provider_acceptance_verified':False,'files':{p.relative_to(kit).as_posix():{'sha256':sha(p.read_bytes()),'bytes':p.stat().st_size}for p in sorted(kit.rglob('*'))if p.is_file()}}
 (kit/'release.json').write_text(json.dumps(release,indent=2)+'\n')
 (kit/'SHA256SUMS.txt').write_text(''.join(sha(p.read_bytes())+'  '+p.relative_to(kit).as_posix()+'\n'for p in sorted(kit.rglob('*'))if p.is_file()and p.name!='SHA256SUMS.txt'))
 archive=OUT/'poolrotater-7.1.8-live-pilot.zip';zip_tree(kit,archive)
 template=(ROOT/'pilot/repair_launcher.py.template').read_text().replace('v7.1.6-repair1','v7.1.8').replace('7.1.6-repair1-live-pilot','7.1.8-live-pilot')
 launcher=template.replace('__ARCHIVE_SHA256__',sha(archive.read_bytes()));ast.parse(launcher)
 launcher_name='poolrotater-repair-v7.1.8.py';(OUT/launcher_name).write_text(launcher)
 (OUT/'RELEASE_SHA256SUMS.txt').write_text(sha(archive.read_bytes())+'  '+archive.name+'\n'+pin+'  runtime.zip\n'+sha(launcher.encode())+'  '+launcher_name+'\n')
 delta=OUT/'repository-delta';delta.mkdir()
 for name in frozen['changed_files']:
  relative=name.removeprefix('payload/');p=delta/relative;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(OUT.parent/'release/source'/relative,p)
 for name in DOCS:
  p=delta/'docs'/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/'docs'/name,p)
 for name in (*TESTS,'scripts/prepare_v718_runtime.py','scripts/prepare_v718_publication.py'):
  p=delta/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
 p=delta/'pilot/repair_and_start.py';p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(kit/'repair_and_start.py',p)
 (delta/'pilot/repair_launcher.py').write_text(launcher);(delta/'pilot/repair_launcher.py.template').write_text(template)
 (delta/'README.md').write_text('# Current release: v7.1.8\n\nUse the [v7.1.8 installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.8), [operator guide](docs/V7_1_8_OPERATOR_GUIDE.md) and [maintenance procedures](docs/MAINTENANCE_OPERATOR_GUIDE.md). Supported controller-owned one-map/empty maintenance, cancellation, safer child startup, consecutive upgrade recovery and richer status preserve existing gameplay and data. Automatic controller updates wait for all configured rooms to empty; worker updates remain per-room. Stopped or recovery-required hosts need their local supported checks before one start attempt. Older repair launchers retain their old pins; use the current launcher for new attempts.\n\n'+(ROOT/'validation/v7.1.7-timeout/publication/repository-delta/README.md').read_text())
 review=OUT/'payload-review';review.mkdir()
 with zipfile.ZipFile(runtime)as z:
  manifest=json.loads(z.read('bundle.json'))
  for name in manifest['files']:
   if not name.startswith('payload/'):continue
   relative=name.removeprefix('payload/');p=review/relative
   if not p.name.endswith('.py')and relative not in ('requirements.txt','requirements-hashed.lock','poolrotater_host/pilot_release.json'):continue
   p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(name))
 for p in delta.rglob('*.py'):
  q=review/p.relative_to(delta);q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
 for folder in (kit,delta):
  for p in folder.rglob('*'):
   if p.is_file()and p.name!='runtime.zip':
    scan(p.read_bytes())
    if p.suffix=='.py':ast.parse(p.read_bytes())
 q={'ok':True,'runtime_sha256':pin,'archive':archive.name,'archive_sha256':sha(archive.read_bytes()),'archive_bytes':archive.stat().st_size,
  'launcher':launcher_name,'launcher_sha256':sha(launcher.encode()),'github_parent':json.loads((OUT.parent/'github-before.json').read_text())['head'],
  'changed_public_files':{p.relative_to(delta).as_posix():{'sha256':sha(p.read_bytes()),'bytes':p.stat().st_size}for p in sorted(delta.rglob('*'))if p.is_file()},'gitleaks_pending':True,'unresolved_findings':None}
 (OUT/'preparation.json').write_text(json.dumps(q,indent=2)+'\n');print(json.dumps({k:q[k]for k in ('ok','runtime_sha256','archive_sha256','launcher_sha256')}))
if __name__=='__main__':main()
