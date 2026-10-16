"""Private full-row owner/network merger; only allowlisted bounded rankings leave it.

The owner ledger predates interval telemetry. Its cumulative connected time is
kept separately: inventing historical intervals would overstate deduplication.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import stat

PERIODS = {"all": 0, "7d": 7 * 86400, "14d": 14 * 86400, "30d": 30 * 86400}
SCOPES = ("all", "standard", "mania_7k", "mania_4k")
MODES = {"standard": "standard", "mania4": "mania_4k", "mania7": "mania_7k",
         "mania_4k": "mania_4k", "mania_7k": "mania_7k", "mania": "mania_other"}
HOST = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")


def number(value, default=0):
    if type(value) not in (int, float) or not math.isfinite(value):
        return default
    return value


def clean(value, limit=64):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value or ""))[:limit]


def read_snapshot(now):
    """Load a local private export, never contact the hub or read its database.

    Missing/expired exports disable network publication; owner statistics remain
    available. The hub publisher, not this web exporter, authenticates sources.
    """
    path = Path(os.environ.get("POOLROTATER_NETWORK_STATISTICS_SNAPSHOT",
                               "/var/lib/poolrotater-network-statistics/network-statistics.json"))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > 128 * 1024 * 1024 or stat.S_IMODE(before.st_mode) & 0o027):
                raise ValueError("unsafe private network snapshot")
            payload = stream.read(128 * 1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
        if (len(payload) > 128 * 1024 * 1024 or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError("network snapshot changed while reading")
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("format") != 1:
            raise ValueError("unsupported network snapshot")
        generated = number(data.get("generated_at"), -1)
        if not 0 <= now - generated <= 600:
            raise ValueError("stale network snapshot")
        for key in ("hosts", "presence", "joins", "matches", "hidden_user_ids"):
            if not isinstance(data.get(key), list):
                raise ValueError("invalid network snapshot")
        return data
    except (OSError, ValueError, TypeError):
        return None


def empty_player(uid):
    return {"user_id": uid, "username": f"Player #{uid}", "seconds": 0,
            "maps": 0, "scores": 0, "pp": 0, "unique": set(), "days": set(),
            "accuracy_sum": 0, "accuracy_n": 0, "best_gain": None,
            "best_score": None, "top_plays": 0, "visits": 0, "time_by_type": {}}


def groups():
    return {mode: {period: {} for period in PERIODS} for mode in SCOPES}


def union_seconds(intervals):
    end = None
    total = 0.0
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += stop - start
        elif stop > end:
            total += stop - end
        end = max(end or stop, stop)
    return total


def publish_group(rows, hidden, *, period, owner_time=False):
    """Exact top 50 for every supported ranking, without a public user directory."""
    totals = {"players": len(rows), "maps": sum(p["maps"] for p in rows.values()),
              "scores": sum(p["scores"] for p in rows.values()),
              "pp": round(sum(p["pp"] for p in rows.values()), 2),
              "seconds": round(sum(p["seconds"] for p in rows.values())) if not owner_time or period == "all" else None,
              "legacy_owner_seconds": round(sum(p.get("legacy_owner_seconds", 0) for p in rows.values())) if period == "all" else None,
              "legacy_community_seconds": round(sum(p.get("legacy_community_seconds", 0) for p in rows.values())) if period == "all" else None}
    visible = []
    for uid, raw in rows.items():
        if uid in hidden:
            continue
        p = deepcopy(raw)
        p["username"] = clean(p.get("username")) or f"Player #{uid}"
        p["unique_maps"] = len(p.pop("unique"))
        p["active_days"] = len(p.pop("days"))
        p["average_accuracy"] = round(p.pop("accuracy_sum") / p["accuracy_n"], 2) if p["accuracy_n"] >= 5 else None
        p["pp"] = round(p["pp"], 2)
        p["seconds"] = round(p["seconds"])
        visible.append(p)
    keys = [lambda p: p["pp"], lambda p: p["maps"], lambda p: p["unique_maps"],
            lambda p: p["active_days"], lambda p: (p["best_gain"] or {}).get("gain", 0),
            lambda p: (p["best_score"] or {}).get("score_pp", 0),
            lambda p: p["average_accuracy"] if p["accuracy_n"] >= 20 and p["average_accuracy"] is not None else -1]
    if not owner_time or period == "all":
        keys.append(lambda p: p["seconds"])
    chosen = set()
    for key in keys:
        chosen.update(p["user_id"] for p in sorted(visible, key=lambda p: (-key(p), p["user_id"]))[:50])
    return {"totals": totals, "public_players": len(visible),
            "time_available": not owner_time or period == "all",
            "leaders": [p for p in visible if p["user_id"] in chosen]}


def publish_groups(raw, hidden, *, owner_time=False):
    return {mode: {period: publish_group(rows, hidden, period=period, owner_time=owner_time)
                   for period, rows in periods.items()} for mode, periods in raw.items()}


def compose(owner_groups, names, hidden, snapshot, owner_room_ids, now):
    """Return combined scopes plus owner/per-host views and coverage metadata.

    Stable osu! user IDs join owner and community rows before ranking truncation.
    Network duration is interval union per user, including overlapping hosts;
    same-room owner match records are excluded because the owner ledger already
    contains them. Historical owner cumulative duration remains separate.
    """
    owner = deepcopy(owner_groups)
    for periods in owner.values():
        for rows in periods.values():
            for uid, p in rows.items():
                p["username"] = names.get(uid, p["username"])
    hidden = set(hidden)
    if snapshot:
        hidden.update(uid for uid in snapshot.get("hidden_user_ids", []) if type(uid) is int and uid > 0)
    combined = deepcopy(owner)
    for periods in combined.values():
        for rows in periods.values():
            for p in rows.values():
                p["legacy_owner_seconds"] = p["seconds"]
                p["seconds"] = 0
                p["time_by_type"] = {}
    host_rows = {}
    host_meta = {}
    if snapshot:
        for row in snapshot["hosts"]:
            hid = row.get("host_id") if isinstance(row, dict) else None
            if not isinstance(hid, str) or not HOST.fullmatch(hid) or hid == "owner" or row.get("enabled") is not True:
                continue
            consent = number(row.get("consent_at"), None)
            if consent is None or consent < 0 or consent > now:
                continue
            host_rows[hid] = groups()
            host_meta[hid] = {"label": clean(row.get("label")) or "Community host", "consent_at": consent,
                              "updated_at": min(now, number(row.get("updated_at"), consent))}
    intervals = defaultdict(list)
    match_seen = defaultdict(set)
    joins_seen = defaultdict(set)
    achievement_seen = defaultdict(set)
    tracked_pp = bool(snapshot and snapshot.get("coverage", {}).get("tracked_pp_supported") is True)
    historical_import_present = False
    baseline_cutoffs = {}
    owner_hosts = set(snapshot.get("owner_host_ids", [])) if snapshot else set()
    if snapshot:
        owner_hosts.update(row.get("host_id") for row in snapshot["presence"] + snapshot["matches"] if isinstance(row, dict) and row.get("room_id") in owner_room_ids)
    def player(target, mode, period, uid, username):
        rows = target[mode][period]
        if uid not in rows:
            rows[uid] = empty_player(uid)
        # Owner names take priority; a community host cannot rename an owner
        # player in the public combined page.
        rows[uid]["username"] = names.get(uid) or clean(username) or rows[uid]["username"]
        return rows[uid]
    def targets(mode, at):
        for scope in (("all", mode) if mode in SCOPES else ("all",)):
            for period, seconds in PERIODS.items():
                if seconds == 0 or at >= now - seconds:
                    yield scope, period
    def valid(row):
        if not isinstance(row, dict):
            return None
        hid = row.get("host_id")
        uid = row.get("user_id")
        mode = MODES.get(row.get("mode"))
        if hid not in host_rows or type(uid) is not int or not 0 < uid <= 2**53 or mode is None:
            return None
        return hid, uid, mode
    if snapshot:
        baseline_rows = {}
        for row in snapshot.get("baselines", []):
            identity = valid(row)
            if not identity:
                continue
            hid, uid, mode = identity
            cutoff = number(row.get("cutoff"), -1)
            seconds = number(row.get("total_seconds"), -1)
            source_id = row.get("source_id")
            visits = row.get("visit_count")
            if not isinstance(source_id, str) or not source_id or not 0 <= cutoff <= now or seconds < 0 or type(visits) is not int or visits < 0:
                continue
            key = (hid, uid, mode)
            previous = baseline_rows.get(key)
            # A newer reviewed cumulative snapshot replaces the old one; it
            # never increments it, even following a database restore/import.
            if previous is None or cutoff > previous["cutoff"]:
                baseline_rows[key] = {**row, "cutoff": cutoff}
        combined_baselines = {}
        for (hid, uid, mode), row in baseline_rows.items():
            baseline_cutoffs[hid, uid, mode] = row["cutoff"]
            for scope in (("all", mode) if mode in SCOPES else ("all",)):
                p = player(host_rows[hid], scope, "all", uid, row.get("username"))
                p["legacy_community_seconds"] = p.get("legacy_community_seconds", 0) + row["total_seconds"]
                p["visits"] += row["visit_count"]
            historical_import_present = True
            if hid in owner_hosts:
                continue
            key = (row["source_id"], uid, mode)
            previous = combined_baselines.get(key)
            if previous is None or row["cutoff"] > previous["cutoff"]:
                combined_baselines[key] = row
        for row in combined_baselines.values():
            hid, uid, mode = valid(row)
            for scope in (("all", mode) if mode in SCOPES else ("all",)):
                p = player(combined, scope, "all", uid, row.get("username"))
                p["legacy_community_seconds"] = p.get("legacy_community_seconds", 0) + row["total_seconds"]
                p["visits"] += row["visit_count"]
        for row in snapshot["presence"]:
            identity = valid(row)
            if not identity:
                continue
            hid, uid, mode = identity
            start = max(number(row.get("started_at"), now), host_meta[hid]["consent_at"], baseline_cutoffs.get((hid, uid, mode), 0), baseline_cutoffs.get((hid, uid, "mania_other"), 0) if mode.startswith("mania_") else 0)
            stop = min(number(row.get("ended_at"), now), now)
            if start >= stop:
                continue
            for scope in (("all", mode) if mode in SCOPES else ("all",)):
                for period, seconds in PERIODS.items():
                    clipped = max(start, now - seconds) if seconds else start
                    if clipped >= stop:
                        continue
                    for key, target in (("combined", combined), (hid, host_rows[hid])):
                        p = player(target, scope, period, uid, row.get("username"))
                        intervals[key, scope, period, uid].append((clipped, stop))
                        # Per-mode union also supplies the existing player Details.
                        if scope == mode:
                            intervals[key, "type:" + mode, period, uid].append((clipped, stop))
                            if mode.startswith("mania_"):
                                intervals[key, "type:mania", period, uid].append((clipped, stop))
                        p["days"].update(range(int(clipped // 86400), int((stop - .00001) // 86400) + 1))
        for row in snapshot["joins"]:
            identity = valid(row)
            if not identity:
                continue
            hid, uid, mode = identity
            at = number(row.get("occurred_at"), -1)
            event = row.get("event_id")
            if not isinstance(event, str) or not max(host_meta[hid]["consent_at"], baseline_cutoffs.get((hid, uid, mode), 0), baseline_cutoffs.get((hid, uid, "mania_other"), 0) if mode.startswith("mania_") else 0) <= at <= now:
                continue
            for scope, period in targets(mode, at):
                for key, target in (("combined", combined), (hid, host_rows[hid])):
                    identity_key = (hid, event, uid)
                    if identity_key in joins_seen[key, scope, period]:
                        continue
                    joins_seen[key, scope, period].add(identity_key)
                    p = player(target, scope, period, uid, row.get("username"))
                    if key != "combined" or row.get("room_id") not in owner_room_ids:
                        p["visits"] += 1
                    p["days"].add(int(at // 86400))
        for row in snapshot["matches"]:
            identity = valid(row)
            if not identity:
                continue
            hid, uid, mode = identity
            at = number(row.get("ended_at"), -1)
            match = row.get("match_key")
            beatmap = row.get("beatmap_id")
            if not isinstance(match, str) or not match or type(beatmap) is not int or beatmap <= 0 or not host_meta[hid]["consent_at"] <= at <= now:
                continue
            for scope, period in targets(mode, at):
                for key, target in (("combined", combined), (hid, host_rows[hid])):
                    if key == "combined" and row.get("room_id") in owner_room_ids:
                        continue
                    identity_key = (match, uid)
                    if identity_key in match_seen[key, scope, period]:
                        continue
                    match_seen[key, scope, period].add(identity_key)
                    p = player(target, scope, period, uid, row.get("username"))
                    p["maps"] += 1
                    p["unique"].add(beatmap)
                    p["days"].add(int(at // 86400))
                    if row.get("score_submitted") is True:
                        p["scores"] += 1
                        acc = number(row.get("accuracy"), -1)
                        if 0 <= acc <= 100:
                            p["accuracy_n"] += 1
                            p["accuracy_sum"] += acc
        if tracked_pp:
            for row in snapshot.get("achievements", []):
                identity = valid(row)
                if not identity:
                    continue
                hid, uid, mode = identity
                at = number(row.get("created_at"), -1)
                achievement_key = row.get("achievement_key")
                bid = row.get("beatmap_id")
                gain = number(row.get("profile_gain_pp"), None)
                score_pp = number(row.get("score_pp"), -1)
                rank = row.get("top_rank")
                if (not isinstance(achievement_key, str) or not achievement_key or type(bid) is not int or bid <= 0
                        or not (0 if row.get("historical") is True else host_meta[hid]["consent_at"]) <= at <= now or gain is None or score_pp < 0
                        or type(rank) is not int or not 1 <= rank <= 100):
                    continue
                gain = max(0, gain)
                historical_import_present = historical_import_present or row.get("historical") is True
                room_id = row.get("room_id")
                item = row.get("playlist_item_id")
                identity_key = ("item", room_id, item, uid) if type(room_id) is int and room_id > 0 and type(item) is int and item > 0 else ("native", achievement_key, uid)
                achievement = {"beatmap_id": bid, "label": clean(row.get("label"), 240) or f"Map #{bid}",
                               "gain": round(gain, 2), "score_pp": round(score_pp, 2),
                               "accuracy": number(row.get("accuracy"), None), "top_rank": rank, "at": at}
                for scope, period in targets(mode, at):
                    for key, target in (("combined", combined), (hid, host_rows[hid])):
                        if key == "combined" and room_id in owner_room_ids:
                            continue
                        if identity_key in achievement_seen[key, scope, period]:
                            continue
                        achievement_seen[key, scope, period].add(identity_key)
                        p = player(target, scope, period, uid, row.get("username"))
                        p["pp"] += gain
                        p["top_plays"] += 1
                        p["days"].add(int(at // 86400))
                        if not p["best_gain"] or gain > p["best_gain"]["gain"]:
                            p["best_gain"] = achievement
                        if not p["best_score"] or score_pp > p["best_score"]["score_pp"]:
                            p["best_score"] = achievement
    for (key, scope, period, uid), spans in intervals.items():
        target = combined if key == "combined" else host_rows[key]
        if scope.startswith("type:"):
            p = player(target, "all", period, uid, "")
            mode = scope[5:]
            p["time_by_type"][mode] = round(union_seconds(spans), 1)
        else:
            target[scope][period][uid]["seconds"] = union_seconds(spans)
    views = {"owner": {"label": "Owner lobbies", "kind": "owner", "scopes": publish_groups(owner, hidden, owner_time=True),
                        "coverage": {"time_basis": "legacy_owner_ledger", "pp_basis": "owner_recorded_achievements"}}}
    for hid in sorted(host_rows):
        views[hid] = {**host_meta[hid], "kind": "community", "scopes": publish_groups(host_rows[hid], hidden),
                      "coverage": {"time_basis": "verified_intervals", "maps_basis": "completed_participation", "pp_basis": "observed_tracked_achievements" if tracked_pp else "not_attributed"}}
    coverage = {"network_available": snapshot is not None, "network_generated_at": snapshot.get("generated_at") if snapshot else None,
                "participating_hosts": len(host_rows), "time_basis": "verified_interval_union",
                "legacy_owner_time_separate": True, "owner_maps_basis": "recorded_map_starts",
                "community_maps_basis": "completed_participation", "pp_basis": "observed_tracked_achievements" if tracked_pp else "owner_recorded_achievements_only",
                "tracked_pp_supported": tracked_pp,
                "pre_opt_in_community_history": historical_import_present}
    return publish_groups(combined, hidden), {"hosts": views, "coverage": coverage}
