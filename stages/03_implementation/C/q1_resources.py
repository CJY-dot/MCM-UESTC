"""Independent event and resource replay for the closed Q1W06 witness."""

from collections import Counter, defaultdict
from math import isclose, isfinite


TOL = 1e-8


def close(actual, expected):
    if actual is None or expected is None:
        return actual is expected
    return isclose(actual, expected, rel_tol=0, abs_tol=TOL)


def audit_capacity(rows, capacities, horizon):
    """Sweep half-open intervals, leaving open intervals active at the horizon."""
    points = defaultdict(lambda: defaultdict(list))
    errors = []
    for index, row in enumerate(rows):
        resource = row["resource_id"]
        start, stop, demand = row["start_s"], row["end_s"], row["demand"]
        if (resource not in capacities or not isfinite(start) or not 0 <= start <= horizon
                or stop is not None and (not isfinite(stop) or not start <= stop <= horizon)
                or type(demand) is not int or demand <= 0):
            errors.append({"check": "invalid_booking", "resource_id": resource, "time_s": start,
                           "task_id": row.get("task_id")})
            continue
        if stop == start:
            continue
        points[resource][start].append((index, demand))
        if stop is not None:
            points[resource][stop].append((index, -demand))
    peak, terminal = {}, {}
    for resource, capacity in capacities.items():
        active = {}
        peak[resource] = 0
        for time, changes in sorted(points[resource].items()):
            for index, change in changes:
                if change < 0:
                    active.pop(index)
            for index, change in changes:
                if change > 0:
                    active[index] = change
            use = sum(active.values())
            peak[resource] = max(use, peak[resource])
            if use > capacity:
                errors.append({"check": "capacity_exceeded", "resource_id": resource, "time_s": time,
                               "demand": use, "capacity": capacity,
                               "task_ids": [rows[index]["task_id"] for index in active]})
        terminal[resource] = [rows[index]["task_id"] for index in active]
    return {"errors": errors, "peak": peak, "terminal": terminal}


def replay_holders(events):
    """At each instant follow a unique holder-to-holder chain for each ladle."""
    by_ladle = defaultdict(lambda: defaultdict(list))
    for event in events:
        before, after = event["holder_before"], event["holder_after"]
        if before is not None or after is not None:
            by_ladle[event["ladle_id"]][event["time_s"]].append(event)
    errors, final = [], {}
    for ladle, timeline in by_ladle.items():
        holder = "outside_ef_upstream"
        for time, pending in sorted(timeline.items()):
            while pending:
                matches = [event for event in pending if event["holder_before"] == holder
                           and event["holder_after"] is not None]
                if len(matches) != 1:
                    errors.append({"check": "holder_chain", "ladle_id": ladle, "time_s": time,
                                   "holder": holder, "task_ids": [e["task_id"] for e in pending]})
                    break
                event = matches[0]
                holder = event["holder_after"]
                pending.remove(event)
        final[ladle] = holder
    return errors, final


def audit_resources(case, result):
    """Derive event times, occupancy, holder transitions and score from frozen fields."""
    errors = []

    def require(condition, check, task_id=None, **detail):
        if not condition:
            error = {"check": check, **detail}
            if task_id is not None:
                error["task_id"] = task_id
            errors.append(error)

    horizon = case["horizon_s"]
    tasks = {t["id"]: t for t in case["tasks"]}
    rows = {row["task_id"]: row for row in result["tasks"]}
    cranes = {c["id"]: c for c in case["cranes"]}
    ef = {tid for tid, t in tasks.items() if t["scope"] == "ef_crane" and t["scope_status"] == "confirmed"}
    completed = {tid for tid in rows.keys() & tasks.keys() if rows[tid]["status"] == "completed"}
    require(len(tasks) == len(case["tasks"]) and len(rows) == len(result["tasks"]) and rows.keys() == tasks.keys(),
            "task_roster_mismatch", missing=sorted(tasks.keys() - rows.keys()), extra=sorted(rows.keys() - tasks.keys()))
    require(ef == {tid for tid, t in tasks.items() if t["required_in_ef_scope"] is True}, "ef_scope_disagreement")
    require(completed <= ef, "unconfirmed_tasks_completed", task_ids=sorted(completed - ef))
    require(not case["initial_occupancy"], "unsupported_initial_occupancy_for_closed_replay")
    require(result["horizon_s"] == horizon, "horizon_mismatch")

    # The order is derived from the caster/campaign and furnace sequence, never from the emitted times.
    casting_groups = defaultdict(list)
    for tid, t in tasks.items():
        if tid in ef and t["service"] is not None and t["service"]["kind"] == "cast":
            casting_groups[t["destination"], t["campaign"]].append(t)
    for group in casting_groups.values():
        orders = [t["order"] for t in group]
        require(None not in orders and len(orders) == len(set(orders)), "ambiguous_casting_order",
                task_ids=[t["id"] for t in group])
        ordered = sorted(group, key=lambda t: (t["order"] is None, t["order"], t["id"]))
        for prior, current in zip([None, *ordered], ordered):
            require(current["previous_cast"] == (prior["id"] if prior else None), "caster_predecessor_not_in_order",
                    current["id"])

    phases = {}
    event_objects = {}
    for e in result["events"]:
        tid, phase, event_id = e["task_id"], e["event_type"], e["event_id"]
        require((tid, phase) not in phases and event_id not in event_objects, "duplicate_event", tid, event=phase)
        phases[tid, phase] = e
        event_objects[event_id] = e
        require(tid in tasks and 0 <= e["time_s"] <= horizon, "event_outside_roster_or_window", tid,
                event=phase, time_s=e["time_s"])
        if tid in tasks:
            require(e["heat_id"] == tasks[tid]["heat"] and e["ladle_id"] == tasks[tid]["ladle"],
                    "event_identity_mismatch", tid, event=phase, time_s=e["time_s"])
    used = set()

    def event(tid, phase, expected_time=None, *, before=None, after=None, crane=None):
        key = (tid, phase)
        used.add(key)
        e = phases.get(key)
        require(e is not None, "missing_event", tid, event=phase)
        if e is None:
            return None
        t = e["time_s"]
        require(expected_time is None or close(t, expected_time), "event_time_mismatch", tid,
                event=phase, time_s=t, expected_s=expected_time)
        require((e["holder_before"], e["holder_after"], e["crane_id"]) == (before, after, crane),
                "unexpected_handover", tid, event=phase, time_s=t)
        return e["event_id"]

    def timestamp(tid, phase):
        return phases.get((tid, phase), {}).get("time_s")

    def terminal_resource(task):
        service = task["service"]["kind"]
        return f"{'return' if service == 'cast' else service}/{task['destination']}"

    # Only completed, already admitted transports with physically matching lineage can release a delivery hold.
    exits = defaultdict(list)
    for tid, task in tasks.items():
        if tid not in completed:
            continue
        pred = tasks.get(task["predecessor"])
        if pred:
            same = (pred["ladle"] == task["ladle"] and pred["heat"] == task["heat"]
                    and pred["destination"] == task["origin"])
            require(same, "physical_predecessor_identity_or_station", tid, predecessor=pred["id"])
            if same:
                exits[pred["id"]].append(tid)
    for pred, successors in exits.items():
        require(len(successors) == 1, "multiple_releases_from_one_hold", pred, task_ids=successors)

    expected = []
    crane_loads = []

    def book(tid, resource, role, start_event, end_event=None):
        if start_event not in event_objects or end_event is not None and end_event not in event_objects:
            return
        expected.append((tid, resource, role, start_event, end_event))

    cast_offsets, misses = {}, {}
    modeled_casts = {
        t["id"]: {"planned_start_s": t["service"]["not_before_s"], "duration_s": t["service"]["duration_s"],
                  "modeled_start_s": None, "modeled_end_s": None, "offset_s": None, "seat_delay_s": None}
        for group in casting_groups.values() for t in group
    }
    lateness = {}
    source_fields = {
        "heat_id": "heat", "ladle_id": "ladle", "scope": "scope", "scope_status": "scope_status",
        "required_in_process": "required_in_process", "required_in_ef_scope": "required_in_ef_scope",
        "original_admission": "source_admission", "original_blockers": "source_blockers",
        "origin": "origin", "destination": "destination", "predecessor": "predecessor",
        "reuse_predecessor": "reuse_predecessor", "previous_cast": "previous_cast", "previous_bof": "previous_bof",
        "plan_target_s": "plan_target_s", "pickup_window": "pickup_window", "delivery_window": "delivery_window",
    }
    for tid, task in tasks.items():
        row = rows.get(tid)
        if row is None:
            continue
        for witness_field, input_field in source_fields.items():
            require(row.get(witness_field) == task.get(input_field), "immutable_task_field_changed", tid,
                    field=witness_field)
        predecessor = task["predecessor"]
        casting_predecessor = task["previous_cast"]
        bof_predecessor = task["previous_bof"]
        links = [tid for tid in (predecessor, task["reuse_predecessor"], casting_predecessor, bof_predecessor)
                 if tid is not None]
        missing = [link for link in links if link not in completed]
        require(set(row["unfulfilled_dependencies"]) == set(missing), "dependency_record_mismatch", tid)
        phi = task["rho_s"]
        transfer = case["transfer_operation_s"]
        if not predecessor and phi is not None and not missing and not task["blocks"] and phi <= horizon:
            prior_exit = timestamp(bof_predecessor, "pickup_complete") if bof_predecessor else 0
            entry = max(phi, prior_exit) if prior_exit is not None else phi
        else:
            entry = None
        if tid not in completed:
            misses[tid] = {"status": row["status"], "reasons": row["reasons"], "missing": missing}
            require(all(row.get(name) is None for name in ("crane_id", "pickup_start_s", "drop_complete_s",
                                                          "service_start_s", "service_end_s")),
                    "unserved_task_has_executed_actions", tid)
            if task["blocks"]:
                require(row["status"] == "input_blocked" and set(task["blocks"]) <= set(row["reasons"]),
                        "explicit_input_block_lost", tid)
            elif missing:
                require(row["status"] == "dependency_blocked", "dependency_block_lost", tid)
            else:
                require(row["status"] in {"candidate_exhausted", "after_horizon"} and row["reasons"],
                        "unresolved_task_without_diagnosis", tid)
            if entry is not None:
                receipt = event(tid, "source_entry", entry, before="outside_ef_upstream", after=f"bof/{task['origin']}")
                book(tid, f"bof/{task['origin']}", "retained_upstream_receipt", receipt)
            continue

        require(not task["blocks"] and not missing, "blocked_task_executed", tid, missing=missing)
        crane_id = row["crane_id"]
        crane = cranes.get(crane_id)
        require(crane is not None and crane_id in task["eligible"] and crane["dispatch"]
                and 0 < task["mass_upper_t"] <= crane["capacity_t"], "crane_not_qualified", tid, crane_id=crane_id)
        require(task["ladle"] is not None and task["service"] is not None, "missing_service_or_identity", tid)
        stage_fields = ("pickup_start_s", "pickup_complete_s", "delivery_arrival_s", "drop_start_s", "drop_complete_s",
                        "service_start_s", "service_end_s")
        times = [row.get(key) for key in stage_fields]
        if any(v is None for v in times):
            require(False, "missing_completed_task_clock", tid, fields=stage_fields)
            continue
        pick, lift, arrive, unload, drop, start, end = times
        require(0 <= pick <= lift <= arrive <= unload <= drop <= start <= end <= horizon,
                "invalid_action_chronology", tid, times_s=times)
        require(close(lift - pick, case["pickup_operation_s"]) and close(drop - unload, case["drop_operation_s"]),
                "lift_or_drop_duration", tid, time_s=pick)

        ready_from = [] if phi is None else [phi]
        if predecessor and predecessor in completed:
            preceding = tasks[predecessor]["service"]["kind"]
            preceding_finish = timestamp(predecessor, "supply_end" if preceding == "cast" else f"{preceding}_end")
            if preceding_finish is not None:
                ready_from.append(preceding_finish + (transfer if preceding == "cast" else 0) + case["params"]["prep_s"])
        release = max(ready_from) if ready_from else None
        require(release is not None and close(row["release_s"], release) and
                release - TOL <= pick <= release + case["params"]["max_pick_wait_s"] + TOL,
                "release_or_wait_limit", tid, time_s=pick, expected_release_s=release)
        if not predecessor:
            require(entry is not None and pick >= entry - TOL and close(row.get("source_entry_s"), entry),
                    "upstream_entry_or_fifo", tid, time_s=pick, receipt_entry_s=entry)
        for label, action in (("pickup_window", pick), ("delivery_window", drop)):
            early, late = (task[label][f"hard_{key}_s"] for key in ("earliest", "latest"))
            require((early is None or action >= early - TOL) and (late is None or action <= late + TOL),
                    "hard_time_window", tid, field=label, time_s=action)
        target = task["plan_target_s"]
        delay = max(0, drop - target) if target is not None else None
        if target is not None and target <= horizon:
            lateness[tid] = delay
        require(close(row["lateness_s"], delay), "seat_delay_mismatch", tid, time_s=drop, expected_delay_s=delay)
        if result["witness_mode"] == "strict_target_conditional":
            require(delay is None or delay <= TOL, "strict_seat_target_missed", tid, time_s=drop)

        service = task["service"]
        kind, start_plan, duration = service["kind"], service["not_before_s"], service["duration_s"]
        finish_holder = terminal_resource(task)
        destination = f"seat/{task['destination']}" if kind == "cast" else finish_holder
        source = terminal_resource(tasks[predecessor]) if predecessor in completed else f"bof/{task['origin']}"
        require((row["source_holder"], row["delivery_holder"], row["next_holder"]) ==
                (source, destination, finish_holder), "row_holder_mismatch", tid)
        event(tid, "physical_release", release, crane=crane_id)
        event(tid, "pickup_start", pick, crane=crane_id)
        pickup = event(tid, "pickup_complete", lift, before=source, after=crane_id, crane=crane_id)
        event(tid, "delivery_arrival", arrive, crane=crane_id)
        drop_start = event(tid, "drop_start", unload, crane=crane_id)
        event(tid, "drop_complete", drop, before=crane_id, after=destination, crane=crane_id)
        if not predecessor:
            receipt = event(tid, "source_entry", entry, before="outside_ef_upstream", after=source, crane=crane_id)
            book(tid, source, "upstream_receipt", receipt, pickup)
        crane_loads.append({"task_id": tid, "resource_id": crane_id, "start_s": lift, "end_s": drop, "demand": 1})

        require(start >= start_plan - TOL and close(end - start, duration),
                "service_plan_or_duration", tid, time_s=start, duration_s=duration)
        successor_list = exits[tid]
        terminal_exit = phases.get((successor_list[0], "pickup_complete"), {}).get("event_id") if successor_list else None
        if kind == "cast":
            cast_offsets[tid] = start - start_plan
            modeled_casts[tid] = {"planned_start_s": start_plan, "duration_s": duration, "modeled_start_s": start,
                                  "modeled_end_s": end, "offset_s": start - start_plan, "seat_delay_s": delay}
            prior_end = timestamp(casting_predecessor, "supply_end") if casting_predecessor else 0
            preparation = drop + case["caster_preparation_s"]
            require(prior_end is not None and start >= max(start_plan, prior_end, preparation) - TOL,
                    "casting_order_or_preparation", tid, time_s=start, preparation_s=preparation, prior_end_s=prior_end)
            require(close(row.get("prepare_complete_s"), preparation), "preparation_record_mismatch", tid)
            require(close(target, start_plan - case["seat_lead_s"]), "seat_target_basis", tid)
            event(tid, "seat_complete", drop, crane=crane_id)
            event(tid, "prepare_complete", preparation, crane=crane_id)
            supply = f"supply/{task['destination']}"
            start_cast = event(tid, "supply_start", start, before=destination, after=supply, crane=crane_id)
            stop_cast = event(tid, "supply_end", end, before=supply, after=finish_holder, crane=crane_id)
            book(tid, destination, "seat", drop_start, start_cast)
            book(tid, supply, "casting", start_cast, stop_cast)
            book(tid, finish_holder, "empty_ladle_position", stop_cast, terminal_exit)
        else:
            event(tid, f"{kind}_start", start, crane=crane_id)
            event(tid, f"{kind}_end", end, crane=crane_id)
            book(tid, finish_holder, f"{kind}_and_holding", drop_start, terminal_exit)
        require(close(row["holder_ready_s"], end + (transfer if kind == "cast" else 0)),
                "holder_ready_after_process_mismatch", tid, time_s=end)

    for tid, phase in sorted(phases.keys() - used):
        require(False, "unexpected_event", tid, event=phase)
    actual = []
    for b in result["occupancy"]:
        key = (b["task_id"], b["resource_id"], b["role"], b["start_event_id"], b["end_event_id"])
        actual.append(key)
        start = event_objects.get(b["start_event_id"], {}).get("time_s")
        end = event_objects.get(b["end_event_id"], {}).get("time_s")
        require(start is not None and close(start, b["start_s"]) and close(end, b["end_s"])
                and (b["end_event_id"] is None) == (b["end_s"] is None), "occupancy_event_clock_or_release",
                b["task_id"], resource_id=b["resource_id"], time_s=b["start_s"])
        require(b["retained_at_horizon"] == (b["end_s"] is None) and b["demand"] == 1
                and b["ladle_id"] == tasks.get(b["task_id"], {}).get("ladle"),
                "occupancy_identity_or_retention", b["task_id"], resource_id=b["resource_id"], time_s=b["start_s"])
    missing_books = list((Counter(expected) - Counter(actual)).elements())
    extra_books = list((Counter(actual) - Counter(expected)).elements())
    require(not missing_books and not extra_books, "occupancy_does_not_reconcile", missing=missing_books, extra=extra_books)
    cap = audit_capacity(result["occupancy"], case["capacity"], horizon)
    errors.extend(cap["errors"])
    errors.extend(audit_capacity(crane_loads, dict.fromkeys(cranes, 1), horizon)["errors"])
    holder_errors, final = replay_holders(result["events"])
    errors.extend(holder_errors)
    terminal = {b["ladle_id"]: b["resource_id"] for b in result["occupancy"] if b["end_s"] is None}
    require(final == terminal, "terminal_holder_does_not_reconcile", event_holders=final, bookings=terminal)

    # By design a caster plan row represents a seat target, distinct from any hard delivery bound.
    planned_overlap = []
    for tid in ef:
        task = tasks[tid]
        if task["service"] is not None and task["service"]["kind"] == "cast" and task["previous_cast"]:
            prev = tasks[task["previous_cast"]]["service"]
            overlap = prev["not_before_s"] + prev["duration_s"] - task["service"]["not_before_s"]
            if overlap > 0:
                planned_overlap.append(overlap)
    expected_targets = {tid for tid in ef if tasks[tid]["plan_target_s"] is not None
                        and tasks[tid]["plan_target_s"] <= horizon}
    summary = {
        "target_ef": len(ef), "completed_ef": len(completed & ef),
        "plan_targets": len(expected_targets), "on_time": sum(d <= TOL for d in lateness.values()),
        "late_count": sum(d > TOL for d in lateness.values()),
        "late_total_s": sum(d for d in lateness.values() if d > TOL),
        "late_max_s": max(lateness.values(), default=0),
        "unserved_targets": len(expected_targets - completed),
        "shifted_supply_count": sum(offset > TOL for offset in cast_offsets.values()),
    }
    for key, value in summary.items():
        require(close(result["metrics"].get(key), value), "metric_mismatch", metric=key, independent_value=value)
    return {
        **summary, "errors": errors, "completed_task_ids": sorted(completed & ef), "unserved": misses,
        "peak_occupancy": cap["peak"], "terminal_occupancy": cap["terminal"],
        "planned_cast_overlaps_s": sorted(planned_overlap), "casting_start_offsets_s": cast_offsets,
        "modeled_casts": modeled_casts,
        "unserved_cast_ids": [tid for tid in tasks if tid in expected_targets - completed],
    }
