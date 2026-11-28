"""Independent Q2 event, resource and dispatch-log checks."""

from collections import Counter, defaultdict
from math import inf, isclose, isfinite


TIME_TOL = 1e-7
PLAN_TOL = 1e-8
POLICIES = ("fcfs", "tight_margin", "quantile_buffer")
TRANSPORT = ("pickup_start", "pickup_complete", "delivery_arrival", "drop_start", "drop_complete")
SERVICES = {
    "refine": ("refine_start", "refine_end"), "slag": ("slag_start", "slag_end"),
    "cast": ("supply_start", "supply_end"),
}


def close(left, right):
    if left is None or right is None:
        return left is right
    return isfinite(left) and isfinite(right) and isclose(left, right, abs_tol=TIME_TOL, rel_tol=0)


def within(at, window):
    return ((window["hard_earliest_s"] is None or at >= window["hard_earliest_s"] - TIME_TOL)
            and (window["hard_latest_s"] is None or at <= window["hard_latest_s"] + TIME_TOL))


def choose_option(strategy, options):
    """Compare each task at its earliest feasible delivery and apply stable ties."""
    if strategy not in POLICIES:
        raise ValueError("unknown Q2 policy")
    per_task = {}
    for option in sorted(options, key=lambda value: (value["drop_s"], value["crane_id"])):
        per_task.setdefault(option["task_id"], option)

    def order(option):
        stable = (option["released_s"], option["task_id"], option["drop_s"], option["crane_id"])
        if strategy == "fcfs":
            return stable
        margin = option["margin_s"] if option["margin_s"] is not None else inf
        if strategy == "quantile_buffer":
            margin -= option["buffer_s"]
        return (margin, *stable)

    return min(per_task.values(), key=order) if per_task else None


def receiver(task):
    service = task["service"]["kind"]
    return f"{'seat' if service == 'cast' else service}/{task['destination']}"


def expected_transition(kind, task, crane):
    if kind == "source_entry":
        return None, f"bof/{task['origin']}"
    if kind == "pickup_complete":
        return None, crane  # The previous event ledger supplies the source.
    if kind == "drop_complete":
        return crane, receiver(task)
    dest = task["destination"]
    if kind == "supply_start":
        return f"seat/{dest}", f"supply/{dest}"
    if kind == "supply_end":
        return f"supply/{dest}", f"return/{dest}"
    return None, None


def inspect_event_ledger(result, tasks, horizon, fail):
    """Rebuild occupancy from transfers and drop starts, without using claims."""
    by_task, open_holds = defaultdict(dict), {}
    owners, crane_contents, intervals = {}, {}, []
    events_by_id = set()
    last = -inf
    for event in result["events"]:
        kind, tid, ladle = (event[key] for key in ("event_type", "task_id", "ladle_id"))
        at, visible, key = (event[name] for name in ("time_s", "visible_at_s", "event_id"))
        if tid not in tasks:
            fail("unknown_event_task", task_id=tid, time_s=at)
            continue
        task = tasks[tid]
        if (key in events_by_id or kind in by_task[tid] or key != f"{tid}/{kind}"
                or event["heat_id"] != task["heat"] or ladle != task["ladle"]):
            fail("event_identity_or_duplicate", task_id=tid, event_id=key)
        if task["blocks"] or task["kind"] != "ef":
            fail("blocked_or_exterior_task_executed", task_id=tid, event_id=key)
        if (not isfinite(at) or not isfinite(visible) or at < last - TIME_TOL
                or at < -TIME_TOL or at > horizon + TIME_TOL or visible < at - TIME_TOL):
            fail("event_time_or_visibility", task_id=tid, event_id=key, time_s=at)
        last = at
        events_by_id.add(key)
        by_task[tid][kind] = event
        crane = event.get("crane_id")
        before, after = expected_transition(kind, task, crane)
        if kind == "pickup_complete":
            before = owners.get(ladle)
            if before is None or before.rsplit("/", 1)[-1] != task["origin"]:
                fail("pickup_wrong_holder_or_station", task_id=tid, time_s=at)
        if (event.get("holder_before"), event.get("holder_after")) != (before, after):
            fail("event_handoff_semantics", task_id=tid, event_id=key)
        if after is not None:
            if owners.get(ladle) != before:
                fail("duplicate_or_unmatched_handoff", task_id=tid, event_id=key)
            if kind == "pickup_complete" and crane:
                if crane in crane_contents:
                    fail("two_ladles_on_one_crane", task_id=tid, event_id=key)
                crane_contents[crane] = ladle
            if kind == "drop_complete" and crane_contents.pop(crane, None) != ladle:
                fail("crane_drops_unowned_ladle", task_id=tid, event_id=key)
            owners[ladle] = after
        if kind in {"pickup_complete", "supply_start", "supply_end"}:
            interval = open_holds.pop((before, ladle), None)
            if interval is None:
                fail("resource_release_without_hold", task_id=tid, event_id=key)
            else:
                interval.update(end_s=at, end_event_id=key, retained_at_horizon=False)
        opening = {
            "source_entry": (after, "upstream_receipt"),
            "supply_start": (after, "casting"),
            "supply_end": (after, "empty_ladle_position"),
        }.get(kind)
        if kind == "drop_start":
            opening = receiver(task), ("seat" if task["service"]["kind"] == "cast" else "service_hold")
        if opening:
            facility, role = opening
            if (facility, ladle) in open_holds:
                fail("duplicate_open_hold", task_id=tid, event_id=key)
            interval = {
                "resource_id": facility, "task_id": tid, "ladle_id": ladle, "start_s": at,
                "end_s": None, "role": role, "start_event_id": key, "end_event_id": None,
                "demand": 1, "retained_at_horizon": True,
            }
            open_holds[(facility, ladle)] = interval
            intervals.append(interval)
    return by_task, intervals


def check_occupancy(archived, reconstructed, capacities, fail):
    expected = {x["start_event_id"]: x for x in reconstructed}
    reported = {x["start_event_id"]: x for x in archived}
    if set(reported) != set(expected) or len(reported) != len(archived):
        fail("occupancy_event_population")
    deltas = defaultdict(Counter)
    for start_id, interval in expected.items():
        other = reported.get(start_id, {})
        if any(not close(value, other.get(k)) if k in {"start_s", "end_s"} else value != other.get(k)
               for k, value in interval.items()):
            fail("occupancy_event_mismatch", event_id=start_id)
        resource, start, end = (interval[k] for k in ("resource_id", "start_s", "end_s"))
        if resource not in capacities or end is not None and end < start - TIME_TOL:
            fail("resource_capacity_or_interval_missing", event_id=start_id, resource_id=resource)
            continue
        deltas[resource][start] += 1
        if end is not None:
            deltas[resource][end] -= 1
    for resource, marks in deltas.items():
        count = 0
        for at in sorted(marks):
            count += marks[at]
            if count > capacities[resource] or count < 0:
                fail("resource_capacity", resource_id=resource, time_s=at, actual=count,
                     limit=capacities[resource])


def phase_at(events, name):
    return events[name]["time_s"] if name in events else None


def end_of(task, by_task):
    service = task["service"]
    return phase_at(by_task.get(task["id"], {}), SERVICES[service["kind"]][1]) if service else None


def validate_task(task, row, events, by_task, tasks, case, profile, settings, tape, fail):
    tid, horizon = task["id"], case["horizon_s"]
    previous = task["predecessor"]
    ready, admission = (phase_at(events, key) for key in ("physical_release", "source_entry"))
    service = task["service"]
    times = {key: phase_at(events, key) for key in TRANSPORT}
    pick, lift, arrival, lower, drop = times.values()
    start = end = None
    if service:
        start_name, end_name = SERVICES[service["kind"]]
        start, end = phase_at(events, start_name), phase_at(events, end_name)
    sequence = ["physical_release", "source_entry", *TRANSPORT]
    if service and service["kind"] == "cast":
        sequence.extend(("seat_complete", "prepare_complete"))
    if service:
        sequence.extend(SERVICES[service["kind"]])
    present = set(events)
    if list(events) != [kind for kind in sequence if kind in present]:
        fail("unexpected_task_event_type_or_order", task_id=tid)
    expected_ready = None
    if task["kind"] == "ef" and not task["blocks"]:
        if previous:
            predecessor = tasks.get(previous)
            if predecessor:
                if (predecessor["heat"], predecessor["ladle"], predecessor["destination"]) != (
                        task["heat"], task["ladle"], task["origin"]):
                    fail("predecessor_identity_or_station", task_id=tid)
                parent_end = end_of(predecessor, by_task)
                if parent_end is not None:
                    transfer = case["transfer_operation_s"] if predecessor["service"]["kind"] == "cast" else 0
                    expected_ready = parent_end + transfer + case["params"]["prep_s"]
        elif task["rho_s"] is not None:
            expected_ready = max(0., task["rho_s"])
    if expected_ready is not None and expected_ready > horizon + PLAN_TOL:
        expected_ready = None
    if not close(ready, expected_ready):
        fail("release_event_propagation", task_id=tid, reported=ready, expected=expected_ready)
    if admission is not None:
        previous_bof = task.get("previous_bof")
        reuse = task.get("reuse_predecessor")
        reuse_finish = end_of(tasks[reuse], by_task) if reuse in tasks else None
        prior_pick = phase_at(by_task.get(previous_bof, {}), "pickup_complete")
        if (previous or ready is None or admission < ready - TIME_TOL
                or previous_bof and (prior_pick is None or admission < prior_pick - TIME_TOL)
                or reuse and (reuse_finish is None or admission < reuse_finish - TIME_TOL)):
            fail("premature_source_admission", task_id=tid, time_s=admission)
    if pick is not None:
        transport = [events.get(key) for key in TRANSPORT]
        is_ordered = all(x is not None for x in times.values()) and all(
            left <= right + TIME_TOL for left, right in zip(times.values(), list(times.values())[1:])
        )
        if not is_ordered:
            fail("transport_event_order_or_incomplete", task_id=tid)
        else:
            crane = transport[0].get("crane_id")
            dispatch = {x["crane_id"] for x in profile["initial_state_layouts"][profile["initial_layout"]]
                        if x["dispatch_allowed"]}
            catalog = {c["crane_id"]: c["capacity_t"] for c in profile["crane_catalog"]}
            paths = {tuple(line) for line in profile["authorized_ef_legs"]}
            if (any(event.get("crane_id") != crane for event in transport)
                    or crane not in dispatch or crane not in task["eligible"]
                    or task["mass_upper_t"] is None or task["mass_upper_t"] > catalog.get(crane, -inf)
                    or (task["origin"], task["destination"]) not in paths):
                fail("transport_crane_capacity_or_path", task_id=tid)
            if (ready is None or pick < ready - TIME_TOL or not previous and
                    (admission is None or pick < admission - TIME_TOL)):
                fail("pickup_before_physical_readiness", task_id=tid, time_s=pick)
            if (not within(pick, task["pickup_window"]) or not within(drop, task["delivery_window"])
                    or ready is not None and pick > ready + case["params"]["max_pick_wait_s"] + TIME_TOL):
                fail("transport_hard_window", task_id=tid)
            if settings["strict"] and task["plan_target_s"] is not None and drop > task["plan_target_s"] + TIME_TOL:
                fail("strict_plan_deadline", task_id=tid, time_s=drop)
            if not close(lift - pick, case["pickup_operation_s"]) or not close(drop - lower, case["drop_operation_s"]):
                fail("handling_operation_mismatch", task_id=tid)
            previous_cast = task.get("previous_cast")
            prior_seat = phase_at(by_task.get(previous_cast, {}), "drop_complete")
            if previous_cast and (prior_seat is None or pick < prior_seat - TIME_TOL):
                fail("cast_seating_order", task_id=tid)
    elif any(x is not None for x in times.values()):
        fail("transport_without_pickup", task_id=tid)
    if service and service["kind"] == "cast":
        seat, prepared = (phase_at(events, key) for key in ("seat_complete", "prepare_complete"))
        expected_prepared = seat + case["caster_preparation_s"] if seat is not None else None
        if expected_prepared is not None and expected_prepared > horizon + PLAN_TOL:
            expected_prepared = None
        if not close(seat, drop) or not close(prepared, expected_prepared):
            fail("casting_preparation_chain", task_id=tid)
        predecessor_cast = task["previous_cast"]
        if start is not None and predecessor_cast:
            prior = tasks.get(predecessor_cast)
            prior_end = end_of(prior, by_task) if prior else None
            if prior_end is None or start < prior_end - TIME_TOL:
                fail("same_campaign_cast_sequence", task_id=tid, time_s=start)
    else:
        prepared = drop
    actual_consumed = 0
    if start is not None and service:
        duration = tape.get(tid)
        if (drop is None or prepared is None or start < max(prepared, service["not_before_s"]) - TIME_TOL
                or service["kind"] != "cast" and not close(start, max(drop, service["not_before_s"]))):
            fail("service_starts_before_readiness", task_id=tid, time_s=start)
        if duration is None or not isfinite(duration) or duration <= 0:
            fail("invalid_service_duration_tape", task_id=tid)
        elif start + duration <= horizon + PLAN_TOL:
            if not close(end, start + duration):
                fail("consumed_duration_mismatch", task_id=tid, expected=duration,
                     actual=end - start if end is not None else None)
            else:
                actual_consumed = 1
        elif end is not None:
            fail("completion_beyond_observation", task_id=tid, time_s=end)
    elif end is not None:
        fail("service_end_without_start", task_id=tid, time_s=end)
    elif service and service["kind"] != "cast" and drop is not None and max(
            drop, service["not_before_s"]) <= horizon + PLAN_TOL:
        fail("missing_service_start", task_id=tid)
    expected_status = ("completed" if end is not None else "delivered_service_censored" if drop is not None
                       else "input_blocked" if task["blocks"] else "unserved")
    censored = horizon - ready if ready is not None and drop is None else None
    row_times = {"drop_complete_s": drop, "service_end_s": end, "service_start_s": start,
                 "wait_censored_s": censored, "plan_target_s": task["plan_target_s"]}
    if drop is not None:
        row_times.update(pickup_start_s=pick, released_s=ready, drop_s=drop, lift_s=lift,
                         arrival_s=arrival, drop_start_s=lower)
    if (row["status"] != expected_status
            or any(row[key] != task[key] for key in ("scope", "source_admission", "required_in_ef_scope"))
            or any(not close(row.get(key), value) for key, value in row_times.items())
            or row["crane_id"] != events.get("drop_complete", {}).get("crane_id")
            or task["blocks"] and set(row["reasons"]) != set(task["blocks"])
            or expected_status == "unserved" and not row["reasons"]):
        fail("task_result_vs_event_ledger", task_id=tid)
    return (ready, admission, pick, lift, arrival, lower, drop, start, end, actual_consumed)


def audit_candidates(result, tasks, case, profile, settings, by_task, holds, fail):
    """Recover visible necessary screens; unchosen paths are absent from the archive."""
    hist = {key: Counter() for key in (
        "task_count_histogram", "option_count_histogram", "per_task_crane_count_histogram",
        "first_failed_screen", "no_dispatch_first_failed_screen", "observable_candidate_count",
    )}
    decisions = result["decisions"]
    dispatch = {x["crane_id"] for x in profile["initial_state_layouts"][profile["initial_layout"]]
                if x["dispatch_allowed"]}
    catalog = {item["crane_id"]: item["capacity_t"] for item in profile["crane_catalog"]}
    paths = {tuple(line) for line in profile["authorized_ef_legs"]}
    points = {0., *(e["visible_at_s"] for e in result["events"])}
    decisions_at = defaultdict(list)
    selected = {}
    assigned = {}
    for decision in decisions:
        at = decision["now_s"]
        decisions_at[at].append(decision)
        options = decision["candidates"]
        counts = Counter(opt["task_id"] for opt in options)
        hist["task_count_histogram"][len(counts)] += 1
        hist["option_count_histogram"][len(options)] += 1
        hist["per_task_crane_count_histogram"].update(counts.values())
        best = choose_option(result["strategy"], options)
        choice = decision["chosen_task_id"], decision["chosen_crane_id"]
        if (decision["strategy"] != result["strategy"] or best is None
                or (best["task_id"], best["crane_id"]) != choice):
            fail("policy_rank_or_vehicle_tie", time_s=at, task_id=choice[0])
        if len({(opt["task_id"], opt["crane_id"]) for opt in options}) != len(options):
            fail("duplicate_candidate_option", time_s=at)
        if at < -TIME_TOL or at > case["horizon_s"] + TIME_TOL or choice[0] in selected:
            fail("invalid_or_duplicate_commitment", time_s=at, task_id=choice[0])
        selected[choice[0]] = at
        if best is not None and (best["task_id"], best["crane_id"]) == choice:
            assigned[choice[0]] = best
            for event, field in zip(TRANSPORT, ("pickup_s", "lift_s", "arrival_s", "drop_start_s", "drop_s")):
                seen = by_task.get(choice[0], {}).get(event, {})
                if seen.get("crane_id") != choice[1] or not close(seen.get("time_s"), best[field]):
                    fail("decision_not_executed_as_archived", time_s=at, task_id=choice[0], event_type=event)
        for option in options:
            tid, crane = option["task_id"], option["crane_id"]
            task = tasks.get(tid)
            if not task:
                fail("candidate_not_in_roster", time_s=at, task_id=tid)
                continue
            own = by_task.get(tid, {})
            ready = own.get("physical_release")
            pred = tasks.get(task["predecessor"])
            complete = None
            if pred and pred["service"]:
                complete = by_task.get(pred["id"], {}).get(SERVICES[pred["service"]["kind"]][1], {})
                complete = complete.get("visible_at_s")
            admitted = own.get("source_entry")
            earlier_cast = by_task.get(task["previous_cast"], {}).get("drop_complete", {})
            seated = earlier_cast.get("visible_at_s")
            if (task["kind"] != "ef" or task["blocks"] or ready is None or ready["visible_at_s"] > at + TIME_TOL
                    or not close(option["released_s"], ready["time_s"]) or not close(option["reserved_at_s"], at)
                    or option["pickup_s"] < at - TIME_TOL or task["predecessor"] and
                    (complete is None or complete > at + TIME_TOL)
                    or not task["predecessor"] and
                    (admitted is None or admitted["visible_at_s"] > at + TIME_TOL)
                    or task["previous_cast"] and (seated is None or seated > at + TIME_TOL)
                    or crane not in dispatch or crane not in task["eligible"]
                    or task["mass_upper_t"] is None or task["mass_upper_t"] > catalog.get(crane, -inf)
                    or (task["origin"], task["destination"]) not in paths):
                fail("candidate_invalid_necessary_conditions", time_s=at, task_id=tid, crane_id=crane)
            if (not within(option["pickup_s"], task["pickup_window"])
                    or not within(option["drop_s"], task["delivery_window"])
                    or option["pickup_s"] > option["released_s"] + case["params"]["max_pick_wait_s"] + TIME_TOL
                    or option["drop_s"] > case["horizon_s"] + TIME_TOL
                    or settings["strict"] and task["plan_target_s"] is not None
                    and option["drop_s"] > task["plan_target_s"] + TIME_TOL):
                fail("candidate_window", time_s=at, task_id=tid, crane_id=crane)
    if set(selected) != {tid for tid, events in by_task.items() if "pickup_start" in events}:
        fail("decision_and_transport_populations")

    # Add the ends of archived yield blocks. Scheduler-only queue events
    # (such as blocked cast_start) may be unobservable, deliberately not guessed.
    trajectories = result["trajectories"]
    moving = defaultdict(list)
    for segment in trajectories:
        if segment["mode"] == "yield" and abs(segment["v1"]) <= TIME_TOL:
            points.add(segment["t1_s"])
        if segment["mode"] not in {"idle", "parked"}:
            moving[segment["crane_id"]].append(segment)
    points.update(decisions_at)
    decisions_without_commit = 0
    structural = defaultdict(set)
    for at in sorted(points):
        has_choice = at in decisions_at
        if not has_choice:
            decisions_without_commit += 1
        state = defaultdict(set)
        deferred_actions = {
            (decision["chosen_task_id"], event_type) for decision in decisions_at[at]
            for event_type in TRANSPORT
        }
        for hold in holds:
            start, end = hold["start_s"], hold["end_s"]
            opening = tuple(hold["start_event_id"].rsplit("/", 1))
            closing = tuple(hold["end_event_id"].rsplit("/", 1)) if hold["end_event_id"] else ()
            starts_after_decision = close(start, at) and opening in deferred_actions
            ends_after_decision = close(end, at) and closing in deferred_actions
            if (start <= at + TIME_TOL and not starts_after_decision
                    and (end is None or end > at + TIME_TOL or ends_after_decision)):
                state[hold["resource_id"]].add(hold["ladle_id"])
        for tid, option in assigned.items():
            # Commitment claims occupy their receiver until its drop starts.
            if selected[tid] < at - TIME_TOL and option["drop_start_s"] > at + TIME_TOL:
                state[receiver(tasks[tid])].add(tasks[tid]["ladle"])
            task = tasks[tid]
            if task["service"]["kind"] == "cast":
                start = phase_at(by_task.get(tid, {}), "supply_start")
                end = phase_at(by_task.get(tid, {}), "supply_end")
                if start is not None and start <= at + TIME_TOL and (end is None or end > at + TIME_TOL):
                    state[f"return/{task['destination']}"].add(task["ladle"])
        eligible_tasks = 0
        for tid, task in tasks.items():
            if task["required_in_ef_scope"] is not True or (tid in selected and selected[tid] < at - TIME_TOL):
                continue
            own = by_task.get(tid, {})
            ready = own.get("physical_release")
            pred = tasks.get(task["predecessor"])
            pred_end = end_of(pred, by_task) if pred else None
            previous_cast = phase_at(by_task.get(task["previous_cast"], {}), "drop_complete")
            if task["blocks"]:
                reason = "input_blocked"
            elif task["predecessor"] and (pred_end is None or pred_end > at + TIME_TOL):
                reason = "predecessor_unfinished"
            elif ready is None or ready["visible_at_s"] > at + TIME_TOL:
                reason = "release_not_visible"
            elif not any(task["ladle"] in ladles and resource.rsplit("/", 1)[-1] == task["origin"]
                         for resource, ladles in state.items()):
                reason = "origin_holder_unavailable"
            elif len(state[receiver(task)]) >= case["capacity"][receiver(task)]:
                reason = "destination_occupied_or_claimed"
            elif task["previous_cast"] and (previous_cast is None or previous_cast > at + TIME_TOL):
                reason = "campaign_seat_predecessor"
            else:
                qualified = {cid for cid in task["eligible"] if cid in dispatch
                             and task["mass_upper_t"] is not None and task["mass_upper_t"] <= catalog[cid]
                             and (task["origin"], task["destination"]) in paths}
                busy = {cid for cid in qualified if any(
                    option["crane_id"] == cid and selected[key] < at - TIME_TOL
                    and option["drop_s"] > at + TIME_TOL for key, option in assigned.items()
                ) or any(seg["t0_s"] < at - TIME_TOL and seg["t1_s"] > at + TIME_TOL
                         for seg in moving[cid])}
                latest = task["pickup_window"]["hard_latest_s"]
                pickup_limit = min(ready["time_s"] + case["params"]["max_pick_wait_s"],
                                   latest if latest is not None else inf)
                if not qualified:
                    reason = "no_authorized_qualified_crane"
                elif qualified == busy:
                    reason = "no_observable_idle_crane"
                elif at > pickup_limit + TIME_TOL:
                    reason = "pickup_limit_past"
                else:
                    reason = "requires_unchosen_path_or_reservation_evidence"
                    eligible_tasks += 1
            bucket = "first_failed_screen" if has_choice else "no_dispatch_first_failed_screen"
            hist[bucket][reason] += 1
            structural[tid].add(reason)
        hist["observable_candidate_count"][eligible_tasks] += 1
    return {"successful_dispatches": len(decisions), "observed_checkpoints": len(points),
            "checkpoints_without_logged_dispatch": decisions_without_commit,
            **{key: dict(counts) for key, counts in hist.items()}}, structural


def audit_transport_trajectory(result, tasks, case, profile, timings, fail):
    """Check accepted load identity and exact transport occupancy against event times."""
    by_crane, task_segments = defaultdict(list), defaultdict(list)
    for seg in result["trajectories"]:
        by_crane[seg["crane_id"]].append(seg)
        if (seg["load_state"] != "no_load" and seg["task_id"] is None
                or seg["load_state"] == "no_load" and seg["ladle_id"] is not None):
            fail("trajectory_load_without_matching_identity", time_s=seg["t0_s"])
        if seg["task_id"] is not None:
            task_segments[seg["task_id"]].append(seg)
            if seg["task_id"] not in tasks:
                fail("trajectory_unknown_task", task_id=seg["task_id"])
    positions = profile["station_positions_m"] | profile["interface_positions_m"]
    for tid, task in tasks.items():
        _, _, pick, lift, arrival, lower, drop, _, _, _ = timings[tid]
        segments = task_segments[tid]
        if pick is None:
            if segments:
                fail("trajectory_without_transport", task_id=tid)
            continue
        crane = next((e["crane_id"] for e in result["events"]
                      if e["task_id"] == tid and e["event_type"] == "pickup_start"), None)
        carrying = [s for s in segments if s["load_state"] != "no_load"]
        if any(s["crane_id"] != crane or s["ladle_id"] != task["ladle"] or
               s["load_state"] != task["load_state"] for s in carrying):
            fail("loaded_trajectory_identity", task_id=tid)
        if lift is not None and drop is not None and drop > lift + TIME_TOL:
            carrying.sort(key=lambda item: item["t0_s"])
            if (not carrying or not close(carrying[0]["t0_s"], lift)
                    or not close(carrying[-1]["t1_s"], drop)
                    or any(not close(left["t1_s"], right["t0_s"])
                           for left, right in zip(carrying, carrying[1:]))):
                fail("loaded_trajectory_does_not_cover_hold", task_id=tid)
        for when, station in ((pick, task["origin"]), (arrival, task["destination"])):
            if when is None:
                continue
            path = by_crane[crane]
            segment = next((s for s in path if s["t0_s"] - TIME_TOL <= when <= s["t1_s"] + TIME_TOL), None)
            if segment is None or station not in positions:
                fail("unknown_pick_or_drop_position", task_id=tid)
                continue
            step = when - segment["t0_s"]
            position = segment["x0"] + segment["v0"] * step + segment["a"] * step**2 / 2
            velocity = segment["v0"] + segment["a"] * step
            if not close(position, positions[station]) or not close(velocity, 0.):
                fail("motion_at_pick_or_delivery", task_id=tid, time_s=when)


def recompute_metrics(result, tasks, timings, horizon):
    target_ids = [tid for tid, task in tasks.items() if task["required_in_ef_scope"] is True]
    plan_targets = [tid for tid in target_ids if tasks[tid]["plan_target_s"] is not None
                    and tasks[tid]["plan_target_s"] <= horizon]
    total = len(target_ids)
    completed = sum(timings[tid][8] is not None for tid in target_ids)
    delays = [max(0., timings[tid][6] - tasks[tid]["plan_target_s"]) for tid in plan_targets
              if timings[tid][6] is not None]
    on_time = sum(delay <= PLAN_TOL for delay in delays)
    waits = Counter()
    for timing in timings.values():
        ready, _, pick, _, arrival, lower, drop, start, _, _ = timing
        if drop is not None and ready is not None and pick is not None:
            waits["pickup"] += pick - ready
            waits["drop"] += lower - arrival
            if start is not None:
                waits["seat"] += start - drop
    movement = Counter()
    for seg in result["trajectories"]:
        duration = seg["t1_s"] - seg["t0_s"]
        mode = seg["mode"]
        movement[mode] += duration
        if abs(seg["v0"]) > abs(seg["v1"]) + PLAN_TOL:
            movement["braking"] += duration
        if mode in {"wait_loaded", "wait_path"}:
            waits["path"] += duration
    root_release_without_source = sum(
        timings[tid][0] is not None and timings[tid][1] is None and not tasks[tid]["predecessor"]
        for tid in target_ids
    )
    censored = [horizon - timings[tid][0] for tid in target_ids
                if timings[tid][0] is not None and timings[tid][6] is None]
    return {
        "target_ef": total, "completed_ef": completed, "plan_targets": len(plan_targets),
        "delivered_ef": sum(timings[tid][6] is not None for tid in target_ids), "on_time": on_time,
        "completion_rate": completed / total if total else None,
        "unfinished_rate": (total - completed) / total if total else None,
        "on_time_rate": on_time / len(plan_targets) if plan_targets else None,
        "late_total_s": sum(delays), "late_max_s": max(delays, default=0.),
        "late_count": sum(delay > PLAN_TOL for delay in delays), "unserved_targets": len(plan_targets) - len(delays),
        "censored_wait_count": len(censored), "censored_wait_total_s": sum(censored),
        "empty_run_s": movement["empty_move"] + movement["yield"], "loaded_run_s": movement["loaded_move"],
        "empty_wait_s": movement["wait_empty"], "handling_s": movement["pickup"] + movement["drop"],
        "braking_s": movement["braking"],
        "hot_ladle_window_exposure_s": sum(max(0., min(horizon, times[6] if times[6] is not None else horizon)
                                               - times[0]) for tid, times in timings.items()
                                          if times[0] is not None and tasks[tid]["load_state"] == "loaded"),
        "root_release_without_source": root_release_without_source,
        **{f"wait_{name}_s": waits[name] for name in ("pickup", "path", "drop", "seat")},
    }


def unfinished_evidence(tasks, rows, timings, screens, holds):
    counts = Counter()
    undeclared = 0
    archived_reasons = Counter()
    observable_screen_history = Counter()
    for tid, task in tasks.items():
        if task["required_in_ef_scope"] is not True or timings[tid][8] is not None:
            continue
        own = timings[tid]
        if task["blocks"]:
            counts["input_blocked"] += 1
            undeclared += "undeclared_service_path" in task["blocks"]
        elif own[6] is not None:
            if own[7] is not None:
                counts["service_running_at_horizon"] += 1
            elif any(hold["task_id"] == tid and hold["resource_id"].startswith("seat/")
                     and hold["end_s"] is None for hold in holds):
                counts["seat_retained_without_service_start"] += 1
            else:
                counts["delivered_pending_service_unclassified"] += 1
        elif own[0] is None:
            prev = task["predecessor"]
            key = "predecessor_unfinished_at_horizon" if prev and timings[prev][8] is None else "release_not_in_horizon"
            counts[key] += 1
        elif task["predecessor"] is None and own[1] is None:
            counts["root_source_not_admitted"] += 1
        else:
            counts["unserved_after_release_unresolved"] += 1
        archived_reasons.update(rows[tid]["reasons"])
        observable_screen_history.update(screens[tid])
    return {"classes": dict(counts), "input_blocked_undeclared_path": undeclared,
            "archived_reasons_nonexclusive": dict(archived_reasons),
            "observable_screen_history_nonexclusive": dict(observable_screen_history)}


def audit_events(result, settings, case, sampled_durations):
    """Audit one archived strategy from its case, condition and duration tape."""
    issues = []

    def fail(code, **evidence):
        issues.append({"code": code, **evidence})

    tasks = {value["id"]: value for value in case["tasks"]}
    rows = {value["task_id"]: value for value in result["tasks"]}
    horizon = case["horizon_s"]
    profile = settings["profile"]
    runtime = settings["settings"]
    if (set(rows) != set(tasks) or len(rows) != len(result["tasks"])
            or len(tasks) != len(case["tasks"])):
        fail("task_roster_difference")
        return {"issues": issues, "metrics": {}, "candidates": {}, "unfinished": {}}
    if case.get("initial_occupancy") or case.get("locked_trajectories"):
        fail("initial_state_outside_archive_model")
    witness = "strict_target_conditional" if runtime["strict"] else "lateness_diagnostic_conditional"
    if not close(result["horizon_s"], horizon) or result["witness_mode"] != witness:
        fail("horizon_or_witness_mode")
    by_task, intervals = inspect_event_ledger(result, tasks, horizon, fail)
    check_occupancy(result["occupancy"], intervals, case["capacity"], fail)
    timings = {
        tid: validate_task(task, rows[tid], by_task.get(tid, {}), by_task, tasks, case, profile,
                           runtime, sampled_durations, fail)
        for tid, task in tasks.items()
    }
    audit_transport_trajectory(result, tasks, case, profile, timings, fail)
    candidates, screens = audit_candidates(result, tasks, case, profile, runtime, by_task, intervals, fail)
    computed = recompute_metrics(result, tasks, timings, horizon)
    reported = result["metrics"]
    report_metrics = reported | {f"wait_{name}_s": value for name, value in reported["waiting_s"].items()}
    for key, value in computed.items():
        if key in report_metrics and not close(value, report_metrics[key]):
            fail("metric_mismatch", metric=key, actual=report_metrics[key], expected=value)
    return {
        "issues": issues, "metrics": computed, "candidates": candidates,
        "unfinished": unfinished_evidence(tasks, rows, timings, screens, intervals),
        "service_duration_checks": {
            "completed_verified": sum(timing[9] for timing in timings.values()),
            "started_but_censored": sum(timing[7] is not None and timing[8] is None
                                        for timing in timings.values()),
            "not_started": sum(timing[7] is None for tid, timing in timings.items()
                               if tid in sampled_durations),
        },
    }
