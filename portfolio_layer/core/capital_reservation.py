"""Portfolio-owned cash reservations derived from governed allocation caps."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


CONSUMER_DEFENSIVE_COHORTS = frozenset(
    {
        "beverages",
        "consumer_staples_distribution_retail",
        "household_personal_tobacco",
        "packaged_foods_agricultural_products",
    }
)


def _finite_fraction(value: object, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite fraction")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite fraction") from exc
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{label} must be within [0, 1]")
    return parsed


def consumer_defensive_reserved_cash_fraction(config: Mapping[str, Any]) -> float:
    """Return the Consumer sector slot not authorized to an active cohort.

    The sector ceiling is a budget reservation, while the four scope caps are
    executable authority.  Their difference must remain cash; it must never be
    silently reassigned to another Consumer cohort or another sector.
    """

    score_contract = config.get("score_contract")
    if not isinstance(score_contract, Mapping):
        raise ValueError("score_contract must be a mapping")
    sectors = score_contract.get("sectors")
    if not isinstance(sectors, list):
        raise ValueError("score_contract.sectors must be a list")
    matches = [
        sector
        for sector in sectors
        if isinstance(sector, Mapping)
        and str(sector.get("model_family") or "").strip() == "consumer_defensive"
    ]
    if len(matches) != 1:
        raise ValueError("Portfolio config requires exactly one Consumer Defensive sector")
    score_cfg = matches[0]
    if score_cfg.get("enabled") is not True:
        return 0.0

    optimizer = config.get("optimizer")
    if not isinstance(optimizer, Mapping):
        raise ValueError("optimizer must be a mapping")
    score_caps = score_cfg.get("optimizer_cap_by_scope")
    optimizer_scope_root = optimizer.get("scope_weight_caps")
    if not isinstance(score_caps, Mapping) or not isinstance(
        optimizer_scope_root, Mapping
    ):
        raise ValueError("Consumer scope-cap surfaces must be mappings")
    optimizer_caps = optimizer_scope_root.get("consumer_defensive")
    if not isinstance(optimizer_caps, Mapping):
        raise ValueError("optimizer Consumer scope caps must be a mapping")
    if (
        set(score_caps) != CONSUMER_DEFENSIVE_COHORTS
        or set(optimizer_caps) != CONSUMER_DEFENSIVE_COHORTS
    ):
        raise ValueError("Consumer scope caps require the exact cohort census")

    active_authority = 0.0
    for cohort in sorted(CONSUMER_DEFENSIVE_COHORTS):
        score_cap = _finite_fraction(
            score_caps[cohort], label=f"Consumer score cap {cohort}"
        )
        optimizer_cap = _finite_fraction(
            optimizer_caps[cohort], label=f"Consumer optimizer cap {cohort}"
        )
        if not math.isclose(
            score_cap, optimizer_cap, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError(f"Consumer scope cap diverges for {cohort}")
        active_authority += score_cap

    score_sector_cap = _finite_fraction(
        score_cfg.get("optimizer_sector_cap"),
        label="Consumer score-contract sector cap",
    )
    optimizer_sector_caps = optimizer.get("sector_weight_caps")
    if not isinstance(optimizer_sector_caps, Mapping):
        raise ValueError("optimizer.sector_weight_caps must be a mapping")
    optimizer_sector_cap = _finite_fraction(
        optimizer_sector_caps.get("consumer_defensive"),
        label="Consumer optimizer sector cap",
    )
    if not math.isclose(
        score_sector_cap,
        optimizer_sector_cap,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("Consumer score/optimizer sector caps diverge")
    if active_authority > score_sector_cap + 1e-12:
        raise ValueError("Consumer cohort authority exceeds its sector cap")
    return max(0.0, score_sector_cap - active_authority)


__all__ = [
    "CONSUMER_DEFENSIVE_COHORTS",
    "consumer_defensive_reserved_cash_fraction",
]
