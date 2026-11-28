"""Q2 training-only forecasts and privately keyed duration tapes."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from math import isfinite, log
from pathlib import Path
from random import Random

_DATA_DIR = str(Path(__file__).resolve().parents[1] / "data")
if _DATA_DIR not in sys.path:
    sys.path.append(_DATA_DIR)
from q2_schema import stable_event_seed


def _role(task):
    service = task.get("service")
    if service is None:
        return None
    kind, device = service["kind"], task["destination"]
    if kind == "refine":
        if device.startswith("LF"):
            return "lf_duration", device
        if device.startswith("RH"):
            return "rh_duration", device
    elif kind == "cast":
        return "cast_duration", device
    elif kind == "slag":
        return "slag_duration", device.removeprefix("SLAG:") + "倒渣位"
    raise ValueError("Unknown Q2 calibrated service role.")


def _model(task, pack, families, override_lf1):
    role = _role(task)
    if role is None:
        return None, None
    metric, device = role
    by_device = f"{metric}/{device}"
    route = task.get("route")
    keys = ([f"{by_device}/{route}"] if route else []) + [by_device, f"{metric}/pool"]
    for key in keys:
        if key in pack["strata"]:
            if key not in families:
                raise ValueError("A calibrated stratum lacks its frozen model selection.")
            family = families[key]
            if override_lf1 is not None and role == ("lf_duration", "LF1"):
                family = override_lf1
            return key, family
    return None, None


def _allocation(case, pack, families, override_lf1):
    """Pair disjoint adjacent plan positions before any strategy runs."""
    if override_lf1 not in (None, "empirical"):
        raise ValueError("The only registered LF1 family override is empirical.")
    tasks = {task["id"]: task for task in case["tasks"] if task.get("service") is not None}
    if len(tasks) != sum(task.get("service") is not None for task in case["tasks"]):
        raise ValueError("Duplicate service event key in Q2 case.")
    models = {key: _model(task, pack, families, override_lf1) for key, task in tasks.items()}
    groups = defaultdict(list)
    for tid, (stratum, family) in models.items():
        t = tasks[tid]
        if family == "block_pair" and t.get("campaign") and type(t.get("order")) is int:
            groups[_role(t), stratum, t["campaign"]].append(t)
    pairs = []
    paired = set()
    for members in groups.values():
        ordered = sorted(members, key=lambda t: (t["order"], t["id"]))
        duplicate_orders = {a["order"] for a, b in zip(ordered, ordered[1:])
                            if a["order"] == b["order"]}
        i = 0
        while i + 1 < len(ordered):
            left, right = ordered[i:i + 2]
            if (right["order"] == left["order"] + 1 and left["heat"] != right["heat"]
                    and left["order"] not in duplicate_orders and right["order"] not in duplicate_orders):
                pairs.append((left, right))
                paired.add(left["id"])
                paired.add(right["id"])
                i += 2
            else:
                i += 1
    for tid, (stratum, family) in models.items():
        if family == "block_pair" and tid not in paired:
            models[tid] = (stratum, "empirical")
    return tasks, models, pairs


def _duration(task, profile, value):
    if profile is None:
        if value is not None:
            raise ValueError("Uncalibrated event received a stochastic value.")
        seconds = task["service"]["duration_s"]
    elif profile["basis"] == "paired_plan_ratio":
        plan_mean = task["service"]["duration_s"]
        if not isfinite(plan_mean) or plan_mean <= 0:
            raise ValueError("A paired service requires a positive declared plan mean.")
        seconds = plan_mean * value
    elif profile["basis"] == "historical_seconds":
        seconds = value
    else:
        raise ValueError("Unsupported Q2 duration basis.")
    if not isfinite(seconds) or seconds <= 0:
        raise ValueError("A Q2 service duration must be finite and strictly positive.")
    return seconds


def _weighted_row(rng, rows):
    weights = [item["weight"] for item in rows]
    if not weights or any(not isfinite(value) or value <= 0 for value in weights):
        raise ValueError("Q2 requires positive finite frozen donor weights.")
    return rng.choices(rows, weights=weights, k=1)[0]


def _stream(master_seed, replicate_id, task, event):
    metric, device = _role(task)
    seed = stable_event_seed(master_seed, replicate_id, task["heat"], metric, event, device)
    return Random(seed)


def build_tape(case, calibration, master_seed, replicate_id, override_lf1=None):
    """Build one private service-duration realization shared by all strategies."""
    if calibration.get("schema") != "mcm-uestc.q2-calibration/1" or calibration.get("fit_source") != "train_only":
        raise ValueError("Q2 sampling requires the frozen training-only calibration sidecar.")
    tasks, models, pairs = _allocation(case, calibration, calibration["model_selection"], override_lf1)
    durations = {}
    for left, right in pairs:
        stratum = models[left["id"]][0]
        profile = calibration["strata"][stratum]
        event = "service_block:" + json.dumps((left["id"], right["id"]), ensure_ascii=False, separators=(",", ":"))
        rng = _stream(master_seed, replicate_id, left, event)
        pair = _weighted_row(rng, profile["fit_parameters"]["block_pair"]["pairs"])
        for task, value in zip((left, right), pair["values"], strict=True):
            durations[task["id"]] = _duration(task, profile, value)

    for tid, task in tasks.items():
        if tid in durations:
            continue
        stratum, family = models[tid]
        if stratum is None:
            durations[tid] = _duration(task, None, None)
            continue
        profile = calibration["strata"][stratum]
        spec = profile["fit_parameters"][family]
        rng = _stream(master_seed, replicate_id, task, f"service_end:{tid}")
        if family == "empirical":
            value = _weighted_row(rng, spec["donors"])["value"]
        elif family == "gamma":
            value = rng.gammavariate(spec["shape"], spec["scale"])
        elif family == "lognormal":
            value = rng.lognormvariate(log(spec["scale"]), spec["shape"])
        else:
            raise ValueError("Unsupported Q2 model family.")
        durations[tid] = _duration(task, profile, value)
    return dict(sorted(durations.items()))


def _public_forecast(task, policy_parameters, stratum, family, percentile):
    if percentile not in (75, 90, 95):
        raise ValueError("Use a registered training percentile for Q2.")
    if stratum is None:
        return _duration(task, None, None), 0.0
    profile = policy_parameters["strata"][stratum]
    mean = _duration(task, profile, profile["predictive_summary"][family]["mean"])
    buffer = profile["positive_buffers_by_model"][family][f"p{percentile}"]
    if not isfinite(buffer) or buffer < 0:
        raise ValueError("Invalid training-only quantile buffer.")
    return mean, buffer * (task["service"]["duration_s"] if profile["basis"] == "paired_plan_ratio" else 1)


def forecast(task, policy_parameters, families, percentile=90, unpaired=False, override_lf1=None):
    """Return (predicted mean seconds, positive buffer seconds), no private input."""
    stratum, family = _model(task, policy_parameters, families, override_lf1)
    if unpaired and family == "block_pair":
        family = "empirical"
    return _public_forecast(task, policy_parameters, stratum, family, percentile)


def forecast_map(case, policy_parameters, families, percentile=90, override_lf1=None):
    """Resolve training forecasts with the same public plan grouping as the tape."""
    tasks, models, _ = _allocation(case, policy_parameters, families, override_lf1)
    return {tid: _public_forecast(task, policy_parameters, *models[tid], percentile)
            for tid, task in tasks.items()}
