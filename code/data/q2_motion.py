"""Conditional SI motion inputs for Q2 on the frozen Q1 EF topology."""

from __future__ import annotations

from itertools import pairwise
from math import isfinite, sqrt


MOTION_SCHEMA = "mcm-uestc.q2-motion-profile/1"
CAPACITY_T = {"2#150": 150, "4#450": 450, "3#450": 450, "5#450": 450, "1#150": 150}
SCALES = {"scale_low": 3.0, "base": 4.0, "scale_high": 5.0}


def rest_to_rest(distance_m: float, speed_cap_m_s: float, acceleration_m_s2: float,
                 deceleration_m_s2: float) -> dict:
    """Analytic single unrestricted segment, with zero speed at both ends."""
    args = (distance_m, speed_cap_m_s, acceleration_m_s2, deceleration_m_s2)
    if not all(isfinite(v) and v > 0 for v in args):
        raise ValueError("Rest-to-rest comparison needs positive finite distance and caps")
    v, forward, brake = speed_cap_m_s, acceleration_m_s2, deceleration_m_s2
    ramp_factor = 1 / forward + 1 / brake
    threshold_m = 0.5 * v * v * ramp_factor
    if distance_m < threshold_m:
        peak = sqrt(2 * distance_m / ramp_factor)
        cruise_s = 0.0
        profile = "triangular"
    else:
        peak = v
        cruise_s = (distance_m - threshold_m) / v
        profile = "trapezoidal"
    accelerate_s, decelerate_s = peak / forward, peak / brake
    return {
        "profile": profile,
        "distance_m": distance_m,
        "critical_distance_m": threshold_m,
        "peak_speed_m_s": peak,
        "accelerate_s": accelerate_s,
        "cruise_s": cruise_s,
        "decelerate_s": decelerate_s,
        "total_s": accelerate_s + cruise_s + decelerate_s,
    }


def _limits(loaded_cruise_m_s: float, ratio: float = 1.0) -> dict:
    profiles = {}
    for state, load_factor in (("no_load", 1.25), ("empty_ladle", 1.125), ("loaded", 1.0)):
        cruise = loaded_cruise_m_s * load_factor * ratio
        profiles[state] = {
            "source_status": "assumed",
            "gear_speed_caps_m_s": {
                "creep": round(cruise * 0.5, 8),
                "approach": round(cruise * 0.75, 8),
                "cruise": round(cruise, 8),
            },
            "acceleration_m_s2": round(cruise / 25, 8),
            "deceleration_m_s2": round(cruise / 20, 8),
        }
    return profiles


def _scenario(q1: dict, scale: float, profiles: dict) -> dict:
    order = q1["crane_order"]
    active = set(q1["dispatch"])
    bounds = [x * scale for x in q1["rail"]]
    stations = {station: x * scale for station, x in q1["stations"].items()}
    interfaces = {site: x * scale for site, x in q1["interface_points"].items()}
    parking = [x * scale for x in q1["parking"]]
    near_slag = [x for name, x in q1["interface_points"].items() if name.startswith("SLAG:")]
    if len(near_slag) != 2:
        raise ValueError("Two declared slag approach interfaces are required")
    zone = [(min(near_slag) - 2) * scale, (max(near_slag) + 3) * scale]
    initial_layouts = {}
    for layout, points in q1["layouts"].items():
        initial_layouts[layout] = [
            {
                "crane_id": crane,
                "position_m": x * scale,
                "speed_m_s": 0.0,
                "mode": "idle" if crane in active else "parked",
                "track_present": True,
                "dispatch_allowed": crane in active,
                "load_state": "no_load",
                "source_status": "assumed",
            }
            for crane, x in zip(order, points, strict=True)
        ]
    gap_m = q1["scenarios"]["base"]["gap"] * scale
    sensitivity_gap = {
        label: q1["scenarios"][scenario]["gap"] * scale
        for label, scenario in (("low", "optimistic"), ("base", "base"), ("high", "conservative"))
    }
    if not all(bounds[0] <= x <= bounds[1]
               for x in (*stations.values(), *interfaces.values(), *parking, *zone)):
        raise ValueError("Scenario locations must be within the declared rail")
    if any(right["position_m"] - left["position_m"] < gap_m
           for states in initial_layouts.values() for left, right in pairwise(states)):
        raise ValueError("Initial fixture violates the baseline clearance")
    return {
        "source_status": "assumed",
        "scale_m_per_reference_unit": scale,
        "rail_limits_m": bounds,
        "station_positions_m": stations,
        "station_sides": dict(q1["station_sides"]),
        "interface_positions_m": interfaces,
        "parking_positions_m": parking,
        "crane_order": list(order),
        "authorized_ef_legs": [list(leg) for leg in q1["authorized_ef_legs"]],
        "initial_layout": "dispersed",
        "initial_state_layouts": initial_layouts,
        "crane_catalog": [
            {
                "crane_id": crane,
                "capacity_t": CAPACITY_T[crane],
                "capacity_source_status": "documented",
                "dispatch_candidate": crane in active,
                "rail_limits_m": bounds,
                "motion_profile_group": "mobile_450t" if crane in active else None,
            }
            for crane in order
        ],
        "motion": {
            "mobile_450t": profiles,
            "parked_150t_speed_m_s": 0.0,
            "parked_150t_acceleration_m_s2": 0.0,
            "gear_applicability": {
                "creep": "positioning_or_yield_on_authorized_path",
                "approach": "local_zone_or_approaching_a_restricted_segment",
                "cruise": "ordinary_segment_outside_restricted_zones",
            },
        },
        "speed_zones": [{
            "zone_id": "slag_approach",
            "interval_m": zone,
            "crane_ids": [crane for crane in order if crane in active],
            "permitted_gears": ["creep", "approach"],
            "speed_caps_m_s": {state: limits["gear_speed_caps_m_s"]["approach"]
                                for state, limits in profiles.items()},
            "source_status": "assumed",
        }],
        "clearance": {
            "convention": "minimum_longitudinal_reference_point_separation_includes_envelope",
            "adjacent_pairs": [{"left_crane": left, "right_crane": right, "min_distance_m": gap_m}
                               for left, right in pairwise(order)],
            "threshold_sensitivities_m": sensitivity_gap,
            "vehicle_envelope_dimension_m": None,
            "source_status": "assumed",
        },
        "waypoint_rules": {
            "stop_and_yield": "declared_parking_only",
            "station_stop_requires_service_reservation": True,
            "allow_passing": False,
            "allow_lateral_detour": False,
            "position_and_speed_continuous": True,
            "direction_change_requires_zero_speed": True,
        },
    }


def motion_profile(q1_assumptions: dict) -> dict:
    """Instantiate declared engineering assumptions without estimating from production timestamps."""
    q1 = q1_assumptions
    order = q1["crane_order"]
    if q1["unit"] != "normalized" or set(order) != set(CAPACITY_T) or len(order) != len(CAPACITY_T):
        raise ValueError("Motion profile requires the frozen normalized five-crane EF layout")
    if set(q1["dispatch"]) != {crane for crane, capacity in CAPACITY_T.items() if capacity == 450}:
        raise ValueError("Motion profile requires the frozen three candidate cranes")
    reference = q1["scenarios"]["base"]["speed"] * SCALES["base"]
    limits = _limits(reference)
    scenarios = {key: _scenario(q1, scale, limits) for key, scale in SCALES.items()}
    analytic = {}
    for label, instance in scenarios.items():
        origin, destination = (instance["station_positions_m"][s] for s in ("LF1", "CC3"))
        zone_start, zone_end = instance["speed_zones"][0]["interval_m"]
        if max(origin, zone_start) <= min(destination, zone_end):
            raise ValueError("The analytic LF1–CC3 example must lie outside restricted segments")
        loaded = limits["loaded"]
        analytic[label] = rest_to_rest(destination - origin, loaded["gear_speed_caps_m_s"]["cruise"],
                                       loaded["acceleration_m_s2"], loaded["deceleration_m_s2"])
    return {
        "schema": MOTION_SCHEMA,
        "source_status": "assumed",
        "approval_required": True,
        "approval_scope": "conditional_q2_simulation_only",
        "positive_direction": "west_to_east",
        "units": {"position": "m", "speed": "m/s", "acceleration": "m/s^2", "time": "s", "capacity": "t"},
        "scenarios": scenarios,
        "kinematic_sensitivities": {"slow": _limits(reference, 0.8), "nominal": limits,
                                    "fast": _limits(reference, 1.2)},
        "analytical_checks": {"unrestricted_loaded_lf1_cc3_rest_to_rest": analytic},
        "evidence": {
            "documented": ["five_ef_cranes_and_nominal_tonnage", "shared_rail_no_overtaking",
                           "station_relative_order_and_side"],
            "assumed": ["longitudinal_scale_3_4_5_m_per_reference_unit", "receiving_interface_points",
                        "parking_positions_and_initial_track_occupancy", "three_speed_gears_and_ramps",
                        "slag_approach_zone_and_reference_clearance"],
            "pending": ["surveyed_coordinates_and_rail_limits", "crane_reference_envelopes",
                        "factory_velocity_and_ramp_profiles", "actual_zones_and_legal_parking",
                        "actual_initial_positions_and_150t_track_occupancy"],
            "kinematics_basis": "Frozen Q1 assumed 0.10 reference-unit/s at the base scale; "
                                "B stage02 scenario ramp and approach ratios; no calibration from transport timestamps",
        },
    }
