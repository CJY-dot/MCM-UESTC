"""Run reproducible conditional Q2 comparisons without modifying Q1."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
import gzip
import json
from pathlib import Path
import platform

import numpy as np
from scipy.stats import t as student_t

from q1_scenario import ROOT, digest
from q2_scenario import B_COMMIT, DELIVERY, MERGE_BASE, condition, file_hash, load_b, load_window
from run_q1 import protected_output, save

POLICIES = ("fcfs", "tight_margin", "quantile_buffer")
WINDOWS = ("Q1W01", "Q1W07")
PUBLIC = ROOT / "stages/03_implementation/A/q2_results"
SENSITIVITIES = {
    "scale_low": {"scale": "scale_low"}, "scale_high": {"scale": "scale_high"},
    "layout_crowded": {"layout": "crowded"},
    "motion_slow": {"motion": "slow"}, "motion_fast": {"motion": "fast"},
    "clearance_low": {"clearance": "low"}, "clearance_high": {"clearance": "high"},
    "quantile_p75": {"quantile": "p75"}, "quantile_p95": {"quantile": "p95"},
    "lf1_empirical": {"override_lf1": "empirical"},
}
METRICS = ("completed_ef", "completion_rate", "on_time", "on_time_rate", "late_total_s",
           "late_max_s", "empty_run_s", "loaded_run_s", "min_gap", "empty_wait_s",
           "handling_s", "braking_s", "hot_ladle_window_exposure_s", "censored_wait_count",
           "wait_pickup_s", "wait_path_s", "wait_drop_s", "wait_seat_s")


def describe(values):
    values = np.asarray(values, dtype=float)
    mean = float(values.mean())
    half = float(student_t.ppf(.975, len(values) - 1) * values.std(ddof=1) / np.sqrt(len(values))) if len(values) > 1 else 0
    return {
        "n": len(values), "mean": mean, "median": float(np.median(values)),
        "p10": float(np.quantile(values, .1)), "p90": float(np.quantile(values, .9)),
        "ci95_mean_t": [mean - half, mean + half] if len(values) > 1 else None,
    }


def compare(rows):
    mappings = {strategy: {rep: metrics for rep, metrics in rows[strategy]} for strategy in POLICIES}
    keys = set(mappings[POLICIES[0]])
    if not keys or any(set(mappings[strategy]) != keys for strategy in POLICIES):
        raise ValueError("Paired analysis requires identical nonempty replicate sets.")
    measurements = {
        strategy: {metric: describe([mappings[strategy][rep][metric] for rep in sorted(keys)])
                   for metric in METRICS}
        for strategy in POLICIES
    }
    for strategy in POLICIES:
        measurements[strategy]["unfinished_rate"] = describe(
            [1 - mappings[strategy][rep]["completion_rate"] for rep in sorted(keys)])
    pairs = {}
    for baseline, alternative in (("fcfs", "tight_margin"), ("fcfs", "quantile_buffer"),
                                   ("tight_margin", "quantile_buffer")):
        pairs[f"{alternative}_minus_{baseline}"] = {
            metric: describe([mappings[alternative][rep][metric] - mappings[baseline][rep][metric]
                              for rep in sorted(keys)])
            for metric in METRICS
        }
    return measurements, pairs


def verify_resources(case, result):
    """Reconcile event-driven holds with half-open resource capacity, including H."""
    events = {e["event_id"]: e for e in result["events"]}
    if len(events) != len(result["events"]):
        raise ValueError("Event identifiers are not unique.")
    holders = {}
    time = -1.
    for event in result["events"]:
        at, ladle = event["time_s"], event["ladle_id"]
        if at < time - 1e-8 or event["visible_at_s"] < at or at > case["horizon_s"] + 1e-8:
            raise ValueError("Invalid event or availability chronology.")
        time = at
        start, end = event.get("holder_before"), event.get("holder_after")
        if end:
            if holders.get(ladle) != start:
                raise ValueError("Incomplete or competing ladle handover.")
            holders[ladle] = end
    capacities = case["capacity"]
    deltas = defaultdict(Counter)
    for interval in result["occupancy"]:
        facility, start, end = (interval[k] for k in ("resource_id", "start_s", "end_s"))
        if start != events[interval["start_event_id"]]["time_s"]:
            raise ValueError("Occupancy opening does not match its event.")
        deltas[facility][start] += interval["demand"]
        if end is not None:
            if end < start or end != events[interval["end_event_id"]]["time_s"]:
                raise ValueError("Occupancy closure does not match its event.")
            deltas[facility][end] -= interval["demand"]
    for facility, changes in deltas.items():
        number = 0
        for at in sorted(changes):
            number += changes[at]
            if not 0 <= number <= capacities[facility]:
                raise ValueError("Half-open resource capacity exceeded.")


def one_condition(job):
    from q2_motion import validate_motion
    from q2_random import build_tape, forecast_map
    from q2_scheduler import simulate

    label, tag, case, source, changes, count, seed, output = job
    folder = output / label / tag
    folder.mkdir(parents=True)
    settings = {"scale": "base", "layout": "dispersed", "motion": "nominal", "clearance": "base",
                "quantile": "p90", "override_lf1": None, "strict": False} | changes
    profile = condition(source["motion_profile"], **{k: settings[k] for k in
                        ("scale", "layout", "motion", "clearance")})
    policy = source["policy_parameters"] | {"model_selection": source["calibration"]["model_selection"]}
    if settings["override_lf1"]:
        policy = policy | {"model_selection": {
            key: ("empirical" if key.startswith("lf_duration/LF1") else model)
            for key, model in policy["model_selection"].items()
        }}
    common_hash = digest({"case": case, "profile": profile, "policy": policy, "settings": settings})
    forecasts = forecast_map(case, policy, policy["model_selection"], int(settings["quantile"][1:]),
                             override_lf1=settings["override_lf1"])
    initial = {c["crane_id"]: c["position_m"]
               for c in profile["initial_state_layouts"][settings["layout"]]}
    save(folder / "settings.json", {"settings": settings, "case_sha256": digest(case),
                                    "common_input_sha256": common_hash, "profile": profile,
                                    "master_seed": seed, "replicates": count})
    rows = {strategy: [] for strategy in POLICIES}
    denominators = None
    with ExitStack() as stack:
        raw = stack.enter_context((folder / "replicates.jsonl.gz").open("wb"))
        sink = stack.enter_context(gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0,
                                                compresslevel=3))
        for rep in range(count):
            tape = build_tape(case, source["calibration"], seed, rep, settings["override_lf1"])
            realization = digest(tape)
            runs = {}
            for strategy in POLICIES:
                result = simulate(case, profile, policy, tape, strategy, settings["quantile"],
                                  strict=settings["strict"], forecasts=forecasts)
                if digest(tape) != realization:
                    raise ValueError("A policy invocation changed a precommitted duration tape.")
                metrics = result["metrics"]
                if metrics["hard_violations"]:
                    raise ValueError("An accepted Q2 result reports a hard violation.")
                verify_resources(case, result)
                tracks = {cid: [] for cid in profile["crane_order"]}
                for segment in result["trajectories"]:
                    tracks[segment["crane_id"]].append(segment)
                if any(not path or abs(path[-1]["t1_s"] - case["horizon_s"]) > 1e-8
                       for path in tracks.values()):
                    raise ValueError("Incomplete five-crane trace.")
                gap = validate_motion(profile, tracks, initial_positions=initial, horizon_s=case["horizon_s"])
                if abs(gap["min_gap"] - metrics["min_gap"]) > 1e-8:
                    raise ValueError("Inconsistent minimum-gap computation.")
                counts = (metrics["target_ef"], metrics["plan_targets"])
                if denominators is None:
                    denominators = counts
                if counts != denominators:
                    raise ValueError("A policy invocation changed the common evaluation population.")
                flat = metrics | {f"wait_{k}_s": v for k, v in metrics["waiting_s"].items()}
                rows[strategy].append((rep, {key: flat[key] for key in METRICS}))
                runs[strategy] = result
            record = {"replicate_id": rep, "master_seed": seed, "common_input_sha256": common_hash,
                      "duration_tape_sha256": realization, "durations_s": tape, "strategies": runs}
            sink.write((json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode())
    statistics, pairs = compare(rows)
    summary = {"window": label, "condition": tag, "settings": settings, "replicates": count,
               "target_ef": denominators[0], "plan_targets": denominators[1],
               "statistics": statistics, "paired": pairs}
    save(folder / "paired.json", summary)
    return summary


def plot_summary(summary, directory):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    chosen = [v for v in summary["experiments"] if v["condition"] == "main"]
    if not chosen:
        return
    fig, axes = plt.subplots(1, 2, figsize=(8.2, 3.7), layout="constrained")
    comparisons = ("tight_margin_minus_fcfs", "quantile_buffer_minus_fcfs",
                   "quantile_buffer_minus_tight_margin")
    labels = ("Margin − FCFS", "Buffer − FCFS", "Buffer − Margin")
    for ax, metric, title in zip(axes, ("completed_ef", "on_time"),
                                ("Completed EF tasks", "On-time seating targets")):
        for w, row in enumerate(chosen):
            for i, name in enumerate(comparisons):
                result = row["paired"][name][metric]
                low, high = result["ci95_mean_t"]
                mean = result["mean"]
                ax.errorbar(mean, i + (-.10 if w == 0 else .10), [[mean - low], [high - mean]],
                            marker=("o", "s")[w], capsize=3, color=("#0072B2", "#D55E00")[w],
                            label=row["window"] if i == 0 else None)
        ax.set_yticks(range(3), labels)
        ax.axvline(0, color="0.55", linewidth=.8)
        ax.set_title(title)
        ax.set_xlabel("Paired mean difference (tasks), 95% CI")
        ax.legend(frameon=False)
    fig.suptitle("Q2 conditional replay · fixed model and window")
    for suffix in ("png", "pdf"):
        kwargs = {"metadata": {"CreationDate": None}} if suffix == "pdf" else {}
        fig.savefig(directory / f"q2_paired_differences.{suffix}", dpi=300, **kwargs)
    plt.close(fig)


def run(args):
    source = load_b()
    if source["window_manifest"]["conditional_review_pair"] != list(WINDOWS):
        raise ValueError("B conditional window selection differs from experiment.")
    output = protected_output(args.output)
    if output.exists():
        raise ValueError("Choose a new output directory.")
    if args.publish and (args.pilot or args.replicates != 500 or args.sensitivity != 200):
        raise ValueError("The public experiment requires the registered replicate counts.")
    output.mkdir(parents=True)
    counts = {"main": 3 if args.pilot else args.replicates,
              "strict": 3 if args.pilot else args.replicates}
    jobs, window_records = [], {}
    for label, directory in zip(WINDOWS, (args.w01, args.w07)):
        case, hashes = load_window(directory, label, source)
        save(output / f"{label}_case.json", case)
        window_records[label] = {"case_sha256": digest(case), "input_sha256": hashes,
                                 "tasks": len(case["tasks"])}
        conditions = {"main": {}, "strict": {"strict": True}}
        if not args.pilot:
            conditions |= SENSITIVITIES
        jobs.extend((label, tag, case, source, changes, counts.get(tag, args.sensitivity),
                     args.seed, output) for tag, changes in conditions.items())
    if args.workers == 1:
        runs = list(map(one_condition, jobs))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as workers:
            runs = list(workers.map(one_condition, jobs))
    summary = {"schema": "mcm-uestc.q2-experiments/1", "pr9_merge": MERGE_BASE,
               "b_head": B_COMMIT, "master_seed": args.seed, "pilot": args.pilot,
               "scope": "offline_conditioned_on_the_frozen_rosters_and_approved_motion",
               "windows": list(WINDOWS), "experiments": runs}
    save(output / "summary.json", summary)
    save(output / "manifest.json", {"windows": window_records, "b_inputs": source["hashes"],
                                   "python": platform.python_version(),
                                   "code_sha256": {p.name: file_hash(p, normalize=True)
                                                   for p in Path(__file__).parent.glob("*q2*.py")},
                                   "outputs": {p.relative_to(output).as_posix(): file_hash(p)
                                               for p in sorted(output.rglob("*")) if p.is_file()}})
    if args.publish:
        # Only summary metrics and plots are exported from the private experiment.
        save(PUBLIC / "q2_summary.json", summary)
        plot_summary(summary, PUBLIC)
    print(f"Completed {len(runs)} window/condition groups, "
          f"{sum(r['replicates'] for r in runs)} paired replicates.")


def main():
    data = ROOT / "problem/data/processed"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--w01", type=Path, default=data / "stage03_a_q2_w01")
    parser.add_argument("--w07", type=Path, default=data / "stage03_a_q2_w07")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replicates", type=int, default=500)
    parser.add_argument("--sensitivity", type=int, default=200)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261128)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if min(args.replicates, args.sensitivity, args.workers) < 1:
        parser.error("Counts and worker number must be positive.")
    run(args)


if __name__ == "__main__":
    main()
