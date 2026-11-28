"""Regression coverage for C-Q2-01 post-unload parking."""

from collections import defaultdict
from copy import deepcopy
import unittest

from test_q2_scheduler import fixture, task
from q2_motion import validate_motion
from q2_scheduler import simulate


def one_delivery():
    case, profile = fixture()
    case["tasks"] = case["tasks"][:1]
    case["tasks"][0]["eligible"] = ["3#450"]
    case["drop_operation_s"] = 12
    return case, profile


def simulate_fixed(case, profile, durations=None):
    if durations is None:
        durations = {row["id"]: 100.0 for row in case["tasks"]}
    return simulate(case, profile, {}, durations, "fcfs", forecasts={})


def split_tracks(result):
    tracks = defaultdict(list)
    for segment in result["trajectories"]:
        tracks[segment["crane_id"]].append(segment)
    return tracks


def validated_gap(result, profile):
    tracks = split_tracks(result)
    layout = profile["initial_state_layouts"][profile["initial_layout"]]
    initial = {crane["crane_id"]: crane["position_m"] for crane in layout}
    return validate_motion(profile, tracks, initial_positions=initial,
                           horizon_s=result["horizon_s"])


class PostDropParkingTests(unittest.TestCase):
    def assert_registered(self, position, profile):
        self.assertTrue(any(abs(position - dock) < 1e-7
                            for dock in profile["parking_positions_m"]))

    def test_empty_parking_starts_at_drop_completion(self):
        case, profile = one_delivery()
        result = simulate_fixed(case, profile)
        drop = next(e for e in result["events"] if e["event_type"] == "drop_complete")
        track = split_tracks(result)[drop["crane_id"]]
        handling = next(s for s in track if s["mode"] == "drop")
        parking = [s for s in track if s["mode"] == "yield"]
        self.assertTrue(parking)
        self.assertEqual(handling["t1_s"], drop["time_s"])
        self.assertEqual(parking[0]["t0_s"], drop["time_s"])
        self.assertEqual(parking[0]["x0"], handling["x1"])
        self.assertAlmostEqual(parking[0]["v0"], 0)
        self.assertAlmostEqual(parking[-1]["v1"], 0)
        self.assert_registered(parking[-1]["x1"], profile)
        for segment in parking:
            self.assertEqual((segment["gear"], segment["load_state"]), ("creep", "no_load"))
            self.assertIsNone(segment["task_id"])
            self.assertIsNone(segment["ladle_id"])
        self.assertTrue(validated_gap(result, profile)["ok"])

    def test_station_idle_after_service_is_removed(self):
        case, profile = one_delivery()
        result = simulate_fixed(case, profile)
        self.assertEqual(result["metrics"]["completed_ef"], 1)
        track = split_tracks(result)["3#450"]
        last = track[-1]
        drop_time = result["tasks"][0]["drop_complete_s"]
        self.assertEqual(last["mode"], "idle")
        self.assertEqual(last["t1_s"], case["horizon_s"])
        self.assert_registered(last["x0"], profile)
        for segment in track:
            if segment["t0_s"] >= drop_time and segment["mode"] == "idle":
                self.assert_registered(segment["x0"], profile)
                self.assertNotEqual(segment["x0"], profile["station_positions_m"]["LF1"])

    def test_chooses_only_a_registered_parking_point(self):
        case, profile = one_delivery()
        profile["parking_positions_m"] = [264.0, 200.0]
        allowed = deepcopy(profile["parking_positions_m"])
        result = simulate_fixed(case, profile)
        stops = [s["x1"] for s in result["trajectories"]
                 if s["mode"] == "yield" and abs(s["v1"]) < 1e-7]
        self.assertEqual(stops, [264.0])
        self.assertEqual(profile["parking_positions_m"], allowed)

    def test_parking_blocked_by_neighbour_keeps_delivery_unserved(self):
        case, profile = one_delivery()
        feasible = simulate_fixed(case, profile | {"parking_positions_m": [264.0]})
        self.assertEqual(feasible["metrics"]["completed_ef"], 1)
        blocked = simulate_fixed(case, profile | {"parking_positions_m": [376.0]})
        self.assertEqual(blocked["metrics"]["completed_ef"], 0)
        self.assertEqual(blocked["tasks"][0]["status"], "unserved")
        self.assertIn("post_drop_parking_unavailable", blocked["tasks"][0]["reasons"])
        self.assertFalse(any(e["event_type"] == "drop_complete" for e in blocked["events"]))

    def test_next_task_can_start_only_at_committed_parking_endpoint(self):
        case, profile = fixture()
        for row in case["tasks"]:
            row["eligible"] = ["3#450"]
        result = simulate_fixed(case, profile, {"T1": 10.0, "T2": 100.0})
        self.assertEqual(result["metrics"]["completed_ef"], 2)
        track = split_tracks(result)["3#450"]
        later = next(s for s in track if s["task_id"] == "T2")
        first_parking = [s for s in track if s["mode"] == "yield" and s["t1_s"] <= later["t0_s"]]
        self.assertTrue(first_parking)
        last = first_parking[-1]
        ready = next(e["time_s"] for e in result["events"]
                     if e["task_id"] == "T2" and e["event_type"] == "physical_release")
        self.assertLess(ready, last["t1_s"])
        next_dispatch = next(d["now_s"] for d in result["decisions"] if d["chosen_task_id"] == "T2")
        self.assertGreaterEqual(next_dispatch, last["t1_s"])
        self.assertGreaterEqual(later["t0_s"], next_dispatch)
        for segment in track:
            if last["t1_s"] <= segment["t0_s"] < later["t0_s"]:
                self.assertEqual((segment["mode"], segment["x0"], segment["x1"]),
                                 ("idle", last["x1"], last["x1"]))
        self.assertEqual(later["x0"], last["x1"])
        self.assertEqual(later["v0"], last["v1"])
        self.assertTrue(validated_gap(result, profile)["ok"])

    def test_simultaneous_post_drop_parking_preserves_five_crane_gaps(self):
        case, profile = fixture()
        west, east = (task("T1", "BOF_W", "LF_W", "refine", ladle="W"),
                      task("T2", "BOF_E", "LF_E", "refine", ladle="E"))
        west["eligible"], east["eligible"] = ["4#450"], ["5#450"]
        case["tasks"] = [west, east]
        case["capacity"] = dict.fromkeys(("bof/BOF_W", "bof/BOF_E", "refine/LF_W", "refine/LF_E"), 1)
        profile["station_positions_m"] = {"LF_W": 60.0, "LF_E": 180.0}
        profile["interface_positions_m"] = {"BOF_W": 40.0, "BOF_E": 160.0}
        profile["authorized_ef_legs"] = [["BOF_W", "LF_W"], ["BOF_E", "LF_E"]]
        profile["parking_positions_m"] = [65.0, 185.0]
        profile["rail_limits_m"] = [0.0, 220.0]
        profile["speed_zones"] = []
        layout = profile["initial_state_layouts"][profile["initial_layout"]]
        for vehicle, position in zip(layout, (0.0, 40.0, 100.0, 160.0, 220.0)):
            vehicle["position_m"] = position
        for crane in profile["crane_catalog"]:
            crane["rail_limits_m"] = profile["rail_limits_m"]
        result = simulate_fixed(case, profile)
        self.assertEqual(result["metrics"]["completed_ef"], 2)
        tracks = split_tracks(result)
        self.assertEqual(len(tracks), 5)
        a, b = ([s for s in tracks[cid] if s["mode"] == "yield"] for cid in ("4#450", "5#450"))
        self.assertLess(max(a[0]["t0_s"], b[0]["t0_s"]), min(a[-1]["t1_s"], b[-1]["t1_s"]))
        self.assertTrue(validated_gap(result, profile)["ok"])

    def test_parking_plan_is_reproducible_for_a_frozen_duration_tape(self):
        case, profile = fixture()
        tape = {"T1": 97.25, "T2": 154.5}
        first = simulate_fixed(case, profile, tape)
        again = simulate_fixed(case, profile, dict(reversed(tuple(tape.items()))))
        self.assertEqual(first, again)


if __name__ == "__main__":
    unittest.main()
