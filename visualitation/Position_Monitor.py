"""Tier-1 portfolio command center over sealed, point-in-time artifacts.

The dashboard is a read-only consumer.  It never writes to portfolio-layer,
broker, macro, or index-correlation inputs.
"""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime
from typing import cast
import io
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

RUNS_ROOT = PROJECT_ROOT / "portfolio_layer" / "output" / "runs"
IB_REPORTS_ROOT = PROJECT_ROOT / "IB_reports"
DB_PATH = PROJECT_ROOT / "portfolio_layer" / "db" / "portfolio_layer.sqlite"
H1_ROOT = PROJECT_ROOT / "portfolio_layer" / "MacroLayer" / "out" / "regime_h1"
SERVING_DB_PATH = PROJECT_ROOT / "portfolio_layer" / "MacroLayer" / "macro_serving.sqlite"
CORRELATION_ROOT = PROJECT_ROOT / "output" / "index_correlations"
SECURITY_MAPPING_PATHS = (
    PROJECT_ROOT / "ticker_mapping" / "All_tickers_biotech_enriched.csv",
    PROJECT_ROOT / "ticker_mapping" / "med_dev_tickers_clean_keep.csv",
    PROJECT_ROOT / "ticker_mapping" / "semiconductor_tickers.csv",
    PROJECT_ROOT / "ticker_mapping" / "software_infrastructure_tickers.csv",
    PROJECT_ROOT / "ticker_mapping" / "technology_hardware.csv",
    PROJECT_ROOT / "ticker_mapping" / "machinery_tickers.csv",
    PROJECT_ROOT / "ticker_mapping" / "defense_tickers.csv",
    PROJECT_ROOT / "ticker_mapping" / "consumer_defensive.csv",
    PROJECT_ROOT / "industrials" / "transportation" / "system_csvs" / "transportation_tickers.csv",
    PROJECT_ROOT / "portfolio_layer" / "data" / "canonical_sector_overrides.csv",
)

# ---------------------------------------------------------------------------
# Palette (validated reference palette, light mode - see dataviz skill)
# ---------------------------------------------------------------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES_BLUE = "#2a78d6"      # categorical slot 1
SERIES_ORANGE = "#e68a00"     # categorical slot 2
STATUS_GOOD = "#0ca30c"
STATUS_CRITICAL = "#d03b3b"
SUCCESS_TEXT = "#006300"     # success text green (light surface)
FONT_STACK = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Status backgrounds (tinted so the cell text stays primary ink - the label,
# not the color, carries the meaning).
BG_GOOD = "background-color: rgba(12,163,12,0.16)"
BG_NEUTRAL = ""
BG_WARNING = "background-color: rgba(250,178,25,0.28)"
BG_SERIOUS = "background-color: rgba(236,131,90,0.28)"
BG_CRITICAL = "background-color: rgba(208,59,59,0.22)"

RATING_STYLE = {
    "strong_buy": "color: #006300; font-weight: 600",
    "buy": "color: #006300",
    "hold": f"color: {INK_SECONDARY}",
    "reduce": "color: #b34d24",
    "avoid": "color: #d03b3b; font-weight: 600",
}
INTERNAL_STATE_BG = {
    "green": BG_GOOD,
    "stable": BG_NEUTRAL,
    "watch": BG_WARNING,
    "deteriorating": BG_SERIOUS,
}
ACTION_STATE_BG = {
    "hold": BG_NEUTRAL,
    "suspend_adds": BG_WARNING,
    "deteriorating": BG_SERIOUS,
}

NUMERIC_COLS = [
    "weight", "IB_Holding", "IB_quantity", "final_score", "score_confidence", "current_price",
    "rel_ret_5d", "rel_ret_20d", "ma50", "ma200",
    "starter_band_low", "starter_band_high", "add_band_low", "add_band_high",
    "trim_band_low", "trim_band_high",
]
# Text columns the page renders; older run schemas predate several of them.
TEXT_COLS = [
    "sector", "rating", "internal_state", "action_state", "benchmark_ticker",
    "price_band_status", "price_band_basis",
]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def list_run_dates() -> list[str]:
    if not RUNS_ROOT.is_dir():
        return []
    dates = []
    for d in RUNS_ROOT.iterdir():
        if (d / "final" / "final_target_book.csv").is_file():
            dates.append(d.name)
    return sorted(dates, reverse=True)


def security_mapping_signature() -> tuple:
    """Cache key covering the local ticker-to-company mapping files."""
    parts = []
    for path in SECURITY_MAPPING_PATHS:
        try:
            stat = path.stat()
            parts.append((str(path), stat.st_mtime, stat.st_size))
        except OSError:
            parts.append((str(path), 0.0, 0))
    return tuple(parts)


@st.cache_data(show_spinner=False)
def load_security_metadata(signature: tuple) -> pd.DataFrame:
    """Load company names and fallback industries from local ticker maps."""
    _ = signature  # cache key only: reload when a mapping file changes
    company_by_ticker: dict[str, str] = {}
    industry_by_ticker: dict[str, str] = {}
    ticker_columns = ("ticker", "Ticker", "symbol", "Symbol")
    company_columns = ("company_name", "CompanyName", "company", "Company", "name")
    industry_columns = ("industry", "Industry", "subsector", "Subsector")

    for path in SECURITY_MAPPING_PATHS:
        if not path.is_file():
            continue
        try:
            raw = pd.read_csv(path, dtype=str, keep_default_na=False)
        except (OSError, pd.errors.ParserError):
            continue
        ticker_col = next((col for col in ticker_columns if col in raw.columns), None)
        if ticker_col is None:
            continue
        company_col = next((col for col in company_columns if col in raw.columns), None)
        industry_col = next((col for col in industry_columns if col in raw.columns), None)
        for _, row in raw.iterrows():
            ticker = str(row.get(ticker_col, "")).strip().upper()
            if not ticker:
                continue
            if company_col:
                company = str(row.get(company_col, "")).strip()
                if company and ticker not in company_by_ticker:
                    company_by_ticker[ticker] = company
            if industry_col:
                industry = str(row.get(industry_col, "")).strip()
                if industry and ticker not in industry_by_ticker:
                    industry_by_ticker[ticker] = industry

    tickers = sorted(set(company_by_ticker) | set(industry_by_ticker))
    return pd.DataFrame({
        "ticker": tickers,
        "company_name": [company_by_ticker.get(ticker, "") for ticker in tickers],
        "industry": [industry_by_ticker.get(ticker, "") for ticker in tickers],
    })


@st.cache_data(show_spinner=False)
def load_score_metadata(run_date: str, mtime: float) -> pd.DataFrame:
    """Load per-run industries from the sealed canonical score artifact."""
    _ = mtime  # cache key only: reload when the score artifact changes
    path = RUNS_ROOT / run_date / "stocks_scores.csv"
    if not path.is_file():
        return pd.DataFrame(columns=pd.Index(["ticker", "industry"]))
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in ("ticker", "industry"):
        if col not in df.columns:
            df[col] = ""
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["industry"] = df["industry"].astype(str).str.strip()
    return df.loc[:, ["ticker", "industry"]].drop_duplicates()


@st.cache_data(show_spinner=False)
def load_book(run_date: str, mtime: float) -> tuple[dict, pd.DataFrame, list[str]]:
    """Parse the preamble key/value block and the book table.

    Preamble rows carry one or more ``key,value`` PAIRS per line (the IB P&L rows
    pack a headline figure plus its components), so parse pairwise rather than
    splitting once. Returns (preamble, book, missing_columns) - older run schemas
    predate several columns and must degrade to blanks instead of raising.
    """
    _ = mtime  # cache key only: reload when the sealed file changes
    text = (RUNS_ROOT / run_date / "final" / "final_target_book.csv").read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    header_idx = next((i for i, ln in enumerate(lines) if ln.startswith("ticker,")), None)
    if header_idx is None:
        raise ValueError(f"{run_date}: final_target_book.csv has no 'ticker,' header row")

    preamble: dict[str, str] = {}
    for row in csv.reader(lines[:header_idx]):
        cells = [c.strip() for c in row]
        for i in range(0, len(cells) - 1, 2):
            key, value = cells[i], cells[i + 1]
            if key:
                preamble[key] = value

    df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
    missing = [c for c in (*NUMERIC_COLS, *TEXT_COLS, "next_earnings_date") if c not in df.columns]
    for col in NUMERIC_COLS:
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in TEXT_COLS:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("")
    if "next_earnings_date" not in df.columns:
        df["next_earnings_date"] = pd.NaT
    df["next_earnings_date"] = pd.to_datetime(df["next_earnings_date"], format="%m/%d/%Y", errors="coerce")
    return preamble, df, missing


def preamble_float(preamble: dict, key: str) -> float | None:
    """Parse a preamble numeric value; None when absent or unparseable."""
    raw = preamble.get(key)
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:
        return None


def db_signature(path: Path) -> tuple:
    """Cache key covering the DB and its WAL sidecar.

    SQLite in WAL mode commits into ``<db>-wal`` without necessarily touching the
    main file's mtime, so keying a cache on the main file alone serves stale rows.
    """
    parts = []
    for candidate in (path, path.with_name(path.name + "-wal")):
        try:
            stat = candidate.stat()
            parts.append((stat.st_mtime, stat.st_size))
        except OSError:
            parts.append((0.0, 0))
    return tuple(parts)


@st.cache_data(show_spinner=False)
def file_sha256(path_str: str, mtime: float) -> str:
    _ = mtime  # cache key only: re-hash when the file changes
    digest = hashlib.sha256()
    with open(path_str, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@st.cache_data(show_spinner=False)
def load_manifest(run_date: str, mtime: float) -> dict:
    _ = mtime
    path = RUNS_ROOT / run_date / "final" / "final_manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def find_h1_decision(run_date: str) -> Path | None:
    """Latest H1 candidate decision file with as-of date <= the run date (PIT)."""
    if not H1_ROOT.is_dir():
        return None
    dated = sorted(
        (d.name for d in H1_ROOT.iterdir()
         if d.is_dir() and len(d.name) == 10 and d.name[:4].isdigit() and d.name <= run_date),
        reverse=True,
    )
    for name in dated:
        path = H1_ROOT / name / "macro_regime_v2_decision_latest.csv"
        if path.is_file():
            return path
    return None


@st.cache_data(show_spinner=False)
def load_h1_decision(path_str: str, mtime: float) -> dict:
    _ = mtime
    df = pd.read_csv(path_str)
    return df.iloc[0].to_dict()


@st.cache_data(show_spinner=False)
def load_v1_decision(run_date: str, signature: tuple) -> dict:
    """Latest V1 decision row (as-of <= run date) from the macro serving DB."""
    _ = signature
    con = sqlite3.connect(f"file:{SERVING_DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            "SELECT as_of_date, active_current_regime, active_next_regime, "
            "current_top_probability, next_top_probability, "
            "current_confidence, next_confidence "
            "FROM macro_regime_decision_daily WHERE as_of_date <= ? "
            "ORDER BY as_of_date DESC LIMIT 1",
            con, params=[run_date],
        )
    finally:
        con.close()
    return df.iloc[0].to_dict() if len(df) else {}


@st.cache_data(show_spinner=False)
def load_holdings(run_date: str, mtime: float) -> pd.DataFrame:
    """IB positions (cost basis vs market value) from the run ledger."""
    _ = mtime
    df = pd.read_csv(RUNS_ROOT / run_date / "ledger" / "holding_state.csv")
    for col in ["quantity", "cost_price", "cost_basis", "market_value", "unrealized_pl"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
        df[col] = df[col].fillna(0.0)
    return df


@st.cache_data(show_spinner=False)
def load_ending_cash(run_date: str, mtime: float) -> float:
    """Ending Cash total from the run's IB cash report."""
    _ = mtime
    df = pd.read_csv(RUNS_ROOT / run_date / "ledger" / "broker_cash_report.csv")
    rows = df.loc[df["line_item"].eq("Ending Cash")]
    # A base-currency summary already totals every currency; summing it alongside
    # per-currency rows would double count, so prefer it when present.
    base = rows.loc[rows["currency"].astype(str).str.contains("Base Currency", case=False, na=False)]
    chosen = base if len(base) else rows
    return float(sum(float(v) for v in chosen["total"] if pd.notna(v)))


@st.cache_data(show_spinner=False)
def load_trades(signature: tuple) -> pd.DataFrame:
    """Load cumulative normalized IB trades from the portfolio-layer SQLite.

    The per-run ledger/broker_trades.csv holds only that statement's trades;
    the DB table is the deduplicated (by trade_key) full history.
    """
    _ = signature
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            "SELECT trade_date, symbol, account, asset_category, realized_pl FROM broker_trades", con
        )
    finally:
        con.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["realized_pl"] = pd.to_numeric(df["realized_pl"], errors="coerce")
    df["realized_pl"] = df["realized_pl"].fillna(0.0)
    return df



def display_date(value: object) -> str:
    """Render an ISO/date-like value as M/D/YYYY without leading zeroes."""
    if value is None:
        return ""
    try:
        timestamp = pd.Timestamp(str(value))
    except (TypeError, ValueError):
        return ""
    if str(timestamp) == "NaT":
        return ""
    return f"{timestamp.month}/{timestamp.day}/{timestamp.year}"


def parse_run_date(value: str) -> pd.Timestamp:
    """Parse an ISO run-folder date with a concrete timestamp type."""
    return cast(pd.Timestamp, pd.Timestamp(datetime.strptime(value, "%Y-%m-%d")))


def cumulative_returns(values: pd.Series) -> pd.Series:
    """Compound daily returns without relying on ambiguous pandas overloads."""
    levels: list[float] = []
    level = 1.0
    for value in values:
        level *= 1.0 + float(value)
        levels.append(level - 1.0)
    return pd.Series(levels, index=values.index)



def performance_signature(run_date: str) -> tuple:
    """Cache key for the selected run's IB reports and benchmark history."""
    parts: list[tuple[str, float, int]] = []
    if not RUNS_ROOT.is_dir():
        return tuple(parts)
    for run_dir in RUNS_ROOT.iterdir():
        if not run_dir.is_dir() or run_dir.name > run_date:
            continue
        candidates = (
            run_dir / "ledger" / "broker_statement_sources.csv",
            run_dir / "risk" / "prices_adjclose.csv",
        )
        for path in candidates:
            try:
                stat = path.stat()
                parts.append((str(path), stat.st_mtime, stat.st_size))
            except OSError:
                parts.append((str(path), 0.0, 0))
    if IB_REPORTS_ROOT.is_dir():
        for path in IB_REPORTS_ROOT.glob("*.csv"):
            try:
                stat = path.stat()
                parts.append((str(path), stat.st_mtime, stat.st_size))
            except OSError:
                continue
    return tuple(parts)


def _read_ib_daily_twr(path: Path) -> tuple[float | None, float | None]:
    """Read IB's daily TWR and ending NAV from an Activity Statement CSV."""
    twr: float | None = None
    ending_value: float | None = None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                if len(row) >= 3 and row[0] == "Net Asset Value" and row[1] == "Data":
                    raw = row[2].strip()
                    if raw.endswith("%"):
                        try:
                            twr = float(raw[:-1].replace(",", "")) / 100.0
                        except ValueError:
                            pass
                if (
                    len(row) >= 4
                    and row[0] == "Change in NAV"
                    and row[1] == "Data"
                    and row[2] == "Ending Value"
                ):
                    try:
                        ending_value = float(row[3].replace(",", ""))
                    except ValueError:
                        pass
    except (OSError, csv.Error):
        return None, None
    return twr, ending_value


def _read_ib_period_twr(path: Path) -> tuple[pd.Timestamp | None, pd.Timestamp | None, float | None]:
    """Read the declared statement period and its authoritative IB TWR."""
    period = ""
    twr: float | None = None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                if len(row) >= 4 and row[:3] == ["Statement", "Data", "Period"]:
                    period = row[3].strip()
                if len(row) >= 3 and row[0] == "Net Asset Value" and row[1] == "Data":
                    raw = row[2].strip()
                    if raw.endswith("%"):
                        try:
                            twr = float(raw[:-1].replace(",", "")) / 100.0
                        except ValueError:
                            pass
    except (OSError, csv.Error):
        return None, None, None
    if not period:
        return None, None, twr
    parts = period.split(" - ", 1)
    start = pd.to_datetime(parts[0], errors="coerce")
    end = pd.to_datetime(parts[-1], errors="coerce")
    if pd.isna(start) or pd.isna(end):
        return None, None, twr
    return cast(pd.Timestamp, start), cast(pd.Timestamp, end), twr


@st.cache_data(show_spinner=False)
def load_ib_period_returns(run_date: str, signature: tuple) -> dict[str, dict[str, object]]:
    """Return exact IB MTD/YTD TWR statements when those periods exist locally.

    Daily statements are intentionally not promoted to period returns here. The
    dashboard may compound a complete daily chain, but an incomplete chain must
    never be labelled MTD or YTD.
    """
    _ = signature
    selected_end = pd.Timestamp(run_date).normalize()
    targets = {
        "MTD": selected_end.replace(day=1),
        "YTD": selected_end.replace(month=1, day=1),
    }
    result: dict[str, dict[str, object]] = {}
    if not IB_REPORTS_ROOT.is_dir():
        return result
    for path in IB_REPORTS_ROOT.glob("*.csv"):
        start, end, twr = _read_ib_period_twr(path)
        if start is None or end is None or twr is None or end.normalize() != selected_end:
            continue
        for label, expected_start in targets.items():
            if start.normalize() == expected_start:
                result[label] = {
                    "return": twr,
                    "period_start": start,
                    "period_end": end,
                    "source_file": str(path),
                }
    return result


@st.cache_data(show_spinner=False)
def load_performance_history(run_date: str, signature: tuple) -> pd.DataFrame:
    """Load every eligible IB daily TWR and attach benchmark returns when available."""
    _ = signature
    rows: list[dict[str, object]] = []
    if not RUNS_ROOT.is_dir():
        return pd.DataFrame()
    try:
        run_dirs = sorted(
            (d for d in RUNS_ROOT.iterdir() if d.is_dir() and d.name <= run_date),
            key=lambda d: d.name,
        )
    except OSError:
        return pd.DataFrame()

    for run_dir in run_dirs:
        source_path = run_dir / "ledger" / "broker_statement_sources.csv"
        if not source_path.is_file():
            continue
        try:
            source = pd.read_csv(source_path, dtype=str, keep_default_na=False)
        except (OSError, pd.errors.ParserError):
            continue
        if source.empty:
            continue
        source_row = source.iloc[0]
        period_start = str(source_row.get("period_start", "")).strip()
        period_end = str(source_row.get("period_end", "")).strip()
        if not period_end or period_start != period_end:
            # The first available report may be a long lookback statement, not a
            # daily observation; do not treat its cumulative TWR as one day.
            continue
        raw_source = str(source_row.get("source_file", "")).strip()
        report_path = Path(raw_source)
        if not report_path.is_file():
            report_path = IB_REPORTS_ROOT / Path(raw_source).name
        daily_twr, ending_value = _read_ib_daily_twr(report_path)
        if daily_twr is None:
            continue
        rows.append({
            "date": pd.Timestamp(period_end),
            "portfolio_twr_daily": daily_twr,
            "account_value": ending_value,
            "account_id": str(source_row.get("account_id", "")).strip(),
        })

    # User-supplied one-day Activity Statements can predate the orchestrated
    # archive. Their declared statement period, rather than the filename, is the
    # eligibility control. Wider/cumulative statements are intentionally excluded
    # from the daily chain and remain available to load_ib_period_returns.
    selected_end = pd.Timestamp(run_date).normalize()
    if IB_REPORTS_ROOT.is_dir():
        for report_path in IB_REPORTS_ROOT.glob("*.csv"):
            period_start, period_end, _ = _read_ib_period_twr(report_path)
            if (
                period_start is None
                or period_end is None
                or period_start.normalize() != period_end.normalize()
                or period_end.normalize() > selected_end
            ):
                continue
            daily_twr, ending_value = _read_ib_daily_twr(report_path)
            if daily_twr is None:
                continue
            rows.append({
                "date": period_end.normalize(),
                "portfolio_twr_daily": daily_twr,
                "account_value": ending_value,
                "account_id": report_path.stem.split("_", 1)[0],
            })

    if not rows:
        return pd.DataFrame()
    twr = pd.DataFrame(rows).drop_duplicates("date", keep="last").sort_values("date")

    risk_paths = sorted(
        (
            d / "risk" / "prices_adjclose.csv"
            for d in run_dirs
            if (d / "risk" / "prices_adjclose.csv").is_file()
        ),
        reverse=True,
    )
    if not risk_paths:
        return pd.DataFrame()
    try:
        prices = pd.read_csv(risk_paths[0], index_col=0)
    except (OSError, pd.errors.ParserError):
        return pd.DataFrame()
    required = {"SPY", "QQQ"}
    if not required.issubset(prices.columns):
        return pd.DataFrame()
    prices.index = pd.to_datetime(prices.index, errors="coerce")
    prices = prices.loc[prices.index.notna(), ["SPY", "QQQ"]].apply(
        pd.to_numeric, errors="coerce"
    )
    benchmark_returns = prices.pct_change(fill_method=None).rename(
        columns={"SPY": "sp500_daily", "QQQ": "nasdaq100_daily"}
    )
    benchmark_returns.index.name = "date"
    benchmark_returns = benchmark_returns.reset_index()
    # IB may publish an account observation on a date when the benchmark exchange
    # is closed (for example, a shortened/holiday reporting date).  The account
    # return is still authoritative and must remain in its independent TWR chain.
    result = twr.merge(benchmark_returns, on="date", how="left")
    return result.dropna(subset=["portfolio_twr_daily"])


@st.cache_data(show_spinner=False)
def load_portfolio_beta(
    run_date: str,
    holdings_mtime: float,
    prices_path_str: str,
    prices_mtime: float,
) -> tuple[float | None, int, int, float]:
    """Calculate stock beta versus SPY using market-value weights, excluding cash."""
    _ = holdings_mtime, prices_mtime
    holdings_path = RUNS_ROOT / run_date / "ledger" / "holding_state.csv"
    prices_path = Path(prices_path_str)
    if not holdings_path.is_file() or not prices_path.is_file():
        return None, 0, 0, 0.0

    try:
        holdings = pd.read_csv(holdings_path)
        prices = pd.read_csv(prices_path, index_col=0)
    except (OSError, pd.errors.ParserError):
        return None, 0, 0, 0.0

    required_columns = {"asset_category", "symbol", "market_value"}
    if not required_columns.issubset(holdings.columns) or "SPY" not in prices.columns:
        return None, 0, 0, 0.0

    positions = holdings.loc[
        holdings["asset_category"].astype(str).str.casefold().eq("stocks")
        & ~holdings["symbol"].astype(str).str.strip().str.upper().eq("CASH")
    ].copy()
    positions["symbol"] = positions["symbol"].astype(str).str.strip().str.upper()
    positions["market_value"] = pd.to_numeric(positions["market_value"], errors="coerce")
    positions = positions.loc[positions["market_value"].notna()]
    grouped = (
        positions.groupby("symbol", as_index=False)["market_value"]
        .sum()
        .loc[lambda frame: frame["market_value"].ne(0.0)]
    )
    total_names = int(len(grouped))
    invested_value = float(grouped["market_value"].sum()) if total_names else 0.0
    if total_names == 0 or invested_value == 0.0:
        return None, 0, total_names, invested_value

    prices.index = pd.to_datetime(prices.index, errors="coerce")
    prices = prices.loc[prices.index.notna()].apply(pd.to_numeric, errors="coerce")
    returns = prices.pct_change(fill_method=None)
    benchmark = returns["SPY"]
    benchmark_variance = benchmark.var()
    if bool(pd.isna(benchmark_variance)) or float(benchmark_variance) <= 0.0:
        return None, 0, total_names, invested_value

    weighted_beta = 0.0
    covered_names = 0
    for row in grouped.itertuples(index=False):
        ticker = str(row.symbol)
        if ticker not in returns.columns:
            continue
        pair = pd.concat(
            [returns[ticker].rename("asset"), benchmark.rename("benchmark")],
            axis=1,
        ).dropna()
        if len(pair) < 60:
            continue
        asset_returns = cast(pd.Series, pair["asset"])
        benchmark_returns = cast(pd.Series, pair["benchmark"])
        asset_beta = asset_returns.cov(benchmark_returns) / benchmark_returns.var()
        if bool(pd.isna(asset_beta)):
            continue
        weighted_beta += (
            float(row.market_value) / invested_value
        ) * float(asset_beta)
        covered_names += 1

    return (
        weighted_beta if covered_names else None,
        covered_names,
        total_names,
        invested_value,
    )


def composition_figure(
    positions: pd.DataFrame, category: str, title: str
) -> go.Figure | None:
    """Build a market-value-weighted donut for one IB stock classification."""
    if positions.empty or category not in positions.columns:
        return None
    grouped = cast(pd.DataFrame, (
        positions.groupby(category, dropna=False, as_index=False)["market_value"]
        .sum()
    ))
    grouped_category = cast(pd.Series, grouped[category])
    grouped[category] = grouped_category.fillna("Unclassified").astype(str).str.strip()
    grouped_category = cast(pd.Series, grouped[category])
    grouped[category] = grouped_category.replace("", "Unclassified")
    grouped = grouped.loc[grouped["market_value"] > 0].sort_values(
        "market_value", ascending=False
    )
    if grouped.empty:
        return None
    if len(grouped) > 10:
        top = grouped.head(9)
        other = float(grouped.iloc[9:]["market_value"].sum())
        grouped = pd.concat(
            [
                top,
                pd.DataFrame([{category: "Other", "market_value": other}]),
            ],
            ignore_index=True,
        )
    fig = go.Figure(go.Pie(
        labels=grouped[category],
        values=grouped["market_value"],
        hole=0.55,
        sort=False,
        textinfo="label+percent",
        textposition="outside",
        hovertemplate="%{label}<br>$%{value:,.0f}<br>%{percent:.1%}<extra></extra>",
    ))
    fig.update_layout(
        height=420,
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT_STACK, color=INK, size=13),
        margin=dict(l=12, r=12, t=46, b=100),
        title=dict(text=title, x=0.02, xanchor="left"),
        showlegend=True,
        legend=dict(orientation="v", x=1.0, xanchor="left", y=0.5, yanchor="middle"),
    )
    return fig


def base_layout(fig: go.Figure, height: int) -> None:
    fig.update_layout(
        height=height,
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT_STACK, color=INK, size=14),
        margin=dict(l=8, r=16, t=36, b=8),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0.0),
    )
    fig.update_xaxes(gridcolor=GRIDLINE, zerolinecolor=BASELINE, linecolor=BASELINE,
                     tickfont=dict(color=INK_MUTED))
    fig.update_yaxes(gridcolor=GRIDLINE, zerolinecolor=BASELINE, linecolor=BASELINE,
                     tickfont=dict(color=INK))


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
from types import SimpleNamespace

from visualitation.dashboard_ui import render_dashboard


render_dashboard(SimpleNamespace(
    RUNS_ROOT=RUNS_ROOT,
    CORRELATION_ROOT=CORRELATION_ROOT,
    list_run_dates=list_run_dates,
    load_book=load_book,
    load_manifest=load_manifest,
    load_security_metadata=load_security_metadata,
    security_mapping_signature=security_mapping_signature,
    load_score_metadata=load_score_metadata,
    load_holdings=load_holdings,
    load_ending_cash=load_ending_cash,
    load_performance_history=load_performance_history,
    performance_signature=performance_signature,
    load_ib_period_returns=load_ib_period_returns,
    find_h1_decision=find_h1_decision,
    load_h1_decision=load_h1_decision,
    load_portfolio_beta=load_portfolio_beta,
    preamble_float=preamble_float,
    file_sha256=file_sha256,
    display_date=display_date,
))
