from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any


PROVIDERS = ("alpha_vantage", "fmp")
METRICS = ("eps", "revenue")


@dataclass(frozen=True)
class CanonicalEstimate:
    snapshot_id: str
    provider: str
    ticker: str
    metric: str
    canonical_period: str
    fiscal_period_end: str
    estimate_average: float | None
    estimate_high: float | None
    estimate_low: float | None
    analyst_count: int | None
    currency: str
    fetched_at_utc: str
    retrieval_cycle: str
    quality_status: str
    quality_reasons: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.ticker, self.metric, self.canonical_period, self.fiscal_period_end)


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Non-finite estimate value: {value!r}")
    return result


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    result = int(value)
    if result < 0:
        raise ValueError(f"Negative analyst count: {value!r}")
    return result


def canonical_identity(provider: str, estimate_type: str, fiscal_period: str) -> tuple[str, str]:
    provider_name = provider.strip().casefold()
    type_name = estimate_type.strip().casefold()
    period_name = fiscal_period.strip().casefold()
    if provider_name not in PROVIDERS:
        raise ValueError(f"Unsupported estimate provider: {provider!r}")

    metric = next((candidate for candidate in METRICS if type_name.startswith(f"{candidate}_")), "")
    if not metric:
        raise ValueError(f"Unsupported estimate type: {estimate_type!r}")

    if type_name.endswith(("_annual", "_fiscal_year")) or period_name in {"annual", "fiscal_year"}:
        canonical_period = "annual"
    elif type_name.endswith(("_quarterly", "_fiscal_quarter")) or period_name in {
        "quarterly",
        "fiscal_quarter",
    }:
        canonical_period = "quarterly"
    else:
        raise ValueError(
            f"Unsupported estimate period for {provider_name}.{type_name}: {fiscal_period!r}"
        )
    return metric, canonical_period


def canonicalize_snapshot(row: Any) -> CanonicalEstimate:
    provider = str(row["provider"]).strip().casefold()
    metric, canonical_period = canonical_identity(
        provider,
        str(row["estimate_type"]),
        str(row["fiscal_period"]),
    )
    fiscal_period_end = str(row["fiscal_period_end"]).strip()
    date.fromisoformat(fiscal_period_end)
    average = _optional_float(row["estimate_average"])
    high = _optional_float(row["estimate_high"])
    low = _optional_float(row["estimate_low"])
    analyst_count = _optional_int(row["analyst_count"])

    reasons: list[str] = []
    if average is None:
        reasons.append("missing_estimate_average")
    if low is not None and high is not None and low > high:
        reasons.append("estimate_low_above_high")
    if average is not None and low is not None and average < low:
        reasons.append("estimate_average_below_low")
    if average is not None and high is not None and average > high:
        reasons.append("estimate_average_above_high")
    if analyst_count == 0:
        reasons.append("zero_analyst_count")

    hard_reasons = {
        "missing_estimate_average",
        "estimate_low_above_high",
        "estimate_average_below_low",
        "estimate_average_above_high",
    }
    quality_status = "FAIL" if hard_reasons.intersection(reasons) else ("WARN" if reasons else "PASS")
    return CanonicalEstimate(
        snapshot_id=str(row["snapshot_id"]).strip(),
        provider=provider,
        ticker=str(row["ticker"]).strip().upper(),
        metric=metric,
        canonical_period=canonical_period,
        fiscal_period_end=fiscal_period_end,
        estimate_average=average,
        estimate_high=high,
        estimate_low=low,
        analyst_count=analyst_count,
        currency=str(row["currency"]).strip().upper(),
        fetched_at_utc=str(row["fetched_at_utc"]).strip(),
        retrieval_cycle=str(row["retrieval_cycle"]).strip(),
        quality_status=quality_status,
        quality_reasons=tuple(reasons),
    )


def currency_comparison(alpha: CanonicalEstimate, fmp: CanonicalEstimate) -> str:
    if alpha.currency and fmp.currency:
        return "MATCH" if alpha.currency == fmp.currency else "MISMATCH"
    return "UNVERIFIED"


def relative_difference(alpha_value: float, fmp_value: float, *, floor: float) -> float:
    if floor <= 0:
        raise ValueError("relative-difference floor must be positive")
    return abs(alpha_value - fmp_value) / max(abs(alpha_value), abs(fmp_value), floor)


def select_conservative(
    alpha: CanonicalEstimate | None,
    fmp: CanonicalEstimate | None,
    *,
    tie_break_provider: str,
) -> tuple[CanonicalEstimate | None, str, bool]:
    if tie_break_provider not in PROVIDERS:
        raise ValueError(f"Unsupported tie-break provider: {tie_break_provider!r}")
    if alpha is None or fmp is None:
        return None, "requires_two_sources", False
    if alpha.quality_status == "FAIL" or fmp.quality_status == "FAIL":
        return None, "requires_two_quality_valid_sources", False
    alpha_row = alpha
    fmp_row = fmp
    assert alpha_row.estimate_average is not None
    assert fmp_row.estimate_average is not None
    if alpha_row.estimate_average < fmp_row.estimate_average:
        return alpha_row, "lower_estimate_alpha_vantage", True
    if fmp_row.estimate_average < alpha_row.estimate_average:
        return fmp_row, "lower_estimate_fmp", True
    selected = alpha_row if tie_break_provider == "alpha_vantage" else fmp_row
    return selected, f"equal_estimate_tie_break_{tie_break_provider}", True
