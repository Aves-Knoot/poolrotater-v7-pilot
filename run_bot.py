#!/usr/bin/env python3
"""Run one lobby-bot instance.

Examples:

    python run_bot.py --config instances/room1.json --check
    python run_bot.py --config instances/room1.json
    python run_bot.py --config instances/room2.json

For VPS hosting, systemd starts one copy of this script per room. Each room has
its own OAuth client/secret/token file and log file, while farm.db,
ratings.db, and the API rate-state database may be shared.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot.api import OsuApi
from bot.auth import TokenManager
from bot.bot import LobbyBot
from bot.community import DEFAULT_DISCORD_INVITE
from bot.pool import MapPool
from bot.mode import merge_room_config

DEFAULT_CONFIG = {
    "ruleset_id": 0,
    "mania_db": "../osu-mania-finder/mania.db",
    # Compact, read-only 4K farm-efficiency signals. Missing/invalid data
    # degrades to neutral selection weights without changing pool eligibility.
    "mania_farm_db": "data/mania_farm.db",
    "mania_farm_bonus_max": 0.30,
    "mania_farm_bonus_min_confidence": 0.50,
    # A chart at or above this validated sidecar score receives exactly this
    # multiplier in every Mania source mode. Farm mode contains only such rows.
    "mania_farm_high_score_threshold": 70.0,
    "mania_farm_pick_multiplier": 1.25,
    "mania_high_star_relax_style": True,
    "mania_style_fallback_enabled": False,
    # Independently vetted standard-to-Mania converts are optional and are
    # admitted only to ordinary variety picks with a hard rolling share cap.
    "mania_convert_db": "",
    "mania_convert_max_share": 0.30,
    "spectator_url": "https://spectator.ppy.sh",
    "token_file": "tokens.json",
    # The VPS edition deliberately does NOT read OSU_BOT_CLIENT_*.
    # Each room can point at completely separate environment-variable names.
    "oauth_client_id_env": "OSU_VPS_CLIENT_ID",
    "oauth_client_secret_env": "OSU_VPS_CLIENT_SECRET",
    "farm_db": "../osu-farm-finder/farm.db",
    # Loved maps deliberately live outside farm.db. Set loved_mode=true for a
    # room to use only this database and disable Farm/Stream source switching.
    "loved_db": "data/loved.db",
    "loved_mode": False,
    "room_name": "FARM | auto-rotate | .help",
    # One shared lobby-version label can live in universal.json. The title
    # renderer always places it at the very end, so room JSON files no longer
    # need their own embedded [vX.Y.Z] suffixes. Both "6.4.8" and "v6.4.8"
    # are accepted and render as [v6.4.8].
    "version": "v6.4.8",
    "command_prefix": ".",
    "bot_user_id": 0,
    "admins": [],
    "sr_min": 4.5,
    "sr_max": 5.5,
    "max_length": 150,
    # Ranked-date eligibility for newly selected maps. Zero disables it.
    "max_map_age_years": 0.0,
    # Sparse pools broaden only relevance filters (length/age) after the
    # preferred candidates are exhausted. The configured SR window is hard.
    "high_star_relax_enabled": True,
    "high_star_relax_min_maps": 20,
    "high_star_relax_length_step": 60,
    "high_star_relax_age_step_years": 2.0,
    "high_star_relax_max_steps": 2,
    "min_passcount": 100000,
    "pool_limit": 120,
    # Mixed Farm + Stream live pool. Existing room JSON files inherit these
    # defaults automatically unless they explicitly override them.
    "stream_enabled": True,
    "stream_share": 0.50,
    "stream_min_score": 0.50,
    "stream_min_bpm": 140.0,
    "stream_min_length": 45,
    "allowed_mods": [],
    "required_mods": [],
    "freestyle": True,
    "queue_ahead": 2,
    "countdown_seconds": 15,
    "auto_start_seconds": 10,
    # v6.6.5 lobby pacing: active, non-frustrated conversation gets a quiet
    # five-second countdown extension; players can stack one ten-second wait
    # each before the countdown begins.
    "active_chat_countdown_extension_seconds": 5.0,
    "active_chat_countdown_window_seconds": 45.0,
    "active_chat_countdown_min_messages": 4,
    "active_chat_countdown_min_speakers": 2,
    "start_wait_seconds": 10.0,
    "min_players": 1,
    "auto_difficulty": True,
    "auto_sr_half_width": 0.4,
    # Every passed .harder/.easier vote moves AutoDiff by this many stars.
    "difficulty_vote_step": 0.20,
    # Maximum number of temporary .harder/.easier steps in either direction.
    "difficulty_vote_max_steps": 3,
    # Re-read the effective room + universal JSON while the lobby runs. Nearly
    # all behavioural options apply live. A small set of process-identity /
    # shared-transport roots (OAuth token/client, shared DB roots, websocket
    # endpoint, log destination, Discord bot token/guild) are detected and
    # reported as restart-required instead of being silently ignored.
    "config_reload_seconds": 2.0,
    # Optional greetings for specific players. Keys may be osu! user IDs or
    # usernames; {name} and {room} are available in each message.
    "custom_join_greetings": {},
    "custom_join_greeting_cooldown_seconds": 300.0,
    # Recreate the room periodically so it returns to lazer's discoverable
    # room listing. Zero disables automatic hard restarts.
    "hard_restart_interval_seconds": 21600,
    # Warn between completed maps before an automatic restart. Zero restarts
    # at the first safe between-map window instead.
    "hard_restart_warning_maps": 3,
    # After the final warning, leave the old room visible for this long before
    # closing it and immediately creating the replacement room.
    "hard_restart_close_delay_seconds": 5,
    # Local Windows lazer coordination. Separate room processes share the
    # SQLite queue so one release client can move between rooms for chat sends.
    "lazer_rejoin_delay_seconds": 5,
    "lazer_join_delay_seconds": 5,
    "lazer_leave_confirm_delay_seconds": 0.5,
    "lazer_leave_delay_seconds": 1,
    "lazer_minimum_room_dwell_seconds": 2,
    # Closely-spaced actions stay in the current room, but these caps prevent
    # one busy room from monopolizing the single lazer client.
    "lazer_visit_max_seconds": 10,
    "lazer_visit_max_actions": 8,
    # Retained for legacy desktop switching only. Headless supervision forces
    # this off even if an older room config still enables it.
    "lazer_chat_patrol_enabled": False,
    "lazer_chat_patrol_interval_seconds": 2,
    "lazer_switch_timeout_seconds": 45,
    "lazer_queue_poll_seconds": 0.10,
    "lazer_coordinator_db": "state/lazer_coordinator.db",
    "lazer_coordinator_enabled": os.name == "nt",
    "lazer_rejoin_on_hard_restart": os.name == "nt",
    "process_control_db": "state/process_control.db",
    # Poll at this rate while waiting in the lobby. Adaptive mode may use a
    # slower interval during active gameplay to save REST requests.
    "chat_poll_seconds": 5.0,
    "adaptive_chat_polling": True,
    "auto_empty_reset_seconds": 300.0,
    "chat_poll_playing_seconds": 10.0,
    # Incoming chat is delivered in real time over one account-level
    # notification WebSocket. HTTP polling remains as reconnect/failure
    # fallback, plus one low-frequency missed-message reconciliation.
    "chat_websocket_enabled": True,
    "chat_websocket_endpoint": "wss://notify.ppy.sh",
    "chat_websocket_reconnect_max_seconds": 30.0,
    "chat_websocket_reconcile_seconds": 300.0,
    # Public command flood guard. A normal short burst is allowed; sustained
    # spam is then ignored briefly so command replies cannot consume the shared
    # osu! API budget.
    "command_spam_burst": 5,
    "command_spam_window_seconds": 10.0,
    "command_spam_block_seconds": 20.0,
    # Low-frequency REST audit of the multiplayer room/playlist while idle.
    # Referee events remain the primary source of truth; this is a safety net
    # for missed playlist events or remote/local drift.
    "playlist_audit_seconds": 8.0,
    # Cache profile + top-100 lookups across quick leaves/rejoins. Six hours
    # keeps AutoDiff reasonably current while avoiding duplicate join traffic.
    "player_profile_cache_seconds": 21600.0,
    # Explicitly confirmed download availability survives in ratings.db.
    "download_availability_cache_days": 90.0,
    "score_delay_seconds": 4,
    # This is a GLOBAL API-v2 budget when api_rate_db is shared by rooms.
    "requests_per_minute": 60,
    # The sustained budget remains 60/min. After idle time, this much burst
    # credit may be used; later requests reserve future slots and wait.
    "requests_burst_capacity": 90,
    "api_rate_db": "state/api_rate.db",
    # Log rolling API request rates periodically. The live Discord .apiusage
    # command is available to moderators/admins regardless of this interval.
    "api_usage_log_seconds": 60,
    "close_room_on_exit": True,
    # Referee SignalR can reconnect after transient TLS/websocket failures.
    # These control how long the bot waits for recovery and how many real 404
    # room probes are required before it recreates a vanished room.
    "hub_recovery_seconds": 120,
    "shutdown_timeout_seconds": 45.0,
    "room_missing_confirmations": 2,
    "ratings_db": "state/ratings.db",
    "rating_window_seconds": 90,
    "info_interval_seconds": 900,
    "log_file": "logs/bot.log",
    "performance_evidence_ratio": 0.40,
    "performance_evidence_min_top20": 2,
    "performance_evidence_min_top30": 3,
    # Optional asynchronous conversational LLM. The API key itself stays in an
    # environment variable, never in the room JSON.
    "ai_enabled": True,
    "ai_model": "gpt-5-mini",
    "ai_base_url": "https://api.openai.com/v1",
    "ai_endpoint": "",
    "ai_api_key_env": "POOLROTATER_LLM_API_KEY",
    "ai_user_cooldown_seconds": 10.0,
    "ai_global_cooldown_seconds": 3.0,
    "ai_max_pending": 4,
    "ai_max_output_chars": 320,
    "ai_max_output_tokens": 120,
    "ai_max_prompt_chars": 700,
    "ai_recent_chat_messages": 10,
    "ai_timeout_seconds": 30.0,
    "ai_temperature": -1.0,
    "ai_context_file": "ai_context.md",
    "ai_config_reload_seconds": 2.0,
    # Optional Discord transcript for chat, commands, and major lobby activity.
    # The webhook URL itself stays in this environment variable, never here.
    "discord_modlog_enabled": True,
    "discord_modlog_webhook_env": "POOLROTATER_DISCORD_MODLOG_WEBHOOK",
    "discord_modlog_timeout_seconds": 5.0,
    "discord_modlog_include_instance_name": False,
    # A low-frequency, player-facing lobby reminder. Zero disables it.
    "discord_invite_url": DEFAULT_DISCORD_INVITE,
    "discord_invite_reminder_seconds": 14400,
    # Player-facing osu!standard PP projections. PoolRotater downloads the
    # exact .osu file once and calculates locally through osu-tools-py, which
    # embeds the official osu!lazer / osu-tools C# calculator. No tosu process
    # or local HTTP service is required.
    "map_pp_display_enabled": True,
    # Keep `.pp` available while allowing operators to suppress unsolicited
    # PP lines after each completed map.
    "map_pp_auto_announce_enabled": True,
    "map_pp_osu_url_template": "https://osu.ppy.sh/osu/{beatmap_id}",
    "map_pp_cache_dir": "state/map_pp",
    "map_pp_cache_days": 7.0,
    "map_pp_timeout_seconds": 6.0,
    "map_pp_announce_wait_seconds": 1.5,
    "map_pp_command_wait_seconds": 5.0,
    # Optional inbound Discord bot bridge. The bot token stays in an environment
    # variable.  It accepts prefixed commands from one Discord channel only.
    "discord_control_enabled": True,
    "discord_control_bot_token_env": "POOLROTATER_DISCORD_BOT_TOKEN",
    "discord_control_guild_id": 0,
    "discord_control_channel_id": 0,
    # Optional room -> channel map. When present, each room logs to its own
    # channel and unqualified commands in that channel control that room.
    "discord_control_channel_ids_by_room": {},
    # If two room processes listen in one Discord channel, unqualified
    # commands go to this instance. Prefix another room, e.g. `.room2 settings`.
    "discord_control_primary_instance": "room1",
    "discord_control_known_room_aliases": ["room1", "room2"],
    "discord_control_accept_untargeted": False,
    # Local, secret-free heartbeat consumed by the operations health collector.
    "discord_control_health_state_file": "state/discord_control_health.json",
    # Discord server owner and members with this exact role name are bot admins.
    "discord_control_admin_role_names": ["Top dev"],
    # Members with this exact role name are bot moderators.
    "discord_control_moderator_role_names": ["mods"],
    # Optional player-visible status channel. The bot edits one persistent
    # status card there and renames the channel green/open or red/closed.
    "discord_public_status_channel_id": 0,
    "discord_public_status_channel_name": "",
    "discord_public_status_label": "",
    "discord_public_join_base_url": "",
    "discord_public_status_state_file": "state/discord_public_status.json",
    # Channel renames are serialized across all rooms. Five seconds keeps a
    # three-room startup quick while avoiding a burst of Modify Channel calls.
    "discord_status_rename_spacing_seconds": 5.0,
}


def universal_config_path(
    room_path: str | Path,
    explicit: str | Path | None = None,
) -> Path:
    """Return the explicit universal JSON, or universal.json beside the room."""
    if explicit is not None:
        return Path(explicit)
    return Path(room_path).parent / "universal.json"


def _read_json_object(path: Path, *, label: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{label} config root must be a JSON object: {path}")
    return data


def load_config(
    path: str,
    universal_path: str | Path | None = None,
) -> dict:
    """Load defaults, then room JSON, then the higher-priority universal JSON."""
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path)
    if p.exists():
        room = _read_json_object(p, label="room")
        cfg.update(room)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        print(f"Wrote a default {path} -- edit it, then re-run.")
        raise SystemExit(0)

    shared = universal_config_path(p, universal_path)
    universal: dict = {}
    if shared.exists():
        # Runtime/private keys can never be injected from a JSON file.
        universal = {
            key: value
            for key, value in _read_json_object(shared, label="universal").items()
            if not str(key).startswith("_")
        }
        cfg.update(universal)
    cfg = merge_room_config(DEFAULT_CONFIG, room, universal)
    explicit_keys = set(room) | set(universal)
    if (
        "lazer_join_delay_seconds" not in explicit_keys
        and "lazer_rejoin_delay_seconds" in explicit_keys
    ):
        cfg["lazer_join_delay_seconds"] = cfg["lazer_rejoin_delay_seconds"]
    return cfg


def _install_signal_handlers() -> None:
    """Turn service-manager SIGTERM into normal KeyboardInterrupt cleanup."""
    def handle_term(signum, frame):
        logging.getLogger("run").info(
            "received signal %s; shutting down cleanly", signum
        )
        raise KeyboardInterrupt

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_term)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument(
        "--universal-config",
        default=None,
        help="higher-priority shared JSON (default: universal.json beside room config)",
    )
    ap.add_argument("--check", action="store_true",
                    help="validate config, token and pool, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the full loop against a fake hub")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="DEBUG level on the console")
    ap.add_argument("--debug-libs", action="store_true",
                    help="also log httpx/httpcore/signalrcore internals")
    ap.add_argument("--errors", action="store_true",
                    help="print WARNING/ERROR lines from this instance's log")
    args = ap.parse_args()

    shared_config_path = universal_config_path(
        args.config, args.universal_config
    ).resolve()
    cfg = load_config(args.config, shared_config_path)
    # Tell LobbyBot exactly which JSON file is authoritative for live admin
    # writes and AI-only external hot reloads. This runtime key is never
    # persisted back into the JSON.
    cfg["_config_path"] = str(Path(args.config).resolve())
    cfg["_universal_config_path"] = str(shared_config_path)
    cfg["_default_config"] = dict(DEFAULT_CONFIG)
    cfg["_instance_name"] = Path(args.config).stem
    if shared_config_path.exists():
        cfg["_universal_config_keys"] = sorted(
            key for key in _read_json_object(
                shared_config_path, label="universal"
            ) if not str(key).startswith("_")
        )
    else:
        cfg["_universal_config_keys"] = []

    from bot.logsetup import configure, extract_problems

    log_file = cfg.get("log_file", "logs/bot.log")
    if args.errors:
        print(extract_problems(log_file))
        return 0

    log_path = configure(
        verbose=args.verbose,
        debug_libs=args.debug_libs,
        log_file=log_file,
    )
    log = logging.getLogger("run")
    log.info("config: %s", Path(args.config).resolve())
    if shared_config_path.exists():
        log.info("universal config (highest priority): %s", shared_config_path)
    else:
        log.info("universal config: %s (not present)", shared_config_path)
    log.info("full session log: %s", log_path.resolve())

    loved_mode = bool(cfg.get("loved_mode", False))
    db_key = "mania_db" if cfg.get("ruleset_id") == 3 else ("loved_db" if loved_mode else "farm_db")
    db = Path(cfg[db_key])
    if not db.exists():
        log.error("%s not found at %s -- set %s in %s",
                  "Loved DB" if loved_mode else "farm DB",
                  db.resolve(), db_key, args.config)
        return 1

    from bot.hub import validate_mods
    validate_mods(cfg.get("allowed_mods", []), cfg.get("required_mods", []))

    if cfg.get("freestyle") and (
        cfg.get("allowed_mods") or cfg.get("required_mods")
    ):
        log.error(
            "freestyle=true requires allowed_mods=[] and required_mods=[]"
        )
        return 1

    if cfg.get("required_mods"):
        log.warning(
            "required_mods=%s -- pool SR values are NOMOD",
            cfg["required_mods"],
        )

    from bot.mania_pool import ManiaPool
    pool_class = ManiaPool if cfg.get("ruleset_id") == 3 else MapPool
    mania_kwargs = {k: v for k, v in cfg.items() if k.startswith("mania_") and k not in ("mania_db", "mania_settings")} if pool_class is ManiaPool else {}
    pool = pool_class(
        str(db),
        sr_min=cfg["sr_min"],
        sr_max=cfg["sr_max"],
        max_length=cfg["max_length"],
        min_passcount=cfg["min_passcount"],
        limit=cfg["pool_limit"],
        ratings_db=cfg.get("ratings_db"),
        stream_enabled=cfg.get("stream_enabled", True),
        stream_share=cfg.get("stream_share", 0.50),
        stream_min_score=cfg.get("stream_min_score", 0.50),
        stream_min_bpm=cfg.get("stream_min_bpm", 140.0),
        stream_min_length=cfg.get("stream_min_length", 45),
        max_map_age_years=cfg.get("max_map_age_years", 0.0),
        loved_mode=loved_mode,
        **mania_kwargs,
    )
    if not pool.maps:
        log.error(
            "pool is empty -- verify the selected database/mode, widen the SR "
            "range, lower min_passcount, or relax map limits"
        )
        return 1
    log.info("pool sample: %s", pool.maps[0].label)

    if args.dry_run:
        from fake_hub import FakeHub, FakeApi
        events: queue.Queue = queue.Queue()
        hub = FakeHub(events)
        api = FakeApi()
        bot = LobbyBot(hub, api, pool, cfg, events)
        log.info("DRY RUN -- no osu! connection, simulated events")
        bot.run()
        return 0

    client_id_env = str(
        cfg.get("oauth_client_id_env") or "OSU_VPS_CLIENT_ID"
    )
    client_secret_env = str(
        cfg.get("oauth_client_secret_env") or "OSU_VPS_CLIENT_SECRET"
    )

    # Deliberately do not fall back to OSU_BOT_CLIENT_ID /
    # OSU_BOT_CLIENT_SECRET. Those names belong to the separate home bot.
    secret = os.environ.get(client_secret_env)
    if not secret:
        log.error(
            "%s is not set (OAuth secret variable selected by %s)",
            client_secret_env,
            args.config,
        )
        return 1

    token_path = Path(cfg["token_file"])
    if not token_path.exists():
        log.error(
            "token file not found: %s -- bootstrap it with get_token.py "
            "--config %s",
            token_path.resolve(),
            args.config,
        )
        return 1

    token_data = json.loads(token_path.read_text(encoding="utf-8"))

    client_id = int(os.environ.get(client_id_env, 0) or 0)
    if not client_id:
        client_id = int(token_data.get("client_id") or 0)
    if not client_id:
        log.error(
            "No OAuth client ID in %s or token file",
            client_id_env,
        )
        return 1

    token_client_id = int(token_data.get("client_id") or 0)
    if token_client_id and token_client_id != client_id:
        log.error(
            "OAuth client mismatch: %s=%s but %s contains client_id=%s. "
            "Refusing to refresh the wrong client's token.",
            client_id_env,
            client_id,
            token_path,
            token_client_id,
        )
        return 1

    log.info(
        "OAuth credentials: client_id=%s via %s; secret via %s",
        client_id,
        client_id_env,
        client_secret_env,
    )

    # Cooperate with the headless supervisor: never refresh its token or open
    # another referee connection for the same account in this project.
    import atexit
    from bot.multiroom import ProcessLease
    token_lease = ProcessLease(token_path.with_suffix(".autohost.lock"))
    token_lease.__enter__()
    atexit.register(token_lease.__exit__)
    tokens = TokenManager(str(token_path), client_id, secret)

    api = OsuApi(
        tokens,
        cfg["requests_per_minute"],
        rate_state_db=cfg.get("api_rate_db"),
        usage_log_seconds=cfg.get("api_usage_log_seconds", 60),
        burst_capacity=cfg.get("requests_burst_capacity", 90),
        profile_cache_seconds=cfg.get("player_profile_cache_seconds", 21600),
    )

    # Resolve the OAuth resource owner once for both --check and live runs.
    # The live bot needs this id to explicitly join/rejoin its multiplayer
    # chat channel via the chat.write_manage endpoint.
    try:
        tok = tokens.access_token()
        if args.check:
            log.info("token OK (%d chars)", len(tok))

        me = api._req("GET", "/me")
        if not me or me.get("id") is None:
            log.error("could not resolve authenticated osu! user")
            return 1

        cfg["_authenticated_user_id"] = int(me["id"])
        cfg["_authenticated_username"] = me.get("username")

        if args.check:
            log.info(
                "authenticated as %s (id %s)",
                me.get("username"), me.get("id")
            )
            if cfg.get("bot_user_id") == me.get("id"):
                log.warning(
                    "bot_user_id is your own account id. In same-account "
                    "operator mode leave bot_user_id=0, otherwise your "
                    "manual commands can be ignored."
                )
            log.info("all checks passed")
            return 0
    finally:
        if args.check:
            api.close()

    account_lease = ProcessLease(Path(__file__).resolve().parent / "state" /
                                 f"osu-account-{int(me['id'])}.lock")
    account_lease.__enter__()
    atexit.register(account_lease.__exit__)
    _install_signal_handlers()

    from bot.hub import RefereeHub
    from bot.chat_websocket import ChatWebSocket
    events = queue.Queue()
    hub = RefereeHub(cfg["spectator_url"], tokens.access_token, events)
    hub.connect()

    chat_websocket = ChatWebSocket(
        tokens.access_token,
        endpoint=cfg.get("chat_websocket_endpoint", "wss://notify.ppy.sh"),
        enabled=cfg.get("chat_websocket_enabled", True),
        reconnect_max_seconds=cfg.get(
            "chat_websocket_reconnect_max_seconds", 30.0
        ),
        invalidate_token=tokens.invalidate_access_token,
    )
    chat_websocket.start()

    bot = LobbyBot(
        hub, api, pool, cfg, events, chat_websocket=chat_websocket
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        chat_websocket.stop()
        hub.stop()
        api.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
