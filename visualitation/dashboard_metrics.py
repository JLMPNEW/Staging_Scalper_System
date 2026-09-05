"""Pure calculations used by the Position Monitor dashboard.

This module deliberately has no Streamlit dependency.  Keeping portfolio-risk
math separate from rendering makes the methodology independently testable and
prevents UI reruns from changing the calculation contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CorrelationCoverage:
    """Coverage and lineage facts for one portfolio/index calculation."""

    covered_names: int
    total_names: int
    covered_gross_value: float
    total_gross_value: float
    complete_observations: int
    first_date: pd.Timestamp | None
    last_date: pd.Timestamp | None

    @property
    def market_value_ratio(self) -> float:
        if self.total_gross_value <= 0:
            return 0.0
        return self.covered_gross_value / self.total_gross_value


def exponentially_weighted_correlation(
    left: pd.Series,
    right: pd.Series,
    *,
    half_life: float = 42.0,
    minimum_observations: int = 90,
) -> float | None:
    """Return the endpoint exponentially weighted correlation of two series."""

    value, _ = _exponentially_weighted_correlation_estimate(
        left,
        right,
        half_life=half_life,
        minimum_observations=minimum_observations,
    )
    return value


def _paired_numeric(left: pd.Series, right: pd.Series) -> pd.DataFrame:
    """Return the finite, pairwise-complete numeric observations for two series."""

    return pd.concat(
        [pd.to_numeric(left, errors="coerce"), pd.to_numeric(right, errors="coerce")],
        axis=1,
    ).replace([np.inf, -np.inf], np.nan).dropna()


def _exponentially_weighted_correlation_estimate(
    left: pd.Series,
    right: pd.Series,
    *,
    half_life: float,
    minimum_observations: int,
) -> tuple[float | None, int]:
    """Return the endpoint EW correlation and its pairwise observation count."""

    pair = _paired_numeric(left, right)
    observations = len(pair)
    if len(pair) < minimum_observations or half_life <= 0:
        return None, observations

    values = pair.to_numpy(dtype=float)
    ages = np.arange(len(values) - 1, -1, -1, dtype=float)
    weights = np.power(0.5, ages / float(half_life))
    weights /= weights.sum()
    centered = values - np.average(values, axis=0, weights=weights)
    covariance = float(np.sum(weights * centered[:, 0] * centered[:, 1]))
    left_variance = float(np.sum(weights * centered[:, 0] ** 2))
    right_variance = float(np.sum(weights * centered[:, 1] ** 2))
    denominator = float(np.sqrt(left_variance * right_variance))
    if not np.isfinite(denominator) or denominator <= 0:
        return None, observations
    value = covariance / denominator
    return float(np.clip(value, -1.0, 1.0)), observations


def _pearson_correlation(
    left: pd.Series,
    right: pd.Series,
    *,
    window: int,
    minimum_observations: int,
) -> tuple[float | None, int]:
    pair = _paired_numeric(left, right)
    pair = pair.tail(int(window))
    if len(pair) < minimum_observations:
        return None, len(pair)
    value = pair.iloc[:, 0].corr(pair.iloc[:, 1])
    if pd.isna(value):
        return None, len(pair)
    return float(np.clip(float(value), -1.0, 1.0)), len(pair)


def _normalise_positions(holdings: pd.DataFrame) -> pd.DataFrame:
    required = {"asset_category", "symbol", "market_value"}
    if not required.issubset(holdings.columns):
        return pd.DataFrame(columns=["symbol", "market_value"])
    positions = holdings.loc[
        holdings["asset_category"].astype(str).str.casefold().eq("stocks")
    ].copy()
    positions["symbol"] = positions["symbol"].astype(str).str.strip().str.upper()
    positions["market_value"] = pd.to_numeric(positions["market_value"], errors="coerce")
    positions = positions.loc[
        positions["symbol"].ne("CASH") & positions["market_value"].notna()
    ]
    return (
        positions.groupby("symbol", as_index=False)["market_value"]
        .sum()
        .loc[lambda frame: frame["market_value"].ne(0.0)]
        .sort_values("symbol")
        .reset_index(drop=True)
    )


def calculate_index_risk(
    holdings: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    benchmark_tickers: Sequence[str],
    benchmark_labels: dict[str, str],
    sector_tickers: Sequence[str],
    half_life: float = 42.0,
    structural_window: int = 250,
    minimum_observations: int = 90,
) -> tuple[pd.DataFrame, pd.DataFrame, CorrelationCoverage]:
    """Build portfolio/index and holding/index correlation views.

    Portfolio returns are a current-market-value, signed, gross-normalised
    combination of covered holdings.  Log returns are used, with no forward
    filling.  Tactical correlation is an endpoint EWMA; structural correlation
    is an ordinary Pearson estimate over the latest ``structural_window`` rows.
    """

    positions = _normalise_positions(holdings)
    total_names = len(positions)
    total_gross = float(positions["market_value"].abs().sum()) if total_names else 0.0
    empty_coverage = CorrelationCoverage(0, total_names, 0.0, total_gross, 0, None, None)
    empty_benchmarks = pd.DataFrame(columns=[
        "benchmark", "label", "tactical", "structural", "shift",
        "tactical_observations", "structural_observations", "observations",
    ])
    empty_holdings = pd.DataFrame(columns=[
        "ticker", "market_value", "book_weight", "dominant_benchmark", "benchmark_label",
        "tactical", "structural", "shift", "tactical_observations",
        "structural_observations", "observations",
    ])
    if positions.empty or prices.empty:
        return empty_benchmarks, empty_holdings, empty_coverage

    panel = prices.copy()
    panel.index = pd.to_datetime(panel.index, errors="coerce")
    panel = panel.loc[panel.index.notna() & ~panel.index.duplicated(keep="last")].sort_index()
    panel.columns = [str(column).strip().upper() for column in panel.columns]
    panel = panel.apply(pd.to_numeric, errors="coerce")
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.log(panel / panel.shift(1))
    returns = returns.replace([np.inf, -np.inf], np.nan)

    eligible_symbols = [
        ticker for ticker in positions["symbol"]
        if ticker in returns.columns and int(returns[ticker].notna().sum()) >= minimum_observations
    ]
    covered = positions.loc[positions["symbol"].isin(eligible_symbols)].copy()
    covered_gross = float(covered["market_value"].abs().sum()) if len(covered) else 0.0
    if covered.empty or covered_gross <= 0:
        return empty_benchmarks, empty_holdings, empty_coverage

    signed_weights = covered.set_index("symbol")["market_value"] / covered_gross
    constituent_returns = returns.loc[:, list(signed_weights.index)]
    complete_constituents = constituent_returns.dropna(how="any")
    portfolio_returns = complete_constituents.mul(signed_weights, axis=1).sum(axis=1)
    first_date = pd.Timestamp(portfolio_returns.index.min()) if len(portfolio_returns) else None
    last_date = pd.Timestamp(portfolio_returns.index.max()) if len(portfolio_returns) else None
    coverage = CorrelationCoverage(
        covered_names=len(covered),
        total_names=total_names,
        covered_gross_value=covered_gross,
        total_gross_value=total_gross,
        complete_observations=len(portfolio_returns),
        first_date=first_date,
        last_date=last_date,
    )

    benchmark_rows: list[dict[str, object]] = []
    for benchmark in benchmark_tickers:
        ticker = str(benchmark).upper()
        tactical: float | None = None
        structural: float | None = None
        tactical_observations = 0
        structural_observations = 0
        if ticker in returns.columns:
            tactical, tactical_observations = _exponentially_weighted_correlation_estimate(
                portfolio_returns,
                returns[ticker],
                half_life=half_life,
                minimum_observations=minimum_observations,
            )
            structural, structural_observations = _pearson_correlation(
                portfolio_returns,
                returns[ticker],
                window=structural_window,
                minimum_observations=minimum_observations,
            )
        benchmark_rows.append({
            "benchmark": ticker,
            "label": benchmark_labels.get(ticker, ticker),
            "tactical": tactical,
            "structural": structural,
            "shift": (
                tactical - structural
                if tactical is not None and structural is not None
                else None
            ),
            "tactical_observations": tactical_observations,
            "structural_observations": structural_observations,
            # Backward-compatible alias for callers that previously treated
            # ``observations`` as the structural-window sample count.
            "observations": structural_observations,
        })
    benchmark_frame = pd.DataFrame(benchmark_rows)
    if not benchmark_frame.empty:
        benchmark_frame = benchmark_frame.sort_values(
            ["tactical", "benchmark"], ascending=[False, True], na_position="last"
        ).reset_index(drop=True)

    holding_rows: list[dict[str, object]] = []
    for row in covered.itertuples(index=False):
        ticker = str(row.symbol)
        candidates: list[dict[str, object]] = []
        for benchmark in sector_tickers:
            index_ticker = str(benchmark).upper()
            if index_ticker not in returns.columns:
                continue
            tactical, tactical_observations = _exponentially_weighted_correlation_estimate(
                returns[ticker],
                returns[index_ticker],
                half_life=half_life,
                minimum_observations=minimum_observations,
            )
            structural, structural_observations = _pearson_correlation(
                returns[ticker],
                returns[index_ticker],
                window=structural_window,
                minimum_observations=minimum_observations,
            )
            if tactical is None:
                continue
            candidates.append({
                "dominant_benchmark": index_ticker,
                "benchmark_label": benchmark_labels.get(index_ticker, index_ticker),
                "tactical": tactical,
                "structural": structural,
                "shift": tactical - structural if structural is not None else None,
                "tactical_observations": tactical_observations,
                "structural_observations": structural_observations,
                "observations": structural_observations,
            })
        if not candidates:
            continue
        dominant = max(candidates, key=lambda item: float(item["tactical"]))
        holding_rows.append({
            "ticker": ticker,
            "market_value": float(row.market_value),
            "book_weight": abs(float(row.market_value)) / total_gross if total_gross else 0.0,
            **dominant,
        })
    holding_frame = pd.DataFrame(holding_rows, columns=empty_holdings.columns)
    if not holding_frame.empty:
        holding_frame = holding_frame.sort_values(
            ["book_weight", "ticker"], ascending=[False, True]
        ).reset_index(drop=True)
    return benchmark_frame, holding_frame, coverage


def latest_correlation_matrix(
    rolling: pd.DataFrame,
    tickers: Sequence[str],
    pair_column,
) -> pd.DataFrame:
    """Expand the latest verified wide pair row into a symmetric matrix."""

    matrix = pd.DataFrame(index=list(tickers), columns=list(tickers), dtype=float)
    if rolling.empty:
        return matrix
    latest = rolling.iloc[-1]
    for left in tickers:
        for right in tickers:
            matrix.loc[left, right] = 1.0 if left == right else latest.get(pair_column(left, right), np.nan)
    return matrix
