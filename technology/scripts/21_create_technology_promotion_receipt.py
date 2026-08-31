#!/usr/bin/env python3
"""Create a manual, immutable receipt for an eligible technology candidate."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.optuna_artifact_governance import FAMILIES, validate_stage8, validate_walk_forward  # noqa: E402
from technology.core.promotion_governance import create_promotion_receipt  # noqa: E402
from technology.core.calibration_governance import sha256_file  # noqa: E402


GOVERNANCE_KEYS = {
    "semiconductors": "semiconductor_governance_reports",
    "software_infrastructure": "software_infrastructure_governance_reports",
    "technology_hardware": "technology_hardware_governance_reports",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PACKAGE_ROOT / "config.yaml")
    parser.add_argument("--family", choices=sorted(FAMILIES), required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--effective-date", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approval-note", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    spec = FAMILIES[args.family]
    output_dir = resolve_path(cfg_get(config, f"{spec.config_key}.output_dir", spec.default_output), base_dir=config_path.parent)
    errors = [*validate_stage8(args.family, config_path=config_path), *validate_walk_forward(args.family, config_path=config_path)]
    if errors:
        raise RuntimeError("Calibration artifacts are not promotion-ready:\n- " + "\n- ".join(errors))
    current_version = str(
        cfg_get(config, f"oos_calibration_standards.families.{args.family}.production_model_version", "") or ""
    )
    if args.model_version == current_version:
        raise RuntimeError("A newly promoted model must use a new production_model_version.")
    governance_dir = resolve_path(
        cfg_get(config, f"{GOVERNANCE_KEYS[args.family]}.output_dir"),
        base_dir=config_path.parent,
    )
    receipt_path = governance_dir / "promotion_receipts" / f"{args.model_version}_{args.effective_date}.json"
    stage8_path = output_dir / "stage8_best_weights.json"
    stage8 = json.loads(stage8_path.read_text(encoding="utf-8"))
    receipt = create_promotion_receipt(
        family=args.family,
        model_version=args.model_version,
        effective_date=args.effective_date,
        approved_by=args.approved_by,
        approval_note=args.approval_note,
        stage8_manifest_path=output_dir / "stage8_run_manifest.json",
        walk_forward_manifest_path=output_dir / "walk_forward" / "walk_forward_run_manifest.json",
        stage8_weights_path=stage8_path,
        production_weights=stage8,
        output_path=receipt_path,
    )
    print(
        json.dumps(
            {
                "receipt_path": str(receipt_path),
                "receipt_sha256": sha256_file(receipt_path),
                "model_version": receipt["model_version"],
                "effective_date": receipt["effective_date"],
                "required_config_updates": {
                    "production_model_version": receipt["model_version"],
                    "production_model_effective_date": receipt["effective_date"],
                    "calibration_lock_date": receipt["effective_date"],
                    "calibration_production_start_date": receipt["effective_date"],
                    "active_promotion_receipt_path": str(receipt_path),
                    "active_promotion_receipt_sha256": sha256_file(receipt_path),
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
