"""Tests for archive-only statistics, pairing, and continuity proxy semantics."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from q2_metric_supplement import casting_gaps, paired_stats, summarize_wait


class SupplementTests(unittest.TestCase):
    def test_wait_censor_lower_bound_is_not_a_complete_sample(self):
        result = summarize_wait([
            {"status": "complete", "duration_s": 4},
            {"status": "censored", "duration_s": 20},
            {"status": "missing", "duration_s": None},
        ])
        self.assertEqual(result["right_censored_fraction_of_started"], .5)
        self.assertEqual(result["complete_case"]["p50"], 4)
        self.assertEqual(result["started_duration_lower_bound"]["p50"], 12)
        self.assertEqual(result["counts"]["missing"], 1)

    def test_casting_proxy_counts_only_known_start_or_censors(self):
        def cast(key, previous):
            return {"id": key, "previous_cast": previous, "destination": "CC3", "campaign": "S"}
        case = {"horizon_s": 200, "tasks": [cast("A", None), cast("B", "A"), cast("C", "B")]}
        r = {"events": [{"task_id": "A", "event_type": "supply_end", "time_s": 80}]}
        a, b = casting_gaps(case, r)
        self.assertEqual((a["status"], a["duration_s"]), ("censored", 120))
        self.assertEqual((b["status"], b["duration_s"]), ("missing", None))

    def test_paired_difference_by_replicate_id(self):
        r = paired_stats({
            "fcfs": {0: {"count": 1}, 1: {"count": 2}},
            "tight_margin": {1: {"count": 3}, 0: {"count": 2}},
            "quantile_buffer": {0: {"count": 1}, 1: {"count": 2}},
        })
        self.assertEqual(r["tight_margin_minus_fcfs"]["count"]["ci95"], [1, 1])
        self.assertEqual(r["quantile_buffer_minus_fcfs"]["count"]["ci95"], [0, 0])


if __name__ == "__main__":
    unittest.main()
