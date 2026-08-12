from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from consumer_defensive.core.universe import (
    MODEL_FAMILY,
    PIT_SOURCE_ID,
    UniversePolicy,
    normalize_ticker,
    read_csv,
    validate_current_rows,
)
from consumer_defensive.core.terminal_events import (
    load_terminal_event_ledger,
    load_terminal_event_policy,
)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def _complete_vehicle_series_count(
    conn: sqlite3.Connection,
    tickers: list[str],
    expected_vehicle_ids: set[str],
    listing_status: str,
) -> int:
    """Count securities with one approved-vehicle row for every provider listing date."""

    if not tickers:
        return 0
    placeholders = ','.join('?' for _ in tickers)
    securities = conn.execute(
        f'''SELECT security_id, ticker FROM dim_security
            WHERE ticker IN ({placeholders}) AND listing_status=?''',
        (*tickers, listing_status),
    ).fetchall()
    complete = 0
    for security_id, _ticker in securities:
        listing_dates = {
            str(row[0])
            for row in conn.execute(
                '''SELECT listing_date FROM fact_major_exchange_listing_daily
                   WHERE security_id=? AND source_id=?''',
                (int(security_id), PIT_SOURCE_ID),
            )
        }
        if not listing_dates:
            continue
        membership_dates: dict[str, set[str]] = {}
        for vehicle_id, membership_date in conn.execute(
            '''SELECT vehicle_id, membership_date
               FROM fact_recognized_vehicle_membership_daily
               WHERE security_id=? AND source_id=?''',
            (int(security_id), PIT_SOURCE_ID),
        ):
            membership_dates.setdefault(str(vehicle_id), set()).add(str(membership_date))
        if set(membership_dates) == expected_vehicle_ids and all(
            dates == listing_dates for dates in membership_dates.values()
        ):
            complete += 1
    return complete


def validate_stage2(
    conn: sqlite3.Connection,
    policy: UniversePolicy,
    *,
    current_csv: Path | None = None,
    require_pit_membership: bool = True,
) -> dict[str, Any]:
    path = current_csv.expanduser().resolve() if current_csv else policy.resolve("authoritative_current_csv")
    csv_rows = read_csv(path)
    validate_current_rows(csv_rows, policy)
    tickers = sorted(normalize_ticker(row["ticker"]) for row in csv_rows)
    placeholders = ",".join("?" for _ in tickers)
    params = tuple(tickers)
    errors: list[str] = []
    warnings: list[str] = []

    expected = len(tickers)
    company_count = _scalar(
        conn,
        f"SELECT COUNT(*) FROM dim_company WHERE primary_ticker IN ({placeholders}) AND is_active=1",
        params,
    )
    security_count = _scalar(
        conn,
        f"SELECT COUNT(*) FROM dim_security WHERE ticker IN ({placeholders}) AND listing_status='active'",
        params,
    )
    taxonomy_count = _scalar(
        conn,
        f"""
        SELECT COUNT(*) FROM dim_consumer_defensive_taxonomy
        WHERE ticker IN ({placeholders}) AND model_family=?
        """,
        (*params, MODEL_FAMILY),
    )
    for name, actual in (
        ("active companies", company_count),
        ("active securities", security_count),
        ("taxonomy rows", taxonomy_count),
    ):
        if actual != expected:
            errors.append(f"{name}: expected={expected} actual={actual}")

    cohort_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"""
            SELECT calibration_cohort_id, COUNT(*)
            FROM dim_consumer_defensive_taxonomy
            WHERE ticker IN ({placeholders}) AND model_family=?
            GROUP BY calibration_cohort_id
            """,
            (*params, MODEL_FAMILY),
        ).fetchall()
    }
    for industry, config in policy.payload["cohorts"].items():
        cohort_id = str(config["cohort_id"])
        actual = cohort_counts.get(cohort_id, 0)
        expected_cohort = int(config["expected_current_rows"])
        if actual != expected_cohort:
            errors.append(f"cohort {cohort_id}: expected={expected_cohort} actual={actual}")

    security_duplicates = conn.execute(
        """
        SELECT ticker, COUNT(*) FROM dim_security
        WHERE listing_status='active' GROUP BY ticker HAVING COUNT(*) > 1
        """
    ).fetchall()
    if security_duplicates:
        errors.append(f"multiple live security rows: {[tuple(row) for row in security_duplicates]}")
    taxonomy_duplicates = conn.execute(
        """
        SELECT ticker, COUNT(*) FROM dim_consumer_defensive_taxonomy
        WHERE model_family=? GROUP BY ticker HAVING COUNT(*) > 1
        """,
        (MODEL_FAMILY,),
    ).fetchall()
    if taxonomy_duplicates:
        errors.append(f"duplicate taxonomy rows: {[tuple(row) for row in taxonomy_duplicates]}")

    separate_alias_collisions = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM dim_security s
        WHERE s.ticker IN ('CCE','DPS')
        """,
    )
    if separate_alias_collisions:
        errors.append("CCE/DPS were loaded as separate securities instead of lineage aliases.")
    alias_map = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            "SELECT alias_ticker, canonical_ticker FROM dim_security_alias WHERE alias_ticker IN ('CCE','DPS')"
        ).fetchall()
    }
    if alias_map != {"CCE": "CCEP", "DPS": "KDP"}:
        errors.append(f"lineage alias map is incomplete or wrong: {alias_map}")

    provider_assets = conn.execute(
        """
        SELECT identifier_value, COUNT(DISTINCT security_id)
        FROM dim_identifier WHERE identifier_type='norgate_assetid'
        GROUP BY identifier_value HAVING COUNT(DISTINCT security_id) > 1
        """
    ).fetchall()
    if provider_assets:
        errors.append(f"Norgate asset IDs map to multiple securities: {[tuple(row) for row in provider_assets]}")

    vehicle_ids = {
        str(row[0])
        for row in conn.execute("SELECT vehicle_id FROM dim_recognized_vehicle WHERE is_active=1").fetchall()
    }
    expected_vehicle_ids = {
        str(row["vehicle_id"]) for row in policy.payload["approved_membership_vehicles"]
    }
    if vehicle_ids != expected_vehicle_ids:
        errors.append(
            f"approved vehicle mismatch: expected={sorted(expected_vehicle_ids)} actual={sorted(vehicle_ids)}"
        )

    source = conn.execute(
        "SELECT status, source_type FROM source_registry WHERE source_id=?", (PIT_SOURCE_ID,)
    ).fetchone()
    if source is None or str(source[0]) != "active":
        errors.append("The authoritative Norgate PIT membership source is absent or inactive.")

    current_membership_count = _scalar(
        conn,
        f"""
        SELECT COUNT(DISTINCT ticker) FROM dim_universe_membership
        WHERE ticker IN ({placeholders}) AND model_family=? AND membership_source_id=?
          AND membership_basis='recognized_index_union' AND point_in_time_flag=1
          AND is_current_member=1 AND live_investable_flag=1
        """,
        (*params, MODEL_FAMILY, PIT_SOURCE_ID),
    )
    current_asset_count = _scalar(
        conn,
        f"""
        SELECT COUNT(DISTINCT s.ticker)
        FROM dim_security s
        JOIN dim_identifier i ON i.security_id=s.security_id AND i.identifier_type='norgate_assetid'
        WHERE s.ticker IN ({placeholders}) AND s.listing_status='active'
        """,
        params,
    )
    daily_series_count = _complete_vehicle_series_count(
        conn,
        tickers,
        expected_vehicle_ids,
        'active',
    )
    terminal_policy = load_terminal_event_policy(policy.resolve("terminal_event_policy"))
    expected_historical_tickers = sorted(
        event.ticker for event in load_terminal_event_ledger(terminal_policy)
    )
    configured_historical = int(policy.payload["expected_historical_identifier_rows"])
    if len(expected_historical_tickers) != configured_historical:
        errors.append(
            "authoritative historical candidate count differs from the reviewed "
            f"identifier contract: terminal_events={len(expected_historical_tickers)} "
            f"configured={configured_historical}"
        )
    historical_placeholders = ",".join("?" for _ in expected_historical_tickers)
    historical_params = tuple(expected_historical_tickers)
    loaded_historical_tickers = {
        str(row[0])
        for row in conn.execute(
            """SELECT t.ticker
               FROM dim_consumer_defensive_taxonomy t
               JOIN dim_security s ON s.security_id=t.security_id
               JOIN dim_company c ON c.company_id=t.company_id
               WHERE t.model_family=? AND s.listing_status='delisted'
                 AND c.is_active=0""",
            (MODEL_FAMILY,),
        )
    }
    expected_historical_set = set(expected_historical_tickers)
    historical_missing = sorted(expected_historical_set - loaded_historical_tickers)
    historical_extra = sorted(loaded_historical_tickers - expected_historical_set)
    historical_taxonomy_count = len(expected_historical_set & loaded_historical_tickers)
    historical_asset_count = 0
    historical_daily_series_count = 0
    historical_recognized_count = 0
    if expected_historical_tickers:
        historical_asset_count = _scalar(
            conn,
            f"""SELECT COUNT(DISTINCT s.ticker)
                FROM dim_security s
                JOIN dim_identifier i
                  ON i.security_id=s.security_id
                 AND i.identifier_type='norgate_assetid'
                WHERE s.ticker IN ({historical_placeholders})
                  AND s.listing_status='delisted'""",
            historical_params,
        )
        historical_daily_series_count = _complete_vehicle_series_count(
            conn,
            expected_historical_tickers,
            expected_vehicle_ids,
            'delisted',
        )
        historical_recognized_count = _scalar(
            conn,
            f"""SELECT COUNT(DISTINCT s.ticker)
                FROM dim_security s
                JOIN fact_recognized_vehicle_membership_daily m
                  ON m.security_id=s.security_id AND m.member_flag=1
                JOIN fact_major_exchange_listing_daily x
                  ON x.security_id=s.security_id
                 AND x.listing_date=m.membership_date
                 AND x.major_exchange_listed_flag=1
                WHERE s.ticker IN ({historical_placeholders})
                  AND s.listing_status='delisted'""",
            historical_params,
        )
    if require_pit_membership:
        if current_membership_count != expected:
            errors.append(
                f"recognized current membership: expected={expected} actual={current_membership_count}"
            )
        if current_asset_count != expected:
            errors.append(f"Norgate asset identities: expected={expected} actual={current_asset_count}")
        if daily_series_count != expected:
            errors.append(f"four-index daily series: expected={expected} actual={daily_series_count}")
        if historical_missing or historical_extra:
            errors.append(
                "historical candidate taxonomy scope mismatch: "
                f"missing={historical_missing} extra={historical_extra}"
            )
        if historical_asset_count != len(expected_historical_tickers):
            errors.append(
                "historical Norgate asset identities: "
                f"expected={len(expected_historical_tickers)} "
                f"actual={historical_asset_count}"
            )
        if historical_daily_series_count != len(expected_historical_tickers):
            errors.append(
                "historical four-index daily series: "
                f"expected={len(expected_historical_tickers)} "
                f"actual={historical_daily_series_count}"
            )
        if historical_recognized_count != len(expected_historical_tickers):
            errors.append(
                "historical recognized-membership coverage: "
                f"expected={len(expected_historical_tickers)} "
                f"actual={historical_recognized_count}"
            )
    else:
        warnings.append("PIT membership was not required for this identity-only validation run.")

    unresolved_terminal = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM fact_security_event
        WHERE survivorship_complete=0
        """,
    )
    if unresolved_terminal:
        warnings.append(
            f"{unresolved_terminal} security event(s) remain terminal-return incomplete and are not calibration-ready."
        )

    expected_cohort_ids = sorted(
        {str(config['cohort_id']) for config in policy.payload['cohorts'].values()}
    )
    cohort_values = ','.join('(?)' for _ in expected_cohort_ids)
    breadth = conn.execute(
        f"""
        WITH cohorts(calibration_cohort_id) AS (VALUES {cohort_values}),
        provider_dates AS (
            SELECT DISTINCT listing_date AS membership_date
            FROM fact_major_exchange_listing_daily
            WHERE source_id=?
        ), union_membership AS (
            SELECT security_id, membership_date, MAX(member_flag) AS any_member
            FROM fact_recognized_vehicle_membership_daily
            WHERE source_id=?
            GROUP BY security_id, membership_date
        ), counts AS (
            SELECT u.membership_date, t.calibration_cohort_id, COUNT(*) AS n
            FROM union_membership u
            JOIN fact_major_exchange_listing_daily x
              ON x.security_id=u.security_id AND x.listing_date=u.membership_date
            JOIN dim_consumer_defensive_taxonomy t ON t.security_id=u.security_id
            WHERE u.any_member=1 AND x.major_exchange_listed_flag=1
              AND x.source_id=?
            GROUP BY u.membership_date, t.calibration_cohort_id
        ), grid AS (
            SELECT d.membership_date, c.calibration_cohort_id
            FROM provider_dates d CROSS JOIN cohorts c
        )
        SELECT g.calibration_cohort_id,
               MIN(COALESCE(n, 0)) AS minimum_count,
               SUM(CASE WHEN COALESCE(n, 0) < 20 THEN 1 ELSE 0 END) AS dates_below_20,
               COUNT(*) AS observed_dates
        FROM grid g
        LEFT JOIN counts c
          ON c.membership_date=g.membership_date
         AND c.calibration_cohort_id=g.calibration_cohort_id
        GROUP BY g.calibration_cohort_id ORDER BY g.calibration_cohort_id
        """,
        (*expected_cohort_ids, PIT_SOURCE_ID, PIT_SOURCE_ID, PIT_SOURCE_ID),
    ).fetchall()
    breadth_summary = [
        {
            "cohort_id": str(row[0]),
            "minimum_count": int(row[1]),
            "dates_below_20": int(row[2]),
            "observed_dates": int(row[3]),
        }
        for row in breadth
    ]
    below_target = [row for row in breadth_summary if row["dates_below_20"] > 0]
    if below_target:
        warnings.append(
            "Cohort breadth below 20 is diagnostic, not a Stage 2 failure: "
            + json.dumps(below_target, sort_keys=True)
        )

    cross_sector = conn.execute(
        """
        SELECT ticker, sector, portfolio_sector FROM dim_consumer_defensive_taxonomy
        WHERE sector <> 'Consumer Defensive' OR portfolio_sector <> 'Consumer Staples'
        """
    ).fetchall()
    if cross_sector:
        errors.append(f"cross-sector taxonomy contamination: {[tuple(row) for row in cross_sector]}")

    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "warnings": warnings,
        "current_rows": expected,
        "active_company_rows": company_count,
        "active_security_rows": security_count,
        "taxonomy_rows": taxonomy_count,
        "recognized_current_members": current_membership_count,
        "norgate_asset_identities": current_asset_count,
        "complete_four_index_daily_series": daily_series_count,
        "historical_candidates_expected": len(expected_historical_tickers),
        "historical_taxonomy_rows": historical_taxonomy_count,
        "historical_norgate_asset_identities": historical_asset_count,
        "historical_complete_four_index_daily_series": historical_daily_series_count,
        "historical_recognized_members": historical_recognized_count,
        "cohort_counts": dict(sorted(cohort_counts.items())),
        "cohort_breadth": breadth_summary,
    }
