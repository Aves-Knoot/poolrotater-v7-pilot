#!/usr/bin/env python3
"""v6.7 process supervisor. Explicit --serve is required to connect to osu!."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import logging
import os
from pathlib import Path
import queue
import subprocess

from bot.auth import TokenManager
from bot.chat_websocket import ChatWebSocket
from bot.hub import RefereeHub
from bot.multiroom import ProcessLease, SharedHeadlessApi
from rolling.release import load_release, require_immutable
from rolling.state import Registry
from rolling.supervisor import RollingSupervisor
from run_multiroom import credentials, load_rooms, make_pool, select_room_paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="offline release/config/catalog checks only")
    mode.add_argument("--serve", action="store_true", help="connect and operate live lobbies")
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--configs", nargs="+")
    parser.add_argument("--universal-config")
    parser.add_argument("--room-count", type=int, choices=range(1, 5))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    release = args.release.resolve()
    runtime = args.runtime.resolve(strict=True)
    if release != Path(__file__).resolve().parent:
        parser.error("launch run_rolling.py from the specified pinned release")
    os.chdir(runtime)
    manifest = load_release(release)
    paths = select_room_paths(args.configs, args.room_count, args.universal_config)
    configs = load_rooms(paths, args.universal_config)
    logs = [str(Path(cfg.get("log_file") or f"logs/{cfg['_instance_name']}.log").resolve()) for cfg in configs]
    if len(logs) != len(set(logs)) or str((runtime / "logs/multiroom.log").resolve()) in logs:
        raise ValueError("each room must have a distinct log file separate from the supervisor")
    for cfg in configs:
        cfg["version"] = manifest["version"]
        cfg["_supervisor_overrides"]["version"] = manifest["version"]
    registry = Registry(runtime / "state/rolling/ownership.json")
    registry.require_clean_start()
    if args.check:
        for cfg in configs:
            # Catalogs belong to the pinned release, while mutable stores live
            # under runtime. Match the worker's configuration during preflight.
            for key, value in list(cfg.items()):
                if key.endswith("_db") and isinstance(value, str) and value:
                    path = Path(value)
                    base = release if path.parts[0] in ("catalog-data", "farm-data") else runtime
                    cfg[key] = str((base / path).resolve())
            make_pool(cfg)
        print(f"{manifest['version']}: release and {len(configs)} room catalogs passed offline checks; no connections opened")
        return 0
    if os.geteuid() == 0:
        raise RuntimeError("run the live supervisor as the unprivileged poolrotator service account")
    require_immutable(release, manifest)
    from bot.logsetup import configure
    configure(log_file=str(runtime / "logs/multiroom.log"))
    formatter = logging.Formatter(
        "%(asctime)s [%(threadName)s] %(name)s %(levelname)s %(message)s",
        "%Y-%m-%d %H:%M:%S")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    # The legacy deploy timer would stop the entire service despite mixed room
    # versions. Activation must deliberately retire it first.
    for unit in ("poolrotator-deploy.timer", "poolrotator-deploy.service"):
        result = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
        if result.stdout.strip() in ("active", "activating", "reloading"):
            raise RuntimeError(f"{unit} is active; retire the legacy whole-service deployer before activation")
    token_file, client_id, secret = credentials(configs[0])
    with ExitStack() as stack:
        stack.enter_context(ProcessLease(runtime / "state/rolling/supervisor.lock"))
        stack.enter_context(ProcessLease(token_file.with_suffix(".autohost.lock")))
        tokens = TokenManager(str(token_file), client_id, secret)
        cfg = configs[0]
        api = SharedHeadlessApi(tokens, per_minute=cfg["requests_per_minute"],
                                rate_state_db=cfg["api_rate_db"],
                                burst_capacity=cfg.get("requests_burst_capacity", 90),
                                profile_cache_seconds=cfg.get("player_profile_cache_seconds", 21600))
        me = api._req("GET", "/me")
        if not isinstance(me, dict) or not me.get("id"):
            raise RuntimeError("OAuth identity unavailable; no referee connection opened")
        stack.enter_context(ProcessLease(runtime / "state" / f"osu-account-{int(me['id'])}.lock"))
        for cfg in configs:
            cfg.update(_authenticated_user_id=int(me["id"]), _authenticated_username=me.get("username"))
        events = queue.Queue()
        hub = RefereeHub(configs[0]["spectator_url"], tokens.access_token, events)
        chat = ChatWebSocket(tokens.access_token,
                            endpoint=cfg.get("chat_websocket_endpoint", "wss://notify.ppy.sh"),
                            enabled=cfg.get("chat_websocket_enabled", True),
                            reconnect_max_seconds=cfg.get("chat_websocket_reconnect_max_seconds", 30),
                            invalidate_token=tokens.invalidate_access_token)
        return RollingSupervisor(configs, api, hub, events, chat, release, runtime).run()


if __name__ == "__main__":
    raise SystemExit(main())
