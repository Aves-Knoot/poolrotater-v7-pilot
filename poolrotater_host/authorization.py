"""Operator callback workflow shared by isolated and prepared authorities.

The URL is available only to the caller driving the invented provider contract.
Diagnostics never contain URLs, state, credentials, usernames or provider bodies.
SSH forwarding is described, never launched by this module.
"""
from urllib.parse import parse_qs, urlsplit

from .oauth import AuthError, OAuthAuthority, OfflineOAuth, redirect_parts
from .oauth_callback import LoopbackCallback


def _authentication(value):
    if not isinstance(value, OAuthAuthority): raise AuthError('offline_fixture_required')
    return value


def authorization_diagnostic(authentication):
    """Read checked local phases without provider traffic or exposing file paths."""
    _authentication(authentication)
    try:
        result = authentication.authorization_status()
    except Exception:
        return {'ok': False, 'execution': authentication.execution, 'live_ready': False,
                'next_action': 'review_private_state_or_stop_owner'}
    phase = result['pending_phase']
    if result['candidate_recovery_available']:
        action = 'recover_candidate_identity'
    elif phase in {'exchange_pending', 'verifying'}:
        action = 'explicit_new_authorization_no_code_replay'
    elif phase == 'waiting':
        action = 'cancel_expired_attempt' if result['callback_expired'] else 'wait_for_callback_or_cancel'
    elif result['token_phase'] in {'refresh_pending', 'revoke_pending', 'reauthorization_required', 'revoked'}:
        action = 'explicit_new_authorization'
    elif result['token_phase'] in {'ready', 'unverified'}:
        action = 'verify_owner_before_use'
    else:
        action = 'begin_authorization'
    return {'ok': True, **result, 'next_action': action}


def recover_authorization(authentication):
    """Explicit candidate verification only; never repeat the code exchange."""
    return _authentication(authentication).recover_authorization()


class OperatorAuthorizationSession:
    """Single-use, attempt-bound callback session for local or SSH operators.

    Bind first, then create durable state. Existing unfinished attempts require
    explicit replacement. Context exit cancels only its own still-waiting attempt;
    uncertain exchanges/candidates remain available for explicit recovery.
    """
    def __init__(self, authentication, redirect_uri, *, mode='local', reauthorize=False,
                 replace_pending=False):
        self.authentication = _authentication(authentication)
        self.parts = redirect_parts(redirect_uri)
        if mode not in ('local', 'ssh'): raise AuthError('invalid_operator_mode')
        if type(reauthorize) is not bool or type(replace_pending) is not bool:
            raise AuthError('invalid_authorization_choice')
        self.mode, self.redirect_uri = mode, redirect_uri
        self.reauthorize, self.replace_pending = reauthorize, replace_pending
        self.listener = None
        self._url = None
        self._state = None
        self._entered = False
        self._active = False

    @property
    def authorization_url(self):
        if not self._active: raise AuthError('authorization_session_inactive')
        return self._url

    def instructions(self):
        result = {'execution': self.authentication.execution, 'live_ready': False, 'mode': self.mode,
                  'redirect_uri': self.redirect_uri, 'browser_opened': False,
                  'ssh_started': False, 'callback_bind': '127.0.0.1',
                  'callback_port': self.parts.port}
        if self.mode == 'ssh':
            result['ssh_forward'] = {'local_bind': '127.0.0.1', 'local_port': self.parts.port,
                                     'remote_host': '127.0.0.1', 'remote_port': self.parts.port}
        return result

    def __enter__(self):
        if self._entered: raise AuthError('authorization_session_already_used')
        self._entered = True
        self.authentication.require_authorization_available()
        self.listener = LoopbackCallback(self.authentication, self.redirect_uri, initially_armed=False)
        self.listener.__enter__()
        try:
            self._url = self.authentication.begin(self.redirect_uri, reauthorize=self.reauthorize,
                                                   replace_pending=self.replace_pending)
            self._state = parse_qs(urlsplit(self._url).query)['state'][0]
            self.listener.arm(self._state)
            self._active = True
        except BaseException:
            try:
                self.listener.__exit__(None, None, None)
            finally:
                if self._state is not None:
                    self.authentication.cancel(expected_state=self._state)
                self._url = self._state = None
            raise
        return self

    def wait(self, timeout=60):
        if not self._active: raise AuthError('authorization_session_inactive')
        return self.listener.wait(timeout)

    def cancel(self):
        if not self._active: raise AuthError('authorization_session_inactive')
        self.listener.cancel()

    def diagnostic(self):
        result = authorization_diagnostic(self.authentication)
        result['callback_retryable'] = bool(self._active and self.listener.retryable)
        return result

    def __exit__(self, *args):
        try:
            self.listener.__exit__(*args)
            # Close the listener before cancellation. No request can race a new
            # attempt or outlive the callback workflow after a normal shutdown.
            if not self.listener._done.is_set():
                self.authentication.cancel(expected_state=self._state)
        finally:
            self._active = False
            self._url = self._state = None


class OfflineAuthorizationSession(OperatorAuthorizationSession):
    """Compatibility fixture session; cannot accept prepared provider authority."""

    def __init__(self, authentication, redirect_uri, **kwargs):
        if not isinstance(authentication, OfflineOAuth):
            raise AuthError('offline_fixture_required')
        super().__init__(authentication, redirect_uri, **kwargs)
