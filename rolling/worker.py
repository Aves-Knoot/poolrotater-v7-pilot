"""A pinned release's lobby controller. Only the main thread mutates LobbyBot."""
from __future__ import annotations

import logging
import queue
import signal
import threading
import time

from bot.bot import LobbyBot as BaseLobbyBot, IDLE, PLAYING, STARTING
from poolrotater_host.catalog_exclusions import ExclusionLobbyMixin


class LobbyBot(ExclusionLobbyMixin, BaseLobbyBot):
    pass
from bot.discord_control import DiscordCommand
from run_multiroom import make_pool
from bot.mode import ruleset_id
from bot.hub import validate_mods
from bot.pool import MapPool
from bot.standard_pool import StandardPool
from pathlib import Path
from . import PROTOCOL
from .adapters import Api, Chat, Discord, Hub, ModLog, Siblings
from .protocol import Peer
from .release import load_release
from .storage import install_guard
from .maintenance import MaintenanceGate

log = logging.getLogger("rolling.worker")


def pin_standard_catalogs(cfg, release):
    """Switch a Standard worker's map/DT pair together, never shared runtime DBs."""
    if cfg.get('_host_managed_catalogs') is True:
        return  # Native host catalogs were validated and pinned by its config source.
    if ruleset_id(cfg) != 0 or cfg.get('loved_mode', False):
        return
    farm = Path(release).resolve() / 'catalog-data/standard_farm.db'
    dt = Path(release).resolve() / 'catalog-data/standard_dt.db'
    if farm.is_file():
        if not dt.is_file():
            raise ValueError('Pinned Standard source requires its matching DT catalog')
        # Reloads must retain this release-local pair rather than reverting to
        # legacy runtime paths from the unchanged operator JSON.
        overrides = cfg.setdefault('_supervisor_overrides', {})
        for key, path in (('farm_db', farm), ('standard_dt_db', dt)):
            cfg[key] = overrides[key] = str(path)
    else:
        cfg.setdefault('standard_dt_db', str(dt))



def pin_mania_validation(cfg, release):
    """Declare the sealed conversion evidence before installing the storage guard."""
    if cfg.get('_host_managed_catalogs') is True:
        return
    if ruleset_id(cfg) != 3 or not cfg.get('mania_convert_db'):
        return
    evidence = Path(release).resolve() / 'catalog-data/mania_convert_validation.db'
    if not evidence.is_file():
        raise ValueError('Mania converts require the release-sealed validation catalog')
    cfg['mania_convert_validation_db'] = str(evidence)
    cfg.setdefault('_supervisor_overrides', {})['mania_convert_validation_db'] = str(evidence)


def make_worker_pool(cfg):
    """Worker-local Ranked policy; shared v6.7.2 factory/contracts stay pinned."""
    if ruleset_id(cfg) == 3 or cfg.get('loved_mode', False):
        return _managed_policy(make_pool(cfg), cfg)
    validate_mods(cfg.get('allowed_mods', []), cfg.get('required_mods', []))
    if cfg.get('freestyle') and (cfg.get('allowed_mods') or cfg.get('required_mods')):
        raise ValueError('freestyle=true needs empty allowed_mods and required_mods')
    path = Path(cfg['farm_db'])
    if not path.is_file():
        raise ValueError(f'Missing farm_db: {path}')
    keys = ('sr_min', 'sr_max', 'max_length', 'min_passcount', 'ratings_db',
            'stream_enabled', 'stream_share', 'stream_min_score', 'stream_min_bpm',
            'stream_min_length', 'max_map_age_years', 'loved_mode')
    policy = {key: cfg.get(key, default) for key, default in (
        ('sparse_passcount_enabled', True), ('sparse_passcount_min_maps', 50),
        ('sparse_passcount_floor', 1000))}
    dt = {key: cfg[key] for key in ('standard_dt_db','dt_auto_threshold','dt_pick_share','standard_catalog_binding') if key in cfg}
    if set(cfg.get('required_mods', [])) - {'NF'}:
        # A plain-DT catalog does not prove DT+HR/EZ/other fixed-mod ratings.
        dt = {}
    if cfg.get('_host_managed_catalogs') is True and not dt.get('standard_dt_db'):
        dt = {}  # An unadvertised companion must not adopt a legacy default path.
    constructor = StandardPool if 'standard_dt_db' in dt else MapPool
    pool = constructor(str(path), limit=cfg['pool_limit'], **policy, **dt,
                   **{key: cfg[key] for key in keys if key in cfg})
    if not pool.maps:
        raise ValueError(f"{cfg['_instance_name']} has an empty map pool")
    return _managed_policy(pool, cfg)


def _managed_policy(pool, cfg):
    if cfg.get('_host_catalog_set'):
        from poolrotater_host.catalog_store import CatalogStore
        from poolrotater_host.catalog_exclusions import ExclusionPolicy, attach_policy
        store = CatalogStore.open(cfg['_host_root'])
        mode = 'standard' if ruleset_id(cfg) == 0 else 'mania'+str(cfg['mania_keys'])
        reference = store._validate_reference(cfg['_host_catalog_reference'])
        if reference['set_id'] != cfg['_host_catalog_set']: raise ValueError('worker catalog reference/set mismatch')
        pool = attach_policy(pool, ExclusionPolicy(store, mode, reference['modes'][mode]))
    return pool


class Lifecycle:
    def __init__(self, peer):
        self.peer = peer
        self.commands = queue.Queue(maxsize=16)
        self.activate = threading.Event()
        self.stop = threading.Event()
        self.paused = False
        self.bot = None
        self.last_status = 0
        self.last_control = None
        self.maintenance = MaintenanceGate()

    def handle(self, method, *args, **kwargs):
        if method == "activate":
            self.activate.set()
            return True
        if method == "stop":
            # Parent owns closure for infrastructure shutdown/rollout. A signal
            # or lost parent must never cause a worker to close a populated room.
            self.stop.set()
            self.activate.set()
            if self.bot:
                self.bot.cfg["close_room_on_exit"] = False
                self.bot.running = False
            return True
        if method not in ("prepare", "resume", "flush"):
            raise ValueError("unknown lifecycle command")
        request_id, = args
        self.commands.put_nowait((method, request_id))
        return True  # Receipt only; acknowledgement comes from the main loop.

    def status(self, bot):
        def map_info(value):
            if value is None:
                return None
            return {"beatmap_id": int(value.beatmap_id), "label": str(value.label)[:240],
                    "stars": float(value.sr), "hit_length": int(value.hit_length)}
        shown = ((bot.playing_map or bot.current_map) if bot.state == PLAYING else
                 (bot.starting_map or bot.current_map) if bot.state == STARTING else bot.current_map)
        upcoming = [value for _, value in bot.queued]
        if (bot.state in (STARTING, PLAYING) and shown and bot.current_map
                and shown.beatmap_id != bot.current_map.beatmap_id):
            upcoming.insert(0, bot.current_map)
        return {
            "version": bot.cfg["version"], "room_id": bot.room_id,
            "ready": bool(bot._room_ready), "running": bool(bot.running),
            "state": bot.state, "players": len(bot.players),
            "paused": self.paused or self.maintenance.locked or self.maintenance.uncertain,
            "maintenance": self.maintenance.status(), "control": self.last_control,
            "reopenable": bool(bot._supervisor_reopenable_stop),
            "auto": bool(bot.cfg.get("auto_difficulty", True) and not bot.manual_range),
            "ruleset": bot.cfg.get("ruleset_id", 0), "loved": bool(bot.cfg.get("loved_mode", False)),
            "public": {"state": bot.state, "players": len(bot.players),
                       "current_map": map_info(shown), "queue": [map_info(m) for m in upcoming[:8]],
                       "queue_known": bool(bot._room_ready),
                       "match_started_at": bot._match_started_at if bot.state == PLAYING else None},
        }

    def tick(self, bot):
        self.bot = bot
        if self.peer.closed.is_set() or self.stop.is_set():
            bot.cfg["close_room_on_exit"] = False
            bot.running = False
            return False
        maintenance_paused = self.maintenance.tick(bot)
        if not maintenance_paused:
            from poolrotater_host.catalog_exclusions import reconcile_exclusions
            reconcile_exclusions(bot)
        try:
            command, request_id = self.commands.get_nowait()
        except queue.Empty:
            command = None
        if command == "prepare":
            self.paused = bool(bot._room_ready and bot.state == IDLE and not bot.players
                               and bot.events.empty() and not bot._hub_rejoin_pending)
            self.last_control = {"id": request_id, "command": command, "ok": self.paused}
        elif command == "resume":
            self.paused = False
            self.last_control = {"id": request_id, "command": command, "ok": True}
        elif command == "flush":
            ok = self.paused and not bot.players and bot.state == IDLE
            if ok:
                bot._flush_all_lobby_time()
            self.last_control = {"id": request_id, "command": command, "ok": ok}
        if command or time.monotonic() - self.last_status >= 1:
            self.peer.emit("status", self.status(bot))
            self.last_status = time.monotonic()
        if self.paused or maintenance_paused:
            time.sleep(.1)
        return not (self.paused or maintenance_paused)


def run(sock, release):
    from contextlib import ExitStack
    catalog_pins = ExitStack()
    manifest = load_release(release)
    lifecycle = None
    events = queue.Queue(maxsize=2048)
    discord = None
    chat = None

    def event(name, value):
        if name == "hub":
            events.put_nowait(tuple(value))
        elif name == "chat" and chat:
            chat.event(value)
        elif name == "discord" and discord:
            discord.enqueue_command(DiscordCommand(**value))
        else:
            raise ValueError("unexpected supervisor event")

    peer = Peer(sock, handler=lambda *a, **kw: lifecycle.handle(*a, **kw), on_event=event,
                name="supervisor")
    lifecycle = Lifecycle(peer)
    lifecycle.maintenance.release = Path(release).resolve()
    peer.start()
    bot = None
    try:
        cfg = peer.call("worker.hello", {"protocol": PROTOCOL, "version": manifest["version"],
                                        "files": manifest["files"]})
        cfg.update(version=manifest["version"], _rolling_worker=True, close_room_on_exit=False)
        cfg.setdefault("_supervisor_overrides", {}).update(version=manifest["version"],
                                                          _rolling_worker=True, close_room_on_exit=False)
        pin_standard_catalogs(cfg, release)
        pin_mania_validation(cfg, release)
        if cfg.get("_rolling_schema_locked"):
            install_guard(cfg)
        # Import/config/catalog validation occurs while the existing controller
        # still runs. LobbyBot's persistent stores start only after activation.
        # The shared supervisor remains backward compatible: only this new
        # worker interprets the opt-in duration policy before its first query.
        from bot.pool import MapPool
        duration_policy = MapPool.duration_policy_from_config(cfg)
        if duration_policy is not None:
            cfg.update(duration_policy, max_length=duration_policy['real_max_length'])
        pool = make_worker_pool(cfg)
        if cfg.get('_host_catalog_set'):
            from poolrotater_host.catalog_store import CatalogStore
            mode = 'standard' if ruleset_id(cfg) == 0 else 'mania'+str(cfg['mania_keys'])
            catalog_pins.enter_context(CatalogStore.open(cfg['_host_root']).worker_pin(cfg['_host_catalog_reference'], modes=[mode]))
        peer.emit("prepared", {"version": manifest["version"], "maps": len(pool.maps)})
        while not lifecycle.activate.wait(.2):
            if peer.closed.is_set():
                return 2
        if lifecycle.stop.is_set() or peer.closed.is_set():
            return 0
        from bot.logsetup import configure
        threading.current_thread().name = cfg["_instance_name"]
        configure(log_file=cfg.get("log_file") or f"logs/{cfg['_instance_name']}.log")
        formatter = logging.Formatter("%(asctime)s [%(threadName)s] %(name)s %(levelname)s %(message)s",
                                      "%Y-%m-%d %H:%M:%S")
        for handler in list(logging.getLogger().handlers):
            if not isinstance(handler, logging.FileHandler):
                # Active output belongs in the existing rotating room log;
                # keep the per-process startup file small over long uptimes.
                logging.getLogger().removeHandler(handler)
                continue
            handler.setFormatter(formatter)
        log.info("starting %s controller from pinned %s", cfg["_instance_name"], manifest["version"])
        discord, chat = Discord(peer, cfg), Chat(peer)
        bot = LobbyBot(Hub(peer), Api(peer), pool, cfg, events,
                       discord_control=discord, chat_websocket=chat,
                       discord_modlog=ModLog(peer, cfg), lifecycle=lifecycle)
        if not cfg.get("_rolling_schema_locked"):
            from bot.player_preferences import preference
            preference(cfg["ratings_db"], "schema-init", 0)
            install_guard(cfg)
        lifecycle.bot = bot
        # Reconcile hot settings once even if a file changed between preparation
        # and construction. Constructor/account settings remain pinned above.
        bot._config_mtime_ns = None
        bot._universal_config_mtime_ns = None
        bot._autodiff_lobby_lookup = Siblings(peer).autodiff_lobby_for

        def stop(signum, frame):
            lifecycle.handle("stop")
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        # The supervisor blocks these only while pinning child ownership. Masks
        # survive exec; restore delivery once this child's handlers are ready.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGINT, signal.SIGTERM))
        bot.run()
        peer.emit("exited", lifecycle.status(bot))
        return 0
    except BaseException as exc:
        log.exception("room worker failed")
        try:
            peer.emit("failure", {"kind": type(exc).__name__, "message": str(exc)[:800]})
        except Exception:
            pass
        return 2
    finally:
        if bot:
            # Supervisor makes infrastructure close/recovery decisions. The
            # normal .closewhenempty path already closed its room explicitly.
            bot.cfg["close_room_on_exit"] = False
            bot.shutdown()
        peer.close()
        catalog_pins.close()
