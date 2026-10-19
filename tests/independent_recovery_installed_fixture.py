"""Protected launcher qualification with real UID separation and full catalogs.

Only systemd discovery is simulated: the bootstrap is executed as an actual
system-Python subprocess, verifies all pinned files/dependencies, and re-execs
its sealed environment. No host mount, credential or external network exists.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, '/source/candidate/source')
from scripts.test_isolation import require_test_isolation
require_test_isolation('provisioning')
from poolrotater_host.layout import setup, load, atomic_json
from poolrotater_host.provisioning import _provision, _control, _write
from poolrotater_host.ratings.operations import _identity
from poolrotater_host.network.client import Client
from poolrotater_host.network.contracts import secret
from poolrotater_host.provider_transport import TrustSnapshot
from poolrotater_host.testing.provider_https import certificates
from poolrotater_host.rescue import operations


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    os.umask(0o077)
    root = Path('/var/lib/poolrotater-v7-pilot/two')
    prefix = Path('/opt/poolrotater-v7-pilot/two')
    units = Path('/runtime/units')
    units.mkdir()
    setup(root, ['standard-auto', 'mania-4k'])
    marker, host = load(root)
    host['execution'] = 'pilot-live'
    atomic_json(root/'config/host.json', host)
    for path in (root, *root.rglob('*')):
        os.chown(path, 1000, 1000)
    baseline = Path('/source/inputs/v7.1.13-runtime.zip')
    old = '98bc2e7a9a127062d7bbff451dbef743395dfe3dfb3c8453a4b53e268fa3010c'
    candidate = Path('/source/candidate/runtime.zip')
    pin = json.loads(Path('/source/candidate/freeze.json').read_text())['build']['sha256']
    assert sha(baseline) == old and sha(candidate) == pin
    _provision(root, prefix, baseline, old, uid=1000, execution='pilot-live', reserve=0)
    registered_path=prefix/'installations'/(marker['installation_id']+'.json')
    registered_before=registered_path.read_bytes()
    pending=prefix/'transitions'/(marker['installation_id']+'.json')
    atomic_json(pending,{'format':1,'phase':'prepared','fixture':'unresolved_transition_retained'})
    pending_before=pending.read_bytes()
    from poolrotater_host.config import ConfigError
    try:
        _provision(root,prefix,candidate,pin,uid=1000,execution='pilot-live',reserve=0,stage_bootstrap=True)
    except ConfigError:
        pass
    else:
        raise AssertionError('ordinary staging bypassed an unresolved transition')
    _provision(root, prefix, candidate, pin, uid=1000, execution='pilot-live', reserve=0,
               stage_bootstrap=True,recovery_only=True)
    assert registered_path.read_bytes()==registered_before and pending.read_bytes()==pending_before
    record = _control(prefix/'candidates'/marker['installation_id']/(pin+'.json'))
    _write(prefix/'pilot-install.json', {'installation_id': marker['installation_id']})
    certs = Path('/runtime/certificates')
    certs.mkdir()
    certificates(certs)
    trust = TrustSnapshot((certs/'ca.crt').read_bytes())
    credential = {'format': 1, **{k:marker[k] for k in ('installation_id','generation')},
        'host_id': '00000000-0000-0000-0000-000000000007',
        'server_epoch': '00000000-0000-0000-0000-000000000008',
        'credential': secret(), 'credential_expires_at': 9999999999}
    with _identity(1000):
        client = Client(root)
        client.configure(url='https://127.0.0.1:9443', trust=trust,
            operator_id='00000000-0000-0000-0000-000000000009', account_id=99, directory=True)
        atomic_json(root/'secrets/network.json', credential)
    directory = prefix/'updates/cache'
    directory.mkdir(mode=0o700, parents=True)
    atomic_json(directory/'policy.json', {'format':1,
        'binding':{**{k:marker[k] for k in ('installation_id','generation')},'host_id':credential['host_id']},
        'channel':'pilot','automatic_download':True,'automatic_install':True,'controller_updates':True,
        'paused':False,'check_seconds':1800,'report_to_hub':True})
    catalogs = root/'catalogs/retained'
    catalogs.mkdir()
    os.chown(catalogs,1000,1000)
    counts = {}
    for mode in ('standard','mania4','mania7'):
        target = catalogs/mode
        target.mkdir()
        os.chown(target,1000,1000)
        for source in (Path('/source/inputs/full-catalogs')/mode).glob('*.db'):
            out = target/source.name
            shutil.copyfile(source,out)
            out.chmod(0o600)
            os.chown(out,1000,1000)
        with sqlite3.connect((target/'catalog.db').as_uri()+'?mode=ro',uri=True) as db:
            counts[mode]=db.execute('SELECT COUNT(*) FROM '+('beatmaps'if mode=='standard'else'maps')).fetchone()[0]
    assert counts['standard'] > 100000 and counts['mania4'] > 19000 and counts['mania7'] > 4000
    retained = {str(p):sha(p) for folder in ('config','secrets','catalogs')
                for p in (root/folder).rglob('*') if p.is_file()}
    # Exercise the actual one-command helper with a local pinned archive.
    # Only systemd calls target a fixture substitute; extraction, checksum,
    # staging, identity switching and initial HTTPS failure are real.
    helper=Path('/source/publication/poolrotater-install-recovery-v7.1.14.py')
    driver="""import sys,runpy
from pathlib import Path
sys.path.insert(0,'/source/candidate/source')
import poolrotater_host.pilot_operations as p
import poolrotater_host.pilot_installation as installation
# Disposable tmpfs cannot reserve five GiB; production keeps that guard.
installation.RESERVE=0
p.UNIT_BASE=Path('/runtime/units')
p._systemctl=lambda *a,**kw:b''
sys.argv=[str(Path(sys.argv[1])), '--instance','two','--bundle','/source/candidate/runtime.zip']
helper=runpy.run_path(sys.argv[0],run_name='fixture_helper')
helper['main']()
"""
    bootstrap=subprocess.run(['/usr/bin/python3.12','-I','-S','-B','-c',driver,str(helper)],
        cwd='/tmp',env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'},
        capture_output=True,text=True,timeout=60)
    assert bootstrap.returncode==0,(bootstrap.stdout,bootstrap.stderr)
    bootstrap_result=json.loads(bootstrap.stdout)
    assert bootstrap_result['recovery_installed'] and bootstrap_result['operator_state_preserved']
    assert registered_path.read_bytes()==registered_before and pending.read_bytes()==pending_before
    actions = []
    with patch('poolrotater_host.pilot_operations.UNIT_BASE',units), \
         patch('poolrotater_host.pilot_operations._systemctl',side_effect=lambda *args,**kw:actions.append(args)):
        result = operations.install('two',record)
    assert result['recovery_installed'] and not result['rooms_started']
    assert actions == [('daemon-reload',),('enable','--now','poolrotater-v7-recovery-two.timer')]
    service=(units/'poolrotater-v7-recovery-two.service').read_text()
    assert 'Nice=17\n' in service and 'CPUQuota=50%\n' in service
    assert 'Requires=' not in service and 'PartOf=' not in service
    launcher=prefix/'recovery-bootstrap.py'
    command=['/usr/bin/python3.12','-I','-S','-B',str(launcher),'--instance','two','--action','status']
    env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
    def status():
        return subprocess.run(command,env=env,cwd='/tmp',capture_output=True,text=True,timeout=45)
    first=status()
    assert first.returncode==0,(first.stdout,first.stderr)
    # Deliberately break the rotating pointer: diagnostics must use its own pin.
    registration=prefix/'installations'/(marker['installation_id']+'.json')
    original=registration.read_bytes()
    atomic_json(registration,{'broken_registration_for_fixture':True})
    second=status()
    assert second.returncode==0,(second.stdout,second.stderr)
    assert json.loads(second.stdout)['service_state']=='unavailable'  # No systemd in the isolated mount.
    registration.write_bytes(original)
    # The pin is retained across normal cleanup, even after registration moves.
    from poolrotater_host.updates.installed_retention import references
    before=_control(registration)
    with patch('poolrotater_host.pilot_operations.UNIT_BASE',units):
        pins,paths=references({'prefix':prefix,'root':root},before)
    assert pin in pins and record['release'] in paths
    # An extra/tampered pinned file refuses *before* imported recovery code.
    extra=Path(record['release'])/'unreviewed.py'
    extra.write_text('raise RuntimeError("must not import")\n')
    bad=status()
    assert bad.returncode==2 and 'recovery_bootstrap_refused' in bad.stdout
    extra.unlink()
    assert status().returncode==0
    assert retained=={name:sha(Path(name)) for name in retained}
    assert pending.read_bytes()==pending_before
    assert not list((prefix/'releases').rglob('*.pyc'))
    print(json.dumps({'ok':True,'full_catalog_rows':counts,'retained_files':len(retained),
        'broken_registration_diagnostics_verified':True,'tamper_refused':True,
        'pinned_recovery_retained':True,'rooms_started':False,'actual_provider_dispatch':False,
        'pending_transition_preserved_during_recovery_staging':True,
        'one_command_helper_with_pinned_local_bundle_verified':True,
        'real_system_python_bootstrap':True,'root_service_uid_separation':True}))


if __name__=='__main__':
    main()
