#!/usr/bin/env python3
"""Run the Consumer Defensive four-layer promotion engine.

The command is intentionally report-only.  It writes a hash-bound decision,
an activation-registry candidate, and (optionally) an activated rank-table
candidate.  It never edits Portfolio Layer configuration or a database; the
registry must be independently reviewed and pinned by path+SHA before the
Portfolio adapter can consume it.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.promotion_artifacts_v3 import (  # noqa: E402
    publish_immutable_json,
)
from consumer_defensive.core.promotion_engine_v3 import (  # noqa: E402
    apply_activation_to_rank_rows,
    build_activation_registry,
    build_promotion_decision,
    load_framework,
    validate_promotion_decision,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--framework",
        type=Path,
        default=ROOT
        / "consumer_defensive/data/consumer_defensive_promotion_framework_v3.yaml",
    )
    parser.add_argument("--promotion-input", type=Path, required=True)
    parser.add_argument("--previous-decision", type=Path)
    parser.add_argument("--trusted-previous-decision-sha256")
    parser.add_argument("--previous-promotion-input", type=Path)
    parser.add_argument("--preregistration", type=Path)
    parser.add_argument("--registration-anchor", type=Path)
    parser.add_argument("--trusted-registration-anchor-sha256")
    parser.add_argument("--fresh-evidence-manifest", type=Path)
    parser.add_argument("--rank-table", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"{label} path is missing or unsafe: {resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must contain a JSON object")
    return dict(payload)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"rank table path is missing or unsafe: {resolved}")
    with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("rank table has no header")
        return list(reader.fieldnames), [dict(row) for row in reader]


def _publish_immutable_csv(
    path: Path, *, original_fields: list[str], rows: list[dict[str, Any]]
) -> None:
    added = sorted({key for row in rows for key in row} - set(original_fields))
    fields = [*original_fields, *added]
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists():
        raise FileExistsError(f"refusing to overwrite immutable artifact: {resolved}")
    temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    args = _parser().parse_args()
    def optional_json(path: Path | None, label: str) -> dict[str, Any] | None:
        return None if path is None else _read_json(path, label=label)

    framework = load_framework(args.framework)
    promotion_input = _read_json(args.promotion_input, label="--promotion-input")
    previous = (
        None
        if args.previous_decision is None
        else _read_json(args.previous_decision, label="--previous-decision")
    )
    previous_input = optional_json(
        args.previous_promotion_input, "--previous-promotion-input"
    )
    preregistration = optional_json(args.preregistration, "--preregistration")
    registration_anchor = optional_json(
        args.registration_anchor, "--registration-anchor"
    )
    fresh_manifest = optional_json(
        args.fresh_evidence_manifest, "--fresh-evidence-manifest"
    )
    evidence_kwargs = {
        "trusted_previous_decision_sha256": args.trusted_previous_decision_sha256,
        "previous_promotion_input": previous_input,
        "preregistration": preregistration,
        "registration_anchor": registration_anchor,
        "trusted_registration_anchor_sha256": args.trusted_registration_anchor_sha256,
        "fresh_evidence_manifest": fresh_manifest,
    }
    decision = build_promotion_decision(
        promotion_input=promotion_input,
        framework=framework,
        previous_decision=previous,
        **evidence_kwargs,
    )
    validate_promotion_decision(
        decision,
        promotion_input=promotion_input,
        framework=framework,
        previous_decision=previous,
        **evidence_kwargs,
    )
    registry = build_activation_registry(
        decision=decision,
        promotion_input=promotion_input,
        framework=framework,
        previous_decision=previous,
        **evidence_kwargs,
    )
    output = args.output_dir.expanduser().resolve()
    publish_immutable_json(
        output / "consumer_defensive_promotion_decision_v3.json", decision
    )
    publish_immutable_json(
        output / "consumer_defensive_activation_registry_v3.json", registry
    )
    activated_path: str | None = None
    if args.rank_table is not None:
        fields, rows = _read_csv(args.rank_table)
        activated = apply_activation_to_rank_rows(
            rows, activation_registry=registry
        )
        destination = output / "consumer_defensive_activated_rank_table_v3.csv"
        _publish_immutable_csv(
            destination,
            original_fields=fields,
            rows=activated,
        )
        activated_path = str(destination)
    summary = {
        "schema_version": "consumer_defensive_promotion_run_summary_v3",
        "status": "PASS",
        "model_family": "consumer_defensive",
        "asof_date": decision["asof_date"],
        "decision_sha256": decision["payload_sha256"],
        "activation_registry_sha256": registry["payload_sha256"],
        "states": {
            cohort: item["state"] for cohort, item in decision["cohorts"].items()
        },
        "optimizer_caps": {
            cohort: item["optimizer_cap"]
            for cohort, item in registry["cohorts"].items()
        },
        "calibration_write_performed": False,
        "portfolio_write_performed": False,
        "registry_requires_explicit_portfolio_pinning": True,
        "activated_rank_table": activated_path,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
