"""Real protected v7.1.0 -> v7.1.1 upgrade on isolated root/UID tmpfs.

Only host platform/account/systemd discovery and tmpfs disk reserve are supplied
by the fixture. Runtime registrations, seals, stopped locks, SQLite, installer
transaction and service-UID bootstraps remain real. No provider or host mounts.
"""
from pathlib import Path
import hashlib,json,os,shutil,sqlite3,subprocess,sys
from types import SimpleNamespace
from unittest.mock import patch

CANDIDATE = 'v7.1.8'
BASELINE = sys.argv[1] if len(sys.argv)>1 else 'v7.1.7'
assert BASELINE in ('v7.1.1','v7.1.2','v7.1.5','v7.1.6','v7.1.7')
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
    base=Path('/source/validation/v7.1.7-timeout/release/runtime.zip' if BASELINE=='v7.1.7' else '/source/validation/v7.1.6-feedback/release/runtime.zip' if BASELINE=='v7.1.6' else '/source/validation/v7.1.5-refresh-repair/release/runtime.zip' if BASELINE=='v7.1.5' else f'/source/validation/{BASELINE}/release/runtime.zip')
    candidate=Path('/source/validation/v7.1.8-maintenance/release/runtime.zip')
    old={'v7.1.0':'8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35',
         'v7.1.1':'d9bc7d722b8b9f20d0c16ad79503bd8e811c41653a3e745f01c95df8a29e9efe',
         'v7.1.2':'c9e51f15715fc96c677258bc2623a47ee18cfe8a04262cf8499404e43889aecc',
         'v7.1.5':'06fa5aebf68a138c16b449db2630ec776bd13244a84d7c46b4ff8da2a498dbde',
         'v7.1.6':'554ac796491d912ee6c94965f09e65897bd3eb2559785540b5304e9a4a15d9ee',
         'v7.1.7':'cc9c8205f283538e37507a66041da079681a19ad8d98fa59bb5b2b013333efb0'}[BASELINE]
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
    # Preserve the incident's closed sibling and unknown replacement exactly.
    rolling=root/'state/rolling';rolling.mkdir(mode=0o700);os.chown(rolling,1000,1000)
    ownership={'format':1,'rooms':{
        'room1':{'phase':'parked','operation':'closed','room_id':None,'ownership_unknown':False,
                 'release':initial['release'],'last_good_release':initial['release']},
        'room2':{'phase':'recovery_required','operation':'make_pending','room_id':None,
                 'channel_id':None,'ownership_unknown':True,'release':initial['release']}}}
    atomic_json(rolling/'ownership.json',ownership);os.chown(rolling/'ownership.json',1000,1000)
    ownership_before=(rolling/'ownership.json').read_bytes()
    target={'prefix':prefix,'root':root,'user':'operator','unit':'invented.service'}
    with patch.object(upgrade,'_admin'),patch.object(upgrade,'paths',return_value=target),patch.object(upgrade,'_account',return_value=SimpleNamespace(pw_uid=1000)),patch.object(upgrade,'_unit_state'),patch.object(upgrade,'RESERVE',0),patch('poolrotater_host.pilot_operations.UNIT_BASE',units):
        result=upgrade.upgrade('upgrade',candidate,new,from_sha256=old)
    assert result['status']=='upgraded' and not result['service_started']
    assert (rolling/'ownership.json').read_bytes()==ownership_before
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
        'explicit_optouts_preserved':True,'ledger_schema_and_rows_preserved':True,'services_started':False,'actual_provider_dispatch':False,'incident_ownership_bytes_preserved':True}))

if __name__=='__main__':main()
