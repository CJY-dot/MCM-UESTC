"""Causal event dispatch for the conditional Q2 EF instances."""

from collections import Counter, defaultdict
from heapq import heappop, heappush
from itertools import count
from math import inf, isfinite

from q2_motion import check_gap, plan_move, validate_motion
from q2_random import forecast_map

EPS = 1e-8
STRATEGIES = ("fcfs", "tight_margin", "quantile_buffer")


def rank_candidate(strategy, option):
    """Pure policy rule. The option contains visible state and forecasts only."""
    released = option["released_s"]
    stable = (released, option["task_id"], option["drop_s"], option["crane_id"])
    if strategy == "fcfs":
        return stable
    margin = option["margin_s"]
    if strategy == "quantile_buffer":
        margin -= option["buffer_s"]
    return (margin, *stable)


def choose_candidate(strategy, options):
    """Rank tasks by their best feasible delivery, with a shared vehicle rule."""
    earliest = {}
    for option in options:
        tid = option["task_id"]
        old = earliest.get(tid)
        if old is None or (option["drop_s"], option["crane_id"]) < (old["drop_s"], old["crane_id"]):
            earliest[tid] = option
    return min(earliest.values(), key=lambda option: rank_candidate(strategy, option))


def stationary(crane, t0, t1, position, mode="idle", task=None, load="no_load"):
    return {"crane_id": crane, "t0_s": t0, "t1_s": t1, "x0": position, "x1": position,
            "v0": 0., "v1": 0., "a": 0., "gear": None, "mode": mode,
            "task_id": task["id"] if task else None,
            "ladle_id": task["ladle"] if task and load != "no_load" else None, "load_state": load}


class Dispatch:
    def __init__(self, case, profile, forecasts, tape, strategy, strict):
        self.case, self.profile, self.forecasts, self.tape = case, profile, forecasts, tape
        self.strategy, self.strict = strategy, strict
        self.horizon = case["horizon_s"]
        self.now = 0.
        self.ids = profile["crane_order"]
        self.stations = profile["station_positions_m"] | profile["interface_positions_m"]
        self.catalog = {c["crane_id"]: c for c in profile["crane_catalog"]}
        layout = profile["initial_state_layouts"][profile["initial_layout"]]
        self.positions = {c["crane_id"]: c["position_m"] for c in layout}
        self.movable = {c["crane_id"] for c in layout if c["dispatch_allowed"]}
        self.gaps = {(p["left_crane"], p["right_crane"]): p["min_distance_m"]
                     for p in profile["clearance"]["adjacent_pairs"]}
        self.paths = {c: [] for c in self.ids}
        self.book = defaultdict(list)
        self.claims = Counter()
        self.holders = {}
        self.queue = []
        self.counter = count()
        self.log, self.decisions, self.occupancy = [], [], []
        self.tasks = {t["id"]: t for t in case["tasks"]}
        self.open = {tid for tid, t in self.tasks.items() if t["kind"] == "ef" and not t["blocks"]}
        self.active, self.unloaded, self.completed = {}, {}, {}
        self.released, self.lifted, self.service_end = {}, {}, {}
        self.work = {}
        self.rejections = defaultdict(Counter)
        self.next_leg = {t["predecessor"]: tid for tid, t in self.tasks.items()
                         if t["predecessor"] and tid in self.open}
        self.deadline = {tid: self.remaining_target(tid) for tid in self.open}
        if case.get("locked_trajectories") or case.get("initial_occupancy"):
            raise ValueError("Q2 evaluator expects frozen empty initial occupancy and unreserved cranes")
        for tid in sorted(self.open):
            t = self.tasks[tid]
            if t["predecessor"] is None and t["rho_s"] is not None:
                self.schedule(max(0., t["rho_s"]), "request", tid)

    def remaining_target(self, tid):
        """Target and additive mean/buffer of predecessor service on its forward path."""
        mean, buffer, cursor = 0., 0., tid
        for _ in self.tasks:
            t = self.tasks[cursor]
            if t["plan_target_s"] is not None:
                return t["plan_target_s"], mean, buffer
            following = self.next_leg.get(cursor)
            if following is None:
                break
            guess, uncertainty = self.forecasts.get(cursor, (t["service"]["duration_s"], 0.))
            next_task = self.tasks[following]
            dist = abs(self.stations[next_task["destination"]] - self.stations[next_task["origin"]])
            limits = self.profile["motion"]["mobile_450t"][next_task["load_state"]]
            # Forecast uses train means and the loaded rest-to-rest template, not the tape.
            if dist:
                route = plan_move(self.profile, self.stations[next_task["origin"]],
                                  self.stations[next_task["destination"]], 0.,
                                  load=next_task["load_state"])
                mean += route[-1]["t1_s"] if route else dist / limits["gear_speed_caps_m_s"]["cruise"]
            mean += guess + self.case["params"]["prep_s"]
            buffer += uncertainty
            cursor = following
        return inf, 0., 0.

    def schedule(self, at, kind, tid):
        heappush(self.queue, (at, next(self.counter), kind, tid))

    def emit(self, kind, tid, **fields):
        task = self.tasks.get(tid)
        self.log.append({"event_type": kind, "event_id": f"{tid}/{kind}", "task_id": tid,
                         "ladle_id": task["ladle"] if task else None,
                         "heat_id": task["heat"] if task else None,
                         "time_s": self.now, "visible_at_s": self.now, **fields})

    def available(self, resource):
        return len(self.book[resource]) + self.claims[resource] < self.case["capacity"][resource]

    def occupy(self, resource, tid, role):
        if not self.available(resource):
            raise RuntimeError(f"Unexpected overlapping resource reservation: {resource}")
        task = self.tasks[tid]
        interval = {"resource_id": resource, "task_id": tid, "ladle_id": task["ladle"],
                    "start_s": self.now, "end_s": None, "role": role,
                    "start_event_id": self.log[-1]["event_id"], "end_event_id": None,
                    "demand": 1, "retained_at_horizon": True}
        self.book[resource].append(interval)
        self.occupancy.append(interval)

    def free(self, resource, ladle):
        matches = [x for x in self.book[resource] if x["ladle_id"] == ladle]
        if len(matches) != 1:
            raise RuntimeError("Unmatched occupancy release")
        row = matches[0]
        row["end_s"], row["retained_at_horizon"] = self.now, False
        row["end_event_id"] = self.log[-1]["event_id"]
        self.book[resource].remove(row)

    def change_holder(self, task, before, after):
        if self.holders.get(task["ladle"]) != before:
            raise RuntimeError("Unexpected ladle holder at handoff")
        self.holders[task["ladle"]] = after

    def process(self, kind, tid):
        t, now = self.tasks[tid], self.now
        ladle, dest = t["ladle"], t["destination"]
        service = t["service"]
        if kind in {"request", "ready"}:
            self.released[tid] = now
            self.emit("physical_release", tid)
            return
        if kind == "start_cast":
            self.work[tid] = "cast_wait"
            return
        if kind == "complete_service":
            self.service_end[tid] = now
            self.work.pop(tid)
            if service["kind"] == "cast":
                source, receiver = f"supply/{dest}", f"return/{dest}"
                self.emit("supply_end", tid, holder_before=source, holder_after=receiver)
                self.change_holder(t, source, receiver)
                self.free(source, ladle)
                self.claims[receiver] -= 1
                self.occupy(receiver, tid, "empty_ladle_position")
                clearance = self.case["transfer_operation_s"]
            else:
                phase = "refine" if service["kind"] == "refine" else "slag"
                self.emit(f"{phase}_end", tid)
                clearance = 0.
            self.completed[tid] = self.unloaded[tid] | {"service_end_s": now}
            following = self.next_leg.get(tid)
            if following:
                self.schedule(now + clearance + self.case["params"]["prep_s"], "ready", following)
            return
        if kind == "start_service":
            self.start_service(tid)
            return
        action = self.active[tid]
        crane, source, receiver = action["crane_id"], action["source"], action["receiver"]
        if kind == "pickup_start":
            self.emit(kind, tid, crane_id=crane)
        elif kind == "pickup_complete":
            self.emit(kind, tid, crane_id=crane, holder_before=source, holder_after=crane)
            self.change_holder(t, source, crane)
            self.free(source, ladle)
            self.lifted[tid] = now
        elif kind == "delivery_arrival":
            self.emit(kind, tid, crane_id=crane)
        elif kind == "drop_start":
            self.emit(kind, tid, crane_id=crane)
            self.claims[receiver] -= 1
            self.occupy(receiver, tid, "seat" if service["kind"] == "cast" else "service_hold")
        elif kind == "drop_complete":
            self.emit(kind, tid, crane_id=crane, holder_before=crane, holder_after=receiver)
            self.change_holder(t, crane, receiver)
            if service["kind"] == "cast":
                self.emit("seat_complete", tid, crane_id=crane)
                prepared = now + self.case["caster_preparation_s"]
                self.schedule(prepared, "prepare_complete", tid)
            else:
                self.schedule(max(now, service["not_before_s"]), "start_service", tid)
            self.unloaded[tid] = action | {"delivered_s": now}
        elif kind == "prepare_complete":
            self.emit(kind, tid)
            self.schedule(max(now, service["not_before_s"]), "start_cast", tid)

    def start_service(self, tid):
        t = self.tasks[tid]
        dest, kind, now = t["destination"], t["service"]["kind"], self.now
        if kind == "cast":
            seat, supply, ret = (f"{z}/{dest}" for z in ("seat", "supply", "return"))
            self.emit("supply_start", tid, holder_before=seat, holder_after=supply)
            self.change_holder(t, seat, supply)
            self.free(seat, t["ladle"])
            self.occupy(supply, tid, "casting")
            self.claims[ret] += 1
        else:
            self.emit("refine_start" if kind == "refine" else "slag_start", tid)
        self.work[tid] = "running"
        self.unloaded[tid]["service_start_s"] = now
        # Only the simulator reads the realized value. No completion timestamp
        # is published until this privately queued event fires.
        duration = self.tape[tid]
        if not isfinite(duration) or duration <= 0:
            raise ValueError("Q2 service duration must be finite and positive")
        self.schedule(now + duration, "complete_service", tid)

    def release_visible(self):
        """Admit root FIFO holders and start service using only committed physical states."""
        for tid in sorted(self.open - self.active.keys()):
            t = self.tasks[tid]
            if t["predecessor"] or tid not in self.released or t["ladle"] in self.holders:
                continue
            previous = t.get("previous_bof")
            resource = f"bof/{t['origin']}"
            if (previous is None or previous in self.lifted) and self.available(resource):
                self.holders[t["ladle"]] = resource
                self.emit("source_entry", tid, holder_after=resource)
                self.occupy(resource, tid, "upstream_receipt")
        for tid in sorted(self.work):
            if self.work[tid] != "cast_wait":
                continue
            t = self.tasks[tid]
            dest = t["destination"]
            earlier = t["previous_cast"]
            if ((not earlier or earlier in self.service_end)
                    and self.available(f"supply/{dest}") and self.available(f"return/{dest}")):
                self.start_service(tid)

    def idle(self, crane):
        path = self.paths[crane]
        return not path or path[-1]["t1_s"] <= self.now + EPS

    def move(self, crane, x, y, at, load, gear, mode, task=None):
        if abs(x - y) <= EPS:
            return []
        return plan_move(self.profile, x, y, at, load, gear, crane_id=crane, mode=mode,
                         task_id=task["id"] if task else None,
                         ladle_id=task["ladle"] if task and load != "no_load" else None)

    def clearance(self, actor, x, y, origin):
        """Build a finite no-passing yield by moving further neighbours first."""
        pivot = self.ids.index(actor)
        segments = {}
        at = self.now
        for side, extreme in ((-1, min(x, y, origin)), (1, max(x, y, origin))):
            target = extreme
            previous = actor
            shifts = []
            for i in range(pivot + side, -1 if side == -1 else len(self.ids), side):
                cid = self.ids[i]
                pair = (cid, previous) if side == -1 else (previous, cid)
                boundary = target + side * self.gaps[pair]
                position = self.paths[cid][-1]["x1"] if self.paths[cid] else self.positions[cid]
                if side * (position - boundary) < -EPS:
                    parking = [p for p in self.profile["parking_positions_m"]
                               if side * (p - boundary) >= -EPS]
                    if cid not in self.movable or not self.idle(cid) or not parking:
                        return None
                    target = min(parking, key=lambda p: abs(p - position))
                    shifts.append((cid, position, target))
                else:
                    target = position
                previous = cid
            for cid, a, b in reversed(shifts):
                route = self.move(cid, a, b, at, "no_load", "creep", "yield")
                if route:
                    segments[cid] = route
                    at = route[-1]["t1_s"]
        return at, segments

    def future_gap(self, additions, *, horizon_s=None):
        suffix = {}
        initial = {}
        for cid, path in self.paths.items():
            # Keep the last completed point for a crane that otherwise has no future segment.
            previous_x = self.positions[cid]
            pending = []
            for segment in reversed(path):
                if segment["t1_s"] <= self.now + EPS:
                    previous_x = segment["x1"]
                    break
                pending.append(segment)
            pending.reverse()
            if pending and pending[0]["t0_s"] < self.now:
                first = pending[0]
                step = self.now - first["t0_s"]
                previous_x = first["x0"] + first["v0"] * step + 0.5 * first["a"] * step ** 2
                pending[0] = first | {"t0_s": self.now, "x0": previous_x,
                                      "v0": first["v0"] + first["a"] * step}
            initial[cid] = previous_x
            suffix[cid] = pending + additions.get(cid, [])
        return check_gap(suffix, self.ids, self.gaps, initial_positions=initial,
                         start_s=self.now, horizon_s=horizon_s)["ok"]

    def parking_after_drop(self, crane, destination, dropped, additions):
        parking = sorted(self.profile["parking_positions_m"],
                         key=lambda point: (abs(point - destination), point))
        for point in parking:
            move = self.move(crane, destination, point, dropped, "no_load", "creep", "yield")
            end = move[-1]["t1_s"] if move else dropped
            if end > self.horizon:
                continue
            proposal = additions | {crane: additions[crane] + move}
            if self.future_gap(proposal):
                return proposal, point, end
        return None

    def candidate(self, t, crane, source, receiver, yield_first):
        now, tid = self.now, t["id"]
        x = self.paths[crane][-1]["x1"] if self.paths[crane] else self.positions[crane]
        origin, dest = self.stations[t["origin"]], self.stations[t["destination"]]
        if yield_first:
            clearance = self.clearance(crane, x, dest, origin)
            if clearance is None or not clearance[1]:
                return None, "parking_unavailable"
            depart, additions = clearance
        else:
            depart, additions = now, {}
        path = self.move(crane, x, origin, depart, "no_load", "cruise", "empty_move", t)
        at_origin = path[-1]["t1_s"] if path else depart
        earliest = t["pickup_window"]["hard_earliest_s"]
        pickup = max(at_origin, earliest if earliest is not None else at_origin)
        latest = t["pickup_window"]["hard_latest_s"]
        if (pickup > self.released[tid] + self.case["params"]["max_pick_wait_s"] + EPS
                or (latest is not None and pickup > latest + EPS)):
            return None, "pickup_window"
        if pickup > at_origin + EPS:
            path.append(stationary(crane, at_origin, pickup, origin, "wait_empty", t))
        lifted = pickup + self.case["pickup_operation_s"]
        if lifted > pickup + EPS:
            path.append(stationary(crane, pickup, lifted, origin, "pickup", t))
        loaded = self.move(crane, origin, dest, lifted, t["load_state"], "cruise", "loaded_move", t)
        path.extend(loaded)
        arrival = loaded[-1]["t1_s"] if loaded else lifted
        drop_duration = self.case["drop_operation_s"]
        earliest = t["delivery_window"]["hard_earliest_s"]
        drop_start = max(arrival, earliest - drop_duration if earliest is not None else arrival)
        if arrival < drop_start - EPS:
            path.append(stationary(crane, arrival, drop_start, dest, "wait_drop", t, t["load_state"]))
        dropped = drop_start + drop_duration
        latest = t["delivery_window"]["hard_latest_s"]
        if latest is not None and dropped > latest + EPS:
            return None, "delivery_hard_window"
        if self.strict and t["plan_target_s"] is not None and dropped > t["plan_target_s"] + EPS:
            return None, "strict_plan_target"
        if dropped > self.horizon:
            return None, "drop_beyond_horizon"
        if dropped > drop_start + EPS:
            path.append(stationary(crane, drop_start, dropped, dest, "drop", t, t["load_state"]))
        additions[crane] = path
        if not self.future_gap(additions, horizon_s=dropped):
            return None, "continuous_gap"
        parking = self.parking_after_drop(crane, dest, dropped, additions)
        if parking is None:
            return None, "post_drop_parking_unavailable"
        additions, parking_position, parking_end = parking
        target, expected_downstream, buffer = self.deadline[tid]
        return {"task_id": tid, "crane_id": crane, "source": source, "receiver": receiver,
                "released_s": self.released[tid], "pickup_s": pickup, "lift_s": lifted,
                "arrival_s": arrival, "drop_start_s": drop_start, "drop_s": dropped,
                "margin_s": target - dropped - expected_downstream, "buffer_s": buffer,
                "reserved_at_s": now, "additions": additions,
                "parking_position_m": parking_position, "parking_end_s": parking_end}, None

    def candidates(self):
        options = []
        for tid in sorted(self.open - self.active.keys()):
            if tid not in self.released:
                continue
            t = self.tasks[tid]
            source = self.holders.get(t["ladle"])
            if not source or source.rsplit("/", 1)[-1] != t["origin"]:
                continue
            if t["predecessor"] and t["predecessor"] not in self.completed:
                continue
            kind = t["service"]["kind"]
            receiver = f"{'seat' if kind == 'cast' else kind}/{t['destination']}"
            if not self.available(receiver):
                self.rejections[tid]["destination_occupied"] += 1
                continue
            earlier = t["previous_cast"]
            if earlier and earlier not in self.unloaded:
                self.rejections[tid]["campaign_precedence"] += 1
                continue
            for cid in t["eligible"]:
                if (cid not in self.movable or not self.idle(cid)
                        or self.catalog[cid]["capacity_t"] < t["mass_upper_t"]):
                    continue
                option, reason = self.candidate(t, cid, source, receiver, False)
                if reason in {"continuous_gap", "post_drop_parking_unavailable"}:
                    retry, secondary_reason = self.candidate(t, cid, source, receiver, True)
                    option = retry
                    if reason != "post_drop_parking_unavailable" or retry is not None:
                        reason = secondary_reason
                if option:
                    options.append(option)
                else:
                    self.rejections[tid][reason] += 1
        return options

    def commit(self, chosen):
        tid = chosen["task_id"]
        for cid, legs in chosen["additions"].items():
            if not legs:
                continue
            old = self.paths[cid]
            if old:
                stop, pos = old[-1]["t1_s"], old[-1]["x1"]
            else:
                stop, pos = 0., self.positions[cid]
            if legs[0]["t0_s"] > stop + EPS:
                old.append(stationary(cid, stop, legs[0]["t0_s"], pos))
            old.extend(legs)
            self.schedule(legs[-1]["t1_s"], "crane_idle", tid)
        action = {k: (None if value == inf else value) for k, value in chosen.items() if k != "additions"}
        self.active[tid] = action
        self.claims[action["receiver"]] += 1
        for key, phase in (("pickup_s", "pickup_start"), ("lift_s", "pickup_complete"),
                           ("arrival_s", "delivery_arrival"), ("drop_start_s", "drop_start"),
                           ("drop_s", "drop_complete")):
            self.schedule(action[key], phase, tid)

    def run(self):
        # Drain equal-time events before constructing any policy snapshot.
        while self.now <= self.horizon:
            while self.queue and self.queue[0][0] <= self.now + EPS:
                _, _, kind, tid = heappop(self.queue)
                if kind != "crane_idle":
                    self.process(kind, tid)
            self.release_visible()
            options = self.candidates()
            if options:
                chosen = choose_candidate(self.strategy, options)
                visible_options = [{k: (None if v == inf else v) for k, v in x.items()
                                    if k not in {"source", "receiver", "additions"}} for x in options]
                self.decisions.append({"now_s": self.now, "strategy": self.strategy,
                                       "chosen_task_id": chosen["task_id"],
                                       "chosen_crane_id": chosen["crane_id"], "candidates": visible_options})
                self.commit(chosen)
                continue
            if not self.queue or self.queue[0][0] > self.horizon:
                break
            self.now = self.queue[0][0]
        return self.result()

    def result(self):
        for cid, path in self.paths.items():
            stop, pos = (path[-1]["t1_s"], path[-1]["x1"]) if path else (0., self.positions[cid])
            if path and cid in self.movable and not any(
                    abs(pos - point) <= EPS for point in self.profile["parking_positions_m"]):
                raise RuntimeError("An accepted Q2 action ends outside registered parking.")
            if stop < self.horizon - EPS:
                path.append(stationary(cid, stop, self.horizon, pos,
                                       "idle" if cid in self.movable else "parked"))
        safety = validate_motion(self.profile, self.paths, initial_positions=self.positions,
                                 horizon_s=self.horizon, minimum_gap=self.gaps)
        rows = []
        waits = Counter()
        for tid, task in self.tasks.items():
            dropped = self.unloaded.get(tid)
            if not dropped:
                status = "input_blocked" if task["blocks"] else "unserved"
                reasons = task["blocks"] or list(self.rejections.get(tid, {})) or ["visible_precedence_or_no_available_crane"]
            elif tid not in self.completed:
                status = "delivered_service_censored"
                reasons = ["service_or_return_pending_at_horizon"]
            else:
                status, reasons = "completed", []
            if dropped:
                waits["pickup"] += dropped["pickup_s"] - dropped["released_s"]
                waits["drop"] += dropped["drop_start_s"] - dropped["arrival_s"]
                if "service_start_s" in dropped:
                    waits["seat"] += dropped["service_start_s"] - dropped["drop_s"]
            censored = (self.horizon - self.released[tid]
                        if tid in self.released and not dropped else None)
            row = {"task_id": tid, "scope": task["scope"], "source_admission": task["source_admission"],
                   "required_in_ef_scope": task["required_in_ef_scope"], "status": status, "reasons": reasons,
                   "wait_censored_s": censored, "plan_target_s": task["plan_target_s"],
                   "crane_id": None, "drop_complete_s": None}
            if dropped:
                row.update(dropped | {"drop_complete_s": dropped["drop_s"],
                                      "pickup_start_s": dropped["pickup_s"]})
            if tid in self.service_end:
                row["service_end_s"] = self.service_end[tid]
            rows.append(row)
        ef = [r for r in rows if r["required_in_ef_scope"] is True]
        completed = sum(r["status"] == "completed" for r in ef)
        targets = [r for r in ef if r["plan_target_s"] is not None and r["plan_target_s"] <= self.horizon]
        delays = [max(0., r["drop_complete_s"] - r["plan_target_s"])
                  for r in targets if r["drop_complete_s"] is not None]
        on_time = sum(value <= EPS for value in delays)
        segments = [seg for path in self.paths.values() for seg in path]
        mode_ticks = Counter()
        brake = 0.
        for seg in segments:
            duration = seg["t1_s"] - seg["t0_s"]
            mode_ticks[seg["mode"]] += duration
            if abs(seg["v0"]) > abs(seg["v1"]) + EPS:
                brake += duration
        return {
            "schema": "mcm-uestc.q2-dispatch-result/1", "strategy": self.strategy,
            "witness_mode": "strict_target_conditional" if self.strict else "lateness_diagnostic_conditional",
            "horizon_s": self.horizon, "unit": "m", "tasks": rows, "events": self.log,
            "trajectories": segments, "occupancy": self.occupancy, "decisions": self.decisions,
            "gap": safety,
            "metrics": {"target_ef": len(ef), "completed_ef": completed, "plan_targets": len(targets),
                        "delivered_ef": sum(r["drop_complete_s"] is not None for r in ef),
                        "on_time": on_time, "completion_rate": completed / len(ef) if ef else None,
                        "on_time_rate": on_time / len(targets) if targets else None,
                        "unfinished_rate": (len(ef) - completed) / len(ef) if ef else None,
                        "late_total_s": sum(delays), "late_max_s": max(delays, default=0.),
                        "late_count": sum(x > EPS for x in delays), "unserved_targets": len(targets) - len(delays),
                        "waiting_s": {key: waits[key] for key in ("pickup", "path", "drop", "seat")},
                        "censored_wait_count": sum(r["wait_censored_s"] is not None for r in ef),
                        "empty_run_s": mode_ticks["empty_move"] + mode_ticks["yield"],
                        "loaded_run_s": mode_ticks["loaded_move"], "empty_wait_s": mode_ticks["wait_empty"],
                        "handling_s": mode_ticks["pickup"] + mode_ticks["drop"], "braking_s": brake,
                        "hot_ladle_window_exposure_s": sum(
                            max(0., min(self.horizon, (self.unloaded.get(tid) or {}).get("drop_s", self.horizon)) - ready)
                            for tid, ready in self.released.items() if self.tasks[tid]["load_state"] == "loaded"),
                        "min_gap": safety["min_gap"], "hard_violations": 0},
        }


def simulate(case, profile_scenario, policy_parameters, tape, strategy, quantile="p90", *,
             strict=False, forecasts=None, override_lf1=None):
    """Run a single fixed sample path with a policy that sees only visible events."""
    if strategy not in STRATEGIES:
        raise ValueError("Unknown Q2 dispatch strategy")
    percentile = int(str(quantile).removeprefix("p"))
    if forecasts is None:
        forecasts = forecast_map(case, policy_parameters, policy_parameters.get("model_selection", {}),
                                 percentile, override_lf1=override_lf1)
    return Dispatch(case, profile_scenario, forecasts, tape, strategy, strict).run()
