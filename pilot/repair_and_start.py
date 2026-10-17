#!/usr/bin/python3.12
"""Verified stopped upgrade/recovery; an explicitly requested start is attempted once."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile

ENV = {'PATH':'/usr/sbin:/usr/bin:/bin', 'LANG':'C.UTF-8',
       'PYTHONDONTWRITEBYTECODE':'1', 'MALLOC_ARENA_MAX':'2'}
REPORT_BASE = Path('/var/lib/poolrotater-v7-repair-reports')
EXPECTED_VERSION = 'v7.1.5'


def require(condition, message):
    if not condition: raise ValueError(message)


def private_directory(path):
    if not path.exists(): path.mkdir(mode=0o700, parents=True)
    require(not path.is_symlink(), 'report_directory_refused')
    info=path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode & 0o077,
            'report_directory_refused')
    return path


def verify_kit(kit):
    release=json.loads((kit/'release.json').read_text())
    require(release.get('runtime_version')==EXPECTED_VERSION, 'repair_release_refused')
    for name,row in release['files'].items():
        path=PurePosixPath(name)
        require(not path.is_absolute() and all(x not in ('','..','.') for x in name.split('/')),
                'repair_member_refused')
        target=kit/name
        require(not target.is_symlink() and target.is_file(), 'repair_member_refused')
        raw=target.read_bytes()
        require(len(raw)==row['bytes'] and hashlib.sha256(raw).hexdigest()==row['sha256'],
                'repair_member_changed')
    require(hashlib.sha256((kit/'runtime.zip').read_bytes()).hexdigest()==release['runtime_sha256'],
            'repair_runtime_changed')
    return release


def unpack_runtime(kit, stage):
    with zipfile.ZipFile(kit/'runtime.zip') as archive:
        rows=archive.infolist()
        require(len({r.filename for r in rows})==len(rows), 'repair_archive_refused')
        require(sum(r.file_size for r in rows)<=128*1024**2, 'repair_archive_refused')
        for row in rows:
            name=row.filename;path=PurePosixPath(name)
            require(not path.is_absolute() and all(x not in ('','..','.') for x in name.split('/'))
                    and stat.S_ISREG(row.external_attr>>16) and not row.flag_bits&1,
                    'repair_archive_refused')
            if not name.startswith('payload/'): continue
            target=stage/name;target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
            target.write_bytes(archive.read(row))
    return stage/'payload'


class Operations:
    def __init__(self, instance, report):
        self.instance=instance;self.report=report;self.counter=0;self.record=None

    def save(self, label, value):
        self.counter+=1
        path=self.report/('%02d-%s.json'%(self.counter,label))
        path.write_text(json.dumps(value,indent=2)+'\n');path.chmod(0o600)

    def systemctl(self, *args):
        result=subprocess.run(['/usr/bin/systemctl',*args],env=ENV,cwd='/tmp',
            capture_output=True,text=True,timeout=35)
        require(result.returncode==0, 'repair_service_operation_refused')
        return result.stdout

    def state(self, unit):
        raw=self.systemctl('show',unit,'--property=LoadState,ActiveState,MainPID,UnitFileState')
        result=dict(line.split('=',1) for line in raw.splitlines())
        require(set(result)=={'LoadState','ActiveState','MainPID','UnitFileState'}, 'repair_service_state_refused')
        result['MainPID']=int(result['MainPID'])
        return result

    def admin(self, action, *args):
        entry=Path(self.record['release'])/'poolrotater_host/pilot_admin_entry.py'
        result=subprocess.run(['/usr/bin/python3.12','-I','-S',str(entry),
            '--instance',self.instance,action,*args],env=ENV,cwd='/tmp',
            capture_output=True,text=True,timeout=240)
        self.counter+=1
        stem=self.report/('%02d-%s'%(self.counter,action))
        stem.with_suffix('.stdout').write_text(result.stdout)
        stem.with_suffix('.stderr').write_text(result.stderr)
        require(result.returncode==0, 'repair_operator_step_refused')
        value=json.loads(result.stdout)
        require(type(value)is dict and value.get('ok',True)is True, 'repair_operator_step_refused')
        return value


def run(args, kit, release, ops, installation, upgrade):
    target,marker,record=installation.administrative_binding(args.instance)
    ops.record=record
    state=ops.state(target['unit']);ops.save('service-before',state)
    if state['MainPID'] or state['ActiveState'] not in ('inactive','failed'):
        require(record['bundle_sha256']==release['runtime_sha256'] and args.start,
                'repair_requires_stopped_bot')
        return {'ok':True,'phase':'already_running','version':EXPECTED_VERSION,
                'service_restarted':False,'state_preserved':True}
    require(state['UnitFileState']=='disabled', 'repair_requires_disabled_bot')
    # Stop only the auxiliary timer/agent. A committing updater is never killed.
    # No enable/disable policy is changed and no room service is stopped here.
    names={'timer':'poolrotater-v7-updates-'+args.instance+'.timer',
           'updater':'poolrotater-v7-updates-'+args.instance+'.service',
           'network':'poolrotater-v7-network-'+args.instance+'.service'}
    auxiliary={k:ops.state(v) for k,v in names.items()}
    ops.save('auxiliary-before',auxiliary)
    if auxiliary['timer']['LoadState']!='not-found':ops.systemctl('stop',names['timer'])
    updater=ops.state(names['updater'])
    require(updater['MainPID']==0 and updater['ActiveState'] in ('inactive','failed'),
            'repair_wait_for_existing_updater')
    if auxiliary['network']['LoadState']!='not-found':ops.admin('network-stop')
    if record['bundle_sha256']!=release['runtime_sha256']:
        supported={r['sha256'] for r in release['supported_predecessors']}
        require(record['bundle_sha256'] in supported, 'repair_predecessor_refused')
        result=upgrade.upgrade(args.instance,kit/'runtime.zip',release['runtime_sha256'],
                               from_sha256=record['bundle_sha256'])
        ops.save('upgrade',result)
        require(result.get('status') in ('upgraded','already_upgraded') and result.get('service_started')is False,
                'repair_upgrade_requires_review')
        target,marker,ops.record=installation.administrative_binding(args.instance)
    review=ops.admin('ownership-review')
    require(review.get('journal_unchanged')is True and not review.get('untracked_active_room_ids')
            and not review.get('untracked_unconfirmed_room_ids'), 'repair_ownership_requires_review')
    rooms=review.get('rooms');require(type(rooms)is list and rooms, 'repair_ownership_requires_review')
    if not all(r.get('result')=='local_clean' for r in rooms):
        flags=['--journal-sha256',review['journal_sha256']]
        unknown=[r for r in rooms if r.get('result')=='unknown_creation']
        if unknown:
            require(len(unknown)==1 and args.room_alias==unknown[0].get('alias') and args.created_room_id is not None,
                    'repair_explicit_creation_identity_required')
            flags+=['--room-alias',args.room_alias,'--created-room-id',str(args.created_room_id),'--confirm-lost-creation']
        clear=ops.admin('ownership-clear',*flags)
        require(clear.get('phase')=='committed' and clear.get('recovery_applied')is True,
                'repair_ownership_requires_review')
        review=ops.admin('ownership-review')
    require(review.get('journal_unchanged')is True and
            all(r.get('result')=='local_clean' for r in review['rooms']) and
            not review.get('untracked_active_room_ids') and not review.get('untracked_unconfirmed_room_ids'),
            'repair_ownership_requires_review')
    ops.admin('status');ops.admin('support')
    if not args.start:
        return {'ok':True,'phase':'ready_for_start','version':EXPECTED_VERSION,'service_started':False,
                'state_preserved':True,'ownership_review':'local_clean'}
    # Reset only systemd's historical failed marker, after clean recovery.
    ops.systemctl('reset-failed',target['unit'])
    ops.admin('start')  # Exactly one attempt. Uncertain outcomes are never retried.
    ops.admin('status');ops.admin('support')
    resumed=[]
    if args.resume_agents:
        if auxiliary['network']['LoadState']!='not-found':
            network=ops.admin('network-status')
            if network.get('enrolled')is True and network.get('pending_operation')is None:
                ops.admin('network-start');resumed.append('network')
        if auxiliary['timer']['LoadState']!='not-found':
            # Existing polling consent/pauses remain authoritative. Starting a
            # previously installed timer does not enable it or change policy.
            ops.systemctl('start',names['timer']);resumed.append('update_polling')
    return {'ok':True,'phase':'started','version':EXPECTED_VERSION,'service_started':True,
            'start_attempts':1,'state_preserved':True,'ownership_review':'local_clean','agents_started':resumed}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance',required=True)
    parser.add_argument('--room-alias')
    parser.add_argument('--created-room-id',type=int)
    parser.add_argument('--start',action='store_true')
    parser.add_argument('--resume-agents',action='store_true')
    args=parser.parse_args()
    require(os.geteuid()==0, 'repair_requires_administrator')
    require(re.fullmatch('[a-z][a-z0-9-]{0,15}',args.instance)is not None and args.instance!='owner',
            'repair_instance_refused')
    require((args.room_alias is None)==(args.created_room_id is None), 'repair_creation_arguments_refused')
    if args.created_room_id is not None:
        require(0<args.created_room_id<10**19 and re.fullmatch('[a-z][a-z0-9_-]{0,31}',args.room_alias)is not None,
                'repair_creation_arguments_refused')
    require(not args.resume_agents or args.start, 'repair_resume_requires_start')
    os.umask(0o077);os.chdir('/tmp')
    kit=Path(__file__).resolve().parent;release=verify_kit(kit)
    report=private_directory(private_directory(REPORT_BASE)/args.instance)/str(uuid.uuid4())
    private_directory(report)
    try:
        with tempfile.TemporaryDirectory(prefix='prv7-verified-repair-') as folder:
            source=unpack_runtime(kit,Path(folder));sys.path.insert(0,str(source))
            from poolrotater_host import pilot_installation, pilot_upgrade
            result=run(args,kit,release,Operations(args.instance,report),pilot_installation,pilot_upgrade)
        result['reports']=str(report)
        (report/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
    except Exception:
        failure={'ok':False,'phase':'requires_review','reports':str(report),
                 'next_action':'retain_state_and_reports; do_not_retry_start_if_outcome_uncertain'}
        (report/'result.json').write_text(json.dumps(failure,indent=2)+'\n')
        print(json.dumps(failure,indent=2));raise SystemExit(2)


if __name__=='__main__':
    try:main()
    except (ValueError,OSError):
        print(json.dumps({'ok':False,'phase':'repair_preflight_refused','service_started':False}));raise SystemExit(2)
