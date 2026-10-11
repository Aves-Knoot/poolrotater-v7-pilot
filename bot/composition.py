"""Native Mania composition filters, independent of empirical Farm scoring."""
import math

COMPOSITIONS = frozenset({'rice', 'noodle'})


def composition_score(values, style, *, relaxed=False):
    """Return strength or None; absent/invalid evidence never means tap-only."""
    if style not in COMPOSITIONS:
        return None
    try:
        hold = float(values['hold_ratio'])
        coverage = float(values['hold_coverage'])
        stream = float(values['stream_event_ratio'])
    except (KeyError, TypeError, ValueError):
        return None
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in (hold, coverage, stream)):
        return None
    if style == 'rice':
        if hold > (.10 if relaxed else .05) or coverage > (.06 if relaxed else .03) or stream > (.35 if relaxed else .20):
            return None
        # Tap chords are allowed; rice here also limits sustained single streams.
        return max(0.0, 1.0 - 3.5 * hold - .30 * stream - .10 * coverage)
    if hold < (.50 if relaxed else .65) or coverage < (.06 if relaxed else .10):
        return None
    return .75 * hold + .25 * min(1.0, coverage / .35)
