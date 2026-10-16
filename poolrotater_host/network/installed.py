"""Verified installed network commands. Never authorize or control provider rooms."""
import hashlib
import os
from pathlib import Path
import signal
import threading
import time

from ..config import ConfigError
from .. import __version__
from ..layout import load, read_json, contained
from ..provisioning import load_provisioned
from ..provider_transport import TrustSnapshot
from .client import Client
from .agent import Agent
from .contracts import NetworkError, require, decode, fields, number
from .projection import read_status, project

ACTIONS=('configure','enroll','resume','rotate','leave','reset','status','support','update-report','reporting-configure','reporting-support','reporting-sync')


def guard(root,release):
    require(os.geteuid()!=0,'network_service_identity_required')
    runtime=load_provisioned(root,release=release,prepared=True)
    require(runtime['registration']['execution'] in ('pilot-candidate','pilot-live'),'network_installed_pilot_required')
    return runtime


def execute(client,action,stream=None):
    require(action in ACTIONS,'invalid_network_action')
    require((stream is not None)==(action in ('configure','enroll','reset','update-report','reporting-configure')),'network_private_input_required')
    if action.startswith('reporting-'):
        from .. import reporting
        if action=='reporting-configure':return reporting.configure(client.root,decode(stream.read(65537)))
        if action=='reporting-support':return reporting.support(client.root)
        return reporting.Reporter(client).step()
    if action=='update-report':
        from ..updates.telemetry import report
        from ..layout import file_lease
        value=report(decode(stream.read(8193)))
        with file_lease(client.lock_path):
            profile=client.profile();client.check_account(profile['account_id'])
            require(client.pending(profile)is None,'network_pending_requires_resume')
            credential=client.credential();require(credential is not None,'network_not_enrolled')
            return client.transport(profile).request('POST','/v1/hosts/self/updates',body=value,token=credential['credential'])
    if action=='status':return client.status()
    if action=='support':return diagnostics(client)
    if action=='configure':
        value=decode(stream.read(65537));fields(value,('origin','ca_pem','ca_sha256','operator_id','account_id','directory'))
        require(type(value['ca_pem']) is str,'invalid_network_trust')
        pem=value['ca_pem'].encode('ascii')
        require(hashlib.sha256(pem).hexdigest()==value['ca_sha256'],'network_trust_digest_mismatch')
        return client.configure(url=value['origin'],trust=TrustSnapshot(pem),operator_id=value['operator_id'],
                                account_id=value['account_id'],directory=value['directory'])
    if action=='enroll':
        value=decode(stream.read(65537));fields(value,('invitation',));return client.enroll(value['invitation'])
    if action=='reset':
        value=decode(stream.read(65537));fields(value,('expected_host_id','acknowledge_central_revocation_required'))
        return client.reset(**value)
    return getattr(client,action)()


def run(root,release,action,stream=None):
    # Must precede reading explicit stdin, any client secret or any hub request.
    guard(root,release)
    try:return {**execute(Client(root),action,stream),'protected_runtime_checked':True,'actual_room_testing':False}
    except NetworkError as exc:
        return {'ok':False,'error':exc.code,'protected_runtime_checked':True,'actual_room_testing':False,'live_rooms_affected':False}


def observation(root,code_version):
    _,config=load(root)
    try:snapshot=read_status(root)
    except (OSError,ConfigError,NetworkError):snapshot={}
    # Actual workers report their pinned set; do not pretend desired catalog
    # updates have already been adopted by a running worker.
    return project(snapshot,config,now=time.time(),code_version=code_version)


def serve(root,release):
    runtime=guard(root,release);client=Client(root);stop=threading.Event()
    previous={sig:signal.signal(sig,lambda *_:stop.set()) for sig in (signal.SIGTERM,signal.SIGINT)}
    from ..ratings.agent import Scheduler,FetchScheduler,run_combined
    from ..ratings.sync import Synchronizer
    sync=Synchronizer(root,network=client)
    from ..reporting import start
    reporting_thread=start(client,stop)
    try:run_combined(Agent(client,lambda:observation(root,__version__)),Scheduler(sync),stop,snapshots=FetchScheduler(sync))
    finally:
        stop.set();reporting_thread.join(timeout=6)
        for sig,handler in previous.items():signal.signal(sig,handler)


def diagnostics(client):
    """Support-safe local summary. No identity, endpoint, invitation or secret."""
    status=client.status()
    summary={k:status[k] for k in ('configured','enrolled','pending_operation','directory',
        'account_binding_matches','credential_expires_at','last_heartbeat_receipt_at','pending_heartbeat') if k in status}
    path=contained(client.root,'state/network-agent.json')
    if os.path.lexists(path):
        value=read_json(path);fields(value,('format','phase','error','attempted_at','retry_seconds','live_rooms_affected'))
        require(type(value['format']) is int and value['format']==1 and value['live_rooms_affected'] is False,'invalid_network_agent_receipt')
        require(value['phase'] in ('idle','not-configured','not-enrolled','operator-recovery-required','left',
                'reenrollment-required','connected','retrying','local-review-required'),'invalid_network_agent_receipt')
        require(value['error'] in (None,'authority-refused','hub-unavailable','local-state-refused'),'invalid_network_agent_receipt')
        number(value['attempted_at']);number(value['retry_seconds'],5,330)
        summary.update(agent_phase=value['phase'],agent_error=value['error'],last_attempt_at=value['attempted_at'])
    if not summary.get('configured'):next_action='network-configure'
    elif summary.get('pending_operation'):next_action='network-resume'
    elif not summary.get('account_binding_matches'):next_action='review-local-account-binding'
    elif not summary.get('enrolled'):next_action='obtain-bound-invitation-and-network-enroll'
    elif summary.get('agent_phase') in ('reenrollment-required','local-review-required','operator-recovery-required'):
        next_action='retain-state-and-review-network-recovery'
    else:next_action='inspect-network-service-and-last-heartbeat; ratings-sharing-is-separate'
    summary.update(next_action=next_action,
        enrollment_does_not_grant_contribution=True,
        directory_listing_verified=False,
        directory_observation=('sharing-disabled' if not summary.get('directory') else
            'last-heartbeat-acknowledged' if summary.get('last_heartbeat_receipt_at') else 'awaiting-heartbeat-acknowledgment'))
    return {'ok':True,**summary,'live_rooms_affected':False,'remote_health_verified':False}
