"""Small independent subset tests. Every fixture is synthetic."""

import unittest

from q1_upper_bound import audit_bound


def task(tid, ladle, origin, destination, kind, *, previous=None, cast=None, order=1):
    return {
        "id": tid, "ladle": ladle, "origin": origin, "destination": destination,
        "kind": "ef", "scope": "ef_crane", "scope_status": "confirmed",
        "required_in_ef_scope": True, "campaign": "test-campaign", "order": order,
        "service": {"kind": kind, "duration_s": 10}, "predecessor": previous,
        "reuse_predecessor": None, "previous_cast": cast, "previous_bof": None,
        "rho_s": None, "blocks": [],
    }


def case(*tasks):
    return {"tasks": tasks, "capacity": {"return/CC3": 1, "slag/SLAG:3#": 1, "refine/LF1": 1}}


class UpperBoundTests(unittest.TestCase):
    def test_caster_turnover_requires_documented_release(self):
        first = task("seat1", "L1", "LF1", "CC3", "cast")
        later = task("seat2", "L2", "LF1", "CC3", "cast", cast="seat1", order=2)
        no_release = audit_bound(case(first, later))
        self.assertEqual(no_release["tested_subsets"], 4)
        self.assertEqual(no_release["gates"]["dependencies"]["upper"], 2)
        self.assertEqual(no_release["gates"]["caster_turnover"]["upper"], 1)
        self.assertEqual(no_release["maximum_subsets"], [["seat1"]])
        release = task("remove1", "L1", "CC3", "SLAG:3#", "slag", previous="seat1")
        with_release = audit_bound(case(first, later, release))
        self.assertEqual(with_release["maximum_subsets"], [["seat1", "seat2", "remove1"]])

    def test_same_place_and_ladle_required_for_physical_release(self):
        first = task("arrive", "L1", "P", "LF1", "refine")
        unauthorized = task("leave", "L2", "LF1", "CC3", "cast", previous="arrive")
        with self.assertRaisesRegex(ValueError, "same-ladle"):
            audit_bound(case(first, unauthorized))

    def test_unconfirmed_release_does_not_empty_terminal_stock(self):
        first = task("arrive1", "L1", "CC3", "SLAG:3#", "slag")
        later = task("arrive2", "L2", "CC3", "SLAG:3#", "slag")
        external = task("external", "L1", "SLAG:3#", "outside", "slag", previous="arrive1")
        external.update(kind="boundary", scope_status="pending_ef_confirmation",
                        required_in_ef_scope=None, blocks=["unconfirmed_ef_handoff"])
        result = audit_bound(case(first, later, external))
        self.assertEqual(result["gates"]["slag"]["upper"], 1)
        self.assertEqual(result["maximum_subsets"], [["arrive1"], ["arrive2"]])
        self.assertEqual(result["release_edges"][0]["blocked_external_candidates"], ["external"])
        external["blocks"] = []
        with self.assertRaisesRegex(ValueError, "external release candidate"):
            audit_bound(case(first, later, external))

    def test_refine_closure_and_terminal_occupancy_are_distinct(self):
        first = task("arrive1", "L1", "P", "LF1", "refine")
        later = task("arrive2", "L2", "P", "LF1", "refine")
        bound = audit_bound(case(first, later))
        self.assertEqual(bound["gates"]["dependencies"]["upper"], 2)
        self.assertEqual(bound["gates"]["terminal_stock"]["upper"], 1)

    def test_frozen_sequence_has_to_match_independent_order(self):
        first = task("seat1", "L1", "LF1", "CC3", "cast")
        later = task("seat2", "L2", "LF1", "CC3", "cast", order=2)
        with self.assertRaisesRegex(ValueError, "reconstructed sequence"):
            audit_bound(case(first, later))


if __name__ == "__main__":
    unittest.main()
