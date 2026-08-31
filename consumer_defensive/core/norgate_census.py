"""Read-only discovery of historical Consumer Defensive census candidates.

Norgate's ``Current & Past`` watchlists provide a useful *superset* of
securities that were constituents of the approved index vehicles.  The
classification endpoint, however, has no as-of-date parameter.  Therefore
this module deliberately produces candidates for review only; it must not be
used to load universe membership or to represent survivorship as complete.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from consumer_defensive.core.norgate_runtime import (
    NORGATE_MEMBERSHIP_DATABASES,
    norgate_database_fingerprint,
    require_norgate_snapshot,
)
from consumer_defensive.core.universe import UniversePolicy


GICS_SCHEME = "GICS"
GICS_RESULT_TYPE = "name"
GICS_SECTOR_LEVEL = 1
GICS_CONSUMER_STAPLES = "Consumer Staples"
CLASSIFICATION_TIME_BASIS = "provider_current_or_final_snapshot_no_asof_parameter"


def approved_watchlists(policy: UniversePolicy) -> dict[str, dict[str, str]]:
    """Return the reviewed vehicle identifiers and provider watchlists."""
    return {
        str(row["vehicle_id"]): {
            "index_name": str(row["norgate_index_name"]),
            "watchlist": str(row["norgate_watchlist_name"]),
        }
        for row in policy.payload["approved_membership_vehicles"]
    }


def catalog_status(symbol: str, active_symbols: set[str], delisted_symbols: set[str]) -> str:
    """Return a deliberately explicit provider-catalog status."""
    active = symbol in active_symbols
    delisted = symbol in delisted_symbols
    if active and delisted:
        return "active_and_delisted_catalog_collision"
    if active:
        return "active"
    if delisted:
        return "delisted"
    return "absent_from_equity_catalogs"


def _safe_metadata(provider: Any, symbol: str) -> tuple[dict[str, str], str]:
    try:
        return (
            {
                "provider_asset_id": str(provider.assetid(symbol) or ""),
                "provider_security_name": str(provider.security_name(symbol) or ""),
                "first_quoted_date": str(provider.first_quoted_date(symbol) or ""),
                "last_quoted_date": str(provider.last_quoted_date(symbol) or ""),
            },
            "",
        )
    except Exception as exc:  # Provider metadata varies for historical symbols.
        return {}, f"metadata_error:{type(exc).__name__}:{exc}"


def _safe_gics(provider: Any, symbol: str) -> tuple[dict[str, str], str]:
    try:
        return (
            {
                "gics_industry_current_or_final": str(
                    provider.classification(symbol, GICS_SCHEME, GICS_RESULT_TYPE) or ""
                ),
                "gics_sector_current_or_final": str(
                    provider.classification_at_level(
                        symbol,
                        GICS_SCHEME,
                        GICS_RESULT_TYPE,
                        GICS_SECTOR_LEVEL,
                    )
                    or ""
                ),
            },
            "",
        )
    except Exception as exc:
        return {}, f"gics_classification_error:{type(exc).__name__}:{exc}"


def discover_candidate_census(
    provider: Any,
    policy: UniversePolicy,
    *,
    max_symbols: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Enumerate and sector-filter a non-PIT review queue from Norgate.

    ``max_symbols`` exists solely for cheap entitlement/smoke checks.  A
    limited run is marked incomplete in the summary and cannot be treated as
    a census.
    """
    if max_symbols is not None and max_symbols <= 0:
        raise ValueError("max_symbols must be a positive integer when supplied.")

    watchlists = approved_watchlists(policy)
    fingerprint = norgate_database_fingerprint(provider, NORGATE_MEMBERSHIP_DATABASES)
    active_symbols = set(provider.database_symbols("US Equities"))
    delisted_symbols = set(provider.database_symbols("US Equities Delisted"))
    symbols_by_watchlist = {
        vehicle_id: sorted(set(provider.watchlist_symbols(config["watchlist"])))
        for vehicle_id, config in watchlists.items()
    }
    require_norgate_snapshot(
        provider,
        fingerprint,
        context="while reading historical-census catalogs and watchlists",
    )

    watchlists_by_symbol: defaultdict[str, list[str]] = defaultdict(list)
    for vehicle_id, symbols in symbols_by_watchlist.items():
        for symbol in symbols:
            watchlists_by_symbol[symbol].append(vehicle_id)
    all_symbols = sorted(watchlists_by_symbol)
    selected_symbols = all_symbols if max_symbols is None else all_symbols[:max_symbols]

    rows: list[dict[str, Any]] = []
    for symbol in selected_symbols:
        status = catalog_status(symbol, active_symbols, delisted_symbols)
        vehicle_ids = sorted(watchlists_by_symbol[symbol])
        row: dict[str, Any] = {
            "provider_symbol": symbol,
            "catalog_status": status,
            "approved_vehicle_ids": ";".join(vehicle_ids),
            "approved_watchlists": ";".join(
                watchlists[vehicle_id]["watchlist"] for vehicle_id in vehicle_ids
            ),
            "classification_time_basis": CLASSIFICATION_TIME_BASIS,
            "classification_is_point_in_time": 0,
            "pit_membership_verified": 0,
            "candidate_discovery_only": 1,
            "candidate_consumer_defensive": 0,
            "status": "review_required",
            "issue": "",
        }
        if status in {"absent_from_equity_catalogs", "active_and_delisted_catalog_collision"}:
            row["status"] = "catalog_anomaly"
            row["issue"] = status
            rows.append(row)
            continue
        metadata, metadata_issue = _safe_metadata(provider, symbol)
        row.update(metadata)
        gics, gics_issue = _safe_gics(provider, symbol)
        row.update(gics)
        issues = [issue for issue in (metadata_issue, gics_issue) if issue]
        if issues:
            row["status"] = "provider_metadata_or_classification_error"
            row["issue"] = ";".join(issues)
        elif row["gics_sector_current_or_final"] == GICS_CONSUMER_STAPLES:
            row["candidate_consumer_defensive"] = 1
            row["status"] = "candidate_review_required"
        else:
            row["status"] = "outside_current_or_final_gics_sector"
        rows.append(row)

    fingerprint_end = require_norgate_snapshot(
        provider,
        fingerprint,
        context="during historical-census candidate discovery",
    )
    status_counts = Counter(str(row["status"]) for row in rows)
    catalog_counts = Counter(str(row["catalog_status"]) for row in rows)
    summary: dict[str, Any] = {
        "status": "CANDIDATE_DISCOVERY_ONLY",
        "point_in_time_survivorship_complete": False,
        "classification_is_point_in_time": False,
        "classification_time_basis": CLASSIFICATION_TIME_BASIS,
        "provider_database_updated_at": fingerprint,
        "provider_database_updated_at_end": fingerprint_end,
        "watchlist_counts": {
            vehicle_id: len(symbols) for vehicle_id, symbols in symbols_by_watchlist.items()
        },
        "watchlist_union_count": len(all_symbols),
        "symbols_examined": len(rows),
        "complete_union_examined": len(rows) == len(all_symbols),
        "max_symbols": max_symbols,
        "catalog_status_counts": dict(sorted(catalog_counts.items())),
        "candidate_consumer_defensive_count": sum(
            int(row["candidate_consumer_defensive"]) for row in rows
        ),
        "status_counts": dict(sorted(status_counts.items())),
    }
    return rows, summary


__all__ = [
    "CLASSIFICATION_TIME_BASIS",
    "GICS_CONSUMER_STAPLES",
    "approved_watchlists",
    "catalog_status",
    "discover_candidate_census",
]
