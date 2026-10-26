"""Root/service-UID cross-process lease qualification in isolated tmpfs."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('provisioning')

import os
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, '/source/validation/maintenance735/source')
from poolrotater_host.layout import LeaseBusy
from poolrotater_host.refresh_fence import LOCK, refresh_fence, update_fence


def kill_service_child(pid):
    # The fixture drops CAP_KILL, so signal as the worker UID itself.
    os.seteuid(1000)
    try:
        os.kill(pid, signal.SIGKILL)
    finally:
        os.seteuid(0)


def main():
    root = Path('/state/refresh-fence-735')
    directory = root / 'state/rolling'
    directory.mkdir(parents=True)
    for path in (root, root / 'state', directory):
        path.chmod(0o700)
        os.chown(path, 1000, 1000)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            os.setgroups([]); os.setgid(1000); os.setuid(1000)
            with refresh_fence(root) as admitted:
                assert admitted
                os.write(write_fd, b'R')
                time.sleep(30)
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 1) == b'R'
        try:
            with update_fence(root, 1000):
                raise AssertionError('update crossed the active worker lease')
        except LeaseBusy:
            pass
        lock = root / LOCK
        assert lock.stat().st_uid == 1000 and lock.stat().st_mode & 0o777 == 0o600
        kill_service_child(pid)
        os.waitpid(pid, 0)
        pid = None
        with update_fence(root, 1000):
            assert os.geteuid() == 0
        assert os.geteuid() == 0
        print('ok: service-owned lease blocks root admission and recovers after worker death')
    finally:
        os.close(read_fd)
        if pid is not None:
            kill_service_child(pid)
            os.waitpid(pid, 0)


if __name__ == '__main__':
    main()
