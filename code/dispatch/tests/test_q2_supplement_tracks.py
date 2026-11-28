"""Vehicle-time accounting tests using synthetic Q2 archive records."""

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from q2_supplement_tracks import ADDITIVE_FIELDS, vehicle_metrics


def span(crane, start, end, x0, x1, mode, load="no_load", v0=0.0, v1=0.0):
    return {
        "crane_id": crane, "t0_s": start, "t1_s": end, "x0": x0, "x1": x1,
        "mode": mode, "load_state": load, "v0": v0, "v1": v1, "a": (v1 - v0) / (end - start),
    }


def fixture():
    cranes = ("0", "A", "B", "C", "4")
    movers = {"A", "B", "C"}
    horizon = 16
    settings = {"profile": {
        "crane_order": cranes, "initial_layout": "toy",
        "initial_state_layouts": {"toy": [
            {"crane_id": crane, "dispatch_allowed": crane in movers, "track_present": True}
            for crane in cranes
        ]},
        "crane_catalog": [
            {"crane_id": crane, "dispatch_candidate": crane in movers,
             "capacity_t": 450 if crane in movers else 150}
            for crane in cranes
        ],
        "parking_positions_m": [15, 20],
    }}
    trajectory = [
        span("A", 0, 2, 0, 0, "idle"),
        span("A", 2, 3, 0, 3, "empty_move"),
        span("A", 3, 4, 3, 3, "wait_empty"),
        span("A", 4, 5, 3, 3, "pickup"),
        span("A", 5, 6, 3, 5, "loaded_move", "empty_ladle"),
        span("A", 6, 7, 5, 5, "wait_drop", "empty_ladle"),
        span("A", 7, 8, 5, 5, "drop", "empty_ladle"),
        span("A", 8, 9, 5, 10, "yield", v1=10),
        span("A", 9, 10, 10, 15, "yield", v0=10),
        span("A", 10, 13, 15, 15, "idle"),
        span("A", 13, 14, 15, 20, "yield"),
        span("A", 14, 16, 20, 20, "idle"),
    ]
    trajectory.extend(span(crane, 0, horizon, x, x, "idle" if crane in movers else "parked")
                      for crane, x in (("0", -20), ("B", 35), ("C", 60), ("4", 100)))
    result = {
        "unit": "m", "horizon_s": horizon, "trajectories": trajectory, "tasks": [
            {"crane_id": "A", "drop_complete_s": 8, "parking_end_s": 10, "parking_position_m": 15}
        ],
    }
    return {"horizon_s": horizon}, settings, result


class VehicleLedgerTests(unittest.TestCase):
    def test_motion_parking_and_capacity_groups_are_disjoint(self):
        rows = vehicle_metrics(*fixture())
        row = rows["A"]
        self.assertEqual(row["empty_pickup_move_s"], 1)
        self.assertEqual(row["post_drop_parking_move_s"], 2)
        self.assertEqual(row["yield_move_s"], 1)
        self.assertEqual(row["empty_move_s"], 4)
        self.assertEqual(row["legal_parking_hold_s"], 5)
        self.assertEqual(row["initial_idle_s"], 2)
        self.assertEqual(row["standby_s"], 7)
        self.assertEqual(row["braking_s"], 1)
        self.assertEqual(row["empty_wait_s"], 1)
        self.assertEqual(row["loaded_wait_s"], 1)
        self.assertEqual(row["handling_s"], 2)
        self.assertEqual(row["loaded_move_s"], 1)
        self.assertEqual(rows["parked_150t"]["non_dispatch_parked_s"], 32)
        self.assertEqual(rows["dispatch_450t"]["covered_s"], 48)
        for ledger in rows.values():
            self.assertEqual(sum(ledger[k] for k in ADDITIVE_FIELDS), ledger["horizon_vehicle_s"])
            self.assertEqual(ledger["closure_error_s"], 0)

    def test_uncovered_time_and_unclassified_time_are_retained(self):
        case, settings, result = fixture()
        tracks = result["trajectories"]
        tracks[0]["mode"] = "unknown"
        tracks.pop(9)
        row = vehicle_metrics(case, settings, result)["A"]
        self.assertEqual(row["unclassified_s"], 2)
        self.assertEqual(row["uncovered_s"], 3)
        self.assertEqual(row["covered_s"], 13)
        self.assertEqual(row["closure_error_s"], 0)

    def test_overlapping_track_times_rejected(self):
        case, settings, result = fixture()
        result["trajectories"][1]["t0_s"] = 1.5
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            vehicle_metrics(case, settings, result)

    def test_absent_or_incompatible_parking_trace_rejected(self):
        case, settings, result = fixture()
        result["tasks"][0]["parking_position_m"] = 20
        with self.assertRaisesRegex(ValueError, "endpoint differs"):
            vehicle_metrics(case, settings, result)
        result["tasks"][0]["parking_position_m"] = 15
        result["trajectories"][7]["mode"] = "empty_move"
        with self.assertRaisesRegex(ValueError, "not traceable"):
            vehicle_metrics(case, settings, result)


if __name__ == "__main__":
    unittest.main()
