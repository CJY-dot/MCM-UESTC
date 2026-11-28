"""Q2 sidecar checks and stable event-key encoding."""

from __future__ import annotations

import hashlib
import json
import math
import re

Q2_PACK = "mcm-uestc.q2-parameter-pack/1"
POLICY_SCHEMA = "mcm-uestc.q2-policy-parameters/1"
DISALLOWED_FIELDS = {
    "heat_id", "heat_ids", "task_id", "task_ids", "ladle_id", "ladle_ids",
    "event_id", "event_ids", "campaign_id", "campaign_ids", "source_row", "source_ref",
    "source_start", "source_end", "source_plan_duration", "raw_value", "normalized_value",
    "timestamp", "absolute_timestamp", "actual_start_s", "actual_end_s", "observed_lift_s",
    "observed_arrival_s", "future_events", "task_actuals", "realized_draws",
    "simulator_truth", "donor_samples", "donors", "donor_rows", "sample_id", "sample_ids",
    "holdout_rows", "validation_rows",
}
POLICY_ONLY_PROHIBITED = {"validation", "holdout", "final_test", "final_holdout", "evaluations",
                          "sample_pool", "bootstrap_pool", "ordered_blocks", "train_blocks"}
ABSOLUTE_VALUE = re.compile(r"(?i)(?:^[a-z]:[\\/]|^\\\\|\b20\d\d-\d\d-\d\d\b)")


def stable_event_seed(master_seed: int, replicate_id: int, heat_id: str, process_stage: str,
                      event_id: str, device_id: str) -> int:
    """Return a deterministic seed; no event outcome or generator state is produced."""
    if type(master_seed) is not int or type(replicate_id) is not int or replicate_id < 0:
        raise ValueError("Use integers for the master seed and nonnegative replicate ID.")
    names = (heat_id, process_stage, event_id, device_id)
    if any(not isinstance(v, str) or not v for v in names):
        raise ValueError("Stable event keys require four nonempty text identifiers.")
    payload = json.dumps((master_seed, replicate_id, *names), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:16], "big")


def check_sharable(value, *, policy=False, path="root") -> None:
    """Reject raw identities, addresses, realized values and row payloads."""
    if isinstance(value, dict):
        banned = DISALLOWED_FIELDS | POLICY_ONLY_PROHIBITED if policy else DISALLOWED_FIELDS
        for field, content in value.items():
            if field.lower() in banned:
                raise ValueError(f"Private or out-of-role field at {path}.{field}.")
            if ABSOLUTE_VALUE.search(field):
                raise ValueError(f"Private path or calendar date in a key at {path}.")
            check_sharable(content, policy=policy, path=f"{path}.{field}")
    elif isinstance(value, (list, tuple)):
        for content in value:
            check_sharable(content, policy=policy, path=f"{path}[]")
    elif isinstance(value, str):
        if ABSOLUTE_VALUE.search(value):
            raise ValueError(f"Private path or calendar date at {path}.")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Non-finite value at {path}.")


def check_policy_parameters(policy: dict) -> None:
    if policy.get("schema") != POLICY_SCHEMA or policy.get("fit_source") != "train_only":
        raise ValueError("Unsupported Q2 policy-parameter sidecar.")
    check_sharable(policy, policy=True)
    for record in policy["strata"].values():
        if record["basis"] not in {"paired_plan_ratio", "historical_seconds"} or record["training_n"] < 1:
            raise ValueError("Invalid training stratum.")
        for forecast in record["predictive_summary"].values():
            mean, quantiles = forecast["mean"], forecast["quantiles"]
            if mean <= 0 or any(q <= 0 for q in quantiles.values()):
                raise ValueError("Duration predictions must be strictly positive.")
            if record["basis"] == "paired_plan_ratio" and abs(mean - 1) > 1e-10:
                raise ValueError("Normalized duration ratios must have unit mean.")
