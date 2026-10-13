"""Bounded local operator-policy reads; never fetch data or mutate catalogs."""
import json
import logging
import time
from pathlib import Path

log = logging.getLogger('exclusions')


class ExclusionList:
    def __init__(self, path):
        self.path = Path(path)
        self.ids = frozenset()
        self.valid = False
        self.signature = None
        self.next_check = 0.0
        self.last_error = None

    def refresh(self):
        now = time.monotonic()
        if now < self.next_check:
            return False
        self.next_check = now + 2.0
        try:
            stat = self.path.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            if signature == self.signature:
                return False
            with self.path.open('rb') as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('exclusion policy exceeds 1 MiB')
            data = json.loads(raw)
            if (not isinstance(data, dict) or type(data.get('schema')) is not int
                    or data['schema'] != 1 or not isinstance(data.get('exclusions'), list)):
                raise ValueError('expected schema 1 with an exclusions list')
            ids = set()
            for row in data['exclusions']:
                if not isinstance(row, dict):
                    raise ValueError('each exclusion must be an object')
                bid = row.get('beatmap_id')
                if type(bid) is not int or bid <= 0:
                    raise ValueError('beatmap_id must be a positive integer')
                ids.add(bid)
            changed = not self.valid or self.ids != ids
            self.ids = frozenset(ids)
            self.valid = True
            self.signature = signature
            self.last_error = None
            return changed
        except (OSError, ValueError, TypeError) as error:
            message = str(error)
            if message != self.last_error:
                log.warning('Exclusion list unavailable/invalid at %s; retaining last valid policy: %s', self.path, error)
                self.last_error = message
            return False
