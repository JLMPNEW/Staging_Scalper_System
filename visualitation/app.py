"""Portfolio visualization dashboard.

Page 1: Final Target Book viewer.

Reads the sealed final_target_book.csv from a portfolio_layer run directory
(macro-regime preamble + book table) plus its manifest and the run's ledger
trades, and renders regime context, KPI tiles, IB realized P&L (MTD/YTD),
and the book table. Read-only consumer of sealed artifacts — never writes
into portfolio_layer/.

Run:
    C:/Users/josel/miniconda3/envs/scalper-staging/python.exe -m streamlit run app.py
"""

from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Palette (validated reference palette, light mode — see dataviz skill)
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

# Status backgrounds (tinted so the cell text stays primary ink — the label,
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
    "weight", "IB_quantity", "final_score", "score_confidence", "current_price",
    "rel_ret_5d", "rel_ret_20d", "ma50", "ma200",
    "starter_band_low", "starter_band_high", "add_band_low", "add_band_high",
    "trim_band_low", "trim_band_high",
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


@st.cache_data(show_spinner=False)
def load_book(run_date: str, mtime: float) -> tuple[dict, pd.DataFrame]:
    """Parse the preamble key/value block and the book table."""
    _ = mtime  # cache key only: reload when the sealed file changes
    text = (RUNS_ROOT / run_date / "final" / "final_target_book.csv").read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    header_idx = next(i for i, ln in enumerate(lines) if ln.startswith("ticker,"))
    preamble: dict[str, str] = {}
    for ln in lines[:header_idx]:
        if "," in ln:
            key, _, value = ln.partition(",")
            if key.strip():
                preamble[key.strip()] = value.strip()
    df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
    for col in NUMERIC_COLS:
        if col not in df.columns:  # older runs predate the market-signal columns
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "benchmark_ticker" not in df.columns:
        df["benchmark_ticker"] = pd.NA
    df["next_earnings_date"] = pd.to_datetime(df["next_earnings_date"], format="%m/%d/%Y", errors="coerce")
    df["sector"] = df["sector"].fillna("")
    return preamble, df


@st.cache_data(show_spinner=False)
def load_manifest(run_date: str, mtime: float) -> dict:
    _ = mtime
    path = RUNS_ROOT / run_date / "final" / "final_manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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
    rows = df.loc[df["line_item"].eq("Ending Cash"), "total"].astype(float).fillna(0.0)
    return float(rows.iloc[0]) if len(rows) else 0.0


@st.cache_data(show_spinner=False)
def load_trades(mtime: float) -> pd.DataFrame:
    """Load cumulative normalized IB trades from the portfolio-layer SQLite.

    The per-run ledger/broker_trades.csv holds only that statement's trades;
    the DB table is the deduplicated (by trade_key) full history.
    """
    _ = mtime
    con = sqlite3.connect(str(DB_PATH))
    try:
        df = pd.read_sql_query(
            "SELECT trade_date, symbol, asset_category, realized_pl FROM broker_trades", con
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
st.set_page_config(page_title="Final Target Book", page_icon="📘", layout="wide")

run_dates = list_run_dates()
if not run_dates:
    st.error(f"No runs with final/final_target_book.csv found under {RUNS_ROOT}")
    st.stop()

with st.sidebar:
    st.header("Run")
    run_date = st.selectbox("Run date", run_dates, index=0)

book_path = RUNS_ROOT / run_date / "final" / "final_target_book.csv"
manifest_path = RUNS_ROOT / run_date / "final" / "final_manifest.json"
preamble, book = load_book(run_date, book_path.stat().st_mtime)
manifest = load_manifest(run_date, manifest_path.stat().st_mtime if manifest_path.is_file() else 0.0)

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
st.title(f"Final Target Book — {run_date}")

acceptance = manifest.get("acceptance", "UNKNOWN")
failed_checks = [c["check"] for c in manifest.get("checks", []) if c.get("status") != "PASS"]
if acceptance == "PASS" and not failed_checks:
    st.success(f"Manifest acceptance: PASS — {len(manifest.get('checks', []))} checks, "
               f"generated {manifest.get('generated_at', 'n/a')}", icon="✅")
else:
    st.error(f"Manifest acceptance: {acceptance} — failing checks: {failed_checks or 'manifest missing'}", icon="⚠️")

# --- Regime strip -----------------------------------------------------------
def fmt_regime(label: str) -> str:
    return label.replace("_", " ").title() if label else "n/a"

rc1, rc2, rc3 = st.columns(3)
rc1.metric("Macro regime (current)", fmt_regime(preamble.get("active_current_regime", "")),
           f"confidence {float(preamble.get('current_confidence', 0)):.1%}", delta_color="off")
rc2.metric("Macro regime (next)", fmt_regime(preamble.get("active_next_regime", "")),
           f"confidence {float(preamble.get('next_confidence', 0)):.1%}", delta_color="off")
rc3.metric("Macro as-of", preamble.get("macro_as_of_date", "n/a"))

st.divider()

# --- KPI row ----------------------------------------------------------------
is_cash = book["ticker"].eq("CASH")
in_target = book["weight"].gt(0) & ~is_cash
is_ib = book["IB_Holding"].eq(1)
monitored_only = is_ib & book["weight"].eq(0)
overlap = in_target & is_ib
earnings_soon = book.loc[in_target | is_ib, "earnings_in_days"].between(0, 7).sum()

k = st.columns(6)
k[0].metric("Target positions", int(in_target.sum()))
k[1].metric("Gross weight", f"{book['weight'].sum():.2%}")
k[2].metric("CASH weight", f"{book.loc[is_cash, 'weight'].sum():.2%}")
k[3].metric("IB holdings", int(is_ib.sum()))
k[4].metric("IB ∩ target", int(overlap.sum()))
k[5].metric("Earnings ≤ 7d", int(earnings_soon))

st.divider()

# --- IB realized P&L (account-level, from the run's normalized IB trades) -----
st.subheader("IB realized P&L")
if not DB_PATH.is_file():
    st.info(f"Portfolio-layer DB not found at {DB_PATH}.")
else:
    trades = load_trades(DB_PATH.stat().st_mtime)
    ytd_trades = trades.loc[
        trades["trade_date"].dt.year.eq(run_ts.year) & trades["trade_date"].le(run_ts)
    ]
    mtd_trades = ytd_trades.loc[ytd_trades["trade_date"].dt.month.eq(run_ts.month)]
    pl1, pl2 = st.columns(2)
    pl1.metric("Realized P&L — MTD", f"${mtd_trades['realized_pl'].sum():,.2f}")
    pl2.metric("Realized P&L — YTD", f"${ytd_trades['realized_pl'].sum():,.2f}")
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
            .sort_values("realized_pl_ytd", ascending=False)
            .reset_index()
            .loc[:, ["symbol", "realized_pl_mtd", "realized_pl_ytd", "trades_ytd"]]
        )
        st.dataframe(
            per_symbol.style.format({"realized_pl_mtd": "${:,.2f}", "realized_pl_ytd": "${:,.2f}"}),
            width="stretch",
        )
    st.caption(
        f"Computed from the portfolio-layer DB `broker_trades` table (deduplicated IB "
        f"statement history), summing `realized_pl` by trade date ≤ {run_date}; "
        f"includes all asset categories (stocks and options)."
    )

st.divider()

# --- IB positions vs account value ---------------------------------------------
st.subheader("IB positions — cost vs market value")
if not holdings_path.is_file():
    st.info("No ledger/holding_state.csv in this run (the ledger only runs on the strategic cadence).")
else:
    holdings = load_holdings(run_date, holdings_path.stat().st_mtime)
    cash = load_ending_cash(run_date, cash_path.stat().st_mtime) if cash_path.is_file() else 0.0
    account_value = holdings["market_value"].sum() + cash

    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Account value", f"${account_value:,.0f}")
    a2.metric("Positions market value", f"${holdings['market_value'].sum():,.0f}")
    a3.metric("Cash", f"${cash:,.0f}", f"{cash / account_value:.1%} of account", delta_color="off")
    a4.metric("Unrealized P&L", f"${holdings['unrealized_pl'].sum():,.0f}")

    pos = holdings.loc[:, ["symbol", "cost_basis", "market_value", "unrealized_pl"]].copy()
    cash_row = pd.DataFrame([{"symbol": "CASH", "cost_basis": cash,
                              "market_value": cash, "unrealized_pl": 0.0}])
    pos = pd.concat([pos, cash_row], ignore_index=True)
    pos["base"] = pos[["cost_basis", "market_value"]].min(axis=1)
    pos["gain"] = (pos["market_value"] - pos["cost_basis"]).clip(lower=0.0)
    pos["loss"] = (pos["cost_basis"] - pos["market_value"]).clip(lower=0.0)
    pos["pct_of_account"] = pos["market_value"] / account_value
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
st.subheader("Book table")

TABLE_COLUMNS = [
    "ticker", "weight", "IB_quantity", "earnings_in_days", "next_earnings_date",
    "sector", "rating", "internal_state", "action_state",
    "benchmark_ticker", "rel_ret_5d", "rel_ret_20d", "avg_cost_price", "current_price",
    "ma50", "ma200",
    "starter_band_high", "starter_band_low", "add_band_high", "add_band_low",
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

st.caption(
    f"Source: `{book_path}` · rows {len(book)} · sha256 "
    f"{manifest.get('files', {}).get('final_target_book.csv', {}).get('sha256', 'n/a')[:16]}…"
)
