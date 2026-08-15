#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.dedicated_parser_adapter import ADAPTER_VERSION  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    SurfaceMetricDomainRule,
    load_investable_universe_policy,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DATA_ROOT = PROJECT_ROOT / "industrials" / "transportation" / "data"
POLICY = DATA_ROOT / "transportation_investable_universe_v3.yaml"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
)
DETAIL_FIELDS = (
    "run_id",
    "metric_id",
    "comparison_domain_id",
    "ticker",
    "calibration_eligibility",
    "required_accepted_breadth",
    "canonical_accepted_period_count",
    "shadow_accepted_evidence_count",
    "shadow_accepted_period_count",
    "shadow_review_evidence_count",
    "shadow_review_period_count",
    "fact_store_review_candidate_count",
    "fact_store_review_period_count",
    "union_accepted_period_count",
    "potential_post_review_period_count",
    "shadow_accepted_lift_flag",
    "coverage_disposition",
)
DOMAIN_FIELDS = (
    "run_id",
    "metric_id",
    "comparison_domain_id",
    "domain_applicable_ticker_count",
    "minimum_accepted_fraction",
    "required_accepted_breadth",
    "calibration_eligibility",
    "normalization_scope",
    "canonical_accepted_ticker_count",
    "shadow_accepted_ticker_count",
    "shadow_accepted_new_ticker_count",
    "union_accepted_ticker_count",
    "accepted_applicable_fraction",
    "potential_post_review_ticker_count",
    "potential_post_review_fraction",
    "shadow_review_ticker_count",
    "shadow_review_evidence_count",
    "fact_store_review_ticker_count",
    "fact_store_review_candidate_count",
    "median_union_accepted_periods",
    "median_union_history_years",
    "median_potential_post_review_periods",
    "median_potential_post_review_history_years",
    "breadth_gate",
    "history_gate",
    "potential_breadth_gate",
    "potential_history_gate",
    "shadow_calibration_disposition",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit canonical, run-scoped shadow, and fact-store review coverage "
            "against metric-specific surface-freight comparison domains."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument("--fact-store-candidates", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _span_years(periods: set[str]) -> float:
    dated = sorted(period for period in periods if period and period != "UNDATED")
    if len(dated) < 2:
        return 0.0
    return (date.fromisoformat(dated[-1]) - date.fromisoformat(dated[0])).days / 365.25


def _period_sets(
    rows: Iterable[Mapping[str, object]],
    *,
    metric_field: str,
    status_field: str | None,
) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str, str], set[str]]]:
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    periods: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        status = (
            str(row[status_field] or "").upper()
            if status_field is not None
            else "REVIEW_REQUIRED"
        )
        key = (str(row["ticker"]), str(row[metric_field]), status)
        counts[key] += 1
        periods[key].add(str(row["period_end"] or "")[:10] or "UNDATED")
    return counts, periods


def _latest_run(connection: sqlite3.Connection, asof: str) -> int:
    row = connection.execute(
        "SELECT run_id FROM sec_parser_run "
        "WHERE model_family='transportation' AND asof_date=? "
        "AND status='COMPLETED' AND failed_work_count=0 "
        "ORDER BY run_id DESC LIMIT 1",
        (asof,),
    ).fetchone()
    if row is None:
        raise ValueError("no completed zero-failure transportation parser run matches the requested asof")
    return int(row[0])


def _direct_rules(policy: Any) -> tuple[SurfaceMetricDomainRule, ...]:
    return tuple(
        rule
        for rule in policy.surface_metric_domain_rules
        if rule.metric_id != "surface_volume_growth"
    )


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
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
    output_dir.mkdir(parents=True, exist_ok=True)
    fact_store_path = (
        args.fact_store_candidates.expanduser().resolve()
        if args.fact_store_candidates
        else output_dir / "transportation_surface_fact_store_ratio_candidates.csv"
    )

    policy = load_investable_universe_policy(POLICY)
    surface_group = next(group for group in policy.groups if group.group_id == "surface_freight_core")
    rules = _direct_rules(policy)
    metrics = tuple(sorted({rule.metric_id for rule in rules}))
    all_tickers = tuple(surface_group.tickers)

    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    run_id = args.run_id or _latest_run(connection, args.asof)
    run = connection.execute("SELECT * FROM sec_parser_run WHERE run_id=?", (run_id,)).fetchone()
    if (
        run is None
        or str(run["model_family"]) != "transportation"
        or str(run["asof_date"]) != args.asof
        or str(run["status"]) != "COMPLETED"
        or int(run["failed_work_count"] or 0) != 0
    ):
        raise ValueError("selected parser run is not an eligible completed surface run")
    source_adapter_version = str(run["adapter_version"])

    ticker_placeholders = ",".join("?" for _ in all_tickers)
    metric_placeholders = ",".join("?" for _ in metrics)
    canonical_rows = connection.execute(
        "SELECT ticker, metric_name, candidate_status, period_end "
        "FROM fact_sec_metric_disclosure_candidate "
        "WHERE model_family='transportation' "
        f"AND ticker IN ({ticker_placeholders}) AND metric_name IN ({metric_placeholders}) "
        "AND filing_date<=?",
        (*all_tickers, *metrics, args.asof),
    ).fetchall()
    shadow_rows = connection.execute(
        "SELECT evidence.ticker, evidence.metric_name, evidence.candidate_status, evidence.period_end "
        "FROM sec_parser_run_metric_evidence AS relation "
        "JOIN sec_parser_metric_evidence_shadow AS evidence "
        "ON evidence.evidence_key=relation.evidence_key "
        "WHERE relation.run_id=? "
        f"AND evidence.ticker IN ({ticker_placeholders}) "
        f"AND evidence.metric_name IN ({metric_placeholders})",
        (run_id, *all_tickers, *metrics),
    ).fetchall()
    fact_store_rows = [
        row
        for row in _csv_rows(fact_store_path)
        if row.get("ticker") in set(all_tickers) and row.get("metric_id") in set(metrics)
    ]
    canonical_counts, canonical_periods = _period_sets(
        canonical_rows, metric_field="metric_name", status_field="candidate_status"
    )
    shadow_counts, shadow_periods = _period_sets(
        shadow_rows, metric_field="metric_name", status_field="candidate_status"
    )
    fact_counts, fact_periods = _period_sets(
        fact_store_rows, metric_field="metric_id", status_field=None
    )

    detail_rows: list[dict[str, object]] = []
    domain_rows: list[dict[str, object]] = []
    for rule in rules:
        metric = rule.metric_id
        tickers = rule.applicable_tickers
        canonical_tickers: set[str] = set()
        shadow_accepted_tickers: set[str] = set()
        shadow_review_tickers: set[str] = set()
        fact_review_tickers: set[str] = set()
        union_by_ticker: dict[str, set[str]] = {}
        potential_by_ticker: dict[str, set[str]] = {}
        shadow_review_evidence_count = 0
        fact_review_candidate_count = 0
        for ticker in tickers:
            canonical = canonical_periods.get((ticker, metric, "ACCEPTED"), set())
            shadow_accepted = shadow_periods.get((ticker, metric, "ACCEPTED"), set())
            shadow_review: set[str] = set()
            shadow_review_count = 0
            for status in ("REVIEW", "REVIEW_REQUIRED", "PENDING_REVIEW"):
                shadow_review.update(shadow_periods.get((ticker, metric, status), set()))
                shadow_review_count += shadow_counts.get((ticker, metric, status), 0)
            fact_review = fact_periods.get((ticker, metric, "REVIEW_REQUIRED"), set())
            fact_review_count = fact_counts.get((ticker, metric, "REVIEW_REQUIRED"), 0)
            union = set(canonical) | set(shadow_accepted)
            potential = union | set(shadow_review) | set(fact_review)
            if canonical:
                canonical_tickers.add(ticker)
            if shadow_accepted:
                shadow_accepted_tickers.add(ticker)
            if shadow_review:
                shadow_review_tickers.add(ticker)
            if fact_review:
                fact_review_tickers.add(ticker)
            if union:
                union_by_ticker[ticker] = union
            if potential:
                potential_by_ticker[ticker] = potential
            shadow_review_evidence_count += shadow_review_count
            fact_review_candidate_count += fact_review_count
            detail_rows.append(
                {
                    "run_id": run_id,
                    "metric_id": metric,
                    "comparison_domain_id": rule.comparison_domain_id,
                    "ticker": ticker,
                    "calibration_eligibility": rule.calibration_eligibility,
                    "required_accepted_breadth": rule.minimum_accepted_breadth,
                    "canonical_accepted_period_count": len(canonical),
                    "shadow_accepted_evidence_count": shadow_counts.get((ticker, metric, "ACCEPTED"), 0),
                    "shadow_accepted_period_count": len(shadow_accepted),
                    "shadow_review_evidence_count": shadow_review_count,
                    "shadow_review_period_count": len(shadow_review),
                    "fact_store_review_candidate_count": fact_review_count,
                    "fact_store_review_period_count": len(fact_review),
                    "union_accepted_period_count": len(union),
                    "potential_post_review_period_count": len(potential),
                    "shadow_accepted_lift_flag": int(bool(shadow_accepted - canonical)),
                    "coverage_disposition": (
                        "ACCEPTED"
                        if union
                        else "SEMANTIC_REVIEW_REQUIRED"
                        if potential
                        else "NO_DISCOVERED_EVIDENCE"
                    ),
                }
            )

        union_tickers = set(union_by_ticker)
        potential_tickers = set(potential_by_ticker)
        accepted_depths = [len(periods) for periods in union_by_ticker.values()]
        accepted_spans = [_span_years(periods) for periods in union_by_ticker.values()]
        potential_depths = [len(periods) for periods in potential_by_ticker.values()]
        potential_spans = [_span_years(periods) for periods in potential_by_ticker.values()]
        median_periods = statistics.median(accepted_depths) if accepted_depths else 0.0
        median_years = statistics.median(accepted_spans) if accepted_spans else 0.0
        median_potential_periods = statistics.median(potential_depths) if potential_depths else 0.0
        median_potential_years = statistics.median(potential_spans) if potential_spans else 0.0
        candidate = rule.is_calibration_candidate
        breadth_pass = candidate and len(union_tickers) >= rule.minimum_accepted_breadth
        history_pass = candidate and (
            median_periods >= policy.minimum_median_periods
            and median_years >= policy.minimum_median_history_years
        )
        potential_breadth_pass = candidate and len(potential_tickers) >= rule.minimum_accepted_breadth
        potential_history_pass = candidate and (
            median_potential_periods >= policy.minimum_median_periods
            and median_potential_years >= policy.minimum_median_history_years
        )
        if not candidate:
            disposition = "DIAGNOSTIC_ONLY_BY_POLICY"
        elif breadth_pass and history_pass:
            disposition = "WOULD_QUALIFY_AFTER_GOVERNED_PROMOTION"
        elif potential_breadth_pass and potential_history_pass:
            disposition = "SEMANTIC_REVIEW_COULD_MEET_DOMAIN_GATES"
        else:
            disposition = "INSUFFICIENT_DISCOVERED_DOMAIN_COVERAGE"
        domain_rows.append(
            {
                "run_id": run_id,
                "metric_id": metric,
                "comparison_domain_id": rule.comparison_domain_id,
                "domain_applicable_ticker_count": len(tickers),
                "minimum_accepted_fraction": rule.minimum_accepted_fraction,
                "required_accepted_breadth": rule.minimum_accepted_breadth,
                "calibration_eligibility": rule.calibration_eligibility,
                "normalization_scope": rule.normalization_scope,
                "canonical_accepted_ticker_count": len(canonical_tickers),
                "shadow_accepted_ticker_count": len(shadow_accepted_tickers),
                "shadow_accepted_new_ticker_count": len(shadow_accepted_tickers - canonical_tickers),
                "union_accepted_ticker_count": len(union_tickers),
                "accepted_applicable_fraction": round(len(union_tickers) / len(tickers), 6),
                "potential_post_review_ticker_count": len(potential_tickers),
                "potential_post_review_fraction": round(len(potential_tickers) / len(tickers), 6),
                "shadow_review_ticker_count": len(shadow_review_tickers),
                "shadow_review_evidence_count": shadow_review_evidence_count,
                "fact_store_review_ticker_count": len(fact_review_tickers),
                "fact_store_review_candidate_count": fact_review_candidate_count,
                "median_union_accepted_periods": median_periods,
                "median_union_history_years": round(median_years, 6),
                "median_potential_post_review_periods": median_potential_periods,
                "median_potential_post_review_history_years": round(median_potential_years, 6),
                "breadth_gate": "PASS" if breadth_pass else "FAIL",
                "history_gate": "PASS" if history_pass else "FAIL",
                "potential_breadth_gate": "PASS" if potential_breadth_pass else "FAIL",
                "potential_history_gate": "PASS" if potential_history_pass else "FAIL",
                "shadow_calibration_disposition": disposition,
            }
        )

    qualifying = [
        f"{row['metric_id']}::{row['comparison_domain_id']}"
        for row in domain_rows
        if row["shadow_calibration_disposition"] == "WOULD_QUALIFY_AFTER_GOVERNED_PROMOTION"
    ]
    potential = [
        f"{row['metric_id']}::{row['comparison_domain_id']}"
        for row in domain_rows
        if row["shadow_calibration_disposition"] == "SEMANTIC_REVIEW_COULD_MEET_DOMAIN_GATES"
    ]
    summary: dict[str, Any] = {
        "acceptance": "PASS",
        "asof_date": args.asof,
        "run_id": run_id,
        "audit_adapter_version": ADAPTER_VERSION,
        "source_run_adapter_version": source_adapter_version,
        "surface_domain_policy_version": policy.surface_domain_policy_version,
        "surface_domain_mapping_sha256": file_sha256(
            policy.surface_metric_domain_mapping_path
        ),
        "surface_metric_source_map_sha256": file_sha256(
            policy.surface_metric_source_map_path
        ),
        "investable_universe_policy_sha256": file_sha256(POLICY),
        "ticker_count": len(all_tickers),
        "direct_metric_count": len(metrics),
        "metric_domain_rule_count": len(rules),
        "candidate_metric_domain_rule_count": sum(rule.is_calibration_candidate for rule in rules),
        "diagnostic_metric_domain_rule_count": sum(not rule.is_calibration_candidate for rule in rules),
        "shadow_evidence_count": len(shadow_rows),
        "fact_store_review_candidate_count": len(fact_store_rows),
        "metric_domains_meeting_gates_after_shadow_union": qualifying,
        "metric_domain_count_meeting_gates_after_shadow_union": len(qualifying),
        "metric_domains_that_could_meet_gates_after_semantic_review": potential,
        "metric_domain_count_that_could_meet_gates_after_semantic_review": len(potential),
        "disposition_counts": dict(
            sorted(Counter(str(row["shadow_calibration_disposition"]) for row in domain_rows).items())
        ),
        "canonical_candidate_mutation": False,
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "SEMANTICALLY_VALIDATE_DOMAIN_SCOPED_REVIEW_EVIDENCE_ONCE",
    }
    write_csv_atomic(
        output_dir / "transportation_surface_delta_parser_coverage_by_ticker_domain.csv",
        DETAIL_FIELDS,
        detail_rows,
    )
    write_csv_atomic(
        output_dir / "transportation_surface_delta_parser_coverage_by_metric_domain.csv",
        DOMAIN_FIELDS,
        domain_rows,
    )
    write_text_atomic(
        output_dir / "transportation_surface_delta_parser_domain_coverage.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
