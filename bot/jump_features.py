"""Offline snap-aim evidence, independent of Farm score, AR and map length.

Slider geometry is approximated from native curves, SV, repeats, curve length
and follow-circle leniency. A style heuristic, NOT an official difficulty calc.
No network, dependencies or database writes.
"""
import bisect
import hashlib
import math

CLASSIFIER_VERSION = 'standard_jumps_v2'


def _distance(a, b):
    return math.hypot(a[0]-b[0], a[1]-b[1])


def _bezier(points, depth=0):
    chord = _distance(points[0], points[-1])
    polygon = sum(_distance(a, b) for a, b in zip(points, points[1:]))
    if len(points) <= 2 or polygon-chord <= 1 or depth >= 10:
        return [points[0], points[-1]]
    levels = [points]
    while len(levels[-1]) > 1:
        levels.append([((a[0]+b[0])/2, (a[1]+b[1])/2)
                       for a, b in zip(levels[-1], levels[-1][1:])])
    left = [level[0] for level in levels]
    right = [level[-1] for level in reversed(levels)]
    return _bezier(left, depth+1)[:-1]+_bezier(right, depth+1)


def _curve(kind, points):
    if kind == 'L':
        return points
    if kind == 'P' and len(points) == 3:
        a, b, c = points
        d = 2*(a[0]*(b[1]-c[1])+b[0]*(c[1]-a[1])+c[0]*(a[1]-b[1]))
        if abs(d) > 1e-6:
            aa, bb, cc = (p[0]**2+p[1]**2 for p in points)
            center = ((aa*(b[1]-c[1])+bb*(c[1]-a[1])+cc*(a[1]-b[1]))/d,
                      (aa*(c[0]-b[0])+bb*(a[0]-c[0])+cc*(b[0]-a[0]))/d)
            angles = [math.atan2(p[1]-center[1], p[0]-center[0]) for p in points]
            arc = (angles[2]-angles[0]) % (2*math.pi)
            if (angles[1]-angles[0]) % (2*math.pi) > arc:
                arc -= 2*math.pi
            radius = _distance(a, center)
            count = min(512, max(2, math.ceil(abs(arc)*radius/8)))
            return [(center[0]+radius*math.cos(angles[0]+arc*i/count),
                     center[1]+radius*math.sin(angles[0]+arc*i/count)) for i in range(count+1)]
    if kind == 'C':
        out = []; padded = [points[0]]+points+[points[-1]]
        for i in range(1, len(padded)-2):
            p0, p1, p2, p3 = padded[i-1:i+3]
            count = min(128, max(4, math.ceil(_distance(p1, p2)/8)))
            for j in range(count):
                t = j/count
                out.append(tuple(.5*(2*p1[k]+(-p0[k]+p2[k])*t+
                                     (2*p0[k]-5*p1[k]+4*p2[k]-p3[k])*t*t+
                                     (-p0[k]+3*p1[k]-3*p2[k]+p3[k])*t*t*t) for k in (0, 1)))
        return out+[points[-1]]
    # Duplicate Bezier control points delimit independent curve segments.
    out, start = [], 0
    for i in range(1, len(points)):
        if points[i] == points[i-1]:
            out += _bezier(points[start:i])[:-1]; start = i
    return out+_bezier(points[start:])


def _slider_path(parts, head):
    curve = parts[5].split('|')
    if curve[0] not in ('B', 'C', 'L', 'P'):
        raise ValueError('unsupported slider curve')
    points = [head]+[tuple(map(float, p.split(':'))) for p in curve[1:]]
    repeats, length = int(parts[6]), float(parts[7])
    if (len(points) < 2 or len(points) > 256 or not 1 <= repeats <= 100
            or not math.isfinite(length) or not 0 <= length <= 100000
            or any(len(p) != 2 or not all(math.isfinite(v) and abs(v) <= 100000 for v in p) for p in points)):
        raise ValueError('invalid slider')
    fitted = _curve(curve[0], points)
    out, cumulative = [fitted[0]], [0.0]
    for a, b in zip(fitted, fitted[1:]):
        segment = _distance(a, b)
        if segment <= 1e-9:
            continue
        remaining = length-cumulative[-1]
        if segment >= remaining:
            out.append((a[0]+(b[0]-a[0])*remaining/segment,
                        a[1]+(b[1]-a[1])*remaining/segment))
            cumulative.append(length); break
        out.append(b); cumulative.append(cumulative[-1]+segment)
    if cumulative[-1] < length and len(out) > 1:
        a, b = out[-2:]; segment = _distance(a, b); extra = length-cumulative[-1]
        out.append((b[0]+(b[0]-a[0])*extra/segment, b[1]+(b[1]-a[1])*extra/segment))
        cumulative.append(length)
    return out, cumulative, repeats, length


def _slider_exit(obj, radius, until):
    path, cumulative, repeats, length = obj['path']; duration = obj['duration']
    if duration <= 0 or length <= 0 or len(path) < 2:
        return obj['head'], obj['time'], 0.0, obj['head']
    elapsed = min(duration, max(0, until-obj['time']))
    progress = repeats*elapsed/duration
    full = min(repeats, int(progress)); fraction = progress-full
    cursor = obj['head']; travel = 0.0
    def follow(p):
        nonlocal cursor, travel
        distance = _distance(cursor, p); movement = max(0.0, distance-1.8*radius)
        if movement:
            cursor = (cursor[0]+(p[0]-cursor[0])*movement/distance,
                      cursor[1]+(p[1]-cursor[1])*movement/distance)
            travel += movement
    for span in range(full):
        for p in (path if span % 2 == 0 else reversed(path)):
            follow(p)
    actual = path[-1] if full % 2 else path[0]
    if full < repeats and fraction:
        target = length*(fraction if full % 2 == 0 else 1-fraction)
        index = min(len(path)-1, max(1, bisect.bisect_left(cumulative, target)))
        iterable = path[:index] if full % 2 == 0 else list(reversed(path[index:]))
        for p in iterable:
            follow(p)
        a, b = path[index-1:index+1]
        proportion = (target-cumulative[index-1])/(cumulative[index]-cumulative[index-1])
        actual = (a[0]+(b[0]-a[0])*proportion, a[1]+(b[1]-a[1])*proportion)
        follow(actual)
    return cursor, obj['time']+elapsed, travel, actual


def analyze_jumps(text, features=None):
    """Return auditable metrics, even for valid non-Jump charts; None if invalid."""
    section, meta, timing, raw_objects = '', {}, [], []
    try:
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith('//'):
                continue
            if line.startswith('['):
                section = line
            elif section in ('[General]', '[Metadata]', '[Difficulty]') and ':' in line:
                key, value = line.split(':', 1); meta[key.strip()] = value.strip()
            elif section == '[TimingPoints]':
                p = line.split(','); t, beat = float(p[0]), float(p[1])
                if not math.isfinite(t) or not math.isfinite(beat) or beat == 0:
                    return None
                timing.append((t, beat, len(p) < 7 or p[6] == '1'))
            elif section == '[HitObjects]':
                p = line.split(','); x, y, t = map(float, p[:3]); kind = int(p[3])
                if not all(math.isfinite(v) and abs(v) <= 100000000 for v in (x, y, t)):
                    return None
                raw_objects.append((t, (x, y), kind, p))
        bid, cs = int(meta.get('BeatmapID', '0')), float(meta['CircleSize'])
        multiplier = float(meta.get('SliderMultiplier', '1.4'))
        if (bid <= 0 or int(meta.get('Mode', '0')) != 0 or not math.isfinite(cs)
                or not 0 <= cs <= 10 or not math.isfinite(multiplier) or not 0 < multiplier <= 20
                or len(raw_objects) < 20 or not any(red for _, b, red in timing if b > 0)):
            return None
        timing.sort(); raw_objects.sort(key=lambda o: o[0])
        ti, beat, sv, objects = 0, next(b for _, b, red in timing if red and b > 0), 1.0, []
        for t, head, kind, p in raw_objects:
            while ti < len(timing) and timing[ti][0] <= t:
                _, value, red = timing[ti]; ti += 1
                if red and value > 0:
                    beat, sv = value, 1.0
                elif not red and value < 0:
                    sv = min(10.0, max(.1, -100/value))
            obj = dict(time=t, head=head, beat=beat, kind=kind, duration=0.0)
            if kind & 2:
                obj['path'] = _slider_path(p, head)
                obj['duration'] = obj['path'][2]*obj['path'][3]*beat/(100*multiplier*sv)
            elif kind & 8:
                obj['duration'] = max(0, float(p[5])-t)
            elif not kind & 1:
                return None
            objects.append(obj)
    except (ValueError, IndexError, KeyError, TypeError, OverflowError, ZeroDivisionError):
        return None
    radius = 54.4-4.48*cs; diameter = 2*radius
    strict, weak, longest, longest_weak, run, weak_run = 0, 0, 0, 0, 0, 0
    rapid, transitions, jump_effort, total_effort, tracking = 0, 0, 0.0, 0.0, 0.0
    events = []
    for a, b in zip(objects, objects[1:]):
        if a['kind'] & 8 or b['kind'] & 8:
            run = weak_run = 0; continue
        dt = b['time']-a['time']
        if dt <= 0:
            run = weak_run = 0; continue
        transitions += 1
        exit_pos, exit_time, travel = (a['head'], a['time'], 0.0)
        if a['kind'] & 2:
            exit_pos, exit_time, travel, tail = _slider_exit(a, radius, b['time'])
            # Following through the tail can be easier than the lazy exit.
            distance = max(0.0, min(_distance(exit_pos, b['head'])-.6*radius,
                                    _distance(tail, b['head'])-2.4*radius))
        else:
            distance = _distance(exit_pos, b['head'])
        spacing = distance/diameter
        available = max(25, b['time']-exit_time); beats = dt/a['beat']
        jump = .35 <= beats <= 1.10 and spacing >= 1.70
        relaxed = .30 <= beats <= 1.25 and spacing >= 1.40
        strict += jump; weak += relaxed
        run = run+1 if jump else 0; weak_run = weak_run+1 if relaxed else 0
        longest = max(longest, run); longest_weak = max(longest_weak, weak_run)
        rapid += 0 < beats <= .30
        # A single extreme slider exit must not define the whole map.
        effort = min(5.0, spacing*200/available)
        total_effort += effort; jump_effort += effort if relaxed else 0
        tracking += min(5.0, travel/diameter*200/max(25, exit_time-a['time']))
        events.append((b['time'], bool(jump), bool(relaxed)))
    pairs = max(1, transitions)
    strict_ratio, weak_ratio, rapid_ratio = strict/pairs, weak/pairs, rapid/pairs
    jump_mass = jump_effort/max(.001, total_effort+tracking)
    tracking_ratio = tracking/max(.001, total_effort+tracking)
    left, count_strict, count_weak, peak_strict, peak_weak = 0, 0, 0, 0.0, 0.0
    section_jumps = 0
    for right, (t, s, w) in enumerate(events):
        count_strict += s; count_weak += w
        while events[left][0] < t-6000:
            count_strict -= events[left][1]; count_weak -= events[left][2]; left += 1
        n = right-left+1
        if n >= 8:
            peak_strict = max(peak_strict, count_strict/n)
            peak_weak = max(peak_weak, count_weak/n)
            section_jumps = max(section_jumps, count_weak)
    strict_ok = (strict >= 12 and longest >= 4 and strict_ratio >= .20
                 and peak_strict >= .45 and jump_mass >= .40
                 and rapid_ratio <= .35 and tracking_ratio <= .45)
    weak_ok = (weak >= 10 and longest_weak >= 3 and weak_ratio >= .14
               and peak_weak >= .35 and jump_mass >= .30
               and rapid_ratio <= .50 and tracking_ratio <= .60)
    aim_ratio, slider_factor = None, None
    if features:
        aim, speed = features.get('aim'), features.get('speed')
        if (isinstance(aim, (float, int)) and isinstance(speed, (float, int))
                and math.isfinite(aim) and math.isfinite(speed) and aim >= 0 and speed > 0):
            aim_ratio = aim/speed
            strict_ok &= aim_ratio >= 1.25; weak_ok &= aim_ratio >= 1.05
        value = features.get('slider_factor')
        if isinstance(value, (float, int)) and math.isfinite(value) and 0 <= value <= 1:
            slider_factor = value
            strict_ok &= value >= .85; weak_ok &= value >= .70
    score = lambda ratio, peak, length: round(min(1.0, .35*ratio+.25*peak+
                                                .25*jump_mass+.15*min(length/12, 1)), 6)
    return dict(beatmap_id=bid, strict_score=score(strict_ratio, peak_strict, longest) if strict_ok else 0.0,
                relaxed_score=score(weak_ratio, peak_weak, longest_weak) if weak_ok else 0.0,
                strict_ratio=strict_ratio, relaxed_ratio=weak_ratio, longest=longest,
                longest_relaxed=longest_weak, peak_strict=peak_strict, peak_relaxed=peak_weak,
                jump_effort_share=jump_mass, tracking_share=tracking_ratio, rapid_ratio=rapid_ratio,
                aim_speed_ratio=aim_ratio, slider_factor=slider_factor, section_jumps=section_jumps,
                slider_ratio=sum(bool(o['kind'] & 2) for o in objects)/len(objects),
                checksum=hashlib.sha256(text.encode('utf-8')).hexdigest())


def classify_jumps(text, features=None):
    result = analyze_jumps(text, features)
    if not result or not (result['strict_score'] or result['relaxed_score']):
        return None
    return result['beatmap_id'], (result['strict_score'], result['relaxed_score'], result['checksum'])
