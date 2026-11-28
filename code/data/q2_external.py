"""Review Q2 boundary evidence without turning historical events into releases."""

from __future__ import annotations

import csv
import hashlib
import math
from collections import Counter
from pathlib import Path


def _rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def _known_time(value):
    return value not in (None, "") and math.isfinite(float(value))


def _signed_audit(rows):
    audit_only = {
        "tap_end_plan_error": "Historical actual tap completion minus planned converter end. Phase semantics differ.",
        "cast_start_plan_error": "Historical caster start is an endogenous response to preparation and resource readiness.",
    }
    result = {}
    for metric, reason in audit_only.items():
        values = [float(row["duration_s"]) for row in rows
                  if row["metric"] == metric and _known_time(row["duration_s"])]
        result[metric] = {
            "n": len(values), "negative": sum(x < 0 for x in values),
            "zero": sum(x == 0 for x in values), "positive": sum(x > 0 for x in values),
            "centered": False, "active_exogenous_model": False,
            "raw_location": "approved_private_calibration_csv",
            "reason": reason,
        }
    return result


def external_boundary(processed: Path, calibration: dict) -> dict:
    """Return public-safe evidence and explicit hold/fallback for each boundary."""
    processed = Path(processed)
    paths = {name: processed / name for name in ("events.csv", "calibration.csv")}
    allowed_statuses = {"canonical"}
    allowed_links = {"unique", "resolved_by_identity"}
    kinds = Counter()
    known_visibility = 0
    known_announcement = 0
    known_forecast = 0
    for event in _rows(paths["events.csv"]):
        if (event["event_status"] not in allowed_statuses or event["link_status"] not in allowed_links
                or not _known_time(event["time_s"])):
            continue
        kinds[event["record_type"], event["event"]] += 1
        if event["record_type"] == "actual":
            known_visibility += _known_time(event.get("visible_at_s"))
            known_announcement += _known_time(event.get("announced_at_s"))
        known_forecast += _known_time(event.get("predicted_time_s"))
    rows = list(_rows(paths["calibration.csv"]))
    positives = Counter(
        row["metric"] for row in rows if _known_time(row["duration_s"]) and float(row["duration_s"]) > 0
    )
    stage_specs = [
        {
            "stage": "converter_upstream", "actual_roles": ("tap_start", "tap_end", "bof_lift"),
            "plan_roles": ("plan_bof_start", "plan_bof_end"), "source_status": "derived",
            "separate_duration_metric": "tap_duration", "handoff_time_model": None,
            "hard_release": None, "handoff_capacity": None,
            "fallback": "Use the Q1 declared plan anchor and preparation scenario. "
                        "If the converter operation is modeled, propagate its simulated finish into readiness.",
            "sensitivity": {"q1_preparation_seconds": [60, 120, 180],
                            "combine_with_independent_finish_residual": False},
            "interpretation": "Observed tapping phase starts and stops are support for tapping service only. "
                              "The planned converter phase does not establish a same-semantic tapping start.",
        },
        {
            "stage": "slag_service", "actual_roles": ("slag_start", "slag_end", "slag_second_start",
                                                       "slag_second_end"), "plan_roles": (),
            "source_status": "observed",
            "separate_duration_metric": "slag_duration", "handoff_time_model": None,
            "hard_release": None, "handoff_capacity": None,
            "fallback": "Keep Q1 conditional slag occupancy and retain the holder until a legal downstream transfer. "
                        "Start the declared operation on arrival; its finish is endogenous.",
            "sensitivity": {"q1_service_duration_scenario_keys": ["optimistic", "base", "conservative"]},
            "interpretation": "Same-stage first slag start and end support separate service duration. Neither "
                              "timestamp identifies crane drop completion or external receiving availability.",
        },
        {
            "stage": "transfer_truck_receipt_release",
            "actual_roles": ("transfer_at_ef", "transfer_seat", "transfer_arrive_repair_area"),
            "plan_roles": (), "source_status": "pending",
            "separate_duration_metric": None, "handoff_time_model": None,
            "hard_release": None, "handoff_capacity": None,
            "fallback": "Preserve pending EF confirmation or the Q1 blocked boundary until receiving point, "
                        "next holder and legal release are approved.",
            "sensitivity": {"case_levels": ["blocked", "approved_fixed_external_release"],
                            "numeric_seconds_range": None, "enabled": False},
            "interpretation": "Arrival and seat records evidence observed actions. They do not identify "
                              "the release of an individually authorized transfer resource.",
        },
        {
            "stage": "repair_and_return", "actual_roles": ("repair_seat", "repair_lift", "return_ef_lift",
                                                            "return_transfer_seat", "return_bof_c_seat"),
            "plan_roles": (), "source_status": "pending",
            "separate_duration_metric": None, "handoff_time_model": None,
            "hard_release": None, "handoff_capacity": None,
            "fallback": "Retain blocked or an approved fixed external handoff; preserve real same-ladle "
                        "dependencies, previous occupancy and known routes.",
            "sensitivity": {"case_levels": ["blocked", "approved_fixed_external_release"],
                            "numeric_seconds_range": None, "enabled": False},
            "interpretation": "Repair seat-to-lift can mix occupied service, queues and transfer waits. "
                              "A historical return action does not prove availability in a future decision.",
        },
        {
            "stage": "caster_acceptance_and_preparation",
            "actual_roles": ("cc_seat", "cast_start", "cast_end"), "plan_roles": ("plan_cast_start", "plan_cast_end"),
            "source_status": "derived", "separate_duration_metric": "cast_duration",
            "handoff_time_model": None, "hard_release": None, "handoff_capacity": None,
            "fallback": "Derive seating, preparation and occupancy from the Q1 plan and resource graph. "
                        "Sample the calibrated casting service separately.",
            "sensitivity": {"q1_baseline_caster_preparation_s": 120,
                            "validated_preparation_range_s": None},
            "interpretation": "Seat-to-start mixes preparation and wait. Adding its observed interval or the "
                              "observed start error as new noise would duplicate internal queueing and preparation.",
        },
    ]
    for stage in stage_specs:
        stage["actual_event_counts"] = {role: kinds["actual", role] for role in stage.pop("actual_roles")}
        stage["planned_event_counts"] = {role: kinds["plan", role] for role in stage.pop("plan_roles")}
        metric = stage["separate_duration_metric"]
        if metric:
            stage["duration_evidence"] = {
                "calibration_schema": calibration.get("schema"),
                "metric": metric, "historical_positive_rows": positives[metric],
                "use": "service_duration_only_if_supported_by_q2_calibration_profile",
            }
    return {
        "schema": "mcm-uestc.q2-external-boundary/1",
        "publication_class": "public_summary_only",
        "scope": "external interface evidence and dependency rules; not a strategy observation stream",
        "source_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()},
        "count_rule": "canonical, valid parsed seconds and unique or identity-resolved link",
        "timing_fields": {
            "actual_time_s": "Simulator actual execution clock; historical realized values stay in private audit.",
            "plan_time_s": "Plan target; enforce only with the inherited Q1 evidence and authority.",
            "predicted_time_s": "Conditioned on a currently visible state, with a model version and time issued.",
            "announced_at_s": "Evidence of an announcement; not inferred from historical operation time.",
            "visible_at_s": "Decision availability inherited from Q1; no future-actual leakage.",
        },
        "historical_timing_field_coverage": {
            "actual_with_announced_at": known_announcement,
            "actual_with_visible_at": known_visibility,
            "rows_with_predicted_time": known_forecast,
        },
        "independent_exogenous_timestamp_models_enabled": [],
        "signed_residuals_audit_only": _signed_audit(rows),
        "seat_to_cast_mixed_interval_count_audit_only": positives["cc_seat_to_cast"],
        "boundaries": stage_specs,
    }
