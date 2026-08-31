#!/usr/bin/env python3
"""Validate the software promotion receipt and forward-probation outputs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.promotion_governance import resolve_production_binding  # noqa: E402
from technology.software_infrastructure.promotion_probation import sha256_file  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    cfg = cfg_get(config, "software_infrastructure_promotion_probation", {}) or {}
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg.get("output_dir"), base_dir=base_dir
    )
    status_path = output_dir / "software_infrastructure_promotion_probation_status.json"
    manifest_path = output_dir / "software_infrastructure_promotion_probation_manifest.json"
    errors: list[str] = []
    for path in (status_path, manifest_path):
        if not path.exists() or path.stat().st_size == 0:
            errors.append(f"Missing probation artifact: {path}")
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    binding = resolve_production_binding(
        config,
        config_path=config_path,
        family="software_infrastructure",
        governance_config_key="software_infrastructure_governance_reports",
    )
    if not binding.valid:
        errors.append(f"Invalid production binding: {binding.status} {list(binding.reasons)}")
    if int(cfg.get("required_trading_sessions") or 0) != 21:
        errors.append("Probation must require exactly 21 trading sessions.")
    if bool(cfg.get("automatic_reversion")) or bool(status.get("automatic_reversion")):
        errors.append("Probation must not automatically mutate production.")
    if status.get("production_model_version") != cfg.get("production_model_version"):
        errors.append("Probation production model version mismatch.")
    if status.get("rollback_model_version") != cfg.get("rollback_model_version"):
        errors.append("Probation rollback model version mismatch.")
    expected_receipt_hash = str(
        cfg_get(config, "software_infrastructure_governance_reports.active_promotion_receipt_sha256", "")
    )
    if status.get("receipt_sha256") != expected_receipt_hash or binding.receipt_sha256 != expected_receipt_hash:
        errors.append("Probation receipt hash does not match the active production binding.")
    run_status = str(status.get("status") or "")
    if run_status not in {"scheduled", "awaiting_scores", "awaiting_entry_price", "monitoring", "data_quality_hold", "complete"}:
        errors.append(f"Unexpected probation status: {run_status!r}")
    completed = int(status.get("completed_return_sessions") or 0)
    if run_status == "complete":
        if completed < 21:
            errors.append("Probation completed before 21 return sessions.")
        if status.get("decision") not in {"keep_promoted_model", "revert_to_v1_recommended"}:
            errors.append(f"Unexpected completed probation decision: {status.get('decision')!r}")
    holdings_path = output_dir / "software_infrastructure_promotion_probation_holdings.csv"
    seal_path = output_dir / "software_infrastructure_promotion_probation_holdings_seal.json"
    if holdings_path.exists():
        seal = json.loads(seal_path.read_text(encoding="utf-8")) if seal_path.exists() else {}
        if seal.get("holdings_sha256") != sha256_file(holdings_path):
            errors.append("Probation holdings seal mismatch.")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(json.dumps({"status": "PASS", "probation_status": run_status, "production_binding": binding.status}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
