"""Curate a pinned local-statistics release over accepted v7.1.11 installer."""
import ast,hashlib,json,shutil,stat,zipfile
from pathlib import Path
from scripts.prepare_pilot_release import scan,zip_tree
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'publication'
BASE=ROOT.parents[2]/'v7.1.11-reliability/publication/poolrotater-7.1.11-live-pilot.zip'
# Work resides below validation/v7.1.12-local-stats/work.
BASE=ROOT/'inputs/predecessor-installer.zip'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(BASE)=='95b88ba3c1a6ca4e54481e7c2b4ae90a0d7aa440d3c7e00c206aef02bcdbbf3f'
assert not OUT.exists();OUT.mkdir();kit=OUT/'kit';kit.mkdir()
frozen=json.loads((ROOT/'candidate/freeze.json').read_text());pin=frozen['build']['sha256']
with zipfile.ZipFile(BASE) as z:
 old=json.loads(z.read('release.json'))
 for i in z.infolist():
  assert not i.filename.startswith('/') and '..' not in Path(i.filename).parts and not stat.S_ISLNK(i.external_attr>>16)
  if i.filename in ('runtime.zip','release.json','SHA256SUMS.txt'):continue
  p=kit/i.filename;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(i.filename))
shutil.copyfile(ROOT/'candidate/runtime.zip',kit/'runtime.zip');assert sha(kit/'runtime.zip')==pin
helper=kit/'repair_and_start.py';raw=helper.read_text();assert raw.count("EXPECTED_VERSION = 'v7.1.11'")==1;helper.write_text(raw.replace("EXPECTED_VERSION = 'v7.1.11'","EXPECTED_VERSION = 'v7.1.12'"))
notes='# v7.1.12 — community room resilience and responsive commands\n\nOptional player-profile and top-score read timeouts no longer terminate a room. The bot retains the confirmed roster and READY/playing state, then retries enrichment with bounded backoff. Authorization, unsafe process cleanup and uncertain room mutations retain their existing recovery requirements.\n\nLive community installations now use the same pinned osu! notification socket as the owner, with existing HTTP history reconciliation and fallback. This removes the routine polling delay before a command is seen; provider pacing and flood guards remain enforced.\n\nA cold `.stats <username>` request now stays pending and posts its answer automatically when the background cache is ready. Requests are bounded, canceled on departure/rejoin/room change, and recheck visibility before replying. Complete local statistics replicas and their separate privacy lease are retained.\n\nOrdinary playlist entries retain existing freemod; DT-specific entries keep their intentional forced mods. Website settings call it Freemod. Community map artwork, length, settings and player counts use the existing shared cards. Actual freemod match starts can show a clearly labelled normal-speed estimate; optional player speed mods can differ.\n\nThe signed controller updater can repair an already-running host whose configured failed rooms are all positively closed and parked. It requires repeated fresh controller observations and the independent clean ownership journal. Deliberate close/maintenance, stale state, live rooms and uncertain creation/closure do not authorize that shortcut. Healthy hosts retain the normal empty-room/private-admission checks. Deliberately stopped services remain stopped.\n\nConfiguration, defaults, titles, catalogs, credentials, enrollment, ratings, personal history, sharing choices and update policy are preserved. This is a controller update from v7.1.11 with unchanged storage and dependencies.\n\nValidation includes typed HTTPS/RPC failure replays, bounded deferred replies and privacy tests, closed-slot admission refusals, community presentation checks, and a real protected root/service-UID upgrade using complete prepared catalogs. See the qualification receipt for exact test results. Unchanged whole-repository suites and new actual-provider gameplay tests were not repeated.\n'
guide="# v7.1.12 operator guide\n\nKeep the existing installation and state. No OAuth sign-in, enrollment, database rebuild or reinstall is required. Existing automatic-update consent governs delivery from v7.1.11.\n\nHealthy running hosts wait for the usual safe empty-room controller transition. A running controller with only failed, positively closed parked slots can apply this repair after fresh repeated observations and ownership checks; its newly repaired workers then start normally. Explicit operator closure/maintenance and unknown ownership stay fenced. A stopped or paused host is not started by publication.\n\nOrdinary freemod and tuned defaults are retained. Forced DT choices intentionally remain DT. Live community rooms gain the existing official notification socket and keep HTTP fallback. A cold personal-statistics lookup posts its answer when ready without another command. Shared results remain subject to current visibility and consent.\n\nUse the retained protected entry for status, support, network-support, ratings-support and update status. An update being offered or downloaded is not proof of activation. Verify the installed version, both room links, fresh website observations and continued activity/rating/log acknowledgments after transition. Preserve state and reports if ownership or startup is uncertain; do not repeat room creation blindly.\n\nThe installer and optional repair launcher are available for a local supported repair if automatic delivery cannot act. Verify RELEASE_SHA256SUMS.txt and download the installer asset rather than GitHub's source ZIP. Fresh installs follow LIVE_AUTHORIZATION.md and NETWORK_OPERATOR_GUIDE.md using their own hosting account.\n"
docs={'V7_1_12_RELEASE_NOTES.md':notes,'V7_1_12_OPERATOR_GUIDE.md':guide}
for name,content in docs.items():(kit/name).write_text(content)
(kit/'README.md').write_text('# PoolRotater v7.1.12 installer\n\nRead V7_1_12_OPERATOR_GUIDE.md and V7_1_12_RELEASE_NOTES.md first. Preserve your existing instance and state. Healthy opted-in hosts use signed updates and wait for all configured rooms to empty naturally. Stopped or held hosts retain their supported local review requirements.\n')
release={**old,'version':'7.1.12','runtime_version':'v7.1.12','runtime_sha256':pin,'kind':'community-reliability-and-responsive-commands','predecessor_sha256':frozen['before_sha256'],'supported_predecessors':[{'version':'v7.1.11','sha256':frozen['before_sha256']}],'storage_unchanged':True,'dependencies_unchanged':True,'actual_provider_acceptance_verified':False,'files':{p.relative_to(kit).as_posix():{'sha256':sha(p),'bytes':p.stat().st_size}for p in sorted(kit.rglob('*'))if p.is_file()}}
(kit/'release.json').write_text(json.dumps(release,indent=2)+'\n');(kit/'SHA256SUMS.txt').write_text(''.join(sha(p)+'  '+p.relative_to(kit).as_posix()+'\n' for p in sorted(kit.rglob('*'))if p.is_file()and p.name!='SHA256SUMS.txt'))
archive=OUT/'poolrotater-7.1.12-live-pilot.zip';zip_tree(kit,archive)
template=(ROOT/'inputs/predecessor-launcher.template').read_text().replace('v7.1.11','v7.1.12').replace('7.1.11-live-pilot','7.1.12-live-pilot')
launcher=template.replace('__ARCHIVE_SHA256__',sha(archive));ast.parse(launcher);launcher_name='poolrotater-repair-v7.1.12.py';(OUT/launcher_name).write_text(launcher)
(OUT/'RELEASE_SHA256SUMS.txt').write_text(sha(archive)+'  '+archive.name+'\n'+pin+'  runtime.zip\n'+sha(OUT/launcher_name)+'  '+launcher_name+'\n')
delta=OUT/'repository-delta';delta.mkdir()
with zipfile.ZipFile(ROOT/'inputs/v7.1.11-runtime.zip') as z:before=json.loads(z.read('bundle.json'))['files']
for p in (ROOT/'candidate/source').rglob('*'):
 if not p.is_file():continue
 name=p.relative_to(ROOT/'candidate/source').as_posix()
 if ('payload/'+name) not in before and name=='catalog-requirements-hashed.lock':continue
 if sha(p)==before.get('payload/'+name,{}).get('sha256'):continue
 q=delta/name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
for name,content in docs.items():p=delta/'docs'/name;p.parent.mkdir(exist_ok=True,parents=True);p.write_text(content)
for name in ('tests/test_profile_read_resilience.py', 'tests/test_deferred_statistics_reply.py', 'tests/test_closed_failed_update.py', 'tests/test_community_live_chat_parity.py', 'tests/test_community_freemod_display.py', 'tests/test_activity_commands.py', 'tests/community_repair_installed_upgrade_fixture.py', 'scripts/freeze_community_repair.py', 'scripts/community_repair_test_batch.py', 'scripts/prepare_community_repair_publication.py', 'website/community/display_model.py', 'website/community/community_feed.py', 'website/community/community.php', 'website/live/presentation.js', 'website/live/presentation.php', 'tests/test_provider_chat_recovery.py', 'tests/test_live_presentation.py', 'tests/closed_failed_installed_fixture.py', 'website/live/template/script.js'):
 p=delta/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
p=delta/'pilot';p.mkdir(exist_ok=True);shutil.copyfile(helper,p/'repair_and_start.py');(p/'repair_launcher.py').write_text(launcher);(p/'repair_launcher.py.template').write_text(template)
prior=(ROOT/'inputs/predecessor-README.md').read_text();detail=prior[prior.index('## Fresh install'):].replace(frozen['before_sha256'],pin).replace('V7_1_11_OPERATOR_GUIDE','V7_1_12_OPERATOR_GUIDE')
(delta/'README.md').write_text('# PoolRotater v7.1.12\n\nUse the [installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.12), [operator guide](docs/V7_1_12_OPERATOR_GUIDE.md) and [release notes](docs/V7_1_12_RELEASE_NOTES.md). Download the installer asset, not the source ZIP.\n\nCommunity profile-read resilience, live chat parity, automatic deferred stats replies and clearer website display. Complete local statistics and the nice-17 background resource policy are retained. Existing settings, credentials, catalogs, history, enrollment and privacy/update choices are preserved. Healthy hosts retain normal all-empty admission; an active host with every failed room confirmed closed can apply the repair. Deliberately stopped or uncertain hosts stay fenced.\n\n'+detail)
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
