"""Outcome-blind capture of pre-registered transportation monitor candidates.

Implements the ``transportation_monitor_candidates_v1`` contract: on each
completed session date it derives the C1 sleeve membership from the published
rank table and computes the C3 asset-light fixed-weight score from already
loaded features. It never reads outcomes, never modifies frozen artifacts,
and refuses to overwrite an existing capture for a date.
"""
from __future__ import annotations

import csv
import sqlite3
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from industrials.transportation.oos_outcomes import finite_float, fmt


MODEL_FAMILY = "transportation"
CANDIDATES_CONTRACT_VERSION = "transportation_monitor_candidates_v1"
SURFACE_COHORT = "surface_freight_and_logistics"

SLEEVE_FIELDS = (
    "asof_date",
    "candidate_id",
    "ticker",
    "weight",
    "membership_basis",
)
SCORE_FIELDS = (
    "asof_date",
    "candidate_id",
    "ticker",
    "signal_asset_turnover_yoy_change",
    "signal_interest_coverage_level",
    "signal_realized_volatility_60d",
    "score",
    "weight",
    "status",
)


def read_rank_table(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def sleeve_rows(
    rank_rows: list[dict[str, str]],
    *,
    asof: str,
) -> list[dict[str, Any]]:
    members = sorted(
        str(row.get("ticker") or "")
        for row in rank_rows
        if str(row.get("calibration_cohort") or "") == SURFACE_COHORT
        and str(row.get("rank_ready_flag") or "") == "1"
    )
    if not members:
        return []
    weight = 1.0 / len(members)
    return [
        {
            "asof_date": asof,
            "candidate_id": "C1_SLEEVE",
            "ticker": ticker,
            "weight": fmt(weight),
            "membership_basis": "rank_table_surface_rank_ready",
        }
        for ticker in members
    ]


def _latest_value(
    connection: sqlite3.Connection,
    *,
    table: str,
    column: str,
    ticker: str,
    asof: str,
) -> float | None:
    if table not in {"feature_financial_statement", "feature_market_technical"}:
        raise ValueError(f"unsupported feature table={table}")
    row = connection.execute(
        f"""
        SELECT {column} AS value FROM {table}
        WHERE ticker=? AND model_family=? AND asof_date<=?
        ORDER BY asof_date DESC, source_id ASC LIMIT 1
        """,
        (ticker, MODEL_FAMILY, asof),
    ).fetchone()
    return finite_float(row["value"]) if row is not None else None


def _zscores(values: Mapping[str, float]) -> dict[str, float]:
    members = list(values.values())
    if len(members) < 2:
        return {ticker: 0.0 for ticker in values}
    mean = statistics.fmean(members)
    spread = statistics.pstdev(members)
    if spread <= 0:
        return {ticker: 0.0 for ticker in values}
    return {
        ticker: (value - mean) / spread for ticker, value in values.items()
    }


def asset_light_rows(
    connection: sqlite3.Connection,
    *,
    contract: Mapping[str, Any],
    asof: str,
) -> list[dict[str, Any]]:
    candidate = contract["candidates"]["C3_ASSET_LIGHT_THREE_SIGNAL"]
    universe = [str(ticker).upper() for ticker in candidate["universe"]]
    minimum_cross_section = int(
        candidate["scoring"]["minimum_cross_section"]
    )
    year_earlier = (
        date.fromisoformat(asof) - timedelta(days=365)
    ).isoformat()
    raw: dict[str, dict[str, float]] = {}
    for ticker in universe:
        turnover_now = _latest_value(
            connection,
            table="feature_financial_statement",
            column="asset_turnover",
            ticker=ticker,
            asof=asof,
        )
        turnover_then = _latest_value(
            connection,
            table="feature_financial_statement",
            column="asset_turnover",
            ticker=ticker,
            asof=year_earlier,
        )
        coverage_spec = candidate["signals"]["interest_coverage_level"]
        coverage_cap = float(coverage_spec.get("cap", 50.0))
        coverage = _latest_value(
            connection,
            table="feature_financial_statement",
            column="interest_coverage",
            ticker=ticker,
            asof=asof,
        )
        if coverage is None and turnover_now is not None:
            # Financial row exists but coverage is NULL: the builder emits
            # that for issuers with no material interest expense, which is
            # the strongest coverage state, not missing data.
            coverage = float(coverage_spec.get("no_debt_cap", coverage_cap))
        elif coverage is not None:
            coverage = min(coverage, coverage_cap)
        volatility = _latest_value(
            connection,
            table="feature_market_technical",
            column="realized_vol_60d",
            ticker=ticker,
            asof=asof,
        )
        signals: dict[str, float] = {}
        if turnover_now is not None and turnover_then is not None:
            signals["asset_turnover_yoy_change"] = (
                turnover_now - turnover_then
            )
        if coverage is not None:
            signals["interest_coverage_level"] = coverage
        if volatility is not None:
            signals["realized_volatility_60d"] = volatility
        raw[ticker] = signals

    signal_specs = candidate["signals"]
    per_signal_z: dict[str, dict[str, float]] = {}
    for signal_id in signal_specs:
        values = {
            ticker: signals[signal_id]
            for ticker, signals in raw.items()
            if signal_id in signals
        }
        per_signal_z[signal_id] = _zscores(values)

    scored: dict[str, float] = {}
    for ticker in universe:
        total = 0.0
        complete = True
        for signal_id, spec in signal_specs.items():
            z = per_signal_z[signal_id].get(ticker)
            if z is None:
                complete = False
                break
            total += float(spec["weight"]) * float(spec["sign"]) * z
        if complete:
            scored[ticker] = total

    if len(scored) < minimum_cross_section:
        return [
            {
                "asof_date": asof,
                "candidate_id": "C3_ASSET_LIGHT_THREE_SIGNAL",
                "ticker": ticker,
                "signal_asset_turnover_yoy_change": fmt(
                    raw.get(ticker, {}).get("asset_turnover_yoy_change")
                ),
                "signal_interest_coverage_level": fmt(
                    raw.get(ticker, {}).get("interest_coverage_level")
                ),
                "signal_realized_volatility_60d": fmt(
                    raw.get(ticker, {}).get("realized_volatility_60d")
                ),
                "score": "",
                "weight": "",
                "status": str(
                    candidate["scoring"]["insufficient_cross_section_status"]
                ),
            }
            for ticker in universe
        ]

    portfolio = candidate["portfolio_form"]
    floor = float(portfolio["floor"])
    cap = float(portfolio["cap"])
    base = 1.0 / len(scored)
    score_z = _zscores(scored)
    tilt_scale = base * 0.5
    tilted = {
        ticker: min(cap, max(floor, base + tilt_scale * score_z[ticker]))
        for ticker in scored
    }
    total_weight = sum(tilted.values())
    weights = {
        ticker: weight / total_weight for ticker, weight in tilted.items()
    }

    output: list[dict[str, Any]] = []
    for ticker in universe:
        signals = raw.get(ticker, {})
        included = ticker in scored
        output.append(
            {
                "asof_date": asof,
                "candidate_id": "C3_ASSET_LIGHT_THREE_SIGNAL",
                "ticker": ticker,
                "signal_asset_turnover_yoy_change": fmt(
                    signals.get("asset_turnover_yoy_change")
                ),
                "signal_interest_coverage_level": fmt(
                    signals.get("interest_coverage_level")
                ),
                "signal_realized_volatility_60d": fmt(
                    signals.get("realized_volatility_60d")
                ),
                "score": fmt(scored.get(ticker)),
                "weight": fmt(weights.get(ticker)) if included else "",
                "status": "SCORED" if included else "MISSING_SIGNAL",
            }
        )
    return output
