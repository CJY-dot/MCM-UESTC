"""Shared-input and paired-summary controls for the Q2 experiment harness."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "code/dispatch"))

from q2_scenario import condition
from run_q2 import METRICS, POLICIES, compare, describe


def measurements(x):
    return {metric: x for metric in METRICS}


class PairedDesignTests(unittest.TestCase):
    def test_comparison_pairs_by_replicate_instead_of_execution_order(self):
        rows = {policy: [(0, measurements(.25)), (1, measurements(.5))] for policy in POLICIES}
        rows["tight_margin"] = [(1, measurements(.5)), (0, measurements(.5))]
        _, deltas = compare(rows)
        result = deltas["tight_margin_minus_fcfs"]["completion_rate"]
        self.assertEqual((result["n"], result["mean"]), (2, .125))
        self.assertEqual(deltas["quantile_buffer_minus_fcfs"]["completion_rate"]["ci95_mean_t"], [0, 0])
        rows["fcfs"].pop()
        with self.assertRaises(ValueError):
            compare(rows)

    def test_constant_sample_has_degenerate_interval(self):
        self.assertEqual(describe([7, 7, 7])["ci95_mean_t"], [7, 7])
        self.assertIsNone(describe([7])["ci95_mean_t"])

    def test_motion_sensitivity_does_not_mutate_parent_configuration(self):
        file = ROOT / "stages/03_implementation/B/q2_parameter_pack.json"
        original = json.loads(file.read_text(encoding="utf-8"))["motion_profile"]
        before = json.dumps(original, sort_keys=True)
        modified = condition(original, layout="crowded", clearance="high")
        self.assertEqual(modified["initial_layout"], "crowded")
        self.assertTrue(all(pair["min_distance_m"] == 24
                            for pair in modified["clearance"]["adjacent_pairs"]))
        self.assertEqual(json.dumps(original, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
