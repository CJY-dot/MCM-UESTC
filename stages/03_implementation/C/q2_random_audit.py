"""Independent reconstruction of frozen Q2 duration tapes and observable use."""

from bisect import bisect
from collections import Counter, defaultdict
from hashlib import sha256
from itertools import accumulate
import json
from math import exp, fsum, isclose, isfinite, log
from random import Random
from statistics import NormalDist


STRATEGIES = {"fcfs", "tight_margin", "quantile_buffer"}
SERVICE_EVENTS = {
    "refine": ("refine_start", "refine_end"),
    "cast": ("supply_start", "supply_end"),
    "slag": ("slag_start", "slag_end"),
}
DURATION_ATOL_S = 1e-7
TRAIN_ATOL = 1e-10


def canonical_sha256(value):
    """Q2 content hash. Git LF and raw file hashes are separate checks."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def stable_seed(master_seed, replicate_id, heat, stage, event, device):
    if type(master_seed) is not int or type(replicate_id) is not int or replicate_id < 0:
        raise ValueError("The seed and nonnegative replicate ID must be integers")
    if not all(isinstance(label, str) and label for label in (heat, stage, event, device)):
        raise ValueError("Four nonempty text labels are required for the event key")
    key = [master_seed, replicate_id, heat, stage, event, device]
    wire = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return int(sha256(wire).hexdigest()[:32], 16)


def _weighted_mean(values, weights):
    values, weights = list(values), list(weights)
    if not values or len(values) != len(weights):
        raise ValueError("The training pool requires a weight for every observation")
    if any(not isfinite(value) or value <= 0 for value in values + weights):
        raise ValueError("Training values and weights must be finite and positive")
    return fsum(x * w for x, w in zip(values, weights)) / fsum(weights)


def _close(actual, expected):
    return isclose(actual, expected, abs_tol=TRAIN_ATOL, rel_tol=1e-12)


def _weighted_quantile(rows, probability):
    ordered = sorted(rows)
    target = probability * fsum(weight for _, weight in ordered)
    for (value, _), cumulative in zip(ordered, accumulate(weight for _, weight in ordered)):
        if cumulative >= target:
            return value
    return ordered[-1][0]


def audit_calibration(calibration, policy, audit, public=None):
    """Check authorized sidecar internals. Raw partition membership needs its ledger."""
    from scipy.special import gammaincinv

    issues, family_counts = [], Counter()
    profiles, selection = calibration["strata"], calibration["model_selection"]
    stats = audit["statistics"]
    ratio_profiles, block_profiles = 0, 0
    if (calibration.get("schema"), policy.get("schema"), audit.get("schema")) != (
        "mcm-uestc.q2-calibration/1", "mcm-uestc.q2-policy-parameters/1", "mcm-uestc.q2-audit/1"
    ):
        issues.append({"code": "schema_version"})
    if calibration.get("fit_source") != "train_only" or policy.get("fit_source") != "train_only":
        issues.append({"code": "fit_source"})
    if set(profiles) != set(selection) or set(profiles) != set(policy["strata"]):
        issues.append({"code": "strata_coverage"})
    if canonical_sha256(policy) != audit["policy_parameter_sha256"]:
        issues.append({"code": "policy_hash"})
    if calibration.get("fallbacks") != policy.get("fallbacks"):
        issues.append({"code": "fallback_map"})
    if public is not None:
        if public["policy_parameters"] != policy or public["model_selection"]["families"] != selection:
            issues.append({"code": "public_policy_or_selection"})
        if public["sidecar_protocol"]["independent_seed_test_vector_hex"] != f"{stable_seed(123, 1, 'H', 'CC', 'E', 'CC3'):032x}":
            issues.append({"code": "stable_key_vector"})

    for key, profile in profiles.items():
        def issue(code, **fields):
            issues.append({"code": code, "stratum": key, **fields})

        try:
            basis, model = profile["basis"], selection[key]
            fit, report, policy_stratum = profile["fit_parameters"], stats["strata"][key], policy["strata"][key]
            family_counts[model] += 1
            paired = basis == "paired_plan_ratio"
            ratio_profiles += paired
            block_profiles += "block_pair" in fit
            if basis != ("paired_plan_ratio" if key.startswith("cast_duration/") else "historical_seconds"):
                issue("duration_basis")
            if basis != policy_stratum["basis"] or report["basis"] != basis:
                issue("basis_disagreement")
            raw_mean = profile["training_mean"]
            if (not _close(raw_mean, policy_stratum["training_mean_raw"])
                    or not _close(raw_mean, report["training_value_summary"]["mean"])):
                issue("training_raw_mean_metadata")
            donors = fit["empirical"]["donors"]
            by_id = {row["sample_id"]: row for row in donors}
            if not (len(by_id) == len(donors) == profile["training_n"] ==
                    policy_stratum["training_n"] == report["n"]["train"]):
                issue("donor_count_or_duplicate")
            if any(row["weight"] != 1 for row in donors):
                issue("frozen_row_weight")
            expected = 1.0 if paired else raw_mean
            means = {"empirical": _weighted_mean((r["value"] for r in donors), (r["weight"] for r in donors))}
            discrete = {"empirical": [(r["value"], r["weight"]) for r in donors]}
            for family in ("gamma", "lognormal"):
                if family not in fit:
                    continue
                shape, scale = fit[family]["shape"], fit[family]["scale"]
                if not all(isfinite(value) and value > 0 for value in (shape, scale)):
                    issue("invalid_family_parameters", family=family)
                means[family] = shape * scale if family == "gamma" else scale * exp(shape**2 / 2)
            if "block_pair" in fit:
                block = fit["block_pair"]
                pairs = block["pairs"]
                members = [tuple(pair["sample_ids"]) for pair in pairs]
                if (len(set(members)) != len(pairs) or len(pairs) != profile["block_candidate_pairs"]
                        or any(len(ids) != 2 or ids[0] == ids[1] for ids in members)):
                    issue("block_pair_membership_or_duplicates")
                weights = [row["weight"] for row in pairs]
                slot_means = []
                if all(by_id.get(sid) is not None for ids in members for sid in ids):
                    if any(pair["weight"] != min(by_id[sid]["weight"] for sid in ids)
                           for pair, ids in zip(pairs, members)):
                        issue("block_pair_weight")
                    for slot in (0, 1):
                        raw = [by_id[ids[slot]]["value"] * (raw_mean if paired else 1) for ids in members]
                        raw_slot_mean = _weighted_mean(raw, weights)
                        used = [pair["values"][slot] for pair in pairs]
                        slot_mean = _weighted_mean(used, weights)
                        slot_means.append(slot_mean)
                        if (not _close(raw_slot_mean, block["position_raw_weighted_mean"][slot])
                                or not _close(slot_mean, block["position_delivered_weighted_mean"][slot])
                                or (paired and not _close(slot_mean, 1))
                                or any(not _close(y, x / (raw_slot_mean if paired else 1))
                                       for x, y in zip(raw, used))):
                            issue("block_slot_anchor", slot=slot)
                    means["block_pair"] = fsum(slot_means) / 2
                    discrete["block_pair"] = [(value, row["weight"]) for row in pairs for value in row["values"]]
                else:
                    issue("block_uses_nondonor")

            for family, mean in means.items():
                prediction = profile["predictions"][family]
                if not _close(mean, prediction["mean"]):
                    issue("prediction_mean", family=family)
                if (family != "block_pair" or paired) and not _close(mean, expected):
                    issue("duration_mean_anchor", family=family)
                for percentile, value in prediction["quantiles"].items():
                    probability = int(percentile[1:]) / 100
                    if family in discrete:
                        quantile = _weighted_quantile(discrete[family], probability)
                    else:
                        shape, scale = fit[family]["shape"], fit[family]["scale"]
                        quantile = (scale * gammaincinv(shape, probability) if family == "gamma"
                                    else scale * exp(shape * NormalDist().inv_cdf(probability)))
                    if not _close(value, quantile):
                        issue("training_quantile", family=family, percentile=percentile)
                for percentile in ("p75", "p90", "p95"):
                    predicted_buffer = max(0.0, prediction["quantiles"][percentile] - mean)
                    if not _close(policy_stratum["positive_buffers_by_model"][family][percentile],
                                  predicted_buffer):
                        issue("positive_buffer", family=family, percentile=percentile)
            validation = report["validation"]
            chosen = "empirical"
            if validation and report["n"]["validation"] >= 10:
                chosen = min(validation, key=lambda n: (validation[n]["cdf_distance"],
                                                        validation[n]["p90_absolute_error"], n))
            if chosen != model or model != report["validation_selected"] or model not in fit:
                issue("validation_family_selection")
            if profile["predictions"] != policy_stratum["predictive_summary"]:
                issue("policy_predictive_summary")
        except (KeyError, ValueError, TypeError, IndexError, OverflowError) as exc:
            issue("invalid_calibration_stratum", error_type=type(exc).__name__)

    partitions = stats["split"]["partition_summary"]
    if (sum(part["rows"] for part in partitions.values()) != stats["source_rows"]
            or any(n > partitions[k]["rows"] for k, n in stats["eligible_rows_by_partition"].items())):
        issues.append({"code": "split_report_arithmetic"})
    return {"issues": issues, "strata": len(profiles), "ratio_strata": ratio_profiles,
            "block_strata": block_profiles, "frozen_families": dict(family_counts),
            "train_eligible_rows": stats["eligible_rows_by_partition"]["train"],
            "raw_partition_membership": "NOT VERIFIED without the approved dated sample ledger"}


def _role(task):
    kind = task["service"]["kind"]
    destination = task["destination"]
    if kind == "refine" and destination.startswith(("LF", "RH")):
        return f"{destination[:2].lower()}_duration", destination
    if kind == "cast":
        return "cast_duration", destination
    if kind == "slag":
        return "slag_duration", destination.removeprefix("SLAG:") + "倒渣位"
    raise ValueError("Unrecognized calibrated service role")


def _plan(case, calibration, override):
    """Fix task and block membership before any realized service or policy action."""
    tasks, models, groups = {}, {}, defaultdict(list)
    for task in case["tasks"]:
        if task.get("service") is None:
            continue
        tid = task["id"]
        if tid in tasks:
            raise ValueError("Duplicate service task ID")
        tasks[tid] = task
        metric, device = _role(task)
        device_key = f"{metric}/{device}"
        keys = [device_key, f"{metric}/pool"]
        if task.get("route"):
            keys.insert(0, f'{device_key}/{task["route"]}')
        key = next((k for k in keys if k in calibration["strata"]), None)
        family = calibration["model_selection"][key] if key else None
        if key and override and (metric, device) == ("lf_duration", "LF1"):
            family = override
        models[tid] = key, family
        if family == "block_pair" and task.get("campaign") and type(task.get("order")) is int:
            groups[(metric, device, key, task["campaign"])].append(task)

    pairs = []
    for group in groups.values():
        counts = Counter(task["order"] for task in group)
        pending = None
        for task in sorted(group, key=lambda t: (t["order"], t["id"])):
            if (pending is not None and pending["order"] + 1 == task["order"]
                    and counts[pending["order"]] == counts[task["order"]] == 1
                    and pending["heat"] != task["heat"]):
                pairs.append((pending["id"], task["id"]))
                pending = None
            else:
                pending = task
    paired = {tid for pair in pairs for tid in pair}
    for tid, (key, family) in models.items():
        if family == "block_pair" and tid not in paired:
            models[tid] = key, "empirical"
    return tasks, models, pairs


def _weighted_pool(rows):
    weights = [row["weight"] for row in rows]
    if not weights or any(not isfinite(w) or w <= 0 for w in weights):
        raise ValueError("Invalid frozen donor pool")
    return rows, list(accumulate(weights))


def _draw_pool(random, pool):
    rows, cumulative = pool
    index = bisect(cumulative, random.random() * cumulative[-1], hi=len(rows) - 1)
    return rows[index]


def _consumption(result, tasks, durations, horizon):
    """Duration visible in paired start/end events, and a necessary censor bound."""
    by_task = defaultdict(dict)
    issues = []
    consumption_types = {name: side for pair in SERVICE_EVENTS.values()
                         for name, side in zip(pair, ("start", "end"))}
    for event in result["events"]:
        kind = event["event_type"]
        if kind not in consumption_types:
            continue
        tid, at = event["task_id"], event["time_s"]
        side = consumption_types[kind]
        task = tasks.get(tid)
        if task is None or kind not in SERVICE_EVENTS[task["service"]["kind"]]:
            issues.append({"code": "service_event_role"})
        if event["visible_at_s"] < at - DURATION_ATOL_S:
            issues.append({"code": "realized_notice_before_occurrence"})
        if side in by_task[tid]:
            issues.append({"code": "duplicate_service_event", "side": side})
        by_task[tid][side] = at
    complete, censored = 0, 0
    for tid, phases in by_task.items():
        if tid not in durations or "start" not in phases:
            issues.append({"code": "service_event_missing_tape_or_start"})
            continue
        expected_end = phases["start"] + durations[tid]
        if "end" in phases:
            complete += 1
            if not isclose(phases["end"], expected_end, rel_tol=0, abs_tol=DURATION_ATOL_S):
                issues.append({"code": "consumed_duration_mismatch", "delta_s": phases["end"] - expected_end})
        else:
            censored += 1
            if expected_end < horizon - DURATION_ATOL_S:
                issues.append({"code": "uncensored_service_end_absent"})
    return issues, complete, censored


class TapeAuditor:
    """A reusable, independent checker for one archived window and condition."""

    def __init__(self, case, settings, calibration, policy):
        self.settings = settings
        self.calibration = calibration
        self.horizon = case["horizon_s"]
        self.master_seed = settings["master_seed"]
        condition = settings["settings"]
        override = condition["override_lf1"]
        if override not in {None, "empirical"}:
            raise ValueError("Unexpected LF1 sensitivity override")
        self.tasks, self.models, self.pairs = _plan(case, calibration, override)
        self.paired = {tid for pair in self.pairs for tid in pair}
        self.pools = {}
        for key, family in set(self.models.values()):
            if family in {"empirical", "block_pair"}:
                field = "pairs" if family == "block_pair" else "donors"
                self.pools[(key, family)] = _weighted_pool(calibration["strata"][key]["fit_parameters"][family][field])
        models = calibration["model_selection"]
        if override is not None:
            models = {k: (override if k.startswith("lf_duration/LF1") else v) for k, v in models.items()}
        self.case_sha256 = canonical_sha256(case)
        self.common_sha256 = canonical_sha256({
            "case": case, "profile": settings["profile"], "settings": condition,
            "policy": policy | {"model_selection": models},
        })

    def _random(self, replicate, task, event):
        metric, device = _role(task)
        return Random(stable_seed(self.master_seed, replicate, task["heat"], metric, event, device))

    def _seconds(self, tid, value):
        key, _ = self.models[tid]
        plan = self.tasks[tid]["service"]["duration_s"]
        if key is None:
            seconds = plan
        elif self.calibration["strata"][key]["basis"] == "paired_plan_ratio":
            seconds = plan * value
        else:
            seconds = value
        if not isfinite(seconds) or seconds <= 0:
            raise ValueError("The reconstructed duration is not positive and finite")
        return seconds

    def rebuild(self, replicate_id):
        durations = {}
        for pair in self.pairs:
            left = pair[0]
            block = json.dumps(pair, ensure_ascii=False, separators=(",", ":"))
            random = self._random(replicate_id, self.tasks[left], "service_block:" + block)
            donor = _draw_pool(random, self.pools[self.models[left]])
            for tid, value in zip(pair, donor["values"], strict=True):
                durations[tid] = self._seconds(tid, value)
        for tid, (key, family) in self.models.items():
            if tid in self.paired:
                continue
            value = None
            if key is not None:
                random = self._random(replicate_id, self.tasks[tid], f"service_end:{tid}")
                if family == "empirical":
                    value = _draw_pool(random, self.pools[(key, family)])["value"]
                else:
                    spec = self.calibration["strata"][key]["fit_parameters"][family]
                    shape, scale = spec["shape"], spec["scale"]
                    if family == "lognormal":
                        value = random.lognormvariate(log(scale), shape)
                    elif family == "gamma":
                        value = random.gammavariate(shape, scale)
                    else:
                        raise ValueError("Unknown frozen family")
            durations[tid] = self._seconds(tid, value)
        return durations

    def audit(self, row):
        issues = []
        rep = row["replicate_id"]
        if type(rep) is not int or not 0 <= rep < self.settings["replicates"]:
            return {"issues": [{"code": "invalid_replicate_id"}], "consumed": 0, "censored": 0}
        if row["master_seed"] != self.master_seed:
            issues.append({"code": "master_seed"})
        if self.settings["case_sha256"] != self.case_sha256:
            issues.append({"code": "case_hash"})
        if (self.settings["common_input_sha256"] != self.common_sha256
                or row["common_input_sha256"] != self.common_sha256):
            issues.append({"code": "common_input_hash"})
        tape = self.rebuild(rep)
        tape_sha = canonical_sha256(tape)
        if row["duration_tape_sha256"] != canonical_sha256(row["durations_s"]):
            issues.append({"code": "archived_tape_hash"})
        if tape_sha != row["duration_tape_sha256"]:
            unequal = set(tape) ^ set(row["durations_s"])
            unequal |= {k for k, value in tape.items() if k in row["durations_s"]
                        and value != row["durations_s"][k]}
            issues.append({"code": "reconstructed_tape_hash", "mismatched_values": len(unequal)})
        if set(row["strategies"]) != STRATEGIES:
            issues.append({"code": "strategy_coverage"})
        consumed, censored = 0, 0
        for name, result in row["strategies"].items():
            differences, finished, unfinished = _consumption(result, self.tasks, tape, self.horizon)
            issues.extend(diff | {"strategy": name} for diff in differences)
            consumed += finished
            censored += unfinished
        return {"issues": issues, "tape": tape, "tape_sha256": tape_sha,
                "common_input_sha256": self.common_sha256,
                "consumed": consumed, "censored": censored}


def information_prefix(result, cutoff_s):
    """Compare authorized histories and irrevocable decisions before hidden notices."""
    history = [event for event in result["events"] if event["visible_at_s"] <= cutoff_s]
    decisions = [row for row in result["decisions"] if row["now_s"] <= cutoff_s]
    return history, decisions


def future_notice_probe(play, tape, result):
    """Delay one service end unseen at the cutoff and check the action prefix."""
    start_types = {start for start, _ in SERVICE_EVENTS.values()}
    end_types = {end for _, end in SERVICE_EVENTS.values()}
    ends = {event["task_id"]: event["visible_at_s"] for event in result["events"]
            if event["event_type"] in end_types}
    choices = []
    for event in result["events"]:
        tid, start = event["task_id"], event["time_s"]
        if event["event_type"] not in start_types or tid not in ends or ends[tid] <= start:
            continue
        cutoff = ends[tid] - min((ends[tid] - start) / 2, 1e-4)
        actions = sum(start <= decision["now_s"] <= cutoff for decision in result["decisions"])
        choices.append((actions, cutoff, tid))
    if not choices:
        return {"checked": False, "post_start_decisions": 0}
    actions, cutoff, tid = max(choices)
    changed = play(tape | {tid: tape[tid] + 173.0})
    return {"checked": True, "post_start_decisions": actions,
            "unchanged_prefix": information_prefix(changed, cutoff) == information_prefix(result, cutoff),
            "changed_suffix": changed != result}
