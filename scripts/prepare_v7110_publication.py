"""Curate the reliability installer/source delta from accepted v7.1.9 bytes."""
import ast,hashlib,json,shutil,stat,zipfile
from pathlib import Path
from scripts.prepare_pilot_release import scan,zip_tree
from scripts.test_isolation import require_test_isolation
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'validation/v7.1.10-reliability/publication'
BASE=ROOT/'validation/v7.1.9-history-stats/publication/poolrotater-7.1.9-live-pilot.zip'
BASE_SHA='6b3e7612a7ee73332ae83bb020ace1ba202d86383fb5caf868fc43b42cc5bed2'
DOCS=('V7_1_10_RELEASE_NOTES.md','V7_1_10_OPERATOR_GUIDE.md')
TESTS=('tests/test_v710_reliability.py','tests/test_controller_commit.py','tests/test_update_recovery.py',
 'tests/test_activity_collector.py','tests/test_activity_history_backfill.py','tests/test_operator_status.py',
 'tests/reliability_cleanup_installed_fixture.py','tests/reliability_installed_upgrade_fixture.py','tests/live_fixed_fixture.py')

def sha(raw):return hashlib.sha256(raw).hexdigest()

def main():
 require_test_isolation('ordinary');assert not OUT.exists();assert sha(BASE.read_bytes())==BASE_SHA
 # Draft documentation cannot accidentally become a published acceptance claim.
 for name in DOCS:assert 'Draft'not in(ROOT/'docs'/name).read_text()
 frozen=json.loads((OUT.parent/'release/freeze.json').read_text());runtime=OUT.parent/'release/runtime.zip';pin=frozen['build']['sha256']
 assert sha(runtime.read_bytes())==pin
 OUT.mkdir();kit=OUT/'kit';kit.mkdir()
 with zipfile.ZipFile(BASE)as archive:
  old=json.loads(archive.read('release.json'))
  for info in archive.infolist():
   name=info.filename;assert not name.startswith('/')and '..'not in Path(name).parts and not stat.S_ISLNK(info.external_attr>>16)
   if name in('runtime.zip','release.json','SHA256SUMS.txt'):continue
   path=kit/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(archive.read(name))
 shutil.copyfile(runtime,kit/'runtime.zip')
 helper=kit/'repair_and_start.py';raw=helper.read_text();assert raw.count("EXPECTED_VERSION = 'v7.1.9'")==1
 helper.write_text(raw.replace("EXPECTED_VERSION = 'v7.1.9'","EXPECTED_VERSION = 'v7.1.10'"))
 for name in DOCS:shutil.copyfile(ROOT/'docs'/name,kit/name)
 (kit/'README.md').write_text('# PoolRotater v7.1.10 live installer\n\nRead V7_1_10_OPERATOR_GUIDE.md and V7_1_10_RELEASE_NOTES.md first. Preserve your existing instance and state. Healthy opted-in hosts use the signed automatic update and natural all-configured-rooms-empty admission. A stopped or held host needs its supported local review; publication does not start it.\n\nFresh installations: follow LIVE_AUTHORIZATION.md and NETWORK_OPERATOR_GUIDE.md. Verify the release checksums, use install.py with this runtime.zip, import full signed catalogs, authorize your own account and enroll using your bound invitation. One Standard Auto room is the default. No local map collection or analysis is required.\n\nUse V7_1_10_OPERATOR_GUIDE.md for the applicable stopped/recovery case. Retained older release notes explain earlier incidents; the current operator guide is authoritative for this release.\n')
 release={**old,'version':'7.1.10','runtime_version':'v7.1.10','runtime_sha256':pin,
  'kind':'qualified-update-recovery-shared-history-controller-update','predecessor_sha256':frozen['before_sha256'],
  'supported_predecessors':frozen['baselines'],'storage_unchanged':True,'dependencies_unchanged':True,
  'actual_provider_acceptance_verified':False,'files':{p.relative_to(kit).as_posix():{'sha256':sha(p.read_bytes()),'bytes':p.stat().st_size}for p in sorted(kit.rglob('*'))if p.is_file()}}
 (kit/'release.json').write_text(json.dumps(release,indent=2)+'\n')
 (kit/'SHA256SUMS.txt').write_text(''.join(sha(p.read_bytes())+'  '+p.relative_to(kit).as_posix()+'\n'for p in sorted(kit.rglob('*'))if p.is_file()and p.name!='SHA256SUMS.txt'))
 archive=OUT/'poolrotater-7.1.10-live-pilot.zip';zip_tree(kit,archive)
 template=(ROOT/'validation/v7.1.9-history-stats/publication/repository-delta/pilot/repair_launcher.py.template').read_text().replace('v7.1.9','v7.1.10').replace('7.1.9-live-pilot','7.1.10-live-pilot')
 launcher=template.replace('__ARCHIVE_SHA256__',sha(archive.read_bytes()));ast.parse(launcher)
 launcher_name='poolrotater-repair-v7.1.10.py';(OUT/launcher_name).write_text(launcher)
 (OUT/'RELEASE_SHA256SUMS.txt').write_text(sha(archive.read_bytes())+'  '+archive.name+'\n'+pin+'  runtime.zip\n'+sha(launcher.encode())+'  '+launcher_name+'\n')
 delta=OUT/'repository-delta';delta.mkdir()
 for name in frozen['changed_files']:
  relative=name.removeprefix('payload/');p=delta/relative;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(OUT.parent/'release/source'/relative,p)
 for name in DOCS:
  p=delta/'docs'/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/'docs'/name,p)
 for name in(*TESTS,'scripts/prepare_v7110_reliability_runtime.py','scripts/prepare_v7110_publication.py'):
  p=delta/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
 p=delta/'pilot/repair_and_start.py';p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(helper,p)
 (delta/'pilot/repair_launcher.py').write_text(launcher);(delta/'pilot/repair_launcher.py.template').write_text(template)
 # Keep the opt-out instructions from the accepted guide, replacing the obsolete
 # release preambles and old literal runtime hash rather than stacking headers.
 prior=(ROOT/'validation/v7.1.9-history-stats/publication/repository-delta/README.md').read_text()
 detail=prior[prior.index('## Fresh install'):].replace('d9bc7d722b8b9f20d0c16ad79503bd8e811c41653a3e745f01c95df8a29e9efe',pin)
 detail=detail.replace('Before starting, review V7_1_1_OPERATOR_GUIDE.md.','Before starting, review docs/V7_1_10_OPERATOR_GUIDE.md.')
 (delta/'README.md').write_text('# PoolRotater v7.1.10\n\nUse the [installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.10), [operator guide](docs/V7_1_10_OPERATOR_GUIDE.md) and [release notes](docs/V7_1_10_RELEASE_NOTES.md). Download the installer asset, not GitHub’s source ZIP.\n\nThis controller release hardens cleanup/recovery and unblocks reviewed historical statistics. Existing settings, credentials, catalogs, enrollment, histories and privacy/update choices remain. Healthy opted-in hosts wait for every configured room to empty naturally. A held or deliberately stopped host requires its supported local checks; publishing does not start it.\n\n## Existing hosts\n\nKeep your current instance and state. Use your existing signed updater and saved policy. Inspect status/support/update status if a transaction is held; do not reinstall, delete journals, clear unknown ownership or retry uncertain room creation. See the current operator guide for bounded automatic recovery and reporting health. Older repair launchers retain their original pins.\n\n'+detail)
 review=OUT/'payload-review';review.mkdir()
 with zipfile.ZipFile(runtime)as runtime_archive:
  manifest=json.loads(runtime_archive.read('bundle.json'))
  for name in manifest['files']:
   if not name.startswith('payload/'):continue
   relative=name.removeprefix('payload/');p=review/relative
   if not p.name.endswith('.py')and relative not in('requirements.txt','requirements-hashed.lock','poolrotater_host/pilot_release.json'):continue
   p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(runtime_archive.read(name))
 for p in delta.rglob('*.py'):
  q=review/p.relative_to(delta);q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
 for folder in(kit,delta):
  for p in folder.rglob('*'):
   if p.is_file()and p.name!='runtime.zip':
    scan(p.read_bytes())
    if p.suffix=='.py':ast.parse(p.read_bytes())
 q={'ok':True,'runtime_sha256':pin,'archive':archive.name,'archive_sha256':sha(archive.read_bytes()),'archive_bytes':archive.stat().st_size,
  'launcher':launcher_name,'launcher_sha256':sha(launcher.encode()),'github_parent':json.loads((OUT.parent/'github-before.json').read_text())['head'],
  'changed_public_files':{p.relative_to(delta).as_posix():{'sha256':sha(p.read_bytes()),'bytes':p.stat().st_size}for p in sorted(delta.rglob('*'))if p.is_file()},'gitleaks_pending':True,'unresolved_findings':None}
 (OUT/'preparation.json').write_text(json.dumps(q,indent=2)+'\n');print(json.dumps({k:q[k]for k in('ok','runtime_sha256','archive_sha256','launcher_sha256')}))
if __name__=='__main__':main()
