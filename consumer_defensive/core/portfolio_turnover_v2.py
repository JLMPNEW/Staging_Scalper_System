from __future__ import annotations

from typing import Iterable, Mapping


def equal_weight_long_short_holdings(
    top_tickers: Iterable[str],
    bottom_tickers: Iterable[str],
    *,
    leg_gross: float = 1.0,
) -> dict[str, float]:
    """Build a top-minus-bottom portfolio with explicit signed weights."""

    top = sorted(set(str(value) for value in top_tickers))
    bottom = sorted(set(str(value) for value in bottom_tickers))
    if not top or not bottom:
        raise ValueError('Long and short sleeves must both be non-empty.')
    overlap = set(top) & set(bottom)
    if overlap:
        raise ValueError(f'Long and short sleeves overlap: {sorted(overlap)}')
    if leg_gross <= 0.0:
        raise ValueError('leg_gross must be positive.')
    output = {ticker: leg_gross / len(top) for ticker in top}
    output.update({
        ticker: -leg_gross / len(bottom) for ticker in bottom
    })
    return output


def trade_notional_turnover(
    previous: Mapping[str, float] | None,
    current: Mapping[str, float] | None,
) -> float:
    """Return exact L1 traded notional, including cash entry or liquidation."""

    previous = previous or {}
    current = current or {}
    return sum(
        abs(float(current.get(ticker, 0.0)) - float(previous.get(ticker, 0.0)))
        for ticker in set(previous) | set(current)
    )


def one_way_leg_turnover(
    previous_tickers: Iterable[str] | None,
    current_tickers: Iterable[str],
) -> float:
    """Return 0.5*L1 equal-weight sleeve turnover for a policy constraint."""

    current = sorted(set(str(value) for value in current_tickers))
    if not current:
        raise ValueError('Current sleeve must be non-empty.')
    if previous_tickers is None:
        return 1.0
    previous = sorted(set(str(value) for value in previous_tickers))
    if not previous:
        return 1.0
    previous_weights = {ticker: 1.0 / len(previous) for ticker in previous}
    current_weights = {ticker: 1.0 / len(current) for ticker in current}
    return 0.5 * trade_notional_turnover(previous_weights, current_weights)
