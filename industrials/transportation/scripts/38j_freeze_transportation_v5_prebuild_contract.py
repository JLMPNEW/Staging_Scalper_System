#!/usr/bin/env python3
"""Freeze the bounded v5 PIT rebuild scope and its already-loaded source slices."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.surface_freight_score_engine import (  # noqa: E402
    load_cohort_score_policy,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


DATA = PROJECT_ROOT / "industrials" / "transportation" / "data"
OUTPUT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5"
DEFAULT_SURFACE_POLICY = DATA / "transportation_surface_freight_score_policy_v3.yaml"
DEFAULT_TANKER_POLICY = DATA / "transportation_tanker_score_policy_v1.yaml"
DEFAULT_READINESS = OUTPUT / "current_readiness" / "2026-08-13" / "transportation_v5_current_readiness.json"
DEFAULT_CURRENT_SCORES = OUTPUT / "current_scores" / "2026-08-13" / "transportation_v5_current_scores.json"
DEFAULT_CURRENT_SCORE_CSV = OUTPUT / "current_scores" / "2026-08-13" / "transportation_v5_scoring_features.csv"
DEFAULT_OUTPUT_DIR = OUTPUT / "prebuild_contract" / "2026-08-15"
DEFAULT_REPAIR_ARTIFACTS = (
    OUTPUT / "ticker_scoped_xbrl" / "2026-07-30" / "transportation_v5_ticker_scoped_xbrl_materialization.json",
    OUTPUT / "asc_operating_bridge" / "2026-08-15" / "transportation_v5_asc_operating_bridge_validation.json",
    OUTPUT / "asc_operating_bridge_load" / "2026-07-30" / "transportation_v5_asc_operating_bridge_load.json",
    OUTPUT / "financial_delta" / "2026-08-15" / "transportation_v5_financial_delta.json",
    OUTPUT / "financial_delta" / "2026-08-15" / "transportation_v5_financial_delta.csv",
)
REBUILD_CODE_PATHS = (
    PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "19_build_transportation_pit_feature_history.py",
    PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py",
    PROJECT_ROOT / "industrials" / "scripts" / "05_build_industrials_market_features.py",
    PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py",
    PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "08_build_transportation_financial_features.py",
    PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "08a_build_transportation_specialized_metrics.py",
    PROJECT_ROOT / "industrials" / "transportation" / "xbrl_backfill.py",
    PROJECT_ROOT / "industrials" / "transportation" / "surface_freight_score_engine.py",
    PROJECT_ROOT / "industrials" / "transportation" / "scoring.py",
    PROJECT_ROOT / "industrials" / "transportation" / "ticker_scoped_xbrl_backfill.py",
    PROJECT_ROOT / "industrials" / "transportation" / "reviewed_operand_repair.py",
    PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "38s_materialize_transportation_v5_ticker_scoped_xbrl.py",
    PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "38t_rebuild_transportation_v5_financial_delta.py",
    PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "38v_extract_transportation_v5_asc_operating_tables.py",
    PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "38w_validate_transportation_v5_asc_operating_bridges.py",
    PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "38x_load_transportation_v5_asc_operating_bridges.py",
    PROJECT_ROOT / "industrials" / "transportation" / "system_csvs" / "transportation_ticker_xbrl_concept_aliases.csv",
    PROJECT_ROOT / "industrials" / "transportation" / "review_policies" / "transportation_asc_operating_bridge_v1.json",
)

OBSERVED = frozenset({"REPORTED", "DERIVED", "PROXY"})
MARKET_REQUIRED = (
    "ret_3m",
    "ret_6m",
    "relative_strength_3m",
    "realized_volatility_60d",
    "maximum_drawdown_12m",
    "average_dollar_volume_60d",
)
FINANCIAL_REQUIRED = ("operating_margin", "fcf_margin", "capex_to_revenue")
CANONICAL_REQUIRED = (
    "revenue",
    "operating_income",
    "operating_cash_flow",
    "capex",
)
MAX_CANONICAL_FILING_AGE_DAYS = 550
MINIMUM_MARKET_BARS = 252
MAXIMUM_MARKET_STALENESS_DAYS = 7

PRICE_FIELDS = (
    "ticker",
    "bar_date",
    "source_id",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "dividend",
    "split_coefficient",
    "dividend_amount",
    "split_factor",
    "price_adjustment",
    "is_adjusted",
)
CANONICAL_FIELDS = (
    "ticker",
    "source_id",
    "canonical_metric",
    "period_start",
    "period_end",
    "filing_date",
    "accepted_at",
    "accession_number",
    "form_type",
    "fiscal_year",
    "fiscal_period",
    "reporting_standard",
    "taxonomy",
    "concept_name",
    "unit",
    "value",
    "value_usd",
    "source_priority",
    "canonical_quality",
)
DATE_FIELDS = (
    "cohort_id",
    "asof_date",
    "policy_active_count",
    "market_ready_count",
    "canonical_financial_ready_count",
    "legacy_required_ready_count",
    "combined_source_ready_count",
    "minimum_cross_section",
    "source_ready_gate",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="2019-01-02")
    parser.add_argument("--end-date", default="2026-07-30")
    parser.add_argument("--surface-policy", type=Path, default=DEFAULT_SURFACE_POLICY)
    parser.add_argument("--tanker-policy", type=Path, default=DEFAULT_TANKER_POLICY)
    parser.add_argument("--current-readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument("--current-scores", type=Path, default=DEFAULT_CURRENT_SCORES)
    parser.add_argument("--current-score-csv", type=Path, default=DEFAULT_CURRENT_SCORE_CSV)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--contract-version",
        default="transportation_v5_bounded_prebuild_v2",
    )
    parser.add_argument("--repair-artifact", type=Path, action="append", default=[])
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def rows_as_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def month_end_dates(
    connection: sqlite3.Connection,
    *,
    benchmark: str,
    source_id: str,
    start_date: str,
    end_date: str,
) -> list[str]:
    rows = connection.execute(
        """
        SELECT MAX(bar_date) AS asof_date
        FROM fact_price_ohlcv
        WHERE ticker=? AND source_id=? AND bar_date>=? AND bar_date<=?
        GROUP BY SUBSTR(bar_date,1,7)
        ORDER BY asof_date
        """,
        (benchmark, source_id, start_date, end_date),
    ).fetchall()
    dates = [str(row["asof_date"]) for row in rows]
    first = connection.execute(
        """
        SELECT MIN(bar_date) FROM fact_price_ohlcv
        WHERE ticker=? AND source_id=? AND bar_date>=? AND bar_date<=?
        """,
        (benchmark, source_id, start_date, end_date),
    ).fetchone()
    first_date = str(first[0] or "") if first else ""
    if first_date and first_date not in dates:
        dates.insert(0, first_date)
    return dates


def policy_scope(policy: dict[str, Any]) -> tuple[set[str], dict[str, dict[str, Any]]]:
    current = {str(item).upper() for item in policy["eligible_tickers"]}
    historical = {
        str(ticker).upper(): dict(entry)
        for ticker, entry in (policy.get("historical_calibration_only") or {}).items()
    }
    return current, historical


def active_for_policy(
    ticker: str,
    asof: str,
    *,
    current: set[str],
    historical: dict[str, dict[str, Any]],
    membership_bounds: dict[str, tuple[str, str]],
) -> bool:
    if ticker in historical:
        entry = historical[ticker]
        return str(entry["effective_from"])[:10] <= asof <= str(entry["effective_to"])[:10]
    if ticker not in current:
        return False
    start, end = membership_bounds.get(ticker, ("9999-12-31", "0001-01-01"))
    return start <= asof <= end


def latest_canonical_is_ready(
    facts: dict[tuple[str, str], list[tuple[str, str]]],
    *,
    ticker: str,
    asof: str,
) -> bool:
    asof_date = date.fromisoformat(asof)
    for metric in CANONICAL_REQUIRED:
        usable = [
            filing_date
            for filing_date, _period_end in facts.get((ticker, metric), [])
            if filing_date <= asof
            and 0 <= (asof_date - date.fromisoformat(filing_date)).days
            <= MAX_CANONICAL_FILING_AGE_DAYS
        ]
        if not usable:
            return False
    return True


def price_source_is_ready(
    prices: dict[tuple[str, str], list[str]],
    *,
    ticker: str,
    asof: str,
    source_ids: list[str],
) -> bool:
    asof_date = date.fromisoformat(asof)
    best: tuple[int, date, int, int] | None = None
    for priority, source_id in enumerate(source_ids):
        usable = [
            bar_date
            for bar_date in prices.get((ticker, source_id), [])
            if bar_date <= asof
        ]
        if not usable:
            continue
        latest = date.fromisoformat(usable[-1])
        key = (
            int(len(usable) >= MINIMUM_MARKET_BARS),
            latest,
            len(usable),
            -priority,
        )
        if best is None or key > best:
            best = key
    return bool(
        best
        and best[0] == 1
        and 0 <= (asof_date - best[1]).days <= MAXIMUM_MARKET_STALENESS_DAYS
    )


def artifact(path: Path) -> dict[str, Any]:
    return {"path": str(path), "sha256": file_sha256(path)}


def main() -> int:
    args = parse_args()
    contract_version = str(args.contract_version).strip()
    if not contract_version:
        raise ValueError("--contract-version cannot be blank")
    start_date = date.fromisoformat(str(args.start_date)[:10]).isoformat()
    end_date = date.fromisoformat(str(args.end_date)[:10]).isoformat()
    if start_date > end_date:
        raise ValueError("start date cannot be after end date")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    surface_path = args.surface_policy.expanduser().resolve()
    tanker_path = args.tanker_policy.expanduser().resolve()
    readiness_path = args.current_readiness.expanduser().resolve()
    current_scores_path = args.current_scores.expanduser().resolve()
    current_score_csv_path = args.current_score_csv.expanduser().resolve()
    repair_artifact_paths = [path.resolve() for path in DEFAULT_REPAIR_ARTIFACTS]
    repair_artifact_paths.extend(path.expanduser().resolve() for path in args.repair_artifact)
    output_dir = args.output_dir.expanduser().resolve()
    manifest_path = output_dir / "transportation_v5_prebuild_contract.json"
    if manifest_path.exists() and not args.allow_overwrite:
        raise FileExistsError(f"immutable prebuild contract already exists: {manifest_path}")

    surface = load_cohort_score_policy(surface_path)
    tanker = load_cohort_score_policy(tanker_path)
    readiness = read_json(readiness_path)
    current_scores = read_json(current_scores_path)
    current_rows = read_csv(current_score_csv_path)
    errors: list[str] = []
    if readiness.get("acceptance") != "PASS":
        errors.append("current readiness is not PASS")
    if current_scores.get("acceptance") != "PASS":
        errors.append("current score gate is not PASS")
    for path in repair_artifact_paths:
        if not path.is_file():
            errors.append(f"missing repair artifact={path}")
            continue
        if path.suffix.lower() == ".json":
            repair_payload = read_json(path)
            if repair_payload.get("acceptance") != "PASS":
                errors.append(f"repair artifact is not PASS={path}")
    score_artifacts = current_scores.get("artifacts") or {}
    expected_policy_hashes = {
        "surface_score_policy": file_sha256(surface_path),
        "tanker_score_policy": file_sha256(tanker_path),
    }
    for artifact_id, expected_hash in expected_policy_hashes.items():
        pinned_hash = str((score_artifacts.get(artifact_id) or {}).get("sha256") or "")
        if pinned_hash != expected_hash:
            errors.append(
                f"current score gate has stale {artifact_id} hash={pinned_hash} expected={expected_hash}"
            )

    policies = (surface, tanker)
    current_by_cohort: dict[str, set[str]] = {}
    historical_by_cohort: dict[str, dict[str, dict[str, Any]]] = {}
    for policy in policies:
        current, historical = policy_scope(policy)
        cohort_id = str(policy["cohort_id"])
        current_by_cohort[cohort_id] = current
        historical_by_cohort[cohort_id] = historical
    current_tickers = set().union(*current_by_cohort.values())
    historical_tickers = set().union(
        *(set(items) for items in historical_by_cohort.values())
    )
    if current_tickers & historical_tickers:
        errors.append("current and historical-only scopes overlap")
    if len(current_tickers) != 35 or len(historical_tickers) != 9:
        errors.append(
            f"governed scope is current={len(current_tickers)} historical={len(historical_tickers)} expected=35/9"
        )
    scored_tickers = {str(row.get("ticker") or "").upper() for row in current_rows}
    if scored_tickers != current_tickers:
        errors.append("current score CSV does not contain the exact 35-name current scope")
    if any(str(row.get("rank_ready_flag") or "") != "1" for row in current_rows):
        errors.append("current score CSV contains blocked rows")

    scope = sorted(current_tickers | historical_tickers)
    benchmark_tickers = ["IYT", "XTN", "SPY"]
    primary_source = str(
        cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted")
    )
    fallback_sources = [
        str(item)
        for item in cfg_get(config, "market_data_policy.scoring_fallback_sources", [])
    ]
    source_ids = list(dict.fromkeys([primary_source, *fallback_sources]))
    slice_start = date.fromisoformat(start_date).replace(year=date.fromisoformat(start_date).year - 2).isoformat()

    with read_only(db_path) as connection:
        marks = ",".join("?" for _ in scope)
        membership_bounds = {
            str(row["ticker"]): (
                str(row["start_date"]),
                str(row["end_date"]),
            )
            for row in connection.execute(
                f"""
                SELECT ticker,MIN(start_date) AS start_date,
                       MAX(COALESCE(end_date,'9999-12-31')) AS end_date
                FROM dim_universe_membership
                WHERE model_family=? AND ticker IN ({marks})
                GROUP BY ticker
                """,
                (MODEL_FAMILY, *scope),
            ).fetchall()
        }
        taxonomy = {
            str(row["ticker"]): dict(row)
            for row in connection.execute(
                f"""
                SELECT ticker,calibration_cohort_id,calibration_use,development_stage
                FROM dim_industrials_taxonomy
                WHERE model_family=? AND ticker IN ({marks})
                """,
                (MODEL_FAMILY, *scope),
            ).fetchall()
        }
        for ticker in historical_tickers:
            if taxonomy.get(ticker, {}).get("calibration_use") != "historical_research":
                errors.append(f"{ticker}: historical-only ticker lacks historical_research taxonomy")
        for cohort_id, entries in historical_by_cohort.items():
            for ticker, entry in entries.items():
                actual_start, actual_end = membership_bounds.get(
                    ticker, ("9999-12-31", "0001-01-01")
                )
                if str(entry["effective_from"]) < actual_start or str(entry["effective_to"]) > actual_end:
                    errors.append(f"{cohort_id}/{ticker}: policy dates exceed loaded PIT membership")

        dates = month_end_dates(
            connection,
            benchmark="IYT",
            source_id=primary_source,
            start_date=start_date,
            end_date=end_date,
        )
        if len(dates) < 48:
            errors.append(f"historical date grid={len(dates)} below 48")

        all_price_tickers = sorted(set(scope) | set(benchmark_tickers))
        price_marks = ",".join("?" for _ in all_price_tickers)
        source_marks = ",".join("?" for _ in source_ids)
        price_rows = rows_as_dicts(
            connection.execute(
                f"""
                SELECT {','.join(PRICE_FIELDS)}
                FROM fact_price_ohlcv
                WHERE ticker IN ({price_marks}) AND source_id IN ({source_marks})
                  AND bar_date>=? AND bar_date<=?
                ORDER BY ticker,bar_date,source_id
                """,
                (*all_price_tickers, *source_ids, slice_start, end_date),
            ).fetchall()
        )
        price_counts = Counter(str(row["ticker"]) for row in price_rows)
        prices: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in price_rows:
            if finite(row.get("adj_close")):
                prices[(str(row["ticker"]), str(row["source_id"]))].append(
                    str(row["bar_date"])
                )
        missing_prices = sorted(ticker for ticker in all_price_tickers if not price_counts[ticker])
        if missing_prices:
            errors.append(f"price slice missing tickers={missing_prices}")

        canonical_rows = rows_as_dicts(
            connection.execute(
                f"""
                SELECT {','.join(CANONICAL_FIELDS)}
                FROM fact_financial_statement_canonical
                WHERE model_family=? AND ticker IN ({marks})
                  AND canonical_metric IN ({','.join('?' for _ in CANONICAL_REQUIRED)})
                  AND filing_date<=? AND period_end>=?
                ORDER BY ticker,canonical_metric,filing_date,period_end,source_priority,source_id
                """,
                (MODEL_FAMILY, *scope, *CANONICAL_REQUIRED, end_date, slice_start),
            ).fetchall()
        )
        canonical_facts: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for row in canonical_rows:
            if finite(row.get("value_usd")) or finite(row.get("value")):
                canonical_facts[(str(row["ticker"]), str(row["canonical_metric"]))].append(
                    (str(row["filing_date"]), str(row["period_end"]))
                )

        metric_ids = (*MARKET_REQUIRED, *FINANCIAL_REQUIRED)
        availability: dict[tuple[str, str, str], str] = {}
        for row in connection.execute(
            f"""
            SELECT ticker,asof_date,metric_name,availability_status
            FROM feature_financial_metric_availability
            WHERE model_family=? AND ticker IN ({marks})
              AND metric_name IN ({','.join('?' for _ in metric_ids)})
              AND asof_date>=? AND asof_date<=?
            """,
            (MODEL_FAMILY, *scope, *metric_ids, start_date, end_date),
        ).fetchall():
            availability[(str(row["ticker"]), str(row["asof_date"]), str(row["metric_name"]))] = str(
                row["availability_status"]
            )

    date_rows: list[dict[str, Any]] = []
    contribution: dict[str, Counter[str]] = defaultdict(Counter)
    cohort_summary: dict[str, dict[str, Any]] = {}
    for policy in policies:
        cohort_id = str(policy["cohort_id"])
        current = current_by_cohort[cohort_id]
        historical = historical_by_cohort[cohort_id]
        cohort_scope = sorted(current | set(historical))
        minimum = int(policy["minimum_active_cohort_size"])
        required_source_dates = int(
            policy["historical_prebuild_gate"]["minimum_source_ready_dates"]
        )
        passing_dates = 0
        for asof in dates:
            active = [
                ticker
                for ticker in cohort_scope
                if active_for_policy(
                    ticker,
                    asof,
                    current=current,
                    historical=historical,
                    membership_bounds=membership_bounds,
                )
            ]
            market_ready = {
                ticker
                for ticker in active
                if price_source_is_ready(
                    prices,
                    ticker=ticker,
                    asof=asof,
                    source_ids=source_ids,
                )
            }
            canonical_ready = {
                ticker
                for ticker in active
                if latest_canonical_is_ready(
                    canonical_facts, ticker=ticker, asof=asof
                )
            }
            legacy_ready = {
                ticker
                for ticker in market_ready
                if all(
                    availability.get((ticker, asof, metric)) in OBSERVED
                    for metric in FINANCIAL_REQUIRED
                )
            }
            combined = market_ready & (canonical_ready | legacy_ready)
            gate = len(combined) >= minimum
            passing_dates += int(gate)
            for ticker in combined:
                contribution[ticker][cohort_id] += 1
            date_rows.append(
                {
                    "cohort_id": cohort_id,
                    "asof_date": asof,
                    "policy_active_count": len(active),
                    "market_ready_count": len(market_ready),
                    "canonical_financial_ready_count": len(canonical_ready),
                    "legacy_required_ready_count": len(legacy_ready),
                    "combined_source_ready_count": len(combined),
                    "minimum_cross_section": minimum,
                    "source_ready_gate": "PASS" if gate else "FAIL",
                }
            )
        zero_contributors = sorted(
            ticker for ticker in cohort_scope if contribution[ticker][cohort_id] == 0
        )
        if passing_dates < required_source_dates:
            errors.append(
                f"{cohort_id}: source-ready dates={passing_dates} below {required_source_dates}"
            )
        if zero_contributors:
            errors.append(f"{cohort_id}: zero-contribution tickers={zero_contributors}")
        cohort_summary[cohort_id] = {
            "current_ticker_count": len(current),
            "historical_calibration_only_count": len(historical),
            "total_pit_scope_count": len(cohort_scope),
            "minimum_cross_section": minimum,
            "source_ready_date_count": passing_dates,
            "required_source_ready_date_count": required_source_dates,
            "historical_prebuild_rationale": policy["historical_prebuild_gate"]["rationale"],
            "zero_contribution_tickers": zero_contributors,
            "contribution_dates_by_ticker": {
                ticker: contribution[ticker][cohort_id] for ticker in cohort_scope
            },
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    scope_path = output_dir / "transportation_v5_historical_rebuild_scope.csv"
    price_path = output_dir / "transportation_v5_prebuild_price_slice.csv"
    canonical_path = output_dir / "transportation_v5_prebuild_canonical_slice.csv"
    date_path = output_dir / "transportation_v5_source_readiness_by_date.csv"
    scope_rows = [
        {
            "ticker": ticker,
            "scope_role": "current_portfolio_candidate" if ticker in current_tickers else "historical_calibration_only",
            "cohort_id": next(
                cohort_id
                for cohort_id in current_by_cohort
                if ticker in current_by_cohort[cohort_id]
                or ticker in historical_by_cohort[cohort_id]
            ),
            "effective_from": (
                next(
                    entries[ticker]["effective_from"]
                    for entries in historical_by_cohort.values()
                    if ticker in entries
                )
                if ticker in historical_tickers
                else membership_bounds[ticker][0]
            ),
            "effective_to": (
                next(
                    entries[ticker]["effective_to"]
                    for entries in historical_by_cohort.values()
                    if ticker in entries
                )
                if ticker in historical_tickers
                else membership_bounds[ticker][1]
            ),
            "portfolio_eligible": "1" if ticker in current_tickers else "0",
        }
        for ticker in scope
    ]
    write_csv_atomic(
        scope_path,
        ("ticker", "scope_role", "cohort_id", "effective_from", "effective_to", "portfolio_eligible"),
        scope_rows,
    )
    write_csv_atomic(price_path, PRICE_FIELDS, price_rows)
    write_csv_atomic(canonical_path, CANONICAL_FIELDS, canonical_rows)
    write_csv_atomic(date_path, DATE_FIELDS, date_rows)

    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "contract_version": contract_version,
        "start_date": start_date,
        "end_date": end_date,
        "historical_date_count": len(dates),
        "current_ticker_count": len(current_tickers),
        "historical_calibration_only_count": len(historical_tickers),
        "bounded_rebuild_ticker_count": len(scope),
        "current_tickers": sorted(current_tickers),
        "historical_calibration_only_tickers": sorted(historical_tickers),
        "explicitly_excluded_historical_tickers": {
            "NNA": "zero complete required-metric snapshots; partial operating-cost tag rejected by semantic validation",
        },
        "cohort_readiness": cohort_summary,
        "source_readiness_rule": {
            "market_metrics": list(MARKET_REQUIRED),
            "financial_metrics": list(FINANCIAL_REQUIRED),
            "canonical_rebuild_inputs": list(CANONICAL_REQUIRED),
            "maximum_canonical_filing_age_days": MAX_CANONICAL_FILING_AGE_DAYS,
            "minimum_market_bars": MINIMUM_MARKET_BARS,
            "maximum_market_staleness_days": MAXIMUM_MARKET_STALENESS_DAYS,
            "minimum_source_ready_dates": "cohort_specific_policy_gate",
        },
        "price_sources": source_ids,
        "network_requests": 0,
        "parser_invocations": 0,
        "historical_reconstruction_performed": False,
        "historical_reconstruction_authorized": not errors,
        "calibration_authorized": False,
        "production_activation_authorized": False,
        "artifacts": {
            "config": artifact(config_path),
            "surface_score_policy": artifact(surface_path),
            "tanker_score_policy": artifact(tanker_path),
            "current_readiness": artifact(readiness_path),
            "current_scores": artifact(current_scores_path),
            "current_score_csv": artifact(current_score_csv_path),
            "bounded_rebuild_scope": artifact(scope_path),
            "price_slice": artifact(price_path),
            "canonical_financial_slice": artifact(canonical_path),
            "source_readiness_by_date": artifact(date_path),
            "repair_lineage": {
                path.name: artifact(path) for path in repair_artifact_paths if path.is_file()
            },
            "rebuild_code": {
                path.relative_to(PROJECT_ROOT).as_posix(): artifact(path)
                for path in REBUILD_CODE_PATHS
            },
        },
        "errors": errors,
        "next_gate": (
            "RUN_ONE_BOUNDED_44_NAME_PIT_REBUILD"
            if not errors
            else "REPAIR_PREBUILD_SOURCE_OR_POLICY_GAPS"
        ),
    }
    write_text_atomic(manifest_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
