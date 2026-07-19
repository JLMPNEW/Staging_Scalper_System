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
from industrials.machinery.contracts import (  # noqa: E402
    cohort_metadata,
    read_csv_rows,
    validate_seed_contracts,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
# Untracked working copies of the seed CSVs (ticker_mapping/ is gitignored).
# They are not read by the pipeline, but silent drift against the canonical
# system_csvs copies has already happened once — fail loudly when they fork.
TICKER_MAPPING_DUPLICATES = {
    "industrials_universe.seed_csv": PROJECT_ROOT / "ticker_mapping" / "machinery_tickers.csv",
    "industrials_universe.delisted_seed_csv": PROJECT_ROOT / "ticker_mapping" / "machinery_delisted.csv",
}


def duplicate_copy_errors(config: dict, base_dir: Path) -> list[str]:
    errors: list[str] = []
    for config_key, duplicate_path in TICKER_MAPPING_DUPLICATES.items():
        if not duplicate_path.exists():
            continue
        canonical_path = resolve_path(cfg_get(config, config_key), base_dir=base_dir)
        if duplicate_path.resolve() == canonical_path.resolve():
            continue
        # Row-level comparison: newline style / trailing-EOF differences are
        # not data drift.
        if read_csv_rows(duplicate_path) != read_csv_rows(canonical_path):
            errors.append(
                f"Untracked duplicate {duplicate_path} has diverged from canonical {canonical_path}; "
                "reconcile them (system_csvs is the pipeline source of truth)."
            )
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate machinery active and delisted seed contracts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    active_path = resolve_path(cfg_get(config, "industrials_universe.seed_csv"), base_dir=base_dir)
    delisted_path = resolve_path(cfg_get(config, "industrials_universe.delisted_seed_csv"), base_dir=base_dir)
    cohort_path = resolve_path(cfg_get(config, "industrials_universe.cohort_path"), base_dir=base_dir)
    policy_path = resolve_path(cfg_get(config, "industrials_universe.policy_path"), base_dir=base_dir)
    policy = load_yaml(policy_path)
    active_rows = read_csv_rows(active_path)
    delisted_rows = read_csv_rows(delisted_path)
    cohorts = cohort_metadata(cohort_path)
    errors = validate_seed_contracts(
        active_rows,
        delisted_rows,
        cohorts,
        expected_active=int(policy.get("expected_ticker_count") or 0),
        expected_delisted=int(policy.get("expected_delisted_count") or 0),
    )
    errors.extend(duplicate_copy_errors(config, base_dir))
    summary = {
        "status": "PASS" if not errors else "FAIL",
        "active_tickers": len(active_rows),
        "delisted_tickers": len(delisted_rows),
        "cohorts": len(cohorts),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
