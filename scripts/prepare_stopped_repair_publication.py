"""Curate only the reviewed stopped repair and its operator instructions."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
import ast,hashlib,json,shutil,zipfile
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'publication';OUT.mkdir()
delta=OUT/'repository-delta';delta.mkdir();review=OUT/'payload-review';review.mkdir()
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
frozen=json.loads((ROOT/'candidate/freeze.json').read_text());pin=frozen['build']['sha256']
runtime=OUT/'poolrotater-v7.1.16-runtime.zip';shutil.copyfile(ROOT/'candidate/runtime.zip',runtime)
helper=OUT/'poolrotater-repair-v7.1.16.py';shutil.copyfile(ROOT/'candidate/repair-launcher.py',helper)
with zipfile.ZipFile(ROOT/'inputs/v7.1.14-runtime.zip')as z:before=json.loads(z.read('bundle.json'))['files']
with zipfile.ZipFile(runtime)as z:
 for name,row in json.loads(z.read('bundle.json'))['files'].items():
  if not name.startswith('payload/'):continue
  rel=name.removeprefix('payload/');data=z.read(name)
  if row['sha256']!=before.get(name,{}).get('sha256'):
   p=delta/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
  if rel.endswith('.py')or rel in('requirements.txt','requirements-hashed.lock','poolrotater_host/pilot_release.json'):
   p=review/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
command=f'''curl -fL https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/download/v7.1.16/{helper.name} -o {helper.name} &&
echo '{sha(helper)}  {helper.name}' | sha256sum -c - &&
sudo /usr/bin/python3.12 -I -S -B ./{helper.name} --instance two --start --resume-agents'''
notes='''# v7.1.16 — repair a stopped v7.1.13 startup failure

This is a local, explicitly authorized repair for an existing operator instance
whose signed v7.1.12 -> v7.1.13 update committed, then failed before room startup.
It retains the same installation, authorization, catalogs, titles, tuning,
ratings, statistics, enrollment and sharing/update preferences. No reinstall,
OAuth sign-in or repeated ownership-clear is needed.

The runtime includes the narrow cached-configuration compatibility fix first
qualified in owner-local v7.1.15: the new managed background-player-profile
boolean can be added without erasing local settings or accepting arbitrary
schema changes. Full catalogs, dependencies, storage formats and provider
pacing are unchanged.

The pinned command verifies the existing failed transaction, original signed
approval, complete code inventories, unchanged configuration, stopped processes
and positively closed ownership. It archives the old job and creates an explicit
stopped repair transaction. Ordinary protected native upgrade/recovery performs
replacement, fresh provider checks and one startup attempt. Successful healthy
workers are required before the old scheduler intent is retired with evidence.
Interrupted/uncertain startup never grants another automatic attempt.

The independent recovery pin and its policy are retained. Existing enabled or
disabled service choices are preserved; this does not grant boot autostart.
Original network-running intent is restored by the native transition. The
--resume-agents flag resumes previously active update polling after the repair.

This package is for the stopped committed-failure state, not an occupied-room
update, a fresh installer, or a general recovery override. The owner instance is
explicitly excluded. Existing release assets and queued owner runtime bytes
remain unchanged. Release registration does not advance the global channel head.

The same command returns already_running without another start after a
successful repair. A refusal retains root-private reports and all state. Do not
repeat startup when an outcome is uncertain. Return the final JSON; report files
may contain private account identifiers and must be redacted before forwarding.

Actual provider/gameplay acceptance remains the operator's next observation.
'''
guide='''# Existing stopped instance: one repair command

Keep the existing two instance and all its state. This command is intended for
an installed v7.1.13 successor held at controller_start after the signed update
from v7.1.12, with every old room confirmed closed. It refuses live rooms,
unknown ownership, unrelated failures, changed settings or a different runtime.

Run on the operator's existing machine:

```sh
'''+command+'''
```

Expect a download and integrity checks, a stopped upgrade, catalog loading and
one startup attempt. Allow several minutes; do not launch another copy while
it runs. Success ends with ok:true, phase:started, version:v7.1.16, room IDs and
a private reports directory. Start attempts are reported explicitly. Send the
final JSON and both room links, plus whether commands and normal map progression
work. The maintainer can inspect subsequent authenticated hub telemetry.

If it refuses, retain the reports and state and return the final JSON. Do not
reinstall, reauthorize, clear ownership or repeatedly start. An already-running
repaired instance returns phase:already_running without restarting rooms.

No bot/network boot enablement is added. The already installed recovery timer's
enabled choice and local pause policy are retained. Current sharing choices,
custom room names and difficulty ranges remain unchanged.

Runtime SHA256: `'''+pin+'''`

Launcher SHA256: `'''+sha(helper)+'''`
'''
(delta/'docs').mkdir();(delta/'docs/V7_1_16_RELEASE_NOTES.md').write_text(notes);(delta/'docs/STOPPED_UPDATE_REPAIR.md').write_text(guide)
(delta/'README.md').write_text('# PoolRotater stopped-update repair\n\n[v7.1.16](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.16) repairs the retained committed v7.1.13 failed-start state. See [the one-command instructions](docs/STOPPED_UPDATE_REPAIR.md) and [release notes](docs/V7_1_16_RELEASE_NOTES.md). It preserves the operator installation and is not a fresh installer.\n\n'+(ROOT/'inputs/current-public-README.md').read_text())
for name in ('tests/test_stopped_repair_launcher.py','tests/test_stopped_update_repair.py','tests/test_profile_schema_upgrade.py','tests/test_update_superseded.py','scripts/freeze_stopped_repair.py','scripts/prepare_stopped_repair_publication.py','pilot/stopped_repair.py.template'):
 p=delta/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
for p in delta.rglob('*.py'):
 q=review/p.relative_to(delta);q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
shutil.copyfile(helper,review/helper.name)
for folder in(delta,review):
 for p in folder.rglob('*.py'):ast.parse(p.read_bytes(),filename=str(p))
(OUT/'RELEASE_NOTES.md').write_text(notes+'\n'+guide)
(OUT/'RELEASE_SHA256SUMS.txt').write_text(pin+'  '+runtime.name+'\n'+sha(helper)+'  '+helper.name+'\n')
(ROOT.parent/'OPERATOR_COMMAND.txt').write_text(command+'\n')
q={'ok':True,'runtime_sha256':pin,'helper_sha256':sha(helper),'runtime':runtime.name,'helper':helper.name,
 'github_parent':json.loads((ROOT/'inputs/github-before.json').read_text())['head'],
 'changed_public_files':{p.relative_to(delta).as_posix():{'sha256':sha(p),'bytes':p.stat().st_size}for p in sorted(delta.rglob('*'))if p.is_file()},
 'scanner_pending':True,'published':False,'channel_targets_changed':False}
(OUT/'preparation.json').write_text(json.dumps(q,indent=2)+'\n');print(json.dumps({k:q[k]for k in('runtime_sha256','helper_sha256')}))
