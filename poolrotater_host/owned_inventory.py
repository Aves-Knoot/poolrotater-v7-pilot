"""Bounded, read-only API room history; not authority to clear unknown creation.

The legacy API returns arrays. Its reviewed cursor is (pinned, id), both in
descending order. Request the terminal empty page rather than inferring
exhaustion from an arbitrary short response or an old room ID.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from urllib.parse import urlencode

from .oauth import AuthError

PAGE_SIZE = 50
MAX_PAGES = 128
MAX_BYTES = 32 * 1024**2
MAX_SECONDS = 300.
HISTORY_PATH = '/api/v2/rooms?' + urlencode({
    'mode': 'owned', 'type_group': 'realtime', 'limit': PAGE_SIZE, 'sort': 'created'})
_CURSOR = re.compile(re.escape(HISTORY_PATH) +
    r'(?:&cursor%5Bpinned%5D=([01])&cursor%5Bid%5D=([1-9][0-9]{0,18}))?\Z')


def history_path(cursor=None):
    if cursor is None:
        return HISTORY_PATH
    if (type(cursor) is not dict or set(cursor) != {'pinned', 'id'}
            or type(cursor['pinned']) is not bool or type(cursor['id']) is not int
            or not 0 < cursor['id'] < 10**19):
        raise AuthError('ownership_inventory_request_refused')
    return HISTORY_PATH + '&' + urlencode({
        'cursor[pinned]': int(cursor['pinned']), 'cursor[id]': cursor['id']})


def history_cursor(path):
    """Only canonical finite cursor paths enter the fixed-provider transport."""
    match = _CURSOR.fullmatch(path) if type(path) is str else None
    if match is None:
        raise AuthError('ownership_inventory_request_refused')
    return None if match[1] is None else {'pinned': match[1] == '1', 'id': int(match[2])}


def is_history_path(path):
    try:
        history_cursor(path)
        return True
    except AuthError:
        return False


def _date(value):
    if type(value) is not str or len(value) > 40:
        raise AuthError('ownership_inventory_refused')
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise AuthError('ownership_inventory_refused') from None


def _project(value, user_id):
    from .reconciliation import _room
    room = _room(value, user_id)
    if type(value.get('pinned')) is not bool:
        raise AuthError('ownership_inventory_refused')
    return {**room, 'pinned': value['pinned'], 'starts_at': _date(value.get('starts_at')),
            'ends_at': None if value.get('ends_at') is None else _date(value['ends_at'])}


def collect(fetch, user_id, *, clock=time.monotonic):
    if not callable(fetch) or type(user_id) is not int or not 0 < user_id < 10**19:
        raise AuthError('ownership_inventory_request_refused')
    started = clock()
    cursor = None
    seen = set()
    previous = None
    rows = []
    transferred = 0
    for number in range(1, MAX_PAGES + 1):
        now = clock()
        if now < started or now - started >= MAX_SECONDS:
            raise AuthError('ownership_inventory_incomplete')
        result = fetch(history_path(cursor))
        if (type(result) is not tuple or len(result) != 2
                or type(result[0]) is not int or not 100 <= result[0] <= 599):
            raise AuthError('ownership_inventory_unavailable')
        if result[0] != 200:
            raise AuthError('ownership_inventory_unavailable', http_status=result[0])
        body = result[1]
        if type(body) is not list or len(body) > PAGE_SIZE:
            raise AuthError('ownership_inventory_refused')
        try:
            transferred += len(json.dumps(body, allow_nan=False).encode('utf-8'))
        except (TypeError, ValueError, OverflowError):
            raise AuthError('ownership_inventory_refused') from None
        finished = clock()
        if transferred > MAX_BYTES or finished < now or finished - started >= MAX_SECONDS:
            raise AuthError('ownership_inventory_incomplete')
        if not body:
            raw = json.dumps(rows, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            return {'format': 1, 'scope': 'owned-non-deleted-realtime-api-history',
                    'history_complete': True, 'requests': number, 'page_size': PAGE_SIZE,
                    'rooms': rows, 'rooms_sha256': hashlib.sha256(raw).hexdigest(),
                    'recovery_applied': False, 'start_allowed': False,
                    'absence_recovery_qualified': False}
        for value in body:
            projected = _project(value, user_id)
            identity = projected['room_id']
            order = (int(projected['pinned']), identity)
            if identity in seen or previous is not None and order >= previous:
                raise AuthError('ownership_inventory_refused')
            rows.append(projected)
            seen.add(identity)
            previous = order
        cursor = {'pinned': rows[-1]['pinned'], 'id': rows[-1]['room_id']}
    raise AuthError('ownership_inventory_incomplete')
