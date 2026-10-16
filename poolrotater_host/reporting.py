"""Explicit reporting consent and isolated background synchronization.

No provider adapters, room locks or provider credentials are used here. The
network service retains its OAuth namespace isolation.
"""
import hashlib
import os
from pathlib import Path
import threading
import time

from .layout import load, read_json, atomic_json, contained, file_lease
from .network.contracts import canonical, fields, require, number

CHOICES=('share_statistics','consume_statistics','operational_logs','public_chat')


def profile(root):
    root=Path(root);marker,_=load(root);path=contained(root,'config/reporting.json')
    if not os.path.lexists(path):return {'format':1,**{k:marker[k] for k in ('installation_id','generation')},**dict.fromkeys(CHOICES,False)}
    value=read_json(path);fields(value,('format','installation_id','generation',*CHOICES))
    require(type(value['format']) is int and value['format']==1,'invalid_reporting_profile')
    require(all(value[k]==marker[k] for k in ('installation_id','generation')),'foreign_reporting_profile')
    require(all(type(value[k]) is bool for k in CHOICES) and (value['operational_logs'] or not value['public_chat']),'invalid_reporting_profile')
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
    for name in names:
        path=contained(Path(root),'secrets/'+name)
        if os.path.lexists(path):values.extend(secrets_from_mapping(read_json(path,maximum=65536)))
    return tuple(values)


def logs(root):
    from .diagnostic_logs import LocalLogs, Source
    root=Path(root);marker,_=load(root)
    # These files are independently scrubbed at emission time. Never tail raw
    # room logs in the namespace that cannot read the protected OAuth secrets.
    sources=[Source('controller',root/'logs/reporting-controller.jsonl',structured=True,start_at_end=False)]
    for path in sorted((root/'logs').glob('reporting-room*.jsonl'))[:16]:
        alias=path.name[len('reporting-'):-len('.jsonl')]
        sources.append(Source(alias,path,room=alias,structured=True,start_at_end=False))
    return LocalLogs(root/'state',installation_id=marker['installation_id'],generation=marker['generation'],
                     secret_provider=lambda:secrets(root),sources=tuple(sources))


def configure(root,value):
    from .activity.collector import Collector
    fields(value,CHOICES);require(all(type(value[k]) is bool for k in CHOICES),'explicit_reporting_choices_required')
    require(value['operational_logs'] or not value['public_chat'],'invalid_reporting_profile')
    root=Path(root)
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
    result={'ok':True,**{k:p[k] for k in CHOICES},'live_rooms_affected':False,'provider_dispatch':False}
    if (root/'state/activity-outbox.sqlite3').exists():result['activity']=Collector(root).status()
    if (root/'state/activity-cache.sqlite3').exists():result['statistics_cache']=Reader(root).status()
    if (root/'state/diagnostic-logs.sqlite3').exists():result['logs']=logs(root).support()
    receipt=root/'state/reporting-agent.json'
    if receipt.exists():
        state=read_json(receipt)
        fields(state,('format','phase','at','live_rooms_affected','provider_dispatch','capture_errors'))
        require(type(state['format']) is int and state['format']==1 and state['live_rooms_affected'] is False
                and state['provider_dispatch'] is False,'invalid_reporting_receipt')
        number(state['at']);require(type(state['capture_errors']) is list and len(state['capture_errors'])<=3,'invalid_reporting_receipt')
        # Never copy arbitrary mutable payloads to support output.
        result['agent_phase']=state.get('phase') if state.get('phase') in ('disabled','synchronized','retrying','not-enrolled','consent-pending','partial') else 'local-review-required'
        result['last_attempt_at']=state.get('at')
        result['capture_errors']=[e for e in state.get('capture_errors',[]) if e in ('activity-capture-unavailable','log-source-unavailable','activity-capacity-paused')]
    return result


class Reporter:
    def __init__(self,client,*,clock=time.time):
        self.client=client;self.root=Path(client.root);self.clock=clock;self.turn=0

    def step(self):
        from .activity.collector import Collector
        from .activity.reader import Reader
        root=self.root
        with file_lease(root/'state/reporting.lock'):
            p=profile(root);collector=Collector(root)
            consent=collector.configure(share_statistics=p['share_statistics'])
            local=logs(root);local.configure(operational=p['operational_logs'],public_chat=p['public_chat'])
            capture_errors=[];activity_ready=True
            if p['share_statistics']:
                try:
                    require(not any((root/'state').glob('reporting-visibility-invalid-*.json')),'activity_visibility_requires_review')
                    captured=collector.collect()
                    if captured.get('capture_paused'):capture_errors.append('activity-capacity-paused')
                except Exception:
                    # An unreadable privacy source must hold activity upload;
                    # independent diagnostic collection can still proceed.
                    activity_ready=False;capture_errors.append('activity-capture-unavailable')
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
            self.turn=(self.turn+1)%3
            # One bounded transfer per tick; independent heartbeat scheduling is
            # never stalled by a whole reporting backlog.
            activity=collector.batch() if p['share_statistics'] and activity_ready else None
            chunk=local.batch() if p['operational_logs'] else None
            if self.turn==0 and p['consume_statistics']:Reader(root).refresh(self.client)
            elif activity is not None and (self.turn==1 or chunk is None):
                collector.acknowledge(request('POST','/v1/activity/events',token=token,body=activity))
            elif chunk is not None:local.acknowledge(request('POST','/v1/logs/chunks',token=token,body=chunk))
            elif p['consume_statistics']:Reader(root).refresh(self.client)
            return self._receipt('partial' if capture_errors else 'synchronized' if any(p[k] for k in CHOICES) else 'disabled',capture_errors=capture_errors)

    def _receipt(self,phase,*,capture_errors=()):
        value={'format':1,'phase':phase,'at':self.clock(),'live_rooms_affected':False,'provider_dispatch':False,'capture_errors':list(capture_errors)}
        atomic_json(self.root/'state/reporting-agent.json',value);return {'ok':True,**value}

    def run(self,stop):
        failures=0
        while not stop.is_set():
            try:self.step();failures=0
            except Exception:
                # Expected transport, opt-in storage and reporting failures are
                # isolated from native workers. No provider mutations are here.
                failures+=1
                try:self._receipt('retrying')
                except Exception:pass
            stop.wait(min(120,2**min(failures,6)) if failures else 2)


def start(client,stop):
    thread=threading.Thread(target=Reporter(client).run,args=(stop,),name='reporting-agent',daemon=True)
    thread.start();return thread
