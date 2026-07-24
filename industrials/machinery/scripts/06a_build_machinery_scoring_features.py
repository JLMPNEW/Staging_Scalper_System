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
from industrials.core.db import connect  # noqa: E402
from industrials.core.policy_loader import load_eligibility_policy  # noqa: E402
from industrials.machinery.scoring import (  # noqa: E402
    build_scoring_feature_rows,
    dated_path,
    parse_asof,
    write_feature_rows,
    write_json_atomic,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build machinery point-in-time scoring features.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_root = resolve_path(cfg_get(config, "machinery_scoring.feature_output_root"), base_dir=base_dir)
    output_path = args.output_csv.expanduser().resolve() if args.output_csv else dated_path(
        output_root,
        asof,
        "machinery_scoring_feature_contract.csv",
    )
    if output_path.exists() and not args.force:
        raise FileExistsError(f"Refusing to overwrite scoring features without --force: {output_path}")
    weights = cfg_get(config, "machinery_scoring.component_weights", {}) or {}
    if not isinstance(weights, dict):
        raise ValueError("machinery_scoring.component_weights must be a mapping")
    policy_path = resolve_path(
        cfg_get(config, "scoring_policy.families.machinery.eligibility_policy_csv"),
        base_dir=base_dir,
    )
    eligibility_policies = load_eligibility_policy(policy_path, asof=asof)
    if not eligibility_policies:
        raise ValueError(f"No machinery scoring eligibility policies are effective at {asof}")
    market_sources = tuple(
        dict.fromkeys(
            [
                str(cfg_get(config, "market_data_policy.scoring_primary_source", "") or "").strip(),
                *[
                    str(value or "").strip()
                    for value in (cfg_get(config, "market_data_policy.scoring_fallback_sources", []) or [])
                ],
            ]
        )
    )
    market_sources = tuple(source for source in market_sources if source)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        rows = build_scoring_feature_rows(
            conn,
            asof=asof,
            eligibility_policies=eligibility_policies,
            market_source_priority=market_sources,
            financial_source_priority=(
                str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts")),
            ),
            positioning_source_priority=(
                str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite")),
            ),
            component_weights=weights,
            min_score_confidence=float(cfg_get(config, "machinery_scoring.min_score_confidence", 0.40)),
            max_staleness_days=int(cfg_get(config, "market_data_policy.max_staleness_days", 7)),
            min_avg_dollar_volume=float(
                cfg_get(config, "market_data_policy.min_avg_dollar_volume_60d_for_full_features", 5000000)
            ),
        )
    write_feature_rows(output_path, rows)
    manifest = {
        "acceptance": "PASS",
        "model_family": "machinery",
        "asof_date": asof,
        "row_count": len(rows),
        "rank_ready_count": sum(row["rank_ready_flag"] == "1" for row in rows),
        "output_csv": str(output_path),
    }
    write_json_atomic(output_path.with_suffix(".manifest.json"), manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
