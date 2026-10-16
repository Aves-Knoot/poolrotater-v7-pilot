#!/usr/bin/python3
"""Explicit reviewed legacy import for the exact installed v7.1.0 runtime.

Default is a private local plan. --apply reads an installation-bound review from
stdin. No provider request, consent change, unit operation or raw player export.
Run from the checksum-verified release kit with system Python -I -S -B as root.
"""
import argparse
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

RUNTIME_SHA = '8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35'
ENTRY_SHA = '9a877925c23f792ba994cd413c764f69e5e026220339011869b22aa648300d0c'


def review(value):
    fields = {'installation_id', 'generation', 'public_room_ids',
              'public_history_confirmed', 'include_lobby_totals'}
    if type(value) is not dict or set(value) != fields:
        raise ValueError('explicit_bound_history_review_required')
    for key in ('installation_id', 'generation'):
        if type(value[key]) is not str or str(uuid.UUID(value[key])) != value[key]:
            raise ValueError('invalid_history_review_identity')
    rooms = value['public_room_ids']
    if (type(rooms) is not list or not 1 <= len(rooms) <= 5000
            or any(type(room) is not int or not 0 < room < 2**63 for room in rooms)
            or len(set(rooms)) != len(rooms)):
        raise ValueError('explicit_reviewed_public_rooms_required')
    if value['public_history_confirmed'] is not True or type(value['include_lobby_totals']) is not bool:
        raise ValueError('explicit_history_confirmation_required')
    return value


def history_progress(root):
    """Bounded scalar progress only; never export source player rows."""
    import sqlite3
    from urllib.parse import quote
    from poolrotater_host.activity.storage import connection
    from poolrotater_host.activity.collector import MAX_OUTBOX_EVENTS
    high = {}
    with sqlite3.connect('file:'+quote(str(root/'state/controller-ratings.db'))+'?mode=ro',uri=True) as source:
        for table in ('autohost_pp_achievements','autohost_pp_achievements_mania'):
            exists=source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            high[table]=source.execute('SELECT COALESCE(MAX(rowid),0) FROM '+table).fetchone()[0] if exists else 0
    with connection(root/'state/activity-outbox.sqlite3') as target:
        complete={}
        for table,maximum in high.items():
            cursor=target.execute('SELECT value FROM meta WHERE key=?',('cursor_'+table+'_history',)).fetchone()
            complete[table]=int(cursor[0] if cursor else 0)>=maximum
        remaining=target.execute('SELECT COUNT(*) FROM history_baselines WHERE queued=0').fetchone()[0]
        full=target.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]>=MAX_OUTBOX_EVENTS
    return {'complete':complete,'baseline_remaining':remaining,'full':full}


def bind_history_review(root,value,consent):
    # A new reviewed room manifest must rescan earlier skipped rows. Existing
    # source keys keep replay idempotent; ordinary post-opt-in cursors are intact.
    from poolrotater_host.activity.storage import connection
    signature=json.dumps({'review':value,'consent_id':consent['consent_id']},sort_keys=True,separators=(',',':'))
    with connection(root/'state/activity-outbox.sqlite3') as target:
        prior=target.execute("SELECT value FROM meta WHERE key='history_helper_review'").fetchone()
        if prior is None or prior[0]!=signature:
            target.execute("DELETE FROM meta WHERE key IN ('cursor_autohost_pp_achievements_history','cursor_autohost_pp_achievements_mania_history')")
            target.execute("INSERT OR REPLACE INTO meta VALUES('history_helper_review',?)",(signature,))


def history_action(root, value, *, apply, batches):
    """Execute under the installed service UID; called by the protected launcher."""
    from poolrotater_host.layout import load, owner_lease, atomic_json
    from poolrotater_host.activity_migration import capture_enabled, _stopped_journal
    from poolrotater_host.reporting import profile
    from poolrotater_host.activity import Collector
    import sqlite3
    from urllib.parse import quote
    marker, config = load(root)
    with owner_lease(root):
        _stopped_journal(root, config)
        if not capture_enabled(root):
            raise ValueError('activity_capture_migration_required')
        # Fixed native ledger only. A read-only consistent snapshot cannot infer
        # room privacy; IDs are supplied for the operator's independent review.
        with sqlite3.connect('file:' + quote(str(root/'state/controller-ratings.db')) + '?mode=ro', uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            rooms = sorted({row[0] for row in db.execute('SELECT room_id FROM autohost_rooms LIMIT 5001')}
                           | {row[0] for row in db.execute('SELECT DISTINCT room_id FROM autohost_room_players LIMIT 5001')})
        if len(rooms) > 5000:
            raise ValueError('history_room_review_bound_exceeded')
        if not apply:
            return {'ok': True, **marker, 'source_room_ids_for_review': rooms,
                    'share_statistics': profile(root)['share_statistics'],
                    'public_history_inferred': False, 'import_applied': False,
                    'provider_dispatch': False, 'services_started': False}
        value = review(value)
        if any(value[key] != marker[key] for key in ('installation_id', 'generation')):
            raise ValueError('foreign_history_review')
        if not profile(root)['share_statistics']:
            raise ValueError('statistics_sharing_must_be_independently_enabled')
        collector = Collector(root)
        if not collector.consent() or not collector.consent()['enabled']:
            raise ValueError('statistics_capture_consent_required')
        bind_history_review(root,value,collector.consent())
        added = 0
        result = {}
        progress=history_progress(root)
        scanned=progress['complete'].copy()
        for _ in range(batches):
            result = collector.import_history(public_room_ids=value['public_room_ids'],
                public_history_confirmed=True, include_lobby_totals=value['include_lobby_totals'], limit=500)
            added += result['historical_events_queued']
            progress=history_progress(root)
            scanned={table:scanned[table] or done for table,done in progress['complete'].items()}
            if progress['full'] or (all(scanned.values()) and (not value['include_lobby_totals'] or not progress['baseline_remaining'])):
                break
        complete=all(scanned.values()) and (not value['include_lobby_totals'] or not progress['baseline_remaining'])
        result.update(historical_events_queued=added, import_applied=True,
                      history_capture_paused=bool(progress['full'] and not complete),
                      history_scan_complete=complete,
                      import_may_need_more_batches=not complete,
                      services_started=False)
        atomic_json(root/'state/activity-history-review.json',
                    {'format': 1, 'review': value, 'result': result})
        return result


def protected(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unprotected_installed_code')
    return path


def stopped_units(instance):
    names=('poolrotater-v7-pilot-'+instance+'.service','poolrotater-v7-network-'+instance+'.service',
           'poolrotater-v7-updates-'+instance+'.service','poolrotater-v7-updates-'+instance+'.timer',
           'poolrotater-v7-controller-update-'+instance+'.service','poolrotater-v7-controller-update-'+instance+'.timer')
    for name in names:
        result=subprocess.run(['/usr/bin/systemctl','show',name,'--property=LoadState,ActiveState,MainPID','--no-pager'],
                              capture_output=True,text=True,timeout=10,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
        state=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode in (0,4) and state.get('LoadState')=='not-found':continue
        if (result.returncode or state.get('LoadState')!='loaded' or state.get('ActiveState') not in ('inactive','failed')
                or (name.endswith('.service') and state.get('MainPID')!='0')
                or (name.endswith('.timer') and state.get('MainPID') not in (None,'0'))):
            raise ValueError('all_instance_services_and_update_timers_must_be_stopped')


def binding(instance):
    if not re.fullmatch('[a-z][a-z0-9-]{0,15}', instance):
        raise ValueError('invalid_instance')
    release = protected(Path('/opt/poolrotater-v7-pilot')/instance/'releases'/('v7.1.0-'+RUNTIME_SHA[:16]))
    entry = protected(release/'poolrotater_host/pilot_admin_entry.py')
    if hashlib.sha256(entry.read_bytes()).hexdigest() != ENTRY_SHA:
        raise ValueError('unexpected_installed_bootstrap')
    spec = importlib.util.spec_from_file_location('verified_v71_admin', entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.verified_release(instance) != release:
        raise ValueError('foreign_installed_release')
    sys.path.insert(0, str(release))
    from poolrotater_host.pilot_installation import administrative_binding, require_no_service_processes
    target, marker, record = administrative_binding(instance)
    if record['bundle_sha256'] != RUNTIME_SHA or record['release'] != str(release):
        raise ValueError('unexpected_installed_runtime')
    stopped_units(instance)
    require_no_service_processes(record['service_uid'])
    return target, marker, record


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate_history_review_field')
        result[key] = value
    return result


def main():
    if os.geteuid() != 0:
        raise ValueError('administrator_required')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--batches', type=int, default=20, choices=range(1, 101))
    args = parser.parse_args()
    value = None
    if args.apply:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('history_review_too_large')
        value = review(json.loads(raw, object_pairs_hook=pairs,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
    target, marker, record = binding(args.instance)
    if value and any(value[key] != marker[key] for key in ('installation_id', 'generation')):
        raise ValueError('foreign_history_review')
    # The installed interpreter and all its dependencies were verified by the
    # existing administrative binding. The helper itself is never imported as
    # the service UID from an operator's home/root-only temporary directory.
    child = ('import json,sys,uuid\nfrom pathlib import Path\n'
             + inspect.getsource(review) + '\n' + inspect.getsource(history_progress)
             + '\n' + inspect.getsource(bind_history_review) + '\n' + inspect.getsource(history_action)
             + '\nx=json.load(sys.stdin)\n'
             + 'sys.path.insert(0,x["release"])\n'
             + 'from poolrotater_host.provisioning import load_provisioned\n'
             + 'load_provisioned(Path(x["root"]),release=Path(x["release"]),prepared=True)\n'
             + 'print(json.dumps(history_action(Path(x["root"]),x["review"],apply=x["apply"],batches=x["batches"])))\n')
    payload = {'root': str(target['root']), 'release': record['release'],
               'review': value, 'apply': args.apply, 'batches': args.batches}
    def identity():
        os.setgroups([])
        os.setgid(record['service_uid'])
        os.setuid(record['service_uid'])
        os.umask(0o077)
        os.nice(max(0, 15-os.getpriority(os.PRIO_PROCESS, 0)))
    result = subprocess.run([str(Path(record['environment'])/'bin/python'), '-I', '-B', '-c', child],
        input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={'PATH':'/usr/sbin:/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'},
        preexec_fn=identity, timeout=180)
    if result.returncode:
        raise ValueError('reviewed_history_import_refused_retain_state')
    response = json.loads(result.stdout)
    if response.get('ok') is not True:
        raise ValueError('reviewed_history_import_refused_retain_state')
    print(json.dumps(response))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok':False,'error':str(error) if type(error) is ValueError else
                          'reviewed_history_import_refused_retain_state','provider_dispatch':False}), file=sys.stderr)
        raise SystemExit(2)
