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

from industrials.core.config import family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.production_lock import load_effective_production_lock  # noqa: E402
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.scoring import finalize_rank_rows, publish_dashboard  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish a dated transportation shadow rank table.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = datetime.strptime(args.asof[:10], "%Y-%m-%d").date().isoformat()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    scoring = family_config(config, MODEL_FAMILY)["scoring"]
    base_dir = config_path.parent
    production_lock = load_effective_production_lock(
        config,
        model_family=MODEL_FAMILY,
        base_dir=base_dir,
        asof=asof,
    )
    input_path = args.input_csv.expanduser().resolve() if args.input_csv else resolve_path(
        scoring["feature_output_csv"], base_dir=base_dir
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else (
        resolve_path(scoring["dashboard_root"], base_dir=base_dir) / asof
    )
    rows = read_rows(input_path)
    if {str(row.get("asof_date") or "") for row in rows} != {asof}:
        raise ValueError("Scoring input asof_date does not exactly match requested publication date")
    final_rows = finalize_rank_rows(
        rows,
        score_model_version=str(scoring["score_model_version"]),
        model_version=str(scoring["model_version"]),
        scoring_contract_version=str(scoring["scoring_contract_version"]),
        production_lock=production_lock,
    )
    manifest = publish_dashboard(
        output_dir=output_dir,
        rows=final_rows,
        asof=asof,
        allow_overwrite=args.force,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
