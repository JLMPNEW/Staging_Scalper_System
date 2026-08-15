#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    LATEST_POLICY_VERSION,
    load_investable_universe_policy,
    validate_investable_universe_policy,
)


TRANSPORTATION_ROOT = PROJECT_ROOT / "industrials" / "transportation"
DEFAULT_POLICY = (
    TRANSPORTATION_ROOT
    / "data"
    / "transportation_investable_universe_v4.yaml"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v4"
    / "transportation_investable_universe_apply.json"
)
V3_EXCLUSIONS = (
    TRANSPORTATION_ROOT
    / "system_csvs"
    / "transportation_investable_exclusions_v3.csv"
)
OVERLAY_PATH = (
    TRANSPORTATION_ROOT
    / "system_csvs"
    / "transportation_classification_overlays.csv"
)
CONFIG_PATH = PROJECT_ROOT / "industrials" / "config.yaml"
V3_CONFIG_POLICY_LINE = (
    '      investable_universe_policy: '
    '"transportation/data/transportation_investable_universe_v3.yaml"'
)
V3_CONFIG_VERSION_LINE = (
    "      investable_universe_version: transportation_investable_universe_v3"
)
V4_CONFIG_POLICY_LINE = (
    '      investable_universe_policy: '
    '"transportation/data/transportation_investable_universe_v4.yaml"'
)
V4_CONFIG_VERSION_LINE = (
    "      investable_universe_version: transportation_investable_universe_v4"
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
EXCLUSION_FIELDS = (
    "ticker",
    "effective_from",
    "disposition",
    "exclusion_group",
    "reason",
    "reentry_rule",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply the outcome-blind v4 transition that retains passenger "
            "airlines for research but removes them from production calibration."
        )
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def read_csv(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        if not fields:
            raise ValueError(f"CSV has no header: {path}")
        return fields, [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def activate_v4_config() -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    if V4_CONFIG_POLICY_LINE in text and V4_CONFIG_VERSION_LINE in text:
        return
    if (
        text.count(V3_CONFIG_POLICY_LINE) != 1
        or text.count(V3_CONFIG_VERSION_LINE) != 1
    ):
        raise ValueError(
            "industrials/config.yaml does not contain exactly one v3 "
            "investable-universe activation block"
        )
    text = text.replace(V3_CONFIG_POLICY_LINE, V4_CONFIG_POLICY_LINE)
    text = text.replace(V3_CONFIG_VERSION_LINE, V4_CONFIG_VERSION_LINE)
    write_text_atomic(CONFIG_PATH, text)


def main() -> int:
    args = parse_args()
    policy = load_investable_universe_policy(args.policy)
    if policy.policy_version != LATEST_POLICY_VERSION:
        raise ValueError(
            f"v4 apply script requires {LATEST_POLICY_VERSION}, "
            f"received {policy.policy_version}"
        )
    if set(policy.selected_tickers) & AIRLINE_TICKERS:
        raise ValueError("passenger airlines remain in the v4 selected universe")
    activate_v4_config()

    catalog_fields, catalog_rows = read_csv(policy.catalog_path)
    catalog = {row["ticker"].upper(): row for row in catalog_rows}
    selected_rows: list[dict[str, str]] = []
    for group in policy.groups:
        for ticker in group.tickers:
            if ticker not in catalog:
                raise ValueError(f"v4 selected ticker is missing from catalog: {ticker}")
            selected_rows.append(
                {
                    **catalog[ticker],
                    "investment_group": group.group_id,
                }
            )
    selected_fields = (*catalog_fields, "investment_group")
    write_csv_atomic(
        policy.positioning_universe_path,
        selected_fields,
        selected_rows,
    )

    _, v3_exclusion_rows = read_csv(V3_EXCLUSIONS)
    exclusion_by_ticker = {
        row["ticker"].upper(): row for row in v3_exclusion_rows
    }
    for ticker in AIRLINE_TICKERS:
        exclusion_by_ticker[ticker] = {
            "ticker": ticker,
            "effective_from": policy.effective_from,
            "disposition": "research_only",
            "exclusion_group": "passenger_airlines_monitor_only",
            "reason": (
                "airline economics and specialized metrics require an "
                "independent model and must not enter pooled production calibration"
            ),
            "reentry_rule": (
                "independent airline cohort contract with minimum issuer breadth "
                "and fresh untouched out-of-sample promotion gates"
            ),
        }
    expected_exclusions = set(catalog) - set(policy.selected_tickers)
    if set(exclusion_by_ticker) != expected_exclusions:
        raise ValueError(
            "v4 exclusion construction is not the exact catalog complement: "
            f"missing={sorted(expected_exclusions - set(exclusion_by_ticker))} "
            f"extra={sorted(set(exclusion_by_ticker) - expected_exclusions)}"
        )
    exclusions = []
    for ticker in sorted(exclusion_by_ticker):
        row = dict(exclusion_by_ticker[ticker])
        row["effective_from"] = policy.effective_from
        exclusions.append(row)
    write_csv_atomic(policy.exclusions_path, EXCLUSION_FIELDS, exclusions)

    overlay_fields, overlay_rows = read_csv(OVERLAY_PATH)
    overlay_by_ticker = {row["ticker"].upper(): row for row in overlay_rows}
    missing_airlines = sorted(AIRLINE_TICKERS - set(overlay_by_ticker))
    if missing_airlines:
        raise ValueError(
            f"airline classification overlays are missing: {missing_airlines}"
        )
    for ticker in AIRLINE_TICKERS:
        row = overlay_by_ticker[ticker]
        row.update(
            {
                "effective_from": policy.effective_from,
                "effective_to": "",
                "economic_peer_group": "passenger_airline",
                "portfolio_role": "airline_satellite_research",
                "review_status": "reviewed",
                "source": policy.path.stem,
                "notes": (
                    "Retained in the research catalogue; excluded from "
                    "production calibration and portfolio eligibility."
                ),
            }
        )
    write_csv_atomic(OVERLAY_PATH, overlay_fields, overlay_rows)

    errors, validation = validate_investable_universe_policy(policy)
    if errors:
        raise ValueError("v4 validation failed after apply: " + "; ".join(errors))
    result = {
        "policy_version": policy.policy_version,
        "effective_from": policy.effective_from,
        "research_catalog_count": len(catalog),
        "production_eligible_count": len(policy.selected_tickers),
        "production_excluded_count": len(exclusions),
        "passenger_airline_satellite_research_count": len(AIRLINE_TICKERS),
        "production_groups": {
            group.group_id: len(group.tickers) for group in policy.groups
        },
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
        "validation": validation,
        "acceptance": "PASS",
    }
    write_text_atomic(
        args.output_json.expanduser().resolve(),
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
