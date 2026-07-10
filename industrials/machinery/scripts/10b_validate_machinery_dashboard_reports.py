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


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    dashboard_root = resolve_path(cfg_get(config, "machinery_scoring.dashboard_root"), base_dir=base_dir)
    input_dir = args.input_dir.expanduser().resolve() if args.input_dir else dashboard_root / asof
    rank_path = input_dir / "machinery_final_rank_table.csv"
    sidecar_path = input_dir / "machinery_stage11_survivorship_calibration_panel.csv"
    manifest_path = input_dir / "machinery_final_rank_table_manifest.json"
    errors = validate_rank_rows(read_rows(rank_path), asof=asof)
    sidecar = read_rows(sidecar_path)
    if not sidecar:
        errors.append("calibration sidecar is empty")
    elif set(sidecar[0]) != set(FINAL_RANK_FIELDS):
        errors.append("calibration sidecar schema differs from final rank contract")
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
    if manifest.get("rank_table_sha256") != file_sha256(rank_path):
        errors.append("rank table hash does not match manifest")
    if manifest.get("sidecar_sha256") != file_sha256(sidecar_path):
        errors.append("sidecar hash does not match manifest")
    summary = {
        "acceptance": "PASS" if not errors else "FAIL",
        "asof_date": asof,
        "row_count": len(sidecar),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
