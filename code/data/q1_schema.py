"""Schema and validation for the Stage 03 Q1 standardized dispatch input.

The schema follows ``mcm-uestc.dispatch/1`` as defined in
``stages/02_modeling/implementation_contract.md``.  It deliberately keeps
``null`` (unknown) distinct from ``[]`` (confirmed empty) and from ``0``.
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA = "mcm-uestc.dispatch/1"
POLICY_SCHEMA = "mcm-uestc.dispatch.policy-view/1"
SIMULATOR_SCHEMA = "mcm-uestc.dispatch.simulator-truth/1"

SCENARIOS = {"Q1", "Q2", "Q3"}
MODES = {"strict_plan", "authorized_recovery"}
SOURCE_STATUSES = {"observed", "derived", "assumed", "pending"}
APPLICABILITY = {"active", "not_applicable", "unknown"}
ADMISSIONS = {"A", "B", "C"}
SCOPES = {"ef_crane", "external_service"}
SCOPE_STATUSES = {"confirmed", "pending_ef_confirmation", "external_boundary"}
EF_STATION_IDS = {"LF1", "LF3", "RH1", "RH2", "CC1", "CC2", "CC3"}
LOAD_STATES = {"loaded", "empty_ladle"}
EVENT_CATEGORIES = {"plan", "actual", "derived"}
PICKUP_EVENTS = {"bof_lift", "lf_lift", "rh_lift", "cc_empty_lift", "return_ef_lift"}
DELIVERY_EVENTS = {
    "lf_seat", "rh_seat", "cc_seat", "transfer_seat", "repair_seat",
    "return_transfer_seat", "return_bof_c_seat",
}

TOP_LEVEL_KEYS = (
    "schema", "instance_id", "scenario", "mode", "clock", "plan_version",
    "scope", "evidence", "device_catalog", "topology", "resources",
    "events", "tasks", "simulator_truth", "initial_state",
)

TASK_KEYS = (
    "task_id", "heat_id", "ladle_id", "ladle_identity", "campaign_id", "campaign_order",
    "route", "scope", "scope_status", "load_state", "origin", "destination",
    "mass_t", "predecessors", "release_interval",
    "physical_release_s", "historical_pickup_proxy_s", "applied_release_s",
    "pickup_window", "delivery_window",
    "capacity_candidates", "eligible_cranes", "eligibility_status",
    "observed_crane", "observed_crane_candidates", "crane_evidence_status",
    "resource_requirements", "resource_requirement_status",
    "required", "required_in_process", "required_in_ef_scope",
    "admission", "blocker_codes",
    "source_status", "source_refs", "flags",
)

EVENT_KEYS = (
    "event_id", "event_type", "event_category", "heat_id", "ladle_id",
    "task_id", "task_ids", "resource_id", "time_s", "visible_at_s",
    "sequence", "source_status", "source_ref", "source_event_status",
    "link_status", "usable_for_canonical", "weight_t", "raw",
)

INITIAL_STATE_KEYS = (
    "now_s", "state_version", "plan_version", "cranes", "holders",
    "active_operations", "occupancy", "campaign_state", "task_states",
    "locked_reservations", "visible_notices", "status", "blocking",
)

WINDOW_KEYS = (
    "hard_earliest_s", "hard_latest_s", "plan_earliest_s", "plan_latest_s",
    "applicability", "source_status", "basis",
)


HOLDER_ROLES = {
    "bof_empty_car": ("BOF", False),
    "bof_lift": ("EF", False),
    "lf_seat": ("STATION", False),
    "lf_lift": ("EF", False),
    "rh_seat": ("STATION", False),
    "rh_lift": ("EF", False),
    "cc_seat": ("STATION", False),
    "cc_empty_lift": ("EF", False),
    "slag_start": ("STATION", True),
    "slag_second_start": ("STATION", True),
    "transfer_seat": ("TRANSFER", False),
    "repair_seat": ("REPAIR", False),
    "return_transfer_seat": ("TRANSFER", False),
    "return_ef_lift": ("EF", False),
    "return_bof_c_seat": ("BOF_RETURN", False),
}


def _holder_after(kind: str, event: dict) -> str:
    if kind == "EF":
        return "EF_CRANE:unknown"
    if kind == "STATION":
        return event["resource_id"] or "STATION:unknown"
    if kind == "TRANSFER":
        return event["resource_id"] or "TRANSFER:unknown"
    if kind == "REPAIR":
        return event["resource_id"] or "REPAIR:unknown"
    if kind == "BOF_RETURN":
        return event["resource_id"] or "BOF_RETURN:unknown"
    return event["resource_id"] or "BOF:unknown"


def holder_attributed_to(events: list[dict]) -> str | None:
    """Resolve corroborating same-time evidence without choosing a physical order."""
    if not events:
        return None
    values = {_holder_after(HOLDER_ROLES[e["event_type"]][0], e) for e in events}
    if len({e["event_type"] for e in events}) == 1:
        known = {value for value in values if "unknown" not in value}
        if known:
            values = known
    return next(iter(values)) if len(values) == 1 else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool))


def policy_event_visible(event: dict, now_s: int) -> bool:
    """Expose announced plans and announced facts that have already occurred."""
    known_at = event.get("visible_at_s")
    if not _is_int(known_at) or known_at > now_s:
        return False
    when = event.get("time_s")
    category = event.get("event_category")
    return _is_int(when) and (
        category == "plan" and str(event.get("event_type", "")).startswith("plan_")
        or category == "actual" and when <= now_s
    )


def release_evidence(task: dict, events: list[dict]) -> dict | None:
    """Resolve a release bound only against an already visible named source."""
    ref = task["source_refs"].get("process_predecessor")
    matches = [e for e in events if ref and e["source_ref"] == ref
               and e["heat_id"] == task["heat_id"] and e["usable_for_canonical"]]
    values = {(e["event_category"], e["time_s"]) for e in matches}
    if len(values) != 1:
        return None
    category, when = values.pop()
    return {
        "value_s": when,
        "type": "planned_release" if category == "plan" else "process_predecessor_lower_bound",
        "source_status": "derived",
    }


def plan_delivery_target(task: dict, events: list[dict]) -> int | None:
    ref = task["source_refs"].get("plan_deadline")
    if task.get("destination_family") != "CC":
        return None
    times = {
        event["time_s"] for event in events
        if event["event_category"] == "plan" and event["event_type"] == "plan_cast_start"
        and event["heat_id"] == task["heat_id"] and event["usable_for_canonical"]
        and (not ref or event["source_ref"] == ref)
    }
    return next(iter(times)) if len(times) == 1 else None


def visible_crane_evidence(task_id: str, events: list[dict], dispatch_ids: set[str]) -> dict:
    observations = [
        (observation["crane_id"], event["source_ref"].rsplit("|", 1)[0])
        for event in events if event["event_category"] == "actual"
        for observation in event.get("crane_observations", [])
        if observation["task_id"] == task_id
    ]
    candidates = sorted({crane for crane, _ in observations})
    observed = candidates[0] if len(candidates) == 1 else None
    return {
        "observed_crane": observed,
        "observed_crane_candidates": candidates,
        "observed_crane_dispatch_candidate": observed in dispatch_ids if observed else None,
        "crane_device_refs": sorted({ref for _, ref in observations}),
        "crane_evidence_status": "confirmed" if observed else "conflict" if candidates else "missing",
        "scope_basis": "device_crane_label" if candidates else "process_interface_inferred",
    }


def _check_time_order(errors: list[str], label: str, window: dict) -> None:
    pairs = (
        ("hard_earliest_s", "hard_latest_s"),
        ("plan_earliest_s", "plan_latest_s"),
    )
    for start_key, end_key in pairs:
        start, end = window.get(start_key), window.get(end_key)
        if start is not None and end is not None and start > end:
            errors.append(f"{label}: {start_key}={start} exceeds {end_key}={end}")


def _require_keys(errors: list[str], label: str, obj: dict, keys: tuple[str, ...]) -> None:
    if not isinstance(obj, dict):
        errors.append(f"{label}: expected object")
        return
    missing = [key for key in keys if key not in obj]
    if missing:
        errors.append(f"{label}: missing keys {missing}")


def validate_task(task: dict, index: int = 0) -> list[str]:
    errors: list[str] = []
    label = f"tasks[{index}]"
    _require_keys(errors, label, task, TASK_KEYS)
    if errors:
        return errors
    if not task["task_id"]:
        errors.append(f"{label}: empty task_id")
    if task["scope"] not in SCOPES:
        errors.append(f"{label}: invalid scope {task['scope']!r}")
    if task["scope_status"] not in SCOPE_STATUSES:
        errors.append(f"{label}: invalid scope_status {task['scope_status']!r}")
    if task["load_state"] not in LOAD_STATES:
        errors.append(f"{label}: invalid load_state {task['load_state']!r}")
    if task["admission"] not in ADMISSIONS:
        errors.append(f"{label}: invalid admission {task['admission']!r}")
    eligible = task["eligible_cranes"]
    if eligible is not None and not isinstance(eligible, list):
        errors.append(f"{label}: eligible_cranes must be a list or null")
    if task["admission"] == "A":
        if not eligible:
            errors.append(f"{label}: A task must have a non-empty verified eligible_cranes list")
        if task["blocker_codes"]:
            errors.append(f"{label}: A task must not carry blocker codes")
        if task["required_in_ef_scope"] is not True:
            errors.append(f"{label}: A task must be required in EF scope")
    if eligible == [] and task["admission"] != "C":
        errors.append(f"{label}: an empty eligible_cranes set requires admission C")
    if eligible == [] and not any(
        code in task["blocker_codes"] for code in ("no_eligible_crane", "no_capacity_compatible_crane")
    ):
        errors.append(f"{label}: empty eligible_cranes needs an explicit no-crane blocker")
    if task["admission"] == "C" and not task["blocker_codes"]:
        errors.append(f"{label}: C task must record at least one blocker code")
    if task["capacity_candidates"] is not None and not isinstance(task["capacity_candidates"], list):
        errors.append(f"{label}: capacity_candidates must be a list or null")
    if task["capacity_candidates"] == [] and (
        not isinstance(task["mass_t"], dict) or task["mass_t"].get("hi") is None
    ):
        errors.append(f"{label}: empty capacity_candidates requires a known mass")
    if task["capacity_candidates"] == [] and "no_capacity_compatible_crane" not in task["blocker_codes"]:
        errors.append(f"{label}: empty capacity_candidates needs a no-capacity blocker")
    if task["scope"] == "ef_crane" and task["scope_status"] == "confirmed":
        if task["required_in_ef_scope"] is not True:
            errors.append(f"{label}: confirmed EF task must set required_in_ef_scope true")
    if task["scope"] == "ef_crane" and task["scope_status"] == "pending_ef_confirmation":
        if task["required_in_ef_scope"] is not None:
            errors.append(f"{label}: pending EF task must set required_in_ef_scope null")
    if task["scope"] == "external_service" and task["required_in_ef_scope"] is not False:
        errors.append(f"{label}: external service must set required_in_ef_scope false")
    identity = task["ladle_identity"]
    if not isinstance(identity, dict) or "status" not in identity:
        errors.append(f"{label}: ladle_identity needs a status")
    elif identity["status"] in {"unique", "project_approved"}:
        if task["ladle_id"] != identity.get("ladle_id"):
            errors.append(f"{label}: resolved ladle_identity must match ladle_id")
    elif task["ladle_id"] is not None:
        errors.append(f"{label}: unresolved ladle identity must leave ladle_id null")
    mass = task["mass_t"]
    if not isinstance(mass, dict) or not {"lo", "hi", "source_status"} <= set(mass):
        errors.append(f"{label}: mass_t needs lo, hi and source_status")
    else:
        lo, hi = mass["lo"], mass["hi"]
        if lo is not None and not _is_number(lo):
            errors.append(f"{label}: mass lo must be a number or null")
        if hi is not None and not _is_number(hi):
            errors.append(f"{label}: mass hi must be a number or null")
        if lo is not None and hi is not None and lo > hi:
            errors.append(f"{label}: mass lo {lo} exceeds hi {hi}")
        if mass["source_status"] not in SOURCE_STATUSES:
            errors.append(f"{label}: invalid mass source_status")
    release = task["release_interval"]
    if not isinstance(release, dict) or not {"lo_s", "hi_s", "rule", "source_status"} <= set(release):
        errors.append(f"{label}: release_interval needs lo_s, hi_s, rule and source_status")
    else:
        lo, hi = release["lo_s"], release["hi_s"]
        if lo is not None and not _is_int(lo):
            errors.append(f"{label}: release lo_s must be an int or null")
        if hi is not None and not _is_int(hi):
            errors.append(f"{label}: release hi_s must be an int or null")
        if lo is not None and hi is not None and lo > hi:
            errors.append(f"{label}: release lo {lo} exceeds hi {hi}")
        if release["source_status"] not in SOURCE_STATUSES:
            errors.append(f"{label}: invalid release source_status")
    for key in ("pickup_window", "delivery_window"):
        window = task[key]
        if not isinstance(window, dict):
            errors.append(f"{label}: {key} must be an object")
            continue
        missing = [k for k in WINDOW_KEYS if k not in window]
        if missing:
            errors.append(f"{label}: {key} missing {missing}")
            continue
        _check_time_order(errors, f"{label}.{key}", window)
        if window["applicability"] not in APPLICABILITY:
            errors.append(f"{label}: {key} invalid applicability")
        if window["source_status"] not in SOURCE_STATUSES:
            errors.append(f"{label}: {key} invalid source_status")
    if task["source_status"] not in SOURCE_STATUSES:
        errors.append(f"{label}: invalid source_status")
    if not isinstance(task["required"], bool):
        errors.append(f"{label}: required must be boolean")
    if not isinstance(task["required_in_process"], bool):
        errors.append(f"{label}: required_in_process must be boolean")
    if isinstance(task["required"], bool) and isinstance(task["required_in_process"], bool):
        if task["required"] != task["required_in_process"]:
            errors.append(f"{label}: required must equal required_in_process")
    if task["required_in_ef_scope"] is not None and not isinstance(task["required_in_ef_scope"], bool):
        errors.append(f"{label}: required_in_ef_scope must be boolean or null")
    if not isinstance(task["predecessors"], list):
        errors.append(f"{label}: predecessors must be a list")
    if task["resource_requirements"] is not None and not isinstance(task["resource_requirements"], list):
        errors.append(f"{label}: resource_requirements must be a list or null")
    if task["resource_requirements"] is None and task["resource_requirement_status"] != "unverified":
        errors.append(f"{label}: null resource_requirements must be marked unverified")
    if task["resource_requirements"] == [] and task["resource_requirement_status"] == "unverified":
        errors.append(f"{label}: empty resource_requirements cannot claim unverified status")
    if not isinstance(task["observed_crane_candidates"], list):
        errors.append(f"{label}: observed_crane_candidates must be a list")
    if task["crane_evidence_status"] not in {"confirmed", "conflict", "missing"}:
        errors.append(f"{label}: invalid crane_evidence_status")
    if not isinstance(task["source_refs"], dict):
        errors.append(f"{label}: source_refs must be an object")
    return errors


def validate_event(event: dict, index: int = 0) -> list[str]:
    errors: list[str] = []
    label = f"events[{index}]"
    _require_keys(errors, label, event, EVENT_KEYS)
    if errors:
        return errors
    if not event["event_id"]:
        errors.append(f"{label}: empty event_id")
    if event["event_category"] not in EVENT_CATEGORIES:
        errors.append(f"{label}: invalid event_category {event['event_category']!r}")
    if event["source_status"] not in SOURCE_STATUSES:
        errors.append(f"{label}: invalid source_status")
    if event["time_s"] is not None and not _is_int(event["time_s"]):
        errors.append(f"{label}: time_s must be an int or null")
    if event["visible_at_s"] is not None and not _is_int(event["visible_at_s"]):
        errors.append(f"{label}: visible_at_s must be an int or null")
    if not _is_int(event["sequence"]):
        errors.append(f"{label}: sequence must be an int")
    # A delayed announcement may legitimately set visible_at_s > time_s.
    if not isinstance(event["task_ids"], list):
        errors.append(f"{label}: task_ids must be a list")
    return errors


def validate_instance(instance: dict) -> list[str]:
    """Return a list of schema errors; an empty list means structurally valid."""
    errors: list[str] = []
    _require_keys(errors, "instance", instance, TOP_LEVEL_KEYS)
    if errors:
        return errors
    if instance["schema"] != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}")
    if instance["scenario"] not in SCENARIOS:
        errors.append(f"invalid scenario {instance['scenario']!r}")
    if instance["mode"] not in MODES:
        errors.append(f"invalid mode {instance['mode']!r}")
    clock = instance["clock"]
    _require_keys(errors, "clock", clock, ("origin_s", "horizon_s", "time_unit", "timezone", "timestamp_origin"))
    if _is_int(clock.get("origin_s")) and _is_int(clock.get("horizon_s")):
        if clock["horizon_s"] <= 0:
            errors.append("clock.horizon_s must be positive")
    if clock.get("time_unit") != "s":
        errors.append("clock.time_unit must be 's'")
    _require_keys(errors, "scope", instance["scope"], ("target_heats", "target_tasks", "window", "due_rule", "process_mode", "recovery_authorization"))
    _require_keys(errors, "evidence", instance["evidence"], ("mode", "source_hashes", "builder_version", "raw_data_local_only", "assumptions", "blocking"))
    _require_keys(errors, "topology", instance["topology"], ("scope", "stations", "rail_limits", "min_gap_m", "speed_mps", "geometry_status", "evidence"))
    _require_keys(errors, "initial_state", instance["initial_state"], INITIAL_STATE_KEYS)
    _require_keys(
        errors, "simulator_truth", instance["simulator_truth"],
        ("visibility", "now_s", "policy_rule", "task_actuals", "future_event_ids",
         "active_operation_actual_end_s"),
    )
    if not isinstance(instance["device_catalog"], list):
        errors.append("device_catalog must be a list")
    if not isinstance(instance["resources"], list):
        errors.append("resources must be a list")
    if not isinstance(instance["events"], list) or not isinstance(instance["tasks"], list):
        errors.append("events and tasks must be lists")
        return errors

    device_ids = set()
    for index, device in enumerate(instance["device_catalog"]):
        if not isinstance(device, dict) or "device_id" not in device:
            errors.append(f"device_catalog[{index}]: missing device_id")
            continue
        if device["device_id"] in device_ids:
            errors.append(f"duplicate device_id {device['device_id']!r}")
        device_ids.add(device["device_id"])
        if device.get("capacity_t") is not None and not _is_number(device.get("capacity_t")):
            errors.append(f"device_catalog[{index}]: capacity_t must be numeric or null")
        if not isinstance(device.get("dispatch_candidate"), bool):
            errors.append(f"device_catalog[{index}]: dispatch_candidate must be boolean")

    resource_ids = set()
    for index, resource in enumerate(instance["resources"]):
        if not isinstance(resource, dict) or "resource_id" not in resource:
            errors.append(f"resources[{index}]: missing resource_id")
            continue
        if resource["resource_id"] in resource_ids:
            errors.append(f"duplicate resource_id {resource['resource_id']!r}")
        resource_ids.add(resource["resource_id"])
        if resource.get("capacity") is not None and not _is_number(resource.get("capacity")):
            errors.append(f"resources[{index}]: capacity must be numeric or null")
        if not isinstance(resource.get("in_ef_topology"), bool):
            errors.append(f"resources[{index}]: in_ef_topology must be boolean")

    topology = instance["topology"]
    station_ids = set()
    if not isinstance(topology.get("stations"), list):
        errors.append("topology.stations must be a list")
    else:
        for index, station in enumerate(topology["stations"]):
            if not isinstance(station, dict) or "station_id" not in station:
                errors.append(f"topology.stations[{index}]: missing station_id")
                continue
            if station["station_id"] in station_ids:
                errors.append(f"duplicate topology station {station['station_id']!r}")
            station_ids.add(station["station_id"])
            if station["station_id"] not in EF_STATION_IDS:
                errors.append(
                    f"topology.stations[{index}]: {station['station_id']!r} is not an EF station"
                )
            if station.get("side") not in {"E", "F"}:
                errors.append(f"topology.stations[{index}]: side must be E or F")
            if not _is_int(station.get("relative_order")):
                errors.append(f"topology.stations[{index}]: relative_order must be an int")
            if topology.get("geometry_status") not in {"verified", "synthetic_fixture"} and station.get("position_m") is not None:
                errors.append(
                    f"topology.stations[{index}]: position_m must be null while geometry is unverified"
                )

    task_ids, event_ids = set(), set()
    for index, task in enumerate(instance["tasks"]):
        task_errors = validate_task(task, index)
        errors.extend(task_errors)
        if task.get("task_id") in task_ids:
            errors.append(f"duplicate task_id {task.get('task_id')!r}")
        task_ids.add(task.get("task_id"))
    for index, event in enumerate(instance["events"]):
        errors.extend(validate_event(event, index))
        if event.get("event_id") in event_ids:
            errors.append(f"duplicate event_id {event.get('event_id')!r}")
        event_ids.add(event.get("event_id"))

    for index, task in enumerate(instance["tasks"]):
        for station_key in ("origin", "destination"):
            value = task.get(station_key)
            if value is not None and value not in resource_ids:
                errors.append(f"tasks[{index}].{station_key}: unknown resource {value!r}")
        requirements = task.get("resource_requirements")
        if requirements is not None:
            for requirement in requirements:
                if requirement.get("resource_id") not in resource_ids:
                    errors.append(
                        f"tasks[{index}].resource_requirements: unknown resource "
                        f"{requirement.get('resource_id')!r}"
                    )
        for predecessor in task.get("predecessors", []):
            if isinstance(predecessor, str) and predecessor not in task_ids | event_ids:
                errors.append(f"tasks[{index}].predecessors: unknown reference {predecessor!r}")
            if (isinstance(predecessor, dict) and "task_id" in predecessor
                    and predecessor.get("task_id") not in task_ids):
                errors.append(
                    f"tasks[{index}].predecessors: unknown task reference "
                    f"{predecessor.get('task_id')!r}"
                )
    for index, event in enumerate(instance["events"]):
        resource_id = event.get("resource_id")
        if resource_id is not None and resource_id not in resource_ids:
            errors.append(f"events[{index}].resource_id: unknown resource {resource_id!r}")

    scope = instance["scope"]
    for task_id in scope.get("target_tasks", []):
        if task_id not in task_ids:
            errors.append(f"scope.target_tasks: unknown task {task_id!r}")
    target_heats = set(scope.get("target_heats", []))
    for heat in target_heats:
        if not any(task.get("heat_id") == heat for task in instance["tasks"]):
            errors.append(f"scope.target_heats: no task for heat {heat!r}")

    initial = instance["initial_state"]
    simulator = instance["simulator_truth"]
    if simulator.get("visibility") != "hidden_from_policy":
        errors.append("simulator_truth.visibility must be 'hidden_from_policy'")
    if simulator.get("now_s") != initial.get("now_s"):
        errors.append("simulator_truth.now_s must equal initial_state.now_s")
    if not isinstance(simulator.get("task_actuals"), list):
        errors.append("simulator_truth.task_actuals must be a list")
    if not isinstance(simulator.get("future_event_ids"), list):
        errors.append("simulator_truth.future_event_ids must be a list")
    for event_id in simulator.get("future_event_ids", []):
        if event_id not in event_ids:
            errors.append(f"simulator_truth.future_event_ids: unknown event {event_id!r}")
    for index, actual in enumerate(simulator.get("task_actuals", [])):
        if actual.get("task_id") not in task_ids:
            errors.append(
                f"simulator_truth.task_actuals[{index}]: unknown task "
                f"{actual.get('task_id')!r}"
            )
    for index, state in enumerate(initial.get("task_states", [])):
        if state.get("task_id") not in task_ids:
            errors.append(f"initial_state.task_states[{index}]: unknown task {state.get('task_id')!r}")
    for index, crane in enumerate(initial.get("cranes", [])):
        if crane.get("crane_id") not in device_ids:
            errors.append(f"initial_state.cranes[{index}]: unknown device {crane.get('crane_id')!r}")
    now_s = initial.get("now_s")
    for index, holder in enumerate(initial.get("holders", [])):
        since = holder.get("since_s")
        if since is not None and _is_int(now_s) and since > now_s:
            errors.append(f"initial_state.holders[{index}]: holder transition is in the future")
    for index, operation in enumerate(initial.get("active_operations", [])):
        if operation.get("end_s") is not None:
            errors.append(f"initial_state.active_operations[{index}]: future end_s leaked")
        if operation.get("end_s_visible") is True:
            errors.append(f"initial_state.active_operations[{index}]: end_s marked visible")
    for index, state in enumerate(initial.get("task_states", [])):
        if "target_end_s" in state:
            errors.append(f"initial_state.task_states[{index}]: historical target_end_s leaked")
    try:
        json.dumps(instance, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        errors.append(f"instance is not strict JSON: {exc}")
    return errors


POLICY_KEYS = (
    "schema", "instance_id", "scenario", "mode", "clock", "plan_version",
    "scope", "evidence", "device_catalog", "topology", "resources",
    "events", "tasks", "initial_state", "visibility",
)
POLICY_TASK_KEYS = set(TASK_KEYS) | {
    "leg", "scope_basis", "origin_label", "destination_label", "origin_family",
    "destination_family", "origin_known", "destination_known", "predecessor_task_id",
    "observed_arrival_s", "observed_crane_dispatch_candidate", "crane_device_refs",
    "source_plan_outer_deadline", "link_status", "holder_before", "holder_after", "holder_status",
}
POLICY_EVENT_KEYS = set(EVENT_KEYS) | {
    "resource_family", "resource_known", "resource_label", "sequence_basis",
    "visibility_assumption", "crane_observations",
}
POLICY_STATE_RECORD_KEYS = {
    "cranes": {
        "crane_id", "capacity_t", "dispatch_candidate", "x", "v", "gear", "mode",
        "carried_task_id", "held_ladle_id", "motion_or_operation_phase", "phase_started_s",
        "remaining_service_s", "dispatch_allowed", "track_presence", "evidence",
    },
    "holders": {"ladle_id", "holder_id", "holder_type", "since_s", "status", "holder_basis",
                "source_event_id", "source_event_ids"},
    "active_operations": {"heat_id", "operation", "start_s", "status", "source_event_ids",
                          "resource_id", "end_s", "end_s_visible", "remaining_service_s"},
    "task_states": {"task_id", "status", "source_event_ids", "visible_release_lo_s",
                    "release_visibility_basis", "plan_target_s", "plan_target_basis"},
    "occupancy": {"resource_id", "occupants", "capacity", "status"},
    "campaign_state": {"campaign_id", "caster", "heats", "current_heat", "next_heat"},
}


def _check_allowed_keys(errors: list[str], label: str, record: dict, keys) -> None:
    if not isinstance(record, dict):
        errors.append(f"{label}: expected object")
    elif set(record) - set(keys):
        errors.append(f"{label}: unexpected fields {sorted(set(record) - set(keys))}")


def _validate_policy_state(policy: dict, events: dict[str, dict], errors: list[str]) -> None:
    initial = policy["initial_state"]
    tasks = {task["task_id"]: task for task in policy["tasks"]}
    visible_handoffs = [
        e for e in events.values() if e["event_category"] == "actual"
        and e["usable_for_canonical"] and e["event_type"] in HOLDER_ROLES
    ]
    _check_allowed_keys(errors, "initial_state", initial, INITIAL_STATE_KEYS)
    if initial["locked_reservations"] or initial["visible_notices"]:
        errors.append("initial_state: unsolicited reservations or notices have no visible evidence")
    for field, keys in POLICY_STATE_RECORD_KEYS.items():
        records = initial[field]
        if not isinstance(records, list):
            errors.append(f"initial_state.{field}: expected list")
            continue
        for i, record in enumerate(records):
            label = f"initial_state.{field}[{i}]"
            _check_allowed_keys(errors, label, record, keys)
            if not isinstance(record, dict):
                continue
            ids = record.get("source_event_ids", [])
            if not isinstance(ids, list) or any(
                not isinstance(ref, str) or ref not in events or events[ref]["event_category"] != "actual"
                for ref in ids
            ):
                errors.append(f"{label}: state source is not a visible actual event")
                continue
            sources = [events[ref] for ref in ids]
            if field == "cranes":
                started = record.get("phase_started_s")
                if started is not None and (not _is_int(started) or started > initial["now_s"]):
                    errors.append(f"{label}: invalid crane phase start")
            elif field == "holders":
                if record.get("source_event_id") != (ids[0] if len(ids) == 1 else None):
                    errors.append(f"{label}: holder reference does not match its sources")
                if any(e["time_s"] != record.get("since_s") or e["ladle_id"] != record.get("ladle_id")
                       for e in sources):
                    errors.append(f"{label}: holder time or identity differs from visible sources")
                if not sources and (record.get("holder_id") is not None or record.get("since_s") is not None
                                    or record.get("status") != "unknown"):
                    errors.append(f"{label}: holder claimed without a visible transition")
                history = [e for e in visible_handoffs if e["ladle_id"] == record.get("ladle_id")]
                if history:
                    last = max(e["time_s"] for e in history)
                    latest = [e for e in history if e["time_s"] == last]
                    value = holder_attributed_to(latest)
                    known = value if value and "unknown" not in value else None
                    if (set(ids) != {e["event_id"] for e in latest} or record.get("holder_id") != known
                            or (value is None and record.get("status") != "ambiguous")
                            or (value is not None and known is None and record.get("status") != "unknown")):
                        errors.append(f"{label}: holder differs from the latest visible transition")
                elif sources:
                    errors.append(f"{label}: holder source is not a holder transition")
            elif field == "active_operations":
                roles = {"LF": "lf_process_start", "RH": "rh_enter", "CC": "cast_start"}
                if not sources or any(
                    e["event_type"] != roles.get(record.get("operation"))
                    or e["heat_id"] != record.get("heat_id")
                    or e["time_s"] != record.get("start_s")
                    or e["resource_id"] != record.get("resource_id") for e in sources
                ):
                    errors.append(f"{label}: operation start has no matching visible evidence")
                if (record.get("end_s") is not None or record.get("end_s_visible") is not False
                        or record.get("remaining_service_s") is not None):
                    errors.append(f"{label}: operation end or actual remaining duration leaked")
                ends = {"LF": "lf_process_end", "RH": "rh_exit", "CC": "cast_end"}
                if sources and any(
                    e["event_category"] == "actual" and e["usable_for_canonical"]
                    and e["heat_id"] == record.get("heat_id")
                    and e["event_type"] == ends.get(record.get("operation"))
                    and e["time_s"] >= sources[0]["time_s"] for e in events.values()
                ):
                    errors.append(f"{label}: operation already ended in visible events")
            elif field == "task_states":
                task = tasks.get(record.get("task_id"))
                if task is None:
                    errors.append(f"{label}: unknown task")
                    continue
                release = task["applied_release_s"]
                lo = (release["value_s"] if release
                      and release["type"] == "process_predecessor_lower_bound" else None)
                if record.get("visible_release_lo_s") != lo:
                    errors.append(f"{label}: initial release is not backed by visible task evidence")
                if record.get("plan_target_s") != task["delivery_window"]["plan_latest_s"]:
                    errors.append(f"{label}: initial plan time differs from the visible plan")
                status = record.get("status")
                roles = {"completed_visible": DELIVERY_EVENTS, "in_progress_visible": PICKUP_EVENTS}
                if sources:
                    if status not in roles or any(
                        e["event_type"] not in roles[status] or task["task_id"] not in e["task_ids"]
                        for e in sources
                    ):
                        errors.append(f"{label}: task state differs from its visible source events")
                elif status != "not_started":
                    errors.append(f"{label}: task progress has no visible evidence")
    if errors:
        return
    casting = [op for op in initial["active_operations"] if op["operation"] == "CC"]
    for slot in initial["occupancy"]:
        expected = sorted({op["heat_id"] for op in casting if op["resource_id"] == slot["resource_id"]})
        if slot["occupants"] != expected:
            errors.append("initial occupancy does not match visible operations")
    for campaign in initial["campaign_state"]:
        active = {op["heat_id"] for op in casting if op["heat_id"] in campaign["heats"]}
        if campaign["current_heat"] != (next(iter(active)) if len(active) == 1 else None):
            errors.append("initial campaign state does not match visible operations")


def validate_policy_view(policy: dict) -> list[str]:
    """Validate the policy boundary, including provenance for time and state claims."""
    errors: list[str] = []
    _require_keys(errors, "policy_view", policy, POLICY_KEYS)
    _check_allowed_keys(errors, "policy_view", policy, POLICY_KEYS)
    if errors:
        return errors
    if policy["schema"] != POLICY_SCHEMA:
        errors.append(f"schema must be {POLICY_SCHEMA!r}")
    initial = policy["initial_state"]
    visibility = policy["visibility"]
    _require_keys(errors, "initial_state", initial, INITIAL_STATE_KEYS)
    _require_keys(errors, "visibility", visibility, ("now_s", "rule", "excluded_fields"))
    _check_allowed_keys(errors, "visibility", visibility, ("now_s", "rule", "excluded_fields"))
    if errors:
        return errors
    now_s = initial["now_s"]
    if not _is_int(now_s) or visibility["now_s"] != now_s:
        return ["invalid or inconsistent decision time"]
    if not isinstance(policy["events"], list) or not isinstance(policy["tasks"], list):
        return ["policy events and tasks must be lists"]
    if policy["evidence"].get("source_hashes") or policy["evidence"].get("simulator_truth_file"):
        errors.append("policy evidence contains hidden input metadata")
    if policy["evidence"].get("interface_extensions", {}).get("simulator_truth", {}).get("hidden_truth"):
        errors.append("policy evidence contains a hidden truth file pointer")
    if any(resource.get("occupancy") is not None for resource in policy["resources"]):
        errors.append("resource occupancy must come from the visible initial state")

    by_id = {}
    for index, event in enumerate(policy["events"]):
        label = f"events[{index}]"
        errors.extend(validate_event(event, index))
        _check_allowed_keys(errors, label, event, POLICY_EVENT_KEYS)
        if not isinstance(event, dict):
            continue
        if not policy_event_visible(event, now_s):
            errors.append(f"{label}: future or invisible event leaked into policy view")
        if event.get("raw") != {}:
            errors.append(f"{label}: raw audit payload is not policy-visible")
        event_id = event.get("event_id")
        if event_id in by_id:
            errors.append(f"{label}: duplicate event_id")
        by_id[event_id] = event
        observations = event.get("crane_observations", [])
        if not isinstance(observations, list):
            errors.append(f"{label}: invalid crane observations")
        elif observations and (
            event.get("event_category") != "actual" or event.get("event_type") not in PICKUP_EVENTS | DELIVERY_EVENTS
            or not event.get("usable_for_canonical")
            or any(not isinstance(o, dict) or set(o) != {"task_id", "crane_id"}
                   or not isinstance(o["task_id"], str) or not isinstance(o["crane_id"], str)
                   or not o["crane_id"] for o in observations)
        ):
            errors.append(f"{label}: crane labels must belong to an announced pickup or placement")
    if errors:
        return errors

    events = policy["events"]
    refs = {e["source_ref"] for e in events if e["source_ref"]}
    weight_events = [e for e in events if e["event_category"] == "actual" and _is_number(e["weight_t"])]
    dispatch_ids = {d["device_id"] for d in policy["device_catalog"] if d["dispatch_candidate"]}
    tasks_by_id = {}
    for index, task in enumerate(policy["tasks"]):
        label = f"tasks[{index}]"
        errors.extend(validate_task(task, index))
        _check_allowed_keys(errors, label, task, POLICY_TASK_KEYS)
        if errors:
            return errors
        task_id = task["task_id"]
        if task_id in tasks_by_id:
            errors.append(f"{label}: duplicate task_id")
        tasks_by_id[task_id] = task

        for field in ("historical_pickup_proxy_s", "observed_arrival_s", "physical_release_s", "holder_after"):
            if task.get(field) is not None:
                errors.append(f"{label}: {field} is not policy-visible")
        _check_allowed_keys(errors, f"{label}.source_refs", task["source_refs"],
                            {"plan", "plan_deadline", "process_predecessor", "arrival", "lift", "weight"})
        for key, ref in task["source_refs"].items():
            if not isinstance(ref, str) or (key != "weight" and ref not in refs):
                errors.append(f"{label}: source {key} is not backed by a visible event")
        weight = task["source_refs"].get("weight")
        if isinstance(weight, str):
            rows = {ref.rsplit("|", 1)[0] for ref in weight.split(";")}
            samples = [e for e in weight_events if e["heat_id"] == task["heat_id"]
                       and e["source_ref"].rsplit("|", 1)[0] in rows]
            if {e["source_ref"].rsplit("|", 1)[0] for e in samples} != rows or not samples or (
                task["mass_t"]["lo"] != max(e["weight_t"] for e in samples)
                or task["mass_t"]["hi"] != task["mass_t"]["lo"]
            ):
                errors.append(f"{label}: weighing does not match visible evidence")
        _check_allowed_keys(errors, f"{label}.mass_t", task["mass_t"], {"lo", "hi", "source_status", "basis"})
        _check_allowed_keys(errors, f"{label}.ladle_identity", task["ladle_identity"], {"status", "ladle_id"})
        release = release_evidence(task, events)
        interval = task["release_interval"]
        _check_allowed_keys(errors, f"{label}.release_interval", interval, ("lo_s", "hi_s", "rule", "source_status"))
        if interval["hi_s"] is not None or interval["lo_s"] != (release["value_s"] if release else None):
            errors.append(f"{label}: release interval is not backed by the visible source")
        if task["applied_release_s"] != release:
            errors.append(f"{label}: applied release is not backed by the visible source")

        deadline = plan_delivery_target(task, events)
        if task.get("source_plan_outer_deadline") != deadline:
            errors.append(f"{label}: outer deadline is not backed by the visible plan")
        for field in ("pickup_window", "delivery_window"):
            window = task[field]
            _check_allowed_keys(errors, f"{label}.{field}", window, set(WINDOW_KEYS) | {
                "bound_event_role", "bound_event_id", "bound_source_ref", "bound_event_status",
                "process_deadline_s", "process_deadline_status",
            })
            if window["plan_latest_s"] != (deadline if field == "delivery_window" else None) or any(
                window.get(key) is not None
                for key in ("hard_earliest_s", "hard_latest_s", "plan_earliest_s", "process_deadline_s")
            ):
                errors.append(f"{label}: {field} contains an unsupported time")
            ref = window.get("bound_event_id")
            if ref is not None and (ref not in by_id or by_id[ref]["source_ref"] != window.get("bound_source_ref")):
                errors.append(f"{label}: {field} points outside the visible evidence")
            if window.get("bound_source_ref") and window["bound_source_ref"] not in refs:
                errors.append(f"{label}: {field} cites an unpublished source")
        for p in task["predecessors"]:
            if not isinstance(p, dict) or (set(p) != {"task_id"} and (
                set(p) != {"event", "type"} or p.get("type") != "process_predecessor"
                or not release or p.get("event") != task["source_refs"].get("process_predecessor")
            )):
                errors.append(f"{label}: predecessor includes unsupported evidence")
        for req in task["resource_requirements"] or []:
            _check_allowed_keys(errors, f"{label}.resource_requirements", req,
                                {"resource_id", "role", "demand", "start_event_id", "release_event_id",
                                 "authorization", "source_status", "basis"})
            if not isinstance(req, dict) or any(
                req.get(key) is not None and req[key] not in by_id
                for key in ("start_event_id", "release_event_id")
            ):
                errors.append(f"{label}: resource requirement points to an invisible event")
        for key, expected in visible_crane_evidence(task_id, events, dispatch_ids).items():
            if task.get(key) != expected:
                errors.append(f"{label}.{key}: does not match disclosed crane evidence")
        if task["flags"]:
            errors.append(f"{label}: retrospective flags are not policy-visible")
    if errors:
        return errors
    for event in events:
        for observation in event.get("crane_observations", []):
            target = tasks_by_id.get(observation["task_id"])
            if target is None or target["heat_id"] != event["heat_id"]:
                errors.append("crane observation does not belong to the referenced task")

    _validate_policy_state(policy, by_id, errors)
    if errors:
        return errors
    holders = {h["ladle_id"]: h for h in initial["holders"] if isinstance(h, dict) and "ladle_id" in h}
    for task in policy["tasks"]:
        holder = holders.get(task["ladle_id"], {})
        if (task.get("holder_before") != (holder.get("holder_id") or "unknown")
                or task.get("holder_status") != holder.get("status", "unknown")):
            errors.append("task holder does not match the policy's visible holder state")
    if policy.get("scenario") not in SCENARIOS:
        errors.append(f"invalid scenario {policy.get('scenario')!r}")
    if policy.get("mode") not in MODES:
        errors.append(f"invalid mode {policy.get('mode')!r}")
    try:
        json.dumps(policy, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        errors.append(f"policy_view is not strict JSON: {exc}")
    return errors


def validate_simulator_truth(truth: dict) -> list[str]:
    errors: list[str] = []
    _require_keys(
        errors, "simulator_truth", truth,
        ("schema", "instance_id", "visibility", "now_s", "policy_rule", "task_actuals",
         "future_events", "active_operation_actual_end_s"),
    )
    if errors:
        return errors
    if truth["schema"] != SIMULATOR_SCHEMA:
        errors.append(f"schema must be {SIMULATOR_SCHEMA!r}")
    if truth["visibility"] != "hidden_from_policy":
        errors.append("simulator truth must be marked hidden_from_policy")
    if not _is_int(truth["now_s"]):
        errors.append("simulator_truth.now_s must be an int")
    for key in ("task_actuals", "future_events", "active_operation_actual_end_s"):
        if not isinstance(truth[key], list):
            errors.append(f"simulator_truth.{key} must be a list")
    if errors:
        return errors
    task_ids = set()
    for index, actual in enumerate(truth["task_actuals"]):
        label = f"task_actuals[{index}]"
        _require_keys(errors, label, actual, (
            "task_id", "historical_pickup_proxy_s", "observed_arrival_s",
            "process_predecessor_s", "plan_outer_deadline_s",
        ))
        if not isinstance(actual, dict):
            continue
        task_id = actual.get("task_id")
        if not isinstance(task_id, str) or not task_id or task_id in task_ids:
            errors.append(f"{label}: invalid or duplicate task_id")
        elif task_id:
            task_ids.add(task_id)
        for field in ("historical_pickup_proxy_s", "observed_arrival_s", "process_predecessor_s",
                      "plan_outer_deadline_s", "release_upper_proxy_s"):
            if actual.get(field) is not None and not _is_int(actual[field]):
                errors.append(f"{label}.{field}: expected seconds or null")
        for key in ("observed_crane_candidates", "crane_device_refs", "crane_observations"):
            if key in actual and not isinstance(actual[key], list):
                errors.append(f"{label}.{key}: expected a list")
    by_id = {}
    for index, event in enumerate(truth["future_events"]):
        errors.extend(validate_event(event, index))
        if not isinstance(event, dict):
            continue
        if policy_event_visible(event, truth["now_s"]):
            errors.append(f"future_events[{index}]: event is already visible")
        event_id = event.get("event_id")
        if event_id in by_id:
            errors.append(f"future_events[{index}]: duplicate event_id")
        by_id[event_id] = event
    for index, operation in enumerate(truth["active_operation_actual_end_s"]):
        if not isinstance(operation, dict):
            errors.append(f"active_operation_actual_end_s[{index}]: expected object")
            continue
        source = by_id.get(operation.get("end_event_id"), {})
        if (source.get("event_category") != "actual"
                or source.get("heat_id") != operation.get("heat_id")
                or not _is_int(operation.get("actual_end_s"))
                or source.get("time_s") != operation["actual_end_s"]):
            errors.append(f"active_operation_actual_end_s[{index}]: end differs from hidden actual")
    try:
        json.dumps(truth, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        errors.append(f"simulator_truth is not strict JSON: {exc}")
    return errors


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
