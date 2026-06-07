#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sec_form4_config import cfg_get, load_sec_form4_config

DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\STAGING\DB\sec_insider.sqlite")
DEFAULT_OUTPUT_DIR = Path("output/sec_form4_staging")


def default_db_path() -> Path:
    return Path(os.getenv("SEC_INSIDER_DB_PATH", str(DEFAULT_DB_PATH)))


def default_output_dir() -> Path:
    return Path(os.getenv("SEC_INSIDER_REPORT_DIR", str(DEFAULT_OUTPUT_DIR)))

DEFAULT_BUY_LATEST_FILENAME_TEMPLATE = "form4_buy_latest_events_top{top_n}.csv"
DEFAULT_BUY_DAILY_SCORE_FILENAME_TEMPLATE = "form4_buy_daily_symbol_scores_top{top_n}.csv"
DEFAULT_BUY_CLUSTER_SIGNALS_FILENAME_TEMPLATE = "form4_buy_cluster_signals_top{cluster_top_n}.csv"
DEFAULT_SELL_LATEST_FILENAME_TEMPLATE = "form4_sell_latest_events_top{top_n}.csv"
DEFAULT_SELL_DAILY_SCORE_FILENAME_TEMPLATE = "form4_sell_daily_symbol_scores_top{top_n}.csv"
DEFAULT_SELL_CLUSTER_SIGNALS_FILENAME_TEMPLATE = "form4_sell_cluster_signals_top{cluster_top_n}.csv"
DEFAULT_TRADEABLE_CANDIDATES_FILENAME_TEMPLATE = "form4_tradeable_buy_candidates_top{top_n}.csv"

DATE_SORT_SQL_TEMPLATE = """
CASE
    WHEN {col} IS NULL THEN NULL
    WHEN length({col}) = 10
         AND substr({col}, 5, 1) = '-'
         AND substr({col}, 8, 1) = '-'
    THEN {col}
    WHEN length({col}) = 11
         AND substr({col}, 3, 1) = '-'
         AND substr({col}, 7, 1) = '-'
    THEN substr({col}, 8, 4) || '-' ||
         CASE upper(substr({col}, 4, 3))
             WHEN 'JAN' THEN '01'
             WHEN 'FEB' THEN '02'
             WHEN 'MAR' THEN '03'
             WHEN 'APR' THEN '04'
             WHEN 'MAY' THEN '05'
             WHEN 'JUN' THEN '06'
             WHEN 'JUL' THEN '07'
             WHEN 'AUG' THEN '08'
             WHEN 'SEP' THEN '09'
             WHEN 'OCT' THEN '10'
             WHEN 'NOV' THEN '11'
             WHEN 'DEC' THEN '12'
             ELSE '00'
         END || '-' || substr({col}, 1, 2)
    ELSE NULL
END
"""
DATE_SORT_FILING = DATE_SORT_SQL_TEMPLATE.format(col="filing_date")
DATE_SORT_FILING_A = DATE_SORT_SQL_TEMPLATE.format(col="a.filing_date")
DATE_SORT_FILING_T = DATE_SORT_SQL_TEMPLATE.format(col="t.filing_date")
DATE_ORDER_FILING = f"(({DATE_SORT_FILING}) IS NULL) ASC, ({DATE_SORT_FILING}) DESC"
DATE_ORDER_FILING_A = f"(({DATE_SORT_FILING_A}) IS NULL) ASC, ({DATE_SORT_FILING_A}) DESC"
DATE_ORDER_FILING_T = f"(({DATE_SORT_FILING_T}) IS NULL) ASC, ({DATE_SORT_FILING_T}) DESC"

QUERY_LATEST_EVENTS_TEMPLATE = """
WITH selected_as_of AS (
    SELECT COALESCE(
        :as_of_date,
        (SELECT MAX(as_of_date) FROM stock_signal_snapshot_tier1)
    ) AS as_of_date
)
SELECT
    t.filing_date,
    t.issuer_trading_symbol,
    t.rptowner_name,
    t.rptowner_relationship,
    t.security_title,
    t.trans_shares,
    t.trans_price_per_share,
    t.trade_value_usd,
    {score_select_expr}
FROM form4_events_tier1 t
JOIN selected_as_of x
  ON t.filing_date_sort IS NOT NULL
 AND t.filing_date_sort <= x.as_of_date
WHERE t.signal_side = '{side}'
ORDER BY
    {date_order_expr},
    {score_order_expr} DESC
LIMIT :top_n;
"""

QUERY_DAILY_SYMBOL_SCORES_TEMPLATE = """
WITH selected_as_of AS (
    SELECT COALESCE(
        :as_of_date,
        (SELECT MAX(as_of_date) FROM stock_signal_snapshot_tier1)
    ) AS as_of_date
),
base AS (
    SELECT
        {date_sort_expr} AS filing_dt,
        t.issuer_trading_symbol,
        COALESCE(t.{score_col}, 0.0) AS side_score
    FROM form4_events_tier1 t
    JOIN selected_as_of x
      ON t.filing_date_sort IS NOT NULL
     AND t.filing_date_sort <= x.as_of_date
    WHERE t.signal_side = '{side}'
)
SELECT
    filing_dt AS filing_date,
    issuer_trading_symbol,
    COUNT(*) AS event_count,
    ROUND(SUM(side_score), 4) AS score_sum
FROM base
GROUP BY filing_dt, issuer_trading_symbol
ORDER BY
    ((filing_dt) IS NULL) ASC,
    filing_dt DESC,
    score_sum DESC
LIMIT :top_n;
"""

QUERY_CLUSTER_REPORT_TEMPLATE = """
WITH selected_as_of AS (
    SELECT COALESCE(
        :as_of_date,
        (SELECT MAX(as_of_date) FROM stock_signal_snapshot_tier1)
    ) AS as_of_date
)
SELECT
    t.filing_date,
    t.issuer_trading_symbol,
    COUNT(*) AS event_count,
    COUNT(
        DISTINCT COALESCE(
            NULLIF(t.rptowner_cik, ''),
            NULLIF(t.rptowner_name, ''),
            t.accession_number || '|' || t.nonderiv_trans_sk
        )
    ) AS distinct_insiders,
    ROUND(SUM(COALESCE(t.trans_shares, 0.0)), 4) AS shares_sum,
    ROUND(SUM(COALESCE(t.trade_value_usd, 0.0)), 2) AS notional_sum_usd,
    ROUND(SUM(COALESCE(t.{side_score_col}, 0.0)), 4) AS side_score_sum,
    ROUND(SUM(COALESCE(t.raw_event_score, 0.0)), 4) AS raw_event_score_sum,
    ROUND(AVG(COALESCE(t.cluster_weight, 1.0)), 4) AS avg_cluster_weight,
    MAX(COALESCE(t.cluster_insiders_5bd, 0)) AS cluster_insiders_5bd_max,
    MAX(COALESCE(t.cluster_insiders_10bd, 0)) AS cluster_insiders_10bd_max,
    MAX(COALESCE(t.cluster_insiders_20bd, 0)) AS cluster_insiders_20bd_max,
    SUM(CASE WHEN COALESCE(t.aff10b5one_flag, 0) = 1 THEN 1 ELSE 0 END) AS trades_10b5
FROM form4_events_tier1 t
JOIN selected_as_of x
  ON t.filing_date_sort IS NOT NULL
 AND t.filing_date_sort <= x.as_of_date
WHERE t.signal_side = '{side}'
GROUP BY t.filing_date, t.issuer_trading_symbol
HAVING
    COUNT(
        DISTINCT COALESCE(
            NULLIF(t.rptowner_cik, ''),
            NULLIF(t.rptowner_name, ''),
            t.accession_number || '|' || t.nonderiv_trans_sk
        )
    ) >= :cluster_min_distinct_insiders
    OR (
        COUNT(*) >= :cluster_min_trades
        AND SUM(COALESCE(t.trade_value_usd, 0.0)) >= :cluster_min_notional_usd
    )
ORDER BY
    {date_order_expr},
    side_score_sum DESC,
    notional_sum_usd DESC
LIMIT :cluster_top_n;
"""

QUERY_TRADEABLE_BUY_CANDIDATES = """
WITH selected_as_of AS (
    SELECT COALESCE(
        :as_of_date,
        (SELECT MAX(as_of_date) FROM stock_signal_snapshot_tier1)
    ) AS as_of_date
),
latest_buy AS (
    SELECT
        t.issuer_cik,
        t.issuer_trading_symbol,
        t.filing_date,
        t.accepted_ts_utc,
        t.tradable_date,
        t.tradable_session,
        t.trade_value_usd,
        t.tradeable_alpha_score,
        t.routine_flag,
        t.opportunistic_flag,
        t.liquidity_pass,
        t.close_px,
        t.adv20_usd,
        ROW_NUMBER() OVER (
            PARTITION BY t.issuer_cik
            ORDER BY t.filing_date_sort DESC, t.event_id DESC
        ) AS rn
    FROM form4_events_tier1 t
    JOIN selected_as_of x
      ON t.filing_date_sort IS NOT NULL
     AND t.filing_date_sort <= x.as_of_date
    WHERE t.signal_side = 'BUY'
),
base AS (
    SELECT
        s.as_of_date,
        s.issuer_cik,
        s.issuer_trading_symbol,
        s.long_rank_score,
        s.buy_score_20bd,
        s.sell_score_20bd,
        s.net_score,
        s.exit_risk_score,
        s.buy_cluster_10bd_max,
        s.buy_cluster_20bd_max,
        s.distinct_buy_insiders_10bd,
        s.action_bucket,
        lb.filing_date,
        lb.accepted_ts_utc,
        lb.tradable_date,
        lb.tradable_session,
        lb.trade_value_usd,
        lb.tradeable_alpha_score,
        lb.routine_flag,
        lb.opportunistic_flag,
        lb.liquidity_pass,
        lb.close_px,
        lb.adv20_usd
    FROM stock_signal_snapshot_tier1 s
    JOIN selected_as_of x
      ON s.as_of_date = x.as_of_date
    LEFT JOIN latest_buy lb
      ON s.issuer_cik = lb.issuer_cik
     AND lb.rn = 1
    WHERE s.action_bucket IN ('BUY_TIER_1', 'BUY_WATCH')
)
SELECT
    as_of_date,
    issuer_cik,
    issuer_trading_symbol,
    action_bucket,
    ROUND(
        COALESCE(tradeable_alpha_score, 0.0) + COALESCE(long_rank_score, 0.0),
        4
    ) AS candidate_score,
    ROUND(COALESCE(tradeable_alpha_score, 0.0), 4) AS tradeable_alpha_score,
    ROUND(COALESCE(long_rank_score, 0.0), 4) AS long_rank_score,
    ROUND(COALESCE(buy_score_20bd, 0.0), 4) AS buy_score_20bd,
    ROUND(COALESCE(sell_score_20bd, 0.0), 4) AS sell_score_20bd,
    ROUND(COALESCE(net_score, 0.0), 4) AS net_score,
    ROUND(COALESCE(exit_risk_score, 0.0), 4) AS exit_risk_score,
    distinct_buy_insiders_10bd,
    buy_cluster_10bd_max,
    buy_cluster_20bd_max,
    filing_date,
    accepted_ts_utc,
    tradable_date,
    tradable_session,
    ROUND(COALESCE(trade_value_usd, 0.0), 2) AS trade_value_usd,
    routine_flag,
    opportunistic_flag,
    ROUND(COALESCE(close_px, 0.0), 6) AS close_px,
    ROUND(COALESCE(adv20_usd, 0.0), 2) AS adv20_usd,
    liquidity_pass
FROM base
WHERE (:liquidity_only = 0 OR COALESCE(liquidity_pass, 0) = 1)
  AND (:min_price <= 0 OR COALESCE(close_px, 0.0) >= :min_price)
  AND (:min_adv20_usd <= 0 OR COALESCE(adv20_usd, 0.0) >= :min_adv20_usd)
ORDER BY candidate_score DESC, long_rank_score DESC
LIMIT :top_n;
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Form 4 buy/sell reports and export CSV files."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to SEC Form 4 YAML config.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help=f"SQLite DB path (default: {default_db_path()})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=f"Directory for report CSV files (default: {default_output_dir()})",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        help="Row limit for the first two reports (default: 50).",
    )
    parser.add_argument(
        "--as-of-date",
        type=str,
        default=None,
        help="Snapshot date (YYYY-MM-DD). Default uses latest available snapshot date.",
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=None,
        help="Optional minimum close price filter for tradeable candidates.",
    )
    parser.add_argument(
        "--min-adv20-usd",
        type=float,
        default=None,
        help="Optional minimum ADV20 USD filter for tradeable candidates.",
    )
    parser.add_argument(
        "--liquidity-only",
        dest="liquidity_only",
        action="store_true",
        help="Require liquidity_pass=1 for tradeable candidates.",
    )
    parser.add_argument(
        "--no-liquidity-only",
        dest="liquidity_only",
        action="store_false",
        help="Do not require liquidity_pass=1 for tradeable candidates.",
    )
    parser.add_argument(
        "--write-legacy-reports",
        dest="write_legacy_reports",
        action="store_true",
        help="Write legacy latest/daily/cluster report CSVs.",
    )
    parser.add_argument(
        "--no-write-legacy-reports",
        dest="write_legacy_reports",
        action="store_false",
        help="Skip legacy latest/daily/cluster report CSVs.",
    )
    parser.add_argument(
        "--min-distinct-insiders",
        type=int,
        default=None,
        help="Minimum insider count for cluster reports (default: 2).",
    )
    parser.add_argument(
        "--cluster-top-n",
        type=int,
        default=None,
        help="Row limit for buy/sell cluster reports (default: 100).",
    )
    parser.add_argument(
        "--cluster-min-trades",
        type=int,
        default=None,
        help="Minimum trades threshold for cluster candidate fallback (default: 3).",
    )
    parser.add_argument(
        "--cluster-min-notional-usd",
        type=float,
        default=None,
        help="Minimum notional USD threshold for cluster candidate fallback (default: 100000).",
    )
    parser.set_defaults(liquidity_only=None, write_legacy_reports=None)
    return parser.parse_args()


def assert_form4_tables_exist(conn: sqlite3.Connection) -> None:
    required = (
        "form4_events_tier1",
        "stock_signal_snapshot_tier1",
    )
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [name for name in required if name not in existing]
    if missing:
        raise RuntimeError(
            "Required Form 4 tables missing: "
            f"{', '.join(missing)}. Run helper_scripts/build_form4_buy_events_v1.py first."
        )

    tier1_cols = {
        row[1].lower()
        for row in conn.execute("PRAGMA table_info(form4_events_tier1)")
    }
    required_tier1_cols = {
        "filing_date",
        "issuer_trading_symbol",
        "rptowner_name",
        "rptowner_relationship",
        "security_title",
        "trans_shares",
        "trans_price_per_share",
        "trade_value_usd",
        "rptowner_cik",
        "signal_side",
        "buy_score",
        "sell_risk_score",
        "cluster_weight",
        "cluster_insiders_5bd",
        "cluster_insiders_10bd",
        "cluster_insiders_20bd",
        "event_score",
        "raw_event_score",
        "aff10b5one_flag",
        "accession_number",
        "nonderiv_trans_sk",
        "accepted_ts_utc",
        "tradable_date",
        "tradable_session",
        "routine_flag",
        "opportunistic_flag",
        "close_px",
        "adv20_usd",
        "liquidity_pass",
        "tradeable_alpha_score",
    }
    missing_cols = sorted(required_tier1_cols - tier1_cols)
    if missing_cols:
        raise RuntimeError(
            "form4_events_tier1 is missing required columns for buy/sell reports: "
            f"{', '.join(missing_cols)}. Rebuild with helper_scripts/build_form4_buy_events_v1.py."
        )

    snapshot_cols = {
        row[1].lower()
        for row in conn.execute("PRAGMA table_info(stock_signal_snapshot_tier1)")
    }
    required_snapshot_cols = {
        "as_of_date",
        "issuer_cik",
        "issuer_trading_symbol",
        "buy_score_20bd",
        "sell_score_20bd",
        "net_score",
        "long_rank_score",
        "exit_risk_score",
        "buy_cluster_10bd_max",
        "buy_cluster_20bd_max",
        "distinct_buy_insiders_10bd",
        "action_bucket",
    }
    missing_snapshot_cols = sorted(required_snapshot_cols - snapshot_cols)
    if missing_snapshot_cols:
        raise RuntimeError(
            "stock_signal_snapshot_tier1 is missing required columns: "
            f"{', '.join(missing_snapshot_cols)}. Rebuild with helper_scripts/build_form4_buy_events_v1.py."
        )


def build_cluster_report_sql(side: str) -> str:
    side_up = str(side).strip().upper()
    if side_up not in {"BUY", "SELL"}:
        raise ValueError(f"Unsupported side for cluster report: {side!r}")
    side_score_col = "buy_score" if side_up == "BUY" else "sell_risk_score"
    return QUERY_CLUSTER_REPORT_TEMPLATE.format(
        side=side_up,
        side_score_col=side_score_col,
        date_order_expr=DATE_ORDER_FILING_T,
    )


def build_latest_events_sql(side: str) -> str:
    side_up = str(side).strip().upper()
    if side_up == "SELL":
        score_select_expr = "COALESCE(t.sell_risk_score, 0.0) AS sell_risk_score"
        score_order_expr = "COALESCE(t.sell_risk_score, 0.0)"
    else:
        side_up = "BUY"
        score_select_expr = "COALESCE(t.event_score, 0.0) AS event_score"
        score_order_expr = "COALESCE(t.event_score, 0.0)"
    return QUERY_LATEST_EVENTS_TEMPLATE.format(
        side=side_up,
        score_select_expr=score_select_expr,
        score_order_expr=score_order_expr,
        date_order_expr=DATE_ORDER_FILING_T,
    )


def build_daily_scores_sql(side: str) -> str:
    side_up = str(side).strip().upper()
    if side_up == "SELL":
        score_col = "sell_risk_score"
    else:
        side_up = "BUY"
        score_col = "buy_score"
    return QUERY_DAILY_SYMBOL_SCORES_TEMPLATE.format(
        side=side_up,
        score_col=score_col,
        date_sort_expr=DATE_SORT_FILING_T,
    )


def resolve_report_filename(
    raw_template: object,
    *,
    default_template: str,
    top_n: int,
    cluster_top_n: int | None = None,
) -> str:
    template = str(raw_template).strip() if raw_template is not None else ""
    if template == "":
        template = default_template
    format_kwargs = {
        "top_n": top_n,
        "cluster_top_n": cluster_top_n if cluster_top_n is not None else top_n,
    }
    try:
        rendered = template.format(**format_kwargs)
    except Exception:
        rendered = default_template.format(**format_kwargs)
    return Path(rendered).name


def normalize_date_tag(raw_value: object) -> str:
    raw = str(raw_value).strip() if raw_value is not None else ""
    if not raw:
        return ""
    parsed = pd.to_datetime(raw, errors="coerce")
    if pd.notna(parsed):
        return parsed.strftime("%Y-%m-%d")
    parsed_head = pd.to_datetime(raw[:10], errors="coerce")
    if pd.notna(parsed_head):
        return parsed_head.strftime("%Y-%m-%d")
    return ""


def resolve_output_date_tag(*, as_of_date: object, latest_snapshot_as_of: object) -> str:
    return (
        normalize_date_tag(as_of_date)
        or normalize_date_tag(latest_snapshot_as_of)
        or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )


def append_date_suffix(filename: str, *, date_tag: str) -> str:
    path = Path(filename)
    if not date_tag:
        return path.name
    suffix = path.suffix
    stem = path.stem
    tag = f"_{date_tag}"
    if stem.endswith(tag):
        return path.name
    return f"{stem}{tag}{suffix}"


def main() -> None:
    args = parse_args()
    _, cfg = load_sec_form4_config(args.config)
    reports_cfg = cfg_get(cfg, "reports", default={})

    db_path = Path(
        args.db_path
        if args.db_path is not None
        else cfg_get(cfg, "db_path", default=str(default_db_path()))
    ).expanduser().resolve()
    output_dir = Path(
        args.output_dir
        if args.output_dir is not None
        else cfg_get(cfg, "report_output_dir", default=str(default_output_dir()))
    ).expanduser().resolve()
    top_n = max(
        1,
        int(
            args.top_n
            if args.top_n is not None
            else cfg_get(reports_cfg, "top_n", default=50)
        ),
    )
    as_of_date = (
        args.as_of_date
        if args.as_of_date is not None
        else cfg_get(reports_cfg, "as_of_date", default=None)
    )
    min_price = max(
        0.0,
        float(
            args.min_price
            if args.min_price is not None
            else cfg_get(reports_cfg, "min_price", default=0.0)
        ),
    )
    min_adv20_usd = max(
        0.0,
        float(
            args.min_adv20_usd
            if args.min_adv20_usd is not None
            else cfg_get(reports_cfg, "min_adv20_usd", default=0.0)
        ),
    )
    liquidity_only = (
        bool(args.liquidity_only)
        if args.liquidity_only is not None
        else bool(cfg_get(reports_cfg, "liquidity_only", default=False))
    )
    write_legacy_reports = (
        bool(args.write_legacy_reports)
        if args.write_legacy_reports is not None
        else bool(cfg_get(reports_cfg, "write_legacy_reports", default=True))
    )
    min_distinct_insiders = max(
        1,
        int(
            args.min_distinct_insiders
            if args.min_distinct_insiders is not None
            else cfg_get(reports_cfg, "min_distinct_insiders", default=2)
        ),
    )
    cluster_top_n = max(
        1,
        int(
            args.cluster_top_n
            if args.cluster_top_n is not None
            else cfg_get(reports_cfg, "cluster_top_n", default=top_n)
        ),
    )
    cluster_min_trades = max(
        1,
        int(
            args.cluster_min_trades
            if args.cluster_min_trades is not None
            else cfg_get(reports_cfg, "cluster_min_trades", default=3)
        ),
    )
    cluster_min_notional_usd = max(
        0.0,
        float(
            args.cluster_min_notional_usd
            if args.cluster_min_notional_usd is not None
            else cfg_get(reports_cfg, "cluster_min_notional_usd", default=100000.0)
        ),
    )
    buy_latest_filename = resolve_report_filename(
        cfg_get(
            reports_cfg,
            "buy_latest_filename_template",
            default=DEFAULT_BUY_LATEST_FILENAME_TEMPLATE,
        ),
        default_template=DEFAULT_BUY_LATEST_FILENAME_TEMPLATE,
        top_n=top_n,
        cluster_top_n=cluster_top_n,
    )
    buy_daily_scores_filename = resolve_report_filename(
        cfg_get(
            reports_cfg,
            "buy_daily_scores_filename_template",
            default=DEFAULT_BUY_DAILY_SCORE_FILENAME_TEMPLATE,
        ),
        default_template=DEFAULT_BUY_DAILY_SCORE_FILENAME_TEMPLATE,
        top_n=top_n,
        cluster_top_n=cluster_top_n,
    )
    sell_latest_filename = resolve_report_filename(
        cfg_get(
            reports_cfg,
            "sell_latest_filename_template",
            default=DEFAULT_SELL_LATEST_FILENAME_TEMPLATE,
        ),
        default_template=DEFAULT_SELL_LATEST_FILENAME_TEMPLATE,
        top_n=top_n,
        cluster_top_n=cluster_top_n,
    )
    sell_daily_scores_filename = resolve_report_filename(
        cfg_get(
            reports_cfg,
            "sell_daily_scores_filename_template",
            default=DEFAULT_SELL_DAILY_SCORE_FILENAME_TEMPLATE,
        ),
        default_template=DEFAULT_SELL_DAILY_SCORE_FILENAME_TEMPLATE,
        top_n=top_n,
        cluster_top_n=cluster_top_n,
    )
    buy_cluster_signals_filename = resolve_report_filename(
        cfg_get(
            reports_cfg,
            "buy_cluster_signals_filename_template",
            default=DEFAULT_BUY_CLUSTER_SIGNALS_FILENAME_TEMPLATE,
        ),
        default_template=DEFAULT_BUY_CLUSTER_SIGNALS_FILENAME_TEMPLATE,
        top_n=top_n,
        cluster_top_n=cluster_top_n,
    )
    sell_cluster_signals_filename = resolve_report_filename(
        cfg_get(
            reports_cfg,
            "sell_cluster_signals_filename_template",
            default=DEFAULT_SELL_CLUSTER_SIGNALS_FILENAME_TEMPLATE,
        ),
        default_template=DEFAULT_SELL_CLUSTER_SIGNALS_FILENAME_TEMPLATE,
        top_n=top_n,
        cluster_top_n=cluster_top_n,
    )
    tradeable_candidates_filename = resolve_report_filename(
        cfg_get(
            reports_cfg,
            "tradeable_candidates_filename_template",
            default=DEFAULT_TRADEABLE_CANDIDATES_FILENAME_TEMPLATE,
        ),
        default_template=DEFAULT_TRADEABLE_CANDIDATES_FILENAME_TEMPLATE,
        top_n=top_n,
        cluster_top_n=cluster_top_n,
    )

    if not db_path.exists():
        raise FileNotFoundError(f"SQLite DB not found: {db_path}")

    output_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        assert_form4_tables_exist(conn)
        latest_snapshot_as_of = conn.execute(
            "SELECT MAX(as_of_date) FROM stock_signal_snapshot_tier1"
        ).fetchone()[0]
        output_date_tag = resolve_output_date_tag(
            as_of_date=as_of_date,
            latest_snapshot_as_of=latest_snapshot_as_of,
        )

        tradeable_candidates_df = pd.read_sql_query(
            QUERY_TRADEABLE_BUY_CANDIDATES,
            conn,
            params={
                "as_of_date": as_of_date,
                "min_price": min_price,
                "min_adv20_usd": min_adv20_usd,
                "liquidity_only": 1 if liquidity_only else 0,
                "top_n": top_n,
            },
        )

        latest_df = pd.DataFrame()
        daily_scores_df = pd.DataFrame()
        latest_sell_df = pd.DataFrame()
        daily_sell_scores_df = pd.DataFrame()
        buy_cluster_signals_df = pd.DataFrame()
        sell_cluster_signals_df = pd.DataFrame()
        if write_legacy_reports:
            latest_df = pd.read_sql_query(
                build_latest_events_sql("BUY"),
                conn,
                params={"top_n": top_n, "as_of_date": as_of_date},
            )
            daily_scores_df = pd.read_sql_query(
                build_daily_scores_sql("BUY"),
                conn,
                params={"top_n": top_n, "as_of_date": as_of_date},
            )

            latest_sell_df = pd.read_sql_query(
                build_latest_events_sql("SELL"),
                conn,
                params={"top_n": top_n, "as_of_date": as_of_date},
            )
            daily_sell_scores_df = pd.read_sql_query(
                build_daily_scores_sql("SELL"),
                conn,
                params={"top_n": top_n, "as_of_date": as_of_date},
            )

            cluster_params_common = {
                "as_of_date": as_of_date,
                "cluster_top_n": cluster_top_n,
                "cluster_min_distinct_insiders": min_distinct_insiders,
                "cluster_min_trades": cluster_min_trades,
                "cluster_min_notional_usd": cluster_min_notional_usd,
            }
            buy_cluster_signals_df = pd.read_sql_query(
                build_cluster_report_sql("BUY"),
                conn,
                params=cluster_params_common,
            )
            sell_cluster_signals_df = pd.read_sql_query(
                build_cluster_report_sql("SELL"),
                conn,
                params=cluster_params_common,
            )

    tradeable_candidates_filename = append_date_suffix(
        tradeable_candidates_filename,
        date_tag=output_date_tag,
    )
    buy_latest_filename = append_date_suffix(
        buy_latest_filename,
        date_tag=output_date_tag,
    )
    buy_daily_scores_filename = append_date_suffix(
        buy_daily_scores_filename,
        date_tag=output_date_tag,
    )
    sell_latest_filename = append_date_suffix(
        sell_latest_filename,
        date_tag=output_date_tag,
    )
    sell_daily_scores_filename = append_date_suffix(
        sell_daily_scores_filename,
        date_tag=output_date_tag,
    )
    buy_cluster_signals_filename = append_date_suffix(
        buy_cluster_signals_filename,
        date_tag=output_date_tag,
    )
    sell_cluster_signals_filename = append_date_suffix(
        sell_cluster_signals_filename,
        date_tag=output_date_tag,
    )

    tradeable_candidates_path = output_dir / tradeable_candidates_filename
    tradeable_candidates_df.to_csv(tradeable_candidates_path, index=False)
    print(f"Wrote {len(tradeable_candidates_df):,} rows -> {tradeable_candidates_path}")

    if write_legacy_reports:
        latest_path = output_dir / buy_latest_filename
        daily_scores_path = output_dir / buy_daily_scores_filename
        latest_sell_path = output_dir / sell_latest_filename
        daily_sell_scores_path = output_dir / sell_daily_scores_filename
        buy_cluster_signals_path = output_dir / buy_cluster_signals_filename
        sell_cluster_signals_path = output_dir / sell_cluster_signals_filename

        latest_df.to_csv(latest_path, index=False)
        daily_scores_df.to_csv(daily_scores_path, index=False)
        latest_sell_df.to_csv(latest_sell_path, index=False)
        daily_sell_scores_df.to_csv(daily_sell_scores_path, index=False)
        buy_cluster_signals_df.to_csv(buy_cluster_signals_path, index=False)
        sell_cluster_signals_df.to_csv(sell_cluster_signals_path, index=False)

        print(f"Wrote {len(latest_df):,} rows -> {latest_path}")
        print(f"Wrote {len(daily_scores_df):,} rows -> {daily_scores_path}")
        print(f"Wrote {len(latest_sell_df):,} rows -> {latest_sell_path}")
        print(f"Wrote {len(daily_sell_scores_df):,} rows -> {daily_sell_scores_path}")
        print(f"Wrote {len(buy_cluster_signals_df):,} rows -> {buy_cluster_signals_path}")
        print(f"Wrote {len(sell_cluster_signals_df):,} rows -> {sell_cluster_signals_path}")


if __name__ == "__main__":
    main()
