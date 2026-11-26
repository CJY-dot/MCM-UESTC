"""Construct and independently replay a four-task shared-rail pilot."""

import argparse
import csv
import itertools
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_CASE = WORKSPACE / "problem/case_relative.json"
EPS = 1e-6


def build_tasks(case, seconds, lead):
    tasks = {}
    for index, heat in enumerate(case["heats"], 1):
        cast_start = seconds(heat["plan_cast_start"])
        tasks[f"H{index}"] = (heat["origin"], case["caster"], heat["weight"],
                             seconds(heat["observed_pickup"]),
                             cast_start - case["seat_early_limit_seconds"], cast_start - lead)
    empty = case["empty"]
    due = seconds(empty["observed_ferry_drop"])
    tasks["E1"] = (case["caster"], empty["slag_station"], empty["pre_slag_weight"],
                   seconds(empty["observed_pickup"]), 0, due)
    tasks["E2"] = (empty["slag_station"], empty["ferry_station"], empty["post_slag_weight"],
                   seconds(empty["observed_ferry_ready"]), 0, due)
    return tasks


def translated(start, active, target, gap):
    """Move idle neighbors only as far as necessary to clear the active crane."""
    end = list(start)
    end[active] = target
    for i in range(active - 1, -1, -1):
        end[i] = min(end[i], end[i + 1] - gap)
    for i in range(active + 1, len(end)):
        end[i] = max(end[i], end[i - 1] + gap)
    return tuple(end)


def replay(records, tasks, gap, limit, geometry, seconds_per_unit, slag, cranes):
    """Validate crane capacities, geometry, timing, and interpolation endpoints."""
    lower, upper = limit
    last = tuple(crane["initial_position"] for crane in cranes)
    time = 0
    task_starts, task_ends = {}, {}
    gaps = []
    motion = 0
    for event in records:
        before, after = event["from"], event["to"]
        t0, t1 = event["start_s"], event["end_s"]
        assert before == last and abs(t0 - time) < EPS and t1 >= t0
        for point in (before, after):
            assert all(lower - EPS <= x <= upper + EPS for x in point)
            distances = [b - a for a, b in zip(point, point[1:])]
            assert min(distances) >= gap - EPS
            gaps.extend(distances)
        travel = [abs(x - y) for x, y in zip(before, after)]
        assert max(travel) * seconds_per_unit <= t1 - t0 + EPS
        motion += sum(travel) * seconds_per_unit
        if event["kind"] == "loaded":
            ref = event["task"]
            origin, destination, mass, ready, seat_early, due = tasks[ref]
            index = event["crane_index"]
            assert ref not in task_ends and mass <= cranes[index]["capacity"]
            assert before[index] == geometry[origin] and after[index] == geometry[destination]
            assert t0 >= ready - EPS and seat_early - EPS <= t1 <= due + EPS
            task_starts[ref], task_ends[ref] = t0, t1
        last, time = after, t1
    assert task_ends.keys() == tasks.keys()
    assert task_starts["E2"] >= task_ends["E1"] + slag - EPS
    assert task_ends["H1"] < task_ends["H2"]
    return min(gaps), motion


def solve(case, seconds, positions, lead, gap, duration, heavy_only_empty):
    limit = case["rail_limits"]
    tasks = build_tasks(case, seconds, lead)
    empty = case["empty"]
    cranes = case["cranes"]
    slag = seconds(empty["observed_slag_end"]) - seconds(empty["observed_slag_start"])
    best = None
    prunes = Counter()
    visits = 0
    starts = tuple(crane["initial_position"] for crane in cranes)

    for ref, (origin, destination, _, ready, _, due) in tasks.items():
        if ready + abs(positions[origin] - positions[destination]) * duration > due + EPS:
            return {"outcome": "travel_lower_bound_failed", "blocking_task": ref}

    def search(now, state, finished, records, total_wait):
        nonlocal best, visits
        visits += 1
        if len(finished) == len(tasks):
            gap_actual, motion = replay(records, tasks, gap, limit, positions, duration, slag, cranes)
            score = (total_wait, motion)
            if best is None or score < best[0]:
                best = (score, gap_actual, records)
            return
        if best is not None and total_wait > best[0][0] + EPS:
            return

        for ref, task in tasks.items():
            dependency = {"H2": "H1", "E2": "E1"}.get(ref)
            if ref in finished or (dependency and dependency not in finished):
                continue
            origin, destination, mass, ready, seat_early, due = task
            if ref == "E2":
                ready = max(ready, finished["E1"] + slag)
            for index, crane in enumerate(cranes):
                if mass > crane["capacity"] or (heavy_only_empty and ref.startswith("E")
                                                and crane["capacity"] < 450):
                    continue
                pickup = translated(state, index, positions[origin], gap)
                drop = translated(pickup, index, positions[destination], gap)
                if any(x[0] < limit[0] - EPS or x[-1] > limit[1] + EPS for x in (pickup, drop)):
                    prunes["rail_limit"] += 1
                    continue
                access = max(abs(u - v) for u, v in zip(pickup, state)) * duration
                carry = max(abs(u - v) for u, v in zip(drop, pickup)) * duration
                t_pickup = max(now + access, ready, seat_early - carry)
                t_drop = t_pickup + carry
                if t_drop > due + EPS:
                    prunes["time_window"] += 1
                    continue
                steps = []
                if access:
                    steps.append({"kind": "deadhead", "task": ref, "crane_index": index,
                                  "start_s": now, "end_s": now + access, "from": state, "to": pickup})
                if t_pickup > now + access:
                    steps.append({"kind": "hold", "task": ref, "crane_index": index,
                                  "start_s": now + access, "end_s": t_pickup, "from": pickup, "to": pickup})
                steps.append({"kind": "loaded", "task": ref, "crane_index": index,
                              "start_s": t_pickup, "end_s": t_drop, "from": pickup, "to": drop})
                search(t_drop, drop, finished | {ref: t_drop}, records + steps,
                       total_wait + t_pickup - ready)

    search(0, starts, {}, [], 0)
    if best is None:
        return {"outcome": "no_serialized_candidate", "visits": visits, "prunes": prunes}
    score, gap_actual, events = best
    deliveries = []
    e1_drop = next(e["end_s"] for e in events if e["kind"] == "loaded" and e["task"] == "E1")
    for e in events:
        if e["kind"] == "loaded":
            origin, destination, mass, release, _, due = tasks[e["task"]]
            effective_release = max(release, e1_drop + slag) if e["task"] == "E2" else release
            deliveries.append({
                "task": e["task"], "from_station": origin, "to_station": destination,
                "crane": cranes[e["crane_index"]]["id"], "mass_t": mass,
                "release_s": effective_release, "pick_s": e["start_s"], "drop_s": e["end_s"],
                "due_s": due, "slack_s": round(due - e["end_s"], 3),
            })
    return {"outcome": "verified_conditional_schedule", "visits": visits, "min_gap": gap_actual,
            "wait_s": round(score[0], 3), "travel_s": round(score[1], 3),
            "deliveries": deliveries, "events": events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, default=DEFAULT_CASE,
                        help="Case JSON (default: the checked-in relative-time fixture)")
    parser.add_argument("--heavy-only-empty", action="store_true")
    args = parser.parse_args()
    case = json.loads(args.case.read_text(encoding="utf-8"))
    base = datetime.fromisoformat(case["base_time"]) if "base_time" in case else None

    def seconds(value):
        if base is None:
            return int(value)
        clock = datetime.strptime(value, "%H:%M:%S").time()
        return int((datetime.combine(base.date(), clock) - base).total_seconds())

    heavy_only_empty = args.heavy_only_empty
    heats = case["heats"]
    assert heats[0]["plan_cast_end"] == heats[1]["plan_cast_start"]
    assert heats[1]["cast_index"] == heats[0]["cast_index"] + 1
    transport = sorted(seconds(h["observed_seat"]) - seconds(h["observed_pickup"]) for h in heats)
    assert transport == [182, 247]
    duration = dict(zip(case["movement_duration_modes"],
                        (transport[0], sum(transport) / 2, transport[-1])))
    cases, nominal = [], None
    for geometry, gap, lead, mode in itertools.product(
        case["geometry_hypotheses"], case["minimum_gap_hypotheses"],
        case["seat_lead_seconds"], case["movement_duration_modes"],
    ):
        g = geometry["positions"]
        scale = duration[mode] / abs(g["CC3"] - g["LF1"])
        result = solve(case, seconds, g, lead, gap, scale, heavy_only_empty)
        labels = {"layout": geometry["id"], "gap": gap, "seat_lead_s": lead,
                  "motion": mode, "seconds_per_unit": scale}
        cases.append(labels | {k: v for k, v in result.items() if k not in ("deliveries", "events")})
        if geometry["id"] == "east_turnaround" and gap == 3 and lead == 120 and mode == "central":
            nominal = labels | result
    tally = Counter(item["outcome"] for item in cases)
    output = {"model": "one loaded move at a time, idle crane repositioning allowed",
              "case_mode": "relative_fixture" if base is None else "authorized_original",
              "heavy_only_empty": heavy_only_empty,
              "observed_transfer_seconds": transport, "summary": tally, "nominal": nominal,
              "cases": cases}
    suffix = "_heavy_only" if heavy_only_empty else ""
    folder = WORKSPACE / "results/replay" / output["case_mode"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"sensitivity{suffix}.json").write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8")
    if nominal and nominal.get("deliveries"):
        with (folder / f"nominal_tasks{suffix}.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(nominal["deliveries"][0]))
            writer.writeheader()
            writer.writerows(nominal["deliveries"])
    print(f"{len(cases)} modeled scenarios. {dict(tally)}.")


if __name__ == "__main__":
    main()
