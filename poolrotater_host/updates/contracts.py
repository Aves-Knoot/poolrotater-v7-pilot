"""Closed release wire contracts, independent from catalogs and player data."""
import hashlib
import json
import math
import re
import uuid

from ..config import ConfigError

MAX_METADATA = 256 * 1024
MAX_ARTIFACT = 128 * 1024**2
CHANNELS = ('pilot', 'stable')


class UpdateError(ConfigError):
    """Only locally enumerated codes are exposed in update diagnostics."""


def require(condition, code):
    if not condition:
        raise UpdateError(code)


def fields(value, names):
    require(type(value) is dict and set(value) == set(names), 'update_fields_refused')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def decode(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_METADATA, 'update_metadata_size_refused')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'update_duplicate_field')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: require(False, 'update_invalid_json'))
    except (ValueError, UnicodeError):
        raise UpdateError('update_invalid_json') from None


def integer(value, minimum=1, maximum=2**53-1):
    require(type(value) is int and minimum <= value <= maximum, 'update_integer_refused')
    return value


def timestamp(value):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
            'update_clock_refused')
    return value


def digest(value):
    require(type(value) is str and re.fullmatch('[a-f0-9]{64}', value) is not None,
            'update_digest_refused')
    return value


def identifier(value):
    try:
        require(type(value) is str and str(uuid.UUID(value)) == value, 'update_identity_refused')
    except (ValueError, AttributeError):
        raise UpdateError('update_identity_refused') from None
    return value


def version(value):
    require(type(value) is str and re.fullmatch(r'v[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}', value),
            'update_version_refused')
    return tuple(map(int, value[1:].split('.')))


def release(value):
    fields(value, ('sequence', 'version', 'kind', 'artifact', 'from_sha256',
                   'manifest_sha256', 'execution', 'rollback_safe'))
    integer(value['sequence']); version(value['version'])
    require(value['kind'] in ('worker', 'controller'), 'update_kind_refused')
    fields(value['artifact'], ('sha256', 'bytes'))
    digest(value['artifact']['sha256']); integer(value['artifact']['bytes'], 1, MAX_ARTIFACT)
    require(type(value['from_sha256']) is list and 1 <= len(value['from_sha256']) <= 16
            and len(set(value['from_sha256'])) == len(value['from_sha256']), 'update_baseline_refused')
    for item in value['from_sha256']: digest(item)
    digest(value['manifest_sha256'])
    require(value['execution'] in ('pilot-live', 'pilot-candidate', 'offline-fixture'),
            'update_execution_refused')
    require(type(value['rollback_safe']) is bool, 'update_rollback_policy_refused')
    return value


def root(value):
    fields(value, ('format', 'purpose', 'repository_id', 'expires_at', 'threshold', 'keys'))
    require(type(value['format']) is int and value['format'] == 1
            and value['purpose'] == 'poolrotater-release-root', 'update_root_refused')
    identifier(value['repository_id']); integer(value['expires_at'])
    require(type(value['keys']) is dict and 2 <= len(value['keys']) <= 8,
            'update_root_keys_refused')
    integer(value['threshold'], 2, len(value['keys']))
    for keyid, public in value['keys'].items():
        digest(keyid); digest(public)
        require(hashlib.sha256(bytes.fromhex(public)).hexdigest() == keyid, 'update_key_identity_refused')
    return value


def index(value):
    fields(value, ('format', 'purpose', 'repository_id', 'sequence', 'issued_at',
                   'expires_at', 'channels', 'releases'))
    require(type(value['format']) is int and value['format'] == 1
            and value['purpose'] == 'poolrotater-release-index', 'update_index_refused')
    identifier(value['repository_id']); integer(value['sequence'])
    integer(value['issued_at']); integer(value['expires_at'])
    require(0 < value['expires_at']-value['issued_at'] <= 7*86400, 'update_expiry_window_refused')
    require(type(value['releases']) is list and len(value['releases']) <= 128,
            'update_release_budget_exceeded')
    seen = set(); sequences = set()
    for item in value['releases']:
        release(item); sha = item['artifact']['sha256']
        require(sha not in seen and item['sequence'] not in sequences, 'update_release_conflict')
        seen.add(sha); sequences.add(item['sequence'])
    require(type(value['channels']) is dict and set(value['channels']) <= set(CHANNELS),
            'update_channel_refused')
    for sha in value['channels'].values():
        digest(sha); require(sha in seen, 'update_channel_target_absent')
    return value
