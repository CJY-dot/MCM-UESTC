"""Read-only replays of the two archived H1/H2/E1/E2 Q1 implementations."""

from collections import Counter
from importlib.util import module_from_spec, spec_from_file_location
from itertools import product
import sys

from q1_scenario import ROOT, read_json

PILOT = ROOT / "stages/01_topic_selection/validation/b_crane_dispatch/code/run_pilot.py"
PROBE = ROOT / "stages/02_modeling/B/B_probe.py"


def load_module(name, path):
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def legacy_q1():
    pilot = load_module("legacy_crane_pilot", PILOT)
    probe = load_module("legacy_b_q1_probe", PROBE)
    case = read_json(pilot.DEFAULT_CASE)
    travel = sorted(h["observed_seat"] - h["observed_pickup"] for h in case["heats"])
    modes = dict(zip(case["movement_duration_modes"], (travel[0], sum(travel) / 2, travel[-1])))
    reference = {"verified_conditional_schedule": 27, "travel_lower_bound_failed": 8,
                 "no_serialized_candidate": 1}
    enumeration = {}
    for heavy_only in (False, True):
        outcomes = Counter()
        nominal = None
        for geometry, gap, lead, mode in product(
                case["geometry_hypotheses"], case["minimum_gap_hypotheses"],
                case["seat_lead_seconds"], case["movement_duration_modes"]):
            positions = geometry["positions"]
            duration = modes[mode] / abs(positions["CC3"] - positions["LF1"])
            run = pilot.solve(case, int, positions, lead, gap, duration, heavy_only)
            outcomes[run["outcome"]] += 1
            if geometry["id"] == "east_turnaround" and gap == 3 and lead == 120 and mode == "central":
                nominal = len(run["deliveries"])
        if outcomes != reference or nominal != 4:
            raise ValueError("Archived four-task enumeration regression changed")
        enumeration["heavy_only" if heavy_only else "all_five"] = {
            "outcomes": dict(outcomes), "nominal_deliveries": nominal,
        }
    tasks, cranes, limits, speed, _ = probe.case_data(probe.DEFAULT_CASE, "east_turnaround", 120, 1.0)
    motion = (speed, 0.0, None, 1.0)
    rules = {}
    for rule in ("fcfs", "margin"):
        rows = probe.dispatch(tasks, cranes, limits, 3.0, motion, rule)
        metrics = probe.replay(rows, tasks, cranes, limits, 3.0, motion)
        result = {k: metrics[k] for k in ("served", "on_time", "min_gap")}
        if result != {"served": 4, "on_time": 3, "min_gap": 3.0}:
            raise ValueError("Archived B Q1 four-task policy regression changed")
        rules[rule] = result
    return {"scope": "original_conditioned_short_window", "stage01": enumeration,
            "stage02_q1": rules}
