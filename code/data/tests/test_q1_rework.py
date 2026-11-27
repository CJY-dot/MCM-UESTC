"""Rework tests for identity, holder ties, visibility, topology and end-to-end assembly."""

import copy
import json
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "code/data"))

from q1_builder import (  # noqa: E402
    SourceIndex,
    add_derived_events,
    admit,
    attach_resource_requirements,
    attach_task_holders,
    build_events,
    build_holder_chain,
    build_instance,
    build_policy_view,
    build_simulator_truth,
    build_tasks,
    heat_device_rows,
    heat_identity_map,
    resource_catalog,
    station_catalog,
    task_crane_evidence,
    window_candidates,
)
from q1_schema import (  # noqa: E402
    validate_event,
    validate_instance,
    validate_policy_view,
    validate_simulator_truth,
)

FIXTURE = ROOT / "stages/03_implementation/B/fixtures/q1_synthetic_fixture.json"


class IdentityAndHolderTests(unittest.TestCase):
    def test_identity_conflict_does_not_use_majority(self):
        events = pd.DataFrame([
            {"heat_id": "H", "ladle_id": "01", "temporary_ladle_id": "",
             "source_file": "a.xlsx", "source_sheet": "s", "source_row": 2,
             "source_column": "钢包号", "source_column_index": 1,
             "event_status": "canonical", "link_status": "unique"},
            {"heat_id": "H", "ladle_id": "01", "temporary_ladle_id": "",
             "source_file": "a.xlsx", "source_sheet": "s", "source_row": 3,
             "source_column": "钢包号", "source_column_index": 1,
             "event_status": "canonical", "link_status": "unique"},
            {"heat_id": "H", "ladle_id": "02", "temporary_ladle_id": "",
             "source_file": "b.xlsx", "source_sheet": "s", "source_row": 2,
             "source_column": "钢包号", "source_column_index": 1,
             "event_status": "canonical", "link_status": "identity_conflict"},
        ])
        identity = heat_identity_map(events, [])["H"]
        self.assertEqual(identity["status"], "identity_conflict")
        self.assertIsNone(identity["ladle_id"])
        self.assertEqual(identity["canonical_ids"], ["01", "02"])

    def test_approved_identity_is_explicit(self):
        events = pd.DataFrame([
            {"heat_id": "H", "ladle_id": "14", "temporary_ladle_id": "",
             "source_file": "a.xlsx", "source_sheet": "s", "source_row": 2,
             "source_column": "钢包号", "source_column_index": 1,
             "event_status": "canonical", "link_status": "unique"},
        ])
        corrections = [{
            "heat_id": "H",
            "source": {"workbook": "a.xlsx", "sheet": "s", "row": 2, "column": 1},
            "raw_value": "x", "normalized_value": "14", "authority_ref": "test",
        }]
        identity = heat_identity_map(events, corrections)["H"]
        self.assertEqual(identity["status"], "project_approved")
        self.assertEqual(identity["ladle_id"], "14")

    def test_same_time_conflict_is_not_event_id_order(self):
        events = [
            {"event_id": "E1", "event_type": "slag_start", "heat_id": "H",
             "ladle_id": "L", "usable_for_canonical": True, "time_s": 100,
             "resource_id": "SLAG:1#", "source_ref": "a"},
            {"event_id": "E2", "event_type": "transfer_seat", "heat_id": "H",
             "ladle_id": "L", "usable_for_canonical": True, "time_s": 100,
             "resource_id": None, "source_ref": "b"},
        ]
        rows, status = build_holder_chain(events)
        self.assertEqual(status["L"], "ambiguous")
        self.assertTrue(any(row["holder_after"] == "HOLDER:ambiguous" for row in rows))

    def test_same_event_type_known_station_beats_unknown(self):
        events = [
            {"event_id": "E1", "event_type": "slag_start", "heat_id": "H",
             "ladle_id": "L", "usable_for_canonical": True, "time_s": 100,
             "resource_id": "SLAG:1#", "source_ref": "a"},
            {"event_id": "E2", "event_type": "slag_start", "heat_id": "H",
             "ladle_id": "L", "usable_for_canonical": True, "time_s": 100,
             "resource_id": None, "source_ref": "b"},
        ]
        rows, status = build_holder_chain(events)
        self.assertNotEqual(status["L"], "ambiguous")
        self.assertTrue(all(row["holder_after"] == "SLAG:1#" for row in rows))


class DeviceAndTopologyTests(unittest.TestCase):
    def test_lf1_and_lf3_rows_are_not_cross_matched(self):
        events = pd.DataFrame([
            {"heat_id": "H", "source_file": "1#LF.xlsx", "source_sheet": "LF1",
             "source_row": 2, "event_status": "canonical"},
            {"heat_id": "H", "source_file": "3#LF.xlsx", "source_sheet": "LF3",
             "source_row": 2, "event_status": "canonical"},
        ])
        device = heat_device_rows(events)
        self.assertEqual(set(device["H"]), {"LF1", "LF3"})

        class FakeSource(SourceIndex):
            def __init__(self):
                pass

            def crane_labels(self, file, sheet, row):
                return ("3#450", "") if "1#LF" in file else ("5#450", "")

        task_lf1 = {"heat_id": "H", "loaded_state": "loaded", "origin": "BOF_B",
                    "destination": "LF1"}
        task_lf3 = {"heat_id": "H", "loaded_state": "loaded", "origin": "BOF_B",
                    "destination": "LF3"}
        self.assertEqual(
            task_crane_evidence(task_lf1, device, FakeSource())["observed_crane"], "3#450"
        )
        self.assertEqual(
            task_crane_evidence(task_lf3, device, FakeSource())["observed_crane"], "5#450"
        )

    def test_ef_topology_sides_and_external_nodes(self):
        instance = json.loads(FIXTURE.read_text(encoding="utf-8"))
        stations = {item["station_id"]: item for item in station_catalog(instance["tasks"])}
        self.assertEqual(stations["LF1"]["side"], "F")
        self.assertEqual(stations["LF1"]["relative_order"], 4)
        self.assertEqual(stations["CC3"]["side"], "E")
        self.assertEqual(stations["CC3"]["relative_order"], 4)
        self.assertNotIn("BOF_B", stations)
        self.assertNotIn("SLAG:1#", stations)
        resources = {item["resource_id"]: item for item in resource_catalog(instance["tasks"])}
        self.assertFalse(resources["BOF_B"]["in_ef_topology"])
        self.assertTrue(resources["LF1"]["in_ef_topology"])


class VisibilityAndAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_initial_state_hides_future_actuals(self):
        initial = self.instance["initial_state"]
        for state in initial["task_states"]:
            self.assertNotIn("target_end_s", state)
        for operation in initial["active_operations"]:
            self.assertIsNone(operation.get("end_s"))
        for holder in initial["holders"]:
            if holder.get("since_s") is not None:
                self.assertLessEqual(holder["since_s"], initial["now_s"])
        self.assertTrue(self.instance["simulator_truth"]["task_actuals"])

    def test_delayed_announcement_is_allowed(self):
        event = copy.deepcopy(self.instance["events"][0])
        event["visible_at_s"] = event["time_s"] + 100
        self.assertEqual(validate_event(event), [])

    def test_empty_eligible_is_never_a(self):
        by_id = {task["task_id"]: task for task in self.instance["tasks"]}
        task = by_id["HEAT_C:03"]
        self.assertEqual(task["eligible_cranes"], [])
        self.assertEqual(task["admission"], "C")
        self.assertIn("no_eligible_crane", task["blocker_codes"])

    def test_overcapacity_has_empty_capacity_and_eligible(self):
        by_id = {task["task_id"]: task for task in self.instance["tasks"]}
        task = by_id["HEAT_C:03"]
        self.assertEqual(task["capacity_candidates"], [])
        self.assertIn("no_capacity_compatible_crane", task["blocker_codes"])

    def test_known_station_resource_requirements_are_not_empty(self):
        for task in self.instance["tasks"]:
            if task["origin"] or task["destination"]:
                self.assertIsNotNone(task["resource_requirements"])
                self.assertNotEqual(task["resource_requirements"], [])
            else:
                self.assertIsNone(task["resource_requirements"])


class EndToEndSyntheticTests(unittest.TestCase):
    def test_pipeline_from_synthetic_tables(self):
        base = 1000

        def event_row(heat, event, equipment, time_s, file, row, column):
            return {
                "heat_id": heat, "record_type": "actual", "event": event,
                "equipment": equipment, "time_s": str(time_s),
                "source_file": file, "source_sheet": "s", "source_row": str(row),
                "source_column": column, "source_column_index": "1",
                "source_engine": "openpyxl", "tap_mark": "M", "ladle_id": f"L{heat}",
                "temporary_ladle_id": "", "route": "L0", "campaign": "C1",
                "plan_order": "1", "link_status": "unique",
                "event_status": "canonical", "confidence": "single_source",
                "gross_weight_t": "", "weight_source_column": "",
            }

        raw_events = pd.DataFrame([
            event_row("H1", "plan_cast_start", "CC3", base + 800, "plan.xls", 2, "浇铸开始时间"),
            event_row("H1", "plan_cast_end", "CC3", base + 1800, "plan.xls", 2, "浇铸结束时间"),
            event_row("H1", "tap_end", "BOF_B", base - 100, "master.xlsx", 2, "出钢结束时间"),
            event_row("H1", "lf_seat", "LF1", base + 100, "1#LF.xlsx", 2, "钢包号"),
            event_row("H1", "lf_lift", "LF1", base + 200, "1#LF.xlsx", 3, "钢包号"),
            event_row("H1", "cc_seat", "CC3", base + 500, "3#连铸.xlsx", 2, "钢包号"),
            event_row("H1", "cast_start", "CC3", base + 800, "3#连铸.xlsx", 2, "大包开浇时刻"),
            event_row("H1", "cast_end", "CC3", base + 1800, "3#连铸.xlsx", 2, "大包终浇时刻"),
            event_row("H2", "plan_cast_start", "CC3", base + 1900, "plan.xls", 3, "浇铸开始时间"),
            event_row("H2", "plan_cast_end", "CC3", base + 2900, "plan.xls", 3, "浇铸结束时间"),
            event_row("H2", "tap_end", "BOF_B", base + 900, "master.xlsx", 3, "出钢结束时间"),
            event_row("H2", "lf_seat", "LF1", base + 1000, "1#LF.xlsx", 4, "钢包号"),
            event_row("H2", "lf_lift", "LF1", base + 1100, "1#LF.xlsx", 5, "钢包号"),
            event_row("H2", "cc_seat", "CC3", base + 1400, "3#连铸.xlsx", 3, "钢包号"),
            event_row("H2", "cast_start", "CC3", base + 1900, "3#连铸.xlsx", 3, "大包开浇时刻"),
            event_row("H2", "cast_end", "CC3", base + 2900, "3#连铸.xlsx", 3, "大包终浇时刻"),
        ])

        def task_row(task_id, heat, order, leg, origin, destination, predecessor,
                     release, arrival, plan_latest, plan_source, pred_source,
                     lift_source, arrival_source):
            return {
                "task_id": task_id, "heat_id": heat, "campaign": "C1",
                "plan_order": order, "route": "L0", "leg": leg,
                "loaded_state": "loaded", "origin": origin, "destination": destination,
                "predecessor_task_id": predecessor, "scope": "ef_candidate",
                "physical_release_s": "", "release_proxy_s": str(release + 100),
                "process_predecessor_s": str(release), "window_open_s": "",
                "window_close_s": "", "plan_outer_deadline_s": str(plan_latest),
                "observed_lift_s": str(release + 100), "observed_arrival_s": str(arrival),
                "gross_weight_t": "300", "weight_status": "observed_endpoint_proxy",
                "cranes_by_capacity": "3#;4#;5#", "eligible_cranes": "",
                "eligibility_status": "topology_and_window_needed",
                "link_status": "unique", "flags": "",
                "source_plan": plan_source, "source_process_predecessor": pred_source,
                "source_lift": lift_source, "source_arrival": arrival_source,
                "source_weight": "", "source_plan_outer_deadline": plan_source,
            }

        raw_tasks = pd.DataFrame([
            task_row("H1:01", "H1", "1", "BOF_to_LF", "BOF_B", "LF1", "",
                     base - 100, base + 100, base + 800, "plan.xls|s|2|浇铸开始时间",
                     "master.xlsx|s|2|出钢结束时间", "1#LF.xlsx|s|3|钢包号",
                     "1#LF.xlsx|s|2|钢包号"),
            task_row("H1:02", "H1", "1", "LF_to_CC", "LF1", "CC3", "H1:01",
                     base + 200, base + 500, base + 800, "plan.xls|s|2|浇铸开始时间",
                     "1#LF.xlsx|s|3|钢包号", "3#连铸.xlsx|s|2|钢包号",
                     "3#连铸.xlsx|s|2|钢包号"),
            task_row("H2:01", "H2", "2", "BOF_to_LF", "BOF_B", "LF1", "",
                     base + 900, base + 1000, base + 1900, "plan.xls|s|3|浇铸开始时间",
                     "master.xlsx|s|3|出钢结束时间", "1#LF.xlsx|s|5|钢包号",
                     "1#LF.xlsx|s|4|钢包号"),
            task_row("H2:02", "H2", "2", "LF_to_CC", "LF1", "CC3", "H2:01",
                     base + 1100, base + 1400, base + 1900, "plan.xls|s|3|浇铸开始时间",
                     "1#LF.xlsx|s|5|钢包号", "3#连铸.xlsx|s|3|钢包号",
                     "3#连铸.xlsx|s|3|钢包号"),
        ])

        source_to_tasks = {}
        for _, row in raw_tasks.iterrows():
            for key in ("source_plan", "source_process_predecessor", "source_lift",
                        "source_arrival", "source_weight", "source_plan_outer_deadline"):
                if row[key]:
                    source_to_tasks.setdefault(row[key], []).append(row["task_id"])
        events = build_events(raw_events, source_to_tasks)
        identities = heat_identity_map(raw_events, [])
        tasks = build_tasks(raw_tasks, heat_device_rows(raw_events), SourceIndex(None), identities)
        add_derived_events(events, tasks)
        chain, _ = build_holder_chain(events)
        attach_task_holders(tasks, chain)
        attach_resource_requirements(tasks, events)
        admit(tasks)
        candidates = window_candidates(tasks)
        self.assertTrue(
            candidates,
            [(t["task_id"], t["admission"], t["blocker_codes"]) for t in tasks],
        )
        instance = build_instance(
            tasks, events, candidates, None, {}, ["synthetic end-to-end"], []
        )
        self.assertEqual(validate_instance(instance), [])
        self.assertGreaterEqual(len(instance["tasks"]), 2)
        self.assertIn("simulator_truth", instance)


class PolicyIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.policy = build_policy_view(cls.instance)
        cls.truth = build_simulator_truth(cls.instance)

    def test_policy_view_is_valid_and_hidden_truth_is_separate(self):
        self.assertEqual(validate_policy_view(self.policy), [])
        self.assertEqual(validate_simulator_truth(self.truth), [])
        self.assertNotIn("simulator_truth", self.policy)
        self.assertEqual(self.truth["visibility"], "hidden_from_policy")

    def test_policy_view_has_no_future_events(self):
        now_s = self.policy["initial_state"]["now_s"]
        for event in self.policy["events"]:
            self.assertIsNotNone(event["visible_at_s"])
            self.assertLessEqual(event["visible_at_s"], now_s)
        self.assertTrue(self.truth["future_events"])

    def test_policy_view_has_no_actual_task_fields(self):
        for task in self.policy["tasks"]:
            self.assertIsNone(task["historical_pickup_proxy_s"])
            self.assertIsNone(task["observed_arrival_s"])
            self.assertIsNone(task["release_interval"]["hi_s"])
        self.assertTrue(self.truth["task_actuals"])

    def test_policy_view_rejects_simulator_truth(self):
        broken = copy.deepcopy(self.policy)
        broken["simulator_truth"] = self.truth
        self.assertTrue(validate_policy_view(broken))

    def test_policy_view_rejects_future_event(self):
        broken = copy.deepcopy(self.policy)
        future = copy.deepcopy(self.instance["events"][0])
        future["visible_at_s"] = broken["initial_state"]["now_s"] + 100
        broken["events"].append(future)
        self.assertTrue(validate_policy_view(broken))

    def test_required_matches_required_in_process(self):
        for task in self.instance["tasks"]:
            self.assertEqual(task["required"], task["required_in_process"])

    def test_formal_scope_status_values(self):
        statuses = {task["scope_status"] for task in self.instance["tasks"]}
        self.assertTrue(statuses <= {"confirmed", "pending_ef_confirmation", "external_boundary"})


if __name__ == "__main__":
    unittest.main()
