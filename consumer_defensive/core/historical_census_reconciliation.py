"""Read-only reconciliation of historical candidates to reviewed identities."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence


def _loaded_norgate_identities(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT i.identifier_value,s.ticker,s.company_id,s.security_id
           FROM dim_identifier AS i
           JOIN dim_security AS s ON s.security_id=i.security_id
           WHERE i.identifier_type='norgate_assetid'
           ORDER BY i.identifier_value,s.ticker"""
    ).fetchall()
    output: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for raw in rows:
        asset_id = str(raw[0] or "")
        value = {
            "loaded_ticker": str(raw[1] or ""),
            "loaded_company_id": int(raw[2]),
            "loaded_security_id": int(raw[3]),
        }
        if not asset_id:
            raise RuntimeError("Loaded Norgate identity has a blank asset ID.")
        if asset_id in output and output[asset_id] != value:
            duplicates.add(asset_id)
        output[asset_id] = value
    if duplicates:
        raise RuntimeError(
            "Norgate asset IDs map to multiple loaded securities: "
            f"{sorted(duplicates)}"
        )
    return output


def reconcile_historical_candidates(
    conn: Any,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Classify PIT-overlap candidates without mutating or auto-approving them."""

    loaded = _loaded_norgate_identities(conn)
    seen_assets: set[str] = set()
    duplicates: set[str] = set()
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        asset_id = str(row.get("provider_asset_id") or "")
        if not asset_id:
            row["reconciliation_status"] = "missing_provider_asset_id"
            row["loaded_identity_match_flag"] = 0
            row["unloaded_candidate_review_flag"] = 0
            row["production_or_calibration_use_allowed"] = 0
            output.append(row)
            continue
        if asset_id in seen_assets:
            duplicates.add(asset_id)
        seen_assets.add(asset_id)
        match = loaded.get(asset_id)
        overlap = int(row.get("pit_index_membership_overlap_flag") or 0) == 1
        row.update(
            {
                "loaded_identity_match_flag": int(match is not None),
                "loaded_ticker": match["loaded_ticker"] if match else "",
                "loaded_company_id": match["loaded_company_id"] if match else "",
                "loaded_security_id": match["loaded_security_id"] if match else "",
                "unloaded_candidate_review_flag": int(overlap and match is None),
                "point_in_time_taxonomy_verified": 0,
                "identity_review_required": int(match is None),
                "production_or_calibration_use_allowed": 0,
            }
        )
        if not overlap:
            row["reconciliation_status"] = "outside_pit_membership_window"
        elif match is not None:
            row["reconciliation_status"] = "already_loaded_identity"
        else:
            row["reconciliation_status"] = (
                "unloaded_identity_and_taxonomy_review_required"
            )
        output.append(row)
    if duplicates:
        raise RuntimeError(
            "Candidate census contains duplicate Norgate asset IDs: "
            f"{sorted(duplicates)}"
        )
    counts = Counter(str(row["reconciliation_status"]) for row in output)
    unloaded = [
        row for row in output if int(row["unloaded_candidate_review_flag"]) == 1
    ]
    unloaded_by_industry = Counter(
        str(row.get("gics_industry_current_or_final") or "UNCLASSIFIED")
        for row in unloaded
    )
    unloaded_by_catalog_status = Counter(
        str(row.get("catalog_status") or "UNKNOWN") for row in unloaded
    )
    return output, {
        "status": "HISTORICAL_CENSUS_RECONCILIATION_REVIEW_ONLY",
        "candidate_count": len(output),
        "pit_membership_overlap_count": sum(
            int(row.get("pit_index_membership_overlap_flag") or 0)
            for row in output
        ),
        "already_loaded_identity_count": sum(
            int(row["loaded_identity_match_flag"]) for row in output
        ),
        "loaded_pit_overlap_identity_count": sum(
            int(row["loaded_identity_match_flag"])
            * int(row.get("pit_index_membership_overlap_flag") or 0)
            for row in output
        ),
        "unloaded_candidate_review_count": sum(
            int(row["unloaded_candidate_review_flag"]) for row in output
        ),
        "unloaded_candidate_count_by_industry": dict(
            sorted(unloaded_by_industry.items())
        ),
        "unloaded_candidate_count_by_catalog_status": dict(
            sorted(unloaded_by_catalog_status.items())
        ),
        "point_in_time_taxonomy_verified": False,
        "survivorship_corrected_panel_ready": False,
        "database_write_count": 0,
        "production_or_calibration_use_allowed": False,
        "status_counts": dict(sorted(counts.items())),
    }


__all__ = ["reconcile_historical_candidates"]
