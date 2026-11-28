"""Synthetic Q2 random-key, calibration, replay and visibility checks."""

from copy import deepcopy
import unittest

from q2_random_audit import (
    TapeAuditor, audit_calibration, canonical_sha256, future_notice_probe, information_prefix, stable_seed,
)


def synthetic_case():
    def task(tid, heat, order, service, dest, duration):
        return {"id": tid, "heat": heat, "order": order, "service": {"kind": service, "duration_s": duration},
                "destination": dest, "campaign": "campaign", "route": "L0"}

    return {
        "horizon_s": 30,
        "tasks": [
            task("b", "synthetic-B", 2, "refine", "LF1", 100.0),
            task("a", "synthetic-A", 1, "refine", "LF1", 100.0),
            task("d", "synthetic-D", 4, "refine", "LF1", 100.0),
            task("c", "synthetic-A", 1, "cast", "CC3", 20.0),
        ],
    }


def synthetic_models():
    calibration = {
        "strata": {
            "lf_duration/LF1/L0": {
                "basis": "historical_seconds",
                "fit_parameters": {
                    "block_pair": {"pairs": [{"values": [2.0, 3.0], "weight": 1.0}]},
                    "empirical": {"donors": [{"value": 6.0, "weight": 1.0}]},
                },
            },
            "cast_duration/CC3": {
                "basis": "paired_plan_ratio",
                "fit_parameters": {
                    "empirical": {"donors": [{"value": 1.5, "weight": 1.0}]},
                },
            },
        },
        "model_selection": {"lf_duration/LF1/L0": "block_pair", "cast_duration/CC3": "empirical"},
    }
    return calibration


def checker_for(case=None, override=None):
    case = case if case is not None else synthetic_case()
    model = synthetic_models()
    settings = {
        "settings": {"override_lf1": override}, "profile": {"layout": "synthetic"},
        "master_seed": 20261128, "replicates": 500,
        "case_sha256": canonical_sha256(case),
    }
    checker = TapeAuditor(case, settings, model, {"strata": {}})
    settings["common_input_sha256"] = checker.common_sha256
    return checker


def event(kind, tid, event_time, visible=None):
    return {"event_type": kind, "task_id": tid, "time_s": event_time,
            "visible_at_s": event_time if visible is None else visible}


def recorded_row(checker, service_end=2.0, supply_start=8.0):
    rep = 20
    tape = checker.rebuild(rep)
    log = [event("refine_start", "a", 0.0), event("refine_end", "a", service_end),
           event("supply_start", "c", supply_start)]
    return {"replicate_id": rep, "master_seed": checker.master_seed,
            "common_input_sha256": checker.common_sha256,
            "durations_s": tape, "duration_tape_sha256": canonical_sha256(tape),
            "strategies": {policy: {"events": deepcopy(log)}
                           for policy in ("fcfs", "tight_margin", "quantile_buffer")}}


class RandomAuditTests(unittest.TestCase):
    def test_frozen_seed_vector_and_compact_utf8(self):
        self.assertEqual(f"{stable_seed(123, 1, 'H', 'CC', 'E', 'CC3'):032x}",
                         "d7c58bd4a81f16baf5c14ee4f3611064")
        self.assertEqual(f"{stable_seed(314159, 7, '炉次-β', '精炼', '起吊🚋', 'LF一号'):032x}",
                         "85700ae2cf6d94bb1d70be1579f6e90f")
        self.assertNotEqual(stable_seed(123, 1, "H", "CC", "E", "CC3"),
                            stable_seed(123, 2, "H", "CC", "E", "CC3"))
        with self.assertRaises(ValueError):
            stable_seed(123, True, "H", "CC", "E", "CC3")

    def test_fixed_disjoint_block_slot_and_unpaired_fallback(self):
        checker = checker_for()
        self.assertEqual(checker.pairs, [("a", "b")])
        self.assertEqual(checker.rebuild(20), {"a": 2.0, "b": 3.0, "c": 30.0, "d": 6.0})
        case = synthetic_case()
        case["tasks"].reverse()
        self.assertEqual(checker_for(case).rebuild(20), checker.rebuild(20))

    def test_duplicate_order_and_model_override_use_empirical(self):
        case = synthetic_case()
        sibling = deepcopy(case["tasks"][1])
        sibling.update({"id": "e", "heat": "synthetic-E"})
        case["tasks"].append(sibling)
        checker = checker_for(case)
        self.assertEqual(checker.pairs, [])
        self.assertEqual(checker.rebuild(21)["e"], 6.0)
        changed = checker_for(override="empirical").rebuild(20)
        self.assertEqual(changed, {"a": 6.0, "b": 6.0, "c": 30.0, "d": 6.0})

    def test_tape_content_and_observable_consumption(self):
        checker = checker_for()
        row = recorded_row(checker)
        audited = checker.audit(row)
        self.assertEqual(audited["issues"], [])
        self.assertEqual((audited["consumed"], audited["censored"]), (3, 3))
        row["durations_s"]["a"] = 99
        row["duration_tape_sha256"] = canonical_sha256(row["durations_s"])
        self.assertEqual([issue["code"] for issue in checker.audit(row)["issues"]],
                         ["reconstructed_tape_hash"])

    def test_wrong_consumed_duration_and_impossible_censor(self):
        checker = checker_for()
        row = recorded_row(checker, service_end=3.0, supply_start=-1.0)
        codes = [issue["code"] for issue in checker.audit(row)["issues"]]
        self.assertEqual(codes.count("consumed_duration_mismatch"), 3)
        self.assertEqual(codes.count("uncensored_service_end_absent"), 3)

    def test_visibility_prefix_uses_notice_time(self):
        visible = event("supply_start", "c", 8.0)
        old_decision = {"now_s": 10.0, "chosen_task_id": "a"}
        early = {"events": [visible, event("supply_end", "c", 29.0, 31.0)],
                 "decisions": [old_decision, {"now_s": 32.0}]}
        late = {"events": [visible, event("supply_end", "c", 40.0)],
                "decisions": [old_decision, {"now_s": 41.0}]}
        self.assertEqual(information_prefix(early, 30.0), information_prefix(late, 30.0))
        self.assertNotEqual(information_prefix(early, 31.0), information_prefix(late, 31.0))

    def test_future_notice_probe_detects_lookahead(self):
        def honest(tape):
            return {"events": [event("refine_start", "a", 0), event("refine_end", "a", tape["a"])],
                    "decisions": [{"now_s": 2.0, "chosen_task_id": "fixed"}]}

        def looks_ahead(tape):
            result = honest(tape)
            if tape["a"] > 10.0:
                result["decisions"][0]["chosen_task_id"] = "changed"
            return result

        tape = {"a": 10.0}
        original = honest(tape)
        self.assertEqual(future_notice_probe(honest, tape, original),
                         {"checked": True, "post_start_decisions": 1,
                          "unchanged_prefix": True, "changed_suffix": True})
        self.assertFalse(future_notice_probe(looks_ahead, tape, original)["unchanged_prefix"])

    def test_weighted_ratio_anchor_detects_changed_donor(self):
        key = "cast_duration/CC3/L0"
        prediction = {"empirical": {"mean": 1.0, "quantiles": {"p75": 1.25, "p90": 1.25, "p95": 1.25}}}
        profile = {
            "basis": "paired_plan_ratio", "training_mean": 2.0, "training_n": 2,
            "predictions": prediction,
            "fit_parameters": {"empirical": {"donors": [
                {"sample_id": "one", "weight": 1.0, "value": .75},
                {"sample_id": "two", "weight": 1.0, "value": 1.25},
            ]}},
        }
        cal = {"schema": "mcm-uestc.q2-calibration/1", "fit_source": "train_only",
               "strata": {key: profile}, "model_selection": {key: "empirical"}}
        policy = {"schema": "mcm-uestc.q2-policy-parameters/1", "fit_source": "train_only",
                  "strata": {key: {"basis": "paired_plan_ratio", "training_n": 2, "training_mean_raw": 2.0,
                                  "predictive_summary": prediction,
                                  "positive_buffers_by_model": {
                                      "empirical": {"p75": 0.25, "p90": 0.25, "p95": 0.25}}}}}
        audit = {"schema": "mcm-uestc.q2-audit/1", "policy_parameter_sha256": canonical_sha256(policy),
                 "statistics": {"source_rows": 4, "eligible_rows_by_partition": {"train": 2},
                                "split": {"partition_summary": {"train": {"rows": 2}, "excluded": {"rows": 2}}},
                                "strata": {key: {"basis": "paired_plan_ratio",
                                                  "training_value_summary": {"mean": 2.0},
                                                  "n": {"train": 2, "validation": 0},
                                                  "validation": None, "validation_selected": "empirical"}}}}
        self.assertEqual(audit_calibration(cal, policy, audit)["issues"], [])
        profile["fit_parameters"]["empirical"]["donors"][0]["value"] = 1.25
        checked = audit_calibration(cal, policy, audit)["issues"]
        self.assertIn("duration_mean_anchor", {issue["code"] for issue in checked})


if __name__ == "__main__":
    unittest.main()
