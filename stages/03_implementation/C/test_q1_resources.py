"""C-owned negative and positive controls for event/occupancy replay."""

import copy
import unittest

from q1_resources import audit_capacity, audit_resources, replay_holders


def interval(task, start, end):
    return {"task_id": task, "resource_id": "slot", "start_s": start, "end_s": end, "demand": 1}


def transfer(before, after, time):
    return {"task_id": "case", "ladle_id": "ladle", "holder_before": before, "holder_after": after,
            "time_s": time}


def event_case():
    task = {"id": "task", "heat": "heat", "ladle": "ladle", "scope": "ef_crane", "scope_status": "confirmed",
            "required_in_process": True, "required_in_ef_scope": True, "source_admission": "B",
            "source_blockers": [], "origin": "BOF", "destination": "CC", "predecessor": None,
            "reuse_predecessor": None, "previous_cast": None, "previous_bof": None, "campaign": "campaign",
            "order": 1, "rho_s": 1, "blocks": [], "eligible": ["k"], "mass_upper_t": 8, "plan_target_s": 6,
            "pickup_window": {"hard_earliest_s": 0, "hard_latest_s": 4},
            "delivery_window": {"hard_earliest_s": 0, "hard_latest_s": 9},
            "service": {"kind": "cast", "not_before_s": 8, "duration_s": 4}}
    case = {"tasks": [task], "initial_occupancy": [], "horizon_s": 12,
            "cranes": [{"id": "k", "capacity_t": 10, "dispatch": True}],
            "params": {"prep_s": 0, "max_pick_wait_s": 10}, "pickup_operation_s": 1,
            "drop_operation_s": 1, "transfer_operation_s": 0, "caster_preparation_s": 2, "seat_lead_s": 2,
            "capacity": dict.fromkeys(("bof/BOF", "seat/CC", "supply/CC", "return/CC"), 1)}
    row = {**task, "task_id": "task", "heat_id": "heat", "ladle_id": "ladle", "crane_id": "k",
           "original_admission": "B", "original_blockers": [], "unfulfilled_dependencies": [],
           "status": "completed", "release_s": 1, "source_entry_s": 1, "pickup_start_s": 2,
           "pickup_complete_s": 3, "delivery_arrival_s": 4, "drop_start_s": 4, "drop_complete_s": 5,
           "service_start_s": 8, "service_end_s": 12, "holder_ready_s": 12, "prepare_complete_s": 7,
           "source_holder": "bof/BOF", "delivery_holder": "seat/CC", "next_holder": "return/CC", "lateness_s": 0}
    events = []
    transfers = {
        "source_entry": ("outside_ef_upstream", "bof/BOF"), "pickup_complete": ("bof/BOF", "k"),
        "drop_complete": ("k", "seat/CC"), "supply_start": ("seat/CC", "supply/CC"),
        "supply_end": ("supply/CC", "return/CC"),
    }
    for phase, time in (
        ("physical_release", 1), ("source_entry", 1), ("pickup_start", 2), ("pickup_complete", 3),
        ("delivery_arrival", 4), ("drop_start", 4), ("drop_complete", 5), ("seat_complete", 5),
        ("prepare_complete", 7), ("supply_start", 8), ("supply_end", 12),
    ):
        before, after = transfers.get(phase, (None, None))
        events.append({"task_id": "task", "heat_id": "heat", "ladle_id": "ladle", "crane_id": "k",
                       "event_id": phase, "event_type": phase, "time_s": time,
                       "holder_before": before, "holder_after": after})
    occupancy = [
        {"task_id": "task", "ladle_id": "ladle", "resource_id": resource, "role": role,
         "start_event_id": start, "end_event_id": end, "start_s": entry, "end_s": exit_time,
         "retained_at_horizon": end is None, "demand": 1}
        for resource, role, start, end, entry, exit_time in (
            ("bof/BOF", "upstream_receipt", "source_entry", "pickup_complete", 1, 3),
            ("seat/CC", "seat", "drop_start", "supply_start", 4, 8),
            ("supply/CC", "casting", "supply_start", "supply_end", 8, 12),
            ("return/CC", "empty_ladle_position", "supply_end", None, 12, None),
        )
    ]
    result = {"tasks": [row], "events": events, "occupancy": occupancy,
              "horizon_s": 12, "witness_mode": "strict_target_conditional",
              "metrics": {"target_ef": 1, "completed_ef": 1, "plan_targets": 1, "on_time": 1,
                          "late_count": 0, "late_total_s": 0, "late_max_s": 0, "unserved_targets": 0,
                          "shifted_supply_count": 0}}
    return case, result


class CapacityTests(unittest.TestCase):
    def test_releases_and_entries_at_same_time_are_half_open(self):
        report = audit_capacity([interval("a", 0, 5), interval("b", 5, None)], {"slot": 1}, 5)
        self.assertFalse(report["errors"])
        self.assertEqual(report["terminal"]["slot"], ["b"])

    def test_new_open_occupation_at_horizon_adds_to_existing_occupancy(self):
        report = audit_capacity([interval("a", 0, None), interval("b", 5, None)], {"slot": 1}, 5)
        self.assertEqual([e["time_s"] for e in report["errors"]], [5])
        self.assertEqual(report["peak"]["slot"], 2)


class HolderTests(unittest.TestCase):
    def test_atomic_sequence_uses_holders_and_allows_shuffled_same_time_events(self):
        sequence = [transfer("BOF", "crane", 1), transfer("crane", "refiner", 1),
                    transfer("outside_ef_upstream", "BOF", 1)]
        self.assertEqual(replay_holders(sequence), ([], {"ladle": "refiner"}))

    def test_two_successors_from_same_holder_violate_uniqueness(self):
        sequence = [transfer("outside_ef_upstream", "BOF", 1), transfer("BOF", "crane1", 2),
                    transfer("BOF", "crane2", 2)]
        self.assertEqual(replay_holders(sequence)[0][0]["check"], "holder_chain")


class EventReplayTests(unittest.TestCase):
    def test_cast_at_horizon_is_counted_as_retained_return(self):
        case, result = event_case()
        report = audit_resources(case, result)
        self.assertFalse(report["errors"])
        self.assertEqual(report["terminal_occupancy"]["return/CC"], ["task"])
        self.assertEqual(report["on_time"], 1)

    def test_shortening_preparation_requirement_in_witness_is_detected(self):
        case, result = event_case()
        case["caster_preparation_s"] = 4
        checks = {e["check"] for e in audit_resources(case, result)["errors"]}
        self.assertIn("casting_order_or_preparation", checks)

    def test_caster_position_must_be_reconstructible_from_events(self):
        case, result = event_case()
        result["occupancy"] = [b for b in result["occupancy"] if b["resource_id"] != "return/CC"]
        checks = {e["check"] for e in audit_resources(case, result)["errors"]}
        self.assertIn("occupancy_does_not_reconcile", checks)
        self.assertIn("terminal_holder_does_not_reconcile", checks)

    def test_delivery_plan_does_not_replace_physical_window(self):
        case, result = event_case()
        charge = {"hard_earliest_s": 0, "hard_latest_s": 4}
        result["tasks"][0]["delivery_window"] = copy.deepcopy(charge)
        case["tasks"][0]["delivery_window"] = charge
        report = audit_resources(case, result)
        self.assertEqual(report["on_time"], 1)
        self.assertIn("hard_time_window", {e["check"] for e in report["errors"]})


if __name__ == "__main__":
    unittest.main()
