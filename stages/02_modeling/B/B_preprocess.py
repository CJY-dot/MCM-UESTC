"""Audit the B workbooks and build local, traceable event and transport tables."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from hashlib import sha256
from importlib.metadata import version
import json
import math
from pathlib import Path
import re
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
PROCESSED = ROOT / "problem/data/processed"
EPOCH = datetime(1970, 1, 1)
ROUTES = {"L0": ("LF",), "R0": ("RH",), "L0R0": ("LF", "RH"), "R0L0": ("RH", "LF")}
CAPACITY = {"1#": 150, "2#": 150, "3#": 450, "4#": 450, "5#": 450}
KINDS = ("master", "BOF", "LF", "RH", "CC")
FILENAMES = {
    "出钢计划表单.xls": ("plan", ""),
    "工序时间信息.xlsx": ("master", ""),
    **{f"{i}炉.xlsx": ("BOF", f"BOF{i}") for i in "ABC"},
    **{f"{i}#LF.xlsx": ("LF", f"LF{i}") for i in (1, 3)},
    **{f"{i}#RH.xlsx": ("RH", f"RH{i}") for i in (1, 2)},
    **{f"{i}#连铸.xlsx": ("CC", f"CC{i}") for i in (1, 2, 3)},
}

# Master-table process timestamps and device-log processing/degassing timestamps
# retain distinct names wherever the underlying operations have different meanings.
FIELDS = {
    "plan": {
        "转炉开始时间": ("plan_bof_start", "BOF"),
        "转炉结束时间": ("plan_bof_end", "BOF"),
        **{f"精炼{i}时间": (f"plan_refine_slot_{i}", "") for i in range(1, 9)},
        "连铸开始时间": ("plan_cast_start_unspecified", "CC"),
        "连铸结束时间": ("plan_cast_end_unspecified", "CC"),
        "浇铸开始时间": ("plan_cast_start", "CC"),
        "浇铸结束时间": ("plan_cast_end", "CC"),
    },
    "master": {
        "空罐放到转炉钢包车时间": ("bof_empty_car", "BOF"),
        "出钢开始时间": ("tap_start", "BOF"),
        "出钢结束时间": ("tap_end", "BOF"),
        "重罐吊起时间": ("bof_lift", "BOF"),
        "重罐落到LF待机位时间": ("lf_seat", "LF"),
        "LF进处理位时间": ("lf_enter", "LF"),
        "LF出处理位时间": ("lf_exit", "LF"),
        "LF重罐吊起时间": ("lf_lift", "LF"),
        "重罐吊起至RH旋转位时间": ("rh_master_rotate", "RH"),
        "RH处理开始时间": ("rh_master_process_start", "RH"),
        "RH处理结束时间": ("rh_master_process_end", "RH"),
        "RH吊起时间": ("rh_lift", "RH"),
        "吊至连铸机时间": ("cc_seat", "CC"),
        "开浇时间": ("cast_start", "CC"),
        "停浇时间": ("cast_end", "CC"),
        "空包在连铸位吊起时间": ("cc_empty_lift", "CC"),
        "翻渣开始时间": ("slag_start", "SLAG"),
        "翻渣结束时间": ("slag_end", "SLAG"),
        "过渡平车到EF跨吊罐位时间": ("transfer_at_ef", "TRANSFER"),
        "空罐放下过渡平车时间": ("transfer_seat", "TRANSFER"),
        "过渡平车至整备跨时间": ("transfer_arrive_repair_area", "TRANSFER"),
        "空罐放置热修位时间": ("repair_seat", "REPAIR"),
        "从热修位吊起时间": ("repair_lift", "REPAIR"),
        "天车将包放置在过渡平车时间": ("return_transfer_seat", "TRANSFER"),
        "在吊罐位起吊时间": ("return_ef_lift", "TRANSFER"),
        "抵达C炉台车时间": ("return_bof_c_seat", "RETURN_C"),
    },
    "BOF": {
        "空罐放到转炉钢包车时间": ("bof_empty_car", "BOF"),
        "转炉兑铁开始时间": ("bof_charge_start", "BOF"),
        "转炉兑铁结束时间": ("bof_charge_end", "BOF"),
        "吹炼开始时间": ("bof_blow_start", "BOF"),
        "吹炼结束时间": ("bof_blow_end", "BOF"),
        "出钢开始时间": ("tap_start", "BOF"),
        "出钢结束时间": ("tap_end", "BOF"),
    },
    "LF": {
        "重包进站到LF精炼待机位时间": ("lf_seat", "LF"),
        "进处理位时间": ("lf_enter", "LF"),
        "出处理位时间": ("lf_exit", "LF"),
        "精炼处理开始时间": ("lf_process_start", "LF"),
        "精炼处理结束时间": ("lf_process_end", "LF"),
        "LF重包从待机位吊起时间": ("lf_lift", "LF"),
    },
    "RH": {
        "重包到RH接受位时间": ("rh_seat", "RH"),
        "进处理位时间": ("rh_enter", "RH"),
        "出处理位时间": ("rh_exit", "RH"),
        "脱气开始时间": ("rh_degassing_start", "RH"),
        "脱气结束时间": ("rh_degassing_end", "RH"),
        "重罐出站到吊罐位时间": ("rh_exit_to_lift_station", "RH"),
        "重包从RH吊起时间": ("rh_lift", "RH"),
    },
    "CC": {
        "重包到铸机接受位座包时间": ("cc_seat", "CC"),
        "大包开浇时刻": ("cast_start", "CC"),
        "大包终浇时刻": ("cast_end", "CC"),
        "空包在铸机接受位吊起时间": ("cc_empty_lift", "CC"),
        "翻渣开始时间1": ("slag_start", "SLAG"),
        "翻渣结束时间1": ("slag_end", "SLAG"),
        "翻渣开始时间2": ("slag_second_start", "SLAG"),
        "翻渣结束时间2": ("slag_second_end", "SLAG"),
        "空罐放下过渡平车时间": ("transfer_seat", "TRANSFER"),
    },
}

# Use only gross weights attached to a named operation. Where both ends of a
# lift have a measure, form a conservative observed weight proxy.
WEIGHTS = {
    "master": {
        "bof_lift": "钢包车上重包重量",
        "slag_end": "翻渣作业后空包重量",
    },
    "LF": {"lf_seat": "进入重量", "lf_lift": "离开重量"},
    "RH": {"rh_seat": "进入重量", "rh_lift": "RH起吊重包重量"},
    "CC": {
        "cc_seat": "铸机接受位重包放下重量", "cc_empty_lift": "铸机接受位空包吊起重量",
        "slag_start": "翻渣前重量1", "slag_end": "翻渣后重量1",
        "slag_second_start": "翻渣前重量2", "slag_second_end": "翻渣后重量2",
    },
}

CALIBRATION_COLUMNS = (
    "heat_id", "metric", "equipment", "route", "day", "start_time_s", "end_time_s",
    "duration_s", "source_start", "source_end", "planned_duration_s", "source_plan_duration",
    "interpretation",
)
EVENT_COLUMNS = (
    "heat_id", "record_type", "event", "equipment", "time_s", "timestamp", "raw_time",
    "time_status", "source_file", "source_sheet", "source_row", "source_column",
    "source_column_index", "source_engine", "tap_mark", "ladle_id", "temporary_ladle_id",
    "route", "campaign", "plan_order", "link_status", "event_status", "confidence",
    "gross_weight_t", "weight_source_column",
)
TASK_COLUMNS = (
    "task_id", "heat_id", "campaign", "plan_order", "route", "leg", "loaded_state",
    "origin", "destination", "predecessor_task_id", "scope", "physical_release_s",
    "release_proxy_s", "process_predecessor_s", "window_open_s", "window_close_s",
    "plan_outer_deadline_s", "observed_lift_s", "observed_arrival_s", "gross_weight_t",
    "weight_status", "cranes_by_capacity", "eligible_cranes", "eligibility_status",
    "link_status", "flags", "source_plan", "source_process_predecessor", "source_lift",
    "source_arrival", "source_weight", "source_plan_outer_deadline",
)

DATE_TEXT = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$")
TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?$")
DURATION_PART = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(小时|分钟|分|秒)")


def clean(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def identifier(value):
    return re.sub(r"\.0+$", "", clean(value)).upper()


def valid_heat(value):
    return bool(value) and value not in {"-2", "-1", "0", "-"}


def timestamp(value):
    if isinstance(value, (date, datetime)):
        # Date-only cells lack a reliable time of day.
        return int((value - EPOCH).total_seconds()) if isinstance(value, datetime) and not pd.isna(value) else None
    label = clean(value)
    if not DATE_TEXT.fullmatch(label) or ":" not in label:
        return None
    try:
        parts = re.split(r"[-/ T:]", label)
        numbers = [int(float(x)) for x in parts]
        return int((datetime(*numbers) - EPOCH).total_seconds())
    except (ValueError, OverflowError):
        return None


def iso(ts):
    return (EPOCH + timedelta(seconds=ts)).isoformat(sep=" ") if ts is not None else ""


def duration_text(value):
    label = re.sub(r"\s+", "", clean(value))
    parts = DURATION_PART.findall(label)
    if not parts or DURATION_PART.sub("", label):
        return None
    scales = {"小时": 3600, "分钟": 60, "分": 60, "秒": 1}
    # Sources can write either -5分钟01秒 or -5分钟-01秒 for the same signed duration.
    total = sum(abs(float(n)) * scales[unit] for n, unit in parts)
    return -total if label.startswith("-") else total


def tonnes(value):
    raw = clean(value).replace(",", "").replace(" ", "").casefold()
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(吨|t|千克|kg)?", raw)
    if not match:
        return None
    weight = float(match[1]) / (1000 if match[2] in {"千克", "kg"} else 1)
    return weight if weight > 0 and math.isfinite(weight) else None


def equipment_code(family, value):
    raw = identifier(value)
    if family == "BOF" and raw in ("A", "B", "C"):
        return f"BOF{raw}"
    if family == "CC" and raw in ("A", "B", "C"):
        return f"CC{'ABC'.index(raw) + 1}"
    if raw.isdecimal():
        return f"{family}{raw}"
    return ""


def field_audit(name, column):
    values = [v for v in column if clean(v)]
    kinds = Counter(type(v).__name__ for v in values)
    result = {"field": name, "present": len(values), "missing": len(column) - len(values),
              "cell_types": dict(sorted(kinds.items()))}
    if any(word in name for word in ("时间", "时刻", "周期", "耗时", "时长")):
        formats = Counter()
        negative = 0
        for v in values:
            duration = duration_text(v)
            if timestamp(v) is not None:
                formats["dated_timestamp"] += 1
            elif duration is not None:
                formats["duration_text"] += 1
                negative += duration < 0
            elif TIME_ONLY.fullmatch(clean(v)):
                formats["clock_only"] += 1
            elif re.fullmatch(r"[+-]?\d+(?:\.\d+)?", clean(v)):
                formats["numeric_unit_not_assigned"] += 1
            else:
                formats["unparsed"] += 1
        result["time_formats"] = dict(sorted(formats.items()))
        result["negative_duration_text"] = negative
    if "重量" in name or name in {"皮重", "罐皮重"}:
        parsed = [x for v in values if (x := tonnes(v)) is not None]
        result["positive_weight_parse_count"] = len(parsed)
    if name in {"连铸机号", "连铸机设备号", "转炉设备号", "LF设备号", "LF精炼设备号",
                "RH设备号", "热修位设备号", "翻渣设备号1", "翻渣设备号2", "精炼路径"}:
        result["label_counts"] = dict(sorted(Counter(clean(v) for v in values).items()))
    return result


def read_workbooks(raw_dir):
    all_rows = defaultdict(lambda: defaultdict(list))
    audits = []
    for filename, (kind, fixed_device) in FILENAMES.items():
        path = raw_dir / filename
        engine = "calamine" if path.suffix == ".xls" else "openpyxl"
        file_audit = {"file": filename, "sha256": sha256(path.read_bytes()).hexdigest(),
                      "engine": engine, "sheets": []}
        with pd.ExcelFile(path, engine=engine) as workbook:
            for sheet in workbook.sheet_names:
                table = pd.read_excel(workbook, sheet_name=sheet, dtype=object)
                if "熔炼号" not in table.columns:
                    raise ValueError(f"{filename}/{sheet} missing 熔炼号")
                keys = [identifier(v) for v in table["熔炼号"]]
                valid = Counter(k for k in keys if valid_heat(k))
                exact = table.duplicated()
                sheet_audit = {
                    "sheet": sheet, "rows": len(table), "column_count": len(table.columns),
                    "columns": [field_audit(name, table[name]) for name in table.columns],
                    "heat_missing": keys.count(""), "placeholder_minus2": keys.count("-2"),
                    "other_invalid_keys": sum(not valid_heat(k) and k not in ("", "-2") for k in keys),
                    "distinct_valid_heats": len(valid), "repeated_heat_groups": sum(n > 1 for n in valid.values()),
                    "rows_with_repeated_valid_heat": sum(n for n in valid.values() if n > 1),
                    "exact_duplicate_rows_after_first": int(exact.sum()),
                }
                file_audit["sheets"].append(sheet_audit)
                columns = list(table.columns)
                for number, values in enumerate(table.itertuples(index=False, name=None), start=2):
                    values = dict(zip(columns, values))
                    key = identifier(values["熔炼号"])
                    ladle = next((id_ for key in ("钢包号", "钢水罐号", "罐号")
                                  if (id_ := identifier(values.get(key)))), "")
                    temp_ladle = next((id_ for key in ("临时罐号", "钢水罐临时罐号")
                                       if (id_ := identifier(values.get(key)))), "")
                    row = {
                        "heat": key, "kind": kind, "file": filename, "engine": engine, "sheet": sheet,
                        "row": number, "values": values,
                        "mark": identifier(values.get("出钢记号")),
                        "ladle": ladle, "temp_ladle": temp_ladle,
                        "converter": fixed_device if kind == "BOF" else equipment_code("BOF", values.get("转炉设备号")),
                        "caster": fixed_device if kind == "CC" else equipment_code(
                            "CC", clean(values.get("连铸机设备号")) or clean(values.get("连铸机号")),
                        ),
                        "lf": fixed_device if kind == "LF" else equipment_code("LF", values.get("LF设备号")),
                        "rh": fixed_device if kind == "RH" else equipment_code("RH", values.get("RH设备号")),
                        "route": identifier(values.get("精炼路径")),
                        "campaign": identifier(values.get("连连浇号")),
                        "order": identifier(values.get("浇次号")) if kind == "plan" else "",
                        "exact_duplicate": bool(exact.iloc[number - 2]),
                        "repeated_in_sheet": valid[key] > 1,
                    }
                    all_rows[key][kind].append(row)
        audits.append(file_audit)
    return all_rows, audits


def locate(row, column=None):
    base = f"{row['file']}|{row['sheet']}|{row['row']}"
    return f"{base}|{column}" if column else base


def resolve_rows(rows, context, fields):
    """Return single cross-checked record or a reason, without dropping candidates."""
    if not rows:
        return None, "missing"
    compatible = []
    for row in rows:
        conflicts = [f for f in fields if context.get(f) and row.get(f) and context[f] != row[f]]
        if not conflicts:
            compatible.append(row)
    if len(compatible) == 1:
        best = compatible[0]
        if len(rows) == 1:
            return best, "unique"
        if any(context.get(f) and context[f] == best.get(f) for f in (*fields, "temp_ladle")):
            return best, "resolved_by_identity"
    if len(compatible) > 1 and context.get("temp_ladle"):
        exact_temporary_ladle = [row for row in compatible if row["temp_ladle"] == context["temp_ladle"]]
        if len(exact_temporary_ladle) == 1:
            return exact_temporary_ladle[0], "resolved_by_temporary_ladle"
    return None, "identity_conflict" if not compatible else "ambiguous"


def reconcile(by_heat, heat):
    if not valid_heat(heat):
        return {}, {}, {}
    chosen = {}
    statuses = {}
    plan, statuses["plan"] = resolve_rows(by_heat["plan"], {}, ())
    if plan:
        chosen["plan"] = plan
    master, statuses["master"] = resolve_rows(by_heat["master"], plan or {}, ("mark", "converter", "caster"))
    if master:
        chosen["master"] = master
    context = {**(plan or {})}
    for field in ("converter", "caster", "lf", "rh", "ladle", "mark", "temp_ladle", "campaign"):
        if master and master.get(field):
            context[field] = master[field]
    for kind, keys in {
        "BOF": ("mark", "converter", "ladle"), "LF": ("mark", "lf", "ladle"),
        "RH": ("mark", "rh", "ladle"), "CC": ("mark", "caster", "ladle", "campaign"),
    }.items():
        row, statuses[kind] = resolve_rows(by_heat[kind], context, keys)
        if row:
            chosen[kind] = row
    return chosen, statuses, context


def event_equipment(row, family, role):
    if family == "SLAG":
        key = "翻渣设备号2" if role.startswith("slag_second") else "翻渣设备号1"
        return clean(row["values"].get(key)) or "SLAG:unknown"
    if family == "TRANSFER":
        return "TRANSFER:unknown"
    if family == "REPAIR":
        return equipment_code("REPAIR", row["values"].get("热修位设备号")) or "REPAIR:unknown"
    return row.get({"BOF": "converter", "CC": "caster", "LF": "lf", "RH": "rh"}.get(family)) or (
        "BOFC" if family == "RETURN_C" else f"{family}:unknown" if family else ""
    )


def assemble_events(by_heat, heat, chosen, statuses, context):
    records = []
    indexed = defaultdict(list)
    for kind, rows in by_heat.items():
        if kind not in FIELDS:
            continue
        for row in rows:
            values = row["values"]
            status = statuses.get(kind, "plan_placeholder" if heat == "-2" else "invalid_heat")
            if chosen.get(kind) is not None and row is not chosen[kind]:
                status = "alternative_row"
            for col, (role, family) in FIELDS[kind].items():
                if col not in values or not clean(values[col]):
                    continue
                time_s = timestamp(values[col])
                mass_col = WEIGHTS.get(kind, {}).get(role, "")
                rec = {
                    "heat_id": heat, "record_type": "plan" if kind == "plan" else "actual",
                    "event": role, "equipment": event_equipment(row, family, role),
                    "time_s": time_s, "timestamp": iso(time_s), "raw_time": clean(values[col]),
                    "time_status": "parsed" if time_s is not None else "unparsed",
                    "source_file": row["file"], "source_sheet": row["sheet"], "source_row": row["row"],
                    "source_column": col, "source_column_index": list(values).index(col) + 1,
                    "source_engine": row["engine"], "tap_mark": row["mark"], "ladle_id": row["ladle"],
                    "temporary_ladle_id": row["temp_ladle"], "route": context.get("route", row["route"]),
                    "campaign": context.get("campaign", row["campaign"]), "plan_order": context.get("order", row["order"]),
                    "link_status": status, "event_status": "raw_retained",
                    "confidence": "unresolved", "gross_weight_t": tonnes(values.get(mass_col)),
                    "weight_source_column": mass_col if mass_col and clean(values.get(mass_col)) else "",
                    "_kind": kind, "_source": locate(row, col), "_row": row,
                }
                records.append(rec)
                if time_s is not None and chosen.get(kind) is row:
                    indexed[role].append(rec)
    canonical = {}
    collisions = set()
    for role, choices in indexed.items():
        times = {e["time_s"] for e in choices}
        known_stations = {e["equipment"] for e in choices if e["equipment"] and ":unknown" not in e["equipment"]}
        if len(times) > 1 or len(known_stations) > 1:
            collisions.add(role)
            for event in choices:
                event["event_status"] = "disagreement"
            continue
        best = min(choices, key=lambda e: (e["_kind"] == "master", ":unknown" in e["equipment"]))
        for event in choices:
            event["event_status"] = "canonical" if event is best else "agrees"
            event["confidence"] = "corroborated" if len(choices) > 1 else "single_source"
        canonical[role] = best
    return records, canonical, collisions


def make_samples(heat, route, events, flags):
    samples = []
    source_pairs = {
        "tap_duration": ("tap_start", "tap_end", "", "actual_tapping", "BOF"),
        "lf_duration": ("lf_process_start", "lf_process_end", "", "actual_lf_processing", "LF"),
        "rh_duration": ("rh_enter", "rh_exit", "", "actual_rh_occupancy", "RH"),
        "cast_duration": ("cast_start", "cast_end", "cast", "actual_casting", "CC"),
        "slag_duration": ("slag_start", "slag_end", "", "actual_first_slag_operation", "CC"),
        "cc_seat_to_cast": ("cc_seat", "cast_start", "", "occupation_or_wait_proxy", "CC"),
        "tap_end_plan_error": ("plan_bof_end", "tap_end", "", "plan_converter_end_vs_tap_end_proxy", ""),
        "cast_start_plan_error": ("plan_cast_start", "cast_start", "", "plan_offset", ""),
    }
    if ROUTES.get(route, ())[-1:] == ("LF",):
        source_pairs["lf_to_cc_service"] = ("lf_lift", "cc_seat", "", "lift_to_seat_service_proxy", "")
    if ROUTES.get(route, ())[-1:] == ("RH",):
        source_pairs["rh_to_cc_service"] = ("rh_lift", "cc_seat", "", "lift_to_seat_service_proxy", "")
    for metric, (start_role, end_role, planned, interpretation, actual_kind) in source_pairs.items():
        start, end = events.get(start_role), events.get(end_role)
        if start is None or end is None:
            flags[metric]["missing_or_conflicting_endpoint"] += 1
            continue
        if actual_kind in ("LF", "RH") and route in ROUTES and actual_kind not in ROUTES[route]:
            flags[metric]["actual_route_contradicts_plan"] += 1
            continue
        if actual_kind and (start["_kind"] not in {actual_kind, "master"} or end["_kind"] not in {actual_kind, "master"}):
            flags[metric]["wrong_source_kind"] += 1
            continue
        if actual_kind in {"BOF", "LF", "RH", "CC"} and metric in {
                "tap_duration", "lf_duration", "rh_duration", "cast_duration", "slag_duration",
        } and (start["_row"] is not end["_row"]):
            flags[metric]["different_operation_rows"] += 1
            continue
        seconds = end["time_s"] - start["time_s"]
        if not metric.endswith("plan_error") and (
                seconds < 0 or (seconds == 0 and metric != "cc_seat_to_cast")):
            flags[metric]["nonpositive_interval"] += 1
            continue
        sample = {
            "heat_id": heat, "metric": metric,
            "equipment": (f"{start['equipment']}->{end['equipment']}"
                          if metric.endswith("_service") else end["equipment"] or start["equipment"]),
            "route": route, "day": iso(start["time_s"]).split()[0],
            "start_time_s": start["time_s"], "end_time_s": end["time_s"], "duration_s": seconds,
            "source_start": start["_source"], "source_end": end["_source"],
            "planned_duration_s": None, "source_plan_duration": "", "interpretation": interpretation,
        }
        if planned:
            p0, p1 = events.get("plan_cast_start"), events.get("plan_cast_end")
            if p0 and p1 and p1["time_s"] > p0["time_s"]:
                sample["planned_duration_s"] = p1["time_s"] - p0["time_s"]
                sample["source_plan_duration"] = f"{p0['_source']};{p1['_source']}"
        samples.append(sample)
        flags[metric]["accepted"] += 1
    return samples


def make_tasks(heat, plan, chosen, statuses, context, events, conflicting_events):
    route = plan["route"]
    operations = ROUTES.get(route, ())
    if not operations:
        return []
    stations = {
        "BOF": plan["converter"] or "BOF:unknown", "CC": plan["caster"] or "CC:unknown",
        "LF": context.get("lf") or chosen.get("LF", {}).get("lf") or "LF:unknown",
        "RH": context.get("rh") or chosen.get("RH", {}).get("rh") or "RH:unknown",
        "SLAG": events.get("slag_start", {}).get("equipment") or "SLAG:unknown",
        "TRANSFER": "TRANSFER:unknown",
        "REPAIR": events.get("repair_seat", {}).get("equipment") or "REPAIR:unknown",
        "BOF_RETURN": events.get("return_bof_c_seat", {}).get("equipment") or "BOF_return:unknown",
    }
    stages = ("BOF", *operations, "CC")
    lift_roles = {"BOF": "bof_lift", "LF": "lf_lift", "RH": "rh_lift"}
    arrival_roles = {"LF": "lf_seat", "RH": "rh_seat", "CC": "cc_seat"}
    previous_roles = {"BOF": "tap_end", "LF": "lf_exit", "RH": "rh_exit"}
    legs = [
        (a, b, "loaded", "ef_candidate", lift_roles[a], arrival_roles[b], previous_roles[a])
        for a, b in zip(stages, stages[1:])
    ]
    legs += [
        ("CC", "SLAG", "empty", "ef_candidate", "cc_empty_lift", "slag_start", "cast_end"),
        ("SLAG", "TRANSFER", "empty", "ef_candidate", "", "transfer_seat", "slag_end"),
        ("TRANSFER", "REPAIR", "empty", "interface", "", "repair_seat", "transfer_seat"),
        ("REPAIR", "TRANSFER", "empty", "interface", "repair_lift", "return_transfer_seat", ""),
        ("TRANSFER", "BOF_RETURN", "empty", "interface", "return_ef_lift", "return_bof_c_seat", "return_transfer_seat"),
    ]
    tasks = []
    prev_id = ""
    for number, (orig, dest, load, scope, lift_role, arrival_role, prior_role) in enumerate(legs, 1):
        origin, destination = stations[orig], stations[dest]
        lift, arrival, prior = [events.get(role, {}) for role in (lift_role, arrival_role, prior_role)]
        flags = []
        if ":unknown" in origin or ":unknown" in destination:
            flags.append("unknown_station")
        if any(role in conflicting_events for role in (lift_role, arrival_role, prior_role)):
            flags.append("conflicting_event_time")
        if any(status in {"ambiguous", "identity_conflict"} for kind, status in statuses.items()
               if kind in {orig, dest, "master"}):
            flags.append("ambiguous_or_conflicting_source")
        # RH rotation is a distinct landmark. Do not substitute it for the RH receiving seat.
        # Slag start is an upper proxy for arrival and does not isolate crane travel.
        if dest == "SLAG" and arrival:
            flags.append("slag_start_is_arrival_upper_proxy")
        if orig == "SLAG" and "slag_second_end" in events:
            prior = events["slag_second_end"]
            flags.append("multiple_slag_operations_use_last_completion")
        if lift and arrival and arrival["time_s"] < lift["time_s"]:
            flags.append("observed_arrival_before_lift")
        if prior and lift and lift["time_s"] < prior["time_s"]:
            flags.append("observed_lift_before_process_end")
        mass_observations = [r for r in (lift, arrival, prior if orig == "SLAG" else {})
                             if r.get("gross_weight_t") is not None]
        mass = max((r["gross_weight_t"] for r in mass_observations), default=None)
        by_capacity = [k for k, cap in CAPACITY.items() if mass is not None and mass <= cap and scope == "ef_candidate"]
        if mass is None:
            flags.append("unknown_gross_weight")
        elif mass > max(CAPACITY.values()) and scope == "ef_candidate":
            flags.append("exceeds_all_observed_ef_capacity")
        upper = events.get("plan_cast_start", {}) if dest == "CC" else {}
        task_id = f"{heat}:{number:02d}"
        tasks.append({
            "task_id": task_id, "heat_id": heat, "campaign": plan["campaign"], "plan_order": plan["order"],
            "route": route, "leg": f"{orig}_to_{dest}", "loaded_state": load,
            "origin": origin, "destination": destination, "predecessor_task_id": prev_id,
            "scope": scope, "physical_release_s": None, "release_proxy_s": lift.get("time_s"),
            "process_predecessor_s": prior.get("time_s"), "window_open_s": None,
            "window_close_s": None, "plan_outer_deadline_s": upper.get("time_s"),
            "observed_lift_s": lift.get("time_s"), "observed_arrival_s": arrival.get("time_s"),
            "gross_weight_t": mass, "weight_status": "observed_endpoint_proxy" if mass is not None else "unknown",
            "cranes_by_capacity": ";".join(by_capacity), "eligible_cranes": "",
            "eligibility_status": "topology_and_window_needed" if scope == "ef_candidate" else "boundary_resource_required",
            "link_status": statuses["plan"], "flags": ";".join(flags), "source_plan": locate(plan),
            "source_process_predecessor": prior.get("_source", ""), "source_lift": lift.get("_source", ""),
            "source_arrival": arrival.get("_source", ""), "source_weight": ";".join(
                locate(item["_row"], item["weight_source_column"]) for item in mass_observations),
            "source_plan_outer_deadline": upper.get("_source", ""),
        })
        prev_id = task_id
    return tasks


def aggregate_samples(samples):
    data = pd.DataFrame(samples, columns=CALIBRATION_COLUMNS)
    summary = {}
    for metric, rows in data.groupby("metric", sort=True):
        durations = rows["duration_s"]
        summary[metric] = {
            "n": len(rows), "day_count": int(rows["day"].nunique()),
            "mean_s": round(float(durations.mean()), 3), "p50_s": round(float(durations.median()), 3),
            "p90_s": round(float(durations.quantile(.90)), 3),
            "equipment_counts": {str(k): int(v) for k, v in rows["equipment"].value_counts().sort_index().items()},
            "route_counts": {str(k): int(v) for k, v in rows["route"].value_counts().sort_index().items()},
        }
    return summary


def process(raw_dir, output):
    all_rows, file_audits = read_workbooks(raw_dir)
    events_all, tasks, samples = [], [], []
    joins = defaultdict(Counter)
    calibration_flags = defaultdict(Counter)
    collisions = Counter()
    route_counts = Counter()
    missing_lf_by_route = Counter()
    missing_rh_by_route = Counter()
    plan_matched_by_route = Counter()
    cart_timing = Counter()
    for heat, by_heat in all_rows.items():
        for row in by_heat["master"]:
            detail = row["values"]
            to_lift = duration_text(detail["重罐钢车开至吊罐位到重罐吊起时间"])
            lift = timestamp(detail["重罐吊起时间"])
            tap_end = timestamp(detail["出钢结束时间"])
            if to_lift is not None and lift is not None:
                if tap_end is None:
                    cart_timing["missing_same_row_tap_end"] += 1
                elif to_lift < 0:
                    cart_timing["negative_duration"] += 1
                elif lift - to_lift < tap_end:
                    cart_timing["inferred_cart_arrival_before_tap_end"] += 1
                else:
                    cart_timing["chronologically_compatible"] += 1
        chosen, statuses, context = reconcile(by_heat, heat)
        events, index, conflicted = assemble_events(by_heat, heat, chosen, statuses, context)
        events_all.extend(events)
        if not valid_heat(heat):
            continue
        collisions.update(conflicted)
        for kind, status in statuses.items():
            joins[kind][status] += 1
        plan = chosen.get("plan")
        if plan:
            code = plan["route"]
            route_counts[code or "unknown"] += 1
            if chosen.get("master"):
                plan_matched_by_route[code] += 1
                missing_lf_by_route[code] += not context.get("lf")
                missing_rh_by_route[code] += not context.get("rh")
            tasks.extend(make_tasks(heat, plan, chosen, statuses, context, index, conflicted))
        # Use traceable observations even when no plan row exists. Such samples
        # keep route unknown and cannot provide signed plan errors by themselves.
        route = context.get("route") or "unknown"
        samples.extend(make_samples(heat, route, index, calibration_flags))
    event_totals = Counter(e["event_status"] for e in events_all)
    task_flags = Counter(flag for task in tasks for flag in task["flags"].split(";") if flag)
    audit = {
        "schema": "stage02_b.audit/1",
        "time_axis": "Local naive source timestamps expressed as seconds since 1970-01-01; no timezone conversion.",
        "weights": "Gross mass in tonnes on mapped scale fields; unadorned source scale values are treated as tonnes.",
        "package_versions": {"python": sys.version.split()[0], **{p: version(p) for p in (
            "pandas", "openpyxl", "python-calamine",
        )}},
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "workbooks": file_audits,
        "additional_workbooks_not_mapped": sorted(p.name for p in raw_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".xls", ".xlsx"}
            and not p.name.startswith("~$") and p.name not in FILENAMES),
        "workbook_count": len(file_audits),
        "sheet_count": sum(len(x["sheets"]) for x in file_audits),
        "row_count": sum(s["rows"] for x in file_audits for s in x["sheets"]),
        "link_status_counts_by_book_kind": {kind: dict(sorted(counts.items())) for kind, counts in joins.items()},
        "observed_event_count": len(events_all),
        "canonical_event_count": event_totals["canonical"],
        "event_status_counts": dict(sorted(event_totals.items())),
        "same_event_conflict_heats_by_event": dict(sorted(collisions.items())),
        "plan_valid_unique_heats_by_route": dict(sorted(route_counts.items())),
        "with_unique_master_by_route": dict(sorted(plan_matched_by_route.items())),
        "missing_master_lf_device_by_route": dict(sorted(missing_lf_by_route.items())),
        "missing_master_rh_device_by_route": dict(sorted(missing_rh_by_route.items())),
        "cart_to_lift_time_check": dict(sorted(cart_timing.items())),
        "task_count": len(tasks), "task_flags": dict(sorted(task_flags.items())),
        "tasks_by_leg": dict(sorted(Counter(t["leg"] for t in tasks).items())),
        "calibration": aggregate_samples(samples),
        "calibration_screening": {k: dict(sorted(v.items())) for k, v in sorted(calibration_flags.items())},
    }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(events_all, columns=EVENT_COLUMNS).to_csv(output / "events.csv", index=False, encoding="utf-8")
    pd.DataFrame(tasks, columns=TASK_COLUMNS).to_csv(output / "tasks.csv", index=False, encoding="utf-8")
    pd.DataFrame(samples, columns=CALIBRATION_COLUMNS).to_csv(output / "calibration.csv", index=False, encoding="utf-8")
    (output / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Audited {audit['workbook_count']} workbooks, {audit['sheet_count']} sheets, {audit['row_count']} source rows")
    print(f"Wrote {len(events_all)} event records, {len(tasks)} transport legs, {len(samples)} calibration samples")
    print(f"Local output: {output.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, help="Directory containing the original B workbooks")
    parser.add_argument("--output", type=Path, default=PROCESSED / "stage02_b",
                        help="Exclusive destination under problem/data/processed")
    args = parser.parse_args()
    raw = args.raw
    if raw is None:
        matches = sorted((ROOT / "docs/校赛题目").glob("B*/附数据文件"))
        if len(matches) != 1:
            parser.error("Expected one local B attachment directory; pass --raw explicitly.")
        raw = matches[0]
    raw = raw.resolve()
    output = args.output.resolve()
    processed_root = PROCESSED.resolve()
    if (ROOT not in processed_root.parents or processed_root not in output.parents
            or raw == output or raw in output.parents or output in raw.parents):
        parser.error("--output must be a child of problem/data/processed, separate from the raw directory.")
    missing = [name for name in FILENAMES if not (raw / name).is_file()]
    if missing:
        parser.error(f"Missing workbooks in --raw: {', '.join(missing)}")
    process(raw, output)


if __name__ == "__main__":
    main()
