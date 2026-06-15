from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from technology.core.config import cfg_get, load_yaml, resolve_path
from technology.core.logging_utils import configure_utc_logging
from technology.core.scoring_features import (
    COMPONENT_SPECS,
    SUBFEATURE_SPECS,
    percentile_scores,
    safe_div,
    safe_float,
    weighted_available_score,
)
from technology.core.text_norm import normalize_ticker


LOGGER = logging.getLogger("technology_signal_diagnostics")

FIN_FIELDS = [
    "gross_margin",
    "operating_margin",
    "fcf_margin",
    "net_cash_to_assets",
    "sbc_pct_revenue",
    "inventory_days",
    "revenue_yoy_growth",
    "gross_profit_yoy_growth",
    "operating_income_yoy_growth",
    "free_cash_flow_yoy_growth",
    "revenue_acceleration",
    "ev_gross_profit",
    "ev_operating_income",
    "fcf_yield",
]


@dataclass(frozen=True)
class SignalDiagnosticsSettings:
    description: str
    validate_description: str
    default_config: Path
    config_key: str
    default_model_family: str
    default_output_dir: str
    default_benchmark_ticker: str
    default_calibrated_config_key: str
    default_price_source_config_key: str = ""
    default_excluded_subfeatures: list[str] = field(default_factory=list)


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def ro_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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


def raw_t_stat(values: list[float]) -> float | None:
    n = len(values)
    if n < 3:
        return None
    mean_value = sum(values) / n
    std_value = math.sqrt(sum((x - mean_value) ** 2 for x in values) / (n - 1))
    return mean_value / std_value * math.sqrt(n) if std_value > 0 else None


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


def configured_price_source_ids(config: dict[str, Any], settings: SignalDiagnosticsSettings) -> list[str]:
    default_source = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted"))
    raw = cfg_get(config, f"{settings.config_key}.price_source_ids", None)
    if raw is None and settings.default_price_source_config_key:
        raw = cfg_get(config, f"{settings.default_price_source_config_key}.price_source_ids", None)
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
            ticker = normalize_ticker(row["ticker"])
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


def market_subfeatures(series: PriceSeries, asof: date, benchmark: PriceSeries) -> dict[str, float | None]:
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
    if out.get("ret_3m") is not None and benchmark.dates:
        bench_idx = benchmark.idx_at(series.dates[idx])
        bench_ret = benchmark.ret(bench_idx, 63) if bench_idx >= 0 else None
        out["rel_strength_bench_3m"] = out["ret_3m"] - bench_ret if bench_ret is not None else None
        out["rel_strength_soxx_3m"] = out["rel_strength_bench_3m"]
    return out


def trailing_beta(series: PriceSeries, bench: PriceSeries, asof: date, lookback: int) -> float:
    idx = series.idx_at(asof)
    if idx < 60:
        return 1.0
    if bench._by_date is None:
        bench._by_date = {d: v for d, v in zip(bench.dates, bench.adj)}
    xs: list[float] = []
    ys: list[float] = []
    for i in range(max(1, idx - lookback + 1), idx + 1):
        b0 = bench._by_date.get(series.dates[i - 1]) if bench._by_date is not None else None
        b1 = bench._by_date.get(series.dates[i]) if bench._by_date is not None else None
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


def load_financial_rows(conn: sqlite3.Connection, source_id: str, model_family: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        f"""
        SELECT ticker, asof_date, fiscal_period_end, diluted_shares,
               free_cash_flow_ttm, net_income_ttm, market_cap, net_cash,
               fx_rate_balance_sheet, inventory, revenue_ttm,
               deferred_revenue, remaining_performance_obligation, {", ".join(FIN_FIELDS)}
        FROM feature_financial_statement
        WHERE source_id = ? AND model_family = ?
        ORDER BY ticker, asof_date, fiscal_period_end
        """,
        (source_id, model_family),
    )
    for row in rows:
        out.setdefault(normalize_ticker(row["ticker"]), []).append(dict(row))
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
    out["_val_asof"] = str(latest.get("asof_date") or "")  # type: ignore[assignment]
    out["_market_cap_f"] = safe_float(latest.get("market_cap"))
    out["_net_cash_f"] = safe_float(latest.get("net_cash"))
    out["_fx_balance_rate_f"] = safe_float(latest.get("fx_rate_balance_sheet"))
    net_income_ttm = safe_float(latest.get("net_income_ttm"))
    out["fcf_to_net_income"] = safe_div(
        safe_float(latest.get("free_cash_flow_ttm")),
        net_income_ttm if net_income_ttm and net_income_ttm > 0 else None,
    )
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
    # Deferred-revenue / RPO booking signals (software forward-demand). RPO/revenue
    # is a point-in-time coverage ratio; the YoY growths are billings momentum.
    out["deferred_revenue_yoy_growth"] = None
    out["rpo_yoy_growth"] = None
    out["rpo_to_revenue"] = None
    latest_dr = safe_float(latest.get("deferred_revenue"))
    latest_rpo = safe_float(latest.get("remaining_performance_obligation"))
    if latest_rev_ttm is not None and latest_rev_ttm > 0 and latest_rpo is not None:
        out["rpo_to_revenue"] = latest_rpo / latest_rev_ttm
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
            prior_dr = safe_float(row.get("deferred_revenue"))
            prior_rpo = safe_float(row.get("remaining_performance_obligation"))
            if out["deferred_revenue_yoy_growth"] is None and latest_dr is not None and prior_dr is not None and prior_dr > 0:
                out["deferred_revenue_yoy_growth"] = latest_dr / prior_dr - 1.0
            if out["rpo_yoy_growth"] is None and latest_rpo is not None and prior_rpo is not None and prior_rpo > 0:
                out["rpo_yoy_growth"] = latest_rpo / prior_rpo - 1.0
            if out["deferred_revenue_yoy_growth"] is not None and out["rpo_yoy_growth"] is not None:
                break
    return out


def reprice_valuation(feats: dict[str, Any], series: PriceSeries, asof: date) -> None:
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
    ratio = asof_adj / filing_adj
    fcf_yield = safe_float(feats.get("fcf_yield"))
    if fcf_yield is not None:
        feats["fcf_yield"] = fcf_yield / ratio
    net_cash = safe_float(feats.get("_net_cash_f"))
    balance_rate = safe_float(feats.get("_fx_balance_rate_f"))
    if net_cash is None or balance_rate is None:
        return
    ev_f = mcap_f - net_cash * balance_rate
    if abs(ev_f) < 1e-9:
        return
    ev_t = ev_f + mcap_f * (ratio - 1.0)
    for field_name in ("ev_gross_profit", "ev_operating_income"):
        field_ratio = safe_float(feats.get(field_name))
        if field_ratio is not None:
            feats[field_name] = field_ratio * ev_t / ev_f


def load_form4(conn: sqlite3.Connection, direct_source: str, upstream_source: str) -> dict[str, list[tuple[str, float, int, str]]]:
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
        ticker = normalize_ticker(row["ticker"])
        avail = str(row["avail_date"] or "")
        value = safe_float(row["transaction_value"]) or 0.0
        if not ticker or not avail:
            continue
        is_purchase = int(row["is_open_market_purchase"] or 0)
        signed = value if is_purchase else -value
        owner = str(row["rptowner_cik"] or "").lstrip("0")
        by_ticker_source.setdefault(ticker, {}).setdefault(str(row["source_id"]), []).append((avail, signed, is_purchase, owner))
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
        out.setdefault(normalize_ticker(row["ticker"]), []).append(dict(row))
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
        out.setdefault(normalize_ticker(row["ticker"]), []).append(dict(row))
    return out


def load_borrow(conn: sqlite3.Connection, source_id: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        "SELECT ticker, asof_date, borrow_fee_rate FROM fact_ibkr_borrow_snapshot WHERE source_id = ? ORDER BY ticker, asof_date",
        (source_id,),
    )
    for row in rows:
        out.setdefault(normalize_ticker(row["ticker"]), []).append(dict(row))
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
    clean = {signal: value for signal, value in birthdates.items() if value is not None}
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


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    keys.append(key)
                    seen.add(key)
        fieldnames = keys
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


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


def component_specs_from_config(config: dict[str, Any], calibrated_config_key: str, excluded_raw: set[str]) -> dict[str, list[tuple[str, float]]]:
    raw_config = cfg_get(config, f"{calibrated_config_key}.subfeature_weights", None)
    if not isinstance(raw_config, dict):
        return {
            component: [(score_key, weight) for score_key, weight in specs if score_key.removesuffix("_score") not in excluded_raw]
            for component, specs in COMPONENT_SPECS.items()
        }
    out: dict[str, list[tuple[str, float]]] = {}
    for component, weights in raw_config.items():
        if not isinstance(weights, dict):
            continue
        specs: list[tuple[str, float]] = []
        for score_key, weight in weights.items():
            score_key_text = str(score_key)
            if score_key_text.removesuffix("_score") in excluded_raw:
                continue
            numeric_weight = safe_float(weight)
            if numeric_weight is None or numeric_weight <= 0:
                continue
            specs.append((score_key_text, numeric_weight))
        if specs:
            total = sum(weight for _score, weight in specs)
            out[str(component)] = [(score, weight / total) for score, weight in specs] if total > 0 else specs
    return out or COMPONENT_SPECS


def configured_subfeature_specs(config: dict[str, Any], settings: SignalDiagnosticsSettings) -> list[tuple[str, str, bool, Any]]:
    excluded = set(settings.default_excluded_subfeatures)
    raw_excluded = cfg_get(config, f"{settings.config_key}.excluded_subfeatures", [])
    if isinstance(raw_excluded, str):
        excluded.update(item.strip() for item in raw_excluded.split(",") if item.strip())
    elif isinstance(raw_excluded, (list, tuple)):
        excluded.update(str(item).strip() for item in raw_excluded if str(item).strip())
    return [spec for spec in SUBFEATURE_SPECS if spec[0] not in excluded]


def parse_run_args(settings: SignalDiagnosticsSettings) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=settings.description)
    parser.add_argument("--config", type=Path, default=settings.default_config)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def parse_validate_args(settings: SignalDiagnosticsSettings) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=settings.validate_description)
    parser.add_argument("--config", type=Path, default=settings.default_config)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    return parser.parse_args()


def run_signal_diagnostics(settings: SignalDiagnosticsSettings) -> int:
    configure_utc_logging()
    args = parse_run_args(settings)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{settings.config_key}.output_dir", settings.default_output_dir),
        base_dir=base_dir,
    )
    start = parse_date(args.start) or parse_date(cfg_get(config, f"{settings.config_key}.start_date", "2011-01-01")) or date(2011, 1, 1)
    end = parse_date(args.end) or date.today()
    step = int(cfg_get(config, f"{settings.config_key}.step_trading_days", 21))
    horizons = [int(value) for value in cfg_get(config, f"{settings.config_key}.horizons_trading_days", [21, 63])]
    newey_west_lags_by_horizon = {horizon: newey_west_lags_for_horizon(horizon, step) for horizon in horizons}
    bench_ticker = normalize_ticker(cfg_get(config, f"{settings.config_key}.benchmark_ticker", settings.default_benchmark_ticker))
    beta_lookback = int(cfg_get(config, f"{settings.config_key}.beta_lookback_days", 252))
    min_cross_section = int(cfg_get(config, f"{settings.config_key}.min_cross_section", 30))
    min_t = float(cfg_get(config, f"{settings.config_key}.min_abs_t_stat_for_keep", 1.5))
    min_forward_return = float(cfg_get(config, f"{settings.config_key}.min_forward_return", -0.95))
    max_forward_return = float(cfg_get(config, f"{settings.config_key}.max_forward_return", 3.00))
    include_inactive = bool(cfg_get(config, f"{settings.config_key}.include_inactive_tickers", True))
    model_family = str(cfg_get(config, f"{settings.config_key}.model_family", settings.default_model_family))
    calibrated_config_key = str(cfg_get(config, f"{settings.config_key}.calibrated_config_key", settings.default_calibrated_config_key))
    fin_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    mp_source = str(cfg_get(config, "positioning_import.market_positioning_source_id", "market_positioning_upstream"))
    direct_source = str(cfg_get(config, "positioning_import.direct_ownership_source_id", "sec_ownership_direct"))
    upstream_source = str(cfg_get(config, "positioning_import.form4_source_id", "sec_insider_upstream"))
    short_change_days = int(cfg_get(config, "positioning_import.lookback_days.short_change", 92))
    price_sources = configured_price_source_ids(config, settings)
    subfeature_specs = configured_subfeature_specs(config, settings)
    excluded_raw = {spec[0] for spec in SUBFEATURE_SPECS if spec not in subfeature_specs}
    component_specs = component_specs_from_config(config, calibrated_config_key, excluded_raw)

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
        LOGGER.info("Universe tickers=%d db=%s model_family=%s include_inactive=%s", len(universe), db_path, model_family, include_inactive)
        prices = load_prices(conn, price_sources, universe + [bench_ticker])
        bench = prices.get(bench_ticker, PriceSeries())
        if not bench.dates:
            LOGGER.error("No benchmark prices for %s; cannot build diagnostics.", bench_ticker)
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
    panel_indices = [idx for idx in panel_indices if bench.dates[idx] <= end]
    LOGGER.info(
        "Panel dates=%d from %s to %s step=%d horizons=%s benchmark=%s",
        len(panel_indices),
        bench.dates[panel_indices[0]] if panel_indices else "-",
        bench.dates[panel_indices[-1]] if panel_indices else "-",
        step,
        horizons,
        bench_ticker,
    )

    sub_ic: dict[tuple[str, int], list[float]] = {}
    sub_spread: dict[tuple[str, int], list[float]] = {}
    sub_cov: dict[tuple[str, int], list[int]] = {}
    comp_ic: dict[tuple[str, int], list[float]] = {}
    comp_spread: dict[tuple[str, int], list[float]] = {}
    comp_cov: dict[tuple[str, int], list[int]] = {}
    panel_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []

    for panel_idx in panel_indices:
        asof = bench.dates[panel_idx]
        asof_iso = asof.isoformat()
        rows: list[dict[str, Any]] = []
        fwd_raw: dict[int, dict[str, float]] = {horizon: {} for horizon in horizons}
        fwd_bench: dict[int, dict[str, float]] = {horizon: {} for horizon in horizons}
        fwd_resid: dict[int, dict[str, float]] = {horizon: {} for horizon in horizons}
        active_members = [ticker for ticker in universe if is_member_on_date(membership_by_ticker.get(ticker), asof)]
        for ticker in active_members:
            series = prices.get(ticker)
            if series is None or not series.dates:
                continue
            feats = market_subfeatures(series, asof, bench)
            if not feats:
                continue
            feats.update(financial_subfeatures(fin_rows.get(ticker, []), asof_iso))
            reprice_valuation(feats, series, asof)
            feats.update(positioning_subfeatures(ticker, asof_iso, form4=form4, inst=inst, short=short, borrow=borrow))
            apply_signal_birthdates(feats, signal_birthdates, asof)
            feats["ticker"] = ticker
            feats["calibration_cohort_id"] = cohort_by_ticker.get(ticker, "")
            idx = series.idx_at(asof)
            beta = trailing_beta(series, bench, asof, beta_lookback)
            feats["beta_to_benchmark"] = beta
            usable = False
            for horizon in horizons:
                target_date = bench.dates[panel_idx + horizon]
                target_idx = series.idx_at(target_date)
                fwd = series.ret_between(idx, target_idx)
                bench_fwd = bench.ret_between(panel_idx, panel_idx + horizon)
                if fwd is None or bench_fwd is None:
                    continue
                if fwd < min_forward_return or fwd > max_forward_return:
                    continue
                fwd_raw[horizon][ticker] = fwd
                fwd_bench[horizon][ticker] = bench_fwd
                fwd_resid[horizon][ticker] = fwd - beta * bench_fwd
                usable = True
            if usable:
                rows.append(feats)

        coverage_rows.append(
            {
                "asof_date": asof_iso,
                "active_members": len(active_members),
                "scored_rows": len(rows),
                **{f"fwd_usable_{horizon}d": len(fwd_resid[horizon]) for horizon in horizons},
                "min_cross_section_pass": int(len(rows) >= min_cross_section),
            }
        )
        if len(rows) < min_cross_section:
            continue

        for row in rows:
            panel_row = {
                "asof_date": asof_iso,
                "ticker": row["ticker"],
                "calibration_cohort_id": row.get("calibration_cohort_id", ""),
                "beta_to_benchmark": row.get("beta_to_benchmark"),
            }
            for raw_key, _score_key, _higher_is_better, _valid in subfeature_specs:
                panel_row[raw_key] = row.get(raw_key)
            for horizon in horizons:
                ticker = str(row["ticker"])
                panel_row[f"fwd_return_{horizon}d"] = fwd_raw[horizon].get(ticker)
                panel_row[f"benchmark_return_{horizon}d"] = fwd_bench[horizon].get(ticker)
                panel_row[f"fwd_resid_{horizon}d"] = fwd_resid[horizon].get(ticker)
            panel_rows.append(panel_row)

        for raw_key, _score_key, higher_is_better, valid in subfeature_specs:
            for horizon in horizons:
                pairs = []
                for row in rows:
                    value = safe_float(row.get(raw_key))
                    resid = fwd_resid[horizon].get(str(row["ticker"]))
                    if value is None or resid is None:
                        continue
                    if valid is not None and not valid(value):
                        continue
                    pairs.append((value if higher_is_better else -value, resid))
                if len(pairs) < min_cross_section:
                    continue
                ic = spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
                if ic is None:
                    continue
                sub_ic.setdefault((raw_key, horizon), []).append(ic)
                sub_cov.setdefault((raw_key, horizon), []).append(len(pairs))
                spread = quintile_spread([pair[0] for pair in pairs], [pair[1] for pair in pairs])
                if spread is not None:
                    sub_spread.setdefault((raw_key, horizon), []).append(spread)

        for raw_key, score_key, higher_is_better, valid in subfeature_specs:
            scores = percentile_scores(rows, raw_key, higher_is_better=higher_is_better, valid=valid)
            for row in rows:
                row[score_key] = scores.get(str(row["ticker"]))
        for component, specs in component_specs.items():
            for horizon in horizons:
                pairs = []
                for row in rows:
                    resid = fwd_resid[horizon].get(str(row["ticker"]))
                    if resid is None:
                        continue
                    score, quality, _available, _missing, _detail = weighted_available_score(row, specs, neutral_score=50.0)
                    if quality <= 0:
                        continue
                    pairs.append((score, resid))
                if len(pairs) < min_cross_section:
                    continue
                ic = spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
                if ic is None:
                    continue
                comp_ic.setdefault((component, horizon), []).append(ic)
                comp_cov.setdefault((component, horizon), []).append(len(pairs))
                spread = quintile_spread([pair[0] for pair in pairs], [pair[1] for pair in pairs])
                if spread is not None:
                    comp_spread.setdefault((component, horizon), []).append(spread)

    score_to_component = {
        score: component
        for component, specs in component_specs.items()
        for score, _weight in specs
    }
    sub_rows: list[dict[str, Any]] = []
    for (raw_key, horizon), series_values in sorted(sub_ic.items()):
        group = score_to_component.get(f"{raw_key}_score", "")
        row = summarize(
            raw_key,
            group,
            series_values,
            sub_spread.get((raw_key, horizon), []),
            sub_cov.get((raw_key, horizon), []),
            min_t,
            newey_west_lags=newey_west_lags_by_horizon.get(horizon, 0),
        )
        row["horizon_days"] = horizon
        sub_rows.append(row)

    comp_rows: list[dict[str, Any]] = []
    for (component, horizon), series_values in sorted(comp_ic.items()):
        row = summarize(
            component,
            "component",
            series_values,
            comp_spread.get((component, horizon), []),
            comp_cov.get((component, horizon), []),
            min_t,
            newey_west_lags=newey_west_lags_by_horizon.get(horizon, 0),
        )
        row["horizon_days"] = horizon
        comp_rows.append(row)

    primary_h = horizons[0]
    weight_rows: list[dict[str, Any]] = []
    for component, specs in component_specs.items():
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

    summary = {
        "model_family": model_family,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "benchmark_ticker": bench_ticker,
        "price_source_ids": price_sources,
        "panel_dates": len(panel_indices),
        "usable_panel_rows": len(panel_rows),
        "subfeature_ic_rows": len(sub_rows),
        "component_ic_rows": len(comp_rows),
        "horizons_trading_days": horizons,
        "newey_west_lags_by_horizon": newey_west_lags_by_horizon,
        "excluded_subfeatures": sorted(excluded_raw),
        "include_inactive_tickers": include_inactive,
    }

    write_csv(output_dir / "signal_panel.csv", panel_rows)
    write_csv(output_dir / "panel_coverage.csv", coverage_rows)
    write_csv(output_dir / "subfeature_ic.csv", sub_rows)
    write_csv(output_dir / "component_ic.csv", comp_rows)
    write_csv(output_dir / "suggested_weights.csv", weight_rows)
    write_csv(output_dir / "signal_birthdates.csv", signal_birthdate_rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "stage8a_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    for row in comp_rows:
        LOGGER.info(
            "component=%s h=%s mean_ic=%s t=%s hit=%s cov=%s",
            row["signal"],
            row["horizon_days"],
            row["mean_ic"],
            row["ic_t_stat"],
            row["hit_rate"],
            row["avg_coverage"],
        )
    weak = [row for row in sub_rows if row["horizon_days"] == primary_h and not row["keep_candidate"]]
    LOGGER.info("Wrote diagnostics to %s", output_dir)
    LOGGER.info(
        "%d/%d subfeatures lack significant positive IC at h=%d.",
        len(weak),
        len([row for row in sub_rows if row["horizon_days"] == primary_h]),
        primary_h,
    )
    return 0 if sub_rows and comp_rows and panel_rows else 1


def validate_signal_diagnostics_outputs(settings: SignalDiagnosticsSettings) -> int:
    configure_utc_logging()
    args = parse_validate_args(settings)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{settings.config_key}.output_dir", settings.default_output_dir),
        base_dir=base_dir,
    )
    start = parse_date(args.start) or parse_date(cfg_get(config, f"{settings.config_key}.start_date", "2011-01-01")) or date(2011, 1, 1)
    end = parse_date(args.end) or date.today()
    min_cross_section = int(cfg_get(config, f"{settings.config_key}.min_cross_section", 30))
    horizons = [int(value) for value in cfg_get(config, f"{settings.config_key}.horizons_trading_days", [21, 63])]

    errors: list[str] = []
    required_files = [
        "signal_panel.csv",
        "panel_coverage.csv",
        "subfeature_ic.csv",
        "component_ic.csv",
        "suggested_weights.csv",
        "signal_birthdates.csv",
        "stage8a_summary.json",
    ]
    for filename in required_files:
        if not (output_dir / filename).exists():
            errors.append(f"Missing required Stage 8A output: {filename}")

    panel_rows = read_csv_rows(output_dir / "signal_panel.csv")
    coverage_rows = read_csv_rows(output_dir / "panel_coverage.csv")
    sub_rows = read_csv_rows(output_dir / "subfeature_ic.csv")
    comp_rows = read_csv_rows(output_dir / "component_ic.csv")
    birth_rows = read_csv_rows(output_dir / "signal_birthdates.csv")

    if not panel_rows:
        errors.append("signal_panel.csv has no rows")
    if not coverage_rows:
        errors.append("panel_coverage.csv has no rows")
    if not sub_rows:
        errors.append("subfeature_ic.csv has no rows")
    if not comp_rows:
        errors.append("component_ic.csv has no rows")
    if not birth_rows:
        errors.append("signal_birthdates.csv has no rows")

    if coverage_rows:
        dates = [parse_date(row.get("asof_date")) for row in coverage_rows]
        clean_dates = [value for value in dates if value is not None]
        if clean_dates:
            if min(clean_dates) > start + timedelta(days=370):
                errors.append(f"Stage 8A panel starts too late: {min(clean_dates)} versus requested {start}")
            if max(clean_dates) < end - timedelta(days=140):
                errors.append(f"Stage 8A panel ends too early: {max(clean_dates)} versus requested {end}")
        pass_rows = [row for row in coverage_rows if int(row.get("min_cross_section_pass") or 0) == 1]
        if len(pass_rows) < 36:
            errors.append(f"Too few panel dates pass min cross-section: {len(pass_rows)}")
        worst_scored = min((int(float(row.get("scored_rows") or 0)) for row in coverage_rows), default=0)
        if worst_scored < min_cross_section:
            LOGGER.warning("Some early/late dates have scored_rows below min_cross_section: min=%s", worst_scored)

    for horizon in horizons:
        if not any(str(row.get("horizon_days")) == str(horizon) for row in sub_rows):
            errors.append(f"No subfeature IC rows for horizon {horizon}")
        if not any(str(row.get("horizon_days")) == str(horizon) for row in comp_rows):
            errors.append(f"No component IC rows for horizon {horizon}")
    for row in sub_rows + comp_rows:
        if "raw_ic_t_stat" not in row or "newey_west_lags" not in row:
            errors.append("IC output is missing Newey-West/raw t-stat columns")
            break

    summary_path = output_dir / "stage8a_summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if int(summary.get("usable_panel_rows") or 0) < min_cross_section * 36:
                errors.append(f"Usable panel rows too low: {summary.get('usable_panel_rows')}")
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid stage8a_summary.json: {exc}")

    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info(
        "Stage 8A diagnostics validation passed: output_dir=%s panel_rows=%d subfeature_rows=%d component_rows=%d",
        output_dir,
        len(panel_rows),
        len(sub_rows),
        len(comp_rows),
    )
    return 0
