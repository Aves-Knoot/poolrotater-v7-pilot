"""Build the historical-statistics/style kit from the frozen runtime and audited v7.1.8 kit."""
import ast,hashlib,json,shutil,zipfile
from pathlib import Path
from scripts.prepare_pilot_release import scan,zip_tree
from scripts.test_isolation import require_test_isolation
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'validation/v7.1.9-history-stats/publication'
BASE=ROOT/'validation/v7.1.8-maintenance/publication/poolrotater-7.1.8-live-pilot.zip'
BASE_SHA='69a81b6c6c8edcae34a85de883e4ba97207ae23fa3bbc4c31da345edfff7e8f7'
DOCS=('V7_1_9_RELEASE_NOTES.md','V7_1_9_OPERATOR_GUIDE.md','PLAYER_STYLE_CONTROLS.md','HISTORY_STATS_REPAIR.md')
TESTS=('tests/test_activity_history_backfill.py','tests/test_history_review_installation.py',
 'tests/test_activity_personal.py','tests/test_activity_commands.py','tests/test_reporting_integration.py',
 'tests/test_v71_history_helper.py','tests/test_standard_style_toggles.py','tests/test_time_command.py',
 'tests/test_language_expansion.py','tests/history_stats_installed_upgrade_fixture.py',
 'tests/style_toggles_full_catalog_fixture.py')
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
 helper=(kit/'repair_and_start.py').read_text();assert helper.count("EXPECTED_VERSION = 'v7.1.8'")==1
 (kit/'repair_and_start.py').write_text(helper.replace("EXPECTED_VERSION = 'v7.1.8'","EXPECTED_VERSION = 'v7.1.9'"))
 for name in DOCS:shutil.copyfile(ROOT/'docs'/name,kit/name)
 shutil.copyfile(ROOT/'pilot/install_history_review.py',kit/'install_history_review.py')
 (kit/'README.md').write_text('# v7.1.9 shared historical statistics and independent style controls\n\nRead V7_1_9_OPERATOR_GUIDE.md and V7_1_9_RELEASE_NOTES.md first. Healthy opted-in running hosts update automatically when all configured rooms are empty. Existing settings, catalogs, credentials, history, ratings and enrollment remain. Historical source import requires a separate reviewed manifest; consuming another host’s imported history requires no manual import. Publishing never starts a stopped host.\n\n'+(kit/'README.md').read_text())
 release={**old,'version':'7.1.9','runtime_version':'v7.1.9','runtime_sha256':pin,
  'kind':'qualified-history-statistics-style-controller-update','predecessor_sha256':frozen['before_sha256'],
  'supported_predecessors':frozen['baselines'],'storage_unchanged':True,'dependencies_unchanged':True,
  'actual_provider_acceptance_verified':False,'files':{p.relative_to(kit).as_posix():{'sha256':sha(p.read_bytes()),'bytes':p.stat().st_size}for p in sorted(kit.rglob('*'))if p.is_file()}}
 (kit/'release.json').write_text(json.dumps(release,indent=2)+'\n')
 (kit/'SHA256SUMS.txt').write_text(''.join(sha(p.read_bytes())+'  '+p.relative_to(kit).as_posix()+'\n'for p in sorted(kit.rglob('*'))if p.is_file()and p.name!='SHA256SUMS.txt'))
 archive=OUT/'poolrotater-7.1.9-live-pilot.zip';zip_tree(kit,archive)
 template=(ROOT/'pilot/repair_launcher.py.template').read_text().replace('v7.1.6-repair1','v7.1.9').replace('7.1.6-repair1-live-pilot','7.1.9-live-pilot')
 launcher=template.replace('__ARCHIVE_SHA256__',sha(archive.read_bytes()));ast.parse(launcher)
 launcher_name='poolrotater-repair-v7.1.9.py';(OUT/launcher_name).write_text(launcher)
 (OUT/'RELEASE_SHA256SUMS.txt').write_text(sha(archive.read_bytes())+'  '+archive.name+'\n'+pin+'  runtime.zip\n'+sha(launcher.encode())+'  '+launcher_name+'\n')
 delta=OUT/'repository-delta';delta.mkdir()
 for name in frozen['changed_files']:
  relative=name.removeprefix('payload/');p=delta/relative;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(OUT.parent/'release/source'/relative,p)
 for name in DOCS:
  p=delta/'docs'/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/'docs'/name,p)
 for name in (*TESTS,'scripts/prepare_v719_history_runtime.py','scripts/prepare_v719_publication.py'):
  p=delta/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
 p=delta/'pilot/repair_and_start.py';p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(kit/'repair_and_start.py',p)
 shutil.copyfile(ROOT/'pilot/install_history_review.py',delta/'pilot/install_history_review.py')
 (delta/'pilot/repair_launcher.py').write_text(launcher);(delta/'pilot/repair_launcher.py.template').write_text(template)
 (delta/'README.md').write_text('# Current release: v7.1.9\n\nUse the [v7.1.9 installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.9), [operator guide](docs/V7_1_9_OPERATOR_GUIDE.md) and [release notes](docs/V7_1_9_RELEASE_NOTES.md). Reviewed public historical stats can be shared across hosts; completed missing-name lookups no longer remain syncing. Standard styles mix independently, DT remains separate, .aim aliases .jump and .t aliases .time. This combined controller release waits for all configured rooms to empty. Existing settings, credentials, catalogs, enrollment, privacy and update choices remain. Older repair launchers retain their original pins.\n\n'+(ROOT/'validation/v7.1.8-maintenance/publication/repository-delta/README.md').read_text())
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
