"""Check frozen Q2 deliveries and write private, independently computed audit evidence."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import gzip
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, quantiles, stdev
import sys

from scipy.stats import t as student_t


ROOT = Path(__file__).resolve().parents[3]
PRIVATE = ROOT / "local/stage03_c2_q2/delivery"
A_PUBLIC = "stages/03_implementation/A"
B_PUBLIC = "stages/03_implementation/B"
POLICIES = ("fcfs", "tight_margin", "quantile_buffer")
WINDOWS = {"Q1W01": (23, 17, 6), "Q1W07": (28, 18, 6)}
MASTER_SEED = 20261128
DEFAULT = dict(scale="base", layout="dispersed", motion="nominal", clearance="base",
               quantile="p90", override_lf1=None, strict=False)
CONDITIONS = {
    "main": {}, "strict": {"strict": True}, "scale_low": {"scale": "scale_low"},
    "scale_high": {"scale": "scale_high"}, "layout_crowded": {"layout": "crowded"},
    "motion_slow": {"motion": "slow"}, "motion_fast": {"motion": "fast"},
    "clearance_low": {"clearance": "low"}, "clearance_high": {"clearance": "high"},
    "quantile_p75": {"quantile": "p75"}, "quantile_p95": {"quantile": "p95"},
    "lf1_empirical": {"override_lf1": "empirical"},
}
SIDECARS = {
    "calibration": "calibration", "policy_parameters": "policy-parameters",
    "motion_profile": "motion-profile", "external_boundary": "external-boundary",
    "window_manifest": "window-manifest", "audit": "audit",
}
REPORTED = (
    "completed_ef", "completion_rate", "on_time", "on_time_rate", "late_total_s",
    "late_max_s", "empty_run_s", "loaded_run_s", "min_gap", "empty_wait_s", "handling_s",
    "braking_s", "hot_ladle_window_exposure_s", "censored_wait_count",
    "wait_pickup_s", "wait_path_s", "wait_drop_s", "wait_seat_s",
)
AUDIT_ONLY = ("censored_wait_total_s", "root_release_without_source", "unfinished_rate")


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                               allow_nan=False) + "\n", encoding="utf-8")


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def file_hash(path, *, text=False):
    if text:
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def catalog(path):
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        sha, filename = line.split("  ", 1)
        if filename in entries or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError(f"Invalid checksum manifest: {path.name}")
        entries[filename] = sha
    return entries


def check_files(directory, members, failures, label, *, git_text=False):
    for name, expected in members.items():
        member = (directory / name).resolve()
        if not member.is_relative_to(directory.resolve()) or member == directory.resolve():
            failures.append({"check": label, "member": name, "error": "path outside delivery"})
            continue
        actual = (file_hash(member, text=git_text and member.suffix in {".py", ".md", ".json", ".txt"})
                  if member.is_file() else None)
        if actual != expected:
            failures.append({"check": label, "member": name, "expected": expected, "actual": actual})


def require(ok, failures, label):
    if not ok:
        failures.append({"check": label})


def selected_profile(sidecar, settings):
    profile = copy.deepcopy(sidecar["scenarios"][settings["scale"]])
    profile["initial_layout"] = settings["layout"]
    profile["motion"]["mobile_450t"] = sidecar["kinematic_sensitivities"][settings["motion"]]
    clearance = profile["clearance"]
    for pair in clearance["adjacent_pairs"]:
        pair["min_distance_m"] = clearance["threshold_sensitivities_m"][settings["clearance"]]
    return profile


def selected_policy(b, settings):
    families = dict(b["calibration"]["model_selection"])
    if settings["override_lf1"] == "empirical":
        for key in families:
            if key.startswith("lf_duration/LF1"):
                families[key] = "empirical"
    return b["policy_parameters"] | {"model_selection": families}


def verify_inputs(a_dir, b_dir):
    failures = []
    pub = {label: catalog(ROOT / path / "Q2_SHA256SUMS.txt")
           for label, path in (("A", A_PUBLIC), ("B", B_PUBLIC))}
    for label, entries in pub.items():
        check_files(ROOT, entries, failures, f"{label} public", git_text=True)
    private_b = catalog(b_dir / "SHA256SUMS.txt")
    require(set(private_b) == {f"q2_{key}.json" for key in SIDECARS}, failures, "B sidecar coverage")
    check_files(b_dir, private_b, failures, "B private")
    manifest = read(a_dir / "manifest.json")
    expected_outputs = {"summary.json", *(f"{window}_case.json" for window in WINDOWS)}
    expected_outputs.update(f"{window}/{condition}/{name}" for window in WINDOWS
                            for condition in CONDITIONS
                            for name in ("settings.json", "replicates.jsonl.gz", "paired.json"))
    actual_outputs = {p.relative_to(a_dir).as_posix() for p in a_dir.rglob("*") if p.is_file()}
    require(set(manifest["outputs"]) == expected_outputs, failures, "A manifest coverage")
    require(actual_outputs == expected_outputs | {"manifest.json"}, failures, "A directory coverage")
    check_files(a_dir, manifest["outputs"], failures, "A private")
    a_code = {f"code/dispatch/{name}": sha for name, sha in manifest["code_sha256"].items()}
    expected_code = {p for p in pub["A"] if p.startswith("code/dispatch/") and "/tests/" not in p}
    require(set(a_code) == expected_code, failures, "A manifest code coverage")
    check_files(ROOT, a_code, failures, "A archive code", git_text=True)
    require(manifest["b_inputs"] == {"private": private_b, "public": pub["B"]},
            failures, "A archived B approval hashes")
    counts = {"a_public_files": len(pub["A"]), "b_public_files": len(pub["B"]),
              "a_manifest_members": len(manifest["outputs"]), "b_private_files": len(private_b),
              "archive_python": manifest["python"], "public_summary_sha256":
              pub["A"][f"{A_PUBLIC}/q2_results/q2_summary.json"]}
    return counts, failures, manifest


def check_case_structure(case, window, failures):
    tasks = {task["id"]: task for task in case["tasks"]}
    holders = defaultdict(set)
    counts = Counter()
    require(len(tasks) == len(case["tasks"]) and case["horizon_s"] > 0
            and not case["initial_occupancy"] and not case["locked_trajectories"],
            failures, f"{window} roster keys, horizon and initial boundary")
    for task in tasks.values():
        holders[task["ladle"]].add(task["heat"])
        for edge in ("predecessor", "previous_cast", "previous_bof", "reuse_predecessor"):
            prior = task[edge]
            if prior is None:
                continue
            counts[edge] += 1
            previous = tasks.get(prior)
            require(previous is not None and previous is not task, failures, f"{window} {edge} reference")
            if previous is None:
                continue
            if edge == "predecessor":
                require((previous["heat"], previous["ladle"], previous["destination"])
                        == (task["heat"], task["ladle"], task["origin"]),
                        failures, f"{window} transport predecessor linkage")
            if edge == "previous_cast":
                require(previous["campaign"] == task["campaign"] and
                        previous["destination"] == task["destination"] and previous["order"] < task["order"],
                        failures, f"{window} caster campaign linkage")
            if edge == "previous_bof":
                require(previous["origin"] == task["origin"] and previous["rho_s"] <= task["rho_s"],
                        failures, f"{window} converter linkage")
        visited, cursor = {task["id"]}, task["predecessor"]
        while cursor in tasks and cursor not in visited:
            visited.add(cursor)
            cursor = tasks[cursor]["predecessor"]
        require(cursor not in visited, failures, f"{window} transport predecessor cycle")
    require(None not in holders and all(len(heat_ids) == 1 for heat_ids in holders.values())
            and counts["reuse_predecessor"] == 0, failures, f"{window} frozen case identity check")
    return {key: counts[key] for key in ("predecessor", "previous_cast", "previous_bof", "reuse_predecessor")}


def load_inputs(a_dir, b_dir, manifest, failures):
    b = {key: read(b_dir / f"q2_{key}.json") for key in SIDECARS}
    for key, suffix in SIDECARS.items():
        require(b[key]["schema"] == f"mcm-uestc.q2-{suffix}/1", failures, f"B schema {key}")
    public = read(ROOT / B_PUBLIC / "q2_parameter_pack.json")
    require(public["schema"] == "mcm-uestc.q2-parameter-pack/1"
            and public["parent_schema"] == "mcm-uestc.dispatch/1", failures, "B public schema")
    for key in ("policy_parameters", "motion_profile", "external_boundary"):
        require(b[key] == public[key], failures, f"B approval {key}")
    require(public["model_selection"]["families"] == b["calibration"]["model_selection"],
            failures, "B frozen family selection")
    require(b["window_manifest"] == read(ROOT / B_PUBLIC / "q2_window_manifest.json"),
            failures, "B approved window manifest")
    require(b["window_manifest"]["conditional_review_pair"] == list(WINDOWS)
            and not b["window_manifest"]["verified_independent_competition_windows"],
            failures, "B window scope")
    require(not b["external_boundary"]["independent_exogenous_timestamp_models_enabled"],
            failures, "B exogenous draw prohibited")
    require(digest(b["policy_parameters"]) == b["audit"]["policy_parameter_sha256"],
            failures, "B policy canonical hash")
    summary = read(a_dir / "summary.json")
    require(summary == read(ROOT / A_PUBLIC / "q2_results/q2_summary.json"),
            failures, "A archive and public summary")
    require(summary["schema"] == "mcm-uestc.q2-experiments/1"
            and summary["master_seed"] == MASTER_SEED and summary["windows"] == list(WINDOWS)
            and not summary["pilot"]
            and summary["pr9_merge"] == "6d8aa34bbfb3bf4e60ac84da8305666138978b7c"
            and summary["b_head"] == "0e81b5d05c2aa7234680a127ac890a1bf2e925de",
            failures, "A summary provenance")
    indexed = {(x["window"], x["condition"]): x for x in summary["experiments"]}
    require(len(indexed) == len(summary["experiments"]) == len(WINDOWS) * len(CONDITIONS),
            failures, "A summary condition coverage")
    cases, settings, case_checks = {}, {}, {}
    assumptions = read(ROOT / A_PUBLIC / "q1_assumption_scenarios.json")
    for window, (total, scope, targets) in WINDOWS.items():
        case = cases[window] = read(a_dir / f"{window}_case.json")
        case_checks[window] = check_case_structure(case, window, failures)
        manifest_window = manifest["windows"][window]
        require(digest(case) == manifest_window["case_sha256"] and
                case["assumption_config_sha256"] == digest(assumptions)
                and manifest_window["tasks"] == len(case["tasks"]) == total,
                failures, f"{window} archived case")
        source_folder = ROOT / f"problem/data/processed/stage03_a_q2_{window[-3:].lower()}"
        for name, sha in manifest_window["input_sha256"].items():
            if (source_folder / name).is_file():
                require(file_hash(source_folder / name) == sha, failures, f"{window} {name}")
        ef = [task for task in case["tasks"] if task["required_in_ef_scope"]]
        require(len(ef) == scope
                and sum(t["plan_target_s"] is not None and t["plan_target_s"] <= case["horizon_s"]
                        for t in ef) == targets
                and sum("undeclared_service_path" in t["blocks"] for t in ef) == 4,
                failures, f"{window} fixed denominators and blocked paths")
        for condition in CONDITIONS:
            cfg = settings[window, condition] = read(a_dir / window / condition / "settings.json")
            expected = DEFAULT | CONDITIONS[condition]
            profile = selected_profile(b["motion_profile"], expected)
            common = digest(dict(case=case, profile=profile, policy=selected_policy(b, expected),
                                 settings=expected))
            count = 500 if condition in {"main", "strict"} else 200
            require(cfg == dict(settings=expected, profile=profile, master_seed=MASTER_SEED,
                                case_sha256=digest(case), common_input_sha256=common, replicates=count),
                    failures, f"{window}/{condition} settings and approval")
            path = a_dir / window / condition / "paired.json"
            require(read(path) == indexed.get((window, condition)), failures, f"{window}/{condition} summary slot")
    return b, public, cases, settings, indexed, case_checks


def lines(path):
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for line in source:
            yield json.loads(line)


def describe(values):
    average = mean(values)
    n = len(values)
    q = quantiles(values, n=100, method="inclusive")
    half = float(student_t.ppf(.975, n - 1)) * stdev(values) / math.sqrt(n)
    return dict(n=n, mean=average, median=q[49], p10=q[9], p90=q[89],
                ci95_mean_t=[average - half, average + half])


def differences(observed, reference, key=()):
    if isinstance(observed, dict):
        for name, value in observed.items():
            yield from differences(value, reference.get(name) if isinstance(reference, dict) else None,
                                   key + (name,))
    elif isinstance(observed, list) and isinstance(reference, list) and len(observed) == len(reference):
        for index, (actual, expected) in enumerate(zip(observed, reference)):
            yield from differences(actual, expected, key + (index,))
    elif type(observed) in (int, float) and type(reference) in (int, float):
        if not math.isclose(observed, reference, abs_tol=1e-7, rel_tol=1e-10):
            yield dict(field=key, actual=observed, expected=reference)
    elif observed != reference:
        yield dict(field=key, actual=observed, expected=reference)


def aggregate(values):
    summary, pairs = {}, {}
    for p, series in values.items():
        summary[p] = {k: describe([row[k] for row in series.values()])
                      for k in (*REPORTED, *AUDIT_ONLY)
                      if len(series) > 1 and all(row.get(k) is not None for row in series.values())}
    for i, a in enumerate(POLICIES):
        for b in POLICIES[i + 1:]:
            ids = sorted(set(values[a]) & set(values[b]))
            pairs[f"{b}_minus_{a}"] = {
                k: describe([values[b][r][k] - values[a][r][k] for r in ids])
                for k in (*REPORTED, *AUDIT_ONLY)
                if len(ids) > 1 and all(values[p][r].get(k) is not None for p in (a, b) for r in ids)
            }
    return dict(statistics=summary, paired=pairs)


def policy_signature(result):
    return [{k: v for k, v in decision.items() if k != "strategy"} for decision in result["decisions"]]


def same_results(results):
    first = results[POLICIES[0]]
    for policy in POLICIES[1:]:
        alternative = results[policy]
        if policy_signature(first) != policy_signature(alternative):
            return False
        if any(first[key] != alternative[key] for key in ("events", "trajectories", "occupancy", "tasks", "metrics")):
            return False
    return True


def audit_archive(a_dir, output, b, cases, configurations, references):
    from q2_event_audit import audit_events
    from q2_motion_audit import audit_motion
    from q2_random_audit import TapeAuditor

    reports, selected, prefix, baseline_tapes = {}, [], {}, {}
    with gzip.open(output / "audit_rows.jsonl.gz", "wt", encoding="utf-8") as records:
        for (window, condition), cfg in configurations.items():
            path = a_dir / window / condition / "replicates.jsonl.gz"
            case = cases[window]
            auditor = TapeAuditor(case, cfg, b["calibration"], b["policy_parameters"])
            encountered, totals = set(), Counter()
            statuses, reason_counts = Counter(), Counter()
            candidates, options, task_cranes, successful_states = Counter(), Counter(), Counter(), set()
            prefilter_counts, exclusions, no_dispatch_exclusions = Counter(), Counter(), Counter()
            observable_width, unfinished, auxiliary = Counter(), Counter(), Counter()
            issue_counts, issue_examples = Counter(), {}
            minimums = {}
            metrics = defaultdict(dict)
            seed_ids = {0, 249, 499} if condition in {"main", "strict"} else {0}
            for row in lines(path):
                rep = row["replicate_id"]
                row_issues = []
                screen_overlap = False
                if (type(rep) is not int or rep in encountered or not 0 <= rep < cfg["replicates"]
                        or row["master_seed"] != MASTER_SEED):
                    row_issues.append({"code": "replicate_key"})
                encountered.add(rep)
                if set(row["strategies"]) != set(POLICIES):
                    raise ValueError(f"{window}/{condition}: strategy coverage is incomplete")
                tape = row["durations_s"]
                tape_audit = auditor.audit(row)
                row_issues.extend(tape_audit["issues"])
                totals["tape_matched"] += tape_audit.get("tape") == tape
                totals["observed_service_durations"] += tape_audit["consumed"]
                totals["censored_service_durations"] += tape_audit["censored"]
                tape_sha = digest(tape)
                if tape_sha != row["duration_tape_sha256"] or row["common_input_sha256"] != cfg["common_input_sha256"]:
                    row_issues.append({"code": "record_digest"})
                key = window, rep
                if condition == "main":
                    prefix[key] = tape_sha
                    if rep < 200:
                        baseline_tapes[key] = tape
                elif condition != "lf1_empirical":
                    totals["common_prefix"] += 1
                    if prefix[key] != tape_sha:
                        row_issues.append({"code": "unexpected_tape_difference"})
                else:
                    totals["lf_override_different_tape"] += prefix[key] != tape_sha
                    unaffected = {task["id"] for task in case["tasks"] if task["destination"] != "LF1"}
                    totals["lf_override_common_non_lf1"] += 1
                    if any(tape.get(tid) != baseline_tapes[key].get(tid) for tid in unaffected):
                        row_issues.append({"code": "lf_override_changed_another_device"})
                identical = same_results(row["strategies"])
                totals["all_policy_outputs_identical"] += identical
                row_result = {}
                for policy, result in row["strategies"].items():
                    if result["strategy"] != policy or result["witness_mode"] != (
                            "strict_target_conditional" if cfg["settings"]["strict"]
                            else "lateness_diagnostic_conditional"):
                        row_issues.append({"code": "strategy_or_mode", "policy": policy})
                    motion = audit_motion(result, cfg, case)
                    events = audit_events(result, cfg, case, tape)
                    totals["five_crane_tracks"] += motion["full_track_coverage"]
                    totals["motion_segments"] += motion["segment_count"]
                    derived = events["metrics"] | {"min_gap": motion["min_gap"]}
                    metrics[policy][rep] = derived
                    for kind, check in (("motion", motion), ("event", events)):
                        row_issues.extend(item | {"policy": policy, "check": kind} for item in check["issues"])
                    for name in ("min_gap", "min_margin"):
                        if motion[name] is not None and (name not in minimums or motion[name] < minimums[name]["value"]):
                            minimums[name] = {"value": motion[name], "replicate": rep, "policy": policy,
                                              "witness": motion.get("gap_witness" if name == "min_gap"
                                                                    else "margin_witness")}
                    archived = result["metrics"] | {
                        f"wait_{k}_s": value for k, value in result["metrics"]["waiting_s"].items()}
                    for diff in differences({k: v for k, v in derived.items() if k in archived}, archived):
                        row_issues.append({"code": "metric", "policy": policy, **diff})
                    # Deduplicate success logs with strategy labels removed; check full equality separately.
                    signature = digest(policy_signature(result))
                    if (rep, signature) not in successful_states:
                        successful_states.add((rep, signature))
                        for decision in result["decisions"]:
                            ways = defaultdict(set)
                            for option in decision["candidates"]:
                                ways[option["task_id"]].add(option["crane_id"])
                            candidates[len(ways)] += 1
                            options[len(decision["candidates"])] += 1
                            task_cranes.update(map(len, ways.values()))
                        for task in result["tasks"]:
                            if task["required_in_ef_scope"] and task["status"] != "completed":
                                statuses[task["status"]] += 1
                                reason_counts.update(set(task["reasons"]))
                        candidate_evidence = events.get("candidates", {})
                        prefilter_counts.update({name: candidate_evidence.get(name, 0) for name in (
                            "observed_checkpoints", "checkpoints_without_logged_dispatch")})
                        exclusions.update(candidate_evidence.get("first_failed_screen", {}))
                        no_dispatch_exclusions.update(candidate_evidence.get("no_dispatch_first_failed_screen", {}))
                        observable_width.update(candidate_evidence.get("observable_candidate_count", {}))
                        screen_overlap |= any(
                            int(width) > 1 for width in candidate_evidence.get("observable_candidate_count", {}))
                        unfinished.update(events.get("unfinished", {}).get("classes", {}))
                        for name in ("root_release_without_source", "censored_wait_total_s"):
                            auxiliary[name] += derived.get(name, 0)
                    totals["strategies"] += 1
                    row_result[policy] = dict(
                        metrics=derived, candidates=events.get("candidates"), unfinished=events.get("unfinished"),
                        motion={k: motion[k] for k in ("min_gap", "min_margin", "gap_witness", "margin_witness",
                                                      "full_track_coverage", "segment_count", "tolerance")})
                for issue in row_issues:
                    issue_counts[issue["code"]] += 1
                    issue_examples.setdefault(issue["code"], {"replicate": rep, **issue})
                record = dict(window=window, condition=condition, replicate=rep, recomputed=row_result,
                              issues=row_issues)
                records.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                if rep in seed_ids or screen_overlap:
                    selected.append((window, condition, rep, row, auditor.rebuild(rep), rep in seed_ids))
            calc = aggregate(metrics)
            comparison = copy.deepcopy(calc)
            for summary in comparison["statistics"].values():
                for key in ("censored_wait_total_s", "root_release_without_source"):
                    summary.pop(key, None)
            for pair in comparison["paired"].values():
                for key in AUDIT_ONLY:
                    pair.pop(key, None)
            expected_ids = set(range(cfg["replicates"]))
            reports[f"{window}/{condition}"] = dict(
                rows=len(encountered), ids_complete=encountered == expected_ids,
                totals=totals, issue_counts=issue_counts, issue_examples=issue_examples, minimums=minimums,
                published_differences=list(differences(comparison, references[window, condition])),
                missing_statistics={p: sorted(set(REPORTED) - set(calc["statistics"][p])) for p in POLICIES},
                statistics=calc, unfinished_status=statuses, unfinished_archived_reasons=reason_counts,
                candidate_tasks=candidates, candidate_options=options, vehicles_per_task=task_cranes,
                candidate_checkpoints=prefilter_counts, candidate_exclusions=exclusions,
                no_dispatch_exclusions=no_dispatch_exclusions, observable_candidate_width=observable_width,
                unfinished_from_events=unfinished, additional_event_totals=auxiliary)
            print(f"{window}/{condition}: {len(encountered)} paired, {totals['strategies']} tracks",
                  flush=True)
    return reports, selected


def replay_selected(selected, cases, settings, b):
    # These imports are for black-box replay only; the C judgments never call A's validators.
    sys.path.insert(0, str(ROOT / "code/dispatch"))
    from q2_random import forecast_map
    from q2_scheduler import Dispatch, simulate
    from q2_event_audit import audit_events
    from q2_motion_audit import audit_motion
    from q2_random_audit import future_notice_probe

    class Probe(Dispatch):
        def __init__(self, *args, repeated_times):
            super().__init__(*args)
            self.counts = Counter()
            self.first_reasons = Counter()
            self.repeated_times = repeated_times
            self.same_timestamp_rounds = []

        def count_visible_transport(self):
            count = 0
            for tid in self.open - self.active.keys():
                task = self.tasks[tid]
                origin = self.holders.get(task["ladle"])
                resource = ("seat" if task["service"]["kind"] == "cast"
                            else task["service"]["kind"]) + "/" + task["destination"]
                if (tid in self.released and origin and origin.rsplit("/", 1)[-1] == task["origin"]
                        and (task["predecessor"] is None or task["predecessor"] in self.completed)
                        and (task["previous_cast"] is None or task["previous_cast"] in self.unloaded)
                        and len(self.book.get(resource, ())) + self.claims.get(resource, 0)
                        < self.case["capacity"][resource]):
                    count += 1
            return count

        def candidates(self):
            self.round_reasons = Counter()
            visible = self.count_visible_transport() if self.now in self.repeated_times else None
            options = super().candidates()
            width = len({x["task_id"] for x in options})
            self.counts["checkpoints"] += 1
            self.counts["without_dispatch"] += not options
            self.counts[f"tasks={width}"] += 1
            if visible is not None:
                self.same_timestamp_rounds.append(dict(time_s=self.now, visible_tasks=visible,
                                                       final_tasks=width, attempts=self.round_reasons))
            return options

        def candidate(self, task, crane, source, receiver, yield_first):
            option, reason = super().candidate(task, crane, source, receiver, yield_first)
            label = f"{'yield' if yield_first else 'direct'}:{reason or 'accepted'}"
            self.first_reasons[label] += 1
            self.round_reasons[label] += 1
            return option, reason

    evidence = []
    for window, condition, rep, row, tape, prespecified in selected:
        case, cfg = cases[window], settings[window, condition]
        condition_settings = cfg["settings"]
        policy = selected_policy(b, condition_settings)
        quantile = condition_settings["quantile"]
        strict = condition_settings["strict"]
        forecasts = forecast_map(case, policy, policy["model_selection"], int(quantile[1:]),
                                 override_lf1=condition_settings["override_lf1"])
        original_hash = digest(tape)
        fresh = {}
        blackbox_diagnosis = {}
        independent = {}
        for p in POLICIES:
            times = Counter(d["now_s"] for d in row["strategies"][p]["decisions"])
            probe = Probe(case, cfg["profile"], forecasts, tape, p, strict,
                          repeated_times={t for t, n in times.items() if n > 1})
            result = fresh[p] = probe.run()
            blackbox_diagnosis[p] = dict(checkpoints=probe.counts, candidate_attempts=probe.first_reasons,
                                         same_timestamp_rounds=probe.same_timestamp_rounds)
            independent[p] = (len(audit_events(result, cfg, case, tape)["issues"])
                              + len(audit_motion(result, cfg, case)["issues"]))
        reverse = {
            p: simulate(case, cfg["profile"], policy, tape, p, quantile, strict=strict,
                        forecasts=forecasts) for p in reversed(POLICIES)
        }
        counterfactual = {}
        for p, result in fresh.items():
            def play(variant):
                return simulate(case, cfg["profile"], policy, variant, p, quantile, strict=strict,
                                forecasts=forecasts)

            counterfactual[p] = future_notice_probe(play, tape, result)
        evidence.append(dict(window=window, condition=condition, replicate=rep, prespecified=prespecified, archive_equal={
            p: fresh[p] == row["strategies"][p] for p in POLICIES}, order_equal={
            p: fresh[p] == reverse[p] for p in POLICIES}, tape_unchanged=digest(tape) == original_hash,
            future_duration_counterfactual=counterfactual, independent_issue_counts=independent,
            blackbox_diagnosis=blackbox_diagnosis))
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a-delivery", type=Path, default=ROOT / "local/stage03_a_q2/delivery")
    parser.add_argument("--b-delivery", type=Path, default=ROOT / "local/stage03_q2_b/delivery")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inputs-only", action="store_true")
    args = parser.parse_args()
    a_dir, b_dir, output = args.a_delivery.resolve(), args.b_delivery.resolve(), args.output.resolve()
    if not output.is_relative_to(PRIVATE.resolve()) or output == PRIVATE.resolve() or output.exists():
        parser.error("Output must be a new subdirectory of the ignored C Q2 delivery directory.")
    output.mkdir(parents=True)
    required = {"A": (a_dir / "manifest.json", a_dir / "summary.json"),
                "B": (b_dir / "SHA256SUMS.txt",)}
    missing = [(label, path.name) for label, members in required.items()
               for path in members if not path.is_file()]
    if missing:
        write(output / "input_checks.json", dict(status="NOT VERIFIED", missing=missing))
        print("Missing required delivery files; see private input_checks.json", file=sys.stderr)
        return 2
    counts, failures, manifest = verify_inputs(a_dir, b_dir)
    case_checks = {}
    if not failures:
        loaded = load_inputs(a_dir, b_dir, manifest, failures)
        case_checks = loaded[-1]
    write(output / "input_checks.json", dict(counts=counts, cases=case_checks, discrepancies=failures))
    if failures:
        print(f"{len(failures)} input discrepancies; see private input_checks.json", file=sys.stderr)
        return 2
    print("Input manifests, schemas, approved settings and fixed denominators verified.", flush=True)
    if args.inputs_only:
        return 0
    b, public, cases, settings, references, _ = loaded
    from q2_random_audit import audit_calibration

    calibration = audit_calibration(b["calibration"], b["policy_parameters"], b["audit"], public)
    write(output / "calibration_checks.json", calibration)
    if calibration["issues"]:
        print("Calibration discrepancies; see private calibration_checks.json", file=sys.stderr)
        return 2
    reports, selected = audit_archive(a_dir, output, b, cases, settings, references)
    write(output / "archive_audit.json", reports)
    write(output / "targeted_replay.json", replay_selected(selected, cases, settings, b))
    print(f"Audited {sum(r['rows'] for r in reports.values())} paired archive rows; "
          f"replayed {len(selected)} rows, including {sum(row[-1] for row in selected)} preselected rows. "
          "See private audit evidence.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
