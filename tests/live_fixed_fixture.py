"""Spawn-safe test seam: real fixed workers, invented TLS and loopback DNS only.

Never shipped in runtime payloads. The real worker receives its original fixed
host/port, verifies that origin and TLS hostname, and repeats the release fence.
Only libc address resolution is replaced within this isolated test child.
"""
from dataclasses import dataclass
import os
import socket
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack


@dataclass(frozen=True)
class FixedWorker:
    port: int
    policy_module: str
    referee: bool = False
    mock_registration: bool = True

    def __call__(self, *args):
        if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1':
            raise RuntimeError('isolated fixed-worker fixture required')
        from poolrotater_host import live_release, provider_transport, referee_wire
        resolver = socket.getaddrinfo

        def loopback(host, port, *other, **kwargs):
            if host not in ('osu.ppy.sh', 'spectator.ppy.sh') or port != 443:
                raise RuntimeError('fixture refused unexpected network destination')
            return resolver('127.0.0.1', self.port, *other, **kwargs)

        with ExitStack() as stack:
            if self.mock_registration:
                stack.enter_context(patch.object(live_release, '__file__', self.policy_module))
                stack.enter_context(patch.object(live_release, 'registered_policy_path',
                    return_value=Path(self.policy_module).with_name('pilot_release.json')))
            stack.enter_context(patch('socket.getaddrinfo', side_effect=loopback))
            worker = referee_wire._worker if self.referee else provider_transport._worker
            return worker(*args)


def fixed_entry(pipe, fixture, *args):
    """Function entry for native bounded spawn; fixture arguments use its IPC."""
    if type(fixture) is not FixedWorker:
        raise RuntimeError('fixed fixture target refused')
    return fixture(pipe, *args)


_native_start = None


def fixed_startup_adapter():
    global _native_start
    from poolrotater_host.process_startup import start
    if start is not start_with_fixed:
        _native_start = start
    return start_with_fixed


def start_with_fixed(target, args, deadline, *, name='provider-wire'):
    """Adapt invented DNS targets without bypassing native startup checks."""
    if type(target) is FixedWorker:
        return _native_start(fixed_entry, (target, *args), deadline, name=name)
    return _native_start(target, args, deadline, name=name)
