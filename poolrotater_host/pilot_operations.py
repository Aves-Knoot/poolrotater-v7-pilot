"""Explicit protected pilot operations, never ambient account discovery.

Administrative commands address only the dedicated instance. A service unit is
installed disabled/stopped. Start/authorization retain the release fence.
"""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

from .config import ConfigError
from .pilot_installation import administrative_binding, paths, _stopped
from .provisioning import protected, _control

ENV = {'PATH': '/usr/sbin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
       'PYTHONDONTWRITEBYTECODE': '1', 'MALLOC_ARENA_MAX': '2'}
UNIT_BASE = Path('/etc/systemd/system')


def unit_text(instance):
    return _unit_text(instance, qualification=False)


def _unit_text(instance, *, qualification):
    target = paths(instance)
    entry = '--provider-room-fixture service' if qualification else '--provider-service'
    # The reserved owner crossover preserves the inventoried v6 scheduling and
    # resource settings. Generic operators keep the bounded pilot defaults;
    # qualification always uses the development limits and nice priority.
    resources = ('Nice=0\nMemoryMax=infinity\nMemorySwapMax=infinity\nTasksMax=256'
                 if instance == 'owner' and not qualification else
                 'Nice=15\nMemoryMax=1G\nMemorySwapMax=512M\nTasksMax=128')
    isolation = ('PrivateNetwork=yes\nEnvironment=POOLROTATER_OFFLINE_SANDBOX=1 '
                 'POOLROTATER_PROVISIONING_SANDBOX=1\n') if qualification else ''
    return f'''[Unit]
Description=PoolRotater v7 isolated pilot ({instance})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={target['user']}
Group={target['user']}
WorkingDirectory={target['root']}
ExecStart=/usr/bin/python3.12 -I -S {target['prefix']}/bootstrap.py --root {target['root']} {entry}
{isolation}Restart=no
KillMode=mixed
KillSignal=SIGTERM
SendSIGKILL=yes
TimeoutStopSec=90
UMask=0077
{resources}
LimitCORE=0
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={target['root']}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
Environment=LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2
EnvironmentFile=-{target['root']}/secrets/integrations.env

[Install]
WantedBy=multi-user.target
'''


def _systemctl(*arguments, timeout=30):
    try:
        result = subprocess.run(['/usr/bin/systemctl', *arguments], env=ENV, cwd='/tmp',
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ConfigError('pilot service operation timed out; inspect status without retrying startup') from None
    if result.returncode:
        raise ConfigError('pilot service operation refused; inspect protected status')
    return result.stdout


def _unit(instance, *, optional=False, expected_text=None):
    target = paths(instance); path = UNIT_BASE / target['unit']
    if optional and not os.path.lexists(path): return None
    protected(path)
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 16384
            or path.read_bytes() != (unit_text(instance) if expected_text is None else expected_text).encode()):
        raise ConfigError('pilot service unit differs from the reviewed instance unit')
    return path


def _service_state(instance):
    target = paths(instance)
    raw = _systemctl('show', target['unit'], '--property=LoadState,ActiveState,SubState,UnitFileState,MainPID')
    value = dict(line.split('=', 1) for line in raw.decode('ascii').splitlines())
    if set(value) != {'LoadState', 'ActiveState', 'SubState', 'UnitFileState', 'MainPID'}:
        raise ConfigError('pilot service state is incomplete')
    try: pid = int(value['MainPID'])
    except ValueError: raise ConfigError('invalid pilot service PID') from None
    if pid < 0: raise ConfigError('invalid pilot service PID')
    return {**value, 'MainPID': pid}


def install_unit(instance):
    target, _, record = administrative_binding(instance)
    return _install_unit(instance, target, record, unit_text(instance))


def _install_unit(instance, target, record, text):
    with _stopped(target['root'], record['service_uid']):
        protected(UNIT_BASE)
        path = _unit(instance, optional=True, expected_text=text)
        state = _service_state(instance)
        if state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed'):
            raise ConfigError('pilot unit installation requires a stopped service')
        if state['UnitFileState'] not in ('', 'disabled', 'not-found'):
            raise ConfigError('pilot unit must remain disabled during preparation')
        if path is None:
            if state['LoadState'] != 'not-found':
                raise ConfigError('unrecognized loaded pilot unit cannot be adopted')
            path = UNIT_BASE / target['unit']
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=UNIT_BASE, prefix='.prv7-unit-', delete=False) as f:
                    temporary = Path(f.name); f.write(text.encode())
                    f.flush(); os.fchmod(f.fileno(), 0o644); os.fsync(f.fileno())
                # Atomic publication refuses existing files including links.
                os.link(temporary, path)
            finally:
                if temporary is not None: temporary.unlink(missing_ok=True)
            fd = os.open(UNIT_BASE, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        _unit(instance, expected_text=text)
        _systemctl('daemon-reload')
        state = _service_state(instance)
        if state['ActiveState'] != 'inactive' or state['MainPID'] or state['UnitFileState'] != 'disabled':
            raise ConfigError('installed pilot unit did not remain disabled/stopped')
    return {'ok': True, 'service_installed': True, 'service_started': False,
            'service_enabled': False, 'live_ready': False}


def _service_command(target, record, arguments, *, input_data=None, timeout=30, notice=None):
    """Both protected bootstrap stages run as the actual service UID, never root."""
    uid = record['service_uid']
    def identity():
        os.setgroups([]); os.setgid(uid); os.setuid(uid)
    command = ['/usr/bin/python3.12', '-I', '-S', str(target['prefix'] / 'bootstrap.py'),
               '--root', str(target['root']), *arguments]
    if notice is not None:
        if input_data is not None: raise ConfigError('authorization console does not accept credentials')
        from .pilot_interaction import run
        return run(command, env=ENV, identity=identity, notice=notice, timeout=timeout)
    try:
        result = subprocess.run(command, input=input_data, env=ENV, cwd='/tmp',
                                capture_output=True, timeout=timeout, preexec_fn=identity)
    except subprocess.TimeoutExpired:
        raise ConfigError('pilot operator operation timed out; inspect pending authority before retry') from None
    if result.returncode:
        # The bootstrap never exposes arbitrary exception text. Preserve no
        # provider response or secret/URL in root diagnostics on failures.
        raise ConfigError('protected pilot operator operation refused')
    try:
        value = json.loads(result.stdout)
        if type(value) is not dict: raise ValueError
    except (ValueError, UnicodeError):
        raise ConfigError('protected pilot operator result is invalid') from None
    return value


def check(instance):
    target, _, record = administrative_binding(instance)
    result = _service_command(target, record, ['--pilot-check'])
    return {'ok': result.get('ok') is True, 'version': result.get('version'),
            'execution': record['execution'], 'protected_runtime_checked': True,
            'live_ready': False, 'provider_dispatch_enabled': record['execution'] == 'pilot-live'}


def code_proof(instance):
    """Public code evidence only; no auth/account/ownership construction."""
    target, _, record = administrative_binding(instance)
    from .runtime_lifecycle import validate_runtime
    current = validate_runtime(record, target['prefix'])
    files = current['manifest']['files']
    from .runtime_package import json_bytes
    proof = {'format': 1, 'kind': 'protected-v7-code-evidence', 'ok': True,
             'version': current['manifest']['version'], 'execution': record['execution'],
             'runtime_sha256': record['bundle_sha256'], 'environment_id': record['environment_id'],
             'source_files_sha256': hashlib.sha256(json_bytes(files)).hexdigest(),
             'source_files': files, 'protected_runtime_checked': True,
             'receipt_scope': 'registered_code_and_environment_only',
             'operational_acceptance_verified': False, 'provider_dispatch': False, 'live_ready': False}
    # A concurrent administrative switch must not turn old observations into
    # current source authority. Read-only diagnostics never acquire owner lease.
    if administrative_binding(instance)[2] != record:
        raise ConfigError('registered runtime changed during public code proof')
    return proof


def ownership(instance, action, *, journal_sha256=None, alias=None, room_id=None,
              confirm_lost_creation=False, transaction_id=None, decision=None):
    if action not in ('review', 'clear', 'resume'):
        raise ConfigError('invalid stopped ownership action')
    target, _, record = administrative_binding(instance)
    if record['execution'] != 'pilot-live':
        raise ConfigError('ownership operator commands require reviewed live pilot')
    _unit(instance)
    state = _service_state(instance)
    if state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed') or state['UnitFileState'] != 'disabled':
        raise ConfigError('ownership recovery requires disabled/stopped pilot service')
    from .pilot_installation import require_no_service_processes
    require_no_service_processes(record['service_uid'])
    arguments = ['--ownership-recovery', action]
    for option, value in (('--journal-sha256', journal_sha256), ('--room-alias', alias),
                         ('--created-room-id', room_id), ('--transaction-id', transaction_id), ('--decision', decision)):
        if value is not None: arguments += [option, str(value)]
    if confirm_lost_creation: arguments += ['--confirm-lost-creation']
    # The service helper holds verified_owner itself; no root-held owner lock
    # may cross this spawn and deadlock its fresh account/evidence checks.
    return _service_command(target, record, arguments, timeout=120)


def operator(instance, action, *, credentials_stream=None, expected_user_id=None,
             redirect_uri=None, mode='local', pacing_state_sha256=None,
             notice=None, reauthorize=False, replace_pending=False):
    if action not in ('configure', 'status', 'authorize', 'cancel', 'recover', 'pacing-recover'):
        raise ConfigError('invalid pilot operator action')
    if mode not in ('local', 'ssh'): raise ConfigError('invalid pilot callback mode')
    if type(reauthorize) is not bool or type(replace_pending) is not bool or ((reauthorize or replace_pending) and action != 'authorize'):
        raise ConfigError('new authorization choices require authorize')
    if action != 'authorize' and notice is not None:
        raise ConfigError('private notice applies only to authorize')
    configuring = action == 'configure'
    if (configuring != (credentials_stream is not None)
            or configuring != (expected_user_id is not None)
            or configuring != (redirect_uri is not None)):
        raise ConfigError('pilot configure requires explicit stdin and operator profile')
    if (action == 'pacing-recover') != (pacing_state_sha256 is not None):
        raise ConfigError('explicit pacing state required only for recovery')
    target, _, record = administrative_binding(instance)
    # Before even reading the private stream, run the service-UID integrity guard.
    _service_command(target, record, ['--pilot-check'])
    if action in ('authorize', 'recover'):
        from .provider_contract import require_live_provider
        from .live_release import policy_bytes, POLICY_NAME
        from .pilot_identity import LIVE
        if (record['execution'] != LIVE
                or (Path(record['release']) / POLICY_NAME).read_bytes() != policy_bytes()):
            require_live_provider()
    arguments = ['--operator-authorization', action, '--operator-mode', mode]
    data = None
    if configuring:
        from .operator_client import credentials_from_stream
        credentials = credentials_from_stream(credentials_stream)
        data = json.dumps({'client_id': credentials.client_id,
                           'client_secret': credentials.client_secret}).encode()
        arguments += ['--credentials-stdin', '--expected-user-id', str(expected_user_id),
                      '--redirect-uri', redirect_uri]
    if pacing_state_sha256 is not None: arguments += ['--pacing-state-sha256', pacing_state_sha256]
    if reauthorize: arguments += ['--reauthorize']
    if replace_pending: arguments += ['--replace-pending']
    if action == 'authorize':
        if not callable(notice): raise ConfigError('private operator console required')
        return _service_command(target, record, arguments, timeout=90, notice=notice)
    return _service_command(target, record, arguments, input_data=data, timeout=90)


def import_catalogs(instance, *, repository, trust_root, trust_sha256, modes):
    target, _, record = administrative_binding(instance)
    from .catalog_store import CatalogStore
    from .catalog_trust import BootstrapTrust, bounded_file
    from .catalog_export import MODES
    if not modes or len(modes) != len(set(modes)) or not set(modes) <= set(MODES):
        raise ConfigError('explicit distinct supported catalog modes required')
    with bounded_file(Path(trust_root), 65536) as f: raw = f.read(65537)
    if hashlib.sha256(raw).hexdigest() != trust_sha256:
        raise ConfigError('independently confirmed catalog bootstrap digest mismatch')
    trust = BootstrapTrust(raw, fixture=False)
    # Initial trust binding is also a stopped administrative transaction.
    with _stopped(target['root'], record['service_uid']):
        store = CatalogStore.initialize_pilot(instance, trust, trust_sha256)
    reference = store.import_local(repository, modes)
    return {'ok': True, 'catalogs_activated': True, 'catalog_set': reference['set_id'],
            'modes': sorted(reference['modes']), 'account_authorized': False, 'live_ready': False}


def https_repository(instance, url, ca_file, ca_sha256):
    target, _, _ = administrative_binding(instance)
    from .catalog_delivery import HttpsOrigin
    return HttpsOrigin(url, target['prefix'] / 'catalog-delivery-cache',
                      ca_file=ca_file, ca_sha256=ca_sha256)


def catalog_recover(instance):
    from .catalog_store import CatalogStore
    store = CatalogStore.open_pilot_administrative(instance)
    with store.stopped(): result = store.recover()
    return {'ok': True, 'catalog_recovery': result['status'], 'live_ready': False}


def replace_catalog_trust(instance, *, repository, trust_root, trust_sha256,
                          previous_trust_sha256, confirm_replacement):
    from .catalog_store import CatalogStore
    from .catalog_trust import BootstrapTrust, bounded_file
    from .catalog_rekey import replace
    store = CatalogStore.open_pilot_administrative(instance)
    with bounded_file(Path(trust_root), 65536) as handle: raw = handle.read(65537)
    if hashlib.sha256(raw).hexdigest() != trust_sha256:
        raise ConfigError('independently confirmed replacement catalog bootstrap digest mismatch')
    with _catalog_trust_stopped(instance):
        return replace(store, repository, BootstrapTrust(raw, fixture=False),
                       old_sha256=previous_trust_sha256, new_sha256=trust_sha256,
                       confirm_replacement=confirm_replacement, owner_lease_held=True)


@contextmanager
def _catalog_trust_stopped(instance):
    from .layout import file_lease
    from .pilot_installation import require_no_service_processes
    from .network import operations as network
    target, _, record = administrative_binding(instance)
    with file_lease(target['prefix'] / 'network-admin.lock'):
        _unit(instance, optional=True)
        network._checked_unit(instance, optional=True)
        for state in (_service_state(instance), network._state(instance)):
            if (state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed')
                    or state['UnitFileState'] not in ('', 'disabled', 'not-found')):
                raise ConfigError('catalog trust replacement requires disabled/stopped bot and network services')
        require_no_service_processes(record['service_uid'])
        with _stopped(target['root'], record['service_uid']):
            require_no_service_processes(record['service_uid'])
            yield


def recover_catalog_trust(instance, *, transaction_id):
    # A half-swapped directory cannot open normally. Reconstruct only the fixed
    # registered administrative namespace, never a caller-selected store path.
    from .catalog_store import CatalogStore, _identity, POINTER
    from .catalog_rekey import resume
    from .provisioning import _control
    target, marker, record = administrative_binding(instance)
    path = target['prefix'] / 'catalog-stores' / marker['installation_id']
    binding = _identity(marker, target['root'], record['service_uid'], path, False,
                        execution=record['execution'])
    if _control(target['root'] / POINTER, record['service_uid'], private=True) != binding:
        raise ConfigError('catalog trust recovery namespace changed')
    with _catalog_trust_stopped(instance):
        return resume(CatalogStore(target['root'], path, binding), transaction_id=transaction_id,
                      owner_lease_held=True)


def status(instance):
    target, _, record = administrative_binding(instance)
    result = check(instance)
    unit = _unit(instance, optional=True)
    result['service_installed'] = unit is not None
    if unit is not None:
        state = _service_state(instance)
        result.update(service_state=state['ActiveState'], service_enabled=state['UnitFileState'] == 'enabled')
        observed = _service_observation(target, record, state)
        if observed is not None:
            result['service_phase'] = observed['phase']
            result['room_phase'] = 'not-observed' if observed['phase'] == 'failed' else observed['phase']
            from .service_diagnostics import FAILURE_CODES
            if type(observed.get('failure_code')) is str and observed['failure_code'] in FAILURE_CODES:
                result['failure_code']=observed['failure_code']
                status = observed.get('failure_http_status')
                if type(status) is int and 100 <= status <= 599: result['failure_http_status'] = status
        elif not state['MainPID']:
            historical = _service_observation(target, record, state, historical=True)
            if historical is not None:
                result['last_service_phase'] = historical['phase']
                if historical['phase'] != 'failed': result['last_service_room_phase'] = historical['phase']
                result['service_observation_current'] = False
                from .service_diagnostics import FAILURE_CODES
                code = historical.get('failure_code')
                if type(code) is str and code in FAILURE_CODES:
                    result['last_service_failure_code'] = code
                    status = historical.get('failure_http_status')
                    if type(status) is int and 100 <= status <= 599: result['last_service_failure_http_status'] = status
            if record['execution'] == 'pilot-live':
                ownership = _service_command(target, record, ['--ownership-local-status'])
                result.update({key: ownership[key] for key in (
                    'room_phase', 'ownership_recovery_phase', 'ownership_status_source', 'failure_code')
                    if key in ownership})
    result['operator_client_configured'] = os.path.lexists(target['root'] / 'secrets/provider-operator-client.json')
    if result['operator_client_configured']: result['authorization'] = operator(instance, 'status')
    if os.path.lexists(target['root'] / 'state/catalog-installation.json'):
        from .catalog_store import CatalogStore
        catalogs = CatalogStore.open_pilot_administrative(instance).inspect()
        result.update(catalog_set=catalogs['reference']['set_id'],
                      catalog_modes=sorted(catalogs['reference']['modes']),
                      catalog_recovery_required=catalogs['recovery_required'])
        result['catalog_rows'] = {mode: {item['name']: item['tables']
            for item in manifest['files']} for mode, manifest in catalogs['manifests'].items()}
    authorization = result.get('authorization', {})
    if authorization.get('authorization_completed'):
        if result.get('room_phase') == 'recovery_required':
            authorization['next_action'] = 'resolve_ownership_recovery_before_start'
        elif result.get('catalog_recovery_required'):
            authorization['next_action'] = 'resolve_catalog_recovery_before_start'
    return result


def _service_observation(target, record, state, *, historical=False):
    """Read service-owned diagnostics; never adopt them as ownership authority."""
    path = target['root'] / 'state/provider-service.json'
    if not os.path.lexists(path): return None
    from .provisioning import _control
    value = _control(path, record['service_uid'], private=True)
    if (type(value) is not dict or value.get('format') != 1
            or value.get('execution') != 'provider-live-pilot'
            or value.get('uid') != record['service_uid']
            or value.get('installation_id') != record['installation_id']
            or value.get('generation') != record['generation']
            or value.get('phase') not in ('running', 'stopped', 'recovery_required', 'failed')
            or type(value.get('pid')) is not int or value['pid'] <= 0):
        raise ConfigError('pilot service observer belongs to another runtime')
    if not historical and (not state['MainPID'] or state['MainPID'] != value['pid']): return None
    return value


def _startup_private_profile(target, record):
    # Absence keeps ordinary startup behavior exact, including fixtures that
    # intentionally have no installed marker. Private records are read under
    # their dedicated UID and validated against the installation/configuration.
    if not os.path.lexists(target['root'] / 'secrets/private-pilot.json'):
        return None
    from .ratings.operations import _identity
    from .private_pilot import load_profile
    with _identity(record['service_uid']):
        return load_profile(target['root'])


def _wait_ready(target, record, *, timeout=None):
    from .config import validate
    configuration = validate(_control(target['root'] / 'config/host.json', record['service_uid'], private=True))
    expected_rooms = configuration['rooms']
    private_profile = _startup_private_profile(target, record)
    expected_room_count = 1 if private_profile is not None else len(expected_rooms)
    if timeout is None:
        # The cold full-catalog load and several checked provider handshakes per
        # room must fit the explicit owner four-room startup observation too.
        timeout = 75 + 45 * (expected_room_count - 1)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _service_state(target['root'].name)
        observed = _service_observation(target, record, state)
        if state['ActiveState'] not in ('active', 'activating') or not state['MainPID']:
            # Failure diagnostics are historical here; they can explain exit but
            # may never be adopted as current ownership/startup authority.
            if observed is None: observed = _service_observation(target, record, state, historical=True)
            from .service_diagnostics import FAILURE_CODES
            code=observed.get('failure_code') if observed else None
            if type(code) is not str or code not in FAILURE_CODES:code='service_failed'
            status = observed.get('failure_http_status') if observed else None
            suffix = f' (HTTP {status})' if type(status) is int and 100 <= status <= 599 else ''
            raise ConfigError('pilot startup failed: '+code+suffix+'; inspect status without repeating room creation')
        if observed is not None:
            if observed['phase'] == 'failed':
                from .service_diagnostics import FAILURE_CODES
                code = observed.get('failure_code')
                if type(code) is not str or code not in FAILURE_CODES: code = 'service_failed'
                status = observed.get('failure_http_status')
                suffix = f' (HTTP {status})' if type(status) is int and 100 <= status <= 599 else ''
                raise ConfigError('pilot startup failed: '+code+suffix+'; inspect status without repeating room creation')
            if observed['phase'] == 'recovery_required':
                raise ConfigError('pilot startup requires stopped ownership review')
            if observed['phase'] == 'running':
                if type(observed.get('room_count')) is not int or observed['room_count'] != expected_room_count:
                    raise ConfigError('pilot service did not observe the exact configured room count')
                if validate(_control(target['root'] / 'config/host.json', record['service_uid'], private=True))['rooms'] != expected_rooms:
                    raise ConfigError('pilot room configuration changed during startup observation')
                if _startup_private_profile(target, record) != private_profile:
                    raise ConfigError('private pilot profile changed during startup observation')
                return state
        time.sleep(.2)
    raise ConfigError('pilot startup observation timed out; inspect existing service without retry')


def lifecycle(instance, action):
    if action not in ('start', 'stop'): raise ConfigError('invalid pilot service action')
    target, _, record = administrative_binding(instance)
    _unit(instance)
    if action == 'start':
        from .provider_contract import require_live_provider
        from .live_release import policy_bytes, POLICY_NAME
        from .pilot_identity import LIVE
        if (record['execution'] != LIVE
                or (Path(record['release']) / POLICY_NAME).read_bytes() != policy_bytes()):
            require_live_provider()
        check(instance)
    _systemctl(action, target['unit'], timeout=100)
    state = _wait_ready(target, record) if action == 'start' else _service_state(instance)
    if action == 'stop':
        if state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed'):
            raise ConfigError('pilot graceful shutdown remains incomplete; do not force kill')
        with _stopped(target['root'], record['service_uid']): pass
    return {'ok': True, 'service_state': state['ActiveState'], 'live_ready': False}


def remove_unit(instance):
    """Remove only the checked disabled/stopped unit; keep private recovery data.

    Code, catalogs, state and the dedicated UID remain for explicit recovery or
    deliberate later data destruction. Never erase uncertain ownership to uninstall.
    """
    target, _, record = administrative_binding(instance)
    with _stopped(target['root'], record['service_uid']):
        unit = _unit(instance, optional=True)
        if unit is not None:
            state = _service_state(instance)
            if (state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed')
                    or state['UnitFileState'] != 'disabled'):
                raise ConfigError('pilot removal requires a disabled/stopped dedicated unit')
            unit.unlink()
            fd = os.open(UNIT_BASE, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
            _systemctl('daemon-reload')
    return {'ok': True, 'service_installed': False, 'service_started': False,
            'code_and_private_state_retained': True, 'service_identity_retained': True,
            'live_ready': False}


def support(instance):
    """Shareable fixed schema: no accounts, room identifiers, paths or chat."""
    value = status(instance)
    authorization = value.get('authorization', {})
    pacing = authorization.get('provider_pacing', {}).get('counters', {})
    # Copy only bounded counters, not ledger/profile/authority structures.
    counts = {key: pacing[key] for key in ('attempted', 'dispatched', 'replies', 'uncertain', 'rate_limited')
              if type(pacing.get(key)) is int and 0 <= pacing[key] < 2**63}
    result = {'kind': 'live-pilot-support' if value['execution'] == 'pilot-live' else 'pilot-candidate-support',
            'ok': value['ok'], 'version': value.get('version'), 'execution': value['execution'],
            'live_capable': value['provider_dispatch_enabled'], 'live_ready': False,
            'actual_room_acceptance_verified': False,
            'protected_runtime_checked': value['protected_runtime_checked'],
            'service_installed': value['service_installed'],
            'service_state': value.get('service_state', 'not-installed'),
            'room_phase': value.get('room_phase', 'not-observed'),
            'service_enabled': value.get('service_enabled', False),
            'operator_client_configured': value['operator_client_configured'],
            'authorization_ok': authorization.get('ok', False),
            'pending_phase': authorization.get('pending_phase', 'absent'),
            'token_phase': authorization.get('token_phase', 'absent'),
            'next_action': authorization.get('next_action', 'configure_operator_client'),
            'start_allowed': False,
            'start_allowed_reason': authorization.get('start_allowed_reason',
                'startup_admission_not_evaluated_by_local_status'),
            'startup_verification_required': authorization.get('startup_verification_required', True),
            'pacing_counters': counts, 'catalog_modes': value.get('catalog_modes', []),
            'catalog_rows': value.get('catalog_rows', {}),
            'catalog_recovery_required': value.get('catalog_recovery_required', False),
            'local_collection_or_analysis': False}
    for key in ('last_service_room_phase', 'service_observation_current',
                'ownership_recovery_phase', 'ownership_status_source', 'failure_code',
                'service_phase', 'failure_http_status', 'last_service_phase',
                'last_service_failure_code', 'last_service_failure_http_status'):
        if key in value: result[key] = value[key]
    from .network.operations import command
    network=command(instance,'support')
    result['network']={key:network[key] for key in ('ok','configured','enrolled','pending_operation','directory',
        'account_binding_matches',
        'credential_expires_at','last_heartbeat_receipt_at','pending_heartbeat','agent_phase','agent_error',
        'last_attempt_at','live_rooms_affected','remote_health_verified','protected_runtime_checked') if key in network}
    return result
