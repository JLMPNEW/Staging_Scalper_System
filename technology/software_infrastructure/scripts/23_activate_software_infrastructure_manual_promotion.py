#!/usr/bin/env python3
"""Create the immutable receipt for the approved software Stage 8 override."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.calibration_governance import sha256_file  # noqa: E402
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.promotion_governance import (  # noqa: E402
    create_manual_economic_override_receipt,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SCORING_KEY = "software_infrastructure_calibrated_scoring"
ROLLBACK_KEY = "software_infrastructure_stage8_v1_rollback_shadow_scoring"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--effective-date", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approval-note", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return value


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    optuna_dir = resolve_path(
        cfg_get(
            config,
            "software_infrastructure_optuna_calibration.output_dir",
            "../output/technology_reports/software_infrastructure/optuna_calibration",
        ),
        base_dir=base_dir,
    )
    decision_path = resolve_path(
        "../output/technology_reports/consolidated_promotion/software_infrastructure_promotion_decision.json",
        base_dir=base_dir,
    )
    stage8_weights_path = optuna_dir / "stage8_best_weights.json"
    candidate = read_json(stage8_weights_path)
    current = cfg_get(config, SCORING_KEY, {}) or {}
    if not isinstance(current, dict):
        raise RuntimeError(f"Missing scoring config: {SCORING_KEY}")
    production_weights = {
        "component_weights": candidate.get("component_weights") or {},
        "subfeature_weights": candidate.get("subfeature_weights") or {},
    }
    rollback_weights = {
        "component_weights": current.get("component_weights") or {},
        "subfeature_weights": current.get("subfeature_weights") or {},
    }
    probation = {
        "schema_version": "software_infrastructure_promotion_probation_v1",
        "effective_date": args.effective_date,
        "required_trading_sessions": 21,
        "benchmark_ticker": "QQQ",
        "portfolio_name": "top_quintile",
        "weight_method": "equal_weight",
        "transaction_cost_bps_per_side": 20.0,
        "decision_rule": "keep_if_promoted_net_return_gte_rollback_else_revert_recommended",
        "automatic_reversion": False,
        "production_source_id": str(current.get("source_id") or "software_infrastructure_calibrated_score_v1"),
        "rollback_source_id": "software_infrastructure_stage8_v1_rollback_shadow_score",
    }
    output_path = args.output.expanduser().resolve()
    receipt = create_manual_economic_override_receipt(
        family="software_infrastructure",
        model_version=args.model_version,
        effective_date=args.effective_date,
        approved_by=args.approved_by,
        approval_note=args.approval_note,
        stage8_manifest_path=optuna_dir / "stage8_run_manifest.json",
        walk_forward_manifest_path=optuna_dir / "walk_forward" / "walk_forward_run_manifest.json",
        stage8_weights_path=stage8_weights_path,
        consolidated_decision_path=decision_path,
        production_weights=production_weights,
        rollback_weights=rollback_weights,
        rollback_scoring_config_key=ROLLBACK_KEY,
        probation_contract=probation,
        output_path=output_path,
    )
    print(json.dumps({
        "receipt_path": str(output_path),
        "receipt_sha256": sha256_file(output_path),
        "model_version": receipt["model_version"],
        "effective_date": receipt["effective_date"],
        "decision_type": receipt["decision_type"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
