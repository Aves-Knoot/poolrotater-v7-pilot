"""Independent authenticated command delivery; never touches provider transport.

The controller applies durable local requests. A separately installed, fixed
privileged reconciler can start a stopped controller; this agent has no shell,
systemctl, OAuth access or privilege escalation endpoint.
"""
import os
from pathlib import Path
import threading
import time

from ..config import ConfigError
from ..layout import atomic_json,file_lease,read_json,LeaseBusy
from ..network.contracts import NetworkError,fields,integer,require
from .contracts import request
from .hub import PATH


class Agent:
    def __init__(self,client,*,clock=time.time,migrate=None,open_state=None,observation=None):
        from .policy import state,migrate as policy_migration
        self.client=client;self.root=Path(client.root);self.clock=clock
        self.open_state=open_state or (lambda:state(self.root))
        self.migrate=migrate or (lambda store:policy_migration(client,store))
        self.observation=observation or self.controller_phase
        self.failures=0;self.path=self.root/'state/managed-agent.json'
        self.details_cache=None;self.details_probe_at=0

    def controller_phase(self):
        # This is an observation only, never ownership or permission authority.
        # A stale service file must not masquerade as a live controller.
        from ..network.projection import read_status
        path=self.root/'state/rolling/status.json'
        if not os.path.lexists(path):return 'unavailable'
        # The native supervisor deliberately publishes this owned observation
        # as0640. Reuse its bounded/no-symlink reader; private config/secrets
        # still require the stricter0600 layout reader.
        value=read_status(self.root)
        at=value.get('generated_at')
        if type(at) not in (int,float) or not 0<=self.clock()-at<=30:return 'unavailable'
        return 'running'

    def exchange(self,store,profile,credential):
        store.supersede_expired_exchange()
        body=store.exchange(controller_phase=self.observation())
        reply=self.client.transport(profile).request('POST',PATH,body=body,token=credential['credential'])
        fields(reply,('ok','sequence','requests'))
        require(reply['ok'] is True and type(reply['sequence']) is int and reply['sequence']==body['sequence'],
                'invalid_managed_exchange')
        require(type(reply['requests']) is list and len(reply['requests'])<=8,'invalid_managed_exchange')
        # Validate the entire authenticated response before admitting any item.
        seen=set()
        for value in reply['requests']:
            request(value)
            require(all(value[k]==store.binding[k] for k in store.binding),'host_identity_mismatch',403)
            require(value['id'] not in seen,'invalid_managed_exchange');seen.add(value['id'])
        for value in reply['requests']:store.receive(value)
        store.accepted_authority(credential['server_epoch'])
        # Crash before here safely replays the exact observation and request IDs.
        # Receipt hashes sent in this body are acknowledged, not newer progress
        # produced by the controller while the HTTP exchange was in flight.
        store.acknowledge_exchange(body)
        return len(reply['requests'])

    def sync_settings(self,store,profile,credential):
        from ..diagnostic_logs.common import transaction
        from . import details
        # Only revisions affect the settings document. Frequent exchanges and
        # heartbeats do not repeatedly serialize large greeting dictionaries.
        with transaction(store.path) as db:
            signature=tuple((r['alias'],r['revision'],r['applied_revision']) for r in
                db.execute('SELECT alias,revision,applied_revision FROM rooms ORDER BY alias'))
        key=(tuple(sorted(store.binding.items())),signature)
        if self.details_cache is None or self.details_cache[0]!=key:
            value=details.snapshot(store);pin,pages=details.encode(value)
            self.details_cache=(key,value,pin,pages)
        _,value,pin,pages=self.details_cache
        path=self.root/'state/managed-settings-upload.json'
        saved=read_json(path) if os.path.lexists(path) else None
        require(saved is None or type(saved) is dict,'invalid_managed_settings')
        binding={**store.binding,'server_epoch':credential['server_epoch']}
        if saved is None or saved.get('binding')!=binding or saved.get('content_id')!=pin:
            saved={'format':1,'binding':binding,'content_id':pin,'next_index':0,'pages':len(pages)}
        fields(saved,('format','binding','content_id','next_index','pages'))
        require(saved['format']==1 and saved['pages']==len(pages),'invalid_managed_settings')
        integer(saved['next_index'],0,len(pages))
        if saved['next_index']==len(pages):
            if self.clock()<self.details_probe_at:return 'synchronized'
            saved['next_index']=0  # Read/repair probe after a restored hub DB.
        for _ in range(8):
            reply=self.client.transport(profile).request('POST',details.PATH,
                body=details.page(value,saved['next_index'],pages,pin),token=credential['credential'])
            fields(reply,('ok','content_id','next_index','complete'))
            require(reply['ok'] is True and reply['content_id']==pin and type(reply['complete']) is bool,'invalid_managed_settings')
            integer(reply['next_index'],0,len(pages))
            require(reply['complete']==(reply['next_index']==len(pages)),'invalid_managed_settings')
            saved['next_index']=reply['next_index'];atomic_json(path,saved)
            if reply['complete']:
                self.details_probe_at=self.clock()+300;return 'synchronized'
        return 'uploading'

    def step(self):
        phase='idle';error=None;delay=5;received=0;settings_phase='not-attempted'
        try:
            from .policy import enabled
            if not enabled(self.root):phase='not-managed';delay=300
            else:
                status=self.client.status()
                if not status.get('configured'):phase='not-configured';delay=60
                elif status.get('pending_operation'):phase='enrollment-pending';delay=30
                elif not status.get('enrolled'):phase='not-enrolled';delay=60
                elif status.get('account_binding_matches') is False:phase='requires-review';error='local-state-refused';delay=300
                elif status['credential_expires_at']<=self.clock():phase='authority-refused';error='authority-refused';delay=300
                else:
                    store=self.open_state();self.migrate(store)
                    # Profile migration may have enabled directory participation.
                    with file_lease(self.client.lock_path):
                        profile=self.client.profile();credential=self.client.credential()
                        self.client.check_account(profile['account_id'])
                        require(credential is not None and self.client.pending(profile) is None,'network_pending_requires_resume')
                        require(all(credential[k]==store.binding[k] for k in store.binding),'host_identity_mismatch')
                    received=self.exchange(store,profile,credential);phase='connected'
                    # A large/failed optional detail upload must never starve
                    # room commands, acknowledgments or the independent agent.
                    try:settings_phase=self.sync_settings(store,profile,credential)
                    except NetworkError as exc:
                        settings_phase='hub-upgrade-required' if exc.code=='route_not_found' else 'retrying'
                    except (ConfigError,OSError,ValueError,TypeError,KeyError):settings_phase='requires-review'
            self.failures=0
        except LeaseBusy:
            # Startup/profile migration can briefly overlap other independent
            # agents. A held local lease is retryable, never corrupt authority.
            phase='migration-pending';error='policy-preparation-required';delay=2
        except NetworkError as exc:
            self.failures=min(7,self.failures+1)
            if exc.code=='route_not_found' and exc.status==404:phase='hub-upgrade-required';error='unsupported-capability';delay=60
            elif exc.code in ('host_revoked','credential_expired','credential_retired','credential_refused','scope_refused'):
                phase='authority-refused';error='authority-refused';delay=300
            elif exc.code in ('managed_root_policy_required','ratings_migration_required'):
                phase='migration-pending';error='policy-preparation-required';delay=30
            elif exc.status>=500 or exc.code in ('hub_deadline_exceeded','hub_transport_failed','hub_rate_limited'):
                phase='retrying';error='hub-unavailable';delay=min(60,5*2**(self.failures-1))
            else:phase='requires-review';error='local-state-refused';delay=60
        except (ConfigError,OSError,ValueError,TypeError,KeyError):
            phase='requires-review';error='local-state-refused';delay=60
        result={'format':1,'phase':phase,'error':error,'attempted_at':self.clock(),'retry_seconds':delay,
                'received_requests':received,'settings_phase':settings_phase,'provider_dispatch':False}
        atomic_json(self.path,result)
        return result

    def run(self,stop):
        os.setpriority(os.PRIO_PROCESS,0,max(17,os.getpriority(os.PRIO_PROCESS,0)))
        with file_lease(self.root/'state/managed-agent.lock'):
            while not stop.is_set():stop.wait(self.step()['retry_seconds'])


def start(client,stop):
    thread=threading.Thread(target=Agent(client).run,args=(stop,),name='managed-host-agent',daemon=True)
    thread.start();return thread


def diagnostics(root):
    """Fixed support fields only; no command/settings payload or private ID."""
    path=Path(root)/'state/managed-agent.json'
    if not os.path.lexists(path):return {'phase':'not-observed','settings_phase':'not-attempted'}
    value=read_json(path)
    expected={'format','phase','error','attempted_at','retry_seconds','received_requests','provider_dispatch'}
    require(type(value) is dict and set(value) in (expected,expected|{'settings_phase'}),'invalid_managed_agent_receipt')
    require(type(value['format']) is int and value['format']==1 and value['provider_dispatch'] is False,'invalid_managed_agent_receipt')
    require(value['phase'] in ('idle','not-managed','not-configured','enrollment-pending','not-enrolled','requires-review',
        'authority-refused','connected','hub-upgrade-required','migration-pending','retrying'),'invalid_managed_agent_receipt')
    require(value['error'] in (None,'unsupported-capability','authority-refused','policy-preparation-required',
            'hub-unavailable','local-state-refused'),'invalid_managed_agent_receipt')
    settings_phase=value.get('settings_phase','not-attempted')
    require(settings_phase in ('not-attempted','synchronized','uploading','hub-upgrade-required','retrying','requires-review'),
            'invalid_managed_agent_receipt')
    from ..network.contracts import number
    number(value['attempted_at']);number(value['retry_seconds'],2,300);integer(value['received_requests'],0,8)
    return {**{k:value[k] for k in ('phase','error','attempted_at','received_requests')},'settings_phase':settings_phase}
