#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    LATEST_POLICY_VERSION,
    load_investable_universe_policy,
    validate_investable_universe_policy,
)


DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_investable_universe_v4.yaml"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v4"
    / "transportation_investable_universe_validation.json"
)
AIRLINE_TICKERS = {
    "AAL",
    "ALGT",
    "ALK",
    "CPA",
    "DAL",
    "JBLU",
    "LUV",
    "RYAAY",
    "UAL",
    "ULCC",
}
CONFIG_PATH = PROJECT_ROOT / "industrials" / "config.yaml"
V4_CONFIG_POLICY_LINE = (
    '      investable_universe_policy: '
    '"transportation/data/transportation_investable_universe_v4.yaml"'
)
V4_CONFIG_VERSION_LINE = (
    "      investable_universe_version: transportation_investable_universe_v4"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the governed 120-name research catalogue, 30-name "
            "production-calibration universe, and airline research-only boundary."
        )
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    policy = load_investable_universe_policy(args.policy)
    errors, result = validate_investable_universe_policy(policy)
    if policy.policy_version != LATEST_POLICY_VERSION:
        errors.append(
            f"policy version is {policy.policy_version}; expected {LATEST_POLICY_VERSION}"
        )
    selected = set(policy.selected_tickers)
    if selected & AIRLINE_TICKERS:
        errors.append(
            "passenger airlines remain production eligible: "
            f"{sorted(selected & AIRLINE_TICKERS)}"
        )
    if len(selected) != 30:
        errors.append(f"selected count={len(selected)} expected=30")
    config_text = CONFIG_PATH.read_text(encoding="utf-8")
    if (
        config_text.count(V4_CONFIG_POLICY_LINE) != 1
        or config_text.count(V4_CONFIG_VERSION_LINE) != 1
    ):
        errors.append("industrials/config.yaml is not pinned exactly once to v4")
    result["passenger_airlines_production_eligible_count"] = len(
        selected & AIRLINE_TICKERS
    )
    result["acceptance"] = "PASS" if not errors else "FAIL"
    result["errors"] = errors
    output = args.output_json.expanduser().resolve()
    write_text_atomic(output, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
