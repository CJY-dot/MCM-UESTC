"""Unit tests for the Stage 03 Q1 schema and builder.

Run from the repository root with the project environment:

    python -m unittest discover -s code/data/tests -v

All test data is synthetic.  The tests do not read production workbooks.
"""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "code/data"))

from q1_builder import (  # noqa: E402
    build_instance,
    station,
    task_graph_acyclic,
    window_candidates,
)
from q1_schema import validate_instance  # noqa: E402

FIXTURE = ROOT / "stages/03_implementation/B/fixtures/q1_synthetic_fixture.json"


class SyntheticFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_is_schema_valid(self):
        self.assertEqual(validate_instance(self.instance), [])

    def test_null_and_empty_are_distinct(self):
        by_id = {task["task_id"]: task for task in self.instance["tasks"]}
        self.assertIsNone(by_id["HEAT_A:02"]["eligible_cranes"])
        self.assertEqual(by_id["HEAT_C:03"]["eligible_cranes"], [])
        self.assertIsNone(by_id["HEAT_A:04"]["eligible_cranes"])

    def test_capacity_screening_is_not_eligibility(self):
        by_id = {task["task_id"]: task for task in self.instance["tasks"]}
        task = by_id["HEAT_A:02"]
        self.assertEqual(task["capacity_candidates"], ["3#450", "4#450", "5#450"])
        self.assertIsNone(task["eligible_cranes"])

    def test_physical_release_and_proxy_are_separate(self):
        for task in self.instance["tasks"]:
            self.assertIsNone(task["physical_release_s"])
            if task["historical_pickup_proxy_s"] is not None:
                self.assertGreaterEqual(
                    task["historical_pickup_proxy_s"],
                    task["release_interval"]["lo_s"],
                )

    def test_plan_window_is_not_a_hard_window(self):
        for task in self.instance["tasks"]:
            self.assertIsNone(task["pickup_window"]["hard_earliest_s"])
            self.assertIsNone(task["pickup_window"]["hard_latest_s"])
            self.assertIsNone(task["delivery_window"]["hard_latest_s"])

    def test_external_service_boundary(self):
        external = [task for task in self.instance["tasks"] if task["scope"] == "external_service"]
        self.assertTrue(external)
        self.assertTrue(all(not task["required"] for task in external))

    def test_task_graph_is_acyclic(self):
        acyclic, missing = task_graph_acyclic(self.instance["tasks"])
        self.assertTrue(acyclic)
        self.assertEqual(missing, 0)

    def test_cross_heat_ladle_reuse(self):
        by_heat = {}
        for task in self.instance["tasks"]:
            by_heat.setdefault(task["heat_id"], set()).add(task["ladle_id"])
        self.assertIn("LADLE_01", by_heat["HEAT_A"])
        self.assertIn("LADLE_01", by_heat["HEAT_B"])
        holders = {item["ladle_id"] for item in self.instance["initial_state"]["holders"]}
        self.assertIn("LADLE_01", holders)
        self.assertIn("LADLE_03", holders)

    def test_cross_midnight_time_axis(self):
        heat_c = [event for event in self.instance["events"] if event["heat_id"] == "HEAT_C"]
        times = [event["time_s"] for event in heat_c if event["time_s"] is not None]
        self.assertLess(min(times), 86_400)
        self.assertGreater(max(times), 86_400)
        self.assertGreater(max(times) - min(times), 0)

    def test_c_tasks_have_blockers(self):
        for task in self.instance["tasks"]:
            if task["admission"] == "C":
                self.assertTrue(task["blocker_codes"], task["task_id"])
            if task["admission"] == "A":
                self.assertIsNotNone(task["eligible_cranes"], task["task_id"])

    def test_external_and_unknown_resource(self):
        resources = {item["resource_id"]: item for item in self.instance["resources"]}
        self.assertIsNone(resources["TRANSFER:unknown"]["capacity"])
        self.assertEqual(resources["TRANSFER:unknown"]["capacity_status"], "unknown")
        scopes = {task["scope"] for task in self.instance["tasks"]}
        self.assertIn("pending_ef_scope", scopes)


class BuilderHelperTests(unittest.TestCase):
    def test_station_canonicalization(self):
        self.assertEqual(station("BOFB")[:2], ("BOF_B", "BOF"))
        self.assertEqual(station("LF1")[:2], ("LF1", "LF"))
        self.assertEqual(station("3#倒渣位")[:2], ("SLAG:3#", "SLAG"))
        self.assertEqual(station("REPAIR2")[:2], ("REPAIR:2#", "REPAIR"))
        self.assertEqual(station("TRANSFER:unknown")[:2], (None, "TRANSFER"))
        self.assertEqual(station("LF:unknown")[:2], (None, "LF"))
        self.assertFalse(station("LF:unknown")[2])

    def test_window_candidates_find_competing_heats(self):
        instance = json.loads(FIXTURE.read_text(encoding="utf-8"))
        candidates = window_candidates(copy.deepcopy(instance["tasks"]))
        self.assertTrue(candidates)
        self.assertTrue(any(len(item["heats"]) >= 2 for item in candidates))
        self.assertTrue(all(item["window_type"] == "campaign_local" for item in candidates))

    def test_build_instance_from_synthetic_fixture(self):
        instance = json.loads(FIXTURE.read_text(encoding="utf-8"))
        tasks = copy.deepcopy(instance["tasks"])
        candidates = window_candidates(copy.deepcopy(tasks))
        self.assertTrue(candidates)
        built = build_instance(
            tasks=tasks,
            events=copy.deepcopy(instance["events"]),
            candidates=candidates,
            primary_id=None,
            source_hashes={},
            assumptions=["synthetic test"],
            blocking=[],
        )
        self.assertEqual(validate_instance(built), [])
        self.assertGreater(len(built["tasks"]), 0)
        self.assertEqual(len(built["initial_state"]["cranes"]), 5)
        self.assertEqual(built["scenario"], "Q1")

    def test_build_instance_deterministic(self):
        instance = json.loads(FIXTURE.read_text(encoding="utf-8"))
        outputs = []
        for _ in range(2):
            tasks = copy.deepcopy(instance["tasks"])
            candidates = window_candidates(copy.deepcopy(tasks))
            built = build_instance(
                tasks=tasks,
                events=copy.deepcopy(instance["events"]),
                candidates=candidates,
                primary_id=None,
                source_hashes={},
                assumptions=["synthetic test"],
                blocking=[],
            )
            outputs.append(json.dumps(built, ensure_ascii=False, sort_keys=True))
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
