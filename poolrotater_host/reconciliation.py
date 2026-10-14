"""Read-only ownership recovery preparation. Never grants startup authority."""
import hashlib
import json
import os
import stat
from datetime import datetime, timezone

from .config import contained
from .layout import load, read_json
from .oauth import AuthError, OfflineOAuth
from .provider_contract import OWNED_PATH, ROOM_LIMIT


def _room(value, user_id, room_id=None):
    if (not isinstance(value, dict) or type(value.get('id')) is not int or not 0 < value['id'] < 10**19
            or type(value.get('user_id')) is not int or value['user_id'] != user_id
            or (room_id is not None and value['id'] != room_id)
            or value.get('type') not in ('head_to_head', 'team_versus')
            or type(value.get('active')) is not bool):
        raise AuthError('ownership_room_evidence_refused')
    # Project only identity/activity: names, passwords, players and chat omitted.
    ended = False
    if value.get('ends_at') is not None:
        try:
            end = value['ends_at']
            if not isinstance(end, str) or len(end) > 40: raise ValueError
            end = datetime.fromisoformat(end)
            if end.tzinfo is None: raise ValueError
            ended = not value['active'] and end <= datetime.now(timezone.utc)
        except (ValueError, OverflowError): raise AuthError('ownership_room_evidence_refused') from None
    return {'room_id': value['id'], 'user_id': user_id, 'active': value['active'], 'ended': ended}


def _journal(value, aliases):
    if (not isinstance(value, dict) or set(value) != {'format', 'rooms'}
            or type(value['format']) is not int or value['format'] != 1
            or not isinstance(value['rooms'], dict) or len(value['rooms']) > 4):
        raise AuthError('ownership_journal_refused')
    try: json.dumps(value, allow_nan=False)
    except (ValueError, TypeError): raise AuthError('ownership_journal_refused') from None
    ids = set()
    for alias, state in value['rooms'].items():
        if alias not in aliases or not isinstance(state, dict): raise AuthError('ownership_journal_refused')
        rid = state.get('room_id')
        if rid is not None:
            if type(rid) is not int or not 0 < rid < 10**19 or rid in ids:
                raise AuthError('ownership_journal_refused')
            ids.add(rid)
        if 'ownership_unknown' in state and type(state['ownership_unknown']) is not bool:
            raise AuthError('ownership_journal_refused')
        if 'operation' in state and state['operation'] not in ('make_pending', 'owned', 'close_pending', 'closed'):
            raise AuthError('ownership_journal_refused')
    return value


def journal_path(root):
    path = contained(root, 'state/rolling/ownership.json')
    try:
        parent = path.parent.stat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) & 0o077): raise ValueError
    except (OSError, ValueError): raise AuthError('ownership_journal_refused') from None
    return path


class OfflineOwnershipReview:
    execution = 'offline-fixture'

    def __init__(self, authentication):
        if (os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1'
                or not isinstance(authentication, OfflineOAuth)
                or getattr(authentication.transport, 'offline_fixture', False) is not True):
            raise AuthError('offline_fixture_required')
        self.auth = authentication
        self.root = authentication.root

    def inspect(self):
        """Observe at most one list and four known IDs under the verified lease.

        Listing omission and 404 never establish absence. Even a known ended room
        is only a proposal for a later stopped, atomic recovery transaction.
        """
        with self.auth.verified_owner() as account:
            return self._inspect(account)

    def _read_journal(self, aliases):
        try: return _journal(read_json(journal_path(self.root)), aliases)
        except FileNotFoundError: raise AuthError('ownership_journal_missing') from None
        except AuthError: raise
        except Exception: raise AuthError('ownership_journal_refused') from None

    def _inspect(self, account, *, journal=None):
        """Internal assessment while the caller holds verified_owner."""
        _, config = load(self.root)
        aliases = {r['alias'] for r in config['rooms']}
        def read():
            return self._read_journal(aliases)
        observed = read()
        journal = observed if journal is None else _journal(journal, aliases)
        digest = hashlib.sha256(json.dumps(journal, sort_keys=True, allow_nan=False).encode()).hexdigest()
        refreshed = False
        def fetch(endpoint):
            nonlocal refreshed
            token = self.auth.access_token()
            status, body = self.auth._request('GET', endpoint, token=token)
            if status == 401 and not refreshed:
                refreshed = True
                self.auth.invalidate_access_token(token)
                status, body = self.auth._request('GET', endpoint, token=self.auth.access_token())
            return status, body
        status, body = fetch(OWNED_PATH)
        if status != 200: raise AuthError('ownership_listing_unavailable')
        if not isinstance(body, list) or len(body) > ROOM_LIMIT:
            raise AuthError('ownership_listing_refused')
        listed = [_room(v, account.user_id) for v in body]
        if len({v['room_id'] for v in listed}) != len(listed): raise AuthError('ownership_listing_refused')
        known = {state['room_id'] for state in journal['rooms'].values() if state.get('room_id') is not None}
        reviews = []
        for alias, state in sorted(journal['rooms'].items()):
            rid = state.get('room_id')
            uncertain = state.get('ownership_unknown', False) or state.get('operation') in ('make_pending', 'close_pending')
            result = 'unknown_creation' if uncertain and rid is None else 'local_clean'
            if rid is not None:
                code, value = fetch('/api/v2/rooms/' + str(rid))
                if code == 200:
                    detail = _room(value, account.user_id, rid)
                    listed_detail = next((v for v in listed if v['room_id'] == rid), None)
                    result = 'evidence_changed' if listed_detail and listed_detail != detail else (
                        'surviving_room' if detail['active'] else
                        'confirmed_ended' if detail['ended'] else 'inactive_unconfirmed')
                else: result = 'unconfirmed_room'
            # A make_pending record can still contain the previous room ID.
            # Proving that old room ended does not identify the lost creation.
            if state.get('operation') == 'make_pending': result = 'unknown_creation'
            reviews.append({'alias': alias, 'room_id': rid, 'result': result})
        if read() != observed: raise AuthError('ownership_journal_changed')
        return {'format': 1, 'execution': self.execution, 'live_ready': False,
                'user_id': account.user_id, 'journal_sha256': digest, 'journal_unchanged': True,
                'rooms': reviews, 'untracked_active_room_ids': sorted(
                    v['room_id'] for v in listed if v['active'] and v['room_id'] not in known),
                'untracked_unconfirmed_room_ids': sorted(
                    v['room_id'] for v in listed if not v['ended'] and v['room_id'] not in known),
                'listing_exhaustive': False, 'listing_at_limit': len(listed) == ROOM_LIMIT,
                'start_allowed': False, 'recovery_applied': False,
                'reason': 'read_only_preparation_requires_stopped_recovery_and_upstream_referee_qualification'}
