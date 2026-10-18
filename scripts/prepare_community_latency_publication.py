"""Curate a pinned local-statistics release over accepted v7.1.12 installer."""
import ast,hashlib,json,shutil,stat,zipfile
from pathlib import Path
from scripts.prepare_pilot_release import scan,zip_tree
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'publication'
BASE=ROOT.parents[2]/'v7.1.12-reliability/publication/poolrotater-7.1.12-live-pilot.zip'
# Work resides below validation/v7.1.13-local-stats/work.
BASE=ROOT/'inputs/predecessor-installer.zip'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(BASE)=='ebb1d0e75f9f51fd86cdf27f74c5c5bfc66fc183b918497ac19b14add7aaa097'
assert not OUT.exists();OUT.mkdir();kit=OUT/'kit';kit.mkdir()
frozen=json.loads((ROOT/'candidate/freeze.json').read_text());pin=frozen['build']['sha256']
with zipfile.ZipFile(BASE) as z:
 old=json.loads(z.read('release.json'))
 for i in z.infolist():
  assert not i.filename.startswith('/') and '..' not in Path(i.filename).parts and not stat.S_ISLNK(i.external_attr>>16)
  if i.filename in ('runtime.zip','release.json','SHA256SUMS.txt'):continue
  p=kit/i.filename;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(i.filename))
shutil.copyfile(ROOT/'candidate/runtime.zip',kit/'runtime.zip');assert sha(kit/'runtime.zip')==pin
helper=kit/'repair_and_start.py';raw=helper.read_text();assert raw.count("EXPECTED_VERSION = 'v7.1.12'")==1;helper.write_text(raw.replace("EXPECTED_VERSION = 'v7.1.12'","EXPECTED_VERSION = 'v7.1.13'"))
notes=(ROOT/'release-notes.md').read_text()
guide=(ROOT/'operator-guide.md').read_text()
docs={'V7_1_13_RELEASE_NOTES.md':notes,'V7_1_13_OPERATOR_GUIDE.md':guide}
for name,content in docs.items():(kit/name).write_text(content)
(kit/'README.md').write_text('# PoolRotater v7.1.13 installer\n\nRead V7_1_13_OPERATOR_GUIDE.md and V7_1_13_RELEASE_NOTES.md first. Preserve your existing instance and state. Healthy opted-in hosts use signed updates and wait for all configured rooms to empty naturally. Stopped or held hosts retain their supported local review requirements.\n')
release={**old,'version':'7.1.13','runtime_version':'v7.1.13','runtime_sha256':pin,'kind':'community-greetings-and-nonblocking-replies','predecessor_sha256':frozen['before_sha256'],'supported_predecessors':[{'version':'v7.1.12','sha256':frozen['before_sha256']}],'storage_unchanged':True,'dependencies_unchanged':True,'actual_provider_acceptance_verified':False,'files':{p.relative_to(kit).as_posix():{'sha256':sha(p),'bytes':p.stat().st_size}for p in sorted(kit.rglob('*'))if p.is_file()}}
(kit/'release.json').write_text(json.dumps(release,indent=2)+'\n');(kit/'SHA256SUMS.txt').write_text(''.join(sha(p)+'  '+p.relative_to(kit).as_posix()+'\n' for p in sorted(kit.rglob('*'))if p.is_file()and p.name!='SHA256SUMS.txt'))
archive=OUT/'poolrotater-7.1.13-live-pilot.zip';zip_tree(kit,archive)
template=(ROOT/'inputs/predecessor-launcher.template').read_text().replace('v7.1.12','v7.1.13').replace('7.1.12-live-pilot','7.1.13-live-pilot')
launcher=template.replace('__ARCHIVE_SHA256__',sha(archive));ast.parse(launcher);launcher_name='poolrotater-repair-v7.1.13.py';(OUT/launcher_name).write_text(launcher)
(OUT/'RELEASE_SHA256SUMS.txt').write_text(sha(archive)+'  '+archive.name+'\n'+pin+'  runtime.zip\n'+sha(OUT/launcher_name)+'  '+launcher_name+'\n')
delta=OUT/'repository-delta';delta.mkdir()
with zipfile.ZipFile(ROOT/'inputs/v7.1.12-runtime.zip') as z:before=json.loads(z.read('bundle.json'))['files']
for p in (ROOT/'candidate/source').rglob('*'):
 if not p.is_file():continue
 name=p.relative_to(ROOT/'candidate/source').as_posix()
 if ('payload/'+name) not in before and name=='catalog-requirements-hashed.lock':continue
 if sha(p)==before.get('payload/'+name,{}).get('sha256'):continue
 q=delta/name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
for name,content in docs.items():p=delta/'docs'/name;p.parent.mkdir(exist_ok=True,parents=True);p.write_text(content)
for name in ('tests/test_background_player_profiles.py', 'tests/test_background_profile_rpc.py', 'tests/test_community_reply_latency.py', 'tests/test_chat_admission_retry.py', 'tests/test_room_chat_backpressure.py', 'tests/test_headless_multiroom.py', 'tests/test_network_statistics.py', 'tests/community_latency_installed_upgrade_fixture.py', 'tests/community_latency_worker_fixture.py', 'scripts/freeze_community_latency.py', 'scripts/community_latency_regression.py', 'scripts/prepare_community_latency_publication.py'):
 p=delta/name;p.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(ROOT/name,p)
prior=(ROOT/'inputs/predecessor-README.md').read_text();detail=prior[prior.index('## Fresh install'):].replace(frozen['before_sha256'],pin).replace('V7_1_12_OPERATOR_GUIDE','V7_1_13_OPERATOR_GUIDE')
(delta/'README.md').write_text('# PoolRotater v7.1.13\n\nUse the [installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.13), [operator guide](docs/V7_1_13_OPERATOR_GUIDE.md) and [release notes](docs/V7_1_13_RELEASE_NOTES.md). Download the installer asset, not the source ZIP.\n\nShared personalized greetings, bounded optional profile reads, independent chat retries and nonblocking local statistics reads. Existing configuration, state, credentials, titles and opt-outs are preserved. Existing failed v7.1.11 hosts still need the documented one-time v7.1.12 activation before the successor can apply. No publication is proof of live activation.\n\n'+detail)
review=OUT/'payload-review';review.mkdir()
with zipfile.ZipFile(kit/'runtime.zip')as z:
 for name in json.loads(z.read('bundle.json'))['files']:
  if not name.startswith('payload/'):continue
  rel=name.removeprefix('payload/')
  if not rel.endswith('.py') and rel not in ('requirements.txt','requirements-hashed.lock','poolrotater_host/pilot_release.json'):continue
  p=review/rel;p.parent.mkdir(exist_ok=True,parents=True);p.write_bytes(z.read(name))
for p in delta.rglob('*.py'):
 q=review/p.relative_to(delta);q.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(p,q)
for folder in (kit,delta):
 for p in folder.rglob('*'):
  if p.is_file() and p.name!='runtime.zip':
   scan(p.read_bytes())
   if p.suffix=='.py':ast.parse(p.read_bytes())
q={'ok':True,'runtime_sha256':pin,'archive':archive.name,'archive_sha256':sha(archive),'archive_bytes':archive.stat().st_size,'launcher':launcher_name,'launcher_sha256':sha(OUT/launcher_name),'github_parent':json.loads((ROOT/'inputs/github-before.json').read_text())['head'],'changed_public_files':{p.relative_to(delta).as_posix():{'sha256':sha(p),'bytes':p.stat().st_size}for p in sorted(delta.rglob('*'))if p.is_file()},'gitleaks_pending':True,'unresolved_findings':None}
(OUT/'preparation.json').write_text(json.dumps(q,indent=2)+'\n');print(json.dumps({k:q[k]for k in ('ok','runtime_sha256','archive_sha256','launcher_sha256')}))
