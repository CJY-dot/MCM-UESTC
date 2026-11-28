"""Reproduce Stage 03-A's paired batch schedules and the online entry check."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

from q1_regressions import legacy_q1
from q1_scenario import (
    CONFIG, ROOT, build_case, digest, online_notice_audit, online_replay_boundary, project_batch, read_json,
)
from q1_scheduler import POLICIES, EPS, solve

LABELS = {
    "speed": "Speed (u/s)", "distance_factor": "Distance factor", "gap": "Safety gap (u)",
    "prep_s": "Preparation (s)", "max_pick_wait_s": "Pickup wait limit (s)",
}
GROUP_METRICS = (
    "target_ef", "completed_ef", "completion_rate", "plan_targets", "on_time", "on_time_rate",
    "late_count", "late_total_s", "late_max_s", "unserved_targets", "waiting_s",
    "censored_wait_count", "empty_run_s", "loaded_run_s", "idle_crane_s",
    "empty_assigned_wait_s", "utilization_by_crane", "min_gap", "min_margin", "gap_threshold",
    "candidate_capacity_rejections", "candidate_gap_rejections", "shifted_supply_count",
    "accepted_capacity_violations", "task_statuses", "supply_continuity",
)


def save(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def protected_output(path):
    directory = path.resolve()
    private = [ROOT / "local", ROOT / "problem/data/processed"]
    if not any(parent.resolve() in directory.parents for parent in private):
        raise ValueError("Production schedules must stay under local/ or problem/data/processed/")
    rel = directory.relative_to(ROOT.resolve())
    tracked = subprocess.run(["git", "ls-files", "--", str(rel)], cwd=ROOT,
                             check=True, capture_output=True, text=True)
    ignored = subprocess.run(["git", "check-ignore", "-q", "--", str(rel)], cwd=ROOT)
    if tracked.stdout or ignored.returncode != 0:
        raise ValueError("Production output must be untracked and Git-ignored")
    return directory


def public_metrics(result):
    return {k: result["metrics"][k] for k in GROUP_METRICS}


def compare_shared_finish(left, right):
    def completed(run):
        return {t["task_id"]: t["waiting_s"] for t in run["tasks"]
                if t["status"] == "completed" and t["required_in_ef_scope"] is True}

    a, b = completed(left), completed(right)
    common = set(a) & set(b)
    count = len(common)
    totals = {rule: sum(sum(rows[tid].values()) for tid in sorted(common))
              for rule, rows in zip(POLICIES, (a, b))}
    return {"count": count, "wait_s_by_policy": totals}


def evaluate(scenario, output, tag, run_log, enforce=False):
    folder = output / tag
    save(folder / "q1_planning_scenario.json", scenario)
    results = []
    for rule in POLICIES:
        start = time.perf_counter()
        result = solve(scenario, rule, enforce_plan=enforce)
        elapsed = time.perf_counter() - start
        save(folder / rule / "q1_dispatch_result.json", result)
        run_log.append({"scenario": tag, "policy": rule, "strict": enforce,
                        "elapsed_s": elapsed, "result_sha256": digest(result),
                        "solver_status": result["solver_status"]})
        results.append(result)
    return results


def load_projection(source):
    bundle_path = source / "q1_instance.json"
    policy_path = source / "q1_policy_view.json"
    truth_path = source / "q1_simulator_truth.json"
    bundle, policy = read_json(bundle_path), read_json(policy_path)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (bundle_path, policy_path, truth_path)}
    online = online_replay_boundary(policy)
    notices = online_notice_audit(policy, read_json(truth_path))
    return project_batch(bundle, policy), online, notices, hashes


def run(args):
    output = protected_output(args.output)
    config = read_json(args.config)
    projected, online, notices, source_hashes = load_projection(args.source)
    run_log = []
    name = projected["id"].rsplit("-", 1)[-1]
    count_basis = {
        "heats": len({t["heat_id"] for t in projected["roster"]}),
        "all_requirements": len(projected["roster"]),
        "confirmed_ef": sum(t["required_in_ef_scope"] is True for t in projected["roster"]),
    }
    summary = {
        "schema": "mcm-uestc.q1-summary/1", "source": f"{name} frozen replay roster",
        "information_mode": "batch_planning", "unit": config["unit"],
        "assumption_config_sha256": digest(config), "count_basis": count_basis,
        "runs": [], "strict_plan_targets": [], "sensitivity": [], "common_completed": {},
        "online_entry_snapshot": online, "online_notice_audit": notices, "legacy_regressions": legacy_q1(),
    }
    for setting in ("base", "optimistic", "conservative"):
        case = build_case(projected, config, setting)
        runs = evaluate(case, output, setting, run_log)
        summary["fixed_plan_overlap_count"] = case["fixed_plan_overlap_count"]
        summary["fixed_plan_max_overlap_s"] = case["fixed_plan_max_overlap_s"]
        for run_result in runs:
            summary["runs"].append({"scenario": setting, "policy": run_result["policy"],
                                    "metrics": public_metrics(run_result)})
        summary["common_completed"][setting] = compare_shared_finish(*runs)
        if setting == "base":
            if not args.no_figures:
                from q1_figures import private_figures

                for record in runs:
                    private_figures(record, output / setting / record["policy"] / "figures")
            strict = evaluate(case, output, "base_strict_targets", run_log, enforce=True)
            for record in strict:
                summary["strict_plan_targets"].append({"policy": record["policy"],
                                                       "metrics": public_metrics(record)})
            summary["base_policy_trace_equal"] = runs[0]["decisions"] == runs[1]["decisions"]
    for key in config["sensitivity"]:
        for level in ("optimistic", "base", "conservative"):
            value = config["scenarios"][level][key]
            case = build_case(projected, config, "base", overrides={key: value})
            for record in evaluate(case, output, f"sensitivity/{key}/{level}", run_log):
                summary["sensitivity"].append({"parameter": LABELS[key], "level": level,
                                               "value": value, "policy": record["policy"],
                                               "metrics": public_metrics(record)})
    for layout in ("dispersed", "crowded"):
        case = build_case(projected, config, "base", layout)
        for record in evaluate(case, output, f"layouts/{layout}", run_log):
            summary["sensitivity"].append({"parameter": "Initial layout", "level": layout,
                                           "value": layout, "policy": record["policy"],
                                           "metrics": public_metrics(record)})
    if args.secondary_source:
        other, boundary, other_notices, other_hashes = load_projection(args.secondary_source)
        scenario = build_case(other, config, "base")
        overlap = len({t["heat_id"] for t in projected["roster"]} &
                      {t["heat_id"] for t in other["roster"]})
        summary["secondary_window"] = {
            "source": other["id"].rsplit("-", 1)[-1], "shared_heats_with_primary": overlap,
            "fixed_plan_overlap_count": scenario["fixed_plan_overlap_count"],
            "fixed_plan_max_overlap_s": scenario["fixed_plan_max_overlap_s"],
            "online_entry_snapshot": boundary,
            "online_notice_audit": other_notices,
            "runs": [{"policy": r["policy"], "metrics": public_metrics(r)}
                     for r in evaluate(scenario, output, "secondary_window", run_log)],
        }
        source_hashes["secondary_window"] = other_hashes
    save(output / "q1_summary.json", summary)
    save(output / "q1_run_record.json", {
        "schema": "mcm-uestc.q1-run-record/1", "inputs_sha256": source_hashes,
        "code_sha256": {p.name: hashlib.sha256(p.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
                        for p in Path(__file__).parent.glob("*.py")},
        "config_sha256": digest(config), "summary_sha256": digest(summary),
        "python": platform.python_version(), "time_unit": "s", "seed": None,
        "rejection_tolerance": EPS, "runs": run_log,
    })
    if args.public_output:
        public = args.public_output
        save(public / "q1_summary.json", summary)
        if not args.no_figures:
            from q1_figures import public_figures

            public_figures(summary, public / "figures")
    print(f"{len(run_log)} deterministic condition/policy runs; private traces validated.")
    for row in summary["runs"]:
        metrics = row["metrics"]
        print(f"{row['scenario']}/{row['policy']}: "
              f"{metrics['completed_ef']}/{metrics['target_ef']} EF, "
              f"{metrics['on_time']}/{metrics['plan_targets']} plan targets on time")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Private B Q1W06 JSON directory")
    parser.add_argument("--secondary-source", type=Path, help="Optional private B corroboration window")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--output", type=Path, required=True, help="Private ignored A output directory")
    parser.add_argument("--public-output", type=Path, help="Publishable aggregate output directory")
    parser.add_argument("--no-figures", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
