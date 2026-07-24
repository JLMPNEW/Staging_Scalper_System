#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.csv_utils import read_csv_flexible  # noqa: E402
from industrials.core.family_universe import validate_seed_contracts  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    PROJECT_ROOT,
    resolve_foundation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate transportation active and delisted seeds.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = resolve_foundation(args.config)
    errors, warnings, counts = validate_seed_contracts(
        active_path=paths.active_path,
        delisted_path=paths.delisted_path,
        cohort_path=paths.cohort_path,
        policy_path=paths.policy_path,
        model_family=MODEL_FAMILY,
    )
    intake_pairs = [
        (PROJECT_ROOT / "ticker_mapping" / "transportation_tickers.csv", paths.active_path),
        (PROJECT_ROOT / "ticker_mapping" / "transportation_delisted.csv", paths.delisted_path),
    ]
    for intake, canonical in intake_pairs:
        if intake.exists() and read_csv_flexible(intake) != read_csv_flexible(canonical):
            errors.append(f"intake seed has drifted from canonical system CSV: {intake} != {canonical}")
    summary = {
        "status": "PASS" if not errors else "FAIL",
        **counts,
        "warnings": warnings,
        "errors": errors,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
