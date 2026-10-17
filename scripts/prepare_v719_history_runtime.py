"""Freeze reviewed history/statistics and prepared player controls over published v7.1.8."""
from pathlib import Path
import ast
import hashlib
import json
import pprint
import subprocess
import sys
import zipfile
from scripts.prepare_v71_runtime import fingerprints, source_files

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'validation/v7.1.9-history-stats/release'
BASE = ROOT / 'validation/v7.1.8-maintenance/release/runtime.zip'
PIN = '744bea14e3c3313ab3c4d269fc0a945214678bac75d78d87da90f7ad261e34e6'
BASELINES = ((BASE, PIN),
 (ROOT / 'validation/v7.1.7-timeout/release/runtime.zip','cc9c8205f283538e37507a66041da079681a19ad8d98fa59bb5b2b013333efb0'),
 (ROOT / 'validation/v7.1.1/release/runtime.zip','d9bc7d722b8b9f20d0c16ad79503bd8e811c41653a3e745f01c95df8a29e9efe'),
 (ROOT / 'validation/v7.1.2/release/runtime.zip','c9e51f15715fc96c677258bc2623a47ee18cfe8a04262cf8499404e43889aecc'),
 (ROOT / 'validation/v7.1.3/release/runtime.zip','bc0f258a8c0dd55de0d0d8773aec6356fddf9ead8067b4fdd614e27a6c0936b3'),
 (ROOT / 'validation/v7.1.4/release/runtime.zip','89628a6a141cec2f208bf7f30ef32466739c5352587336904a8435ef38803d68'),
 (ROOT / 'validation/v7.1.5-refresh-repair/release/runtime.zip','06fa5aebf68a138c16b449db2630ec776bd13244a84d7c46b4ff8da2a498dbde'),
 (ROOT / 'validation/v7.1.6-feedback/release/runtime.zip','554ac796491d912ee6c94965f09e65897bd3eb2559785540b5304e9a4a15d9ee'))
OVERLAYS = ('bot/bot.py','bot/languages.py','bot/standard_pool.py',
 'poolrotater_host/activity/collector.py','poolrotater_host/activity/reader.py',
 'poolrotater_host/activity/history.py','poolrotater_host/reporting.py')


def main():
    from scripts.test_isolation import require_test_isolation
    require_test_isolation('ordinary')
    assert not OUT.exists(), 'retain partial build and inspect before retry'
    assert hashlib.sha256(BASE.read_bytes()).hexdigest() == PIN
    OUT.mkdir(parents=True)
    source = OUT / 'source'
    wheels = OUT / 'wheelhouse'
    source.mkdir()
    wheels.mkdir()
    with zipfile.ZipFile(BASE) as archive:
        before = json.loads(archive.read('bundle.json'))
        for name in before['files']:
            prefix = 'payload/' if name.startswith('payload/') else 'wheelhouse/'
            target = (source if prefix == 'payload/' else wheels) / name.removeprefix(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    for name in OVERLAYS:
        raw = (ROOT / name).read_bytes()
        ast.parse(raw)
        (source / name).write_bytes(raw)
    init = source / 'poolrotater_host/__init__.py'
    old = init.read_text()
    assert old.count("__version__ = '7.1.8'") == 1
    init.write_text(old.replace("__version__ = '7.1.8'", "__version__ = '7.1.9'"))
    # Preserve every shipped repair-family allowance. Only add the new exact
    # unchanged-storage family here; never copy the older checkout allowlist.
    upgrade=source/'poolrotater_host/pilot_upgrade.py';text=upgrade.read_text()
    anchor="('v7.1.7', 'v7.1.8')):"
    assert text.count(anchor)==1
    pairs=",\n            ".join(repr((f'v7.1.{i}','v7.1.9'))for i in range(1,9))
    upgrade.write_text(text.replace(anchor,"('v7.1.7', 'v7.1.8'),\n            "+pairs+"):",1))
    release_source = (source / 'rolling/release.py').read_text()
    after = fingerprints(source_files(source), release_source)
    transitions = []
    for path, pin in BASELINES:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == pin
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('bundle.json'))
            old = fingerprints(manifest['files'], archive.read('payload/rolling/release.py').decode())
        assert old['storage'] == after['storage']
        transitions.append((old, after))
    transition = source / 'poolrotater_host/integrated_upgrade.py'
    transition.write_text(transition.read_text() +
        '\n# Exact unchanged-storage v7.1.1-v7.1.8 -> v7.1.9 reviewed history and player controls transitions.\nREVIEWED_TRANSITIONS += ' +
        pprint.pformat(tuple(transitions), width=110, sort_dicts=True) + '\n')
    pins = {row['name']: row for row in before['wheels']}
    trust = ast.parse((source / 'poolrotater_host/catalog_trust.py').read_text())
    names = next(ast.literal_eval(n.value) for n in trust.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'PINS' for t in n.targets))
    (source / 'catalog-requirements-hashed.lock').write_text(''.join(
        name + '==' + names[name] + ' --hash=sha256:' + pins[name]['sha256'] + '\n'
        for name in sorted(names)))
    (OUT / 'overlays.json').write_text(json.dumps(OVERLAYS))
    script = '''import json,sys
from pathlib import Path
source=Path(sys.argv[1]);sys.path.insert(0,str(source))
from poolrotater_host.runtime_package import build_live_pilot,inspect
from poolrotater_host.updates.publisher import classify
from poolrotater_host.updates.approval import compatible
from poolrotater_host.pilot_upgrade import require_repair_compatible
from scripts.prepare_v71_runtime import fingerprints
out=source.parent
build=build_live_pilot(source,out/'wheelhouse',out/'runtime.zip',version='v7.1.9',catalog_cache=out/'wheelhouse')
new=inspect(out/'runtime.zip',build['sha256'],execution='pilot-live')
def contract(spec):
 return {'version':spec['version'],'protocol':1,'storage_epoch':'poolrotater-6.7-v1','requirements':spec['environment_id'],'python_hash':spec['interpreter_sha256'],'environment_hash':spec['environment_id'],'migrations':[],**fingerprints(spec['files'],(source/'rolling/release.py').read_text())}
baselines=[]
for path,pin in json.loads(sys.argv[2]):
 old=inspect(Path(path),pin,execution='pilot-live')
 assert classify(old,new)=='controller';compatible(contract(old),contract(new));require_repair_compatible(contract(old),contract(new))
 for key in ('target','wheels','environment_id','interpreter_sha256'):assert old[key]==new[key]
 assert contract(old)['storage']==contract(new)['storage']
 baselines.append({'version':old['version'],'sha256':pin,'classification':'controller','signed_admission_compatible':True})
old=inspect(Path(sys.argv[3]),sys.argv[4],execution='pilot-live')
changes=sorted(name for name in new['files'] if new['files'][name]!=old['files'].get(name))
allowed={'payload/'+name for name in json.loads((out/'overlays.json').read_text())}|{'payload/poolrotater_host/__init__.py','payload/poolrotater_host/integrated_upgrade.py','payload/poolrotater_host/pilot_upgrade.py'}
assert set(changes)<=allowed
result={'ok':True,'build':build,'before_sha256':sys.argv[4],'baselines':baselines,'classification':'controller','changed_files':changes,'storage_unchanged':True,'dependencies_unchanged':True}
(out/'freeze.json').write_text(json.dumps(result,indent=2)+'\\n');print(json.dumps(result))
'''
    (OUT / 'build.py').write_text(script)
    subprocess.run([sys.executable, str(OUT / 'build.py'), str(source),
        json.dumps([(str(p), pin) for p, pin in BASELINES]), str(BASE), PIN], check=True)
    import shutil
    shutil.rmtree(wheels)


if __name__ == '__main__':
    main()
