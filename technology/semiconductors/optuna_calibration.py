from __future__ import annotations

# ruff: noqa: E402

import argparse
import csv
import hashlib
import importlib.util
import json
import logging
import math
import subprocess
import sys
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path
from technology.core.db import connect, init_db
from technology.core.logging_utils import configure_utc_logging
from technology.core.scoring_features import SUBFEATURE_SPECS, percentile_scores, safe_float, weighted_available_score
from technology.core.source_registry import load_source_registry, upsert_source_registry
from technology.core.text_norm import normalize_ticker
from technology.semiconductors.calibrated_scoring import (
    component_weight_specs as stage7_component_weight_specs,
)
from technology.semiconductors.calibrated_scoring import (
    subfeature_weight_specs as stage7_subfeature_weight_specs,
)


LOGGER = logging.getLogger("semiconductor_optuna_calibration")
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "semiconductor_optuna_calibration"
DIAGNOSTICS_SCRIPT = PACKAGE_ROOT / "semiconductors" / "scripts" / "07_run_semiconductor_signal_diagnostics.py"
RUN_TYPE = "run_semiconductor_optuna_calibration"


@dataclass(frozen=True)
class Candidate:
    component_weights: dict[str, float]
    subfeature_specs: dict[str, list[tuple[str, float]]]


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--n-trials", type=int, default=None)
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument(
        "--allow-post-lock-panel",
        action="store_true",
        help=(
            "Research override: extend the calibration panel past the configured "
            "calibration_train_end_date. Outputs are stamped post_lock_data_included=true "
            "and must never be promoted."
        ),
    )
    return parser.parse_args()


_DIAGNOSTICS_MODULE: ModuleType | None = None


def load_diagnostics_module() -> ModuleType:
    global _DIAGNOSTICS_MODULE
    if _DIAGNOSTICS_MODULE is not None:
        return _DIAGNOSTICS_MODULE
    spec = importlib.util.spec_from_file_location("semiconductor_signal_diagnostics_module", DIAGNOSTICS_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {DIAGNOSTICS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _DIAGNOSTICS_MODULE = module
    return module


def load_registry_into_db(conn: Any, config: dict[str, Any], base_dir: Path) -> None:
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    upsert_source_registry(conn, load_source_registry(registry_path))


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def calibration_train_end(config: dict[str, Any]) -> date | None:
    """Configured calibration train-end date for this family, if declared.

    Calibration runs cap their panel here by default so that no post-lock
    price bars leak into weight selection after the 2026-06-15 model lock.
    """
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    return parse_date(cfg_get(config, f"oos_calibration_standards.families.{model_family}.calibration_train_end_date", ""))


def normalize_weights(raw: dict[str, float]) -> dict[str, float]:
    clean = {key: max(0.0, float(value)) for key, value in raw.items()}
    total = sum(clean.values())
    if total <= 0:
        return {key: 0.0 for key in clean}
    return {key: value / total for key, value in clean.items()}


def component_bounds(config: dict[str, Any]) -> dict[str, tuple[float, float]]:
    raw = cfg_get(config, f"{CONFIG_KEY}.component_bounds", {}) or {}
    defaults = {
        "valuation": (0.20, 0.45),
        "quality": (0.15, 0.40),
        "risk_control": (0.15, 0.40),
        "positioning": (0.00, 0.18),
        "market_behavior": (0.00, 0.18),
        # Growth stays pinned at zero: the Stage 7 validator rejects any growth
        # weight in v1, so searching it would only produce unadoptable candidates.
        "growth": (0.00, 0.00),
    }
    out: dict[str, tuple[float, float]] = {}
    for component, default in defaults.items():
        value = raw.get(component) if isinstance(raw, dict) else None
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            lo = safe_float(value[0])
            hi = safe_float(value[1])
            out[component] = (float(lo if lo is not None else default[0]), float(hi if hi is not None else default[1]))
        else:
            out[component] = default
    return out


def enforce_component_bounds(weights: dict[str, float], bounds: dict[str, tuple[float, float]]) -> dict[str, float]:
    adjusted = dict(weights)
    # Iteratively cap high weights and lift low weights, then renormalize the
    # remaining free weights. The in-loop scaling already leaves the weights
    # summing to 1.0, so there is deliberately no global renormalize afterwards
    # (it could push clamped weights back outside their bounds).
    converged = False
    for _ in range(32):
        fixed_total = 0.0
        free_keys: list[str] = []
        changed = False
        for key, value in list(adjusted.items()):
            lo, hi = bounds.get(key, (0.0, 1.0))
            if value < lo:
                adjusted[key] = lo
                fixed_total += lo
                changed = True
            elif value > hi:
                adjusted[key] = hi
                fixed_total += hi
                changed = True
            else:
                free_keys.append(key)
        free_total = sum(adjusted[key] for key in free_keys)
        remaining = max(0.0, 1.0 - fixed_total)
        if free_keys and free_total > 0:
            for key in free_keys:
                adjusted[key] = adjusted[key] / free_total * remaining
        elif free_keys:
            equal = remaining / len(free_keys)
            for key in free_keys:
                adjusted[key] = equal
        if not changed:
            converged = True
            break
    if not converged:
        LOGGER.warning("Component bound projection did not converge; bounds may be infeasible: weights=%s bounds=%s", adjusted, bounds)
    return adjusted


def candidate_subfeature_keys(config: dict[str, Any]) -> dict[str, list[str]]:
    raw = cfg_get(config, f"{CONFIG_KEY}.subfeature_candidates", {}) or {}
    out: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for component, values in raw.items():
            if isinstance(values, (list, tuple)):
                out[str(component)] = [str(value) for value in values]
    if out:
        return out
    return {component: [score_key for score_key, weight in specs if weight > 0] for component, specs in stage7_subfeature_weight_specs(config).items()}


def stage7_candidate(config: dict[str, Any]) -> Candidate:
    return Candidate(
        component_weights=stage7_component_weight_specs(config),
        subfeature_specs=stage7_subfeature_weight_specs(config),
    )


def sample_candidate(trial: Any, config: dict[str, Any], bounds: dict[str, tuple[float, float]]) -> Candidate:
    # Components pinned to zero by their bounds are not sampled at all: it keeps
    # the search space small and avoids wasting TPE dimensions on dead weights.
    raw_weights = {
        component: trial.suggest_float(f"component__{component}", lo, hi) if hi > 0 else 0.0
        for component, (lo, hi) in bounds.items()
    }
    component_weights = enforce_component_bounds(normalize_weights(raw_weights), bounds)
    specs: dict[str, list[tuple[str, float]]] = {}
    for component, keys in candidate_subfeature_keys(config).items():
        if not keys or bounds.get(component, (0.0, 1.0))[1] <= 0:
            specs[component] = []
            continue
        raw = {
            key: trial.suggest_float(f"subfeature__{component}__{key}", 0.0, 1.0)
            for key in keys
        }
        weights = normalize_weights(raw)
        specs[component] = [(key, weight) for key, weight in weights.items() if weight > 0]
    return Candidate(component_weights=component_weights, subfeature_specs=specs)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": 0.0, "std": 0.0, "hit_rate": 0.0}
    mean = sum(values) / len(values)
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1)) if len(values) >= 2 else 0.0
    return {
        "n": len(values),
        "mean": mean,
        "std": std,
        "hit_rate": sum(1 for value in values if value > 0) / len(values),
    }


def load_wsts_regimes(conn: Any, lag_days: int) -> list[tuple[str, str]]:
    """[(available_date_iso, 'up'|'down')] from worldwide 3MMA billings YoY, sorted.

    `lag_days` is measured from the month START: WSTS publishes each month's
    billings roughly 5-7 weeks after the month ENDS, so from the month start
    the data is only observable ~80 days later (30-31 days of month plus the
    publication lag). Using a shorter lag would let the regime label see
    billings before they were public. Months without a 12-month-prior value
    are skipped.
    """
    rows = conn.execute(
        """
        SELECT period_month, value_millions_usd
        FROM fact_semiconductor_wsts_billings
        WHERE dataset_type = '3mma' AND region = 'Worldwide' AND value_millions_usd IS NOT NULL
        ORDER BY period_month
        """
    ).fetchall()
    by_month = {str(row["period_month"])[:7]: float(row["value_millions_usd"]) for row in rows}
    out: list[tuple[str, str]] = []
    for month_key, value in sorted(by_month.items()):
        year, month = int(month_key[:4]), int(month_key[5:7])
        prior = by_month.get(f"{year - 1:04d}-{month:02d}")
        if prior is None or prior <= 0:
            continue
        month_start = date(year, month, 1)
        available = date.fromordinal(month_start.toordinal() + lag_days)
        out.append((available.isoformat(), "up" if value / prior - 1.0 > 0 else "down"))
    return out


def regime_at(regimes: list[tuple[str, str]], asof_iso: str) -> str:
    label = "unknown"
    for available, state in regimes:
        if available <= asof_iso:
            label = state
        else:
            break
    return label


def load_membership_intervals(
    conn: Any,
    *,
    model_family: str,
    include_inactive: bool,
) -> tuple[dict[str, list[tuple[date, date | None]]], dict[str, str], dict[str, int]]:
    rows = conn.execute(
        """
        SELECT m.ticker,
               m.start_date,
               m.end_date,
               m.is_current_member,
               m.point_in_time_flag,
               COALESCE(t.calibration_cohort_id, '') AS cohort
        FROM dim_universe_membership m
        JOIN dim_technology_taxonomy t
          ON t.ticker = m.ticker
         AND t.model_family = m.model_family
        WHERE m.model_family = ?
          AND m.membership_status IN ('active', 'historical', 'inactive', 'review')
          AND (? = 1 OR m.is_current_member = 1)
        ORDER BY m.ticker, m.start_date
        """,
        (model_family, 1 if include_inactive else 0),
    ).fetchall()
    membership: dict[str, list[tuple[date, date | None]]] = {}
    cohort_by_ticker: dict[str, str] = {}
    pit_tickers: set[str] = set()
    current_tickers: set[str] = set()
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if not ticker:
            continue
        start = parse_date(row["start_date"]) or date(1900, 1, 1)
        end = parse_date(row["end_date"])
        membership.setdefault(ticker, []).append((start, end))
        cohort_by_ticker[ticker] = str(row["cohort"] or "")
        if int(row["point_in_time_flag"] or 0) == 1:
            pit_tickers.add(ticker)
        if int(row["is_current_member"] or 0) == 1:
            current_tickers.add(ticker)
    if membership:
        stats = {
            "membership_rows": len(rows),
            "membership_tickers": len(membership),
            "current_membership_tickers": len(current_tickers),
            "point_in_time_membership_tickers": len(pit_tickers),
        }
        return membership, cohort_by_ticker, stats

    active_clause = "" if include_inactive else "WHERE c.is_active = 1"
    fallback_rows = conn.execute(
        f"""
        SELECT c.ticker, COALESCE(t.calibration_cohort_id, '') AS cohort
        FROM dim_company c
        JOIN dim_technology_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        {active_clause}
        ORDER BY c.ticker
        """,
        (model_family,),
    ).fetchall()
    for row in fallback_rows:
        ticker = normalize_ticker(row["ticker"])
        if not ticker:
            continue
        membership[ticker] = [(date(1900, 1, 1), None)]
        cohort_by_ticker[ticker] = str(row["cohort"] or "")
    return membership, cohort_by_ticker, {
        "membership_rows": 0,
        "membership_tickers": len(membership),
        "current_membership_tickers": len(membership),
        "point_in_time_membership_tickers": 0,
    }


def is_member_on_date(intervals: list[tuple[date, date | None]] | None, asof: date) -> bool:
    if not intervals:
        return False
    for start, end in intervals:
        if start <= asof and (end is None or asof <= end):
            return True
    return False


def top_quantile_rows(scored_rows: list[dict[str, Any]], quantile: float) -> list[dict[str, Any]]:
    ordered = sorted(scored_rows, key=lambda row: (-float(row["score"]), str(row["ticker"])))
    size = max(5, int(math.ceil(len(ordered) * quantile)))
    return ordered[:size]


def score_row(row: dict[str, Any], candidate: Candidate, *, neutral_score: float) -> tuple[float, float, dict[str, float], dict[str, float]]:
    component_scores: dict[str, float] = {}
    component_quality: dict[str, float] = {}
    available_weight = 0.0
    weighted_score = 0.0
    weighted_quality = 0.0
    positive_weight = sum(weight for weight in candidate.component_weights.values() if weight > 0)
    for component, weight in candidate.component_weights.items():
        specs = candidate.subfeature_specs.get(component, [])
        if not specs:
            score = neutral_score
            quality = 0.0
        else:
            score, quality, _available, _missing, _missing_detail = weighted_available_score(row, specs, neutral_score=neutral_score)
        component_scores[component] = score
        component_quality[component] = quality
        if weight > 0 and quality > 0:
            available_weight += weight
            weighted_score += score * weight
            weighted_quality += quality * weight
    if available_weight <= 0:
        return neutral_score, 0.0, component_scores, component_quality
    return weighted_score / available_weight, weighted_quality / positive_weight if positive_weight > 0 else 0.0, component_scores, component_quality


def build_panel(
    config: dict[str, Any],
    db_path: Path,
    *,
    panel_end_date: date | None = None,
) -> tuple[list[dict[str, Any]], list[date], list[int]]:
    diag = load_diagnostics_module()
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    price_sources = diag.research_price_source_ids(config)
    fin_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    mp_source = str(cfg_get(config, "positioning_import.market_positioning_source_id", "market_positioning_upstream"))
    direct_source = str(cfg_get(config, "positioning_import.direct_ownership_source_id", "sec_ownership_direct"))
    upstream_source = str(cfg_get(config, "positioning_import.form4_source_id", "sec_insider_upstream"))
    start = diag.parse_date(cfg_get(config, "semiconductor_signal_diagnostics.start_date", "2018-01-01")) or date(2018, 1, 1)
    step = int(cfg_get(config, "semiconductor_signal_diagnostics.step_trading_days", 21))
    horizons = [int(value) for value in cfg_get(config, "semiconductor_signal_diagnostics.horizons_trading_days", [21, 63])]
    benchmark = normalize_ticker(cfg_get(config, "semiconductor_signal_diagnostics.benchmark_ticker", "SMH"))
    beta_lookback = int(cfg_get(config, "semiconductor_signal_diagnostics.beta_lookback_days", 252))
    min_cross_section = int(cfg_get(config, "semiconductor_signal_diagnostics.min_cross_section", 30))
    short_change_days = int(cfg_get(config, "positioning_import.lookback_days.short_change", 92))

    include_inactive = bool(cfg_get(config, f"{CONFIG_KEY}.include_inactive_tickers", True))
    regime_lag_days = int(cfg_get(config, f"{CONFIG_KEY}.wsts_regime_lag_days", 80))

    with diag.ro_connect(db_path) as conn:
        membership_by_ticker, cohort_by_ticker, membership_stats = load_membership_intervals(
            conn,
            model_family=model_family,
            include_inactive=include_inactive,
        )
        universe = sorted(membership_by_ticker)
        LOGGER.info(
            "Loaded Stage 8 membership intervals: tickers=%d rows=%d current_tickers=%d pit_tickers=%d include_inactive=%s",
            membership_stats["membership_tickers"],
            membership_stats["membership_rows"],
            membership_stats["current_membership_tickers"],
            membership_stats["point_in_time_membership_tickers"],
            include_inactive,
        )
        if membership_stats["point_in_time_membership_tickers"] == 0:
            LOGGER.warning(
                "Stage 8 membership is seeded from the current source-of-truth universe only; "
                "historical/delisted semiconductor backfill is still required to remove survivorship bias."
            )
        prices = diag.load_prices(conn, price_sources, universe + [benchmark, "SOXX"])
        bench = prices.get(benchmark, diag.PriceSeries())
        soxx = prices.get("SOXX", diag.PriceSeries())
        if not bench.dates:
            raise RuntimeError(f"No benchmark price series found for {benchmark}")
        fin_rows = diag.load_financial_rows(conn, fin_source, model_family)
        form4 = diag.load_form4(conn, direct_source, upstream_source)
        inst = diag.load_13f(conn, mp_source)
        short = diag.load_short(conn, mp_source)
        borrow = diag.load_borrow(conn, mp_source)
        signal_birthdates, _signal_birthdate_rows = diag.load_positioning_signal_birthdates(
            conn,
            direct_source=direct_source,
            upstream_source=upstream_source,
            market_positioning_source=mp_source,
            short_change_days=short_change_days,
        )
        regimes = load_wsts_regimes(conn, regime_lag_days)
        wsts_cycle = diag.load_wsts_cycle_series(conn, regime_lag_days)

    start_idx = bisect_right(bench.dates, start)
    max_h = max(horizons)
    end_limit = len(bench.dates) - max_h
    if panel_end_date is not None:
        # Post-lock leakage guard: every forward-return target
        # (panel_date + max_h bars in the benchmark calendar) must land on or
        # before the calibration train-end date, so the last usable panel
        # index is capped accordingly.
        end_limit = min(end_limit, bisect_right(bench.dates, panel_end_date) - max_h)
    panel_indices = list(range(max(start_idx, beta_lookback + 8), end_limit, step))
    panel_dates = [bench.dates[idx] for idx in panel_indices]
    panel: list[dict[str, Any]] = []
    for panel_idx in panel_indices:
        asof = bench.dates[panel_idx]
        asof_iso = asof.isoformat()
        rows: list[dict[str, Any]] = []
        returns_by_ticker: dict[str, dict[int, dict[str, float]]] = {}
        members = [ticker for ticker in universe if is_member_on_date(membership_by_ticker.get(ticker), asof)]
        exposures = diag.cycle_exposure_signals(members, prices, wsts_cycle, asof, cohort_by_ticker)
        for ticker in members:
            series = prices.get(ticker)
            if series is None or not series.dates:
                continue
            feats = diag.market_subfeatures(series, asof, soxx)
            if not feats:
                continue
            feats.update(diag.financial_subfeatures(fin_rows.get(ticker, []), asof_iso))
            diag.reprice_valuation(feats, series, asof)
            feats.update(diag.positioning_subfeatures(ticker, asof_iso, form4=form4, inst=inst, short=short, borrow=borrow))
            diag.apply_signal_birthdates(feats, signal_birthdates, asof)
            feats["wsts_cycle_exposure"] = exposures.get(ticker)
            feats["ticker"] = ticker
            feats["asof_date"] = asof
            feats["cohort"] = cohort_by_ticker.get(ticker, "")
            feats["regime"] = regime_at(regimes, asof_iso)
            idx = series.idx_at(asof)
            beta = diag.trailing_beta(series, bench, asof, beta_lookback)
            feats["beta_to_benchmark"] = beta
            ticker_returns: dict[int, dict[str, float]] = {}
            for horizon in horizons:
                target_date = bench.dates[panel_idx + horizon]
                target_idx = series.idx_at(target_date)
                fwd = series.ret_between(idx, target_idx)
                bench_fwd = bench.ret_between(panel_idx, panel_idx + horizon)
                if fwd is None or bench_fwd is None:
                    continue
                ticker_returns[horizon] = {
                    "fwd": fwd,
                    "bench": bench_fwd,
                    "resid": fwd - beta * bench_fwd,
                }
            if ticker_returns:
                rows.append(feats)
                returns_by_ticker[ticker] = ticker_returns
        if len(rows) < min_cross_section:
            continue
        for raw_key, score_key, higher_is_better, valid in SUBFEATURE_SPECS:
            scores = percentile_scores(rows, raw_key, higher_is_better=higher_is_better, valid=valid)
            for row in rows:
                row[score_key] = scores.get(str(row["ticker"]))
        for row in rows:
            for horizon, values in returns_by_ticker.get(str(row["ticker"]), {}).items():
                row[f"fwd_{horizon}"] = values["fwd"]
                row[f"bench_{horizon}"] = values["bench"]
                row[f"resid_{horizon}"] = values["resid"]
            panel.append(row)
    panel_dates = sorted({row["asof_date"] for row in panel})
    LOGGER.info("Built optimization panel: rows=%d dates=%d horizons=%s", len(panel), len(panel_dates), horizons)
    return panel, panel_dates, horizons


def evaluate_candidate(
    panel: list[dict[str, Any]],
    dates: list[date],
    horizons: list[int],
    candidate: Candidate,
    *,
    neutral_score: float,
    top_quantile: float,
    max_turnover: float,
    max_top_cohort_share: float,
    min_cross_section: int = 30,
    stability_lambda: float = 0.10,
    complexity_penalty_per_subfeature: float = 0.0005,
    turnover_cost_bps: float = 20.0,
    emit_date_rows: bool = False,
) -> dict[str, Any]:
    diag = load_diagnostics_module()
    date_set = set(dates)
    rows_by_date: dict[date, list[dict[str, Any]]] = {}
    for row in panel:
        asof = row["asof_date"]
        if asof in date_set:
            rows_by_date.setdefault(asof, []).append(row)
    ic_values: dict[int, list[float]] = {horizon: [] for horizon in horizons}
    regime_ic_values: dict[tuple[int, str], list[float]] = {}
    spread_values: dict[int, list[float]] = {horizon: [] for horizon in horizons}
    coverage_values: dict[int, list[int]] = {horizon: [] for horizon in horizons}
    date_rows: list[dict[str, Any]] = []
    prev_top: set[str] | None = None
    turnovers: list[float] = []
    cohort_shares: list[float] = []
    score_ranges: list[float] = []

    for asof in sorted(rows_by_date):
        scored_rows: list[dict[str, Any]] = []
        for row in rows_by_date[asof]:
            score, quality, _component_scores, _component_quality = score_row(row, candidate, neutral_score=neutral_score)
            if quality <= 0:
                continue
            scored = dict(row)
            scored["score"] = score
            scored["score_quality"] = quality
            scored_rows.append(scored)
        if len(scored_rows) < min_cross_section:
            continue
        regime = str(rows_by_date[asof][0].get("regime") or "unknown")
        score_values = [float(row["score"]) for row in scored_rows]
        score_ranges.append(max(score_values) - min(score_values))
        top_rows = top_quantile_rows(scored_rows, top_quantile)
        top = {str(row["ticker"]) for row in top_rows}
        if prev_top is not None and top:
            turnovers.append(1.0 - len(top & prev_top) / len(top))
        prev_top = top
        cohort_counts: dict[str, int] = {}
        for row in top_rows:
            cohort_counts[str(row.get("cohort") or "")] = cohort_counts.get(str(row.get("cohort") or ""), 0) + 1
        if top_rows:
            cohort_shares.append(max(cohort_counts.values()) / len(top_rows))

        for horizon in horizons:
            pairs = [
                (float(row["score"]), float(row[f"resid_{horizon}"]))
                for row in scored_rows
                if row.get(f"resid_{horizon}") is not None
            ]
            if len(pairs) < min_cross_section:
                continue
            ic = diag.spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
            if ic is None:
                continue
            spread = diag.quintile_spread([pair[0] for pair in pairs], [pair[1] for pair in pairs])
            ic_values[horizon].append(ic)
            regime_ic_values.setdefault((horizon, regime), []).append(ic)
            coverage_values[horizon].append(len(pairs))
            if spread is not None:
                spread_values[horizon].append(spread)
            if emit_date_rows:
                date_rows.append(
                    {
                        "asof_date": asof.isoformat(),
                        "horizon_days": horizon,
                        "regime": regime,
                        "ic": round(ic, 6),
                        "coverage": len(pairs),
                        "q5_minus_q1_fwd_resid": round(spread, 6) if spread is not None else "",
                        "top_turnover": round(turnovers[-1], 6) if turnovers else "",
                        "top_max_cohort_share": round(cohort_shares[-1], 6) if cohort_shares else "",
                    }
                )

    avg_turnover = sum(turnovers) / len(turnovers) if turnovers else 0.0
    # Round-trip rebalance drag for the top quantile per panel step, in
    # forward-residual-return units.
    cost_drag = avg_turnover * 2.0 * turnover_cost_bps / 10000.0
    nonzero_subfeatures = sum(
        len(candidate.subfeature_specs.get(component, []))
        for component, weight in candidate.component_weights.items()
        if weight > 0
    )
    metrics: dict[str, Any] = {
        "avg_top_turnover": avg_turnover,
        "avg_top_cohort_share": sum(cohort_shares) / len(cohort_shares) if cohort_shares else 0.0,
        "avg_score_range": sum(score_ranges) / len(score_ranges) if score_ranges else 0.0,
        "cost_drag_per_step": cost_drag,
        "nonzero_subfeatures": nonzero_subfeatures,
        "date_rows": date_rows,
    }
    objective = 0.0
    for horizon in horizons:
        ic_stat = stats(ic_values[horizon])
        spread_stat = stats(spread_values[horizon])
        cov_stat = stats([float(value) for value in coverage_values[horizon]])
        metrics[f"mean_ic_{horizon}"] = ic_stat["mean"]
        metrics[f"std_ic_{horizon}"] = ic_stat["std"]
        metrics[f"hit_rate_{horizon}"] = ic_stat["hit_rate"]
        metrics[f"n_dates_{horizon}"] = ic_stat["n"]
        metrics[f"mean_spread_{horizon}"] = spread_stat["mean"]
        metrics[f"mean_spread_net_{horizon}"] = spread_stat["mean"] - cost_drag
        metrics[f"avg_coverage_{horizon}"] = cov_stat["mean"]
        for regime in ("up", "down", "unknown"):
            regime_stat = stats(regime_ic_values.get((horizon, regime), []))
            metrics[f"mean_ic_{horizon}_{regime}"] = regime_stat["mean"]
            metrics[f"n_dates_{horizon}_{regime}"] = regime_stat["n"]
    if horizons:
        primary = horizons[0]
        secondary = horizons[1] if len(horizons) > 1 else horizons[0]
        objective = (
            0.58 * float(metrics.get(f"mean_ic_{primary}", 0.0))
            + 0.32 * float(metrics.get(f"mean_ic_{secondary}", 0.0))
            + 0.04 * (float(metrics.get(f"hit_rate_{primary}", 0.0)) - 0.50)
            + 0.03 * (float(metrics.get(f"hit_rate_{secondary}", 0.0)) - 0.50)
            + 0.02 * float(metrics.get(f"mean_spread_net_{primary}", 0.0))
            + 0.01 * float(metrics.get(f"mean_spread_net_{secondary}", 0.0))
            # Stability: prefer candidates whose IC is consistent across dates,
            # not just high on average — mean-only objectives chase noise.
            - stability_lambda * float(metrics.get(f"std_ic_{primary}", 0.0))
            # Complexity: every weighted subfeature is a degree of freedom the
            # panel must justify.
            - complexity_penalty_per_subfeature * nonzero_subfeatures
        )
    turnover_penalty = max(0.0, float(metrics["avg_top_turnover"]) - max_turnover) * 0.08
    cohort_penalty = max(0.0, float(metrics["avg_top_cohort_share"]) - max_top_cohort_share) * 0.10
    metrics["constraint_penalty"] = turnover_penalty + cohort_penalty
    metrics["objective"] = objective - metrics["constraint_penalty"]
    return metrics


def split_dates(config: dict[str, Any], panel_dates: list[date], *, horizons: list[int], step: int) -> tuple[list[date], list[date]]:
    holdout_fraction = float(cfg_get(config, f"{CONFIG_KEY}.holdout_fraction", 0.30))
    # The embargo must always cover the longest forward-return window or the
    # last train dates leak into the holdout, regardless of the configured value.
    min_embargo = int(math.ceil(max(horizons) / max(1, step))) + 1
    embargo_dates = max(min_embargo, int(cfg_get(config, f"{CONFIG_KEY}.embargo_panel_dates", 4)))
    holdout_count = max(8, int(math.ceil(len(panel_dates) * holdout_fraction)))
    holdout_start = max(0, len(panel_dates) - holdout_count)
    train_end = max(0, holdout_start - embargo_dates)
    return panel_dates[:train_end], panel_dates[holdout_start:]


def contiguous_folds(panel_dates: list[date], folds: int) -> list[list[date]]:
    if folds <= 1 or len(panel_dates) < folds:
        return [list(panel_dates)]
    size = len(panel_dates) / folds
    return [panel_dates[int(i * size): int((i + 1) * size)] for i in range(folds)]


def subfeature_correlation_rows(panel: list[dict[str, Any]], min_obs: int = 20) -> list[dict[str, Any]]:
    """Average per-date cross-sectional Spearman correlation between subfeature scores.

    Surfaces collinear clusters (vol / drawdown / 52w-high) so weight reviews
    can see when components are triple-counting the same underlying signal.
    """
    diag = load_diagnostics_module()
    score_keys = [score_key for _, score_key, _, _ in SUBFEATURE_SPECS]
    rows_by_date: dict[date, list[dict[str, Any]]] = {}
    for row in panel:
        rows_by_date.setdefault(row["asof_date"], []).append(row)
    sums: dict[tuple[str, str], list[float]] = {}
    for rows in rows_by_date.values():
        for i, key_a in enumerate(score_keys):
            for key_b in score_keys[i + 1:]:
                pairs = [
                    (float(row[key_a]), float(row[key_b]))
                    for row in rows
                    if row.get(key_a) is not None and row.get(key_b) is not None
                ]
                if len(pairs) < min_obs:
                    continue
                corr = diag.spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if corr is not None:
                    sums.setdefault((key_a, key_b), []).append(corr)
    out = [
        {
            "subfeature_a": key_a,
            "subfeature_b": key_b,
            "avg_rank_correlation": round(sum(values) / len(values), 4),
            "n_dates": len(values),
        }
        for (key_a, key_b), values in sums.items()
        if values
    ]
    out.sort(key=lambda row: -abs(float(row["avg_rank_correlation"])))
    return out


def json_ready_weights(candidate: Candidate) -> dict[str, Any]:
    return {
        "component_weights": candidate.component_weights,
        "subfeature_weights": {
            component: {key: weight for key, weight in specs}
            for component, specs in candidate.subfeature_specs.items()
        },
    }


def flatten_metrics(prefix: str, metrics: dict[str, Any], horizons: list[int]) -> dict[str, Any]:
    out = {
        f"{prefix}_objective": metrics.get("objective"),
        f"{prefix}_avg_top_turnover": metrics.get("avg_top_turnover"),
        f"{prefix}_avg_top_cohort_share": metrics.get("avg_top_cohort_share"),
        f"{prefix}_avg_score_range": metrics.get("avg_score_range"),
        f"{prefix}_cost_drag_per_step": metrics.get("cost_drag_per_step"),
        f"{prefix}_nonzero_subfeatures": metrics.get("nonzero_subfeatures"),
        f"{prefix}_constraint_penalty": metrics.get("constraint_penalty"),
    }
    for horizon in horizons:
        for key in ("mean_ic", "std_ic", "hit_rate", "n_dates", "mean_spread", "mean_spread_net", "avg_coverage"):
            out[f"{prefix}_{key}_{horizon}"] = metrics.get(f"{key}_{horizon}")
        for regime in ("up", "down"):
            out[f"{prefix}_mean_ic_{horizon}_{regime}"] = metrics.get(f"mean_ic_{horizon}_{regime}")
            out[f"{prefix}_n_dates_{horizon}_{regime}"] = metrics.get(f"n_dates_{horizon}_{regime}")
    return out


def current_candidate_scores(
    config: dict[str, Any],
    db_path: Path,
    candidate: Candidate,
    *,
    neutral_score: float,
) -> list[dict[str, Any]]:
    baseline_source = str(cfg_get(config, f"{CONFIG_KEY}.baseline_feature_source_id", "semiconductor_scoring_contract"))
    stage7_source = str(cfg_get(config, f"{CONFIG_KEY}.stage7_source_id", "semiconductor_calibrated_score_v1"))
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM feature_scoring_input
            WHERE source_id = ?
              AND model_family = ?
              AND asof_date = (
                  SELECT MAX(asof_date)
                  FROM feature_scoring_input
                  WHERE source_id = ? AND model_family = ?
              )
            ORDER BY ticker
            """,
            (baseline_source, model_family, baseline_source, model_family),
        ).fetchall()
        stage7_rows = conn.execute(
            """
            SELECT ticker, final_rank, final_score
            FROM feature_scoring_model_output
            WHERE source_id = ?
              AND model_family = ?
              AND asof_date = (
                  SELECT MAX(asof_date)
                  FROM feature_scoring_model_output
                  WHERE source_id = ? AND model_family = ?
              )
            """,
            (stage7_source, model_family, stage7_source, model_family),
        ).fetchall()
    out_rows = [dict(row) for row in rows]
    for raw_key, score_key, higher_is_better, valid in SUBFEATURE_SPECS:
        scores = percentile_scores(out_rows, raw_key, higher_is_better=higher_is_better, valid=valid)
        for row in out_rows:
            row[score_key] = scores.get(str(row["ticker"]))
    stage7_by_ticker = {str(row["ticker"]): dict(row) for row in stage7_rows}
    overlay_weight = max(0.0, min(1.0, float(cfg_get(config, "semiconductor_calibrated_scoring.overlay_weight", 0.05))))
    scored: list[dict[str, Any]] = []
    for row in out_rows:
        core_score, quality, component_scores, component_quality = score_row(row, candidate, neutral_score=neutral_score)
        # Explicit None check: `or neutral_score` would coerce a legitimate
        # 0.0 overlay score to neutral.
        overlay_value = safe_float(row.get("sector_overlay_score"))
        overlay = overlay_value if overlay_value is not None else neutral_score
        overlay_quality = safe_float(row.get("sector_overlay_quality")) or 0.0
        final_score = core_score * (1.0 - overlay_weight) + overlay * overlay_weight if overlay_quality > 0 else core_score
        scored.append(
            {
                "ticker": row["ticker"],
                "asof_date": row["asof_date"],
                "stage8_candidate_score": final_score,
                "stage8_core_score": core_score,
                "stage8_quality": quality,
                "stage7_rank": stage7_by_ticker.get(str(row["ticker"]), {}).get("final_rank"),
                "stage7_score": stage7_by_ticker.get(str(row["ticker"]), {}).get("final_score"),
                "component_scores_json": json.dumps(component_scores, sort_keys=True),
                "component_quality_json": json.dumps(component_quality, sort_keys=True),
                # Ranking eligibility here is the baseline gate; Stage 7 applies
                # its own stricter gates when these weights are adopted.
                "baseline_rank_ready_flag": row.get("rank_ready_flag"),
            }
        )
    rankable = sorted([row for row in scored if int(row.get("baseline_rank_ready_flag") or 0) == 1], key=lambda row: (-float(row["stage8_candidate_score"]), str(row["ticker"])))
    for idx, row in enumerate(rankable, start=1):
        row["stage8_candidate_rank"] = idx
    for row in scored:
        row.setdefault("stage8_candidate_rank", "")
    return sorted(scored, key=lambda row: (row["stage8_candidate_rank"] == "", row["stage8_candidate_rank"] or 10**9, row["ticker"]))


def load_eval_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "neutral_score": float(cfg_get(config, "semiconductor_calibrated_scoring.neutral_score", 50.0)),
        "top_quantile": float(cfg_get(config, f"{CONFIG_KEY}.top_quantile", 0.20)),
        "max_turnover": float(cfg_get(config, f"{CONFIG_KEY}.max_turnover", 0.55)),
        "max_top_cohort_share": float(cfg_get(config, f"{CONFIG_KEY}.max_top_cohort_share", 0.45)),
        "min_cross_section": int(cfg_get(config, "semiconductor_signal_diagnostics.min_cross_section", 30)),
        "stability_lambda": float(cfg_get(config, f"{CONFIG_KEY}.stability_lambda", 0.10)),
        "complexity_penalty_per_subfeature": float(cfg_get(config, f"{CONFIG_KEY}.complexity_penalty_per_subfeature", 0.0005)),
        "turnover_cost_bps": float(cfg_get(config, f"{CONFIG_KEY}.turnover_cost_bps", 20.0)),
    }


INFEASIBLE_OBJECTIVE_PENALTY = 1.0


def optimize_weights(
    panel: list[dict[str, Any]],
    train_dates: list[date],
    horizons: list[int],
    config: dict[str, Any],
    bounds: dict[str, tuple[float, float]],
    eval_kwargs: dict[str, Any],
    *,
    n_trials: int,
    seed: int,
    timeout_sec: int | None = None,
    storage_url: str | None = None,
    study_name: str | None = None,
) -> tuple[Candidate, Any]:
    """Run one constrained TPE search on the given train dates and return the best feasible candidate.

    With hard constraints enabled (default), candidates violating the turnover
    or cohort-concentration caps on the train window receive a large objective
    penalty: TPE still learns to move away from them, but any feasible candidate
    beats every infeasible one, so the returned best is feasible whenever one
    exists — instead of the search wasting its budget on winners the promotion
    gate must reject.
    """
    import optuna

    hard_constraints = bool(cfg_get(config, f"{CONFIG_KEY}.hard_constraints_in_search", True))
    max_turnover = float(eval_kwargs["max_turnover"])
    max_top_cohort_share = float(eval_kwargs["max_top_cohort_share"])

    def objective(trial: Any) -> float:
        candidate = sample_candidate(trial, config, bounds)
        metrics = evaluate_candidate(panel, train_dates, horizons, candidate, **eval_kwargs)
        feasible = (
            float(metrics["avg_top_turnover"]) <= max_turnover
            and float(metrics["avg_top_cohort_share"]) <= max_top_cohort_share
        )
        trial.set_user_attr("metrics", {key: value for key, value in metrics.items() if key != "date_rows"})
        trial.set_user_attr("weights", json_ready_weights(candidate))
        trial.set_user_attr("feasible", int(feasible))
        if hard_constraints and not feasible:
            return float(metrics["objective"]) - INFEASIBLE_OBJECTIVE_PENALTY
        return float(metrics["objective"])

    sampler = optuna.samplers.TPESampler(seed=seed)
    if storage_url and study_name:
        study = optuna.create_study(direction="maximize", sampler=sampler, study_name=study_name, storage=storage_url)
    else:
        study = optuna.create_study(direction="maximize", sampler=sampler)
    study.optimize(objective, n_trials=n_trials, timeout=timeout_sec, show_progress_bar=False)
    complete = [trial for trial in study.trials if trial.state == optuna.trial.TrialState.COMPLETE]
    if not complete:
        raise RuntimeError("All Stage 8 trials failed; no best candidate available.")
    best_trial = study.best_trial
    if hard_constraints and not int(best_trial.user_attrs.get("feasible", 1)):
        raise RuntimeError(
            "No feasible candidate found within turnover/cohort caps over "
            f"{len(complete)} trials; constraints may be infeasible for this universe."
        )
    best_candidate = sample_candidate(best_trial, config, bounds)
    best_weights_raw = best_trial.user_attrs.get("weights")
    if isinstance(best_weights_raw, dict):
        # Rebuild best from user attrs to avoid any edge case where Optuna's
        # trial object re-samples values differently in future versions.
        best_candidate = Candidate(
            component_weights={str(k): float(v) for k, v in best_weights_raw["component_weights"].items()},
            subfeature_specs={
                str(component): [(str(key), float(weight)) for key, weight in weights.items()]
                for component, weights in best_weights_raw["subfeature_weights"].items()
            },
        )
    return best_candidate, study


def run_semiconductor_optuna_calibration() -> None:
    configure_utc_logging()
    args = parse_args("Run Stage 8 constrained Optuna calibration for semiconductor scores.")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_dir"), base_dir=base_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    n_trials = int(args.n_trials if args.n_trials is not None else cfg_get(config, f"{CONFIG_KEY}.n_trials", 180))
    timeout_cfg = int(args.timeout_sec if args.timeout_sec is not None else cfg_get(config, f"{CONFIG_KEY}.timeout_sec", 0))
    timeout_sec = timeout_cfg if timeout_cfg > 0 else None
    seed = int(cfg_get(config, f"{CONFIG_KEY}.random_seed", 357))
    neutral_score = float(cfg_get(config, "semiconductor_calibrated_scoring.neutral_score", 50.0))
    max_turnover = float(cfg_get(config, f"{CONFIG_KEY}.max_turnover", 0.55))
    max_top_cohort_share = float(cfg_get(config, f"{CONFIG_KEY}.max_top_cohort_share", 0.45))
    step = int(cfg_get(config, "semiconductor_signal_diagnostics.step_trading_days", 21))
    eval_kwargs = load_eval_kwargs(config)
    bounds = component_bounds(config)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry_into_db(conn, config, base_dir)

    # The panel is capped at the configured calibration train-end date by
    # default so post-lock bars cannot leak into weight selection; the
    # --allow-post-lock-panel research override is stamped into the outputs.
    configured_train_end = calibration_train_end(config)
    train_end_cap = configured_train_end
    if args.allow_post_lock_panel:
        LOGGER.warning("--allow-post-lock-panel set: calibration panel extends past the model lock (research only).")
        train_end_cap = None
    post_lock_data_included = train_end_cap is None
    panel, panel_dates, horizons = build_panel(config, db_path, panel_end_date=train_end_cap)
    train_dates, holdout_dates = split_dates(config, panel_dates, horizons=horizons, step=step)
    LOGGER.info("Stage 8 split: train_dates=%d holdout_dates=%d", len(train_dates), len(holdout_dates))
    if len(train_dates) < 20 or len(holdout_dates) < 8:
        raise RuntimeError("Insufficient panel dates for Stage 8 calibration.")

    stage7 = stage7_candidate(config)
    stage7_train = evaluate_candidate(panel, train_dates, horizons, stage7, **eval_kwargs)
    stage7_holdout = evaluate_candidate(panel, holdout_dates, horizons, stage7, emit_date_rows=True, **eval_kwargs)

    trial_rows: list[dict[str, Any]] = []
    study_name = f"stage8_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    storage_path = output_dir / "stage8_optuna_study.sqlite"
    best_candidate, study = optimize_weights(
        panel,
        train_dates,
        horizons,
        config,
        bounds,
        eval_kwargs,
        n_trials=n_trials,
        seed=seed,
        timeout_sec=timeout_sec,
        storage_url=f"sqlite:///{storage_path.as_posix()}",
        study_name=study_name,
    )
    best_train = evaluate_candidate(panel, train_dates, horizons, best_candidate, emit_date_rows=True, **eval_kwargs)
    best_holdout = evaluate_candidate(panel, holdout_dates, horizons, best_candidate, emit_date_rows=True, **eval_kwargs)
    stage7_full = evaluate_candidate(panel, panel_dates, horizons, stage7, emit_date_rows=True, **eval_kwargs)

    # Robustness: both candidates re-evaluated on contiguous panel-date folds.
    # No fitting happens per fold, so no purging is required; this measures
    # whether the improvement is consistent through time or one lucky regime.
    folds = contiguous_folds(panel_dates, int(cfg_get(config, f"{CONFIG_KEY}.robustness_folds", 5)))
    fold_rows: list[dict[str, Any]] = []
    fold_wins = 0
    scored_folds = 0
    for fold_idx, fold_dates in enumerate(folds):
        if len(fold_dates) < 4:
            continue
        stage7_fold = evaluate_candidate(panel, fold_dates, horizons, stage7, **eval_kwargs)
        best_fold = evaluate_candidate(panel, fold_dates, horizons, best_candidate, **eval_kwargs)
        scored_folds += 1
        win = int(float(best_fold.get("objective", 0.0)) > float(stage7_fold.get("objective", 0.0)))
        fold_wins += win
        fold_rows.append(
            {
                "fold": fold_idx,
                "fold_start": fold_dates[0].isoformat(),
                "fold_end": fold_dates[-1].isoformat(),
                "n_dates": len(fold_dates),
                "stage7_objective": stage7_fold.get("objective"),
                "stage8_objective": best_fold.get("objective"),
                "stage8_wins": win,
                **{f"stage7_mean_ic_{h}": stage7_fold.get(f"mean_ic_{h}") for h in horizons},
                **{f"stage8_mean_ic_{h}": best_fold.get(f"mean_ic_{h}") for h in horizons},
            }
        )
    fold_win_fraction = fold_wins / scored_folds if scored_folds else 0.0

    for trial in study.trials:
        metrics = trial.user_attrs.get("metrics", {})
        weights = trial.user_attrs.get("weights", {})
        row = {
            "trial": trial.number,
            "value": trial.value,
            "state": str(trial.state),
            "feasible": trial.user_attrs.get("feasible", ""),
            **flatten_metrics("train", metrics, horizons),
            "component_weights_json": json.dumps(weights.get("component_weights", {}), sort_keys=True),
            "subfeature_weights_json": json.dumps(weights.get("subfeature_weights", {}), sort_keys=True),
        }
        trial_rows.append(row)

    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else horizons[0]
    min_ic_primary = float(cfg_get(config, f"{CONFIG_KEY}.min_holdout_mean_ic_21", 0.01))
    min_ic_secondary = float(cfg_get(config, f"{CONFIG_KEY}.min_holdout_mean_ic_63", 0.01))
    min_hit = float(cfg_get(config, f"{CONFIG_KEY}.min_holdout_hit_rate", 0.50))
    min_improvement = float(cfg_get(config, f"{CONFIG_KEY}.promotion_min_objective_improvement", 0.002))
    min_fold_win_fraction = float(cfg_get(config, f"{CONFIG_KEY}.min_fold_win_fraction", 0.5))
    promotion_candidate = int(
        float(best_holdout.get("objective", 0.0)) >= float(stage7_holdout.get("objective", 0.0)) + min_improvement
        and float(best_holdout.get(f"mean_ic_{primary}", 0.0)) >= min_ic_primary
        and float(best_holdout.get(f"mean_ic_{secondary}", 0.0)) >= min_ic_secondary
        and float(best_holdout.get(f"hit_rate_{primary}", 0.0)) >= min_hit
        and float(best_holdout.get("avg_top_turnover", 1.0)) <= max_turnover
        and float(best_holdout.get("avg_top_cohort_share", 1.0)) <= max_top_cohort_share
        and fold_win_fraction >= min_fold_win_fraction
    )

    summary_rows = [
        {
            "model": "stage7_baseline",
            **flatten_metrics("train", stage7_train, horizons),
            **flatten_metrics("holdout", stage7_holdout, horizons),
            "fold_win_fraction": "",
            "promotion_candidate": 0,
        },
        {
            "model": "stage8_best_candidate",
            **flatten_metrics("train", best_train, horizons),
            **flatten_metrics("holdout", best_holdout, horizons),
            "fold_win_fraction": round(fold_win_fraction, 4),
            "promotion_candidate": promotion_candidate,
        },
    ]
    config_bytes = config_path.read_bytes()
    try:
        git_commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PACKAGE_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except OSError:
        git_commit = ""
    best_weights = {
        "source_id": str(cfg_get(config, f"{CONFIG_KEY}.source_id", "semiconductor_stage8_optuna_calibration")),
        "n_trials": len(study.trials),
        "train_dates": [train_dates[0].isoformat(), train_dates[-1].isoformat()],
        "holdout_dates": [holdout_dates[0].isoformat(), holdout_dates[-1].isoformat()],
        "promotion_candidate": promotion_candidate,
        "stage7_holdout_objective": stage7_holdout.get("objective"),
        "stage8_holdout_objective": best_holdout.get("objective"),
        "objective_improvement": float(best_holdout.get("objective", 0.0)) - float(stage7_holdout.get("objective", 0.0)),
        "fold_win_fraction": fold_win_fraction,
        "robustness_folds": scored_folds,
        "random_seed": seed,
        "study_name": study_name,
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "git_commit": git_commit,
        "calibration_train_end_date": configured_train_end.isoformat() if configured_train_end is not None else "",
        "panel_end_cap_date": train_end_cap.isoformat() if train_end_cap is not None else "",
        "post_lock_data_included": post_lock_data_included,
        "objective_params": {
            "stability_lambda": eval_kwargs["stability_lambda"],
            "complexity_penalty_per_subfeature": eval_kwargs["complexity_penalty_per_subfeature"],
            "turnover_cost_bps": eval_kwargs["turnover_cost_bps"],
            "hard_constraints_in_search": bool(cfg_get(config, f"{CONFIG_KEY}.hard_constraints_in_search", True)),
        },
        **json_ready_weights(best_candidate),
    }

    write_csv(output_dir / "stage8_trials.csv", trial_rows)
    write_csv(output_dir / "stage8_best_summary.csv", summary_rows)
    write_csv(output_dir / "stage8_best_train_by_date.csv", best_train["date_rows"])
    write_csv(output_dir / "stage8_best_holdout_by_date.csv", best_holdout["date_rows"])
    write_csv(output_dir / "stage8_stage7_holdout_by_date.csv", stage7_holdout["date_rows"])
    write_csv(output_dir / "stage8_stage7_full_by_date.csv", stage7_full["date_rows"])
    write_csv(output_dir / "stage8_fold_robustness.csv", fold_rows)
    write_csv(output_dir / "stage8_subfeature_correlation.csv", subfeature_correlation_rows(panel))
    write_csv(output_dir / "stage8_candidate_current_scores.csv", current_candidate_scores(config, db_path, best_candidate, neutral_score=neutral_score))
    (output_dir / "stage8_best_weights.json").write_text(json.dumps(best_weights, indent=2, sort_keys=True), encoding="utf-8")
    LOGGER.info(
        "Stage 8 calibration complete: best_train=%s best_holdout=%s fold_win_fraction=%.2f promotion_candidate=%s output=%s",
        best_train.get("objective"),
        best_holdout.get("objective"),
        fold_win_fraction,
        promotion_candidate,
        output_dir,
    )


def run_semiconductor_walk_forward_calibration() -> None:
    """Walk-forward refit validation: does re-calibrating weights beat Stage 7 out of sample?

    At each refit point the TPE search runs on an expanding train window only;
    the refit candidate and the static Stage 7 baseline are then both evaluated
    on the next embargoed, untouched test block. Aggregating across blocks
    measures whether the calibration *procedure* adds value, instead of whether
    one particular weight set got lucky on one split.
    """
    configure_utc_logging()
    args = parse_args("Run Stage 8 walk-forward refit validation for semiconductor weight calibration.")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    base_output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_dir"), base_dir=base_dir)
    output_dir = base_output_dir / str(cfg_get(config, f"{CONFIG_KEY}.walk_forward.output_subdir", "walk_forward"))
    output_dir.mkdir(parents=True, exist_ok=True)
    n_trials = int(args.n_trials if args.n_trials is not None else cfg_get(config, f"{CONFIG_KEY}.walk_forward.n_trials_per_refit", 60))
    seed = int(cfg_get(config, f"{CONFIG_KEY}.random_seed", 357))
    initial_train = int(cfg_get(config, f"{CONFIG_KEY}.walk_forward.initial_train_dates", 40))
    block_size = int(cfg_get(config, f"{CONFIG_KEY}.walk_forward.test_block_dates", 12))
    min_blocks = int(cfg_get(config, f"{CONFIG_KEY}.walk_forward.min_test_blocks", 3))
    step = int(cfg_get(config, "semiconductor_signal_diagnostics.step_trading_days", 21))
    eval_kwargs = load_eval_kwargs(config)
    bounds = component_bounds(config)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry_into_db(conn, config, base_dir)

    # Same post-lock leakage guard as the main calibration path: refit blocks
    # must not train or test on bars past the configured train-end date unless
    # the research override is set (and stamped into the summary).
    configured_train_end = calibration_train_end(config)
    train_end_cap = configured_train_end
    if args.allow_post_lock_panel:
        LOGGER.warning("--allow-post-lock-panel set: walk-forward panel extends past the model lock (research only).")
        train_end_cap = None
    post_lock_data_included = train_end_cap is None
    panel, panel_dates, horizons = build_panel(config, db_path, panel_end_date=train_end_cap)
    min_embargo = int(math.ceil(max(horizons) / max(1, step))) + 1
    embargo = max(min_embargo, int(cfg_get(config, f"{CONFIG_KEY}.embargo_panel_dates", 4)))
    stage7 = stage7_candidate(config)
    primary = horizons[0]

    block_rows: list[dict[str, Any]] = []
    improvements: list[float] = []
    refit_ics: list[float] = []
    stage7_ics: list[float] = []
    wins = 0
    block_idx = 0
    test_start = initial_train + embargo
    while test_start < len(panel_dates):
        train_dates = panel_dates[: test_start - embargo]
        test_dates = panel_dates[test_start: test_start + block_size]
        if len(test_dates) < 4 or len(train_dates) < 20:
            break
        candidate, _study = optimize_weights(
            panel,
            train_dates,
            horizons,
            config,
            bounds,
            eval_kwargs,
            n_trials=n_trials,
            seed=seed + block_idx,
        )
        candidate_metrics = evaluate_candidate(panel, test_dates, horizons, candidate, **eval_kwargs)
        stage7_metrics = evaluate_candidate(panel, test_dates, horizons, stage7, **eval_kwargs)
        improvement = float(candidate_metrics.get("objective", 0.0)) - float(stage7_metrics.get("objective", 0.0))
        win = int(improvement > 0)
        wins += win
        improvements.append(improvement)
        refit_ics.append(float(candidate_metrics.get(f"mean_ic_{primary}", 0.0)))
        stage7_ics.append(float(stage7_metrics.get(f"mean_ic_{primary}", 0.0)))
        block_rows.append(
            {
                "block": block_idx,
                "train_start": train_dates[0].isoformat(),
                "train_end": train_dates[-1].isoformat(),
                "test_start": test_dates[0].isoformat(),
                "test_end": test_dates[-1].isoformat(),
                "n_train_dates": len(train_dates),
                "n_test_dates": len(test_dates),
                "refit_objective": candidate_metrics.get("objective"),
                "stage7_objective": stage7_metrics.get("objective"),
                "objective_improvement": improvement,
                "refit_wins": win,
                **{f"refit_mean_ic_{h}": candidate_metrics.get(f"mean_ic_{h}") for h in horizons},
                **{f"stage7_mean_ic_{h}": stage7_metrics.get(f"mean_ic_{h}") for h in horizons},
                **{f"refit_hit_rate_{h}": candidate_metrics.get(f"hit_rate_{h}") for h in horizons},
                **{f"refit_mean_spread_net_{h}": candidate_metrics.get(f"mean_spread_net_{h}") for h in horizons},
                "refit_avg_top_turnover": candidate_metrics.get("avg_top_turnover"),
                "refit_avg_top_cohort_share": candidate_metrics.get("avg_top_cohort_share"),
                "component_weights_json": json.dumps(candidate.component_weights, sort_keys=True),
            }
        )
        LOGGER.info(
            "Walk-forward block %d: test=%s..%s improvement=%.5f win=%d",
            block_idx,
            test_dates[0],
            test_dates[-1],
            improvement,
            win,
        )
        test_start += block_size
        block_idx += 1

    if len(block_rows) < min_blocks:
        raise RuntimeError(
            f"Only {len(block_rows)} walk-forward blocks available (need {min_blocks}); "
            "extend the panel history or reduce walk_forward.test_block_dates."
        )

    improvement_stats = stats(improvements)
    paired_t = (
        improvement_stats["mean"] / improvement_stats["std"] * math.sqrt(improvement_stats["n"])
        if improvement_stats["std"] > 0
        else None
    )
    summary = {
        "n_blocks": len(block_rows),
        "n_trials_per_refit": n_trials,
        "initial_train_dates": initial_train,
        "test_block_dates": block_size,
        "embargo_panel_dates": embargo,
        "refit_win_rate": wins / len(block_rows),
        "mean_objective_improvement": improvement_stats["mean"],
        "improvement_paired_t": paired_t,
        f"mean_refit_oos_ic_{primary}": sum(refit_ics) / len(refit_ics),
        f"mean_stage7_oos_ic_{primary}": sum(stage7_ics) / len(stage7_ics),
        # The verdict the run exists to produce: re-calibration must beat the
        # static Stage 7 weights in most blocks AND on average to be worth it.
        "procedure_adds_value": int(wins / len(block_rows) >= 0.5 and improvement_stats["mean"] > 0),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "random_seed": seed,
        "calibration_train_end_date": configured_train_end.isoformat() if configured_train_end is not None else "",
        "panel_end_cap_date": train_end_cap.isoformat() if train_end_cap is not None else "",
        "post_lock_data_included": post_lock_data_included,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_csv(output_dir / "walk_forward_blocks.csv", block_rows)
    (output_dir / "walk_forward_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_csv(output_dir / "walk_forward_summary.csv", [summary])
    LOGGER.info(
        "Walk-forward complete: blocks=%d win_rate=%.2f mean_improvement=%.5f paired_t=%s procedure_adds_value=%s output=%s",
        len(block_rows),
        summary["refit_win_rate"],
        summary["mean_objective_improvement"],
        f"{paired_t:.2f}" if paired_t is not None else "n/a",
        summary["procedure_adds_value"],
        output_dir,
    )


def validate_semiconductor_optuna_calibration() -> int:
    configure_utc_logging()
    args = parse_args("Validate Stage 8 constrained Optuna calibration outputs.")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_dir"), base_dir=base_dir)
    errors: list[str] = []
    required = [
        output_dir / "stage8_trials.csv",
        output_dir / "stage8_best_summary.csv",
        output_dir / "stage8_best_weights.json",
        output_dir / "stage8_best_holdout_by_date.csv",
        output_dir / "stage8_fold_robustness.csv",
        output_dir / "stage8_candidate_current_scores.csv",
    ]
    for path in required:
        if not path.exists() or path.stat().st_size == 0:
            errors.append(f"Missing or empty Stage 8 output: {path}")
    best: dict[str, Any] = {}
    if (output_dir / "stage8_best_weights.json").exists():
        best = json.loads((output_dir / "stage8_best_weights.json").read_text(encoding="utf-8"))
        if int(best.get("n_trials") or 0) < 20:
            errors.append(f"Stage 8 trial count too low: {best.get('n_trials')}")
        bounds = component_bounds(config)
        weights = best.get("component_weights", {})
        if isinstance(weights, dict):
            for component, (lo, hi) in bounds.items():
                value = float(weights.get(component, 0.0))
                if value < lo - 0.0001 or value > hi + 0.0001:
                    errors.append(f"Component weight outside bounds: {component}={value} expected [{lo}, {hi}]")
            if abs(sum(float(value) for value in weights.values()) - 1.0) > 0.0001:
                errors.append("Component weights do not sum to 1.0")
        min_fold_win_fraction = float(cfg_get(config, f"{CONFIG_KEY}.min_fold_win_fraction", 0.5))
        if int(best.get("promotion_candidate") or 0) and float(best.get("fold_win_fraction") or 0.0) < min_fold_win_fraction:
            errors.append(
                f"Promoted candidate fails fold robustness: fold_win_fraction={best.get('fold_win_fraction')} < {min_fold_win_fraction}"
            )
    summary_rows: list[dict[str, str]] = []
    summary_path = output_dir / "stage8_best_summary.csv"
    if summary_path.exists():
        with summary_path.open("r", encoding="utf-8", newline="") as handle:
            summary_rows = list(csv.DictReader(handle))
    if len(summary_rows) < 2:
        errors.append("Stage 8 summary must include Stage 7 baseline and Stage 8 candidate rows.")
    else:
        candidate = next((row for row in summary_rows if row.get("model") == "stage8_best_candidate"), {})
        promotion_candidate = int(float(candidate.get("promotion_candidate") or 0))
        max_turnover = float(cfg_get(config, f"{CONFIG_KEY}.max_turnover", 0.55))
        max_cohort = float(cfg_get(config, f"{CONFIG_KEY}.max_top_cohort_share", 0.45))
        if promotion_candidate and float(candidate.get("holdout_avg_top_turnover") or 1.0) > max_turnover + 0.0001:
            errors.append(f"Holdout turnover exceeds cap: {candidate.get('holdout_avg_top_turnover')}")
        elif float(candidate.get("holdout_avg_top_turnover") or 1.0) > max_turnover + 0.0001:
            LOGGER.info("Stage 8 candidate is not promoted; holdout turnover exceeds cap: %s", candidate.get("holdout_avg_top_turnover"))
        if promotion_candidate and float(candidate.get("holdout_avg_top_cohort_share") or 1.0) > max_cohort + 0.0001:
            errors.append(f"Holdout cohort concentration exceeds cap: {candidate.get('holdout_avg_top_cohort_share')}")
        elif float(candidate.get("holdout_avg_top_cohort_share") or 1.0) > max_cohort + 0.0001:
            LOGGER.info("Stage 8 candidate is not promoted; holdout cohort concentration exceeds cap: %s", candidate.get("holdout_avg_top_cohort_share"))
    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info("Stage 8 Optuna calibration outputs validated: %s", output_dir)
    return 0


if __name__ == "__main__":
    from technology.core.optuna_artifact_governance import (
        run_stage8_with_governance,
        run_walk_forward_with_governance,
        validate_stage8_from_argv,
        validate_walk_forward_from_argv,
    )

    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "validate":
        sys.argv.pop(1)
        raise SystemExit(max(validate_semiconductor_optuna_calibration(), validate_stage8_from_argv("semiconductors")))
    if command == "walk-forward":
        sys.argv.pop(1)
        run_walk_forward_with_governance(run_semiconductor_walk_forward_calibration, "semiconductors")
        raise SystemExit(0)
    if command == "validate-walk-forward":
        sys.argv.pop(1)
        raise SystemExit(validate_walk_forward_from_argv("semiconductors"))
    run_stage8_with_governance(run_semiconductor_optuna_calibration, "semiconductors")
