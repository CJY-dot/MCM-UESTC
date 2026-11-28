"""Build Q2-B sidecars from approved inputs; leave the Q1 bundle untouched."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from openpyxl import load_workbook

from q1_schema import validate_instance, validate_policy_view, validate_simulator_truth
from q2_schema import Q2_PACK, check_policy_parameters, check_sharable, stable_event_seed

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "56736cff4905914e11287253eac5a06cca15587c"
Q1_ASSUMPTIONS = ROOT / "stages/03_implementation/A/q1_assumption_scenarios.json"
PROCESSED_BASE = ROOT / "problem/data/processed"
DEFAULT_INPUT = PROCESSED_BASE / "stage02_b_ladle_identity_approved"
DEFAULT_Q1 = PROCESSED_BASE / "stage03_q1_visibility_verification/final1"
DEFAULT_OUTPUT = PROCESSED_BASE / "stage03_q2_b"
DEFAULT_DELIVERY = ROOT / "local/stage03_q2_b/delivery"
PUBLIC = ROOT / "stages/03_implementation/B"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def semantic_sha(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, value) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True)
        stream.write("\n")


def raw_workbook_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve(strict=True)
    options = list((ROOT / "docs/校赛题目").glob("B*/附数据文件"))
    if len(options) != 1:
        raise ValueError("Pass --raw to select the authorized B workbook directory.")
    return options[0]


def verify_sources(processed: Path, raw: Path, frozen_q1: Path) -> dict:
    """Check the source lineage and approved cells without regenerating Q1."""
    stage02 = read_json(processed / "audit.json")
    records = stage02["workbooks"]
    expected = {row["file"]: row["sha256"] for row in records}
    present = {p.name for p in raw.iterdir() if p.is_file() and p.suffix.lower() in {".xls", ".xlsx"}}
    if len(records) != 12 or len(expected) != 12 or set(expected) != present:
        raise ValueError("Authorized workbook roster differs from the approved 12 workbooks.")
    raw_hashes = {name: sha_file(raw / name) for name in expected}
    if raw_hashes != expected:
        raise ValueError("Workbook hash mismatch against approved Stage 02 audit.")
    approvals = stage02["identity_corrections"]
    if len(approvals) != 3 or not stage02["correction_comparison"]["original_workbooks_unchanged"]:
        raise ValueError("Expected the three independently approved corrections.")
    cells = set()
    norms = set()
    approved_rows = {}
    for entry in approvals:
        source = entry["source"]
        name, sheet, row, column = (source[k] for k in ("workbook", "sheet", "row", "column"))
        cells.add((name, sheet, row, column))
        norms.add((entry["heat_id"], entry["normalized_value"]))
        approved_rows[name, sheet, str(row), entry["heat_id"]] = entry["normalized_value"]
        if (entry["status"] != "project_approved" or not entry["authority_ref"]
                or source["sha256"] != raw_hashes.get(name)
                or not entry["correction_reason"] or not entry["normalized_value"]):
            raise ValueError("Incomplete or stale identity approval.")
        book = load_workbook(raw / name, read_only=True, data_only=True)
        try:
            ws = book[sheet]
            # Some source sheets have stale dimension metadata.
            ws.reset_dimensions()
            observed = ws.cell(row, column).value
        finally:
            book.close()
        if str(observed).strip() != entry["raw_value"]:
            raise ValueError("The original value of an approved identity cell has changed.")
    if len(cells) != 3 or len(norms) != 1:
        raise ValueError("Approval scope does not match the documented three-cell normalization.")
    normalized_seen = set()
    with (processed / "events.csv").open(encoding="utf-8-sig", newline="") as stream:
        for event in csv.DictReader(stream):
            key = tuple(event[name] for name in ("source_file", "source_sheet", "source_row", "heat_id"))
            if key in approved_rows:
                if event["ladle_id"] != approved_rows[key]:
                    raise ValueError("The approved event ledger contains an inconsistent normalized identity.")
                normalized_seen.add(key)
    if normalized_seen != set(approved_rows):
        raise ValueError("An approved source row is missing from the frozen event ledger.")

    stage02_hashes = {f: sha_file(processed / f) for f in ("audit.json", "events.csv", "tasks.csv", "calibration.csv")}
    q1_inputs = {
        "q1_instance.json": validate_instance,
        "q1_policy_view.json": validate_policy_view,
        "q1_simulator_truth.json": validate_simulator_truth,
    }
    q1_blobs = {name: read_json(frozen_q1 / name) for name in q1_inputs}
    for name, validator in q1_inputs.items():
        if validator(q1_blobs[name]):
            raise ValueError(f"Stored Q1 package fails its existing validator: {name}")
    q1 = q1_blobs["q1_instance.json"]
    refs = q1["evidence"]["source_hashes"]
    for name, key in (("audit.json", "audit_json"), ("events.csv", "events_csv"), ("tasks.csv", "tasks_csv")):
        if refs[key] != stage02_hashes[name]:
            raise ValueError("Q1 lineage does not match the approved Stage 02 inputs.")
    if refs["workbooks"] != raw_hashes:
        raise ValueError("Q1 workbook fingerprints do not match the checked workbooks.")
    q1_hashes = {name: sha_file(frozen_q1 / name) for name in q1_inputs}
    archived_c = read_json(ROOT / "stages/03_implementation/C/Q1_REPRODUCTION_RECORD.json")
    if q1_hashes != archived_c["input_fingerprints"]["b_primary_file_sha256"]:
        raise ValueError("Q1 bundle differs from the C-audited frozen primary input.")
    return {
        "approved_stage02_sha256": stage02_hashes, "workbook_sha256": raw_hashes,
        "workbooks_verified": len(raw_hashes), "approved_cells_verified": len(cells),
        "identity_approval_audit_sha256": stage02_hashes["audit.json"],
        "q1_base_instance": q1["instance_id"], "q1_bundle_sha256": q1_hashes,
        "q1_builder_version": q1["evidence"]["builder_version"],
        "q1_builder_sha256": sha_file(Path(__file__).with_name("q1_builder.py")),
        "q1_window_index_sha256": sha_file(frozen_q1 / "window_candidates.json"),
        "q1_assumptions_semantic_sha256": semantic_sha(read_json(Q1_ASSUMPTIONS)),
    }


def protected_path(path: Path) -> Path:
    path = path.resolve()
    if not any(parent.resolve() in path.parents for parent in (PROCESSED_BASE, ROOT / "local")):
        raise ValueError("Q2 private outputs must be below local/ or problem/data/processed/.")
    relative = path.relative_to(ROOT.resolve()).as_posix()
    ignored = subprocess.run(["git", "check-ignore", "-q", "--", relative], cwd=ROOT)
    tracked = subprocess.run(["git", "ls-files", "--", relative], cwd=ROOT,
                             check=True, capture_output=True)
    if ignored.returncode != 0 or tracked.stdout or path.exists():
        raise ValueError("Choose a new, untracked, Git-ignored directory for private artifacts.")
    return path


def write_manifest(directory: Path) -> None:
    files = sorted(p for p in directory.rglob("*") if p.is_file() and p.name != "SHA256SUMS.txt")
    manifest = [f"{sha_file(p)}  {p.relative_to(directory).as_posix()}" for p in files]
    (directory / "SHA256SUMS.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8", newline="\n")


def create_package(args) -> dict:
    from q2_external import external_boundary
    from q2_motion import motion_profile
    from q2_statistics import calibrate
    from q2_windows import window_manifest

    output, delivery = protected_path(args.output), protected_path(args.delivery)
    if output == delivery or output in delivery.parents or delivery in output.parents:
        raise ValueError("Use separate, non-nested calibration and delivery directories.")
    if args.publish_summary and args.publish_summary.resolve() != PUBLIC.resolve():
        raise ValueError("Publication is limited to Stage 03 B's agreed directory.")
    raw = raw_workbook_dir(args.raw)
    fingerprints = verify_sources(args.processed, raw, args.q1)
    q1_assumptions = read_json(Q1_ASSUMPTIONS)
    output.mkdir(parents=True)
    delivery.mkdir(parents=True)
    calibration, policy, statistical_audit = calibrate(args.processed, output)
    check_policy_parameters(policy)
    choices = {key: statistical_audit["strata"][key]["validation_selected"] for key in policy["strata"]}
    calibration["model_selection"] = choices
    motion = motion_profile(q1_assumptions)
    external = external_boundary(args.processed, calibration)
    windows = window_manifest(args.processed, args.q1 / "window_candidates.json", q1_assumptions, output)
    parameter_pack = {
        "schema": Q2_PACK, "basis": BASELINE, "status": "conditional_pending_approval",
        "parent_schema": "mcm-uestc.dispatch/1",
        "policy_parameters": policy, "motion_profile": motion, "external_boundary": external,
        "model_selection": {"source": "validation_selection_without_final_holdout", "families": choices},
        "window_manifest_schema": windows["schema"],
        "sidecar_protocol": {
            "stable_draw_key_fields": ["replicate_id", "heat_id", "process_stage", "event_id", "device_id"],
            "seed_encoding": "First 16 SHA-256 bytes as a big-endian integer of compact UTF-8 JSON "
                             "[master_seed,replicate_id,heat_id,process_stage,event_id,device_id]",
            "independent_seed_test_vector_hex": f"{stable_event_seed(123, 1, 'H', 'CC', 'E', 'CC3'):032x}",
            "block_draw": "Choose donor using a frozen group key and map events to fixed ranks before dispatch.",
            "exposure": "Policy receives training prediction parameters and visible simulator notices only.",
            "no_draws_created": True,
        },
        "inputs": {
            "approved_calibration_sha256": fingerprints["approved_stage02_sha256"]["calibration.csv"],
            "approved_identity_audit_sha256": fingerprints["identity_approval_audit_sha256"],
            "q1_bundle_sha256": fingerprints["q1_bundle_sha256"],
        },
    }
    audit = {
        "schema": "mcm-uestc.q2-audit/1", "baseline": BASELINE, "provenance": fingerprints,
        "statistics": statistical_audit,
        "code_sha256": {
            name: hashlib.sha256(Path(__file__).with_name(name).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            for name in ("q2_calibration.py", "q2_schema.py", "q2_statistics.py", "q2_motion.py",
                         "q2_external.py", "q2_windows.py")
        },
        "policy_parameter_sha256": semantic_sha(policy),
        "q2_policy_inputs_do_not_include_realized_draws": True,
    }
    check_sharable(parameter_pack)
    check_sharable(windows)
    artifacts = {
        "q2_calibration.json": calibration, "q2_policy_parameters.json": policy,
        "q2_motion_profile.json": motion, "q2_external_boundary.json": external,
        "q2_window_manifest.json": windows, "q2_audit.json": audit,
    }
    for filename, blob in artifacts.items():
        write_json(output / filename, blob)
        shutil.copyfile(output / filename, delivery / filename)
    write_manifest(output)
    write_manifest(delivery)
    if args.publish_summary:
        write_json(PUBLIC / "q2_parameter_pack.json", parameter_pack)
        write_json(PUBLIC / "q2_window_manifest.json", windows)
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--q1", type=Path, default=DEFAULT_Q1)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--delivery", type=Path, default=DEFAULT_DELIVERY)
    parser.add_argument("--publish-summary", type=Path)
    args = parser.parse_args()
    audit = create_package(args)
    print(f"Q2-B sidecars built after verifying {audit['provenance']['workbooks_verified']} workbooks "
          f"and {audit['provenance']['approved_cells_verified']} approved cells.")


if __name__ == "__main__":
    main()
