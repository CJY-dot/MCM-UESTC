"""Build the Stage 03 Q1 standardized dispatch input from Stage 02 outputs.

The builder is deliberately conservative:

* it consumes the corrected, byte-checked Stage 02 event/task tables;
* it never invents coordinates, speeds, crane positions or hard windows;
* it keeps unknown values as ``null`` and confirmed-empty candidate sets as ``[]``;
* it applies only the three project-approved ladle-identity mappings;
* it writes production-level detail only under ``problem/data/processed/`` (git-ignored).

The public output is an ``mcm-uestc.dispatch/1`` instance plus flat event/task
tables, a coverage audit, a parameter-evidence table and Q1 window candidates.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from q1_schema import (
    POLICY_SCHEMA, SCHEMA, SIMULATOR_SCHEMA,
    validate_instance, validate_policy_view, validate_simulator_truth,
)

BUILDER_VERSION = "stage03-q1-b/1.2.0"
ROOT = Path(__file__).resolve().parents[2]
APPROVED_AUDIT = ROOT / "problem/data/processed/stage02_b_ladle_identity_approved/audit.json"

EF_CRANES = (
    {"crane_id": "1#150", "capacity_t": 150, "dispatch_candidate": False,
     "evidence": "figure 1 nominal crane; no operating record in the 12 workbooks"},
    {"crane_id": "2#150", "capacity_t": 150, "dispatch_candidate": False,
     "evidence": "figure 1 nominal crane; no operating record in the 12 workbooks"},
    {"crane_id": "3#450", "capacity_t": 450, "dispatch_candidate": True,
     "evidence": "crane labels in LF/RH/CC handling records"},
    {"crane_id": "4#450", "capacity_t": 450, "dispatch_candidate": True,
     "evidence": "crane labels in LF/RH/CC handling records"},
    {"crane_id": "5#450", "capacity_t": 450, "dispatch_candidate": True,
     "evidence": "crane labels in LF/RH/CC handling records"},
)
CRANE_BY_ID = {crane["crane_id"]: crane for crane in EF_CRANES}
DISPATCH_IDS = {crane["crane_id"] for crane in EF_CRANES if crane["dispatch_candidate"]}

DEVICE_STATIONS = {
    "1#LF.xlsx": "LF1", "3#LF.xlsx": "LF3",
    "1#RH.xlsx": "RH1", "2#RH.xlsx": "RH2",
    "1#连铸.xlsx": "CC1", "2#连铸.xlsx": "CC2", "3#连铸.xlsx": "CC3",
}

# Figure 1 relative order only.  LF2 is documented but excluded from current
# operations; it is left out of the EF station list rather than renumbered.
EF_STATIONS = {
    "LF3": {"family": "LF", "side": "E", "relative_order": 1},
    "CC1": {"family": "CC", "side": "E", "relative_order": 2},
    "CC2": {"family": "CC", "side": "E", "relative_order": 3},
    "CC3": {"family": "CC", "side": "E", "relative_order": 4},
    "RH2": {"family": "RH", "side": "F", "relative_order": 1},
    "RH1": {"family": "RH", "side": "F", "relative_order": 2},
    "LF1": {"family": "LF", "side": "F", "relative_order": 4},
}
SCOPE_STATUSES = {"confirmed", "pending_ef_confirmation", "external_boundary"}
PLAN_FILE = "出钢计划表单.xls"
MASTER_FILE = "工序时间信息.xlsx"

LOADED_LEGS = {
    "BOF_to_LF", "BOF_to_RH", "LF_to_RH", "RH_to_LF", "LF_to_CC", "RH_to_CC",
}
EMPTY_EF_LEGS = {"CC_to_SLAG"}
PENDING_EF_LEGS = {"SLAG_to_TRANSFER"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_int(value) -> int | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def parse_float(value) -> float | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def iso(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone().isoformat(
        sep=" ", timespec="seconds"
    )


def station(raw: str, fallback_family: str = "") -> tuple[str | None, str, bool]:
    """Return (canonical station id, family, known). Unknown is (None, family, False)."""
    text = str(raw or "").strip()
    upper = text.upper()
    if not text:
        return None, fallback_family, False
    if "UNKNOWN" in upper:
        if "BOF_RETURN" in upper:
            return None, "BOF_RETURN", False
        prefix = re.match(r"(BOF|LF|RH|CC|TRANSFER|REPAIR|SLAG)", upper)
        return None, (prefix.group(1) if prefix else fallback_family), False
    match = re.fullmatch(r"BOF[_\s]*([ABC])", upper)
    if match:
        return f"BOF_{match.group(1)}", "BOF", True
    match = re.fullmatch(r"(LF|RH|CC)\s*(\d+)", upper)
    if match:
        return f"{match.group(1)}{match.group(2)}", match.group(1), True
    match = re.fullmatch(r"(\d+)#倒渣位", text)
    if match:
        return f"SLAG:{match.group(1)}#", "SLAG", True
    match = re.fullmatch(r"REPAIR(\d+)", upper)
    if match:
        return f"REPAIR:{match.group(1)}#", "REPAIR", True
    if upper in {"TRANSFER", "TRANSFER:UNKNOWN"}:
        return None, "TRANSFER", False
    if upper in {"BOF_RETURN:UNKNOWN", "BOF_RETURN"}:
        return None, "BOF_RETURN", False
    if upper in {"BOFC"}:
        return "BOF_C", "BOF", True
    return None, fallback_family, False


def family_of(raw: str) -> str:
    text = str(raw or "").upper()
    if text.startswith("BOF"):
        return "BOF"
    if text.startswith("LF"):
        return "LF"
    if text.startswith("RH"):
        return "RH"
    if text.startswith("CC"):
        return "CC"
    if "倒渣" in text or text.startswith("SLAG"):
        return "SLAG"
    if text.startswith("REPAIR") or "热修" in text:
        return "REPAIR"
    if "TRANSFER" in text or "过渡" in text:
        return "TRANSFER"
    return ""


class SourceIndex:
    """Read source workbook rows for crane labels without changing the original files."""

    def __init__(self, raw_dir: Path | None):
        self.raw_dir = raw_dir
        self._tables: dict[tuple[str, str], pd.DataFrame] = {}

    def _table(self, file: str, sheet: str) -> pd.DataFrame:
        if self.raw_dir is None:
            return pd.DataFrame()
        key = (file, sheet)
        if key not in self._tables:
            path = self.raw_dir / file
            engine = "calamine" if path.suffix.lower() == ".xls" else "openpyxl"
            with pd.ExcelFile(path, engine=engine) as workbook:
                self._tables[key] = pd.read_excel(
                    workbook, sheet_name=sheet, dtype=object
                )
        return self._tables[key]

    def row(self, file: str, sheet: str, row_number: int | None) -> dict:
        if not self.raw_dir or row_number is None:
            return {}
        index = row_number - 2
        table = self._table(file, sheet)
        if index < 0 or index >= len(table):
            return {}
        return {str(column): value for column, value in table.iloc[index].items()}

    def crane_labels(self, file: str, sheet: str, row_number: int | None) -> tuple[str, str]:
        row = self.row(file, sheet, row_number)
        return (
            str(row.get("放包天车") or "").strip(),
            str(row.get("吊包天车") or "").strip(),
        )


def parse_source_ref(ref: str) -> tuple[str, str, int | None, str]:
    parts = str(ref or "").split("|")
    if len(parts) < 3:
        return "", "", None, ""
    return parts[0], parts[1], parse_int(parts[2]), parts[3] if len(parts) > 3 else ""


def heat_device_rows(events: pd.DataFrame) -> dict[str, dict[str, list[tuple[str, str, int]]]]:
    """Map heat -> exact station -> source rows.

    LF1/LF3 and RH1/RH2 are kept separate.  A family-level lookup is not
    sufficient and would attach the wrong device row to a task.
    """
    grouped: dict[tuple[str, str], set[tuple[str, str, int]]] = defaultdict(set)
    for _, event in events.iterrows():
        file = str(event.get("source_file", ""))
        station_id = DEVICE_STATIONS.get(file)
        if not station_id:
            continue
        heat = str(event.get("heat_id", ""))
        row = parse_int(event.get("source_row"))
        if not heat or row is None:
            continue
        grouped[(heat, station_id)].add((file, str(event.get("source_sheet", "")), row))
    result: dict[str, dict[str, list[tuple[str, str, int]]]] = defaultdict(dict)
    for (heat, station_id), rows in grouped.items():
        result[heat][station_id] = sorted(rows)
    return result


def task_crane_evidence(
    task_row: dict,
    device_rows: dict[str, dict[str, list[tuple[str, str, int]]]],
    source_index: SourceIndex,
) -> dict:
    """Return historical crane evidence for the exact task station.

    A family-level row is never used.  Multiple distinct labels at the same
    station are a conflict, not a reason to pick one.
    """
    heat = str(task_row.get("heat_id", ""))
    loaded = str(task_row.get("loaded_state")) == "loaded"
    origin_id, origin_family, _ = station(task_row.get("origin", ""))
    destination_id, destination_family, _ = station(task_row.get("destination", ""))
    target_station = destination_id if loaded else origin_id
    target_family = destination_family if loaded else origin_family
    if target_family not in {"LF", "RH", "CC"} or not target_station:
        return {
            "observed_crane": None, "observed_crane_candidates": [],
            "crane_evidence_status": "missing", "scope_basis": "process_interface_inferred",
            "device_refs": [],
        }
    rows = device_rows.get(heat, {}).get(target_station, [])
    if not rows:
        return {
            "observed_crane": None, "observed_crane_candidates": [],
            "crane_evidence_status": "missing", "scope_basis": "process_interface_inferred",
            "device_refs": [],
        }
    candidates: set[str] = set()
    refs = []
    for file, sheet, row in rows:
        release, lift = source_index.crane_labels(file, sheet, row)
        refs.append(f"{file}|{sheet}|{row}")
        # Placement labels belong to loaded arrivals; lift labels belong to
        # empty-ladle pickups.  Do not union the two operations.
        value = release if loaded else lift
        cleaned = str(value or "").strip()
        if cleaned and cleaned.lower() not in {"nan", "none"}:
            candidates.add(cleaned)
    if len(candidates) > 1:
        return {
            "observed_crane": None, "observed_crane_candidates": sorted(candidates),
            "crane_evidence_status": "conflict", "scope_basis": "device_crane_label",
            "device_refs": refs,
        }
    observed = next(iter(candidates), None)
    return {
        "observed_crane": observed,
        "observed_crane_candidates": sorted(candidates),
        "crane_evidence_status": "confirmed" if observed else "missing",
        "scope_basis": "device_crane_label" if observed else "process_interface_inferred",
        "device_refs": refs,
    }


def build_events(raw_events: pd.DataFrame, source_to_tasks: dict[str, list[str]]) -> list[dict]:
    rows = []
    for _, event in raw_events.iterrows():
        heat = str(event.get("heat_id", ""))
        event_type = str(event.get("event", ""))
        time_s = parse_int(event.get("time_s"))
        if not heat or not event_type or time_s is None:
            continue
        source_ref = "|".join(
            str(event.get(key, ""))
            for key in ("source_file", "source_sheet", "source_row", "source_column")
        )
        event_status = str(event.get("event_status", ""))
        record_type = str(event.get("record_type", ""))
        status = "observed" if event_status in {"canonical", "agrees"} else "pending"
        equipment_raw = str(event.get("equipment", ""))
        resource_id, family, known = station(equipment_raw)
        rows.append({
            "event_id": "",
            "event_type": event_type,
            "event_category": "plan" if record_type == "plan" else "actual",
            "heat_id": heat,
            "ladle_id": str(event.get("ladle_id", "")) or None,
            "task_id": None,
            "task_ids": list(source_to_tasks.get(source_ref, [])),
            "resource_id": resource_id,
            "resource_family": family,
            "resource_known": known,
            "resource_label": equipment_raw,
            "time_s": time_s,
            "visible_at_s": time_s if record_type != "plan" else None,
            "sequence": 0,
            "sequence_basis": "time_then_source; ties do not assert physical precedence",
            "source_status": status,
            "source_ref": source_ref,
            "source_event_status": event_status,
            "link_status": str(event.get("link_status", "")),
            "usable_for_canonical": event_status in {"canonical", "agrees"},
            "weight_t": parse_float(event.get("gross_weight_t")),
            "raw": {
                "file": str(event.get("source_file", "")),
                "sheet": str(event.get("source_sheet", "")),
                "row": parse_int(event.get("source_row")),
                "column": str(event.get("source_column", "")),
                "column_index": parse_int(event.get("source_column_index")),
                "engine": str(event.get("source_engine", "")),
                "tap_mark": str(event.get("tap_mark", "")),
                "temporary_ladle_id": str(event.get("temporary_ladle_id", "")),
                "raw_time": str(event.get("raw_time", "")),
                "time_status": str(event.get("time_status", "")),
            },
            "_sort": (time_s, str(event.get("source_file", "")), parse_int(event.get("source_row")) or 0,
                      parse_int(event.get("source_column_index")) or 0),
        })
    rows.sort(key=lambda item: (item["heat_id"], item["_sort"]))
    counter: Counter[str] = Counter()
    for row in rows:
        counter[row["heat_id"]] += 1
        row["sequence"] = counter[row["heat_id"]]
        row["event_id"] = f"EV:{row['heat_id']}:{row['sequence']:05d}"
        row["task_id"] = row["task_ids"][0] if row["task_ids"] else None
        del row["_sort"]
    return rows


def load_approved_corrections(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    corrections = data.get("identity_corrections")
    return corrections if isinstance(corrections, list) else []


def heat_identity_map(events: pd.DataFrame, corrections: list[dict]) -> dict[str, dict]:
    """Resolve heat-level ladle identity without majority voting.

    The approved corrections are the only authoritative overrides.  Other
    heats are unique only when all canonical/agreeing records point to one
    physical ladle.  Multiple incompatible values are retained as a conflict;
    no id is selected.
    """
    approved: dict[str, dict] = {}
    approved_sources: dict[tuple[str, str, int, int], dict] = {}
    for entry in corrections:
        heat = str(entry.get("heat_id", ""))
        source = entry.get("source", {})
        key = (
            str(source.get("workbook", "")), str(source.get("sheet", "")),
            int(source.get("row", 0) or 0), int(source.get("column", 0) or 0),
        )
        approved[heat] = entry
        approved_sources[key] = entry

    by_heat: dict[str, list[dict]] = defaultdict(list)
    for _, event in events.iterrows():
        heat = str(event.get("heat_id", ""))
        ladle = str(event.get("ladle_id", "")).strip()
        if not heat or not ladle:
            continue
        by_heat[heat].append({
            "ladle_id": ladle,
            "temporary_ladle_id": str(event.get("temporary_ladle_id", "")).strip(),
            "source_file": str(event.get("source_file", "")),
            "source_sheet": str(event.get("source_sheet", "")),
            "source_row": parse_int(event.get("source_row")),
            "source_column": str(event.get("source_column", "")),
            "event_status": str(event.get("event_status", "")),
            "link_status": str(event.get("link_status", "")),
            "approved": (
                str(event.get("source_file", "")), str(event.get("source_sheet", "")),
                parse_int(event.get("source_row")) or 0,
                parse_int(event.get("source_column_index")) or 0,
            ) in approved_sources,
        })

    result: dict[str, dict] = {}
    for heat, rows in by_heat.items():
        canonical = [row for row in rows if row["event_status"] in {"canonical", "agrees"}]
        canonical_ids = sorted({row["ladle_id"] for row in canonical})
        all_ids = sorted({row["ladle_id"] for row in rows})
        entry = approved.get(heat)
        candidates = []
        for ladle in all_ids:
            selected = [row for row in rows if row["ladle_id"] == ladle]
            candidates.append({
                "ladle_id": ladle,
                "record_count": len(selected),
                "canonical_count": sum(
                    row["event_status"] in {"canonical", "agrees"} for row in selected
                ),
                "namespaces": sorted({row["source_file"] for row in selected}),
                "source_refs": [
                    f"{row['source_file']}|{row['source_sheet']}|{row['source_row']}|"
                    f"{row['source_column']}"
                    for row in selected
                ],
                "temporary_ladle_ids": sorted({
                    row["temporary_ladle_id"] for row in selected if row["temporary_ladle_id"]
                }),
                "project_approved": any(row["approved"] for row in selected),
            })
        unresolved = sorted({
            row["ladle_id"] for row in rows
            if row["event_status"] not in {"canonical", "agrees"}
            and row["ladle_id"] not in canonical_ids
        })
        if entry is not None:
            normalized = str(entry.get("normalized_value", ""))
            if canonical_ids == [normalized] or not canonical_ids:
                status = "project_approved"
                ladle_id = normalized
            else:
                status = "identity_conflict"
                ladle_id = None
        elif len(canonical_ids) == 1:
            status = "unique"
            ladle_id = canonical_ids[0]
        elif len(canonical_ids) > 1:
            status = "identity_conflict"
            ladle_id = None
        else:
            status = "unknown"
            ladle_id = None
        result[heat] = {
            "ladle_id": ladle_id,
            "status": status,
            "canonical_ids": canonical_ids,
            "all_ids": all_ids,
            "unresolved_raw_ids": unresolved,
            "candidates": candidates,
            "approved_source_refs": [
                {
                    "workbook": entry["source"].get("workbook"),
                    "sheet": entry["source"].get("sheet"),
                    "row": entry["source"].get("row"),
                    "column": entry["source"].get("column"),
                    "raw_value": entry.get("raw_value"),
                    "normalized_value": entry.get("normalized_value"),
                    "authority_ref": entry.get("authority_ref"),
                }
            ] if entry else [],
        }
    return result


def build_tasks(
    raw_tasks: pd.DataFrame,
    device_rows: dict[str, dict[str, list[tuple[str, str, int]]]],
    source_index: SourceIndex,
    heat_identities: dict[str, dict],
) -> list[dict]:
    tasks = []
    for _, row in raw_tasks.iterrows():
        task_id = str(row.get("task_id", ""))
        heat = str(row.get("heat_id", ""))
        if not task_id or not heat:
            continue
        leg = str(row.get("leg", ""))
        old_scope = str(row.get("scope", ""))
        origin_id, origin_family, origin_known = station(row.get("origin", ""))
        destination_id, destination_family, destination_known = station(row.get("destination", ""))
        loaded = str(row.get("loaded_state")) == "loaded"
        flags = [flag for flag in str(row.get("flags", "")).split(";") if flag]
        process_lo = parse_int(row.get("process_predecessor_s"))
        lift_hi = parse_int(row.get("release_proxy_s")) or parse_int(row.get("observed_lift_s"))
        arrival = parse_int(row.get("observed_arrival_s"))
        plan_outer = parse_int(row.get("plan_outer_deadline_s"))
        mass = parse_float(row.get("gross_weight_t"))
        release_conflict = process_lo is not None and lift_hi is not None and lift_hi < process_lo
        if release_conflict:
            flags.append("release_upper_below_lower")
        if arrival is not None and lift_hi is not None and arrival < lift_hi:
            flags.append("observed_arrival_before_lift")
        if old_scope == "interface":
            scope = "external_service"
            scope_status = "external_boundary"
        elif leg in PENDING_EF_LEGS:
            scope = "ef_crane"
            scope_status = "pending_ef_confirmation"
        else:
            scope = "ef_crane"
            scope_status = "confirmed"
        crane_evidence = task_crane_evidence(row.to_dict(), device_rows, source_index)
        observed_crane = crane_evidence["observed_crane"]
        scope_basis = (
            crane_evidence["scope_basis"] if observed_crane
            else "process_interface_inferred"
        )
        source_refs = {
            "plan": str(row.get("source_plan", "")),
            "process_predecessor": str(row.get("source_process_predecessor", "")),
            "lift": str(row.get("source_lift", "")),
            "arrival": str(row.get("source_arrival", "")),
            "weight": str(row.get("source_weight", "")),
            "plan_deadline": str(row.get("source_plan_outer_deadline", "")),
        }
        capacity_ids = None if mass is None else [
            crane["crane_id"] for crane in EF_CRANES
            if mass <= crane["capacity_t"]
        ]
        if mass is None:
            eligible = None
            eligibility_status = "mass_unverified"
        elif capacity_ids == []:
            eligible = []
            eligibility_status = "no_capacity_compatible_crane"
        else:
            eligible = None
            eligibility_status = "station_reachability_and_permission_unverified"
        delivery_window = {
            "hard_earliest_s": None,
            "hard_latest_s": None,
            "plan_earliest_s": None,
            "plan_latest_s": plan_outer if destination_family == "CC" else None,
            "process_deadline_s": None,
            "process_deadline_status": "unverified",
            "bound_event_role": "drop_complete_or_seat_complete",
            "bound_source_ref": source_refs.get("arrival", ""),
            "bound_event_id": None,
            "applicability": "active" if destination_family == "CC" and plan_outer is not None else "unknown",
            "source_status": "derived" if destination_family == "CC" and plan_outer is not None else "pending",
            "basis": (
                "plan cast start used as an outer delivery target; seat lead not evidenced"
                if destination_family == "CC" and plan_outer is not None
                else "no evidenced delivery window"
            ),
        }
        pickup_window = {
            "hard_earliest_s": None,
            "hard_latest_s": None,
            "plan_earliest_s": None,
            "plan_latest_s": None,
            "process_deadline_s": None,
            "process_deadline_status": "unverified",
            "bound_event_role": "pickup_start",
            "bound_source_ref": source_refs.get("lift", ""),
            "bound_event_id": None,
            "applicability": "unknown",
            "source_status": "pending",
            "basis": "no physical pickup window evidence; release interval is separate",
        }
        release_interval = {
            "lo_s": process_lo,
            "hi_s": lift_hi if not release_conflict else None,
            "rule": "process predecessor completion as lower bound; historical lift as upper proxy",
            "source_status": "derived" if process_lo is not None else "pending",
        }
        predecessors = []
        if str(row.get("predecessor_task_id", "")):
            predecessors.append({"task_id": str(row.get("predecessor_task_id"))})
        if process_lo is not None:
            predecessors.append({
                "event": str(row.get("source_process_predecessor", "")) or None,
                "type": "process_predecessor",
            })
        tasks.append({
            "task_id": task_id,
            "heat_id": heat,
            "ladle_id": heat_identities.get(heat, {}).get("ladle_id"),
            "ladle_identity": heat_identities.get(heat, {
                "ladle_id": None, "status": "unknown", "canonical_ids": [],
                "all_ids": [], "unresolved_raw_ids": [], "candidates": [],
                "approved_source_refs": [],
            }),
            "campaign_id": str(row.get("campaign", "")) or None,
            "campaign_order": str(row.get("plan_order", "")) or None,
            "route": str(row.get("route", "")) or None,
            "leg": leg,
            "scope": scope,
            "scope_status": scope_status,
            "scope_basis": scope_basis,
            "load_state": "loaded" if loaded else "empty_ladle",
            "origin": origin_id,
            "destination": destination_id,
            "origin_label": str(row.get("origin", "")),
            "destination_label": str(row.get("destination", "")),
            "origin_family": origin_family,
            "destination_family": destination_family,
            "origin_known": origin_known,
            "destination_known": destination_known,
            "mass_t": {
                "lo": mass,
                "hi": mass,
                "source_status": "observed" if mass is not None else "pending",
                "basis": str(row.get("weight_status", "")) or "unknown",
            },
            "predecessors": predecessors,
            "predecessor_task_id": str(row.get("predecessor_task_id", "")) or None,
            "release_interval": release_interval,
            "physical_release_s": None,
            "historical_pickup_proxy_s": lift_hi,
            "observed_arrival_s": arrival,
            "applied_release_s": (
                {"value_s": process_lo, "type": "process_predecessor_lower_bound",
                 "source_status": "derived"}
                if process_lo is not None else None
            ),
            "pickup_window": pickup_window,
            "delivery_window": delivery_window,
            "capacity_candidates": capacity_ids,
            "eligible_cranes": eligible,
            "eligibility_status": eligibility_status,
            "observed_crane": observed_crane,
            "observed_crane_candidates": crane_evidence["observed_crane_candidates"],
            "crane_evidence_status": crane_evidence["crane_evidence_status"],
            "observed_crane_dispatch_candidate": (
                observed_crane in DISPATCH_IDS if observed_crane else None
            ),
            "resource_requirements": None,
            "resource_requirement_status": "unverified",
            "required_in_process": True,
            "required_in_ef_scope": (
                None if scope_status == "pending_ef_confirmation"
                else scope == "ef_crane"
            ),
            "required": True,
            "admission": "C",
            "blocker_codes": [],
            "source_status": "pending" if flags else "observed",
            "source_refs": source_refs,
            "source_plan_outer_deadline": plan_outer,
            "link_status": str(row.get("link_status", "")),
            "flags": sorted(set(flags)),
            "holder_before": None,
            "holder_after": None,
            "holder_status": "unknown",
            "crane_device_refs": crane_evidence["device_refs"],
        })
    return tasks


def attach_resource_requirements(tasks: list[dict], events: list[dict]) -> None:
    """Attach evidenced station occupancy requirements to tasks.

    Unknown requirements stay null.  A task with a known origin/destination
    gets one requirement per station; the event links are kept when the source
    event exists.  This is a derived requirement, not a capacity claim.
    """
    by_task: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        for task_id in event.get("task_ids", []):
            by_task[task_id].append(event)

    for task in tasks:
        stations = []
        if task["origin"]:
            stations.append(("origin", task["origin"], task["source_refs"].get("lift", "")))
        if task["destination"]:
            stations.append(("destination", task["destination"], task["source_refs"].get("arrival", "")))
        if not stations:
            task["resource_requirements"] = None
            task["resource_requirement_status"] = "unverified"
            continue
        requirements = []
        for role, resource_id, source_ref in stations:
            linked = [
                event["event_id"] for event in by_task.get(task["task_id"], [])
                if event["source_ref"] == source_ref
                or event["event_type"] in {"release_lower_bound", "historical_pickup_proxy"}
                and role == "origin"
            ]
            requirements.append({
                "resource_id": resource_id,
                "role": role,
                "demand": 1,
                "start_event_id": linked[0] if linked else None,
                "release_event_id": None,
                "authorization": "unknown",
                "source_status": "derived",
                "basis": "task origin/destination station; capacity and release rule unverified",
            })
        task["resource_requirements"] = requirements
        task["resource_requirement_status"] = "derived"


def attach_window_bindings(tasks: list[dict], events: list[dict]) -> None:
    """Link pickup/delivery windows to the corresponding source event when unique."""
    by_ref: dict[tuple[str, str], list[str]] = defaultdict(list)
    for event in events:
        if (event.get("source_ref") and event.get("usable_for_canonical")
                and event.get("event_category") != "derived"):
            by_ref[(event["heat_id"], event["source_ref"])].append(event["event_id"])
    for task in tasks:
        for key in ("pickup_window", "delivery_window"):
            window = task[key]
            ref = window.get("bound_source_ref", "")
            ids = by_ref.get((task["heat_id"], ref), [])
            window["bound_event_id"] = ids[0] if len(ids) == 1 else None
            if ids and len(ids) > 1:
                window["bound_event_status"] = "multiple_source_events"
            else:
                window["bound_event_status"] = "bound" if len(ids) == 1 else "no_source_event"


def add_derived_events(events: list[dict], tasks: list[dict]) -> None:
    existing = {(event["event_id"]) for event in events}
    for task in tasks:
        lo = task["release_interval"]["lo_s"]
        if lo is not None:
            event_id = f"DEV:{task['task_id']}:release_lower_bound"
            if event_id not in existing:
                events.append({
                    "event_id": event_id,
                    "event_type": "release_lower_bound",
                    "event_category": "derived",
                    "heat_id": task["heat_id"],
                    "ladle_id": task["ladle_id"],
                    "task_id": task["task_id"],
                    "task_ids": [task["task_id"]],
                    "resource_id": task["origin"],
                    "resource_family": task["origin_family"],
                    "resource_known": task["origin_known"],
                    "resource_label": task["origin_label"],
                    "time_s": lo,
                    "visible_at_s": lo,
                    "sequence": 0,
                    "sequence_basis": "derived from task release interval",
                    "source_status": "derived",
                    "source_ref": task["source_refs"].get("process_predecessor", ""),
                    "source_event_status": "derived",
                    "link_status": task["link_status"],
                    "usable_for_canonical": False,
                    "weight_t": None,
                    "raw": {"rule": "process predecessor completion"},
                })
                existing.add(event_id)
        hi = task["release_interval"]["hi_s"]
        if hi is not None:
            event_id = f"DEV:{task['task_id']}:pickup_proxy"
            if event_id not in existing:
                events.append({
                    "event_id": event_id,
                    "event_type": "historical_pickup_proxy",
                    "event_category": "derived",
                    "heat_id": task["heat_id"],
                    "ladle_id": task["ladle_id"],
                    "task_id": task["task_id"],
                    "task_ids": [task["task_id"]],
                    "resource_id": task["origin"],
                    "resource_family": task["origin_family"],
                    "resource_known": task["origin_known"],
                    "resource_label": task["origin_label"],
                    "time_s": hi,
                    "visible_at_s": hi,
                    "sequence": 0,
                    "sequence_basis": "derived from historical lift observation",
                    "source_status": "observed",
                    "source_ref": task["source_refs"].get("lift", ""),
                    "source_event_status": "historical_proxy",
                    "link_status": task["link_status"],
                    "usable_for_canonical": False,
                    "weight_t": None,
                    "raw": {"rule": "historical pickup used only as an upper proxy"},
                })
                existing.add(event_id)


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


def build_holder_chain(events: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Build holder transitions without using event_id to break same-time ties.

    A same-time group with different candidate holders is recorded as
    ambiguous.  ``slag_start`` is an operation-start proxy, not a confirmed
    physical handoff, and is marked conditional.
    """
    by_ladle: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        if not event["ladle_id"] or not event["usable_for_canonical"]:
            continue
        if event["event_type"] not in HOLDER_ROLES:
            continue
        by_ladle[event["ladle_id"]].append(event)

    chain_rows = []
    statuses: dict[str, str] = {}
    for ladle, items in by_ladle.items():
        items.sort(key=lambda item: item["time_s"])
        groups: list[list[dict]] = []
        for event in items:
            if groups and groups[-1][0]["time_s"] == event["time_s"]:
                groups[-1].append(event)
            else:
                groups.append([event])
        holder = None
        chain_status = "complete"
        for group in groups:
            transitions = []
            for event in group:
                kind, conditional = HOLDER_ROLES[event["event_type"]]
                transitions.append((event, _holder_after(kind, event), kind, conditional))
            values = [value for _, value, _, _ in transitions]
            event_types = {event["event_type"] for event in group}
            known_values = {value for value in values if "unknown" not in value}
            ambiguous = False
            if len(event_types) == 1:
                if len(known_values) > 1:
                    ambiguous = True
                elif len(known_values) == 1:
                    chosen = next(iter(known_values))
                elif len(set(values)) == 1:
                    chosen = values[0]
                else:
                    ambiguous = True
            elif len(set(values)) > 1:
                ambiguous = True
            else:
                chosen = values[0]
            if holder is None and group[0]["event_type"] not in {"bof_empty_car", "bof_lift"}:
                chain_status = "partial"
            if ambiguous:
                chain_status = "ambiguous"
                for event, value, kind, _ in transitions:
                    chain_rows.append({
                        "ladle_id": ladle,
                        "heat_id": event["heat_id"],
                        "event_id": event["event_id"],
                        "event_type": event["event_type"],
                        "time_s": event["time_s"],
                        "holder_before": holder or "unknown",
                        "holder_after": "HOLDER:ambiguous",
                        "holder_type": "ambiguous",
                        "chain_status": "ambiguous",
                        "holder_basis": "same-time conflict; no event_id tie-break",
                        "ambiguous_event_ids": [item["event_id"] for item in group],
                        "source_ref": event["source_ref"],
                    })
                holder = "HOLDER:ambiguous"
                continue
            for event, value, kind, conditional in transitions:
                if conditional:
                    chain_status = "conditional"
                chain_rows.append({
                    "ladle_id": ladle,
                    "heat_id": event["heat_id"],
                    "event_id": event["event_id"],
                    "event_type": event["event_type"],
                    "time_s": event["time_s"],
                    "holder_before": holder or "unknown",
                    "holder_after": chosen,
                    "holder_type": kind,
                    "chain_status": chain_status,
                    "holder_basis": (
                        "operation_start_proxy" if conditional
                        else "physical_event" if len(event_types) == 1
                        else "corroborated_same_time_group"
                    ),
                    "ambiguous_event_ids": [],
                    "source_ref": event["source_ref"],
                })
            holder = chosen
        statuses[ladle] = chain_status
    return chain_rows, statuses


def attach_task_holders(tasks: list[dict], chain_rows: list[dict]) -> None:
    by_ladle: dict[str, list[dict]] = defaultdict(list)
    for row in chain_rows:
        by_ladle[row["ladle_id"]].append(row)
    for rows in by_ladle.values():
        rows.sort(key=lambda row: row["time_s"])
    for task in tasks:
        ladle = task["ladle_id"]
        lo = task["release_interval"]["lo_s"]
        before = "unknown"
        status = "unknown"
        if ladle and ladle in by_ladle and lo is not None:
            groups: dict[int, list[dict]] = defaultdict(list)
            for row in by_ladle[ladle]:
                if row["time_s"] <= lo:
                    groups[row["time_s"]].append(row)
            if groups:
                last_time = max(groups)
                last_rows = groups[last_time]
                values = {row["holder_after"] for row in last_rows}
                statuses = {row["chain_status"] for row in last_rows}
                if "ambiguous" in statuses or len(values) > 1:
                    status = "ambiguous"
                    before = "HOLDER:ambiguous"
                else:
                    before = last_rows[-1]["holder_after"]
                    status = last_rows[-1]["chain_status"]
        task["holder_before"] = before
        task["holder_after"] = task["destination"] or "unknown"
        task["holder_status"] = status
        if task["holder_status"] == "unknown" and task["origin"]:
            task["holder_before"] = task["origin"]
            task["holder_status"] = "derived_from_task_origin"


def task_blockers(task: dict) -> list[str]:
    blockers = []
    identity_status = task.get("ladle_identity", {}).get("status", "unknown")
    if identity_status not in {"unique", "project_approved"}:
        blockers.append("identity_conflict")
    if "ambiguous_or_conflicting_source" in task.get("flags", []):
        blockers.append("identity_conflict")
    if task["scope_status"] == "pending_ef_confirmation":
        blockers.append("ef_scope_unconfirmed")
    if task["scope"] == "external_service" and not task["source_refs"].get("arrival"):
        blockers.append("boundary_receipt_unverified")
    if not task["origin_known"]:
        blockers.append("unknown_origin_station")
    if not task["destination_known"]:
        blockers.append("unknown_destination_station")
    if task["mass_t"]["hi"] is None:
        blockers.append("unknown_mass")
    if task["release_interval"]["lo_s"] is None:
        blockers.append("missing_release_lower_bound")
    if "release_upper_below_lower" in task["flags"]:
        blockers.append("release_upper_below_lower")
    if "observed_arrival_before_lift" in task["flags"]:
        blockers.append("observed_arrival_before_lift")
    if task["link_status"] not in {"unique", "resolved_by_identity", "resolved_by_temporary_ladle"}:
        blockers.append("identity_not_unique")
    if task.get("crane_evidence_status") == "conflict":
        blockers.append("crane_evidence_conflict")
    if task["scope"] == "ef_crane" and task["observed_crane"] is None:
        blockers.append("ef_crane_assignment_not_confirmed")
    if task["scope"] == "ef_crane" and task["observed_crane"] and task["observed_crane"] not in DISPATCH_IDS:
        blockers.append("observed_crane_not_in_dispatch_set")
    if task["capacity_candidates"] == []:
        blockers.append("no_capacity_compatible_crane")
    if task["eligible_cranes"] == []:
        blockers.append("no_eligible_crane")
    if task.get("holder_status") in {"unknown", "partial", "ambiguous"}:
        blockers.append("holder_chain_unconfirmed")
    if task.get("holder_status") == "conditional":
        blockers.append("holder_chain_conditional")
    if task.get("resource_requirements") is None:
        blockers.append("resource_requirements_unverified")
    if task["scope"] in {"ef_crane"} and task["eligible_cranes"] is None:
        blockers.append("eligible_cranes_unverified")
    return sorted(set(blockers))


def admit(tasks: list[dict]) -> None:
    critical = {
        "unknown_origin_station", "unknown_destination_station", "unknown_mass",
        "missing_release_lower_bound", "release_upper_below_lower",
        "observed_arrival_before_lift", "identity_conflict", "identity_not_unique",
        "ef_scope_unconfirmed", "crane_evidence_conflict", "no_capacity_compatible_crane",
        "no_eligible_crane", "holder_chain_unconfirmed", "resource_requirements_unverified",
    }
    for task in tasks:
        blockers = task_blockers(task)
        task["blocker_codes"] = blockers
        if task["scope"] == "external_service":
            task["admission"] = "C" if any(
                code in blockers for code in ("boundary_receipt_unverified", "holder_chain_unconfirmed")
            ) else "B"
            continue
        if task["scope_status"] == "pending_ef_confirmation" or any(
            code in critical for code in blockers
        ):
            task["admission"] = "C"
        elif task["eligible_cranes"]:
            task["admission"] = "A"
        else:
            task["admission"] = "B"


def window_candidates(tasks: list[dict], width_s: int = 7200) -> list[dict]:
    """Find campaign-local windows with evidenced overlapping task intervals.

    A task without an evidenced end is a start-point cluster, not proof of
    resource competition.  The candidate keeps both counts and labels the
    evidence level.
    """
    grouped: dict[str, list[tuple[int, int | None, str, dict]]] = defaultdict(list)
    for task in tasks:
        if (task["scope"] != "ef_crane" or task["scope_status"] != "confirmed"
                or task["admission"] == "C"):
            continue
        start = task["release_interval"]["lo_s"]
        if start is None:
            continue
        plan_end = task["delivery_window"]["plan_latest_s"]
        observed_end = task["historical_pickup_proxy_s"] or task["observed_arrival_s"]
        if plan_end is not None and plan_end > start:
            end, basis = plan_end, "release_to_plan"
        elif observed_end is not None and observed_end > start:
            end, basis = observed_end, "release_to_observed"
        else:
            end, basis = None, "start_point_only"
        grouped[task["campaign_id"] or "__none__"].append((start, end, basis, task))

    candidates = []
    for campaign, intervals in grouped.items():
        heats_all = {task["heat_id"] for _, _, _, task in intervals}
        if len(heats_all) < 2:
            continue
        starts = sorted({start for start, _, _, _ in intervals})
        for window_start in starts:
            window_end = window_start + width_s
            selected = []
            for start, end, basis, task in intervals:
                if end is None:
                    hit = window_start <= start < window_end
                else:
                    hit = start < window_end and end >= window_start
                if hit:
                    selected.append((start, end, basis, task))
            if len(selected) < 2:
                continue
            heats = sorted({task["heat_id"] for _, _, _, task in selected})
            if len(heats) < 2:
                continue
            interval_tasks = [item for item in selected if item[1] is not None]
            point_tasks = [item for item in selected if item[1] is None]
            competitive = len(interval_tasks) >= 2 and len({
                item[3]["heat_id"] for item in interval_tasks
            }) >= 2
            campaigns = sorted({task["campaign_id"] for _, _, _, task in selected if task["campaign_id"]})
            loaded = sum(task["load_state"] == "loaded" for _, _, _, task in selected)
            empty = len(selected) - loaded
            known_station = sum(task["origin_known"] and task["destination_known"] for _, _, _, task in selected)
            known_mass = sum(task["mass_t"]["hi"] is not None for _, _, _, task in selected)
            identity_clean = sum("identity_conflict" not in task["blocker_codes"] for _, _, _, task in selected)
            c_count = sum(task["admission"] == "C" for _, _, _, task in selected)
            candidates.append({
                "window_id": "",
                "window_type": "campaign_local",
                "start_s": window_start,
                "end_s": window_end,
                "campaigns": campaigns,
                "heats": heats,
                "task_ids": [task["task_id"] for _, _, _, task in selected],
                "interval_task_count": len(interval_tasks),
                "point_only_task_count": len(point_tasks),
                "interval_basis_counts": dict(sorted(Counter(
                    basis for _, _, basis, _ in interval_tasks
                ).items())),
                "loaded_task_count": loaded,
                "empty_task_count": empty,
                "known_station_count": known_station,
                "known_mass_count": known_mass,
                "identity_clean_count": identity_clean,
                "c_blocked_count": c_count,
                "competition_basis": (
                    "overlapping evidenced release-to-plan/observed intervals"
                    if competitive else "start-time cluster only"
                ),
                "evidence_level": (
                    "competitive_interval" if competitive and c_count == 0
                    else "B_conditional" if competitive else "time_cluster_only"
                ),
                "score": (
                    len(interval_tasks) * 12 + len(heats) * 5 + loaded * 2 + known_mass
                    + (5 if competitive else 0) + len(point_tasks)
                ),
            })

    candidates.sort(key=lambda item: (-item["score"], item["start_s"]))
    selected = []
    used_tasks: set[str] = set()
    for candidate in candidates:
        overlap = len(set(candidate["task_ids"]) & used_tasks)
        if overlap > len(candidate["task_ids"]) // 2:
            continue
        selected.append(candidate)
        used_tasks.update(candidate["task_ids"])
        if len(selected) >= 8:
            break
    for index, candidate in enumerate(selected, 1):
        candidate["window_id"] = f"Q1W{index:02d}"
    return selected


def station_catalog(tasks: list[dict]) -> list[dict]:
    """EF topology nodes only.  BOF, slag, transfer and repair are external."""
    seen: dict[str, dict] = {}
    for task in tasks:
        for station_id, label in (
            (task["origin"], task["origin_label"]),
            (task["destination"], task["destination_label"]),
        ):
            if station_id in EF_STATIONS and station_id not in seen:
                meta = EF_STATIONS[station_id]
                seen[station_id] = {
                    "station_id": station_id,
                    "family": meta["family"],
                    "source_label": label,
                    "side": meta["side"],
                    "relative_order": meta["relative_order"],
                    "position_m": None,
                    "in_ef_topology": True,
                    "evidence": "figure 1 relative order only; no scale or coordinate evidence",
                }
    return sorted(seen.values(), key=lambda item: (item["side"], item["relative_order"]))


def resource_catalog(tasks: list[dict], events: list[dict] | None = None) -> list[dict]:
    resources = []
    seen: set[str] = set()
    station_ids = {
        value for task in tasks for value in (task["origin"], task["destination"])
        if value
    }
    if events:
        station_ids |= {
            event["resource_id"] for event in events if event.get("resource_id")
        }
    for station_id in sorted(station_ids):
        seen.add(station_id)
        family = station_id.split(":")[0] if ":" in station_id else re.sub(r"\d+$", "", station_id)
        is_ef = station_id in EF_STATIONS
        is_seat = family == "CC"
        resources.append({
            "resource_id": station_id,
            "resource_type": (
                "caster_receiving_seat" if is_seat
                else "ef_process_position" if is_ef
                else "external_process_or_handoff_position"
            ),
            "capacity": 1 if is_seat else None,
            "capacity_status": "derived_structural" if is_seat else "unknown",
            "location_station": station_id if is_ef else None,
            "in_ef_topology": is_ef,
            "occupancy": None,
            "evidence": (
                "one ladle receiving seat per caster (structural); no schedule-level occupancy record"
                if is_seat else "capacity and receipt rule not evidenced in the 12 workbooks"
            ),
        })
    for resource_id, resource_type in (
        ("BOF_A", "converter_process_node"),
        ("BOF_B", "converter_process_node"),
        ("BOF_C", "converter_process_node"),
        ("TRANSFER:unknown", "transfer_receipt"),
        ("REPAIR:unknown", "repair_position"),
        ("SLAG:unknown", "slag_position"),
        ("BOF_RETURN:unknown", "converter_return"),
    ):
        if resource_id in seen:
            continue
        resources.append({
            "resource_id": resource_id,
            "resource_type": resource_type,
            "capacity": None,
            "capacity_status": "unknown",
            "location_station": None,
            "in_ef_topology": False,
            "occupancy": None,
            "evidence": "external node; capacity and release rule unverified",
        })
    return resources


def build_instance(
    tasks: list[dict],
    events: list[dict],
    candidates: list[dict],
    primary_id: str | None,
    source_hashes: dict,
    assumptions: list[str],
    blocking: list[str],
) -> dict:
    if not candidates:
        raise SystemExit("no Q1 window candidate with at least two competing heats")
    primary = next((item for item in candidates if item["window_id"] == primary_id), None)
    primary = primary or candidates[0]
    heats = set(primary["heats"])
    start_s, end_s = primary["start_s"], primary["end_s"]

    def overlaps(task: dict) -> bool:
        lo = task["release_interval"]["lo_s"]
        end = (
            task["delivery_window"]["plan_latest_s"]
            or task["historical_pickup_proxy_s"]
            or task["observed_arrival_s"]
            or lo
        )
        if lo is None:
            observed = task["observed_arrival_s"]
            return observed is not None and start_s - 1800 <= observed <= end_s + 1800
        return lo <= end_s + 1800 and (end if end is not None else lo) >= start_s - 1800

    target_tasks = [
        task for task in tasks
        if task["heat_id"] in heats and overlaps(task)
    ]
    # Pull in predecessor tasks so the in-window tasks keep a complete chain.
    by_id = {task["task_id"]: task for task in tasks}
    selected_ids = {task["task_id"] for task in target_tasks}
    changed = True
    while changed:
        changed = False
        for task in list(target_tasks):
            predecessor = task.get("predecessor_task_id")
            if predecessor and predecessor in by_id and predecessor not in selected_ids:
                target_tasks.append(by_id[predecessor])
                selected_ids.add(predecessor)
                changed = True
    target_tasks.sort(key=lambda task: (task["heat_id"], task["task_id"]))
    task_actuals = [
        {
            "task_id": task["task_id"],
            "historical_pickup_proxy_s": task["historical_pickup_proxy_s"],
            "observed_arrival_s": task["observed_arrival_s"],
            "process_predecessor_s": task["release_interval"]["lo_s"],
            "plan_outer_deadline_s": task["source_plan_outer_deadline"],
        }
        for task in target_tasks
    ]
    # The policy-visible task records keep only plan and derived bounds.  Actual
    # historical pickup/arrival values live in simulator_truth.
    target_tasks = [copy.deepcopy(task) for task in target_tasks]
    for task in target_tasks:
        task["historical_pickup_proxy_s"] = None
        task["observed_arrival_s"] = None
        task["release_interval"]["hi_s"] = None
    target_ids = {task["task_id"] for task in target_tasks}
    target_events = []
    for event in events:
        if event["heat_id"] in heats:
            target_events.append(event)
        elif event["time_s"] is not None and start_s - 1800 <= event["time_s"] <= end_s + 1800:
            target_events.append(event)
    for event in target_events:
        if event["event_category"] == "plan" and event["visible_at_s"] is None:
            event["visible_at_s"] = min(event["time_s"], start_s) if event["time_s"] is not None else start_s
            event["visibility_assumption"] = "plan assumed known by window start"
    for task in target_tasks:
        task["required_in_process"] = True
        task["required_in_ef_scope"] = (
            None if task["scope_status"] == "pending_ef_confirmation"
            else task["scope"] == "ef_crane"
        )
        task["required"] = True
    holder_rows, holder_status = build_holder_chain(target_events)
    attach_task_holders(target_tasks, holder_rows)
    attach_resource_requirements(target_tasks, target_events)
    holders = []
    for ladle, status in sorted(holder_status.items()):
        visible = [row for row in holder_rows
                   if row["ladle_id"] == ladle and row["time_s"] <= start_s]
        last = None
        ambiguous = False
        if visible:
            last_time = max(row["time_s"] for row in visible)
            at_time = [row for row in visible if row["time_s"] == last_time]
            values = {row["holder_after"] for row in at_time}
            if "ambiguous" in {row["chain_status"] for row in at_time} or len(values) > 1:
                ambiguous = True
            else:
                last = at_time[-1]
        holders.append({
            "ladle_id": ladle,
            "holder_id": None if ambiguous else (last["holder_after"] if last else None),
            "holder_type": "ambiguous" if ambiguous else (last["holder_type"] if last else "unknown"),
            "since_s": None if ambiguous else (last["time_s"] if last else None),
            "status": "ambiguous" if ambiguous else (status if last else "unknown"),
            "holder_basis": (
                "same-time conflict" if ambiguous
                else (last["holder_basis"] if last else "no visible transition before window start")
            ),
            "source_event_id": None if ambiguous else (last["event_id"] if last else None),
        })
    known_ladles = {item["ladle_id"] for item in holders}
    for task in target_tasks:
        ladle = task["ladle_id"]
        if ladle and ladle not in known_ladles:
            holders.append({
                "ladle_id": ladle,
                "holder_id": task["holder_before"],
                "holder_type": "derived_from_task_origin",
                "since_s": None,
                "status": task["holder_status"],
                "holder_basis": "task origin; conditional assumption, not A-class evidence",
                "source_event_id": None,
            })
            known_ladles.add(ladle)
    holders.sort(key=lambda item: item["ladle_id"])
    cranes = []
    for crane in EF_CRANES:
        cranes.append({
            "crane_id": crane["crane_id"],
            "capacity_t": crane["capacity_t"],
            "dispatch_candidate": crane["dispatch_candidate"],
            "x": None,
            "v": None,
            "gear": None,
            "mode": "unknown",
            "carried_task_id": None,
            "held_ladle_id": None,
            "motion_or_operation_phase": "unknown",
            "phase_started_s": None,
            "remaining_service_s": None,
            "dispatch_allowed": "unknown",
            "track_presence": "unknown",
            "evidence": "no window-level crane position or occupancy record; figure order only",
        })
    active_operations = []
    actual_operation_ends = []
    for heat in sorted(heats):
        heat_events = [event for event in target_events if event["heat_id"] == heat]
        times = {event["event_type"]: event for event in heat_events if event["usable_for_canonical"]}
        for start_type, end_type, operation in (
            ("lf_process_start", "lf_process_end", "LF"),
            ("rh_enter", "rh_exit", "RH"),
            ("cast_start", "cast_end", "CC"),
        ):
            start = times.get(start_type)
            end = times.get(end_type)
            if not start or start["time_s"] > start_s:
                continue
            if end is not None and end["visible_at_s"] is not None and end["visible_at_s"] <= start_s:
                continue
            active_operations.append({
                "heat_id": heat,
                "operation": operation,
                "start_s": start["time_s"],
                "status": "in_progress",
                "source_event_ids": [start["event_id"]],
                "end_s": None,
                "end_s_visible": False,
                "remaining_service_s": None,
            })
            if end is not None:
                actual_operation_ends.append({
                    "heat_id": heat,
                    "operation": operation,
                    "actual_end_s": end["time_s"],
                    "end_event_id": end["event_id"],
                })
    occupancy = []
    for resource in resource_catalog(target_tasks, target_events):
        if resource["resource_id"].startswith("CC") and not resource["resource_id"].startswith("CC:"):
            occupancy.append({
                "resource_id": resource["resource_id"],
                "occupants": [item["heat_id"] for item in active_operations if item["operation"] == "CC"],
                "capacity": resource["capacity"],
                "status": "derived",
            })
    campaign_state = {}
    for task in target_tasks:
        campaign = task["campaign_id"]
        if not campaign:
            continue
        campaign_state.setdefault(campaign, {
            "campaign_id": campaign,
            "caster": next((item["destination"] for item in target_tasks
                            if item["campaign_id"] == campaign and item["destination_family"] == "CC"), None),
            "heats": [],
            "orders": {},
            "current_heat": None,
            "next_heat": None,
        })
        if task["heat_id"] not in campaign_state[campaign]["heats"]:
            campaign_state[campaign]["heats"].append(task["heat_id"])
            campaign_state[campaign]["orders"][task["heat_id"]] = task["campaign_order"]
    for state in campaign_state.values():
        state["heats"].sort(key=lambda heat: (
            int(state["orders"][heat]) if str(state["orders"].get(heat, "")).isdigit() else 10**6,
            heat,
        ))
        casting = {
            item["heat_id"] for item in active_operations
            if item["operation"] == "CC" and item["heat_id"] in state["heats"]
        }
        state["current_heat"] = next(iter(casting), None)
        if state["current_heat"] in state["heats"]:
            index = state["heats"].index(state["current_heat"])
            state["next_heat"] = state["heats"][index + 1] if index + 1 < len(state["heats"]) else None
        elif state["heats"]:
            state["next_heat"] = state["heats"][0]
        del state["orders"]
    visible_events = [
        event for event in target_events
        if event.get("visible_at_s") is not None and event["visible_at_s"] <= start_s
    ]
    task_states = []
    for task in target_tasks:
        task_events = [
            event for event in visible_events
            if task["task_id"] in event.get("task_ids", [])
        ]
        seat_events = {
            "lf_seat", "rh_seat", "cc_seat", "transfer_seat", "repair_seat",
            "return_transfer_seat", "return_bof_c_seat",
        }
        lift_events = {"bof_lift", "lf_lift", "rh_lift", "cc_empty_lift", "return_ef_lift"}
        if any(event["event_type"] in seat_events for event in task_events):
            status = "completed_visible"
        elif any(event["event_type"] in lift_events for event in task_events):
            status = "in_progress_visible"
        else:
            status = "not_started"
        lo = task["release_interval"]["lo_s"]
        task_states.append({
            "task_id": task["task_id"],
            "status": status,
            "visible_release_lo_s": lo if lo is not None and lo <= start_s else None,
            "release_visibility_basis": (
                "release lower bound at or before window start"
                if lo is not None and lo <= start_s
                else "future actual release is hidden from the policy"
            ),
            "plan_target_s": task["delivery_window"]["plan_latest_s"],
            "plan_target_basis": "plan cast start; visible as a plan commitment",
        })
    simulator_truth = {
        "visibility": "hidden_from_policy",
        "now_s": start_s,
        "policy_rule": "read only events with visible_at_s <= initial_state.now_s",
        "task_actuals": task_actuals,
        "future_event_ids": [
            event["event_id"] for event in target_events
            if event.get("visible_at_s") is not None and event["visible_at_s"] > start_s
        ],
        "active_operation_actual_end_s": actual_operation_ends,
    }
    instance = {
        "schema": SCHEMA,
        "instance_id": f"stage03-q1-b-{primary['window_id']}",
        "scenario": "Q1",
        "mode": "strict_plan",
        "clock": {
            "origin_s": start_s,
            "horizon_s": max(end_s - start_s, 1),
            "time_unit": "s",
            "timezone": "local naive source timestamps; no timezone conversion applied",
            "timestamp_origin": iso(start_s),
        },
        "plan_version": f"stage02-corrected-{source_hashes.get('audit_sha256', '')[:12]}",
        "scope": {
            "target_heats": sorted(heats),
            "target_tasks": [task["task_id"] for task in target_tasks],
            "window": {"start_s": start_s, "end_s": end_s, "horizon_s": max(end_s - start_s, 1)},
            "due_rule": "plan cast start as outer target; seat lead not evidenced",
            "process_mode": "deterministic",
            "recovery_authorization": "none",
            "policy_visibility_rule": "only events with visible_at_s <= initial_state.now_s are policy-visible",
            "included_external_tasks": [
                task["task_id"] for task in target_tasks if task["scope"] == "external_service"
            ],
            "pending_ef_confirmation_tasks": [
                task["task_id"] for task in target_tasks
                if task["scope_status"] == "pending_ef_confirmation"
            ],
        },
        "evidence": {
            "mode": "historical_replay",
            "source_hashes": source_hashes,
            "builder_version": BUILDER_VERSION,
            "raw_data_local_only": True,
            "identity_corrections_applied": 3,
            "assumptions": assumptions,
            "blocking": blocking,
            "interface_extensions": {
                "status": "adopted",
                "decision_date": "2026-11-27",
                "scope_status": {
                    "values": ["confirmed", "pending_ef_confirmation", "external_boundary"],
                    "semantics": "pending EF scope is not a dispatch decision",
                },
                "required_split": {
                    "required_in_process": "production process requires the leg",
                    "required_in_ef_scope": "EF dispatch requires the leg; null for pending EF scope",
                    "legacy_required": "must equal required_in_process",
                },
                "simulator_truth": {
                    "visibility": "hidden_from_policy",
                    "policy_input": "q1_policy_view.json",
                    "hidden_truth": "q1_simulator_truth.json",
                },
            },
            "policy_view_file": "q1_policy_view.json",
            "simulator_truth_file": "q1_simulator_truth.json",
        },
        "device_catalog": [
            {
                "device_id": crane["crane_id"],
                "device_type": "EF_crane",
                "capacity_t": crane["capacity_t"],
                "dispatch_candidate": crane["dispatch_candidate"],
                "window_available": None,
                "track_presence": "unknown",
                "evidence": crane["evidence"],
            }
            for crane in EF_CRANES
        ],
        "topology": {
            "scope": "EF",
            "stations": station_catalog(target_tasks),
            "rail_limits": None,
            "min_gap_m": None,
            "speed_mps": None,
            "allowed_segments": None,
            "parking_points": None,
            "geometry_status": "unverified",
            "evidence": "figure 1 gives relative station order only; no scale, coordinate, clearance or speed evidence",
        },
        "resources": resource_catalog(target_tasks, target_events),
        "events": target_events,
        "tasks": target_tasks,
        "simulator_truth": simulator_truth,
        "initial_state": {
            "now_s": start_s,
            "state_version": f"stage03-q1-b:{primary['window_id']}",
            "plan_version": f"stage02-corrected-{source_hashes.get('audit_sha256', '')[:12]}",
            "cranes": cranes,
            "holders": holders,
            "active_operations": active_operations,
            "occupancy": occupancy,
            "campaign_state": list(campaign_state.values()),
            "task_states": task_states,
            "locked_reservations": [],
            "visible_notices": [],
            "status": "conditional",
            "blocking": blocking,
        },
    }
    errors = validate_instance(instance)
    if errors:
        raise SystemExit("instance schema validation failed:\n" + "\n".join(errors))
    return instance


def build_policy_view(instance: dict) -> dict:
    """Return the policy-visible subset; reject hidden truth and future events."""
    now_s = instance["initial_state"]["now_s"]
    visible_events = [
        copy.deepcopy(event) for event in instance["events"]
        if event.get("visible_at_s") is not None and event["visible_at_s"] <= now_s
    ]
    policy_tasks = []
    for task in instance["tasks"]:
        item = copy.deepcopy(task)
        item["historical_pickup_proxy_s"] = None
        item["observed_arrival_s"] = None
        item["release_interval"]["hi_s"] = None
        policy_tasks.append(item)
    policy_evidence = copy.deepcopy(instance["evidence"])
    policy_evidence.pop("simulator_truth_file", None)
    policy = {
        "schema": POLICY_SCHEMA,
        "instance_id": instance["instance_id"],
        "scenario": instance["scenario"],
        "mode": instance["mode"],
        "clock": copy.deepcopy(instance["clock"]),
        "plan_version": instance["plan_version"],
        "scope": copy.deepcopy(instance["scope"]),
        "evidence": policy_evidence,
        "device_catalog": copy.deepcopy(instance["device_catalog"]),
        "topology": copy.deepcopy(instance["topology"]),
        "resources": copy.deepcopy(instance["resources"]),
        "events": visible_events,
        "tasks": policy_tasks,
        "initial_state": copy.deepcopy(instance["initial_state"]),
        "visibility": {
            "now_s": now_s,
            "rule": "only events with visible_at_s <= now_s are policy-visible",
            "excluded_fields": [
                "simulator_truth",
                "historical_pickup_proxy_s",
                "observed_arrival_s",
                "release_interval.hi_s",
            ],
            "future_event_count": sum(
                1 for event in instance["events"]
                if event.get("visible_at_s") is None or event["visible_at_s"] > now_s
            ),
        },
    }
    errors = validate_policy_view(policy)
    if errors:
        raise SystemExit("policy view validation failed:\n" + "\n".join(errors))
    return policy


def build_simulator_truth(instance: dict) -> dict:
    now_s = instance["initial_state"]["now_s"]
    future_events = [
        copy.deepcopy(event) for event in instance["events"]
        if event.get("visible_at_s") is None or event["visible_at_s"] > now_s
    ]
    truth = {
        "schema": SIMULATOR_SCHEMA,
        "instance_id": instance["instance_id"],
        "visibility": "hidden_from_policy",
        "now_s": now_s,
        "policy_rule": "never expose this file to the scheduling policy",
        "task_actuals": copy.deepcopy(instance["simulator_truth"]["task_actuals"]),
        "future_events": future_events,
        "active_operation_actual_end_s": copy.deepcopy(
            instance["simulator_truth"]["active_operation_actual_end_s"]
        ),
    }
    errors = validate_simulator_truth(truth)
    if errors:
        raise SystemExit("simulator truth validation failed:\n" + "\n".join(errors))
    return truth


def task_graph_acyclic(tasks: list[dict]) -> tuple[bool, int]:
    """Check the task predecessor relation; return (acyclic, missing references)."""
    by_id = {task["task_id"]: task for task in tasks}
    missing = sum(
        1 for task in tasks
        if task.get("predecessor_task_id") and task["predecessor_task_id"] not in by_id
    )

    def visit(task_id: str, trail: set[str]) -> bool:
        if task_id in trail:
            return False
        task = by_id.get(task_id)
        if not task:
            return True
        predecessor = task.get("predecessor_task_id")
        if not predecessor:
            return True
        return visit(predecessor, trail | {task_id})

    acyclic = all(visit(task_id, set()) for task_id in by_id)
    return acyclic, missing


def coverage_audit(
    raw_events: pd.DataFrame,
    raw_tasks: pd.DataFrame,
    tasks: list[dict],
    events: list[dict],
    candidates: list[dict],
    instance: dict,
    source_audit: dict,
) -> dict:
    by_scope = Counter(task["scope"] for task in tasks)
    by_scope_status = Counter(task["scope_status"] for task in tasks)
    by_admission = Counter(task["admission"] for task in tasks)
    by_leg_scope = Counter(f"{task['leg']}:{task['scope']}:{task['scope_status']}" for task in tasks)
    blockers = Counter(code for task in tasks for code in task["blocker_codes"])
    heats_with_tasks = {task["heat_id"] for task in tasks}
    all_heats = set(str(value) for value in raw_tasks["heat_id"].unique())
    acyclic, missing_predecessors = task_graph_acyclic(tasks)
    return {
        "builder_version": BUILDER_VERSION,
        "source": {
            "workbook_count": source_audit.get("workbook_count"),
            "row_count": source_audit.get("row_count"),
            "observed_event_count": source_audit.get("observed_event_count"),
            "canonical_event_count": source_audit.get("canonical_event_count"),
            "task_count": source_audit.get("task_count"),
            "identity_corrections": len(
                source_audit.get("identity_corrections", [])
            ) if isinstance(source_audit.get("identity_corrections"), list) else None,
        },
        "plans": {
            "valid_unique_heats": sum(source_audit.get("plan_valid_unique_heats_by_route", {}).values()),
            "matched_unique_master": sum(source_audit.get("with_unique_master_by_route", {}).values()),
        },
        "standardized": {
            "events": len(events),
            "tasks": len(tasks),
            "heats_with_tasks": len(heats_with_tasks),
            "campaigns_with_tasks": len({task["campaign_id"] for task in tasks if task["campaign_id"]}),
        },
        "scope_counts": dict(sorted(by_scope.items())),
        "scope_status_counts": dict(sorted(by_scope_status.items())),
        "admission_counts": dict(sorted(by_admission.items())),
        "required_in_ef_scope_counts": {
            "true": sum(task["required_in_ef_scope"] is True for task in tasks),
            "false": sum(task["required_in_ef_scope"] is False for task in tasks),
            "null": sum(task["required_in_ef_scope"] is None for task in tasks),
        },
        "leg_scope_counts": dict(sorted(by_leg_scope.items())),
        "blocker_counts": dict(sorted(blockers.items())),
        "field_coverage": {
            "origin_known": sum(task["origin_known"] for task in tasks),
            "destination_known": sum(task["destination_known"] for task in tasks),
            "mass_known": sum(task["mass_t"]["hi"] is not None for task in tasks),
            "release_lower_bound": sum(task["release_interval"]["lo_s"] is not None for task in tasks),
            "release_upper_proxy": sum(task["release_interval"]["hi_s"] is not None for task in tasks),
            "physical_release": sum(task["physical_release_s"] is not None for task in tasks),
            "pickup_window": sum(task["pickup_window"]["hard_earliest_s"] is not None for task in tasks),
            "delivery_plan_latest": sum(task["delivery_window"]["plan_latest_s"] is not None for task in tasks),
            "eligible_cranes_verified": sum(task["eligible_cranes"] is not None for task in tasks),
            "eligible_cranes_confirmed_empty": sum(task["eligible_cranes"] == [] for task in tasks),
            "resource_requirements_derived": sum(
                task["resource_requirement_status"] == "derived" for task in tasks
            ),
            "resource_requirements_unverified": sum(
                task["resource_requirement_status"] == "unverified" for task in tasks
            ),
            "crane_evidence_confirmed": sum(
                task["crane_evidence_status"] == "confirmed" for task in tasks
            ),
            "crane_evidence_conflict": sum(
                task["crane_evidence_status"] == "conflict" for task in tasks
            ),
        },
        "windows": {
            "candidate_count": len(candidates),
            "candidate_ids": [item["window_id"] for item in candidates],
            "competitive_interval_candidates": sum(
                item["evidence_level"] == "competitive_interval" for item in candidates
            ),
            "time_cluster_only_candidates": sum(
                item["evidence_level"] == "time_cluster_only" for item in candidates
            ),
            "primary": instance["instance_id"],
            "primary_task_count": len(instance["tasks"]),
            "primary_event_count": len(instance["events"]),
        },
        "task_graph": {
            "acyclic": acyclic,
            "missing_predecessor_refs": missing_predecessors,
        },
        "holder_chain": {
            "ladles_in_instance": len(instance["initial_state"]["holders"]),
            "unknown_holders": sum(
                item["status"] == "unknown" for item in instance["initial_state"]["holders"]
            ),
            "task_holder_status_counts": dict(sorted(
                Counter(task["holder_status"] for task in tasks).items()
            )),
        },
        "denominators": {
            "all_candidate_legs": len(tasks),
            "ef_required": sum(task["required"] for task in tasks),
            "external_boundary": sum(task["scope"] == "external_service" for task in tasks),
            "plan_heats_all": len(all_heats),
            "heats_without_tasks": len(all_heats - heats_with_tasks),
        },
    }


def parameter_evidence() -> list[dict]:
    rows = []
    for crane in EF_CRANES:
        rows.append({
            "parameter": f"crane_capacity[{crane['crane_id']}]",
            "value": crane["capacity_t"],
            "unit": "t",
            "source": "题面/图 1 行车吨位",
            "source_status": "observed",
            "applicability": "active",
            "impact": "tonnage screening only; station reachability and window availability still unverified",
        })
    for name, value, unit, source, status, impact in (
        ("rail_limits", None, "m", "no scale evidence", "pending", "trajectory domain unknown"),
        ("min_gap_m", None, "m", "no clearance evidence", "pending", "safety separation cannot be certified"),
        ("speed_mps", None, "m/s", "no speed evidence", "pending", "Q1 travel time cannot be instantiated"),
        ("station_positions", None, "m", "figure 1 relative order only", "pending", "normalized geometry not used for this instance"),
        ("crane_initial_positions", None, "m", "no window-level crane record", "pending", "initial state blocked"),
        ("seat_early_limit_s", 900, "s", "shared regression case", "assumed", "regression-only; not a field hard limit"),
        ("seat_lead_options_s", "0;120;180", "s", "shared regression case", "assumed", "regression-only; plan lead not evidenced"),
        ("release_rule", "process predecessor completion", "s", "Stage 02 audit", "derived", "used as lower bound; historical lift kept as upper proxy"),
        ("delivery_rule", "plan cast start", "s", "plan table", "derived", "outer target only; seat lead not evidenced"),
        ("mass_rule", "observed endpoint gross mass", "t", "Stage 02 audit", "observed", "point proxy; unknown remains null"),
        ("scope_status", "confirmed|pending_ef_confirmation|external_boundary", "", "project interface decision 2026-11-27", "derived", "adopted; keeps pending EF scope out of dispatch without adding a new scope enum"),
        ("required_split", "required_in_process; required_in_ef_scope", "", "project interface decision 2026-11-27", "derived", "adopted; required must equal required_in_process"),
        ("simulator_truth", "hidden_from_policy", "", "project interface decision 2026-11-27", "derived", "adopted; policy view and hidden truth are separate files"),
        ("eligible_cranes", "null unless station and permission verified", "", "model contract", "pending", "null is unknown; [] is confirmed no legal crane"),
        ("resource_requirements", "null unless evidenced", "", "model contract", "pending", "unknown is not an empty requirement list"),
        ("holder_chain_status", "complete|conditional|partial|ambiguous|unknown", "", "event evidence", "derived", "conditional and incomplete states are recorded, not promoted to A"),
    ):
        rows.append({
            "parameter": name,
            "value": value,
            "unit": unit,
            "source": source,
            "source_status": status,
            "applicability": "active" if status in {"observed", "derived"} else "unknown",
            "impact": impact,
        })
    return rows


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False, encoding="utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed", type=Path,
                        default=ROOT / "problem/data/processed/stage03_q1_b/source_rebuild/corrected",
                        help="Corrected Stage 02 output directory")
    parser.add_argument("--raw", type=Path, default=None,
                        help="Original B workbook directory; enables crane-label evidence")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "problem/data/processed/stage03_q1_b",
                        help="Local output directory (must be under problem/data/processed)")
    parser.add_argument("--primary-window", default=None,
                        help="Window id to use for q1_instance.json (default: highest score)")
    args = parser.parse_args()

    processed = args.processed.resolve()
    output = args.output.resolve()
    processed_root = (ROOT / "problem/data/processed").resolve()
    if processed_root not in output.parents:
        raise SystemExit("--output must be a child of problem/data/processed")
    output.mkdir(parents=True, exist_ok=True)

    raw = args.raw
    if raw is None:
        matches = sorted((ROOT / "docs/校赛题目").glob("B*/附数据文件"))
        raw = matches[0] if len(matches) == 1 else None

    raw_events = pd.read_csv(processed / "events.csv", dtype=str, keep_default_na=False)
    raw_tasks = pd.read_csv(processed / "tasks.csv", dtype=str, keep_default_na=False)
    source_audit = json.loads((processed / "audit.json").read_text(encoding="utf-8"))

    source_to_tasks: dict[str, list[str]] = defaultdict(list)
    if "source_plan" in raw_tasks:
        for _, task in raw_tasks.iterrows():
            task_id = str(task.get("task_id", ""))
            for key in ("source_plan", "source_process_predecessor", "source_lift",
                        "source_arrival", "source_weight", "source_plan_outer_deadline"):
                ref = str(task.get(key, ""))
                if ref and task_id not in source_to_tasks[ref]:
                    source_to_tasks[ref].append(task_id)

    source_index = SourceIndex(raw)
    device_rows = heat_device_rows(raw_events)
    corrections = load_approved_corrections(APPROVED_AUDIT)
    heat_identities = heat_identity_map(raw_events, corrections)
    events = build_events(raw_events, source_to_tasks)
    tasks = build_tasks(raw_tasks, device_rows, source_index, heat_identities)
    add_derived_events(events, tasks)
    attach_window_bindings(tasks, events)
    chain_rows, _ = build_holder_chain(events)
    attach_task_holders(tasks, chain_rows)
    attach_resource_requirements(tasks, events)
    admit(tasks)
    candidates = window_candidates(tasks)
    if not candidates:
        raise SystemExit("no Q1 window candidate found")

    source_hashes = {
        "events_csv": sha256_file(processed / "events.csv"),
        "tasks_csv": sha256_file(processed / "tasks.csv"),
        "audit_json": sha256_file(processed / "audit.json"),
        "workbooks": {
            item["file"]: item["sha256"] for item in source_audit.get("workbooks", [])
        },
    }
    assumptions = [
        "Only EF crane transport is a dispatch decision; transfer, repair and internal return legs remain external boundaries.",
        "Release lower bound is the process predecessor completion; historical lift is an upper proxy, not a physical release.",
        "Plan cast start is used as an outer delivery target; seat lead and hard supply deadlines are not evidenced.",
        "Station coordinates, rail limits, minimum gap, speed and crane initial states are not evidenced and remain null.",
        "Unknown values stay null; capacity_candidates is tonnage screening only and eligible_cranes is null.",
        "All production-level identifiers and timestamps stay in the local processed directory.",
    ]
    blocking = [
        "ef_station_coordinates_and_scale",
        "rail_limits_and_safety_distance",
        "crane_window_initial_state_and_track_presence",
        "physical_release_and_hard_windows",
        "transfer_receipt_capacity_and_release",
        "eligible_cranes_by_station_and_permission",
    ]
    instance = build_instance(
        tasks, events, candidates, args.primary_window, source_hashes, assumptions, blocking
    )
    audit = coverage_audit(raw_events, raw_tasks, tasks, events, candidates, instance, source_audit)
    policy_view = build_policy_view(instance)
    simulator_truth = build_simulator_truth(instance)
    audit["policy_isolation"] = {
        "policy_view": "q1_policy_view.json",
        "hidden_truth": "q1_simulator_truth.json",
        "policy_event_count": len(policy_view["events"]),
        "hidden_event_count": len(simulator_truth["future_events"]),
        "policy_task_count": len(policy_view["tasks"]),
        "hidden_action_count": len(simulator_truth["task_actuals"]),
        "policy_actual_fields_null": all(
            task["historical_pickup_proxy_s"] is None
            and task["observed_arrival_s"] is None
            and task["release_interval"]["hi_s"] is None
            for task in policy_view["tasks"]
        ),
    }

    (output / "q1_instance.json").write_text(
        json.dumps(instance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "q1_policy_view.json").write_text(
        json.dumps(policy_view, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "q1_simulator_truth.json").write_text(
        json.dumps(simulator_truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "window_candidates.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_csv(
        output / "events.csv", events,
        ["event_id", "event_type", "event_category", "heat_id", "ladle_id", "task_id",
         "resource_id", "resource_family", "resource_known", "time_s", "visible_at_s",
         "sequence", "source_status", "source_ref", "source_event_status", "link_status",
         "usable_for_canonical", "weight_t"],
    )
    # The flat task CSV uses scalar columns; build them from the nested records.
    flat_tasks = []
    for task in tasks:
        flat_tasks.append({
            "task_id": task["task_id"], "heat_id": task["heat_id"],
            "ladle_id": task["ladle_id"],
            "identity_status": task["ladle_identity"]["status"],
            "identity_candidates": ";".join(
                item["ladle_id"] for item in task["ladle_identity"]["candidates"]
            ),
            "campaign_id": task["campaign_id"],
            "campaign_order": task["campaign_order"], "route": task["route"],
            "leg": task["leg"], "scope": task["scope"],
            "scope_status": task["scope_status"], "scope_basis": task["scope_basis"],
            "load_state": task["load_state"], "origin": task["origin"],
            "destination": task["destination"], "origin_label": task["origin_label"],
            "destination_label": task["destination_label"],
            "origin_known": task["origin_known"], "destination_known": task["destination_known"],
            "mass_lo_t": task["mass_t"]["lo"], "mass_hi_t": task["mass_t"]["hi"],
            "mass_status": task["mass_t"]["source_status"],
            "release_lo_s": task["release_interval"]["lo_s"],
            "release_hi_s": task["release_interval"]["hi_s"],
            "physical_release_s": task["physical_release_s"],
            "historical_pickup_proxy_s": task["historical_pickup_proxy_s"],
            "observed_arrival_s": task["observed_arrival_s"],
            "plan_outer_deadline_s": task["source_plan_outer_deadline"],
            "capacity_candidates": (
                "null" if task["capacity_candidates"] is None
                else ";".join(task["capacity_candidates"])
            ),
            "eligible_cranes": (
                "null" if task["eligible_cranes"] is None
                else ";".join(task["eligible_cranes"])
            ),
            "observed_crane": task["observed_crane"],
            "observed_crane_candidates": ";".join(task["observed_crane_candidates"]),
            "crane_evidence_status": task["crane_evidence_status"],
            "required": task["required"],
            "required_in_process": task["required_in_process"],
            "required_in_ef_scope": (
                "null" if task["required_in_ef_scope"] is None
                else task["required_in_ef_scope"]
            ),
            "resource_requirement_status": task["resource_requirement_status"],
            "admission": task["admission"], "blocker_codes": ";".join(task["blocker_codes"]),
            "link_status": task["link_status"], "flags": ";".join(task["flags"]),
            "holder_before": task["holder_before"], "holder_after": task["holder_after"],
            "holder_status": task["holder_status"],
        })
    write_csv(output / "tasks.csv", flat_tasks, list(flat_tasks[0]))
    write_csv(
        output / "holder_chain.csv", chain_rows,
        ["ladle_id", "heat_id", "event_id", "event_type", "time_s", "holder_before",
         "holder_after", "holder_type", "chain_status", "holder_basis",
         "ambiguous_event_ids", "source_ref"],
    )
    write_csv(
        output / "parameter_evidence.csv", parameter_evidence(),
        ["parameter", "value", "unit", "source", "source_status", "applicability", "impact"],
    )
    print(f"Wrote {len(tasks)} standardized tasks and {len(events)} events")
    print(f"Admission: {dict(Counter(task['admission'] for task in tasks))}")
    print(f"Window candidates: {[item['window_id'] for item in candidates]}")
    print(f"Primary instance: {instance['instance_id']} ({len(instance['tasks'])} tasks, {len(instance['events'])} events)")
    print(f"Local output: {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
