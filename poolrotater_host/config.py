"""Strict, secret-free configuration for the offline v7 host foundation."""
from __future__ import annotations

import math
import re
from pathlib import Path, PurePosixPath

PRESETS = {
    'standard-auto': {'ruleset': 0, 'keys': None, 'auto_difficulty': True, 'sr_min': 4.5, 'sr_max': 5.5},
    'standard-fixed': {'ruleset': 0, 'keys': None, 'auto_difficulty': False, 'sr_min': 4.5, 'sr_max': 5.5},
    'mania-4k': {'ruleset': 3, 'keys': 4, 'auto_difficulty': False, 'sr_min': 2.5, 'sr_max': 4.5},
    'mania-7k': {'ruleset': 3, 'keys': 7, 'auto_difficulty': False, 'sr_min': 2.5, 'sr_max': 4.5},
}
LANGUAGES = ('en', 'zh', 'ko', 'es', 'ru', 'ja', 'pl', 'id', 'fr', 'de')


class ConfigError(ValueError):
    pass


def fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ConfigError(f'{label}: unexpected or missing fields')


def relative_path(value: str) -> Path:
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ConfigError('path must be a nonempty relative POSIX path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')):
        raise ConfigError('absolute paths and traversal are forbidden')
    return Path(*path.parts)


def contained(root: Path, value: str) -> Path:
    path = root
    for component in relative_path(value).parts:
        path = path / component
        if path.is_symlink():
            raise ConfigError('symlinks are forbidden in host-managed paths')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ConfigError('path escapes host root')
    return path


def number(value, low, high, label):
    if type(value) not in (float, int) or not low <= value <= high or not math.isfinite(value):
        raise ConfigError(f'{label}: expected finite number in {low}..{high}')


def validate(config: dict) -> dict:
    fields(config, ('schema_version', 'execution', 'network', 'updates', 'account', 'rooms'), 'host')
    if type(config['schema_version']) is not int or config['schema_version'] != 1:
        raise ConfigError('unsupported configuration schema')
    if config['execution'] not in ('offline', 'pilot-candidate', 'pilot-qualification', 'pilot-live'):
        raise ConfigError('unsupported host execution identity')
    fields(config['network'], ('enabled', 'share_ratings', 'public_directory', 'share_chat'), 'network')
    if any(v is not False for v in config['network'].values()):
        raise ConfigError('network integrations are not implemented in the offline foundation')
    fields(config['updates'], ('channel', 'automatic'), 'updates')
    if config['updates']['channel'] != 'preview' or config['updates']['automatic'] is not False:
        raise ConfigError('offline foundation requires manual preview updates')
    fields(config['account'], ('expected_user_id', 'client_id_env', 'client_secret_env', 'token_file'), 'account')
    account = config['account']
    if account['expected_user_id'] is not None and (type(account['expected_user_id']) is not int or account['expected_user_id'] <= 0):
        raise ConfigError('expected_user_id must be null or a positive integer')
    for key in ('client_id_env', 'client_secret_env'):
        if not isinstance(account[key], str) or not re.fullmatch(r'POOLROTATER_[A-Z][A-Z0-9_]{0,60}', account[key]):
            raise ConfigError('credential environment names must use the POOLROTATER_ prefix')
    if account['client_id_env'] == account['client_secret_env']:
        raise ConfigError('credential environment names must be distinct')
    if account['token_file'] != 'secrets/osu-token.json':
        raise ConfigError('token_file must use the private host-local location')
    rooms = config['rooms']
    if not isinstance(rooms, list) or not 1 <= len(rooms) <= 4:
        raise ConfigError('configure between one and four rooms')
    if config['execution'] == 'pilot-live' and len(rooms) != 1:
        raise ConfigError('live pilot requires exactly one room')
    aliases = set()
    for room in rooms:
        fields(room, ('alias', 'preset', 'sr_min', 'sr_max', 'language', 'catalog'), 'room')
        alias = room['alias']
        if not isinstance(alias, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,23}', alias) or alias in aliases:
            raise ConfigError('room aliases must be unique lowercase identifiers')
        aliases.add(alias)
        if not isinstance(room['preset'], str) or room['preset'] not in PRESETS:
            raise ConfigError('unsupported room preset')
        if room['language'] not in LANGUAGES:
            raise ConfigError('unsupported language')
        number(room['sr_min'], 0, 20, 'sr_min'); number(room['sr_max'], 0, 20, 'sr_max')
        if room['sr_min'] >= room['sr_max']:
            raise ConfigError('star-rating minimum must be below maximum')
        path = relative_path(room['catalog'])
        if path.parts[0] != 'catalogs' or path.suffix != '.db':
            raise ConfigError('catalog must be a .db beneath catalogs/')
    return config


def defaults(presets: list[str] | None = None) -> dict:
    presets = list(PRESETS) if presets is None else presets
    rooms = []
    for index, name in enumerate(presets, 1):
        if name not in PRESETS:
            raise ConfigError('unsupported preset')
        spec = PRESETS[name]
        filename = 'standard.db' if spec['ruleset'] == 0 else f'mania_{spec["keys"]}k.db'
        rooms.append({'alias': f'room{index}', 'preset': name, 'sr_min': spec['sr_min'],
                      'sr_max': spec['sr_max'], 'language': 'en', 'catalog': f'catalogs/{filename}'})
    return validate({'schema_version': 1, 'execution': 'offline',
                     'network': {'enabled': False, 'share_ratings': False, 'public_directory': False, 'share_chat': False},
                     'updates': {'channel': 'preview', 'automatic': False},
                     'account': {'expected_user_id': None, 'client_id_env': 'POOLROTATER_CLIENT_ID',
                                 'client_secret_env': 'POOLROTATER_CLIENT_SECRET', 'token_file': 'secrets/osu-token.json'},
                     'rooms': rooms})
