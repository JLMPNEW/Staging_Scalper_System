from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from consumer_defensive.core.market_data import (
    NORGATE_SOURCE_ID,
    SELECTION_PURPOSE,
    YAHOO_SOURCE_ID,
    MarketDataPolicy,
    coverage_policy_kwargs,
    coverage_qualifies,
    current_tickers,
    point_in_time_feature_tickers,
    price_coverage,
    required_price_coverage_window,
    security_rows,
)
from consumer_defensive.core.universe import MODEL_FAMILY
from consumer_defensive.core.terminal_events import TerminalEventPolicy, load_terminal_event_policy, validate_terminal_events
from consumer_defensive.core.stage3_runtime import DEFAULT_TERMINAL_POLICY


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row is not None else 0


def validate_stage3_market_data(
    conn: sqlite3.Connection,
    policy: MarketDataPolicy,
    *,
    as_of: str,
    expected_active: int,
    terminal_policy: TerminalEventPolicy | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str, *, severity: str = "error") -> None:
        checks.append({"check": name, "status": "PASS" if passed else ("WARN" if severity == "warning" else "FAIL"), "detail": detail})
        if not passed:
            (warnings if severity == "warning" else errors).append(f"{name}: {detail}")

    active = current_tickers(conn)
    securities = security_rows(conn)
    history_start = str(policy.payload["history_start"])
    relevant = [
        row
        for row in securities
        if str(row.get("listing_start_date") or history_start) <= as_of
        and str(row.get("listing_end_date") or as_of) >= history_start
    ]
    relevant_tickers = {str(row["ticker"]) for row in relevant}
    delisted = sorted(
        str(row["ticker"]) for row in relevant if not int(row.get("is_active") or 0)
    )
    active_relevant = sorted(set(active) & relevant_tickers)
    check("active_security_count", len(active) == expected_active, f"observed={len(active)} expected={expected_active}")

    selection_rows = conn.execute(
        """
        SELECT ticker, selected_source_id, selection_asof_date, coverage_status,
               first_bar_date, last_bar_date, bar_count, adjustment_basis
        FROM dim_price_series_selection WHERE purpose=? ORDER BY ticker
        """,
        (SELECTION_PURPOSE,),
    ).fetchall()
    selections = {str(row["ticker"]): dict(row) for row in selection_rows}
    expected_selection = set(active_relevant) | set(delisted) | {"XLP", "SPY"}
    check(
        "one_selection_per_required_ticker",
        set(selections) == expected_selection,
        f"observed={len(selections)} expected={len(expected_selection)} missing={sorted(expected_selection-set(selections))} extra={sorted(set(selections)-expected_selection)}",
    )
    wrong_asof = sorted(ticker for ticker, row in selections.items() if row["selection_asof_date"] != as_of)
    check("selection_asof_date", not wrong_asof, f"wrong={wrong_asof}")
    benchmark_bad = sorted(
        ticker for ticker in ("XLP", "SPY")
        if selections.get(ticker, {}).get("selected_source_id") != YAHOO_SOURCE_ID
    )
    check("benchmarks_use_yahoo", not benchmark_bad, f"wrong={benchmark_bad}")
    delisted_bad = sorted(
        ticker for ticker in delisted
        if selections.get(ticker, {}).get("selected_source_id") != NORGATE_SOURCE_ID
    )
    check("delisted_use_mandatory_norgate", not delisted_bad, f"wrong={delisted_bad}")
    active_bad = sorted(
        ticker for ticker in active_relevant
        if selections.get(ticker, {}).get("selected_source_id") not in {YAHOO_SOURCE_ID, NORGATE_SOURCE_ID}
    )
    check("active_source_allowed", not active_bad, f"wrong={active_bad}")
    weak = sorted(ticker for ticker, row in selections.items() if row["coverage_status"] not in {"complete", "fallback"})
    check("selected_coverage_status", not weak, f"weak={weak}")

    audit_failures = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT ticker FROM fact_market_data_audit
            WHERE audit_asof_date=? AND coverage_status NOT IN ('complete','fallback')
            ORDER BY ticker
            """,
            (as_of,),
        ).fetchall()
    ]
    audit_count = _scalar(conn, "SELECT COUNT(*) FROM fact_market_data_audit WHERE audit_asof_date=?", (as_of,))
    check("market_audit_complete", audit_count == len(expected_selection) and not audit_failures, f"rows={audit_count} failures={audit_failures}")

    snapshot = str(policy.payload["requested_snapshot_start"])
    snapshot_day = date.fromisoformat(snapshot)
    snapshot_members = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT s.ticker
            FROM fact_recognized_vehicle_membership_daily m
            JOIN fact_major_exchange_listing_daily x
              ON x.security_id=m.security_id AND x.listing_date=m.membership_date
            JOIN dim_security s ON s.security_id=m.security_id
            WHERE m.membership_date=? AND m.member_flag=1
              AND x.major_exchange_listed_flag=1
            ORDER BY s.ticker
            """,
            (snapshot,),
        ).fetchall()
    ]
    check("first_snapshot_has_pit_members", bool(snapshot_members), f"members={len(snapshot_members)}")
    minimum_partial = int(policy.payload["selection"]["minimum_rows_partial"])
    minimum_snapshot = int(policy.payload["selection"]["first_snapshot_minimum_observations"])
    snapshot_failures: list[str] = []
    recent_listing_partial: list[str] = []
    snapshot_calendar = tuple(
        str(row[0])
        for row in conn.execute(
            '''SELECT trading_date FROM (
                   SELECT bar_date AS trading_date FROM fact_price_ohlcv
                   WHERE ticker='SPY' AND source_id=? AND bar_date<=?
                   UNION
                   SELECT listing_date AS trading_date
                   FROM fact_major_exchange_listing_daily
                   WHERE listing_date<=?
               ) ORDER BY trading_date''',
            (YAHOO_SOURCE_ID, snapshot, snapshot),
        )
    )
    security_by_ticker = {
        str(row['ticker']): row for row in security_rows(conn)
    }
    settings = policy.payload['selection']
    coverage_kwargs = coverage_policy_kwargs(policy)
    for ticker in snapshot_members:
        security = security_by_ticker.get(ticker)
        coverage_window = (
            required_price_coverage_window(security, policy, as_of=snapshot)
            if security is not None
            else None
        )
        if coverage_window is None:
            snapshot_failures.append(f'{ticker}:no_pit_relevant_coverage_window')
            continue
        expected_start, expected_end = coverage_window
        source_id = ''
        for candidate_source in (YAHOO_SOURCE_ID, NORGATE_SOURCE_ID):
            candidate = price_coverage(
                conn,
                ticker,
                candidate_source,
                start=expected_start,
                end=expected_end,
                include_dates=True,
            )
            if coverage_qualifies(
                candidate,
                expected_start=expected_start,
                expected_end=expected_end,
                start_tolerance_days=int(settings['start_tolerance_calendar_days']),
                end_tolerance_days=int(settings['active_max_staleness_calendar_days']),
                minimum_rows=minimum_snapshot,
                expected_dates=snapshot_calendar,
                **coverage_kwargs,
            ):
                source_id = candidate_source
                break
        if not source_id:
            snapshot_failures.append(f'{ticker}:no_qualifying_source_at_snapshot')
            continue
        row = conn.execute(
            """
            SELECT COUNT(*), MAX(bar_date) FROM fact_price_ohlcv
            WHERE ticker=? AND source_id=? AND bar_date<=? AND adjusted_close>0
            """,
            (ticker, source_id, snapshot),
        ).fetchone()
        rows_before = int(row[0] or 0)
        last_before = str(row[1] or "")
        if rows_before < minimum_snapshot:
            snapshot_failures.append(f"{ticker}:no_price_history")
        elif not last_before or date.fromisoformat(last_before) < snapshot_day - timedelta(days=7):
            snapshot_failures.append(f"{ticker}:last_bar={last_before}")
        elif rows_before < minimum_partial:
            recent_listing_partial.append(f"{ticker}:history_rows={rows_before}")
    check("first_snapshot_member_price_access", not snapshot_failures, f"failures={snapshot_failures}")
    check(
        "first_snapshot_recent_listing_lookback",
        not recent_listing_partial,
        f"limited_history={recent_listing_partial}; retain PIT member but mark unavailable long-lookback features",
        severity="warning",
    )

    benchmark_history_failures: list[str] = []
    minimum_full = int(policy.payload["selection"]["minimum_rows_full"])
    for ticker in ("XLP", "SPY"):
        count = _scalar(
            conn,
            "SELECT COUNT(*) FROM fact_price_ohlcv WHERE ticker=? AND source_id=? AND bar_date<=? AND adjusted_close>0",
            (ticker, YAHOO_SOURCE_ID, snapshot),
        )
        if count < minimum_full:
            benchmark_history_failures.append(f"{ticker}:{count}")
    check("first_snapshot_benchmark_history", not benchmark_history_failures, f"failures={benchmark_history_failures}")

    feature_rows = conn.execute(
        """
        SELECT ticker, source_id, quality_status, history_days FROM feature_market_technical
        WHERE model_family=? AND asof_date=?
        """,
        (MODEL_FAMILY, as_of),
    ).fetchall()
    feature_by_ticker = {str(row["ticker"]): dict(row) for row in feature_rows}
    expected_features = point_in_time_feature_tickers(
        conn,
        as_of=as_of,
        max_staleness_calendar_days=int(policy.payload["selection"]["active_max_staleness_calendar_days"]),
    )
    check(
        "point_in_time_market_feature_count",
        set(feature_by_ticker) == set(expected_features) and len(feature_rows) == len(feature_by_ticker),
        f"observed={len(feature_by_ticker)} expected={len(expected_features)} "
        f"missing={sorted(set(expected_features)-set(feature_by_ticker))} "
        f"extra={sorted(set(feature_by_ticker)-set(expected_features))}",
    )
    lineage_bad = sorted(
        ticker for ticker, row in feature_by_ticker.items()
        if row["source_id"] != selections.get(ticker, {}).get("selected_source_id")
    )
    check("feature_source_matches_selection", not lineage_bad, f"wrong={lineage_bad}")
    insufficient = sorted(
        ticker
        for ticker, row in feature_by_ticker.items()
        if row["quality_status"] == "insufficient_history"
        and int(row["history_days"] or 0) < minimum_snapshot
    )
    recent_listing_limited = sorted(
        f"{ticker}:history_rows={int(row['history_days'] or 0)}"
        for ticker, row in feature_by_ticker.items()
        if row["quality_status"] == "insufficient_history"
        and int(row["history_days"] or 0) >= minimum_snapshot
    )
    check("feature_minimum_history", not insufficient, f"insufficient={insufficient}")
    check(
        "feature_recent_listing_history",
        not recent_listing_limited,
        f"limited_history={recent_listing_limited}; retain PIT member but leave unavailable long-lookback features null",
        severity="warning",
    )

    terminal_missing = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT s.ticker FROM dim_security s
            JOIN dim_company c ON c.company_id=s.company_id
            WHERE s.listing_status<>'active' AND c.is_active=0
              AND NOT EXISTS (
                  SELECT 1 FROM fact_terminal_event_reconciliation e
                  WHERE e.ticker=s.ticker
              )
            ORDER BY s.ticker
            """
        ).fetchall()
    ]
    check(
        "delisted_terminal_event_coverage",
        not terminal_missing,
        f"missing={terminal_missing}; required before survivorship-complete calibration, not before Stage 3 price storage",
        severity="warning",
    )
    terminal_excluded = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT ticker FROM fact_terminal_event_reconciliation
            WHERE calibration_eligible=0 ORDER BY ticker
            """
        ).fetchall()
    ]
    check(
        "delisted_terminal_event_explicit_exclusions",
        not terminal_excluded,
        f"terminal_crossing_labels_excluded={terminal_excluded}; unresolved consideration is visible and never imputed",
        severity="warning",
    )
    terminal_rows = _scalar(conn, "SELECT COUNT(*) FROM fact_terminal_event_reconciliation")
    if terminal_rows:
        effective_terminal_policy = terminal_policy or load_terminal_event_policy(DEFAULT_TERMINAL_POLICY)
        terminal_validation = validate_terminal_events(
            conn,
            effective_terminal_policy,
            as_of=as_of,
        )
        checks.extend(
            {**row, "check": f"terminal_event::{row['check']}"}
            for row in terminal_validation["checks"]
        )
        errors.extend(f"terminal_event::{value}" for value in terminal_validation["errors"])
        warnings.extend(f"terminal_event::{value}" for value in terminal_validation["warnings"])

    fallback_active = sorted(
        ticker for ticker in active
        if selections.get(ticker, {}).get("selected_source_id") == NORGATE_SOURCE_ID
    )
    check(
        "active_yahoo_primary_usage",
        not fallback_active,
        f"norgate_whole_ticker_fallback={fallback_active}",
        severity="warning",
    )
    return {
        "status": "PASS" if not errors else "FAIL",
        "as_of": as_of,
        "requested_snapshot_start": snapshot,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "counts": {
            "active_securities": len(active),
            "delisted_securities": len(delisted),
            "first_snapshot_pit_members": len(snapshot_members),
            "price_selections": len(selections),
            "market_features": len(feature_by_ticker), "expected_pit_market_features": len(expected_features),
            "terminal_events": terminal_rows,
            "terminal_events_excluded": len(terminal_excluded),
        },
    }
