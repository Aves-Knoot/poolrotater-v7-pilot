"""Aggregate-only Taiko total-PP to NM-star calibration with safe fallback."""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path


def read_calibration(db_path):
    """Read monotone aggregate points; return an empty tuple on missing/invalid data."""
    path = Path(db_path).resolve()
    if not path.is_file() or path.is_symlink():
        return ()
    try:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        rows = db.execute('SELECT pp,sr,players,scores,charts FROM taiko_auto_calibration ORDER BY pp').fetchall()
        db.close()
    except sqlite3.Error:
        return ()
    if len(rows) < 2:
        return ()
    points = []
    last_pp = last_sr = -math.inf
    for pp, sr, players, scores, charts in rows:
        if (type(pp) not in (float, int) or type(sr) not in (float, int)
                or not math.isfinite(pp) or not math.isfinite(sr) or pp < 0 or sr <= 0
                or type(players) is not int or players < 5 or type(scores) is not int or scores < 20
                or type(charts) is not int or charts < 1 or pp <= last_pp or sr < last_sr):
            return ()
        points.append((float(pp), float(sr)))
        last_pp, last_sr = pp, sr
    return tuple(points)


def target_sr_for_pp(taiko_total_pp, calibration):
    """Linearly interpolate aggregate exposure medians; clamp to sampled endpoints.

    Returns None when the profile value or calibration is unavailable so callers
    can preserve their configured/manual range without substituting Standard PP.
    """
    if (type(taiko_total_pp) not in (int, float) or not math.isfinite(taiko_total_pp)
            or taiko_total_pp < 0 or len(calibration) < 2):
        return None
    pp = float(taiko_total_pp)
    if pp <= calibration[0][0]:
        return calibration[0][1]
    if pp >= calibration[-1][0]:
        return calibration[-1][1]
    for (lo_pp, lo_sr), (hi_pp, hi_sr) in zip(calibration, calibration[1:]):
        if lo_pp <= pp <= hi_pp:
            fraction = (pp - lo_pp) / (hi_pp - lo_pp)
            return lo_sr + (hi_sr - lo_sr) * fraction
    return None



def calibrated_target_sr(taiko_total_pp, calibration, *, half_width=0.4, bias=0.0, step=0.05):
    if (type(half_width) not in (int, float) or not math.isfinite(half_width) or half_width < 0
            or type(bias) not in (int, float) or not math.isfinite(bias)
            or type(step) not in (int, float) or not math.isfinite(step) or step < 0):
        return None
    target = target_sr_for_pp(taiko_total_pp, calibration)
    if target is None:
        return None
    target += float(bias)
    if step:
        target = round(target / float(step)) * float(step)
    return max(0.1 + float(half_width), target)


def range_for_pp(taiko_total_pp, calibration, *, half_width=0.4, step=0.05, bias=0.0):
    """Return a Taiko range from the aggregate curve, or None when uncalibrated."""
    if (type(half_width) not in (int, float) or not math.isfinite(half_width) or half_width < 0
            or type(step) not in (int, float) or not math.isfinite(step) or step < 0
            or type(bias) not in (int, float) or not math.isfinite(bias)):
        return None
    target = calibrated_target_sr(taiko_total_pp, calibration, half_width=half_width, bias=bias, step=step)
    if target is None:
        return None
    return max(0.1, target - float(half_width)), target + float(half_width)
