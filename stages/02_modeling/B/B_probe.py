"""Evaluate B's short-window policies using the authorized pilot and audited samples."""
import argparse
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[3]
PRIVATE = ROOT / "problem/data/processed"
PILOT = ROOT / "stages/01_topic_selection/validation/b_crane_dispatch/problem/case.json"
EPS = 1e-7


@dataclass(frozen=True)
class Task:
    name: str
    start: float
    end: float
    mass: float
    release: float
    early: float
    due: float
    predecessor: str = ""
    service: float = 0.0


@dataclass(frozen=True)
class Move:
    start: tuple
    finish: tuple
    ramp: float
    cruise: float
    peak: float

    @property
    def duration(self):
        return 2 * self.ramp + self.cruise

    def phases(self):
        if not self.ramp:
            return [(self.cruise, 0.0, self.peak, 0.0)]
        slope = self.peak / self.ramp
        ramp_progress = self.peak * self.ramp / 2
        return [
            (self.ramp, 0.0, 0.0, slope),
            (self.cruise, ramp_progress, self.peak, 0.0),
            (self.ramp, ramp_progress + self.peak * self.cruise, self.peak, -slope),
        ]


def move(start, finish, speed, acceleration, zone, slow_factor):
    distance = max(abs(b - a) for a, b in zip(start, finish))
    if distance < EPS:
        return Move(start, finish, 0.0, 0.0, 0.0)
    if acceleration and zone and any(
        abs(a - b) > EPS and min(a, b) <= zone[1] and max(a, b) >= zone[0]
        for a, b in zip(start, finish)
    ):
        speed *= slow_factor
    if not acceleration:
        return Move(start, finish, 0.0, distance / speed, speed / distance)
    peak_speed = min(speed, np.sqrt(acceleration * distance))
    ramp = peak_speed / acceleration
    cruise = max(0.0, distance / peak_speed - ramp)
    return Move(start, finish, ramp, cruise, peak_speed / distance)


def minimum_segment_gap(duration, g, v, a):
    values = [g, g + v * duration + a * duration**2 / 2]
    if a > 0.0 and 0.0 < (t := -v / a) < duration:
        values.append(g + v * t + a * t**2 / 2)
    return min(values)


def neighbors(position, index, target, gap):
    new = list(position)
    new[index] = target
    for k in range(index - 1, -1, -1):
        new[k] = min(new[k], new[k + 1] - gap)
    for k in range(index + 1, len(new)):
        new[k] = max(new[k], new[k - 1] + gap)
    return tuple(new)


def dispatch(tasks, cranes, limits, gap, motion, policy, delay_after_first=0.0):
    """A single shared motion reservation at a time. Late deliveries are diagnostic."""
    now = 0.0
    state = tuple(float(c["initial_position"]) for c in cranes)
    task_map = {task.name: task for task in tasks}
    carried = {}
    checked_deliveries = []
    while len(carried) < len(tasks):
        known = []
        for task in task_map.values():
            if task.name in carried or (task.predecessor and task.predecessor not in carried):
                continue
            release = task.release
            if task.predecessor:
                release = max(release, carried[task.predecessor] + task.service)
            known.append((task, release))
        ready = [(task, release) for task, release in known if release <= now + EPS]
        if not ready:
            if not known:
                break
            now = min(release for _, release in known)
            continue
        candidates = []
        for task, release in ready:
            per_task = []
            for k, crane in enumerate(cranes):
                # Maintain the team's conservative policy of using heavy cranes for empty moves.
                if float(crane["capacity"]) < task.mass or (
                    task.name.startswith("E") and float(crane["capacity"]) < 450
                ):
                    continue
                pickup = neighbors(state, k, task.start, gap)
                delivery = neighbors(pickup, k, task.end, gap)
                if any(point[0] < limits[0] - EPS or point[-1] > limits[1] + EPS for point in (pickup, delivery)):
                    continue
                access = move(state, pickup, *motion)
                loaded = move(pickup, delivery, *motion)
                pick = max(now + access.duration, task.early - loaded.duration)
                finish = pick + loaded.duration
                per_task.append((finish, k, access, loaded, pick))
            if per_task:
                finish, k, access, loaded, pick = min(per_task, key=lambda item: item[:2])
                priority = release if policy == "fcfs" else task.due - finish
                key = (priority, finish, task.name, k)
                candidates.append((key, task, release, access, loaded, pick, finish))
        if not candidates:
            future = [release for _, release in known if release > now + EPS]
            if not future:
                break
            now = min(future)
            continue
        key, task, release, access, loaded, pick, finish = min(candidates, key=lambda item: item[0])
        checked_deliveries.append((task, key[-1], now, release, pick, finish, access, loaded))
        carried[task.name] = finish
        state, now = loaded.finish, finish
        if len(carried) == 1 and delay_after_first:
            target = task_map["H2"]
            if target.name not in carried:
                task_map[target.name] = replace(target, release=max(target.release, now + delay_after_first))
    return checked_deliveries


def replay(rows, tasks, cranes, limits, gap, motion):
    state = tuple(float(c["initial_position"]) for c in cranes)
    last = 0.0
    delivered = {}
    min_gap = min(np.diff(state))
    active_time = 0.0
    empty_time = 0.0
    loaded_time = 0.0
    waits, delays = [], []
    if not all(limits[0] <= x <= limits[1] for x in state) or min_gap < gap - EPS:
        raise ValueError("Initial crane state violates rail bounds or net distance")
    for task, k, begin, release, pick, finish, access, loaded in rows:
        assert access.start == state and loaded.start == access.finish
        assert begin >= last - EPS and pick >= begin + access.duration - EPS
        assert pick >= release - EPS and pick >= task.release - EPS
        assert finish >= task.early - EPS and abs(finish - pick - loaded.duration) < EPS
        assert cranes[k]["capacity"] >= task.mass
        assert not task.name.startswith("E") or cranes[k]["capacity"] >= 450
        assert loaded.start[k] == task.start and loaded.finish[k] == task.end
        assert task.name not in delivered
        if task.predecessor:
            assert task.predecessor in delivered and pick >= delivered[task.predecessor] + task.service - EPS
        for leg in (access, loaded):
            delta = np.subtract(leg.finish, leg.start)
            distance = float(np.max(np.abs(delta)))
            speed_cap, acceleration_cap, zone, factor = motion
            if acceleration_cap and zone and any(
                abs(a - b) > EPS and min(a, b) <= zone[1] and max(a, b) >= zone[0]
                for a, b in zip(leg.start, leg.finish)
            ):
                speed_cap *= factor
            assert distance * leg.peak <= speed_cap + EPS
            assert not acceleration_cap or leg.duration < EPS or (
                leg.ramp > EPS and distance * leg.peak / leg.ramp <= acceleration_cap + EPS
            )
            assert all(limits[0] - EPS <= x <= limits[1] + EPS for x in (*leg.start, *leg.finish))
            end_progress = 0.0
            end_speed = 0.0
            for duration, progress, speed, accel in leg.phases():
                assert abs(progress - end_progress) <= EPS
                if acceleration_cap:
                    assert abs(speed - end_speed) <= EPS
                segment_start = np.add(leg.start, delta * progress)
                for g, v, a in zip(np.diff(segment_start), np.diff(delta * speed), np.diff(delta * accel)):
                    min_gap = min(min_gap, minimum_segment_gap(duration, g, v, a))
                end_progress = progress + speed * duration + accel * duration**2 / 2
                end_speed = speed + accel * duration
            assert abs(end_progress - int(distance >= EPS)) <= EPS
            assert not acceleration_cap or abs(end_speed) <= EPS
            moving = np.count_nonzero(np.abs(delta) > EPS)
            active_time += moving * leg.duration
            loaded_used = leg.duration if leg is loaded and abs(delta[k]) > EPS else 0.0
            loaded_time += loaded_used
            empty_time += moving * leg.duration - loaded_used
        assert min_gap >= gap - EPS
        delivered[task.name] = finish
        waits.append(pick - release)
        delays.append(max(0.0, finish - task.due))
        state, last = loaded.finish, finish
    on_time = int(sum(d <= EPS for d in delays))
    heavy_misses = sum(task.name.startswith("H") and (
        task.name not in delivered or delivered[task.name] > task.due + EPS
    ) for task in tasks)
    return {
        "served": len(delivered), "on_time": on_time, "all_on_time": int(on_time == len(tasks)),
        "heavy_window_miss": int(heavy_misses > 0), "wait_s": float(sum(waits)),
        "wait_task_s": waits, "late_s": delays, "running_s": float(active_time),
        "empty_running_s": float(empty_time), "loaded_running_s": float(loaded_time),
        "min_gap": float(min_gap),
    }


def case_data(path, layout, lead, travel_scale):
    case = json.loads(path.read_text(encoding="utf-8"))
    start = datetime.fromisoformat(case["base_time"])

    def offset(clock):
        return (datetime.combine(start.date(), datetime.strptime(clock, "%H:%M:%S").time()) - start).total_seconds()

    positions = next(item["positions"] for item in case["geometry_hypotheses"] if item["id"] == layout)
    tasks = [
        Task(f"H{i}", positions[h["origin"]], positions[case["caster"]], h["weight"],
             offset(h["observed_pickup"]), offset(h["plan_cast_start"]) - case["seat_early_limit_seconds"],
             offset(h["plan_cast_start"]) - lead, "H1" if i == 2 else "")
        for i, h in enumerate(case["heats"], 1)
    ]
    e = case["empty"]
    close = offset(e["observed_ferry_drop"])
    slag = positions[e["slag_station"]]
    tasks += [
        Task("E1", positions[case["caster"]], slag, e["pre_slag_weight"], offset(e["observed_pickup"]), 0, close),
        Task("E2", slag, positions[e["ferry_station"]], e["post_slag_weight"],
             offset(e["observed_ferry_ready"]), 0, close, "E1",
             offset(e["observed_slag_end"]) - offset(e["observed_slag_start"])),
    ]
    central = np.mean([offset(h["observed_seat"]) - offset(h["observed_pickup"]) for h in case["heats"]])
    speed = abs(tasks[0].end - tasks[0].start) / central / travel_scale
    return tasks, case["cranes"], case["rail_limits"], float(speed), slag


def quantiles(series):
    values = np.asarray(series, dtype=float)
    if not len(values):
        return None
    return dict(zip(("p10", "p50", "p90"), map(float, np.quantile(values, [.1, .5, .9]))))


def calibrate(calibration_path):
    frame = pd.read_csv(calibration_path, dtype={"day": str})
    result = {}
    for metric, pool in frame.groupby("metric"):
        duration = pool.duration_s.dropna()
        if len(duration) >= 20:
            result[str(metric)] = {"n": len(duration), "mean": float(duration.mean()), **quantiles(duration)}
    lf = frame[(frame.metric == "lf_duration") & (frame.duration_s > 0)].copy()
    equipment = lf.equipment.value_counts().index[0]
    lf = lf[lf.equipment == equipment].sort_values(["day", "start_time_s"])
    days = sorted(lf.day.unique())
    if len(days) < 4:
        raise ValueError("The LF history lacks four days for a temporal holdout")
    split = max(1, len(days) - int(np.ceil(len(days) / 4)))
    train = lf[lf.day.isin(days[:split])]
    held = lf[lf.day.isin(days[split:])].duration_s.to_numpy()
    sample = train.duration_s.to_numpy()
    probs = [.5, .9]
    observed = np.quantile(held, probs)
    grid = np.unique(np.concatenate([sample, held]))
    observed_left = np.searchsorted(np.sort(held), grid, side="left") / len(held)
    observed_right = np.searchsorted(np.sort(held), grid, side="right") / len(held)
    empirical_left = np.searchsorted(np.sort(sample), grid, side="left") / len(sample)
    empirical_right = np.searchsorted(np.sort(sample), grid, side="right") / len(sample)
    fitted = {"empirical": (np.quantile(sample, probs), empirical_left, empirical_right)}
    for name in ("gamma", "lognorm"):
        dist = getattr(stats, name)
        params = dist.fit(sample, floc=0)
        cdf = dist.cdf(grid, *params)
        fitted[name] = (dist.ppf(probs, *params), cdf, cdf)
    fitted = {
        name: {"q50_absolute_error_s": float(abs(pred[0] - observed[0])),
               "q90_absolute_error_s": float(abs(pred[1] - observed[1])),
               "cdf_distance": float(max(np.max(np.abs(left - observed_left)),
                                         np.max(np.abs(right - observed_right))))}
        for name, (pred, left, right) in fitted.items()
    }
    pairs = []
    for _, group in train.groupby("day"):
        values = group.duration_s.to_numpy()
        pairs.extend(zip(values[:-1], values[1:]))
    pairs = np.asarray(pairs)
    ratios = pairs / pairs.mean(axis=0)
    cast = frame[frame.metric == "cast_duration"].copy()
    cast_check = None
    if "planned_duration_s" in cast:
        matched = cast[(cast.duration_s > 0) & (cast.planned_duration_s > 0)].copy()
        if not matched.empty:
            ratio = matched.duration_s / matched.planned_duration_s
            cast_check = {"n_plan_matched": len(ratio), "raw_mean": float(ratio.mean()),
                          "anchored_mean": float((ratio / ratio.mean()).mean()), **quantiles(ratio)}
    return result, {"equipment": equipment, "train_n": len(sample), "holdout_n": len(held),
                    "train_days": split, "holdout_days": len(days) - split, "fits": fitted,
                    "adjacent_train_pairs": len(ratios), "pair_correlation": float(np.corrcoef(pairs.T)[0, 1]),
                    "cast_plan_ratios": cast_check}, sample / sample.mean(), ratios


def summarize(outcomes, task_count, pairs_finished):
    waits = [v for keep, result in zip(pairs_finished, outcomes) if keep for v in result["wait_task_s"]]
    delays = [v for result in outcomes for v in result["late_s"]]
    return {
        "n": len(outcomes), "task_service_rate": float(np.mean([r["served"] / task_count for r in outcomes])),
        "task_on_time_rate": float(np.mean([r["on_time"] / task_count for r in outcomes])),
        "plan_completion_rate": float(np.mean([r["all_on_time"] for r in outcomes])),
        "heavy_window_miss_rate": float(np.mean([r["heavy_window_miss"] for r in outcomes])),
        "wait_both_served_s": quantiles(waits), "delay_served_s": quantiles(delays),
        "mean_running_s": float(np.mean([r["running_s"] for r in outcomes])),
        "mean_empty_running_s": float(np.mean([r["empty_running_s"] for r in outcomes])),
        "mean_loaded_running_s": float(np.mean([r["loaded_running_s"] for r in outcomes])),
        "minimum_rail_gap": float(min(r["min_gap"] for r in outcomes)),
    }


def compare(draws, policy_model, tasks, run):
    outcomes = {policy: [] for policy in ("fcfs", "margin")}
    for pair in draws:
        scenario = [replace(task, release=task.release + pair[i]) if i < 2 else task for i, task in enumerate(tasks)]
        for policy in outcomes:
            outcomes[policy].append(run(scenario, policy_model, policy)[1])
    paired = [all(result["served"] == len(tasks) for result in results) for results in zip(*outcomes.values())]
    summary = {policy: summarize(items, len(tasks), paired) for policy, items in outcomes.items()}
    a, b = outcomes["fcfs"], outcomes["margin"]
    summary["paired"] = {
        "both_serve_all": sum(paired),
        "margin_on_time_task_delta_mean": float(np.mean([y["on_time"] - x["on_time"] for x, y in zip(a, b)])),
        "margin_wait_delta_s": quantiles([y["wait_s"] - x["wait_s"] for ok, x, y in zip(paired, a, b) if ok]),
        "margin_improves_task_count": sum(y["on_time"] > x["on_time"] for x, y in zip(a, b)),
        "margin_worsens_task_count": sum(y["on_time"] < x["on_time"] for x, y in zip(a, b)),
    }
    return summary


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--case", type=Path, default=PILOT)
    cli.add_argument("--calibration", type=Path, default=PRIVATE / "stage02_b/calibration.csv")
    cli.add_argument("--out", type=Path, default=PRIVATE / "stage02_b/probe_summary.json")
    cli.add_argument("--samples", type=int, default=200)
    cli.add_argument("--seed", type=int, default=20261126)
    args = cli.parse_args()
    if not args.out.resolve().is_relative_to(PRIVATE) or args.samples < 1:
        cli.error("Output must be inside problem/data/processed and samples must be positive")

    assert minimum_segment_gap(2, 3, -2, 2) == 2.0
    assert abs(minimum_segment_gap(4000, 3, -1e-4, 5e-8) - 2.9) < EPS
    assert abs(move((0,), (9,), 8, 4, None, 1).duration - 3) < EPS
    assert abs(move((0,), (25,), 8, 4, None, 1).duration - 5.125) < EPS
    aggregates, fit, iid, donor_pairs = calibrate(args.calibration)
    tasks, cranes, limits, speed, slag = case_data(args.case, "east_turnaround", 120, 1.0)
    gap = 3.0
    linear = (speed, 0.0, None, 1.0)
    slow_zone = (slag - gap / 2, slag + gap / 2)
    kinetics = (speed * 1.25, speed / 25.0, slow_zone, 0.75)

    def run(current, motion, policy, delay=0):
        rows = dispatch(current, cranes, limits, gap, motion, policy, delay)
        return rows, replay(rows, current, cranes, limits, gap, motion)

    q1 = {policy: run(tasks, linear, policy)[1] for policy in ("fcfs", "margin")}
    # Diagnostic congestion fixture: make H1 and E1 simultaneously available at
    # the decision origin, with E1 having arrived first. Preserve relative deadlines.
    origin = tasks[0].release
    overlap = [replace(task, release=task.release - origin, early=task.early - origin,
                       due=task.due - origin) for task in tasks]
    overlap[2] = replace(overlap[2], release=-60.0)
    overlap_runs = {policy: run(overlap, kinetics, policy) for policy in ("fcfs", "margin")}
    overlap_summary = {policy: {**metrics, "first_task_class": rows[0][0].name[0]}
                       for policy, (rows, metrics) in overlap_runs.items()}
    bound = case_data(args.case, "east_turnaround", 180, 1.0)[0][0]
    bound_miss_s = bound.release + abs(bound.end - bound.start) / speed - bound.due
    assert bound_miss_s > 0
    normal, normal_metrics = run(tasks, kinetics, "margin")
    repairs = {}
    for wait in (120, 1800):
        repaired, metric = run(tasks, kinetics, "margin", wait)
        assert normal[:1] == repaired[:1]
        promised_release = next(task.release for task in tasks if task.name == "H2")
        injected_delta = max(0.0, normal[0][5] + wait - promised_release)
        repairs[str(wait)] = {**metric, "release_delay_s": injected_delta, "frozen_first_delivery": True}

    q2 = {}
    for seed in (args.seed, args.seed + 1, args.seed + 2):
        rng = np.random.default_rng(seed)
        draws_iid = rng.choice(iid, (args.samples, 2), replace=True) - 1.0
        draws_block = donor_pairs[rng.integers(len(donor_pairs), size=args.samples)] - 1.0
        q2[str(seed)] = {
            "iid_180": compare(draws_iid * 180.0, kinetics, tasks, run),
            **{f"adjacent_{s}": compare(draws_block * s, kinetics, tasks, run) for s in (90, 180, 360)},
        }
    summary = {
        "input_sha256": {path.name: sha256(path.read_bytes()).hexdigest() for path in (args.case, args.calibration)},
        "aggregate_calibration": aggregates, "lf_holdout": fit,
        "scenario_assumptions": {"track_layout": "east_turnaround", "gap": gap, "lead_s": 120,
                                 "proxy_shock_scales_s": [90, 180, 360],
                                 "speed_relative_to_linear": 1.25, "acceleration_divisor_s": 25,
                                 "slow_factor": 0.75, "samples_per_seed_and_setting": args.samples},
        "q1_rules": q1, "q1_180s_optimistic_miss_s": float(bound_miss_s),
        "synthetic_overlap": overlap_summary,
        "q2": q2, "q3_fault_proxy": {"normal": normal_metrics, "releases_after_first_s": repairs},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print("PASS: replay, continuous gap, LF holdout, paired policies, fault-prefix checks.")


if __name__ == "__main__":
    main()
