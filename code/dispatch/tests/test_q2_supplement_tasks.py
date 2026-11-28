"""Small controls for the Q2 roster and plan-target reconciliation."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from q2_supplement_tasks import task_metrics


def task(task_id, *, service="cast", plan=None, blocked=(), release=0, predecessor=None,
         scope="ef_crane", required=True):
    return {
        "id": task_id, "service": {"kind": service} if service else None,
        "plan_target_s": plan, "blocks": list(blocked),
        "required_in_ef_scope": required, "rho_s": release,
        "predecessor": predecessor, "pickup_window": {"hard_earliest_s": None},
        "scope": scope, "scope_status": "confirmed" if required else "boundary",
    }


def archive(tasks, events):
    case = {"horizon_s": 100, "params": {"prep_s": 5}, "transfer_operation_s": 4, "tasks": tasks}
    rows, ledger = [], []
    for item in tasks:
        tid = item["id"]
        phases = events.get(tid, {})
        service = item["service"]
        name = {"cast": "supply", "refine": "refine", "slag": "slag"}.get(service["kind"]) if service else None
        start, end, drop = phases.get(f"{name}_start"), phases.get(f"{name}_end"), phases.get("drop_complete")
        rows.append({"task_id": tid, "required_in_ef_scope": item["required_in_ef_scope"],
                     "status": "completed" if end is not None else "delivered_service_censored" if drop is not None
                     else "input_blocked" if item["blocks"] else "unserved",
                     "plan_target_s": item["plan_target_s"], "drop_complete_s": drop,
                     "service_start_s": start, "service_end_s": end, "reasons": item["blocks"]})
        ledger.extend({"task_id": tid, "event_type": key, "time_s": value} for key, value in phases.items())
    return case, {"horizon_s": 100, "tasks": rows, "events": ledger}


class TaskMetricTests(unittest.TestCase):
    def test_delivery_and_completion_have_separate_denominators_and_statuses(self):
        tasks = [task("complete", plan=50), task("seated", plan=50), task("missing", plan=60),
                 task("external", scope="external_service", required=False, service=None),
                 task("pending", required=None, service=None)]
        events = {
            "complete": {"drop_complete": 58, "seat_complete": 58, "supply_start": 65, "supply_end": 90},
            "seated": {"drop_complete": 45, "seat_complete": 45},
            "missing": {"physical_release": 10},
        }
        metrics, rows = task_metrics(*archive(tasks, events))
        self.assertEqual((metrics["known_tasks"], metrics["target_ef"], metrics["outside_ef"]), (5, 3, 2))
        self.assertEqual((metrics["delivered_ef"], metrics["completed_ef"], metrics["delivered_unfinished_ef"]),
                         (2, 1, 1))
        self.assertEqual(tuple(metrics[k] for k in ("plan_targets", "on_time", "late_count", "unserved_targets")),
                         (3, 1, 1, 1))
        self.assertEqual(metrics["late_total_s"], 8)
        self.assertEqual(rows[1]["terminal_state"], "delivered_service_censored")
        self.assertEqual(rows[1]["plan_status"], "on_time")
        self.assertIsNone(rows[2]["plan_delay_s"])

    def test_blocked_future_and_no_event_are_retained_after_censoring(self):
        tasks = [
            task("path", blocked=("undeclared_service_path",), service=None),
            task("future", plan=130, release=145),
            task("unknown", release=None),
            task("other", blocked=("no_qualified_crane",)),
            task("pending_service", service="refine"),
            task("downstream", predecessor="pending_service", release=None),
            task("done", service="cast"),
            task("future_child", predecessor="done", release=None),
        ]
        events = {
            "pending_service": {"drop_complete": 85, "refine_start": 85},
            "done": {"drop_complete": 80, "seat_complete": 80, "supply_start": 80, "supply_end": 99},
        }
        m, rows = task_metrics(*archive(tasks, events))
        self.assertEqual([row["terminal_state"] for row in rows],
                         ["blocked_missing_path", "not_due_in_window", "unknown_unreleased",
                          "blocked_other_input", "delivered_service_censored",
                          "predecessor_unfinished", "completed", "not_due_in_window"])
        self.assertEqual((m["authorized_route_ef"], m["undeclared_route_ef"]), (7, 1))
        self.assertEqual((m["plan_targets"], m["future_plan_targets_ef"], m["unbound_plan_targets_ef"]),
                         (0, 1, 7))
        self.assertEqual(m["not_due_waiting_ef"], 2)

    def test_archive_discrepancy_is_detected_from_events(self):
        case, result = archive([task("T1", plan=50)], {
            "T1": {"drop_complete": 45, "seat_complete": 45, "supply_start": 55, "supply_end": 80}
        })
        result["tasks"][0]["status"] = "unserved"
        with self.assertRaisesRegex(ValueError, "does not reconcile"):
            task_metrics(case, result)


if __name__ == "__main__":
    unittest.main()
