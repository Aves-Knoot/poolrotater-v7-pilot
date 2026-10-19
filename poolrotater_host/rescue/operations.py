"""Independent, pinned recovery coordinator; only exact approved native jobs."""
import os
import json
from pathlib import Path
import time

from ..layout import atomic_json, file_lease
from ..network.contracts import fingerprint, require
from ..provisioning import _control, protected
from .contracts import observation

NAME = 'recovery/configuration.json'


def target_for(instance, pinned):
    from ..pilot_installation import paths
    target = paths(instance)
    require(pinned['prefix'] == str(target['prefix']) and pinned['host_root'] == str(target['root']),
            'rescue_binding_changed')
    # Marker identity must remain the same even if registration is interrupted.
    marker = _control(target['root'] / '.poolrotater-host.json', pinned['service_uid'], private=True)
    require(all(marker.get(k) == pinned[k] for k in ('installation_id', 'generation')),
            'rescue_binding_changed')
    return target


def policy_allowed(directory, pinned, *, install=False):
    from ..updates.agent import policy
    config = _control(protected(directory / 'configuration.json'), private=True)
    require(config['format'] == 1 and config['record'] == pinned, 'rescue_binding_changed')
    require(config['enabled'] is True, 'rescue_policy_disabled')
    choices = _control(protected(directory.parent / 'updates/cache/policy.json'), private=True)
    bound = config['binding']
    policy(choices, bound)
    require(choices['report_to_hub'] is True, 'rescue_policy_disabled')
    if install:
        require(config['allow_recovery'] is True and not choices['paused']
                and choices['automatic_install'] and choices['controller_updates'],
                'rescue_policy_disabled')
    return config


class Backend:
    def __init__(self, instance, pinned):
        self.instance = instance
        self.pinned = pinned
        self.target = target_for(instance, pinned)
        self.directory = self.target['prefix'] / 'recovery'

    def job(self):
        return _control(self.target['prefix'] / 'controller-update.json', optional=True)

    def observe(self):
        from ..pilot_operations import _service_state
        from ..update_lifecycle import STAGES
        from .contracts import FAILURES
        value = {'phase': 'absent', 'operation_id': None, 'candidate_sha256': None,
                 'job_sha256': None, 'failure_stage': None, 'failure_code': None,
                 'service_state': 'unavailable', 'startup_attempted': False}
        try:
            job = self.job()
            if job:
                value.update(phase=job['phase'], operation_id=job['id'], job_sha256=fingerprint(job),
                             candidate_sha256=job['after']['bundle_sha256'],
                             failure_stage=job.get('failure_stage') if job.get('failure_stage') in STAGES else None,
                             failure_code=job.get('failure_code') if job.get('failure_code') in FAILURES else None,
                             startup_attempted=job.get('startup_attempted_at') is not None)
            state = _service_state(self.instance)
            value['service_state'] = state['ActiveState']
            if (state['ActiveState'] == 'failed' and not state['MainPID']
                    and (job is None or job.get('phase') in ('complete','cancelled'))):
                current, journal = self.failed_service_evidence()
                value.update(phase='service_failed',operation_id=None,
                    candidate_sha256=current['bundle_sha256'],
                    job_sha256=fingerprint({'registration':current,'journal':journal}),
                    failure_stage=None,failure_code=None,startup_attempted=False)
            return observation(value)
        except Exception:
            return observation({**value, 'phase': 'unavailable', 'operation_id': None,
                                'candidate_sha256': None, 'job_sha256': None,
                                'service_state': 'unavailable'})

    def collect(self):
        # Existing redaction and sharing controls apply. Read fixed units as
        # root, then upload only the sanitized local spool as the service UID.
        from ..reporting_external import collect
        from ..ratings.operations import _identity
        from ..reporting import profile as reporting_profile, logs, CachedSecrets
        from ..network.client import Client
        try:
            collect(self.instance, binding=(self.target, {}, self.pinned))
        except Exception:
            pass  # A missing/vacuumed journal must not hide structured evidence.
        frame = self.inspect()
        with _identity(self.pinned['service_uid']):
            root = self.target['root']
            with file_lease(root / 'state/reporting.lock'):
                choice = reporting_profile(root)
                if not choice['operational_logs']:
                    return {'phase': 'disabled'}
                local = logs(root)
                local.secret_provider = CachedSecrets(root)
                local.configure(operational=True, public_chat=choice['public_chat'])
                # Closed diagnostic fields only. No raw job, configuration,
                # provider reply or credential document is ever serialized.
                local.append('independent recovery: '+json.dumps(frame,sort_keys=True),source='recovery')
                client = Client(root)
                with file_lease(client.lock_path):
                    profile = client.profile()
                    client.check_account(profile['account_id'])
                    require(client.pending(profile) is None, 'rescue_binding_changed')
                    credential = client.credential()
                    require(credential is not None, 'rescue_binding_changed')
                    transport = client.transport(profile)
                body = {'format': 1, **{k: choice[k] for k in ('installation_id', 'generation')},
                        'operational': True, 'public_chat': choice['public_chat']}
                reply = transport.request('POST', '/v1/logs/consent', body=body, token=credential['credential'])
                require(reply.get('host_id') == credential['host_id'] and
                        all(reply.get(k) == body[k] for k in ('operational', 'public_chat')),
                        'rescue_binding_changed')
                chunk = local.batch()
                if chunk is not None:
                    local.acknowledge(transport.request('POST', '/v1/logs/chunks',
                                                       body=chunk, token=credential['credential']))
                return {'phase': 'synchronized'}

    def inspect(self):
        """Report useful transition evidence without exposing config contents."""
        frame = {'observation':self.observe(),'registration':'unavailable',
                 'upgrade':'unavailable','configuration':'unavailable'}
        try:
            job = self.job()
            if job is None:
                frame.update(registration='current',upgrade='absent',configuration='not-applicable')
                return frame
            for record in (job['before'],job['after']):
                require(all(record.get(k)==self.pinned[k] for k in (
                    'installation_id','generation','host_root','prefix','service_uid'))
                    and Path(record['release']).parent == self.target['prefix']/'releases',
                    'rescue_binding_changed')
            registered = _control(self.target['prefix']/'installations'/(self.pinned['installation_id']+'.json'))
            frame['registration'] = 'before' if registered==job['before'] else 'after' if registered==job['after'] else 'other'
            upgrade = _control(self.target['prefix']/'pilot-upgrade.json',optional=True)
            frame['upgrade'] = ('absent' if upgrade is None else
                upgrade['phase'] if upgrade.get('before')==job['before'] and upgrade.get('after')==job['after']
                and upgrade.get('phase') in ('prepared','committed','aborted') else 'other')
            from ..controller_update import _config_hashes
            if _config_hashes(self.target['root']) == job['configuration']:
                frame['configuration']='unchanged'
            else:
                from ..updates.recovery import _configuration_matches
                before = _control(protected(Path(job['before']['release'])/'release.json'))['version']
                after = _control(protected(Path(job['after']['release'])/'release.json'))['version']
                frame['configuration'] = ('version-stamp-only' if
                    _configuration_matches(self.target,job,before_version=before,after_version=after) else 'changed')
        except Exception:
            pass
        return frame

    def perform(self, command):
        with file_lease(self.directory / 'policy.lock'):
            policy_allowed(self.directory, self.pinned, install=command['action'] != 'inspect')
            if command['action'] == 'inspect':
                self.collect()
                return
            if command['action'] == 'start-failed-service':
                self.start_failed_service(command)
                return
            require(command['action'] == 'complete-update', 'rescue_state_refused')
            # All local authority is rechecked by the native recovery, while the
            # rescue lease makes its bounded attempt count durable across crashes.
            with file_lease(self.directory / 'completion.lock'):
                job = self.job()
                require(job and fingerprint(job) == command['job_sha256']
                        and job['id'] == command['operation_id']
                        and job['after']['bundle_sha256'] == command['candidate_sha256'],
                        'rescue_request_changed')
                require(job.get('release_approval_required') is True
                        and type(job.get('commit_admitted_at')) in (int, float)
                        and job['commit_admitted_at'] <= time.time()
                        and job['phase'] in ('held', 'committing', 'recovering', 'retrying'),
                        'rescue_update_requires_review')
                for record in (job['before'], job['after']):
                    require(all(record.get(k) == self.pinned[k] for k in (
                        'installation_id', 'generation', 'host_root', 'prefix', 'service_uid', 'execution')),
                        'rescue_binding_changed')
                from ..pilot_operations import _service_state
                state = _service_state(self.instance)
                if not state['MainPID']:
                    self.require_clean_closed(job)
                # No automatic second startup if the first attempt could have
                # created an untracked room. A clean closed journal and normal
                # native fresh provider/owner checks are both required.
                path = self.directory / 'attempts.json'
                attempts = _control(path, optional=True, private=True) or {}
                previous = attempts.get(job['id'], {'count': 0, 'at': 0})
                require(type(previous['count']) is int and 0 <= previous['count'] < 3,
                        'rescue_attempt_limit')
                require(time.time() - previous['at'] >= 60, 'rescue_attempt_limit')
                attempts[job['id']] = {'count': previous['count'] + 1, 'at': time.time()}
                # Keep recent bounded receipts, never overwrite this operation's
                # limit to authorize an endless retry cycle.
                require(len(attempts) <= 128, 'rescue_attempt_limit')
                atomic_json(path, attempts)
                from ..updates.executor import controller
                controller(self.instance, self.target, job, decision='complete')
                result = self.job()
                require(result['id'] == job['id'] and result.get('phase') in ('complete', 'waiting_empty'),
                        'rescue_update_requires_review')

    def failed_service_evidence(self):
        """Settled installed runtime plus local ownership; never a status hint."""
        from ..pilot_installation import administrative_binding
        target, _, current = administrative_binding(self.instance)
        require(target['prefix'] == self.target['prefix'] and all(current[k] == self.pinned[k]
                for k in ('installation_id','generation','service_uid','host_root','prefix','execution')),
                'rescue_binding_changed')
        job = self.job()
        require(job is None or job.get('phase') in ('complete','cancelled'), 'rescue_update_requires_review')
        # administrative_binding also validates the settled runtime. Native
        # start performs fresh provider/identity and owned-room checks later.
        journal = _control(self.target['root']/'state/rolling/ownership.json',
                           current['service_uid'],private=True)
        return current, journal

    def start_failed_service(self, command):
        """Maintainer-requested recovery of an exited, positively closed service.

        No running service can be stopped/replaced by this action. Explicit
        local stops fence it through policy and an inactive unit is refused.
        """
        with file_lease(self.directory/'completion.lock'), \
             file_lease(self.target['prefix']/'deployment-admission.lock'), \
             file_lease(self.target['prefix']/'controller-update-admin.lock'):
            from ..pilot_operations import _service_state
            state = _service_state(self.instance)
            require(state['ActiveState'] == 'failed' and state['MainPID'] == 0,
                    'rescue_update_requires_review')
            current, journal = self.failed_service_evidence()
            require(command['operation_id'] is None and command['candidate_sha256'] == current['bundle_sha256']
                    and command['job_sha256'] == fingerprint({'registration':current,'journal':journal}),
                    'rescue_request_changed')
            from ..ratings.operations import _identity
            from ..layout import load
            with _identity(current['service_uid']):
                _, host = load(self.target['root'])
            self.require_clean_closed({'rooms':[r['alias'] for r in host['rooms']]})
            path=self.directory/'failed-start-attempts.json'
            attempts=_control(path,private=True,optional=True) or {}
            key=command['job_sha256']; previous=attempts.get(key,{'count':0,'at':0})
            require(type(previous['count']) is int and 0<=previous['count']<3
                    and time.time()-previous['at']>=60 and len(attempts)<=128,'rescue_attempt_limit')
            attempts[key]={'count':previous['count']+1,'at':time.time()};atomic_json(path,attempts)
            from ..update_lifecycle import wait_stopped
            from ..network import operations as network
            network_state=network._state(self.instance)
            network_active=bool(network_state['MainPID'])
            # Use the registered, integrity-checked native admin entry rather
            # than the rescue pin's older service implementation.
            import subprocess
            from ..runtime_lifecycle import validate_runtime
            runtime=validate_runtime(current,self.target['prefix'])
            try:
                if network_active:
                    network.unit(self.instance,'stop')
                wait_stopped(self.target['root'],current['service_uid'])
                result=subprocess.run(['/usr/bin/python3.12','-I','-S','-B',
                    str(runtime['release']/'poolrotater_host/pilot_admin_entry.py'),
                    '--instance',self.instance,'start'],cwd='/tmp',stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180,
                    env={'PATH':'/usr/sbin:/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
                require(result.returncode == 0,'rescue_update_requires_review')
            finally:
                if network_active:
                    network.unit(self.instance,'start')
    def require_clean_closed(self, job):
        from ..reconciliation import _journal
        from ..ratings.operations import _identity
        from ..layout import load
        with _identity(self.pinned['service_uid']):
            _, host = load(self.target['root'])
        aliases = {r['alias'] for r in host['rooms']}
        require(aliases == set(job['rooms']), 'rescue_ownership_requires_review')
        journal = _journal(_control(self.target['root'] / 'state/rolling/ownership.json',
                            self.pinned['service_uid'], private=True), aliases)
        require(set(journal['rooms']) == aliases, 'rescue_ownership_requires_review')
        for row in journal['rooms'].values():
            require(row.get('phase') in ('stopped', 'parked') and row.get('operation') == 'closed'
                    and row.get('ownership_unknown') is False and row.get('room_id') is None
                    and row.get('channel_id') is None and row.get('pid') is None
                    and not row.get('creation_candidate') and not row.get('target') and not row.get('error'),
                    'rescue_ownership_requires_review')

    def automatic(self):
        """Bounded transient continuation; a generic held guard stays visible."""
        job = self.job()
        if not job or job.get('phase') not in ('committing', 'recovering', 'retrying', 'held'):
            return 'idle'
        if (job.get('failure_code') not in (None, 'update_interrupted_commit', 'update_cleanup_timeout',
                                          'update_local_io_failed', 'update_recovery_fresh_workers_required')
                or type(job.get('commit_admitted_at')) not in (int, float)
                or not 0 <= time.time() - job['commit_admitted_at'] <= 1800):
            return 'requires-review'
        self.perform({'action': 'complete-update', 'operation_id': job['id'],
                      'candidate_sha256': job['after']['bundle_sha256'], 'job_sha256': fingerprint(job)})
        return 'continued'


def tick(instance, pinned):
    require(os.geteuid() == 0, 'rescue_administrator_required')
    backend = Backend(instance, pinned)
    config = policy_allowed(backend.directory, pinned)
    from ..ratings.operations import _identity
    from ..network.client import Client
    from .agent import Agent

    def transport(value):
        # No registered-runtime invocation: the pinned coordinator stays usable
        # when the current release's entry point or network process has failed.
        with _identity(pinned['service_uid']):
            client = Client(backend.target['root'])
            with file_lease(client.lock_path):
                profile = client.profile()
                client.check_account(profile['account_id'])
                require(client.pending(profile) is None, 'rescue_binding_changed')
                credential = client.credential()
                require(credential is not None and credential['host_id'] == config['binding']['host_id'],
                        'rescue_binding_changed')
                return client.transport(profile).request('POST', '/v1/hosts/self/recovery',
                    body=value, token=credential['credential'])
    # Collection failure cannot suppress the recovery exchange or its receipt.
    try:
        backend.collect()
    except Exception:
        pass
    try:
        backend.automatic()
    except Exception:
        pass  # Report the retained native failure; never suppress the channel.
    return Agent(backend.directory, config['binding'], backend, transport).step()


def pause(instance):
    from ..pilot_installation import paths
    directory = paths(instance)['prefix'] / 'recovery'
    path = directory / 'configuration.json'
    if not os.path.lexists(path):
        return
    with file_lease(directory / 'policy.lock'):
        value = _control(protected(path), private=True)
        atomic_json(path, {**value, 'allow_recovery': False})


def install(instance, record):
    """Explicit bootstrap grant, pinned independently of current registration.

    This installs/starts only recovery polling. It neither starts rooms nor
    changes update/network policy. Existing disabled recovery stays disabled.
    """
    from ..pilot_operations import UNIT_BASE, _systemctl
    from ..runtime_lifecycle import validate_runtime
    from ..catalog_publication import _write
    from ..ratings.operations import _identity
    from ..network.client import Client
    require(os.geteuid() == 0, 'rescue_administrator_required')
    target = target_for(instance, record)
    runtime = validate_runtime(record, target['prefix'])
    with _identity(record['service_uid']):
        client = Client(target['root'])
        profile = client.profile()
        client.check_account(profile['account_id'])
        credential = client.credential()
        require(credential is not None and client.pending(profile) is None, 'rescue_binding_changed')
    bound = {**{k: record[k] for k in ('installation_id', 'generation')}, 'host_id': credential['host_id']}
    directory = target['prefix'] / 'recovery'
    directory.mkdir(mode=0o700, exist_ok=True)
    protected(directory)
    with file_lease(directory / 'policy.lock'):
        path = directory / 'configuration.json'
        existing = _control(path, optional=True, private=True)
        if existing is not None:
            require(existing['binding'] == bound, 'rescue_binding_changed')
            # Never replace its code/policy during a queued transition or
            # mistake a new runtime for authority to re-enable an opt-out.
            require(existing['record'] == record, 'rescue_pin_replacement_requires_review')
        else:
            atomic_json(path, {'format': 1, 'binding': bound, 'record': record,
                               'enabled': True, 'allow_recovery': True})
        policy_allowed(directory, record)
        source = protected(runtime['release'] / 'poolrotater_host/recovery_bootstrap.py')
        launcher = target['prefix'] / 'recovery-bootstrap.py'
        if os.path.lexists(launcher):
            require(protected(launcher).read_bytes() == source.read_bytes(), 'rescue_launcher_changed')
        else:
            _write(launcher, source.read_bytes(), mode=0o644)
        name = 'poolrotater-v7-recovery-' + instance
        service = f'''[Unit]
Description=PoolRotater independent recovery ({instance})
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3.12 -I -S -B {launcher} --instance {instance}
WorkingDirectory=/tmp
Nice=17
Slice=poolrotater-v7-transfer.slice
CPUQuota=50%
UMask=0077
MemoryMax=512M
MemorySwapMax=128M
TasksMax=32
TimeoutStartSec=1200
TimeoutStopSec=30
KillMode=control-group
SendSIGKILL=yes
LimitCORE=0
Environment=LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2
'''
        timer = f'''[Unit]
Description=PoolRotater independent recovery polling ({instance})

[Timer]
OnBootSec=90
OnUnitInactiveSec=30
RandomizedDelaySec=5
Unit={name}.service

[Install]
WantedBy=timers.target
'''
        from ..network.resources import ensure
        ensure(UNIT_BASE)
        for suffix, text in (('.service', service), ('.timer', timer)):
            unit = UNIT_BASE / (name + suffix)
            if os.path.lexists(unit):
                require(protected(unit).read_text() == text, 'rescue_unit_changed')
            else:
                _write(unit, text.encode(), mode=0o644)
        _systemctl('daemon-reload')
        _systemctl('enable', '--now', name + '.timer')
    return {'ok': True, 'recovery_installed': True, 'rooms_started': False,
            'operator_state_preserved': True}


def ensure(instance, record):
    """Default for enrolled auto-updating hosts; never re-enable an opt-out."""
    prefix = Path(record['prefix'])
    if os.path.lexists(prefix / NAME):
        return {'existing_recovery_preserved': True}
    path = prefix / 'updates/cache/policy.json'
    if not os.path.lexists(path):
        return {'recovery_installed': False}
    choices = _control(protected(path), private=True)
    if not (choices.get('report_to_hub') is True and choices.get('automatic_install') is True
            and choices.get('controller_updates') is True and choices.get('paused') is False):
        return {'recovery_installed': False}
    return install(instance, record)
