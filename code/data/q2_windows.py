"""Audit frozen Q1 windows as possible Q2 competition cases, without replaying them."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path


BAD_FLAGS = {
    "ambiguous_or_conflicting_source", "conflicting_event_time",
    "observed_lift_before_process_end", "observed_arrival_before_lift",
}
PROCESS_EVENT_TYPES = {
    "BOF": {"tap_end"}, "LF": {"lf_exit", "lf_process_end"},
    "RH": {"rh_exit", "rh_master_process_end"}, "CC": {"cast_end"},
}
LIFT_EVENT_TYPES = {
    "BOF": {"bof_lift"}, "LF": {"lf_lift"},
    "RH": {"rh_lift"}, "CC": {"cc_empty_lift"},
}
SOURCE_FIELDS = ("source_file", "source_sheet", "source_row", "source_column")


def _read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _number(value):
    if value is None or value == "":
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _private_folder(path):
    root = Path(__file__).resolve().parents[2]
    path = Path(path).resolve()
    artifact_roots = (root / "local", root / "problem/data/processed")
    if not any(path.is_relative_to(folder) for folder in artifact_roots):
        raise ValueError("Q2 window details require an ignored local or processed directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _supporting_event(events, row, field, timestamp, allowed):
    ref = row[field]
    matches = events.get((row["heat_id"], ref), ())
    return any(e["event"] in allowed and _number(e["time_s"]) == timestamp for e in matches)


def _readiness_proxy(task, row, tasks, raw, events):
    """Return evidence-bound proxy endpoints; they do not establish physical readiness."""
    if task["scope"] != "ef_crane" or task["scope_status"] != "confirmed":
        return None, "ef_scope_not_confirmed"
    if task["admission"] == "C" or task["identity_status"] != "unique" or not task["ladle_id"]:
        return None, "identity_or_admission_pending"
    if BAD_FLAGS.intersection(row["flags"].split(";")):
        return None, "ambiguous_or_conflicting_times"
    prior, lift = _number(row["process_predecessor_s"]), _number(row["observed_lift_s"])
    if prior is None or lift is None or lift <= prior:
        return None, "no_positive_predecessor_to_lift_interval"
    origin = task["origin"].split("_", 1)[0] if task["origin"].startswith("BOF_") else task["origin"][:2]
    if not _supporting_event(events, row, "source_process_predecessor", prior,
                             PROCESS_EVENT_TYPES.get(origin, ())):
        return None, "process_predecessor_source_not_verified"
    if not _supporting_event(events, row, "source_lift", lift, LIFT_EVENT_TYPES.get(origin, ())):
        return None, "observed_lift_source_not_verified"
    if pred_id := row["predecessor_task_id"]:
        predecessor, prior_row = tasks.get(pred_id), raw.get(pred_id)
        if (not predecessor or not prior_row or predecessor["heat_id"] != task["heat_id"]
                or predecessor["ladle_id"] != task["ladle_id"]
                or predecessor["destination"] != task["origin"]
                or BAD_FLAGS.intersection(prior_row["flags"].split(";"))):
            return None, "same_heat_holder_path_not_verified"
        previous_arrival = _number(prior_row["observed_arrival_s"])
        if previous_arrival is None or previous_arrival > prior:
            return None, "previous_leg_arrival_not_supported_before_predecessor"
    return (prior, lift), None


def _conditional_cranes(task, assumptions):
    route = (task["origin"], task["destination"])
    if route not in {tuple(leg) for leg in assumptions["authorized_ef_legs"]}:
        return set()
    mass = _number(task["mass_hi_t"])
    upper = assumptions["assumed_mass_upper_t"].get(task["load_state"])
    if mass is None or upper is None or not 0 < mass <= upper:
        return set()
    eligible = task["eligible_cranes"]
    dispatch = set(assumptions["dispatch"])
    if eligible not in ("", "null"):
        # Q1 records are null/currently unknown or explicitly serialized arrays.
        dispatch &= set(json.loads(eligible))
    return {crane for crane in dispatch if int(crane.split("#", 1)[1]) >= upper}


def _route_geometry(a, b, assumptions):
    """Check direct simultaneous travel only; full crane states remain unknown."""
    points = assumptions["stations"] | assumptions["interface_points"]
    coords = [(points[t["origin"]], points[t["destination"]]) for t in (a, b)]
    (xa, ya), (xb, yb) = coords
    assumptions_base = assumptions["scenarios"]["base"]
    factor = assumptions_base["distance_factor"]
    threshold = assumptions_base["gap"]
    if xa > xb:
        xa, xb, ya, yb = xb, xa, yb, ya
    xa, xb, ya, yb = (v * factor for v in (xa, xb, ya, yb))
    distance_a, distance_b = abs(ya - xa), abs(yb - xb)
    direction_a = 0 if not distance_a else math.copysign(1, ya - xa)
    direction_b = 0 if not distance_b else math.copysign(1, yb - xb)
    # With equal speed and both starting together, either crossing or a gap below the
    # threshold must be visible at an endpoint of this common moving interval.
    common_distance = min(distance_a, distance_b)
    end_gap = xb - xa + (direction_b - direction_a) * common_distance
    simultaneous_direct_conflict = min(xb - xa, end_gap) < threshold
    first, second = sorted((tuple(sorted((xa, ya))), tuple(sorted((xb, yb)))))
    footprint_intersects = first[1] >= second[0]
    lower, upper = (point * factor for point in assumptions["rail"])
    ordered_cranes = assumptions["crane_order"]
    layout = assumptions["layouts"]["dispersed"]
    if ordered_cranes[0] not in assumptions["dispatch"]:
        lower = max(lower, layout[0] * factor + threshold)
    if ordered_cranes[-1] not in assumptions["dispatch"]:
        upper = min(upper, layout[-1] * factor - threshold)
    by_stage = {"BOF": "bof_receipt", "LF": "refine_position",
                "RH": "refine_position", "CC": "caster_seat", "SLAG": "slag_position"}
    shared_nodes = set((a["origin"], a["destination"])) & set((b["origin"], b["destination"]))
    exclusive = sorted(
        node for node in shared_nodes
        if any(node.startswith(prefix) and assumptions["capacity"].get(resource) == 1
               for prefix, resource in by_stage.items())
    )
    return {
        "shared_conditional_station": exclusive,
        "overlapping_rail_footprints": footprint_intersects,
        "simultaneous_direct_travel_gap_conflict": simultaneous_direct_conflict,
        "inside_rail_and_parked_end_crane_bounds": lower <= min(xa, xb, ya, yb)
        and max(xa, xb, ya, yb) <= upper,
    }


def _window_review(window, tasks, raw, events, q1_assumptions):
    roster = [tasks[key] for key in window["task_ids"]]
    support = {}
    rejected = Counter()
    prep_by_case = {key: cfg["prep_s"] for key, cfg in q1_assumptions["scenarios"].items()}
    for task in roster:
        key = task["task_id"]
        row = raw[key]
        if task["heat_id"] != row["heat_id"]:
            raise ValueError("Q1 and corrected historical roster disagree on heat identity")
        proxy, reason = _readiness_proxy(task, row, tasks, raw, events)
        if reason:
            rejected[reason] += 1
        else:
            support[key] = proxy

    configs = dict.fromkeys(prep_by_case, 0)
    base_pairs = []
    for a, b in combinations(roster, 2):
        keys = (a["task_id"], b["task_id"])
        if (a["heat_id"] == b["heat_id"] or a["ladle_id"] == b["ladle_id"]
                or not all(key in support for key in keys)):
            continue
        prior_a, lift_a = support[keys[0]]
        prior_b, lift_b = support[keys[1]]
        end = min(window["end_s"], lift_a, lift_b)
        for label, prep in prep_by_case.items():
            if max(window["start_s"], prior_a + prep, prior_b + prep) < end:
                configs[label] += 1
        start = max(window["start_s"], prior_a + prep_by_case["base"], prior_b + prep_by_case["base"])
        if start >= end:
            continue
        cranes_a = _conditional_cranes(a, q1_assumptions)
        cranes_b = _conditional_cranes(b, q1_assumptions)
        shared_cranes = sorted(cranes_a & cranes_b)
        geometry = _route_geometry(a, b, q1_assumptions) if shared_cranes else None
        observed_same_crane = bool(a["observed_crane"] and a["observed_crane"] == b["observed_crane"]
                                   and a["crane_evidence_status"] == b["crane_evidence_status"] == "confirmed")
        base_pairs.append({
            "task_ids": keys, "proxy_ready_overlap_s": [start, end],
            "conditional_crane_sets": [sorted(cranes_a), sorted(cranes_b)],
            "shared_conditional_cranes": shared_cranes, "known_geometry": geometry,
            "observed_same_crane": observed_same_crane,
        })
    comparable = [pair for pair in base_pairs if pair["known_geometry"]]
    known_resources = [pair for pair in comparable if pair["known_geometry"]["shared_conditional_station"]]
    historical_overlap = [pair for pair in comparable if pair["observed_same_crane"]]
    public = {
        "window_id": window["window_id"], "freeze_role": window["evidence_level"],
        "heat_count": len(window["heats"]), "candidate_task_count": len(roster),
        "source_checked_proxy_tasks": len(support), "proxy_task_exclusions": dict(rejected),
        "exact_physical_release_fields": sum(_number(task["physical_release_s"]) is not None for task in roster),
        "verified_full_qualification_fields": sum(
            bool(task["eligible_cranes"]) and task["eligible_cranes"] != "null" for task in roster
        ),
        "positive_proxy_ready_pair_counts": configs,
        "common_conditionally_qualified_crane_pairs_at_base": len(comparable),
        "shared_conditional_endpoint_resource_pairs_at_base": len(known_resources),
        "overlapping_conditional_rail_footprint_pairs_at_base": sum(
            pair["known_geometry"]["overlapping_rail_footprints"] for pair in comparable
        ),
        "simultaneous_direct_template_gap_conflict_pairs_at_base": sum(
            pair["known_geometry"]["simultaneous_direct_travel_gap_conflict"] for pair in comparable
        ),
        "same_historical_crane_assignment_pairs_at_base": sum(pair["observed_same_crane"] for pair in base_pairs),
        "same_historical_crane_with_conditional_route_pairs_at_base": len(historical_overlap),
        "same_historical_crane_and_shared_conditional_endpoint_pairs_at_base": sum(
            pair["observed_same_crane"] for pair in known_resources
        ),
        "conditional_pairs_inside_rail_and_parked_end_crane_bounds_at_base": sum(
            pair["known_geometry"]["inside_rail_and_parked_end_crane_bounds"] for pair in comparable
        ),
        "status": "conditional_screen_only" if comparable else "insufficient_conditional_route_evidence",
    }
    return public, {"window_id": window["window_id"], "window": window,
                    "proxy_task_intervals_s": support, "base_pair_evidence": base_pairs}


def window_manifest(processed: Path, window_index: Path, q1_assumptions: dict,
                    private_out: Path) -> dict:
    """Review all frozen candidates. No dates, source IDs or per-task clocks are exported."""
    window_index = Path(window_index)
    window_dir = window_index.parent
    paths = {
        "approved_stage02_tasks": Path(processed) / "tasks.csv",
        "approved_stage02_events": Path(processed) / "events.csv",
        "q1_window_index": window_index,
        "q1_full_task_index": window_dir / "tasks.csv",
        "q1_full_event_index": window_dir / "events.csv",
    }
    windows = json.loads(window_index.read_text(encoding="utf-8-sig"))
    raw = {task["task_id"]: task for task in _read_csv(paths["approved_stage02_tasks"])}
    tasks = {task["task_id"]: task for task in _read_csv(paths["q1_full_task_index"])}
    events = defaultdict(list)
    for row in _read_csv(paths["approved_stage02_events"]):
        if (row["record_type"] == "actual" and row["event_status"] in {"canonical", "agrees"}
                and row["link_status"] in {"unique", "resolved_by_identity"}):
            ref = "|".join(row[key] for key in SOURCE_FIELDS)
            events[(row["heat_id"], ref)].append(row)
    actual = [e for e in _read_csv(paths["q1_full_event_index"]) if e["event_category"] == "actual"]
    announced = sum(_number(e["visible_at_s"]) is not None for e in actual)
    reviews, private = [], []
    for window in windows:
        public, debug = _window_review(window, tasks, raw, events, q1_assumptions)
        reviews.append(public)
        private.append(debug)

    pairs = []
    possible = []
    by_id = {row["window_id"]: row for row in reviews}
    for a, b in combinations(windows, 2):
        left, right = a["window_id"], b["window_id"]
        shared = len(set(a["heats"]) & set(b["heats"]))
        campaigns = len(set(a["campaigns"]) & set(b["campaigns"]))
        time_overlap = max(a["start_s"], b["start_s"]) < min(a["end_s"], b["end_s"])
        comparable_counts = [by_id[key]["common_conditionally_qualified_crane_pairs_at_base"]
                             for key in (left, right)]
        if not (shared or campaigns or time_overlap) and all(comparable_counts):
            possible.append({
                "window_ids": [left, right],
                "conditional_comparison_pair_count": sum(comparable_counts),
                "both_have_historical_same_crane_and_shared_endpoint_pair": all(
                    by_id[key]["same_historical_crane_and_shared_conditional_endpoint_pairs_at_base"]
                    for key in (left, right)
                ),
            })
        pairs.append({"window_ids": [left, right], "shared_heats": shared,
                      "shared_campaigns": campaigns, "overlapping_time_ranges": time_overlap})
    possible.sort(key=lambda item: (-item["both_have_historical_same_crane_and_shared_endpoint_pair"],
                                    -item["conditional_comparison_pair_count"], item["window_ids"]))
    detail_dir = _private_folder(private_out)
    detail_path = detail_dir / "q2_window_detail.json"
    detail_path.write_text(json.dumps(private, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    return {
        "schema": "mcm-uestc.q2-window-manifest/1",
        "publication_class": "public_summary_only",
        "source_status": "derived",
        "source_sha256": {key: hashlib.sha256(path.read_bytes()).hexdigest()
                          for key, path in paths.items()},
        "historical_event_visibility": {"q1_full_actual_events": len(actual), "with_visible_at": announced},
        "screen": {
            "proxy_ready_interval": "[canonical actual process completion + assumed preparation, canonical actual lift)",
            "preparation_seconds": {key: val["prep_s"] for key, val in q1_assumptions["scenarios"].items()},
            "proxy_end_use": "offline diagnostic only; never a release time or a policy feature",
            "shared_crane_use": "assumed mass upper bound, dispatch class and registered conditional route only",
            "geometry_use": "Q1 normalized topology; footprint intersection is no clearance certificate",
            "strict_competition_requires": ["physical availability for distinct ladles at a common time",
                                            "audited crane qualification and occupancy",
                                            "resource or trajectory constraints limiting simultaneous service",
                                            "time-stamped plan and announcement evidence"],
            "disjoint_screen": "no shared heats, no shared campaign identifiers, no overlap in window times",
        },
        "window_reviews": reviews,
        "window_pair_overlap_review": pairs,
        "conditional_disjoint_pair_candidates": possible,
        "conditional_review_pair": possible[0]["window_ids"] if possible else [],
        "verified_independent_competition_windows": [],
        "strict_field_evidence_status": "not_verified",
        "enabled_for_online_comparison": False,
    }
