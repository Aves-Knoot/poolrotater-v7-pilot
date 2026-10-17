#!/usr/bin/python3
"""Install a reviewed history manifest for the exact v7.1.9 runtime.

Read JSON on protected stdin. Only an explicitly reviewed source is admitted;
this does not enable sharing, import history synchronously or touch rooms.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

RUNTIME_SHA = '6e0d7260a803cdded105b086a04c1740e1dd2e389d6bd85aa6ad552fbedcc601'
ENTRY_SHA = 'c109a18f2697d35296c98149b6b59c578f4a675555ce509ae15cd38fdb88209f'


def install_review(root, value):
    """Called as the actual service UID, under root deployment admission."""
    from poolrotater_host.activity.history import review, source, PLAN
    from poolrotater_host.activity.collector import Collector
    from poolrotater_host.layout import file_lease, atomic_json, read_json, contained
    from poolrotater_host.network.contracts import require, fingerprint
    from poolrotater_host.reporting import profile
    root=Path(root)
    with file_lease(root/'state/reporting.lock',timeout=10):
        value=review(root,value)
        require(profile(root)['share_statistics'],'statistics_sharing_disabled')
        consent=Collector(root).consent()
        require(consent and consent['enabled'],'statistics_capture_consent_required')
        source(root,value)  # Verify a pinned backup before changing configuration.
        path=contained(root,PLAN)
        if os.path.lexists(path):
            require(review(root,read_json(path,maximum=256*1024))==value,'existing_history_review_changed')
            changed=False
        else:
            atomic_json(path,value);changed=True
        return {'ok':True,'history_review_installed':True,'configuration_changed':changed,
                'review_sha256':fingerprint(value),'backfill_started':False,
                'next_action':'independent_reporting_agent_captures_and_uploads_reviewed_history',
                'live_rooms_affected':False,'provider_dispatch':False,'sharing_changed':False}


def admit(target,parent):
    """Do not invalidate an already queued deployment's configuration proof."""
    from poolrotater_host.provisioning import _control
    from poolrotater_host.operator_maintenance import require_idle
    from poolrotater_host.updates.native import require_idle_worker_deployment
    require_idle(target,parent)
    require_idle_worker_deployment(target,parent)
    job=_control(target['prefix']/'controller-update.json',optional=True)
    if job and job.get('phase') not in ('complete','cancelled'):
        raise ValueError('history_review_waits_for_pending_controller_update')


def main():
    if os.geteuid()!=0:raise ValueError('administrator_required')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance',required=True)
    args=parser.parse_args()
    if not re.fullmatch('[a-z][a-z0-9-]{0,15}',args.instance):raise ValueError('invalid_instance')
    release=Path('/opt/poolrotater-v7-pilot')/args.instance/'releases'/('v7.1.9-'+RUNTIME_SHA[:16])
    entry=release/'poolrotater_host/pilot_admin_entry.py'
    import stat
    for path in (entry,*entry.parents):
        info=path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('unprotected_installed_code')
    if hashlib.sha256(entry.read_bytes()).hexdigest()!=ENTRY_SHA:raise ValueError('unexpected_installed_bootstrap')
    spec=importlib.util.spec_from_file_location('verified_history_admin',entry)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    if module.verified_release(args.instance)!=release:raise ValueError('foreign_installed_release')
    sys.path.insert(0,str(release))
    from poolrotater_host.pilot_installation import administrative_binding
    from poolrotater_host.layout import file_lease
    from poolrotater_host.network.contracts import decode
    from poolrotater_host.ratings.operations import _identity
    value=decode(sys.stdin.buffer.read(256*1024+1),maximum=256*1024)
    target,marker,parent=administrative_binding(args.instance)
    with file_lease(target['prefix']/'deployment-admission.lock'):
        target,marker,parent=administrative_binding(args.instance)
        if parent['bundle_sha256']!=RUNTIME_SHA:raise ValueError('unexpected_installed_runtime')
        admit(target,parent)
        with _identity(parent['service_uid']):result=install_review(target['root'],value)
    print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except Exception as error:
        # No arbitrary provider body, private path or player record is returned.
        allowed=('administrator_required','invalid_instance','unprotected_installed_code',
                 'unexpected_installed_bootstrap','foreign_installed_release','unexpected_installed_runtime',
                 'history_review_waits_for_pending_controller_update','statistics_sharing_disabled',
                 'statistics_capture_consent_required','foreign_history_review','existing_history_review_changed',
                 'history_source_digest_mismatch','history_source_requires_checkpoint')
        code=str(error) if str(error) in allowed else 'history_review_install_refused'
        print(json.dumps({'ok':False,'failure_code':code,'live_rooms_affected':False,'provider_dispatch':False}))
        raise SystemExit(2)
