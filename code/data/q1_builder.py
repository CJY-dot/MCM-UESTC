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
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from q1_schema import SCHEMA, validate_instance

BUILDER_VERSION = "stage03-q1-b/1.0.0"
ROOT = Path(__file__).resolve().parents[2]

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

DEVICE_FILES = {
    "1#LF.xlsx": "LF", "3#LF.xlsx": "LF",
    "1#RH.xlsx": "RH", "2#RH.xlsx": "RH",
    "1#连铸.xlsx": "CC", "2#连铸.xlsx": "CC", "3#连铸.xlsx": "CC",
}
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
    match = re.fullmatch(r"BOF\s*([ABC])", upper)
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


def heat_device_rows(events: pd.DataFrame) -> dict[str, dict[str, tuple[str, str, int]]]:
    """Map heat -> device family -> one source row, preferring canonical rows."""
    grouped: dict[tuple[str, str], list[tuple[int, str, str, int]]] = defaultdict(list)
    for _, event in events.iterrows():
        file = str(event.get("source_file", ""))
        family = DEVICE_FILES.get(file)
        if not family:
            continue
        heat = str(event.get("heat_id", ""))
        row = parse_int(event.get("source_row"))
        if not heat or row is None:
            continue
        priority = 0 if str(event.get("event_status")) == "canonical" else 1
        grouped[(heat, family)].append((priority, file, str(event.get("source_sheet", "")), row))
    result: dict[str, dict[str, tuple[str, str, int]]] = defaultdict(dict)
    for (heat, family), candidates in grouped.items():
        candidates.sort()
        _, file, sheet, row = candidates[0]
        result[heat][family] = (file, sheet, row)
    return result


def task_crane_evidence(
    task_row: dict,
    device_rows: dict[str, dict[str, tuple[str, str, int]]],
    source_index: SourceIndex,
) -> tuple[str | None, str, str, str]:
    """Return (observed_crane, scope_basis, device_file, device_row_label)."""
    heat = str(task_row.get("heat_id", ""))
    loaded = str(task_row.get("loaded_state")) == "loaded"
    origin_family = family_of(task_row.get("origin", ""))
    destination_family = family_of(task_row.get("destination", ""))
    family = destination_family if loaded else origin_family
    if family not in {"LF", "RH", "CC"}:
        return None, "process_interface_inferred", "", ""
    device = device_rows.get(heat, {}).get(family)
    if not device:
        return None, "process_interface_inferred", "", ""
    file, sheet, row = device
    release, lift = source_index.crane_labels(file, sheet, row)
    observed = (lift if not loaded else release) or release or lift
    if observed.strip().lower() in {"nan", "none"}:
        observed = ""
    basis = "device_crane_label" if observed else "process_interface_inferred"
    return observed or None, basis, file, f"{file}|{sheet}|{row}"


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


def heat_ladle_map(events: pd.DataFrame) -> dict[str, str]:
    """Choose the most frequent actual ladle id per heat as the task-level identifier."""
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for _, event in events.iterrows():
        heat = str(event.get("heat_id", ""))
        ladle = str(event.get("ladle_id", "")).strip()
        if not heat or not ladle:
            continue
        if str(event.get("event_status")) in {"canonical", "agrees"}:
            counts[heat][ladle] += 1
    return {heat: counter.most_common(1)[0][0] for heat, counter in counts.items() if counter}


def build_tasks(
    raw_tasks: pd.DataFrame,
    device_rows: dict[str, dict[str, tuple[str, str, int]]],
    source_index: SourceIndex,
    heat_ladles: dict[str, str],
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
            scope_basis = "boundary_interface"
        elif leg in PENDING_EF_LEGS:
            scope = "pending_ef_scope"
            scope_basis = "no_recorded_ef_lift"
        else:
            scope = "ef_crane"
            scope_basis = "process_interface_inferred"
        observed_crane, crane_basis, device_file, device_ref = task_crane_evidence(
            row.to_dict(), device_rows, source_index
        )
        if observed_crane and observed_crane in CRANE_BY_ID:
            scope_basis = crane_basis
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
        delivery_window = {
            "hard_earliest_s": None,
            "hard_latest_s": None,
            "plan_earliest_s": None,
            "plan_latest_s": plan_outer if destination_family == "CC" else None,
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
            "ladle_id": heat_ladles.get(heat),
            "campaign_id": str(row.get("campaign", "")) or None,
            "campaign_order": str(row.get("plan_order", "")) or None,
            "route": str(row.get("route", "")) or None,
            "leg": leg,
            "scope": scope,
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
            "eligible_cranes": None,
            "eligibility_status": "topology_and_window_unverified",
            "observed_crane": observed_crane,
            "observed_crane_dispatch_candidate": (
                observed_crane in DISPATCH_IDS if observed_crane else None
            ),
            "resource_requirements": [],
            "required": scope in {"ef_crane", "pending_ef_scope"},
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
            "_device_ref": device_ref,
        })
    return tasks


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


def build_holder_chain(events: list[dict]) -> tuple[list[dict], dict[str, str]]:
    holder_roles = {
        "bof_empty_car": "BOF",
        "bof_lift": "EF",
        "lf_seat": "STATION",
        "lf_lift": "EF",
        "rh_seat": "STATION",
        "rh_lift": "EF",
        "cc_seat": "STATION",
        "cc_empty_lift": "EF",
        "slag_start": "STATION",
        "slag_second_start": "STATION",
        "transfer_seat": "TRANSFER",
        "repair_seat": "REPAIR",
        "return_transfer_seat": "TRANSFER",
        "return_ef_lift": "EF",
        "return_bof_c_seat": "BOF_RETURN",
    }
    by_ladle: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        if not event["ladle_id"] or not event["usable_for_canonical"]:
            continue
        if event["event_type"] not in holder_roles:
            continue
        by_ladle[event["ladle_id"]].append(event)
    chain_rows = []
    statuses: dict[str, str] = {}
    for ladle, items in by_ladle.items():
        items.sort(key=lambda item: (item["time_s"], item["event_id"]))
        holder = None
        chain_status = "complete"
        for event in items:
            kind = holder_roles[event["event_type"]]
            if kind == "EF":
                new_holder = "EF_CRANE:unknown"
            elif kind == "STATION":
                new_holder = event["resource_id"] or "STATION:unknown"
            elif kind == "TRANSFER":
                new_holder = event["resource_id"] or "TRANSFER:unknown"
            elif kind == "REPAIR":
                new_holder = event["resource_id"] or "REPAIR:unknown"
            elif kind == "BOF_RETURN":
                new_holder = event["resource_id"] or "BOF_RETURN:unknown"
            else:
                new_holder = event["resource_id"] or "BOF:unknown"
            if holder is None and event["event_type"] not in {"bof_empty_car", "bof_lift"}:
                chain_status = "partial"
            chain_rows.append({
                "ladle_id": ladle,
                "heat_id": event["heat_id"],
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "time_s": event["time_s"],
                "holder_before": holder or "unknown",
                "holder_after": new_holder,
                "holder_type": kind,
                "chain_status": chain_status,
                "source_ref": event["source_ref"],
            })
            holder = new_holder
        statuses[ladle] = chain_status
    return chain_rows, statuses


def attach_task_holders(tasks: list[dict], chain_rows: list[dict]) -> None:
    by_ladle: dict[str, list[dict]] = defaultdict(list)
    for row in chain_rows:
        by_ladle[row["ladle_id"]].append(row)
    for rows in by_ladle.values():
        rows.sort(key=lambda row: (row["time_s"], row["event_id"]))
    for task in tasks:
        ladle = task["ladle_id"]
        lo = task["release_interval"]["lo_s"]
        before = "unknown"
        status = "unknown"
        if ladle and ladle in by_ladle and lo is not None:
            for row in by_ladle[ladle]:
                if row["time_s"] <= lo:
                    before = row["holder_after"]
                    status = row["chain_status"]
                else:
                    break
        task["holder_before"] = before
        task["holder_after"] = task["destination"] or "unknown"
        task["holder_status"] = status
        if task["holder_status"] == "unknown" and task["origin"]:
            task["holder_before"] = task["origin"]
            task["holder_status"] = "derived_from_task_origin"


def task_blockers(task: dict) -> list[str]:
    blockers = []
    if task["scope"] == "pending_ef_scope":
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
    if "ambiguous_or_conflicting_source" in task["flags"]:
        blockers.append("identity_conflict")
    if task["link_status"] not in {"unique", "resolved_by_identity", "resolved_by_temporary_ladle"}:
        blockers.append("identity_not_unique")
    if task["scope"] == "ef_crane" and task["observed_crane"] is None:
        blockers.append("ef_crane_assignment_not_confirmed")
    if task["scope"] == "ef_crane" and task["observed_crane"] and task["observed_crane"] not in DISPATCH_IDS:
        blockers.append("observed_crane_not_in_dispatch_set")
    if task["scope"] in {"ef_crane", "pending_ef_scope"} and task["eligible_cranes"] is None:
        blockers.append("eligible_cranes_unverified")
    return sorted(set(blockers))


def admit(tasks: list[dict]) -> None:
    for task in tasks:
        blockers = task_blockers(task)
        if task["scope"] == "external_service":
            task["admission"] = "C" if "boundary_receipt_unverified" in blockers else "B"
        elif task["scope"] == "pending_ef_scope":
            task["admission"] = "C"
        elif any(code in blockers for code in (
            "unknown_origin_station", "unknown_destination_station", "unknown_mass",
            "missing_release_lower_bound", "release_upper_below_lower",
            "observed_arrival_before_lift", "identity_conflict", "identity_not_unique",
        )):
            task["admission"] = "C"
        else:
            task["admission"] = "B"
        task["blocker_codes"] = blockers


def window_candidates(tasks: list[dict], width_s: int = 7200) -> list[dict]:
    """Find campaign-local windows with at least two competing heats.

    A shop-wide 2-hour slice mixes several casters and produces a very large
    instance.  Q1 starts from campaign-local windows; the global overlap remains
    visible through the campaign list of each candidate.
    """
    grouped: dict[str, list[tuple[int, int, dict]]] = defaultdict(list)
    for task in tasks:
        if task["scope"] != "ef_crane" or task["admission"] == "C":
            continue
        start = task["release_interval"]["lo_s"]
        if start is None:
            continue
        end = task["delivery_window"]["plan_latest_s"] or task["historical_pickup_proxy_s"] or start
        grouped[task["campaign_id"] or "__none__"].append((start, max(end, start), task))

    candidates = []
    for campaign, intervals in grouped.items():
        heats_all = {task["heat_id"] for _, _, task in intervals}
        if len(heats_all) < 2:
            continue
        starts = sorted({start for start, _, _ in intervals})
        for window_start in starts:
            window_end = window_start + width_s
            selected = [
                task for start, end, task in intervals
                if start < window_end and end >= window_start
            ]
            if len(selected) < 2:
                continue
            heats = sorted({task["heat_id"] for task in selected})
            if len(heats) < 2:
                continue
            campaigns = sorted({task["campaign_id"] for task in selected if task["campaign_id"]})
            loaded = sum(task["load_state"] == "loaded" for task in selected)
            empty = len(selected) - loaded
            known_station = sum(task["origin_known"] and task["destination_known"] for task in selected)
            known_mass = sum(task["mass_t"]["hi"] is not None for task in selected)
            identity_clean = sum("identity_conflict" not in task["blocker_codes"] for task in selected)
            c_count = sum(task["admission"] == "C" for task in selected)
            candidates.append({
                "window_id": "",
                "window_type": "campaign_local",
                "start_s": window_start,
                "end_s": window_end,
                "campaigns": campaigns,
                "heats": heats,
                "task_ids": [task["task_id"] for task in selected],
                "loaded_task_count": loaded,
                "empty_task_count": empty,
                "known_station_count": known_station,
                "known_mass_count": known_mass,
                "identity_clean_count": identity_clean,
                "c_blocked_count": c_count,
                "competition_basis": "overlapping release-to-delivery intervals inside one campaign",
                "evidence_level": "B_conditional" if c_count == 0 else "C_blocked",
                "score": len(selected) * 10 + len(heats) * 5 + loaded * 2 + known_mass,
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
    seen: dict[str, dict] = {}
    for task in tasks:
        for station_id, family, label in (
            (task["origin"], task["origin_family"], task["origin_label"]),
            (task["destination"], task["destination_family"], task["destination_label"]),
        ):
            if station_id and station_id not in seen:
                seen[station_id] = {
                    "station_id": station_id,
                    "family": family,
                    "source_label": label,
                    "side": "E" if family in {"LF", "CC"} else "F" if family in {"RH", "BOF"} else None,
                    "relative_order": None,
                    "position_m": None,
                    "evidence": "figure 1 relative order only; no scale or coordinate evidence",
                }
    return sorted(seen.values(), key=lambda item: item["station_id"])


def resource_catalog(tasks: list[dict]) -> list[dict]:
    resources = []
    for station_id in sorted({item["station_id"] for item in station_catalog(tasks)}):
        family = station_id.split(":")[0] if ":" in station_id else re.sub(r"\d+$", "", station_id)
        is_seat = family == "CC"
        resources.append({
            "resource_id": station_id,
            "resource_type": "caster_receiving_seat" if is_seat else "process_or_handoff_position",
            "capacity": 1 if is_seat else None,
            "capacity_status": "derived_structural" if is_seat else "unknown",
            "location_station": station_id,
            "occupancy": None,
            "evidence": (
                "one ladle receiving seat per caster (structural); no schedule-level occupancy record"
                if is_seat else "capacity and receipt rule not evidenced in the 12 workbooks"
            ),
        })
    for resource_id, resource_type in (
        ("TRANSFER:unknown", "transfer_receipt"),
        ("REPAIR:unknown", "repair_position"),
        ("SLAG:unknown", "slag_position"),
        ("BOF_RETURN:unknown", "converter_return"),
    ):
        resources.append({
            "resource_id": resource_id,
            "resource_type": resource_type,
            "capacity": None,
            "capacity_status": "unknown",
            "location_station": None,
            "occupancy": None,
            "evidence": "source label lacks a stable equipment id; capacity and release rule unverified",
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
        task["required"] = task["scope"] in {"ef_crane", "pending_ef_scope"}
    holder_rows, holder_status = build_holder_chain(target_events)
    attach_task_holders(target_tasks, holder_rows)
    holders = []
    for ladle, status in sorted(holder_status.items()):
        last = None
        for row in holder_rows:
            if row["ladle_id"] == ladle and row["time_s"] <= start_s:
                last = row
        holders.append({
            "ladle_id": ladle,
            "holder_id": last["holder_after"] if last else None,
            "holder_type": last["holder_type"] if last else "unknown",
            "since_s": last["time_s"] if last else None,
            "status": status if last else "unknown",
            "source_event_id": last["event_id"] if last else None,
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
            if start and end and start["time_s"] <= start_s <= end["time_s"]:
                active_operations.append({
                    "heat_id": heat,
                    "operation": operation,
                    "start_s": start["time_s"],
                    "end_s": end["time_s"],
                    "status": "in_progress",
                    "source_event_ids": [start["event_id"], end["event_id"]],
                })
    occupancy = []
    for resource in resource_catalog(target_tasks):
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
    task_states = []
    for task in target_tasks:
        lo = task["release_interval"]["lo_s"]
        end = task["delivery_window"]["plan_latest_s"] or task["historical_pickup_proxy_s"]
        if lo is None:
            status = "unknown"
        elif start_s < lo:
            status = "not_started"
        elif end is not None and start_s > end:
            status = "possibly_completed"
        else:
            status = "in_window"
        task_states.append({
            "task_id": task["task_id"],
            "status": status,
            "release_lo_s": lo,
            "target_end_s": end,
        })
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
            "included_external_tasks": [
                task["task_id"] for task in target_tasks if task["scope"] == "external_service"
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
        "resources": resource_catalog(target_tasks),
        "events": target_events,
        "tasks": target_tasks,
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
    by_admission = Counter(task["admission"] for task in tasks)
    by_leg_scope = Counter(f"{task['leg']}:{task['scope']}" for task in tasks)
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
        "admission_counts": dict(sorted(by_admission.items())),
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
        },
        "windows": {
            "candidate_count": len(candidates),
            "candidate_ids": [item["window_id"] for item in candidates],
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
    heat_ladles = heat_ladle_map(raw_events)
    events = build_events(raw_events, source_to_tasks)
    tasks = build_tasks(raw_tasks, device_rows, source_index, heat_ladles)
    add_derived_events(events, tasks)
    chain_rows, _ = build_holder_chain(events)
    attach_task_holders(tasks, chain_rows)
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

    (output / "q1_instance.json").write_text(
        json.dumps(instance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
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
            "ladle_id": task["ladle_id"], "campaign_id": task["campaign_id"],
            "campaign_order": task["campaign_order"], "route": task["route"],
            "leg": task["leg"], "scope": task["scope"], "scope_basis": task["scope_basis"],
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
            "capacity_candidates": ";".join(task["capacity_candidates"] or []),
            "eligible_cranes": "null" if task["eligible_cranes"] is None else ";".join(task["eligible_cranes"]),
            "observed_crane": task["observed_crane"], "required": task["required"],
            "admission": task["admission"], "blocker_codes": ";".join(task["blocker_codes"]),
            "link_status": task["link_status"], "flags": ";".join(task["flags"]),
            "holder_before": task["holder_before"], "holder_after": task["holder_after"],
            "holder_status": task["holder_status"],
        })
    write_csv(output / "tasks.csv", flat_tasks, list(flat_tasks[0]))
    write_csv(
        output / "holder_chain.csv", chain_rows,
        ["ladle_id", "heat_id", "event_id", "event_type", "time_s", "holder_before",
         "holder_after", "holder_type", "chain_status", "source_ref"],
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
