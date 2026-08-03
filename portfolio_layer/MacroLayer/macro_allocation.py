#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Hashable, Sequence

import numpy as np
import pandas as pd


TOLERANCE = 1e-12


def bounded_normalize(
    raw: pd.Series,
    *,
    lower: float | pd.Series = 0.0,
    upper: float | pd.Series = 1.0,
    target_sum: float = 1.0,
    tolerance: float = TOLERANCE,
) -> pd.Series:
    """Normalize non-negative scores while satisfying exact lower/upper bounds.

    Positive scores receive residual budget proportionally. If they all saturate,
    any feasible remainder is distributed across zero-score rows instead of
    silently returning an underweight portfolio or re-breaking a cap.
    """
    scores = pd.to_numeric(raw, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(lower=0.0)
    low = pd.Series(lower, index=scores.index, dtype="float64") if np.isscalar(lower) else pd.to_numeric(lower, errors="coerce").reindex(scores.index)
    high = pd.Series(upper, index=scores.index, dtype="float64") if np.isscalar(upper) else pd.to_numeric(upper, errors="coerce").reindex(scores.index)
    if low.isna().any() or high.isna().any() or not np.isfinite(low).all() or not np.isfinite(high).all():
        raise ValueError("Allocation bounds must be finite for every row.")
    if (low < -tolerance).any() or (high < low - tolerance).any():
        raise ValueError("Allocation bounds require 0 <= lower <= upper for every row.")
    target = float(target_sum)
    if not np.isfinite(target) or target < -tolerance:
        raise ValueError(f"Allocation target_sum must be finite and non-negative; got {target_sum!r}.")
    min_sum = float(low.sum())
    max_sum = float(high.sum())
    if target < min_sum - tolerance or target > max_sum + tolerance:
        raise ValueError(
            f"Infeasible allocation bounds: lower_sum={min_sum:.12f} target={target:.12f} upper_sum={max_sum:.12f}."
        )

    out = low.astype(float).copy()
    capacity = (high - low).clip(lower=0.0).astype(float)
    remaining = max(0.0, target - float(out.sum()))
    while remaining > tolerance:
        eligible = capacity > tolerance
        if not bool(eligible.any()):
            raise RuntimeError(f"Allocation exhausted capacity with residual budget={remaining:.12g}.")
        preference = scores.where(eligible, 0.0)
        if float(preference.sum()) <= tolerance:
            preference = eligible.astype(float)
        proposed = preference / float(preference.sum()) * remaining
        saturating = eligible & proposed.ge(capacity - tolerance)
        if bool(saturating.any()):
            addition = capacity.where(saturating, 0.0)
        else:
            addition = proposed.where(eligible, 0.0)
        added = float(addition.sum())
        if added <= tolerance:
            raise RuntimeError(f"Allocation made no progress with residual budget={remaining:.12g}.")
        out = out + addition
        capacity = (capacity - addition).clip(lower=0.0)
        remaining = max(0.0, target - float(out.sum()))

    residual = target - float(out.sum())
    if abs(residual) > tolerance:
        eligible = capacity > tolerance
        if residual > 0 and bool(eligible.any()):
            idx = capacity.loc[eligible].idxmax()
            out.loc[idx] += residual
        elif residual < 0:
            reducible = (out - low) > tolerance
            if bool(reducible.any()):
                idx = (out - low).loc[reducible].idxmax()
                out.loc[idx] += residual
    if abs(float(out.sum()) - target) > max(tolerance, 1e-10):
        raise RuntimeError(f"Bounded allocation does not sum to target: {float(out.sum()):.12f} vs {target:.12f}.")
    if (out < low - tolerance).any() or (out > high + tolerance).any():
        raise RuntimeError("Bounded allocation violated a row bound.")
    return out.clip(lower=low, upper=high)


def hierarchical_bounded_normalize(
    raw: pd.Series,
    groups: Sequence[Hashable] | pd.Series,
    *,
    item_cap: float,
    group_cap: float,
    item_floor: float = 0.0,
    target_sum: float = 1.0,
) -> pd.Series:
    """Allocate exactly to target while enforcing both item and group caps."""
    scores = pd.to_numeric(raw, errors="coerce").fillna(0.0).clip(lower=0.0)
    group_series = pd.Series(groups, index=scores.index, dtype="object").fillna("UNKNOWN")
    if scores.empty:
        if abs(float(target_sum)) <= TOLERANCE:
            return pd.Series(dtype="float64", index=scores.index)
        raise ValueError("Cannot allocate a positive target over an empty universe.")
    if item_cap <= 0.0 or group_cap <= 0.0:
        raise ValueError("item_cap and group_cap must both be positive.")

    group_scores = scores.groupby(group_series, sort=False).sum()
    group_counts = group_series.value_counts(sort=False).reindex(group_scores.index).astype(float)
    group_lower = group_counts * float(item_floor)
    group_upper = np.minimum(float(group_cap), group_counts * float(item_cap))
    group_budget = bounded_normalize(
        group_scores,
        lower=group_lower,
        upper=group_upper,
        target_sum=target_sum,
    )

    out = pd.Series(0.0, index=scores.index, dtype="float64")
    for group_name, budget in group_budget.items():
        idx = group_series.index[group_series.eq(group_name)]
        out.loc[idx] = bounded_normalize(
            scores.loc[idx],
            lower=float(item_floor),
            upper=float(item_cap),
            target_sum=float(budget),
        )
    observed_groups = out.groupby(group_series).sum()
    if (observed_groups > float(group_cap) + 1e-10).any():
        raise RuntimeError("Hierarchical allocation violated a group cap.")
    return out