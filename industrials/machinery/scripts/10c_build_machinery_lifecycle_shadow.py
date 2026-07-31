#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.machinery.lifecycle_policy import (  # noqa: E402
    SHADOW_FIELDS,
    evaluate_lifecycle_shadow,
    file_sha256,
    load_lifecycle_policy,
    validate_lifecycle_policy,
)
from industrials.machinery.scoring import parse_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a non-production comparison of operating-only and "
            "lifecycle-v1 machinery eligibility."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "") for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    dashboard_root = resolve_path(
        cfg_get(config, "machinery_scoring.dashboard_root"),
        base_dir=base_dir,
    )
    input_path = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else dashboard_root / asof / "machinery_final_rank_table.csv"
    )
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    output_root = resolve_path(
        cfg_get(
            config,
            "machinery_lifecycle.output_root",
            "../../output/industrials/machinery/lifecycle",
        ),
        base_dir=base_dir,
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else output_root / asof
    )
    policy = load_lifecycle_policy(config, config_path=config_path)
    validation = validate_lifecycle_policy(policy)
    if validation["acceptance"] != "PASS":
        raise ValueError(
            "Machinery lifecycle policy is invalid: "
            + ";".join(validation["issues"])
        )
    rows = _read_rows(input_path)
    shadow = evaluate_lifecycle_shadow(rows, asof=asof, policy=policy)
    output_path = output_dir / "machinery_lifecycle_shadow_universe.csv"
    manifest_path = output_dir / "machinery_lifecycle_shadow_manifest.json"
    write_csv_atomic(output_path, SHADOW_FIELDS, shadow)
    operating_count = sum(
        row["operating_only_eligible_flag"] == "1" for row in shadow
    )
    lifecycle_count = sum(
        row["lifecycle_universe_eligible_flag"] == "1" for row in shadow
    )
    changed = [
        row["ticker"]
        for row in shadow
        if row["eligibility_changed_flag"] == "1"
    ]
    class_counts: dict[str, int] = {}
    for row in shadow:
        lifecycle_class = row["lifecycle_class"]
        class_counts[lifecycle_class] = (
            class_counts.get(lifecycle_class, 0) + 1
        )
    manifest = {
        "acceptance": "PASS",
        "artifact_family": "machinery_lifecycle_shadow_universe",
        "policy_version": policy.policy_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asof_date": asof,
        "input_csv": str(input_path.resolve()),
        "input_csv_sha256": file_sha256(input_path),
        "row_count": len(shadow),
        "rank_ready_count": sum(
            row["rank_ready_flag"] == "1" for row in shadow
        ),
        "operating_only_eligible_count": operating_count,
        "lifecycle_eligible_count": lifecycle_count,
        "eligibility_changed_count": len(changed),
        "eligibility_changed_tickers": changed,
        "lifecycle_class_counts": class_counts,
        "output_csv": str(output_path.resolve()),
        "output_csv_sha256": file_sha256(output_path),
        "production_policy_changed": False,
        "calibration_cohort_changed": False,
    }
    write_text_atomic(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
