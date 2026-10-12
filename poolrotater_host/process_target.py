"""Linux child identity pinned before any observation can reap it.

Never send numeric signals to a caller-supplied PID. A pidfd remains bound to the
original task after exit/reaping/PID reuse. Positive PID and parent checks also
reject process-group selectors and mocked identities before opening that handle.
"""
import os
from pathlib import Path
import signal

from .oauth import AuthError


def require_child_platform():
    if not all(hasattr(os, name) for name in ('pidfd_open', 'waitid', 'WNOWAIT')) or not hasattr(signal, 'pidfd_send_signal'):
        raise AuthError('process_identity_platform_required')


def child_identity(pid):
    if type(pid) is not int or pid <= 1 or pid == os.getpid():
        raise AuthError('process_target_refused')
    try:
        # comm may contain spaces or parentheses; field indexing starts after it.
        fields = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()
        if int(fields[1]) != os.getpid(): raise ValueError
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        return (pid, int(fields[19]))
    except (OSError, ValueError, IndexError):
        raise AuthError('process_target_refused') from None


class OwnedChild:
    def __init__(self, pid):
        require_child_platform()
        identity = child_identity(pid)
        self.fd = None
        try:
            self.fd = os.pidfd_open(pid)
            if child_identity(pid) != identity: raise AuthError('process_target_refused')
        except BaseException:
            self.close()
            raise

    def send(self, number):
        if self.fd is None: raise AuthError('process_target_refused')
        if number not in (signal.SIGTERM, signal.SIGKILL): raise AuthError('process_target_refused')
        try: signal.pidfd_send_signal(self.fd, number)
        except ProcessLookupError: pass

    def kill(self):
        self.send(signal.SIGKILL)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
