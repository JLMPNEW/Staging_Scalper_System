#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.transportation.contracts import write_manifest, write_scoring_rows  # noqa: E402
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.scoring import build_scoring_rows  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cohort-aware transportation scoring features.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def parse_asof(value: str) -> str:
    return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    financial = family["financial"]
    scoring = family["scoring"]
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=base_dir
    )
    output_path = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(
        scoring["feature_output_csv"], base_dir=base_dir
    )
    if output_path.exists() and not args.force:
        raise FileExistsError(f"Refusing to overwrite scoring features without --force: {output_path}")
    registry_path = resolve_path(financial["metric_registry"], base_dir=base_dir)
    policy_path = resolve_path(
        cfg_get(config, "scoring_policy.families.transportation.eligibility_policy_csv"),
        base_dir=base_dir,
    )
    registry_version, definitions = load_metric_registry(registry_path)
    weights = {str(key): float(value) for key, value in scoring["component_weights"].items()}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        init_db(conn)
        rows = build_scoring_rows(
            conn,
            asof=asof,
            active_source_id=str(universe["seed_source_id"]),
            definitions=definitions,
            registry_version=registry_version,
            policy_path=policy_path,
            component_weights=weights,
            max_staleness_days=int(scoring["max_staleness_days"]),
            minimum_avg_dollar_volume=float(scoring["minimum_avg_dollar_volume_60d"]),
            minimum_score_confidence=float(scoring["minimum_score_confidence"]),
            minimum_specialized_coverage=float(scoring["minimum_specialized_coverage"]),
        )
    write_scoring_rows(output_path, rows)
    manifest = {
        "acceptance": "PASS",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "row_count": len(rows),
        "rank_ready_count": sum(row["rank_ready_flag"] == "1" for row in rows),
        "blocked_count": sum(row["rank_ready_flag"] == "0" for row in rows),
        "metric_registry_version": registry_version,
        "output_csv": str(output_path),
    }
    write_manifest(output_path.with_suffix(".manifest.json"), manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
