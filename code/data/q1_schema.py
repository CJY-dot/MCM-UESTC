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
SCOPES = {"ef_crane", "external_service", "pending_ef_scope"}
LOAD_STATES = {"loaded", "empty_ladle"}
EVENT_CATEGORIES = {"plan", "actual", "derived"}

TOP_LEVEL_KEYS = (
    "schema", "instance_id", "scenario", "mode", "clock", "plan_version",
    "scope", "evidence", "device_catalog", "topology", "resources",
    "events", "tasks", "initial_state",
)

TASK_KEYS = (
    "task_id", "heat_id", "ladle_id", "campaign_id", "campaign_order",
    "route", "scope", "load_state", "origin", "destination",
    "mass_t", "predecessors", "release_interval",
    "physical_release_s", "historical_pickup_proxy_s", "applied_release_s",
    "pickup_window", "delivery_window",
    "capacity_candidates", "eligible_cranes",
    "resource_requirements", "required", "admission", "blocker_codes",
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
    if task["load_state"] not in LOAD_STATES:
        errors.append(f"{label}: invalid load_state {task['load_state']!r}")
    if task["admission"] not in ADMISSIONS:
        errors.append(f"{label}: invalid admission {task['admission']!r}")
    if task["admission"] == "A" and task["eligible_cranes"] is None:
        errors.append(f"{label}: A task must have a verified eligible_cranes list, not null")
    if task["admission"] == "C" and not task["blocker_codes"]:
        errors.append(f"{label}: C task must record at least one blocker code")
    if task["eligible_cranes"] is not None and not isinstance(task["eligible_cranes"], list):
        errors.append(f"{label}: eligible_cranes must be a list or null")
    if task["capacity_candidates"] is not None and not isinstance(task["capacity_candidates"], list):
        errors.append(f"{label}: capacity_candidates must be a list or null")
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
    if not isinstance(task["predecessors"], list):
        errors.append(f"{label}: predecessors must be a list")
    if not isinstance(task["resource_requirements"], list):
        errors.append(f"{label}: resource_requirements must be a list")
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
    if event["time_s"] is not None and event["visible_at_s"] is not None:
        if event["visible_at_s"] > event["time_s"]:
            errors.append(f"{label}: event visible after it occurs")
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
    if not isinstance(instance["device_catalog"], list):
        errors.append("device_catalog must be a list")
    if not isinstance(instance["resources"], list):
        errors.append("resources must be a list")
    if not isinstance(instance["events"], list) or not isinstance(instance["tasks"], list):
        errors.append("events and tasks must be lists")
        return errors

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
        for key in ("predecessors",):
            for predecessor in task.get(key, []):
                if isinstance(predecessor, str) and predecessor not in task_ids | event_ids:
                    errors.append(f"tasks[{index}].{key}: unknown reference {predecessor!r}")
                if (isinstance(predecessor, dict) and "task_id" in predecessor
                        and predecessor.get("task_id") not in task_ids):
                    errors.append(
                        f"tasks[{index}].{key}: unknown task reference "
                        f"{predecessor.get('task_id')!r}"
                    )
    try:
        json.dumps(instance, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        errors.append(f"instance is not strict JSON: {exc}")
    return errors


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
