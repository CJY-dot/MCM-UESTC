"""Synthetic checks for frozen Q2 service sampling and forecast isolation."""

import math
import sys
import unittest
from pathlib import Path
from random import Random

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from q2_random import build_tape, forecast_map
from q2_schema import stable_event_seed


def event(tid, *, order=1, campaign="CAME", route="L0", destination="CC3", kind="cast", mean=100):
    return {
        "id": tid, "heat": f"HEAT/{tid}", "destination": destination, "campaign": campaign,
        "order": order, "route": route, "service": {"kind": kind, "duration_s": mean},
    }


def scenario(*tasks):
    return {"tasks": list(tasks)}


def strata(basis="paired_plan_ratio", donors=(0.5, 1.5), pair=(0.7, 1.3)):
    return {
        "basis": basis,
        "fit_parameters": {
            "empirical": {"donors": [{"value": x, "weight": 1} for x in donors]},
            "block_pair": {"pairs": [{"values": list(pair), "weight": 1}]},
            "gamma": {"shape": 4, "scale": 0.25},
            "lognormal": {"shape": 0.3, "scale": math.exp(-0.3**2 / 2)},
        },
    }


def calibration(profiles, models):
    return {
        "schema": "mcm-uestc.q2-calibration/1", "fit_source": "train_only", "strata": profiles,
        "model_selection": models,
    }


class RandomTapeTests(unittest.TestCase):
    def test_stable_event_seed_matches_B_protocol_and_order_invariance(self):
        self.assertEqual(
            format(stable_event_seed(123, 1, "H", "CC", "E", "CC3"), "032x"),
            "d7c58bd4a81f16baf5c14ee4f3611064",
        )
        left, right = event("a"), event("b", order=2)
        pack = calibration({"cast_duration/CC3": strata()}, {"cast_duration/CC3": "gamma"})
        first = build_tape(scenario(left, right), pack, 317, 0)
        reverse = build_tape(scenario(right, left), pack, 317, 0)
        self.assertEqual(list(first.items()), list(reverse.items()))
        self.assertTrue(all(x > 0 for x in first.values()))
        self.assertNotEqual(first, build_tape(scenario(left, right), pack, 317, 1))

    def test_non_overlapping_planned_pairs_remain_fixed(self):
        first, second, third = event("a"), event("b", order=2, mean=200), event("c", order=3)
        other = event("d", order=4, campaign="DIFFERENT")
        pack = calibration({"cast_duration/pool": strata(donors=(1.0,))},
                           {"cast_duration/pool": "block_pair"})
        tape = build_tape(scenario(third, other, second, first), pack, 19, 1)
        self.assertEqual(tape, {"a": 70, "b": 260, "c": 100, "d": 100})
        self.assertEqual(tape, build_tape(scenario(first, second, third, other), pack, 19, 1))

    def test_plan_anchor_applied_once_for_each_fitted_family(self):
        fit = strata()
        case = scenario(event("a", mean=300))
        key = "cast_duration/CC3/L0"
        event_seed = stable_event_seed(55, 9, "HEAT/a", "cast_duration", "service_end:a", "CC3")
        for family in ("gamma", "lognormal"):
            pack = calibration({key: fit}, {key: family})
            shape = fit["fit_parameters"][family]["shape"]
            scale = fit["fit_parameters"][family]["scale"]
            expected_rng = Random(event_seed)
            if family == "gamma":
                expected = expected_rng.gammavariate(shape, scale)
                self.assertEqual(shape * scale, 1)
            else:
                expected = expected_rng.lognormvariate(math.log(scale), shape)
                self.assertAlmostEqual(scale * math.exp(shape**2 / 2), 1)
            self.assertAlmostEqual(build_tape(case, pack, 55, 9)["a"], 300 * expected)

    def test_device_and_process_fallback_and_missing_fit(self):
        tasks = [event("route"), event("device", route="R0"), event("pooled", destination="CC9"),
                 event("missing", destination="RH1", kind="refine")]
        profiles = {
            "cast_duration/CC3/L0": strata(donors=(1.1,)),
            "cast_duration/CC3": strata(donors=(1.2,)),
            "cast_duration/pool": strata(donors=(1.3,)),
        }
        pack = calibration(profiles, dict.fromkeys(profiles, "empirical"))
        tape = build_tape(scenario(*tasks), pack, 1, 0)
        self.assertAlmostEqual(tape["route"], 110)
        self.assertAlmostEqual(tape["device"], 120)
        self.assertAlmostEqual(tape["pooled"], 130)
        self.assertEqual(tape["missing"], 100)

    def test_fallback_forecast_uses_public_parameters_only(self):
        case = scenario(event("a", destination="LF1", kind="refine"),
                        event("b", order=2, destination="LF1", kind="refine"),
                        event("c", order=4, destination="LF1", kind="refine"))
        key = "lf_duration/LF1"
        policy = {"strata": {key: {
            "basis": "historical_seconds",
            "predictive_summary": {"empirical": {"mean": 2300}, "block_pair": {"mean": 2200}},
            "positive_buffers_by_model": {"empirical": {"p90": 200}, "block_pair": {"p90": 300}},
        }}}
        families = {key: "block_pair"}
        expected = {"a": (2200, 300), "b": (2200, 300), "c": (2300, 200)}
        self.assertEqual(forecast_map(case, policy, families), expected)
        self.assertEqual(
            forecast_map(case, policy, families, override_lf1="empirical"),
            dict.fromkeys(expected, (2300, 200)),
        )
        for pair in ((1500, 1700), (3000, 3200)):
            pack = calibration({key: strata("historical_seconds", donors=(2100,), pair=pair)}, families)
            self.assertEqual(build_tape(case, pack, 91, 1)["a"], pair[0])
            self.assertEqual(forecast_map(case, policy, families), expected)


if __name__ == "__main__":
    unittest.main()
