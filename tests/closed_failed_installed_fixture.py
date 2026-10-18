"""Real protected v7.1.10 -> v7.1.11 upgrade on isolated root/UID tmpfs.

Only host platform/account/systemd discovery and tmpfs disk reserve are supplied
by the fixture. Runtime registrations, seals, stopped locks, SQLite, installer
transaction and service-UID bootstraps remain real. No provider or host mounts.
"""
from pathlib import Path
import hashlib,json,os,shutil,sqlite3,subprocess,sys
from types import SimpleNamespace
from unittest.mock import patch

CANDIDATE = 'v7.1.12'
BASELINE = 'v7.1.11'
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
    base=Path('/source/inputs/v7.1.11-runtime.zip')
    candidate=Path('/source/candidate/runtime.zip')
    old='9750a3ee044db4aa852c4d3dfecb48838281ad237cad2e96d112983ea4ad8149'
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
    # A real staged successor handles the predecessor's two failed/closed slots.
    import time,signal,sys
    from poolrotater_host import controller_update as controller
    from poolrotater_host import pilot_operations
    from poolrotater_host.provisioning import _control
    from poolrotater_host.network import operations as network
    rolling=root/'state/rolling';rolling.mkdir(mode=0o700);os.chown(rolling,1000,1000)
    closed={a:{'phase':'parked','operation':'closed','room_id':None,'pid':None,'ownership_unknown':False,
               'generation':'fixture-worker-'+a,'release':initial['release'],'last_good_release':initial['release'],
               'error':None}for a in ('room1','room2')}
    ownership={'format':1,'rooms':closed}
    atomic_json(rolling/'ownership.json',ownership);os.chown(rolling/'ownership.json',1000,1000)
    ownership_before=(rolling/'ownership.json').read_bytes()
    snapshot={'rooms':{a:{**r,'status':{'running':True,'reopenable':False,'paused':False,
              'maintenance':{'locked':False,'uncertain':False}}}for a,r in closed.items()},'configuration_error':None}
    def publish():
        value={**snapshot,'generated_at':time.time()};atomic_json(rolling/'status.json',value);os.chown(rolling/'status.json',1000,1000)
    publish()
    writer_code = "import sys,json,time;from pathlib import Path;from poolrotater_host.layout import atomic_json;path=Path(sys.argv[1]);value=json.loads(sys.argv[2]);print('ready',flush=True)\nwhile True:\n value['generated_at']=time.time();atomic_json(path,value);time.sleep(.1)"
    writer=subprocess.Popen([sys.executable,'-B','-c',writer_code,str(rolling/'status.json'),json.dumps(snapshot)],
        preexec_fn=identity,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    assert writer.stdout.readline().strip()=='ready'

    _provision(root,prefix,candidate,new,uid=1000,execution='pilot-live',reserve=0,stage_bootstrap=True)
    parent=_control(prefix/'installations'/(marker['installation_id']+'.json'))
    after_registration=_control(prefix/'candidates'/marker['installation_id']/(new+'.json'))
    target={'prefix':prefix,'root':root,'user':'operator','unit':'invented.service'}
    job={'id':'invented','rooms':['room1','room2'],'phase':'waiting_empty','empty_since':None,
         'before':parent,'after':after_registration,'archive':str(candidate),'network_active':False,
         'network_enabled':False,'service_enabled':False,'configuration':controller._config_hashes(root)}
    events=[]
    from poolrotater_host import update_lifecycle
    original_failed=update_lifecycle.failed
    def explain_failed(save,job,error):
        print('fixture_commit_error',type(error).__name__,str(error),flush=True)
        return original_failed(save,job,error)
    def save(**values):job.update(values)
    def systemctl(*args,**kwargs):
        events.append(args[0])
        if args[0]=='stop'and writer.poll()is None:
            with _identity(1000):writer.terminate()
            writer.wait(timeout=3)
    def lifecycle(instance,action):
        assert action=='start';current=_control(prefix/'installations'/(marker['installation_id']+'.json'))
        assert current['bundle_sha256']==new;events.append('one_successor_start');return {'ok':True}
    def waited(instance,target,after):assert after['bundle_sha256']==new;events.append('successor_readiness')
    try:
        with patch.object(upgrade,'_admin'),patch.object(upgrade,'paths',return_value=target),patch.object(upgrade,'_account',return_value=SimpleNamespace(pw_uid=1000)),patch.object(upgrade,'_unit_state'),patch.object(upgrade,'RESERVE',0),patch('poolrotater_host.pilot_operations.UNIT_BASE',units),patch.object(pilot_operations,'_systemctl',side_effect=systemctl),patch.object(pilot_operations,'lifecycle',side_effect=lifecycle),patch.object(network,'_checked_unit'),patch('poolrotater_host.updates.recovery.wait_for_workers',side_effect=waited),patch.object(controller,'disarm'),patch.object(update_lifecycle,'failed',side_effect=explain_failed):
            assert controller._step_closed_failed('upgrade',target,parent,job,save,now=time.time())
            assert job['phase']=='waiting_empty' and events==[]
            job['empty_since']-=31
            assert controller._step_closed_failed('upgrade',target,parent,job,save,now=time.time())
        assert job['phase']=='complete',{k:job.get(k)for k in ('phase','stage','failure_stage','failure_code','reason')}
        assert events.count('one_successor_start')==1 and 'successor_readiness'in events
        assert (rolling/'ownership.json').read_bytes()==ownership_before
    finally:
        if writer.poll()is None:
            with _identity(1000):writer.terminate()
            writer.wait(timeout=3)
    after={str(p):sha(p) for p in retained if p.name!='controller-ratings.db'}
    assert ledger_contents()==ledger_before,'ledger schema or rows changed'
    assert before==after,{'changed_retained_files':[name for name in before if before[name]!=after[name]]}
    print(json.dumps({'ok':True,'real_root_service_uid_admission':True,'real_protected_upgrade':'committed',
        'predecessor':BASELINE,'version':CANDIDATE,'runtime_sha256':new,'full_catalog_rows':counts,
        'configuration_and_credentials_preserved':True,'ratings_and_history_preserved':True,
        'closed_ownership_journal_unchanged':True,'successor_start_attempts':events.count('one_successor_start'),
        'external_service_boundaries_invented':True,'actual_provider_dispatch':False}))

if __name__=='__main__':main()
