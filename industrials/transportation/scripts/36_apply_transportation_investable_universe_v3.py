#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    load_investable_universe_policy,
)


TRANSPORTATION_ROOT = PROJECT_ROOT / "industrials" / "transportation"
DEFAULT_POLICY = (
    TRANSPORTATION_ROOT
    / "data"
    / "transportation_investable_universe_v3.yaml"
)

HISTORY_FIELDS = (
    "internal_ticker",
    "exchange_ticker",
    "price_source_symbol",
    "company_name",
    "cik",
    "exchange",
    "country",
    "currency",
    "security_type",
    "calibration_cohort_id",
    "calibration_cohort",
    "start_date",
    "end_date",
    "eligibility_basis",
    "membership_status",
    "successor_ticker",
    "event_type",
    "confidence",
    "source_url",
    "notes",
)
LISTING_FIELDS = (
    "ticker",
    "first_eligible_date",
    "last_eligible_date",
    "eligibility_basis",
    "source",
    "confidence",
    "notes",
)
ALIAS_FIELDS = (
    "contract_ticker",
    "active_ticker",
    "predecessor_ticker",
    "effective_date",
    "price_history_csv",
    "issuer_id",
    "reason",
    "source",
    "verified_flag",
    "notes",
)
OVERLAY_FIELDS = (
    "ticker",
    "effective_from",
    "effective_to",
    "economic_peer_group",
    "portfolio_role",
    "review_status",
    "source",
    "notes",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply the idempotent 112-to-120 transportation research-catalog "
            "migration while keeping the legacy DP0 active seed immutable."
        )
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    return parser.parse_args()


def read_csv(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        return fields, [
            {str(key): str(value or "") for key, value in row.items()}
            for row in reader
        ]


def require_unique_tickers(
    rows: Iterable[Mapping[str, str]], *, field: str, label: str
) -> dict[str, Mapping[str, str]]:
    output: dict[str, Mapping[str, str]] = {}
    for row in rows:
        ticker = str(row.get(field) or "").strip().upper()
        if not ticker or ticker in output:
            raise ValueError(f"{label}: blank or duplicate ticker={ticker!r}")
        output[ticker] = row
    return output


def update_text_contract(path: Path, *, needle: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    if replacement in text:
        return
    if text.count(needle) != 1:
        raise ValueError(
            f"{path}: expected exactly one migration anchor={needle!r}"
        )
    write_text_atomic(path, text.replace(needle, replacement))


def main() -> int:
    args = parse_args()
    policy = load_investable_universe_policy(args.policy)
    system_dir = TRANSPORTATION_ROOT / "system_csvs"
    active_path = system_dir / "transportation_tickers.csv"
    legacy_active_path = system_dir / "transportation_tickers_dp0_v1.csv"
    universe_policy_path = (
        TRANSPORTATION_ROOT / "data" / "transportation_universe_policy.yaml"
    )
    config_path = PROJECT_ROOT / "industrials" / "config.yaml"
    history_path = system_dir / "transportation_historical_membership.csv"
    listing_path = system_dir / "transportation_listing_dates.csv"
    alias_path = system_dir / "transportation_ticker_aliases.csv"
    overlay_path = system_dir / "transportation_classification_overlays.csv"

    catalog_fields, catalog_rows = read_csv(policy.catalog_path)
    catalog = require_unique_tickers(
        catalog_rows, field="ticker", label="authoritative catalog"
    )
    if len(catalog) != 120:
        raise ValueError(f"authoritative catalog count={len(catalog)} expected=120")

    active_fields, active_rows = read_csv(active_path)
    active = require_unique_tickers(active_rows, field="ticker", label="active seed")
    if set(active) not in ({*catalog}, set(catalog) - set(policy.new_tanker_tickers)):
        raise ValueError(
            "implementation seed is neither the approved pre-migration 112 set "
            "nor the exact 120-name authoritative catalog"
        )
    if not legacy_active_path.exists():
        if len(active) != 112:
            raise ValueError("cannot create legacy DP0 seed from non-112 input")
        write_csv_atomic(legacy_active_path, active_fields, active_rows)
    _, legacy_rows = read_csv(legacy_active_path)
    if len(require_unique_tickers(legacy_rows, field="ticker", label="legacy DP0 seed")) != 112:
        raise ValueError("legacy DP0 active seed must contain exactly 112 tickers")
    if active_fields != catalog_fields:
        raise ValueError("authoritative and implementation ticker schemas differ")
    write_csv_atomic(active_path, active_fields, catalog_rows)

    update_text_contract(
        universe_policy_path,
        needle="expected_ticker_count: 112",
        replacement="expected_ticker_count: 120",
    )
    update_text_contract(
        config_path,
        needle=(
            "      classification_policy_version: transportation_classification_v1\n"
        ),
        replacement=(
            "      classification_policy_version: transportation_classification_v1\n"
            "      investable_universe_policy: \"transportation/data/transportation_investable_universe_v3.yaml\"\n"
            "      investable_universe_version: transportation_investable_universe_v3\n"
        ),
    )
    update_text_contract(
        config_path,
        needle=(
            "      dp0_manifest_json: \"transportation/data/transportation_dp0_contract_manifest.json\"\n"
        ),
        replacement=(
            "      dp0_manifest_json: \"transportation/data/transportation_dp0_contract_manifest.json\"\n"
            "      dp0_active_seed_csv: \"transportation/system_csvs/transportation_tickers_dp0_v1.csv\"\n"
        ),
    )
    update_text_contract(
        config_path,
        needle=(
            "      output_root: \"../output/industrials/transportation/dedicated_parser\"\n"
        ),
        replacement=(
            "      output_root: \"../output/industrials/transportation/dedicated_parser\"\n"
            "      tanker_delta_output_root: \"../output/industrials/transportation/investable_v3/tanker_delta\"\n"
        ),
    )

    _, history_rows = read_csv(history_path)
    history = require_unique_tickers(
        history_rows, field="internal_ticker", label="historical membership"
    )
    new_history: list[dict[str, str]] = []
    for ticker in policy.new_tanker_tickers:
        source = catalog[ticker]
        new_history.append(
            {
                "internal_ticker": ticker,
                "exchange_ticker": ticker,
                "price_source_symbol": ticker,
                "company_name": source["company_name"],
                "cik": str(source["cik"]).zfill(10),
                "exchange": source["exchange"],
                "country": source["country"],
                "currency": source["currency"],
                "security_type": source["security_type"],
                "calibration_cohort_id": source["calibration_cohort"],
                "calibration_cohort": "Marine Shipping & Maritime",
                "start_date": "2019-01-02",
                "end_date": "",
                "eligibility_basis": "approved_investable_v3_identity_migration",
                "membership_status": "active",
                "successor_ticker": "",
                "event_type": "active_at_contract_build",
                "confidence": "1.00" if ticker == "TEN" else "0.95",
                "source_url": (
                    "https://www.sec.gov/Archives/edgar/data/1166663/"
                    "000119312524161669/d805052d6k.htm"
                    if ticker == "TEN"
                    else "transportation_investable_universe_v3"
                ),
                "notes": (
                    "Continuous issuer history; TNP predecessor symbol through "
                    "2024-06-30 and TEN from 2024-07-01."
                    if ticker == "TEN"
                    else "Approved active oil-tanker mapping."
                ),
            }
        )
    history.update({row["internal_ticker"]: row for row in new_history})
    ordered_history = [
        *[
            dict(row)
            for row in history_rows
            if row["membership_status"].strip().lower() == "active"
            and row["internal_ticker"] not in set(policy.new_tanker_tickers)
        ],
        *new_history,
        *[
            dict(row)
            for row in history_rows
            if row["membership_status"].strip().lower() != "active"
        ],
    ]
    if len(ordered_history) != 167:
        raise ValueError(
            f"historical membership count={len(ordered_history)} expected=167"
        )
    write_csv_atomic(history_path, HISTORY_FIELDS, ordered_history)

    _, listing_rows = read_csv(listing_path)
    listing = require_unique_tickers(
        listing_rows, field="ticker", label="listing dates"
    )
    for ticker in policy.new_tanker_tickers:
        listing[ticker] = {
            "ticker": ticker,
            "first_eligible_date": "2019-01-02",
            "last_eligible_date": "",
            "eligibility_basis": "approved_investable_v3_identity_migration",
            "source": (
                "https://www.sec.gov/Archives/edgar/data/1166663/"
                "000119312524161669/d805052d6k.htm"
                if ticker == "TEN"
                else "transportation_investable_universe_v3"
            ),
            "confidence": "1.00" if ticker == "TEN" else "0.95",
            "notes": (
                "TNP through 2024-06-30; TEN effective 2024-07-01; continuous issuer."
                if ticker == "TEN"
                else "Approved active oil-tanker mapping; clipped to research start."
            ),
        }
    ordered_listing = [
        listing[row["internal_ticker"]] for row in ordered_history
    ]
    write_csv_atomic(listing_path, LISTING_FIELDS, ordered_listing)

    _, alias_rows = read_csv(alias_path)
    alias_rows = [row for row in alias_rows if row["contract_ticker"] != "TEN"]
    alias_rows.append(
        {
            "contract_ticker": "TEN",
            "active_ticker": "TEN",
            "predecessor_ticker": "TNP",
            "effective_date": "2024-07-01",
            "price_history_csv": "",
            "issuer_id": "0001166663",
            "reason": "ticker_symbol_change_continuous_issuer",
            "source": (
                "https://www.sec.gov/Archives/edgar/data/1166663/"
                "000119312524161669/d805052d6k.htm"
            ),
            "verified_flag": "1",
            "notes": "Preserve TNP history before TEN; no corporate-entity break.",
        }
    )
    write_csv_atomic(alias_path, ALIAS_FIELDS, alias_rows)

    _, overlay_rows = read_csv(overlay_path)
    selected_air_or_tanker = set(policy.groups[1].tickers) | set(
        policy.groups[2].tickers
    )
    overlay_rows = [
        row for row in overlay_rows if row["ticker"] not in selected_air_or_tanker
    ]
    peer_groups = {
        **{ticker: "passenger_airline" for ticker in policy.groups[1].tickers},
        **{ticker: "crude_tanker" for ticker in ("DHT", "ECO", "FRO", "NAT")},
        **{
            ticker: "product_tanker"
            for ticker in ("ASC", "HAFN", "STNG", "TRMD")
        },
        **{
            ticker: "diversified_tanker"
            for ticker in ("INSW", "TNK", "TEN")
        },
    }
    for ticker in (*policy.groups[1].tickers, *policy.groups[2].tickers):
        overlay_rows.append(
            {
                "ticker": ticker,
                "effective_from": policy.effective_from,
                "effective_to": "",
                "economic_peer_group": peer_groups[ticker],
                "portfolio_role": "core_candidate",
                "review_status": "reviewed",
                "source": policy.path.stem,
                "notes": "Outcome-blind v3 investable-universe classification.",
            }
        )
    write_csv_atomic(overlay_path, OVERLAY_FIELDS, overlay_rows)

    print(
        "transportation investable v3 migration PASS: "
        "120 research identities, 40 selected, 80 excluded, "
        "legacy DP0 seed preserved at 112"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
