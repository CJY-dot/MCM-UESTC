"""Reconstruct a disjoint vehicle-time ledger from a frozen Q2 trajectory."""

from bisect import bisect_right
from collections import defaultdict
from math import isfinite


TIME_TOL = 1e-7
POSITION_TOL = 1e-7
MOTION_TOL = 1e-8
MOVEMENT_FIELDS = ("empty_pickup_move_s", "yield_move_s", "post_drop_parking_move_s", "loaded_move_s")
ADDITIVE_FIELDS = (
    *MOVEMENT_FIELDS, "empty_wait_s", "loaded_wait_s", "handling_s", "initial_idle_s",
    "legal_parking_hold_s", "non_dispatch_parked_s", "unclassified_s", "uncovered_s",
)


def is_stationary(segment):
    return (
        abs(segment["x1"] - segment["x0"]) <= POSITION_TOL
        and max(abs(segment[k]) for k in ("v0", "v1", "a")) <= MOTION_TOL
    )


def is_registered(position, parking):
    return any(abs(position - allowed) <= POSITION_TOL for allowed in parking)


def parking_windows(result, order, parking, horizon):
    """The archived task gives the boundaries of the post-drop movement."""
    windows = {crane: [] for crane in order}
    for row in result["tasks"]:
        start = row.get("drop_complete_s")
        if start is None:
            continue
        crane = row["crane_id"]
        end, position = row["parking_end_s"], row["parking_position_m"]
        if not 0 <= start <= end <= horizon or not is_registered(position, parking):
            raise ValueError(f"Invalid post-drop parking window for {crane}")
        if end > start:
            windows[crane].append((start, end, position))
    for crane, spans in windows.items():
        spans.sort()
        if any(nxt[0] < prior[1] - TIME_TOL for prior, nxt in zip(spans, spans[1:])):
            raise ValueError(f"Overlapping post-drop parking windows for {crane}")
    return windows


def classify_segment(segment, *, moved, dispatch, has_parking_window, parking):
    stationary = is_stationary(segment)
    mode, load = segment["mode"], segment["load_state"]
    unloaded = load == "no_load"
    if not stationary:
        if dispatch:
            if unloaded and mode == "yield":
                return "post_drop_parking_move_s" if has_parking_window else "yield_move_s"
            if unloaded and mode == "empty_move":
                return "empty_pickup_move_s"
            if load in {"loaded", "empty_ladle"} and mode == "loaded_move":
                return "loaded_move_s"
    elif mode == "wait_empty" and unloaded and dispatch:
        return "empty_wait_s"
    elif mode == "wait_drop" and load in {"loaded", "empty_ladle"} and dispatch:
        return "loaded_wait_s"
    elif mode in {"pickup", "drop"} and dispatch:
        return "handling_s"
    elif mode == "idle" and unloaded and dispatch:
        if not moved:
            return "initial_idle_s"
        if is_registered(segment["x0"], parking):
            return "legal_parking_hold_s"
    elif mode == "parked" and unloaded and not dispatch:
        return "non_dispatch_parked_s"
    return "unclassified_s"


def track_ledger(crane, track, spans, parking, horizon, dispatch):
    tally = dict.fromkeys(ADDITIVE_FIELDS, 0.0)
    starts = [span[0] for span in spans]
    parking_seen = defaultdict(float)
    moved = False
    previous_end = 0.0
    braking = 0.0
    for segment in track:
        start, end = segment["t0_s"], segment["t1_s"]
        if not (isfinite(start) and isfinite(end) and 0 <= start < end <= horizon + TIME_TOL):
            raise ValueError(f"An invalid trajectory time interval on {crane}")
        gap = start - previous_end
        if gap < -TIME_TOL:
            raise ValueError(f"Overlapping or out-of-order trajectory on {crane}")
        if gap > TIME_TOL:
            tally["uncovered_s"] += gap
        index = bisect_right(starts, start + TIME_TOL) - 1
        in_parking = index >= 0 and end <= spans[index][1] + TIME_TOL
        field = classify_segment(segment, moved=moved, dispatch=dispatch,
                                 has_parking_window=in_parking, parking=parking)
        elapsed = end - start
        tally[field] += elapsed
        if field == "post_drop_parking_move_s":
            parking_seen[index] += elapsed
            _, stop, position = spans[index]
            if abs(stop - end) <= TIME_TOL and abs(position - segment["x1"]) > POSITION_TOL:
                raise ValueError(f"Post-drop parking endpoint differs on {crane}")
        if field in MOVEMENT_FIELDS and abs(segment["v0"]) > abs(segment["v1"]) + MOTION_TOL:
            braking += elapsed
        moved = moved or not is_stationary(segment)
        previous_end = end
    if horizon - previous_end > TIME_TOL:
        tally["uncovered_s"] += horizon - previous_end
    for index, (start, end, _) in enumerate(spans):
        if abs(parking_seen[index] - (end - start)) > TIME_TOL:
            raise ValueError(f"Post-drop parking time is not traceable on {crane}")
    covered = sum(tally[field] for field in ADDITIVE_FIELDS if field != "uncovered_s")
    return dict(
        tally,
        empty_move_s=sum(tally[field] for field in MOVEMENT_FIELDS if field != "loaded_move_s"),
        standby_s=tally["initial_idle_s"] + tally["legal_parking_hold_s"],
        braking_s=braking,
        covered_s=covered,
        horizon_vehicle_s=horizon,
        closure_error_s=abs(covered + tally["uncovered_s"] - horizon),
    )


def vehicle_metrics(case, settings, result):
    """Return seconds per crane and vehicle-seconds per declared group and system.

    Only ADDITIVE_FIELDS sum to the horizon. Movement totals, standby and
    braking are separately reported and must not be added a second time.
    """
    profile = settings.get("profile", settings)
    horizon = case["horizon_s"]
    order = profile["crane_order"]
    layout = profile["initial_state_layouts"][profile["initial_layout"]]
    identifiable = {crane["crane_id"]: crane for crane in layout}
    catalog = {crane["crane_id"]: crane for crane in profile["crane_catalog"]}
    if not isfinite(horizon) or horizon <= 0 or result["horizon_s"] != horizon or result["unit"] != "m":
        raise ValueError("Unexpected Q2 horizon or coordinate units")
    if len(order) != 5 or len(set(order)) != 5 or set(order) != set(identifiable) or set(order) != set(catalog):
        raise ValueError("Vehicle ledger requires the frozen five-crane roster")
    dispatch = [crane for crane in order if identifiable[crane]["dispatch_allowed"] and
                catalog[crane]["dispatch_candidate"] and catalog[crane]["capacity_t"] == 450]
    parked = [crane for crane in order if not identifiable[crane]["dispatch_allowed"] and
              not catalog[crane]["dispatch_candidate"] and catalog[crane]["capacity_t"] == 150]
    if len(dispatch) != 3 or len(parked) != 2 or not all(crane["track_present"] for crane in layout):
        raise ValueError("Frozen dispatch and fixed parking groups do not cover the five cranes")
    tracks = {crane: [] for crane in order}
    for segment in result["trajectories"]:
        tracks[segment["crane_id"]].append(segment)
    parking = profile["parking_positions_m"]
    spans = parking_windows(result, order, parking, horizon)
    ledgers = {crane: track_ledger(crane, tracks[crane], spans[crane], parking, horizon, crane in dispatch)
               for crane in order}
    for label, members in (("dispatch_450t", dispatch), ("parked_150t", parked), ("system", order)):
        ledgers[label] = {field: sum(ledgers[crane][field] for crane in members)
                          for field in ledgers[members[0]]}
    return ledgers
