#!/usr/bin/env python3
"""Run independent lobby controllers on ONE account/referee connection.

Selects instances/room1.json through roomN.json from universal.json room_count
(default 2, supported 1-4). Only the first config supplies osu! OAuth
credentials. No lazer installation is required.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import queue
import signal
import threading
import time

from bot.auth import TokenManager
from bot.bot import LobbyBot
from bot.chat_websocket import ChatWebSocket
from bot.discord_control import DiscordControl
from bot.hub import RefereeHub, validate_mods
from bot.multiroom import ProcessLease, SharedReferee, SharedHeadlessApi, SharedDiscord
from bot.pool import MapPool
from bot.mania_pool import ManiaPool
from bot.mode import ruleset_id, validate_mania_mods
from bot.logsetup import PersistentRotatingFileHandler
from run_bot import DEFAULT_CONFIG, load_config, universal_config_path

log = logging.getLogger("supervisor")


def select_room_paths(explicit=None, room_count=None, universal=None):
    """Explicit --configs wins; otherwise launch room1..N from shared config."""
    if explicit is not None:
        if room_count is not None:
            raise ValueError("Use either --configs or --room-count, not both")
        return explicit
    if room_count is None:
        shared_path = universal_config_path(Path("instances/room1.json"), universal)
        data = json.loads(shared_path.read_text(encoding="utf-8")) if shared_path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("universal config root must be an object")
        room_count = data.get("room_count", 2)
    if type(room_count) is not int or not 1 <= room_count <= 4:
        raise ValueError("room_count must be an integer from 1 to 4")
    return [f"instances/room{index}.json" for index in range(1, room_count + 1)]


def load_rooms(paths, universal=None):
    configs = []
    aliases = set()
    for path in paths:
        path = Path(path).resolve()
        if not path.is_file():
            raise ValueError(f"Missing config: {path}; no files have been changed")
        shared_path = universal_config_path(path, universal).resolve()
        cfg = load_config(str(path), shared_path)
        # Native host context is supplied only by its trusted config source,
        # never by a legacy room/universal JSON file.
        for key in ('_host_root', '_host_managed_catalogs', '_host_offline', '_host_installation_id'):
            cfg.pop(key, None)
        alias = str(cfg.get("discord_control_room_alias") or path.stem).strip().casefold()
        if not alias or alias in aliases:
            raise ValueError("Each room needs a distinct room alias/config filename")
        aliases.add(alias)
        cfg.update(_config_path=str(path), _universal_config_path=str(shared_path),
                   _instance_name=alias, _default_config=dict(DEFAULT_CONFIG))
        configs.append(cfg)
    if not 1 <= len(configs) <= 4:
        raise ValueError("Supply between one and four room configs")

    primary = configs[0]
    primary_discord = DiscordControl(primary, start=False)
    if primary.get("discord_control_enabled", True) and not primary_discord._token():
        log.warning("Discord is configured but its bot-token environment variable is empty; Discord will be offline")
    # Shared transport settings have one owner. Ordinary room preferences keep
    # their existing room JSON + universal JSON precedence and hot reload.
    shared_settings = {
        "_headless_supervised": True,
        "close_room_on_exit": True,
        "lazer_coordinator_enabled": False,
        "lazer_chat_patrol_enabled": False,
        "lazer_rejoin_on_hard_restart": False,
        "api_rate_db": primary.get("api_rate_db", "state/api_rate.db"),
        "process_control_db": primary.get("process_control_db", "state/process_control.db"),
        "discord_control_accept_untargeted": False,
    }
    mapping = {}
    public_status_mapping = {}
    for cfg in configs:
        view = DiscordControl(cfg, start=False)
        channel = view._channel_id()
        if channel:
            if channel in mapping.values():
                raise ValueError("Use a distinct Discord channel for each room")
            mapping[cfg["_instance_name"]] = channel
        if cfg.get("discord_control_enabled", True):
            if not channel:
                raise ValueError(f"Set a Discord channel ID for {cfg['_instance_name']} or disable Discord for it")
            if not primary.get("discord_control_enabled", True):
                raise ValueError("Enable Discord on the first room too, or disable it on all rooms")
            if view._guild_id() != int(primary.get("discord_control_guild_id", 0) or 0):
                raise ValueError("Shared Discord requires the same guild for each room")
            if view._token() != primary_discord._token():
                raise ValueError("Shared Discord requires the same Discord bot token")
        public_channel = view._public_status_channel_id()
        if public_channel:
            if not cfg.get("discord_control_enabled", True):
                raise ValueError(
                    f"Enable Discord control for {cfg['_instance_name']} before "
                    "configuring a public status channel"
                )
            if public_channel in public_status_mapping.values():
                raise ValueError("Use a distinct public Discord status channel for each room")
            public_status_mapping[cfg["_instance_name"]] = public_channel
        if Path(cfg["ratings_db"]).resolve() != Path(primary["ratings_db"]).resolve():
            raise ValueError("Rooms must share ratings_db; existing databases were not changed")
    overlap = set(mapping.values()) & set(public_status_mapping.values())
    if overlap:
        raise ValueError(
            "Public status channels must be different from private control/log channels"
        )
    shared_settings.update({
        "discord_control_channel_ids_by_room": mapping,
        "discord_control_known_room_aliases": sorted(aliases),
        "discord_control_primary_instance": primary["_instance_name"],
    })
    for cfg in configs:
        cfg.update(shared_settings)
        cfg["_supervisor_overrides"] = dict(shared_settings)
    return configs


def make_pool(cfg):
    if ruleset_id(cfg) == 2:
        from bot.mode import validate_catch_mods
        from bot.catch_pool import CatchPool, ProductionCatchPool
        validate_catch_mods(cfg.get('allowed_mods', []), cfg.get('required_mods', []), cfg.get('freestyle', False))
        if cfg.get('auto_difficulty', False):
            raise ValueError('Catch requires fixed difficulty')
        managed = cfg.get('_host_managed_catalogs') is True
        if not managed and cfg.get('_host_offline') is not True:
            raise ValueError('Catch requires a signed managed catalog or explicit offline research')
        constructor = ProductionCatchPool if managed else CatchPool
        pool = constructor(cfg['catch_catalog'], purpose='production' if managed else 'research',
                         catch_include_loved=cfg.get('catch_include_loved', False),
                         catch_min_length=cfg.get('catch_min_length', 0),
                         **{k: cfg[k] for k in ('sr_min','sr_max','max_length','min_passcount','max_map_age_years','ratings_db') if k in cfg},
                         limit=cfg.get('pool_limit', 200000))
        from poolrotater_host.ratings.reader import for_settings
        pool.ratings_reader = for_settings(cfg)
        return pool
    mania = ruleset_id(cfg) == 3
    if mania:
        validate_mania_mods(cfg.get("allowed_mods", []), cfg.get("required_mods", []), cfg.get("freestyle", False))
        if cfg.get("auto_difficulty", False) or cfg.get("loved_mode", False):
            raise ValueError("Mania requires fixed difficulty and its own catalog")
    validate_mods(cfg.get("allowed_mods", []), cfg.get("required_mods", []))
    if cfg.get("freestyle") and (cfg.get("allowed_mods") or cfg.get("required_mods")):
        raise ValueError("freestyle=true needs empty allowed_mods and required_mods")
    loved_mode = bool(cfg.get("loved_mode", False))
    db_key = "mania_db" if mania else ("loved_db" if loved_mode else "farm_db")
    db_path = Path(cfg[db_key])
    if not db_path.is_file():
        raise ValueError(f"Missing {db_key}: {db_path}")
    keys = ("sr_min", "sr_max", "max_length", "min_passcount", "ratings_db",
            "stream_enabled", "stream_share", "stream_min_score", "stream_min_bpm",
            "stream_min_length", "max_map_age_years", "loved_mode")
    extra = {k: v for k, v in cfg.items() if k.startswith("mania_") and k not in ("mania_db", "mania_settings")} if mania else {}
    pool = (ManiaPool if mania else MapPool)(str(db_path), limit=cfg["pool_limit"], **extra, **{
        key: cfg[key] for key in keys if key in cfg
    })
    from poolrotater_host.ratings.reader import for_settings
    pool.ratings_reader=for_settings(cfg)
    if not pool.maps:
        raise ValueError(f"{cfg['_instance_name']} has an empty map pool")
    return pool


def credentials(cfg):
    path = Path(cfg["token_file"])
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("refresh_token"):
        raise ValueError("OAuth token file has no refresh token; file left unchanged")
    env = cfg["oauth_client_id_env"]
    client_id = int(os.environ.get(env, "0") or data.get("client_id") or 0)
    stored = int(data.get("client_id") or 0)
    if not client_id or (stored and stored != client_id):
        raise ValueError("OAuth client ID missing/mismatched; token file left unchanged")
    secret_env = cfg["oauth_client_secret_env"]
    secret = os.environ.get(secret_env)
    if not secret:
        raise ValueError(f"{secret_env} is not set; load secrets/room1.ps1")
    return path, client_id, secret


def setup_logging(configs, verbose):
    from bot.logsetup import configure
    configure(verbose=verbose, log_file="logs/multiroom.log")
    formatter = logging.Formatter(
        "%(asctime)s [%(threadName)s] %(name)s %(levelname)s %(message)s",
        "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setFormatter(formatter)
    used = {Path("logs/multiroom.log").resolve()}
    for cfg in configs:
        alias = cfg["_instance_name"]
        path = Path(cfg.get("log_file") or f"logs/{alias}.log").resolve()
        if path in used:
            raise ValueError("Each room must use a distinct log_file")
        used.add(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = PersistentRotatingFileHandler(path)
        handler.setFormatter(formatter)
        handler.addFilter(lambda record, alias=alias: record.threadName == alias)
        root.addHandler(handler)


class Supervisor:
    def __init__(self, configs, pools, api, hub, events, chat_websocket=None):
        self.configs, self.pools, self.api = configs, pools, api
        self.shared = SharedReferee(hub, events)
        self.discord = SharedDiscord(configs)
        self.chat_websocket = chat_websocket
        self.bots = []
        self.threads = []
        self._threads_by_alias = {}
        self._room_specs = {
            cfg["_instance_name"]: (cfg, pool)
            for cfg, pool in zip(configs, pools)
        }
        self._reopenable_rooms = set()
        self._state_lock = threading.Lock()
        self.failures = []
        self._stop_requested_at = None

    def autodiff_lobby_for(self, requester):
        """Advertise an online AutoDiff sibling with the same pool family."""
        requester_loved = bool(requester.cfg.get("loved_mode", False))
        for bot in self.bots:
            if (bot is requester or not bot.running or not bot._room_ready
                    or bot.cfg.get("ruleset_id", 0) != requester.cfg.get("ruleset_id", 0)
                    or bot.manual_range or not bot.cfg.get("auto_difficulty", True)
                    or bool(bot.cfg.get("loved_mode", False)) != requester_loved):
                continue
            rid = bot.room_id
            if rid is not None:
                return {"alias": bot.cfg["_instance_name"], "room_id": int(rid)}
        return None

    def request_stop(self):
        if self._stop_requested_at is not None:
            return
        self._stop_requested_at = time.monotonic()
        log.warning("shutdown requested once; bounded room cleanup in progress")
        self.shared.begin_shutdown(float(self.configs[0].get("shutdown_timeout_seconds", 45.0)))
        self.api.cancel_pending.set()
        for bot in self.bots:
            bot.running = False

    def _make_bot(self, alias, *, discord_view=None, fresh_pool=False):
        cfg, pool = self._room_specs[alias]
        if fresh_pool:
            # The old MapPool may still carry a temporary AutoDiff/community
            # window from the session that was just parked. Rebuild from the
            # saved config baseline so a reopened lobby starts cleanly; normal
            # JSON hot reload resumes as soon as the new worker enters its loop.
            pool = make_pool(cfg)
            self._room_specs[alias] = (cfg, pool)
            index = next(
                i for i, room_cfg in enumerate(self.configs)
                if room_cfg["_instance_name"] == alias
            )
            self.pools[index] = pool
        events = queue.Queue()
        view = self.shared.room_view(alias, events)
        if discord_view is None:
            discord_view = self.discord.view_for(alias)
        bot = LobbyBot(
            view,
            self.api,
            pool,
            cfg,
            events,
            discord_control=discord_view,
            chat_websocket=self.chat_websocket,
        )
        bot._autodiff_lobby_lookup = self.autodiff_lobby_for
        return bot

    def _launch_bot(self, bot):
        alias = bot.cfg["_instance_name"]
        thread = threading.Thread(
            target=self._worker,
            args=(bot,),
            name=alias,
            daemon=True,
        )
        with self._state_lock:
            self.bots.append(bot)
            self.threads.append(thread)
            self._threads_by_alias[alias] = thread
        thread.start()
        return thread

    def _discord_reply(self, command, text):
        try:
            self.discord.send_to_channel(text, channel_id=command.channel_id)
        except Exception:
            log.exception("could not send supervisor Discord reply")

    def _reopen_room(self, alias, command):
        normalized = str(alias or "").strip().casefold()
        if normalized not in self._room_specs:
            self._discord_reply(command, f"Unknown room: {normalized or alias}.")
            return False
        if self.shared.stopping.is_set():
            self._discord_reply(command, "Supervisor is shutting down; room cannot be reopened.")
            return False

        with self._state_lock:
            thread = self._threads_by_alias.get(normalized)
            if thread is not None and thread.is_alive():
                self._discord_reply(command, f"{normalized} is already running.")
                return False
            if normalized not in self._reopenable_rooms:
                self._discord_reply(
                    command,
                    f"{normalized} is not parked by close-when-empty; refusing to start a second controller.",
                )
                return False
            # Reserve this room before constructing the replacement so two
            # rapid .open messages can never start duplicate controllers.
            self._reopenable_rooms.remove(normalized)

        discord_view = None
        try:
            discord_view = self.discord.reopen_view(normalized)
            bot = self._make_bot(
                normalized,
                discord_view=discord_view,
                fresh_pool=True,
            )
            self._discord_reply(command, f"Reopening {normalized}; creating a fresh osu! lobby now.")
            self._launch_bot(bot)
            log.info("%s reopened from Discord supervisor command", normalized)
            return True
        except Exception as exc:
            # Construction/thread-start failures happen before a new room is
            # known to exist, so the previously confirmed parked state remains
            # safe to retry. Unknown MakeRoom outcomes happen inside _worker
            # after thread start and are deliberately not re-armed here.
            with self._state_lock:
                current = self._threads_by_alias.get(normalized)
                if current is None or not current.is_alive():
                    self._reopenable_rooms.add(normalized)
            if discord_view is not None:
                # No LobbyBot owns this newly-created view if construction or
                # thread start failed. Park it again so ordinary room commands
                # cannot accumulate for a nonexistent worker; .open remains
                # available through SharedDiscord.command_targets().
                discord_view.stop()
            log.exception("could not reopen %s", normalized)
            self._discord_reply(command, f"Could not reopen {normalized}: {exc}")
            return False

    def _drain_supervisor_commands(self):
        while True:
            try:
                alias, command = self.discord.get_supervisor_command_nowait()
            except queue.Empty:
                return

            prefix = str(self.configs[0].get("command_prefix", ".") or ".")
            text = str(command.content or "").strip()
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
            parts = text.split()
            name = parts[0].casefold() if parts else ""
            if name in {"open", "reopen"}:
                self._reopen_room(alias, command)
            elif (
                name == "shutdown"
                and len(parts) == 2
                and parts[1].casefold() == "all"
            ):
                self._discord_reply(command, "Shutting down all lobby rooms and the supervisor.")
                self.request_stop()

    def _worker(self, bot):
        normal_return = False
        try:
            if not self.shared.stopping.is_set():
                bot.run()
                normal_return = True
        except BaseException as exc:
            # A failed room cannot tear down another room's session. Do not
            # blind-recreate after an unknown MakeRoom result or close failure.
            self.failures.append(bot.cfg["_instance_name"])
            log.error("%s stopped unexpectedly: %s", bot.cfg["_instance_name"], exc,
                      exc_info=not isinstance(exc, SystemExit))
        finally:
            # Also covers exceptions during create_room(), before run's finally.
            # shutdown clears room_id; normal shutdown will not re-close it.
            if bot.room_id or bot.running:
                bot.running = False
                try:
                    bot.shutdown()
                except BaseException:
                    log.exception("room cleanup failed")
            alias = bot.cfg["_instance_name"]
            if (
                normal_return
                and getattr(bot, "_supervisor_reopenable_stop", False)
                and not getattr(bot.hub, "owned", set())
                and not self.shared.stopping.is_set()
            ):
                with self._state_lock:
                    self._reopenable_rooms.add(alias)
                log.info(
                    "%s parked after close-when-empty; Discord .open can recreate it",
                    alias,
                )
            elif normal_return and getattr(bot, "_supervisor_reopenable_stop", False):
                log.error(
                    "%s requested close-when-empty but room closure was not confirmed; "
                    "reopen is disabled to avoid a duplicate room",
                    alias,
                )
            log.info("%s stopped; other rooms keep running", alias)

    def run(self):
        previous = {}
        def on_stop(signum, frame):
            self.request_stop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, on_stop)
            self.shared.hub.connect()
            self.shared.start_dispatch()
            if self.chat_websocket is not None:
                self.chat_websocket.start()
            for cfg, pool, discord_view in zip(self.configs, self.pools, self.discord.views):
                if self.shared.stopping.is_set():
                    break
                bot = self._make_bot(
                    cfg["_instance_name"], discord_view=discord_view
                )
                self.bots.append(bot)
            self.discord.start()
            initial_bots = list(self.bots)
            # _launch_bot registers bots. The initial batch was registered
            # above so build their threads directly to avoid duplicate entries.
            for bot in initial_bots:
                if self.shared.stopping.is_set():
                    break
                thread = threading.Thread(target=self._worker, args=(bot,),
                                          name=bot.cfg["_instance_name"], daemon=True)
                self.threads.append(thread)
                self._threads_by_alias[bot.cfg["_instance_name"]] = thread
                thread.start()
            while True:
                self._drain_supervisor_commands()
                if self.shared.stopping.is_set() and self.shared.remaining_shutdown() <= 0:
                    log.error("shutdown deadline reached; stopping transports")
                    break
                alive = [thread for thread in self.threads if thread.is_alive()]
                if not alive:
                    with self._state_lock:
                        parked = bool(self._reopenable_rooms)
                    discord_thread = getattr(self.discord, "_thread", None)
                    discord_available = bool(
                        discord_thread is not None and discord_thread.is_alive()
                    )
                    if (
                        parked
                        and discord_available
                        and not self.shared.stopping.is_set()
                    ):
                        # Keep the shared referee/API/Discord transports alive
                        # so an admin can issue .open even when every room is
                        # intentionally parked.
                        time.sleep(0.3)
                        continue
                    if parked and not discord_available:
                        log.warning(
                            "parked rooms remain but Discord control is unavailable; "
                            "supervisor will exit and a normal restart is required"
                        )
                    break
                for thread in alive:
                    thread.join(timeout=0.3)
        finally:
            self.request_stop()
            while (any(thread.is_alive() for thread in self.threads)
                   and self.shared.remaining_shutdown() > 0):
                for thread in self.threads:
                    thread.join(timeout=0.3)
            for bot in self.bots:
                try:
                    # Workers own shutdown, including startup exceptions. Never
                    # race them by closing or recording the same room twice.
                    # stop auxiliary workers even if startup never reached run()
                    bot.ai.stop(timeout=0)
                    bot.discord_modlog.stop(timeout=0)
                except Exception:
                    log.exception("auxiliary worker cleanup failed")
            # Any IDs still owned are cleanup failures, never foreign rooms.
            remaining_rooms = []
            for view in self.shared.views:
                for rid in list(view.owned):
                    remaining_rooms.append(rid)
            if remaining_rooms:
                self.failures.append("cleanup")
                log.error("Unconfirmed room closures. After this process exits run: python close_room.py --config \"%s\" %s",
                          self.configs[0].get("_config_path", "instances/room1.json"),
                          " ".join(str(rid) for rid in sorted(remaining_rooms)))
            self.discord.flush(timeout=1.5)
            self.discord.stop(timeout=3)
            if self.chat_websocket is not None:
                self.chat_websocket.stop()
            self.shared.stop()
            # In-flight HTTP may still be unwinding. Do not block termination
            # on its client's close lock after the shutdown deadline.
            threading.Thread(target=self.api.close, name="http-cleanup", daemon=True).start()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return 2 if self.failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configs", nargs="+", help="explicit room configs; overrides JSON room_count")
    parser.add_argument("--room-count", type=int, choices=range(1, 5), help="override universal.json room_count")
    parser.add_argument("--universal-config")
    parser.add_argument("--check", action="store_true", help="offline config/pool/credential-file checks; no connection or room")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    os.chdir(Path(__file__).resolve().parent)
    try:
        paths = select_room_paths(args.configs, args.room_count, args.universal_config)
        configs = load_rooms(paths, args.universal_config)
        setup_logging(configs, args.verbose)
        pools = [make_pool(cfg) for cfg in configs]
        token_file, client_id, secret = credentials(configs[0])
        if args.check:
            log.info("offline checks passed for %s; live OAuth/Discord connectivity not tested",
                     ", ".join(cfg["_instance_name"] for cfg in configs))
            return 0
        with ExitStack() as stack:
            stack.enter_context(ProcessLease(token_file.with_suffix(".autohost.lock")))
            tokens = TokenManager(str(token_file), client_id, secret)
            cfg = configs[0]
            api = SharedHeadlessApi(tokens, per_minute=cfg["requests_per_minute"],
                                    rate_state_db=cfg["api_rate_db"],
                                    burst_capacity=cfg.get("requests_burst_capacity", 90),
                                    profile_cache_seconds=cfg.get("player_profile_cache_seconds", 21600))
            # Supervisor closes the client; avoid a second blocking close on
            # ExitStack after an exhausted shutdown deadline.
            me = api._req("GET", "/me")
            if not isinstance(me, dict) or not me.get("id"):
                raise ValueError("Could not resolve OAuth account; no referee connection opened")
            stack.enter_context(ProcessLease(Path("state") / f"osu-account-{int(me['id'])}.lock"))
            for cfg in configs:
                cfg.update(_authenticated_user_id=int(me["id"]),
                           _authenticated_username=me.get("username"))
            log.info("HEADLESS: %s (%s), %d rooms, ONE referee connection, lazer disabled",
                     me.get("username"), me["id"], len(configs))
            events = queue.Queue()
            hub = RefereeHub(configs[0]["spectator_url"], tokens.access_token, events)
            chat_websocket = ChatWebSocket(
                tokens.access_token,
                endpoint=cfg.get(
                    "chat_websocket_endpoint", "wss://notify.ppy.sh"
                ),
                enabled=cfg.get("chat_websocket_enabled", True),
                reconnect_max_seconds=cfg.get(
                    "chat_websocket_reconnect_max_seconds", 30.0
                ),
                invalidate_token=tokens.invalidate_access_token,
            )
            return Supervisor(
                configs, pools, api, hub, events, chat_websocket
            ).run()
    except Exception as exc:
        log.error("Supervisor stopped: %s", exc, exc_info=args.verbose)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
