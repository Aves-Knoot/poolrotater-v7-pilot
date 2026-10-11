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

def ruleset_id(cfg):
    value = cfg.get("ruleset_id", 0)
    if type(value) is not int or value not in (0, 3):
        raise ValueError("ruleset_id must be 0 (standard) or 3 (native Mania)")
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
    return cfg
