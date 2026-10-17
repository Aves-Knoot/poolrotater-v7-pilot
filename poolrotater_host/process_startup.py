"""Small spawn bootstrap; large private arguments use deadline-bound IPC.

Multiprocessing writes preparation data and process arguments synchronously in
start(). Sending trust bundles there can deadlock after a child fails to chdir.
Only a small fixed bootstrap travels through that unbounded startup pipe. All
large arguments stay in memory and cross a separate pipe under the caller's
deadline, after a pidfd is acquired. No secret enters argv, environment or disk.
"""
import multiprocessing
from multiprocessing import reduction, spawn
import os
import pickle
import select
import signal
import struct
import time

from .oauth import AuthError
from .process_target import OwnedChild, require_child_platform

MAX_ARGUMENTS = 4 * 1024**2
# Leave ample space in even a 4096-byte startup pipe for the small Process frame.
MAX_PREPARATION = 2048


def preflight():
    require_child_platform()
    try:
        cwd = os.getcwd()
        if not os.path.isdir(cwd) or not os.access(cwd, os.X_OK): raise ValueError
        data = spawn.get_preparation_data('provider-wire')
        data['authkey'] = bytes(data['authkey'])
        if len(data['authkey']) > 256: raise ValueError
        if len(reduction.ForkingPickler.dumps(data)) > MAX_PREPARATION: raise ValueError
    except Exception:
        raise AuthError('provider_child_setup_refused') from None


def _bootstrap(pipe, target, parent_pid):
    try:
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise ValueError
        if os.getppid() != parent_pid: raise ValueError
        # Private parent-owned IPC only; never decode provider-supplied pickle.
        try: raw = pipe.recv_bytes(MAX_ARGUMENTS)
        except EOFError: return  # Parent cancelled before arguments/dispatch.
        args = pickle.loads(raw)
        target(pipe, *args)
    finally:
        pipe.close()


def _send(pipe, raw, deadline):
    fd = pipe.fileno()
    packet = struct.pack('!i', len(raw)) + raw
    os.set_blocking(fd, False)
    try:
        offset = 0
        while offset < len(packet):
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [fd], [], left)[1]:
                raise AuthError('provider_deadline_exceeded')
            try: offset += os.write(fd, packet[offset:offset + 16384])
            except BlockingIOError: continue
    finally:
        os.set_blocking(fd, True)


def start(target, args, deadline, *, name='provider-wire'):
    preflight()
    # Function references, not captured callables/closures or arbitrary state.
    # The fixed provider callers select the target; there is no CLI target input.
    import types
    if (type(target) is not types.FunctionType or '<locals>' in target.__qualname__
            or len(target.__module__) + len(target.__qualname__) > 256):
        raise AuthError('provider_child_setup_refused')
    raw = pickle.dumps(args, protocol=4)
    if len(raw) > MAX_ARGUMENTS: raise AuthError('provider_child_setup_refused')
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe(duplex=True)
    process = context.Process(target=_bootstrap, args=(child, target, os.getpid()),
                              name=name, daemon=True)
    owned = None
    try:
        if time.monotonic() >= deadline: raise AuthError('provider_deadline_exceeded')
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM))
        try:
            process.start()
            owned = OwnedChild(process.pid)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        child.close()
        _send(parent, raw, deadline)
        return process, owned, parent
    except BaseException:
        child.close(); parent.close()
        try:
            if owned is not None:
                owned.kill()
            # Failure to acquire a pidfd must not leak a cooperative bootstrap.
            # No private arguments were sent; closed IPC makes it exit on EOF.
            # Never fall back to an unpinned numeric signal if it does not exit.
            if process.pid is not None:
                process.join(timeout=1)
                if process.exitcode is None:
                    raise AuthError('provider_child_cleanup_uncertain')
        finally:
            if owned is not None: owned.close()
            if process.pid is None or process.exitcode is not None: process.close()
        raise
