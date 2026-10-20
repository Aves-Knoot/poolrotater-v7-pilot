"""Strict offline recovery of graph observations from sanitized health snapshots.

The collector only stages evidence. The graph sampler owns locking and merging
into its private stores. No network snapshot is inferred from owner telemetry.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import math
import os

BUCKET_SECONDS = 300
RETENTION_SECONDS = 31 * 86400
ROOM_ALIASES = ("room1", "room2", "room3", "room4")
MAX_SNAPSHOT_BYTES = 256 * 1024
MAX_SNAPSHOTS = 10000


def _epoch(value):
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _load_snapshot(path):
    """Read one small regular health snapshot; ignore links and oversized files."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return None
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _validated_snapshot(item, *, now, cutoff):
    """Return a verified owner observation and non-secret provenance, or None.

    generated_at is the sample's public snapshot timestamp. Each room's own
    fresh worker timestamp must be within 30 seconds of it. Health snapshots
    older than the graph retention window are never considered.
    """
    if item.get("schema") != 1 or item.get("sanitized") is not True:
        return None
    generated = _epoch(item.get("generated_at"))
    if generated is None or generated < cutoff or generated > now + 30:
        return None
    health = item.get("health")
    if not isinstance(health, dict) or health.get("healthy") is not True:
        return None
    system = item.get("system")
    service = system.get("service") if isinstance(system, dict) else None
    if not isinstance(service, dict) or service.get("active_state") != "active" or service.get("sub_state") != "running":
        return None

    topology = item.get("room_topology")
    if not isinstance(topology, dict):
        return None
    expected = topology.get("expected_aliases")
    inactive = topology.get("intentionally_inactive_aliases")
    if (topology.get("source") != "owner config/host.json" or not isinstance(expected, list)
            or not isinstance(inactive, list) or len(expected) + len(inactive) != 4
            or any(alias not in ROOM_ALIASES for alias in expected + inactive)
            or len(set(expected + inactive)) != 4
            or topology.get("expected_count") != len(expected)):
        return None
    rooms = item.get("rooms")
    if not isinstance(rooms, list) or len(rooms) != len(expected):
        return None
    by_alias = {}
    for room in rooms:
        if not isinstance(room, dict) or room.get("alias") not in expected or room["alias"] in by_alias:
            return None
        by_alias[room["alias"]] = room
    if set(by_alias) != set(expected):
        return None

    counts = []
    room_provenance = []
    for alias in ROOM_ALIASES:
        room = by_alias.get(alias)
        if room is None:
            counts.append(None)
            room_provenance.append({"alias": alias, "status": "intentionally_inactive",
                                    "state": "OFFLINE"})
            continue
        room_id = room.get("room_id")
        count = room.get("occupants")
        observed_at = _epoch(room.get("live_status_observed_at"))
        if (type(room_id) is not int or room_id <= 0
                or room.get("occupants_source") != "fresh_worker_status"
                or type(count) is not int or not 0 <= count <= 512
                or observed_at is None or abs(generated - observed_at) > 30
                or room.get("state") not in ("IDLE", "STARTING", "PLAYING", "SCORING")):
            return None
        counts.append(count)
        room_provenance.append({"alias": alias, "room_id": room_id,
                                "observed_at": observed_at,
                                "state": room["state"],
                                "occupants_source": "fresh_worker_status"})
    return {"observed_at": generated, "counts": counts,
            "service_online": 1,
            "source": "sanitized_health_snapshot",
            "source_generated_at": generated,
            "rooms": room_provenance}


def discover_candidates(snapshot_dir, *, existing_samples=(), now, max_snapshots=MAX_SNAPSHOTS):
    """Build candidate observation rows from verified health snapshots.

    Rows use the sampler's five-minute bucket schema. Original snapshot and
    per-room observed timestamps, room IDs, and evidence source are retained in
    private provenance. Existing rows always win. Conflicting verified health
    records in one bucket are withheld as unknown rather than selected or
    averaged. Callers must hold the sampler's lock when merging the returned
    rows into persistent state.
    """
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("now must be a finite timestamp")
    cutoff = now - RETENTION_SECONDS
    existing_buckets = {row[0] // BUCKET_SECONDS * BUCKET_SECONDS for row in existing_samples
                        if isinstance(row, list) and row and type(row[0]) is int}
    root = Path(snapshot_dir)
    candidates_by_bucket = {}
    conflicted_buckets = set()
    counts = {"files_seen": 0, "invalid_or_stale": 0, "verified_snapshots": 0,
              "existing_bucket_retained": 0, "conflicting_buckets": 0}
    try:
        paths = sorted(root.glob("health-*.json"), key=lambda p: p.name)[-min(max_snapshots, MAX_SNAPSHOTS):]
    except OSError:
        paths = []
    for path in paths:
        counts["files_seen"] += 1
        item = _load_snapshot(path)
        evidence = _validated_snapshot(item, now=now, cutoff=cutoff) if item else None
        if evidence is None:
            counts["invalid_or_stale"] += 1
            continue
        counts["verified_snapshots"] += 1
        bucket = int(evidence["observed_at"] // BUCKET_SECONDS) * BUCKET_SECONDS
        if bucket in existing_buckets:
            counts["existing_bucket_retained"] += 1
            continue
        row = [bucket, *evidence["counts"], evidence["service_online"]]
        payload = (tuple((room["alias"], room.get("room_id"), evidence["counts"][i], room["state"])
                         for i, room in enumerate(evidence["rooms"])), evidence["service_online"])
        if bucket in conflicted_buckets:
            continue
        record = {"row": row, "evidence": evidence, "payload": payload}
        prior = candidates_by_bucket.get(bucket)
        if prior is None:
            candidates_by_bucket[bucket] = record
        elif prior["payload"] != payload:
            conflicted_buckets.add(bucket)
            candidates_by_bucket.pop(bucket, None)
    conflicts = conflicted_buckets
    counts["conflicting_buckets"] = len(conflicts)
    candidates = [candidates_by_bucket[b] for b in sorted(candidates_by_bucket)
                  if b not in conflicts]
    return {"rows": [entry["row"] for entry in candidates],
            "provenance": [{"bucket_at": entry["row"][0], **entry["evidence"]}
                           for entry in candidates],
            "withheld_conflict_buckets": sorted(conflicts),
            "counts": counts}


def merge_candidates(existing_samples, staged, *, now):
    """Pure merge for offline review/tests; never replaces a known bucket."""
    cutoff = now - RETENTION_SECONDS
    by_time = {}
    occupied_buckets = set()
    for row in existing_samples:
        if (isinstance(row, list) and len(row) == 6 and type(row[0]) is int
                and cutoff <= row[0] <= now + 30):
            by_time[row[0]] = row
            occupied_buckets.add(row[0] // BUCKET_SECONDS * BUCKET_SECONDS)
    for row in staged.get("rows", []):
        if (isinstance(row, list) and len(row) == 6 and type(row[0]) is int
                and cutoff <= row[0] <= now + 30
                and row[0] // BUCKET_SECONDS * BUCKET_SECONDS not in occupied_buckets):
            by_time[row[0]] = row
            occupied_buckets.add(row[0] // BUCKET_SECONDS * BUCKET_SECONDS)
    return [by_time[key] for key in sorted(by_time)]


def write_staged_backfill(path, staged):
    """Atomically write a reviewed stage document with owner-only permissions."""
    target = Path(path)
    if target.is_symlink():
        raise ValueError("staging target must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".new")
    payload = json.dumps(staged, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
