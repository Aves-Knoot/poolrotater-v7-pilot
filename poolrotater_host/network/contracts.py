"""Closed, bounded M3 wire contracts. Unknown/private fields are errors."""
import hashlib
import json
import math
import re
import secrets
import uuid

MAX_BODY = 65536
MAX_SEQUENCE = 2**53 - 1
PRESETS = ('standard-auto', 'standard-fixed', 'mania-4k', 'mania-7k')
SCOPES = ('heartbeat', 'rotate', 'leave')


class NetworkError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def require(condition, code='invalid_request', status=400):
    if not condition: raise NetworkError(code, status)


def canonical(value):
    try: return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError): raise NetworkError('invalid_json') from None


def fingerprint(value): return hashlib.sha256(canonical(value)).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_field'); result[key] = value
    return result


def decode(raw, *, maximum=MAX_BODY):
    require(type(maximum) is int and 0 < maximum <= 512*1024, 'invalid_body_limit')
    require(type(raw) is bytes and len(raw) <= maximum, 'request_too_large', 413)
    try: return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: require(False, 'invalid_json'))
    except (ValueError, UnicodeError): raise NetworkError('invalid_json') from None


def fields(value, expected):
    require(type(value) is dict and set(value) == set(expected), 'unexpected_fields')


def identifier(value):
    require(type(value) is str and len(value) == 36, 'invalid_identity')
    try: require(str(uuid.UUID(value)) == value, 'invalid_identity')
    except ValueError: raise NetworkError('invalid_identity') from None
    return value


def integer(value, minimum=1, maximum=MAX_SEQUENCE):
    require(type(value) is int and minimum <= value <= maximum, 'invalid_integer')
    return value


def number(value, minimum=0, maximum=MAX_SEQUENCE):
    require(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum, 'invalid_number')
    return value


def text(value, maximum=160):
    require(type(value) is str and 0 < len(value) <= maximum and not any(ord(c) < 32 or ord(c) == 127 for c in value), 'invalid_text')
    return value


def secret(prefix='prn_'): return prefix + secrets.token_urlsafe(32)


def verifier(value, prefix='prn_'):
    require(type(value) is str and re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9_-]{43}', value), 'invalid_credential', 401)
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def base(value):
    require(type(value.get('protocol_version')) is int and value['protocol_version'] == 1, 'unsupported_protocol', 409)


def identity(value):
    for key in ('installation_id', 'generation', 'operator_id'): identifier(value[key])
    integer(value['account_id'])
    require(type(value['directory']) is bool, 'invalid_sharing_choice')


def enrollment(value):
    fields(value, ('protocol_version', 'request_id', 'invitation', 'credential', 'installation_id', 'generation', 'operator_id', 'account_id', 'directory'))
    base(value); identifier(value['request_id']); identity(value)
    verifier(value['invitation'], 'pri_'); verifier(value['credential'])
    return value


def rotation(value):
    fields(value, ('protocol_version', 'request_id', 'credential'))
    base(value); identifier(value['request_id']); verifier(value['credential'])
    return value


def room(value):
    fields(value, ('alias', 'osu_room_id', 'preset', 'state', 'occupants', 'visibility', 'title', 'min_stars', 'max_stars', 'observed_at', 'code_version', 'catalog_version', 'current_map'))
    require(type(value['alias']) is str and re.fullmatch('[a-z][a-z0-9_-]{0,23}', value['alias']), 'invalid_alias')
    integer(value['osu_room_id']); require(value['preset'] in PRESETS, 'invalid_preset')
    require(value['state'] in ('idle', 'starting', 'playing', 'draining', 'unavailable'), 'invalid_room_state')
    integer(value['occupants'], 0, 128); require(value['visibility'] == 'public', 'private_room_refused')
    text(value['title']); number(value['min_stars'], 0, 20); number(value['max_stars'], 0, 20)
    require(value['min_stars'] <= value['max_stars'], 'invalid_star_range'); number(value['observed_at'])
    versions(value)
    if value['current_map'] is not None:
        fields(value['current_map'], ('beatmap_id', 'title', 'stars'))
        integer(value['current_map']['beatmap_id']); text(value['current_map']['title']); number(value['current_map']['stars'], 0, 20)
    return value


def versions(value):
    require(type(value['code_version']) is str and re.fullmatch(r'[A-Za-z0-9.+-]{1,64}', value['code_version']), 'invalid_version')
    require(type(value['catalog_version']) is str and (value['catalog_version'] == 'unavailable' or re.fullmatch('[a-f0-9]{64}', value['catalog_version'])), 'invalid_catalog_version')


def heartbeat(value):
    fields(value, ('protocol_version', 'installation_id', 'generation', 'sequence', 'code_version', 'catalog_version', 'rooms'))
    base(value); identifier(value['installation_id']); identifier(value['generation']); integer(value['sequence'], 0); versions(value)
    require(type(value['rooms']) is list and len(value['rooms']) <= 4, 'room_limit')
    for row in value['rooms']: room(row)
    require(len({r['alias'] for r in value['rooms']}) == len(value['rooms']), 'duplicate_room_alias')
    require(len({r['osu_room_id'] for r in value['rooms']}) == len(value['rooms']), 'duplicate_room_id')
    return value
