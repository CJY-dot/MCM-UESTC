"""Stage 03 Q2 calibration for the approved Stage 02 duration ledger."""

from __future__ import annotations

import hashlib
import json
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import optimize, special, stats


DURATION_ROLES = {
    "tap_duration": ("tap_start", "tap_end"),
    "lf_duration": ("lf_process_start", "lf_process_end"),
    "rh_duration": ("rh_enter", "rh_exit"),
    "cast_duration": ("cast_start", "cast_end"),
    "slag_duration": ("slag_start", "slag_end"),
}
OTHER_ROLES = {
    "cc_seat_to_cast": ("cc_seat", "cast_start"),
    "lf_to_cc_service": ("lf_lift", "cc_seat"),
    "rh_to_cc_service": ("rh_lift", "cc_seat"),
    "tap_end_plan_error": ("plan_bof_end", "tap_end"),
    "cast_start_plan_error": ("plan_cast_start", "cast_start"),
}
PROBS = (0.10, 0.50, 0.75, 0.90, 0.95)
TRAIN_MIN = 30
EVALUATION_MIN = 10
BLOCK_MIN = 10
SPLITS = ("train", "validation", "final_holdout")


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def fingerprint(*fields) -> str:
    return hashlib.sha256("\x1f".join(str(f) for f in fields).encode()).hexdigest()


def verify_ledger(processed: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    ledger = read_csv(processed / "calibration.csv")
    events = read_csv(processed / "events.csv")
    events = events[(events.event_status == "canonical") & events.time_s.ne("")].copy()
    events["source"] = (events["source_file"] + "|" + events["source_sheet"] + "|"
                        + events["source_row"] + "|" + events["source_column"])
    lookup = {(e.heat_id, e.source, int(e.time_s), e.event) for e in events.itertuples()}
    plans = events[events.event.isin(("plan_cast_start", "plan_cast_end"))].copy()
    if plans.duplicated(["heat_id", "event"]).any():
        raise ValueError("Casting plan events are not unique per heat and role")
    plan_by_heat = {h: {e.event: e for e in group.itertuples()}
                    for h, group in plans.groupby("heat_id", sort=False)}
    if ledger.duplicated(["heat_id", "metric"]).any():
        raise ValueError("Duplicate heat/metric in approved calibration ledger")
    roles = DURATION_ROLES | OTHER_ROLES
    for row in ledger.itertuples():
        first, last = roles[row.metric]
        start, end = int(row.start_time_s), int(row.end_time_s)
        if (end - start != int(row.duration_s)
                or (row.heat_id, row.source_start, start, first) not in lookup
                or (row.heat_id, row.source_end, end, last) not in lookup):
            raise ValueError("Approved calibration endpoints do not reconcile with canonical events")
        if row.planned_duration_s:
            paired = plan_by_heat.get(row.heat_id, {})
            p_start, p_end = paired.get("plan_cast_start"), paired.get("plan_cast_end")
            expected = int(p_end.time_s) - int(p_start.time_s) if p_start and p_end else None
            if (row.metric != "cast_duration" or not expected or expected <= 0
                    or int(float(row.planned_duration_s)) != expected
                    or row.source_plan_duration != f"{p_start.source};{p_end.source}"):
                raise ValueError("Unverified positive same-semantic casting pair")
    for col in ("start_time_s", "end_time_s", "duration_s"):
        ledger[col] = pd.to_numeric(ledger[col])
    ledger["planned_duration_s"] = pd.to_numeric(ledger["planned_duration_s"], errors="coerce")
    ledger["sample_id"] = [fingerprint(r.heat_id, r.metric, r.source_start, r.source_end)
                           for r in ledger.itertuples()]
    return ledger, plans


def chronological_split(ledger: pd.DataFrame, plans: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    ledger = ledger.copy()
    start_plans = plans[plans.event == "plan_cast_start"]
    origins = start_plans.set_index("heat_id")
    ledger["cast_device"] = ledger.heat_id.map(origins.equipment).fillna("")
    ledger["campaign"] = ledger.heat_id.map(origins.campaign).fillna("")
    ledger["campaign_order"] = pd.to_numeric(ledger.heat_id.map(origins.plan_order), errors="coerce")
    ledger["campaign_block_id"] = [
        fingerprint(device, campaign) if device and campaign and "unknown" not in device else ""
        for device, campaign in zip(ledger.cast_device, ledger.campaign)
    ]
    ledger["day_block_id"] = [fingerprint(day, device) for day, device in zip(ledger.day, ledger.equipment)]
    ledger["boundary_status"] = ""
    ledger["partition"] = "excluded"
    # The first and last observed dates may be incomplete. Never estimate a closure time from the sampling density.
    days = sorted(ledger.day.unique())
    n = len(days)
    if n < 8:
        raise ValueError("Need at least eight dated days for three chronological partitions")
    active = days[1:-1]
    train_end, validation_end = int(len(active) * 0.6), int(len(active) * 0.8)
    phase = dict.fromkeys(active[:train_end], "train")
    phase.update(dict.fromkeys(active[train_end:validation_end], "validation"))
    phase.update(dict.fromkeys(active[validation_end:], "final_holdout"))
    # Include all planned casting endpoints, even for heats without a calibrated duration.
    intervals = {}
    for row in ledger.itertuples():
        if row.campaign_block_id:
            dates = pd.to_datetime([row.start_time_s, row.end_time_s], unit="s").strftime("%Y-%m-%d")
            intervals.setdefault(row.campaign_block_id, set()).update(dates)
    for row in plans.itertuples():
        p = origins.loc[row.heat_id]
        if p.equipment and p.campaign and "unknown" not in p.equipment:
            block = fingerprint(p.equipment, p.campaign)
            if block in intervals:
                intervals[block].add(pd.to_datetime(int(row.time_s), unit="s").strftime("%Y-%m-%d"))
    for block, dates in intervals.items():
        scope = ledger.campaign_block_id == block
        if any(day not in phase for day in dates):
            ledger.loc[scope, "boundary_status"] = "part_day_boundary_or_outside_coverage"
        else:
            partitions = {phase[d] for d in dates}
            if len(partitions) == 1:
                ledger.loc[scope, "partition"] = next(iter(partitions))
            else:
                ledger.loc[scope, "boundary_status"] = "cross_partition_campaign"
    ledger.loc[ledger.campaign_block_id.eq(""), "boundary_status"] = "unknown_campaign"
    for name in ("heat_id", "campaign_block_id"):
        if ledger[ledger.partition.isin(SPLITS)].groupby(name).partition.nunique().max() > 1:
            raise ValueError("Chronological partition leakage")
    return ledger, {
        "method": "chronological_whole_caster_campaign_purge",
        "phase_days": {p: list(phase.values()).count(p) for p in SPLITS},
        "first_last_days_excluded": 2,
        "planned_and_actual_endpoints_used_for_boundary_purge": True,
        "cutoff_dates": [active[train_end], active[validation_end]],
        "partition_summary": {
            p: {"rows": int(len(group)), "heats": int(group.heat_id.nunique()),
                "campaigns": int(group[group.campaign_block_id.ne("")].campaign_block_id.nunique())}
            for p, group in ledger.groupby("partition", sort=True)
        },
        "excluded_rows_by_boundary": {str(k): int(v) for k, v in
                                     ledger[ledger.partition.eq("excluded")].boundary_status.value_counts().items()},
    }


def weighted_quantiles(values, weights, probs=PROBS):
    order = np.argsort(values, kind="stable")
    sorted_weights = np.asarray(weights)[order]
    cumulative = np.cumsum(sorted_weights) / np.sum(sorted_weights)
    at = np.searchsorted(cumulative, probs, side="left")
    return np.asarray(values)[order][at]


def stat_summary(values, weights):
    center = np.average(values, weights=weights)
    sd = np.sqrt(np.average((values - center) ** 2, weights=weights))
    qs = weighted_quantiles(values, weights)
    return {"mean": float(center), "sd": float(sd),
            "quantiles": {f"p{int(p * 100)}": float(q) for p, q in zip(PROBS, qs)}}


def empirical_cdf(values, weights, grid, side):
    order = np.argsort(values, kind="stable")
    cumulative = np.r_[0.0, np.cumsum(weights[order]) / np.sum(weights)]
    return cumulative[np.searchsorted(values[order], grid, side=side)]


def duration_blocks(frame: pd.DataFrame):
    """Candidate sliding pairs from consecutive planned heats on the same device/day."""
    members = []
    ordered = frame.sort_values(["equipment", "day", "start_time_s", "sample_id"])
    for _, group in ordered.groupby(["equipment", "day"], sort=True):
        seq = list(group.itertuples())
        for left, right in zip(seq, seq[1:]):
            if (left.campaign_block_id == right.campaign_block_id
                    and right.campaign_order == left.campaign_order + 1
                    and left.end_time_s <= right.start_time_s):
                members.append((left, right))
    return members


def fitted_models(train: pd.DataFrame, paired: bool) -> tuple[dict, dict, dict]:
    values = train.model_value.to_numpy()
    weights = train.weight.to_numpy()
    mean = float(np.average(values, weights=weights))
    log_values = np.log(values)
    log_mean = np.average(log_values, weights=weights)
    log_sd = np.sqrt(np.average((log_values - log_mean) ** 2, weights=weights))
    delta = np.log(mean) - log_mean
    scale_factor = mean if paired else 1.0
    laws = {}
    parameters = {}
    if delta > 1e-12 and log_sd > 1e-12:
        shape = float(optimize.brentq(lambda a: np.log(a) - special.digamma(a) - delta, 1e-4, 1e10))
        parameters["gamma"] = {"shape": shape, "scale": mean / shape / scale_factor}
        # Match the fitted lognormal arithmetic mean to the same weighted train anchor.
        ln_scale = mean / np.exp(log_sd**2 / 2)
        parameters["lognormal"] = {"shape": float(log_sd), "scale": float(ln_scale / scale_factor)}
        laws["gamma"] = stats.gamma(shape, scale=mean / shape)
        laws["lognormal"] = stats.lognorm(log_sd, scale=ln_scale)
    donors = [{"sample_id": r.sample_id, "value": r.model_value / scale_factor, "weight": r.weight}
              for r in train.itertuples()]
    block_members = duration_blocks(train)
    block = {}
    discrete = {"empirical": (values, weights)}
    if len(block_members) >= BLOCK_MIN:
        pair_values = np.array([[a.model_value, b.model_value] for a, b in block_members])
        pair_weights = np.array([min(a.weight, b.weight) for a, b in block_members])
        pair_mean = np.average(pair_values, weights=pair_weights, axis=0)
        # Draw one weighted pair, map its two chronological slots separately to the mean anchor.
        pair_used = pair_values / pair_mean if paired else pair_values
        pair_evaluation = pair_used * mean if paired else pair_values
        position_expectation = np.average(pair_used, weights=pair_weights, axis=0)
        if paired and not np.allclose(position_expectation, 1, atol=1e-12, rtol=0):
            raise ValueError("Paired block slots violate the weighted mean anchor")
        discrete["block_pair"] = (pair_evaluation.ravel(), np.repeat(pair_weights, 2))
        block = {
            "pairs": [{"sample_ids": [a.sample_id, b.sample_id], "values": row.tolist(),
                       "weight": float(weight)} for (a, b), row, weight in
                      zip(block_members, pair_used, pair_weights)],
            "position_raw_weighted_mean": pair_mean.tolist(),
            "position_delivered_weighted_mean": position_expectation.tolist(),
            "draw_rule": "probability proportional to pair weight, fixed chronological slot order",
        }
    models = {
        "empirical": {"donors": donors},
        **parameters,
        **({"block_pair": block} if block else {}),
    }
    fitted = {}
    for name, (series, frequencies) in discrete.items():
        fitted[name] = stat_summary(series / scale_factor, frequencies)
    for name, law in laws.items():
        fitted[name] = {"mean": float(law.mean() / scale_factor), "sd": float(law.std() / scale_factor),
                        "quantiles": {f"p{int(p * 100)}": float(v / scale_factor)
                                      for p, v in zip(PROBS, law.ppf(PROBS))}}
    normalized = {"training_mean": mean, "fit_parameters": models, "predictions": fitted,
                  "training_n": int(len(train)), "block_candidate_pairs": len(block_members)}
    return normalized, discrete, laws


def evaluate(frame: pd.DataFrame, discrete, laws):
    observed = frame.model_value.to_numpy()
    weights = frame.weight.to_numpy()
    if not len(observed):
        return None
    q_obs = weighted_quantiles(observed, weights, (.5, .9))
    grid = np.unique(np.concatenate([observed, *(pair[0] for pair in discrete.values())]))
    left = empirical_cdf(observed, weights, grid, "left")
    right = empirical_cdf(observed, weights, grid, "right")
    predictions = {}
    for name, (series, weight) in discrete.items():
        predictions[name] = (weighted_quantiles(series, weight, (.5, .9)),
                             empirical_cdf(series, weight, grid, "left"),
                             empirical_cdf(series, weight, grid, "right"))
    for name, law in laws.items():
        cdf = law.cdf(grid)
        predictions[name] = (law.ppf((.5, .9)), cdf, cdf)
    return {
        name: {"p50_absolute_error": float(abs(quantiles[0] - q_obs[0])),
               "p90_absolute_error": float(abs(quantiles[1] - q_obs[1])),
               "cdf_distance": float(max(np.max(np.abs(p_left - left)), np.max(np.abs(p_right - right))))}
        for name, (quantiles, p_left, p_right) in predictions.items()
    }


def correlation(pairs):
    if len(pairs) < 3:
        return {"n": len(pairs), "pearson": None}
    array = np.asarray(pairs, dtype=float)
    corr = np.corrcoef(array.T)[0, 1] if np.all(array.std(axis=0) > 0) else None
    return {"n": len(pairs), "pearson": float(corr) if corr is not None else None}


def correlation_audit(train_pool):
    by_metric = {}
    for metric, group in train_pool.groupby("metric", sort=True):
        adjacent = []
        centering = group.groupby("equipment").model_value.transform("mean")
        group = group.assign(relative=group.model_value / centering)
        for _, block in group.sort_values(["start_time_s", "sample_id"]).groupby(["equipment", "day"]):
            values = block.relative.to_numpy()
            adjacent.extend(zip(values[:-1], values[1:]))
        same_campaign = [(a.relative, b.relative) for a, b in duration_blocks(group)]
        by_metric[metric] = {"same_device_day_adjacent": correlation(adjacent),
                             "same_device_day_campaign_adjacent": correlation(same_campaign),
                             "independent_pair_covariance_by_design": 0.0}
    # Device-centered values compare related furnace-level histories on the same split.
    frame = train_pool.assign(relative=train_pool.model_value /
                              train_pool.groupby(["metric", "equipment"]).model_value.transform("mean"))
    wide = frame.pivot(index="heat_id", columns="metric", values="relative")
    paired = {}
    for a, b in combinations(wide.columns, 2):
        both = wide[[a, b]].dropna().to_numpy()
        paired[f"{a}/{b}"] = correlation(both)
    return {"per_metric": by_metric, "within_heat_relative_to_device_mean": paired}


def historical_check(ledger):
    """Reproduce the old Stage 02 exploratory statistic with the original split/quantiles."""
    lf = ledger[ledger.metric.eq("lf_duration") & ledger.equipment.eq("LF1")].sort_values(
        ["day", "start_time_s"])
    dates = sorted(lf.day.unique())
    cut = len(dates) - int(np.ceil(len(dates) / 4))
    training = lf[lf.day.isin(dates[:cut])]
    pairs = []
    for _, day in training.groupby("day"):
        values = day.duration_s.to_numpy()
        pairs.extend(zip(values[:-1], values[1:]))
    cast = ledger[ledger.metric.eq("cast_duration") & ledger.planned_duration_s.gt(0)]
    ratios = (cast.duration_s / cast.planned_duration_s).to_numpy()
    center = float(ratios.mean())
    return {
        "lf1": {"train_days": cut, "holdout_days": len(dates) - cut, "train_n": len(training),
                "holdout_n": len(lf) - len(training), "train_same_day_adjacent": correlation(pairs)},
        "cast": {"n": len(cast), "mean": center, "normalized_mean": float((ratios / center).mean()),
                 "p10_p50_p90": np.quantile(ratios, (.1, .5, .9)).tolist()},
    }


def calibrate(processed: Path, private_out: Path) -> tuple[dict, dict, dict]:
    """Build train-only simulator and policy parameters, plus retrospective audit summaries."""
    ledger, plans = verify_ledger(processed)
    history = historical_check(ledger)
    ledger, split = chronological_split(ledger, plans)
    # Row weights are frozen at one per eligible heat/metric, never fitted on held dates.
    ledger["weight"] = 1.0
    ledger["basis"] = ledger.metric.map({name: ("paired_plan_ratio" if name == "cast_duration"
                                               else "historical_seconds") for name in DURATION_ROLES})
    ledger["exclusion_reason"] = ledger.boundary_status
    for condition, reason in (
        (~ledger.metric.isin(DURATION_ROLES), "descriptive_only_no_independent_exogenous_evidence"),
        (ledger.equipment.str.contains("unknown", case=False) | ledger.equipment.eq(""), "unknown_equipment"),
        (ledger.metric.eq("cast_duration") & ~ledger.planned_duration_s.gt(0), "missing_positive_semantic_pair"),
        (ledger.metric.isin(DURATION_ROLES) & ~ledger.duration_s.gt(0), "nonpositive_duration"),
    ):
        ledger.loc[condition & ledger.exclusion_reason.eq(""), "exclusion_reason"] = reason
    ledger["model_value"] = ledger.duration_s.astype(float)
    cast_rows = ledger.metric.eq("cast_duration")
    ledger.loc[cast_rows, "model_value"] = ledger.loc[cast_rows, "duration_s"] / ledger.loc[
        cast_rows, "planned_duration_s"]
    ledger["endpoint_status"] = "canonical_endpoints_verified"
    ledger["censor_status"] = np.where(ledger.boundary_status.eq("part_day_boundary_or_outside_coverage"),
                                       "boundary_coverage_not_established", "complete_observed_endpoints")
    ledger["sampling_weight"] = np.where(ledger.exclusion_reason.eq(""), ledger.weight, 0)
    pool = ledger[ledger.exclusion_reason.eq("")].copy()
    protected = private_out.resolve()
    root = Path(__file__).resolve().parents[2]
    permitted = [(root / "local").resolve(), (root / "problem/data/processed").resolve()]
    if not any(protected.is_relative_to(path) and protected != path for path in permitted):
        raise ValueError("Calibration details require an ignored workspace directory")
    protected.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(protected / "q2_sample_ledger.csv", index=False, lineterminator="\n")
    source_audit = json.loads((processed / "audit.json").read_text(encoding="utf-8"))
    counts = {name: {"source_n": int(len(group)), "eligible_n": int(group.exclusion_reason.eq("").sum()),
                     "reasons": {str(k): int(v) for k, v in group.exclusion_reason.value_counts().items() if k}}
              for name, group in ledger.groupby("metric", sort=True)}
    details = {
        "source_rows": int(len(ledger)), "source_metrics": counts,
        "source_endpoint_screening": source_audit["calibration_screening"],
        "historical_replication": history, "split": split, "strata": {},
        "signed_offsets_descriptive": {
            name: stat_summary(group.duration_s.to_numpy(), group.weight.to_numpy()) | {"n": len(group)}
            for name, group in ledger[ledger.metric.str.endswith("plan_error")].groupby("metric")
        },
    }
    fallback_rule = "metric/device/route -> metric/device -> metric/pool -> no calibrated fit"
    simulator = {"schema": "mcm-uestc.q2-calibration/1", "fit_source": "train_only",
                 "strata": {}, "fallbacks": {}, "fallback_rule": fallback_rule,
                 "ratio_formula": "plan_mean_s * normalized_ratio",
                 "row_weight": "one_per_eligible_heat_metric", "empirical_donors": "authorized_local_only"}
    policy = {"schema": "mcm-uestc.q2-policy-parameters/1", "fit_source": "train_only",
              "strata": {}, "fallbacks": {}, "fallback_rule": fallback_rule,
              "buffer_probability_candidates": [0.75, 0.90, 0.95],
              "buffer_rule": "max(0, Qp - mean); multiply by plan_mean_s for ratio strata"}
    for metric, group in pool.groupby("metric", sort=True):
        scopes = {"pool": group}
        for device, subset in group.groupby("equipment", sort=True):
            scopes[device] = subset
            for route, subroute in subset.groupby("route", sort=True):
                if route:
                    scopes[f"{device}/{route}"] = subroute
        for label, scoped in scopes.items():
            key = f"{metric}/{label}"
            by_phase = {p: scoped[scoped.partition.eq(p)] for p in SPLITS}
            train = by_phase["train"]
            supported = int(len(train)) >= TRAIN_MIN
            parent = f"{metric}/{label.rsplit('/', 1)[0]}" if "/" in label else f"{metric}/pool"
            if not supported:
                fallback = parent if parent != key else None
                if len(train):
                    for output in (simulator, policy):
                        output["fallbacks"][key] = fallback
                details["strata"][key] = {"n": {p: len(which) for p, which in by_phase.items()},
                                          "status": "fallback", "fallback": fallback}
                continue
            paired = metric == "cast_duration"
            pack, discrete, laws = fitted_models(train, paired)
            for name, info in pack["predictions"].items():
                if paired and not np.isclose(info["mean"], 1, atol=1e-12, rtol=0):
                    raise ValueError(f"{key}/{name}: nonunit train prediction mean")
            report = {
                "n": {p: len(which) for p, which in by_phase.items()},
                "basis": "paired_plan_ratio" if paired else "historical_seconds",
                "status": "retrospective_time_held_out" if all(len(by_phase[p]) >= EVALUATION_MIN
                                                               for p in SPLITS[1:]) else "exploratory_small_holdout",
                "block_candidate_pairs": pack["block_candidate_pairs"],
                "training_value_summary": stat_summary(train.model_value.to_numpy(), train.weight.to_numpy()),
            }
            validation = evaluate(by_phase["validation"], discrete, laws)
            chosen = "empirical"
            if validation and len(by_phase["validation"]) >= EVALUATION_MIN:
                chosen = min(validation, key=lambda n: (validation[n]["cdf_distance"],
                                                       validation[n]["p90_absolute_error"], n))
            report["validation_selected"] = chosen
            report["validation"] = validation
            report["final_holdout"] = evaluate(by_phase["final_holdout"], discrete, laws)
            e = pack["predictions"]["empirical"]
            buffers = {
                model: {p: max(0.0, prediction["quantiles"][p] - prediction["mean"])
                        for p in ("p75", "p90", "p95")}
                for model, prediction in pack["predictions"].items()
            }
            split_means = {}
            # A stability estimate is internal to the training period and never refitted on v/test.
            dates = sorted(train.day.unique())
            if len(dates) >= 2:
                cut = len(dates) // 2
                for field, subset in (("early_train", train[train.day.isin(dates[:cut])]),
                                      ("late_train", train[train.day.isin(dates[cut:])])):
                    split_means[field] = {
                        "n": len(subset), "raw_stats": stat_summary(subset.model_value.to_numpy(),
                                                                  subset.weight.to_numpy()),
                    }
            report["train_stability"] = split_means
            details["strata"][key] = report
            simulator["strata"][key] = pack | {"basis": report["basis"]}
            policy["strata"][key] = {
                "basis": report["basis"], "training_n": len(train), "training_mean_raw": pack["training_mean"],
                "predictive_summary": pack["predictions"], "empirical_positive_buffers": buffers["empirical"],
                "positive_buffers_by_model": buffers,
            }
    details["correlation"] = correlation_audit(pool[pool.partition.eq("train")])
    details["eligible_rows_by_partition"] = {str(k): int(v) for k, v in pool.partition.value_counts().items()}
    # Cutoff dates are available to authorized audit only. Keep public aggregates free of calendar positions.
    (protected / "q2_split_private.json").write_text(json.dumps({"cutoff_dates": split.pop("cutoff_dates")},
                                                               indent=2) + "\n", encoding="utf-8")
    return simulator, policy, details
