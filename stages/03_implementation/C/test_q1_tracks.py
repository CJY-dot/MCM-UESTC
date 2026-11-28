"""Synthetic controls for the independent Q1 trajectory audit."""

from copy import deepcopy
import unittest

from q1_tracks import audit_counterexample_41, audit_tracks


def segment(crane, start, end, origin, destination, mode="idle", task=None, ladle=None):
    speed = (destination - origin) / (end - start)
    return {
        "crane_id": crane, "t0_s": start, "t1_s": end, "x0": origin, "x1": destination,
        "v0": speed, "v1": speed, "a": 0, "mode": mode, "task_id": task, "ladle_id": ladle,
    }


def fixture(stop):
    cranes = [{"id": f"C{i}", "x": x, "dispatch": i == 2}
              for i, x in enumerate((5, 20, stop, 80, 95), 1)]
    case = {
        "cranes": cranes, "rail": [0, 100], "horizon_s": 12, "parking": [],
        "positions": {"source": 20, "target": 44}, "params": {"speed": 6, "gap": 8},
        "tasks": [{"id": "T", "origin": "source", "destination": "target", "ladle": "L"}],
    }
    row = {
        "task_id": "T", "crane_id": "C2", "status": "completed", "yield_cranes": [],
        "first_action_s": 0, "empty_arrival_s": 0, "pickup_start_s": 0, "pickup_complete_s": 0,
        "delivery_arrival_s": 4, "drop_start_s": 4, "drop_complete_s": 4,
    }
    track = [
        *(segment(c["id"], 0, 12, c["x"], c["x"]) for c in cranes if c["id"] != "C2"),
        segment("C2", 0, 4, 20, 44, mode="loaded_move", task="T", ladle="L"),
        segment("C2", 4, 8, 44, 20, mode="empty_move", task="T"),
        segment("C2", 8, 12, 20, 20),
    ]
    return case, {"trajectories": track, "horizon_s": 12, "gap_threshold": 8, "tasks": [row]}


class TrackAuditTests(unittest.TestCase):
    def test_inner_breakpoint_and_valid_higher_clearance(self):
        blocked = audit_tracks(*fixture(50))
        self.assertEqual(blocked["minimum_gap"], 6)
        self.assertEqual(blocked["minimum_witness"]["time_s"], 4)
        self.assertEqual(blocked["minimum_witness"]["pair"], ["C2", "C3"])
        self.assertEqual({e["rule"] for e in blocked["errors"]}, {"continuous_gap"})
        allowed = audit_tracks(*fixture(60))
        self.assertEqual(allowed["errors"], [])
        self.assertEqual(allowed["minimum_gap"], 15)

    def test_equality_passes_with_zero_margin(self):
        audit = audit_tracks(*fixture(52))
        self.assertEqual(audit["errors"], [])
        self.assertEqual(audit["minimum_gap"], 8)
        self.assertEqual(audit["margin"], 0)

    def test_detects_missing_stationary_crane(self):
        case, result = fixture(60)
        result["trajectories"] = [s for s in result["trajectories"] if s["crane_id"] != "C3"]
        audit = audit_tracks(case, result)
        self.assertIsNone(audit["minimum_gap"])
        self.assertTrue(any(e["rule"] == "incomplete_horizon" and e["crane"] == "C3" for e in audit["errors"]))

    def test_detects_ladle_substitution_and_inconsistent_speed(self):
        case, result = fixture(60)
        moving = next(s for s in result["trajectories"] if s["mode"] == "loaded_move")
        moving["ladle_id"] = "OTHER"
        moving["v1"] = 5
        audit = audit_tracks(case, result)
        rules = {e["rule"] for e in audit["errors"]}
        self.assertIn("missing_or_wrong_task_phase", rules)
        self.assertIn("wrong_loaded_crane_or_route", rules)
        self.assertIn("q1_motion_or_speed_limit", rules)

    def test_detects_forced_hold_violations(self):
        case, result = fixture(60)
        case["locked_trajectories"] = {"C2": [segment("C2", 0, 1, 20, 20)]}
        changed = deepcopy(result)
        c3 = next(s for s in changed["trajectories"] if s["crane_id"] == "C3")
        c3.update(mode="empty_move", x1=72, v0=1, v1=1, task_id="T")
        audit = audit_tracks(case, changed)
        rules = {e["rule"] for e in audit["errors"]}
        self.assertIn("immobilized_crane", rules)
        self.assertIn("changed_locked_motion", rules)

    def test_original_counterexample_distinguishes_controls(self):
        result = audit_counterexample_41()
        self.assertEqual(result["negative"]["furthest_m"], 42)
        self.assertFalse(result["negative"]["reachable"])
        self.assertEqual(result["uniform_control"], {"arrival_s": 10, "minimum_gap_m": 10})
        self.assertEqual(result["acceleration_control"]["arrival_s"], 12)
        self.assertEqual(result["acceleration_control"]["minimum_gap_m"], 10)


if __name__ == "__main__":
    unittest.main()
