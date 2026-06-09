#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, init_db  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_technology_market_stage")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FEATURE_STAGE = "build_technology_market_features"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Stage 3 technology market-data gates.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Validation date for staleness checks. Defaults to today.")
    parser.add_argument("--strict-history", action="store_true", help="Fail low-history tickers instead of review-only.")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def scalar(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def value(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def read_expected_ticker_count(config: dict[str, Any], base_dir: Path) -> int:
    policy_path = resolve_path(cfg_get(config, "technology_universe.policy_path"), base_dir=base_dir)
    policy = load_yaml(policy_path)
    return int(policy.get("expected_ticker_count") or 0)


def load_universe(conn: Any, model_family: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT c.ticker
        FROM dim_company c
        JOIN dim_technology_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family,),
    ).fetchall()
    return [normalize_ticker(row["ticker"]) for row in rows if normalize_ticker(row["ticker"])]


def validate() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors") or "semiconductors")
    source_id = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted") or "yahoo_finance_adjusted")
    benchmark_tickers = [normalize_ticker(x) for x in cfg_get(config, "technology_universe.benchmark_tickers", [])]
    benchmark_tickers = [ticker for ticker in benchmark_tickers if ticker]
    expected_count = read_expected_ticker_count(config, base_dir)
    min_days = int(cfg_get(config, "market_data_policy.min_trading_days_for_full_features", 252))
    min_avg_dollar_volume_60d = float(cfg_get(config, "market_data_policy.min_avg_dollar_volume_60d_for_full_features", 0) or 0)
    max_staleness_days = int(cfg_get(config, "market_data_policy.max_staleness_days", 7))
    audit_asof = parse_date(args.asof) or date.today()

    errors: list[str] = []
    warnings: list[str] = []

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        source_status = value(conn, "SELECT status FROM source_registry WHERE source_id = ?", (source_id,))
        if source_status != "active":
            errors.append(f"Source {source_id} is not active in source_registry: {source_status!r}")

        universe = load_universe(conn, model_family)
        if expected_count and len(universe) != expected_count:
            errors.append(f"Universe count mismatch: expected={expected_count} actual={len(universe)}")

        all_tickers = sorted(set(universe + benchmark_tickers))
        ph_all = placeholders(all_tickers)
        ph_universe = placeholders(universe)
        params_all = (source_id, *all_tickers)
        params_universe = (source_id, *universe)

        price_rows = conn.execute(
            f"""
            SELECT
                ticker,
                COUNT(*) AS bar_count,
                SUM(CASE WHEN adj_close IS NOT NULL THEN 1 ELSE 0 END) AS adjusted_count,
                SUM(CASE WHEN COALESCE(price_adjustment, '') <> '' THEN 1 ELSE 0 END) AS explicit_adjustment_count,
                MIN(bar_date) AS first_bar_date,
                MAX(bar_date) AS latest_bar_date
            FROM fact_price_ohlcv
            WHERE source_id = ? AND ticker IN ({ph_all})
            GROUP BY ticker
            """,
            params_all,
        ).fetchall()
        price_by_ticker = {str(row["ticker"]): row for row in price_rows}
        missing_prices = [ticker for ticker in all_tickers if ticker not in price_by_ticker]
        if missing_prices:
            errors.append(f"Missing adjusted OHLCV for tickers: {missing_prices}")

        low_history: list[str] = []
        stale: list[str] = []
        no_adjusted: list[str] = []
        no_adjustment_status: list[str] = []
        for ticker in all_tickers:
            row = price_by_ticker.get(ticker)
            if row is None:
                continue
            bar_count = int(row["bar_count"] or 0)
            adjusted_count = int(row["adjusted_count"] or 0)
            explicit_adjustment_count = int(row["explicit_adjustment_count"] or 0)
            latest_bar = parse_date(row["latest_bar_date"])
            if adjusted_count == 0:
                no_adjusted.append(ticker)
            if explicit_adjustment_count != bar_count:
                no_adjustment_status.append(ticker)
            if bar_count < min_days:
                low_history.append(f"{ticker}:{bar_count}")
            if latest_bar is None or (audit_asof - latest_bar).days > max_staleness_days:
                stale_days = "" if latest_bar is None else str((audit_asof - latest_bar).days)
                stale.append(f"{ticker}:{stale_days}")

        if no_adjusted:
            errors.append(f"Tickers with no adjusted close: {no_adjusted}")
        if no_adjustment_status:
            errors.append(f"Tickers missing explicit price adjustment status: {no_adjustment_status}")
        if stale:
            errors.append(f"Tickers stale beyond {max_staleness_days} days: {stale}")
        if low_history and args.strict_history:
            errors.append(f"Low-history tickers under {min_days} bars: {low_history}")
        elif low_history:
            warnings.append(f"Low-history review tickers under {min_days} bars: {low_history}")

        snapshot_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_market_snapshot WHERE source_id = ? AND ticker IN ({ph_all})",
            params_all,
        )
        if snapshot_tickers != len(all_tickers):
            errors.append(f"Market snapshot ticker coverage mismatch: expected={len(all_tickers)} actual={snapshot_tickers}")

        corporate_actions = scalar(conn, "SELECT COUNT(*) FROM fact_corporate_action WHERE source_id = ?", (source_id,))
        if corporate_actions == 0:
            errors.append(f"No corporate actions loaded for source_id={source_id}")

        feature_asof = value(
            conn,
            "SELECT MAX(asof_date) FROM feature_market_technical WHERE source_id = ? AND model_family = ?",
            (source_id, model_family),
        )
        if not feature_asof:
            errors.append(f"No market technical features found for model_family={model_family}")
            feature_count = 0
            review_features: list[str] = []
            missing_features = universe
        else:
            feature_count = scalar(
                conn,
                f"""
                SELECT COUNT(*)
                FROM feature_market_technical
                WHERE source_id = ?
                  AND model_family = ?
                  AND asof_date = ?
                  AND ticker IN ({ph_universe})
                """,
                (source_id, model_family, feature_asof, *universe),
            )
            feature_rows = conn.execute(
                f"""
                SELECT ticker, market_data_quality, low_history_flag, low_liquidity_flag, stale_flag
                FROM feature_market_technical
                WHERE source_id = ?
                  AND model_family = ?
                  AND asof_date = ?
                  AND ticker IN ({ph_universe})
                ORDER BY ticker
                """,
                (source_id, model_family, feature_asof, *universe),
            ).fetchall()
            feature_tickers = {str(row["ticker"]) for row in feature_rows}
            missing_features = [ticker for ticker in universe if ticker not in feature_tickers]
            review_features = [
                f"{row['ticker']}:{row['market_data_quality']}"
                for row in feature_rows
                if str(row["market_data_quality"] or "") != "complete"
            ]
            stale_features = [str(row["ticker"]) for row in feature_rows if int(row["stale_flag"] or 0) == 1]
            low_liquidity_features = [str(row["ticker"]) for row in feature_rows if int(row["low_liquidity_flag"] or 0) == 1]
            missing_quality = [
                str(row["ticker"])
                for row in feature_rows
                if str(row["market_data_quality"] or "") in {"", "missing"}
            ]
            if feature_count != len(universe):
                errors.append(f"Market feature row count mismatch: expected={len(universe)} actual={feature_count}")
            if missing_features:
                errors.append(f"Missing market feature rows: {missing_features}")
            if stale_features:
                errors.append(f"Stale market feature rows: {stale_features}")
            if missing_quality:
                errors.append(f"Missing-quality market feature rows: {missing_quality}")

        review_issue_count = scalar(
            conn,
            "SELECT COUNT(*) FROM data_quality_issues WHERE stage = ? AND issue_type = 'market_feature_review'",
            (FEATURE_STAGE,),
        )
        if feature_asof and len(review_features) != review_issue_count:
            errors.append(f"Feature review issue mismatch: features={len(review_features)} issues={review_issue_count}")

        total_bars = scalar(conn, f"SELECT COUNT(*) FROM fact_price_ohlcv WHERE source_id = ? AND ticker IN ({ph_all})", params_all)
        raw_response_count = scalar(
            conn,
            f"SELECT COUNT(DISTINCT endpoint || COALESCE(query_params_json, '')) FROM raw_api_responses WHERE source_id = ? AND asof_date = ?",
            (source_id, audit_asof.isoformat()),
        )

        warnings.append(f"Universe tickers={len(universe)} benchmarks={len(benchmark_tickers)} total_symbols={len(all_tickers)}")
        warnings.append(f"Adjusted OHLCV rows={total_bars} covered_symbols={len(price_by_ticker)}")
        warnings.append(f"Market snapshots covered_symbols={snapshot_tickers}")
        warnings.append(f"Corporate actions rows={corporate_actions}")
        warnings.append(f"Market feature asof={feature_asof or ''} rows={feature_count} review={len(review_features)}")
        warnings.append(
            f"Low-liquidity feature flags={len(low_liquidity_features) if feature_asof else 0} "
            f"threshold_60d_adtv={min_avg_dollar_volume_60d:g}"
        )
        warnings.append(f"Raw Yahoo responses for {audit_asof.isoformat()}={raw_response_count}")

    for message in warnings:
        LOGGER.info(message)
    if errors:
        for message in errors:
            LOGGER.error(message)
        return 1
    LOGGER.info("Technology Stage 3 market-data validation passed for model_family=%s", model_family)
    return 0


if __name__ == "__main__":
    raise SystemExit(validate())
