"""Private provenance for the provider supervisor's existing ownership journal."""
import os
from pathlib import Path

from .config import contained
from .layout import atomic_json, load, read_json
from .oauth import AuthError

BINDING_PATH = 'state/provider-room-binding.json'
JOURNAL_PATH = 'state/rolling/ownership.json'
RECOVERY_PATH = 'state/ownership-recovery.json'


def binding(root, authority, user_id):
    marker, _ = load(root)
    return {'format': 1, 'execution': authority['execution'], 'host_root': str(Path(root)),
            'uid': os.geteuid(), **{k: marker[k] for k in ('installation_id', 'generation')},
            'client_id': authority['client_id'], 'user_id': user_id,
            'client_profile_sha256': authority['client_profile_sha256']}


def require_namespace(root, expected):
    """Read-only. Missing/foreign provenance never adopts an existing journal."""
    paths = [contained(root, name) for name in (BINDING_PATH, JOURNAL_PATH, RECOVERY_PATH)]
    present = [os.path.lexists(path) for path in paths]
    if not any(present): return False
    if not present[0]: raise AuthError('unbound_authority_requires_migration')
    try:
        value = read_json(paths[0], maximum=4096)
        if (type(value) is not dict or set(value) != set(expected)
                or any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items())):
            raise ValueError
        if not present[1]: raise ValueError
        # Validate private journal bytes and shape even before a controller exists.
        from .reconciliation import _journal, journal_path
        _, config = load(root)
        _journal(read_json(journal_path(root)), {room['alias'] for room in config['rooms']})
    except Exception: raise AuthError('provider_room_binding_requires_review') from None
    return True


def enroll(root, expected):
    """Caller holds the verified owner lease; no tokens or room operations here."""
    if require_namespace(root, expected): return
    directory = contained(root, 'state/rolling')
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_uid != os.geteuid() or directory.stat().st_mode & 0o077:
        raise AuthError('provider_room_binding_requires_review')
    # An interrupted first write is deliberately unbound and requires review.
    # It cannot be mistaken for an empty old journal and automatically repaired.
    atomic_json(contained(root, JOURNAL_PATH), {'format': 1, 'rooms': {}})
    atomic_json(contained(root, BINDING_PATH), expected)
