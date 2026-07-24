#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.machinery.scoring import (  # noqa: E402
    FINAL_RANK_FIELDS,
    file_sha256,
    parse_asof,
    read_rows,
    validate_rank_rows,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate machinery dashboard and calibration sidecar artifacts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--input-dir", type=Path, default=None)
    return parser.parse_args()


def validate_dashboard_artifacts(input_dir: Path, *, asof: str) -> tuple[list[str], int]:
    rank_path = input_dir / "machinery_final_rank_table.csv"
    sidecar_path = input_dir / "machinery_stage11_survivorship_calibration_panel.csv"
    manifest_path = input_dir / "machinery_final_rank_table_manifest.json"
    errors: list[str] = []
    try:
        rank_rows = read_rows(rank_path)
    except (OSError, ValueError) as exc:
        return [f"invalid rank table: {exc}"], 0
    errors.extend(validate_rank_rows(rank_rows, asof=asof))
    if rank_rows and set(rank_rows[0]) != set(FINAL_RANK_FIELDS):
        errors.append("final rank table schema differs from final rank contract")
    try:
        sidecar = read_rows(sidecar_path)
    except (OSError, ValueError) as exc:
        errors.append(f"invalid calibration sidecar: {exc}")
        sidecar = []
    if not sidecar:
        errors.append("calibration sidecar is empty")
    else:
        errors.extend(validate_rank_rows(sidecar, asof=asof))
        if set(sidecar[0]) != set(FINAL_RANK_FIELDS):
            errors.append("calibration sidecar schema differs from final rank contract")
        rank_identity = [(row.get("ticker"), row.get("final_rank")) for row in rank_rows]
        sidecar_identity = [(row.get("ticker"), row.get("final_rank")) for row in sidecar]
        if sidecar_identity != rank_identity:
            errors.append("calibration sidecar ticker/rank identity differs from final rank table")
    for row in sidecar:
        if row.get("survivorship_corrected_panel_flag") != "1":
            errors.append(f"{row.get('ticker')}: sidecar survivorship_corrected_panel_flag must be 1")
        if row.get("stage11_calibration_input_eligible_flag") == "1" and row.get("calibration_sample_role") != "pre_lock_research":
            errors.append(f"{row.get('ticker')}: eligible sidecar row must be pre_lock_research")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid manifest: {exc}")
        manifest = {}
    if manifest.get("acceptance") != "PASS":
        errors.append("manifest acceptance must be PASS")
    if manifest.get("model_family") != "machinery":
        errors.append("manifest model_family must be machinery")
    if manifest.get("asof_date") != asof:
        errors.append(f"manifest asof_date={manifest.get('asof_date')!r} expected={asof}")
    expected_manifest_values = {
        "row_count": len(rank_rows),
        "rank_ready_count": sum(row.get("rank_ready_flag") == "1" for row in rank_rows),
        "portfolio_candidate_count": 0,
        "sidecar_calibration_eligible_count": sum(
            row.get("stage11_calibration_input_eligible_flag") == "1" for row in sidecar
        ),
    }
    for field, expected in expected_manifest_values.items():
        if manifest.get(field) != expected:
            errors.append(f"manifest {field}={manifest.get(field)!r} expected={expected}")
    if manifest.get("contract_fields") != FINAL_RANK_FIELDS:
        errors.append("manifest contract_fields differ from final rank contract")
    expected_versions = sorted({row.get("scoring_contract_version", "") for row in rank_rows})
    if manifest.get("scoring_contract_versions") != expected_versions:
        errors.append("manifest scoring_contract_versions differ from rank table")
    if manifest.get("rank_table_sha256") != file_sha256(rank_path):
        errors.append("rank table hash does not match manifest")
    if sidecar_path.exists() and manifest.get("sidecar_sha256") != file_sha256(sidecar_path):
        errors.append("sidecar hash does not match manifest")
    return errors, len(sidecar)


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    dashboard_root = resolve_path(cfg_get(config, "machinery_scoring.dashboard_root"), base_dir=base_dir)
    input_dir = args.input_dir.expanduser().resolve() if args.input_dir else dashboard_root / asof
    errors, row_count = validate_dashboard_artifacts(input_dir, asof=asof)
    summary = {
        "acceptance": "PASS" if not errors else "FAIL",
        "asof_date": asof,
        "row_count": row_count,
        "errors": errors,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
