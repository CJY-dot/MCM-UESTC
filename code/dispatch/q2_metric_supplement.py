"""Read the frozen Q2 archive and rebuild metrics without invoking a simulator."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
from scipy.stats import t as student_t

from q2_supplement_tasks import task_metrics
from q2_supplement_tracks import vehicle_metrics
from q2_supplement_wait import waiting_rows

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "local/stage03_a_q2/delivery_parking_fix"
OUTPUT = ROOT / "local/stage03_a_q2_metrics/delivery"
PUBLIC = ROOT / "stages/03_implementation/A/Q2_METRIC_SUPPLEMENT_SUMMARY.json"
SOURCE_DIGEST = "c01157f16f3873e7699d53277fee580ce4769c1643ea805693becec61ef39673"
POLICIES = ("fcfs", "tight_margin", "quantile_buffer")
WINDOWS = {"Q1W01": (23, 17, 6), "Q1W07": (28, 18, 6)}
STATS = ("complete", "censored", "missing", "not_applicable")
NEEDED = ("target_ef", "delivered_ef", "completed_ef", "plan_targets", "on_time",
          "late_count", "unserved_targets", "known_tasks")
COUNTS = {"main": 500, "strict": 500, **dict.fromkeys((
    "scale_low", "scale_high", "layout_crowded", "motion_slow", "motion_fast",
    "clearance_low", "clearance_high", "quantile_p75", "quantile_p95", "lf1_empirical",
), 200)}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path, obj):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(obj, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def verified_archive(path):
    manifest_path = path / "manifest.json"
    if sha(manifest_path) != SOURCE_DIGEST:
        raise ValueError("Unexpected frozen parking-repair archive fingerprint.")
    manifest = read(manifest_path)
    expected = {"summary.json", *(f"{w}_case.json" for w in WINDOWS)}
    expected.update(f"{w}/{c}/{name}" for w in WINDOWS for c in COUNTS
                    for name in ("settings.json", "paired.json", "replicates.jsonl.gz"))
    members = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}
    if set(manifest["outputs"]) != expected or members != expected | {"manifest.json"}:
        raise ValueError("Archive inventory does not match 24 expected conditions.")
    if any(sha(path / f) != h for f, h in manifest["outputs"].items()):
        raise ValueError("Frozen archive member does not match its fingerprint.")
    return manifest


def output_dir(path):
    path = path.resolve()
    if (ROOT / "local").resolve() not in path.parents or path.exists():
        raise ValueError("Output must be a new directory below local/.")
    args = ["--", path.relative_to(ROOT.resolve()).as_posix()]
    ignored = subprocess.run(["git", "check-ignore", "-q", *args], cwd=ROOT)
    tracked = subprocess.check_output(["git", "ls-files", *args], cwd=ROOT)
    if ignored.returncode != 0 or tracked:
        raise ValueError("Detailed outputs must be untracked and Git-ignored.")
    path.mkdir(parents=True)
    return path


def distribution(values, *, paired=False):
    """Empirical summaries. A paired t interval uses one value per replicate."""
    seq = np.asarray(values, dtype=float)
    if not len(seq):
        return {"n": 0, "mean": None, "p50": None, "p90": None, **({"ci95": None} if paired else {})}
    mean = float(seq.mean())
    p50, p90 = (float(q) for q in np.quantile(seq, (.5, .9)))
    answer = {"n": len(seq), "mean": mean, "p50": p50, "p90": p90}
    if paired:
        interval = None
        if len(seq) > 1:
            half = float(student_t.ppf(.975, len(seq) - 1) * seq.std(ddof=1) / np.sqrt(len(seq)))
            interval = [mean - half, mean + half]
        answer["ci95"] = interval
    return answer


def summarize_wait(rows):
    status = Counter(r["status"] for r in rows)
    if status.keys() - set(STATS):
        raise ValueError("Unexpected waiting observation class.")
    complete = [r["duration_s"] for r in rows if r["status"] == "complete"]
    censored = [r["duration_s"] for r in rows if r["status"] == "censored"]
    started = len(complete) + len(censored)
    return {
        "counts": {k: status[k] for k in STATS},
        "right_censored_fraction_of_started": len(censored) / started if started else None,
        "complete_case": distribution(complete),
        "started_duration_lower_bound": distribution(complete + censored),
    }


def casting_gaps(case, result):
    """Same-campaign successive model supply gaps; missing ends remain unknown."""
    tasks = {t["id"]: t for t in case["tasks"]}
    times = {(e["task_id"], e["event_type"]): e["time_s"] for e in result["events"]}
    pairs = []
    for successor in tasks.values():
        prev_id = successor["previous_cast"]
        if not prev_id:
            continue
        predecessor = tasks[prev_id]
        if (predecessor["destination"], predecessor["campaign"]) != (
                successor["destination"], successor["campaign"]):
            raise ValueError("Cross-campaign pairing is not a supply gap.")
        prev_end = times.get((prev_id, "supply_end"))
        curr_start = times.get((successor["id"], "supply_start"))
        seconds = None
        if prev_end is None:
            if curr_start is not None:
                raise ValueError("Supply began before the required preceding cast ended.")
            status = "missing"
        else:
            end = curr_start if curr_start is not None else case["horizon_s"]
            if end < prev_end - 1e-8:
                raise ValueError("Overlapping same-campaign supply events.")
            seconds = max(0., end - prev_end)
            status = "complete" if curr_start is not None else "censored"
        pairs.append({"previous_task": prev_id, "next_task": successor["id"],
                      "status": status, "duration_s": seconds})
    return pairs


def candidates(case, settings, result):
    """Use C's read-only necessary screening, never reopen the scheduler."""
    sys.path.append(str(ROOT / "stages/03_implementation/C"))
    from q2_event_audit import audit_candidates, inspect_event_ledger

    errors = []
    fail = lambda code, **details: errors.append(code)
    tasks = {t["id"]: t for t in case["tasks"]}
    by_task, holds = inspect_event_ledger(result, tasks, case["horizon_s"], fail)
    screening, _ = audit_candidates(result, tasks, case, settings["profile"], settings["settings"],
                                    by_task, holds, fail)
    if errors:
        raise ValueError(f"Archived candidates or event ledger inconsistent: {sorted(set(errors))}")
    # The archive logs only successful rounds. C's distinct timestamps are a
    # separate observable population, even when multiple rounds share a time.
    screening["task_vehicle_options"] = sum(k * n for k, n in screening["option_count_histogram"].items())
    return screening


def same_run(left, right):
    if any(left[k] != right[k] for k in ("tasks", "events", "occupancy", "trajectories", "metrics")):
        return False
    def decisions(r):
        return [{k: v for k, v in d.items() if k != "strategy"} for d in r["decisions"]]
    return decisions(left) == decisions(right)


def check_metrics(rebuilt, recorded, systems, case, result):
    if any(rebuilt[m] != recorded[m] for m in NEEDED if m != "known_tasks"):
        raise ValueError("Recomputed task and seating counts differ from the archived metrics.")
    if rebuilt["known_tasks"] != len(case["tasks"]):
        raise ValueError("Roster coverage changed.")
    sysrow = systems["system"]
    if any(abs(sysrow[new] - recorded[old]) > 1e-6 for new, old in (
        ("empty_move_s", "empty_run_s"), ("loaded_move_s", "loaded_run_s"),
        ("empty_wait_s", "empty_wait_s"), ("braking_s", "braking_s"),
    )):
        raise ValueError("Rebuilt vehicle totals differ from the recorded metrics.")
    if abs(sysrow["covered_s"] - 5 * result["horizon_s"]) > 1e-6:
        raise ValueError("The system time ledger does not cover five complete tracks.")


def update_screen(total, item):
    for k, value in item.items():
        if isinstance(value, dict):
            total.setdefault(k, Counter()).update(value)
        else:
            total[k] = total.get(k, 0) + value


def paired_stats(entries):
    comparisons = (("fcfs", "tight_margin"), ("fcfs", "quantile_buffer"),
                   ("tight_margin", "quantile_buffer"))
    return {f"{b}_minus_{a}": {k: distribution([
        entries[b][rep][k] - entries[a][rep][k] for rep in sorted(entries[a])
    ], paired=True) for k in next(iter(entries[a].values()))} for a, b in comparisons}


def derive(case, settings, result):
    counts, task_rows = task_metrics(case, result)
    waits = waiting_rows(case, result)
    systems = vehicle_metrics(case, settings, result)
    proxy = casting_gaps(case, result)
    screen = candidates(case, settings, result)
    check_metrics(counts, result["metrics"], systems, case, result)
    return counts, task_rows, waits, systems, proxy, screen


def collect(path, output, case, settings, window, condition):
    seen = set()
    summaries = {p: defaultdict(list) for p in POLICIES}
    car_rows = {p: defaultdict(lambda: defaultdict(list)) for p in POLICIES}
    wait_rows = {p: defaultdict(list) for p in POLICIES}
    proxies = {p: [] for p in POLICIES}
    screens = {p: {} for p in POLICIES}
    unique_screens = {}
    pairs = {p: {} for p in POLICIES}
    same = 0
    with gzip.open(path, "rt", encoding="utf-8") as source, output.open("xb") as raw, \
            gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=3, mtime=0) as sink:
        for line in source:
            record = json.loads(line)
            rep = record["replicate_id"]
            if (rep in seen or set(record["strategies"]) != set(POLICIES)
                    or record["common_input_sha256"] != settings["common_input_sha256"]
                    or record["master_seed"] != settings["master_seed"]):
                raise ValueError("Malformed or unpaired archived repeat.")
            seen.add(rep)
            derived = []
            details = {}
            for p in POLICIES:
                result = record["strategies"][p]
                if result["strategy"] != p:
                    raise ValueError("Policy label differs from archive index.")
                existing = next((v for previous, v in derived if same_run(previous, result)), None)
                if existing is None:
                    existing = derive(case, settings, result)
                    derived.append((result, existing))
                    update_screen(unique_screens, existing[-1])
                counts, rows, waits, cars, proxy, screen = existing
                expected = WINDOWS[window]
                if (counts["known_tasks"], counts["target_ef"], counts["plan_targets"]) != expected:
                    raise ValueError("Fixed model denominators changed.")
                update_screen(screens[p], screen)
                running = {f"{k}_rate": counts[k] / counts["target_ef"]
                           for k in ("delivered_ef", "completed_ef")}
                running["seat_on_time_rate"] = counts["on_time"] / counts["plan_targets"]
                values = counts | running
                for key, value in values.items():
                    summaries[p][key].append(value)
                for group, metrics in cars.items():
                    for name, value in metrics.items():
                        car_rows[p][group][name].append(value)
                by_stage = defaultdict(list)
                for row in waits:
                    wait_rows[p][row["stage"], "all"].append(row)
                    wait_rows[p][row["stage"], row["process"]].append(row)
                    by_stage[row["stage"]].append(row)
                totals = {}
                for stage, members in by_stage.items():
                    for status in STATS:
                        totals[f"wait/{stage}/{status}_n"] = sum(row["status"] == status for row in members)
                    totals[f"wait/{stage}/known_time_lower_sum_s"] = sum(
                        row["duration_s"] for row in members if row["duration_s"] is not None)
                # Include a common column set in the paired vectors, including empty stages.
                proxies[p].extend(proxy)
                pairs[p][rep] = values | totals | {
                    f"system/{k}": v for k, v in cars["system"].items()
                } | {f"supply_gap/{status}_n": sum(r["status"] == status for r in proxy)
                     for status in ("complete", "censored", "missing")}
                details[p] = {"tasks": rows, "waits": waits, "vehicles": cars,
                              "supply_gaps": proxy, "candidates": screen}
            same += len(derived) == 1
            row = {"replicate_id": rep, "strategies": details}
            sink.write((json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8"))
    if seen != set(range(COUNTS[condition])) or len(seen) != settings["replicates"]:
        raise ValueError("Archived replicate inventory is incomplete.")
    names = {name for series in pairs.values() for entry in series.values() for name in entry}
    for p in POLICIES:
        for values in pairs[p].values():
            for name in names - values.keys():
                values[name] = 0  # zero rows in an unrepresented stage, not zero task wait
    return {
        "window": window, "condition": condition, "replicates": len(seen),
        "identical_strategy_replicates": same, "candidate_screens_deduplicated": unique_screens,
        "strategies": {p: {
            "tasks": {k: distribution(v) for k, v in summaries[p].items()},
            "vehicle_seconds": {group: {k: distribution(v) for k, v in stats.items()}
                                for group, stats in car_rows[p].items()},
            "waiting": {f"{stage}/{process}": summarize_wait(rows)
                        for (stage, process), rows in wait_rows[p].items()},
            "supply_gap_proxy": summarize_wait(proxies[p]),
            "candidates": screens[p],
        } for p in POLICIES},
        "paired_differences": paired_stats(pairs),
    }


def run(args):
    archive = args.archive.resolve()
    if archive in args.output.resolve().parents:
        raise ValueError("The output directory must be outside the frozen archive.")
    manifest = verified_archive(archive)
    output = output_dir(args.output)
    conditions = []
    for w in WINDOWS:
        case_file = archive / f"{w}_case.json"
        case = read(case_file)
        for c in COUNTS:
            settings = read(archive / w / c / "settings.json")
            if settings["case_sha256"] != manifest["windows"][w]["case_sha256"]:
                raise ValueError("Archive case content digest disagrees with condition.")
            conditions.append(collect(archive / w / c / "replicates.jsonl.gz",
                                      output / f"{w}_{c}.jsonl.gz", case, settings, w, c))
            print(f"{w}/{c}: {conditions[-1]['replicates']} archived replicates checked", flush=True)
    assert sum(c["replicates"] for c in conditions) == 6000
    summary = {
        "schema": "mcm-uestc.q2-metric-supplement/1", "frozen_manifest_sha256": SOURCE_DIGEST,
        "scope": "Q2 frozen parking-repair conditional archive",
        "plant_plan_completion_rate": None, "actual_casting_interruptions": None,
        "actual_fully_feasible_candidate_zero_rounds": None,
        "waiting_percentiles": "Descriptive episodic quantiles. Lower-bound statistics include right censoring.",
        "paired_intervals": "Mean t interval at within-window and condition replicate unit.",
        "experiments": conditions,
    }
    save(output / "summary.json", summary)
    save(output / "manifest.json", {"archive": SOURCE_DIGEST,
                                   "postprocess_c_audit_sha256": sha(ROOT / "stages/03_implementation/C/q2_event_audit.py"),
                                   "code_sha256": {p.name: sha(p) for p in sorted(
                                       Path(__file__).parent.glob("q2_*supplement*.py"))},
                                   "outputs": {p.name: sha(p) for p in sorted(output.iterdir())}})
    if args.publish:
        save(PUBLIC, summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--publish", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
