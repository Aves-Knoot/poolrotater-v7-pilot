"""Serialize shared JSON edits across the v6.7 Linux room processes."""
from functools import wraps
import logging
import time


def serialized_config(attribute):
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            if not self.cfg.get("_rolling_worker"):
                return method(self, *args, **kwargs)
            import fcntl  # Rolling supervisor is Linux-only; legacy launcher is unchanged.
            path = getattr(self, attribute)
            try:
                if self.cfg.get('_host_root'):
                    from pathlib import Path
                    from poolrotater_host.config import contained
                    from poolrotater_host.layout import file_lease, load
                    root = Path(self.cfg['_host_root'])
                    load(root)
                    relative = path.relative_to(root).as_posix()
                    checked = contained(root, relative + '.lock')
                    with file_lease(checked):
                        return method(self, *args, **kwargs)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.with_name(path.name + ".lock").open("a") as lock:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("configuration writer is busy")
                            time.sleep(.02)
                    return method(self, *args, **kwargs)
            except (OSError, ValueError) as exc:
                logging.getLogger("bot").error("could not lock config %s: %s", path, exc)
                return False
        return wrapped
    return decorate
