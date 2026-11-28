"""Synthetic checks for the Q2 archive-only waiting readout."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from q2_supplement_wait import waiting_rows


def task(tid="t", predecessor=None, origin="BOF_B", destination="LF1",
         service="refine", required=True):
    return {"id": tid, "predecessor": predecessor, "origin": origin, "destination": destination,
            "ladle": "L", "service": {"kind": service}, "required_in_ef_scope": required}


def event(phase, time, tid="t"):
    return {"task_id": tid, "event_type": phase, "time_s": time}


def segment(start, end, mode, *, moving=False):
    return {"t0_s": start, "t1_s": end, "x0": 0, "x1": end - start if moving else 0,
            "v0": int(moving), "v1": int(moving), "a": 0, "mode": mode,
            "task_id": "t", "load_state": "loaded"}


def run(*, tasks=None, events=(), holds=(), segments=(), horizon=100):
    input_case = {"horizon_s": horizon, "tasks": tasks or [task()]}
    output = {"horizon_s": horizon, "events": events, "occupancy": holds, "trajectories": segments}
    return {(r["task_id"], r["stage"]): r for r in waiting_rows(input_case, output)}


class WaitingRowsTests(unittest.TestCase):
    def assert_wait(self, result, tid, stage, status, duration):
        row = result[tid, stage]
        self.assertEqual((row["status"], row["duration_s"]), (status, duration))

    def test_root_entry_delay_and_pickup_are_disjoint(self):
        r = run(events=[event("physical_release", 10), event("source_entry", 15), event("pickup_start", 37)])
        self.assert_wait(r, "t", "release_to_source_entry", "complete", 5)
        self.assert_wait(r, "t", "source_ready_to_pickup", "complete", 22)

    def test_root_without_entry_censors_only_first_started_wait(self):
        r = run(events=[event("physical_release", 10)])
        self.assert_wait(r, "t", "release_to_source_entry", "censored", 90)
        self.assert_wait(r, "t", "source_ready_to_pickup", "missing", None)

    def test_source_entered_but_no_pickup_censors_second_stage(self):
        r = run(events=[event("physical_release", 3), event("source_entry", 7)])
        self.assert_wait(r, "t", "release_to_source_entry", "complete", 4)
        self.assert_wait(r, "t", "source_ready_to_pickup", "censored", 93)

    def test_nonroot_requires_matching_predecessor_hold(self):
        tasks = [task(), task("next", "t", "LF1", "CC3", "cast")]
        events = [event("drop_complete", 26), event("physical_release", 41, "next")]
        hold = {"task_id": "t", "role": "service_hold", "ladle_id": "L", "resource_id": "refine/LF1",
                "start_s": 25, "end_s": None}
        r = run(tasks=tasks, events=events, holds=[hold])
        self.assert_wait(r, "next", "release_to_source_entry", "not_applicable", None)
        self.assert_wait(r, "next", "source_ready_to_pickup", "censored", 59)
        r = run(tasks=tasks, events=events, holds=[hold | {"ladle_id": "other"}])
        self.assert_wait(r, "next", "source_ready_to_pickup", "missing", None)

    def test_movement_and_lowering_do_not_count_as_transit_wait(self):
        events = [event(phase, t) for phase, t in (
            ("pickup_complete", 10), ("delivery_arrival", 25), ("drop_start", 28),
            ("drop_complete", 31), ("refine_start", 40),
        )]
        segments = [segment(10, 15, "loaded_move", moving=True), segment(15, 18, "wait_path"),
                    segment(18, 21, "yield"), segment(21, 25, "loaded_move", moving=True),
                    segment(25, 28, "wait_drop"), segment(28, 31, "drop")]
        r = run(events=events, segments=segments)
        self.assert_wait(r, "t", "loaded_transport_stops", "complete", 6)
        self.assertEqual(r["t", "loaded_transport_stops"]["moving_s"], 9)
        self.assertEqual(r["t", "loaded_transport_stops"]["waiting_to_yield_s"], 3)
        self.assert_wait(r, "t", "arrival_to_drop_start", "complete", 3)
        self.assert_wait(r, "t", "drop_to_service_start", "complete", 9)

    def test_dropped_without_service_is_right_censored(self):
        r = run(events=[event("drop_complete", 92)])
        self.assert_wait(r, "t", "drop_to_service_start", "censored", 8)

    def test_absent_trajectory_is_not_a_zero_stop(self):
        r = run(events=[event("pickup_complete", 10), event("delivery_arrival", 25)])
        self.assert_wait(r, "t", "loaded_transport_stops", "missing", None)

    def test_non_ef_task_has_no_applicable_wait(self):
        r = run(tasks=[task(required=False)])
        self.assertEqual(len(r), 5)
        self.assertEqual({item["status"] for item in r.values()}, {"not_applicable"})

    def test_unknown_binding_on_an_ef_task_is_missing(self):
        r = run(tasks=[task() | {"service": None}])
        self.assert_wait(r, "t", "drop_to_service_start", "missing", None)

    def test_reversed_start_fails(self):
        with self.assertRaises(ValueError):
            run(events=[event("physical_release", 10), event("source_entry", 1)])


if __name__ == "__main__":
    unittest.main()
