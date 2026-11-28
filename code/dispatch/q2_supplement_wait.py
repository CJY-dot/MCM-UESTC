"""Derive mutually exclusive Q2 waits and right-censor bounds from archived events."""

from collections import defaultdict
from math import isfinite

TIME_EPS = 1e-8
STAGES = (
    "release_to_source_entry",
    "source_ready_to_pickup",
    "loaded_transport_stops",
    "arrival_to_drop_start",
    "drop_to_service_start",
)
SERVICE_START = {"refine": "refine_start", "cast": "supply_start", "slag": "slag_start"}
ORIGIN_HOLDER_ROLES = {"service_hold", "empty_ladle_position"}
STATIONARY_WAIT_MODES = {"wait_loaded", "wait_path", "wait_drop", "wait", "idle"}


def _elapsed(start, end, horizon):
    if start is None:
        return "missing", None
    limit = horizon if end is None else end
    if not (isfinite(start) and isfinite(limit) and 0 <= start <= horizon + TIME_EPS
            and start - TIME_EPS <= limit <= horizon + TIME_EPS):
        raise ValueError("Archived wait has invalid or reversed event times")
    return ("censored" if end is None else "complete"), max(0.0, limit - start)


def _held_predecessor_origin(task, predecessor, holds, events, release):
    """Verify that a nonroot ladle already occupies its modeled origin when ready."""
    if predecessor is None or release is None:
        return False
    if predecessor["ladle"] != task["ladle"] or predecessor["destination"] != task["origin"]:
        return False
    previous_events = events.get(predecessor["id"], {})
    if (predecessor.get("service") or {}).get("kind") == "cast":
        handover = previous_events.get("supply_end")
    else:
        handover = previous_events.get("drop_complete")
    return handover is not None and handover <= release + TIME_EPS and any(
        hold["role"] in ORIGIN_HOLDER_ROLES and hold["ladle_id"] == task["ladle"]
        and hold["resource_id"].rsplit("/", 1)[-1] == task["origin"]
        and hold["start_s"] <= release + TIME_EPS
        and (hold["end_s"] is None or hold["end_s"] >= release - TIME_EPS)
        for hold in holds.get(predecessor["id"], ())
    )


def _loaded_stops(start, arrival, horizon, carrying):
    """Account for each instant of in-transit ownership once, before lowering."""
    status, duration = _elapsed(start, arrival, horizon)
    if duration is None:
        return status, None, {}
    stop = horizon if arrival is None else arrival
    accounted = start
    parts = dict(moving_s=0., waiting_s=0., waiting_to_yield_s=0., other_stop_s=0.,
                 moving_yield_s=0.)
    for segment in sorted(carrying, key=lambda row: row["t0_s"]):
        begin, end = max(start, segment["t0_s"]), min(stop, segment["t1_s"])
        if end <= begin:
            continue
        if abs(begin - accounted) > TIME_EPS:
            return "missing", None, {}
        moving = any(abs(segment[k]) > TIME_EPS for k in ("v0", "v1", "a"))
        moving = moving or abs(segment["x1"] - segment["x0"]) > TIME_EPS
        mode = segment["mode"]
        if moving:
            parts["moving_yield_s" if mode == "yield" else "moving_s"] += end - begin
        elif mode == "yield":
            parts["waiting_to_yield_s"] += end - begin
        elif mode in STATIONARY_WAIT_MODES:
            parts["waiting_s"] += end - begin
        else:
            parts["other_stop_s"] += end - begin
        accounted = end
    if abs(accounted - stop) > TIME_EPS:
        return "missing", None, {}
    return status, parts["waiting_s"] + parts["waiting_to_yield_s"] + parts["other_stop_s"], parts


def waiting_rows(case, result):
    """Return private per-task records. Censored durations are lower bounds."""
    horizon = case["horizon_s"]
    if abs(horizon - result["horizon_s"]) > TIME_EPS:
        raise ValueError("Case and strategy archive horizons differ")
    events = defaultdict(dict)
    for event in result["events"]:
        task_events = events[event["task_id"]]
        phase = event["event_type"]
        if phase in task_events:
            raise ValueError("Ambiguous duplicate wait event")
        task_events[phase] = event["time_s"]
    holds, segments = defaultdict(list), defaultdict(list)
    for hold in result["occupancy"]:
        holds[hold["task_id"]].append(hold)
    for segment in result["trajectories"]:
        if segment["load_state"] != "no_load" and segment["task_id"] is not None:
            segments[segment["task_id"]].append(segment)
    tasks = {task["id"]: task for task in case["tasks"]}
    output = []
    for tid, task in tasks.items():
        service = (task.get("service") or {}).get("kind")
        common = {"task_id": tid, "process": service or "unbound"}
        if task["required_in_ef_scope"] is not True:
            output.extend(common | {"stage": stage, "status": "not_applicable", "duration_s": None,
                                    "start_basis": None} for stage in STAGES)
            continue

        times = events.get(tid, {})
        ready, entry, pickup = (times.get(k) for k in ("physical_release", "source_entry", "pickup_start"))
        lift, arrival, lower, drop = (
            times.get(k) for k in ("pickup_complete", "delivery_arrival", "drop_start", "drop_complete")
        )
        pred = task.get("predecessor")
        if pred is None:
            admit_status, admit_duration = _elapsed(ready, entry, horizon)
            ready_and_held = max(ready, entry) if ready is not None and entry is not None else None
            basis = "condition_release_and_model_source_entry"
        else:
            admit_status, admit_duration = "not_applicable", None
            ready_and_held = ready if _held_predecessor_origin(
                task, tasks.get(pred), holds, events, ready
            ) else None
            basis = "condition_release_with_same_ladle_predecessor_origin_hold"
        lift_status, lift_duration = _elapsed(ready_and_held, pickup, horizon)
        transit_status, transit_duration, transit_parts = _loaded_stops(lift, arrival, horizon, segments[tid])
        lower_status, lower_duration = _elapsed(arrival, lower, horizon)
        if service in SERVICE_START:
            start_status, start_duration = _elapsed(drop, times.get(SERVICE_START[service]), horizon)
        else:
            start_status, start_duration = "missing", None
        pieces = (
            (admit_status, admit_duration, "condition_release"),
            (lift_status, lift_duration, basis),
            (transit_status, transit_duration, "pickup_complete"),
            (lower_status, lower_duration, "delivery_arrival"),
            (start_status, start_duration, "drop_complete"),
        )
        for stage, (status, duration, start_basis) in zip(STAGES, pieces, strict=True):
            row = common | {"stage": stage, "status": status, "duration_s": duration,
                            "start_basis": start_basis if status != "not_applicable" else None}
            if stage == "loaded_transport_stops" and transit_parts:
                row.update(transit_parts)
            output.append(row)
    return output
