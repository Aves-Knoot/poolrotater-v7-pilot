"""v7 private journal writer; inherited ownership startup refusal is preserved."""
import copy
import threading
import time

from rolling.state import Registry
from .layout import atomic_json


class PrivateRegistry(Registry):
    def __init__(self, original):
        # Created under the host lease before any worker/room operation. The
        # original supervisor has already required a clean startup snapshot.
        self.path = original.path
        self.data = original.snapshot()
        self.lock = threading.RLock()

    def record(self, alias, **values):
        with self.lock:
            new = copy.deepcopy(self.data)
            new['rooms'].setdefault(alias, {}).update(values, updated_at=time.time())
            atomic_json(self.path, new)
            self.data = new
