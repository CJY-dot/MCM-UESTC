"""Audit frozen Q1 witnesses. A's solver is used only as a subprocess under test."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from q1_resources import audit_resources
from q1_tracks import audit_counterexample_41, audit_tracks
from q1_upper_bound import audit_bound

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "stages/03_implementation/A/q1_assumption_scenarios.json"
A_CODE = ROOT / "code/dispatch"
POLICIES = ("fcfs", "tight_margin")
BASELINE = "65663a76ba4db65e3b5315b90c3dac5b129bd4bb"
B_NAMES = ("q1_instance.json", "q1_policy_view.json", "q1_simulator_truth.json")
REPORTED = {
    "base": (9, 3, 0, 4),
    "gap_4_alternate": (9, 3, 0, 8),
    "gap_6": (6, 2, 0, 6),
    "gap_6_alternate": (9, 3, 0, 8),
    "conservative": (3, 1, 0, 6.9),
    "conservative_expanded_strict": (4, 1, 0, 6.9),
    "conservative_expanded_diagnostic": (6, 1, 434, 6.9),
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    return sha_bytes(path.read_bytes())


def canonical_sha(record):
    return sha_bytes(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                allow_nan=False).encode("utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def private_dir(path):
    path = path.resolve()
    prefix = (ROOT / "local").resolve()
    if prefix not in path.parents:
        raise ValueError("C audit output must be a new directory below local/")
    relative = path.relative_to(ROOT.resolve()).as_posix()
    if git("ls-files", "--", relative) or not git("check-ignore", "--", relative):
        raise ValueError("C output is not safely ignored")
    path.mkdir(parents=True, exist_ok=False)
    return path


def provenance(args, case):
    original = read(args.a_delivery / "q1_run_record.json")
    diagnostics = read(args.a_diagnostic / "diagnostic_summary.json")
    config = read(CONFIG)
    evidence = {
        "checked_git_head": git("rev-parse", "HEAD").decode().strip(),
        "merge_baseline": BASELINE, "a_delivery_index_sha256": sha_file(args.a_delivery / "q1_run_record.json"),
        "a_diagnostic_index_sha256": sha_file(args.a_diagnostic / "diagnostic_summary.json"),
        "python": sys.version.split()[0],
        "configuration_file_sha256": sha_file(CONFIG), "configuration_semantic_sha256": canonical_sha(config),
        "case_semantic_sha256": canonical_sha(case),
        "b_primary_file_sha256": {name: sha_file(args.b_primary / name) for name in B_NAMES},
        "archived_source_verification": {}, "checked_code_sha256": {},
    }
    faults = []
    merged = subprocess.run(["git", "merge-base", "--is-ancestor", BASELINE, "HEAD"], cwd=ROOT)
    if merged.returncode:
        faults.append("HEAD does not contain accepted PR merge")
    config_hash = evidence["configuration_semantic_sha256"]
    if any(h != config_hash for h in (case["assumption_config_sha256"], original["config_sha256"],
                                     diagnostics["config_sha256"])):
        faults.append("configuration hash disagreement")
    if evidence["case_semantic_sha256"] != diagnostics["registered"]["base"]["case_sha256"]:
        faults.append("base case and diagnostic index disagree")
    if any(evidence["b_primary_file_sha256"][name] != original["inputs_sha256"][name] for name in B_NAMES):
        faults.append("input differs from A delivery B bundle hash")
    for filename, expected in original["code_sha256"].items():
        before_a1 = git("show", f"7346ca2:code/dispatch/{filename}")
        initial = sha_bytes(before_a1.replace(b"\r\n", b"\n"))
        merged_bytes = git("show", f"{BASELINE}:code/dispatch/{filename}").replace(b"\r\n", b"\n")
        current = (A_CODE / filename).read_bytes().replace(b"\r\n", b"\n")
        evidence["archived_source_verification"][filename] = {
            "archived_expected": expected, "historical_blob": initial,
            "merged_blob": sha_bytes(merged_bytes), "current_blob": sha_bytes(current),
        }
        if initial != expected or current != merged_bytes:
            faults.append(f"A solver provenance disagreement at {filename}")
    diagnostic_src = "code/dispatch/q1_diagnostic.py"
    if (ROOT / diagnostic_src).read_bytes().replace(b"\r\n", b"\n") != git(
            "show", f"{BASELINE}:{diagnostic_src}").replace(b"\r\n", b"\n"):
        faults.append("A diagnostic source differs from merge baseline")
    for path in (*Path(__file__).parent.glob("*.py"), A_CODE / "q1_diagnostic.py"):
        evidence["checked_code_sha256"][path.name] = sha_file(path)
    return evidence, faults


def check_scenario_sources(case, config, bundle, public_view, tag):
    """Check physical assumptions and planned inputs against their separate sources."""
    params = config["scenarios"][tag]
    errors = []
    if case["params"] != params:
        errors.append("scenario parameter mix differs from frozen configuration")
    factor = params["distance_factor"]
    if case["positions"] != {s: x * factor for s, x in
                             (config["stations"] | config["interface_points"]).items()}:
        errors.append("planning coordinates differ from declared scaled geometry")
    if (case["rail"] != [x * factor for x in config["rail"]]
            or case["parking"] != [x * factor for x in config["parking"]]
            or case["topology"]["station_sides"] != config["station_sides"]):
        errors.append("rail, parking or station sides differ")
    devices = {d["device_id"]: d for d in bundle["device_catalog"]}
    layout = config["layouts"][case["layout"]]
    cranes = [
        {"id": cid, "x": x * factor, "capacity_t": devices[cid]["capacity_t"],
         "dispatch": cid in config["dispatch"] and devices[cid]["dispatch_candidate"]}
        for cid, x in zip(config["crane_order"], layout)
    ]
    if case["cranes"] != cranes:
        errors.append("crane assumptions or source catalog differ")
    for key in ("lookahead_s", "seat_lead_s", "caster_preparation_s", "pickup_operation_s",
                "drop_operation_s", "transfer_operation_s"):
        if case[key] != config[key]:
            errors.append(f"setup differs at {key}")
    prefix_map = {
        "bof": "bof_receipt", "refine": "refine_position", "seat": "caster_seat",
        "supply": "caster_supply", "return": "caster_return", "slag": "slag_position",
    }
    if any(n != config["capacity"][prefix_map[r.split("/")[0]]] for r, n in case["capacity"].items()):
        errors.append("capacity differs from registered scenario")
    if case["source"] != bundle["instance_id"] or bundle["instance_id"] != public_view["instance_id"]:
        errors.append("bundle identity and A scenario differ")
    before = {t["task_id"]: t for t in bundle["tasks"]}
    visible = {t["task_id"]: t for t in public_view["tasks"]}
    if {t["id"] for t in case["tasks"]} != set(bundle["scope"]["target_tasks"]):
        errors.append("scope differs from frozen roster")
    heats = {t["heat"] for t in case["tasks"]}
    plans = defaultdict(set)
    for event in bundle["events"]:
        if (event["heat_id"] in heats and event["event_category"] == "plan"
                and event["usable_for_canonical"] and event["time_s"] is not None):
            plans[event["heat_id"], event["event_type"]].add(event["time_s"])
    def plan(heat, event_type):
        times = plans[heat, event_type]
        if len(times) != 1:
            raise ValueError("Expected unique planned time in frozen Q1W06 roster")
        return next(iter(times))

    origin = min(plan(heat, "plan_bof_end") for heat in heats)
    planned_horizon = max(plan(heat, "plan_cast_end") for heat in heats) - origin + config["planning_margin_s"]
    if case["horizon_s"] != planned_horizon:
        errors.append("horizon differs from registered plan origin and margin")
    identities = defaultdict(set)
    reverse_ids = defaultdict(set)
    for t in case["tasks"]:
        tid, heat = t["id"], t["heat"]
        b = before[tid]
        identities[b["ladle_id"]].add(t["ladle"])
        reverse_ids[t["ladle"]].add(b["ladle_id"])
        for field, b_field in (
            ("heat", "heat_id"), ("campaign", "campaign_id"), ("scope", "scope"),
            ("scope_status", "scope_status"), ("source_admission", "admission"),
            ("source_blockers", "blocker_codes"), ("load_state", "load_state"),
            ("predecessor", "predecessor_task_id"), ("route", "route"), ("origin", "origin"),
            ("destination", "destination"), ("required_in_ef_scope", "required_in_ef_scope"),
            ("required_in_process", "required_in_process"),
        ):
            if t[field] != b[b_field]:
                errors.append(f"{tid}: frozen B field differs: {field}")
        if t["order"] != int(b["campaign_order"]):
            errors.append(f"{tid}: order differs")
        if t["kind"] != "ef":
            continue
        if [t["origin"], t["destination"]] not in config["authorized_ef_legs"]:
            errors.append(f"{tid}: route lacks conditional authorization")
        if t["mass_upper_t"] != config["assumed_mass_upper_t"][t["load_state"]]:
            errors.append(f"{tid}: weighing assumption differs")
        expected_cranes = [c["id"] for c in cranes if c["dispatch"] and c["capacity_t"] >= t["mass_upper_t"]
                           and (b["eligible_cranes"] is None or c["id"] in b["eligible_cranes"])]
        if t["eligible"] != expected_cranes:
            errors.append(f"{tid}: conditional crane qualification differs")
        for window in ("pickup_window", "delivery_window"):
            for bound in ("hard_earliest_s", "hard_latest_s"):
                expected = visible[tid][window][bound]
                if t[window][bound] != (expected - origin if expected is not None else None):
                    errors.append(f"{tid}: hard bound differs from B policy")
        expected_ready = None if t["predecessor"] else plan(heat, "plan_bof_end") - origin + params["prep_s"]
        if t["rho_s"] != expected_ready:
            errors.append(f"{tid}: readiness input differs")
        kind = t["service"]["kind"]
        if kind == "cast":
            start = plan(heat, "plan_cast_start")
            duration = plan(heat, "plan_cast_end") - start
            service = {"kind": "cast", "not_before_s": start - origin, "duration_s": duration}
            if t["plan_target_s"] != start - origin - config["seat_lead_s"]:
                errors.append(f"{tid}: seating target differs")
        elif kind == "refine":
            service = {"kind": "refine", "not_before_s": plan(heat, "plan_refine_slot_1") - origin,
                       "duration_s": params["lf_s"]}
        else:
            service = {"kind": "slag", "not_before_s": 0, "duration_s": params["slag_s"][t["destination"]]}
        if t["service"] != service:
            errors.append(f"{tid}: planned service differs")
    if any(len(ids) != 1 for ids in (*identities.values(), *reverse_ids.values())):
        errors.append("Ladle alias mapping merges or splits B identities")
    return errors


# This code runs in the DUT process. None of A's output checkers are used in this module.
REPLAY = '''
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from q1_scheduler import solve
request=json.load(sys.stdin)
opts=request["search"]
case=request["case"]
if request["east_after_delivery"]:
    axis={c["id"]: i for i,c in enumerate(case["cranes"])}
    def rank(option):
        return (option["lateness_s"] or 0, option["drop_complete_s"], -axis[option["crane_id"]],
                option["empty_run_s"] + option["loaded_run_s"])
    opts["rank_candidate"]=rank
result=solve(case,request["policy"],enforce_plan=request["strict"],**opts)
json.dump(result,sys.stdout,ensure_ascii=False,allow_nan=False)
'''


def replay(case, policy, strict, search, east):
    request = {"case": case, "policy": policy, "strict": strict,
               "search": search, "east_after_delivery": east}
    completed = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", REPLAY, str(A_CODE)],
                               input=json.dumps(request), capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError("A solver replay failed")
    return json.loads(completed.stdout)


def run(args):
    output = private_dir(args.output)
    cases = {
        tag: read(args.a_delivery / tag / "q1_planning_scenario.json")
        for tag in ("base", "conservative")
    }
    base, conserved = cases["base"], cases["conservative"]
    evidence, issues = provenance(args, base)
    config = read(CONFIG)
    bundle, policy_view = (read(args.b_primary / name) for name in B_NAMES[:2])
    for tag, case in cases.items():
        issues.extend(check_scenario_sources(case, config, bundle, policy_view, tag))
        if case["initial_occupancy"] or case["locked_trajectories"]:
            issues.append(f"unexpected inherited occupancy in {tag}")
        scenario_path = args.a_delivery / tag / "q1_planning_scenario.json"
        evidence[f"{tag}_case_file_sha256"] = sha_file(scenario_path)
        if canonical_sha(case) != read(args.a_diagnostic / "diagnostic_summary.json")["registered"][tag]["case_sha256"]:
            issues.append(f"{tag}: scenario differs from A.1 diagnostic index")

    bound = audit_bound(base)
    # Compare A's answer only after deriving the independent certificate.
    a_certificate = read(args.a_diagnostic / "coverage_certificate.json")
    for key in ("tested_subsets", "gates", "maximum_subsets", "never_feasible_in_relaxation"):
        if a_certificate[key] != bound[key]:
            issues.append(f"independent bound differs from A certificate at {key}")
    write(output / "coverage_audit.json", bound)
    rank_case = base | {"params": base["params"] | {"gap": 6}}
    pad_s = 1.5 * conserved["params"]["gap"] / conserved["params"]["speed"]
    strict_controls = {"search_lookahead_s": 1200, "unlock_pad_s": pad_s}
    diagnostic_index = read(args.a_diagnostic / "diagnostic_summary.json")
    if pad_s != 120 or diagnostic_index["conservative_search_controls"] != strict_controls:
        issues.append("declared conservative search controls differ")
    cases_to_replay = (
        ("base", base, False, {}, False, args.a_delivery / "base"),
        ("gap_4_alternate", base, True, {}, True, args.a_diagnostic / "witnesses/gap_4_alternate"),
        ("gap_6", rank_case, True, {}, False, args.a_diagnostic / "witnesses/gap_6"),
        ("gap_6_alternate", rank_case, True, {}, True, args.a_diagnostic / "witnesses/gap_6_alternate"),
        ("conservative", conserved, False, {}, False, args.a_delivery / "conservative"),
        ("conservative_expanded_strict", conserved, True, strict_controls, False,
         args.a_diagnostic / "witnesses/conservative_expanded_strict"),
        ("conservative_expanded_diagnostic", conserved, False, strict_controls, False,
         args.a_diagnostic / "witnesses/conservative_expanded_diagnostic"),
    )
    rows, baseline_members, search_observations = [], [], []
    archived_hashes = {(m["scenario"], m["policy"]): m["result_sha256"]
                       for m in read(args.a_delivery / "q1_run_record.json")["runs"]}
    for tag, case, strict, search, east, folder in cases_to_replay:
        for policy in POLICIES:
            source = folder / policy / "q1_dispatch_result.json" if folder.is_relative_to(
                args.a_delivery) else folder / f"{policy}.json"
            result = read(source)
            replayed = replay(case, policy, strict, search, east)
            if replayed != result:
                issues.append(f"{tag}/{policy}: A replay differs from archived witness")
            case_sha = canonical_sha(case)
            if result["case_sha256"] != case_sha:
                issues.append(f"{tag}/{policy}: A witness uses other input case")
            if (tag, policy) in archived_hashes and canonical_sha(result) != archived_hashes[tag, policy]:
                issues.append(f"{tag}/{policy}: A witness changed since manifest")
            expected_mode = "strict_target_conditional" if strict else "lateness_diagnostic_conditional"
            if result["witness_mode"] != expected_mode:
                issues.append(f"{tag}/{policy}: witness mode disagrees with replay profile")
            resources = audit_resources(case, result)
            tracks = audit_tracks(case, result)
            if resources["errors"] or tracks["errors"]:
                issues.append(f"{tag}/{policy}: C detected trace violations")
            computed = (len(resources["completed_task_ids"]), resources["on_time"],
                        resources["late_total_s"], tracks["minimum_gap"])
            if (resources["target_ef"] != 17 or resources["plan_targets"] != 6
                    or any(abs(got - expected) > 1e-8 for got, expected in zip(computed, REPORTED[tag]))):
                issues.append(f"{tag}/{policy}: recomputed headline differs")
            if tag == "base":
                baseline_members.append(set(resources["completed_task_ids"]))
            detail = {
                "tag": tag, "policy": policy, "strict": strict, "search": search,
                "rank_east_after_delivery": east, "case_semantic_sha256": case_sha,
                "archive_file_sha256": sha_file(source), "replay_semantic_sha256": canonical_sha(replayed),
                "replay_matches": replayed == result, "resources": resources, "tracks": tracks,
            }
            if tag.startswith("conservative"):
                roster = [t for t in case["tasks"] if t["required_in_ef_scope"] is True]
                by_id = {t["task_id"]: t for t in result["tasks"]}
                decision = {t["task_id"]: t for t in result["decisions"]}
                p, arriving = roster[1]["id"], roster[3]["id"]
                release, pick = by_id[arriving]["release_s"], by_id[arriving]["pickup_start_s"]
                unload, unlock = by_id[arriving]["drop_complete_s"], by_id[p]["pickup_complete_s"]
                search_observations.append({
                    "tag": tag, "policy": policy, "T02_booked_s": decision[p]["now_s"],
                    "T02_unlock_s": unlock, "T04_release_s": release,
                    "T04_pickup_latest_s": release + case["params"]["max_pick_wait_s"],
                    "T04_pickup_s": pick, "T04_unload_s": unload,
                    "T04_unload_after_unlock_s": unload - unlock if unload is not None else None,
                    "T04_loaded_wait_s": sum(s["t1_s"] - s["t0_s"] for s in result["trajectories"]
                                             if s["task_id"] == arriving and s["mode"] == "wait_loaded"),
                })
            write(output / f"{tag}_{policy}.json", detail)
            rows.append({"tag": tag, "policy": policy, "gap": tracks["minimum_gap"],
                         "completed": computed[0], "on_time": computed[1], "total_seat_delay_s": computed[2],
                         "replay_matches": replayed == result,
                         "physical_errors": len(resources["errors"]) + len(tracks["errors"])})
    maxima = [set(group) for group in bound["maximum_subsets"]]
    if len(maxima) != 1 or any(m not in maxima for m in baseline_members):
        issues.append("actual baseline task set disagrees with independent maximum")
    write(output / "counterexample_41.json", audit_counterexample_41())
    write(output / "conservative_search_audit.json", {"profiles": cases["conservative"]["params"],
                                                       "observations": search_observations})
    write(output / "audit_record.json", {"provenance": evidence, "witnesses": rows,
                                         "issues": issues, "closure_equals_baseline": not any(
                                             m not in maxima for m in baseline_members)})
    sums = [f"{sha_file(p)}  {p.name}" for p in sorted(output.glob("*.json"))]
    (output / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8", newline="\n")
    print(f"Audited {len(rows)} witnesses; joint trace / provenance findings: {len(issues)}")
    for row in rows:
        print(row["tag"], row["policy"], row["completed"], row["gap"], row["physical_errors"])
    return 1 if issues else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a-delivery", type=Path, required=True)
    parser.add_argument("--a-diagnostic", type=Path, required=True)
    parser.add_argument("--b-primary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
