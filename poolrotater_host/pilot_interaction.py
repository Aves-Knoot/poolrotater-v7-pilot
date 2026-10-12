"""Bounded private operator console; notices are never diagnostic artifacts."""
import json
import os
import selectors
import signal
import subprocess
import time
from urllib.parse import parse_qsl, urlsplit

from .config import ConfigError
from .layout import unique_fields
from .oauth import SCOPES, redirect_parts


def private_notice(value):
    if type(value) is not dict or set(value) != {'authorization_url', 'instructions'}:
        raise ValueError
    url = urlsplit(value['authorization_url'])
    pairs = parse_qsl(url.query, strict_parsing=True)
    query = dict(pairs)
    if (url.scheme != 'https' or url.netloc != 'osu.ppy.sh'
            or url.path != '/oauth/authorize' or url.fragment
            or len(query) != len(pairs)
            or set(query) != {'client_id', 'redirect_uri', 'response_type', 'scope', 'state'}
            or not query['client_id'].isascii() or not query['client_id'].isdigit()
            or int(query['client_id']) <= 0 or query['response_type'] != 'code'
            or tuple(query['scope'].split()) != SCOPES
            or not 16 <= len(query['state']) <= 128):
        raise ValueError
    parts = redirect_parts(query['redirect_uri'])
    instructions = value['instructions']
    keys = {'execution', 'live_ready', 'mode', 'redirect_uri', 'browser_opened',
            'ssh_started', 'callback_bind', 'callback_port'}
    if (type(instructions) is not dict or instructions.get('mode') not in ('local', 'ssh')
            or set(instructions) != keys | ({'ssh_forward'} if instructions['mode'] == 'ssh' else set())
            or instructions['execution'] != 'provider-live-pilot'
            or any(instructions[k] is not False for k in ('live_ready', 'browser_opened', 'ssh_started'))
            or instructions['redirect_uri'] != query['redirect_uri']
            or instructions['callback_bind'] != '127.0.0.1'
            or type(instructions['callback_port']) is not int or instructions['callback_port'] != parts.port):
        raise ValueError
    if instructions['mode'] == 'ssh' and instructions['ssh_forward'] != {
            'local_bind': '127.0.0.1', 'local_port': parts.port,
            'remote_host': '127.0.0.1', 'remote_port': parts.port}:
        raise ValueError
    return value


def run(command, *, env, identity, notice, timeout=90):
    """Forward one checked notice while the owned child waits for its callback.

    Bound stdout/stderr independently, parse only complete JSON lines, discard
    stderr, and reap this exact child on every failure. Never retry authorization.
    No new session/process group is used; provider children retain parent-death
    cleanup and the installed account/owner transactions retain uncertainty.
    """
    if not callable(notice): raise ConfigError('private operator notice required')
    from .process_target import OwnedChild, require_child_platform
    require_child_platform()
    child = None; target = None
    deadline = time.monotonic() + timeout
    result = None; noticed = False; pending = bytearray(); totals = {'stdout': 0, 'stderr': 0}
    try:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM))
        try:
            def child_identity():
                if identity is not None: identity()
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, cwd='/tmp', preexec_fn=child_identity)
            target = OwnedChild(child.pid)
        finally: signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(child.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise ValueError
                for key, _ in selector.select(min(remaining, .2)):
                    chunk = os.read(key.fd, 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    totals[key.data] += len(chunk)
                    if totals[key.data] > 65536: raise ValueError
                    if key.data == 'stderr': continue
                    pending.extend(chunk)
                    if len(pending) > 32768: raise ValueError
                    while b'\n' in pending:
                        raw, _, rest = pending.partition(b'\n'); pending[:] = rest
                        value = json.loads(raw, object_pairs_hook=unique_fields,
                                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                        if type(value) is not dict: raise ValueError
                        if 'authorization_url' in value:
                            if noticed or result is not None: raise ValueError
                            notice(private_notice(value)); noticed = True
                        else:
                            if result is not None or not noticed: raise ValueError
                            # Only the checked installed workflow's diagnostics
                            # may follow. Notices cannot be smuggled in a result.
                            if 'authorization_url' in json.dumps(value): raise ValueError
                            result = value
            if pending or not noticed or result is None: raise ValueError
            remaining = deadline - time.monotonic()
            if remaining <= 0 or child.wait(timeout=remaining): raise ValueError
            return result
    except Exception:
        raise ConfigError('pilot authorization incomplete; inspect private status before retry') from None
    finally:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM))
        try:
            if target is not None:
                target.kill()
                child.wait(timeout=2)
        finally:
            if target is not None: target.close()
            if child is not None: child.stdout.close(); child.stderr.close()
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
