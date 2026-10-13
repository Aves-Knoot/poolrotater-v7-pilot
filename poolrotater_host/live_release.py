"""Artifact-only pilot release contract; source/rehearsal builds stay fenced.

The builder emits this exact policy only into the live artifact. Protected code
inventory covers it; configuration, credentials and environment cannot supply it.
Installed entry guards must additionally verify the matching registration before
reading authority. Transport children repeat the artifact/execution fence.
"""
import json
import hashlib
import os
from pathlib import Path
import re

from .oauth import AuthError

POLICY_NAME = 'poolrotater_host/pilot_release.json'
VERSION = 'v7.0.1'
INSTALLATION = 'pilot-live'
PROVIDER = 'provider-live-pilot'


def policy():
    return {'format': 1, 'release': VERSION, 'installation': INSTALLATION,
            'provider': PROVIDER, 'maximum_rooms': 4,
            'automatic_authorization': False, 'automatic_updates': False,
            'collection_or_analysis': False}


def policy_bytes():
    return (json.dumps(policy(), sort_keys=True, separators=(',', ':')) + '\n').encode()


def registered_policy_path():
    """Cheap repeat fence after full installed code/environment entry checks.

    A source snapshot containing a policy is insufficient. Only the currently
    registered dedicated service UID and protected fixed installation may use it.
    Every child repeats this read-only binding check before DNS/TLS/socket I/O.
    """
    from .provisioning import protected, _control, _host_identity, _pointer
    from .layout import load
    release = Path(__file__).absolute().parent.parent
    prefix = release.parent.parent
    if (release.parent.name != 'releases' or prefix.parent != Path('/opt/poolrotater-v7-pilot')
            or not re.fullmatch('[a-z][a-z0-9-]{0,15}', prefix.name)
            or not re.fullmatch(r'v7\.0\.1-[a-f0-9]{16}', release.name)
            or os.geteuid() == 0):
        raise ValueError
    protected(release)
    root = Path('/var/lib/poolrotater-v7-pilot') / prefix.name
    marker = _host_identity(root, os.geteuid())
    pointer = _pointer(marker, prefix)
    if _control(root / 'state/runtime-installation.json', os.geteuid(), private=True) != pointer:
        raise ValueError
    record = _control(protected(prefix / 'installations' / (marker['installation_id'] + '.json')))
    if (any(record.get(k) != v for k, v in {**pointer, 'execution': INSTALLATION,
            'service_uid': os.geteuid(), 'host_root': str(root), 'release': str(release)}.items())
            or record['release_snapshot']['files'].get(POLICY_NAME) != hashlib.sha256(policy_bytes()).hexdigest()):
        raise ValueError
    _, config = load(root)
    if config['execution'] != INSTALLATION or not 1 <= len(config['rooms']) <= policy()['maximum_rooms']: raise ValueError
    return protected(release / POLICY_NAME)


def require(execution):
    if execution != PROVIDER:
        raise AuthError('live_provider_disabled')
    try:
        # No search path, alternate policy filename or environment override.
        path = registered_policy_path()
        if path.is_symlink() or path.stat().st_size != len(policy_bytes()):
            raise ValueError
        if path.read_bytes() != policy_bytes(): raise ValueError
    except Exception:
        raise AuthError('live_provider_disabled') from None
