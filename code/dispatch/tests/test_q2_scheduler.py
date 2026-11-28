"""Synthetic causal and resource controls for the Q2 scheduler."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "code/dispatch"))

from q2_scheduler import STRATEGIES, choose_candidate, rank_candidate, simulate


def task(tid, origin, destination, service, predecessor=None, *, ladle="L1", blocked=None):
    return {
        "id": tid, "heat": "H1", "ladle": ladle, "origin": origin, "destination": destination,
        "scope": "ef_crane", "kind": "ef", "required_in_ef_scope": True, "source_admission": "B",
        "rho_s": None if predecessor else 0, "predecessor": predecessor, "previous_bof": None,
        "previous_cast": None, "load_state": "loaded", "eligible": ["4#450", "3#450", "5#450"],
        "service": {"kind": service, "duration_s": 100, "not_before_s": 0},
        "plan_target_s": 2500 if service == "cast" else None, "blocks": blocked or [],
        "mass_upper_t": 450,
        "pickup_window": {"hard_earliest_s": None, "hard_latest_s": None},
        "delivery_window": {"hard_earliest_s": None, "hard_latest_s": None},
    }


def fixture():
    pack = json.loads((ROOT / "stages/03_implementation/B/q2_parameter_pack.json").read_text(
        encoding="utf-8"))["motion_profile"]["scenarios"]["base"]
    case = {
        "horizon_s": 3000, "tasks": [
            task("T1", "BOF_B", "LF1", "refine"),
            task("T2", "LF1", "CC3", "cast", "T1"),
        ],
        "params": {"prep_s": 20, "max_pick_wait_s": 1000},
        "capacity": {"bof/BOF_B": 1, "refine/LF1": 1, "seat/CC3": 1, "supply/CC3": 1, "return/CC3": 1},
        "caster_preparation_s": 30, "pickup_operation_s": 0, "drop_operation_s": 0,
        "transfer_operation_s": 0,
    }
    return case, pack


class Q2ScheduleTests(unittest.TestCase):
    def test_slack_never_promotes_a_slower_crane_for_the_same_task(self):
        early = {"released_s": 0, "task_id": "T", "drop_s": 100, "crane_id": "C1",
                 "margin_s": 100, "buffer_s": 15}
        late = early | {"drop_s": 175, "margin_s": 25, "crane_id": "C2"}
        for strategy in STRATEGIES:
            self.assertEqual(choose_candidate(strategy, [late, early]), early)

    def test_event_propagation_only_after_actual_completion_and_preparation(self):
        case, profile = fixture()
        for policy in STRATEGIES:
            result = simulate(case, profile, {}, {"T1": 100., "T2": 150.}, policy,
                              forecasts={"T1": (100, 10), "T2": (100, 10)})
            events = {(e["task_id"], e["event_type"]): e["time_s"] for e in result["events"]}
            self.assertEqual(result["metrics"]["completed_ef"], 2)
            self.assertEqual(events["T1", "refine_end"] - events["T1", "refine_start"], 100)
            self.assertEqual(events["T2", "physical_release"] - events["T1", "refine_end"], 20)
            self.assertEqual(events["T2", "supply_start"] - events["T2", "seat_complete"], 30)
            self.assertEqual(events["T2", "supply_end"] - events["T2", "supply_start"], 150)
            self.assertEqual({s["crane_id"] for s in result["trajectories"]}, set(profile["crane_order"]))
            self.assertTrue(result["gap"]["ok"])
            retained = [x["resource_id"] for x in result["occupancy"] if x["end_s"] is None]
            self.assertEqual(retained, ["return/CC3"])

    def test_future_realizations_do_not_change_first_commitment(self):
        case, profile = fixture()
        for policy in STRATEGIES:
            short = simulate(case, profile, {}, {"T1": 70., "T2": 200.}, policy, forecasts={})
            long = simulate(case, profile, {}, {"T1": 700., "T2": 900.}, policy, forecasts={})
            self.assertEqual(short["decisions"][0], long["decisions"][0])
            self.assertNotEqual(short["events"], long["events"])

    def test_closed_external_handoff_blocks_following_leg(self):
        case, profile = fixture()
        case["tasks"][0]["blocks"] = ["case_boundary_unconfirmed"]
        result = simulate(case, profile, {}, {}, "fcfs", forecasts={})
        self.assertEqual(result["metrics"]["completed_ef"], 0)
        self.assertEqual(result["decisions"], [])
        self.assertEqual([t["status"] for t in result["tasks"]], ["input_blocked", "unserved"])

    def test_plan_target_and_hard_window_are_checked_separately(self):
        case, profile = fixture()
        case["tasks"][1]["plan_target_s"] = 50
        tape = {"T1": 100., "T2": 150.}
        diagnostic = simulate(case, profile, {}, tape, "fcfs", forecasts={})
        strict = simulate(case, profile, {}, tape, "fcfs", strict=True, forecasts={})
        self.assertEqual(diagnostic["metrics"]["late_count"], 1)
        self.assertEqual(diagnostic["metrics"]["completed_ef"], 2)
        self.assertEqual(strict["metrics"]["completed_ef"], 1)
        case["tasks"][1]["delivery_window"]["hard_latest_s"] = 50
        physical = simulate(case, profile, {}, tape, "fcfs", forecasts={})
        self.assertEqual(physical["metrics"]["completed_ef"], 1)

    def test_second_cast_waits_for_occupied_return_slot(self):
        case, profile = fixture()
        third = task("T3", "BOF_B", "LF1", "refine", ladle="L2")
        third["previous_bof"] = "T1"
        fourth = task("T4", "LF1", "CC3", "cast", "T3", ladle="L2")
        fourth["previous_cast"] = "T2"
        case["tasks"].extend((third, fourth))
        result = simulate(case, profile, {}, dict.fromkeys(("T1", "T2", "T3", "T4"), 100.),
                          "fcfs", forecasts={})
        rows = {t["task_id"]: t for t in result["tasks"]}
        self.assertEqual(rows["T2"]["status"], "completed")
        self.assertEqual(rows["T4"]["status"], "delivered_service_censored")
        terminal = {(b["resource_id"], b["ladle_id"]) for b in result["occupancy"]
                    if b["end_s"] is None}
        self.assertEqual(terminal, {("seat/CC3", "L2"), ("return/CC3", "L1")})

    def test_buffer_uses_visible_rank_only(self):
        early = {"released_s": 10, "task_id": "a", "drop_s": 100, "crane_id": "C",
                 "margin_s": 20, "buffer_s": 0}
        late = early | {"released_s": 11, "task_id": "b", "margin_s": 40, "buffer_s": 30}
        self.assertLess(rank_candidate("fcfs", early), rank_candidate("fcfs", late))
        self.assertLess(rank_candidate("tight_margin", early), rank_candidate("tight_margin", late))
        self.assertLess(rank_candidate("quantile_buffer", late), rank_candidate("quantile_buffer", early))


if __name__ == "__main__":
    unittest.main()
