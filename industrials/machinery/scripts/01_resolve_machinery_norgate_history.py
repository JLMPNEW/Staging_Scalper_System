#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.machinery.contracts import (  # noqa: E402
    build_membership_rows,
    cohort_metadata,
    load_norgate_overrides,
    read_csv_rows,
    resolve_norgate_mappings,
    validate_seed_contracts,
    write_identity_contracts,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve actual machinery tickers to local Norgate symbols and build PIT membership contracts."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--allow-review-required", action="store_true")
    return parser.parse_args()


def load_provider() -> Any:
    try:
        import norgatedata  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise SystemExit(
            "norgatedata is not installed in this Python environment. Run this resolver with the base Miniconda Python."
        ) from exc
    return norgatedata


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    policy_path = resolve_path(cfg_get(config, "industrials_universe.policy_path"), base_dir=base_dir)
    policy = load_yaml(policy_path)
    cohort_path = resolve_path(cfg_get(config, "industrials_universe.cohort_path"), base_dir=base_dir)
    active_path = resolve_path(cfg_get(config, "industrials_universe.seed_csv"), base_dir=base_dir)
    delisted_path = resolve_path(cfg_get(config, "industrials_universe.delisted_seed_csv"), base_dir=base_dir)
    mapping_path = resolve_path(cfg_get(config, "industrials_universe.norgate_symbol_map_csv"), base_dir=base_dir)
    membership_path = resolve_path(cfg_get(config, "industrials_universe.historical_membership_csv"), base_dir=base_dir)
    listing_path = resolve_path(cfg_get(config, "industrials_universe.listing_dates_csv"), base_dir=base_dir)
    overrides_path = resolve_path(cfg_get(config, "industrials_universe.norgate_symbol_overrides_csv"), base_dir=base_dir)
    active_rows = read_csv_rows(active_path)
    delisted_rows = read_csv_rows(delisted_path)
    cohorts = cohort_metadata(cohort_path)
    seed_errors = validate_seed_contracts(
        active_rows,
        delisted_rows,
        cohorts,
        expected_active=int(policy.get("expected_ticker_count") or 0),
        expected_delisted=int(policy.get("expected_delisted_count") or 0),
    )
    if seed_errors:
        raise SystemExit("Seed validation failed: " + "; ".join(seed_errors[:10]))
    history_start = str(policy.get("history_start_date") or "2019-01-02")
    exclusions_raw = policy.get("known_norgate_exclusions") or {}
    known_exclusions = (
        {str(key).strip().upper(): str(value).strip() for key, value in exclusions_raw.items()}
        if isinstance(exclusions_raw, dict)
        else {}
    )
    mappings = resolve_norgate_mappings(
        active_rows=active_rows,
        delisted_rows=delisted_rows,
        provider=load_provider(),
        history_start=history_start,
        known_exclusions=known_exclusions,
        overrides=load_norgate_overrides(overrides_path),
    )
    memberships, listing_rows = build_membership_rows(
        active_rows=active_rows,
        delisted_rows=delisted_rows,
        mappings=mappings,
        cohorts=cohorts,
        history_start=history_start,
    )
    write_identity_contracts(
        mapping_path=mapping_path,
        membership_path=membership_path,
        listing_path=listing_path,
        mappings=mappings,
        memberships=memberships,
        listing_rows=listing_rows,
    )
    review_required = [
        mapping.internal_ticker
        for mapping in mappings
        if mapping.mapping_status in {"review_required", "unresolved", "invalid_override"}
    ]
    known_excluded = [mapping.internal_ticker for mapping in mappings if mapping.mapping_status == "excluded_known_unresolved"]
    summary = {
        "status": "PASS" if not review_required or args.allow_review_required else "FAIL",
        "mapping_rows": len(mappings),
        "calibration_usable_mappings": sum(mapping.calibration_usable_flag == "1" for mapping in mappings),
        "historical_membership_rows": len(memberships),
        "known_excluded_tickers": known_excluded,
        "review_required_tickers": review_required,
        "mapping_path": str(mapping_path),
        "membership_path": str(membership_path),
        "listing_path": str(listing_path),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
