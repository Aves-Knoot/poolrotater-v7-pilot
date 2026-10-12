"""Installed operator actions with private explicit input and disabled live I/O.

Local configuration/status/pacing recovery never need provider TLS or a browser.
Authorization uses the same transaction/session engine as invented integration
tests, but the prepared provider authority refuses before binding a callback.
"""
import math

from .authorization import OperatorAuthorizationSession, authorization_diagnostic
from .installed_authorization import (
    _runtime_guard, configure_installed_provider_client, installed_provider_state,
    installed_provider_authentication,
)
from .oauth import AuthError
from .operator_client import OperatorClient, credentials_from_stream
from .provider_authorization import ProviderOAuth

ACTIONS = ('configure', 'status', 'authorize', 'cancel', 'recover', 'pacing-recover')


def _choices(action, *, mode, reauthorize, replace_pending, pacing_state_sha256, timeout):
    if action not in ACTIONS or mode not in ('local', 'ssh'):
        raise AuthError('invalid_operator_action')
    if type(reauthorize) is not bool or type(replace_pending) is not bool:
        raise AuthError('invalid_authorization_choice')
    if (reauthorize or replace_pending) and action != 'authorize':
        raise AuthError('invalid_operator_action')
    if (action == 'pacing-recover') != (pacing_state_sha256 is not None):
        raise AuthError('explicit_pacing_state_required')
    if pacing_state_sha256 is not None and (type(pacing_state_sha256) is not str
            or len(pacing_state_sha256) != 64
            or any(c not in '0123456789abcdef' for c in pacing_state_sha256)):
        raise AuthError('explicit_pacing_state_required')
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 600):
        raise AuthError('invalid_callback_timeout')


def execute(authentication, action, *, mode='local', reauthorize=False,
            replace_pending=False, pacing_state_sha256=None, notice=None, timeout=60):
    """One complete action on an already checked, provider-specific authority.

    A private caller receives the session through notice to drive local/SSH
    authorization. URLs and callback state are never part of diagnostic output.
    The invented driver uses this hook to supply its loopback callback.
    """
    _choices(action, mode=mode, reauthorize=reauthorize, replace_pending=replace_pending,
             pacing_state_sha256=pacing_state_sha256, timeout=timeout)
    if not isinstance(authentication, ProviderOAuth):
        raise AuthError('provider_authority_required')
    instructions = None
    if action == 'authorize':
        # This must precede input hooks, listener binding and durable OAuth intent.
        authentication.require_authorization_available()
        if not callable(notice): raise AuthError('private_operator_notice_required')
        with OperatorAuthorizationSession(authentication, authentication._redirect_uri,
                mode=mode, reauthorize=reauthorize, replace_pending=replace_pending) as session:
            instructions = {key: value for key, value in session.instructions().items()
                            if key != 'redirect_uri'}
            notice(session)
            session.wait(timeout)
    elif action == 'cancel':
        authentication.cancel()
    elif action == 'recover':
        authentication.recover_authorization()
    elif action == 'pacing-recover':
        authentication.recover_pacing(expected_state_sha256=pacing_state_sha256)
    result = authorization_diagnostic(authentication)
    result['live_capable'] = authentication.execution == 'provider-live-pilot'
    result.update(provider_pacing=authentication.pacing_snapshot(), action=action,
                  operator_client_configured=True, provider_client_ownership_verified=False,
                  protected_runtime_checked=True, workers_started=0, start_allowed=False,
                  actual_room_testing=False, live_ready=False)
    if result['live_capable']:
        result['authorization_completed'] = result['pending_phase'] == 'complete' and result['token_phase'] == 'ready'
        result['startup_verification_required'] = True
        result['provider_client_ownership_verified'] = None
        result['verification_scope'] = 'authorization_checks_expected_identity; startup_rechecks_identity_client_scopes_and_room_ownership'
        if result['authorization_completed'] and result['next_action'] == 'verify_owner_before_use':
            result['next_action'] = 'start_performs_fresh_owner_verification; retain_state_if_ownership_recovery_required'
        result['start_allowed_reason'] = 'startup_admission_not_evaluated_by_local_status'
    if instructions is not None: result['instructions'] = instructions
    return result


def run(root, release, action, *, credentials_stream=None, expected_user_id=None,
        redirect_uri=None, mode='local', reauthorize=False, replace_pending=False,
        pacing_state_sha256=None, notice=None, timeout=60, configuration=None, trust=None):
    """Prepared installed entry: no ambient credentials, URL or enable switches."""
    _choices(action, mode=mode, reauthorize=reauthorize, replace_pending=replace_pending,
             pacing_state_sha256=pacing_state_sha256, timeout=timeout)
    configuring = action == 'configure'
    if configuring != (credentials_stream is not None):
        raise AuthError('explicit_operator_credentials_required')
    if (expected_user_id is not None or redirect_uri is not None) and not configuring:
        raise AuthError('invalid_operator_action')
    if configuring and (expected_user_id is None or redirect_uri is None):
        raise AuthError('explicit_operator_profile_required')
    # Protect the input boundary before reading even an explicitly supplied stream.
    _runtime_guard(root, release, prepared=True)
    if configuring:
        profile = OperatorClient(credentials_from_stream(credentials_stream),
                                 expected_user_id, redirect_uri)
        configure_installed_provider_client(root, release, profile)
    if action in ('authorize', 'recover'):
        # The local state-only transport has no CA snapshot. A future approved
        # authorization must reconstruct the full fixed adapter, never send a
        # credential with the diagnostic adapter or fall back to fixture TLS.
        from .provider_contract import require_live_provider
        from .provider_transport import TrustSnapshot
        from .pilot_identity import provider_execution
        runtime = _runtime_guard(root, release, prepared=True)()
        require_live_provider(provider_execution(runtime))
        authentication = installed_provider_authentication(root, release,
            TrustSnapshot.system(prepared=True) if trust is None else trust, configuration)
    else:
        authentication = installed_provider_state(root, release,
                                                   initialize=configuring,
                                                   configuration=configuration)
    return execute(authentication, action, mode=mode, reauthorize=reauthorize,
                   replace_pending=replace_pending, pacing_state_sha256=pacing_state_sha256,
                   notice=notice, timeout=timeout)
