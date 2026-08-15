"""Shared normalization for provider estimate payloads.

Raw provider payloads are accepted only in memory. Callers persist the returned
normalized rows and must discard the source payload after normalization.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from portfolio_layer.expectations_monitor.provider_common import ProviderPayloadResult


def _clean(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.casefold() in {"", "none", "null", "n/a", "na", "-"} else value


def _first_clean(row: Mapping[str, Any], *fields: str) -> Any:
    for field in fields:
        value = _clean(row.get(field))
        if value is not None:
            return value
    return None


def _period_slug(value: Any, *, default: str) -> str:
    text = str(value if value is not None else default).strip().casefold()
    cleaned = "".join(char if char.isalnum() else "_" for char in text).strip("_")
    return cleaned or default


def _base_snapshot(
    result: ProviderPayloadResult,
    row: Mapping[str, Any],
    *,
    snapshot_run_id: str,
    retrieval_cycle: str,
    fiscal_period: str,
    estimate_type: str,
    entitlement_version: str,
) -> dict[str, Any]:
    fiscal_period_end = str(row.get("date", "")).strip()
    date.fromisoformat(fiscal_period_end)
    return {
        "snapshot_run_id": snapshot_run_id,
        "provider": result.provider,
        "endpoint_id": result.capability,
        "ticker": result.symbol,
        "fiscal_period_end": fiscal_period_end,
        "fiscal_period": fiscal_period,
        "estimate_type": f"{estimate_type}_{_period_slug(fiscal_period, default='unknown')}",
        "currency": str(row.get("currency", "")).strip(),
        "provider_published_at_utc": "",
        "request_started_at_utc": result.requested_at_utc,
        "fetched_at_utc": result.response_received_at_utc,
        "available_at_utc": result.response_received_at_utc,
        "retrieval_cycle": retrieval_cycle,
        "source_uid": f"{result.provider}:{result.symbol}:{fiscal_period_end}:{estimate_type}",
        "response_sha256": result.response_sha256,
        "entitlement_version": entitlement_version,
        "retention_class": "provisional_user_authorized",
        "coverage_status": "available",
    }


def normalize_estimates(
    result: ProviderPayloadResult,
    *,
    snapshot_run_id: str,
    retrieval_cycle: str,
    entitlement_version: str,
) -> list[dict[str, Any]]:
    """Normalize FMP and Alpha Vantage estimate snapshots to one contract."""
    if result.status != "AVAILABLE":
        return []
    normalized: list[dict[str, Any]] = []
    if result.provider == "alpha_vantage":
        payload = result.payload
        source_rows = payload.get("estimates", []) if isinstance(payload, dict) else []
        for source in source_rows:
            if not isinstance(source, dict):
                continue
            period = str(source.get("horizon", "unknown"))
            eps = _base_snapshot(
                result,
                source,
                snapshot_run_id=snapshot_run_id,
                retrieval_cycle=retrieval_cycle,
                fiscal_period=period,
                estimate_type="eps",
                entitlement_version=entitlement_version,
            )
            eps.update(
                {
                    "estimate_average": _clean(source.get("eps_estimate_average")),
                    "estimate_high": _clean(source.get("eps_estimate_high")),
                    "estimate_low": _clean(source.get("eps_estimate_low")),
                    "analyst_count": _clean(source.get("eps_estimate_analyst_count")),
                    "estimate_average_7_days_ago": _clean(source.get("eps_estimate_average_7_days_ago")),
                    "estimate_average_30_days_ago": _clean(source.get("eps_estimate_average_30_days_ago")),
                    "estimate_average_60_days_ago": _clean(source.get("eps_estimate_average_60_days_ago")),
                    "estimate_average_90_days_ago": _clean(source.get("eps_estimate_average_90_days_ago")),
                    "revision_up_7_days": _clean(source.get("eps_estimate_revision_up_trailing_7_days")),
                    "revision_down_7_days": _clean(source.get("eps_estimate_revision_down_trailing_7_days")),
                    "revision_up_30_days": _clean(source.get("eps_estimate_revision_up_trailing_30_days")),
                    "revision_down_30_days": _clean(source.get("eps_estimate_revision_down_trailing_30_days")),
                }
            )
            revenue = _base_snapshot(
                result,
                source,
                snapshot_run_id=snapshot_run_id,
                retrieval_cycle=retrieval_cycle,
                fiscal_period=period,
                estimate_type="revenue",
                entitlement_version=entitlement_version,
            )
            revenue.update(
                {
                    "estimate_average": _clean(source.get("revenue_estimate_average")),
                    "estimate_high": _clean(source.get("revenue_estimate_high")),
                    "estimate_low": _clean(source.get("revenue_estimate_low")),
                    "analyst_count": _clean(source.get("revenue_estimate_analyst_count")),
                }
            )
            normalized.extend((eps, revenue))
    elif result.provider == "fmp":
        source_rows = result.payload if isinstance(result.payload, list) else []
        fiscal_period = "quarterly" if result.capability == "analyst_estimates_quarterly" else "annual"
        field_aliases = {
            "eps": {
                "average": ("epsAvg", "estimatedEpsAvg"),
                "high": ("epsHigh", "estimatedEpsHigh"),
                "low": ("epsLow", "estimatedEpsLow"),
                "analysts": (
                    "numAnalystsEps",
                    "numberAnalystEstimatedEps",
                    "numberAnalystsEstimatedEps",
                ),
            },
            "revenue": {
                "average": ("revenueAvg", "estimatedRevenueAvg"),
                "high": ("revenueHigh", "estimatedRevenueHigh"),
                "low": ("revenueLow", "estimatedRevenueLow"),
                "analysts": (
                    "numAnalystsRevenue",
                    "numberAnalystEstimatedRevenue",
                    "numberAnalystsEstimatedRevenue",
                ),
            },
        }
        for source in source_rows:
            if not isinstance(source, dict):
                continue
            for estimate_type, aliases in field_aliases.items():
                row = _base_snapshot(
                    result,
                    source,
                    snapshot_run_id=snapshot_run_id,
                    retrieval_cycle=retrieval_cycle,
                    fiscal_period=fiscal_period,
                    estimate_type=estimate_type,
                    entitlement_version=entitlement_version,
                )
                row.update(
                    {
                        "estimate_average": _first_clean(source, *aliases["average"]),
                        "estimate_high": _first_clean(source, *aliases["high"]),
                        "estimate_low": _first_clean(source, *aliases["low"]),
                        "analyst_count": _first_clean(source, *aliases["analysts"]),
                    }
                )
                normalized.append(row)
    else:
        raise ValueError(f"Unsupported estimate provider: {result.provider}")
    return [
        row
        for row in normalized
        if any(
            row.get(field) is not None
            for field in ("estimate_average", "estimate_high", "estimate_low", "analyst_count")
        )
    ]


def capture_plan(provider: str) -> tuple[str, ...]:
    if provider == "alpha_vantage":
        return ("earnings_estimates",)
    if provider == "fmp":
        return ("analyst_estimates", "analyst_estimates_quarterly")
    raise ValueError(f"Unsupported estimate provider: {provider}")
