"""Bridge strict host configuration to the existing room/controller settings.

Rendering is stdlib-only and read-only. Materialization is a development runtime
operation performed under the host owner lease. Native edits win for fields they
change; unrelated gameplay preferences saved by a worker survive refresh/restart.
"""
from __future__ import annotations

from contextlib import ExitStack
import copy
import threading
from pathlib import Path

from . import __version__
from .catalogs import make_pool
from bot.custom_greetings import DEFAULT_JOIN_GREETINGS
from .config import ConfigError, PRESETS, contained
from .layout import atomic_json, checked_root, file_lease, load, read_json
from .layout import DIRECTORIES
from .catalog_store import resolved_catalogs, settings_for, POINTER
from .owner_settings import GAMEPLAY_KEYS, RECORD as GAMEPLAY_RECORD, load_overrides, validate_settings
from . import owner_integrations

EDITABLE_BASE = {'sr_min', 'sr_max', 'auto_difficulty', 'default_response_language'}
# Native permission commands and the existing hot-loader own these room-local
# lists. Identity, transport, credentials and Discord role routing stay pinned.
EDITABLE_STAFF = frozenset({'admins', 'moderators'})
STATE_FILE = 'state/runtime-config.json'
ROOM_DIRECTORY = 'config/rooms'
from .managed_host.settings import ROOM_JSON_LIMIT,HOST_JSON_LIMIT


def default_room_title(preset):
    return {'standard-auto':'AutoHost|Type .help|', 'standard-fixed':'AutoHost|Type .help|',
            'mania-4k':'AutoHost 4K|Type .help|', 'mania-7k':'AutoHost 7K|Type .help|',
            'catch-fixed':'PoolRotater | Catch', 'taiko-fixed':'AutoHost Taiko|Type .help|', 'taiko-auto':'AutoHost Taiko|Type .help|', 'standard-loved':'AutoHost Loved|Type .help|'}[preset]


def render(root: Path, *, account_id=None, room_alias=None, catalog_reference=None, _preparation=None) -> dict:
    root = checked_root(root)
    marker, original_config = load(root)
    from .managed_host.projection import configuration as managed_configuration,read as managed_projection,validate_document
    if _preparation is None:
        shadow=managed_projection(root,marker)
        gameplay=load_overrides(root,marker,original_config)
        integrations=owner_integrations.load(root,marker,original_config)
    else:
        # Pure planning of a stopped, root-pinned topology transaction. No CLI
        # or network operation accepts this internal render context.
        from .config import validate as validate_host
        from .owner_settings import validate_record
        if type(_preparation) is not dict or set(_preparation)!={'host','gameplay','integrations','managed'}:
            raise ConfigError('invalid topology preparation context')
        planned=validate_host(copy.deepcopy(_preparation['host']))
        if any(planned[k]!=original_config[k] for k in original_config if k!='rooms'):
            raise ConfigError('topology preparation changed host authority')
        original_config=planned
        gameplay=(validate_record(_preparation['gameplay'],marker,planned)['rooms']
                  if _preparation['gameplay'] is not None else {})
        integrations=(owner_integrations.validate_record(_preparation['integrations'],marker,planned)
                      if _preparation['integrations'] is not None else None)
        shadow=validate_document(_preparation['managed'],marker)
    config = managed_configuration(root,marker,original_config,shadow=shadow)
    expected = config['account']['expected_user_id']
    if account_id is not None:
        if type(account_id) is not int or account_id <= 0:
            raise ConfigError('account identity must be a positive integer')
        if expected is not None and account_id != expected:
            raise ConfigError('account identity does not match expected_user_id')
    account = account_id if account_id is not None else expected
    if room_alias is not None and room_alias not in {room['alias'] for room in config['rooms']}:
        raise ConfigError('unknown configured room')
    if _preparation is None:
        resolved = resolved_catalogs(root, room_alias=room_alias, reference=catalog_reference, configuration=config)
    else:
        # The future topology is not on disk yet. Resolve the sealed complete
        # catalog reference against the current host first, then require every
        # planned mode. Normal runtime alias checks remain unchanged.
        if room_alias is not None or catalog_reference is not None:
            raise ConfigError('topology preparation requires complete host catalogs')
        resolved = resolved_catalogs(root)
        if resolved is not None:
            from .catalog_store import mode_for
            if not {mode_for(r) for r in config['rooms']} <= set(resolved['bundles']):
                raise ConfigError('managed catalog set is incomplete; stopped import required')
    live = config['execution'] == 'pilot-live'
    from .ratings.migration import capture_enabled
    prepared_ratings = capture_enabled(root)
    from .ratings.reader import profile as rating_profile
    reader=rating_profile(root)
    rooms = []
    for room in config['rooms']:
        alias = room['alias']
        if room_alias is not None and alias != room_alias:
            continue
        spec = PRESETS[room['preset']]
        catalog = str(contained(root, room['catalog']))
        settings = {
            'version': __version__ if live else 'v7.0.0',
            'room_name': default_room_title(room['preset']) if live else f'PoolRotater offline | {alias}',
            'room_count': len(config['rooms']), 'discord_control_room_alias': alias,
            'ruleset_id': spec['ruleset'], 'mania_keys': spec['keys'] or 4,
            'auto_difficulty': spec['auto_difficulty'],
            'sr_min': room['sr_min'], 'sr_max': room['sr_max'],
            'default_response_language': room['language'],
            'farm_db': catalog if spec['ruleset'] == 0 else '',
            'mania_db': catalog if spec['ruleset'] == 3 else '',
            **({'catch_catalog': catalog} if spec['ruleset'] == 2 else {}),
            **({'taiko_catalog': catalog} if spec['ruleset'] == 1 else {}),
            'loved_db': catalog if room['preset']=='standard-loved' else '', 'loved_mode': room['preset']=='standard-loved', 'mania_farm_db': '',
            'mania_farm_enabled': False, 'mania_convert_db': '',
            'max_length': 240, 'min_passcount': 0 if room['preset']=='standard-loved' else 1000, 'pool_limit': 200,
            'max_map_age_years': 0,
            'ratings_db': str(contained(root, 'state/controller-ratings.db')),
            'ratings_schema_prepared': prepared_ratings,
            'ratings_reader_mode':reader['mode'],'ratings_reader_root':str(root),
            'ratings_reader_epoch':reader['server_epoch'],
            'api_rate_db': str(contained(root, 'state/controller-api-rate.db')),
            'process_control_db': str(contained(root, 'state/controller-process-control.db')),
            'lazer_coordinator_db': str(contained(root, 'state/controller-lazer-coordinator.db')),
            'lazer_coordinator_enabled': False,
            'lazer_chat_patrol_enabled': False, 'lazer_rejoin_on_hard_restart': False,
            'log_file': str(contained(root, f'logs/room-{alias}.log')),
            'map_pp_cache_dir': str(contained(root, 'state/controller-map-pp')),
            'discord_control_health_state_file': str(contained(root, 'state/controller-discord-health.json')),
            'discord_public_status_state_file': str(contained(root, 'state/controller-discord-public.json')),
            'token_file': str(contained(root, config['account']['token_file'])),
            'oauth_client_id_env': config['account']['client_id_env'],
            'oauth_client_secret_env': config['account']['client_secret_env'],
            'bot_user_id': account or 0, 'admins': [],
            'requests_per_minute': 50, 'requests_burst_capacity': 60,
            'spectator_url': 'offline://fixture-referee',
            'chat_websocket_enabled': live,
            'background_player_profiles': live,
            'chat_websocket_endpoint': 'wss://notify.ppy.sh' if live else '',
            'discord_control_enabled': False, 'discord_modlog_enabled': False,
            'discord_control_channel_id': 0, 'discord_control_guild_id': 0,
            'discord_public_status_channel_id': 0,
            'discord_control_bot_token_env': 'POOLROTATER_DISABLED_DISCORD',
            'discord_modlog_webhook_env': 'POOLROTATER_DISABLED_MODLOG',
            'discord_invite_url': '', 'discord_invite_reminder_seconds': 0,
            'ai_enabled': False, 'ai_api_key_env': 'POOLROTATER_DISABLED_AI',
            'ai_context_file': '', 'map_pp_display_enabled': False,
            'custom_join_greetings': copy.deepcopy(DEFAULT_JOIN_GREETINGS),
            'allowed_mods': [], 'required_mods': [], 'freestyle': False,
        }
        if spec['ruleset'] == 2:
            from .gameplay_defaults import settings_for as research_defaults
            settings.update(research_defaults('catch-fixed'))
        from .runtime_defaults import settings_for as default_gameplay
        from .managed_host.settings import DISALLOWED_GAMEPLAY
        # Operational defaults belong to the host, not to a map-pool mode.
        # Catch's smaller gameplay profile must not drop shutdown/reload/
        # transport settings when an existing room changes ruleset. Explicit
        # retained owner overlays below continue to take precedence.
        settings.update({k:v for k,v in default_gameplay(root,marker,'standard-auto').items()
                         if k in DISALLOWED_GAMEPLAY})
        settings.update(default_gameplay(root,marker,room['preset']))
        if resolved is not None:
            settings.update(settings_for(room, resolved))
        if room['alias'] in gameplay:
            settings.update(gameplay[room['alias']]['settings'])
        settings.update(owner_integrations.settings(root, integrations, room['alias']))
        managed=copy.deepcopy(shadow['rooms'][alias]['settings']) if shadow else {}
        settings.update({k:v for k,v in managed.items() if k not in ('preset','visibility','password')})
        if managed:
            revision=shadow['rooms'][alias]['revision']
            settings.update(managed_visibility=managed.get('visibility','public'),managed_preset=room['preset'],managed_revision=revision)
        rooms.append({'alias': alias, 'preset': room['preset'],
                      'config_path': str(contained(root, f'{ROOM_DIRECTORY}/{alias}.json')),
                      'settings': settings})
    return {'version': __version__, 'execution': 'offline', 'live_ready': False,
            'installation_id': marker['installation_id'], 'generation': marker['generation'],
            'account_id': account, 'rooms': rooms,
            'catalog_set': resolved['set_id'] if resolved is not None else None,
            'catalog_reference': resolved['reference'] if resolved is not None else None,
            'binding': {'account': config['account'],
                        'rooms': [{k: r[k] for k in ('alias', 'preset', 'catalog')} for r in original_config['rooms']]}}


def render_room(root: Path, alias, *, account_id=None, catalog_reference=None, catalog_set=None, private_session=None):
    """Worker-local reload without broadening its installed SQLite authority."""
    if catalog_reference is not None:
        if not isinstance(catalog_reference, dict) or catalog_reference.get('set_id') != catalog_set:
            raise ConfigError('worker catalog reference/set mismatch')
    elif catalog_set is not None:
        raise ConfigError('worker pinned catalog reference is missing')
    native = render(root, account_id=account_id, room_alias=alias,
                    catalog_reference=catalog_reference)
    room = native['rooms'][0]
    if private_session is not None:
        from .private_pilot import private_room
        room = private_room(root, room, private_session)
    return room


def pinned_keys(settings):
    # Installation/account/transport paths remain pinned; gameplay tuning does not.
    return set(settings) - EDITABLE_BASE - EDITABLE_STAFF - GAMEPLAY_KEYS - {'selection_style','selection_styles','custom_join_greetings','managed_revision'} - {
        'max_length', 'min_passcount', 'pool_limit', 'max_map_age_years',
    }


def validate_room_data(data: dict, base: dict):
    if not isinstance(data, dict):
        raise ConfigError('runtime room configuration must be an object')
    if not set(base) <= set(data):
        raise ConfigError('runtime room configuration is missing managed fields')
    if 'managed_revision' in base and (type(data.get('managed_revision')) is not int
            or data['managed_revision']!=base['managed_revision']):
        raise ConfigError('managed runtime revision differs from committed defaults')
    # Native files are flat. Legacy nested Mania overrides could otherwise mask
    # host-controlled catalog/account settings and a new native difficulty band.
    if 'mania_settings' in data or 'catch_settings' in data:
        raise ConfigError('native runtime room preferences must use flat fields')
    for key, value in data.items():
        if key.startswith('_'):
            raise ConfigError('private runtime keys cannot be stored in room configuration')
        if key in pinned_keys(base) and (type(value) is not type(base[key]) or value != base[key]):
            raise ConfigError('pinned runtime configuration changed; coordinated reconfiguration required')
        if key not in base and (key.endswith(('_db', '_file', '_dir', '_path', '_env'))
                                or key in {'access_token', 'refresh_token', 'client_secret', 'password'}):
            raise ConfigError('undeclared runtime path or secret field')
    from .config import LANGUAGES, number
    if 'sr_min' in data or 'sr_max' in data:
        low, high = data.get('sr_min', base['sr_min']), data.get('sr_max', base['sr_max'])
        maximum = 100 if base['ruleset_id'] == 2 else 20
        number(low, 0, maximum, 'sr_min'); number(high, 0, maximum, 'sr_max')
        if low >= high:
            raise ConfigError('runtime difficulty range must be ordered')
    if data.get('default_response_language', base['default_response_language']) not in LANGUAGES:
        raise ConfigError('unsupported runtime response language')
    if 'auto_difficulty' in data and type(data['auto_difficulty']) is not bool:
        raise ConfigError('auto_difficulty must be boolean')
    if (base['ruleset_id'] in (2, 3) or base.get('loved_mode')) and data.get('auto_difficulty', False):
        raise ConfigError('Catch and Mania require fixed difficulty')
    owner_integrations.validate_settings({k:v for k,v in data.items() if k in EDITABLE_STAFF}, set())
    preset = ('mania-' + str(base['mania_keys']) + 'k') if base['ruleset_id'] == 3 else ('taiko-auto' if base.get('auto_difficulty') else 'taiko-fixed') if base['ruleset_id']==1 else 'catch-fixed' if base['ruleset_id'] == 2 else 'standard-loved' if base.get('loved_mode') else 'standard-auto'
    validate_settings({k: v for k, v in data.items() if k in GAMEPLAY_KEYS}, preset=preset)
    if base['ruleset_id'] == 2:
        from bot.mode import validate_catch_config
        try: validate_catch_config(data)
        except ValueError as exc: raise ConfigError(str(exc)) from exc


class RuntimeConfigBridge:
    """Private generated room files retain the existing live preference machinery."""

    def __init__(self, root: Path, *, account_id=None, integration_capabilities=()):
        self.root = checked_root(root)
        self.account_id = account_id
        self.integration_capabilities = frozenset(integration_capabilities)
        self.initial = render(self.root, account_id=account_id)
        self._cached_configs = None
        self._cached_stamp = None
        # Native maintenance and fleet application share this bridge. Serialize
        # both materialization and its cache; the file lease additionally fences
        # other processes and is deliberately nonblocking.
        self._configuration_lock = threading.RLock()

    def stamp(self):
        names = ['.poolrotater-host.json', 'config/host.json', STATE_FILE, GAMEPLAY_RECORD,
                 owner_integrations.RECORD, owner_integrations.ENVIRONMENT_FILE,
                 f'{ROOM_DIRECTORY}/universal.json', 'state/rating-migration.json',
                 'config/managed-rooms.json','state/managed-host-policy.json']
        names += [f'{ROOM_DIRECTORY}/{r["alias"]}.json' for r in self.initial['rooms']]
        paths = [contained(self.root, n) for n in names]
        paths += [Path(r['settings'].get('catch_catalog') or r['settings'].get('loved_db') or r['settings']['farm_db'] or r['settings']['mania_db']) for r in self.initial['rooms']]
        pointer = contained(self.root, POINTER)
        paths.append(pointer)
        if pointer.exists():
            from .catalog_store import CatalogStore
            store = CatalogStore.open(self.root)
            for name in ('current.json', 'pending.json', 'binding.json', 'bootstrap.json'):
                paths.append(store.path / name)
            ref = store.current()
            for identity in ref['modes'].values():
                folder = store.path / 'versions' / identity
                paths.append(folder); paths.extend(folder.iterdir())
                paths.append(store.path / 'receipts' / (identity + '.json'))
        result = []
        for path in [self.root, *[contained(self.root, d) for d in DIRECTORIES],
                     contained(self.root, ROOM_DIRECTORY)]:
            try:
                st = path.stat()
                result.append((st.st_ino, st.st_mode, st.st_uid))
            except FileNotFoundError:
                result.append(None)
        for path in paths:
            try:
                stat = path.stat()
                result.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_mode, stat.st_uid, stat.st_nlink))
            except FileNotFoundError:
                result.append(None)
        return tuple(result)

    def materialize(self, *, preview=False) -> list[dict] | dict:
        with self._configuration_lock:
            return self._materialize(preview=preview)

    def _materialize(self, *, preview=False) -> list[dict] | dict:
        if type(preview) is not bool:
            raise ConfigError('invalid runtime materialization preview')
        from .owner_migration import admission as owner_admission
        owner_admission(self.root)
        current = render(self.root, account_id=self.account_id)
        identity = ('installation_id', 'generation', 'account_id', 'binding')
        if any(current[k] != self.initial[k] for k in identity):
            raise ConfigError('installation, account or room topology changed; coordinated reconfiguration required')
        # No fake worker or ledger is created until all catalogs pass preflight.
        _, host = load(self.root)
        if current['catalog_set'] is not None:
            from rolling.worker import make_worker_pool
            for room in current['rooms']:
                if not make_worker_pool({**room['settings'], '_host_managed_catalogs': True}).maps:
                    raise ConfigError('no eligible maps in managed difficulty range')
        else:
            for room in host['rooms']:
                make_pool(self.root, room)
        directory = contained(self.root, ROOM_DIRECTORY)
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.stat().st_mode & 0o077 or directory.stat().st_uid != self.root.stat().st_uid:
            raise ConfigError('runtime configuration directory must be private')
        state_path = contained(self.root, STATE_FILE)
        with file_lease(contained(self.root, 'state/runtime-config.lock')), ExitStack() as locks:
            previous = read_json(state_path,maximum=HOST_JSON_LIMIT) if state_path.exists() else None
            if previous is not None:
                if (not isinstance(previous, dict) or set(previous) != {'format', 'identity', 'bases'}
                        or type(previous['format']) is not int or previous['format'] != 1
                        or previous['identity'] != {k: current[k] for k in identity}
                        or not isinstance(previous['bases'], dict)
                        or set(previous['bases']) != {r['alias'] for r in current['rooms']}):
                    raise ConfigError('runtime configuration binding changed; coordinated reconfiguration required')
            plans = []
            for room in current['rooms']:
                path = contained(self.root, f'{ROOM_DIRECTORY}/{room["alias"]}.json')
                if not preview:
                    locks.enter_context(file_lease(path.with_name(path.name + '.lock')))
                base = room['settings']
                old = previous['bases'].get(room['alias']) if previous else None
                if path.exists() and old is None:
                    raise ConfigError('refusing unrecognized existing runtime room configuration')
                data = read_json(path,maximum=ROOM_JSON_LIMIT) if path.exists() else copy.deepcopy(base)
                if old is not None:
                    # v7.1.13 added this managed execution setting. A retained
                    # v7.1.11/12 base legitimately lacks it. Admit only this
                    # boolean addition; never discard an operator's conflicting
                    # preexisting value or loosen the rest of the schema fence.
                    profile_addition = (isinstance(old, dict)
                        and 'background_player_profiles' not in old
                        and type(base.get('background_player_profiles')) is bool)
                    if profile_addition and 'background_player_profiles' in data:
                        value = data['background_player_profiles']
                        if type(value) is not bool or value != base['background_player_profiles']:
                            raise ConfigError('new managed profile setting conflicts with retained preference')
                    additions = {'standard_dt_db', 'standard_catalog_binding',
                                 'mania_convert_validation_db'} if current['catalog_set'] else set()
                    if profile_addition:
                        additions.add('background_player_profiles')
                    from .managed_host.policy import enabled as managed_enabled
                    if managed_enabled(self.root):
                        additions.update(('managed_visibility','managed_preset','managed_revision'))
                        from .managed_host.settings import FIELDS
                        additions.update(FIELDS-{'preset','visibility','password'})
                        from .managed_host.settings import DISALLOWED_GAMEPLAY
                        additions.update(DISALLOWED_GAMEPLAY)
                    if room['alias'] in load_overrides(self.root, *load(self.root)):
                        additions.update(GAMEPLAY_KEYS)
                    if owner_integrations.load(self.root, *load(self.root)) is not None:
                        additions.update(owner_integrations.INTEGRATION_KEYS)
                        additions.update(owner_integrations.PATHS)
                    if base['ratings_schema_prepared']:
                        # Only a completed stopped ledger migration allows the
                        # new managed storage field on an existing runtime base.
                        additions.add('ratings_schema_prepared')
                        additions.update(('ratings_reader_mode','ratings_reader_root','ratings_reader_epoch'))
                    # Previous pilot bridges injected this unused spelling.
                    # The native controller consumes hard_restart_interval_seconds;
                    # accept only the exact old inert zero, never erase an edit.
                    if 'hard_restart_interval_minutes' in old and 'hard_restart_interval_minutes' not in base:
                        if (type(old['hard_restart_interval_minutes']) is not int
                                or old['hard_restart_interval_minutes'] != 0
                                or type(data.get('hard_restart_interval_minutes')) is not int
                                or data['hard_restart_interval_minutes'] != 0):
                            raise ConfigError('obsolete pilot field changed; coordinated review required')
                        old=copy.deepcopy(old);old.pop('hard_restart_interval_minutes')
                        data.pop('hard_restart_interval_minutes')
                    retired=set()
                    if managed_enabled(self.root):
                        retired=(set(old)-set(base))&{'selection_style','selection_styles'}
                        if (old.get('managed_preset') and old.get('managed_preset')!=base.get('managed_preset')):
                            from .catalog_store import CATALOG_KEYS
                            from .managed_host.settings import FIELDS
                            additions.update(CATALOG_KEYS)
                            # A mode's renderer may add defaults that do not
                            # exist in the next mode (for example Catch-only
                            # length settings). Drop only declared gameplay/
                            # catalog keys absent from the new desired base.
                            # Credential, account and arbitrary path fields
                            # remain outside this retirement allowlist.
                            retired|=(set(old)-set(base))&(set(CATALOG_KEYS)|FIELDS)
                    if (not isinstance(old, dict) or set(old) - set(base) - retired
                            or set(base) - set(old) - additions):
                        raise ConfigError('runtime base schema changed; coordinated reconfiguration required')
                    try:validate_room_data(data, old)
                    except ConfigError:
                        # A managed change can write a room file and then lose
                        # power before publishing the shared bases file. Admit
                        # that completed room only if its revision AND every
                        # pinned field validate against the current rendered
                        # desired base. A revision stamp alone is no authority.
                        if (not managed_enabled(self.root) or type(base.get('managed_revision')) is not int
                                or data.get('managed_revision')!=base['managed_revision']):raise
                        validate_room_data(data,base)
                    for key in retired:data.pop(key,None)
                    explicit = load_overrides(self.root, *load(self.root)).get(room['alias'], {}).get('settings', {})
                    for key, value in base.items():
                        # New-install title defaults are not a rename request for
                        # a retained host. Preserve its saved native title unless
                        # the operator explicitly changed the title overlay.
                        if (key == 'room_name' and key not in explicit
                                and value == default_room_title(room['preset'])):
                            continue
                        if key in pinned_keys(base) or old.get(key) != value:
                            data[key] = value
                validate_room_data(data, base)
                plans.append((path, data, room))
            shared = contained(self.root, f'{ROOM_DIRECTORY}/universal.json')
            if shared.exists() and read_json(shared) != {}:
                raise ConfigError('shared runtime overrides are unavailable in the offline host')
            # Mark identity first, making an interrupted initial materialization resumable.
            state = {'format': 1, 'identity': {k: current[k] for k in identity},
                     'bases': {r['alias']: r['settings'] for r in current['rooms']}}
            if preview:
                # A stopped root upgrader pins these exact outputs before the
                # first configuration replacement. Preview never writes room
                # data, shared overrides, bases or per-room lock files.
                return {'rooms': copy.deepcopy(current['rooms']), 'state': state,
                        'writes': {path.relative_to(self.root).as_posix(): data
                                   for path, data, _ in plans}
                                  | {shared.relative_to(self.root).as_posix(): {}}}
            if previous is None:
                atomic_json(state_path, state)
            if not shared.exists():
                atomic_json(shared, {})
            for path, data, _ in plans:
                if not path.exists() or read_json(path,maximum=ROOM_JSON_LIMIT) != data:
                    atomic_json(path, data)
            if previous is not None and previous != state:
                atomic_json(state_path, state)
        self._materialized_set = current['catalog_set']
        self._materialized_reference = current['catalog_reference']
        return current['rooms']

    def load_rooms(self) -> list[dict]:
        with self._configuration_lock:
            return self._load_rooms()

    def _load_rooms(self) -> list[dict]:
        from .owner_migration import admission as owner_admission
        owner_admission(self.root)
        marker, host = load(self.root)
        owner_integrations.admission(self.root, owner_integrations.load(self.root, marker, host),
                                     capabilities=self.integration_capabilities)
        if self._cached_configs is not None and self.stamp() == self._cached_stamp:
            return copy.deepcopy(self._cached_configs)
        before_stamp = self.stamp()
        rooms = self.materialize()
        # Full worker imports belong to the locked regression/runtime environment.
        from run_multiroom import load_rooms
        configs = load_rooms([r['config_path'] for r in rooms],
                             contained(self.root, f'{ROOM_DIRECTORY}/universal.json'))
        bases = {r['alias']: r['settings'] for r in rooms}
        for cfg in configs:
            base = bases[cfg['_instance_name']]
            cfg.update(_host_root=str(self.root), _host_managed_catalogs=True,
                       _host_offline=True, _host_installation_id=self.initial['installation_id'])
            if self._materialized_set is not None:
                cfg['_host_catalog_set'] = self._materialized_set
                cfg['_host_catalog_reference'] = copy.deepcopy(self._materialized_reference)
                cfg['_supervisor_overrides']['_host_catalog_set'] = self._materialized_set
                cfg['_supervisor_overrides']['_host_catalog_reference'] = copy.deepcopy(self._materialized_reference)
            cfg['_supervisor_overrides'].update({k: base[k] for k in pinned_keys(base)})
            cfg['_supervisor_overrides'].update(_host_root=str(self.root), _host_managed_catalogs=True,
                                                _host_offline=True)
            if self.account_id is not None:
                cfg['_authenticated_user_id'] = self.account_id
        self._cached_configs = copy.deepcopy(configs)
        # A concurrent reference publication forces refresh on the next call.
        self._cached_stamp = before_stamp
        return configs
