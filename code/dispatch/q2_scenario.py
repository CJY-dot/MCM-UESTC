"""Verified Q2 supplement, with the existing Q1 scenario projection unchanged."""

from pathlib import Path
import hashlib
import copy
import sys

from q1_scenario import ROOT, build_case, project_batch, read_json, digest
from q1_schema import validate_instance
from q2_schema import check_policy_parameters, check_sharable

# Q1 locates its data schema by prepending code/data. Prefer A's motion module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

MERGE_BASE = "6d8aa34bbfb3bf4e60ac84da8305666138978b7c"
B_COMMIT = "0e81b5d05c2aa7234680a127ac890a1bf2e925de"
B_PUBLIC = ROOT / "stages/03_implementation/B"
ASSUMPTIONS = ROOT / "stages/03_implementation/A/q1_assumption_scenarios.json"
DELIVERY = ROOT / "local/stage03_q2_b/delivery"
VERSIONS = {
    "calibration": "calibration",
    "policy_parameters": "policy-parameters",
    "motion_profile": "motion-profile",
    "external_boundary": "external-boundary",
    "window_manifest": "window-manifest",
    "audit": "audit",
}


def file_hash(path, normalize=False):
    data = path.read_bytes()
    if normalize:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def manifest(directory, name="SHA256SUMS.txt"):
    rows = (directory / name).read_text(encoding="utf-8").splitlines()
    result = {}
    for row in rows:
        sha, file = row.split("  ", 1)
        path = (directory / file).resolve()
        if directory.resolve() not in path.parents or file in result:
            raise ValueError("Invalid or repeated manifest member")
        result[file] = sha
    return result


def load_b(delivery=DELIVERY):
    """Private donor models and policy summaries have separate consumers."""
    private = manifest(delivery)
    required = {f"q2_{key}.json" for key in VERSIONS}
    if set(private) != required or any(file_hash(delivery / p) != sha for p, sha in private.items()):
        raise ValueError("B private sidecars do not match the delivery manifest")
    merged = manifest(ROOT, "stages/03_implementation/B/Q2_SHA256SUMS.txt")
    if any(file_hash(ROOT / p, normalize=True) != sha for p, sha in merged.items()):
        raise ValueError("Q2 public delivery differs from its Git manifest")
    source = {key: read_json(delivery / f"q2_{key}.json") for key in VERSIONS}
    for key, suffix in VERSIONS.items():
        if source[key]["schema"] != f"mcm-uestc.q2-{suffix}/1":
            raise ValueError(f"Unexpected Q2 schema for {key}")
    public = read_json(B_PUBLIC / "q2_parameter_pack.json")
    if public["parent_schema"] != "mcm-uestc.dispatch/1":
        raise ValueError("B sidecar does not extend the frozen input")
    for name in ("policy_parameters", "motion_profile", "external_boundary"):
        if public[name] != source[name]:
            raise ValueError(f"Local {name} differs from approved delivery")
    if public["model_selection"]["families"] != source["calibration"]["model_selection"]:
        raise ValueError("Validation-selected models differ across sidecars")
    if source["window_manifest"] != read_json(B_PUBLIC / "q2_window_manifest.json"):
        raise ValueError("Unexpected window manifest")
    if source["external_boundary"]["independent_exogenous_timestamp_models_enabled"]:
        raise ValueError("Independent boundary draws were outside this experiment approval")
    check_policy_parameters(source["policy_parameters"])
    check_sharable(public)
    if digest(source["policy_parameters"]) != source["audit"]["policy_parameter_sha256"]:
        raise ValueError("Policy predictions differ from B's audit")
    source["hashes"] = {"private": private, "public": merged}
    return source


def load_window(path, label, source):
    """Use a frozen offline roster and explicit plan. No history queues or release proxies."""
    files = ("q1_instance.json", "q1_policy_view.json")
    full, visible = (read_json(path / fn) for fn in files)
    if full["instance_id"] != f"stage03-q1-b-{label}" or validate_instance(full):
        raise ValueError(f"{label}: invalid or incomplete Q1 builder output")
    audit = source["audit"]["provenance"]
    hashes = full["evidence"]["source_hashes"]
    for passed, approved in (("events_csv", "events.csv"), ("tasks_csv", "tasks.csv"),
                             ("audit_json", "audit.json")):
        if hashes[passed] != audit["approved_stage02_sha256"][approved]:
            raise ValueError(f"{label}: upstream approval differs from B's Q2 calibration")
    if hashes["workbooks"] != audit["workbook_sha256"]:
        raise ValueError(f"{label}: unexpected original workbook fingerprints")
    case = build_case(project_batch(full, visible), read_json(ASSUMPTIONS), "base")
    return case, {fn: file_hash(path / fn) for fn in files}


def condition(profile, scale="base", layout="dispersed", motion="nominal", clearance="base"):
    chosen = copy.deepcopy(profile["scenarios"][scale])
    chosen["initial_layout"] = layout
    chosen["motion"]["mobile_450t"] = profile["kinematic_sensitivities"][motion]
    for pair in chosen["clearance"]["adjacent_pairs"]:
        pair["min_distance_m"] = chosen["clearance"]["threshold_sensitivities_m"][clearance]
    return chosen
