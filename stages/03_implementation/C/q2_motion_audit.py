"""Independent SI-track audit for archived Q2 dispatch results."""

from collections import Counter, defaultdict
from itertools import pairwise
from math import isfinite, sqrt


TOLERANCE = {"time_s": 1e-7, "position_m": 1e-7, "speed_m_s": 1e-8,
             "acceleration_m_s2": 1e-10}
TIME_EPS = TOLERANCE["time_s"]
POSITION_EPS = TOLERANCE["position_m"]
SPEED_EPS = TOLERANCE["speed_m_s"]
ACCELERATION_EPS = TOLERANCE["acceleration_m_s2"]
NUMERIC_FIELDS = ("t0_s", "t1_s", "x0", "x1", "v0", "v1", "a")
MOVING_STATES = {"empty_move", "loaded_move", "yield"}
HOLD_STATES = {"idle", "parked", "wait_empty", "pickup", "wait_drop", "drop"}
HANDLING_STATES = {
    "wait_empty": ("origin", "reserved_at_s", "pickup_s"),
    "pickup": ("origin", "pickup_s", "lift_s"),
    "wait_drop": ("destination", "arrival_s", "drop_start_s"),
    "drop": ("destination", "drop_start_s", "drop_s"),
}


def finite(value):
    return type(value) in (int, float) and isfinite(value)


def state_at(segment, time_s):
    dt = time_s - segment["t0_s"]
    a = segment["a"]
    return segment["x0"] + dt * (segment["v0"] + a * dt / 2), segment["v0"] + a * dt


def interval_clearance(left, right, start_s, end_s):
    """Return (gap, time, left position, right position) for one overlap."""
    _, left_speed = state_at(left, start_s)
    _, right_speed = state_at(right, start_s)
    relative_accel = right["a"] - left["a"]
    relative_speed = right_speed - left_speed
    times = [start_s, end_s]
    if relative_accel > 0:
        offset = -relative_speed / relative_accel
        if 0 < offset < end_s - start_s:
            times.append(start_s + offset)
    candidates = []
    for time_s in times:
        x_left, _ = state_at(left, time_s)
        x_right, _ = state_at(right, time_s)
        candidates.append((x_right - x_left, time_s, x_left, x_right))
    return min(candidates)


def clearance_overlaps(left_track, right_track):
    """Intersect both breakpoint sequences in linear time."""
    i, j = 0, 0
    while i < len(left_track) and j < len(right_track):
        left, right = left_track[i], right_track[j]
        start_s = max(left["t0_s"], right["t0_s"])
        end_s = min(left["t1_s"], right["t1_s"])
        if end_s >= start_s:
            yield interval_clearance(left, right, start_s, end_s)
        left_end, right_end = left["t1_s"], right["t1_s"]
        if left_end <= right_end:
            i += 1
        if right_end <= left_end:
            j += 1


def add_issue(issues, code, severity="Critical", **where):
    issues.append({"code": code, "severity": severity, **where})


def audit_zone(segment, x1, zones, issues, where):
    x0, v0, a = (segment[k] for k in ("x0", "v0", "a"))
    low, high = sorted((x0, x1))
    for zone in zones:
        if segment["crane_id"] not in zone["crane_ids"]:
            continue
        lo, hi = zone["interval_m"]
        start_x, end_x = max(lo, low), min(hi, high)
        if end_x < start_x:
            continue
        # On a monotone constant-acceleration segment, v^2 is affine in x.
        # Include a boundary touched at a single point in the speed test.
        speed = sqrt(max(0.0, *(v0 * v0 + 2 * a * (x - x0)
                                for x in (start_x, end_x))))
        limit = zone["speed_caps_m_s"][segment["load_state"]]
        crossed = end_x - start_x > POSITION_EPS
        if speed > limit + SPEED_EPS or (
                crossed and segment["gear"] not in zone["permitted_gears"]):
            add_issue(issues, "restricted_zone", zone=zone["zone_id"], speed_m_s=speed,
                      limit_m_s=limit, gear=segment["gear"], **where)


def audit_task_segment(segment, task, output, stations, authorized_legs, crane, issues, where):
    mode, load = segment["mode"], segment["load_state"]
    task_id = segment.get("task_id")
    if mode in {"idle", "parked", "yield"}:
        if load != "no_load" or task_id is not None or segment.get("ladle_id") is not None:
            add_issue(issues, "unattended_motion_has_load_or_task", **where)
        return
    if task is None or output is None or output.get("crane_id") != crane["crane_id"]:
        add_issue(issues, "unregistered_task_or_crane", **where)
        return
    if (crane["crane_id"] not in task["eligible"] or not finite(task["mass_upper_t"])
            or not 0 <= task["mass_upper_t"] <= crane["capacity_t"]
            or (task["origin"], task["destination"]) not in authorized_legs):
        add_issue(issues, "task_load_or_eligibility", **where)
    carries = mode in {"loaded_move", "wait_drop", "drop"}
    expected_load = task["load_state"] if carries else "no_load"
    expected_ladle = task["ladle"] if carries else None
    if load != expected_load or segment.get("ladle_id") != expected_ladle:
        add_issue(issues, "task_load_state_or_ladle", **where)
    if mode == "loaded_move":
        start_key, end_key = "lift_s", "arrival_s"
    elif mode == "empty_move":
        start_key, end_key = "reserved_at_s", "pickup_s"
    else:
        role, start_key, end_key = HANDLING_STATES[mode]
        x_station = stations.get(task[role])
        if x_station is None or abs(segment["x0"] - x_station) > POSITION_EPS:
            add_issue(issues, "handling_station_position", **where)
    start_s, end_s = output.get(start_key), output.get(end_key)
    if (not finite(start_s) or not finite(end_s)
            or segment["t0_s"] < start_s - TIME_EPS
            or segment["t1_s"] > end_s + TIME_EPS):
        add_issue(issues, "segment_outside_task_phase", phase=mode, **where)


def audit_track(track, initial, crane, config, issues):
    horizon, profile, standings, authorized_legs, tasks, outputs = config
    cid = crane["crane_id"]
    last_end = 0.0
    last_x, last_speed = initial["position_m"], initial["speed_m_s"]
    rail_l, rail_r = profile["rail_limits_m"]
    crane_l, crane_r = crane["rail_limits_m"]
    lo, hi = max(rail_l, crane_l), min(rail_r, crane_r)
    traversed = False
    covered = True
    totals = Counter()
    for index, segment in enumerate(track):
        t0, t1, x0, x1, v0, v1, a = (segment[key] for key in NUMERIC_FIELDS)
        where = {"crane_id": cid, "time_s": t0, "segment_index": index}
        if t1 <= t0 or abs(last_end - t0) > TIME_EPS or t1 > horizon + TIME_EPS:
            add_issue(issues, "trajectory_time_coverage", prior_end_s=last_end,
                      end_s=t1, **where)
            covered = False
        x_end, v_end = state_at(segment, t1)
        if (abs(last_x - x0) > POSITION_EPS or abs(last_speed - v0) > SPEED_EPS
                or abs(x_end - x1) > POSITION_EPS or abs(v_end - v1) > SPEED_EPS):
            add_issue(issues, "trajectory_kinematics", calculated_x1_m=x_end,
                      calculated_v1_m_s=v_end, **where)
        last_end, last_x, last_speed = t1, x1, v1
        points = [x0, x_end]
        if a and 0 < -v0 / a < t1 - t0:
            turn_s = t0 - v0 / a
            points.append(state_at(segment, turn_s)[0])
            if min(abs(v0), abs(v_end)) > SPEED_EPS:
                add_issue(issues, "interior_direction_reversal", turn_s=turn_s, **where)
        if min(points) < lo - POSITION_EPS or max(points) > hi + POSITION_EPS:
            add_issue(issues, "rail_bounds", **where)

        mode, load, gear = (segment.get(k) for k in ("mode", "load_state", "gear"))
        if mode not in MOVING_STATES | HOLD_STATES:
            add_issue(issues, "unknown_motion_mode", **where)
            continue
        totals[mode] += t1 - t0
        if abs(v0) > abs(v_end) + SPEED_EPS:
            totals["braking"] += t1 - t0
        task_id = segment.get("task_id")
        task = tasks.get(task_id)
        audit_task_segment(segment, task, outputs.get(task_id),
                           standings, authorized_legs, crane, issues, where)
        moving = (abs(x_end - x0) > POSITION_EPS or
                  max(abs(v0), abs(v_end)) > SPEED_EPS or abs(a) > ACCELERATION_EPS)
        can_move = crane["dispatch_candidate"] and initial["dispatch_allowed"]
        if moving:
            traversed = True
            if not can_move:
                add_issue(issues, "parked_crane_moved", **where)
                continue
            if mode not in MOVING_STATES:
                add_issue(issues, "moving_hold", **where)
            group = profile["motion"].get(crane["motion_profile_group"], {})
            limit = group.get(load, {})
            caps = limit.get("gear_speed_caps_m_s", {})
            if gear not in caps:
                add_issue(issues, "unknown_load_or_gear", **where)
                continue
            signed_accel = a if x_end >= x0 else -a
            if (max(abs(v0), abs(v_end)) > caps[gear] + SPEED_EPS
                    or signed_accel > limit["acceleration_m_s2"] + ACCELERATION_EPS
                    or -signed_accel > limit["deceleration_m_s2"] + ACCELERATION_EPS):
                add_issue(issues, "gear_or_acceleration_limit", **where)
            audit_zone(segment, x_end, profile["speed_zones"], issues, where)
            if mode == "yield":
                if gear != "creep":
                    add_issue(issues, "yield_gear", **where)
                if abs(v_end) <= SPEED_EPS and not any(
                        abs(x_end - p) <= POSITION_EPS for p in profile["parking_positions_m"]):
                    add_issue(issues, "yield_stop_outside_parking", position_m=x_end, **where)
            if mode in {"empty_move", "loaded_move"} and task and abs(v_end) <= SPEED_EPS:
                endpoint = standings.get(task["origin" if mode == "empty_move" else "destination"])
                if endpoint is None or abs(x_end - endpoint) > POSITION_EPS:
                    add_issue(issues, "task_stop_away_from_service", position_m=x_end, **where)
        else:
            if mode not in HOLD_STATES or gear is not None:
                add_issue(issues, "hold_mode_or_gear", **where)
            if not can_move and mode != "parked":
                add_issue(issues, "non_dispatch_crane_hold_mode", **where)
            if mode == "idle" and traversed and not any(
                    abs(x0 - p) <= POSITION_EPS for p in profile["parking_positions_m"]):
                station = any(abs(x0 - p) <= POSITION_EPS for p in standings.values())
                code = "station_idle_reservation_absent" if station else "idle_outside_waypoints"
                # The archived service action ends at drop_s; no subsequent crane
                # reservation is carried by the idle record. Keep this separate
                # from a demonstrated motion or distance violation.
                add_issue(issues, code, "Major" if station else "Critical",
                          position_m=x0, end_s=t1, **where)
    if abs(last_end - horizon) > TIME_EPS or abs(last_speed) > SPEED_EPS:
        add_issue(issues, "trajectory_terminal_state", crane_id=cid, end_s=last_end,
                  horizon_s=horizon, speed_m_s=last_speed)
        covered = covered and abs(last_end - horizon) <= TIME_EPS
    return covered, totals


def audit_service_positions(tracks, tasks, outputs, standings, issues):
    for task_id, output in outputs.items():
        if output.get("drop_complete_s") is None:
            continue
        task = tasks.get(task_id)
        cid = output.get("crane_id")
        if task is None or cid not in tracks:
            add_issue(issues, "delivered_task_track_absent", crane_id=cid, task_id=task_id)
            continue
        path = tracks[cid]
        checks = (
            ("pickup_s", "origin"), ("lift_s", "origin"),
            ("arrival_s", "destination"), ("drop_s", "destination"),
        )
        for time_key, role in checks:
            time_s = output.get(time_key)
            expected = standings.get(task[role])
            segment = next((s for s in path
                            if finite(time_s) and s["t0_s"] - TIME_EPS <= time_s <= s["t1_s"] + TIME_EPS),
                           None)
            if segment is None or expected is None:
                add_issue(issues, "service_position_unverifiable", crane_id=cid,
                          task_id=task_id, phase=time_key, time_s=time_s)
                continue
            position, speed = state_at(segment, time_s)
            if abs(position - expected) > POSITION_EPS or abs(speed) > SPEED_EPS:
                add_issue(issues, "service_position_or_speed", crane_id=cid, task_id=task_id,
                          phase=time_key, time_s=time_s, position_m=position,
                          expected_position_m=expected, speed_m_s=speed)
        lift_s, drop_s = output.get("lift_s"), output.get("drop_s")
        if not finite(lift_s) or not finite(drop_s) or lift_s > drop_s + TIME_EPS:
            add_issue(issues, "invalid_load_phase", crane_id=cid, task_id=task_id)
            continue
        for s in path:
            begin = max(lift_s, s["t0_s"])
            if min(drop_s, s["t1_s"]) - begin > TIME_EPS and (
                    s.get("task_id") != task_id or s.get("ladle_id") != task["ladle"]
                    or s.get("load_state") != task["load_state"]):
                add_issue(issues, "load_phase_not_preserved", crane_id=cid, task_id=task_id,
                          time_s=begin)


def audit_motion(result, settings, case):
    """Audit one archived strategy without A's motion replay or cached metrics."""
    profile = settings.get("profile", settings)
    horizon = case["horizon_s"]
    order = profile["crane_order"]
    issue = []
    initial = {entry["crane_id"]: entry for entry in
               profile["initial_state_layouts"][profile["initial_layout"]]}
    catalog = {entry["crane_id"]: entry for entry in profile["crane_catalog"]}
    standings = profile["station_positions_m"] | profile["interface_positions_m"]
    tasks = {entry["id"]: entry for entry in case["tasks"]}
    outputs = {entry["task_id"]: entry for entry in result["tasks"]}
    authorized_legs = {tuple(leg) for leg in profile["authorized_ef_legs"]}
    config = (horizon, profile, standings, authorized_legs, tasks, outputs)
    tracks = defaultdict(list)
    for index, segment in enumerate(result["trajectories"]):
        cid = segment.get("crane_id")
        if cid not in order or any(not finite(segment.get(key)) for key in NUMERIC_FIELDS):
            add_issue(issue, "malformed_track_segment", crane_id=cid, segment_index=index)
        else:
            tracks[cid].append(segment)
    if result.get("unit") != "m" or result.get("horizon_s") != horizon:
        add_issue(issue, "track_units_or_horizon", recorded_horizon_s=result.get("horizon_s"))
    if (len(order) != 5 or len(set(order)) != 5 or set(order) != set(initial)
            or set(order) != set(catalog) or not all(i["track_present"] for i in initial.values())
            or sum(bool(c["dispatch_candidate"]) for c in catalog.values()) != 3):
        add_issue(issue, "profile_five_crane_invariant")
    thresholds = {(p["left_crane"], p["right_crane"]): p["min_distance_m"]
                  for p in profile["clearance"]["adjacent_pairs"]}
    durations, covered = Counter(), set()
    for cid in order:
        if cid not in initial or cid not in catalog:
            continue
        if not tracks[cid]:
            add_issue(issue, "missing_crane_track", crane_id=cid)
            continue
        valid, totals = audit_track(tracks[cid], initial[cid], catalog[cid], config, issue)
        durations.update(totals)
        if valid:
            covered.add(cid)
    audit_service_positions(tracks, tasks, outputs, standings, issue)

    pair_minima = []
    for left, right in pairwise(order):
        limit = thresholds.get((left, right))
        if not finite(limit) or limit <= 0:
            add_issue(issue, "missing_pair_threshold", pair=[left, right])
            continue
        if left not in covered or right not in covered:
            continue
        gap, time_s, x_left, x_right = min(clearance_overlaps(tracks[left], tracks[right]))
        margin = gap - limit
        witness = {"pair": [left, right], "time_s": time_s, "left_position_m": x_left,
                   "right_position_m": x_right, "min_gap": gap, "threshold_m": limit,
                   "min_margin": 0.0 if abs(margin) <= POSITION_EPS else margin}
        if margin < -POSITION_EPS:
            add_issue(issue, "continuous_clearance", **witness)
        pair_minima.append(witness)
    gap = min(pair_minima, key=lambda row: row["min_gap"], default=None)
    margin = min(pair_minima, key=lambda row: row["min_margin"], default=None)
    return {
        "issues": issue, "min_gap": gap and gap["min_gap"],
        "min_margin": margin and margin["min_margin"],
        "gap_witness": gap, "margin_witness": margin, "pair_minima": pair_minima,
        "full_track_coverage": (len(covered) == 5 and len(pair_minima) == 4 and
                                not any(i["code"] == "malformed_track_segment" for i in issue)),
        "segment_count": sum(map(len, tracks.values())), "mode_seconds": dict(durations),
        "tolerance": TOLERANCE,
    }
