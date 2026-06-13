#!/usr/bin/env python3
"""Point-in-time signal diagnostics for the semiconductor scoring model.

Builds a historical panel of every scoring subfeature directly from
technology.sqlite (opened read-only), computes forward beta-hedged residual
returns, and reports rank information coefficients (IC), t-stats, hit rates and
quintile spreads per subfeature and per component. Also emits IC-proportional
suggested weights for review; nothing is written back to the database.

Point-in-time rules:
- Financial features use filing dates (feature_financial_statement.asof_date).
- Insider flows enter on Form 4 filing dates, deduplicated to one source feed.
- 13F snapshots enter on their (max) filing date per reporting period.
- Short interest enters on FINRA publication dates.
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import sqlite3
import sys
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.scoring_features import (  # noqa: E402
    COMPONENT_SPECS,
    SUBFEATURE_SPECS,
    percentile_scores,
    safe_div,
    safe_float,
    weighted_available_score,
)
from technology.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("semiconductor_signal_diagnostics")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "semiconductor_signal_diagnostics"

FIN_FIELDS = [
    "gross_margin", "operating_margin", "fcf_margin", "net_cash_to_assets",
    "sbc_pct_revenue", "inventory_days", "revenue_yoy_growth",
    "gross_profit_yoy_growth", "operating_income_yoy_growth",
    "free_cash_flow_yoy_growth", "revenue_acceleration", "ev_gross_profit",
    "ev_operating_income", "fcf_yield",
]


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run read-only IC diagnostics for semiconductor scoring signals.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def ro_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- statistics

def rankdata(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    rx = rankdata(xs)
    ry = rankdata(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy)


def quintile_spread(values: list[float], returns: list[float]) -> float | None:
    n = len(values)
    if n < 10:
        return None
    order = sorted(range(n), key=lambda i: values[i])
    q = max(1, n // 5)
    bottom = [returns[i] for i in order[:q]]
    top = [returns[i] for i in order[-q:]]
    return sum(top) / len(top) - sum(bottom) / len(bottom)


def newey_west_t_stat(values: list[float], lags: int) -> float | None:
    n = len(values)
    if n < 3:
        return None
    mean_value = sum(values) / n
    centered = [value - mean_value for value in values]
    gamma0 = sum(value * value for value in centered) / n
    long_run_var = gamma0
    max_lag = min(max(0, int(lags)), n - 1)
    for lag in range(1, max_lag + 1):
        gamma = sum(centered[i] * centered[i - lag] for i in range(lag, n)) / n
        weight = 1.0 - lag / (max_lag + 1.0)
        long_run_var += 2.0 * weight * gamma
    if long_run_var <= 0:
        return None
    standard_error = math.sqrt(long_run_var / n)
    return mean_value / standard_error if standard_error > 0 else None


def raw_t_stat(values: list[float]) -> float | None:
    n = len(values)
    if n < 3:
        return None
    mean_value = sum(values) / n
    std_value = math.sqrt(sum((x - mean_value) ** 2 for x in values) / (n - 1))
    return mean_value / std_value * math.sqrt(n) if std_value > 0 else None


def newey_west_lags_for_horizon(horizon_days: int, step_days: int) -> int:
    if step_days <= 0 or horizon_days <= step_days:
        return 0
    return max(0, math.ceil(horizon_days / step_days) - 1)


def is_member_on_date(intervals: list[tuple[date, date | None]] | None, asof: date) -> bool:
    if not intervals:
        return False
    for start, end in intervals:
        if start <= asof and (end is None or asof <= end):
            return True
    return False


# ---------------------------------------------------------------- price layer

class PriceSeries:
    __slots__ = ("dates", "adj", "close", "volume", "_by_date")

    def __init__(self) -> None:
        self.dates: list[date] = []
        self.adj: list[float] = []
        self.close: list[float] = []
        self.volume: list[float] = []
        self._by_date: dict[date, float] | None = None

    def idx_at(self, asof: date) -> int:
        return bisect_right(self.dates, asof) - 1

    def ret(self, end_idx: int, lookback: int) -> float | None:
        start_idx = end_idx - lookback
        if start_idx < 0 or end_idx >= len(self.adj):
            return None
        start = self.adj[start_idx]
        return self.adj[end_idx] / start - 1.0 if start > 0 else None

    def ret_between(self, start_idx: int, end_idx: int) -> float | None:
        if start_idx < 0 or end_idx <= start_idx or end_idx >= len(self.adj):
            return None
        start = self.adj[start_idx]
        return self.adj[end_idx] / start - 1.0 if start > 0 else None


def coerce_source_ids(raw: Any, default: str) -> list[str]:
    if raw is None:
        return [default]
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        values = [str(item).strip() for item in raw]
    else:
        values = [str(raw).strip()]
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out or [default]


def research_price_source_ids(config: dict[str, Any]) -> list[str]:
    default_source = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted"))
    raw = cfg_get(config, "semiconductor_research.price_source_ids", None)
    return coerce_source_ids(raw, default_source)


def load_prices(conn: sqlite3.Connection, source_ids: str | list[str], tickers: list[str]) -> dict[str, PriceSeries]:
    out: dict[str, PriceSeries] = {ticker: PriceSeries() for ticker in tickers}
    source_list = coerce_source_ids(source_ids, str(source_ids) if isinstance(source_ids, str) else "")
    placeholders = ",".join("?" for _ in tickers)
    by_ticker_date: dict[tuple[str, date], tuple[float, float, float]] = {}

    for source_id in source_list:
        rows = conn.execute(
            f"""
            SELECT ticker, bar_date, adj_close, close, volume
            FROM fact_price_ohlcv
            WHERE source_id = ? AND adj_close IS NOT NULL AND ticker IN ({placeholders})
            ORDER BY ticker, bar_date
            """,
            (source_id, *tickers),
        )
        for row in rows:
            ticker = str(row["ticker"])
            bar_date = parse_date(row["bar_date"])
            adj = safe_float(row["adj_close"])
            if ticker not in out or bar_date is None or adj is None or adj <= 0:
                continue
            key = (ticker, bar_date)
            if key in by_ticker_date:
                continue
            by_ticker_date[key] = (adj, float(row["close"] or 0.0), float(row["volume"] or 0.0))

    for (ticker, bar_date), values in sorted(by_ticker_date.items(), key=lambda item: (item[0][0], item[0][1])):
        series = out[ticker]
        adj, close, volume = values
        series.dates.append(bar_date)
        series.adj.append(adj)
        series.close.append(close)
        series.volume.append(volume)
    return out


def market_subfeatures(series: PriceSeries, asof: date, soxx: PriceSeries) -> dict[str, float | None]:
    idx = series.idx_at(asof)
    if idx < 20 or (asof - series.dates[idx]).days > 10:
        return {}
    out: dict[str, float | None] = {}
    out["ret_3m"] = series.ret(idx, 63)
    out["ret_12m_ex_1m"] = series.ret_between(idx - 252, idx - 21)
    log_returns = [
        math.log(series.adj[i] / series.adj[i - 1])
        for i in range(max(1, idx - 59), idx + 1)
        if series.adj[i - 1] > 0
    ]
    if len(log_returns) >= 20:
        mean = sum(log_returns) / len(log_returns)
        var = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        out["realized_vol_60d"] = math.sqrt(var) * math.sqrt(252.0)
    window = series.adj[max(0, idx - 251): idx + 1]
    if len(window) >= 20:
        peak = window[0]
        worst = 0.0
        for value in window:
            peak = max(peak, value)
            worst = min(worst, value / peak - 1.0)
        out["max_drawdown_12m"] = worst
        out["distance_from_52w_high"] = series.adj[idx] / max(window) - 1.0
    if idx >= 59:
        dollar = [series.adj[i] * series.volume[i] for i in range(idx - 59, idx + 1)]
        out["avg_dollar_volume_60d"] = sum(dollar) / len(dollar)
    if out.get("ret_3m") is not None and soxx.dates:
        soxx_idx = soxx.idx_at(series.dates[idx])
        bench_ret = soxx.ret(soxx_idx, 63) if soxx_idx >= 0 else None
        out["rel_strength_soxx_3m"] = out["ret_3m"] - bench_ret if bench_ret is not None else None
    return out


def trailing_beta(series: PriceSeries, bench: PriceSeries, asof: date, lookback: int) -> float:
    idx = series.idx_at(asof)
    if idx < 60:
        return 1.0
    if bench._by_date is None:
        bench._by_date = {d: v for d, v in zip(bench.dates, bench.adj)}
    bench_by_date = bench._by_date
    xs: list[float] = []
    ys: list[float] = []
    for i in range(max(1, idx - lookback + 1), idx + 1):
        b0 = bench_by_date.get(series.dates[i - 1])
        b1 = bench_by_date.get(series.dates[i])
        if not b0 or not b1:
            continue
        xs.append(b1 / b0 - 1.0)
        ys.append(series.adj[i] / series.adj[i - 1] - 1.0)
    if len(xs) < 60:
        return 1.0
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return 1.0
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
    return max(0.25, min(3.0, beta))


# ------------------------------------------------------------ feature layers

def load_financial_rows(conn: sqlite3.Connection, source_id: str, model_family: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        f"""
        SELECT ticker, asof_date, fiscal_period_end, diluted_shares,
               free_cash_flow_ttm, net_income_ttm, market_cap, net_cash,
               fx_rate_balance_sheet, inventory, revenue_ttm, {", ".join(FIN_FIELDS)}
        FROM feature_financial_statement
        WHERE source_id = ? AND model_family = ?
        ORDER BY ticker, asof_date, fiscal_period_end
        """,
        (source_id, model_family),
    )
    for row in rows:
        out.setdefault(str(row["ticker"]), []).append(dict(row))
    return out


def financial_subfeatures(rows: list[dict[str, Any]], asof_iso: str) -> dict[str, float | None]:
    latest = None
    for row in rows:
        if str(row["asof_date"]) <= asof_iso:
            latest = row
        else:
            break
    if latest is None:
        return {}
    out: dict[str, float | None] = {field: safe_float(latest.get(field)) for field in FIN_FIELDS}
    # Stash filing-date valuation inputs (underscore keys are ignored by the
    # subfeature registry) so reprice_valuation can move ratios to the panel date.
    out["_val_asof"] = str(latest.get("asof_date") or "")  # type: ignore[assignment]
    out["_market_cap_f"] = safe_float(latest.get("market_cap"))
    out["_net_cash_f"] = safe_float(latest.get("net_cash"))
    out["_fx_balance_rate_f"] = safe_float(latest.get("fx_rate_balance_sheet"))
    net_income_ttm = safe_float(latest.get("net_income_ttm"))
    out["fcf_to_net_income"] = safe_div(
        safe_float(latest.get("free_cash_flow_ttm")),
        net_income_ttm if net_income_ttm and net_income_ttm > 0 else None,
    )
    # Share dilution: latest diluted share count vs the row ~one year earlier.
    out["share_count_yoy_growth"] = None
    latest_shares = safe_float(latest.get("diluted_shares"))
    latest_end = parse_date(latest.get("fiscal_period_end"))
    if latest_shares is not None and latest_end is not None:
        for row in reversed(rows):
            if str(row["asof_date"]) > asof_iso:
                continue
            prior_end = parse_date(row.get("fiscal_period_end"))
            prior_shares = safe_float(row.get("diluted_shares"))
            if prior_end is None or prior_shares is None or prior_shares <= 0:
                continue
            gap = (latest_end - prior_end).days
            if 300 <= gap <= 460:
                out["share_count_yoy_growth"] = latest_shares / prior_shares - 1.0
                break
    # Inventory cycle: YoY change in inventory days, and inventory growth in
    # excess of TTM revenue growth. Both compare the latest filing to the row
    # whose period end sits ~one year earlier (balance-sheet items, so the
    # annuality class of the filing does not matter). Reported-currency values
    # are fine because the ratios are within-ticker.
    out["inventory_days_yoy_change"] = None
    out["inventory_to_revenue_growth_gap"] = None
    latest_inv_days = safe_float(latest.get("inventory_days"))
    latest_inv = safe_float(latest.get("inventory"))
    latest_rev_ttm = safe_float(latest.get("revenue_ttm"))
    if latest_end is not None:
        for row in reversed(rows):
            if str(row["asof_date"]) > asof_iso:
                continue
            prior_end = parse_date(row.get("fiscal_period_end"))
            if prior_end is None:
                continue
            gap = (latest_end - prior_end).days
            if not 300 <= gap <= 460:
                continue
            prior_inv_days = safe_float(row.get("inventory_days"))
            prior_inv = safe_float(row.get("inventory"))
            prior_rev_ttm = safe_float(row.get("revenue_ttm"))
            if out["inventory_days_yoy_change"] is None and latest_inv_days is not None and prior_inv_days is not None:
                out["inventory_days_yoy_change"] = latest_inv_days - prior_inv_days
            if (
                out["inventory_to_revenue_growth_gap"] is None
                and latest_inv is not None
                and prior_inv is not None
                and prior_inv > 0
                and latest_rev_ttm is not None
                and prior_rev_ttm is not None
                and prior_rev_ttm > 0
            ):
                out["inventory_to_revenue_growth_gap"] = (latest_inv / prior_inv - 1.0) - (latest_rev_ttm / prior_rev_ttm - 1.0)
            if out["inventory_days_yoy_change"] is not None and out["inventory_to_revenue_growth_gap"] is not None:
                break
    return out


def load_wsts_cycle_series(conn: sqlite3.Connection, lag_days: int) -> list[tuple[str, float]]:
    """[(available_date_iso, yoy_growth)] from worldwide 3MMA billings, publication-lagged, sorted."""
    rows = conn.execute(
        """
        SELECT period_month, value_millions_usd
        FROM fact_semiconductor_wsts_billings
        WHERE dataset_type = '3mma' AND region = 'Worldwide' AND value_millions_usd IS NOT NULL
        ORDER BY period_month
        """
    ).fetchall()
    by_month = {str(row["period_month"])[:7]: float(row["value_millions_usd"]) for row in rows}
    out: list[tuple[str, float]] = []
    for month_key, value in sorted(by_month.items()):
        year, month = int(month_key[:4]), int(month_key[5:7])
        prior = by_month.get(f"{year - 1:04d}-{month:02d}")
        if prior is None or prior <= 0:
            continue
        available = date(year, month, 1) + timedelta(days=lag_days)
        out.append((available.isoformat(), value / prior - 1.0))
    return out


def wsts_yoy_at(series: list[tuple[str, float]], asof_iso: str) -> float | None:
    value: float | None = None
    for available, yoy in series:
        if available <= asof_iso:
            value = yoy
        else:
            break
    return value


def wsts_regime_at(series: list[tuple[str, float]], asof_iso: str) -> str:
    yoy = wsts_yoy_at(series, asof_iso)
    if yoy is None:
        return "unknown"
    return "up" if yoy > 0 else "down"


def month_end_dates(asof: date, count: int) -> list[date]:
    end = date(asof.year, asof.month, 1) - timedelta(days=1)
    out = [end]
    for _ in range(count - 1):
        end = date(end.year, end.month, 1) - timedelta(days=1)
        out.append(end)
    return list(reversed(out))


def cycle_exposure_signals(
    tickers: list[str],
    prices: dict[str, PriceSeries],
    wsts: list[tuple[str, float]],
    asof: date,
    cohort_by_ticker: dict[str, str],
    *,
    months: int = 60,
    min_months: int = 24,
    min_peers: int = 4,
) -> dict[str, float]:
    """Ticker-specific cycle signal: cohort-shrunk beta to WSTS YoY innovations x current YoY.

    Per ticker, monthly returns are regressed on month-over-month changes in the
    *available* (publication-lagged) worldwide billings YoY. Raw betas are
    shrunk toward their calibration-cohort mean with empirical-Bayes weights
    w = var_cohort / (var_cohort + se_beta^2), so short-history names lean on
    their cohort's structural cyclicality instead of a noisy regression. The
    signal flips sign with the cycle: high-beta names rank high only while the
    (lagged) cycle state is positive.
    """
    if not wsts:
        return {}
    yoy_now = wsts_yoy_at(wsts, asof.isoformat())
    if yoy_now is None:
        return {}
    ends = month_end_dates(asof, months + 1)
    yoy_levels = [wsts_yoy_at(wsts, d.isoformat()) for d in ends]
    raw: dict[str, tuple[float, float]] = {}
    for ticker in tickers:
        series = prices.get(ticker)
        if series is None or not series.dates:
            continue
        xs: list[float] = []
        ys: list[float] = []
        prev_adj: float | None = None
        prev_yoy: float | None = None
        for month_end, yoy in zip(ends, yoy_levels):
            idx = series.idx_at(month_end)
            adj = series.adj[idx] if idx >= 0 and (month_end - series.dates[idx]).days <= 10 else None
            if adj is not None and prev_adj is not None and prev_adj > 0 and yoy is not None and prev_yoy is not None:
                xs.append(yoy - prev_yoy)
                ys.append(adj / prev_adj - 1.0)
            prev_adj = adj
            prev_yoy = yoy
        n = len(xs)
        if n < min_months:
            continue
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx <= 0:
            continue
        beta = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx
        resid_ss = sum((y - mean_y - beta * (x - mean_x)) ** 2 for x, y in zip(xs, ys))
        se2 = resid_ss / max(1, n - 2) / sxx
        raw[ticker] = (beta, se2)

    by_cohort: dict[str, list[float]] = {}
    for ticker, (beta, _se2) in raw.items():
        by_cohort.setdefault(cohort_by_ticker.get(ticker, ""), []).append(beta)
    all_betas = [beta for beta, _se2 in raw.values()]
    priors: dict[str, tuple[float, float]] = {}
    for cohort, betas in by_cohort.items():
        if len(betas) >= min_peers:
            mean = sum(betas) / len(betas)
            var = sum((beta - mean) ** 2 for beta in betas) / max(1, len(betas) - 1)
            priors[cohort] = (mean, var)
    global_prior: tuple[float, float] | None = None
    if len(all_betas) >= min_peers:
        mean = sum(all_betas) / len(all_betas)
        var = sum((beta - mean) ** 2 for beta in all_betas) / max(1, len(all_betas) - 1)
        global_prior = (mean, var)

    out: dict[str, float] = {}
    for ticker in tickers:
        prior = priors.get(cohort_by_ticker.get(ticker, "")) or global_prior
        entry = raw.get(ticker)
        if entry is not None and prior is not None:
            beta, se2 = entry
            prior_mean, prior_var = prior
            weight = prior_var / (prior_var + se2) if (prior_var + se2) > 0 else 0.0
            shrunk = weight * beta + (1.0 - weight) * prior_mean
        elif entry is not None:
            shrunk = entry[0]
        elif prior is not None:
            shrunk = prior[0]
        else:
            continue
        out[ticker] = shrunk * yoy_now
    return out


def reprice_valuation(feats: dict[str, Any], series: PriceSeries, asof: date) -> None:
    """Move filing-date valuation ratios to the panel date.

    Upstream stores mcap/EV ratios priced at the filing availability date. With
    the adjusted-close ratio r = adj(asof)/adj(filing): mcap(t) = mcap_f*r,
    EV(t) = EV_f + mcap_f*(r-1) holding net debt constant between filings, and
    fcf_yield(t) = fcf_yield_f / r (FCF yield is market-cap based upstream).
    """
    mcap_f = safe_float(feats.get("_market_cap_f"))
    val_asof = parse_date(feats.get("_val_asof"))
    if mcap_f is None or mcap_f <= 0 or val_asof is None:
        return
    f_idx = series.idx_at(val_asof)
    t_idx = series.idx_at(asof)
    if f_idx < 0 or t_idx <= f_idx:
        return
    filing_adj = series.adj[f_idx]
    asof_adj = series.adj[t_idx]
    if filing_adj <= 0 or asof_adj <= 0:
        return
    r = asof_adj / filing_adj
    fcf_yield = safe_float(feats.get("fcf_yield"))
    if fcf_yield is not None:
        feats["fcf_yield"] = fcf_yield / r
    net_cash = safe_float(feats.get("_net_cash_f"))
    balance_rate = safe_float(feats.get("_fx_balance_rate_f"))
    if net_cash is None or balance_rate is None:
        return
    ev_f = mcap_f - net_cash * balance_rate
    if abs(ev_f) < 1e-9:
        return
    ev_t = ev_f + mcap_f * (r - 1.0)
    for field in ("ev_gross_profit", "ev_operating_income"):
        ratio = safe_float(feats.get(field))
        if ratio is not None:
            feats[field] = ratio * ev_t / ev_f


def load_form4(conn: sqlite3.Connection, direct_source: str, upstream_source: str) -> dict[str, list[tuple[str, float, int, str]]]:
    """ticker -> sorted [(avail_date, signed_value, is_purchase, owner)] from one preferred feed."""
    rows = conn.execute(
        """
        SELECT ticker, source_id,
               COALESCE(NULLIF(filing_date, ''), transaction_date) AS avail_date,
               transaction_value, is_open_market_purchase, is_open_market_sale, rptowner_cik
        FROM fact_sec_form4_transaction
        WHERE source_id IN (?, ?)
          AND (is_open_market_purchase = 1 OR is_open_market_sale = 1)
        """,
        (direct_source, upstream_source),
    ).fetchall()
    by_ticker_source: dict[str, dict[str, list[tuple[str, float, int, str]]]] = {}
    for row in rows:
        avail = str(row["avail_date"] or "")
        value = safe_float(row["transaction_value"]) or 0.0
        if not avail:
            continue
        is_purchase = int(row["is_open_market_purchase"] or 0)
        signed = value if is_purchase else -value
        owner = str(row["rptowner_cik"] or "").lstrip("0")
        by_ticker_source.setdefault(str(row["ticker"]), {}).setdefault(str(row["source_id"]), []).append(
            (avail, signed, is_purchase, owner)
        )
    out: dict[str, list[tuple[str, float, int, str]]] = {}
    for ticker, sources in by_ticker_source.items():
        chosen = sources.get(direct_source) or sources.get(upstream_source) or []
        out[ticker] = sorted(chosen)
    return out


def insider_subfeatures(events: list[tuple[str, float, int, str]], asof_iso: str, window_days: int) -> dict[str, float | None]:
    asof = parse_date(asof_iso)
    if asof is None:
        return {}
    start_iso = (asof - timedelta(days=window_days)).isoformat()
    net = 0.0
    buyers: set[str] = set()
    seen = False
    for avail, signed, is_purchase, owner in events:
        if avail < start_iso or avail > asof_iso:
            continue
        seen = True
        net += signed
        if is_purchase and owner:
            buyers.add(owner)
    if not seen:
        return {"insider_net_value_90d": 0.0, "insider_cluster_buyers_90d": 0.0}
    return {"insider_net_value_90d": net, "insider_cluster_buyers_90d": float(len(buyers))}


def load_13f(conn: sqlite3.Connection, source_id: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        """
        SELECT ticker, asof_date, institutional_ownership_delta_pct
        FROM fact_13f_positioning
        WHERE source_id = ?
        ORDER BY ticker, asof_date
        """,
        (source_id,),
    )
    for row in rows:
        out.setdefault(str(row["ticker"]), []).append(dict(row))
    return out


def load_short(conn: sqlite3.Connection, source_id: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        """
        SELECT ticker, settlement_date, publication_date, short_interest_pct_float, days_to_cover
        FROM fact_short_interest
        WHERE source_id = ?
        ORDER BY ticker, settlement_date
        """,
        (source_id,),
    )
    for row in rows:
        out.setdefault(str(row["ticker"]), []).append(dict(row))
    return out


def load_borrow(conn: sqlite3.Connection, source_id: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        "SELECT ticker, asof_date, borrow_fee_rate FROM fact_ibkr_borrow_snapshot WHERE source_id = ? ORDER BY ticker, asof_date",
        (source_id,),
    )
    for row in rows:
        out.setdefault(str(row["ticker"]), []).append(dict(row))
    return out


def add_days(day: date | None, days: int) -> date | None:
    return day + timedelta(days=days) if day is not None else None


def min_table_date(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> date | None:
    row = conn.execute(query, params).fetchone()
    return parse_date(row[0]) if row is not None else None


def load_positioning_signal_birthdates(
    conn: sqlite3.Connection,
    *,
    direct_source: str,
    upstream_source: str,
    market_positioning_source: str,
    short_change_days: int,
) -> tuple[dict[str, date], list[dict[str, Any]]]:
    """Global signal start dates used to prevent pre-feed eras from diluting ICs."""
    form4_birth = min_table_date(
        conn,
        """
        SELECT MIN(COALESCE(NULLIF(filing_date, ''), transaction_date))
        FROM fact_sec_form4_transaction
        WHERE source_id IN (?, ?)
        """,
        (direct_source, upstream_source),
    )
    inst_birth = min_table_date(
        conn,
        "SELECT MIN(asof_date) FROM fact_13f_positioning WHERE source_id = ?",
        (market_positioning_source,),
    )
    short_birth = min_table_date(
        conn,
        """
        SELECT MIN(COALESCE(NULLIF(publication_date, ''), settlement_date, asof_date))
        FROM fact_short_interest
        WHERE source_id = ?
        """,
        (market_positioning_source,),
    )
    borrow_birth = min_table_date(
        conn,
        "SELECT MIN(asof_date) FROM fact_ibkr_borrow_snapshot WHERE source_id = ?",
        (market_positioning_source,),
    )
    birthdates = {
        "insider_net_value_90d": form4_birth,
        "insider_cluster_buyers_90d": form4_birth,
        "institutional_ownership_delta_pct": inst_birth,
        "latest_short_interest_pct_float": short_birth,
        "latest_days_to_cover": short_birth,
        "short_interest_change_3m": add_days(short_birth, short_change_days),
        "latest_borrow_fee_rate": borrow_birth,
    }
    clean = {signal: value for signal, value in birthdates.items() if value is not None}
    rows = [
        {
            "signal": signal,
            "birthdate": value.isoformat() if value is not None else "",
            "source_scope": (
                "form4"
                if signal.startswith("insider_")
                else "13f"
                if signal.startswith("institutional_")
                else "short_interest"
                if signal.startswith("short_") or signal.startswith("latest_short") or signal == "latest_days_to_cover"
                else "borrow"
            ),
            "gating_rule": "signal is set to NULL before this date in diagnostics/calibration panels",
        }
        for signal, value in sorted(birthdates.items())
    ]
    return clean, rows


def apply_signal_birthdates(feats: dict[str, Any], birthdates: dict[str, date], asof: date) -> None:
    for signal, birthdate in birthdates.items():
        if asof < birthdate:
            feats[signal] = None


def positioning_subfeatures(
    ticker: str,
    asof_iso: str,
    *,
    form4: dict[str, list[tuple[str, float, int, str]]],
    inst: dict[str, list[dict[str, Any]]],
    short: dict[str, list[dict[str, Any]]],
    borrow: dict[str, list[dict[str, Any]]],
) -> dict[str, float | None]:
    out = insider_subfeatures(form4.get(ticker, []), asof_iso, 90)
    latest_inst = None
    for row in inst.get(ticker, []):
        if str(row["asof_date"]) <= asof_iso:
            latest_inst = row
        else:
            break
    out["institutional_ownership_delta_pct"] = safe_float(latest_inst["institutional_ownership_delta_pct"]) if latest_inst else None
    available = [
        row for row in short.get(ticker, [])
        if str(row["settlement_date"]) <= asof_iso and (not str(row["publication_date"] or "") or str(row["publication_date"]) <= asof_iso)
    ]
    if available:
        latest_short = available[-1]
        out["latest_short_interest_pct_float"] = safe_float(latest_short["short_interest_pct_float"])
        out["latest_days_to_cover"] = safe_float(latest_short["days_to_cover"])
        cutoff = (parse_date(asof_iso) - timedelta(days=92)).isoformat()  # type: ignore[union-attr]
        prior = None
        for row in available:
            if str(row["settlement_date"]) <= cutoff:
                prior = row
        latest_pct = safe_float(latest_short["short_interest_pct_float"])
        prior_pct = safe_float(prior["short_interest_pct_float"]) if prior else None
        out["short_interest_change_3m"] = latest_pct - prior_pct if latest_pct is not None and prior_pct is not None else None
    latest_borrow = None
    for row in borrow.get(ticker, []):
        if str(row["asof_date"]) <= asof_iso:
            latest_borrow = row
        else:
            break
    out["latest_borrow_fee_rate"] = safe_float(latest_borrow["borrow_fee_rate"]) if latest_borrow else None
    return out


# ----------------------------------------------------------------- main run

def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def summarize(
    label: str,
    group: str,
    ic_series: list[float],
    spreads: list[float],
    coverage: list[int],
    min_t: float,
    *,
    newey_west_lags: int = 0,
) -> dict[str, Any]:
    n = len(ic_series)
    mean_ic = sum(ic_series) / n if n else None
    raw_t = raw_t_stat(ic_series)
    nw_t = newey_west_t_stat(ic_series, newey_west_lags)
    t_stat = nw_t if nw_t is not None else raw_t
    return {
        "signal": label,
        "group": group,
        "n_dates": n,
        "avg_coverage": round(sum(coverage) / len(coverage), 1) if coverage else 0,
        "mean_ic": round(mean_ic, 4) if mean_ic is not None else "",
        "ic_t_stat": round(t_stat, 2) if t_stat is not None else "",
        "raw_ic_t_stat": round(raw_t, 2) if raw_t is not None else "",
        "newey_west_lags": int(newey_west_lags),
        "hit_rate": round(sum(1 for x in ic_series if x > 0) / n, 3) if n else "",
        "q5_minus_q1_fwd_resid": round(sum(spreads) / len(spreads), 5) if spreads else "",
        "keep_candidate": int(t_stat is not None and abs(t_stat) >= min_t and (mean_ic or 0) > 0),
    }


def summarize_correlations(
    label: str,
    values: list[float],
    coverage: list[int],
    min_t: float,
    *,
    newey_west_lags: int = 0,
) -> dict[str, Any]:
    n = len(values)
    mean_value = sum(values) / n if n else None
    raw_t = raw_t_stat(values)
    nw_t = newey_west_t_stat(values, newey_west_lags)
    t_stat = nw_t if nw_t is not None else raw_t
    return {
        "signal": "wsts_cycle_exposure",
        "comparison_signal": label,
        "n_dates": n,
        "avg_coverage": round(sum(coverage) / len(coverage), 1) if coverage else 0,
        "mean_spearman": round(mean_value, 4) if mean_value is not None else "",
        "spearman_t_stat": round(t_stat, 2) if t_stat is not None else "",
        "raw_spearman_t_stat": round(raw_t, 2) if raw_t is not None else "",
        "newey_west_lags": int(newey_west_lags),
        "low_correlation_flag": int(t_stat is None or abs(mean_value or 0.0) < 0.50),
        "review_flag": int(t_stat is not None and abs(t_stat) >= min_t and abs(mean_value or 0.0) >= 0.70),
    }


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/signal_diagnostics"), base_dir=base_dir)
    start = parse_date(args.start) or parse_date(cfg_get(config, f"{CONFIG_KEY}.start_date", "2018-01-01")) or date(2018, 1, 1)
    end = parse_date(args.end) or date.today()
    step = int(cfg_get(config, f"{CONFIG_KEY}.step_trading_days", 21))
    horizons = [int(h) for h in cfg_get(config, f"{CONFIG_KEY}.horizons_trading_days", [21, 63])]
    newey_west_lags_by_horizon = {h: newey_west_lags_for_horizon(h, step) for h in horizons}
    bench_ticker = normalize_ticker(cfg_get(config, f"{CONFIG_KEY}.benchmark_ticker", "SMH"))
    beta_lookback = int(cfg_get(config, f"{CONFIG_KEY}.beta_lookback_days", 252))
    min_cross_section = int(cfg_get(config, f"{CONFIG_KEY}.min_cross_section", 30))
    min_t = float(cfg_get(config, f"{CONFIG_KEY}.min_abs_t_stat_for_keep", 1.5))
    price_sources = research_price_source_ids(config)
    fin_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    mp_source = str(cfg_get(config, "positioning_import.market_positioning_source_id", "market_positioning_upstream"))
    direct_source = str(cfg_get(config, "positioning_import.direct_ownership_source_id", "sec_ownership_direct"))
    upstream_source = str(cfg_get(config, "positioning_import.form4_source_id", "sec_insider_upstream"))
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    short_change_days = int(cfg_get(config, "positioning_import.lookback_days.short_change", 92))
    include_inactive = bool(cfg_get(config, f"{CONFIG_KEY}.include_inactive_tickers", cfg_get(config, "semiconductor_optuna_calibration.include_inactive_tickers", True)))

    with ro_connect(db_path) as conn:
        membership_by_ticker: dict[str, list[tuple[date, date | None]]] = {}
        cohort_by_ticker: dict[str, str] = {}
        if include_inactive:
            membership_rows = conn.execute(
                """
                SELECT m.ticker, m.start_date, m.end_date,
                       COALESCE(t.calibration_cohort_id, '') AS cohort
                FROM dim_universe_membership m
                JOIN dim_technology_taxonomy t
                  ON t.ticker = m.ticker
                 AND t.model_family = m.model_family
                WHERE m.model_family = ?
                  AND m.point_in_time_flag = 1
                  AND m.membership_status IN ('active', 'historical', 'inactive', 'review')
                ORDER BY m.ticker, m.start_date
                """,
                (model_family,),
            ).fetchall()
            for row in membership_rows:
                ticker = normalize_ticker(row["ticker"])
                start_date = parse_date(row["start_date"])
                if not ticker or start_date is None:
                    continue
                membership_by_ticker.setdefault(ticker, []).append((start_date, parse_date(row["end_date"])))
                cohort_by_ticker[ticker] = str(row["cohort"] or "")

        if not membership_by_ticker:
            universe_rows = conn.execute(
                """
                SELECT c.ticker, COALESCE(t.calibration_cohort_id, '') AS cohort
                FROM dim_company c
                JOIN dim_technology_taxonomy t ON t.ticker = c.ticker AND t.model_family = ?
                WHERE c.is_active = 1 ORDER BY c.ticker
                """,
                (model_family,),
            ).fetchall()
            for row in universe_rows:
                ticker = normalize_ticker(row["ticker"])
                if not ticker:
                    continue
                membership_by_ticker[ticker] = [(date(1900, 1, 1), None)]
                cohort_by_ticker[ticker] = str(row["cohort"] or "")

        universe = sorted(membership_by_ticker)
        LOGGER.info(
            "Universe tickers=%d db=%s (read-only, include_inactive=%s)",
            len(universe),
            db_path,
            include_inactive,
        )
        wsts_lag_days = int(cfg_get(config, "semiconductor_optuna_calibration.wsts_regime_lag_days", 45))
        lag_values_raw = cfg_get(config, f"{CONFIG_KEY}.wsts_lag_sensitivity_days", [30, 45, 60, 75])
        wsts_lag_sensitivity_days = sorted({int(value) for value in lag_values_raw} | {wsts_lag_days})
        wsts_cycles_by_lag = {lag_days: load_wsts_cycle_series(conn, lag_days) for lag_days in wsts_lag_sensitivity_days}
        wsts_cycle = wsts_cycles_by_lag.get(wsts_lag_days, [])
        prices = load_prices(conn, price_sources, universe + [bench_ticker, "SOXX"])
        bench = prices.get(bench_ticker, PriceSeries())
        soxx = prices.get("SOXX", PriceSeries())
        if not bench.dates:
            LOGGER.error("No benchmark prices for %s; cannot build panel.", bench_ticker)
            return 1
        fin_rows = load_financial_rows(conn, fin_source, model_family)
        form4 = load_form4(conn, direct_source, upstream_source)
        inst = load_13f(conn, mp_source)
        short = load_short(conn, mp_source)
        borrow = load_borrow(conn, mp_source)
        signal_birthdates, signal_birthdate_rows = load_positioning_signal_birthdates(
            conn,
            direct_source=direct_source,
            upstream_source=upstream_source,
            market_positioning_source=mp_source,
            short_change_days=short_change_days,
        )

    max_h = max(horizons)
    start_idx = bisect_right(bench.dates, start)
    panel_indices = list(range(max(start_idx, 260), len(bench.dates) - max_h, step))
    panel_indices = [i for i in panel_indices if bench.dates[i] <= end]
    LOGGER.info("Panel dates=%d from %s to %s step=%d", len(panel_indices), bench.dates[panel_indices[0]] if panel_indices else "-", bench.dates[panel_indices[-1]] if panel_indices else "-", step)

    raw_names = [spec[0] for spec in SUBFEATURE_SPECS]
    sub_ic: dict[tuple[str, int], list[float]] = {}
    sub_spread: dict[tuple[str, int], list[float]] = {}
    sub_cov: dict[tuple[str, int], list[int]] = {}
    comp_ic: dict[tuple[str, int], list[float]] = {}
    comp_spread: dict[tuple[str, int], list[float]] = {}
    comp_cov: dict[tuple[str, int], list[int]] = {}
    wsts_regime_ic: dict[tuple[str, int], list[float]] = {}
    wsts_regime_spread: dict[tuple[str, int], list[float]] = {}
    wsts_regime_cov: dict[tuple[str, int], list[int]] = {}
    wsts_cohort_ic: dict[tuple[str, str, int], list[float]] = {}
    wsts_cohort_spread: dict[tuple[str, str, int], list[float]] = {}
    wsts_cohort_cov: dict[tuple[str, str, int], list[int]] = {}
    wsts_lag_ic: dict[tuple[int, int], list[float]] = {}
    wsts_lag_spread: dict[tuple[int, int], list[float]] = {}
    wsts_lag_cov: dict[tuple[int, int], list[int]] = {}
    wsts_corr: dict[str, list[float]] = {}
    wsts_corr_cov: dict[str, list[int]] = {}
    wsts_corr_fields = [
        "ret_12m_ex_1m",
        "ret_3m",
        "rel_strength_soxx_3m",
        "realized_vol_60d",
        "max_drawdown_12m",
        "distance_from_52w_high",
        "avg_dollar_volume_60d",
    ]
    spec_by_raw = {raw: (higher_is_better, valid) for raw, _score_key, higher_is_better, valid in SUBFEATURE_SPECS}
    wsts_cohort_min_cross_section = int(cfg_get(config, f"{CONFIG_KEY}.wsts_cohort_min_cross_section", 10))

    for panel_idx in panel_indices:
        asof = bench.dates[panel_idx]
        asof_iso = asof.isoformat()
        rows: list[dict[str, Any]] = []
        fwd_resid: dict[int, dict[str, float]] = {h: {} for h in horizons}
        regime = wsts_regime_at(wsts_cycle, asof_iso)
        active_members = [ticker for ticker in universe if is_member_on_date(membership_by_ticker.get(ticker), asof)]
        exposures = cycle_exposure_signals(active_members, prices, wsts_cycle, asof, cohort_by_ticker)
        for ticker in active_members:
            series = prices.get(ticker)
            if series is None or not series.dates:
                continue
            feats = market_subfeatures(series, asof, soxx)
            if not feats:
                continue
            feats.update(financial_subfeatures(fin_rows.get(ticker, []), asof_iso))
            reprice_valuation(feats, series, asof)
            feats.update(positioning_subfeatures(ticker, asof_iso, form4=form4, inst=inst, short=short, borrow=borrow))
            apply_signal_birthdates(feats, signal_birthdates, asof)
            feats["wsts_cycle_exposure"] = exposures.get(ticker)
            feats["ticker"] = ticker
            feats["calibration_cohort_id"] = cohort_by_ticker.get(ticker, "")
            idx = series.idx_at(asof)
            beta = trailing_beta(series, bench, asof, beta_lookback)
            usable = False
            for h in horizons:
                target_date = bench.dates[panel_idx + h]
                target_idx = series.idx_at(target_date)
                fwd = series.ret_between(idx, target_idx)
                bench_fwd = bench.ret_between(panel_idx, panel_idx + h)
                if fwd is None or bench_fwd is None:
                    continue
                fwd_resid[h][ticker] = fwd - beta * bench_fwd
                usable = True
            if usable:
                rows.append(feats)
        if len(rows) < min_cross_section:
            continue
        # Subfeature ICs on direction-adjusted raw values.
        for raw_key, _score_key, higher_is_better, valid in SUBFEATURE_SPECS:
            for h in horizons:
                pairs = []
                for row in rows:
                    value = safe_float(row.get(raw_key))
                    resid = fwd_resid[h].get(str(row["ticker"]))
                    if value is None or resid is None:
                        continue
                    if valid is not None and not valid(value):
                        continue
                    pairs.append((value if higher_is_better else -value, resid))
                if len(pairs) < min_cross_section:
                    continue
                ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if ic is None:
                    continue
                sub_ic.setdefault((raw_key, h), []).append(ic)
                sub_cov.setdefault((raw_key, h), []).append(len(pairs))
                spread = quintile_spread([p[0] for p in pairs], [p[1] for p in pairs])
                if spread is not None:
                    sub_spread.setdefault((raw_key, h), []).append(spread)
                if raw_key == "wsts_cycle_exposure":
                    wsts_regime_ic.setdefault((regime, h), []).append(ic)
                    wsts_regime_cov.setdefault((regime, h), []).append(len(pairs))
                    if spread is not None:
                        wsts_regime_spread.setdefault((regime, h), []).append(spread)
                    cohort_pairs: dict[str, list[tuple[float, float]]] = {}
                    for row in rows:
                        value = safe_float(row.get(raw_key))
                        resid = fwd_resid[h].get(str(row["ticker"]))
                        if value is None or resid is None:
                            continue
                        cohort = str(row.get("calibration_cohort_id") or "unknown")
                        cohort_pairs.setdefault(cohort, []).append((value, resid))
                    for cohort, cpairs in cohort_pairs.items():
                        if len(cpairs) < wsts_cohort_min_cross_section:
                            continue
                        cohort_ic = spearman([p[0] for p in cpairs], [p[1] for p in cpairs])
                        if cohort_ic is None:
                            continue
                        wsts_cohort_ic.setdefault((cohort, regime, h), []).append(cohort_ic)
                        wsts_cohort_cov.setdefault((cohort, regime, h), []).append(len(cpairs))
                        cohort_spread = quintile_spread([p[0] for p in cpairs], [p[1] for p in cpairs])
                        if cohort_spread is not None:
                            wsts_cohort_spread.setdefault((cohort, regime, h), []).append(cohort_spread)

        for lag_days, lag_cycle in wsts_cycles_by_lag.items():
            lag_exposures = exposures if lag_days == wsts_lag_days else cycle_exposure_signals(universe, prices, lag_cycle, asof, cohort_by_ticker)
            for h in horizons:
                pairs = []
                for row in rows:
                    value = safe_float(lag_exposures.get(str(row["ticker"])))
                    resid = fwd_resid[h].get(str(row["ticker"]))
                    if value is None or resid is None:
                        continue
                    pairs.append((value, resid))
                if len(pairs) < min_cross_section:
                    continue
                ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if ic is None:
                    continue
                wsts_lag_ic.setdefault((lag_days, h), []).append(ic)
                wsts_lag_cov.setdefault((lag_days, h), []).append(len(pairs))
                spread = quintile_spread([p[0] for p in pairs], [p[1] for p in pairs])
                if spread is not None:
                    wsts_lag_spread.setdefault((lag_days, h), []).append(spread)

        for field in wsts_corr_fields:
            higher_is_better, valid = spec_by_raw.get(field, (True, None))
            pairs = []
            for row in rows:
                wsts_value = safe_float(row.get("wsts_cycle_exposure"))
                other = safe_float(row.get(field))
                if wsts_value is None or other is None:
                    continue
                if valid is not None and not valid(other):
                    continue
                pairs.append((wsts_value, other if higher_is_better else -other))
            if len(pairs) < min_cross_section:
                continue
            corr = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            if corr is None:
                continue
            wsts_corr.setdefault(field, []).append(corr)
            wsts_corr_cov.setdefault(field, []).append(len(pairs))
        # Component ICs using the production transform (percentiles + weights).
        for raw_key, score_key, higher_is_better, valid in SUBFEATURE_SPECS:
            scores = percentile_scores(rows, raw_key, higher_is_better=higher_is_better, valid=valid)
            for row in rows:
                row[score_key] = scores.get(str(row["ticker"]))
        for component, specs in COMPONENT_SPECS.items():
            for h in horizons:
                pairs = []
                for row in rows:
                    resid = fwd_resid[h].get(str(row["ticker"]))
                    if resid is None:
                        continue
                    score, quality, _a, _m, _d = weighted_available_score(row, specs, neutral_score=50.0)
                    if quality <= 0:
                        continue
                    pairs.append((score, resid))
                if len(pairs) < min_cross_section:
                    continue
                ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if ic is None:
                    continue
                comp_ic.setdefault((component, h), []).append(ic)
                comp_cov.setdefault((component, h), []).append(len(pairs))
                spread = quintile_spread([p[0] for p in pairs], [p[1] for p in pairs])
                if spread is not None:
                    comp_spread.setdefault((component, h), []).append(spread)

    score_to_component = {score: comp for comp, specs in COMPONENT_SPECS.items() for score, _w in specs}
    sub_rows: list[dict[str, Any]] = []
    for (raw_key, h), series_values in sorted(sub_ic.items()):
        group = score_to_component.get(f"{raw_key}_score", "")
        row = summarize(
            raw_key,
            group,
            series_values,
            sub_spread.get((raw_key, h), []),
            sub_cov.get((raw_key, h), []),
            min_t,
            newey_west_lags=newey_west_lags_by_horizon.get(h, 0),
        )
        row["horizon_days"] = h
        sub_rows.append(row)
    comp_rows: list[dict[str, Any]] = []
    for (component, h), series_values in sorted(comp_ic.items()):
        row = summarize(
            component,
            "component",
            series_values,
            comp_spread.get((component, h), []),
            comp_cov.get((component, h), []),
            min_t,
            newey_west_lags=newey_west_lags_by_horizon.get(h, 0),
        )
        row["horizon_days"] = h
        comp_rows.append(row)

    # IC-proportional suggested weights per component (primary horizon), for review only.
    primary_h = horizons[0]
    weight_rows: list[dict[str, Any]] = []
    for component, specs in COMPONENT_SPECS.items():
        raw_ics: dict[str, float] = {}
        for score_key, _weight in specs:
            raw_key = score_key.removesuffix("_score")
            values = sub_ic.get((raw_key, primary_h), [])
            raw_ics[score_key] = max(0.0, sum(values) / len(values)) if values else 0.0
        total = sum(raw_ics.values())
        for score_key, current_weight in specs:
            suggested = raw_ics[score_key] / total if total > 0 else current_weight
            weight_rows.append(
                {
                    "component": component,
                    "subfeature": score_key.removesuffix("_score"),
                    "current_weight": current_weight,
                    "mean_ic": round(raw_ics[score_key], 4),
                    "suggested_weight_ic_proportional": round(suggested, 3),
                    "horizon_days": primary_h,
                }
            )

    wsts_regime_rows: list[dict[str, Any]] = []
    for (regime, h), series_values in sorted(wsts_regime_ic.items()):
        row = summarize(
            "wsts_cycle_exposure",
            "wsts_regime",
            series_values,
            wsts_regime_spread.get((regime, h), []),
            wsts_regime_cov.get((regime, h), []),
            min_t,
            newey_west_lags=newey_west_lags_by_horizon.get(h, 0),
        )
        row["regime"] = regime
        row["horizon_days"] = h
        wsts_regime_rows.append(row)

    wsts_cohort_rows: list[dict[str, Any]] = []
    for (cohort, regime, h), series_values in sorted(wsts_cohort_ic.items()):
        row = summarize(
            "wsts_cycle_exposure",
            "wsts_cohort",
            series_values,
            wsts_cohort_spread.get((cohort, regime, h), []),
            wsts_cohort_cov.get((cohort, regime, h), []),
            min_t,
            newey_west_lags=newey_west_lags_by_horizon.get(h, 0),
        )
        row["cohort"] = cohort
        row["regime"] = regime
        row["horizon_days"] = h
        wsts_cohort_rows.append(row)

    wsts_lag_rows: list[dict[str, Any]] = []
    for (lag_days, h), series_values in sorted(wsts_lag_ic.items()):
        row = summarize(
            "wsts_cycle_exposure",
            "wsts_lag_sensitivity",
            series_values,
            wsts_lag_spread.get((lag_days, h), []),
            wsts_lag_cov.get((lag_days, h), []),
            min_t,
            newey_west_lags=newey_west_lags_by_horizon.get(h, 0),
        )
        row["lag_days"] = lag_days
        row["horizon_days"] = h
        row["is_configured_lag"] = int(lag_days == wsts_lag_days)
        wsts_lag_rows.append(row)

    wsts_corr_rows = [
        summarize_correlations(
            field,
            values,
            wsts_corr_cov.get(field, []),
            min_t,
            newey_west_lags=max(newey_west_lags_by_horizon.values()) if newey_west_lags_by_horizon else 0,
        )
        for field, values in sorted(wsts_corr.items())
    ]

    write_csv(output_dir / "subfeature_ic.csv", sub_rows)
    write_csv(output_dir / "component_ic.csv", comp_rows)
    write_csv(output_dir / "suggested_weights.csv", weight_rows)
    write_csv(output_dir / "wsts_cycle_regime_ic.csv", wsts_regime_rows)
    write_csv(output_dir / "wsts_cycle_cohort_ic.csv", wsts_cohort_rows)
    write_csv(output_dir / "wsts_cycle_lag_sensitivity.csv", wsts_lag_rows)
    write_csv(output_dir / "wsts_cycle_correlations.csv", wsts_corr_rows)
    write_csv(output_dir / "signal_birthdates.csv", signal_birthdate_rows)
    for row in comp_rows:
        LOGGER.info(
            "component=%s h=%s mean_ic=%s t=%s hit=%s cov=%s",
            row["signal"], row["horizon_days"], row["mean_ic"], row["ic_t_stat"], row["hit_rate"], row["avg_coverage"],
        )
    weak = [row for row in sub_rows if row["horizon_days"] == primary_h and not row["keep_candidate"]]
    LOGGER.info(
        "Wrote diagnostics to %s including WSTS regime/cohort/lag/correlation review files.",
        output_dir,
    )
    LOGGER.info("%d/%d subfeatures lack significant positive IC at h=%d (see keep_candidate=0 rows before reweighting).", len(weak), len([r for r in sub_rows if r["horizon_days"] == primary_h]), primary_h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
