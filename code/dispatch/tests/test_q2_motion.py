"""Tests of Q2 EF kinematics; no production traces."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "code/dispatch"))

from q2_motion import check_gap, hold_segment, plan_move, validate_motion


def motion_profile():
    with (ROOT / "stages/03_implementation/B/q2_parameter_pack.json").open(encoding="utf-8") as file:
        return json.load(file)["motion_profile"]["scenarios"]["base"]


class MotionTests(unittest.TestCase):
    def test_analytical_lf1_cc3(self):
        profile = motion_profile()
        track = plan_move(profile, 248, 312, 0, "loaded", crane_id="3#450")
        self.assertEqual(len(track), 3)
        self.assertAlmostEqual(track[-1]["t1_s"], 182.5)
        self.assertEqual(track[-1]["x1"], 312)
        self.assertEqual((track[0]["v0"], track[-1]["v1"]), (0, 0))

    def test_zone_slowdown_in_both_directions_and_reverse_at_zero(self):
        profile = motion_profile()
        for start, end, boundary in ((280, 390, 336), (390, 280, 368)):
            with self.subTest(start=start):
                track = plan_move(profile, start, end, 0, "loaded", crane_id="3#450")
                entry = next(i for i, span in enumerate(track) if span["x1"] == boundary)
                self.assertLessEqual(abs(track[entry]["v1"]), 0.3 + 1e-8)
                self.assertEqual(track[entry + 1]["gear"], "approach")
                self.assertGreater(abs(track[entry]["a"]), 0)
                for a, b in zip(track, track[1:]):
                    self.assertEqual((a["t1_s"], a["x1"], a["v1"]),
                                     (b["t0_s"], b["x0"], b["v0"]))
                reverse = plan_move(profile, end, start, track[-1]["t1_s"], "loaded",
                                    crane_id="3#450")
                self.assertEqual((track[-1]["v1"], reverse[0]["v0"]), (0, 0))

    def test_analytic_c42_interior_minimum(self):
        # C's 4.2 construction, with equal endpoint gaps of 10 m.
        left = {"t0_s": 0, "t1_s": 2, "x0": 20, "x1": 26, "v0": 4, "v1": 2, "a": -1}
        right = {"t0_s": 0, "t1_s": 2, "x0": 30, "x1": 36, "v0": 0, "v1": 6, "a": 3}
        value = check_gap({"C2": [left], "C3": [right]}, ["C2", "C3"], 9, detailed=True)
        self.assertFalse(value["ok"])
        self.assertEqual(value["min_gap"], 8)
        self.assertEqual(value["time_s"], 1)

    def test_fixed_occupied_crane_is_checked_through_tail(self):
        profile = motion_profile()
        path = plan_move(profile, 20, 44, 1, gear="cruise", crane_id="4#450")
        res = check_gap({"4#450": path, "3#450": []}, ["4#450", "3#450"], 8,
                        initial_positions={"4#450": 20, "3#450": 50})
        self.assertFalse(res["ok"])
        self.assertAlmostEqual(res["min_gap"], 6)
        with self.assertRaises(ValueError):
            plan_move(profile, 0, 5, 0, crane_id="2#150")

    def test_trajectory_validation(self):
        profile = motion_profile()
        order = profile["crane_order"]
        initial = dict(zip(order, (0, 104, 216, 304, 400)))
        track = plan_move(profile, 104, 136, 1, crane_id=order[1], gear="creep")
        paths = {crane: [] for crane in order}
        paths[order[1]] = [hold_segment(0, 1, 104), *track]
        end = track[-1]["t1_s"]
        self.assertTrue(validate_motion(profile, paths, initial_positions=initial,
                                        horizon_s=end)["ok"])
        broken = paths.copy()
        broken[order[1]] = paths[order[1]][:2] + [track[1] | {"v0": 0}] + track[2:]
        with self.assertRaises(ValueError):
            validate_motion(profile, broken, initial_positions=initial, horizon_s=end)


if __name__ == "__main__":
    unittest.main()
