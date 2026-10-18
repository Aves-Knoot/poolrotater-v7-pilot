"""Curate a pinned local-statistics release over accepted v7.1.10 installer."""
import ast,hashlib,json,shutil,stat,zipfile
from pathlib import Path
from scripts.prepare_pilot_release import scan,zip_tree
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'publication'
BASE=ROOT.parents[2]/'v7.1.10-reliability/publication/poolrotater-7.1.10-live-pilot.zip'
# Work resides below validation/v7.1.11-local-stats/work.
BASE=ROOT/'inputs/predecessor-installer.zip'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(BASE)=='2d2455e65581c7e8c5d42584a8635d7c8bef1ce3d8a57baffd30e320b801690e'
assert not OUT.exists();OUT.mkdir();kit=OUT/'kit';kit.mkdir()
frozen=json.loads((ROOT/'candidate/freeze.json').read_text());pin=frozen['build']['sha256']
with zipfile.ZipFile(BASE) as z:
 old=json.loads(z.read('release.json'))
 for i in z.infolist():
  assert not i.filename.startswith('/') and '..' not in Path(i.filename).parts and not stat.S_ISLNK(i.external_attr>>16)
  if i.filename in ('runtime.zip','release.json','SHA256SUMS.txt'):continue
  p=kit/i.filename;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(i.filename))
shutil.copyfile(ROOT/'candidate/runtime.zip',kit/'runtime.zip');assert sha(kit/'runtime.zip')==pin
helper=kit/'repair_and_start.py';raw=helper.read_text();assert raw.count("EXPECTED_VERSION = 'v7.1.10'")==1;helper.write_text(raw.replace("EXPECTED_VERSION = 'v7.1.10'","EXPECTED_VERSION = 'v7.1.11'"))
notes='''# PoolRotater v7.1.11\n\nPlayer statistics are replicated as a complete local, indexed database on each participating host. Ordinary totals refresh around every 20 minutes; successful `.stats` replies use the familiar player-facing presentation. Privacy and consent checks run separately, with a short lease that prevents stale sharing policy from remaining usable during an outage.\n\nAuthenticated hub transfers no longer have an artificial rate cap when both sides negotiate support. Background hub/network/catalog/release services use nice 17 and a shared CPU ceiling of half one core per machine. osu! API pacing and player anti-abuse protections remain unchanged.\n\nOld clients work with the new hub. New clients retain conservative upload pacing against an older hub. Legacy request, cache and receipt formats remain supported. Existing gameplay, credentials, catalogs, settings, defaults, enrollment, history, opt-outs and update policies are preserved.\n\nThis is a controller update. Opted-in running hosts apply it after every configured room is empty naturally; publication does not start deliberately stopped rooms or override paused update policies.\n\nValidation: 173 affected tests passed against the frozen runtime, a broader 222-test related regression batch passed (overlapping the affected batch), root/service-user resource guards, a protected full-catalog upgrade, mixed-version HTTPS checks, complete replica import under the CPU limit, and standalone protected hub lifecycle checks. Whole-repository discovery, unchanged long gameplay suites and new actual-provider gameplay tests were not repeated.\n'''
guide='''# v7.1.11 operator guide\n\n## Existing healthy installations\n\nKeep your current instance, state and saved update policy. The signed updater accepts the reviewed v7.1.10 predecessor and queues a controller transition. It waits for all configured rooms on your host to empty naturally, then preserves operator state and restores the previously running network service. Hosts on earlier releases should complete their previously offered supported transitions first.\n\nThe central hub is upgraded before the signed client offer. Both upgrade orders remain compatible. No new OAuth sign-in, enrollment or catalog rebuild is required. The root-owned network service unit gains nice 17 and membership in the shared half-core transfer slice at its next supported start. Native room workers stay outside that slice.\n\nUse `status`, `support`, `network-support`, `ratings-support` and update status through your existing protected admin entry to review progress. Privacy/consent opt-outs and paused policies remain effective. A deliberately stopped or held host is not remotely started by publication. Retain state on an uncertain ownership outcome; use the supported ownership review instead of reinstalling, deleting journals or repeating room creation.\n\n## Statistics and transfers\n\nThe network agent downloads a complete public player totals projection for Standard and Mania. The initial download can take longer; later unchanged checks transfer metadata only. Ordinary snapshots refresh around 20 minutes. Privacy checks occur every 30 seconds and must remain fresh within the 120-second lease; a disconnected host therefore stops serving shared totals until policy can be verified again. Local history is retained.\n\nA negotiated new hub permits successful backlogs to drain without fixed interbatch delays. Old or unrecognized hubs retain legacy conservative upload pacing. Socket timeouts, bounded request/page sizes, concurrency limits, authorization and per-player anti-abuse checks remain. The background transfer slice shares 50% of one CPU across its participating services, at nice 17; this is not a network bandwidth cap.\n\n## Fresh installations and recovery\n\nDownload the installer ZIP, not GitHub's source ZIP. Verify RELEASE_SHA256SUMS.txt, then use install.py with runtime.zip and follow LIVE_AUTHORIZATION.md and NETWORK_OPERATOR_GUIDE.md. Configure your own account, import the full signed catalogs, and enroll with your bound invitation. One Standard Auto room is the default. No local map research pipeline is needed.\n\nThe optional repair launcher preserves an existing supported installation and makes at most the explicitly requested startup attempt. It does not bypass unknown ownership. Retain reports and state if it refuses. Older incident guides are historical; this guide describes the current release.\n'''
docs={'V7_1_11_RELEASE_NOTES.md':notes,'V7_1_11_OPERATOR_GUIDE.md':guide}
for name,content in docs.items():(kit/name).write_text(content)
(kit/'README.md').write_text('# PoolRotater v7.1.11 installer\n\nRead V7_1_11_OPERATOR_GUIDE.md and V7_1_11_RELEASE_NOTES.md first. Preserve your existing instance and state. Healthy opted-in hosts use signed updates and wait for all configured rooms to empty naturally. Stopped or held hosts retain their supported local review requirements.\n')
release={**old,'version':'7.1.11','runtime_version':'v7.1.11','runtime_sha256':pin,'kind':'complete-local-shared-statistics-controller-update','predecessor_sha256':frozen['before_sha256'],'supported_predecessors':[{'version':'v7.1.10','sha256':frozen['before_sha256']}],'storage_unchanged':True,'dependencies_unchanged':True,'actual_provider_acceptance_verified':False,'files':{p.relative_to(kit).as_posix():{'sha256':sha(p),'bytes':p.stat().st_size}for p in sorted(kit.rglob('*'))if p.is_file()}}
(kit/'release.json').write_text(json.dumps(release,indent=2)+'\n');(kit/'SHA256SUMS.txt').write_text(''.join(sha(p)+'  '+p.relative_to(kit).as_posix()+'\n' for p in sorted(kit.rglob('*'))if p.is_file()and p.name!='SHA256SUMS.txt'))
archive=OUT/'poolrotater-7.1.11-live-pilot.zip';zip_tree(kit,archive)
template=(ROOT/'inputs/predecessor-launcher.template').read_text().replace('v7.1.10','v7.1.11').replace('7.1.10-live-pilot','7.1.11-live-pilot')
launcher=template.replace('__ARCHIVE_SHA256__',sha(archive));ast.parse(launcher);launcher_name='poolrotater-repair-v7.1.11.py';(OUT/launcher_name).write_text(launcher)
(OUT/'RELEASE_SHA256SUMS.txt').write_text(sha(archive)+'  '+archive.name+'\n'+pin+'  runtime.zip\n'+sha(OUT/launcher_name)+'  '+launcher_name+'\n')
delta=OUT/'repository-delta';delta.mkdir()
with zipfile.ZipFile(ROOT/'inputs/v7.1.10-runtime.zip') as z:before=json.loads(z.read('bundle.json'))['files']
for p in (ROOT/'candidate/source').rglob('*'):
 if not p.is_file():continue
 name=p.relative_to(ROOT/'candidate/source').as_posix()
 if ('payload/'+name) not in before and name=='catalog-requirements-hashed.lock':continue
 if sha(p)==before.get('payload/'+name,{}).get('sha256'):continue
 q=delta/name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
for name,content in docs.items():p=delta/'docs'/name;p.parent.mkdir(exist_ok=True,parents=True);p.write_text(content)
for name in ('tests/test_local_statistics_replica.py','tests/legacy_statistics_fixture.py','tests/transfer_resource_fixture.py','tests/local_statistics_installed_upgrade_fixture.py','scripts/prepare_v7111_publication.py','scripts/freeze_local_statistics.py',*(f'tests/fixtures/v7110/{n}'for n in ('server.py','reader.py','transport.py','transport_worker.py'))):
 p=delta/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
p=delta/'pilot';p.mkdir(exist_ok=True);shutil.copyfile(helper,p/'repair_and_start.py');(p/'repair_launcher.py').write_text(launcher);(p/'repair_launcher.py.template').write_text(template)
prior=(ROOT/'inputs/predecessor-README.md').read_text();detail=prior[prior.index('## Fresh install'):].replace(frozen['before_sha256'],pin).replace('V7_1_10_OPERATOR_GUIDE','V7_1_11_OPERATOR_GUIDE')
(delta/'README.md').write_text('# PoolRotater v7.1.11\n\nUse the [installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.11), [operator guide](docs/V7_1_11_OPERATOR_GUIDE.md) and [release notes](docs/V7_1_11_RELEASE_NOTES.md). Download the installer asset, not the source ZIP.\n\nComplete local player statistics, familiar `.stats` replies, negotiated uncapped hub transfers and a shared nice-17 half-core background CPU budget. Existing settings, credentials, catalogs, history, enrollment and privacy/update choices are preserved. Controller activation waits for all configured rooms on each host to empty naturally. Deliberately stopped or held hosts are not started by publishing.\n\n'+detail)
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
