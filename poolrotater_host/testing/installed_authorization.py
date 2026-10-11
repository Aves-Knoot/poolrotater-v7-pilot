"""Invented installed setup/recovery driver; reachable only through fixture bootstrap."""
from contextlib import contextmanager
import http.client
import os
from pathlib import Path
import signal
import tempfile
from urllib.parse import parse_qs, urlencode, urlsplit

from ..authorization import OfflineAuthorizationSession, authorization_diagnostic, recover_authorization
from ..installed_authorization import configure_installed_client, installed_https_authentication, _runtime_guard
from ..layout import load
from ..operator_client import (PROFILE_PATH, OperatorClient, credentials_from_environment,
                               operator_client_diagnostic, same_client)
from ..oauth import AuthError, SCOPES
from ..provider_transport import TrustSnapshot
from ..provider_pacing import STATE_PATH, ANCHOR_PATH
from .provider_https import TLSProvider, certificates

REDIRECT = 'http://127.0.0.1:51111/oauth/callback'
SCENARIOS = ('status', 'authorize', 'cancel', 'timeout', 'candidate', 'recover',
             'lost-reply', 'waiting-kill', 'promotion-kill', 'pacing-recover')


def configure_installed_provider_fixture_client(root, release, profile):
    """Explicit invented qualification profile, never a fixed-provider profile."""
    from ..operator_client import _configure_provider_client
    return _configure_provider_client(root, profile, local_guard=_runtime_guard(root, release),
                                      execution='provider-fixture')


def load_provider_fixture_client(root):
    from ..operator_client import _load_provider_client
    return _load_provider_client(root, execution='provider-fixture')


def installed_provider_fixture_authentication(root, release, origin, trust, *, timeout=2,
                                             minimum_interval=.05, initialize=False):
    """Same installed workflow with explicit loopback TLS and invented authority."""
    from ..installed_authorization import _provider_authentication
    from ..provider_transport import DeadlineFixtureTransport, RequestGate
    guard = _runtime_guard(root, release)
    if type(timeout) not in (int, float) or not 0 < timeout <= 5:
        raise AuthError('invalid_provider_timeout')
    if type(minimum_interval) not in (int, float) or not .05 <= minimum_interval <= 60:
        raise AuthError('invalid_provider_pacing')
    transport = DeadlineFixtureTransport(origin, trust, timeout=timeout,
                                         gate=RequestGate(minimum_interval=minimum_interval))
    return _provider_authentication(root, guard, transport, initialize=initialize)


@contextmanager
def authority(root, release, *, initialize=True):
    """Recreate only this fixture's invented provider across process restarts."""
    root = Path(root)
    existing_authority=any(os.path.lexists(root/path) for path in (
        'secrets/osu-token.json','secrets/oauth-pending.json','secrets/oauth-candidate.json','state/oauth-account.json'))
    if not initialize or existing_authority:
        _runtime_guard(root, release)
        if any(not os.path.lexists(root/path) for path in (PROFILE_PATH, STATE_PATH, ANCHOR_PATH)):
            raise AuthError('provider_pacing_state_missing')
    _, config = load(root)
    account = config['account']
    credentials = credentials_from_environment(config, {
        account['client_id_env']: '123', account['client_secret_env']: 'invented-client-secret'})
    profile = OperatorClient(credentials, 99, REDIRECT)
    if not os.path.lexists(root / PROFILE_PATH): configure_installed_client(root, release, profile)
    with tempfile.TemporaryDirectory(prefix='installed-authority-tls-') as folder:
        certificates(folder)
        with TLSProvider(folder) as provider:
            trust = TrustSnapshot((Path(folder)/'ca.crt').read_bytes())
            origin = 'https://127.0.0.1:'+str(provider.server.server_port)
            auth = installed_https_authentication(root, release, origin, trust)
            if not same_client(profile, OperatorClient(auth.credentials, auth._expected_user_id, auth._redirect_uri)):
                raise AuthError('invented_installed_credentials_required')
            for path in (auth.token_path, auth.candidate_path):
                if not path.exists(): continue
                token = auth._read(path, 'token')
                if not token['access_token'].endswith('.invented') or not token['refresh_token'].startswith('invented-refresh-'):
                    raise AuthError('invented_installed_credentials_required')
                identity = (99, 123, SCOPES, False)
                provider.access[token['access_token']] = identity
                provider.refresh[token['refresh_token']] = (identity, token['access_token'])
            yield auth, provider


def authorize(auth, provider, *, scenario='authorize', reauthorize=False, replace_pending=False):
    if scenario not in SCENARIOS or scenario in ('status', 'recover', 'pacing-recover'):
        raise AuthError('invalid_installed_fixture_scenario')
    with OfflineAuthorizationSession(auth, REDIRECT, reauthorize=reauthorize,
                                      replace_pending=replace_pending) as session:
        if scenario == 'waiting-kill': os.kill(os.getpid(), signal.SIGKILL)
        if scenario == 'cancel':
            session.cancel()
            return
        if scenario == 'timeout':
            try: session.wait(.02)
            except AuthError as error:
                if error.code != 'callback_timeout': raise
            return
        if scenario == 'candidate': provider.fault('/api/v2/me', 503, {})
        if scenario == 'lost-reply': provider.fault('/oauth/token', 'disconnect', after=True)
        state = parse_qs(urlsplit(session.authorization_url).query)['state'][0]
        parts = urlsplit(REDIRECT)
        connection = http.client.HTTPConnection('127.0.0.1', parts.port, timeout=15)
        try:
            connection.request('GET', parts.path + '?' + urlencode({'code': provider.code(), 'state': state}))
            response = connection.getresponse(); response.read()
        finally:
            connection.close()
        try: return session.wait(1)
        except AuthError as error:
            expected = {'candidate': 'identity_temporarily_unavailable',
                        'lost-reply': 'transport_reply_uncertain'}
            if error.code != expected.get(scenario): raise


def run(root, release, *, scenario, reauthorize=False, replace_pending=False, pacing_state_sha256=None):
    """No controller or worker import, room transport, credential arguments or env input."""
    if scenario not in SCENARIOS: raise AuthError('invalid_installed_fixture_scenario')
    if (scenario == 'pacing-recover') != (pacing_state_sha256 is not None):
        raise AuthError('explicit_pacing_state_required')
    if scenario in ('status', 'recover', 'pacing-recover') and (reauthorize or replace_pending):
        raise AuthError('invalid_installed_fixture_scenario')
    if pacing_state_sha256 is not None and (not isinstance(pacing_state_sha256,str)
            or len(pacing_state_sha256)!=64 or any(c not in '0123456789abcdef' for c in pacing_state_sha256)):
        raise AuthError('explicit_pacing_state_required')
    with authority(root, release, initialize=scenario!='pacing-recover') as (auth, provider):
        if scenario == 'promotion-kill':
            # Fault injection is confined to this invented fixture process.
            from .. import oauth
            original = oauth.atomic_json
            def kill_after_token(path, record):
                original(path, record)
                if path == auth.token_path: os.kill(os.getpid(), signal.SIGKILL)
            oauth.atomic_json = kill_after_token
        try:
            if scenario == 'pacing-recover':
                auth.transport.gate.recover(expected_state_sha256=pacing_state_sha256,
                                            local_guard=auth._local_guard)
            elif scenario == 'recover': recover_authorization(auth)
            elif scenario != 'status':
                authorize(auth, provider, scenario=scenario, reauthorize=reauthorize,
                          replace_pending=replace_pending)
            result = authorization_diagnostic(auth)
            result.update(operator_client_diagnostic(root))
            result['provider_pacing'] = auth.transport.gate.snapshot()
            result.update(scenario=scenario, protected_runtime_checked=True,
                          synthetic_code_exchanges=sum(c['method'] == 'POST' for c in provider.calls),
                          workers_started=0, actual_room_testing=False, start_allowed=False)
            return result
        finally:
            if scenario == 'promotion-kill': oauth.atomic_json = original
