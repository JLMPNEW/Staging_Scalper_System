#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    ADAPTER_VERSION,
)
from industrials.transportation.investable_universe import (  # noqa: E402
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
DETAIL_FIELDS = (
    "run_id",
    "ticker",
    "metric_id",
    "canonical_accepted_period_count",
    "shadow_accepted_evidence_count",
    "shadow_accepted_period_count",
    "shadow_review_evidence_count",
    "shadow_review_period_count",
    "shadow_rejected_evidence_count",
    "union_accepted_period_count",
    "shadow_accepted_lift_flag",
    "coverage_disposition",
)
METRIC_FIELDS = (
    "run_id",
    "metric_id",
    "applicable_ticker_count",
    "canonical_accepted_ticker_count",
    "shadow_accepted_ticker_count",
    "shadow_accepted_new_ticker_count",
    "union_accepted_ticker_count",
    "shadow_review_ticker_count",
    "shadow_review_evidence_count",
    "minimum_accepted_breadth",
    "median_union_accepted_periods",
    "median_union_history_years",
    "breadth_gate",
    "history_gate",
    "shadow_calibration_disposition",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit canonical and shadow-parser specialized-metric coverage for "
            "the exact transportation tanker cohort without promoting evidence."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def span_years(periods: set[str]) -> float:
    dated = sorted(period for period in periods if period and period != "UNDATED")
    if len(dated) < 2:
        return 0.0
    return (date.fromisoformat(dated[-1]) - date.fromisoformat(dated[0])).days / 365.25


def latest_run_id(connection: sqlite3.Connection, *, asof: str) -> int:
    row = connection.execute(
        """
        SELECT run_id
        FROM sec_parser_run
        WHERE model_family='transportation' AND asof_date=?
          AND adapter_version=? AND status='COMPLETED'
          AND failed_work_count=0
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (asof, ADAPTER_VERSION),
    ).fetchone()
    if row is None:
        raise ValueError("no completed zero-failure tanker parser run matches the requested asof")
    return int(row[0])


def period_sets(
    rows: list[sqlite3.Row],
    *,
    status_field: str,
) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str, str], set[str]]]:
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    periods: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        status = str(row[status_field] or "").upper()
        key = (str(row["ticker"]), str(row["metric_name"]), status)
        counts[key] += 1
        period = str(row["period_end"] or "")[:10] or "UNDATED"
        periods[key].add(period)
    return counts, periods


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, "transportation")
    policy = load_investable_universe_policy(args.policy.expanduser().resolve())
    errors, _ = validate_investable_universe_policy(policy)
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
        else resolve_path(
            family["dedicated_parser"]["tanker_delta_output_root"],
            base_dir=config_path.parent,
        )
        / args.asof
    )
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    run_id = args.run_id or latest_run_id(connection, asof=args.asof)
    run = connection.execute(
        "SELECT * FROM sec_parser_run WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if (
        run is None
        or str(run["model_family"]) != "transportation"
        or str(run["adapter_version"]) != ADAPTER_VERSION
        or str(run["status"]) != "COMPLETED"
        or int(run["failed_work_count"] or 0) != 0
    ):
        raise ValueError("selected parser run is not an eligible completed transportation run")

    tickers = policy.tanker_tickers
    metrics = policy.direct_tanker_metrics
    ticker_placeholders = ",".join("?" for _ in tickers)
    metric_placeholders = ",".join("?" for _ in metrics)
    canonical_rows = connection.execute(
        f"""
        SELECT ticker, metric_name, candidate_status, period_end
        FROM fact_sec_metric_disclosure_candidate
        WHERE model_family='transportation'
          AND ticker IN ({ticker_placeholders})
          AND metric_name IN ({metric_placeholders})
          AND filing_date<=?
        """,
        (*tickers, *metrics, args.asof),
    ).fetchall()
    shadow_rows = connection.execute(
        f"""
        SELECT evidence.ticker, evidence.metric_name,
               evidence.candidate_status, evidence.period_end
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS evidence
          ON evidence.evidence_key = relation.evidence_key
        WHERE relation.run_id=?
          AND evidence.ticker IN ({ticker_placeholders})
          AND evidence.metric_name IN ({metric_placeholders})
        """,
        (run_id, *tickers, *metrics),
    ).fetchall()
    canonical_counts, canonical_periods = period_sets(canonical_rows, status_field="candidate_status")
    shadow_counts, shadow_periods = period_sets(shadow_rows, status_field="candidate_status")

    detail_rows: list[dict[str, object]] = []
    metric_rows: list[dict[str, object]] = []
    tanker_group = next(group for group in policy.groups if group.group_id == "oil_tanker_operators")
    for metric in metrics:
        canonical_tickers: set[str] = set()
        shadow_accepted_tickers: set[str] = set()
        shadow_review_tickers: set[str] = set()
        union_periods_by_ticker: dict[str, set[str]] = {}
        review_evidence_count = 0
        for ticker in tickers:
            canonical = canonical_periods.get((ticker, metric, "ACCEPTED"), set())
            shadow_accepted = shadow_periods.get((ticker, metric, "ACCEPTED"), set())
            shadow_review = set()
            shadow_review_count = 0
            for status in ("REVIEW", "REVIEW_REQUIRED", "PENDING_REVIEW"):
                shadow_review.update(shadow_periods.get((ticker, metric, status), set()))
                shadow_review_count += shadow_counts.get((ticker, metric, status), 0)
            rejected_count = shadow_counts.get((ticker, metric, "REJECTED_POLICY"), 0)
            union_periods = set(canonical) | set(shadow_accepted)
            if canonical:
                canonical_tickers.add(ticker)
            if shadow_accepted:
                shadow_accepted_tickers.add(ticker)
            if shadow_review:
                shadow_review_tickers.add(ticker)
            if union_periods:
                union_periods_by_ticker[ticker] = union_periods
            review_evidence_count += shadow_review_count
            detail_rows.append(
                {
                    "run_id": run_id,
                    "ticker": ticker,
                    "metric_id": metric,
                    "canonical_accepted_period_count": len(canonical),
                    "shadow_accepted_evidence_count": shadow_counts.get((ticker, metric, "ACCEPTED"), 0),
                    "shadow_accepted_period_count": len(shadow_accepted),
                    "shadow_review_evidence_count": shadow_review_count,
                    "shadow_review_period_count": len(shadow_review),
                    "shadow_rejected_evidence_count": rejected_count,
                    "union_accepted_period_count": len(union_periods),
                    "shadow_accepted_lift_flag": int(bool(shadow_accepted - canonical)),
                    "coverage_disposition": (
                        "CANONICAL_ACCEPTED"
                        if canonical
                        else "SHADOW_ACCEPTED_NOT_PROMOTED"
                        if shadow_accepted
                        else "SHADOW_REVIEW_REQUIRED"
                        if shadow_review
                        else "NO_ACCEPTED_EVIDENCE"
                    ),
                }
            )
        union_tickers = set(union_periods_by_ticker)
        period_depths = [len(value) for value in union_periods_by_ticker.values()]
        history_years = [span_years(value) for value in union_periods_by_ticker.values()]
        median_periods = statistics.median(period_depths) if period_depths else 0.0
        median_years = statistics.median(history_years) if history_years else 0.0
        breadth_pass = len(union_tickers) >= tanker_group.minimum_specialized_breadth
        history_pass = (
            median_periods >= policy.minimum_median_periods
            and median_years >= policy.minimum_median_history_years
        )
        metric_rows.append(
            {
                "run_id": run_id,
                "metric_id": metric,
                "applicable_ticker_count": len(tickers),
                "canonical_accepted_ticker_count": len(canonical_tickers),
                "shadow_accepted_ticker_count": len(shadow_accepted_tickers),
                "shadow_accepted_new_ticker_count": len(shadow_accepted_tickers - canonical_tickers),
                "union_accepted_ticker_count": len(union_tickers),
                "shadow_review_ticker_count": len(shadow_review_tickers),
                "shadow_review_evidence_count": review_evidence_count,
                "minimum_accepted_breadth": tanker_group.minimum_specialized_breadth,
                "median_union_accepted_periods": median_periods,
                "median_union_history_years": round(median_years, 6),
                "breadth_gate": "PASS" if breadth_pass else "FAIL",
                "history_gate": "PASS" if history_pass else "FAIL",
                "shadow_calibration_disposition": (
                    "WOULD_QUALIFY_AFTER_GOVERNED_PROMOTION"
                    if breadth_pass and history_pass
                    else "INSUFFICIENT_SHADOW_COVERAGE"
                ),
            }
        )

    qualifying = [row["metric_id"] for row in metric_rows if row["shadow_calibration_disposition"] == "WOULD_QUALIFY_AFTER_GOVERNED_PROMOTION"]
    summary: dict[str, Any] = {
        "acceptance": "PASS",
        "asof_date": args.asof,
        "run_id": run_id,
        "adapter_version": ADAPTER_VERSION,
        "ticker_count": len(tickers),
        "direct_metric_count": len(metrics),
        "shadow_evidence_count": len(shadow_rows),
        "shadow_accepted_evidence_count": sum(str(row["candidate_status"]).upper() == "ACCEPTED" for row in shadow_rows),
        "shadow_review_evidence_count": sum(str(row["candidate_status"]).upper() in {"REVIEW", "REVIEW_REQUIRED", "PENDING_REVIEW"} for row in shadow_rows),
        "shadow_rejected_evidence_count": sum(str(row["candidate_status"]).upper() == "REJECTED_POLICY" for row in shadow_rows),
        "metrics_meeting_gates_after_shadow_union": qualifying,
        "metric_count_meeting_gates_after_shadow_union": len(qualifying),
        "canonical_candidate_mutation": False,
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "REVIEW_SHADOW_EVIDENCE_AND_FREEZE_REALISTIC_METRIC_SET",
    }
    write_csv_atomic(
        output_dir / "transportation_tanker_delta_parser_coverage_by_ticker.csv",
        DETAIL_FIELDS,
        detail_rows,
    )
    write_csv_atomic(
        output_dir / "transportation_tanker_delta_parser_coverage_by_metric.csv",
        METRIC_FIELDS,
        metric_rows,
    )
    write_text_atomic(
        output_dir / "transportation_tanker_delta_parser_coverage.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
