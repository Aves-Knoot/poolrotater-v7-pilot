"""Protected 1.2->1.4->1.8 and ancestor-worker retirement on root/UID tmpfs.

Only host platform/account/systemd discovery and tmpfs disk reserve are supplied
by the fixture. Runtime registrations, seals, stopped locks, SQLite, installer
transaction and service-UID bootstraps remain real. No provider or host mounts.
"""
from pathlib import Path
import hashlib,json,os,shutil,sqlite3,subprocess,sys
from types import SimpleNamespace
from unittest.mock import patch

CANDIDATE = 'v7.1.8'
BASELINE = 'v7.1.2'
assert CANDIDATE == 'v7.1.8' and BASELINE == 'v7.1.2'
sys.path.insert(0,'/source/validation/v7.1.8-maintenance/release/source')
from scripts.test_isolation import require_test_isolation
require_test_isolation('provisioning')
from poolrotater_host.layout import setup,load,atomic_json
from poolrotater_host.provisioning import _provision,_write
from poolrotater_host import pilot_upgrade as upgrade,pilot_installation as installation
from poolrotater_host.activity_migration import prepare
from poolrotater_host.runtime_defaults import prepare as defaults

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def identity():os.setgroups([]);os.setgid(1000);os.setuid(1000)

def main():
    os.umask(0o077)
    root=Path('/state/upgrade');prefix=Path('/runtime/upgrade');units=Path('/runtime/units');units.mkdir()
    base=Path('/source/validation/v7.1.0-release-final2/runtime.zip' if BASELINE=='v7.1.0' else f'/source/validation/{BASELINE}/release/runtime.zip')
    candidate=Path('/source/validation/v7.1.8-maintenance/release/runtime.zip')
    old={'v7.1.0':'8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35',
         'v7.1.1':'d9bc7d722b8b9f20d0c16ad79503bd8e811c41653a3e745f01c95df8a29e9efe',
         'v7.1.2':'c9e51f15715fc96c677258bc2623a47ee18cfe8a04262cf8499404e43889aecc'}[BASELINE]
    new=json.loads(Path('/source/validation/v7.1.8-maintenance/release/freeze.json').read_text())['build']['sha256']
    assert sha(base)==old and sha(candidate)==new
    setup(root,['standard-auto','mania-4k']);marker,cfg=load(root)
    cfg['execution']='pilot-live';atomic_json(root/'config/host.json',cfg)
    for p in [root,*root.rglob('*')]:os.chown(p,1000,1000)
    initial=_provision(root,prefix,base,old,uid=1000,execution='pilot-live',reserve=0)
    lock=root/'state/owner.lock';lock.touch(mode=0o600);os.chown(lock,1000,1000)
    with installation._quiescent(root,1000):prepare(root,marker,uid=1000,owner_lease_held=True)
    defaults(root,marker,uid=1000,previous_release=initial['release'])
    intent={'format':1,'execution':'pilot-live','bundle_sha256':old,'service_uid':1000,
        'host_root':str(root),'account_ready':True,'presets':['standard-auto','mania-4k'],
        **{k:marker[k] for k in ('installation_id','generation')}}
    _write(prefix/'pilot-install.json',intent)
    import bot
    bot.__path__=[str(Path(initial['release'])/'bot')]
    from poolrotater_host.ratings.operations import _identity
    from bot.ratings import RatingStore
    with _identity(1000):
        ledger=RatingStore(str(root/'state/controller-ratings.db'))
        for i in range(12):ledger.rate(900000+i,7000+i,1)
        ledger.record_room_visibility(99,public=True,observed_at=1000,mode='standard')
        ledger.add_room_player_time(room_id=99,user_id=7000,username='Invented retained player',seconds=120,left_at=1200)
    # Explicit v7.1.0 opt-outs, credentials and arbitrary local history stay byte
    # identical; the missing-profile default is checked separately over HTTPS.
    _write(root/'config/reporting.json',{'format':1,**{k:marker[k] for k in ('installation_id','generation')},
        **dict.fromkeys(('share_statistics','consume_statistics','operational_logs','public_chat'),False)},owner=1000,private=True)
    _write(root/'secrets/retained-fixture.json',{'credential':'invented-retained-canary'},owner=1000,private=True)
    # Use complete prepared catalog SQLite files, not miniature test catalogs.
    prepared=Path('/source/validation/owner-pilot/conversion-catalog-qualification/prepared')
    counts={};destination=root/'catalogs/retained';destination.mkdir();os.chown(destination,1000,1000)
    for mode in ('standard','mania4','mania7'):
        target=destination/mode;target.mkdir();os.chown(target,1000,1000)
        for source in (prepared/mode).glob('*.db'):
            out=target/source.name;shutil.copyfile(source,out);out.chmod(0o600);os.chown(out,1000,1000)
        with sqlite3.connect((target/'catalog.db').as_uri()+'?mode=ro',uri=True) as db:
            table='beatmaps' if mode=='standard' else 'maps'
            counts[mode]=db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    assert counts['standard']>100000 and counts['mania4']>19000 and counts['mania7']>4000
    retained=[p for folder in ('config','secrets','catalogs') for p in (root/folder).rglob('*') if p.is_file()]
    retained += [root/'state/controller-ratings.db',root/'state/gameplay-defaults.json']
    def ledger_contents():
        with sqlite3.connect((root/'state/controller-ratings.db').as_uri()+'?mode=ro',uri=True) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            return '\n'.join(db.iterdump())
    ledger_before=ledger_contents()
    before={str(p):sha(p) for p in retained if p.name!='controller-ratings.db'}
    target={'prefix':prefix,'root':root,'user':'operator','unit':'invented.service'}
    with patch.object(upgrade,'_admin'),patch.object(upgrade,'paths',return_value=target),patch.object(upgrade,'_account',return_value=SimpleNamespace(pw_uid=1000)),patch.object(upgrade,'_unit_state'),patch.object(upgrade,'RESERVE',0),patch('poolrotater_host.pilot_operations.UNIT_BASE',units):
        # First commit the already-qualified 1.2->1.4 transition while keeping
        # a genuinely approved 1.3 worker as a closed launch hint. Only the old
        # frozen reviewed fingerprints are temporarily used for this old hop.
        from poolrotater_host.provisioning import _control
        from poolrotater_host.worker_releases import receipt_path,validate_candidate
        initial_record=_control(prefix/'installations'/(marker['installation_id']+'.json'))
        worker_digest='bc0f258a8c0dd55de0d0d8773aec6356fddf9ead8067b4fdd614e27a6c0936b3'
        worker_bundle=Path('/source/validation/v7.1.3/release/runtime.zip')
        assert sha(worker_bundle)==worker_digest
        staged=_provision(root,prefix,worker_bundle,worker_digest,uid=1000,execution='pilot-live',reserve=0,stage_bootstrap=True)
        proposal=_control(prefix/'candidates'/marker['installation_id']/(worker_digest+'.json'))
        approval={'format':1,'parent_sha256':old,'candidate':proposal}
        approval_path=receipt_path(initial_record,worker_digest)
        for folder in (prefix/'worker-releases',approval_path.parent):
            folder.mkdir(mode=0o755,exist_ok=True);folder.chmod(0o755)
        _write(approval_path,approval);validate_candidate(initial_record,approval)
        from poolrotater_host.runtime_registry import PrivateRegistry
        from rolling.state import Registry
        rolling=root/'state/rolling';rolling.mkdir(mode=0o700);os.chown(rolling,1000,1000)
        with _identity(1000):
            registry=PrivateRegistry(Registry(root/'state/rolling/ownership.json'))
            registry.record('room1',phase='stopped',operation='closed',room_id=None,ownership_unknown=False,
                release=staged['release'],last_good_release=staged['release'],version='v7.1.3',last_good_version='v7.1.3')
            registry.record('room2',phase='parked',operation='closed',room_id=None,ownership_unknown=False,
                release=initial['release'],last_good_release=initial['release'],version='v7.1.2',last_good_version='v7.1.2')
        ownership_before=(root/'state/rolling/ownership.json').read_bytes()
        intermediate=Path('/source/validation/v7.1.4/release/runtime.zip')
        mid_digest='89628a6a141cec2f208bf7f30ef32466739c5352587336904a8435ef38803d68'
        assert sha(intermediate)==mid_digest
        # Load every exact grant, including appended reviewed transitions,
        # from the pinned intermediate archive; the initial AST assignment alone
        # omits later release-family grants.
        import zipfile
        with zipfile.ZipFile(intermediate) as archive:
            code=archive.read('payload/poolrotater_host/integrated_upgrade.py')
        namespace={};exec(compile(code,'pinned-intermediate-grants','exec'),namespace)
        contracts=namespace['REVIEWED_TRANSITIONS']
        from poolrotater_host import integrated_upgrade
        with patch.object(integrated_upgrade,'REVIEWED_TRANSITIONS',contracts):
            first=upgrade.upgrade('upgrade',intermediate,mid_digest,from_sha256=old)
        assert first['status']=='upgraded' and not first['service_started']
        result=upgrade.upgrade('upgrade',candidate,new,from_sha256=mid_digest)
        assert (root/'state/rolling/ownership.json').read_bytes()==ownership_before
        current=_control(prefix/'installations'/(marker['installation_id']+'.json'))
        sys.path.insert(1,str(Path(current['environment'])/'lib/python3.12/site-packages'))
        bot.__path__=[str(Path(current['release'])/'bot')]
        from poolrotater_host.worker_releases import retired_controller,load_worker
        from rolling.supervisor import RollingSupervisor
        from rolling.release import load_release
        closed=registry.snapshot()['rooms']['room1']
        assert retired_controller(current,Path(staged['release']),closed)
        assert retired_controller(current,Path(initial_record['release']),closed)
        assert not retired_controller(current,Path('/runtime/unapproved-worker'),closed)
        assert not retired_controller(current,Path(staged['release']),{**closed,'room_id':99})
        view=SimpleNamespace(registry=registry,release=Path(current['release']),protected_parent=current,
            runtime_validator=lambda p:load_worker(root,p,prepared=True),manifest=load_release(Path(current['release'])))
        with _identity(1000):
            assert RollingSupervisor.start_release_for(view,'room1')==Path(current['release'])
            assert RollingSupervisor.start_release_for(view,'room2')==Path(current['release'])
        with installation._stopped(root,1000):pass
        # Real protected unit files plus fake systemd discovery only. Rebinding
        # a stopped earlier launcher must not require manual root unit editing.
        from poolrotater_host import controller_update
        from poolrotater_host.config import ConfigError
        def systemctl(*arguments,**kwargs):
            return b'ActiveState=inactive\nMainPID=0\n' if arguments[0]=='show' else b''
        with patch.object(controller_update,'_binding',return_value=(target,marker,None,current)),patch('poolrotater_host.pilot_operations.UNIT_BASE',units),patch('poolrotater_host.pilot_operations._systemctl',side_effect=systemctl):
            _write(prefix/'controller-update.json',{'phase':'waiting_empty','after':upgrade._record(_control(prefix/'pilot-upgrade.json'),prefix,marker['installation_id'])['before']})
            controller_update.arm('upgrade')
            service=units/'poolrotater-v7-controller-update-upgrade.service'
            original=service.read_bytes()
            _write(prefix/'controller-update.json',{'phase':'waiting_empty','after':current})
            controller_update.arm('upgrade')
            rebound=service.read_bytes()
            assert str(Path(current['release'])).encode() in rebound and original!=rebound
            service.chmod(0o644);service.write_bytes(original+b'ExecStartPost=/bin/false\n')
            try:controller_update.arm('upgrade')
            except ConfigError:pass
            else:raise AssertionError('unrecognized unit accepted')
            service.write_bytes(original)
            with patch('poolrotater_host.pilot_operations._systemctl',return_value=b'ActiveState=active\nMainPID=123\n'):
                try:controller_update.arm('upgrade')
                except ConfigError:pass
                else:raise AssertionError('active timer service rewritten')
            assert service.read_bytes()==original
            service.write_bytes(rebound)
            _write(prefix/'controller-update.json',{'phase':'cancelled','after':current})
            with patch('poolrotater_host.pilot_operations._systemctl') as calls:
                terminal=controller_update.arm('upgrade')
                assert terminal['timer_armed'] is False
                calls.assert_not_called()
        assert (root/'state/rolling/ownership.json').read_bytes()==ownership_before
        print(json.dumps({'ok':True,'chain':['v7.1.2','v7.1.4','v7.1.8'],
            'approved_ancestor_worker':'v7.1.3','service_uid_start_release_for':True,
            'ownership_bytes_unchanged':True,'no_intermediate_rooms':True,'native_timer_rebind_verified':True,'unknown_and_active_unit_refused':True}))
    assert result['status']=='upgraded' and not result['service_started']
    after={str(p):sha(p) for p in retained if p.name!='controller-ratings.db'}
    assert ledger_contents()==ledger_before,'ledger schema or rows changed'
    assert before==after,{'changed_retained_files':[name for name in before if before[name]!=after[name]]}
    env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1',
        'POOLROTATER_OFFLINE_SANDBOX':'1','POOLROTATER_PROVISIONING_SANDBOX':'1'}
    code="import runpy,sys,json;from pathlib import Path;module=runpy.run_path(sys.argv[1],run_name='fixture_preflight');value=module['preflight'](Path(sys.argv[2]),Path(sys.argv[3]),prepared=True);print(json.dumps({'ok':True,'protected_runtime_checked':True}))"
    checked=subprocess.run(['/usr/bin/python3.12','-I','-S','-c',code,str(prefix/'bootstrap.py'),str(root),str(prefix)],preexec_fn=identity,env=env,cwd='/tmp',capture_output=True,text=True,timeout=45)
    assert checked.returncode==0,checked.stderr
    if CANDIDATE=='v7.1.4':
        from tests.v714_external_logging_fixture import qualify
        from poolrotater_host.provisioning import _control
        current=_control(prefix/'installations'/(marker['installation_id']+'.json'))
        qualify(root,prefix,marker,current)
    print(json.dumps({'ok':True,'root_and_service_uid_verified':True,'upgrade_transaction':'committed',
        'version':CANDIDATE,'baseline':BASELINE,'runtime_sha256':new,'retained_files':len(retained),'full_catalog_rows':counts,
        'explicit_optouts_preserved':True,'ledger_schema_and_rows_preserved':True,'services_started':False,'actual_provider_dispatch':False}))

if __name__=='__main__':main()
