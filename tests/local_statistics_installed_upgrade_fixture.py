"""Real protected v7.1.10 -> v7.1.11 upgrade on isolated root/UID tmpfs.

Only host platform/account/systemd discovery and tmpfs disk reserve are supplied
by the fixture. Runtime registrations, seals, stopped locks, SQLite, installer
transaction and service-UID bootstraps remain real. No provider or host mounts.
"""
from pathlib import Path
import hashlib,json,os,shutil,sqlite3,subprocess,sys
from types import SimpleNamespace
from unittest.mock import patch

CANDIDATE = 'v7.1.11'
BASELINE = 'v7.1.10'
assert len(sys.argv)==1
sys.path.insert(0,'/source/candidate/source')

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
    base=Path('/source/inputs/v7.1.10-runtime.zip')
    candidate=Path('/source/candidate/runtime.zip')
    old='4dac79a77705494df51a34a0e25e8cd9c6e9bba2c6e61a02ef2292bd156be2e9'
    new=json.loads(Path('/source/candidate/freeze.json').read_text())['build']['sha256']
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
    prepared=Path('/source/inputs/full-catalogs')
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
    # Exercise the new history helper with real root/service UID separation,
    # after proving the upgrade preserved the explicit original opt-outs.
    import importlib.util
    spec=importlib.util.spec_from_file_location('qualified_history_installer',Path('/source/pilot/install_history_review.py'))
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    helper_copy=Path('/runtime/history-review.py');shutil.copyfile('/source/pilot/install_history_review.py',helper_copy);helper_copy.chmod(0o444)
    from poolrotater_host.provisioning import _control
    current=_control(prefix/'installations'/(marker['installation_id']+'.json'))
    helper.admit(target,current)
    review={'format':1,**{k:marker[k]for k in ('installation_id','generation')},
        'public_room_ids':[99],'public_history_confirmed':True,'include_lobby_totals':False,
        'exclude_unreviewed_players':True}
    code = """import sys,json,importlib.util
from pathlib import Path
x=json.load(sys.stdin);sys.path.insert(0,x['release'])
from poolrotater_host.provisioning import load_provisioned
load_provisioned(Path(x['root']),release=Path(x['release']),prepared=True)
spec=importlib.util.spec_from_file_location('history_review_helper','/runtime/history-review.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
root=Path(x['root'])
try:helper.install_review(root,x['review'])
except Exception as error:assert str(error)=='statistics_sharing_disabled'
else:raise AssertionError('history helper enabled explicit opt-out')
from poolrotater_host import reporting
reporting.configure(root,dict(share_statistics=True,consume_statistics=False,operational_logs=False,public_chat=False))
installed=helper.install_review(root,x['review'])
assert installed['history_review_installed'] and not installed['live_rooms_affected']
assert not helper.install_review(root,x['review'])['configuration_changed']
assert (root/'config/activity-history.json').stat().st_uid==1000
assert (root/'config/activity-history.json').stat().st_mode&0o777==0o600
print(json.dumps({'ok':True,'actual_service_uid':1000,'manifest_private':True}))
"""
    child=subprocess.run([str(Path(current['environment'])/'bin/python'),'-I','-B','-c',code],
        input=json.dumps({'root':str(root),'release':current['release'],'review':review}),
        preexec_fn=identity,env=env,cwd='/tmp',capture_output=True,text=True,timeout=45)
    assert child.returncode==0,child.stderr
    assert json.loads(child.stdout)['ok']
    assert (rolling/'ownership.json').read_bytes()==ownership_before
    assert ledger_contents()==ledger_before
    print(json.dumps({'ok':True,'root_and_service_uid_verified':True,'upgrade_transaction':'committed',
        'version':CANDIDATE,'baseline':BASELINE,'runtime_sha256':new,'retained_files':len(retained),'full_catalog_rows':counts,
        'explicit_optouts_preserved':True,'ledger_schema_and_rows_preserved':True,'services_started':False,'actual_provider_dispatch':False,'incident_ownership_bytes_preserved':True,'history_review_root_service_uid_verified':True,'history_helper_preserved_optout_and_ownership':True}))

if __name__=='__main__':main()
