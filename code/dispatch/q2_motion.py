"""Continuous SI motion and adjacent-crane clearance for the Q2 EF track."""

from __future__ import annotations

from bisect import bisect_right
from itertools import pairwise
from math import inf, isclose, isfinite, sqrt

EPS = 1e-8


def _segment(start, end, x0, x1, v0, v1, a, *, crane_id, mode, gear, load,
             task_id, ladle_id):
    return {
        "crane_id": crane_id, "t0_s": start, "t1_s": end, "x0": x0, "x1": x1,
        "v0": v0, "v1": v1, "a": a, "mode": mode, "gear": gear,
        "load_state": load, "task_id": task_id, "ladle_id": ladle_id,
        "threshold_context": "adjacent_reference_point_separation",
    }


def hold_segment(start, end, position, *, crane_id=None, mode="idle", load="no_load",
                 task_id=None, ladle_id=None):
    """Reserve a stationary position on the same track as all moving cranes."""
    return _segment(start, end, position, position, 0.0, 0.0, 0.0, crane_id=crane_id,
                    mode=mode, gear=None, load=load, task_id=task_id, ladle_id=ladle_id)


def _zones(profile, crane_id):
    for zone in profile.get("speed_zones", []):
        if crane_id is None or crane_id in zone["crane_ids"]:
            yield zone


def _span_limits(gears, gear, load, zones, midpoint):
    containing = [z for z in zones if z["interval_m"][0] < midpoint < z["interval_m"][1]]
    if not containing:
        return gear, gears[gear]
    permitted = set(gears)
    for zone in containing:
        permitted.intersection_update(zone["permitted_gears"])
    allowed = [name for name in permitted if gears[name] <= gears[gear] + EPS]
    if not allowed:
        raise ValueError("No permitted speed gear for a crossed restricted zone")
    active = max(allowed, key=lambda name: (gears[name], name))
    return active, min(gears[active], *(z["speed_caps_m_s"][load] for z in containing))


def plan_move(profile, x0, x1, start, load="no_load", gear="cruise", *,
              crane_id=None, mode=None, task_id=None, ladle_id=None):
    """Return a rest-to-rest profile respecting all crossed EF zone boundaries.

    At each boundary the squared speed is first limited by adjacent caps. A
    forward acceleration pass and backward braking pass impose reachability;
    each remaining span is then solved with its two boundary speeds fixed.
    """
    left, right = profile["rail_limits_m"]
    if not all(isfinite(x) for x in (x0, x1, start)) or not (
            left - EPS <= x0 <= right + EPS and left - EPS <= x1 <= right + EPS):
        raise ValueError("Motion endpoint outside EF rail or nonfinite")
    if abs(x1 - x0) <= EPS:
        return []
    limits = profile["motion"]["mobile_450t"][load]
    gears = limits["gear_speed_caps_m_s"]
    acceleration, brake = limits["acceleration_m_s2"], limits["deceleration_m_s2"]
    if min(acceleration, brake, gears[gear]) <= 0:
        raise ValueError("Motion caps must be positive")
    if crane_id is not None and any(
            c["crane_id"] == crane_id and not c["dispatch_candidate"]
            for c in profile.get("crane_catalog", [])):
        raise ValueError("A fixed occupied crane cannot move")
    zones = list(_zones(profile, crane_id))
    direction = 1 if x1 > x0 else -1
    nodes = [x0, x1]
    lo, hi = sorted((x0, x1))
    nodes.extend(x for zone in zones for x in zone["interval_m"] if lo < x < hi)
    nodes = sorted(set(nodes), reverse=direction < 0)
    spans = [_span_limits(gears, gear, load, zones, (a + b) / 2)
             for a, b in pairwise(nodes)]
    lengths = [abs(b - a) for a, b in pairwise(nodes)]
    caps2 = [cap * cap for _, cap in spans]
    speed2 = [0.0, *(min(a, b) for a, b in pairwise(caps2)), 0.0]
    for i, distance in enumerate(lengths):
        speed2[i + 1] = min(speed2[i + 1], speed2[i] + 2 * acceleration * distance)
    for i in range(len(lengths) - 1, -1, -1):
        speed2[i] = min(speed2[i], speed2[i + 1] + 2 * brake * lengths[i])

    segments = []
    time, position = start, x0
    label = mode or ("empty_move" if load == "no_load" else "loaded_move")
    for i, (active_gear, cap) in enumerate(spans):
        distance = lengths[i]
        u, v = sqrt(speed2[i]), sqrt(speed2[i + 1])
        peak = min(cap, sqrt((2 * distance + u * u / acceleration + v * v / brake)
                             / (1 / acceleration + 1 / brake)))
        accelerate_s = max(0.0, (peak - u) / acceleration)
        brake_s = max(0.0, (peak - v) / brake)
        cruise_m = distance - (u + peak) * accelerate_s / 2 - (peak + v) * brake_s / 2
        phases = [(accelerate_s, u, peak, acceleration),
                  (max(0.0, cruise_m) / peak, peak, peak, 0.0),
                  (brake_s, peak, v, -brake)]
        phases = [phase for phase in phases if phase[0] > EPS]
        for j, (dt, first, final, accel) in enumerate(phases):
            end = (nodes[i + 1] if j + 1 == len(phases)
                   else position + direction * (first + final) * dt / 2)
            segments.append(_segment(time, time + dt, position, end, direction * first,
                                     direction * final, direction * accel, crane_id=crane_id,
                                     mode=label, gear=active_gear, load=load, task_id=task_id,
                                     ladle_id=ladle_id))
            time, position = time + dt, end
    return segments


def state_at(path, initial, at, ends=None):
    """Return position, signed speed, and acceleration, with stationary tails."""
    if not path:
        return initial, 0.0, 0.0
    if ends is None:
        ends = [segment["t1_s"] for segment in path]
    i = bisect_right(ends, at)
    if i == len(path):
        return path[-1]["x1"], 0.0, 0.0
    segment = path[i]
    dt = at - segment["t0_s"]
    if dt < 0:
        return (path[i - 1]["x1"] if i else initial), 0.0, 0.0
    return (segment["x0"] + segment["v0"] * dt + 0.5 * segment["a"] * dt * dt,
            segment["v0"] + segment["a"] * dt, segment["a"])


def position_at(path, initial, at, ends=None):
    return state_at(path, initial, at, ends)[0]


def check_gap(paths, order, minimum_gap, *, initial_positions=None, detailed=False,
              start_s=0.0, horizon_s=None):
    """Check every affine-acceleration overlap, including its interior extrema."""
    order = tuple(order)
    initial = initial_positions or {}
    tracks = {cid: paths.get(cid, []) for cid in order}
    first = {cid: initial[cid] if cid in initial else tracks[cid][0]["x0"] for cid in order}
    ends = {cid: [s["t1_s"] for s in path] for cid, path in tracks.items()}
    if horizon_s is None:
        horizon_s = max((max(times, default=start_s) for times in ends.values()), default=start_s)
    if horizon_s < start_s:
        raise ValueError("Clearance horizon must not precede its start")
    best = {"ok": True, "min_gap": inf, "pair": [], "time_s": start_s, "threshold": None}
    worst_margin = inf
    for lhs, rhs in pairwise(order):
        threshold = minimum_gap[lhs, rhs] if isinstance(minimum_gap, dict) else minimum_gap
        knots = {start_s, horizon_s}
        for cid in (lhs, rhs):
            knots.update(v for s in tracks[cid] for v in (s["t0_s"], s["t1_s"])
                         if start_s < v < horizon_s)
        grid = sorted(knots)
        intervals = list(pairwise(grid)) or [(start_s, start_s)]
        for begin, end in intervals:
            l, u, a = state_at(tracks[lhs], first[lhs], begin, ends[lhs])
            r, v, b = state_at(tracks[rhs], first[rhs], begin, ends[rhs])
            gap, dg, ddg, width = r - l, v - u, b - a, end - begin
            values = [(gap, begin), (gap + dg * width + ddg * width * width / 2, end)]
            if ddg > 0:
                t = -dg / ddg
                if 0 < t < width:
                    values.append((gap + dg * t + ddg * t * t / 2, begin + t))
            minimum, time = min(values)
            if minimum < best["min_gap"]:
                best.update(min_gap=minimum, pair=[lhs, rhs], time_s=time, threshold=threshold)
            margin = minimum - threshold
            if margin < worst_margin:
                worst_margin = margin
            if margin < -EPS:
                best["ok"] = False
                if not detailed:
                    return {"ok": False, "min_gap": minimum, "pair": [lhs, rhs],
                            "time_s": time, "threshold": threshold, "margin": margin}
    best["margin"] = worst_margin
    return best


def validate_motion(profile, paths, *, initial_positions, horizon_s, minimum_gap=None):
    """Verify exported motion, including SI equations, gear rules and clearance."""
    left, right = profile["rail_limits_m"]
    order = profile["crane_order"]
    parked = {c["crane_id"] for c in profile["crane_catalog"] if not c["dispatch_candidate"]}

    def same(a, b):
        return isclose(a, b, rel_tol=0, abs_tol=EPS)

    for cid in order:
        end, x, v = 0.0, initial_positions[cid], 0.0
        if not isfinite(x) or not left - EPS <= x <= right + EPS:
            raise ValueError("Initial crane position outside EF rail")
        for seg in paths[cid]:
            t0, t1, x0, x1, v0, v1, accel = (
                seg[k] for k in ("t0_s", "t1_s", "x0", "x1", "v0", "v1", "a")
            )
            if not all(isfinite(q) for q in (t0, t1, x0, x1, v0, v1, accel)):
                raise ValueError("Nonfinite motion record")
            dt = t1 - t0
            if (dt <= 0 or not same(t0, end) or not same(x0, x) or not same(v0, v)
                    or t1 > horizon_s + EPS or not left - EPS <= x1 <= right + EPS
                    or not same(x0 + v0 * dt + 0.5 * accel * dt * dt, x1)
                    or not same(v0 + accel * dt, v1)
                    or (v0 > EPS and v1 < -EPS) or (v0 < -EPS and v1 > EPS)):
                raise ValueError("Invalid segment kinematics, connection or rail boundary")
            if abs(x1 - x0) > EPS:
                if cid in parked:
                    raise ValueError("Non-dispatch crane moved")
                load, gear = seg["load_state"], seg["gear"]
                params = profile["motion"]["mobile_450t"][load]
                gears = params["gear_speed_caps_m_s"]
                signed = accel if x1 > x0 else -accel
                if (max(abs(v0), abs(v1)) > gears[gear] + EPS
                        or signed > params["acceleration_m_s2"] + EPS
                        or -signed > params["deceleration_m_s2"] + EPS):
                    raise ValueError("Speed or acceleration exceeded")
                lo, hi = sorted((x0, x1))
                for zone in _zones(profile, cid):
                    a, b = zone["interval_m"]
                    z0, z1 = max(lo, a), min(hi, b)
                    if z1 - z0 > EPS:
                        peak = sqrt(max(0.0, *(v0 * v0 + 2 * accel * (z - x0) for z in (z0, z1))))
                        if gear not in zone["permitted_gears"] or peak > zone["speed_caps_m_s"][load] + EPS:
                            raise ValueError("Restricted-zone gear or speed exceeded")
            elif max(abs(v0), abs(v1), abs(accel)) > EPS:
                raise ValueError("Nonstationary hold")
            end, x, v = t1, x1, v1
        if not same(v, 0):
            raise ValueError("A trajectory cannot end at nonzero speed")
    if minimum_gap is None:
        minimum_gap = {(p["left_crane"], p["right_crane"]): p["min_distance_m"]
                       for p in profile["clearance"]["adjacent_pairs"]}
    gap = check_gap(paths, order, minimum_gap, initial_positions=initial_positions,
                    detailed=True, horizon_s=horizon_s)
    if not gap["ok"]:
        raise ValueError("Continuous adjacent-crane gap violated")
    return gap
