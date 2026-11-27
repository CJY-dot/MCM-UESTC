"""Schema and validation for the Stage 03 Q1 standardized dispatch input.

The schema follows ``mcm-uestc.dispatch/1`` as defined in
``stages/02_modeling/implementation_contract.md``.  It deliberately keeps
``null`` (unknown) distinct from ``[]`` (confirmed empty) and from ``0``.
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA = "mcm-uestc.dispatch/1"

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


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool))


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


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
