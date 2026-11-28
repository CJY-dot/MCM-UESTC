"""Independent EF longitudinal trajectory checks for frozen Q1 witnesses."""

from bisect import bisect_right
from collections import Counter, defaultdict
from math import isclose, isfinite


TOL = 1e-8
MOVING = {"empty_move", "loaded_move", "yield"}
STATIONARY = {"idle", "wait_empty", "wait_loaded", "wait_drop", "pickup", "drop"}
ONBOARD = {"loaded_move", "wait_loaded", "wait_drop", "drop"}


def _close(a, b):
    return isclose(a, b, rel_tol=0, abs_tol=TOL)


def _position(segment, at):
    t0, t1 = segment["t0_s"], segment["t1_s"]
    return segment["x0"] + (at - t0) * (segment["x1"] - segment["x0"]) / (t1 - t0)


def _state(track, starts, at):
    return track[max(0, bisect_right(starts, at) - 1)]


def _ordered(values):
    return (
        all(isinstance(value, (int, float)) and isfinite(value) for value in values)
        and all(a <= b + TOL for a, b in zip(values, values[1:]))
    )


def audit_tracks(case, result):
    """Recompute motion, ownership on cranes, and fixed-order continuous gap."""
    errors = []
    cranes = {crane["id"]: crane for crane in case["cranes"]}
    horizon = case["horizon_s"]
    rail_left, rail_right = case["rail"]
    threshold = case["params"]["gap"]
    limit = case["params"]["speed"]
    if result.get("horizon_s") != horizon or result.get("gap_threshold") != threshold:
        errors.append({"rule": "reported_model_bounds"})
    if len(cranes) != 5 or len(case["cranes"]) != 5:
        errors.append({"rule": "five_distinct_cranes"})

    by_vehicle = defaultdict(list)
    for segment in result["trajectories"]:
        cid = segment["crane_id"]
        if cid not in cranes:
            errors.append({"rule": "unknown_crane", "crane": cid})
            continue
        data = [segment.get(k) for k in ("t0_s", "t1_s", "x0", "x1", "v0", "v1", "a")]
        if not all(isinstance(v, (float, int)) and isfinite(v) for v in data) or data[0] >= data[1]:
            errors.append({"rule": "invalid_segment", "crane": cid, "segment": data[:2]})
            continue
        by_vehicle[cid].append(segment)
    tracks = {cid: sorted(by_vehicle[cid], key=lambda s: s["t0_s"]) for cid in cranes}
    starts = {cid: [s["t0_s"] for s in track] for cid, track in tracks.items()}
    gaps_ready = True
    for cid, track in tracks.items():
        last_time, last_x = 0, cranes[cid]["x"]
        for segment in track:
            t0, t1, x0, x1 = (segment[k] for k in ("t0_s", "t1_s", "x0", "x1"))
            velocity = (x1 - x0) / (t1 - t0)
            ref = {"crane": cid, "interval_s": [t0, t1], "task": segment.get("task_id")}
            if not _close(t0, last_time) or not _close(x0, last_x):
                gaps_ready = False
                errors.append({"rule": "track_time_or_position_discontinuity", **ref})
            if not all(rail_left - TOL <= x <= rail_right + TOL for x in (x0, x1)):
                errors.append({"rule": "rail_limit", "positions": [x0, x1], **ref})
            if (abs(velocity) > limit + TOL or not _close(segment["v0"], velocity)
                    or not _close(segment["v1"], velocity) or not _close(segment["a"], 0)):
                errors.append({"rule": "q1_motion_or_speed_limit", "slope": velocity, **ref})
            mode = segment.get("mode")
            if (mode not in MOVING | STATIONARY
                    or (mode in STATIONARY and not _close(x0, x1))
                    or (mode in MOVING and _close(x0, x1))):
                errors.append({"rule": "motion_mode", "mode": mode, **ref})
            if not cranes[cid]["dispatch"] and (mode != "idle" or not _close(x1, cranes[cid]["x"])):
                errors.append({"rule": "immobilized_crane", **ref})
            if mode in {"yield", "wait_loaded"} and not any(_close(x1, p) for p in case["parking"]):
                errors.append({"rule": "unauthorized_parking", "position": x1, **ref})
            if (mode in ONBOARD) != (segment.get("ladle_id") is not None):
                errors.append({"rule": "motion_mode_ladle", **ref})
            if mode == "idle" and segment.get("task_id") is not None:
                errors.append({"rule": "idle_has_task", **ref})
            last_time, last_x = t1, x1
        if not track or not _close(last_time, horizon):
            errors.append({"rule": "incomplete_horizon", "crane": cid, "last_s": last_time})
            gaps_ready = False

    minimum = None
    intervals = 0
    if gaps_ready and len(cranes) >= 2:
        order = list(cranes)
        for west, east in zip(order, order[1:]):
            knots = sorted(set(starts[west]) | set(starts[east]) | {horizon})
            # This is the fixed order in the frozen input. Never reorder by output position.
            for t0, t1 in zip(knots, knots[1:]):
                if t1 <= t0:
                    continue
                middle = (t0 + t1) / 2
                a = _state(tracks[west], starts[west], middle)
                b = _state(tracks[east], starts[east], middle)
                endpoints = [(at, _position(a, at), _position(b, at)) for at in (t0, t1)]
                at, x_west, x_east = min(endpoints, key=lambda item: (item[2] - item[1], item[0]))
                gap = x_east - x_west
                witness = {
                    "gap": gap, "time_s": at, "pair": [west, east],
                    "interval_s": [t0, t1], "positions": [x_west, x_east],
                    "modes": [a["mode"], b["mode"]],
                }
                intervals += 1
                if minimum is None or (gap, at) < (minimum["gap"], minimum["time_s"]):
                    minimum = witness
                if gap < threshold - TOL:
                    errors.append({"rule": "continuous_gap", "threshold": threshold, **witness})

        for cid, fixed in case.get("locked_trajectories", {}).items():
            if cid not in tracks:
                errors.append({"rule": "unknown_locked_crane", "crane": cid})
                continue
            for segment in fixed:
                t0, t1 = segment["t0_s"], segment["t1_s"]
                if t0 < 0 or t1 > horizon or t1 <= t0:
                    errors.append({"rule": "invalid_input_lock", "crane": cid, "interval_s": [t0, t1]})
                    continue
                for actual in tracks[cid]:
                    lo, hi = max(t0, actual["t0_s"]), min(t1, actual["t1_s"])
                    if lo >= hi:
                        continue
                    same_state = all(actual.get(key) == segment.get(key)
                                     for key in ("mode", "task_id", "ladle_id"))
                    if not same_state or not all(_close(_position(segment, t), _position(actual, t))
                                                  for t in (lo, hi)):
                        errors.append({"rule": "changed_locked_motion", "crane": cid, "interval_s": [lo, hi]})

        errors.extend(_audit_task_tracks(case, result, tracks, starts))
    return {
        "errors": errors, "minimum_gap": minimum["gap"] if minimum else None,
        "threshold": threshold,
        "margin": minimum["gap"] - threshold if minimum else None,
        "minimum_witness": minimum,
        "adjacent_affine_intervals": intervals,
        "cranes_with_paths": len(by_vehicle),
        "modes": dict(Counter(s.get("mode", "unknown") for track in tracks.values() for s in track)),
        "absolute_tolerance": TOL,
    }


def _audit_task_tracks(case, result, tracks, starts):
    errors = []
    tasks = {task["id"]: task for task in case["tasks"]}
    assignments = {row["task_id"]: row for row in result["tasks"] if row["status"] == "completed"}
    points = case["positions"]
    operations = ["first_action_s", "empty_arrival_s", "pickup_start_s", "pickup_complete_s",
                  "delivery_arrival_s", "drop_start_s", "drop_complete_s"]
    horizon = case["horizon_s"]
    onboard_by_ladle = defaultdict(list)
    for tid, row in assignments.items():
        cid = row.get("crane_id")
        task = tasks.get(tid)
        stamps = [row.get(key) for key in operations]
        if (task is None or cid not in tracks or not _ordered([0, *stamps, horizon])
                or task["origin"] not in points or task["destination"] not in points):
            errors.append({"rule": "invalid_motion_task_assignment", "task": tid, "crane": cid})
            continue
        _, got_empty, pick, lifted, arrive, unload, dropped = stamps
        if dropped - lifted > TOL:
            onboard_by_ladle[task["ladle"]].append((lifted, dropped, tid, cid))
        for station, times in ((task["origin"], (got_empty, pick, lifted)),
                               (task["destination"], (arrive, unload, dropped))):
            for at in times:
                x = _position(_state(tracks[cid], starts[cid], at), at)
                if not _close(x, points[station]):
                    errors.append({"rule": "task_event_position", "task": tid, "crane": cid,
                                   "station": station, "time_s": at, "actual_x": x})
        stage_windows = [
            (pick, lifted, {"pickup"}),
            (lifted, arrive, {"loaded_move", "wait_loaded"}),
            (arrive, unload, {"wait_drop"}),
            (unload, dropped, {"drop"}),
        ]
        for begin, end, allowed in stage_windows:
            for segment in tracks[cid]:
                t0, t1 = max(begin, segment["t0_s"]), min(end, segment["t1_s"])
                if t1 - t0 <= TOL:
                    continue
                if (segment["mode"] not in allowed or segment.get("task_id") != tid
                        or (segment["mode"] in ONBOARD and segment.get("ladle_id") != task["ladle"])):
                    errors.append({"rule": "missing_or_wrong_task_phase", "task": tid, "crane": cid,
                                   "interval_s": [t0, t1], "mode": segment["mode"]})
    for cid, track in tracks.items():
        for segment in track:
            mode = segment.get("mode")
            tid = segment.get("task_id")
            if mode == "idle":
                continue
            row = assignments.get(tid)
            if row is None or tid not in tasks or (row["crane_id"] != cid and mode != "yield"):
                errors.append({"rule": "motion_without_matching_assignment", "crane": cid, "task": tid,
                               "time_s": segment["t0_s"], "mode": mode})
                continue
            t0, t1, x0, x1 = (segment[k] for k in ("t0_s", "t1_s", "x0", "x1"))
            task = tasks[tid]
            if not _ordered([row.get(k) for k in operations]):
                continue
            if mode == "yield" and (cid not in row["yield_cranes"]
                                    or t0 < row["first_action_s"] - TOL or t1 > row["pickup_start_s"] + TOL):
                errors.append({"rule": "unreserved_yield", "crane": cid, "task": tid, "interval_s": [t0, t1]})
            if mode == "wait_empty" and (t0 < row["first_action_s"] - TOL or t1 > row["pickup_start_s"] + TOL):
                errors.append({"rule": "empty_wait_outside_approach", "crane": cid,
                               "task": tid, "interval_s": [t0, t1]})
            if mode == "empty_move" and not (
                row["first_action_s"] - TOL <= t0 and t1 <= row["empty_arrival_s"] + TOL
                or row["drop_complete_s"] - TOL <= t0
            ):
                errors.append({"rule": "empty_move_during_transport", "crane": cid,
                               "task": tid, "interval_s": [t0, t1]})
            if mode in ONBOARD and task["origin"] in points and task["destination"] in points:
                origin, destination = points[task["origin"]], points[task["destination"]]
                lo, hi = sorted((origin, destination))
                if (t0 < row["pickup_complete_s"] - TOL or t1 > row["drop_complete_s"] + TOL
                        or segment["ladle_id"] != task["ladle"]
                        or not all(lo - TOL <= x <= hi + TOL for x in (x0, x1))
                        or (destination > origin and x1 < x0 - TOL)
                        or (destination < origin and x1 > x0 + TOL)):
                    errors.append({"rule": "wrong_loaded_crane_or_route", "crane": cid, "task": tid,
                                   "interval_s": [t0, t1]})
    for ladle, intervals in onboard_by_ladle.items():
        intervals.sort()
        for previous, following in zip(intervals, intervals[1:]):
            if following[0] < previous[1] - TOL:
                errors.append({"rule": "overlapping_onboard_ladle", "ladle": ladle,
                               "tasks": [previous[2], following[2]], "time_s": following[0]})
    return errors


def audit_counterexample_41():
    """Compute C's original blocking example and its two distinct controls."""
    start_positions = (5, 20, 50, 80, 95)
    destination, gap_required, cutoff = 60, 8, 15
    speed, acceleration = 4, 2
    ceiling = start_positions[2] - gap_required
    control_initial = (5, 20, 70, 80, 95)
    control_final = (5, destination, 70, 80, 95)
    distance = destination - control_initial[1]
    # C2 moves monotonically, so the minima for adjacent pairs are at endpoints.
    minimum = min(b - a for row in (control_initial, control_final) for a, b in zip(row, row[1:]))
    ramp_total_distance = speed**2 / acceleration
    trapezoid = 2 * speed / acceleration + (distance - ramp_total_distance) / speed
    return {
        "negative": {"latest_s": cutoff, "required_gap_m": gap_required,
                     "furthest_m": ceiling, "destination_m": destination, "reachable": destination <= ceiling},
        "uniform_control": {"arrival_s": distance / speed, "minimum_gap_m": minimum},
        "acceleration_control": {"arrival_s": trapezoid, "minimum_gap_m": minimum,
                                 "scope": "analytic_original_comparison_only"},
    }
