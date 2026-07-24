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
from industrials.core.text_norm import normalize_ticker  # noqa: E402
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


def identity_contract_errors(
    *,
    active_rows: list[dict[str, str]],
    delisted_rows: list[dict[str, str]],
    mapping_rows: list[dict[str, str]],
    membership_rows: list[dict[str, str]],
    listing_rows: list[dict[str, str]],
) -> list[str]:
    errors: list[str] = []

    def keyed(rows: list[dict[str, str]], field: str, label: str) -> dict[str, dict[str, str]]:
        output: dict[str, dict[str, str]] = {}
        for row in rows:
            ticker = normalize_ticker(row.get(field))
            if not ticker:
                errors.append(f"{label}: blank {field}")
                continue
            if ticker in output:
                errors.append(f"{label}: duplicate {field}={ticker}")
                continue
            output[ticker] = row
        return output

    active = keyed(active_rows, "ticker", "active")
    delisted = keyed(delisted_rows, "ticker", "delisted")
    mappings = keyed(mapping_rows, "internal_ticker", "norgate_map")
    memberships = keyed(membership_rows, "internal_ticker", "historical_membership")
    listings = keyed(listing_rows, "ticker", "listing_dates")
    expected_mapping_tickers = set(active) | set(delisted)
    if set(mappings) != expected_mapping_tickers:
        errors.append(
            "norgate map ticker set mismatch: "
            f"missing={sorted(expected_mapping_tickers - set(mappings))[:20]} "
            f"unexpected={sorted(set(mappings) - expected_mapping_tickers)[:20]}"
        )
    if set(memberships) != set(listings):
        errors.append(
            "historical membership/listing ticker mismatch: "
            f"membership_only={sorted(set(memberships) - set(listings))[:20]} "
            f"listing_only={sorted(set(listings) - set(memberships))[:20]}"
        )
    for ticker, mapping in mappings.items():
        if normalize_ticker(mapping.get("actual_ticker")) != ticker:
            errors.append(f"{ticker}: actual_ticker must equal internal_ticker")
        usable = str(mapping.get("calibration_usable_flag") or "").strip() == "1"
        if usable and not str(mapping.get("norgate_symbol") or "").strip():
            errors.append(f"{ticker}: usable Norgate mapping has a blank symbol")
    for ticker, membership in memberships.items():
        mapping = mappings.get(ticker)
        listing = listings.get(ticker)
        if mapping is None or listing is None:
            continue
        if str(mapping.get("calibration_usable_flag") or "").strip() != "1":
            errors.append(f"{ticker}: historical membership references a non-usable Norgate mapping")
        if str(membership.get("price_source_symbol") or "").strip().upper() != str(
            mapping.get("norgate_symbol") or ""
        ).strip().upper():
            errors.append(f"{ticker}: historical membership price_source_symbol differs from Norgate map")
        if str(membership.get("start_date") or "").strip() != str(
            listing.get("first_eligible_date") or ""
        ).strip():
            errors.append(f"{ticker}: historical/listing start dates differ")
        if str(membership.get("end_date") or "").strip() != str(
            listing.get("last_eligible_date") or ""
        ).strip():
            errors.append(f"{ticker}: historical/listing end dates differ")
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
    errors.extend(
        identity_contract_errors(
            active_rows=active_rows,
            delisted_rows=delisted_rows,
            mapping_rows=read_csv_rows(
                resolve_path(cfg_get(config, "industrials_universe.norgate_symbol_map_csv"), base_dir=base_dir)
            ),
            membership_rows=read_csv_rows(
                resolve_path(cfg_get(config, "industrials_universe.historical_membership_csv"), base_dir=base_dir)
            ),
            listing_rows=read_csv_rows(
                resolve_path(cfg_get(config, "industrials_universe.listing_dates_csv"), base_dir=base_dir)
            ),
        )
    )
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
