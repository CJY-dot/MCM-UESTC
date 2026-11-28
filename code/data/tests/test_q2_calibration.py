"""Q2 sidecar tests. All test observations are synthetic."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "code/data"))

from q2_motion import motion_profile, rest_to_rest
from q2_schema import POLICY_SCHEMA, check_policy_parameters, check_sharable, stable_event_seed
from q2_statistics import chronological_split, fitted_models


class PolicyBoundaryTests(unittest.TestCase):
    def test_stable_keys_do_not_depend_on_generation_order(self):
        keys = [(0, "H1", "LF", "E1", "LF1"), (0, "H2", "CC", "E1", "CC3"),
                (1, "H1", "LF", "E1", "LF1")]
        forward = {k: stable_event_seed(42, *k) for k in keys}
        backward = {k: stable_event_seed(42, *k) for k in reversed(keys)}
        self.assertEqual(forward, backward)
        self.assertEqual(len(set(forward.values())), len(keys))
        self.assertNotEqual(stable_event_seed(42, 0, "ab", "c", "E", "D"),
                            stable_event_seed(42, 0, "a", "bc", "E", "D"))

    def test_policy_refuses_realizations_and_holdout(self):
        policy = {"schema": POLICY_SCHEMA, "fit_source": "train_only", "strata": {}}
        check_policy_parameters(policy)
        for key, payload in (("holdout", {"cdf": 0.3}), ("realized_draws", [1.0]),
                             ("donor_rows", [1.0]), ("heat_id", "H1")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_policy_parameters(policy | {"extra": {key: payload}})
        with self.assertRaises(ValueError):
            check_policy_parameters({"schema": "mcm-uestc.dispatch/1"})

    def test_public_release_rejects_raw_source_and_calendar(self):
        for value in ({"records": [{"source_row": 12}]}, {"value": "2020-05-04 00:00:00"},
                      {"path": "D:\\private.xlsx"}, {"quantile": float("nan")}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_sharable(value)


class MotionProfileTests(unittest.TestCase):
    def test_rest_to_rest_formula(self):
        long = rest_to_rest(40, 4, 2, 2)
        short = rest_to_rest(2, 4, 2, 2)
        self.assertEqual(long["profile"], "trapezoidal")
        self.assertAlmostEqual(long["total_s"], 12)
        self.assertEqual(short["profile"], "triangular")
        self.assertAlmostEqual(short["peak_speed_m_s"], 2)
        self.assertAlmostEqual(short["total_s"], 2)
        with self.assertRaises(ValueError):
            rest_to_rest(5, 0, 2, 2)

    def test_si_geometry_keeps_parked_cranes(self):
        path = ROOT / "stages/03_implementation/A/q1_assumption_scenarios.json"
        q1 = json.loads(path.read_text(encoding="utf-8"))
        profile = motion_profile(q1)
        for row in profile["scenarios"].values():
            scale = row["scale_m_per_reference_unit"]
            self.assertEqual(row["rail_limits_m"], [p * scale for p in q1["rail"]])
            self.assertEqual(row["station_positions_m"]["CC3"], q1["stations"]["CC3"] * scale)
            self.assertEqual(row["crane_order"], q1["crane_order"])
            self.assertEqual(len(row["crane_catalog"]), 5)
        self.assertTrue(profile["approval_required"])


class CalibrationTests(unittest.TestCase):
    def test_weighted_mean_anchor_including_pair_slots(self):
        rows = [{
            "sample_id": f"S{i}", "model_value": 0.7 + (i % 7) / 10, "weight": i % 3 + 1,
            "campaign_block_id": "R1", "campaign_order": i, "equipment": "CC3", "day": "toy",
            "start_time_s": 10 * i, "end_time_s": 10 * i + 5,
        } for i in range(20)]
        fit, _, _ = fitted_models(pd.DataFrame(rows), paired=True)
        self.assertEqual(set(fit["predictions"]), {"gamma", "lognormal", "empirical", "block_pair"})
        for model in fit["predictions"].values():
            self.assertAlmostEqual(model["mean"], 1)
        for value in fit["fit_parameters"]["block_pair"]["position_delivered_weighted_mean"]:
            self.assertAlmostEqual(value, 1)
        donors = fit["fit_parameters"]["empirical"]["donors"]
        self.assertAlmostEqual(sum(d["value"] * d["weight"] for d in donors) /
                               sum(d["weight"] for d in donors), 1)

    def test_date_split_purges_entire_cross_cut_campaign(self):
        rows, plans = [], []
        for i in range(10):
            day = f"1970-01-{i + 1:02d}"
            stamp = i * 86400
            heat = f"H{i}"
            campaign = "joint" if i in (4, 5) else f"C{i}"
            rows.append({"heat_id": heat, "day": day, "equipment": "CC3",
                         "start_time_s": stamp, "end_time_s": stamp + 5})
            plans.append({"heat_id": heat, "equipment": "CC3", "campaign": campaign, "plan_order": i,
                          "event": "plan_cast_start", "time_s": stamp})
        partition, _ = chronological_split(pd.DataFrame(rows), pd.DataFrame(plans))
        self.assertEqual(set(partition.loc[partition.campaign.eq("joint"), "partition"]), {"excluded"})
        self.assertEqual(set(partition.loc[partition.campaign.eq("joint"), "boundary_status"]),
                         {"cross_partition_campaign"})
        self.assertEqual(set(partition.partition), {"train", "validation", "final_holdout", "excluded"})


if __name__ == "__main__":
    unittest.main()
