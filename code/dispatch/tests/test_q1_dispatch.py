"""Synthetic regression tests for the Q1 planning adapter and scheduler."""

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "code/dispatch"))

from q1_scenario import CASE_SCHEMA, CONFIG, build_case, online_replay_boundary, project_batch, read_json
from q1_scheduler import add_segment, capacity_conflict, continuous_gaps, solve, verify_trace
from q1_diagnostic import terminal_bound
from q1_builder import build_policy_view

B_FIXTURE = ROOT / "stages/03_implementation/B/fixtures/q1_synthetic_fixture.json"


def task(tid="T1", ladle="L1", origin="BOF:A", destination="LF:A", crane="C2",
         predecessor=None, service="refine", target=None):
    return {
        "id": tid, "heat": f"heat-{ladle}", "ladle": ladle, "campaign": "synthetic",
        "order": 1, "load_state": "loaded", "scope": "ef_crane",
        "scope_status": "confirmed", "required_in_process": True, "required_in_ef_scope": True,
        "source_admission": "B", "source_blockers": [], "kind": "ef",
        "origin": origin, "destination": destination, "predecessor": predecessor,
        "reuse_predecessor": None, "previous_cast": None, "rho_s": None if predecessor else 0,
        "service": {"kind": service, "not_before_s": 0, "duration_s": 2},
        "eligible": [crane], "mass_upper_t": 330, "blocks": [], "plan_target_s": target,
        "pickup_window": {"hard_earliest_s": None, "hard_latest_s": None},
        "delivery_window": {"hard_earliest_s": None, "hard_latest_s": None},
    }


def scenario(tasks, *, middle=50, horizon=90):
    return {
        "schema": CASE_SCHEMA, "information_mode": "batch_planning", "source": "synthetic",
        "plan_version": "test", "scenario": "base", "layout": "test", "unit": "m",
        "horizon_s": horizon, "rail": [0, 100], "positions": {"BOF:A": 20, "LF:A": 60},
        "cranes": [
            {"id": f"C{index}", "x": x, "capacity_t": 450 if index in (2, 3, 4) else 150,
             "dispatch": index == 2}
            for index, x in enumerate((5, 20, middle, 80, 95), 1)
        ],
        "parking": [70], "capacity": {"bof/BOF:A": 1, "refine/LF:A": 1},
        "params": {"gap": 8, "speed": 4, "prep_s": 0, "max_pick_wait_s": horizon},
        "tasks": tasks, "lookahead_s": 0, "seat_lead_s": 0, "caster_preparation_s": 0,
        "pickup_operation_s": 0, "drop_operation_s": 0, "transfer_operation_s": 0,
        "locked_trajectories": {}, "initial_occupancy": [], "fixed_plan_overlap_count": 0,
    }


def paths_for(case):
    return {c["id"]: [] for c in case["cranes"]}


class SafetyTests(unittest.TestCase):
    def test_stationary_crane_blocks_c_fixture_and_positive_control(self):
        order = task()
        order["delivery_window"]["hard_latest_s"] = 15
        blocked = solve(scenario([order]), "fcfs")
        self.assertEqual(blocked["tasks"][0]["status"], "candidate_exhausted")
        self.assertIn("continuous_gap", blocked["tasks"][0]["reasons"])
        stopped = [s for s in blocked["trajectories"] if s["crane_id"] == "C3"]
        self.assertEqual([(s["t0_s"], s["t1_s"], s["x0"], s["x1"]) for s in stopped],
                         [(0, 90, 50, 50)])
        self.assertLess(50 - 8, 60)  # C's fixed no-bypass reachability bound is 42 m.
        positive = solve(scenario([order], middle=70), "fcfs")
        self.assertEqual(positive["tasks"][0]["drop_complete_s"], 10)
        self.assertEqual(positive["gap"]["min_gap"], 10)
        self.assertEqual(40 / 4 + 4 / 2, 12)  # C's separate acceleration-control reference.

    def test_checks_internal_union_of_linear_breakpoints(self):
        case = scenario([])
        paths = paths_for(case)
        add_segment(paths["C2"], "C2", 0, 6, 20, 44, "empty_move")
        add_segment(paths["C2"], "C2", 6, 12, 44, 20, "empty_move")
        gap = continuous_gaps(case, paths, detailed=True)
        self.assertFalse(gap["ok"])
        self.assertEqual(gap["min_gap"], 6)
        self.assertEqual(gap["pair"], ["C2", "C3"])
        self.assertAlmostEqual(gap["first_violation"]["time_s"], 5.5)

    def test_half_open_capacity_with_simultaneous_departure(self):
        def occupancy(start, end):
            return {"resource_id": "seat", "start_s": start, "end_s": end, "demand": 1}

        self.assertIsNone(capacity_conflict({"seat": 1}, [occupancy(0, 5), occupancy(5, 10)], 10))
        # A retained holder keeps consuming capacity until the horizon.
        actual = capacity_conflict({"seat": 1}, [occupancy(0, None), occupancy(5, 10)], 10)
        self.assertEqual((actual["time_s"], actual["demand"]), (5, 2))

    def test_terminal_arrival_cannot_hide_retained_holder(self):
        def occupancy(start, end):
            return {"resource_id": "seat", "start_s": start, "end_s": end, "demand": 1}

        simultaneous = capacity_conflict({"seat": 1}, [occupancy(0, None), occupancy(10, None)], 10)
        self.assertEqual((simultaneous["time_s"], simultaneous["demand"]), (10, 2))
        self.assertIsNone(capacity_conflict({"seat": 1}, [occupancy(0, 10), occupancy(10, None)], 10))

    def test_empty_yield_parking_clears_the_route(self):
        case = scenario([task()])
        case["cranes"][2]["dispatch"] = True
        result = solve(case, "fcfs")
        self.assertEqual(result["tasks"][0]["status"], "completed")
        yields = [s for s in result["trajectories"] if s["mode"] == "yield"]
        self.assertEqual([(s["crane_id"], s["x0"], s["x1"]) for s in yields], [("C3", 50, 70)])
        self.assertGreaterEqual(result["tasks"][0]["pickup_start_s"], yields[0]["t1_s"])
        self.assertGreaterEqual(result["gap"]["min_gap"], 8)

    def test_locked_idle_segment_is_not_used_as_a_yield_move(self):
        case = scenario([task()])
        case["cranes"][2]["dispatch"] = True
        locked = []
        add_segment(locked, "C3", 0, 90, 50, 50, "idle")
        case["locked_trajectories"] = {"C3": locked}
        result = solve(case, "fcfs")
        self.assertEqual(result["tasks"][0]["status"], "candidate_exhausted")
        self.assertEqual([s for s in result["trajectories"] if s["crane_id"] == "C3"], locked)

    def test_declared_eligibility_cannot_override_lifting_capacity(self):
        too_heavy = task() | {"mass_upper_t": 451}
        with self.assertRaises(ValueError):
            solve(scenario([too_heavy], middle=70), "fcfs")


class DispatchTests(unittest.TestCase):
    def test_distinct_cranes_execute_nonconflicting_tasks_in_parallel(self):
        first = task(destination="LF:A")
        second = task("T2", "L2", "BOF:B", "LF:B", "C4")
        case = scenario([first, second])
        case["positions"] |= {"LF:A": 35, "BOF:B": 80, "LF:B": 65}
        case["capacity"] |= {"bof/BOF:B": 1, "refine/LF:B": 1}
        case["cranes"][3]["dispatch"] = True
        result = solve(case, "tight_margin")
        self.assertEqual(result["metrics"]["completed_ef"], 2)
        self.assertEqual([row["pickup_start_s"] for row in result["tasks"]], [0, 0])
        self.assertEqual([row["drop_complete_s"] for row in result["tasks"]], [3.75, 3.75])
        self.assertEqual([row["crane_id"] for row in result["tasks"]], ["C2", "C4"])

    def test_priority_rules_use_the_same_tasks_but_choose_different_first_reservations(self):
        first = task(destination="LF:A")
        second = task("T2", "L2", "BOF:B", "LF:B", target=4)
        case = scenario([first, second], middle=65)
        case["positions"] |= {"LF:A": 35, "BOF:B": 24, "LF:B": 28}
        case["capacity"] |= {"bof/BOF:B": 1, "refine/LF:B": 1}
        results = [solve(case, policy) for policy in ("fcfs", "tight_margin")]
        self.assertEqual([r["decisions"][0]["task_id"] for r in results], ["T1", "T2"])
        self.assertEqual([r["metrics"]["completed_ef"] for r in results], [2, 2])
        self.assertEqual([r["metrics"]["late_count"] for r in results], [1, 0])

    def test_known_release_allows_preposition_without_premature_pickup(self):
        order = task() | {"rho_s": 20}
        case = scenario([order], middle=70)
        case["cranes"][1]["x"] = 25
        case["lookahead_s"] = 10
        result = solve(case, "fcfs")
        row = result["tasks"][0]
        self.assertLess(row["empty_arrival_s"], row["release_s"])
        self.assertEqual(row["pickup_start_s"], row["release_s"])
        self.assertTrue(any(s["mode"] == "wait_empty" for s in result["trajectories"]))

    def test_empty_return_depends_on_cast_end_and_transfers_the_same_holder(self):
        cast = task(destination="CC:A", service="cast")
        empty = task("T2", "L1", "CC:A", "SLAG:A", predecessor="T1", service="slag")
        empty["load_state"] = "empty_ladle"
        case = scenario([cast, empty])
        case["positions"] |= {"CC:A": 40, "SLAG:A": 35}
        case["capacity"] |= {"seat/CC:A": 1, "supply/CC:A": 1, "return/CC:A": 1,
                             "slag/SLAG:A": 1}
        case["params"]["prep_s"] = 3
        result = solve(case, "fcfs")
        hot, returned = result["tasks"]
        self.assertEqual([hot["status"], returned["status"]], ["completed", "completed"])
        self.assertEqual(returned["release_s"], hot["service_end_s"] + 3)
        self.assertEqual(returned["source_holder"], hot["next_holder"])
        self.assertEqual(returned["ladle_id"], hot["ladle_id"])
        self.assertTrue(any(s["mode"] == "loaded_move" and s["x1"] < s["x0"]
                            for s in result["trajectories"]))
        self.assertEqual(result, solve(case, "fcfs"))
        verify_trace(case, result)

    def test_blocked_ef_task_preserves_identity_problem_and_propagates(self):
        blocked = task() | {"blocks": ["unresolved_ladle_identity"], "source_admission": "C"}
        dependent = task("T2", "L1", "LF:A", "LF:B", predecessor="T1")
        case = scenario([blocked, dependent], middle=70)
        case["positions"]["LF:B"] = 40
        case["capacity"]["refine/LF:B"] = 1
        a, b = solve(case, "fcfs")["tasks"]
        self.assertEqual((a["status"], a["original_admission"]), ("input_blocked", "C"))
        self.assertEqual((b["status"], b["unfulfilled_dependencies"]), ("dependency_blocked", ["T1"]))

    def test_unserved_valid_root_retains_bof_receipt_and_prevents_fifo_entry(self):
        first = task(destination="LF:A")
        later = task("T2", "L2", "BOF:A", "LF:B") | {"previous_bof": "T1"}
        case = scenario([first, later])
        case["positions"]["LF:B"] = 35  # Reachable if the second root could enter alone.
        case["capacity"]["refine/LF:B"] = 1
        result = solve(case, "fcfs")
        a, b = result["tasks"]
        self.assertEqual((a["status"], b["status"]), ("candidate_exhausted", "dependency_blocked"))
        self.assertEqual(b["unfulfilled_dependencies"], ["T1"])
        receipt = [r for r in result["occupancy"] if r["resource_id"] == "bof/BOF:A"]
        self.assertEqual([(r["task_id"], r["start_s"], r["end_s"], r["retained_at_horizon"])
                          for r in receipt], [("T1", 0, None, True)])
        self.assertTrue(any(e["event_id"] == receipt[0]["start_event_id"] for e in result["events"]))

    def test_strict_plan_and_diagnostic_late_witness_are_distinct(self):
        case = scenario([task(target=9)], middle=70)
        strict = solve(case, "fcfs", enforce_plan=True)
        diagnostic = solve(case, "fcfs", enforce_plan=False)
        self.assertEqual(strict["metrics"]["completed_ef"], 0)
        self.assertEqual(diagnostic["metrics"]["late_count"], 1)
        self.assertEqual(diagnostic["metrics"]["late_total_s"], 1)
        self.assertEqual(diagnostic["outcome"], "unresolved")

    def test_cast_duration_and_preparation_are_checked_in_returned_trace(self):
        order = task(destination="CC:A", service="cast", target=20)
        case = scenario([order], middle=70)
        case["positions"]["CC:A"] = 60
        case["capacity"].update({"seat/CC:A": 1, "supply/CC:A": 1, "return/CC:A": 1})
        case["caster_preparation_s"] = 3
        witness = solve(case, "fcfs")
        row = witness["tasks"][0]
        self.assertEqual((row["drop_complete_s"], row["service_start_s"], row["service_end_s"]), (10, 13, 15))
        changed = copy.deepcopy(witness)
        changed["tasks"][0]["service_start_s"] -= 1
        with self.assertRaisesRegex(ValueError, "Casting timing"):
            verify_trace(case, changed)

    def test_optional_decision_audit_does_not_change_committed_result(self):
        case = scenario([task()], middle=70)
        journal = []
        self.assertEqual(solve(case, "fcfs"), solve(case, "fcfs", audit=journal))
        first = next(s for s in journal if s["selected"])
        self.assertEqual(first["selected"]["drop_complete_s"], 10)
        self.assertEqual(first["tasks"][0]["attempts"][0]["direct_loaded_travel_s"], 10)

    def test_relaxed_bound_counts_retained_resource_stock(self):
        first = task("T1", "L1", destination="SLAG:A", service="slag")
        second = task("T2", "L2", destination="SLAG:A", service="slag")
        case = scenario([first, second], middle=70)
        case["capacity"]["slag/SLAG:A"] = 1
        proof = terminal_bound(case)
        self.assertEqual(proof["gates"]["external"]["upper"], 2)
        self.assertEqual(proof["gates"]["terminal_stock"]["upper"], 1)
        self.assertEqual(proof["maximum_subsets"], [["T1"], ["T2"]])


class DataBoundaryTests(unittest.TestCase):
    @staticmethod
    def bundle():
        bundle = read_json(B_FIXTURE)
        prototype = next(e for e in bundle["events"] if e["event_category"] == "plan")
        for heat, offset in (("HEAT_A", 0), ("HEAT_B", 1800), ("HEAT_C", 86400)):
            for kind, stamp in (("plan_bof_end", offset), ("plan_refine_slot_1", offset + 120)):
                bundle["events"].append(prototype | {
                    "event_id": f"{heat}/{kind}", "event_type": kind, "heat_id": heat,
                    "time_s": stamp, "visible_at_s": 0, "task_id": None, "task_ids": [],
                    "source_ref": f"synthetic-plan/{heat}/{kind}",
                })
        return bundle

    def test_future_actual_mutations_do_not_change_batch_projection_or_online_boundary(self):
        bundle = self.bundle()
        policy = build_policy_view(bundle)
        mutated = copy.deepcopy(bundle)
        changed = 0
        for event in mutated["events"]:
            if event["event_category"] == "actual" and event["time_s"] > 0:
                event["time_s"] += 500
                event["visible_at_s"] += 500
                changed += 1
        self.assertGreater(changed, 0)
        for row in mutated["tasks"]:
            row["observed_crane"] = "hidden-change"
            row["observed_arrival_s"] = 999999
            row["historical_pickup_proxy_s"] = 999900
        mutated["simulator_truth"]["future_event_ids"] = ["private-change"]
        other_policy = build_policy_view(mutated)
        self.assertEqual(policy, other_policy)
        base = project_batch(bundle, policy)
        self.assertEqual(base, project_batch(mutated, other_policy))
        config = read_json(CONFIG)
        original_run = solve(build_case(base, config, "base"), "fcfs")
        mutated_run = solve(build_case(project_batch(mutated, other_policy), config, "base"), "fcfs")
        self.assertEqual(original_run, mutated_run)
        self.assertEqual(online_replay_boundary(policy), online_replay_boundary(other_policy))
        self.assertFalse(any(k in base["roster"][0] for k in ("mass_t", "observed_crane",
                                                              "historical_pickup_proxy_s")))

    def test_original_scope_admission_and_real_ladle_reuse_are_preserved(self):
        bundle = self.bundle()
        projection = project_batch(bundle, build_policy_view(bundle))
        case = build_case(projection, read_json(CONFIG), "base")
        source = {row["task_id"]: row for row in bundle["tasks"]}
        tasks = {row["id"]: row for row in case["tasks"]}
        for tid, row in tasks.items():
            self.assertEqual(row["source_admission"], source[tid]["admission"])
            self.assertEqual(row["required_in_ef_scope"], source[tid]["required_in_ef_scope"])
            self.assertEqual(row["scope_status"], source[tid]["scope_status"])
        self.assertEqual(tasks["HEAT_A:01"]["ladle"], tasks["HEAT_B:01"]["ladle"])
        self.assertIn("return_to_bof_unverified", tasks["HEAT_B:01"]["blocks"])
        self.assertEqual(tasks["HEAT_A:04"]["kind"], "boundary")
        self.assertEqual(tasks["HEAT_A:05"]["kind"], "boundary")


if __name__ == "__main__":
    unittest.main()
