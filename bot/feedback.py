"""Bounded acknowledgments of committed feedback, with verified shared counts.

No network requests, writes, local overlays or additional threads. Gameplay does
not wait for synchronization. During an outage a saved vote is still thanked,
without displaying a stale total as though it includes that vote.
"""
import json
import logging
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

log = logging.getLogger("bot")


class FeedbackReplies:
    LIMIT = 64
    TIMEOUT = 12.0

    def __init__(self, store, *, clock=time.monotonic):
        self.store = store
        self.clock = clock
        self.pending = {}
        self.next_check = 0.0

    def _connect(self):
        connection = sqlite3.connect(
            'file:' + quote(str(Path(self.store.path).absolute())) + '?mode=ro',
            uri=True, timeout=0.05,
        )
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        return connection

    @staticmethod
    def _message(item, tally=None):
        message = f"Thanks for the feedback! {item['title'][:30]}"
        if tally is None:
            return message + f" — noted as {item['label']}."
        return message + f" — {tally[0]} good / {tally[1]} bad."

    def enqueue(self, beatmap_id, user_id, title, label):
        item = {'beatmap_id': beatmap_id, 'user_id': user_id,
                'title': title, 'label': label, 'deadline': self.clock() + self.TIMEOUT}
        if getattr(self.store, 'ruleset_id', None) == 2:
            # The caller invokes this only after its local transaction commits.
            # Catch never waits for an upload, cache, or shared count to thank it.
            return [self._message(item)]
        key = (beatmap_id, user_id)
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    'SELECT event_id FROM rating_outbox WHERE beatmap_id=? AND user_id=? '
                    'ORDER BY sequence DESC LIMIT 1', key,
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                return [self._message(item)]
            item['event_id'] = row['event_id']
        except (sqlite3.Error, OSError, ValueError):
            log.warning('saved feedback acknowledgment could not read its event')
            return [self._message(item)]
        messages = []
        if key not in self.pending and len(self.pending) >= self.LIMIT:
            oldest = next(iter(self.pending))
            messages.append(self._message(self.pending.pop(oldest)))
        # A quick change of opinion replaces its own pending acknowledgment.
        self.pending[key] = item
        return messages

    def drain(self):
        now = self.clock()
        if not self.pending or now < self.next_check:
            return []
        self.next_check = now + 1.0
        rows = {}
        counts = {}
        status = {}
        try:
            connection = self._connect()
            try:
                ids = [item['event_id'] for item in self.pending.values()]
                placeholders = ','.join('?' for _ in ids)
                rows = {row['event_id']: row for row in connection.execute(
                    f'SELECT event_id,phase,acknowledgment FROM rating_outbox '
                    f'WHERE event_id IN ({placeholders})', ids,
                )}
            finally:
                connection.close()
            if any(row['phase'] in ('accepted', 'already_accepted', 'superseded') for row in rows.values()):
                reader = self.store.reader
                counts = reader.tallies([item['beatmap_id'] for item in self.pending.values()])
                status = reader.status
        except Exception:
            # A failed optional cache/display read cannot terminate a worker
            # after its vote transaction has already committed.
            log.debug('saved feedback acknowledgment is waiting for readable totals', exc_info=True)
        messages = []
        for key, item in list(self.pending.items()):
            row = rows.get(item['event_id'])
            ready = False
            terminal = False
            if row is not None:
                try:
                    ack = json.loads(row['acknowledgment']) if row['acknowledgment'] else {}
                    countable = (row['phase'] in ('accepted', 'already_accepted')
                                 or row['phase'] == 'superseded' and ack.get('reason') == 'same_current_value')
                    terminal = row['phase'] not in ('pending', 'retryable') and not countable
                    if countable:
                        watermark = ack['watermark']
                        ready = (type(watermark) is int and watermark > 0
                                 and status.get('phase') in ('current', 'last-good', 'previous')
                                 and status.get('watermark', 0) >= watermark)
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass
            if ready:
                # A verified aggregate can legitimately have no row, e.g. a
                # later policy suppression. Never add the local vote to it.
                messages.append(self._message(item, counts.get(item['beatmap_id'], (0, 0))))
            elif terminal or now >= item['deadline']:
                messages.append(self._message(item))
            else:
                continue
            del self.pending[key]
        return messages
