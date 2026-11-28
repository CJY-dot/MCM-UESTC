"""Synthetic event and competition regression. No production inputs."""

from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest

from q2_event_audit import POLICIES, audit_events, choose_option


ROOT = Path(__file__).resolve().parents[3]


def task(tid, origin, destination, target, predecessor=None, *, ladle):
    return {
        "id": tid, "heat": ladle, "ladle": ladle, "origin": origin, "destination": destination,
        "scope": "ef_crane", "required_in_ef_scope": True, "kind": "ef", "source_admission": "B",
        "predecessor": predecessor, "previous_bof": None, "previous_cast": None,
        "rho_s": None if predecessor else 0., "load_state": "loaded",
        "eligible": ["4#450", "3#450", "5#450"],
        "service": {"kind": "cast" if destination == "CC3" else "refine",
                    "duration_s": 100., "not_before_s": 0.},
        "plan_target_s": target, "blocks": [], "mass_upper_t": 330.,
        "pickup_window": {"hard_earliest_s": None, "hard_latest_s": None},
        "delivery_window": {"hard_earliest_s": None, "hard_latest_s": None},
    }


def synthetic_case():
    profile = json.loads((ROOT / "stages/03_implementation/B/q2_parameter_pack.json")
                         .read_text(encoding="utf-8"))["motion_profile"]["scenarios"]["base"]
    # Add a fictional independent root at LF3 and refinement receiver at RH2.
    profile["authorized_ef_legs"].append(["LF3", "RH2"])
    profile["initial_layout"] = "dispersed"
    case = {
        "horizon_s": 3000., "tasks": [
            task("A", "BOF_B", "LF1", None, ladle="LA"),
            task("B", "LF3", "RH2", 400., ladle="LB"),
            task("AC", "LF1", "CC3", 2000., "A", ladle="LA"),
        ],
        "params": {"prep_s": 20., "max_pick_wait_s": 1500.},
        "capacity": {"bof/BOF_B": 1, "bof/LF3": 1, "refine/LF1": 1, "refine/RH2": 1,
                     "seat/CC3": 1, "supply/CC3": 1, "return/CC3": 1},
        "caster_preparation_s": 30., "pickup_operation_s": 0., "drop_operation_s": 0.,
        "transfer_operation_s": 0.,
    }
    return case, {"settings": {"strict": False}, "profile": profile}


def system_under_test(case, settings, tape, strategy, forecasts=None):
    # A is only the source of test outputs. C's accept/reject logic is independent.
    sys.path.insert(0, str(ROOT / "code/dispatch"))
    from q2_scheduler import simulate
    return simulate(case, settings["profile"], {}, tape, strategy,
                    strict=settings["settings"]["strict"], forecasts=forecasts or {})


class EventAuditTests(unittest.TestCase):
    def setUp(self):
        self.case, self.settings = synthetic_case()
        self.tape = {"A": 100., "B": 110., "AC": 120.}

    def test_full_chain_and_unreleased_return(self):
        result = system_under_test(self.case, self.settings, self.tape, "fcfs")
        checked = audit_events(result, self.settings, self.case, self.tape)
        self.assertEqual(checked["issues"], [])
        self.assertEqual(checked["metrics"]["completed_ef"], 3)
        self.assertEqual(checked["service_duration_checks"]["completed_verified"], 3)
        self.assertEqual([x["resource_id"] for x in result["occupancy"] if x["end_s"] is None],
                         ["refine/RH2", "return/CC3"])

    def test_artificial_competition_and_vehicle_first_ranking(self):
        def option(tid, cid, release, drop, margin, buffer=0):
            return {"task_id": tid, "crane_id": cid, "released_s": release, "drop_s": drop,
                    "margin_s": margin, "buffer_s": buffer}

        early = option("A", "1", 1, 8, 20)
        slow = option("A", "2", 1, 14, 14)
        tighter = option("B", "1", 2, 10, 16)
        buffered = option("C", "1", 3, 9, 30, 15)
        options = [buffered, slow, tighter, early]
        expected = dict(zip(POLICIES, (early, tighter, buffered)))
        for policy, winner in expected.items():
            with self.subTest(policy=policy):
                self.assertEqual(choose_option(policy, options), winner)
        faster_competing_task = option("B", "1", 2, 5, 30)
        self.assertEqual(choose_option("fcfs", [early, faster_competing_task]), early)
        self.assertEqual(choose_option("tight_margin", [early, faster_competing_task]), early)

    def test_two_tasks_simultaneously_pass_archived_candidate_filter(self):
        first = {}
        for strategy in POLICIES:
            result = system_under_test(self.case, self.settings, self.tape, strategy)
            checked = audit_events(result, self.settings, self.case, self.tape)
            self.assertEqual(checked["issues"], [])
            decision = result["decisions"][0]
            self.assertEqual({option["task_id"] for option in decision["candidates"]}, {"A", "B"})
            first[strategy] = decision["chosen_task_id"]
        self.assertEqual(first, {"fcfs": "A", "tight_margin": "B", "quantile_buffer": "B"})

    def test_undelivered_wait_is_censored_not_zero(self):
        blocked = self.case["tasks"][0]
        blocked["blocks"] = ["unconfirmed_external_receipt"]
        result = system_under_test(self.case, self.settings, self.tape, "fcfs")
        checked = audit_events(result, self.settings, self.case, self.tape)
        self.assertEqual(checked["issues"], [])
        self.assertEqual(checked["metrics"]["target_ef"], 3)
        self.assertEqual(checked["metrics"]["completed_ef"], 1)
        self.assertEqual(checked["unfinished"]["classes"],
                         {"input_blocked": 1, "predecessor_unfinished_at_horizon": 1})
        rows = {item["task_id"]: item for item in result["tasks"]}
        self.assertIsNone(rows["AC"]["wait_censored_s"])

    def test_detect_corruption_of_tape_and_retained_capacity(self):
        result = system_under_test(self.case, self.settings, self.tape, "fcfs")
        altered_tape = self.tape | {"A": 105.}
        checked = audit_events(result, self.settings, self.case, altered_tape)
        self.assertIn("consumed_duration_mismatch", {issue["code"] for issue in checked["issues"]})
        bad = deepcopy(result)
        retained = next(x for x in bad["occupancy"] if x["end_s"] is None)
        retained.update(end_s=3000., retained_at_horizon=False)
        checked = audit_events(bad, self.settings, self.case, self.tape)
        self.assertIn("occupancy_event_mismatch", {issue["code"] for issue in checked["issues"]})

    def test_detect_missing_roster_and_forged_policy_decision(self):
        result = system_under_test(self.case, self.settings, self.tape, "fcfs")
        bad = deepcopy(result)
        bad["tasks"].pop()
        checked = audit_events(bad, self.settings, self.case, self.tape)
        self.assertEqual(checked["issues"], [{"code": "task_roster_difference"}])
        result["decisions"][0]["chosen_task_id"] = "B"
        checked = audit_events(result, self.settings, self.case, self.tape)
        self.assertIn("policy_rank_or_vehicle_tie", {issue["code"] for issue in checked["issues"]})


if __name__ == "__main__":
    unittest.main()
