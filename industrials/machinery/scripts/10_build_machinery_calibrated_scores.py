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
    dated_path,
    file_sha256,
    finalize_rank_rows,
    parse_asof,
    read_rows,
    validate_rank_rows,
    write_json_atomic,
    write_rank_rows,
)
from industrials.machinery.stage12_activation import (  # noqa: E402
    apply_active_production_policy,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build machinery shadow calibrated scores and ranks.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    feature_root = resolve_path(cfg_get(config, "machinery_scoring.feature_output_root"), base_dir=base_dir)
    score_root = resolve_path(cfg_get(config, "machinery_scoring.score_output_root"), base_dir=base_dir)
    input_path = args.input_csv.expanduser().resolve() if args.input_csv else dated_path(
        feature_root,
        asof,
        "machinery_scoring_feature_contract.csv",
    )
    output_path = args.output_csv.expanduser().resolve() if args.output_csv else dated_path(
        score_root,
        asof,
        "machinery_calibrated_scores.csv",
    )
    if output_path.exists() and not args.force:
        raise FileExistsError(f"Refusing to overwrite machinery scores without --force: {output_path}")
    shadow_rows = finalize_rank_rows(
        read_rows(input_path),
        score_model_version=str(cfg_get(config, "machinery_scoring.score_model_version")),
        model_version=str(cfg_get(config, "machinery_scoring.model_version")),
        scoring_contract_version=str(cfg_get(config, "machinery_scoring.contract_version")),
    )
    stage12_output = str(
        cfg_get(config, "machinery_stage12.output_root", "")
    ).strip()
    if stage12_output:
        rows, production_metadata = apply_active_production_policy(
            config,
            config_path=config_path,
            governance_root=resolve_path(stage12_output, base_dir=base_dir),
            asof=asof,
            shadow_rows=shadow_rows,
        )
    else:
        rows = shadow_rows
        production_metadata = {
            "production_policy_active": False,
            "production_policy_status": "SHADOW_NO_STAGE12_CONFIG",
        }
    production_active = bool(
        production_metadata["production_policy_active"]
    )
    errors = validate_rank_rows(
        rows,
        asof=asof,
        allow_production=production_active,
    )
    if errors:
        raise ValueError("; ".join(errors[:20]))
    write_rank_rows(output_path, rows)
    manifest = {
        "acceptance": "PASS",
        "model_family": "machinery",
        "asof_date": asof,
        "row_count": len(rows),
        "rank_ready_count": sum(row["rank_ready_flag"] == "1" for row in rows),
        "portfolio_candidate_count": sum(
            row["portfolio_candidate_gate"] == "1" for row in rows
        ),
        "production_policy_active": production_active,
        "production_metadata": production_metadata,
        "output_csv": str(output_path),
        "output_sha256": file_sha256(output_path),
    }
    write_json_atomic(output_path.with_suffix(".manifest.json"), manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
