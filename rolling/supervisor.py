"""Persistent account owner with independently pinned room processes.

Only one rollout runs at a time. Preparation failures leave the current room
running. A timeout after a remote mutation is an unknown outcome, never a cue
to create another room. Other room processes and account connections survive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
import logging
import os
from pathlib import Path
import queue
import secrets
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import uuid

from bot.hub import HubTransportError
from bot.discord_modlog import DiscordModLog
from bot.multiroom import SharedReferee, SharedDiscord
from .broker import Broker
from .protocol import Peer
from .release import atomic_json, load_release, require_compatible, require_immutable, IncompatibleRelease
from .state import Registry, schemas
from .supervisor_settings import deployment_seconds, read_settings

log = logging.getLogger("rolling.supervisor")


class HandoffDeferred(RuntimeError):
    """A definite local eligibility refusal before any room mutation."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass
class Worker:
    alias: str
    release: Path
    manifest: dict
    broker: Broker
    process: object = None
    signal_target: object = None
    peer: object = None
    prepared: threading.Event = field(default_factory=threading.Event)
    status: dict = field(default_factory=dict)
    status_at: float = 0
    failure: str = ""
    healthy_since: float | None = None
    empty_since: float | None = None
    empty_epoch: int = -1
    handoff_retry_at: float = 0.0
    handoff_deferrals: int = 0
    failure_cleanup_deadline: float | None = None
    exited: bool = False
    started_at: float = field(default_factory=time.time)

    def fresh(self, now=None):
        now = time.monotonic() if now is None else now
        return (self.process is not None and self.process.poll() is None
                and not self.peer.closed.is_set() and now - self.status_at < 15)

    def display_fresh(self, now=None):
        """Grace for public telemetry only; never grants mutation/update admission."""
        from poolrotater_host.network.projection import DISPLAY_OBSERVATION_SECONDS
        now = time.monotonic() if now is None else now
        return (self.process is not None and self.process.poll() is None
                and self.peer is not None and not self.peer.closed.is_set()
                and 0 <= now - self.status_at < DISPLAY_OBSERVATION_SECONDS)

    def idle_empty(self):
        return (self.fresh() and self.status.get("ready") and self.status.get("running")
                and self.status.get("state") == "IDLE" and self.status.get("players") == 0
                and self.status.get("room_id") in self.broker.hub.owned and self.broker.empty())


class RollingSupervisor:
    def __init__(self, configs, api, hub, events, chat_websocket, release, runtime,
                 *, empty_seconds=5, health_seconds=5, startup_timeout=180,
                 config_loader=None, settings_path=None, runtime_validator=None):
        self.config_loader = config_loader
        self.runtime_validator = runtime_validator
        self.config_error = None
        self.configs = {cfg["_instance_name"]: cfg for cfg in configs}
        logs = [str((Path(runtime) / (cfg.get("log_file") or f"logs/{cfg['_instance_name']}.log")).resolve()) for cfg in configs]
        if len(logs) != len(set(logs)):
            raise ValueError("room workers require distinct log files")
        self.api, self.chat_websocket = api, chat_websocket
        self.shared = SharedReferee(hub, events)
        self.discord = SharedDiscord(configs)
        self.modlogs = {alias: DiscordModLog(cfg) for alias, cfg in self.configs.items()}
        self.release, self.runtime = Path(release).resolve(), Path(runtime).resolve()
        self.manifest = load_release(self.release)
        self.directory = self.runtime / "state" / "rolling"
        self.registry = Registry(self.directory / "ownership.json")
        self.registry.require_clean_start()
        self.empty_seconds = deployment_seconds(empty_seconds)
        self.settings_path = Path(settings_path) if settings_path is not None else self.runtime / 'instances/supervisor.json'
        self.settings_stamp = object()
        self.health_seconds, self.startup_timeout = float(health_seconds), float(startup_timeout)
        self.workers = {}
        self.handles = {}
        self.targets = {}
        self.stopping = threading.Event()
        self._explicit_shutdown = False
        self.rollout_thread = None
        self.rollout_alias = None
        self.schema_baseline = None
        self.status_lock = threading.RLock()
        self.last_status_write = 0

    def request_shutdown(self):
        """Parent-authorized service stop; never a worker configuration option."""
        self._explicit_shutdown = True
        self.stopping.set()

    def record(self, alias, **values):
        self.registry.record(alias, **values)

    def room_closed(self, alias, rid):
        try:
            path = (self.runtime / self.configs[alias]["ratings_db"]).resolve()
            with sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=2) as conn:
                now = time.time()
                conn.execute("UPDATE autohost_rooms SET closed_at=COALESCE(closed_at, ?), "
                             "close_reason=COALESCE(close_reason, 'supervisor'), "
                             "last_activity_at=MAX(last_activity_at, ?) WHERE room_id=?", (now, now, rid))
                conn.execute("UPDATE autohost_matches SET ended_at=?, result='room_closed' "
                             "WHERE room_id=? AND ended_at IS NULL", (now, rid))
        except Exception:
            log.exception("room %s closed but its history row could not be finalized", rid)
        try:
            path = self.runtime / (self.configs[alias].get("log_file") or f"logs/{alias}.log")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as stream:
                stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{alias}] bot INFO room {rid} closed\n")
        except Exception:
            log.exception("could not append confirmed closure to %s log", alias)

    def hello(self, broker, identity):
        worker = self.handles[broker.generation]
        if identity != {"protocol": worker.manifest["protocol"], "version": worker.manifest["version"],
                        "files": worker.manifest["files"]}:
            raise IncompatibleRelease("worker release identity mismatch")
        # The parent keeps account settings pinned, but room preferences may
        # have been saved by workers since supervisor startup. Re-read them.
        from run_multiroom import load_rooms
        fresh = (self.config_loader() if self.config_loader is not None else
                 load_rooms([c["_config_path"] for c in self.configs.values()],
                            self.configs[broker.alias]["_universal_config_path"]))
        refreshed = {c["_instance_name"]: c for c in fresh}
        if refreshed.keys() != self.configs.keys():
            raise IncompatibleRelease("room topology changed; coordinated restart required")
        fixed = {"spectator_url", "token_file", "oauth_client_id_env", "oauth_client_secret_env",
                 "api_rate_db", "process_control_db", "ratings_db", "log_file", "room_count",
                 "chat_websocket_enabled", "chat_websocket_endpoint", "chat_websocket_reconnect_max_seconds",
                 "_host_root", "_host_managed_catalogs", "_host_offline", "_host_installation_id"}
        for alias, original in self.configs.items():
            fixed_keys = fixed | {key for key in original if key.startswith((
                "discord_control_", "discord_public_status_", "discord_modlog_"))}
            if any(original.get(key) != refreshed[alias].get(key) for key in fixed_keys):
                raise IncompatibleRelease("shared configuration changed; coordinated restart required")
        cfg = copy.deepcopy(refreshed[broker.alias])
        if broker.cfg.get('_host_catalog_set'):
            from poolrotater_host.catalog_store import CATALOG_KEYS
            # A generation pins its complete set at spawn, even if desired refs
            # change during the hello exchange. Preferences remain freshly read.
            for key in CATALOG_KEYS:
                cfg[key] = copy.deepcopy(broker.cfg.get(key))
                cfg['_supervisor_overrides'][key] = copy.deepcopy(broker.cfg.get(key))
        for key in ("_authenticated_user_id", "_authenticated_username"):
            if key in self.configs[broker.alias]:
                cfg[key] = self.configs[broker.alias][key]
        cfg["_rolling_schema_locked"] = self.schema_baseline is not None
        for key, value in list(cfg.items()):
            if key.endswith("_db") and isinstance(value, str) and value:
                path = Path(value)
                base = worker.release if path.parts[0] in ("catalog-data", "farm-data") else self.runtime
                cfg[key] = str((base / path).resolve())
                cfg["_supervisor_overrides"][key] = cfg[key]
        if cfg.get('_host_root'):
            # Managed adoption imported the existing PP cache here. Do not
            # silently redirect it into an empty version-specific directory.
            from poolrotater_host.config import contained
            preserved_cache = str(contained(Path(cfg['_host_root']), 'state/controller-map-pp'))
            if cfg.get('map_pp_cache_dir') not in ('', preserved_cache):
                raise IncompatibleRelease('managed PP cache path changed')
            cfg['map_pp_cache_dir'] = preserved_cache
        else:
            cfg["map_pp_cache_dir"] = str(self.runtime / "state/map_pp" / worker.manifest["version"])
        cfg["_supervisor_overrides"]["map_pp_cache_dir"] = cfg["map_pp_cache_dir"]
        context = Path(cfg.get("ai_context_file") or "ai_context.md")
        if not cfg.get('_host_root') and not context.is_absolute() and (worker.release / context).is_file():
            cfg["ai_context_file"] = str(worker.release / context)
            cfg["_supervisor_overrides"]["ai_context_file"] = cfg["ai_context_file"]
        # Coordinated maintenance belongs to this controller, including when
        # a child is running an independently updated worker release. Assign
        # after reloading user settings so they cannot select another bridge.
        cfg['_rolling_controller_release'] = str(self.release)
        broker.cfg = copy.deepcopy(cfg)
        return cfg

    def autodiff(self, requester, spec):
        for alias, worker in list(self.workers.items()):
            status = worker.status
            if (alias != requester and worker.fresh() and status.get("ready") and status.get("running")
                    and status.get("auto") and status.get("ruleset") == spec.get("ruleset")
                    and status.get("loved") == spec.get("loved")):
                return {"alias": alias, "room_id": status["room_id"]}
        return None

    def _event(self, worker, name, value):
        if name == "prepared":
            if value.get("version") != worker.manifest["version"] or not value.get("maps"):
                raise ValueError("invalid prepared worker")
            worker.prepared.set()
        elif name in ("status", "exited"):
            if value.get("version") != worker.manifest["version"]:
                raise ValueError("worker reported an incorrect version")
            with self.status_lock:
                worker.status = value
                worker.status_at = time.monotonic()
                if name == "exited":
                    worker.exited = True
        elif name == "failure":
            worker.failure = str(value.get("message", "worker failed"))[:800]
        else:
            raise ValueError("unsupported worker event")

    def spawn(self, alias, release):
        from poolrotater_host.process_target import OwnedChild, require_child_platform
        require_child_platform()
        if self.runtime_validator is not None:
            self.runtime_validator(release)
        manifest = load_release(release)
        require_compatible(self.manifest, manifest)
        if os.geteuid() != 0:
            require_immutable(release, manifest)
        available = next((int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                          if line.startswith("MemAvailable:")), 0)
        if available < 256 * 1024:
            raise RuntimeError("less than 256 MiB available; retaining existing room workers")
        generation = uuid.uuid4().hex
        cfg = self.configs[alias]
        if self.config_loader is not None and cfg.get('_host_root'):
            fresh = {c['_instance_name']: c for c in self.config_loader()}
            if fresh.keys() != self.configs.keys():
                raise IncompatibleRelease('room topology changed; coordinated restart required')
            cfg = fresh[alias]
        broker = Broker(self, alias, generation, copy.deepcopy(cfg))
        worker = Worker(alias, Path(release).resolve(), manifest, broker)
        self.handles[generation] = worker
        parent, child = socket.socketpair()
        env = dict(os.environ)
        cfg = self.configs[alias]
        for key in ("oauth_client_secret_env", "discord_control_bot_token_env", "discord_modlog_webhook_env"):
            env.pop(str(cfg.get(key, "")), None)
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        logdir = self.runtime / "logs" / "workers"
        logdir.mkdir(parents=True, exist_ok=True)
        try:
            with (logdir / f"{alias}-{generation}.log").open("ab", buffering=0) as output:
                if cfg.get('_host_root'):
                    try:
                        from poolrotater_host.reporting_external import register_worker
                        register_worker(cfg['_host_root'],alias,generation,manifest['version'])
                    except Exception:
                        log.warning('worker stderr capture registration unavailable for %s',alias)
                mask = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM))
                try:
                    worker.process = subprocess.Popen(
                        [manifest["python"], "-B", str(worker.release / "run_room_worker.py"), "--fd", str(child.fileno())],
                        cwd=self.runtime, env=env, pass_fds=(child.fileno(),),
                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                        start_new_session=True)
                    worker.signal_target = OwnedChild(worker.process.pid)
                finally: signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            worker.peer = broker.peer = Peer(parent, handler=broker.handle,
                on_event=lambda n, v: self._event(worker, n, v), name=alias + "-" + generation[:8],
                background_profiles=broker.cfg.get('background_player_profiles', False))
            worker.peer.start()
            threading.Thread(target=self._pump, args=(broker,), daemon=True, name=alias + "-events").start()
            return worker
        except BaseException:
            parent.close()
            if worker.peer is not None:
                worker.peer.close()
            if worker.process and worker.process.poll() is None:
                if worker.signal_target is None: raise RuntimeError('unverified worker cleanup refused')
                worker.signal_target.send(signal.SIGTERM)
                worker.process.wait(timeout=10)
            if worker.signal_target is not None: worker.signal_target.close()
            broker.retire()
            self.handles.pop(generation, None)
            raise
        finally:
            child.close()

    @staticmethod
    def _pump(broker):
        try:
            broker.pump()
        except Exception:
            broker.peer.close()

    def wait_for(self, predicate, seconds, worker=None):
        deadline = time.monotonic() + seconds
        while not predicate():
            if self.stopping.is_set():
                raise RuntimeError("supervisor is stopping")
            if worker and (worker.failure or worker.process.poll() is not None or worker.peer.closed.is_set()):
                raise RuntimeError(worker.failure or "worker exited or disconnected")
            if time.monotonic() >= deadline:
                raise TimeoutError("worker lifecycle deadline expired")
            self.stopping.wait(.1)

    def prepare(self, alias, release):
        worker = self.spawn(alias, release)
        try:
            self.wait_for(worker.prepared.is_set, self.startup_timeout, worker)
            return worker
        except BaseException:
            self.dispose(worker)
            raise

    def activate(self, worker):
        alias = worker.alias
        self.record(alias, phase="starting", release=str(worker.release), version=worker.manifest["version"],
                    pid=worker.process.pid, generation=worker.broker.generation, error=None)
        self.workers[alias] = worker
        worker.broker.activate()
        worker.peer.call("activate", timeout=10)
        def healthy():
            good = (worker.fresh() and worker.status.get("ready") and worker.status.get("running")
                    and worker.status.get("room_id") in worker.broker.hub.owned)
            if not good:
                worker.healthy_since = None
                return False
            if worker.healthy_since is None:
                worker.healthy_since = time.monotonic()
            return time.monotonic() - worker.healthy_since >= self.health_seconds
        self.wait_for(healthy, self.startup_timeout, worker)
        worker.broker.publish()
        self.record(alias, phase="running", error=None, last_good_release=str(worker.release),
                    last_good_version=worker.manifest["version"])

    def control(self, worker, command, seconds=30):
        request_id = uuid.uuid4().hex
        worker.peer.call(command, request_id, timeout=10)
        self.wait_for(lambda: (worker.status.get("control") or {}).get("id") == request_id, seconds, worker)
        if not worker.status["control"]["ok"]:
            if command == "prepare":
                raise HandoffDeferred("worker_not_quiescent")
            raise RuntimeError("room no longer eligible for " + command)

    def dispose(self, worker):
        # Fencing rejects any late RPCs before a replacement can take ownership.
        worker.broker.fenced = True
        try:
            if worker.process.poll() is None and not worker.peer.closed.is_set():
                worker.peer.call("stop", timeout=3)
        except Exception:
            pass
        try:
            worker.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if worker.signal_target is None: raise RuntimeError('unverified worker cleanup refused')
            worker.signal_target.send(signal.SIGTERM)
            try:
                worker.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.signal_target.kill()
                worker.process.wait(timeout=5)
        if worker.signal_target is not None: worker.signal_target.close()
        worker.peer.close()
        worker.broker.retire()
        self.handles.pop(worker.broker.generation, None)

    def request(self, aliases, release):
        maintenance=getattr(self,'_operator_maintenance_receipt',None)
        if maintenance and maintenance.get('phase') not in ('complete','cancelled','expired'):
            raise RuntimeError('maintenance_operation_pending')
        aliases = list(dict.fromkeys(aliases))
        if not aliases or any(alias not in self.configs for alias in aliases):
            raise ValueError("select existing room aliases")
        if self.runtime_validator is not None:
            self.runtime_validator(Path(release))
        candidate = load_release(release)
        require_compatible(self.manifest, candidate)
        for existing in [self.manifest] + [w.manifest for w in self.workers.values()]:
            if existing["version"] == candidate["version"] and existing["files"] != candidate["files"]:
                raise IncompatibleRelease("a changed release must have a new version identifier")
        if self.rollout_alias or self.targets:
            raise RuntimeError("finish or cancel the current rollout before requesting another")
        if self.schema_baseline is None or schemas(list(self.configs.values())) != self.schema_baseline:
            raise IncompatibleRelease("database schema changed or startup not healthy; rolling update held")
        for alias in aliases:
            worker = self.workers.get(alias)
            if worker is None or not worker.fresh():
                raise RuntimeError(f"{alias} is not healthy")
            require_compatible(worker.manifest, candidate)
        for alias in aliases:
            if self.workers[alias].release == Path(release).resolve():
                continue
            self.record(alias, phase="waiting_empty", target=str(Path(release).resolve()),
                        target_version=candidate["version"], error=None,
                        handoff_deferred_reason=None, handoff_deferred_attempts=0)
            self.targets[alias] = Path(release).resolve()

    def request_catalogs(self, aliases):
        """Explicit offline catalog handoff through the existing empty-room gates."""
        if not self.config_loader or not all(c.get('_host_offline') for c in self.configs.values()):
            raise ValueError('managed catalog handoff requires the offline host configuration bridge')
        aliases = list(dict.fromkeys(aliases))
        if not aliases or any(a not in self.configs for a in aliases):
            raise ValueError('select existing room aliases')
        if self.rollout_alias or self.targets or self.schema_baseline is None:
            raise RuntimeError('catalog handoff requires healthy startup and an idle deployment')
        fresh = {c['_instance_name']: c for c in self.config_loader()}
        if fresh.keys() != self.configs.keys() or schemas(list(self.configs.values())) != self.schema_baseline:
            raise IncompatibleRelease('room topology/schema changed; catalog handoff held')
        for alias in aliases:
            worker = self.workers.get(alias)
            if worker is None or not worker.fresh() or not fresh[alias].get('_host_catalog_set'):
                raise RuntimeError('catalog handoff requires healthy managed room workers')
        for alias in aliases:
            worker = self.workers[alias]
            if worker.broker.cfg.get('_host_catalog_set') == fresh[alias]['_host_catalog_set']:
                continue
            self.targets[alias] = worker.release
            self.record(alias, phase='waiting_empty', target=str(worker.release),
                        target_catalog_set=fresh[alias]['_host_catalog_set'], error=None)

    def roll(self, alias, release):
        old = self.workers[alias]
        candidate = None
        locked = False
        old_closed = False
        try:
            from poolrotater_host.bookings.update_guard import holds_alias
            if holds_alias(self.runtime, alias):
                return
            if not old.idle_empty():
                return
            if schemas(list(self.configs.values())) != self.schema_baseline:
                raise IncompatibleRelease("database schema changed; rolling update held")
            self.record(alias, phase="preparing")
            candidate = self.prepare(alias, release)
            # Preparation can take time. All liveness/occupancy checks repeat.
            if not old.idle_empty():
                raise HandoffDeferred("room_changed_during_preparation")
            self.control(old, "prepare")
            broker = old.broker
            with self.shared.lock:
                if not old.idle_empty():
                    raise HandoffDeferred("room_changed_before_lock")
                if broker.password != "":
                    raise RuntimeError("room is occupied, uncertain, or already private")
                broker.quiesced = True
                rid, = broker.hub.owned
                self.record(alias, phase="locking", previous=str(old.release), admission_locked=True)
                # Mark before submission: a timeout may still have locked it.
                locked = True
                broker.hub.change_room_settings(rid, password=secrets.token_urlsafe(24))
            self.record(alias, phase="draining")
            # Allow ordered server events from before the password change to
            # reach both roster mirrors. Any join resets/cancels the attempt.
            self.stopping.wait(2)
            self.control(old, "flush")
            with self.shared.lock:
                # Drain any already-received events before the final check;
                # dispatch may have been waiting for the shared operation lock.
                while True:
                    try:
                        self.shared.route_event(self.shared.events.get_nowait())
                    except queue.Empty:
                        break
                if self.stopping.is_set() or not old.idle_empty() or not old.status.get("paused"):
                    raise RuntimeError("room changed after locking; keeping the current worker")
                # Keep old controller paused but alive until close is confirmed,
                # so a late join can cancel the update without losing control.
                self.record(alias, phase="closing")
                broker.close_room(rid)
                old_closed = True
            self.dispose(old)
            self.record(alias, admission_locked=False)
            self.activate(candidate)
            self.targets.pop(alias, None)
            self.record(alias, target=None, target_version=None, target_catalog_set=None,
                        catalog_set=candidate.broker.cfg.get("_host_catalog_set"), previous=str(old.release), phase="running",
                        handoff_deferred_reason=None)
        except Exception as exc:
            if (isinstance(exc, HandoffDeferred) and not locked and not old_closed
                    and old.fresh() and not old.broker.uncertain and not old.broker.fenced):
                # No provider mutation has been submitted. Only a definite
                # worker NACK/changed local eligibility may retain this target.
                # Unknown RPC outcomes and cleanup failures still fail closed.
                try:
                    if candidate:
                        self.dispose(candidate)
                        candidate = None
                    if old.status.get("paused"):
                        self.control(old, "resume")
                except Exception as cleanup_error:
                    exc = cleanup_error
                else:
                    old.empty_since = None
                    old.empty_epoch = -1
                    old.handoff_deferrals = getattr(old, "handoff_deferrals", 0) + 1
                    delay = min(30.0, 2.0 ** min(old.handoff_deferrals, 5))
                    old.handoff_retry_at = time.monotonic() + delay
                    # Do not re-add a target: protected cancellation still owns
                    # that intent and is consumed before the next admission.
                    waiting = alias in self.targets
                    self.record(alias, phase="waiting_empty" if waiting else "running", error=None,
                                handoff_deferred_reason=exc.reason,
                                handoff_deferred_attempts=old.handoff_deferrals)
                    log.info("room %s handoff deferred before mutation: %s; retry after %.1fs and fresh empty interval",
                             alias, exc.reason, delay)
                    return
            log.error("room rollout failed for %s", alias, exc_info=(type(exc), exc, exc.__traceback__))
            self.targets.pop(alias, None)
            self.record(alias, error=str(exc)[:800], target=None, target_version=None)
            if not old_closed:
                if candidate:
                    self.dispose(candidate)
                if old.broker.uncertain:
                    # A close may have succeeded. No reopen/recreate guessing.
                    old.broker.fenced = True
                    self.record(alias, phase="recovery_required")
                else:
                    try:
                        if locked:
                            with self.shared.lock:
                                rid, = old.broker.hub.owned
                                old.broker.hub.change_room_settings(rid, password="")
                            self.record(alias, admission_locked=False)
                        old.broker.quiesced = False
                        if old.status.get("paused"):
                            self.control(old, "resume")
                        self.record(alias, phase="running", error=str(exc)[:800])
                    except Exception as resume_error:
                        # Do not close the room to "fix" an uncertain unlock.
                        old.broker.quiesced = False
                        try:
                            old.peer.call("resume", uuid.uuid4().hex, timeout=3)
                        except Exception:
                            pass
                        self.record(alias, phase="recovery_required", error=str(resume_error)[:800])
                return
            # A failed candidate can be rolled back only after confirmed loss
            # of its ownership. Never force-close players to recover a release.
            safe = True
            if candidate:
                b = candidate.broker
                b.fenced = True
                with self.shared.lock:
                    safe = not b.uncertain
                    if safe and b.hub.owned:
                        safe = b.private and b.empty()
                        if safe:
                            try:
                                rid, = b.hub.owned
                                b.close_room(rid)
                            except Exception:
                                safe = False
                self.dispose(candidate)
            if safe and not self.stopping.is_set():
                try:
                    self.record(alias, phase="rolling_back")
                    rollback = self.prepare(alias, old.release)
                    self.activate(rollback)
                    self.record(alias, phase="running", error="Candidate failed; previous release restored")
                    return
                except Exception as rollback_error:
                    self.record(alias, error=str(rollback_error)[:800])
            self.record(alias, phase="recovery_required")
        finally:
            self.rollout_alias = None

    def _protected_requests(self):
        from poolrotater_host.worker_deployment import poll
        try:
            poll(self)
        except Exception:
            log.exception('protected worker deployment held; existing workers retained')

    def _requests(self):
        inbox = self.directory / "requests"
        inbox.mkdir(parents=True, exist_ok=True)
        for path in sorted(inbox.glob("*.json"))[:8]:
            claimed = path.with_suffix(".processing")
            try:
                path.rename(claimed)
                if claimed.stat().st_size > 16384:
                    raise ValueError("oversized rollout request")
                value = json.loads(claimed.read_text())
                if value.get("action") == "deploy":
                    self.request(value["rooms"], value["release"])
                elif value.get("action") == "cancel":
                    if self.rollout_alias:
                        raise RuntimeError("a handoff is in progress; cancellation held")
                    for alias in list(self.targets):
                        self.record(alias, phase="running", target=None, target_version=None)
                    self.targets.clear()
                else:
                    raise ValueError("unknown rollout action")
                result = {"ok": True, "at": time.time()}
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "at": time.time()}
            atomic_json(self.directory / "results" / path.name, result)
            claimed.unlink(missing_ok=True)

    def reload_deployment_settings(self):
        try:
            stat = self.settings_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except FileNotFoundError:
            stamp = None
        except OSError:
            return
        if stamp == self.settings_stamp:
            return
        self.settings_stamp = stamp
        try:
            seconds = read_settings(self.settings_path)
        except (OSError, ValueError):
            log.warning('ignored invalid supervisor deployment settings; retaining %.1fs', self.empty_seconds)
            return
        if seconds != self.empty_seconds:
            self.empty_seconds = seconds
            # A changed grace starts a new continuous empty/idle interval.
            for worker in self.workers.values():
                worker.empty_since = None
            log.info('hot-loaded deployment empty/idle grace: %.1fs', seconds)

    def _settle_failed_worker(self, worker, now):
        """Close only a dead worker's positively owned room after ordered refresh.

        A still-running broker RPC owns the routing lock. Let it finish without
        blocking sibling event processing or confusing close_pending with a lost
        reply. Once the lock is free, actual uncertainty is still terminal.
        """
        if worker.failure_cleanup_deadline is None:
            worker.failure_cleanup_deadline = now + 30.0
        deadline = worker.failure_cleanup_deadline
        if not self.shared.lock.acquire(blocking=False):
            if now >= deadline:
                raise RuntimeError('worker_cleanup_deadline_exceeded')
            return False
        try:
            state = self.registry.snapshot()['rooms'].get(worker.alias, {})
            broker = worker.broker
            if (state.get('operation') == 'closed' and not state.get('room_id')
                    and state.get('ownership_unknown') is False
                    and not broker.uncertain and not broker.hub.owned):
                self.dispose(worker)
                self.record(worker.alias, phase='parked', pid=None, error=None)
                return True
            if (state.get('operation') != 'owned' or state.get('ownership_unknown') is not False
                    or broker.uncertain or len(broker.hub.owned) != 1
                    or state.get('room_id') not in broker.hub.owned):
                raise RuntimeError('worker_ownership_requires_review')
            rid = state['room_id']
            try:
                if not self.shared.hub.is_connected():
                    raise HubTransportError('referee_disconnected')
                progress = broker.hub.roster_progress()
                if progress is not None and progress[2]:
                    raise HubTransportError('waiting_for_ordered_roster')
                if (not broker.known or broker.hub.roster_fenced
                        or broker.snapshot_generation != broker.hub._generation()):
                    # Refresh only this known existing room. The ordinary broker
                    # validates owner, connection generation and ordered roster.
                    broker.handle('hub.invoke', 'JoinRoom', [rid])
                broker.close_room(rid, require_empty=False)
            except HubTransportError:
                if broker.uncertain or time.monotonic() >= deadline:
                    raise
                return False
            self.dispose(worker)
            self.record(worker.alias, phase='parked', pid=None, error=None)
            return True
        finally:
            self.shared.lock.release()

    def tick(self):
        self._tick_component = 'configuration'
        if self.config_loader is not None:
            try:
                self.config_loader()
                self.config_error = None
            except (OSError, ValueError) as exc:
                if self.config_error != type(exc).__name__:
                    log.warning('native configuration refresh rejected; retaining running room settings (%s)', type(exc).__name__)
                self.config_error = type(exc).__name__
        self._tick_component = 'deployment_settings'
        self.reload_deployment_settings()
        # Consume protected cancellation before choosing the next empty alias.
        # A completed handoff must not immediately launch its sibling ahead of
        # an already queued cancellation acknowledgement.
        self._tick_component = 'protected_requests'
        self._protected_requests()
        from poolrotater_host.operator_maintenance import poll as maintenance_poll
        self._tick_component = 'maintenance'
        maintenance_poll(self)
        managed=getattr(self,'managed',None)
        bookings=getattr(self,'bookings',None)
        self._tick_component = 'bookings'
        if bookings is not None:bookings.tick()
        self._tick_component = 'managed_rooms'
        if managed is not None:managed.tick()
        booking_panels=getattr(self,'booking_panels',None)
        self._tick_component = 'booking_panels'
        if booking_panels is not None:booking_panels.tick()
        self._tick_component = 'workers'
        now = time.monotonic()
        for alias, worker in list(self.workers.items()):
            if alias == self.rollout_alias:
                continue
            if worker.idle_empty():
                if worker.empty_since is None or worker.empty_epoch != worker.broker.epoch:
                    worker.empty_since = now
                    worker.empty_epoch = worker.broker.epoch
            else:
                worker.empty_since = None
            # A normal periodic hard restart creates a private room too.
            state = self.registry.snapshot()["rooms"].get(alias, {})
            if (state.get("phase") in ("running", "waiting_empty") and worker.fresh()
                    and worker.status.get("ready") and worker.status.get("running")
                    and worker.status.get("room_id") in worker.broker.hub.owned
                    and worker.broker.pending_public):
                worker.broker.publish()
            if worker.process.poll() is not None:
                if state.get("phase") not in ("recovery_required", "parked"):
                    try:
                        if worker.failure or not worker.exited:
                            self._settle_failed_worker(worker, now)
                            continue
                        # The worker's own close may have completed since the
                        # earlier status read. Re-read under the routing lock.
                        with self.shared.lock:
                            state = self.registry.snapshot()['rooms'].get(alias, {})
                            if (state.get('operation') == 'closed' and not state.get('room_id')
                                    and state.get('ownership_unknown') is False
                                    and not worker.broker.uncertain and not worker.broker.hub.owned):
                                self.dispose(worker)
                                self.record(alias, phase='parked', pid=None, error=None)
                                continue
                        if not worker.exited or worker.failure or worker.broker.uncertain:
                            raise RuntimeError(worker.failure or "Worker exited unexpectedly; ownership retained")
                        if worker.status.get("reopenable") and not worker.broker.empty():
                            raise RuntimeError("Player arrived during close-when-empty; ownership retained")
                        if worker.broker.hub.owned:
                            rid, = worker.broker.hub.owned
                            worker.broker.close_room(rid)
                        self.dispose(worker)
                        self.record(alias, phase="parked", pid=None)
                    except Exception as exc:
                        self.record(alias, phase="recovery_required", error=str(exc))
                        # Report allowlisted diagnostics even if a killed worker
                        # could not flush its own traceback. Never print RPC
                        # exception text, room passwords or provider bodies.
                        from poolrotater_host.service_diagnostics import failure_details
                        detail = failure_details(exc)
                        log.error('room worker closure requires recovery: alias=%s exit_code=%s failure_code=%s',
                                  alias, getattr(worker.process, 'returncode', None),
                                  detail['failure_code'])
            if (alias in self.targets and self.rollout_alias is None and worker.empty_since is not None
                    and now - worker.empty_since >= self.empty_seconds
                    and now >= getattr(worker, "handoff_retry_at", 0.0)):
                from poolrotater_host.bookings.update_guard import holds_alias
                if holds_alias(self.runtime, alias):
                    continue
                self.rollout_alias = alias
                self.rollout_thread = threading.Thread(target=self.roll, args=(alias, self.targets[alias]),
                                                       daemon=True, name="rollout-" + alias)
                self.rollout_thread.start()
        if self.schema_baseline is None and len(self.workers) == len(self.configs) and all(
                w.fresh() and w.status.get("ready") for w in self.workers.values()):
            self.schema_baseline = schemas(list(self.configs.values()))
        self._tick_component = 'rollout_requests'
        self._requests()
        self._tick_component = 'discord_commands'
        self._discord_commands()
        self._tick_component = 'status'
        self.write_status()
        self._tick_component = 'idle'

    def _discord_commands(self):
        for _ in range(16):
            try:
                alias, command = self.discord.get_supervisor_command_nowait()
            except queue.Empty:
                return
            prefix = str(self.configs[alias].get("command_prefix", ".") or ".")
            content = command.content.removeprefix(prefix).strip().split()
            name = content[0].casefold() if content else ""
            managed=getattr(self,'managed',None)
            if managed is not None and (name in ('open','reopen','close') or
                    name=='shutdown' and [v.casefold() for v in content[1:]]==['all']):
                if command.role!='ADMIN':
                    self.discord.send_to_channel('Room administration requires an administrator.',command.channel_id)
                    continue
                if name=='shutdown':
                    from poolrotater_host.managed_host.projection import export
                    from poolrotater_host.discord_relay.fleet import operation_identity
                    try:
                        fresh=managed.state.operator_stop(operation_id=operation_identity(command.channel_id,command.message_id),
                                                          actor='discord:'+str(command.discord_user_id))
                    except Exception:
                        self.discord.send_to_channel('Room shutdown refused; inspect fleet status.',command.channel_id)
                        continue
                    if not fresh:
                        self.discord.send_to_channel('That shutdown request was already processed; no new shutdown was sent.',command.channel_id)
                        continue
                    export(managed.state)
                    self.discord.send_to_channel('Stopping all rooms; their stopped intent is saved.',command.channel_id)
                    self.request_shutdown();continue
                try:
                    from poolrotater_host.discord_relay.fleet import operation_identity
                    message=getattr(command,'message_id',0)
                    if type(message) is not int or message<=0 or len(content)!=1:raise ValueError('invalid_lifecycle_request')
                    action='close' if name=='close' else 'open';operation_id=operation_identity(command.channel_id,message)
                    receipt=managed.state.local_lifecycle(alias,action,operation_id=operation_id,actor='discord:'+str(command.discord_user_id))
                    self.discord.send_to_channel('Room '+action+': '+receipt['phase']+
                        '; operation '+operation_id+'. Closure waits for the room to be empty.',command.channel_id)
                except Exception:
                    self.discord.send_to_channel('Room operation refused; inspect the current fleet status before retrying.',command.channel_id)
                continue
            if name == "shutdown" and [v.casefold() for v in content[1:]] == ["all"]:
                self.discord.send_to_channel("Shutting down all lobby rooms and the supervisor.", command.channel_id)
                self.request_shutdown()
            elif name in ("open", "reopen"):
                state = self.registry.snapshot()["rooms"].get(alias, {})
                if self.rollout_alias or state.get("phase") != "parked" or state.get("room_id") or state.get("ownership_unknown"):
                    self.discord.send_to_channel("Room cannot reopen until closure is confirmed and deployment is idle.", command.channel_id)
                    continue
                self.rollout_alias = alias
                def reopen(alias=alias, state=state):
                    try:
                        self.activate(self.prepare(alias, state["release"]))
                    except Exception as exc:
                        self.record(alias, phase="recovery_required", error=str(exc)[:800])
                    finally:
                        self.rollout_alias = None
                self.rollout_thread = threading.Thread(target=reopen, daemon=True, name="reopen-" + alias)
                self.rollout_thread.start()

    def prepare_startup_status(self):
        """Finish initial schema observation before advertising service readiness.

        The first full managed-catalog verification can exceed the installed
        predecessor's worker-observation window. It is read-only and must finish
        before that window begins. Initial workers keep their existing prepare
        policy; subsequent rollouts still require their normal schema/ownership
        and fresh-heartbeat admission. A private subset never pins a full-host
        baseline on behalf of rooms it did not activate.
        """
        if self.schema_baseline is None and len(self.workers) == len(self.configs):
            self.schema_baseline = schemas(list(self.configs.values()))
        self.write_status(force=True)

    def write_status(self, force=False):
        from poolrotater_host.network.projection import verified_visibility
        if not force and time.monotonic() - self.last_status_write < 5:
            return
        state = self.registry.snapshot()
        for row in state['rooms'].values():
            row['network_visibility'] = 'unknown'
        for alias, worker in list(self.workers.items()):
            row = state["rooms"].setdefault(alias, {})
            row.update(status=worker.status, heartbeat_age=round(time.monotonic() - worker.status_at, 2)
                       if worker.status_at else None, healthy=worker.fresh(),
                       roster_known=worker.broker.known, observed_players=len(worker.broker.players),
                       empty_seconds=round(time.monotonic() - worker.empty_since, 1) if worker.empty_since else 0)
            row['network_visibility'] = verified_visibility(worker, row)
        state.update(supervisor_version=self.manifest["version"], generated_at=time.time(),
                     deployment_empty_seconds=self.empty_seconds, configuration_error=self.config_error)
        atomic_json(self.directory / "status.json", state)
        public = {"generated_at": state["generated_at"], "supervisor_version": self.manifest["version"], "rooms": {}}
        for alias, row in state["rooms"].items():
            worker = self.workers.get(alias)
            if (getattr(worker,'broker',None) is not None
                    and worker.broker.cfg.get('managed_visibility')=='private'):
                continue
            managed=getattr(self,'managed',None)
            if managed is not None and managed.state.room(alias)['config'].get('visibility')=='private':
                continue
            fresh = bool(worker and worker.fresh() and worker.status.get("ready")
                         and worker.status.get("running") and row.get("room_id")
                         and row.get("room_id") == worker.status.get("room_id"))
            closed = row.get("operation") == "closed" and not row.get("room_id")
            public["rooms"][alias] = {"version": row.get("version"), "target_version": row.get("target_version"),
                "phase": row.get("phase"), "room_id": row.get("room_id"), "healthy": row.get("healthy", False),
                "uptime_seconds": max(0, int(time.time() - worker.started_at)) if worker else None,
                "observed_at": time.time() - max(0, time.monotonic() - worker.status_at) if fresh else None,
                **({key: value for key, value in worker.status.get("public", {}).items()
                    if key in {"state", "players", "current_map", "queue", "queue_known", "match_started_at"}} if fresh else
                   {"state": "OFFLINE" if closed else "STALE", "players": None,
                    "current_map": None, "queue": [], "queue_known": False, "match_started_at": None})}
        private = getattr(self, 'private_pilot', None)
        if private is not None and private.alias in public['rooms']:
            public['rooms'][private.alias].update(room_id=None, healthy=False, observed_at=None, state='STALE', players=None, current_map=None, queue=[], queue_known=False, match_started_at=None)
        atomic_json(self.directory / "public-status.json", public)
        self.last_status_write = time.monotonic()

    def start_release_for(self, alias):
        previous = self.registry.snapshot()["rooms"].get(alias, {})
        pinned = Path(previous.get("last_good_release") or previous.get("release") or self.release)
        if self.runtime_validator is not None and pinned != self.release:
            try:
                approved = self.runtime_validator(pinned)
                require_compatible(self.manifest, approved['manifest'])
            except Exception:
                # A stopped coordinated controller upgrade intentionally retires
                # the old parent's shared contract. Only its exact committed
                # predecessor may fall back to the new registered controller.
                from poolrotater_host.worker_releases import retired_controller
                if not retired_controller(getattr(self, 'protected_parent', None), pinned, previous):
                    raise
                return self.release
        return pinned

    def start_discord_relay(self):
        if getattr(self.discord, 'remote_relay', None) is not None:
            return
        try:
            from poolrotater_host.discord_relay.host import HostRelay
            relay = HostRelay(self)
            self.discord.remote_relay = relay
            relay.start()
        except Exception:
            log.warning("Discord host relay unavailable; existing rooms retained")

    def stop_discord_relay(self):
        relay = getattr(self.discord, 'remote_relay', None)
        if relay is not None:
            relay.stop()

    def run(self):
        signal.signal(signal.SIGINT, lambda *_: self.request_shutdown())
        signal.signal(signal.SIGTERM, lambda *_: self.request_shutdown())
        try:
            self.shared.hub.connect()
            self.shared.start_dispatch()
            self.discord.start()
            self.start_discord_relay()
            if self.chat_websocket:
                self.chat_websocket.start()
            for alias in self.configs:
                managed=getattr(self,'managed',None)
                if managed is not None and not managed.startup_allowed(alias):
                    managed.park(alias)
                    continue
                try:
                    pinned = self.start_release_for(alias)
                    self.activate(self.prepare(alias, pinned))
                    self.record(alias, target=None, target_version=None)
                except Exception as exc:
                    self.record(alias, phase="recovery_required", error=str(exc)[:800])
                    log.exception("room %s did not start; other rooms continue", alias)
            while not self.stopping.wait(.5):
                try:
                    self.tick()
                except Exception:
                    log.exception("supervisor tick failed; existing workers retained")
            return 0
        finally:
            self.stopping.set()
            if self.rollout_thread:
                self.rollout_thread.join(timeout=35)
            # An explicit service shutdown is a coordinated stop. Infrastructure
            # rollout never reaches this path and never stops account transports.
            self.shared.begin_shutdown(45)
            self.api.cancel_pending.set()
            for worker in list(self.handles.values()):
                try:
                    self.dispose(worker)
                    if worker.broker.hub.owned and not worker.broker.uncertain:
                        rid, = worker.broker.hub.owned
                        worker.broker.close_room(rid, require_empty=not self._explicit_shutdown)
                    self.record(worker.alias, phase="stopped", pid=None)
                except Exception:
                    log.exception("shutdown ownership unresolved for %s", worker.alias)
            self.write_status(force=True)
            relay = getattr(self.discord, "remote_relay", None)
            if relay:
                relay.stop()
            self.discord.stop()
            for modlog in self.modlogs.values():
                modlog.stop(timeout=1)
            if self.chat_websocket:
                self.chat_websocket.stop()
            self.shared.stop()
