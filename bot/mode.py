"""Room-local ruleset boundaries shared by launch and hot reload."""
from __future__ import annotations

MANIA_FREE_MODS = frozenset({"NF", "EZ", "HD", "FI", "FL", "MR", "SD", "PF"})
MANIA_RATE_MODS = frozenset({"DT", "NC", "HT", "DC"})
MANIA_KEYS = frozenset({
    "mania_db", "mania_keys", "mania_source_mode", "mania_min_length",
    "mania_style_min_score", "mania_farm_enabled", "mania_min_rank_score",
    "mania_min_confidence", "mania_min_appearances", "mania_min_bands",
    "mania_farm_db", "mania_farm_bonus_max", "mania_farm_bonus_min_confidence",
    "mania_farm_high_score_threshold", "mania_farm_pick_multiplier",
    "mania_high_star_relax_style",
    "mania_style_fallback_enabled",
    "mania_convert_db", "mania_convert_max_share", "mania_convert_validation_db",
    "sr_min", "sr_max", "max_length", "min_passcount", "max_map_age_years",
    "high_star_relax_enabled", "high_star_relax_threshold",
    "high_star_relax_min_maps", "high_star_relax_length_step",
    "high_star_relax_age_step_years", "high_star_relax_max_steps",
    "pool_limit", "auto_difficulty", "difficulty_vote_step",
    "difficulty_vote_max_steps", "allowed_mods", "required_mods", "freestyle",
    "room_name", "stream_enabled", "stream_share", "loved_mode",
    "discord_control_enabled", "discord_control_channel_id",
    "discord_public_status_channel_id", "discord_public_status_label",
    "discord_public_status_channel_name", "discord_public_join_base_url",
    "ai_context_file",
})

# Research Catch has no qualified alternate variants or player-skill calibration.
CATCH_DEFAULTS = {
    'auto_difficulty': False, 'allowed_mods': [], 'required_mods': [], 'freestyle': False,
    'loved_mode': False, 'stream_enabled': False, 'mania_farm_enabled': False,
    'catch_include_loved': False, 'catch_min_length': 0,
    'max_length': 240, 'min_passcount': 0, 'max_map_age_years': 0, 'pool_limit': 200000,
    'high_star_relax_enabled': False, 'map_pp_display_enabled': False,
    'map_pp_auto_announce_enabled': False,
}
CATCH_KEYS = frozenset({
    'catch_catalog', 'catch_include_loved', 'catch_min_length',
    'sr_min', 'sr_max', 'max_length', 'preferred_min_length', 'preferred_max_length',
    'preferred_length_share', 'pool_limit', 'auto_difficulty', 'difficulty_vote_step',
    'difficulty_vote_max_steps', 'allowed_mods', 'required_mods', 'freestyle',
    'room_name', 'stream_enabled', 'loved_mode',
    'discord_control_enabled', 'discord_control_channel_id',
    'discord_public_status_channel_id', 'discord_public_status_label',
    'discord_public_status_channel_name', 'discord_public_join_base_url', 'ai_context_file',
})


def validate_catch_mods(allowed, required, freestyle):
    if type(allowed) is not list or type(required) is not list or allowed or required or freestyle is not False:
        raise ValueError('Native Catch currently supports NM only: empty allowed/required mods and freestyle=false')


def validate_catch_config(cfg):
    validate_catch_mods(cfg.get('allowed_mods', []), cfg.get('required_mods', []), cfg.get('freestyle', False))
    if cfg.get('auto_difficulty', False) is not False:
        raise ValueError('Native Catch requires fixed difficulty')
    if cfg.get('loved_mode', False) is not False or cfg.get('stream_enabled', False) is not False:
        raise ValueError('Native Catch cannot use Standard Loved or stream pools')
    if type(cfg.get('catch_include_loved', False)) is not bool:
        raise ValueError('catch_include_loved must be boolean')
    if cfg.get('mania_farm_enabled', False) or cfg.get('high_star_relax_enabled', False):
        raise ValueError('Native Catch cannot use unqualified farm or high-star relaxation')
    for key in ('clock_rate', 'speed_rate', 'rate'):
        if key in cfg and (type(cfg[key]) not in (int, float) or cfg[key] != 1):
            raise ValueError('Native Catch supports clock rate 1.0 only')

TAIKO_DEFAULTS = {
    'auto_sr_bias':0.,'auto_sr_half_width':.5,
    'allowed_mods': [], 'required_mods': [], 'freestyle': False,
    'loved_mode': False, 'stream_enabled': False, 'mania_farm_enabled': False,
    'min_passcount': 0, 'max_map_age_years': 0, 'high_star_relax_enabled': False,
    'map_pp_display_enabled': True, 'map_pp_auto_announce_enabled': True,
}

# Room-local settings that have defined Taiko behavior. Calibration and PP
# display choices are deliberately local to the Taiko room; Standard-wide
# values must not be inherited through ``universal.json``. Guarded unsupported
# fields remain present here so nonzero/true settings are rejected below rather
# than silently hidden by the Taiko defaults.
TAIKO_KEYS = frozenset({
    'taiko_catalog', 'sr_min', 'sr_max', 'max_length', 'pool_limit',
    'preferred_min_length', 'preferred_max_length', 'preferred_length_share',
    'auto_difficulty', 'auto_sr_bias', 'auto_sr_half_width',
    'difficulty_vote_step', 'difficulty_vote_max_steps',
    'allowed_mods', 'required_mods', 'freestyle', 'min_passcount',
    'max_map_age_years', 'loved_mode', 'stream_enabled',
    'mania_farm_enabled', 'high_star_relax_enabled',
    'map_pp_display_enabled', 'map_pp_auto_announce_enabled',
    'room_name', 'discord_control_enabled', 'discord_control_channel_id',
    'discord_public_status_channel_id', 'discord_public_status_label',
    'discord_public_status_channel_name', 'discord_public_join_base_url',
    'ai_context_file',
})


def validate_taiko_mods(allowed, required, freestyle):
    if type(allowed) is not list or type(required) is not list or allowed or required or freestyle is not False:
        raise ValueError('Taiko currently supports qualified NoMod at clock rate 1.0 only')


def validate_taiko_config(cfg):
    validate_taiko_mods(cfg.get('allowed_mods', []),cfg.get('required_mods', []),cfg.get('freestyle',False))
    if any(cfg.get(key, 0) != 0 for key in ('min_passcount', 'max_map_age_years')):
        raise ValueError('Taiko does not support passcount or map-age filters')
    if any(cfg.get(k,False) for k in ('loved_mode','stream_enabled','mania_farm_enabled','high_star_relax_enabled')):
        raise ValueError('Taiko cannot inherit other ruleset selection policies')
    for key in ('clock_rate','speed_rate','rate'):
        if key in cfg and (type(cfg[key]) not in (int,float) or cfg[key]!=1):
            raise ValueError('Taiko supports qualified clock rate 1.0 only')


def ruleset_id(cfg):
    value = cfg.get("ruleset_id", 0)
    if type(value) is not int or value not in (0, 1, 2, 3):
        raise ValueError("ruleset_id must be 0 (standard), 1 (Taiko), 2 (native Catch) or 3 (native Mania)")
    return value

def validate_mania_mods(allowed, required, freestyle):
    if not isinstance(freestyle, bool):
        raise ValueError("freestyle must be true or false")
    for label, values, permitted in (
        ("allowed_mods", allowed, MANIA_FREE_MODS),
        ("required_mods", required, MANIA_FREE_MODS),
    ):
        if isinstance(values, list) and any(
            isinstance(m, str) and m.upper() in MANIA_RATE_MODS for m in values
        ):
            raise ValueError("Mania speed mods (DT/NC/HT/DC) are disabled for this release")
        if not isinstance(values, list) or any(
            not isinstance(m, str) or m.upper() not in permitted for m in values
        ):
            raise ValueError(f"Invalid mania {label}; key-changing, automation and standard-only mods are not allowed")
    if freestyle and (allowed or required):
        raise ValueError("freestyle=true needs empty allowed_mods and required_mods")
    mods = {m.upper() for m in allowed + required}
    required_set = {m.upper() for m in required}
    if {m.upper() for m in allowed} & required_set:
        raise ValueError("Mods cannot be both allowed and required")
    for pair in ({"HD", "FI"}, {"SD", "PF"}, {"NF", "SD"}, {"NF", "PF"}):
        if pair <= required_set or (pair & required_set and pair <= mods):
            raise ValueError("Incompatible required/free mania mods")

def merge_room_config(defaults, room, universal):
    """Global operational settings still win; mania tuning is explicitly local.

    ruleset_id is room identity, never a universal preference. The mania_settings
    block protects mania's SR/length/mod defaults from standard-wide overrides.
    """
    cfg = dict(defaults)
    cfg.update({k: v for k, v in room.items() if not str(k).startswith("_")})
    cfg.update({k: v for k, v in universal.items() if not str(k).startswith("_")})
    explicit = set(room) | set(universal)
    if "lazer_join_delay_seconds" not in explicit and "lazer_rejoin_delay_seconds" in explicit:
        cfg["lazer_join_delay_seconds"] = cfg["lazer_rejoin_delay_seconds"]
    cfg["ruleset_id"] = room.get("ruleset_id", 0)
    if ruleset_id(cfg) == 3:
        local = room.get("mania_settings", {})
        if not isinstance(local, dict) or set(local) - MANIA_KEYS:
            raise ValueError("mania_settings must contain only documented mania room-tuning keys")
        cfg.update(local)
        # Never allow standard calibration or a Loved/standard source to leak in.
        cfg.update(auto_difficulty=False, loved_mode=False, stream_enabled=False)
        if cfg.get("mania_keys", 4) not in (4, 7):
            raise ValueError("Only native 4K or 7K mania is supported in this release")
        validate_mania_mods(cfg.get("allowed_mods", []), cfg.get("required_mods", []), cfg.get("freestyle", False))
    elif ruleset_id(cfg) == 1:
        cfg.update(TAIKO_DEFAULTS)
        cfg.update({key:room[key] for key in TAIKO_KEYS if key in room})
        validate_taiko_config(cfg)
    elif ruleset_id(cfg) == 2:
        local = room.get('catch_settings', {})
        if not isinstance(local, dict) or set(local) - CATCH_KEYS:
            raise ValueError('catch_settings must contain only documented Catch room-tuning keys')
        # Global Standard tuning must not silently select an unqualified variant.
        # Flat explicit room values are restored after safe defaults; local block wins.
        cfg.update({key: list(value) if isinstance(value, list) else value
                    for key, value in CATCH_DEFAULTS.items()})
        cfg.update({key: room[key] for key in CATCH_KEYS if key in room})
        cfg.update(local)
        validate_catch_config(cfg)
    return cfg
