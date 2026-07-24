#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import family_config, resolve_path  # noqa: E402
from industrials.core.csv_utils import read_csv_flexible  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402
from industrials.transportation.security_continuity import (  # noqa: E402
    SOURCE_ID,
    load_security_continuity_policies,
    upsert_security_continuity_policies,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    resolve_foundation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load primary-source-verified transportation security-continuity policies."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = resolve_foundation(args.config, args.db)
    family = family_config(paths.config, "transportation")
    universe = family["universe"]
    policy_path = resolve_path(
        universe["security_continuity_overrides_csv"],
        base_dir=paths.config_path.parent,
    )
    source_id = str(universe["security_continuity_source_id"])
    if source_id != SOURCE_ID:
        raise ValueError(
            f"Unexpected transportation continuity source_id={source_id!r}; expected={SOURCE_ID!r}"
        )
    policies = load_security_continuity_policies(policy_path)
    active = {
        normalize_ticker(row.get("ticker")): row
        for row in read_csv_flexible(paths.active_path)
    }
    listings = {
        normalize_ticker(row.get("ticker")): row
        for row in read_csv_flexible(paths.listing_path)
    }
    required_fx = {
        str(value).strip().upper()
        for value in family["historical_load"]["required_fx_pairs"]
        if str(value).strip()
    }
    for ticker, policy in policies.items():
        if ticker not in active:
            raise ValueError(f"Continuity policy ticker is not active: {ticker}")
        if str(active[ticker].get("company_name") or "").strip() != policy.company_name:
            raise ValueError(f"Continuity company-name mismatch: {ticker}")
        if ticker not in listings:
            raise ValueError(f"Continuity policy lacks listing-date row: {ticker}")
        if (
            str(listings[ticker].get("first_eligible_date") or "").strip()
            != policy.current_security_start_date
        ):
            raise ValueError(
                f"Listing date does not reflect continuity policy: ticker={ticker} "
                f"listing={listings[ticker].get('first_eligible_date')} "
                f"policy={policy.current_security_start_date}"
            )
        if policy.required_fx_pair and policy.required_fx_pair not in required_fx:
            raise ValueError(
                f"Continuity policy FX pair is not pinned: ticker={ticker} "
                f"pair={policy.required_fx_pair}"
            )
    with connect(paths.db_path, timeout_sec=paths.timeout_sec) as connection:
        init_db(connection)
        upsert_source_registry(connection, load_source_registry(paths.registry_path))
        run_id = start_run(
            connection,
            run_type="load_transportation_security_continuity",
            input_path=policy_path,
        )
        try:
            with connection:
                count = upsert_security_continuity_policies(
                    connection,
                    policies=policies,
                    source_id=source_id,
                )
            finish_run(
                connection,
                run_id=run_id,
                status="success",
                row_count=count,
                message=f"security_continuity_policies={count}",
            )
        except BaseException as exc:
            finish_run(
                connection,
                run_id=run_id,
                status="failed",
                row_count=0,
                message=f"{type(exc).__name__}: {exc}",
            )
            raise
    print(f"PASS: loaded transportation security-continuity policies={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
