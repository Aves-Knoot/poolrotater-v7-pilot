"""Actual root/UID cleanup, owner flock and /proc checks in a private PID namespace.

No systemd, provider, production paths or credentials are mounted. This is the
cleanup layer acceptance; full controller lifecycle acceptance is separate.
"""
from scripts.test_isolation import require_test_isolation
require_test_isolation('provisioning')
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
sys.path.insert(0,'/source/validation/v7.1.10-reliability/work')
from poolrotater_host.update_lifecycle import wait_stopped,diagnostic
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import atomic_json
from poolrotater_host.pilot_installation import _quiescent
from poolrotater_host.ratings.operations import _identity


def uid():os.setgroups([]);os.setgid(1000);os.setuid(1000)


def child(root,*,seconds=.3,lock=True):
    code='''import fcntl,os,sys,time
root,delay,locked=sys.argv[1],float(sys.argv[2]),sys.argv[3]=='yes'
fd=os.open(root+'/state/owner.lock',os.O_RDONLY)
if locked:fcntl.flock(fd,fcntl.LOCK_EX)
print('ready',flush=True)
time.sleep(delay)
'''
    proc=subprocess.Popen(['/usr/bin/python3.12','-B','-c',code,str(root),str(seconds),'yes'if lock else'no'],
        preexec_fn=uid,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    assert proc.stdout.readline().strip()=='ready'
    return proc


def main():
    assert os.geteuid()==0
    root=Path('/state/cleanup');root.mkdir(mode=0o700);os.chown(root,1000,1000)
    state=root/'state';state.mkdir(mode=0o700);os.chown(state,1000,1000)
    lock=state/'owner.lock';lock.touch(mode=0o600);os.chown(lock,1000,1000)
    records=[]
    for locked in (True,False):
        proc=child(root,lock=locked);receipt={};start=time.monotonic()
        wait_stopped(root,1000,seconds=3,save=lambda **v:receipt.update(v))
        elapsed=time.monotonic()-start
        assert proc.wait(timeout=2)==0 and elapsed>=.2
        assert receipt['cleanup_observations']>=2
        assert receipt['cleanup_wait_code']==('update_owner_lock_busy'if locked else'update_children_still_running')
        records.append({'case':'released_owner_lock'if locked else'delayed_uid_child','observations':receipt['cleanup_observations']})
    # Stubborn child is never killed by the updater and a hard bound holds.
    proc=child(root,seconds=20);start=time.monotonic()
    try:
        try:wait_stopped(root,1000,seconds=.2)
        except ConfigError as error:assert diagnostic(error)=='update_cleanup_timeout'
        else:raise AssertionError('unreleased lock must refuse')
        assert time.monotonic()-start<2 and proc.poll() is None
        records.append({'case':'persistent_child_retained','bounded':True})
    finally:
        with _identity(1000):os.kill(proc.pid,signal.SIGTERM)
        proc.wait(timeout=2)
    # A malformed owner lock must not be classified as a temporary busy lease.
    with _identity(1000):lock.chmod(0o644)
    start=time.monotonic()
    try:wait_stopped(root,1000,seconds=10)
    except ConfigError as error:assert str(error)=='invalid pilot owner lock'
    else:raise AssertionError('malformed lock accepted')
    assert time.monotonic()-start<1
    records.append({'case':'unsafe_lock_immediate_refusal'})
    with _identity(1000):lock.chmod(0o600)
    # Independent repeated recovery sees clean stopped state, without mutations.
    before=lock.stat()
    for _ in range(3):wait_stopped(root,1000,seconds=1)
    assert lock.stat().st_ino==before.st_ino and lock.stat().st_mtime_ns==before.st_mtime_ns
    records.append({'case':'repeat_without_lock_replacement'})
    # Unknown provider ownership stays fenced even after all real processes
    # disappear. Actual native reconciliation, no guard mocks.
    from poolrotater_host.layout import setup
    setup_root=Path('/state/ownership')
    setup(setup_root,['standard-auto','mania-4k'])
    for path in [setup_root,*setup_root.rglob('*')]:os.chown(path,1000,1000)
    owner=setup_root/'state/owner.lock';owner.touch(mode=0o600);os.chown(owner,1000,1000)
    rolling=setup_root/'state/rolling';rolling.mkdir(mode=0o700);os.chown(rolling,1000,1000)
    journal=rolling/'ownership.json'
    value={'format':1,'rooms':{'room1':{'phase':'stopped','operation':'closed','ownership_unknown':False},
        'room2':{'phase':'recovery_required','operation':'make_pending','ownership_unknown':True,'room_id':None}}}
    atomic_json(journal,value);os.chown(journal,1000,1000)
    raw=journal.read_bytes();start=time.monotonic()
    try:wait_stopped(setup_root,1000,seconds=10)
    except ConfigError as error:assert str(error)=='pilot ownership requires stopped reconciliation'
    else:raise AssertionError('unknown creation incorrectly accepted')
    assert time.monotonic()-start<1 and journal.read_bytes()==raw
    records.append({'case':'unknown_ownership_retained_after_process_exit'})
    print(json.dumps({'ok':True,'cases':records,'actual_uid':1000,'provider_dispatch':False,'nice':os.getpriority(os.PRIO_PROCESS,0)}))

if __name__=='__main__':main()
