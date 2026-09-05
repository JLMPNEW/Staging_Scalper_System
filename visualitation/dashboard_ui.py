"""Native Streamlit interface for the portfolio command center.

Rendering lives here while data access remains in ``Position_Monitor.py``.
The separation lets the legacy single-scroll page be replaced atomically while
retaining its proven, point-in-time parsers and cache keys.
"""

from __future__ import annotations

import html as html_lib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

from visualitation.dashboard_metrics import calculate_index_risk, latest_correlation_matrix
from index_correlations.dashboard_data import (
    DashboardArtifactError,
    load_verified_manifest,
    load_verified_rolling,
    pair_column,
    publication_signature,
)
from index_correlations.pipeline import ETF_LABELS, ETF_TICKERS


SECTOR_ETFS = ("XBI", "IHI", "SOXX", "IGV", "XLK", "XAR", "XLI", "IYT", "XLP")
CORRELATION_COVERAGE_GATE = 0.80
PROTOTYPE_BLUE = "#1f67a6"
PROTOTYPE_GREEN = "#147a4e"
PROTOTYPE_AMBER = "#946200"
PROTOTYPE_RED = "#b33a3a"


def _inject_css() -> None:
    st.markdown(
        """
        <style>
        :root {
          --cockpit-ink: #111827;
          --cockpit-muted: #4b5b6b;
          --cockpit-border: #d8e0e8;
          --cockpit-blue: #1f67a6;
          --cockpit-navy: #1f3b59;
          --cockpit-green: #147a4e;
          --cockpit-amber: #946200;
          --cockpit-red: #b33a3a;
        }
        .stApp { background: #f5f7fa; color: var(--cockpit-ink); }
        [data-testid="stHeader"] { background: rgba(245,247,250,.92); }
        [data-testid="stSidebar"] { background: #eef2f7; border-right: 1px solid var(--cockpit-border); }
        .block-container { max-width: 1680px; padding-top: 1.25rem; padding-bottom: 3rem; }
        h1, h2, h3 { letter-spacing: -.025em; color: var(--cockpit-navy); }
        h1 { font-size: clamp(1.55rem, 2.4vw, 2rem) !important; }
        h2 { font-size: 1.12rem !important; font-weight: 600 !important; }
        h3 { font-size: .98rem !important; font-weight: 600 !important; }
        h2 { margin-top: .2rem !important; }
        [data-testid="stMetric"] {
          background: #ffffff;
          border: 1px solid var(--cockpit-border);
          border-radius: 9px;
          padding: .64rem .76rem;
          min-height: 88px;
          box-shadow: none;
        }
        [data-testid="stMetricLabel"] { color: var(--cockpit-muted); font-weight: 650; }
        [data-testid="stMetricValue"] { color: var(--cockpit-navy); letter-spacing: -.035em; }
        [data-baseweb="tab-list"] {
          gap: .25rem;
          border-bottom: 1px solid var(--cockpit-border);
          overflow-x: auto;
        }
        [data-baseweb="tab"] { padding-left: .9rem; padding-right: .9rem; white-space: nowrap; }
        [data-baseweb="tab"][aria-selected="true"] { color: var(--cockpit-blue); font-weight: 700; }
        div[data-testid="stDataFrame"] { border: 1px solid var(--cockpit-border); border-radius: 8px; overflow: hidden; }
        .cockpit-kicker { color: var(--cockpit-blue); font-size: .76rem; font-weight: 800; letter-spacing: .13em; }
        .cockpit-subtitle { color: var(--cockpit-muted); margin-top: -.55rem; margin-bottom: .75rem; }
        .cockpit-badge {
          display: inline-block; border-radius: 999px; padding: .25rem .58rem;
          font-size: .72rem; font-weight: 750; margin-right: .35rem;
          border: 1px solid var(--cockpit-border); background: #fff;
        }
        .badge-pass { color: #0e6f4d; background: #eaf7f1; border-color: #b9dfd0; }
        .badge-warn { color: #8b5700; background: #fff5dd; border-color: #ecd39b; }
        .badge-fail { color: #9d2f36; background: #fdecee; border-color: #efbdc1; }
        .section-note { color: var(--cockpit-muted); font-size: .88rem; }
        .pc-table-wrap {
          width: 100%; overflow-x: auto; border-top: 1px solid var(--cockpit-border);
          border-bottom: 1px solid var(--cockpit-border); background: #fff;
        }
        table.pc-table { width: 100%; border-collapse: collapse; font-size: .75rem; color: var(--cockpit-ink); }
        .pc-table th {
          padding: .48rem .45rem; text-align: left; white-space: nowrap; color: #1f4f73;
          font-size: .69rem; font-weight: 500; border-bottom: 1px solid var(--cockpit-border);
          background: #fff;
        }
        .pc-table td {
          padding: .48rem .45rem; border-bottom: 1px solid #e3e8ee; white-space: nowrap;
          vertical-align: middle;
        }
        .pc-table tbody tr:last-child td { border-bottom: 0; }
        .pc-table tbody tr:hover td { background: #f7f9fb; }
        .pc-table .pc-ticker { font-weight: 750; color: #0f1f33; }
        .pc-table .pc-num { text-align: right; font-variant-numeric: tabular-nums; }
        .pc-positive { color: var(--cockpit-green); }
        .pc-negative { color: var(--cockpit-red); }
        .pc-state {
          display: inline-block; border-radius: 999px; padding: .18rem .46rem;
          background: #eef3f7; color: #33485d;
        }
        .pc-state-warn { color: var(--cockpit-amber); background: #fff3d6; }
        .pc-state-fail { color: var(--cockpit-red); background: #fde7e8; }
        .pc-ladder { background: #fff; border: 1px solid var(--cockpit-border); border-radius: 9px; padding: .7rem .8rem; }
        .pc-ladder-legend { font-size: .78rem; color: var(--cockpit-muted); margin: 0 0 .8rem 4.7rem; }
        .pc-ladder-row { display: grid; grid-template-columns: 4rem 1fr 9.5rem; gap: .65rem; align-items: center; margin: .56rem 0; }
        .pc-ladder-name { font-size: .84rem; font-weight: 750; }
        .pc-ladder-track { position: relative; height: 11px; border-radius: 7px; background: #e8edf2; }
        .pc-ladder-bar { position: absolute; left: 0; top: 0; height: 11px; border-radius: 7px; background: var(--cockpit-blue); }
        .pc-ladder-marker { position: absolute; top: -4px; width: 2px; height: 19px; background: #6957a8; }
        .pc-ladder-values { font-size: .86rem; font-weight: 650; color: #33485d; font-variant-numeric: tabular-nums; }
        .pc-stat-grid { display:grid; grid-template-columns: repeat(2,minmax(0,1fr)); border:1px solid var(--cockpit-border); border-radius:9px; overflow:hidden; background:#fff; }
        .pc-stat { padding:.82rem; border-right:1px solid var(--cockpit-border); border-bottom:1px solid var(--cockpit-border); }
        .pc-stat:nth-child(even) { border-right:0; } .pc-stat:nth-last-child(-n+2) { border-bottom:0; }
        .pc-stat-label { font-size:.78rem; color:var(--cockpit-muted); margin-bottom:.24rem; }
        .pc-stat-value { font-size:1.46rem; font-weight:600; color:var(--cockpit-navy); font-variant-numeric:tabular-nums; }
        .pc-readthrough { margin-top:.65rem; padding:.72rem .8rem; border-left:3px solid var(--cockpit-blue); background:#f3f7fb; font-size:.84rem; }
        .pc-example-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.7rem; margin:.4rem 0 .9rem; }
        .pc-example-card { background:#fff; border:1px solid var(--cockpit-border); border-radius:9px; padding:.78rem .85rem; font-size:.82rem; line-height:1.45; }
        .pc-example-title { color:#1f4f73; font-size:.75rem; font-weight:750; text-transform:uppercase; letter-spacing:.04em; margin-bottom:.35rem; }
        [data-testid="stDownloadButton"] button { border-color: var(--cockpit-border); color: #1f4f73; min-height: 2rem; padding: .25rem .62rem; }
        @media (max-width: 760px) {
          .block-container { padding-left: .7rem; padding-right: .7rem; }
          [data-testid="stMetric"] { min-height: 96px; padding: .6rem .7rem; }
          [data-testid="stMetricValue"] { font-size: 1.4rem; }
          .pc-ladder-row { grid-template-columns: 2.75rem minmax(0,1fr) 7.5rem; gap:.4rem; }
          .pc-example-grid { grid-template-columns:1fr; }
          .pc-table-wrap { border:0; background:transparent; overflow:visible; }
          table.pc-table, .pc-table tbody { display:block; width:100%; }
          .pc-table thead { display:none; }
          .pc-table tr { display:block; margin:0 0 .65rem; border:1px solid var(--cockpit-border); border-radius:8px; background:#fff; overflow:hidden; }
          .pc-table td { display:grid; grid-template-columns:minmax(7.8rem,42%) 1fr; gap:.6rem; white-space:normal; text-align:right !important; padding:.44rem .55rem; }
          .pc-table td::before { content:attr(data-label); text-align:left; color:#1f4f73; font-size:.69rem; font-weight:650; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _money(value: object, decimals: int = 0) -> str:
    if value is None or bool(pd.isna(value)):
        return "n/a"
    return f"${float(value):,.{decimals}f}"


def _percent(value: object, decimals: int = 1) -> str:
    if value is None or bool(pd.isna(value)):
        return "n/a"
    return f"{float(value):.{decimals}%}"


def _number(value: object, decimals: int = 2) -> str:
    if value is None or bool(pd.isna(value)):
        return "n/a"
    return f"{float(value):,.{decimals}f}"


def _long_date(value: object) -> str:
    if value is None or bool(pd.isna(value)):
        return ""
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return ""
    if pd.isna(timestamp):
        return ""
    return f"{timestamp.strftime('%b')} {timestamp.day}, {timestamp.year}"


def _format_table_value(value: object, kind: str) -> tuple[str, str]:
    if value is None or bool(pd.isna(value)):
        return "", ""
    css = ""
    if kind == "money":
        text = _money(value, 0)
        css = "pc-num"
    elif kind == "money2":
        text = _money(value, 2)
        css = "pc-num"
    elif kind == "signed_money":
        number = float(value)
        text = f"{ '+' if number > 0 else ''}${number:,.0f}" if number >= 0 else f"−${abs(number):,.0f}"
        css = "pc-num pc-positive" if number > 0 else "pc-num pc-negative" if number < 0 else "pc-num"
    elif kind == "percent":
        text = _percent(value, 1)
        css = "pc-num"
    elif kind == "percent2":
        text = _percent(value, 2)
        css = "pc-num"
    elif kind == "signed_percent":
        number = float(value)
        text = f"{number:+.2%}"
        css = "pc-num pc-positive" if number > 0 else "pc-num pc-negative" if number < 0 else "pc-num"
    elif kind == "signed_percent1":
        number = float(value)
        text = f"{number:+.1%}"
        css = "pc-num pc-positive" if number > 0 else "pc-num pc-negative" if number < 0 else "pc-num"
    elif kind == "signed_pp":
        number = float(value)
        text = f"{number * 100:+.1f} pp"
        css = "pc-num pc-positive" if number > 0 else "pc-num pc-negative" if number < 0 else "pc-num"
    elif kind == "decimal3":
        text = _number(value, 3)
        css = "pc-num"
    elif kind == "signed3":
        number = float(value)
        text = f"{number:+.3f}"
        css = "pc-num pc-positive" if number > 0 else "pc-num pc-negative" if number < 0 else "pc-num"
    elif kind == "integer":
        text = f"{float(value):,.0f}"
        css = "pc-num"
    elif kind == "date":
        text = _long_date(value)
    elif kind == "ticker":
        text = str(value)
        css = "pc-ticker"
    elif kind == "state":
        text = str(value)
        lowered = text.casefold()
        if lowered in {"deteriorating", "fail", "missing/fail"}:
            css = "pc-state pc-state-fail"
        elif lowered in {"watch", "limited", "missing"}:
            css = "pc-state pc-state-warn"
        else:
            css = "pc-state"
    else:
        text = str(value)
    return html_lib.escape(text), css


def _render_prototype_table(
    frame: pd.DataFrame,
    columns: list[tuple[str, str, str]],
    *,
    empty_message: str = "No rows are available for this view.",
) -> None:
    if frame.empty:
        st.info(empty_message)
        return
    header = "".join(
        f'<th class="{"pc-num" if kind in {"money", "money2", "signed_money", "percent", "percent2", "signed_percent", "signed_percent1", "signed_pp", "decimal3", "signed3", "integer"} else ""}">{html_lib.escape(label)}</th>'
        for _, label, kind in columns
    )
    body_rows: list[str] = []
    for _, row in frame.iterrows():
        cells: list[str] = []
        for key, label, kind in columns:
            text, css = _format_table_value(row.get(key), kind)
            cells.append(
                f'<td class="{css}" data-label="{html_lib.escape(label, quote=True)}">{text}</td>'
            )
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    st.markdown(
        '<div class="pc-table-wrap"><table class="pc-table"><thead><tr>'
        + header
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>",
        unsafe_allow_html=True,
    )


def _download_csv(frame: pd.DataFrame, filename: str, key: str, label: str) -> None:
    st.download_button(
        label,
        data=frame.to_csv(index=False).encode("utf-8"),
        file_name=filename,
        mime="text/csv",
        key=key,
    )


def _fmt_regime(value: object) -> str:
    label = str(value or "").strip()
    return label.replace("_", " ").title() if label else "n/a"


def _regime_detail(probability: object, confidence: object) -> str | None:
    values: list[str] = []
    if probability is not None and bool(pd.notna(probability)):
        values.append(f"top probability {float(probability):.1%}")
    if confidence is not None and bool(pd.notna(confidence)):
        values.append(f"confidence {float(confidence):.1%}")
    return " · ".join(values) or None


def _find_risk_prices(runs_root: Path, run_date: str) -> Path | None:
    selected = runs_root / run_date / "risk" / "prices_adjclose.csv"
    if selected.is_file():
        return selected
    try:
        candidates = sorted(
            (
                directory / "risk" / "prices_adjclose.csv"
                for directory in runs_root.iterdir()
                if directory.is_dir()
                and directory.name <= run_date
                and (directory / "risk" / "prices_adjclose.csv").is_file()
            ),
            reverse=True,
        )
    except OSError:
        return None
    return candidates[0] if candidates else None


@st.cache_data(show_spinner=False)
def _load_prices(path_str: str, mtime: float, size: int) -> pd.DataFrame:
    _ = mtime, size
    frame = pd.read_csv(path_str, index_col=0)
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    return frame.loc[frame.index.notna()].sort_index()


@st.cache_data(show_spinner=False)
def _load_correlation_manifest(
    directory_str: str, signature: tuple[tuple[str, str, int], ...]
) -> dict[str, Any]:
    _ = signature
    return load_verified_manifest(Path(directory_str))


@st.cache_data(show_spinner=False)
def _load_correlation_rolling(
    directory_str: str,
    method: str,
    window: int,
    signature: tuple[tuple[str, str, int], ...],
) -> pd.DataFrame:
    _ = signature
    return load_verified_rolling(Path(directory_str), method, window)


def _prepare_book(api: SimpleNamespace, run_date: str) -> tuple[pd.DataFrame, dict, dict, list[str], str, bool]:
    book_path = api.RUNS_ROOT / run_date / "final" / "final_target_book.csv"
    manifest_path = api.RUNS_ROOT / run_date / "final" / "final_manifest.json"
    preamble, book, missing_columns = api.load_book(run_date, book_path.stat().st_mtime)
    manifest = api.load_manifest(
        run_date, manifest_path.stat().st_mtime if manifest_path.is_file() else 0.0
    )

    book["ticker"] = book["ticker"].fillna("").astype(str).str.strip().str.upper()
    if "ib_symbol" not in book.columns:
        book["ib_symbol"] = book["ticker"]
    book["ib_symbol"] = book["ib_symbol"].fillna("").astype(str).str.strip().str.upper()
    book["ib_symbol"] = book["ib_symbol"].where(book["ib_symbol"].ne(""), book["ticker"])

    security_metadata = api.load_security_metadata(api.security_mapping_signature())
    book = book.merge(security_metadata, on="ticker", how="left", suffixes=("", "_map"))
    for column in ("company_name", "industry"):
        if column not in book.columns:
            book[column] = ""
        book[column] = book[column].fillna("").astype(str).str.strip()
        mapped = f"{column}_map"
        if mapped in book.columns:
            fallback = book[mapped].fillna("").astype(str).str.strip()
            book[column] = book[column].where(book[column].ne(""), fallback)
            book = book.drop(columns=[mapped])

    score_path = api.RUNS_ROOT / run_date / "stocks_scores.csv"
    score_metadata = api.load_score_metadata(
        run_date, score_path.stat().st_mtime if score_path.is_file() else 0.0
    )
    book = book.merge(
        score_metadata.rename(columns={"industry": "score_industry"}),
        on="ticker",
        how="left",
    )
    book["score_industry"] = book["score_industry"].fillna("").astype(str).str.strip()
    book["industry"] = book["score_industry"].where(
        book["score_industry"].ne(""), book["industry"]
    )
    book = book.drop(columns=["score_industry"])
    book["earnings_in_days"] = (
        book["next_earnings_date"] - pd.Timestamp(run_date)
    ).dt.days

    recorded = ""
    files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    if isinstance(files, dict):
        entry = files.get("final_target_book.csv", {})
        if isinstance(entry, dict):
            recorded = str(entry.get("sha256", ""))
    actual = api.file_sha256(str(book_path), book_path.stat().st_mtime)
    verified = bool(recorded) and recorded == actual
    return book, preamble, manifest, missing_columns, actual, verified


def _load_selected_run(api: SimpleNamespace, run_date: str) -> dict[str, Any]:
    book, preamble, manifest, missing_columns, actual_sha, sha_verified = _prepare_book(api, run_date)
    holdings_path = api.RUNS_ROOT / run_date / "ledger" / "holding_state.csv"
    cash_path = api.RUNS_ROOT / run_date / "ledger" / "broker_cash_report.csv"
    holdings = (
        api.load_holdings(run_date, holdings_path.stat().st_mtime)
        if holdings_path.is_file()
        else pd.DataFrame()
    )
    cash = (
        api.load_ending_cash(run_date, cash_path.stat().st_mtime)
        if cash_path.is_file()
        else 0.0
    )
    performance_signature = api.performance_signature(run_date)
    performance = api.load_performance_history(run_date, performance_signature)
    period_returns = (
        api.load_ib_period_returns(run_date, performance_signature)
        if hasattr(api, "load_ib_period_returns")
        else {}
    )
    h1_path = api.find_h1_decision(run_date)
    h1 = api.load_h1_decision(str(h1_path), h1_path.stat().st_mtime) if h1_path else {}
    risk_path = _find_risk_prices(api.RUNS_ROOT, run_date)
    risk_prices = pd.DataFrame()
    if risk_path is not None:
        stat = risk_path.stat()
        risk_prices = _load_prices(str(risk_path), stat.st_mtime, stat.st_size)

    beta = None
    beta_covered = beta_total = 0
    invested_value = 0.0
    if holdings_path.is_file() and risk_path is not None:
        beta, beta_covered, beta_total, invested_value = api.load_portfolio_beta(
            run_date,
            holdings_path.stat().st_mtime,
            str(risk_path),
            risk_path.stat().st_mtime,
        )

    correlation_dir = api.CORRELATION_ROOT / run_date
    correlation_manifest: dict[str, Any] = {}
    correlation_signature: tuple[tuple[str, str, int], ...] = tuple()
    correlation_error = ""
    if correlation_dir.is_dir():
        correlation_signature = publication_signature(correlation_dir)
        try:
            correlation_manifest = _load_correlation_manifest(
                str(correlation_dir), correlation_signature
            )
        except DashboardArtifactError as exc:
            correlation_error = str(exc)
    else:
        correlation_error = f"No exact-date correlation publication for {run_date}."

    benchmark_risk, holding_risk, correlation_coverage = calculate_index_risk(
        holdings,
        risk_prices,
        benchmark_tickers=ETF_TICKERS,
        benchmark_labels=ETF_LABELS,
        sector_tickers=SECTOR_ETFS,
    )
    return {
        "book": book,
        "preamble": preamble,
        "manifest": manifest,
        "missing_columns": missing_columns,
        "actual_sha": actual_sha,
        "sha_verified": sha_verified,
        "holdings": holdings,
        "holdings_path": holdings_path,
        "cash_path": cash_path,
        "cash": cash,
        "performance": performance,
        "period_returns": period_returns,
        "h1": h1,
        "h1_path": h1_path,
        "risk_path": risk_path,
        "risk_prices": risk_prices,
        "beta": beta,
        "beta_covered": beta_covered,
        "beta_total": beta_total,
        "invested_value": invested_value,
        "correlation_dir": correlation_dir,
        "correlation_manifest": correlation_manifest,
        "correlation_signature": correlation_signature,
        "correlation_error": correlation_error,
        "benchmark_risk": benchmark_risk,
        "holding_risk": holding_risk,
        "correlation_coverage": correlation_coverage,
    }


def _performance_window(performance: pd.DataFrame, run_date: str, period: str) -> pd.DataFrame:
    if performance.empty:
        return performance
    end = pd.Timestamp(run_date)
    start = end.replace(day=1) if period == "MTD" else end.replace(month=1, day=1)
    visible = performance.loc[performance["date"].between(start, end)].copy()
    if visible.empty:
        return visible
    for source, output in (
        ("portfolio_twr_daily", "portfolio_return"),
        ("sp500_daily", "sp500_return"),
        ("nasdaq100_daily", "nasdaq100_return"),
    ):
        values = pd.to_numeric(visible[source], errors="coerce").fillna(0.0)
        visible[output] = (1.0 + values).cumprod() - 1.0
    return visible


def _benchmark_performance_window(prices: pd.DataFrame, run_date: str, period: str) -> pd.DataFrame:
    """Build calendar-period benchmark total returns from the prior trading close."""
    if prices.empty or not {"SPY", "QQQ"}.issubset(prices.columns):
        return pd.DataFrame()
    end = pd.Timestamp(run_date).normalize()
    start = end.replace(day=1) if period == "MTD" else end.replace(month=1, day=1)
    selected = prices.loc[prices.index <= end, ["SPY", "QQQ"]].apply(pd.to_numeric, errors="coerce")
    selected = selected.dropna(how="any")
    prior = selected.loc[selected.index < start].tail(1)
    body = selected.loc[selected.index.to_series().between(start, end)]
    if prior.empty or body.empty:
        return pd.DataFrame()
    window = pd.concat([prior, body])
    base = window.iloc[0]
    result = pd.DataFrame({
        "date": window.index,
        "sp500_return": window["SPY"].div(float(base["SPY"])) - 1.0,
        "nasdaq100_return": window["QQQ"].div(float(base["QQQ"])) - 1.0,
    }).reset_index(drop=True)
    return result


def _portfolio_window_complete(
    portfolio: pd.DataFrame, benchmark: pd.DataFrame, run_date: str, period: str
) -> bool:
    if benchmark.empty or len(benchmark) < 2:
        return False
    observed, expected, _ = _portfolio_window_coverage(portfolio, run_date, period)
    return expected > 0 and observed == expected


def _portfolio_window_coverage(
    portfolio: pd.DataFrame, run_date: str, period: str
) -> tuple[int, int, list[pd.Timestamp]]:
    """Return observed/expected IB weekday coverage and the missing dates."""

    end = pd.Timestamp(run_date).normalize()
    start = end.replace(day=1) if period == "MTD" else end.replace(month=1, day=1)
    expected_dates = set(pd.bdate_range(start, end))
    if portfolio.empty or "date" not in portfolio.columns:
        return 0, len(expected_dates), sorted(expected_dates)
    observed_dates = set(
        pd.to_datetime(
            portfolio.loc[portfolio["date"].between(start, end), "date"],
            errors="coerce",
        ).dropna().dt.normalize()
    )
    covered_dates = expected_dates & observed_dates
    missing_dates = sorted(expected_dates - observed_dates)
    return len(covered_dates), len(expected_dates), missing_dates


def _missing_date_ranges(dates: list[pd.Timestamp]) -> str:
    if not dates:
        return "none"
    ranges: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    start = previous = pd.Timestamp(dates[0]).normalize()
    for value in dates[1:]:
        current = pd.Timestamp(value).normalize()
        if current == previous + pd.offsets.BDay(1):
            previous = current
            continue
        ranges.append((start, previous))
        start = previous = current
    ranges.append((start, previous))
    return "; ".join(
        _long_date(left) if left == right else f"{_long_date(left)} to {_long_date(right)}"
        for left, right in ranges
    )


def _next_business_window(run_date: str, count: int = 7) -> pd.DatetimeIndex:
    business_day = CustomBusinessDay(calendar=USFederalHolidayCalendar())
    first = pd.Timestamp(run_date).normalize() + business_day
    return pd.date_range(first, periods=count, freq=business_day)


def _position_state(value: object) -> str:
    raw = str(value or "").strip().casefold()
    if raw in {"deteriorating", "watch"}:
        return raw.title()
    return "Stable"


def _position_next_action(row: pd.Series) -> str:
    state = str(row.get("internal_state", "")).casefold()
    raw_action = str(row.get("action_state", "")).casefold()
    if float(row.get("target_weight", 0.0) or 0.0) > 0:
        return "Hold"
    if raw_action == "suspend_adds" or state in {"deteriorating", "watch"}:
        return "Suspend adds"
    cost_basis = float(row.get("market_value", 0.0) or 0.0) - float(row.get("unrealized_pl", 0.0) or 0.0)
    return_on_cost = float(row.get("unrealized_pl", 0.0) or 0.0) / cost_basis if cost_basis > 0 else 0.0
    if return_on_cost <= -0.15:
        return "Re-underwrite"
    if return_on_cost <= -0.05:
        return "Watch"
    return "Review fit"


def _research_next_action(row: pd.Series) -> str:
    state = str(row.get("internal_state", "")).casefold()
    raw_action = str(row.get("action_state", "")).casefold()
    if raw_action == "suspend_adds" or state in {"deteriorating", "watch"}:
        return "Suspend adds"
    if float(row.get("weight", 0.0) or 0.0) > 0:
        return "Review entry"
    return "Monitor"


def _risk_read(tactical: object, shift: object) -> str:
    if tactical is None or bool(pd.isna(tactical)):
        return "Insufficient history"
    level = _risk_label(tactical)
    if shift is None or bool(pd.isna(shift)) or abs(float(shift)) < 0.05:
        regime = "stable"
    elif float(shift) > 0:
        regime = "tightening"
    else:
        regime = "decoupling"
    return f"{level} · {regime}"


def _preamble_or_manifest(snapshot: dict[str, Any], api: SimpleNamespace, key: str) -> float | None:
    value = api.preamble_float(snapshot["preamble"], key)
    if value is not None:
        return value
    performance = snapshot["manifest"].get("ib_performance", {})
    if isinstance(performance, dict):
        raw = performance.get(key)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _reconcile_positions(book: pd.DataFrame, holdings: pd.DataFrame, account_value: float) -> pd.DataFrame:
    columns = [
        "ticker", "company_name", "sector", "quantity", "avg_cost", "current_price",
        "market_value", "account_weight", "target_weight", "allocation_gap",
        "unrealized_pl", "next_earnings_date", "earnings_in_days", "relation", "internal_state", "action_state",
        "starter_band_low", "starter_band_high", "add_band_low", "add_band_high",
        "trim_band_low", "trim_band_high",
    ]
    if holdings.empty:
        return pd.DataFrame(columns=columns)
    stocks = holdings.loc[
        holdings["asset_category"].astype(str).str.casefold().eq("stocks")
    ].copy()
    if stocks.empty:
        return pd.DataFrame(columns=columns)
    stocks["symbol"] = stocks["symbol"].astype(str).str.strip().str.upper()
    numeric = ["quantity", "cost_basis", "market_value", "unrealized_pl"]
    for column in numeric:
        stocks[column] = pd.to_numeric(stocks[column], errors="coerce").fillna(0.0)
    grouped = stocks.groupby("symbol", as_index=False)[numeric].sum()
    grouped["avg_cost"] = grouped["cost_basis"].div(grouped["quantity"].replace(0, pd.NA))
    grouped["ledger_price"] = grouped["market_value"].div(grouped["quantity"].replace(0, pd.NA))

    lookup_columns = [
        "ib_symbol", "ticker", "company_name", "sector", "weight", "current_price",
        "next_earnings_date", "earnings_in_days", "internal_state", "action_state",
        "starter_band_low", "starter_band_high", "add_band_low", "add_band_high",
        "trim_band_low", "trim_band_high",
    ]
    lookup = book.loc[:, lookup_columns].copy()
    lookup["join_symbol"] = lookup["ib_symbol"].where(lookup["ib_symbol"].ne(""), lookup["ticker"])
    lookup = (
        lookup.sort_values(["weight", "ticker"], ascending=[False, True])
        .drop_duplicates("join_symbol", keep="first")
    )
    frame = grouped.merge(lookup, left_on="symbol", right_on="join_symbol", how="left")
    # A fallback ticker join handles older books whose ib_symbol field was absent.
    missing = frame["ticker"].isna()
    if bool(missing.any()):
        fallback = book.loc[:, lookup_columns[1:]].drop_duplicates("ticker").set_index("ticker")
        for column in lookup_columns[1:]:
            if column == "ticker":
                continue
            frame.loc[missing, column] = frame.loc[missing, "symbol"].map(fallback[column])
        frame.loc[missing, "ticker"] = frame.loc[missing, "symbol"]

    frame["ticker"] = frame["ticker"].fillna(frame["symbol"])
    frame["company_name"] = frame["company_name"].fillna("")
    frame["sector"] = frame["sector"].fillna("Unclassified").replace("", "Unclassified")
    frame["target_weight"] = pd.to_numeric(frame["weight"], errors="coerce").fillna(0.0)
    frame["account_weight"] = frame["market_value"] / account_value if account_value > 0 else pd.NA
    frame["allocation_gap"] = frame["target_weight"] - frame["account_weight"]
    frame["current_price"] = pd.to_numeric(frame["current_price"], errors="coerce")
    frame["current_price"] = frame["current_price"].fillna(frame["ledger_price"])
    frame["relation"] = frame["target_weight"].gt(0).map({True: "Held + target", False: "Held only"})
    frame["internal_state"] = frame["internal_state"].fillna("")
    frame["action_state"] = frame["action_state"].fillna("").replace("", "n/a")
    return frame.loc[:, columns].sort_values("market_value", ascending=False).reset_index(drop=True)


def _dataframe_height(rows: int, maximum: int = 680) -> int:
    return min(maximum, max(150, 38 + 35 * int(rows)))


def _render_header(api: SimpleNamespace, run_date: str, snapshot: dict[str, Any]) -> None:
    manifest = snapshot["manifest"]
    acceptance = str(manifest.get("acceptance", "UNKNOWN"))
    correlation_ok = bool(snapshot["correlation_manifest"])
    seal_class = "badge-pass" if acceptance == "PASS" and snapshot["sha_verified"] else "badge-fail"
    corr_class = "badge-pass" if correlation_ok else "badge-warn"
    st.markdown('<div class="cockpit-kicker">PORTFOLIO INTELLIGENCE · DECISION SUPPORT</div>', unsafe_allow_html=True)
    st.title("Portfolio command center")
    st.markdown(
        f'<div class="cockpit-subtitle">As of {api.display_date(run_date)} · '
        f'<span class="cockpit-badge {seal_class}">BOOK {acceptance}</span>'
        f'<span class="cockpit-badge {corr_class}">ETF MATRIX {"VERIFIED" if correlation_ok else "LIMITED"}</span>'
        '<span class="cockpit-badge">READ ONLY</span></div>',
        unsafe_allow_html=True,
    )


def _render_integrity_banner(snapshot: dict[str, Any]) -> None:
    manifest = snapshot["manifest"]
    checks = manifest.get("checks", []) if isinstance(manifest, dict) else []
    failed = [item.get("check", "unknown") for item in checks if item.get("status") not in ("PASS", "WARN")]
    warnings = [item.get("check", "unknown") for item in checks if item.get("status") == "WARN"]
    if manifest.get("acceptance") == "PASS" and snapshot["sha_verified"] and not failed:
        st.success(
            f"Decision book is sealed and verified · {len(checks)} controls · "
            f"generated {manifest.get('generated_at', 'n/a')}",
        )
    elif not manifest:
        st.error("Final manifest is missing. Treat this run as unsealed.")
    elif not snapshot["sha_verified"]:
        st.error("Book integrity failure: the rendered CSV does not match its manifest SHA-256.")
    else:
        st.error(f"Final-manifest failure: {', '.join(failed) or manifest.get('acceptance', 'UNKNOWN')}")
    if warnings:
        st.warning(f"Non-gating diagnostics: {', '.join(warnings)}")
    if snapshot["missing_columns"]:
        st.warning(
            "Legacy schema: these fields are unavailable and render blank — "
            + ", ".join(snapshot["missing_columns"])
        )


def _render_overview(api: SimpleNamespace, run_date: str, snapshot: dict[str, Any], positions: pd.DataFrame) -> None:
    book = snapshot["book"]
    holdings = snapshot["holdings"]
    cash = float(snapshot["cash"])
    positions_value = float(holdings["market_value"].sum()) if not holdings.empty else 0.0
    account_value = positions_value + cash
    realized_mtd = _preamble_or_manifest(snapshot, api, "ib_realized_profit_loss_mtd")
    realized_ytd = _preamble_or_manifest(snapshot, api, "ib_realized_profit_loss_ytd")
    dividends_ytd = _preamble_or_manifest(snapshot, api, "ib_dividends_ytd")
    interest_ytd = _preamble_or_manifest(snapshot, api, "ib_net_broker_interest_ytd")

    st.subheader("Executive snapshot")
    first = st.columns(4)
    first[0].metric("Account value", _money(account_value, 2), "positions + ending cash", delta_color="off")
    first[1].metric("Realized P&L · MTD", _money(realized_mtd, 2))
    first[2].metric("Realized P&L · YTD", _money(realized_ytd, 2))
    first[3].metric(
        "Ending cash",
        _money(cash, 2),
        _percent(cash / account_value) + " of account" if account_value > 0 else None,
        delta_color="off",
    )
    second = st.columns(4)
    second[0].metric("Dividends · YTD", _money(dividends_ytd, 2), "included in realized P&L", delta_color="off")
    second[1].metric("Net interest · YTD", _money(interest_ytd, 2), "included in realized P&L", delta_color="off")
    second[2].metric(
        "Portfolio beta · SPY",
        _number(snapshot["beta"], 2),
        f"{snapshot['beta_covered']} / {snapshot['beta_total']} holdings covered",
        delta_color="off",
    )
    is_cash = book["ticker"].eq("CASH")
    is_target = book["weight"].gt(0) & ~is_cash
    second[3].metric(
        "Target deployment",
        _percent(float(book.loc[is_target, "weight"].sum())),
        f"{int(is_target.sum())} target names",
        delta_color="off",
    )

    st.caption(
        "Account and P&L are selected-run facts. Dividends and net interest are disclosed components of realized P&L, not additive returns."
    )

    st.divider()
    st.subheader("H1 macro estimate")
    h1 = snapshot["h1"]
    regime_columns = st.columns([1, 1, 1.15])
    regime_columns[0].metric(
        "H1 estimate · current",
        _fmt_regime(h1.get("active_current_regime")),
        _regime_detail(h1.get("current_top_probability"), h1.get("current_confidence")),
        delta_color="off",
    )
    regime_columns[1].metric(
        "H1 estimate · next",
        _fmt_regime(h1.get("active_next_regime")),
        _regime_detail(h1.get("next_top_probability"), h1.get("next_confidence")),
        delta_color="off",
    )
    regime_columns[2].metric(
        "H1 estimate · as of",
        api.display_date(h1.get("as_of_date")) or "n/a",
        "shadow candidate · not sizing authority",
        delta_color="off",
    )

    st.divider()
    left, right = st.columns([1.55, 1.0])
    with left:
        title, control = st.columns([2.3, 1])
        title.subheader("Performance versus benchmarks")
        with control:
            period = st.radio(
                "Performance period",
                ["MTD", "YTD"],
                horizontal=True,
                label_visibility="collapsed",
                key="overview_performance_period",
            )
        daily_visible = _performance_window(snapshot["performance"], run_date, period)
        benchmark_visible = _benchmark_performance_window(snapshot["risk_prices"], run_date, period)
        daily_complete = _portfolio_window_complete(
            snapshot["performance"], benchmark_visible, run_date, period
        )
        exact_period = snapshot["period_returns"].get(period, {})
        portfolio_return = exact_period.get("return")
        portfolio_basis = "exact IB period TWR"
        if portfolio_return is None and daily_complete and not daily_visible.empty:
            portfolio_return = float(daily_visible.iloc[-1]["portfolio_return"])
            portfolio_basis = "complete chain of IB daily TWR observations"
        benchmark_latest = benchmark_visible.iloc[-1] if not benchmark_visible.empty else None
        spy_return = benchmark_latest["sp500_return"] if benchmark_latest is not None else None
        qqq_return = benchmark_latest["nasdaq100_return"] if benchmark_latest is not None else None
        active_return = (
            float(portfolio_return) - float(spy_return)
            if portfolio_return is not None and spy_return is not None and bool(pd.notna(spy_return))
            else None
        )
        metrics = st.columns(4)
        metrics[0].metric(
            f"Portfolio · {period}",
            _percent(portfolio_return, 2) if portfolio_return is not None else "Unavailable",
            portfolio_basis if portfolio_return is not None else "incomplete local IB period history",
            delta_color="off",
        )
        metrics[1].metric("SPY", _percent(spy_return, 2), "adjusted-close total-return proxy", delta_color="off")
        metrics[2].metric("QQQ", _percent(qqq_return, 2), "adjusted-close total-return proxy", delta_color="off")
        metrics[3].metric(
            "Active vs SPY",
            f"{float(active_return) * 100:+.2f} pp" if active_return is not None else "Unavailable",
            "portfolio TWR minus SPY return - percentage points"
            if active_return is not None
            else "requires comparable portfolio TWR",
            delta_color="off",
        )

        if benchmark_visible.empty:
            st.info(f"No complete SPY / QQQ adjusted-close history is available for {period}.")
        else:
            benchmark_chart = benchmark_visible.copy()
            benchmark_chart['date'] = pd.to_datetime(
                benchmark_chart['date'], errors='coerce'
            ).dt.normalize()
            benchmark_chart = benchmark_chart.dropna(subset=['date'])
            benchmark_visible = benchmark_chart
            figure = go.Figure()
            if daily_complete and not daily_visible.empty:
                anchor = pd.DataFrame({
                    "date": [benchmark_visible.iloc[0]["date"]],
                    "portfolio_return": [0.0],
                })
                portfolio_chart = pd.concat(
                    [anchor, daily_visible[["date", "portfolio_return"]]], ignore_index=True
                ).drop_duplicates("date", keep="last")
                portfolio_chart['date'] = pd.to_datetime(
                    portfolio_chart['date'], errors='coerce'
                ).dt.normalize()
                portfolio_chart = portfolio_chart.dropna(subset=['date'])
                figure.add_trace(go.Scatter(
                    x=portfolio_chart["date"], y=portfolio_chart["portfolio_return"], mode="lines",
                    name="Portfolio TWR", line={"width": 2.2, "color": PROTOTYPE_BLUE},
                    hovertemplate="%{x|%b %d, %Y}<br>Portfolio: %{y:.2%}<extra></extra>",
                ))
            for column, label, color in (
                ("sp500_return", "SPY", PROTOTYPE_GREEN),
                ("nasdaq100_return", "QQQ", PROTOTYPE_AMBER),
            ):
                figure.add_trace(go.Scatter(
                    x=benchmark_visible["date"], y=benchmark_visible[column], mode="lines",
                    name=label, line={"width": 1.8, "color": color},
                    hovertemplate=f"%{{x|%b %d, %Y}}<br>{label}: %{{y:.2%}}<extra></extra>",
                ))
            chart_start = pd.Timestamp(benchmark_chart['date'].min()).normalize()
            chart_end = pd.Timestamp(benchmark_chart['date'].max()).normalize()
            span_days = max((chart_end - chart_start).days, 1)
            if span_days <= 10:
                date_tick = 86_400_000
                date_tick_format = '%b %d'
            elif span_days <= 62:
                date_tick = 7 * 86_400_000
                date_tick_format = '%b %d'
            else:
                date_tick = 'M1'
                date_tick_format = '%b'
            figure.update_xaxes(
                type='date',
                tickformat=date_tick_format,
                hoverformat='%b %d, %Y',
                dtick=date_tick,
                range=[chart_start, chart_end],
            )
            figure.update_layout(
                height=285, margin={"l": 8, "r": 8, "t": 32, "b": 8},
                paper_bgcolor="#ffffff", plot_bgcolor="#ffffff", hovermode="x unified",
                legend={"orientation": "h", "y": 1.10, "x": 0, "font": {"size": 11}},
                font={"family": "system-ui", "color": "#111827", "size": 11},
            )
            figure.update_xaxes(gridcolor="#edf1f4", linecolor="#d8e0e8")
            figure.update_yaxes(tickformat=".1%", gridcolor="#edf1f4", zerolinecolor="#c6d0da")
            st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
            st.caption(
                f"Portfolio: {portfolio_basis if portfolio_return is not None else 'not shown because the IB chain is incomplete'}. "
                "Benchmarks: adjusted-close total-return proxies measured from the last trading close before the period. "
                "Active vs SPY is an arithmetic return spread, not regression alpha."
            )

        if period == "YTD" and portfolio_return is None and not daily_visible.empty:
            observed_days, expected_days, missing_days = _portfolio_window_coverage(
                snapshot["performance"], run_date, period
            )
            st.warning(
                f"IB daily TWR coverage is {observed_days} of {expected_days} required weekdays; "
                f"{len(missing_days)} dates are missing ({_missing_date_ranges(missing_days)}). "
                "Discontinuous observations are retained for reconciliation but are not compounded or labelled YTD. "
                "A wider lookback statement can contain complete account activity, full-statement TWR, and YTD dollar P&L "
                "without containing calendar-YTD TWR. Export an IB Activity Statement whose declared period begins January 1 "
                "and ends on the selected date, or a daily PortfolioAnalyst/NAV-TWR export, to populate authoritative YTD TWR."
            )
    with right:
        st.subheader("Decision brief")
        target_names = set(book.loc[is_target, "ticker"])
        held_names = set(positions["ticker"]) if not positions.empty else set()
        held_only = sorted(held_names - target_names)
        business_dates = _next_business_window(run_date, 7)
        upcoming = positions.loc[
            pd.to_datetime(positions["next_earnings_date"], errors="coerce").between(
                business_dates.min(), business_dates.max()
            )
        ].sort_values("next_earnings_date") if not positions.empty else pd.DataFrame()
        coverage = snapshot["correlation_coverage"]
        if held_only:
            st.warning(f"{len(held_only)} held name(s) are outside the target book: {', '.join(held_only)}")
        else:
            st.success("Every held stock is represented in the target book.")
        if not upcoming.empty:
            earnings_list = ", ".join(
                f"{row.ticker} · {_long_date(row.next_earnings_date)}"
                for row in upcoming.itertuples()
            )
            st.warning(
                f"{len(upcoming)} held position(s) report within the next 7 business dates: {earnings_list}."
            )
        else:
            st.info(
                "No held position has a known earnings date within the next 7 business dates "
                f"({_long_date(business_dates.min())}–{_long_date(business_dates.max())})."
            )
        if coverage.market_value_ratio < CORRELATION_COVERAGE_GATE:
            st.warning(
                f"Index-risk coverage is {_percent(coverage.market_value_ratio)} — "
                f"{_money(coverage.covered_gross_value, 0)} of {_money(coverage.total_gross_value, 0)} in stock market value "
                "has sufficient point-in-time return history. The uncovered sleeve is excluded and the covered sleeve is "
                f"renormalized; below the internal {CORRELATION_COVERAGE_GATE:.0%} gate, correlations are diagnostic only "
                "and should not drive precise hedge sizing."
            )
        else:
            st.success(f"Index-risk coverage passes at {_percent(coverage.market_value_ratio)}.")
        with st.expander("P&L basis"):
            performance_meta = snapshot["manifest"].get("ib_performance", {})
            st.write(performance_meta.get("basis", "Selected-run sealed book preamble."))
            st.caption(f"P&L as of {api.display_date(performance_meta.get('ib_profit_as_of_date')) or 'n/a'}")


def _render_positions(api: SimpleNamespace, snapshot: dict[str, Any], positions: pd.DataFrame, account_value: float) -> None:
    title, note = st.columns([1.4, 1.0])
    title.subheader("Holdings reconciliation")
    note.markdown(
        '<div class="section-note" style="text-align:right;padding-top:.35rem">Market value, target relation and exception state</div>',
        unsafe_allow_html=True,
    )
    if positions.empty:
        st.info("No stock holdings are available for this run.")
        return

    table = positions.copy()
    if not snapshot["holding_risk"].empty:
        dominant = snapshot["holding_risk"][["ticker", "dominant_benchmark"]].drop_duplicates("ticker")
        table = table.merge(dominant, on="ticker", how="left")
    else:
        table["dominant_benchmark"] = ""
    held_only = table["target_weight"].le(0)
    has_sector_map = table["dominant_benchmark"].isin(SECTOR_ETFS)
    table.loc[held_only & has_sector_map, "relation"] = (
        table.loc[held_only & has_sector_map, "relation"]
        + " · "
        + table.loc[held_only & has_sector_map, "dominant_benchmark"].astype(str)
    )
    table["state"] = table["internal_state"].map(_position_state)
    table["next_action"] = table.apply(_position_next_action, axis=1)
    display = table.rename(columns={
        "ticker": "Ticker",
        "next_earnings_date": "Next earnings",
        "market_value": "Market value",
        "account_weight": "Account",
        "avg_cost": "Average cost",
        "unrealized_pl": "Unrealized P&L",
        "starter_band_low": "Starter low",
        "starter_band_high": "Starter high",
        "add_band_low": "Add low",
        "add_band_high": "Add high",
        "trim_band_low": "Trim low",
        "trim_band_high": "Trim high",
        "relation": "Target relation",
        "state": "State",
        "next_action": "Next action",
    })
    display = display[[
        "Ticker", "Next earnings", "Market value", "Account", "Average cost", "Unrealized P&L",
        "Starter low", "Starter high", "Add low", "Add high", "Trim low", "Trim high",
        "Target relation", "State", "Next action",
    ]]
    _render_prototype_table(display, [
        ("Ticker", "Ticker", "ticker"),
        ("Next earnings", "Next earnings", "date"),
        ("Market value", "Market value", "money"),
        ("Account", "Account", "percent"),
        ("Average cost", "Average cost", "money2"),
        ("Unrealized P&L", "Unrealized P&L", "signed_money"),
        ("Starter low", "Starter low", "money2"),
        ("Starter high", "Starter high", "money2"),
        ("Add low", "Add low", "money2"),
        ("Add high", "Add high", "money2"),
        ("Trim low", "Trim low", "money2"),
        ("Trim high", "Trim high", "money2"),
        ("Target relation", "Target relation", "text"),
        ("State", "State", "state"),
        ("Next action", "Next action", "text"),
    ])
    _download_csv(display, "positions.csv", "download_positions", "Download positions · CSV")
    st.caption(
        f"Source: ledger/holding_state.csv + sealed target book · account denominator {_money(account_value, 2)}. "
        "State is the book's qualitative health flag. Next action applies the prototype policy: target holdings are held; "
        "watch/deteriorating names suspend adds; held-only drawdowns trigger watch or re-underwrite; all others receive a fit review."
    )


def _risk_label(value: object) -> str:
    if value is None or bool(pd.isna(value)):
        return "n/a"
    number = float(value)
    if number >= 0.70:
        return "Strong"
    if number >= 0.40:
        return "Moderate"
    return "Low"


def _render_index_risk(api: SimpleNamespace, run_date: str, snapshot: dict[str, Any]) -> None:
    st.subheader("Index risk · tactical versus structural")
    st.caption(
        "Every portfolio-level estimate is corr(covered current-holdings proxy, ETF daily log return), not ETF versus ETF. "
        "The proxy uses fixed current signed market-value weights, gross-normalised across covered stocks. "
        "Cash, options, and uncovered holdings are excluded."
    )
    st.caption(
        "Correlation percentages equal the coefficient r × 100. Shift is tactical minus structural and is shown in percentage points (pp)."
    )
    benchmark_risk = snapshot["benchmark_risk"]
    holding_risk = snapshot["holding_risk"]
    coverage = snapshot["correlation_coverage"]
    correlation_ok = bool(snapshot["correlation_manifest"])

    dominant = benchmark_risk.dropna(subset=["tactical"]).head(1)
    dominant_ticker = str(dominant.iloc[0]["benchmark"]) if not dominant.empty else "n/a"
    dominant_label = str(dominant.iloc[0]["label"]) if not dominant.empty else "Unavailable"
    dominant_value = dominant.iloc[0]["tactical"] if not dominant.empty else None
    spy = benchmark_risk.loc[benchmark_risk["benchmark"].eq("SPY")]
    spy_value = spy.iloc[0]["tactical"] if not spy.empty else None
    negative_shifts = int(benchmark_risk["shift"].lt(0).sum()) if not benchmark_risk.empty else 0
    shift_total = int(benchmark_risk["shift"].notna().sum()) if not benchmark_risk.empty else 0

    cards = st.columns(4)
    cards[0].metric(
        "Dominant index",
        dominant_ticker,
        f"{dominant_label} · tactical {_percent(dominant_value)} · {_risk_label(dominant_value)}",
        delta_color="off",
    )
    cards[1].metric(
        "Covered portfolio vs SPY",
        _percent(spy_value),
        f"tactical correlation - {_risk_label(spy_value)}",
        delta_color="off",
    )
    cards[2].metric("Correlations falling", f"{negative_shifts} / {shift_total}", "tactical below structural", delta_color="off")
    cards[3].metric(
        "Holding coverage",
        _percent(coverage.market_value_ratio),
        f"{coverage.covered_names} / {coverage.total_names} names",
        delta_color="off",
    )

    gate_pass = (
        coverage.market_value_ratio >= CORRELATION_COVERAGE_GATE
        and coverage.total_names > 0
        and coverage.covered_names / coverage.total_names >= CORRELATION_COVERAGE_GATE
    )
    if gate_pass:
        st.success("Coverage gate PASS — portfolio/index statistics are decision-grade for the covered methodology.")
    else:
        st.warning(
            f"Coverage gate LIMITED — {_money(coverage.covered_gross_value, 0)} of "
            f"{_money(coverage.total_gross_value, 0)} ({_percent(coverage.market_value_ratio)}) has sufficient return history. "
            f"The internal gate requires {CORRELATION_COVERAGE_GATE:.0%} by gross market value and name count. "
            "The covered sleeve is renormalized to 100%, so use these estimates for diagnosis and monitoring—not precise hedge sizing."
        )

    with st.expander("How to interpret and use these risk views"):
        st.markdown(
            "**Exact tactical estimator.** Pairwise-valid returns are ordered newest to oldest. An observation of age "
            "`a` receives raw weight `0.5^(a / 42)`; those weights are normalised to sum to one, then used in the "
            "weighted means, covariance, and variances. This is not a 42-day window: all valid paired observations are "
            "included, but a return 42 trading days old has half the raw weight of the latest return. The structural "
            "estimate is the ordinary, unweighted Pearson correlation over the latest 250 paired returns. For QQQ, the "
            "two series are the covered portfolio proxy and QQQ - not QQQ and SPY."
        )
        st.markdown(
            "**Tactical correlation** weights recent daily returns more heavily using a 42-trading-day half-life; "
            "it is the faster regime signal. **Structural correlation** is the ordinary Pearson correlation over the "
            "latest 250 trading days; it is the slower baseline. **Shift** is tactical minus structural: a positive "
            "shift means the portfolio is moving more closely with that index now, while a negative shift signals "
            "decoupling.\n\n"
            "The **portfolio-to-index ladder** ranks which ETF currently best explains covered portfolio co-movement. "
            "Use it to choose monitoring benchmarks and form a hedge shortlist. **Current estimates** gives the exact "
            "fast, slow, shift, and empirically mapped book weight for one selected ETF. The **holding-to-index map** "
            "assigns each covered stock to the sector ETF with its highest tactical correlation, revealing hidden factor "
            "clusters that issuer classifications can miss.\n\n"
            "Correlation is not beta: it measures co-movement, not the dollar hedge ratio or sensitivity magnitude. "
            "A hedge decision still requires beta, volatility, liquidity, cost, and basis-risk checks."
        )

    if benchmark_risk.empty:
        st.info("No eligible risk-price panel is available for portfolio/index estimates.")
    else:
        left, right = st.columns([1.45, 1.0])
        with left:
            st.markdown("#### Covered portfolio-to-index ladder")
            chart = benchmark_risk.dropna(subset=["tactical", "structural"]).sort_values("tactical", ascending=False)
            values = chart[["tactical", "structural"]].to_numpy(dtype=float)
            scale_min = -1.0 if values.size and float(values.min()) < 0 else 0.0
            scale_span = 1.0 - scale_min
            ladder_rows: list[str] = []
            for row in chart.itertuples():
                tactical = float(row.tactical)
                structural = float(row.structural)
                bar_width = max(0.0, min(100.0, 100.0 * (tactical - scale_min) / scale_span))
                marker_left = max(0.0, min(100.0, 100.0 * (structural - scale_min) / scale_span))
                ladder_rows.append(
                    '<div class="pc-ladder-row">'
                    f'<div class="pc-ladder-name">{html_lib.escape(str(row.benchmark))}</div>'
                    '<div class="pc-ladder-track">'
                    f'<span class="pc-ladder-bar" style="width:{bar_width:.1f}%"></span>'
                    f'<span class="pc-ladder-marker" style="left:{marker_left:.1f}%"></span>'
                    '</div>'
                    f'<div class="pc-ladder-values">T {_percent(tactical)} · S {_percent(structural)}</div>'
                    '</div>'
                )
            midpoint = (scale_min + 1.0) / 2.0
            st.markdown(
                '<div class="pc-ladder"><div class="pc-ladder-legend">'
                f'Solid blue = tactical · purple marker = structural · scale {_percent(scale_min, 0)} / {_percent(midpoint, 0)} / 100%'
                '</div>' + ''.join(ladder_rows) + '</div>',
                unsafe_allow_html=True,
            )
        with right:
            st.markdown("#### Current estimates")
            benchmark_options = list(benchmark_risk["benchmark"])
            default_index = benchmark_options.index("SPY") if "SPY" in benchmark_options else 0
            selected_index = st.selectbox(
                "Benchmark detail", benchmark_options, index=default_index, key="index_benchmark_detail"
            )
            selected_row = benchmark_risk.loc[benchmark_risk["benchmark"].eq(selected_index)].iloc[0]
            tactical_observations = int(selected_row.get("tactical_observations", 0) or 0)
            structural_observations = int(selected_row.get("structural_observations", 0) or 0)
            st.caption(
                f"Pair: covered current-holdings proxy vs {selected_index} - "
                f"{selected_row.get('label', selected_index)}. "
                f"Tactical uses {tactical_observations:,} paired returns; structural uses {structural_observations:,}."
            )
            mapped_weight = float(
                holding_risk.loc[
                    holding_risk["dominant_benchmark"].eq(selected_index), "book_weight"
                ].sum()
            ) if not holding_risk.empty else 0.0
            selected_shift = selected_row.get("shift")
            if selected_shift is None or bool(pd.isna(selected_shift)) or abs(float(selected_shift)) < 0.05:
                shift_read = "The recent relationship is broadly consistent with its structural baseline."
            elif float(selected_shift) > 0:
                shift_read = "Co-movement is tightening; common-index risk is increasing relative to the long baseline."
            else:
                shift_read = "Recent co-movement is below the structural baseline; current index overlap is lower than its long-run relationship."
            st.markdown(
                '<div class="pc-stat-grid">'
                f'<div class="pc-stat"><div class="pc-stat-label">Tactical · 42D half-life</div><div class="pc-stat-value">{_percent(selected_row.get("tactical"))}</div></div>'
                f'<div class="pc-stat"><div class="pc-stat-label">Structural · 250D</div><div class="pc-stat-value">{_percent(selected_row.get("structural"))}</div></div>'
                f'<div class="pc-stat"><div class="pc-stat-label">Shift</div><div class="pc-stat-value">{float(selected_shift) * 100:+.1f} pp</div></div>'
                f'<div class="pc-stat"><div class="pc-stat-label">Mapped book weight</div><div class="pc-stat-value">{_percent(mapped_weight)}</div></div>'
                '</div>'
                f'<div class="pc-readthrough"><strong>PM read-through · {html_lib.escape(str(selected_index))} · {html_lib.escape(str(selected_row.get("label", "")))}</strong><br>{html_lib.escape(shift_read)} '
                f'Tactical co-movement is {_risk_label(selected_row.get("tactical")).casefold()}.</div>',
                unsafe_allow_html=True,
            )
            with st.expander("All current estimates"):
                estimates = benchmark_risk.copy()
                estimates["Pair"] = "Covered portfolio vs " + estimates["benchmark"].astype(str)
                estimates = estimates.rename(columns={
                    "benchmark": "Index", "label": "Exposure", "tactical": "Tactical",
                    "structural": "Structural", "shift": "Shift",
                    "tactical_observations": "Tactical observations",
                    "structural_observations": "Structural observations",
                })
                _render_prototype_table(estimates, [
                    ("Pair", "Pair", "text"), ("Index", "Index", "ticker"), ("Exposure", "Exposure", "text"),
                    ("Tactical", "Tactical", "percent"), ("Structural", "Structural", "percent"),
                    ("Shift", "Shift", "signed_pp"),
                    ("Tactical observations", "Tactical obs.", "integer"),
                    ("Structural observations", "Structural obs.", "integer"),
                ])
                _download_csv(estimates, "portfolio_index_estimates.csv", "download_index_estimates", "Download estimates · CSV")

    st.markdown("#### Holding-to-index map")
    if holding_risk.empty:
        st.info("No holding-level correlation map passed the minimum-observation rule.")
    else:
        mapped = holding_risk.copy()
        mapped["risk_read"] = mapped.apply(lambda row: _risk_read(row["tactical"], row["shift"]), axis=1)
        display = mapped.rename(columns={
            "ticker": "Ticker", "market_value": "Held MV", "book_weight": "Book weight",
            "dominant_benchmark": "Dominant index", "tactical": "Tactical corr.",
            "structural": "Structural corr.", "shift": "Shift", "risk_read": "Risk read",
        })
        _render_prototype_table(display, [
            ("Ticker", "Ticker", "ticker"), ("Held MV", "Held MV", "money"),
            ("Book weight", "Book weight", "percent"), ("Dominant index", "Dominant index", "ticker"),
            ("Tactical corr.", "Tactical corr.", "percent"),
            ("Structural corr.", "Structural corr.", "percent"),
            ("Shift", "Shift", "signed_pp"), ("Risk read", "Risk read", "text"),
        ])
        _download_csv(display, "holding_to_index_map.csv", "download_holding_map", "Download holding map · CSV")
        clusters = (
            mapped.groupby(["dominant_benchmark", "benchmark_label"], as_index=False)
            .agg(book_weight=("book_weight", "sum"), holdings=("ticker", "size"))
            .sort_values("book_weight", ascending=False)
        )
        st.caption(
            "Dominant ETF is the highest tactical correlation among the nine sector benchmarks. "
            "This is an empirical exposure map, not an issuer classification."
        )
        with st.expander("Exposure clusters"):
            cluster_display = clusters.rename(columns={
                "dominant_benchmark": "Index", "benchmark_label": "Exposure",
                "book_weight": "Book weight", "holdings": "Holdings",
            })
            _render_prototype_table(cluster_display, [
                ("Index", "Index", "ticker"), ("Exposure", "Exposure", "text"),
                ("Book weight", "Book weight", "percent"), ("Holdings", "Holdings", "integer"),
            ])
            _download_csv(cluster_display, "exposure_clusters.csv", "download_exposure_clusters", "Download clusters · CSV")

    if not benchmark_risk.empty:
        st.markdown("#### Worked interpretation examples")
        st.caption(
            "Examples use the selected run's point-in-time estimates. Hedge sizing is illustrative and remains non-executable until beta, "
            "coverage, live pricing, liquidity, cost, mandate, and basis-risk checks pass."
        )
        example_cards: list[tuple[str, str]] = []
        leader = benchmark_risk.dropna(subset=["tactical"]).head(1)
        spy_example = benchmark_risk.loc[benchmark_risk["benchmark"].eq("SPY")]
        if not leader.empty and not spy_example.empty:
            leader_row = leader.iloc[0]
            spy_row = spy_example.iloc[0]
            spy_shift = spy_row.get("shift")
            spy_shift_text = (
                f"{float(spy_shift) * 100:+.1f} pp" if pd.notna(spy_shift) else "n/a"
            )
            example_cards.append((
                "Portfolio ladder + SPY benchmark detail",
                f"Fact: {leader_row['benchmark']} ({leader_row['label']}) ranks first at "
                f"T {_percent(leader_row['tactical'])} · S {_percent(leader_row['structural'])}; SPY is "
                f"T {_percent(spy_row['tactical'])} · S {_percent(spy_row['structural'])}, a {spy_shift_text} shift. "
                f"PM read: {leader_row['benchmark']} is the strongest current common-movement proxy, while SPY remains a meaningful "
                "market-risk reference; a negative SPY shift means recent co-movement is below the structural baseline. "
                "Boundary: correlation ranks proxy fit, but it is not beta and does not specify hedge notional.",
            ))
        else:
            example_cards.append((
                "Portfolio ladder + SPY benchmark detail",
                "Fact: a complete ladder leader and SPY estimate are not available. PM read: proxy ranking is inconclusive. "
                "Boundary: do not infer market exposure or size a hedge from incomplete history.",
            ))

        for example_ticker, expected_benchmark in (("LHX", "XAR"), ("ISRG", "IHI")):
            holding_example = holding_risk.loc[holding_risk["ticker"].eq(example_ticker)]
            if holding_example.empty:
                example_cards.append((
                    f"{example_ticker} → {expected_benchmark}",
                    f"Fact: {example_ticker} lacks sufficient history for a holding-level mapping. "
                    "PM read: the sector proxy cannot yet be validated. Boundary: retain the name as unhedged idiosyncratic risk.",
                ))
                continue
            holding_row = holding_example.iloc[0]
            holding_shift = holding_row.get("shift")
            holding_shift_text = (
                f"{float(holding_shift) * 100:+.1f} pp" if pd.notna(holding_shift) else "n/a"
            )
            benchmark = str(holding_row["dominant_benchmark"])
            benchmark_label = str(holding_row["benchmark_label"])
            company_risk = (
                "program and contract risk"
                if example_ticker == "LHX"
                else "procedure, product, regulatory, and earnings-gap risk"
            )
            example_cards.append((
                f"{example_ticker} → {benchmark}",
                f"Fact: {example_ticker} maps to {benchmark} ({benchmark_label}) at "
                f"T {_percent(holding_row['tactical'])} · S {_percent(holding_row['structural'])}, a {holding_shift_text} shift. "
                f"PM read: {benchmark} is the best of the tested ETF proxies for separating broad sector co-movement from the stock. "
                f"Boundary: it cannot hedge {company_risk}.",
            ))

        example_cards.append((
            "Illustrative hedge workflow",
            "Fact: assume $100,000 of unwanted market exposure, SPY beta of 1.10, and a 50% neutralization target. "
            "PM read: indicative SPY notional is $100,000 × 1.10 × 50% = $55,000 short, or comparable delta-equivalent "
            "protection after live pricing. Correlation screens proxy stability; beta sizes the hedge. "
            "Boundary: re-test market, sector, and correlation-break scenarios and define resize/remove triggers; with coverage below "
            "the 80% gate, this is a diagnostic example, not an implementation instruction.",
        ))
        example_html = "".join(
            '<div class="pc-example-card">'
            f'<div class="pc-example-title">{html_lib.escape(title)}</div>'
            f'{html_lib.escape(body)}'
            '</div>'
            for title, body in example_cards
        )
        st.markdown(f'<div class="pc-example-grid">{example_html}</div>', unsafe_allow_html=True)

    st.markdown("#### Verified ETF-to-ETF correlation matrix")
    if not correlation_ok:
        st.error(
            f"Exact-date orchestrator publication is unavailable or invalid: {snapshot['correlation_error']} "
            "A newer date is never substituted into this selected-run view."
        )
    else:
        matrix_window = st.radio(
            "ETF matrix horizon", [90, 250], horizontal=True,
            format_func=lambda value: f"{value} trading days",
            key="index_matrix_window",
        )
        try:
            rolling = _load_correlation_rolling(
                str(snapshot["correlation_dir"]),
                "pearson",
                int(matrix_window),
                snapshot["correlation_signature"],
            )
            matrix = latest_correlation_matrix(rolling, ETF_TICKERS, pair_column)
            values = matrix.to_numpy(dtype=float)
            figure = go.Figure(go.Heatmap(
                z=values, x=list(matrix.columns), y=list(matrix.index),
                zmin=-1, zmax=1, zmid=0,
                colorscale=[[0, PROTOTYPE_BLUE], [.5, "#f7f9fb"], [1, PROTOTYPE_RED]],
                text=[[f"{item:.0%}" for item in row] for row in values],
                texttemplate="%{text}", textfont={"size": 12},
                hovertemplate="%{y} vs %{x}<br>%{z:.1%}<extra></extra>",
                colorbar={"title": "ρ", "len": .72, "thickness": 11, "tickformat": ".0%"}, xgap=2, ygap=2,
            ))
            figure.update_layout(
                height=630, margin={"l": 34, "r": 8, "t": 12, "b": 44},
                paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
                font={"family": "system-ui", "color": "#111827", "size": 12},
            )
            figure.update_xaxes(tickfont={"size": 12})
            figure.update_yaxes(autorange="reversed", tickfont={"size": 12})
            st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
            matrix_csv = matrix.reset_index().rename(columns={"index": "Index"})
            _download_csv(matrix_csv, f"etf_correlation_matrix_{matrix_window}d.csv", "download_matrix", "Download matrix · CSV")
            manifest = snapshot["correlation_manifest"]
            st.caption(
                f"Orchestrator-owned publication PASS · exact as-of {manifest.get('as_of')} · "
                f"{manifest.get('return_rows', 0):,} return rows · total-return-adjusted ETF prices · no external requests."
            )
        except DashboardArtifactError as exc:
            st.error(f"Verified matrix rejected at read time: {exc}")

    covered = set(holding_risk["ticker"]) if not holding_risk.empty else set()
    all_stocks = set(
        snapshot["holdings"].loc[
            snapshot["holdings"]["asset_category"].astype(str).str.casefold().eq("stocks"), "symbol"
        ].astype(str).str.upper()
    ) if not snapshot["holdings"].empty else set()
    uncovered = sorted(all_stocks - covered)
    st.caption(
        f"Portfolio-risk price source: {snapshot['risk_path'] or 'unavailable'} · complete observations: "
        f"{coverage.complete_observations:,} · covered gross value {_money(coverage.covered_gross_value, 0)} / "
        f"{_money(coverage.total_gross_value, 0)}. Uncovered: {', '.join(uncovered) or 'none'}."
    )


def _render_research(api: SimpleNamespace, snapshot: dict[str, Any], positions: pd.DataFrame) -> None:
    book = snapshot["book"].loc[~snapshot["book"]["ticker"].eq("CASH")].copy()
    st.subheader("Research queue")
    st.caption("Common-scale ranking with price trend, earnings timing, and advisory execution bands.")
    left, right = st.columns([1.15, 2.0])
    with left:
        queue = st.radio(
            "Queue", ["All candidates", "Top 50", "Target only", "Held only"],
            horizontal=True, key="research_queue_view",
        )
    sectors = sorted(book["sector"].fillna("").replace("", "Unclassified").unique())
    with right:
        selected_sectors = st.multiselect(
            "Sector", sectors, default=sectors, key="research_sector_filter"
        )
    mask = book["sector"].fillna("").replace("", "Unclassified").isin(selected_sectors)
    if queue == "Target only":
        mask &= book["weight"].gt(0)
    elif queue == "Held only":
        mask &= book["IB_Holding"].eq(1)
    scoped = book.loc[mask].copy()
    scoped["normalized_score"] = pd.to_numeric(scoped["final_score"], errors="coerce")
    scoped = scoped.sort_values(["normalized_score", "ticker"], ascending=[False, True], na_position="last")
    if queue == "Top 50":
        scoped = scoped.loc[scoped["normalized_score"].notna()].head(50).copy()
    scoped["rank"] = range(1, len(scoped) + 1)
    scoped["target_flag"] = scoped["weight"].gt(0).map({True: "Target", False: ""})
    scoped["state"] = scoped["internal_state"].map(_position_state)
    scoped["next_action"] = scoped.apply(_research_next_action, axis=1)
    if not positions.empty:
        held_policy = positions.copy()
        held_policy["next_action"] = held_policy.apply(_position_next_action, axis=1)
        action_map = held_policy.drop_duplicates("ticker").set_index("ticker")["next_action"]
        held_actions = scoped["ticker"].map(action_map)
        scoped["next_action"] = held_actions.where(held_actions.notna(), scoped["next_action"])
    table = scoped.rename(columns={
        "rank": "Rank", "ticker": "Ticker", "sector": "Pipeline",
        "target_flag": "Target", "rel_ret_20d": "20D vs ETF",
        "next_earnings_date": "Next earnings", "current_price": "Current",
        "ma50": "MA50", "ma200": "MA200", "starter_band_low": "Starter low",
        "starter_band_high": "Starter high", "add_band_low": "Add low",
        "add_band_high": "Add high", "state": "State", "next_action": "Next action",
    })[[
        "Rank", "Ticker", "Pipeline", "Target", "20D vs ETF", "Next earnings",
        "Current", "MA50", "MA200", "Starter low", "Starter high", "Add low", "Add high",
        "State", "Next action",
    ]]
    _render_prototype_table(table, [
        ("Rank", "Rank", "integer"), ("Ticker", "Ticker", "ticker"),
        ("Pipeline", "Pipeline", "text"), ("Target", "Target", "text"),
        ("20D vs ETF", "20D vs ETF", "signed_percent"),
        ("Next earnings", "Next earnings", "date"), ("Current", "Current", "money2"),
        ("MA50", "MA50", "money2"), ("MA200", "MA200", "money2"),
        ("Starter low", "Starter low", "money2"), ("Starter high", "Starter high", "money2"),
        ("Add low", "Add low", "money2"), ("Add high", "Add high", "money2"),
        ("State", "State", "state"), ("Next action", "Next action", "text"),
    ])
    _download_csv(table, "research_queue.csv", "download_research_queue", "Download selected queue · CSV")
    st.caption(
        f"{len(table)} name(s) · ranking still uses calibrated final_score, but Score is intentionally removed from the table. "
        "Top 50 ranks the selected-sector universe from highest to lowest normalized score. "
        "Next earnings, trend levels, execution bands, State, and Next action remain visible in every queue view."
    )


def _render_data_quality(api: SimpleNamespace, run_date: str, snapshot: dict[str, Any]) -> None:
    st.subheader("Data quality and lineage")
    st.caption("Every panel declares its own contract. A PASS book does not silently certify supplementary calculations.")
    book = snapshot["book"]
    non_cash = book.loc[~book["ticker"].eq("CASH")]
    earnings_known = int(non_cash["next_earnings_date"].notna().sum())
    performance = snapshot["performance"]
    ytd_benchmark = _benchmark_performance_window(snapshot["risk_prices"], run_date, "YTD")
    ytd_complete = bool(snapshot["period_returns"].get("YTD")) or _portfolio_window_complete(
        performance, ytd_benchmark, run_date, "YTD"
    )
    coverage = snapshot["correlation_coverage"]
    correlation_gate_pass = (
        coverage.market_value_ratio >= CORRELATION_COVERAGE_GATE
        and coverage.total_names > 0
        and coverage.covered_names / coverage.total_names >= CORRELATION_COVERAGE_GATE
    )
    manifest = snapshot["manifest"]
    rows = [
        {
            "Layer": "Decision book",
            "Status": "PASS" if manifest.get("acceptance") == "PASS" and snapshot["sha_verified"] else "FAIL",
            "As of": run_date,
            "Coverage / control": f"{len(book)} rows · displayed SHA {snapshot['actual_sha'][:16]}…",
            "Authority": "final_manifest.json + final_target_book.csv",
        },
        {
            "Layer": "Broker holdings + cash",
            "Status": "AVAILABLE" if not snapshot["holdings"].empty else "MISSING",
            "As of": run_date,
            "Coverage / control": f"{len(snapshot['holdings'])} ledger rows · cash {_money(snapshot['cash'], 2)}",
            "Authority": "selected-run ledger artifacts; input hashes where declared",
        },
        {
            "Layer": "H1 shadow macro",
            "Status": "AVAILABLE" if snapshot["h1"] else "MISSING",
            "As of": api.display_date(snapshot["h1"].get("as_of_date")) if snapshot["h1"] else "",
            "Coverage / control": "latest candidate observation with as-of ≤ selected run",
            "Authority": "MacroLayer regime_h1; not sizing authority",
        },
        {
            "Layer": "Performance",
            "Status": "PASS" if ytd_complete else "LIMITED" if not performance.empty else "MISSING",
            "As of": api.display_date(performance["date"].max()) if not performance.empty else "",
            "Coverage / control": (
                "Exact/complete YTD portfolio TWR available"
                if ytd_complete
                else f"{len(performance)} aligned daily observations · exact YTD IB statement unavailable"
            ),
            "Authority": "IB exact-period or complete daily TWR + SPY/QQQ adjusted-close proxies",
        },
        {
            "Layer": "Portfolio beta",
            "Status": "AVAILABLE" if snapshot["beta"] is not None else "LIMITED",
            "As of": run_date,
            "Coverage / control": f"{snapshot['beta_covered']} / {snapshot['beta_total']} stock holdings",
            "Authority": "selected/fallback PIT risk price panel",
        },
        {
            "Layer": "Portfolio/index risk",
            "Status": "PASS" if correlation_gate_pass else "LIMITED",
            "As of": api.display_date(coverage.last_date) if coverage.last_date is not None else "",
            "Coverage / control": f"{coverage.covered_names}/{coverage.total_names} names · {_percent(coverage.market_value_ratio)} gross value",
            "Authority": "current holdings + PIT risk panel; 42D-HL/250D",
        },
        {
            "Layer": "ETF correlation publication",
            "Status": "PASS" if snapshot["correlation_manifest"] else "MISSING/FAIL",
            "As of": snapshot["correlation_manifest"].get("as_of", ""),
            "Coverage / control": "exact selected date · full hash/output/semantic verification",
            "Authority": "global orchestrator / index_correlations",
        },
        {
            "Layer": "Next earnings",
            "Status": "AVAILABLE" if earnings_known else "MISSING",
            "As of": run_date,
            "Coverage / control": f"{earnings_known} / {len(non_cash)} non-cash names",
            "Authority": "sealed target-book field; unknown dates remain blank",
        },
    ]
    quality = pd.DataFrame(rows)
    _render_prototype_table(quality, [
        ("Layer", "Layer", "text"), ("Status", "Status", "state"),
        ("As of", "As of", "text"), ("Coverage / control", "Coverage / control", "text"),
        ("Authority", "Authority", "text"),
    ])
    _download_csv(quality, "data_quality.csv", "download_data_quality", "Download data quality · CSV")
    if snapshot["correlation_error"]:
        st.warning(f"ETF correlation status: {snapshot['correlation_error']}")
    checks = manifest.get("checks", []) if isinstance(manifest, dict) else []
    with st.expander("Final-manifest controls"):
        check_frame = pd.DataFrame(checks)
        if check_frame.empty:
            st.info("No final-manifest controls are available.")
        else:
            check_columns = [
                (column, str(column).replace("_", " ").title(), "state" if str(column).casefold() == "status" else "text")
                for column in check_frame.columns
            ]
            _render_prototype_table(check_frame, check_columns)
            _download_csv(check_frame, "final_manifest_controls.csv", "download_manifest_controls", "Download controls · CSV")
    with st.expander("Artifact lineage"):
        st.code(
            "\n".join([
                f"book: {api.RUNS_ROOT / run_date / 'final' / 'final_target_book.csv'}",
                f"holdings: {snapshot['holdings_path']}",
                f"cash: {snapshot['cash_path']}",
                f"risk prices: {snapshot['risk_path'] or 'unavailable'}",
                f"ETF correlations: {snapshot['correlation_dir']}",
                f"H1 estimate: {snapshot['h1_path'] or 'unavailable'}",
            ]),
            language=None,
        )


def _render_sector_value_future() -> None:
    st.subheader("Sector relative value · future module")
    st.info(
        "The dashboard shell is ready for the ETF fundamental layer, but no valuation signal is fabricated before point-in-time holdings, prices and earnings are archived."
    )
    rows = []
    for ticker in SECTOR_ETFS:
        rows.append({
            "ETF": ticker,
            "Sector benchmark": ETF_LABELS[ticker],
            "Current status": "Awaiting PIT fundamental history",
            "Planned sector signal": "earnings yield + history z-score + dispersion",
            "Planned stock signal": "sector-neutral residual value percentile",
        })
    future = pd.DataFrame(rows)
    _render_prototype_table(future, [
        ("ETF", "ETF", "ticker"), ("Sector benchmark", "Sector benchmark", "text"),
        ("Current status", "Current status", "state"),
        ("Planned sector signal", "Planned sector signal", "text"),
        ("Planned stock signal", "Planned stock signal", "text"),
    ])
    _download_csv(future, "sector_value_future.csv", "download_sector_future", "Download module specification · CSV")
    st.markdown(
        """
        The production module will keep two contracts separate:

        - **Risk:** total-return-adjusted ETF prices from the correlation pipeline.
        - **Valuation:** point-in-time ETF holdings, constituent fundamentals and unadjusted tradable prices.

        ETF earnings yield will be aggregated as `Σ(weight × constituent earnings yield)` with explicit negative-earnings policy and coverage. ETF P/E will be the reciprocal only when the aggregate yield is valid; constituent P/E ratios will never be averaged directly.
        """
    )


def render_dashboard(api: SimpleNamespace) -> None:
    """Render the complete replacement dashboard using the supplied read API."""

    st.set_page_config(
        page_title="Portfolio Command Center",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_css()
    run_dates = api.list_run_dates()
    if not run_dates:
        st.error(f"No final target-book runs were found under {api.RUNS_ROOT}")
        st.stop()

    with st.sidebar:
        st.markdown("### Portfolio command center")
        run_date = st.selectbox("As-of date", run_dates, index=0, key="selected_run_date")
        st.caption("All panels are constrained to this date. Newer publications are never substituted.")
        st.divider()
        st.markdown("**Risk method**")
        st.caption("Tactical: 42D half-life EWMA\n\nStructural: 250D Pearson\n\nCoverage gate: 80%")
        st.divider()
        st.markdown("**Operating mode**")
        st.caption("Read-only · sealed inputs where available · blanks preserved")

    try:
        snapshot = _load_selected_run(api, run_date)
    except Exception as exc:
        st.error(f"The selected run could not be loaded: {exc}")
        st.exception(exc)
        st.stop()

    _render_header(api, run_date, snapshot)
    _render_integrity_banner(snapshot)

    holdings = snapshot["holdings"]
    positions_value = float(holdings["market_value"].sum()) if not holdings.empty else 0.0
    account_value = positions_value + float(snapshot["cash"])
    positions = _reconcile_positions(snapshot["book"], holdings, account_value)

    overview, positions_tab, index_risk, research, quality, sector_value = st.tabs([
        "Overview",
        "Positions",
        "Index risk",
        "Research queue",
        "Data quality",
        "Sector value · future",
    ])
    with overview:
        _render_overview(api, run_date, snapshot, positions)
    with positions_tab:
        _render_positions(api, snapshot, positions, account_value)
    with index_risk:
        _render_index_risk(api, run_date, snapshot)
    with research:
        _render_research(api, snapshot, positions)
    with quality:
        _render_data_quality(api, run_date, snapshot)
    with sector_value:
        _render_sector_value_future()

    st.divider()
    st.caption(
        "Decision-support interface only · portfolio artifacts remain the source of truth · "
        "correlation is exposure evidence, not a buy/sell signal."
    )
