"""Deterministic EF reservation scheduler for the Q1 planning schema."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from math import inf

from q1_scenario import digest, validate_case

EPS = 1e-8
POLICIES = ("fcfs", "tight_margin")
FINISHED = "completed"


def add_segment(path, crane, start, stop, x0, x1, mode, task=None, ladle=None):
    if stop - start > EPS:
        speed = (x1 - x0) / (stop - start)
        path.append({
            "crane_id": crane, "t0_s": start, "t1_s": stop, "x0": x0, "x1": x1,
            "v0": speed, "v1": speed, "a": 0, "gear": None, "mode": mode,
            "task_id": task, "ladle_id": ladle,
            "threshold_context": "constant_adjacent_reference_distance",
        })


def tail(case, paths, crane):
    path = paths[crane]
    if path:
        return path[-1]["t1_s"], path[-1]["x1"]
    return 0, next(c["x"] for c in case["cranes"] if c["id"] == crane)


def write_tail(case, paths, crane, start):
    end, position = tail(case, paths, crane)
    add_segment(paths[crane], crane, end, start, position, position, "idle")
    return position


def position_at(path, initial, at, ends):
    idx = bisect_right(ends, at)
    if idx == len(path):
        return path[-1]["x1"] if path else initial
    seg = path[idx]
    return seg["x0"] + seg["v0"] * (at - seg["t0_s"])


def continuous_gaps(case, paths, detailed=False):
    """All breakpoints for each adjacent pair, including stationary tails."""
    horizon = case["horizon_s"]
    order = [c["id"] for c in case["cranes"]]
    threshold = case["params"]["gap"]
    initial = {c["id"]: c["x"] for c in case["cranes"]}
    ends = {cid: [s["t1_s"] for s in segs] for cid, segs in paths.items()}

    def position(cid, at):
        return position_at(paths[cid], initial[cid], at, ends[cid])

    best = (inf, "", "", 0)
    samples = {}
    first_violation = None
    for left, right in zip(order, order[1:]):
        breaks = sorted({0, horizon, *ends[left], *ends[right]})
        previous = None
        for at in breaks:
            gap = position(right, at) - position(left, at)
            if gap < best[0]:
                best = (gap, left, right, at)
            if gap < threshold - EPS:
                if not detailed:
                    return {"ok": False}
                crossing = at
                if previous and previous[1] >= threshold:
                    t0, g0 = previous
                    crossing = t0 + (at - t0) * (g0 - threshold) / (g0 - gap)
                finding = {"time_s": crossing, "pair": [left, right],
                           "interval_s": [previous[0] if previous else at, at],
                           "threshold": threshold, "end_gap": gap}
                if first_violation is None or crossing < first_violation["time_s"]:
                    first_violation = finding
            previous = (at, gap)
            if detailed:
                samples[at] = inf
    if detailed:
        for at in samples:
            samples[at] = min(position(right, at) - position(left, at)
                              for left, right in zip(order, order[1:]))
    return {
        "ok": first_violation is None, "min_gap": best[0], "pair": list(best[1:3]),
        "time_s": best[3], "threshold": threshold, "margin": best[0] - threshold,
        "first_violation": first_violation,
        "samples": [{"time_s": at, "min_gap": samples[at]} for at in sorted(samples)],
    }


def capacity_conflict(capacity, bookings, horizon):
    """Sweep [start, end); equal-time exits and entries use one net change."""
    deltas = defaultdict(lambda: defaultdict(int))
    for booking in bookings:
        resource = booking["resource_id"]
        begin = booking["start_s"]
        end = horizon if booking["end_s"] is None else booking["end_s"]
        if resource not in capacity or begin < 0 or begin > end or end > horizon:
            raise ValueError("Invalid resource booking")
        deltas[resource][begin] += booking["demand"]
        # A retained holder remains present in the terminal state, including at H.
        if booking["end_s"] is not None:
            deltas[resource][end] -= booking["demand"]
    for resource, events in sorted(deltas.items()):
        use = 0
        # Resolve negligible numerical differences between the same event endpoints.
        net = defaultdict(int)
        for at, delta in events.items():
            net[round(at, 8)] += delta
        for at, delta in sorted(net.items()):
            use += delta
            if use > capacity[resource]:
                return {"resource_id": resource, "time_s": at, "demand": use,
                        "capacity": capacity[resource]}
    return None


def clearing_positions(case, paths, actor, start, through):
    """Try to clear the full route by moving idle neighbours to declared bays."""
    ordered = case["cranes"]
    index = next(i for i, c in enumerate(ordered) if c["id"] == actor)
    gap = case["params"]["gap"]
    moves = {}
    for side in (-1, 1):
        limit = (min(through) if side < 0 else max(through)) + side * gap
        for idx in range(index + side, -1 if side < 0 else len(ordered), side):
            crane = ordered[idx]
            cid = crane["id"]
            end, x = tail(case, paths, cid)
            if end > start + EPS:
                segment = next(s for s in paths[cid] if s["t1_s"] > start)
                x = segment["x0"] + segment["v0"] * (start - segment["t0_s"])
            if side * (x - limit) < -EPS:
                # The crane can only be moved after all of its reservations clear.
                legal = [p for p in case["parking"] if side * (p - limit) >= -EPS]
                if not legal or not crane["dispatch"] or end > start + EPS:
                    return None
                x = min(legal, key=lambda p: (abs(p - x), p))
                moves[cid] = x
            limit = x + side * gap
    return moves


def booking(resource, start, end, task, role):
    return {"resource_id": resource, "start_s": start, "end_s": end, "task_id": task["id"],
            "ladle_id": task["ladle"], "demand": 1, "role": role,
            "retained_at_horizon": end is None, "authority": "q1_declared_scenario"}


def source_entry(task, completed):
    prior = task.get("previous_bof")
    return max(task["rho_s"], completed[prior]["pickup_complete_s"]) if prior else task["rho_s"]


def make_reservations(case, task, bookings, completed, lifted, unloading, dropped):
    """Reserve the held source, destination stages and final continuing holder."""
    tid = task["id"]
    source_task = task["predecessor"]
    updated = list(bookings)
    if source_task:
        held = next((i for i, item in enumerate(bookings)
                     if item["task_id"] == source_task and item["end_s"] is None), None)
        if held is None or bookings[held]["ladle_id"] != task["ladle"]:
            return None, "source_holder_unavailable"
        source_holder = bookings[held]["resource_id"]
        updated[held] = bookings[held] | {
            "end_s": lifted, "retained_at_horizon": False, "end_event_id": f"{tid}/pickup_complete",
        }
    else:
        source_holder = f"bof/{task['origin']}"
        updated.append(booking(source_holder, source_entry(task, completed), lifted, task, "upstream_receipt")
                       | {"start_event_id": f"{tid}/source_entry", "end_event_id": f"{tid}/pickup_complete"})
    service = task["service"]
    dest = task["destination"]
    kind = service["kind"]
    start = max(dropped, service["not_before_s"])
    handoff = None
    if kind == "cast":
        previous = task["previous_cast"]
        end_of_previous = completed[previous]["service_end_s"] if previous else 0
        start = max(start, dropped + case.get("caster_preparation_s", case["seat_lead_s"]), end_of_previous)
        stop = start + service["duration_s"]
        turn = case["transfer_operation_s"]
        done = stop + turn
        receiver = f"seat/{dest}"
        handoff = f"return/{dest}"
        updated += [
            booking(receiver, unloading, start, task, "seat")
            | {"start_event_id": f"{tid}/drop_start", "end_event_id": f"{tid}/supply_start"},
            booking(f"supply/{dest}", start, stop, task, "casting")
            | {"start_event_id": f"{tid}/supply_start", "end_event_id": f"{tid}/supply_end"},
            booking(handoff, stop, None, task, "empty_ladle_position")
            | {"start_event_id": f"{tid}/supply_end", "end_event_id": None},
        ]
    else:
        done = stop = start + service["duration_s"]
        receiver = handoff = f"{kind}/{dest}"
        updated.append(booking(receiver, unloading, None, task, f"{kind}_and_holding")
                       | {"start_event_id": f"{tid}/drop_start", "end_event_id": None})
    if done > case["horizon_s"] + EPS:
        return None, "service_after_horizon"
    conflict = capacity_conflict(case["capacity"], updated, case["horizon_s"])
    if conflict:
        return None, f"resource_conflict:{conflict['resource_id']}"
    return (updated, source_holder, receiver, handoff, start, stop, done), None


def candidate(case, task, crane, paths, bookings, completed, departure, release, drop_floor,
              enforce, *, clear=False, pause=None, slot=None, timing=None):
    """Construct and check one complete spatiotemporal reservation."""
    new_paths = {k: list(v) for k, v in paths.items()}
    reconnect = None
    if slot is not None:
        reconnect = new_paths[crane][slot]
        new_paths[crane] = new_paths[crane][:slot]
    t0, before = tail(case, new_paths, crane)
    departure = max(t0, departure)
    params = case["params"]
    speed = params["speed"]
    x, y = case["positions"][task["origin"]], case["positions"][task["destination"]]
    tid, ladle = task["id"], task["ladle"]
    move_time = departure
    if clear:
        moves = clearing_positions(case, paths, crane, departure, (before, x, y))
        if not moves:
            return None, "no_clearance_template"
        duration = max(abs(pos - tail(case, paths, cid)[1]) for cid, pos in moves.items()) / speed
        move_time += duration
        for cid, target in moves.items():
            initial = write_tail(case, new_paths, cid, departure)
            add_segment(new_paths[cid], cid, departure, move_time, initial, target, "yield", tid)
    p = new_paths[crane]
    write_tail(case, new_paths, crane, departure)
    add_segment(p, crane, departure, move_time, before, before, "wait_empty", tid)
    approach = move_time + abs(x - before) / speed
    entry = source_entry(task, completed) if not task["predecessor"] else None
    pick = max(approach, release, task["pickup_window"]["hard_earliest_s"] or 0, entry or 0)
    if timing is not None:
        timing["pickup_start_s"] = pick
        timing["direct_loaded_travel_s"] = abs(y - x) / speed
    pickup_limit = task["pickup_window"]["hard_latest_s"]
    if pick > min(release + params["max_pick_wait_s"],
                  inf if pickup_limit is None else pickup_limit) + EPS:
        return None, "pickup_window"
    lift = pick + case["pickup_operation_s"]
    add_segment(p, crane, move_time, approach, before, x, "empty_move", tid)
    add_segment(p, crane, approach, pick, x, x, "wait_empty", tid)
    add_segment(p, crane, pick, lift, x, x, "pickup", tid)
    arrival = lift + abs(y - x) / speed
    drop_s = case["drop_operation_s"]
    drop_earliest = task["delivery_window"]["hard_earliest_s"]
    unload = max(arrival, drop_floor, (drop_earliest or 0) - drop_s)
    drop_wait = unload - arrival
    if pause is None:
        add_segment(p, crane, lift, arrival, x, y, "loaded_move", tid, ladle)
        add_segment(p, crane, arrival, unload, y, y, "wait_drop", tid, ladle)
        enroute_wait = 0
    else:
        first = lift + abs(pause - x) / speed
        add_segment(p, crane, lift, first, x, pause, "loaded_move", tid, ladle)
        add_segment(p, crane, first, first + drop_wait, pause, pause, "wait_loaded", tid, ladle)
        add_segment(p, crane, first + drop_wait, unload, pause, y, "loaded_move", tid, ladle)
        enroute_wait, drop_wait = drop_wait, 0
        arrival = unload
    dropped = unload + drop_s
    if timing is not None:
        timing["drop_complete_s"] = dropped
    delivery_limit = task["delivery_window"]["hard_latest_s"]
    if delivery_limit is not None and dropped > delivery_limit + EPS:
        return None, "hard_delivery_window"
    target = task["plan_target_s"]
    lateness = max(0, dropped - target) if target is not None else None
    if enforce and lateness is not None and lateness > EPS:
        return None, "plan_target"
    if dropped > case["horizon_s"]:
        return None, "after_horizon"
    add_segment(p, crane, unload, dropped, y, y, "drop", tid, ladle)
    return_run = 0
    if reconnect is not None:
        return_run = abs(y - reconnect["x1"]) / speed
        restored = dropped + return_run
        if restored > reconnect["t1_s"] + EPS:
            return None, "reserved_future_action"
        add_segment(p, crane, dropped, restored, y, reconnect["x1"], "empty_move", tid)
        add_segment(p, crane, restored, reconnect["t1_s"], reconnect["x1"], reconnect["x1"], "idle")
        p.extend(paths[crane][slot + 1:])
    if not continuous_gaps(case, new_paths)["ok"]:
        return None, "continuous_gap"
    result, reason = make_reservations(case, task, bookings, completed, lift, unload, dropped)
    if reason:
        return None, reason
    held, source, receiver, final_holder, service_start, service_end, available = result
    return {
        "paths": new_paths, "bookings": held, "first_action_s": departure,
        "task_id": tid, "heat_id": task["heat"], "ladle_id": ladle, "crane_id": crane,
        "departure_s": departure, "empty_arrival_s": approach, "release_s": release,
        "pickup_start_s": pick, "pickup_complete_s": lift,
        "delivery_arrival_s": arrival, "drop_start_s": unload, "drop_complete_s": dropped,
        "service_start_s": service_start, "service_end_s": service_end,
        "source_entry_s": entry,
        "prepare_complete_s": (dropped + case.get("caster_preparation_s", case["seat_lead_s"])
                               if task["service"]["kind"] == "cast" else None),
        "holder_ready_s": available, "source_holder": source, "delivery_holder": receiver,
        "next_holder": final_holder, "lateness_s": lateness,
        "waiting_s": {"pickup": pick - release, "path": enroute_wait,
                      "drop": drop_wait, "seat": service_start - dropped},
        "empty_run_s": (abs(x - before) / speed + return_run
                        + (len(moves) * (move_time - departure) if clear else 0)),
        "loaded_run_s": abs(y - x) / speed,
        "yield_cranes": sorted(moves) if clear else [],
    }, None


def propose(case, task, paths, books, complete, now, release, enforce, *, attempts=None, unlock_pad_s=0):
    rejections = Counter()
    choices = []
    _, boundary = case["rail"]
    phases = {s["t1_s"] for segments in paths.values() for s in segments if s["t1_s"] > now}
    # Include finite crossings used for waiting or clearing after a locked reservation.
    unlocks = sorted({b["end_s"] for b in books if b["end_s"] is not None and b["end_s"] > now})
    if unlock_pad_s:
        unlocks = sorted(set(unlocks) | {t + unlock_pad_s for t in unlocks})
    end = case["horizon_s"]
    release_end = release + case["params"]["max_pick_wait_s"]
    max_service = boundary / case["params"]["speed"] + 1
    drop_times = sorted({0, *(x for x in unlocks if x < min(end, release_end + max_service))})

    def try_candidate(crane, start, floor, *, clear, pause=None, slot=None):
        timing = {} if attempts is not None else None
        option, reason = candidate(case, task, crane, paths, books, complete, start,
                                   release, floor, enforce, clear=clear, pause=pause, slot=slot,
                                   timing=timing)
        if attempts is not None:
            attempts.append({
                "crane_id": crane, "departure_s": start, "drop_floor_s": floor,
                "clear": clear, "pause_position": pause, "idle_slot_index": slot,
                "rejection": reason, **timing,
            })
        return option, reason

    for crane in task["eligible"]:
        locked = len(case["locked_trajectories"].get(crane, []))
        for slot, segment in enumerate(paths[crane]):
            if slot < locked or segment["mode"] != "idle" or segment["t1_s"] <= now:
                continue
            start = max(now, segment["t0_s"])
            if start > release_end:
                continue
            for floor in drop_times:
                found = False
                for clear in (False, True):
                    option, reason = try_candidate(crane, start, floor, slot=slot, clear=clear)
                    if option:
                        choices.append(option)
                        found = True
                    else:
                        rejections[reason] += 1
                if found:
                    break
        ready, position = tail(case, paths, crane)
        depart = max(ready, now)
        origin = case["positions"][task["origin"]]
        destination = case["positions"][task["destination"]]
        drive = abs(position - origin) / case["params"]["speed"]
        time_to_release = max(depart, release - drive)
        starts = sorted({depart, time_to_release, *(t for t in phases if depart <= t <= release_end),
                         *(t for t in unlocks if depart <= t <= release_end)})
        for start in starts:
            if start > release_end:
                continue
            accepted = False
            for floor in drop_times:
                for clear in (False, True):
                    option, reason = try_candidate(crane, start, floor, clear=clear)
                    if option:
                        choices.append(option)
                        accepted = True
                    else:
                        rejections[reason] += 1
                    if reason == "continuous_gap" and floor > 0:
                        # A later receipt can wait at a declared intermediate stop.
                        for p in case["parking"]:
                            if min(origin, destination) < p < max(origin, destination):
                                option, reason = try_candidate(crane, start, floor, clear=clear, pause=p)
                                if option:
                                    choices.append(option)
                                    accepted = True
                                else:
                                    rejections[reason] += 1
                if accepted:
                    break
            if accepted:
                break
    return choices, rejections


def task_row(task, proposed, ready, status, reasons, horizon):
    outcome = {
        "task_id": task["id"], "heat_id": task["heat"], "ladle_id": task["ladle"],
        "scope": task["scope"], "scope_status": task["scope_status"],
        "required_in_process": task["required_in_process"],
        "required_in_ef_scope": task["required_in_ef_scope"],
        "original_admission": task["source_admission"], "original_blockers": task["source_blockers"],
        "origin": task["origin"], "destination": task["destination"],
        "predecessor": task["predecessor"], "reuse_predecessor": task["reuse_predecessor"],
        "previous_cast": task["previous_cast"], "previous_bof": task.get("previous_bof"),
        "plan_target_s": task["plan_target_s"],
        "pickup_window": task["pickup_window"], "delivery_window": task["delivery_window"],
        "release_s": ready, "release_type": "planning_assumption",
        "status": status, "reasons": reasons, "crane_id": None, "pickup_start_s": None,
        "drop_complete_s": None,
        "wait_censored_s": (max(0, horizon - ready) if ready is not None and ready <= horizon
                            else None) if status != FINISHED else None,
    }
    if proposed:
        outcome.update({k: v for k, v in proposed.items() if k not in {"paths", "bookings"}})
    return outcome


def event_rows(tasks, completed, horizon, retained_roots):
    events = []
    for task in tasks:
        tid = task["id"]
        if tid not in completed:
            if tid in retained_roots:
                events.append({
                    "event_id": f"{tid}/source_entry", "event_type": "source_entry",
                    "task_id": tid, "heat_id": task["heat"], "ladle_id": task["ladle"],
                    "crane_id": None, "time_s": retained_roots[tid], "visible_at_s": None,
                    "holder_before": "outside_ef_upstream", "holder_after": f"bof/{task['origin']}",
                    "source_status": "derived", "clock_bound_s": horizon,
                })
            continue
        row = completed[tid]
        source, receiver = row["source_holder"], row["delivery_holder"]
        crane = row["crane_id"]
        stages = [
            ("physical_release", "release_s", None, None),
            ("pickup_start", "pickup_start_s", None, None),
            ("pickup_complete", "pickup_complete_s", source, crane),
            ("delivery_arrival", "delivery_arrival_s", None, None),
            ("drop_start", "drop_start_s", None, None),
            ("drop_complete", "drop_complete_s", crane, receiver),
        ]
        if row["source_entry_s"] is not None:
            stages.insert(1, ("source_entry", "source_entry_s", "outside_ef_upstream", source))
        kind = task["service"]["kind"]
        if kind == "cast":
            stages += [
                ("seat_complete", "drop_complete_s", None, None),
                ("prepare_complete", "prepare_complete_s", None, None),
                ("supply_start", "service_start_s", receiver, f"supply/{task['destination']}"),
                ("supply_end", "service_end_s", f"supply/{task['destination']}", row["next_holder"]),
            ]
        else:
            stages += [(f"{kind}_start", "service_start_s", None, None),
                       (f"{kind}_end", "service_end_s", None, None)]
        for phase, stamp, sender, recipient in stages:
            at = row[stamp]
            events.append({
                "event_id": f"{tid}/{phase}", "event_type": phase, "task_id": tid,
                "heat_id": task["heat"], "ladle_id": task["ladle"], "crane_id": crane,
                "time_s": at, "visible_at_s": None, "holder_before": sender,
                "holder_after": recipient, "source_status": "derived",
                "clock_bound_s": horizon,
            })
    # Presentation order only. Physical precedence is encoded by the stage and dependency.
    return sorted(events, key=lambda e: e["time_s"])


def task_dependencies(task):
    return tuple(tid for tid in (task["predecessor"], task["reuse_predecessor"],
                                 task["previous_cast"], task.get("previous_bof")) if tid)


def planned_release(case, task, complete):
    if any(tid not in complete for tid in task_dependencies(task)):
        return None
    bounds = [task["rho_s"]] if task["rho_s"] is not None else []
    if task["predecessor"]:
        bounds.append(complete[task["predecessor"]]["holder_ready_s"] + case["params"]["prep_s"])
    return max(bounds) if bounds else None


def calculate_metrics(rows, paths, gap, horizon, rejections, plans):
    scope = [t for t in rows if t["required_in_ef_scope"] is True]
    complete = [t for t in scope if t["status"] == FINISHED]
    targets = [t for t in scope if t["plan_target_s"] is not None and t["plan_target_s"] <= horizon]
    done_targets = [t for t in targets if t["status"] == FINISHED]
    late = [t["lateness_s"] for t in done_targets if t["lateness_s"] > EPS]
    waits = {k: sum(t["waiting_s"][k] for t in complete) for k in ("pickup", "path", "drop", "seat")}
    per_crane = {}
    for cid, path in paths.items():
        times = defaultdict(float)
        for segment in path:
            times[segment["mode"]] += segment["t1_s"] - segment["t0_s"]
        per_crane[cid] = dict(times) | {"utilization": (horizon - times["idle"]) / horizon}
    return {
        "target_ef": len(scope), "completed_ef": len(complete),
        "completion_rate": len(complete) / len(scope) if scope else None,
        "plan_targets": len(targets), "on_time": len(done_targets) - len(late),
        "on_time_rate": (len(done_targets) - len(late)) / len(targets) if targets else None,
        "late_count": len(late), "late_total_s": sum(late), "late_max_s": max(late, default=0),
        "unserved_targets": len(targets) - len(done_targets),
        "unfinished_delay_lower_bound_s": sum(horizon - t["plan_target_s"] for t in targets
                                              if t["status"] != FINISHED),
        "waiting_s": waits,
        "censored_wait_s": sum(t["wait_censored_s"] or 0 for t in scope),
        "censored_wait_count": sum(t["wait_censored_s"] is not None for t in scope),
        "empty_run_s": sum(c.get("empty_move", 0) + c.get("yield", 0) for c in per_crane.values()),
        "loaded_run_s": sum(c.get("loaded_move", 0) for c in per_crane.values()),
        "idle_crane_s": sum(c.get("idle", 0) for c in per_crane.values()),
        "empty_assigned_wait_s": sum(c.get("wait_empty", 0) for c in per_crane.values()),
        "utilization_by_crane": per_crane, "min_gap": gap["min_gap"],
        "min_margin": gap["margin"], "gap_threshold": gap["threshold"],
        "candidate_capacity_rejections": sum(n for k, n in rejections.items()
                                             if k.startswith("resource_conflict:")),
        "candidate_gap_rejections": rejections.get("continuous_gap", 0),
        "shifted_supply_count": sum(t["service_start_s"] > plans[t["task_id"]] + EPS
                                    for t in complete if t["task_id"] in plans),
        "accepted_capacity_violations": 0,
        "task_statuses": dict(Counter(t["status"] for t in rows)),
        "blocked_reasons": dict(Counter(reason for t in rows if t["status"] != FINISHED
                                        for reason in t["reasons"])),
        "supply_continuity": "unknown_without_buffer_evidence",
    }


def candidate_rank(option):
    return (option["lateness_s"] or 0, sum(option["waiting_s"].values()),
            option["empty_run_s"] + option["loaded_run_s"],
            option["drop_complete_s"], option["crane_id"])


def return_rank(option):
    return (option["drop_complete_s"], option["empty_run_s"], option["crane_id"])


def recorded_option(option):
    fields = ("task_id", "crane_id", "first_action_s", "pickup_start_s", "drop_complete_s",
              "lateness_s", "waiting_s", "empty_run_s", "loaded_run_s", "yield_cranes")
    return {key: option[key] for key in fields}


def solve(case, policy, enforce_plan=False, *, audit=None, rank_candidate=candidate_rank,
          search_lookahead_s=None, unlock_pad_s=0):
    validate_case(case)
    if policy not in POLICIES:
        raise ValueError("Unknown Q1 dispatch policy")
    paths = {c["id"]: list(case["locked_trajectories"].get(c["id"], [])) for c in case["cranes"]}
    books = list(case["initial_occupancy"])
    horizon = case["horizon_s"]
    lookahead = case["lookahead_s"] if search_lookahead_s is None else search_lookahead_s
    validate_trajectory(case, paths)
    if not continuous_gaps(case, paths)["ok"] or capacity_conflict(case["capacity"], books, horizon):
        raise ValueError("Unsafe locked Q1 snapshot")
    complete = {}
    known_release = {}
    blocked = {t["id"]: list(t["blocks"]) for t in case["tasks"] if t["blocks"]}
    rejected = Counter()
    last_reasons = {}
    decisions = []
    empty_returns = {
        t["predecessor"]: t for t in case["tasks"]
        if t["kind"] == "ef" and t["load_state"] == "empty_ladle" and t["predecessor"]
        and not t["blocks"]
    }
    now = 0
    while now <= horizon + EPS:
        offers = []
        next_times = []
        step = {"now_s": now, "tasks": [], "selected": None, "return_reservation": None}
        for task in case["tasks"]:
            tid = task["id"]
            if tid in complete or tid in blocked:
                continue
            release = planned_release(case, task, complete)
            entry = None
            if audit is not None:
                entry = {
                    "task_id": tid, "release_s": release, "eligible": task["eligible"],
                    "missing": [dep for dep in task_dependencies(task) if dep not in complete],
                    "attempts": [], "options": [], "task_priority": None,
                }
                step["tasks"].append(entry)
            if release is None:
                if entry is not None:
                    entry["status"] = "predecessor_unfulfilled"
                continue
            known_release[tid] = release
            if release > now + lookahead + EPS:
                next_times.append(release - lookahead)
                if entry is not None:
                    entry["status"] = "outside_lookahead"
                continue
            proposals, causes = propose(case, task, paths, books, complete, now, release, enforce_plan,
                                         attempts=entry["attempts"] if entry is not None else None,
                                         unlock_pad_s=unlock_pad_s)
            rejected.update(causes)
            if causes:
                last_reasons[tid] = causes
            current = [p for p in proposals if p["first_action_s"] <= now + EPS]
            next_times.extend(p["first_action_s"] for p in proposals if p["first_action_s"] > now + EPS)
            if entry is not None:
                entry["options"] = [recorded_option(p) | {"rank": rank_candidate(p)}
                                    for p in proposals]
                entry["status"] = ("selectable" if current else
                                   "future_option" if proposals else "no_generated_option")
            if not current:
                if release > now + EPS:
                    next_times.append(release)
                continue
            # Earliest feasible delivery for the priority rule, then evaluate
            # the dispatched task's alternatives by target breach, wait and motion.
            first_del = min(p["drop_complete_s"] for p in current)
            margin = task["plan_target_s"] - first_del if task["plan_target_s"] is not None else inf
            best = min(current, key=rank_candidate)
            order = (release, tid) if policy == "fcfs" else (margin, release, tid)
            if entry is not None:
                entry["task_priority"] = [release > now + EPS, *(None if v == inf else v for v in order)]
            offers.append(((release > now + EPS, *order), best, margin, len(current)))
        if offers:
            _, choice, margin, count = min(offers, key=lambda offer: offer[0])
            tid = choice["task_id"]
            if audit is not None:
                step["selected"] = recorded_option(choice)
            paths, books = choice["paths"], choice["bookings"]
            complete[tid] = {k: v for k, v in choice.items() if k not in {"paths", "bookings"}}
            decisions.append({"now_s": now, "task_id": tid, "crane_id": choice["crane_id"],
                              "candidate_count": count, "plan_margin_s": None if margin == inf else margin,
                              "yield_cranes": choice["yield_cranes"]})
            # Reserve a safe empty exit as part of the cast handoff if the
            # chosen scenario and existing reservations support it. Future
            # commitments retain any free, reconnectable earlier crane gaps.
            returning = empty_returns.get(tid)
            if returning:
                due = planned_release(case, returning, complete)
                if due is not None:
                    known_release[returning["id"]] = due
                    attempt_time = max(now, due - lookahead)
                    return_attempts = [] if audit is not None else None
                    options, causes = propose(case, returning, paths, books, complete,
                                              attempt_time, due, enforce_plan, attempts=return_attempts,
                                              unlock_pad_s=unlock_pad_s)
                    rejected.update(causes)
                    if audit is not None:
                        step["return_reservation"] = {
                            "task_id": returning["id"], "release_s": due, "search_from_s": attempt_time,
                            "eligible": returning["eligible"], "attempts": return_attempts,
                            "options": [recorded_option(p) | {"rank": return_rank(p)} for p in options],
                            "selected": None,
                        }
                    if options:
                        follow = min(options, key=return_rank)
                        paths, books = follow["paths"], follow["bookings"]
                        complete[returning["id"]] = {
                            k: v for k, v in follow.items() if k not in {"paths", "bookings"}
                        }
                        decisions.append({
                            "now_s": now, "task_id": returning["id"], "crane_id": follow["crane_id"],
                            "candidate_count": len(options), "plan_margin_s": None,
                            "yield_cranes": follow["yield_cranes"], "reserved_return_for": tid,
                        })
                        if audit is not None:
                            step["return_reservation"]["selected"] = recorded_option(follow)
            if audit is not None:
                audit.append(step)
            continue
        next_times.extend(s["t1_s"] for path in paths.values() for s in path if s["t1_s"] > now + EPS)
        future = [t for t in next_times if now + EPS < t <= horizon]
        if audit is not None:
            step["advance_to_s"] = min(future) if future else None
            audit.append(step)
        if not future:
            break
        now = min(future)

    rows = []
    retained_roots = {}
    for task in case["tasks"]:
        tid = task["id"]
        row = complete.get(tid)
        if row:
            status = FINISHED
            reasons = []
        elif tid in blocked:
            status = "input_blocked"
            reasons = blocked[tid]
        else:
            missing = [pred for pred in task_dependencies(task) if pred not in complete]
            if missing:
                status, reasons = "dependency_blocked", ["predecessor_unfulfilled"]
            elif known_release.get(tid, 0) > horizon:
                status, reasons = "after_horizon", ["not_due_within_horizon"]
            else:
                status = "candidate_exhausted"
                reasons = [reason for reason, _ in last_reasons.get(tid, Counter()).most_common(3)]
                reasons = reasons or ["no_legal_template"]
        if status == "candidate_exhausted" and task["predecessor"] is None and task["rho_s"] is not None:
            entry = source_entry(task, complete)
            if entry <= horizon:
                retained_roots[tid] = entry
                books.append(booking(f"bof/{task['origin']}", entry, None, task, "retained_upstream_receipt")
                             | {"start_event_id": f"{tid}/source_entry", "end_event_id": None})
        rows.append(task_row(task, row, known_release.get(tid), status, reasons, horizon)
                    | {"unfulfilled_dependencies": [pred for pred in task_dependencies(task)
                                                    if pred not in complete]})
    for cid in paths:
        write_tail(case, paths, cid, horizon)
    gap = continuous_gaps(case, paths, detailed=True)
    events = event_rows(case["tasks"], complete, horizon, retained_roots)
    plans = {t["id"]: t["service"]["not_before_s"] for t in case["tasks"]
             if t["service"] and t["service"]["kind"] == "cast"}
    metrics = calculate_metrics(rows, paths, gap, horizon, rejected, plans)
    succeeded = (all(t["status"] == FINISHED for t in rows if t["required_in_process"] is True)
                 and not (metrics["late_count"] or metrics["unserved_targets"]))
    result = {
        "schema": "mcm-uestc.dispatch-result/1", "information_mode": case["information_mode"],
        "witness_mode": "strict_target_conditional" if enforce_plan else "lateness_diagnostic_conditional",
        "outcome": "on_plan" if succeeded else "unresolved", "scenario": case["scenario"],
        "source": case["source"], "policy": policy, "layout": case["layout"], "unit": case["unit"],
        "horizon_s": horizon, "gap_threshold": gap["threshold"], "tasks": rows,
        "trajectories": [s for path in paths.values() for s in path], "occupancy": books,
        "events": events, "decisions": decisions, "gap_samples": gap.pop("samples"), "gap": gap,
        "metrics": metrics, "candidate_rejections": dict(rejected),
        "case_sha256": digest(case), "plan_version": case["plan_version"],
        "solver_status": "conditional_schedule" if succeeded else "partial_witness",
        "certificate_scope": "declared_Q1_parameters_no_complete_search",
        "fixed_plan_overlap_count": case["fixed_plan_overlap_count"],
    }
    verify_trace(case, result)
    return result


def validate_trajectory(case, paths, full=False):
    for crane in case["cranes"]:
        cid = crane["id"]
        end, x = 0, crane["x"]
        for segment in paths[cid]:
            dt = segment["t1_s"] - segment["t0_s"]
            velocity = (segment["x1"] - segment["x0"]) / dt if dt > 0 else inf
            if (dt <= 0 or abs(end - segment["t0_s"]) > EPS or abs(x - segment["x0"]) > EPS
                    or not case["rail"][0] <= segment["x1"] <= case["rail"][1]
                    or abs(velocity) > case["params"]["speed"] + EPS
                    or abs(velocity - segment["v0"]) > EPS or abs(velocity - segment["v1"]) > EPS
                    or (not crane["dispatch"] and abs(velocity) > EPS)):
                raise ValueError("Invalid Q1 trajectory kinematics or stationary crane")
            end, x = segment["t1_s"], segment["x1"]
        if end > case["horizon_s"] + EPS or (full and abs(end - case["horizon_s"]) > EPS):
            raise ValueError("Incomplete Q1 trajectory clock coverage")


def verify_trace(case, result):
    """A's same-interface self-check. C rebuilds checks independently."""
    paths = {c["id"]: [] for c in case["cranes"]}
    for segment in result["trajectories"]:
        paths[segment["crane_id"]].append(segment)
    validate_trajectory(case, paths, full=True)
    if (not continuous_gaps(case, paths)["ok"]
            or capacity_conflict(case["capacity"], result["occupancy"], case["horizon_s"])):
        raise ValueError("Committed Q1 witness violates a hard constraint")
    inputs = {t["id"]: t for t in case["tasks"]}
    completed = {r["task_id"]: r for r in result["tasks"] if r["status"] == FINISHED}
    if len(result["tasks"]) != len(inputs) or {r["task_id"] for r in result["tasks"]} != set(inputs):
        raise ValueError("Q1 result does not cover the frozen roster")
    initial = {c["id"]: c["x"] for c in case["cranes"]}
    ends = {cid: [s["t1_s"] for s in path] for cid, path in paths.items()}
    events = {e["event_id"]: e for e in result["events"]}
    for interval in result["occupancy"]:
        for edge in ("start", "end"):
            event_id = interval.get(f"{edge}_event_id")
            if event_id and abs(events[event_id]["time_s"] - interval[f"{edge}_s"]) > EPS:
                raise ValueError("Resource booking disagrees with its triggering event")
    carrying = defaultdict(list)
    crane_busy = defaultdict(list)
    for tid, row in completed.items():
        task = inputs[tid]
        crane = row["crane_id"]
        expected = planned_release(case, task, completed)
        order = [row[f"{k}_s"] for k in ("release", "pickup_start", "pickup_complete",
                                        "delivery_arrival", "drop_start", "drop_complete")]
        if (task["blocks"] or task["kind"] != "ef" or row["crane_id"] not in task["eligible"]
                or expected is None or abs(expected - order[0]) > EPS
                or any(b < a - EPS for a, b in zip(order, order[1:]))
                or order[1] > order[0] + case["params"]["max_pick_wait_s"] + EPS):
            raise ValueError("Invalid task assignment, order or release")
        for window, stamp in (("pickup_window", order[1]), ("delivery_window", order[-1])):
            earliest, latest = (task[window][f"hard_{edge}_s"] for edge in ("earliest", "latest"))
            if ((earliest is not None and stamp < earliest - EPS)
                    or (latest is not None and stamp > latest + EPS)):
                raise ValueError("Q1 task violates a hard window")
        for at, site in ((order[1], task["origin"]), (order[2], task["origin"]),
                         (order[3], task["destination"]), (order[4], task["destination"]),
                         (order[5], task["destination"])):
            if abs(position_at(paths[crane], initial[crane], at, ends[crane])
                   - case["positions"][site]) > EPS:
                raise ValueError("Q1 pickup or drop is off its service station")
        actual_dwell = sum(max(0, min(segment["t1_s"], order[-1]) - max(segment["t0_s"], order[2]))
                           for segment in paths[crane]
                           if segment["task_id"] == tid and segment["ladle_id"] == task["ladle"])
        if abs(actual_dwell - (order[-1] - order[2])) > EPS:
            raise ValueError("Q1 trajectory omits the ladle in a loaded interval")
        carrying[task["ladle"]].append((order[2], order[-1]))
        crane_busy[crane].append((order[1], order[-1]))
        if task["predecessor"] and completed[task["predecessor"]]["next_holder"] != row["source_holder"]:
            raise ValueError("Discontinuous holder chain")
        if not task["predecessor"] and row["pickup_start_s"] < source_entry(task, completed) - EPS:
            raise ValueError("Upstream receipt not ready")
        service = task["service"]
        if service["kind"] == "cast":
            previous = task["previous_cast"]
            previous_end = completed[previous]["service_end_s"] if previous else 0
            preparation = order[-1] + case.get("caster_preparation_s", case["seat_lead_s"])
            start, stop = row["service_start_s"], row["service_end_s"]
            supply = [(b["start_s"], b["end_s"]) for b in result["occupancy"]
                      if b["task_id"] == tid and b["resource_id"] == f"supply/{task['destination']}"]
            if (start < max(preparation, previous_end, service["not_before_s"]) - EPS
                    or abs(stop - start - service["duration_s"]) > EPS
                    or supply != [(start, stop)]):
                raise ValueError("Casting timing or capacity booking disagrees with the planned process")
    for intervals in (*carrying.values(), *crane_busy.values()):
        intervals.sort()
        if any(b[0] < a[1] - EPS for a, b in zip(intervals, intervals[1:])):
            raise ValueError("Duplicate ladle holder or overlapping crane operations")
