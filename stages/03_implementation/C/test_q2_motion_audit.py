"""Synthetic regression tests for the independent Q2 trajectory checker."""

import unittest

from q2_motion_audit import audit_motion, interval_clearance


def segment(cid, start, end, x0, v0=0.0, acceleration=0.0, mode="idle", gear=None):
    dt = end - start
    return {"crane_id": cid, "t0_s": start, "t1_s": end, "x0": x0,
            "x1": x0 + v0 * dt + acceleration * dt**2 / 2,
            "v0": v0, "v1": v0 + acceleration * dt, "a": acceleration,
            "mode": mode, "gear": gear, "load_state": "no_load",
            "task_id": None, "ladle_id": None}


def fixture():
    ids = ["A", "B", "C", "D", "E"]
    positions = [0.0, 16.0, 34.0, 65.0, 100.0]
    active = {"B", "C", "D"}
    initial = [{"crane_id": cid, "dispatch_allowed": cid in active, "track_present": True,
                "position_m": x, "speed_m_s": 0.0} for cid, x in zip(ids, positions)]
    catalog = [{"crane_id": cid, "dispatch_candidate": cid in active,
                "rail_limits_m": [0.0, 100.0], "capacity_t": 450 if cid in active else 150,
                "motion_profile_group": "mobile_450t" if cid in active else None}
               for cid in ids]
    profile = {
        "crane_order": ids, "crane_catalog": catalog, "initial_layout": "synthetic",
        "initial_state_layouts": {"synthetic": initial}, "rail_limits_m": [0, 100],
        "station_positions_m": {"source": 16.0, "receiver": 20.0},
        "interface_positions_m": {}, "parking_positions_m": [20.0],
        "authorized_ef_legs": [["source", "receiver"]],
        "clearance": {"adjacent_pairs": [
            {"left_crane": left, "right_crane": right, "min_distance_m": 10.0}
            for left, right in zip(ids[:-1], ids[1:])]},
        "motion": {"mobile_450t": {load: {
            "gear_speed_caps_m_s": {"creep": 3, "cruise": 4},
            "acceleration_m_s2": 1, "deceleration_m_s2": 1}
            for load in ("no_load", "loaded")}},
        "speed_zones": [],
    }
    case = {"horizon_s": 10.0, "tasks": []}
    tracks = [
        segment(cid, 0, 10, x, mode="idle" if cid in active else "parked")
        for cid, x in zip(ids, positions) if cid != "B"
    ]
    tracks += [segment("B", 0, 2, 16, 0, 1, "yield", "creep"),
               segment("B", 2, 4, 18, 2, -1, "yield", "creep"),
               segment("B", 4, 10, 20)]
    return {"unit": "m", "horizon_s": 10.0, "tasks": [], "trajectories": tracks}, profile, case


class MotionAuditTests(unittest.TestCase):
    def test_stage02_interior_minimum(self):
        # C Stage 02 §4.2: g(t)=10-4t+2t², endpoints 10 m and interior minimum 8 m.
        left = segment("C2", 0, 2, 20, 4, -1)
        right = segment("C3", 0, 2, 30, 0, 3)
        self.assertEqual(interval_clearance(left, right, 0, 0)[0], 10)
        self.assertEqual(interval_clearance(left, right, 2, 2)[0], 10)
        self.assertEqual(interval_clearance(left, right, 0, 2), (8, 1, 23.5, 31.5))
        self.assertLess(interval_clearance(left, right, 0, 2)[0], 9)

    def test_five_tracks_and_zero_margin(self):
        result, profile, case = fixture()
        profile["clearance"]["adjacent_pairs"][1]["min_distance_m"] = 14.0
        audit = audit_motion(result, {"profile": profile}, case)
        self.assertEqual(audit["issues"], [])
        self.assertTrue(audit["full_track_coverage"])
        self.assertEqual(audit["segment_count"], 7)
        self.assertEqual(audit["min_gap"], 14)
        self.assertEqual(audit["min_margin"], 0)
        self.assertEqual(audit["margin_witness"]["pair"], ["B", "C"])
        self.assertEqual(audit["mode_seconds"]["yield"], 4)
        self.assertEqual(audit["mode_seconds"]["braking"], 2)

    def test_boundary_speed_and_forbidden_gear(self):
        result, profile, case = fixture()
        profile["speed_zones"].append({
            "crane_ids": ["B"], "interval_m": [18, 19], "zone_id": "synthetic",
            "permitted_gears": ["cruise"], "speed_caps_m_s": {"no_load": 0.75},
        })
        issues = audit_motion(result, profile, case)["issues"]
        violations = [row for row in issues if row["code"] == "restricted_zone"]
        self.assertEqual(len(violations), 2)  # the entry endpoint and the adjoining segment
        self.assertEqual(violations[0]["speed_m_s"], 2)

    def test_missing_parked_tail_and_wrong_direction(self):
        result, profile, case = fixture()
        result["trajectories"][0]["t1_s"] = 8
        result["trajectories"][4].update(segment("B", 0, 2, 16, 1, -1, "yield", "creep"))
        audit = audit_motion(result, profile, case)
        codes = {row["code"] for row in audit["issues"]}
        self.assertIn("trajectory_terminal_state", codes)
        self.assertIn("interior_direction_reversal", codes)
        self.assertFalse(audit["full_track_coverage"])

    def test_task_load_and_station_reservation(self):
        result, profile, case = fixture()
        profile["parking_positions_m"] = []
        case["tasks"] = [{"id": "T", "origin": "source", "destination": "receiver", "ladle": "L",
                          "mass_upper_t": 330, "eligible": ["B"], "load_state": "loaded"}]
        result["tasks"] = [{"task_id": "T", "crane_id": "B", "pickup_s": 0, "lift_s": 0,
                            "arrival_s": 4, "drop_s": 4, "drop_complete_s": 4}]
        for row in result["trajectories"][4:6]:
            row.update(mode="loaded_move", gear="cruise", load_state="loaded",
                       task_id="T", ladle_id="L")
        issues = audit_motion(result, profile, case)["issues"]
        self.assertEqual([row["code"] for row in issues], ["station_idle_reservation_absent"])
        result["trajectories"][4]["ladle_id"] = "wrong"
        issues = audit_motion(result, profile, case)["issues"]
        self.assertIn("task_load_state_or_ladle", [row["code"] for row in issues])
        self.assertIn("load_phase_not_preserved", [row["code"] for row in issues])


if __name__ == "__main__":
    unittest.main()
