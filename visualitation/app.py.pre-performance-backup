"""Portfolio visualization dashboard.

Page 1: Final Target Book viewer.

Reads the sealed final_target_book.csv from a portfolio_layer run directory
(macro-regime preamble + book table) plus its manifest and the run's ledger
trades, and renders regime context, KPI tiles, IB realized P&L (MTD/YTD),
and the book table. Read-only consumer of sealed artifacts - never writes
into portfolio_layer/.

Run:
    C:/Users/josel/miniconda3/envs/scalper-staging/python.exe -m streamlit run app.py
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
RUNS_ROOT = APP_DIR.parent / "portfolio_layer" / "output" / "runs"
DB_PATH = APP_DIR.parent / "portfolio_layer" / "db" / "portfolio_layer.sqlite"
H1_ROOT = APP_DIR.parent / "portfolio_layer" / "MacroLayer" / "out" / "regime_h1"
SERVING_DB_PATH = APP_DIR.parent / "portfolio_layer" / "MacroLayer" / "macro_serving.sqlite"
SECURITY_MAPPING_PATHS = (
    APP_DIR.parent / "ticker_mapping" / "All_tickers_biotech_enriched.csv",
    APP_DIR.parent / "ticker_mapping" / "med_dev_tickers_clean_keep.csv",
    APP_DIR.parent / "ticker_mapping" / "semiconductor_tickers.csv",
    APP_DIR.parent / "ticker_mapping" / "software_infrastructure_tickers.csv",
    APP_DIR.parent / "ticker_mapping" / "technology_hardware.csv",
    APP_DIR.parent / "ticker_mapping" / "machinery_tickers.csv",
    APP_DIR.parent / "ticker_mapping" / "defense_tickers.csv",
    APP_DIR.parent / "ticker_mapping" / "consumer_defensive.csv",
    APP_DIR.parent / "industrials" / "transportation" / "system_csvs" / "transportation_tickers.csv",
    APP_DIR.parent / "portfolio_layer" / "data" / "canonical_sector_overrides.csv",
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
    return df.loc[:, ["ticker", "industry"]].drop_duplicates("ticker")


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
    con = sqlite3.connect(str(SERVING_DB_PATH))
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
    con = sqlite3.connect(str(DB_PATH))
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
st.set_page_config(page_title="Final Target Book", layout="wide")

run_dates = list_run_dates()
if not run_dates:
    st.error(f"No runs with final/final_target_book.csv found under {RUNS_ROOT}")
    st.stop()

with st.sidebar:
    st.header("Run")
    run_date = st.selectbox("Run date", run_dates, index=0)

book_path = RUNS_ROOT / run_date / "final" / "final_target_book.csv"
manifest_path = RUNS_ROOT / run_date / "final" / "final_manifest.json"
preamble, book, missing_cols = load_book(run_date, book_path.stat().st_mtime)
manifest = load_manifest(run_date, manifest_path.stat().st_mtime if manifest_path.is_file() else 0.0)

security_metadata = load_security_metadata(security_mapping_signature())
book = book.merge(security_metadata, on="ticker", how="left", suffixes=("", "_map"))
for col in ("company_name", "industry"):
    if col not in book.columns:
        book[col] = ""
    book[col] = book[col].fillna("").astype(str).str.strip()
    mapped_col = f"{col}_map"
    if mapped_col in book.columns:
        book[col] = book[col].where(book[col].ne(""), book[mapped_col].fillna("").astype(str).str.strip())
        book = book.drop(columns=[mapped_col])
score_path = RUNS_ROOT / run_date / "stocks_scores.csv"
score_metadata = load_score_metadata(
    run_date, score_path.stat().st_mtime if score_path.is_file() else 0.0
)
book = book.merge(score_metadata.rename(columns={"industry": "score_industry"}), on="ticker", how="left")
book["score_industry"] = book["score_industry"].fillna("").astype(str).str.strip()
book["industry"] = book["score_industry"].where(book["score_industry"].ne(""), book["industry"])
book = book.drop(columns=["score_industry"])

run_ts = pd.Timestamp(run_date)
book["earnings_in_days"] = (book["next_earnings_date"] - run_ts).dt.days

# Average cost per share of held IB positions (ledger), shown beside current_price.
holdings_path = RUNS_ROOT / run_date / "ledger" / "holding_state.csv"
cash_path = RUNS_ROOT / run_date / "ledger" / "broker_cash_report.csv"
if holdings_path.is_file():
    _stocks = load_holdings(run_date, holdings_path.stat().st_mtime)
    _stocks = _stocks.loc[_stocks["asset_category"].eq("Stocks")]
    _grouped = _stocks.groupby("symbol")[["cost_basis", "quantity"]].sum()
    avg_cost = (_grouped["cost_basis"] / _grouped["quantity"]).replace([float("inf"), -float("inf")], pd.NA)
    book["avg_cost_price"] = book["ticker"].map(avg_cost)
else:
    book["avg_cost_price"] = pd.NA

# --- Header -----------------------------------------------------------------
st.title(f"Final Target Book - {run_date}")

acceptance = manifest.get("acceptance", "UNKNOWN")
# WARN checks are advisory diagnostics (they never gate acceptance) and must not
# render as failures; anything else non-PASS is a real failing check.
failed_checks = [
    c["check"]
    for c in manifest.get("checks", [])
    if c.get("status") not in ("PASS", "WARN")
]
warn_checks = [c["check"] for c in manifest.get("checks", []) if c.get("status") == "WARN"]

# Never display acceptance without verifying the manifest-recorded sha against the CSV
# actually being rendered (a stale PASS manifest must not front a different book).
_files_section = manifest.get("files", {})
_book_entry = _files_section.get("final_target_book.csv", {}) if isinstance(_files_section, dict) else {}
recorded_sha = str(_book_entry.get("sha256", "")) if isinstance(_book_entry, dict) else ""
actual_sha = file_sha256(str(book_path), book_path.stat().st_mtime)
sha_verified = bool(recorded_sha) and recorded_sha == actual_sha

if not manifest:
    st.error("Manifest missing or empty: final_manifest.json not found for this run - "
             "the book cannot be verified and must be treated as unsealed.")
elif recorded_sha and not sha_verified:
    st.error(f"Integrity error: final_target_book.csv sha256 {actual_sha[:16]}... does not "
             f"match the manifest-recorded {recorded_sha[:16]}... - "
             "the displayed book is not the sealed book.")
elif acceptance == "PASS" and not failed_checks:
    seal = "sha256 verified" if sha_verified else "NO sha recorded in manifest (unverifiable)"
    body = (f"Manifest acceptance: PASS - {len(manifest.get('checks', []))} checks, {seal}, "
            f"generated {manifest.get('generated_at', 'n/a')}")
    (st.success if sha_verified else st.warning)(body)
    if warn_checks:
        st.warning(f"Advisory diagnostics (non-gating): {warn_checks}")
else:
    st.error(f"Manifest acceptance: {acceptance} - failing checks: {failed_checks}")

# The manifest hashes only the book; ledger/DB-sourced panels are not covered by it.
if missing_cols:
    st.warning(
        f"This run predates the current book schema - {len(missing_cols)} column(s) are absent "
        f"and render blank: {', '.join(missing_cols)}.")

# --- Regime strip -----------------------------------------------------------
def fmt_regime(label: str) -> str:
    return label.replace("_", " ").title() if label else "n/a"

h1_path = find_h1_decision(run_date)
h1 = load_h1_decision(str(h1_path), h1_path.stat().st_mtime) if h1_path else {}
v1 = load_v1_decision(run_date, db_signature(SERVING_DB_PATH)) if SERVING_DB_PATH.is_file() else {}


def regime_delta(top_prob: object, confidence: object) -> str:
    parts = []
    if top_prob is not None and bool(pd.notna(top_prob)):
        parts.append(f"top prob {float(top_prob):.1%}")  # type: ignore[arg-type]
    if confidence is not None and bool(pd.notna(confidence)):
        parts.append(f"conf {float(confidence):.1%}")  # type: ignore[arg-type]
    return " - ".join(parts)


# V1 top probabilities come from the serving DB decision row; only trust them when
# that row agrees with the sealed preamble regimes the book was actually sized with.
v1_cur_tp = v1.get("current_top_probability") if str(v1.get("active_current_regime", "")) == preamble.get("active_current_regime", "") else None
v1_nxt_tp = v1.get("next_top_probability") if str(v1.get("active_next_regime", "")) == preamble.get("active_next_regime", "") else None

r1c1, r1c2, r1c3 = st.columns(3)
r1c1.metric("Macro regime (current)", fmt_regime(preamble.get("active_current_regime", "")),
            regime_delta(v1_cur_tp, preamble.get("current_confidence")) or None, delta_color="off")
r1c2.metric("Macro regime (next)", fmt_regime(preamble.get("active_next_regime", "")),
            regime_delta(v1_nxt_tp, preamble.get("next_confidence")) or None, delta_color="off")
r1c3.metric("Macro as-of", preamble.get("macro_as_of_date", "n/a"))

r2c1, r2c2, r2c3 = st.columns(3)
if h1:
    r2c1.metric("H1 estimate (current)", fmt_regime(str(h1.get("active_current_regime", ""))),
                regime_delta(h1.get("current_top_probability"), h1.get("current_confidence")) or None,
                delta_color="off")
    r2c2.metric("H1 estimate (next)", fmt_regime(str(h1.get("active_next_regime", ""))),
                regime_delta(h1.get("next_top_probability"), h1.get("next_confidence")) or None,
                delta_color="off")
    r2c3.metric("H1 as-of", str(h1.get("as_of_date", "n/a")))
    st.caption("H1 is the shadow candidate regime model (not promoted); "
               "the book is sized by the active source in the first row.")
else:
    r2c1.metric("H1 estimate (current)", "n/a")
    r2c2.metric("H1 estimate (next)", "n/a")
    r2c3.metric("H1 as-of", "n/a")

st.divider()

# --- KPI row ----------------------------------------------------------------
is_cash = book["ticker"].eq("CASH")
in_target = book["weight"].gt(0) & ~is_cash
is_ib = book["IB_Holding"].eq(1)
monitored_only = is_ib & book["weight"].eq(0)
overlap = in_target & is_ib
earnings_soon = book.loc[in_target | is_ib, "earnings_in_days"].between(0, 7).sum()

cash_weight = book.loc[is_cash, "weight"].sum()
k = st.columns(6)
k[0].metric("Target positions", int(in_target.sum()))
k[1].metric("Equity gross", f"{book['weight'].sum() - cash_weight:.2%}",
            f"{book['weight'].sum():.2%} incl. CASH", delta_color="off")
k[2].metric("CASH weight", f"{cash_weight:.2%}")
k[3].metric("IB holdings", int(is_ib.sum()))
k[4].metric("IB & target", int(overlap.sum()))
k[5].metric("Earnings <= 7d", int(earnings_soon))

st.divider()

# --- IB realized P&L (account-level, from the run's normalized IB trades) -----
st.subheader("IB realized P&L")

# The sealed preamble carries IB's own statement figures - those are authoritative.
# Summing broker_trades.realized_pl reproduces only the trade-level short-term
# component (it excludes dividends and broker interest), so it must not headline.
sealed_realized_mtd = preamble_float(preamble, "ib_realized_profit_loss_mtd")
sealed_realized_ytd = preamble_float(preamble, "ib_realized_profit_loss_ytd")
sealed_mtm_mtd = preamble_float(preamble, "ib_mark_to_market_mtd_profit")
sealed_mtm_ytd = preamble_float(preamble, "ib_mark_to_market_ytd_profit")

if sealed_realized_mtd is not None or sealed_realized_ytd is not None:
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Realized P&L - MTD",
              f"${sealed_realized_mtd:,.2f}" if sealed_realized_mtd is not None else "n/a")
    s2.metric("Realized P&L - YTD",
              f"${sealed_realized_ytd:,.2f}" if sealed_realized_ytd is not None else "n/a")
    s3.metric("Mark-to-market - MTD",
              f"${sealed_mtm_mtd:,.2f}" if sealed_mtm_mtd is not None else "n/a")
    s4.metric("Mark-to-market - YTD",
              f"${sealed_mtm_ytd:,.2f}" if sealed_mtm_ytd is not None else "n/a")

    def component_line(period: str) -> str:
        labels = [("ib_realized_short_term", "short-term"), ("ib_realized_long_term", "long-term"),
                  ("ib_dividends", "dividends"), ("ib_net_broker_interest", "net interest")]
        parts = []
        for key, label in labels:
            value = preamble_float(preamble, f"{key}_{period}")
            if value is not None:
                parts.append(f"{label} ${value:,.2f}")
        return " - ".join(parts)

    st.caption(
        f"IB statement figures sealed in the book preamble (as of "
        f"{preamble.get('ib_profit_as_of_date', 'n/a')}) - the authoritative account numbers.  \n"
        f"MTD components: {component_line('mtd') or 'n/a'}  \n"
        f"YTD components: {component_line('ytd') or 'n/a'}"
    )
else:
    st.info("This run's book carries no sealed IB P&L preamble; falling back to trade-derived sums.")

st.markdown("**Trade-level attribution** (from the `broker_trades` history)")
if not DB_PATH.is_file():
    st.info(f"Portfolio-layer DB not found at {DB_PATH}.")
else:
    trades = load_trades(db_signature(DB_PATH))
    ytd_trades = trades.loc[
        trades["trade_date"].dt.year.eq(run_ts.year) & trades["trade_date"].le(run_ts)
    ]
    mtd_trades = ytd_trades.loc[ytd_trades["trade_date"].dt.month.eq(run_ts.month)]
    pl1, pl2 = st.columns(2)
    pl1.metric("Trade realized - MTD", f"${mtd_trades['realized_pl'].sum():,.2f}")
    pl2.metric("Trade realized - YTD", f"${ytd_trades['realized_pl'].sum():,.2f}")
    with st.expander("Per-symbol breakdown"):
        per_symbol = (
            ytd_trades.groupby("symbol")
            .agg(
                realized_pl_ytd=("realized_pl", "sum"),
                trades_ytd=("realized_pl", "size"),
            )
            .join(mtd_trades.groupby("symbol")["realized_pl"].sum().rename("realized_pl_mtd"))
        )
        per_symbol["realized_pl_mtd"] = per_symbol["realized_pl_mtd"].fillna(0.0)
        per_symbol = (
            per_symbol.loc[per_symbol["realized_pl_ytd"].ne(0) | per_symbol["realized_pl_mtd"].ne(0)]
            .sort_index()
            .reset_index()
            .loc[:, ["symbol", "realized_pl_mtd", "realized_pl_ytd", "trades_ytd"]]
        )
        st.dataframe(
            per_symbol.style.format({"realized_pl_mtd": "${:,.2f}", "realized_pl_ytd": "${:,.2f}"}),
            width="stretch",
        )
    accounts = sorted(str(a) for a in ytd_trades["account"].dropna().unique())
    st.caption(
        f"Per-trade `realized_pl` by trade date <= {run_date}, all asset categories "
        f"(stocks, options, crypto) across account(s) {', '.join(accounts) or 'n/a'}. "
        f"This is trade-level attribution only - it excludes dividends and broker interest, "
        f"so it will NOT tie to the sealed IB realized total above."
    )

st.divider()

# --- IB positions vs account value ---------------------------------------------
st.subheader("IB positions - cost vs market value")
if not holdings_path.is_file():
    st.info("No ledger/holding_state.csv in this run (the ledger only runs on the strategic cadence).")
else:
    holdings = load_holdings(run_date, holdings_path.stat().st_mtime)
    cash = load_ending_cash(run_date, cash_path.stat().st_mtime) if cash_path.is_file() else 0.0
    account_value = float(holdings["market_value"].sum()) + cash
    if account_value <= 0:  # never divide by a zero/absent account value
        st.warning("Account value is zero or unavailable; percentage-of-account figures are hidden.")

    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Account value", f"${account_value:,.0f}")
    a2.metric("Positions market value", f"${holdings['market_value'].sum():,.0f}")
    a3.metric("Cash", f"${cash:,.0f}",
              f"{cash / account_value:.1%} of account" if account_value > 0 else None,
              delta_color="off")
    a4.metric("Unrealized P&L", f"${holdings['unrealized_pl'].sum():,.0f}")

    pos = holdings.loc[:, ["symbol", "cost_basis", "market_value", "unrealized_pl"]].copy()
    cash_row = pd.DataFrame([{"symbol": "CASH", "cost_basis": cash,
                              "market_value": cash, "unrealized_pl": 0.0}])
    pos = pd.concat([pos, cash_row], ignore_index=True)
    pos["base"] = pos[["cost_basis", "market_value"]].min(axis=1)
    pos["gain"] = (pos["market_value"] - pos["cost_basis"]).clip(lower=0.0)
    pos["loss"] = (pos["cost_basis"] - pos["market_value"]).clip(lower=0.0)
    pos["pct_of_account"] = pos["market_value"] / account_value if account_value > 0 else 0.0
    pos = pos.sort_values("market_value")  # smallest at bottom; largest on top

    hover = ("<b>%{y}</b><br>cost basis $%{customdata[0]:,.0f}"
             "<br>market value $%{customdata[1]:,.0f}"
             "<br>unrealized P&L $%{customdata[2]:,.0f}"
             "<br>%{customdata[3]:.1%} of account<extra>%{fullData.name}</extra>")
    custom = pos[["cost_basis", "market_value", "unrealized_pl", "pct_of_account"]].values
    is_cash_bar = pos["symbol"].eq("CASH")
    fig = go.Figure()
    fig.add_trace(go.Bar(  # blue = the lower of cost basis / market value
        y=pos["symbol"], x=pos["base"].where(~is_cash_bar, 0.0), orientation="h",
        name="Position value (min of cost & market)", marker=dict(color=SERIES_BLUE),
        width=0.62, customdata=custom, hovertemplate=hover,
    ))
    fig.add_trace(go.Bar(
        y=pos["symbol"], x=pos["base"].where(is_cash_bar, 0.0), orientation="h",
        name="Cash", marker=dict(color=INK_MUTED),
        width=0.62, customdata=custom, hovertemplate=hover,
    ))
    fig.add_trace(go.Bar(  # green tip: bar ends at market value, boundary = cost
        y=pos["symbol"], x=pos["gain"], orientation="h",
        name="Unrealized gain", marker=dict(color=STATUS_GOOD),
        width=0.62, customdata=custom, hovertemplate=hover,
    ))
    fig.add_trace(go.Bar(  # red tip: bar ends at cost basis, boundary = market value
        y=pos["symbol"], x=pos["loss"], orientation="h",
        name="Unrealized loss", marker=dict(color=STATUS_CRITICAL),
        width=0.62, customdata=custom, hovertemplate=hover,
    ))
    for _, r in pos.iterrows():
        fig.add_annotation(
            x=max(r["cost_basis"], r["market_value"]), y=r["symbol"],
            text=f"{r['pct_of_account']:.1%} (${r['market_value']:,.0f})",
            showarrow=False, xanchor="left", xshift=6,
            font=dict(color=INK_SECONDARY, size=13),
        )
    base_layout(fig, height=max(380, 28 * len(pos) + 130))
    fig.update_layout(barmode="stack", xaxis_tickprefix="$", xaxis_tickformat=",.0f",
                      margin=dict(r=150))
    fig.update_yaxes(tickfont=dict(color=INK, size=13))
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "Each bar spans max(cost basis, market value): the colored tip is the unrealized "
        "gain (green, bar ends at market value) or loss (red, bar ends at cost basis); "
        "the label is the position's market value as % of the account. "
        "Source: `ledger/holding_state.csv` + `broker_cash_report.csv` Ending Cash."
    )

# --- Filter row (scopes every chart and the table below) ---------------------
all_sectors = [s if s else "Unclassified" for s in sorted(book["sector"].unique())]
fc1, fc2 = st.columns([2, 3])
with fc1:
    view = st.radio("View", ["All rows", "Target book", "IB holdings", "Monitored only"],
                    horizontal=True)
with fc2:
    picked = st.multiselect("Sectors", options=all_sectors, default=all_sectors)

mask = pd.Series(True, index=book.index)
if view == "Target book":
    mask &= in_target | is_cash
elif view == "IB holdings":
    mask &= is_ib
elif view == "Monitored only":
    mask &= monitored_only
sector_labels = book["sector"].where(book["sector"].ne(""), "Unclassified")
mask &= sector_labels.isin(picked)
scoped: pd.DataFrame = book.loc[mask].copy()

st.divider()

# --- Book table -----------------------------------------------------------------
st.subheader("Target Portfolio + IB positions")

TABLE_COLUMNS = [
    "ticker", "company_name", "weight", "IB_quantity", "earnings_in_days", "next_earnings_date",
    "sector", "rating", "internal_state", "action_state",
    "benchmark_ticker", "rel_ret_5d", "rel_ret_20d", "avg_cost_price", "current_price",
    "ma50", "ma200", "price_band_status",
    "starter_band_low", "starter_band_high", "add_band_low", "add_band_high",
    "trim_band_low", "trim_band_high",
]
table = (
    scoped.sort_values("weight", ascending=False)
    .reset_index(drop=True)
    .loc[:, TABLE_COLUMNS]
)


def style_rating(v: object) -> str:
    return RATING_STYLE.get(str(v), "")


def style_internal(v: object) -> str:
    return INTERNAL_STATE_BG.get(str(v), "")


def style_action(v: object) -> str:
    return ACTION_STATE_BG.get(str(v), "")


styler = (
    table.style
    .format({
        "weight": "{:.2%}", "current_price": "{:,.2f}", "avg_cost_price": "{:,.2f}",
        "rel_ret_5d": "{:.2%}", "rel_ret_20d": "{:.2%}",
        "ma50": "{:,.2f}", "ma200": "{:,.2f}",
        "starter_band_low": "{:,.2f}", "starter_band_high": "{:,.2f}",
        "add_band_low": "{:,.2f}", "add_band_high": "{:,.2f}",
        "trim_band_low": "{:,.2f}", "trim_band_high": "{:,.2f}",
        "IB_quantity": "{:,.0f}", "earnings_in_days": "{:.0f}",
        "next_earnings_date": lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "",
    }, na_rep="")
)
# Styler.map is the pandas>=2.1 name for applymap; local stubs predate it.
styler = styler.map(style_rating, subset=["rating"])  # pyright: ignore[reportAttributeAccessIssue]
styler = styler.map(style_internal, subset=["internal_state"])
styler = styler.map(style_action, subset=["action_state"])


def _price_css(r: pd.Series) -> str:
    price = r["current_price"]
    if bool(pd.isna(price)):
        return ""
    css = []
    starter_high = r["starter_band_high"]
    if bool(pd.notna(starter_high)) and bool(price <= starter_high):
        css.append(f"color: {SUCCESS_TEXT}; font-weight: 700")
    add_high = r["add_band_high"]
    if bool(pd.notna(add_high)) and bool(price <= add_high):
        css.append("background-color: rgba(12,163,12,0.16)")
    return "; ".join(css)


def _avg_cost_css(r: pd.Series) -> str:
    avg_cost = r["avg_cost_price"]
    price = r["current_price"]
    if bool(pd.isna(avg_cost)) or bool(pd.isna(price)):
        return ""
    if bool(avg_cost < price):  # position is in profit
        return "background-color: rgba(12,163,12,0.16)"
    if bool(avg_cost > price):  # position is under water
        return "background-color: rgba(208,59,59,0.16)"
    return ""


price_styles = table.apply(_price_css, axis=1)
styler = styler.apply(lambda col: price_styles, subset=["current_price"])
avg_cost_styles = table.apply(_avg_cost_css, axis=1)
styler = styler.apply(lambda col: avg_cost_styles, subset=["avg_cost_price"])

st.dataframe(styler, width="stretch", height=min(46 + 35 * len(table), 900))

_diag = table["price_band_status"].eq("diagnostic_only_missing_intrinsic").sum()
st.caption(
    f"`current_price` is green-bold when <= `starter_band_high` and filled green when <= "
    f"`add_band_high` - the test is the band's UPPER edge only, so a price that has fallen "
    f"*below* the band also colors. Check `price_band_status`: "
    f"{_diag} of {len(table)} shown rows are `diagnostic_only_missing_intrinsic` "
    f"(band derived from market reference, no intrinsic valuation - not an actionable level).  \n"
    f"Source: `{book_path}` - rows {len(book)} - sha256 {actual_sha[:16]}... "
    f"({'verified against manifest' if sha_verified else 'NOT VERIFIED'}). "
    f"The manifest hashes the book only - ledger, cash and DB panels above are not covered by it."
)


# --- Top normalized-score table ---------------------------------------------
st.divider()
title_col, control_col = st.columns([3, 1])
with title_col:
    st.subheader("Top normalized-score names")
with control_col:
    top_n = int(st.selectbox(
        "Number of names", options=[10, 20, 30, 40, 50], index=0, key="top_n_names"
    ))

# final_score is the calibrated common-scale score used for cross-sector ranking.
ranked = scoped.loc[~scoped["ticker"].eq("CASH")].copy()
ranked["normalized_score"] = pd.to_numeric(ranked["final_score"], errors="coerce")
ranked = (
    ranked.loc[ranked["normalized_score"].notna()]
    .sort_values(["normalized_score", "ticker"], ascending=[False, True])
    .head(top_n)
    .reset_index(drop=True)
)
TOP_TABLE_COLUMNS = [
    "ticker", "company_name", "normalized_score", "earnings_in_days", "next_earnings_date",
    "sector", "industry", "internal_state", "action_state", "current_price",
    "starter_band_low", "starter_band_high", "add_band_low", "add_band_high",
]
top_table = ranked.loc[:, TOP_TABLE_COLUMNS]
if top_table.empty:
    st.info("No scored rows are available for the current view and sector filters.")
else:
    top_styler = top_table.style.format({
        "normalized_score": "{:.4f}",
        "earnings_in_days": "{:.0f}",
        "current_price": "{:,.2f}",
        "starter_band_low": "{:,.2f}", "starter_band_high": "{:,.2f}",
        "add_band_low": "{:,.2f}", "add_band_high": "{:,.2f}",
        "next_earnings_date": lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "",
    }, na_rep="")
    top_styler = top_styler.map(style_internal, subset=["internal_state"])
    top_styler = top_styler.map(style_action, subset=["action_state"])
    st.dataframe(top_styler, width="stretch", height=min(46 + 35 * len(top_table), 900))
    st.caption(
        f"Top {len(top_table)} rows in the current view, sorted descending by the canonical "
        f"calibrated `final_score` (shown as normalized score)."
    )









