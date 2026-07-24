#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


FIELDS = [
    "ticker",
    "taxonomy",
    "concept_name",
    "unit",
    "raw_fact_count",
    "first_period_end",
    "last_period_end",
    "form_types",
    "source_details",
    "existing_canonical_metrics",
    "candidate_status",
    "recommended_metric",
    "recommended_action",
]
EXCLUDED_NAME_PARTS = (
    "abstract",
    "textblock",
    "member",
    "axis",
    "table",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit SB/VLRS raw XBRL concepts before approving transportation aliases."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--tickers", default="SB,VLRS")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def read_aliases(path: Path) -> set[tuple[str, str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            (
                str(row.get("taxonomy") or "").strip(),
                str(row.get("concept_name") or "").strip(),
                str(row.get("canonical_metric") or "").strip(),
            )
            for row in reader
            if str(row.get("review_status") or "").strip().lower() == "reviewed"
        }


def candidate_decision(
    *,
    concept_name: str,
    existing_metrics: set[str],
    approved_aliases: set[tuple[str, str, str]],
    taxonomy: str,
) -> tuple[str, str, str]:
    lower = concept_name.lower()
    if any(part in lower for part in EXCLUDED_NAME_PARTS):
        return "EXCLUDED_PRESENTATION_CONCEPT", "", "none"
    if (
        taxonomy,
        concept_name,
        "revenue",
    ) in approved_aliases:
        return "APPROVED_ALIAS", "revenue", "remap_companyfacts"
    # Concepts the live concept map already resolves to a canonical total do
    # not need another analyst review; report them as mapped instead of
    # re-queueing them on every audit rerun.
    if "revenue" in existing_metrics and "revenue" in lower:
        return "ALREADY_MAPPED", "revenue", "none"
    if "assets" in existing_metrics and "asset" in lower:
        return "ALREADY_MAPPED", "assets", "none"
    if concept_name in {
        "RevenueFromRenderingOfPassengerTransportServices",
        "RevenueFromRenderingOfCargoAndMailTransportServices",
    }:
        return "COMPONENT_ONLY", "", "retain_as_component_not_total_revenue"
    if "revenue" in lower:
        if any(
            token in lower
            for token in (
                "receivable",
                "deferred",
                "remainingperformance",
                "increase",
                "taxeffect",
            )
        ):
            return "NOT_TOTAL_REVENUE", "", "do_not_alias"
        return "REVIEW_REVENUE_CONCEPT", "revenue", "analyst_review_required"
    if "asset" in lower:
        return "NOT_TOTAL_ASSETS", "", "do_not_alias"
    return "OUT_OF_SCOPE", "", "none"


def read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, "transportation")
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(config["paths"]["database_path"], base_dir=base_dir)
    )
    output_dir = resolve_path(
        family["historical_load"]["output_dir"],
        base_dir=base_dir,
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else output_dir / "transportation_xbrl_tag_candidates.csv"
    )
    output_json = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else output_dir / "transportation_xbrl_tag_candidate_validation.json"
    )
    aliases_path = resolve_path(
        family["financial"]["concept_aliases_csv"],
        base_dir=base_dir,
    )
    approved_aliases = read_aliases(aliases_path)
    tickers = sorted(
        {
            value.strip().upper()
            for value in str(args.tickers or "").split(",")
            if value.strip()
        }
    )
    if not tickers:
        raise ValueError("At least one --tickers value is required")
    placeholders = ",".join("?" for _ in tickers)
    with read_only_connection(db_path) as connection:
        mappings: dict[tuple[str, str], set[str]] = {}
        for row in connection.execute(
            """
            SELECT taxonomy, concept_name, canonical_metric
            FROM dim_xbrl_concept_map
            WHERE active_flag=1
            """
        ).fetchall():
            mappings.setdefault(
                (str(row["taxonomy"]), str(row["concept_name"])),
                set(),
            ).add(str(row["canonical_metric"]))
        for taxonomy, concept_name, metric in approved_aliases:
            mappings.setdefault((taxonomy, concept_name), set()).add(metric)
        raw_rows = connection.execute(
            f"""
            SELECT ticker, taxonomy, concept_name, COALESCE(unit, '') AS unit,
                   COUNT(*) AS raw_fact_count,
                   MIN(period_end) AS first_period_end,
                   MAX(period_end) AS last_period_end,
                   GROUP_CONCAT(DISTINCT form_type) AS form_types,
                   GROUP_CONCAT(DISTINCT source_detail) AS source_details
            FROM fact_sec_xbrl_fact_raw
            WHERE ticker IN ({placeholders})
            GROUP BY ticker, taxonomy, concept_name, COALESCE(unit, '')
            ORDER BY ticker, taxonomy, concept_name, unit
            """,
            tuple(tickers),
        ).fetchall()
        core_presence = {
            str(row["ticker"]): {
                str(value)
                for value in str(row["metrics"] or "").split(",")
                if value
            }
            for row in connection.execute(
                f"""
                SELECT ticker, GROUP_CONCAT(DISTINCT canonical_metric) AS metrics
                FROM fact_sec_xbrl_fact
                WHERE ticker IN ({placeholders})
                  AND canonical_metric IN ('revenue', 'assets')
                GROUP BY ticker
                """,
                tuple(tickers),
            ).fetchall()
        }
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        concept_name = str(raw["concept_name"])
        lower = concept_name.lower()
        if "revenue" not in lower and "asset" not in lower:
            continue
        taxonomy = str(raw["taxonomy"])
        existing_metrics = mappings.get((taxonomy, concept_name), set())
        status, metric, action = candidate_decision(
            concept_name=concept_name,
            existing_metrics=existing_metrics,
            approved_aliases=approved_aliases,
            taxonomy=taxonomy,
        )
        if status in {"EXCLUDED_PRESENTATION_CONCEPT", "OUT_OF_SCOPE"}:
            continue
        rows.append(
            {
                "ticker": str(raw["ticker"]),
                "taxonomy": taxonomy,
                "concept_name": concept_name,
                "unit": str(raw["unit"] or ""),
                "raw_fact_count": int(raw["raw_fact_count"] or 0),
                "first_period_end": str(raw["first_period_end"] or ""),
                "last_period_end": str(raw["last_period_end"] or ""),
                "form_types": str(raw["form_types"] or ""),
                "source_details": str(raw["source_details"] or ""),
                "existing_canonical_metrics": "|".join(sorted(existing_metrics)),
                "candidate_status": status,
                "recommended_metric": metric,
                "recommended_action": action,
            }
        )
    ticker_actions: dict[str, str] = {}
    for ticker in tickers:
        presence = core_presence.get(ticker, set())
        if {"revenue", "assets"} <= presence:
            ticker_actions[ticker] = "CORE_PRESENT"
        elif ticker == "SB" and "assets" in presence:
            ticker_actions[ticker] = "TARGETED_ARCHIVE_REQUIRED_FOR_TOTAL_REVENUE"
        elif ticker == "VLRS" and any(
            row["ticker"] == ticker
            and row["candidate_status"] == "APPROVED_ALIAS"
            for row in rows
        ):
            ticker_actions[ticker] = "APPROVED_ALIAS_REMAP_REQUIRED"
        else:
            ticker_actions[ticker] = "ANALYST_REVIEW_REQUIRED"
    write_csv_atomic(output_csv, FIELDS, rows)
    result = {
        "status": "PASS",
        "tickers": tickers,
        "candidate_row_count": len(rows),
        "candidate_status_counts": dict(
            sorted(Counter(str(row["candidate_status"]) for row in rows).items())
        ),
        "core_presence_before_remap": {
            ticker: sorted(core_presence.get(ticker, set())) for ticker in tickers
        },
        "ticker_actions": ticker_actions,
        "approved_aliases": sorted(
            {
                f"{taxonomy}:{concept}->{metric}"
                for taxonomy, concept, metric in approved_aliases
            }
        ),
        "output_csv": str(output_csv),
        "database_path": str(db_path),
    }
    write_manifest(output_json, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
