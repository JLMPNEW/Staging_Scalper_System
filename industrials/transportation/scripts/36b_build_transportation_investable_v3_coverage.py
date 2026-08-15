#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    InvestableUniversePolicy,
    load_investable_universe_policy,
    validate_investable_universe_policy,
)


DEFAULT_CONFIG = PROJECT_ROOT / "industrials" / "config.yaml"
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_investable_universe_v3.yaml"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
)

INVENTORY_FIELDS = (
    "asof_date",
    "ticker",
    "investment_group",
    "calibration_pool",
    "catalog_status",
    "current_membership_flag",
    "price_row_count",
    "price_first_date",
    "price_last_date",
    "price_current_flag",
    "sec_filing_count",
    "sec_first_filing_date",
    "sec_last_filing_date",
    "mapped_financial_fact_count",
    "mapped_financial_period_count",
    "mapped_financial_first_period_end",
    "mapped_financial_last_period_end",
    "share_snapshot_count",
    "latest_share_asof_date",
    "latest_shares_outstanding_flag",
    "latest_public_float_flag",
    "positioning_snapshot_count",
    "latest_positioning_asof_date",
    "latest_positioning_observed_field_count",
    "specialized_accepted_metric_count",
    "specialized_review_metric_count",
    "scanned_document_count",
    "cached_source_ready_flag",
    "raw_load_status",
    "raw_load_gaps",
)

SPECIALIZED_FIELDS = (
    "investment_group",
    "metric_id",
    "metric_source_mode",
    "applicable_ticker_count",
    "accepted_ticker_count",
    "accepted_ticker_fraction",
    "minimum_accepted_breadth",
    "median_accepted_periods",
    "median_history_years",
    "breadth_gate",
    "history_gate",
    "calibration_disposition",
)

DELTA_FIELDS = (
    "ticker",
    "investment_group",
    "metric_id",
    "metric_source_mode",
    "accepted_period_count",
    "accepted_first_period_end",
    "accepted_last_period_end",
    "review_candidate_count",
    "sec_filing_count",
    "scanned_document_count",
    "cache_status",
    "delta_action",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the exact 40-name no-network raw and specialized-metric "
            "coverage inventory plus the bounded tanker delta queue."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _query_by_ticker(
    connection: sqlite3.Connection,
    *,
    sql: str,
    tickers: Iterable[str],
    prefix_args: tuple[object, ...] = (),
) -> dict[str, sqlite3.Row]:
    ordered = tuple(tickers)
    placeholders = ",".join("?" for _ in ordered)
    rows = connection.execute(
        sql.format(tickers=placeholders), (*prefix_args, *ordered)
    ).fetchall()
    return {str(row["ticker"]): row for row in rows}


def _date_span_years(start: str, end: str) -> float:
    if not start or not end:
        return 0.0
    return max(0.0, (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days / 365.25)


def _metric_evidence(
    connection: sqlite3.Connection,
    *,
    tickers: tuple[str, ...],
    metrics: set[str],
    asof: str,
) -> tuple[
    dict[tuple[str, str], set[str]],
    dict[tuple[str, str], int],
    dict[tuple[str, str], tuple[str, str]],
]:
    accepted: dict[tuple[str, str], set[str]] = defaultdict(set)
    reviews: dict[tuple[str, str], int] = defaultdict(int)
    periods: dict[tuple[str, str], list[str]] = defaultdict(list)
    if not _table_exists(connection, "fact_sec_metric_disclosure_candidate"):
        return accepted, reviews, {}
    placeholders = ",".join("?" for _ in tickers)
    metric_placeholders = ",".join("?" for _ in metrics)
    rows = connection.execute(
        f"""
        SELECT ticker, metric_name, candidate_status, period_end
        FROM fact_sec_metric_disclosure_candidate
        WHERE model_family='transportation'
          AND ticker IN ({placeholders})
          AND metric_name IN ({metric_placeholders})
          AND filing_date<=?
        """,
        (*tickers, *sorted(metrics), asof),
    ).fetchall()
    for row in rows:
        key = (str(row["ticker"]), str(row["metric_name"]))
        status = str(row["candidate_status"] or "").upper()
        period = str(row["period_end"] or "")[:10]
        if status == "ACCEPTED":
            accepted[key].add(period or "UNDATED")
            if period:
                periods[key].append(period)
        elif status in {"REVIEW", "REVIEW_REQUIRED", "PENDING_REVIEW"}:
            reviews[key] += 1
    spans = {
        key: (min(values), max(values))
        for key, values in periods.items()
        if values
    }
    return accepted, reviews, spans


def _positioning_observed(row: sqlite3.Row | None) -> int:
    if row is None:
        return 0
    fields = (
        "insider_net_value_90d",
        "insider_cluster_buyers_90d",
        "institutional_ownership_delta_pct",
        "short_interest_change_3m",
        "latest_borrow_fee_rate",
    )
    return sum(row[field] is not None for field in fields)


def build_inventory(
    connection: sqlite3.Connection,
    *,
    policy: InvestableUniversePolicy,
    asof: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    tickers = policy.selected_tickers
    group_by_ticker = policy.group_by_ticker
    all_metrics = {
        *policy.direct_tanker_metrics,
        *policy.derived_tanker_metrics,
        "operating_ratio",
        "passenger_load_factor",
    }
    membership = _query_by_ticker(
        connection,
        tickers=tickers,
        prefix_args=(asof, asof),
        sql="""
        SELECT ticker, COUNT(*) AS row_count
        FROM dim_universe_membership
        WHERE model_family='transportation'
          AND membership_status='active'
          AND start_date<=? AND COALESCE(end_date, '9999-12-31')>=?
          AND ticker IN ({tickers})
        GROUP BY ticker
        """,
    )
    prices = _query_by_ticker(
        connection,
        tickers=tickers,
        prefix_args=(asof,),
        sql="""
        SELECT ticker, COUNT(*) AS row_count, MIN(bar_date) AS first_date,
               MAX(bar_date) AS last_date
        FROM fact_price_ohlcv
        WHERE bar_date<=? AND ticker IN ({tickers})
        GROUP BY ticker
        """,
    )
    filings = (
        _query_by_ticker(
            connection,
            tickers=tickers,
            prefix_args=(asof,),
            sql="""
            SELECT ticker, COUNT(*) AS row_count,
                   MIN(filing_date) AS first_date, MAX(filing_date) AS last_date
            FROM fact_sec_filing
            WHERE filing_date<=? AND ticker IN ({tickers})
            GROUP BY ticker
            """,
        )
        if _table_exists(connection, "fact_sec_filing")
        else {}
    )
    facts = _query_by_ticker(
        connection,
        tickers=tickers,
        prefix_args=(asof,),
        sql="""
        SELECT ticker, COUNT(*) AS row_count,
               COUNT(DISTINCT period_end) AS period_count,
               MIN(period_end) AS first_period, MAX(period_end) AS last_period
        FROM fact_sec_xbrl_fact
        WHERE filing_date<=? AND ticker IN ({tickers})
        GROUP BY ticker
        """,
    )
    shares = _query_by_ticker(
        connection,
        tickers=tickers,
        prefix_args=(asof,),
        sql="""
        WITH scoped AS (
          SELECT *, ROW_NUMBER() OVER (
            PARTITION BY ticker ORDER BY
              CASE
                WHEN shares_outstanding IS NOT NULL AND float_shares IS NOT NULL THEN 0
                WHEN shares_outstanding IS NOT NULL OR float_shares IS NOT NULL THEN 1
                ELSE 2
              END,
              asof_date DESC,
              source_asof_date DESC
          ) AS rn,
          COUNT(*) OVER (PARTITION BY ticker) AS snapshot_count
          FROM fact_share_snapshot
          WHERE model_family='transportation' AND asof_date<=?
            AND ticker IN ({tickers})
        )
        SELECT ticker, snapshot_count AS row_count, asof_date,
               shares_outstanding, float_shares
        FROM scoped WHERE rn=1
        """,
    )
    positioning = _query_by_ticker(
        connection,
        tickers=tickers,
        prefix_args=(asof,),
        sql="""
        WITH scoped AS (
          SELECT *, ROW_NUMBER() OVER (
            PARTITION BY ticker ORDER BY asof_date DESC
          ) AS rn,
          COUNT(*) OVER (PARTITION BY ticker) AS snapshot_count
          FROM feature_positioning
          WHERE model_family='transportation' AND asof_date<=?
            AND ticker IN ({tickers})
        )
        SELECT * FROM scoped WHERE rn=1
        """,
    )
    scans = (
        _query_by_ticker(
            connection,
            tickers=tickers,
            prefix_args=(asof,),
            sql="""
            SELECT ticker, COUNT(*) AS row_count
            FROM fact_sec_metric_disclosure_document_scan
            WHERE model_family='transportation' AND filing_date<=?
              AND ticker IN ({tickers})
            GROUP BY ticker
            """,
        )
        if _table_exists(connection, "fact_sec_metric_disclosure_document_scan")
        else {}
    )
    accepted, reviews, spans = _metric_evidence(
        connection,
        tickers=tickers,
        metrics=all_metrics,
        asof=asof,
    )

    inventory: list[dict[str, object]] = []
    for ticker in tickers:
        group = group_by_ticker[ticker]
        price = prices.get(ticker)
        filing = filings.get(ticker)
        fact = facts.get(ticker)
        share = shares.get(ticker)
        position = positioning.get(ticker)
        scan = scans.get(ticker)
        accepted_metrics = {
            metric for candidate_ticker, metric in accepted if candidate_ticker == ticker
        }
        review_metrics = {
            metric for candidate_ticker, metric in reviews if candidate_ticker == ticker
        }
        gaps: list[str] = []
        if ticker not in membership:
            gaps.append("membership")
        if price is None or int(price["row_count"] or 0) == 0:
            gaps.append("prices")
        elif str(price["last_date"] or "") < asof:
            gaps.append("price_right_edge")
        if filing is None or int(filing["row_count"] or 0) == 0:
            gaps.append("sec_filings")
        if fact is None or int(fact["row_count"] or 0) == 0:
            gaps.append("financial_facts")
        if share is None or share["shares_outstanding"] is None:
            gaps.append("shares_outstanding")
        if share is None or share["float_shares"] is None:
            gaps.append("public_float")
        if _positioning_observed(position) == 0:
            gaps.append("positioning")
        inventory.append(
            {
                "asof_date": asof,
                "ticker": ticker,
                "investment_group": group.group_id,
                "calibration_pool": group.calibration_pool,
                "catalog_status": "selected_shadow_precalibration",
                "current_membership_flag": int(ticker in membership),
                "price_row_count": int(price["row_count"] or 0) if price else 0,
                "price_first_date": str(price["first_date"] or "") if price else "",
                "price_last_date": str(price["last_date"] or "") if price else "",
                "price_current_flag": int(bool(price) and str(price["last_date"] or "") >= asof),
                "sec_filing_count": int(filing["row_count"] or 0) if filing else 0,
                "sec_first_filing_date": str(filing["first_date"] or "") if filing else "",
                "sec_last_filing_date": str(filing["last_date"] or "") if filing else "",
                "mapped_financial_fact_count": int(fact["row_count"] or 0) if fact else 0,
                "mapped_financial_period_count": int(fact["period_count"] or 0) if fact else 0,
                "mapped_financial_first_period_end": str(fact["first_period"] or "") if fact else "",
                "mapped_financial_last_period_end": str(fact["last_period"] or "") if fact else "",
                "share_snapshot_count": int(share["row_count"] or 0) if share else 0,
                "latest_share_asof_date": str(share["asof_date"] or "") if share else "",
                "latest_shares_outstanding_flag": int(bool(share) and share["shares_outstanding"] is not None),
                "latest_public_float_flag": int(bool(share) and share["float_shares"] is not None),
                "positioning_snapshot_count": int(position["snapshot_count"] or 0) if position else 0,
                "latest_positioning_asof_date": str(position["asof_date"] or "") if position else "",
                "latest_positioning_observed_field_count": _positioning_observed(position),
                "specialized_accepted_metric_count": len(accepted_metrics),
                "specialized_review_metric_count": len(review_metrics),
                "scanned_document_count": int(scan["row_count"] or 0) if scan else 0,
                "cached_source_ready_flag": int(bool(filing) and int(filing["row_count"] or 0) > 0),
                "raw_load_status": "COMPLETE" if not gaps else "INCOMPLETE",
                "raw_load_gaps": "|".join(gaps),
            }
        )

    group_metrics = {
        "surface_freight_core": (("operating_ratio",), "direct_or_financial_derived"),
        "passenger_airlines": (("passenger_load_factor",), "direct_parser"),
        "oil_tanker_operators": (
            (*policy.direct_tanker_metrics, *policy.derived_tanker_metrics),
            "direct_parser_or_declared_derivation",
        ),
    }
    specialized: list[dict[str, object]] = []
    for group in policy.groups:
        metrics, source_mode = group_metrics[group.group_id]
        for metric in metrics:
            period_counts = [
                len(accepted.get((ticker, metric), set()))
                for ticker in group.tickers
                if accepted.get((ticker, metric))
            ]
            history_years = [
                _date_span_years(*spans[(ticker, metric)])
                for ticker in group.tickers
                if (ticker, metric) in spans
            ]
            accepted_count = len(period_counts)
            median_periods = statistics.median(period_counts) if period_counts else 0.0
            median_years = statistics.median(history_years) if history_years else 0.0
            breadth_pass = accepted_count >= group.minimum_specialized_breadth
            history_pass = (
                median_periods >= policy.minimum_median_periods
                and median_years >= policy.minimum_median_history_years
            )
            if breadth_pass and history_pass:
                disposition = "CALIBRATION_CANDIDATE"
            elif accepted_count >= policy.diagnostic_minimum_breadth:
                disposition = "DIAGNOSTIC_ONLY"
            else:
                disposition = "DROP_INSUFFICIENT_COVERAGE"
            specialized.append(
                {
                    "investment_group": group.group_id,
                    "metric_id": metric,
                    "metric_source_mode": source_mode,
                    "applicable_ticker_count": len(group.tickers),
                    "accepted_ticker_count": accepted_count,
                    "accepted_ticker_fraction": round(accepted_count / len(group.tickers), 6),
                    "minimum_accepted_breadth": group.minimum_specialized_breadth,
                    "median_accepted_periods": median_periods,
                    "median_history_years": round(median_years, 6),
                    "breadth_gate": "PASS" if breadth_pass else "FAIL",
                    "history_gate": "PASS" if history_pass else "FAIL",
                    "calibration_disposition": disposition,
                }
            )

    delta: list[dict[str, object]] = []
    inventory_by_ticker = {str(row["ticker"]): row for row in inventory}
    for ticker in policy.tanker_tickers:
        for metric in (*policy.direct_tanker_metrics, *policy.derived_tanker_metrics):
            key = (ticker, metric)
            period_set = accepted.get(key, set())
            first, last = spans.get(key, ("", ""))
            review_count = reviews.get(key, 0)
            inventory_row = inventory_by_ticker[ticker]
            filing_count = int(inventory_row["sec_filing_count"])
            scan_count = int(inventory_row["scanned_document_count"])
            mode = "derived" if metric in policy.derived_tanker_metrics else "direct_parser"
            if period_set:
                action = "reuse_accepted"
            elif mode == "derived":
                action = "derive_after_direct_operands"
            elif review_count:
                action = "adjudicate_existing_review_then_parse_residual"
            elif filing_count:
                action = "parse_cached_sources"
            else:
                action = "hydrate_then_parse"
            delta.append(
                {
                    "ticker": ticker,
                    "investment_group": "oil_tanker_operators",
                    "metric_id": metric,
                    "metric_source_mode": mode,
                    "accepted_period_count": len(period_set),
                    "accepted_first_period_end": first,
                    "accepted_last_period_end": last,
                    "review_candidate_count": review_count,
                    "sec_filing_count": filing_count,
                    "scanned_document_count": scan_count,
                    "cache_status": "AVAILABLE" if filing_count else "MISSING",
                    "delta_action": action,
                }
            )

    summary: dict[str, object] = {
        "acceptance": "PASS",
        "asof_date": asof,
        "policy_version": policy.path.stem,
        "network_requests": 0,
        "database_mode": "read_only",
        "selected_ticker_count": len(tickers),
        "raw_complete_ticker_count": sum(
            row["raw_load_status"] == "COMPLETE" for row in inventory
        ),
        "raw_incomplete_ticker_count": sum(
            row["raw_load_status"] != "COMPLETE" for row in inventory
        ),
        "tanker_delta_row_count": len(delta),
        "tanker_direct_parse_row_count": sum(
            row["metric_source_mode"] == "direct_parser"
            and row["delta_action"] != "reuse_accepted"
            for row in delta
        ),
        "calibration_candidate_metrics": sorted(
            row["metric_id"]
            for row in specialized
            if row["calibration_disposition"] == "CALIBRATION_CANDIDATE"
        ),
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
    }
    return inventory, specialized, delta, summary


def main() -> int:
    args = parse_args()
    date.fromisoformat(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    policy = load_investable_universe_policy(args.policy)
    errors, policy_summary = validate_investable_universe_policy(policy)
    if errors:
        raise ValueError(f"investable-universe policy is invalid: {errors}")
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    inventory, specialized, delta, summary = build_inventory(
        connection, policy=policy, asof=args.asof
    )
    summary["policy_validation"] = policy_summary
    write_csv_atomic(
        output_dir / "transportation_investable_v3_raw_coverage.csv",
        INVENTORY_FIELDS,
        inventory,
    )
    write_csv_atomic(
        output_dir / "transportation_investable_v3_specialized_coverage.csv",
        SPECIALIZED_FIELDS,
        specialized,
    )
    write_csv_atomic(
        output_dir / "transportation_investable_v3_tanker_delta_queue.csv",
        DELTA_FIELDS,
        delta,
    )
    write_text_atomic(
        output_dir / "transportation_investable_v3_coverage_manifest.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
