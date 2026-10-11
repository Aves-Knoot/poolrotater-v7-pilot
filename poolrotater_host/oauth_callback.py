"""Bounded loopback-only synthetic callback listener; never opens a browser."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import math
import threading

from .oauth import AuthError, OAuthAuthority, redirect_parts


class LoopbackCallback:
    def __init__(self, authentication, redirect_uri, *, initially_armed=True):
        if not isinstance(authentication, OAuthAuthority): raise AuthError('offline_fixture_required')
        if type(initially_armed) is not bool: raise AuthError('invalid_callback_choice')
        self.authentication = authentication
        self.parts = redirect_parts(redirect_uri)
        self.redirect_uri = redirect_uri
        self._done = threading.Event()
        self._stop = threading.Event()
        self.result = None
        self.error = None
        self.server = None
        self.thread = None
        self._handling = threading.RLock()
        self._armed = initially_armed
        self._expected_state = None
        self.retryable = False
        self._entered = False

    def arm(self, state):
        from .oauth import _secret
        with self._handling:
            if self._armed or self._done.is_set() or self.server is None:
                raise AuthError('callback_arm_unavailable')
            self._expected_state = _secret(state, 128)
            self._armed = True

    def _retryable(self, error):
        # Only pre-intent admission refusal is safe to resubmit. The same code
        # must never be exchanged twice after an uncertain provider outcome.
        if error.code not in {'provider_pacing_deferred', 'provider_pacing_busy', 'provider_deadline_exceeded'}:
            return False
        try:
            return self.authentication.callback_retry_available(self._expected_state)
        except Exception:
            return False

    def __enter__(self):
        if self._entered: raise AuthError('callback_listener_already_used')
        self._entered = True
        self.authentication.require_authorization_available()
        callback = self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def log_message(self, *args): pass

            def send_error(self, code, message=None, explain=None):
                # BaseHTTPRequestHandler can include a malformed request line in
                # its default HTML. Never reflect callback codes/state, even when
                # parsing fails before do_GET.
                self.reply(code)

            def reply(self, status):
                body = (b'Local request admission deferred. Retry this callback before it expires.'
                        if status == 503 and callback.retryable else
                        b'Authorization handled. Return to the local operator tool.')
                self.send_response(status)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                try: self.wfile.write(body)
                except OSError: pass  # Browser disconnect does not change durable auth outcome.

            def do_GET(self):
                with callback._handling:
                    if callback._done.is_set() or callback._stop.is_set():
                        status = 410
                    elif not callback._armed:
                        status = 503
                    else:
                        try:
                            if len(self.headers.get_all('Host', [])) != 1: raise AuthError('invalid_callback')
                            callback.result = callback.authentication.complete(
                                self.path, self.headers['Host'], expected_state=callback._expected_state)
                            callback.retryable = False
                            status = 200
                            callback._done.set()
                        except AuthError as exc:
                            status = 400
                            callback.retryable = callback._retryable(exc)
                            if callback.retryable:
                                status = 503
                            elif exc.code != 'invalid_callback':
                                callback.error = exc
                                callback._done.set()
                        except Exception:
                            callback.error = AuthError('callback_failed')
                            callback._done.set()
                            status = 400
                self.reply(status)
        try: self.server = HTTPServer(('127.0.0.1', self.parts.port), Handler)
        except OSError: raise AuthError('callback_listener_unavailable') from None
        self.server.timeout = .05
        self.thread = threading.Thread(target=self._serve, name='offline-oauth-callback', daemon=True)
        try: self.thread.start()
        except Exception:
            self.server.server_close()
            raise AuthError('callback_listener_unavailable') from None
        return self

    def _serve(self):
        while not self._stop.is_set(): self.server.handle_request()

    def wait(self, timeout=60):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise AuthError('invalid_callback_timeout')
        if not self._done.wait(timeout):
            self.cancel('callback_timeout')
        if self.error is not None: raise self.error
        return self.result

    def cancel(self, code='authorization_cancelled'):
        if code not in {'authorization_cancelled', 'callback_timeout'}:
            raise AuthError('invalid_callback_choice')
        self._stop.set()
        with self._handling:
            if not self._done.is_set():
                self.authentication.cancel(expected_state=self._expected_state)
                self.error = AuthError(code)
                self.retryable = False
                self._done.set()

    def __exit__(self, *args):
        self._stop.set()
        self.thread.join(timeout=7)
        # A stuck request must retain its listening socket until the thread is
        # reaped, rather than permit another listener to claim the same port.
        if self.thread.is_alive(): raise AuthError('callback_shutdown_incomplete')
        self.server.server_close()
