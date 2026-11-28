"""Reconstruct Q2 task and plan-target counts from a frozen case and its events."""

from collections import Counter, defaultdict
from math import isclose, isfinite


TIME_TOL = 1e-8
SERVICE_PHASE = {"refine": "refine", "cast": "supply", "slag": "slag"}
TERMINAL_BUCKETS = {
    "completed": "completed_ef",
    "delivered_service_censored": "delivered_unfinished_ef",
    "blocked_missing_path": "undeclared_route_ef",
    "blocked_other_input": "other_input_blocked_ef",
    "not_due_in_window": "not_due_waiting_ef",
    "censored_after_release": "released_censored_ef",
    "predecessor_unfinished": "predecessor_unfinished_ef",
    "unknown_unreleased": "unreleased_unknown_ef",
}


def _by_task(items, field):
    indexed = {row[field]: row for row in items}
    if len(indexed) != len(items):
        raise ValueError("Duplicate roster entries in Q2 archive.")
    return indexed


def _events_by_task(result, roster, horizon):
    events = defaultdict(dict)
    for event in result["events"]:
        task, stage, at = (event[k] for k in ("task_id", "event_type", "time_s"))
        if task not in roster or stage in events[task]:
            raise ValueError("Unmatched or duplicated event in Q2 archive.")
        if not isfinite(at) or not 0 <= at <= horizon + TIME_TOL:
            raise ValueError("Q2 event occurs outside the archived horizon.")
        events[task][stage] = at
    return events


def _known_release_bound(task, roster, events, case):
    if task["predecessor"] is None:
        return task["rho_s"]
    parent = roster[task["predecessor"]]
    if parent["service"] is None:
        return None
    kind = parent["service"]["kind"]
    end = events[parent["id"]].get(f"{SERVICE_PHASE[kind]}_end")
    if end is None:
        return None
    transfer = case["transfer_operation_s"] if kind == "cast" else 0
    return end + transfer + case["params"]["prep_s"]


def _classify(task, at, dropped, service_end, roster, events, case):
    if task["required_in_ef_scope"] is not True:
        return "boundary_external" if task["scope"] == "external_service" else "boundary_pending_scope"
    if service_end is not None:
        return "completed"
    if dropped is not None:
        return "delivered_service_censored"
    if "undeclared_service_path" in task["blocks"]:
        return "blocked_missing_path"
    if task["blocks"]:
        return "blocked_other_input"
    earliest = task["pickup_window"]["hard_earliest_s"]
    known_release = _known_release_bound(task, roster, events, case)
    if any(value is not None and value > case["horizon_s"] for value in (earliest, known_release)):
        return "not_due_in_window"
    if "physical_release" in at:
        return "censored_after_release"
    if task["predecessor"] is not None and known_release is None:
        return "predecessor_unfinished"
    return "unknown_unreleased"


def task_metrics(case, result):
    """Return traceable counts and private task rows without extending the original denominators.

    Terminal transport/service status and timeliness are separate. A seated task
    can be on time while its downstream service remains censored at the horizon.
    """
    horizon = case["horizon_s"]
    roster = _by_task(case["tasks"], "id")
    archived = _by_task(result["tasks"], "task_id")
    if set(archived) != set(roster) or horizon != result["horizon_s"]:
        raise ValueError("Mismatched frozen roster or observation horizon.")
    events = _events_by_task(result, roster, horizon)
    keys = ("target_ef", "plan_targets", "on_time", "late_count", "unserved_targets",
            "delivered_ef", "undelivered_ef", "unfinished_ef",
            "known_tasks", "authorized_route_ef", "outside_ef", "external_tasks",
            "pending_scope_tasks", "future_plan_targets_ef", "unbound_plan_targets_ef",
            "late_total_s", "late_max_s")
    counts = Counter(dict.fromkeys((*keys, *TERMINAL_BUCKETS.values()), 0))
    rows = []
    for tid, task in roster.items():
        at = events[tid]
        archived_task = archived[tid]
        in_ef = task["required_in_ef_scope"] is True
        drop = at.get("drop_complete")
        service = SERVICE_PHASE[task["service"]["kind"]] if task["service"] else None
        start = at.get(f"{service}_start") if service else None
        end = at.get(f"{service}_end") if service else None
        if ((start is not None and (drop is None or start < drop - TIME_TOL))
                or (end is not None and (start is None or end < start - TIME_TOL))
                or (drop is not None and (not in_ef or task["blocks"]))):
            raise ValueError("Q2 service sequence or transport eligibility disagrees with events.")
        state = _classify(task, at, drop, end, roster, events, case)
        plan = task["plan_target_s"]
        seat = at.get("seat_complete")
        if service == "supply" and ((seat is None) != (drop is None)
                                     or seat is not None and not isclose(seat, drop, abs_tol=TIME_TOL, rel_tol=0)):
            raise ValueError("Seat and delivery events disagree in the frozen Q2 model.")

        delay = None
        counts["known_tasks"] += 1
        if not in_ef:
            plan_status = "outside_ef"
            counts["outside_ef"] += 1
            counts["external_tasks" if state == "boundary_external" else "pending_scope_tasks"] += 1
        else:
            counts["target_ef"] += 1
            counts[TERMINAL_BUCKETS[state]] += 1
            counts["authorized_route_ef"] += "undeclared_service_path" not in task["blocks"]
            counts["delivered_ef" if drop is not None else "undelivered_ef"] += 1
            counts["unfinished_ef"] += end is None
            if plan is None:
                plan_status = "unbound"
                counts["unbound_plan_targets_ef"] += 1
            elif plan > horizon:
                plan_status = "future"
                counts["future_plan_targets_ef"] += 1
            else:
                counts["plan_targets"] += 1
                if drop is None:
                    plan_status = "unserved"
                    counts["unserved_targets"] += 1
                else:
                    delay = max(0.0, drop - plan)
                    plan_status = "late" if delay > TIME_TOL else "on_time"
                    counts["late_count" if delay > TIME_TOL else "on_time"] += 1
                    counts["late_total_s"] += delay
                    counts["late_max_s"] = max(counts["late_max_s"], delay)
        expected_native = ("completed" if end is not None else "delivered_service_censored"
                           if drop is not None else "input_blocked" if task["blocks"] else "unserved")
        if any(actual != expected for actual, expected in (
            (archived_task["status"], expected_native),
            (archived_task.get("drop_complete_s"), drop),
            (archived_task.get("service_start_s"), start),
            (archived_task.get("service_end_s"), end),
            (archived_task["required_in_ef_scope"], task["required_in_ef_scope"]),
            (archived_task["plan_target_s"], plan),
        )):
            raise ValueError("Q2 task archive does not reconcile with its event ledger.")
        rows.append({
            "task_id": tid, "scope": task["scope"], "scope_status": task["scope_status"],
            "required_in_ef_scope": task["required_in_ef_scope"], "terminal_state": state,
            "service_kind": task["service"]["kind"] if task["service"] else None,
            "plan_status": plan_status, "plan_target_s": plan, "plan_delay_s": delay,
            "condition_release_s": at.get("physical_release"), "source_entry_s": at.get("source_entry"),
            "pickup_start_s": at.get("pickup_start"), "drop_complete_s": drop,
            "service_start_s": start, "service_end_s": end, "reasons": archived_task["reasons"],
        })
    return dict(counts), rows
