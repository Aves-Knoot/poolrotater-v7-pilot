"""Cached osu!standard PP projections using the official osu!lazer calculator.

PoolRotater downloads each .osu file once and calculates PP locally through
``osu-tools-py``.  That package embeds the official osu!lazer / osu-tools C#
assemblies and exposes them to Python through pythonnet, so no tosu process or
local HTTP service is required.

The service is intentionally backgrounded: map rotation never depends on PP
calculation succeeding.  Default prefetches calculate NoMod 100/99/95, while
``.pp`` can request arbitrary supported mod combinations and one or more
accuracy presets.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata as importlib_metadata
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Iterable, Sequence

import httpx

log = logging.getLogger("pp_projection")

DEFAULT_ACCURACIES = (100.0, 99.0, 95.0)

# Common osu!standard / lazer acronyms.  Separate tokens not present here are
# still accepted when they look like a mod acronym so newer lazer mods can pass
# through to osu-tools-py.  This table is mainly needed to split compact inputs
# such as HDDT or HDHR.
KNOWN_MOD_ACRONYMS = frozenset({
    "NF", "EZ", "TD", "HD", "HR", "SD", "DT", "RX", "HT", "NC", "FL",
    "AT", "SO", "AP", "PF", "FI", "RD", "CN", "TP", "V2", "MR", "CL",
    "DA", "DC", "WU", "WD", "AS", "BL", "ST", "AC", "DP", "BM", "WG",
    "SI", "TR", "TC",
})

# Stable display/cache order.  This makes `.pp HD HR` and `.pp HRHD` share the
# same cache entry and show the same label.
MOD_ORDER = (
    "NF", "EZ", "TD", "HD", "HR", "SD", "PF", "DT", "NC", "HT", "DC",
    "RX", "AP", "FL", "SO", "FI", "MR", "DA", "WU", "WD", "AS", "BL",
    "ST", "AC", "DP", "BM", "WG", "SI", "RD", "CN", "TP", "V2", "TR",
    "TC", "AT", "CL",
)
_MOD_ORDER_INDEX = {name: idx for idx, name in enumerate(MOD_ORDER)}


@dataclass(frozen=True, slots=True)
class PPProjection:
    """One PP result table.

    The first three fields preserve compatibility with the v6.4.5/v6.4.6
    constructor used by existing tests and call sites.  Custom requests use the
    ``values`` field instead.
    """

    pp100: float | None = None
    pp99: float | None = None
    pp95: float | None = None
    mods: tuple[str, ...] = ()
    values: tuple[tuple[float, float], ...] = ()

    def rows(self) -> tuple[tuple[float, float], ...]:
        if self.values:
            return self.values
        out: list[tuple[float, float]] = []
        if self.pp100 is not None:
            out.append((100.0, float(self.pp100)))
        if self.pp99 is not None:
            out.append((99.0, float(self.pp99)))
        if self.pp95 is not None:
            out.append((95.0, float(self.pp95)))
        return tuple(out)

    @property
    def mod_label(self) -> str:
        return "".join(self.mods) if self.mods else "NM"

    @staticmethod
    def _fmt_acc(value: float) -> str:
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return f"{value:.2f}".rstrip("0").rstrip(".")

    def display(self) -> str:
        rows = self.rows()
        body = " | ".join(
            f"{self._fmt_acc(acc)}% {pp:.0f}pp" for acc, pp in rows
        )
        return f"{self.mod_label}: {body}"


@dataclass(frozen=True, slots=True)
class PPRequest:
    mods: tuple[str, ...]
    accuracies: tuple[float, ...]


class PPArgumentError(ValueError):
    pass


def _canonical_mods(mods: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in mods:
        mod = str(raw or "").strip().upper()
        if not mod or mod in ("NM", "NOMOD"):
            continue
        if mod not in seen:
            seen.add(mod)
            cleaned.append(mod)
    return tuple(
        sorted(cleaned, key=lambda m: (_MOD_ORDER_INDEX.get(m, 10_000), m))
    )


def _split_compact_mods(token: str) -> list[str] | None:
    """Split e.g. ``HDHR`` or ``HDDT`` into known acronyms."""
    text = token.upper()
    if text in KNOWN_MOD_ACRONYMS:
        return [text]
    if text in ("NM", "NOMOD"):
        return []

    # Dynamic programming avoids greedy mistakes and keeps the user-facing
    # parser deterministic if acronym lengths diverge in future.
    memo: dict[int, list[str] | None] = {}

    def solve(pos: int) -> list[str] | None:
        if pos == len(text):
            return []
        if pos in memo:
            return memo[pos]
        for length in (3, 2):
            part = text[pos : pos + length]
            if part in KNOWN_MOD_ACRONYMS:
                tail = solve(pos + length)
                if tail is not None:
                    memo[pos] = [part, *tail]
                    return memo[pos]
        memo[pos] = None
        return None

    return solve(0)


def parse_pp_arguments(args: Sequence[str]) -> PPRequest:
    """Parse arguments after `.pp`.

    Examples:
      `.pp` -> NM at 100/99/95
      `.pp 98.5` -> NM at 98.5
      `.pp HR` -> HR at 100/99/95
      `.pp HDHR` / `.pp HD HR` -> HDHR at 100/99/95
      `.pp HDHR 99.5` -> HDHR at 99.5
    """

    mods: list[str] = []
    accuracies: list[float] = []
    explicit_nm = False

    for raw in args:
        token = str(raw or "").strip()
        if not token:
            continue
        numeric = token[:-1] if token.endswith("%") else token
        try:
            acc = float(numeric)
        except ValueError:
            acc = None

        if acc is not None:
            if not (0.0 < acc <= 100.0):
                raise PPArgumentError("accuracy must be greater than 0 and at most 100")
            rounded = round(acc, 4)
            if rounded not in accuracies:
                accuracies.append(rounded)
            continue

        upper = re.sub(r"[^A-Za-z0-9]", "", token).upper()
        if not upper:
            raise PPArgumentError(f"could not parse {token!r}")
        if upper in ("NM", "NOMOD"):
            explicit_nm = True
            continue

        split = _split_compact_mods(upper)
        if split is None:
            # If a user supplied one standalone acronym, let the official
            # calculator decide whether it is a currently supported lazer mod.
            if re.fullmatch(r"[A-Z0-9]{2,4}", upper):
                split = [upper]
            else:
                raise PPArgumentError(f"unknown mod input {token!r}")
        mods.extend(split)

    mods_tuple = _canonical_mods(mods)
    if explicit_nm and mods_tuple:
        raise PPArgumentError("NM cannot be combined with other mods")

    mod_set = set(mods_tuple)
    if "EZ" in mod_set and "HR" in mod_set:
        raise PPArgumentError("EZ and HR cannot be combined")
    if (mod_set & {"DT", "NC"}) and (mod_set & {"HT", "DC"}):
        raise PPArgumentError("DT/NC cannot be combined with HT/DC")

    if not accuracies:
        accuracies = list(DEFAULT_ACCURACIES)

    return PPRequest(mods_tuple, tuple(accuracies))


class MapPPProjection:
    """Background/cached osu!standard PP projection service."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._memory: dict[
            tuple[int, tuple[str, ...], tuple[float, ...]],
            tuple[float, PPProjection],
        ] = {}
        self._pending: set[tuple[int, tuple[str, ...], tuple[float, ...]]] = set()
        self._events: dict[
            tuple[int, tuple[str, ...], tuple[float, ...]], threading.Event
        ] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[
            tuple[int, tuple[str, ...], tuple[float, ...]] | None
        ] = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._backend_warning_sent = False
        self._calculator_instance = None
        self._calculator_version: str | None = None
        self._thread = threading.Thread(
            target=self._worker,
            name=f"{cfg.get('_instance_name', 'room')}-map-pp",
            daemon=True,
        )
        self._thread.start()

    def enabled(self) -> bool:
        return bool(self.cfg.get("map_pp_display_enabled", True))

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.0, float(timeout)))

    @staticmethod
    def _request_key(
        beatmap_id: int,
        mods: Sequence[str] | None,
        accuracies: Sequence[float] | None,
    ) -> tuple[int, tuple[str, ...], tuple[float, ...]]:
        bid = int(beatmap_id)
        canonical = _canonical_mods(mods or ())
        accs = tuple(
            round(float(v), 4) for v in (accuracies or DEFAULT_ACCURACIES)
        )
        return bid, canonical, accs

    def prefetch(
        self,
        beatmap_id: int,
        *,
        mods: Sequence[str] | None = None,
        accuracies: Sequence[float] | None = None,
    ) -> None:
        if not self.enabled():
            return
        if accuracies is None and self.cfg.get("ruleset_id", 0) == 3:
            accuracies = (100.0, 99.0, 98.0, 95.0)
        key = self._request_key(beatmap_id, mods, accuracies)
        if key[0] <= 0 or self._cached(key) is not None:
            return
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
            self._events.setdefault(key, threading.Event()).clear()
        try:
            self._queue.put_nowait(key)
        except queue.Full:
            with self._lock:
                self._pending.discard(key)
            log.debug("map PP prefetch queue full; skipping %s", key)

    def get(
        self,
        beatmap_id: int,
        wait_seconds: float = 0.0,
        *,
        mods: Sequence[str] | None = None,
        accuracies: Sequence[float] | None = None,
    ) -> PPProjection | None:
        if not self.enabled():
            return None
        if accuracies is None and self.cfg.get("ruleset_id", 0) == 3:
            accuracies = (100.0, 99.0, 98.0, 95.0)
        key = self._request_key(beatmap_id, mods, accuracies)
        cached = self._cached(key)
        if cached is not None:
            return cached
        self.prefetch(key[0], mods=key[1], accuracies=key[2])
        if wait_seconds > 0:
            with self._lock:
                event = self._events.setdefault(key, threading.Event())
            event.wait(max(0.0, float(wait_seconds)))
        return self._cached(key)

    def _cache_dir(self) -> Path:
        raw = str(self.cfg.get("map_pp_cache_dir", "state/map_pp") or "state/map_pp")
        return Path(raw)

    def _cache_seconds(self) -> float:
        try:
            days = float(self.cfg.get("map_pp_cache_days", 7.0) or 0.0)
        except (TypeError, ValueError):
            days = 7.0
        return max(0.0, min(365.0, days)) * 86400.0

    def _timeout(self) -> float:
        try:
            value = float(self.cfg.get("map_pp_timeout_seconds", 6.0) or 6.0)
        except (TypeError, ValueError):
            value = 6.0
        return max(0.5, min(30.0, value))

    @staticmethod
    def _fmt_acc_key(value: float) -> str:
        return PPProjection._fmt_acc(value).replace(".", "p")

    def _cache_path(
        self, key: tuple[int, tuple[str, ...], tuple[float, ...]]
    ) -> Path:
        bid, mods, accs = key
        mod_key = "".join(mods) if mods else "NM"
        acc_key = "-".join(self._fmt_acc_key(v) for v in accs)
        folder = "results_mania" if self.cfg.get("ruleset_id", 0) == 3 else "results"
        return self._cache_dir() / folder / f"{bid}__{mod_key}__{acc_key}.json"

    def _osu_path(self, bid: int) -> Path:
        return self._cache_dir() / "osu" / f"{bid}.osu"

    def _fresh(self, calculated_at: float) -> bool:
        lifetime = self._cache_seconds()
        return lifetime > 0.0 and (time.time() - float(calculated_at)) <= lifetime

    def _cached(
        self, key: tuple[int, tuple[str, ...], tuple[float, ...]]
    ) -> PPProjection | None:
        with self._lock:
            memory = self._memory.get(key)
        if memory and self._fresh(memory[0]):
            return memory[1]

        path = self._cache_path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not str(payload.get("backend", "")).startswith("osu-tools-py-"):
                return None
            calculated_at = float(payload["calculated_at"])
            if not self._fresh(calculated_at):
                return None
            mods = tuple(str(v) for v in payload.get("mods", []))
            rows = tuple(
                (float(row["accuracy"]), float(row["pp"]))
                for row in payload["values"]
            )
            if not rows or min(pp for _acc, pp in rows) <= 0:
                return None
            projection = self._projection_for(rows, mods)
            with self._lock:
                self._memory[key] = (calculated_at, projection)
            return projection
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                key = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if key is None:
                return
            try:
                projection = self._calculate_request(key)
                if projection is not None:
                    self._store(key, projection)
            except Exception as exc:
                if not self._backend_warning_sent:
                    self._backend_warning_sent = True
                    log.warning(
                        "current map PP display unavailable (%s). Install the "
                        "project requirements and the .NET 8 runtime; PoolRotater "
                        "uses osu-tools-py with the official osu!lazer calculator. "
                        "Map rotation will continue normally.",
                        exc,
                    )
                else:
                    log.debug("map PP projection unavailable for %s: %s", key, exc)
            finally:
                with self._lock:
                    self._pending.discard(key)
                    event = self._events.get(key)
                    if event is not None:
                        event.set()

    def _download_osu(self, bid: int) -> Path:
        path = self._osu_path(bid)
        if path.is_file() and path.stat().st_size > 32:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        template = str(
            self.cfg.get(
                "map_pp_osu_url_template",
                "https://osu.ppy.sh/osu/{beatmap_id}",
            )
            or "https://osu.ppy.sh/osu/{beatmap_id}"
        )
        url = template.format(beatmap_id=bid)
        with httpx.Client(
            timeout=self._timeout(),
            follow_redirects=True,
            headers={"User-Agent": "PoolRotater/6.4.8 map-pp"},
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            content = response.content
        if len(content) <= 32 or b"osu file format" not in content[:128].lower():
            raise ValueError("download did not look like an .osu file")
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)
        return path

    def _calculator(self):
        if self._calculator_instance is not None:
            return self._calculator_instance
        try:
            from osu_tools import OsuCalculator
        except Exception as exc:  # ImportError plus pythonnet/.NET bootstrap errors
            raise RuntimeError(
                "osu-tools-py could not load; run `python -m pip install -r "
                "requirements.txt` and install the .NET 8 Runtime"
            ) from exc

        try:
            version = importlib_metadata.version("osu-tools-py")
        except importlib_metadata.PackageNotFoundError:
            version = "unknown"
        self._calculator_version = version
        try:
            self._calculator_instance = OsuCalculator()
        except Exception as exc:
            raise RuntimeError(
                "osu-tools-py found, but the official calculator could not "
                "initialise; verify the .NET 8 Runtime is installed"
            ) from exc
        log.info("map PP calculator ready: osu-tools-py %s (official lazer DLLs)", version)
        return self._calculator_instance

    @staticmethod
    def _result_pp(result) -> float:
        success = getattr(result, "is_success", None)
        if success is False:
            raise ValueError(str(getattr(result, "error", "PP calculation failed")))
        error = getattr(result, "error", None)
        value = getattr(result, "pp", None)
        if value is None:
            raise ValueError(str(error or "calculator returned no pp value"))
        value = float(value)
        if value <= 0:
            raise ValueError(str(error or "calculator returned non-positive pp"))
        return value

    def _calculate_request(
        self, key: tuple[int, tuple[str, ...], tuple[float, ...]]
    ) -> PPProjection | None:
        if not self.enabled():
            return None
        bid, mods, accs = key
        osu_path = self._download_osu(bid).resolve()
        calc = self._calculator()
        requests = [
            {
                "file_path": str(osu_path),
                "mode": int(self.cfg.get("ruleset_id", 0)),
                "mods": list(mods),
                "acc": float(accuracy),
            }
            for accuracy in accs
        ]

        # calculate_many() reuses map decoding / difficulty work.  Keep a
        # compatibility fallback in case a future package temporarily changes
        # the batch API while preserving calculate().
        try:
            results = calc.calculate_many(requests)
        except (AttributeError, TypeError):
            results = [calc.calculate(**payload) for payload in requests]

        # pythonnet collections are enumerable, but depending on the concrete
        # bridge type they are not guaranteed to implement Python's ``len``.
        # Materialise once so the count check and zip below are predictable.
        results = list(results)

        if len(results) != len(accs):
            raise ValueError("calculator returned an unexpected result count")
        rows = tuple(
            (float(acc), self._result_pp(result))
            for acc, result in zip(accs, results)
        )
        return self._projection_for(rows, mods)

    @staticmethod
    def _projection_for(
        rows: tuple[tuple[float, float], ...], mods: tuple[str, ...]
    ) -> PPProjection:
        if tuple(acc for acc, _pp in rows) == DEFAULT_ACCURACIES:
            return PPProjection(
                rows[0][1], rows[1][1], rows[2][1], mods=mods
            )
        return PPProjection(mods=mods, values=rows)

    def _store(
        self,
        key: tuple[int, tuple[str, ...], tuple[float, ...]],
        projection: PPProjection,
    ) -> None:
        now = time.time()
        with self._lock:
            self._memory[key] = (now, projection)
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "beatmap_id": key[0],
            "calculated_at": now,
            "backend": f"osu-tools-py-{self._calculator_version or 'unknown'}",
            "mods": list(projection.mods),
            "values": [
                {"accuracy": acc, "pp": pp} for acc, pp in projection.rows()
            ],
        }
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
