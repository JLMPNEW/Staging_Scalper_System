"""Read-only PIT index-membership enrichment for historical candidates."""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Mapping, Sequence

from .norgate_census import CLASSIFICATION_TIME_BASIS, approved_watchlists
from .norgate_runtime import (
    NORGATE_MEMBERSHIP_DATABASES,
    norgate_database_fingerprint,
    require_norgate_snapshot,
)
from .universe import UniversePolicy


def membership_dates_flags(frame: Any) -> tuple[list[str], list[int]]:
    """Normalize a provider membership series and reject ambiguous values."""

    import pandas as pd  # type: ignore

    if frame is None or frame.empty:
        return [], []
    dates = pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None)
    if not dates.is_monotonic_increasing or dates.has_duplicates:
        raise ValueError("Provider membership dates are not ordered and unique.")
    numeric = pd.to_numeric(frame.iloc[:, 0], errors="coerce")
    distinct = set(numeric.dropna().astype(float).tolist())
    if numeric.isna().any() or not distinct.issubset({0.0, 1.0}):
        raise ValueError(
            "Provider membership series contains null, non-numeric, or "
            f"non-binary values: {sorted(distinct)}"
        )
    return (
        [value.date().isoformat() for value in dates],
        numeric.astype(int).tolist(),
    )


def enrich_candidate_pit_membership(
    provider: Any,
    policy: UniversePolicy,
    rows: Sequence[Mapping[str, Any]],
    *,
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Verify index overlap without claiming point-in-time sector taxonomy.

    Candidate classification comes from Norgate's current/final GICS
    snapshot. Even exact index membership therefore cannot establish a
    survivorship-complete Consumer Defensive panel. Outputs remain a review
    queue and are explicitly prohibited from calibration or production use.
    """

    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("PIT membership dates must use YYYY-MM-DD.") from exc
    if start > end:
        raise ValueError("PIT membership start_date cannot exceed end_date.")

    vehicles = approved_watchlists(policy)
    fingerprint = norgate_database_fingerprint(
        provider, NORGATE_MEMBERSHIP_DATABASES
    )
    candidates = [
        dict(row)
        for row in rows
        if int(row.get("candidate_consumer_defensive") or 0) == 1
    ]
    output: list[dict[str, Any]] = []
    for source in candidates:
        row = dict(source)
        symbol = str(row.get("provider_symbol") or "")
        vehicle_ids = [
            value
            for value in str(row.get("approved_vehicle_ids") or "").split(";")
            if value
        ]
        member_dates: set[str] = set()
        member_vehicles: list[str] = []
        errors: list[str] = []
        query_row_count = 0
        for vehicle_id in vehicle_ids:
            config = vehicles.get(vehicle_id)
            if config is None:
                errors.append(f"unknown_approved_vehicle:{vehicle_id}")
                continue
            try:
                frame = provider.index_constituent_timeseries(
                    symbol,
                    config["index_name"],
                    start_date=start_date,
                    end_date=end_date,
                    timeseriesformat="pandas-dataframe",
                )
                dates, flags = membership_dates_flags(frame)
                query_row_count += len(dates)
                flagged = {
                    value
                    for value, flag in zip(dates, flags, strict=True)
                    if flag == 1
                }
                if flagged:
                    member_vehicles.append(vehicle_id)
                    member_dates.update(flagged)
            except Exception as exc:
                errors.append(f"{vehicle_id}:{type(exc).__name__}:{exc}")
        ordered_dates = sorted(member_dates)
        row.update(
            {
                "pit_membership_window_start": start_date,
                "pit_membership_window_end": end_date,
                "pit_index_membership_query_row_count": query_row_count,
                "pit_index_membership_overlap_flag": int(bool(ordered_dates)),
                "pit_index_membership_first_date": (
                    ordered_dates[0] if ordered_dates else ""
                ),
                "pit_index_membership_last_date": (
                    ordered_dates[-1] if ordered_dates else ""
                ),
                "pit_index_membership_session_count": len(ordered_dates),
                "pit_index_membership_vehicle_ids": ";".join(
                    sorted(member_vehicles)
                ),
                "pit_index_membership_query_error": ";".join(errors),
                "pit_index_membership_overlap_verified": int(not errors),
                "point_in_time_taxonomy_verified": 0,
                "identity_review_required": 1,
                "candidate_discovery_only": 1,
                "pit_membership_verified": 0,
            }
        )
        if errors:
            row["status"] = "pit_membership_query_error_review_required"
        elif ordered_dates:
            row["status"] = "pit_membership_overlap_review_required"
        else:
            row["status"] = "no_pit_membership_overlap_in_window"
        output.append(row)

    fingerprint_end = require_norgate_snapshot(
        provider,
        fingerprint,
        context="during historical-census PIT membership enrichment",
    )
    status_counts = Counter(str(row["status"]) for row in output)
    overlap_count = sum(
        int(row["pit_index_membership_overlap_flag"]) for row in output
    )
    error_count = sum(
        bool(row["pit_index_membership_query_error"]) for row in output
    )
    return output, {
        "status": "PIT_INDEX_MEMBERSHIP_ENRICHED_REVIEW_ONLY",
        "membership_window_start": start_date,
        "membership_window_end": end_date,
        "candidate_count": len(output),
        "pit_index_membership_overlap_count": overlap_count,
        "no_pit_index_membership_overlap_count": len(output) - overlap_count,
        "pit_index_membership_query_error_count": error_count,
        "point_in_time_index_membership_queried": True,
        "point_in_time_taxonomy_verified": False,
        "point_in_time_survivorship_complete": False,
        "production_or_calibration_use_allowed": False,
        "classification_time_basis": CLASSIFICATION_TIME_BASIS,
        "provider_database_updated_at": fingerprint,
        "provider_database_updated_at_end": fingerprint_end,
        "status_counts": dict(sorted(status_counts.items())),
    }


__all__ = ["enrich_candidate_pit_membership", "membership_dates_flags"]
