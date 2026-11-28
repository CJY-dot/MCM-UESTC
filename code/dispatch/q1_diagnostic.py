"""Reproduce Q1-A.1 evidence from an already frozen, private A planning run."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

from q1_scenario import digest, read_json
from q1_scheduler import EPS, FINISHED, POLICIES, solve, task_dependencies
from run_q1 import protected_output, save


def ef_roster(case):
    return [task for task in case["tasks"] if task["required_in_ef_scope"] is True]


def terminal_bound(case):
    """Exhaust all subsets of the necessary dependency and terminal-stock system."""
    tasks = ef_roster(case)
    if len(tasks) > 20:
        raise ValueError("This exact subset diagnostic is limited to 20 EF tasks")
    bit = {task["id"]: 1 << n for n, task in enumerate(tasks)}
    excluded = 0
    need, exits, groups, turnover = {}, {}, defaultdict(list), []
    for task in tasks:
        tid = task["id"]
        deps = task_dependencies(task)
        if task["blocks"] or any(dep not in bit for dep in deps) or task["kind"] != "ef":
            excluded |= bit[tid]
        need[tid] = sum(bit[dep] for dep in set(deps) if dep in bit)
        exits[tid] = sum(bit[s["id"]] for s in tasks if s["predecessor"] == tid
                         and s["origin"] == task["destination"] and s["ladle"] == task["ladle"])
        if task["service"] is not None and task["service"]["kind"] in {"refine", "cast", "slag"}:
            kind = task["service"]["kind"]
            resource = f"{'return' if kind == 'cast' else kind}/{task['destination']}"
            groups[resource].append(tid)
    for later in tasks:
        previous = later["previous_cast"]
        if previous in bit and any(previous in members and later["id"] in members
                                   for resource, members in groups.items()
                                   if resource.startswith("return/") and case["capacity"][resource] == 1):
            turnover.append((bit[later["id"]], exits[previous]))

    labels = ("external", "slag", "dependencies", "caster_turnover", "terminal_stock")
    passed = {stage: {"subsets": 0, "upper": 0} for stage in labels}
    appearing, maxima = 0, []

    def ok_stock(mask, prefixes):
        return all(sum(bool(mask & bit[tid]) and not bool(mask & exits[tid])
                       for tid in members) <= case["capacity"][resource]
                   for resource, members in groups.items() if resource.startswith(prefixes))

    for mask in range(1 << len(tasks)):
        if mask & excluded:
            continue
        count = mask.bit_count()
        active = [tid for tid in bit if bit[tid] & mask]
        conditions = (
            lambda: True,
            lambda: ok_stock(mask, "slag/"),
            lambda: all(need[tid] & mask == need[tid] for tid in active),
            lambda: all(not mask & following or mask & releases for following, releases in turnover),
            lambda: ok_stock(mask, ("refine/", "return/")),
        )
        for stage, acceptable in zip(labels, conditions):
            if not acceptable():
                break
            row = passed[stage]
            row["subsets"] += 1
            row["upper"] = max(row["upper"], count)
        else:
            appearing |= mask
            if count == passed["terminal_stock"]["upper"]:
                if maxima and len(maxima[0]) < count:
                    maxima.clear()
                maxima.append(active)
    return {
        "tested_subsets": 1 << len(tasks), "gates": passed,
        "maximum_subsets": maxima, "never_feasible_in_relaxation": [tid for tid in bit if not bit[tid] & appearing],
    }


def casting_audit(case, result):
    tasks = {t["id"]: t for t in ef_roster(case)}
    rows = {r["task_id"]: r for r in result["tasks"]}
    records = []
    for tid, task in tasks.items():
        service = task["service"]
        if not service or service["kind"] != "cast":
            continue
        row = rows[tid]
        plan, duration = service["not_before_s"], service["duration_s"]
        prev = task["previous_cast"]
        prev_service = tasks[prev]["service"] if prev else None
        prev_result = rows[prev] if prev else {}
        prev_end = prev_result.get("service_end_s")
        start, end = row.get("service_start_s"), row.get("service_end_s")
        seat = row.get("drop_complete_s")
        prepared = seat + case["caster_preparation_s"] if seat is not None else None
        active = start is not None
        record = {
            "task_id": tid, "previous_cast": prev, "planned_start_s": plan, "planned_duration_s": duration,
            "planned_end_s": plan + duration, "overlap_with_previous_plan_s":
                max(0, prev_service["not_before_s"] + prev_service["duration_s"] - plan) if prev else 0,
            "modeled_start_s": start, "modeled_end_s": end, "previous_modeled_end_s": prev_end,
            "modeled_start_delay_s": start - plan if active else None,
            "conditioned_previous_end_delay_lower_bound_s": max(0, prev_end - plan) if prev_end is not None else None,
            "modeled_seat_s": seat, "modeled_prepared_s": prepared, "seat_target_s": task["plan_target_s"],
            "seat_delay_s": row.get("lateness_s"), "process_hard_latest_s": task["delivery_window"]["hard_latest_s"],
        }
        if active:
            assert not prev or prev_end is not None
            assert start + EPS >= max(plan, prepared, prev_end or 0)
            assert abs(end - start - duration) <= EPS
            intervals = [(b["start_s"], b["end_s"]) for b in result["occupancy"]
                         if b["task_id"] == tid and b["resource_id"] == f"supply/{task['destination']}"]
            assert intervals == [(start, end)]
            assert abs(record["seat_delay_s"] - max(0, seat - task["plan_target_s"])) <= EPS
        records.append(record)
    return records


def peak_occupancy(case, result):
    peaks = {}
    for resource, capacity in case["capacity"].items():
        holds = [x for x in result["occupancy"] if x["resource_id"] == resource]
        stamps = {0, case["horizon_s"], *(x["start_s"] for x in holds)}
        peak = max(sum(x["demand"] for x in holds if x["start_s"] <= at
                       and (x["end_s"] is None or at < x["end_s"])) for at in stamps)
        assert peak <= capacity
        peaks[resource] = {"peak": peak, "capacity": capacity}
    return peaks


def gap_witness(case, result):
    order = [crane["id"] for crane in case["cranes"]]
    paths = {cid: [s for s in result["trajectories"] if s["crane_id"] == cid] for cid in order}

    def at(seg, t):
        return seg["x0"] + (seg["x1"] - seg["x0"]) * ((t - seg["t0_s"]) / (seg["t1_s"] - seg["t0_s"]))

    def position(cid, t):
        return at(next(s for s in paths[cid] if s["t0_s"] - EPS <= t <= s["t1_s"] + EPS), t)

    def state(cid, t):
        before = next((s for s in reversed(paths[cid]) if s["t0_s"] < t <= s["t1_s"] + EPS), None)
        after = next((s for s in paths[cid] if s["t0_s"] - EPS <= t < s["t1_s"]), None)
        return {"crane_id": cid, "x": position(cid, t),
                "before": {"mode": before["mode"], "speed": before["v0"]} if before else None,
                "after": {"mode": after["mode"], "speed": after["v0"]} if after else None}

    candidates = []
    for left, right in zip(order, order[1:]):
        for time in {0, *(s["t1_s"] for cid in (left, right) for s in paths[cid])}:
            candidates.append((position(right, time) - position(left, time), time, left, right))
    minimum = min(item[0] for item in candidates)
    gap, time, left, right = min((a for a in candidates if a[0] <= minimum + EPS), key=lambda a: a[1:])
    threshold = case["params"]["gap"]
    assert abs(minimum - result["gap"]["min_gap"]) <= EPS
    assert minimum >= threshold - EPS
    return {"first_time_s": time, "min_gap": minimum, "gap_at_first_time": gap, "threshold": threshold,
            "margin": minimum - threshold,
            "initial_min_gap": min(position(b, 0) - position(a, 0) for a, b in zip(order, order[1:])),
            "states": [state(left, time), state(right, time)]}


def decision_audit(result_audit):
    """Annotate why a checked option did not become the commitment at its step."""
    def label(entry, chosen, now):
        for option in entry["options"]:
            if chosen and all(option.get(key) == val for key, val in chosen.items()):
                reason = "selected_or_identical_action"
            elif option["first_action_s"] > now + EPS:
                reason = "future_start"
            elif chosen and entry["task_id"] != chosen["task_id"]:
                reason = "different_task_selected_by_policy_priority"
            else:
                reason = "same_task_alternative_ranked_after_selection"
            option["decision_disposition"] = reason

    for step in result_audit:
        for entry in step["tasks"]:
            label(entry, step["selected"], step["now_s"])
        if step["return_reservation"]:
            reserve = step["return_reservation"]
            label(reserve, reserve["selected"], float("inf"))
    return result_audit


def task_audit(case, result, journal, excluded):
    tasks = ef_roster(case)
    aliases = {t["id"]: f"T{i:02d}" for i, t in enumerate(tasks, 1)}
    rows = {r["task_id"]: r for r in result["tasks"]}
    trials = defaultdict(list)
    for step in journal:
        for entry in (*step["tasks"], step["return_reservation"]):
            if entry:
                trials[entry["task_id"]].append(entry)
    diagnostic = []
    for t in tasks:
        tid = t["id"]
        row = rows[tid]
        attempts = [a for entry in trials[tid] for a in entry["attempts"]]
        options = [o for entry in trials[tid] for o in entry["options"]]
        peaks = Counter(a["rejection"] for a in attempts if a["rejection"] is not None)
        prev = t["predecessor"]
        predecessor = rows[prev] if prev else {}
        diagnostic.append({
            "alias": aliases[tid], "task_id": tid, "production_order": t["order"],
            "origin": t["origin"], "destination": t["destination"], "source_admission": t["source_admission"],
            "required_ef": t["required_in_ef_scope"], "predecessor_ids": list(task_dependencies(t)),
            "missing_predecessor_ids": row["unfulfilled_dependencies"],
            "previous_operation_complete_s": predecessor.get("holder_ready_s"),
            "conditioned_release_s": row["release_s"],
            "conditioned_ready_before_pickup": True if row["status"] == FINISHED else None,
            "preparation_lag_s": case["params"]["prep_s"], "eligible_cranes": t["eligible"],
            "mass_upper_t": t["mass_upper_t"], "candidate_attempts": len(attempts),
            "feasible_option_count": len(options),
            "planned_trajectory_and_capacity_passed": bool(options),
            "selected_crane": row["crane_id"], "pickup_s": row["pickup_start_s"],
            "delivery_s": row["drop_complete_s"], "minimum_generated_pickup_s":
                min((o["pickup_start_s"] for o in options), default=None),
            "minimum_generated_delivery_s": min((o["drop_complete_s"] for o in options), default=None),
            "direct_loaded_travel_s": abs(case["positions"][t["origin"]] -
                                          case["positions"][t["destination"]]) / case["params"]["speed"],
            "retained_resources": [b["resource_id"] for b in result["occupancy"] if
                                   b["task_id"] == tid and b["end_s"] is None],
            "return_or_slag_scope": t["destination"] if t["load_state"] == "empty_ladle" else t["reuse_predecessor"],
            "terminal_status": row["status"], "terminal_reasons": row["reasons"],
            "excluded_by_terminal_stock_bound": tid in excluded,
            "all_attempt_rejections": dict(peaks),
        })
    return diagnostic


def csv_write(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def headline(result):
    metrics = result["metrics"]
    chosen = ("completed_ef", "on_time", "late_count", "late_total_s", "min_gap",
              "empty_run_s", "loaded_run_s", "waiting_s")
    return {key: metrics[key] for key in chosen}


def run(experiment, output):
    output = protected_output(output)
    output.mkdir(parents=True, exist_ok=True)
    cases, base_runs, journals = {}, {}, {}
    summary = {"schema": "mcm-uestc.q1-diagnostic/1", "registered": {}, "extras": []}
    for tag in ("base", "optimistic", "conservative"):
        directory = experiment / tag
        case = cases[tag] = read_json(directory / "q1_planning_scenario.json")
        summary["registered"][tag] = {"case_sha256": digest(case), "parameters": case["params"]}
        for policy in POLICIES:
            journal = []
            actual = solve(case, policy, audit=journal)
            archived = read_json(directory / policy / "q1_dispatch_result.json")
            assert actual == archived, f"Archived {tag}/{policy} differs from diagnostic replay"
            summary["registered"][tag][policy] = headline(actual)
            if tag == "base":
                base_runs[policy], journals[policy] = actual, decision_audit(journal)
            save(output / f"{tag}_{policy}_decisions.json", decision_audit(journal))
    base = cases["base"]
    summary["plan_version"] = base["plan_version"]
    summary["source"] = base["source"]
    summary["config_sha256"] = base["assumption_config_sha256"]
    bound = terminal_bound(base)
    excluded = set(bound["never_feasible_in_relaxation"])
    save(output / "coverage_certificate.json", bound)
    summary["bounds"] = bound["gates"]
    summary["cast_start_vs_plan"] = {}
    for policy, result in base_runs.items():
        casts = casting_audit(base, result)
        delays = [c["modeled_start_delay_s"] for c in casts if c["modeled_start_s"] is not None]
        summary["cast_start_vs_plan"][policy] = {
            "plan_overlap_s": [c["overlap_with_previous_plan_s"] for c in casts if c["overlap_with_previous_plan_s"]],
            "completed_casts": len(delays), "unserved_casts": len(casts) - len(delays),
            "shifted_caster_starts": sum(d > EPS for d in delays),
            "total_caster_start_delay_s": sum(delays), "max_caster_start_delay_s": max(delays, default=0),
        }
        csv_write(output / f"casting_{policy}.csv", casts)
        csv_write(output / f"tasks_{policy}.csv", task_audit(base, result, journals[policy], excluded))
        save(output / f"occupancy_and_gap_{policy}.json",
             {"peaks": peak_occupancy(base, result), "gap": gap_witness(base, result)})
    def comparable(journal):
        return [(step["now_s"], step["advance_to_s"] if not step["selected"] else None,
                 [(row["task_id"], row["status"], row["attempts"]) for row in step["tasks"]],
                 step["selected"], step["return_reservation"])
                for step in journal]
    summary["same_candidate_stream_and_choices"] = comparable(journals["fcfs"]) == comparable(journals["tight_margin"])
    ordinary = [step for step in journals["fcfs"] if step["selected"]]
    summary["ordinary_decision_candidate_task_counts"] = [
        sum(entry["status"] == "selectable" for entry in step["tasks"]) for step in ordinary
    ]
    summary["paired_returns"] = sum(bool(step["return_reservation"] and step["return_reservation"]["selected"])
                                    for step in ordinary)
    crane_index = {c["id"]: i for i, c in enumerate(base["cranes"])}

    def alternate_rank(option):
        return (option["lateness_s"] or 0, option["drop_complete_s"], -crane_index[option["crane_id"]],
                option["empty_run_s"] + option["loaded_run_s"])

    def witness(tag, case, *, strict, policy="fcfs", **search):
        result = solve(case, policy, enforce_plan=strict, **search)
        summary["extras"].append({"tag": tag, "policy": policy, "strict": strict, **headline(result)})
        save(output / "witnesses" / tag / f"{policy}.json", result)
        return result

    for gap in (4, 4.25, 4.5, 5, 5.5, 6):
        case = base | {"params": base["params"] | {"gap": gap}}
        for policy in POLICIES:
            witness(f"gap_{gap:g}", case, strict=True, policy=policy)
            if gap in (4, 6):
                witness(f"gap_{gap:g}_alternate", case, strict=True, policy=policy,
                        rank_candidate=alternate_rank)
    conservative = cases["conservative"]
    search = {"search_lookahead_s": 1200,
              "unlock_pad_s": 1.5 * conservative["params"]["gap"] / conservative["params"]["speed"]}
    summary["conservative_search_controls"] = search
    witness("conservative_lookahead_only", conservative, strict=False,
            search_lookahead_s=search["search_lookahead_s"])
    witness("conservative_unlock_pad_only", conservative, strict=False,
            unlock_pad_s=search["unlock_pad_s"])
    for policy in POLICIES:
        for strict in (False, True):
            mode = "strict" if strict else "diagnostic"
            witness(f"conservative_expanded_{mode}", conservative, strict=strict, policy=policy, **search)
    for key, value in conservative["params"].items():
        if key == "distance_factor":
            # Geometry jointly with its distance scale must be the same registered setup.
            continue
        case = base | {"params": base["params"] | {key: value}}
        if key == "lf_s":
            case["tasks"] = [t | {"service": t["service"] | {"duration_s": value}}
                             if t["service"] and t["service"]["kind"] == "refine"
                             and t["destination"].startswith("LF") else t for t in case["tasks"]]
        elif key == "prep_s":
            case["tasks"] = [t | {"rho_s": t["rho_s"] + value - base["params"]["prep_s"]}
                             if t["rho_s"] is not None else t for t in case["tasks"]]
        elif key in ("rh_s", "slag_s"):
            if key == "rh_s":
                continue  # Q1W06 has no RH template.
            case["tasks"] = [t | {"service": t["service"] | {"duration_s": value[t["destination"]]}}
                             if t["service"] and t["service"]["kind"] == "slag" else t for t in case["tasks"]]
        witness(f"one_factor_{key}", case, strict=False)
    registered_distance = experiment / "sensitivity/distance_factor/conservative/q1_planning_scenario.json"
    witness("one_factor_distance_factor", read_json(registered_distance), strict=False)
    save(output / "diagnostic_summary.json", summary)
    print(f"{len(ef_roster(base))} EF tasks, {bound['tested_subsets']} subsets and {len(summary['extras'])} supplemental runs")
    print(f"Conditional upper {bound['gates']['terminal_stock']['upper']}; base witness {base_runs['fcfs']['metrics']['completed_ef']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.experiment_dir, args.output)


if __name__ == "__main__":
    main()
