"""Invented installed operator driver; no credentials accepted from its caller."""
from contextlib import contextmanager
import http.client
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlencode, urlsplit

from ..installed_authorization import _runtime_guard
from ..installed_operator_workflow import execute
from ..oauth import AuthError, ClientCredentials, SCOPES
from ..operator_client import OperatorClient
from ..provider_transport import TrustSnapshot
from .installed_authorization import (
    configure_installed_provider_fixture_client, installed_provider_fixture_authentication,
)
from .provider_https import TLSProvider, certificates

REDIRECT = 'http://127.0.0.1:51111/oauth/callback'
SCENARIOS = ('configure', 'status', 'authorize', 'cancel', 'timeout', 'candidate',
             'recover', 'lost-reply', 'pacing-recover')


@contextmanager
def authority(root, release, *, initialize=False):
    """Reconstruct only the explicitly invented authority in its own namespace."""
    _runtime_guard(root, release)
    if initialize:
        configure_installed_provider_fixture_client(root, release, OperatorClient(
            ClientCredentials(123, 'invented-client-secret'), 99, REDIRECT))
    with tempfile.TemporaryDirectory(prefix='operator-workflow-invented-tls-') as folder:
        certificates(folder)
        with TLSProvider(folder) as provider:
            trust = TrustSnapshot((Path(folder) / 'ca.crt').read_bytes())
            origin = 'https://127.0.0.1:' + str(provider.server.server_port)
            auth = installed_provider_fixture_authentication(root, release, origin, trust,
                                                               initialize=initialize)
            if (auth.credentials.client_id != 123
                    or auth.credentials.client_secret != 'invented-client-secret'
                    or auth._expected_user_id != 99 or auth._redirect_uri != REDIRECT):
                raise AuthError('invented_installed_credentials_required')
            for path in (auth.token_path, auth.candidate_path):
                if not path.exists(): continue
                token = auth._read(path, 'token')
                if (not token['access_token'].endswith('.invented')
                        or not token['refresh_token'].startswith('invented-refresh-')):
                    raise AuthError('invented_installed_credentials_required')
                identity = (99, 123, SCOPES, False)
                provider.access[token['access_token']] = identity
                provider.refresh[token['refresh_token']] = (identity, token['access_token'])
            yield auth, provider


def run(root, release, *, scenario, mode='local', reauthorize=False,
        replace_pending=False, pacing_state_sha256=None):
    if scenario not in SCENARIOS or mode not in ('local', 'ssh'):
        raise AuthError('invalid_installed_fixture_scenario')
    if type(reauthorize) is not bool or type(replace_pending) is not bool:
        raise AuthError('invalid_authorization_choice')
    if scenario in ('configure', 'status', 'recover', 'pacing-recover') and (reauthorize or replace_pending):
        raise AuthError('invalid_installed_fixture_scenario')
    if (scenario == 'pacing-recover') != (pacing_state_sha256 is not None):
        raise AuthError('explicit_pacing_state_required')
    if pacing_state_sha256 is not None and (type(pacing_state_sha256) is not str
            or len(pacing_state_sha256) != 64
            or any(c not in '0123456789abcdef' for c in pacing_state_sha256)):
        raise AuthError('explicit_pacing_state_required')
    initialize = scenario not in ('status', 'recover', 'pacing-recover')
    with authority(root, release, initialize=initialize) as (auth, provider):
        if scenario in ('configure', 'status', 'recover', 'pacing-recover'):
            result = execute(auth, scenario, mode=mode,
                             pacing_state_sha256=pacing_state_sha256)
        else:
            def callback(session):
                if scenario == 'cancel':
                    session.cancel()
                    return
                if scenario == 'timeout': return
                if scenario == 'candidate': provider.fault('/api/v2/me', 503, {})
                if scenario == 'lost-reply': provider.fault('/oauth/token', 'disconnect', after=True)
                state = parse_qs(urlsplit(session.authorization_url).query)['state'][0]
                parts = urlsplit(REDIRECT)
                connection = http.client.HTTPConnection('127.0.0.1', parts.port, timeout=15)
                try:
                    connection.request('GET', parts.path + '?' + urlencode({
                        'code': provider.code(), 'state': state}))
                    response = connection.getresponse(); response.read()
                finally:
                    connection.close()
            try:
                result = execute(auth, 'authorize', mode=mode, notice=callback,
                                 timeout=.02 if scenario == 'timeout' else 1,
                                 reauthorize=reauthorize, replace_pending=replace_pending)
            except AuthError as error:
                expected = {'cancel': 'authorization_cancelled', 'timeout': 'callback_timeout',
                            'candidate': 'identity_temporarily_unavailable',
                            'lost-reply': 'transport_reply_uncertain'}
                if error.code != expected.get(scenario): raise
                result = execute(auth, 'status', mode=mode)
        result.update(scenario=scenario, synthetic_code_exchanges=sum(
            call['method'] == 'POST' for call in provider.calls), workers_started=0,
            actual_room_testing=False, start_allowed=False, live_ready=False)
        return result
