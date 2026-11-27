"""Build Q1 planning inputs at the boundary of the B data package."""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter, defaultdict
from math import isfinite
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code/data"))
from q1_schema import policy_event_visible, validate_policy_view, validate_simulator_truth

CONFIG = ROOT / "stages/03_implementation/A/q1_assumption_scenarios.json"
CASE_SCHEMA = "mcm-uestc.q1-planning/1"
SOURCE_KEYS = (
    "task_id", "heat_id", "ladle_id", "campaign_id", "campaign_order", "route",
    "leg", "scope", "scope_status", "required_in_process", "required_in_ef_scope",
    "admission", "blocker_codes", "origin", "destination", "load_state",
    "eligible_cranes", "predecessor_task_id",
)
PLAN_TYPES = {
    "plan_bof_end", "plan_refine_slot_1", "plan_refine_slot_3", "plan_cast_start", "plan_cast_end",
}
HARD_KEYS = ("hard_earliest_s", "hard_latest_s")
CONDITIONAL_BLOCKERS = {"eligible_cranes_unverified", "holder_chain_conditional"}
CAPACITY_FOR_STAGE = {
    "bof": "bof_receipt", "refine": "refine_position", "seat": "caster_seat",
    "supply": "caster_supply", "return": "caster_return", "slag": "slag_position",
}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(obj: dict) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def checked_policy(policy: dict) -> dict:
    errors = validate_policy_view(policy)
    if errors:
        raise ValueError(f"B policy view rejected ({len(errors)} violations)")
    return policy


def project_batch(bundle: dict, policy: dict) -> dict:
    """Copy only the frozen roster, explicitly planned times, and static catalog."""
    if bundle.get("schema") != "mcm-uestc.dispatch/1":
        raise ValueError("B full bundle required at the offline projection boundary")
    checked_policy(policy)
    if (policy["instance_id"] != bundle["instance_id"]
            or policy["initial_state"]["now_s"] != bundle["initial_state"]["now_s"]):
        raise ValueError("B view and bundle belong to different instances")
    selected = set(bundle["scope"]["target_tasks"])
    if selected != set(policy["scope"]["target_tasks"]):
        raise ValueError("B view and bundle have different target task rosters")
    policy_tasks = {t["task_id"]: t for t in policy["tasks"]}
    roster = []
    for task in bundle["tasks"]:
        if task["task_id"] not in selected:
            continue
        row = {key: task[key] for key in SOURCE_KEYS}
        # Known hard bounds can only cross from B's validated visible projection.
        for name in ("pickup_window", "delivery_window"):
            row[name] = {key: policy_tasks[row["task_id"]][name][key] for key in HARD_KEYS}
        roster.append(row)
    heats = {t["heat_id"] for t in roster}
    planned = [
        {key: e[key] for key in ("heat_id", "event_type", "time_s")}
        for e in bundle["events"]
        if e["event_category"] == "plan" and e["usable_for_canonical"]
        and e["event_type"] in PLAN_TYPES and e["heat_id"] in heats
        and e["time_s"] is not None
    ]
    full_keys = {(e["heat_id"], e["event_type"], e["time_s"]) for e in planned}
    visible_keys = {(e["heat_id"], e["event_type"], e["time_s"]) for e in policy["events"]
                    if e["heat_id"] in heats and e["event_category"] == "plan"
                    and e["event_type"] in PLAN_TYPES and e["usable_for_canonical"]
                    and e["time_s"] is not None}
    if not visible_keys <= full_keys:
        raise ValueError("B views disagree on selected published plan times")
    devices = [
        {key: d[key] for key in ("device_id", "capacity_t", "dispatch_candidate")}
        for d in bundle["device_catalog"] if d["device_type"] == "EF_crane"
    ]
    if len(roster) != len(selected):
        raise ValueError("Incomplete or duplicate tasks in B roster")
    return {
        "id": bundle["instance_id"], "plan_version": f"q1-batch-{digest({'plan': sorted(full_keys)})[:12]}",
        "roster": roster, "plan": planned, "devices": devices,
    }


def online_replay_boundary(policy: dict) -> dict:
    """Audit the online dispatchable set without supplying A's offline assumptions."""
    policy = checked_policy(policy)
    state = policy["initial_state"]
    task_states = {t["task_id"]: t for t in state["task_states"]}
    available = {
        c["crane_id"] for c in state["cranes"]
        if c["dispatch_allowed"] is True and c["x"] is not None
    }
    targets = set(policy["scope"]["target_tasks"])
    candidates = []
    for task in policy["tasks"]:
        if task["task_id"] not in targets or task["admission"] != "A":
            continue
        release = task_states.get(task["task_id"], {}).get("visible_release_lo_s")
        if (task["scope"] == "ef_crane" and task["scope_status"] == "confirmed"
                and release is not None and release <= state["now_s"]
                and available.intersection(task["eligible_cranes"] or [])):
            candidates.append(task["task_id"])
    return {
        "information_mode": "online_policy_replay",
        "target_tasks": len(targets), "visible_events": len(policy["events"]),
        "visible_admission": dict(Counter(t["admission"] for t in policy["tasks"]
                                          if t["task_id"] in targets)),
        "qualified_and_released_count": len(candidates),
        "dispatch_count": 0, "scope": "entry_snapshot_only",
    }


def online_notice_audit(policy: dict, truth: dict) -> dict:
    """Simulator-side audit of when hidden rows may become public in the B window."""
    errors = validate_simulator_truth(truth)
    if errors:
        raise ValueError(f"B simulator truth rejected ({len(errors)} violations)")
    if truth["instance_id"] != policy["instance_id"]:
        raise ValueError("B view and truth belong to different instances")
    now = policy["initial_state"]["now_s"]
    end = now + policy["clock"]["horizon_s"]
    visible_times = []
    for event in truth["future_events"]:
        when = event["visible_at_s"]
        if when is None:
            continue
        if event["event_category"] == "actual":
            if event["time_s"] is None:
                continue
            when = max(when, event["time_s"])
        if now < when <= end and policy_event_visible(event, when):
            visible_times.append(when)
    return {
        "audited_seconds": end - now, "events_publishable_after_entry": len(visible_times),
        "scope": "simulator_notice_rules_only",
    }


def build_case(projected: dict, config: dict, scenario: str, layout: str = "dispersed",
               overrides: dict | None = None) -> dict:
    params = config["scenarios"][scenario] | (overrides or {})
    plans = defaultdict(lambda: defaultdict(set))
    for event in projected["plan"]:
        plans[event["heat_id"]][event["event_type"]].add(event["time_s"])
    # Preserve conflicting planned values as unknown. Never resolve by history.
    known = {h: {k: next(iter(v)) for k, v in times.items() if len(v) == 1}
             for h, times in plans.items()}
    if not any("plan_bof_end" in times for times in known.values()):
        raise ValueError("No unique planned converter-end anchor")
    origin_s = min(times["plan_bof_end"] for times in known.values() if "plan_bof_end" in times)
    stop_s = max(t for times in known.values() for t in times.values())
    horizon = stop_s - origin_s + config["planning_margin_s"]
    factor = params["distance_factor"]
    positions = {k: x * factor for k, x in (config["stations"] | config["interface_points"]).items()}
    catalog = {c["device_id"]: c for c in projected["devices"]}
    cranes = [
        {"id": cid, "x": x * factor, "capacity_t": catalog[cid]["capacity_t"],
         "dispatch": cid in config["dispatch"] and catalog[cid]["dispatch_candidate"]}
        for cid, x in zip(config["crane_order"], config["layouts"][layout], strict=True)
    ]
    cranes_by_id = {c["id"]: c for c in cranes}
    ladles = {key: f"L{n:02d}" for n, key in enumerate(
        dict.fromkeys(t["ladle_id"] for t in projected["roster"] if t["ladle_id"]), 1)}
    authorized = {tuple(leg) for leg in config["authorized_ef_legs"]}
    result_tasks = []
    resources = {}
    for row in projected["roster"]:
        plan = known.get(row["heat_id"], {})
        values = {key: value - origin_s for key, value in plan.items()}
        origin, destination = row["origin"], row["destination"]
        pred = row["predecessor_task_id"]
        reasons = []
        service = None
        rho = None
        plan_target = None
        kind = "ef" if row["scope_status"] == "confirmed" and row["scope"] == "ef_crane" else "boundary"
        if kind == "boundary":
            reasons.append("unconfirmed_ef_handoff" if row["scope_status"] == "pending_ef_confirmation"
                           else "external_receipt_unverified")
        else:
            if (origin, destination) not in authorized:
                reasons.append("undeclared_service_path")
            reasons += sorted(set(row["blocker_codes"]) - CONDITIONAL_BLOCKERS)
            if not row["ladle_id"]:
                reasons.append("unresolved_ladle_identity")
            if not pred:
                rho = values.get("plan_bof_end")
                if rho is None:
                    reasons.append("missing_converter_plan")
                else:
                    rho += params["prep_s"]
            if destination and destination.startswith(("LF", "RH")):
                second = origin and origin.startswith(("LF", "RH"))
                slot = "plan_refine_slot_3" if second else "plan_refine_slot_1"
                if slot in values:
                    service = {"kind": "refine", "not_before_s": values[slot],
                               "duration_s": params["lf_s" if destination.startswith("LF") else "rh_s"]}
                else:
                    reasons.append("missing_refine_plan")
            elif destination and destination.startswith("CC"):
                start, stop = values.get("plan_cast_start"), values.get("plan_cast_end")
                if start is not None and stop is not None and stop > start:
                    plan_target = start - config["seat_lead_s"]
                    service = {"kind": "cast", "not_before_s": start, "duration_s": stop - start}
                else:
                    reasons.append("missing_or_invalid_caster_plan")
            elif destination in params["slag_s"]:
                service = {"kind": "slag", "not_before_s": 0, "duration_s": params["slag_s"][destination]}
            else:
                reasons.append("undeclared_destination_service")
        upper = config["assumed_mass_upper_t"].get(row["load_state"])
        eligible = [
            cid for cid, crane in cranes_by_id.items() if kind == "ef" and crane["dispatch"]
            and upper is not None and upper <= crane["capacity_t"]
            and (row["eligible_cranes"] is None or cid in row["eligible_cranes"])
        ]
        if kind == "ef" and not eligible:
            reasons.append("no_qualified_crane")
        windows = {name: {k: (x - origin_s if x is not None else None)
                          for k, x in row[name].items()}
                   for name in ("pickup_window", "delivery_window")}
        for role, station in (("bof", origin), ("refine", destination), ("seat", destination),
                              ("supply", destination), ("return", destination), ("slag", destination)):
            expected = {"bof": "BOF", "refine": ("LF", "RH"), "seat": "CC", "supply": "CC",
                        "return": "CC", "slag": "SLAG"}[role]
            if station in positions and station.startswith(expected):
                resources[f"{role}/{station}"] = config["capacity"][CAPACITY_FOR_STAGE[role]]
        order = int(row["campaign_order"]) if row["campaign_order"] not in (None, "") else None
        result_tasks.append({
            "id": row["task_id"], "heat": row["heat_id"], "ladle": ladles.get(row["ladle_id"]),
            "campaign": row["campaign_id"], "order": order, "load_state": row["load_state"],
            "scope": row["scope"], "scope_status": row["scope_status"],
            "required_in_process": row["required_in_process"],
            "required_in_ef_scope": row["required_in_ef_scope"],
            "source_admission": row["admission"], "source_blockers": row["blocker_codes"],
            "route": row["route"], "leg": row["leg"], "kind": kind, "origin": origin,
            "destination": destination, "predecessor": pred, "reuse_predecessor": None,
            "previous_cast": None, "previous_bof": None, "rho_s": rho, "service": service, "eligible": eligible,
            "mass_upper_t": upper, "blocks": reasons, "plan_target_s": plan_target,
            **windows,
        })
    by_id = {t["id"]: t for t in result_tasks}
    chains = defaultdict(list)
    for t in result_tasks:
        if t["ladle"] and t["order"] is not None:
            chains[t["ladle"]].append(t)
        if t["predecessor"] and t["predecessor"] not in by_id:
            t["blocks"].append("predecessor_outside_roster")
    for uses in chains.values():
        by_heat = defaultdict(list)
        for t in uses:
            by_heat[t["heat"]].append(t)
        groups = sorted(by_heat.values(), key=lambda group: group[0]["order"])
        for earlier, later in zip(groups, groups[1:]):
            leaves = [t for t in earlier if not any(s["predecessor"] == t["id"] for s in earlier)]
            for task in later:
                if not task["predecessor"]:
                    task["reuse_predecessor"] = leaves[0]["id"] if len(leaves) == 1 else None
                    # The prior exit in this bundle never hands the ladle back to BOF.
                    task["blocks"].append("return_to_bof_unverified")
    receipts = defaultdict(list)
    for t in result_tasks:
        if t["kind"] == "ef" and t["predecessor"] is None and t["rho_s"] is not None:
            receipts[t["origin"]].append(t)
    for queue in receipts.values():
        queue.sort(key=lambda t: (t["rho_s"], t["order"] or 0, t["id"]))
        for earlier, later in zip(queue, queue[1:]):
            later["previous_bof"] = earlier["id"]
    campaigns = defaultdict(list)
    for t in result_tasks:
        if t["service"] and t["service"]["kind"] == "cast":
            if t["order"] is None or not t["campaign"]:
                t["blocks"].append("caster_order_unverified")
            else:
                campaigns[t["campaign"], t["destination"]].append(t)
    overlaps = []
    for tasks in campaigns.values():
        tasks.sort(key=lambda t: t["order"])
        for prev, cur in zip(tasks, tasks[1:]):
            cur["previous_cast"] = prev["id"]
            overlap = (prev["service"]["not_before_s"] + prev["service"]["duration_s"]
                       - cur["service"]["not_before_s"])
            if overlap > 0:
                overlaps.append(overlap)
    case = {
        "schema": CASE_SCHEMA, "information_mode": "batch_planning", "source": projected["id"],
        "plan_version": projected["plan_version"], "scenario": scenario, "layout": layout,
        "unit": config["unit"], "horizon_s": horizon, "rail": [x * factor for x in config["rail"]],
        "positions": positions, "parking": [x * factor for x in config["parking"]],
        "topology": {
            "axis": "shared_longitudinal_EF", "station_sides": config["station_sides"],
            "assumed_interfaces": sorted(config["interface_points"]),
        },
        "cranes": cranes, "capacity": resources, "params": params, "tasks": result_tasks,
        "lookahead_s": config["lookahead_s"], "seat_lead_s": config["seat_lead_s"],
        "caster_preparation_s": config["caster_preparation_s"],
        "pickup_operation_s": config["pickup_operation_s"],
        "drop_operation_s": config["drop_operation_s"],
        "transfer_operation_s": config["transfer_operation_s"],
        "locked_trajectories": {}, "initial_occupancy": [],
        "fixed_plan_overlap_count": len(overlaps),
        "fixed_plan_max_overlap_s": max(overlaps, default=0),
        "assumption_config_sha256": digest(config), "projection_sha256": digest(projected),
    }
    validate_case(case)
    return case


def validate_case(case: dict) -> None:
    """Reject inconsistent geometry, missing holders, or an invalid task graph."""
    if case["schema"] != CASE_SCHEMA or case["information_mode"] != "batch_planning":
        raise ValueError("Unexpected scheduler input schema or information mode")
    left, right = case["rail"]
    gap = case["params"]["gap"]
    cranes = case["cranes"]
    if (not all(isfinite(x) for x in (left, right, gap, case["params"]["speed"], case["horizon_s"]))
            or left >= right or gap <= 0 or case["params"]["speed"] <= 0 or case["horizon_s"] <= 0):
        raise ValueError("Invalid Q1 rail, horizon or movement parameters")
    if len(cranes) != 5 or len({c["id"] for c in cranes}) != len(cranes):
        raise ValueError("Five distinct EF cranes required")
    if any(not left <= x <= right for x in [
        *(c["x"] for c in cranes), *case["positions"].values(), *case["parking"],
    ]):
        raise ValueError("Position outside declared rail")
    if any(b["x"] - a["x"] < gap - 1e-9 for a, b in zip(cranes, cranes[1:])):
        raise ValueError("Initial layout violates the declared distance")
    if any(type(c) is not int or c <= 0 for c in case["capacity"].values()):
        raise ValueError("Resource capacity must be a positive integer")
    if any(case["params"][k] < 0 for k in ("prep_s", "max_pick_wait_s")):
        raise ValueError("Negative preparation or waiting bound")
    crane_by_id = {c["id"]: c for c in cranes}
    by_id = {t["id"]: t for t in case["tasks"]}
    if len(by_id) != len(case["tasks"]):
        raise ValueError("Repeated task ID")
    visited = set()

    def visit(tid: str, trail: set[str]) -> None:
        if tid in visited:
            return
        if tid in trail:
            raise ValueError("Cycle in Q1 task and casting dependencies")
        task = by_id[tid]
        for predecessor in (task["predecessor"], task["previous_cast"], task["reuse_predecessor"],
                            task.get("previous_bof")):
            if predecessor in by_id:
                visit(predecessor, trail | {tid})
        if task["kind"] == "ef" and not task["blocks"]:
            if (not task["ladle"] or not task["eligible"] or not task["service"]
                    or any(task[k] not in case["positions"] for k in ("origin", "destination"))):
                raise ValueError("Missing prerequisites for an EF candidate")
            mass = task["mass_upper_t"]
            if (mass is None or not isfinite(mass) or mass <= 0
                    or any(cid not in crane_by_id or not crane_by_id[cid]["dispatch"]
                           or mass > crane_by_id[cid]["capacity_t"] for cid in task["eligible"])):
                raise ValueError("EF candidate violates declared dispatch eligibility or mass limit")
            if task["predecessor"] in by_id:
                pred = by_id[task["predecessor"]]
                if pred["ladle"] != task["ladle"] or pred["destination"] != task["origin"]:
                    raise ValueError("Broken physical ladle handoff")
        visited.add(tid)

    for tid in by_id:
        visit(tid, set())
