"""Default-on reporting with durable operator opt-outs and isolated synchronization.

No provider adapters, room locks or provider credentials are used here. The
network service retains its OAuth namespace isolation.
"""
import hashlib
import os
import re
import shlex
import stat
from pathlib import Path
import threading
import time

from .layout import load, read_json, atomic_json, contained, file_lease
from .network.contracts import canonical, fields, require, number

CHOICES=('share_statistics','consume_statistics','operational_logs','public_chat')


def profile(root):
    root=Path(root);marker,_=load(root);path=contained(root,'config/reporting.json')
    if not os.path.lexists(path):return {'format':1,**{k:marker[k] for k in ('installation_id','generation')},**dict.fromkeys(CHOICES,True)}
    value=read_json(path);fields(value,('format','installation_id','generation',*CHOICES))
    require(type(value['format']) is int and value['format']==1,'invalid_reporting_profile')
    require(all(value[k]==marker[k] for k in ('installation_id','generation')),'foreign_reporting_profile')
    require(all(type(value[k]) is bool for k in CHOICES) and (value['operational_logs'] or not value['public_chat']),'invalid_reporting_profile')
    from .managed_host.policy import enabled
    if enabled(root):value={**value,**dict.fromkeys(CHOICES,True)}
    return value


def secrets(root, *, provider=False):
    """Read fixed authorized secret documents only; never traverse configured paths."""
    from .diagnostic_logs.redact import secrets_from_mapping
    names=['network.json','integrations.json']
    if provider:names+=['provider-osu-token.json','provider-operator-client.json','provider-oauth-pending.json',
                        'provider-oauth-candidate.json','osu-token.json','operator-client.json','oauth-pending.json','oauth-candidate.json','private-pilot.json']
    values=[]
    if provider:
        from .owner_integrations import environment_names,LEGACY_CREDENTIAL_NAMES
        values.extend(os.environ[n] for n in environment_names(root)|LEGACY_CREDENTIAL_NAMES if os.environ.get(n))
    integrations=contained(Path(root),'config/integrations.json')
    if integrations.exists():values.extend(secrets_from_mapping(read_json(integrations,maximum=1024*1024)))
    if provider:
        # The journal collector does not inherit the bot's credential environment.
        # Read only the fixed private integration file; never execute its values.
        envfile=contained(Path(root),'secrets/integrations.env')
        if os.path.lexists(envfile):
            fd=os.open(envfile,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'r') as stream:
                info=os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                        or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>65536):
                    raise ValueError('invalid private integration environment')
                for line in stream:
                    if not line.strip() or line.lstrip().startswith('#'):continue
                    name,sep,raw=line.partition('=')
                    if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name.strip()):
                        raise ValueError('invalid private integration assignment')
                    parsed=shlex.split(raw,comments=False,posix=True)
                    if len(parsed)>1:raise ValueError('ambiguous private integration assignment')
                    if parsed:values.append(parsed[0])
        _,config=load(root)
        room_paths=['config/gameplay.json','config/room-settings.json']
        room_paths += ['config/rooms/'+room['alias']+'.json' for room in config['rooms']]
        for relative in room_paths:
            path=contained(Path(root),relative)
            if os.path.lexists(path):values.extend(secrets_from_mapping(read_json(path,maximum=1024*1024)))
    for name in names:
        path=contained(Path(root),'secrets/'+name)
        if os.path.lexists(path):values.extend(secrets_from_mapping(read_json(path,maximum=65536)))
    from .managed_host.secrets import values as managed_passwords
    values.extend(managed_passwords(root))
    return tuple(values)


class CachedSecrets:
    """Reload credentials on metadata/environment changes, before each emission.

    No time-only cache window can expose a newly rotated credential. Keep a
    bounded history in memory to scrub previous credentials in delayed errors.
    The cache and its signature are never persisted, logged or uploaded.
    """
    def __init__(self,root):
        self.root=Path(root);self.signature=None;self.values=()

    def __call__(self):
        root=self.root
        paths=[root/'config/host.json',root/'config/integrations.json',root/'config/gameplay.json',root/'config/room-settings.json']
        paths += [root/'state'/('managed-host.sqlite3'+suffix) for suffix in ('','-wal','-shm','-journal')]
        paths += sorted((root/'config/rooms').glob('*.json'))[:50]
        paths += [root/'secrets'/name for name in ('network.json','integrations.json','integrations.env',
            'provider-osu-token.json','provider-operator-client.json','provider-oauth-pending.json',
            'provider-oauth-candidate.json','osu-token.json','operator-client.json',
            'oauth-pending.json','oauth-candidate.json','private-pilot.json')]
        stamps=[]
        for path in paths:
            try:
                info=path.lstat();stamp=(info.st_dev,info.st_ino,info.st_mtime_ns,info.st_ctime_ns,info.st_size)
            except FileNotFoundError:stamp=None
            stamps.append((str(path),stamp))
        signature=(tuple(stamps),tuple(sorted(os.environ.items())))
        if signature!=self.signature:
            fresh=secrets(root,provider=True)
            self.values=tuple(dict.fromkeys((*fresh,*self.values)))[:512]
            self.signature=signature
        return self.values


def logs(root):
    from .diagnostic_logs import LocalLogs, Source
    root=Path(root);marker,_=load(root)
    # These files are independently scrubbed at emission time. Never tail raw
    # room logs in the namespace that cannot read the protected OAuth secrets.
    sources=[Source('controller',root/'logs/reporting-controller.jsonl',structured=True,start_at_end=False,rotations=4)]
    for path in sorted((root/'logs').glob('reporting-room*.jsonl'))[:50]:
        alias=path.name[len('reporting-'):-len('.jsonl')]
        sources.append(Source(alias,path,room=alias,structured=True,start_at_end=False,rotations=4))
    choice=root/'config/reporting.json'
    since=(choice if choice.exists() else root/'.poolrotater-host.json').stat().st_mtime
    return LocalLogs(root/'state',installation_id=marker['installation_id'],generation=marker['generation'],
                     secret_provider=lambda:secrets(root),sources=tuple(sources),
                     full_stream=True,capture_start=since,quota_bytes=128*1024**2)


def configure(root,value):
    from .activity.collector import Collector
    fields(value,CHOICES);require(all(type(value[k]) is bool for k in CHOICES),'explicit_reporting_choices_required')
    require(value['operational_logs'] or not value['public_chat'],'invalid_reporting_profile')
    root=Path(root)
    from .managed_host.policy import require_enabled_settings
    require_enabled_settings(root,value)
    with file_lease(root/'state/reporting.lock'):
        old=profile(root);new={**old,**value}
        # First persist conservative disabled choices before purging queues.
        # An interrupted enable is retried idempotently by the background agent.
        atomic_json(root/'config/reporting.json',new)
        Collector(root).configure(share_statistics=new['share_statistics'])
        logs(root).configure(operational=new['operational_logs'],public_chat=new['public_chat'])
    return support(root)


def support(root):
    from .activity.collector import Collector
    from .activity.reader import Reader
    root=Path(root);p=profile(root)
    result={'ok':True,**{k:p[k] for k in CHOICES},'live_rooms_affected':False,'provider_dispatch':False,
        'log_scope':'all-poolrotater-rooms-and-services','minimum_log_level':'DEBUG',
        'private_room_logs_included':p['operational_logs']}
    if (root/'state/activity-outbox.sqlite3').exists():result['activity']=Collector(root).status()
    if (root/'state/activity-cache.sqlite3').exists():result['statistics_cache']=Reader(root).status()
    if (root/'state/diagnostic-logs.sqlite3').exists():result['logs']=logs(root).support()
    receipt=root/'state/reporting-agent.json'
    if receipt.exists():
        state=read_json(receipt)
        fields(state,('format','phase','at','live_rooms_affected','provider_dispatch','capture_errors'))
        require(type(state['format']) is int and state['format']==1 and state['live_rooms_affected'] is False
                and state['provider_dispatch'] is False,'invalid_reporting_receipt')
        number(state['at']);require(type(state['capture_errors']) is list and len(state['capture_errors'])<=5,'invalid_reporting_receipt')
        # Never copy arbitrary mutable payloads to support output.
        result['agent_phase']=state.get('phase') if state.get('phase') in ('disabled','synchronized','retrying','not-enrolled','consent-pending','policy-pending','partial') else 'local-review-required'
        result['last_attempt_at']=state.get('at')
        result['agent_observation_current']=0<=time.time()-state['at']<=120
        result['reporting_health']='stalled' if not result['agent_observation_current'] else (
            'blocked' if state['capture_errors'] else result['agent_phase'])
        result['capture_errors']=[e for e in state.get('capture_errors',[]) if e in ('activity-capture-unavailable','log-source-unavailable','activity-capacity-paused','activity-history-review-required','activity-privacy-review-required')]
    external=root/'state/reporting-external.json'
    if external.exists():
        state=read_json(external)
        fields(state,('format','phase','at','journal_records','worker_sources','worker_bytes','provider_dispatch'))
        require(state['format']==1 and state['phase'] in ('collected','partial','retrying')
            and state['provider_dispatch'] is False,'invalid_external_log_receipt')
        number(state['at'])
        require(all(type(state[k])is int and state[k]>=0 for k in ('journal_records','worker_sources','worker_bytes')),
            'invalid_external_log_receipt')
        result['external_logs']={k:state[k] for k in ('phase','at','journal_records','worker_sources','worker_bytes')}
    return result


class Reporter:
    def __init__(self,client,*,clock=time.time):
        self.client=client;self.root=Path(client.root);self.clock=clock;self.turn=0;self.backlog=False;self.stop=None

    def step(self):
        from .activity.collector import Collector
        from .activity.reader import Reader
        root=self.root
        # Initial mandatory migration configures these same stores under the
        # reporting lease. Do not let a continuously draining reporter starve
        # that migration (and therefore the independent administration agent).
        # Existing unmanaged installations retain their reporting behaviour.
        from .managed_host.policy import enabled,state
        if enabled(root) and state(root).snapshot()['policy_version']!=1:
            self.backlog=False
            return self._receipt('policy-pending')
        with file_lease(root/'state/reporting.lock'):
            p=profile(root);collector=Collector(root)
            consent=collector.configure(share_statistics=p['share_statistics'])
            local=logs(root);local.configure(operational=p['operational_logs'],public_chat=p['public_chat'])
            capture_errors=[];activity_ready=True
            if p['share_statistics']:
                try:
                    require(not any((root/'state').glob('reporting-visibility-invalid-*.json')),'activity_visibility_requires_review')
                    captured=collector.collect()
                    if captured.get('privacy_review_required'):capture_errors.append('activity-privacy-review-required')
                    elif captured.get('capture_paused'):capture_errors.append('activity-capacity-paused')
                except Exception:
                    # An unreadable privacy source must hold activity upload;
                    # independent diagnostic collection can still proceed.
                    activity_ready=False;capture_errors.append('activity-capture-unavailable')
                if activity_ready:
                    try:
                        from .activity.history import step as history_step
                        history_step(root)
                    except Exception:
                        # A refused historical review must not stop ordinary
                        # public telemetry, cache refresh or diagnostic uploads.
                        capture_errors.append('activity-history-review-required')
            if p['operational_logs']:
                for source in local.sources.values():
                    try:local.collect(source)
                    except Exception:
                        if 'log-source-unavailable' not in capture_errors:capture_errors.append('log-source-unavailable')
            with file_lease(self.client.lock_path):
                if not self.client.profile_path.exists():return self._receipt('not-enrolled')
                network=self.client.profile();self.client.check_account(network['account_id'])
                require(self.client.pending(network) is None,'network_pending_requires_resume')
                credential=self.client.credential()
                if credential is None:return self._receipt('not-enrolled')
                token=credential['credential'];host=credential['host_id']
            request=self.client.transport(network).request
            receipt_path=root/'state/reporting-consents.json'
            receipts=read_json(receipt_path) if receipt_path.exists() else {}
            choices={'activity':consent,'logs':{'format':1,'installation_id':p['installation_id'],
                     'generation':p['generation'],'operational':p['operational_logs'],'public_chat':p['public_chat']}}
            for kind,body in choices.items():
                digest=hashlib.sha256(canonical([host,body])).hexdigest()
                if receipts.get(kind)!=digest:
                    route='/v1/activity/consent' if kind=='activity' else '/v1/logs/consent'
                    reply=request('POST',route,token=token,body=body)
                    require(reply.get('host_id')==host,'reporting_consent_reply_mismatch')
                    if kind=='activity':require(all(reply.get(k)==body[k] for k in ('enabled','consent_id','consent_at')),'reporting_consent_reply_mismatch')
                    else:require(all(reply.get(k)==body[k] for k in ('operational','public_chat')),'reporting_consent_reply_mismatch')
                    receipts[kind]=digest;atomic_json(receipt_path,receipts)
                    return self._receipt('consent-pending')
            from .activity.replica import Replica
            from .network.transfer_policy import enabled as uncapped_transfers
            try:uncapped=uncapped_transfers(self.client,clock=self.clock,refresh=True)
            except Exception:uncapped=False
            if p['consume_statistics']:
                try:Replica(root,clock=self.clock).tick(self.client,stop=self.stop)
                except Exception:
                    # Existing uploads remain independent of replica availability.
                    pass
            self.turn=(self.turn+1)%3
            # One bounded transfer per tick; independent heartbeat scheduling is
            # never stalled by a whole reporting backlog.
            activity=collector.batch() if p['share_statistics'] and activity_ready else None
            chunk=local.batch() if p['operational_logs'] else None
            try:replica_ready=Replica(root,clock=self.clock).status()['phase'] not in ('cold','legacy')
            except Exception:replica_ready=True
            legacy_lookup=p['consume_statistics'] and not replica_ready
            self.backlog=False
            if self.turn==0 and legacy_lookup:Reader(root).refresh(self.client)
            elif activity is not None and (self.turn==1 or chunk is None):
                collector.acknowledge(request('POST','/v1/activity/events',token=token,body=activity));self.backlog=uncapped
            elif chunk is not None:
                local.acknowledge(request('POST','/v1/logs/chunks',token=token,body=chunk));self.backlog=uncapped
            elif legacy_lookup:Reader(root).refresh(self.client)
            return self._receipt('partial' if capture_errors else 'synchronized' if any(p[k] for k in CHOICES) else 'disabled',capture_errors=capture_errors)

    def _receipt(self,phase,*,capture_errors=()):
        value={'format':1,'phase':phase,'at':self.clock(),'live_rooms_affected':False,'provider_dispatch':False,'capture_errors':list(capture_errors)}
        atomic_json(self.root/'state/reporting-agent.json',value);return {'ok':True,**value}

    def run(self,stop):
        failures=0;self.stop=stop
        while not stop.is_set():
            try:self.step();failures=0
            except Exception:
                # Expected transport, opt-in storage and reporting failures are
                # isolated from native workers. No provider mutations are here.
                failures+=1
                try:self._receipt('retrying')
                except Exception:pass
            # No inter-batch sleep while an authenticated backlog is draining.
            # The dedicated service cgroup supplies the50%-of-one-core CPU budget.
            stop.wait(min(120,2**min(failures,6)) if failures else 0 if self.backlog else 2)


def start(client,stop):
    thread=threading.Thread(target=Reporter(client).run,args=(stop,),name='reporting-agent',daemon=True)
    thread.start();return thread
