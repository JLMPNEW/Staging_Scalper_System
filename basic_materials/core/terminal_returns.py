"""Fail-closed terminal-return calculations for the Stage 2B historical pilot."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
import hashlib
import json
import math
import sqlite3
from typing import Any

from basic_materials.core.db import assert_database_identity, utc_now
from basic_materials.core.market_data_contract import MarketDataPolicy


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def calculate_terminal_components(
    *,
    outcome_class: str,
    cash_weight: float,
    stock_weight: float,
    cash_consideration: float | None,
    successor_share_ratio: float | None,
    successor_close: float | None,
    bankruptcy_distribution_value: float | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Return cash, stock, distribution, and total value per original share."""

    cash_component: float | None = None
    stock_component: float | None = None
    distribution_component: float | None = None
    if outcome_class in {"fixed_cash", "mixed_prorated"}:
        if cash_consideration is None:
            return None, None, None, None
        cash_component = cash_weight * cash_consideration
    if outcome_class in {"stock_conversion", "mixed_prorated"}:
        if successor_share_ratio is None or successor_close is None:
            return cash_component, None, None, None
        stock_component = stock_weight * successor_share_ratio * successor_close
    if outcome_class == "bankruptcy_distribution":
        if bankruptcy_distribution_value is None:
            return None, None, None, None
        distribution_component = bankruptcy_distribution_value
    if outcome_class == "otc_continuation":
        if successor_close is None:
            return None, None, None, None
        stock_component = successor_close
    pieces = [cash_component, stock_component, distribution_component]
    populated = [value for value in pieces if value is not None]
    if not populated:
        return cash_component, stock_component, distribution_component, None
    return cash_component, stock_component, distribution_component, float(sum(populated))


def _price_on_exact_date(
    conn: sqlite3.Connection,
    instrument_id: int,
    price_date: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT bar_date, close, adjusted_close, snapshot_key
        FROM fact_adjusted_price_bar
        WHERE instrument_id = ? AND bar_date = ?
        """,
        (instrument_id, price_date),
    ).fetchone()


def _successor_price(
    conn: sqlite3.Connection,
    instrument_id: int,
    reference_date: str,
    as_of: str,
    max_lag_days: int,
) -> sqlite3.Row | None:
    upper = min(
        date.fromisoformat(as_of),
        date.fromisoformat(reference_date) + timedelta(days=max_lag_days),
    ).isoformat()
    return conn.execute(
        """
        SELECT bar_date, close, adjusted_close, snapshot_key
        FROM fact_adjusted_price_bar
        WHERE instrument_id = ? AND bar_date >= ? AND bar_date <= ?
        ORDER BY bar_date
        LIMIT 1
        """,
        (instrument_id, reference_date, upper),
    ).fetchone()


def _successor_instrument_id(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    successor_ticker: str,
) -> int | None:
    direct = conn.execute(
        """
        SELECT instrument_id FROM bridge_market_instrument_role
        WHERE role_type = 'terminal_successor' AND event_key = ?
        """,
        (event_key,),
    ).fetchone()
    if direct is not None:
        return int(direct["instrument_id"])
    if not successor_ticker:
        return None
    fallback = conn.execute(
        """
        SELECT instrument_id FROM bridge_market_instrument_role
        WHERE UPPER(model_ticker) = UPPER(?)
        ORDER BY CASE role_type WHEN 'current_universe' THEN 0 ELSE 1 END, role_key
        LIMIT 1
        """,
        (successor_ticker,),
    ).fetchone()
    return int(fallback["instrument_id"]) if fallback is not None else None


def reconcile_terminal_returns(
    conn: sqlite3.Connection,
    *,
    policy: MarketDataPolicy,
    as_of: str,
    snapshot_key: str,
) -> dict[str, Any]:
    """Calculate terminal values without activating historical calibration."""

    assert_database_identity(conn)
    date.fromisoformat(as_of)
    if conn.in_transaction:
        raise RuntimeError("reconcile_terminal_returns requires a clean connection")
    rows = conn.execute(
        """
        SELECT t.event_key, t.event_date, t.evidence_json AS event_evidence_json,
               t.resolved AS prior_resolved,
               r.outcome_class, r.cash_weight, r.stock_weight,
               r.bankruptcy_distribution_value, r.distribution_currency,
               r.otc_continuation_symbol, r.fractional_share_treatment,
               r.max_reference_lag_calendar_days, r.rule_status,
               r.contract_sha256 AS rule_contract_sha256,
               h.instrument_id AS historical_instrument_id
        FROM fact_terminal_event_reconciliation AS t
        JOIN dim_terminal_return_rule AS r ON r.event_key = t.event_key
        JOIN bridge_market_instrument_role AS h
          ON h.security_id = t.security_id
         AND h.role_type = 'historical_pilot'
        ORDER BY t.event_key
        """
    ).fetchall()
    required = int(policy.payload["terminal_returns"]["required_event_count"])
    if len(rows) != required:
        raise RuntimeError(f"Expected {required} governed terminal events and found {len(rows)}")
    now = utc_now()
    calculations: list[dict[str, Any]] = []
    merged_event_evidence: dict[str, str] = {}
    for row in rows:
        event_key = str(row["event_key"])
        event = json.loads(str(row["event_evidence_json"]))
        outcome = str(row["outcome_class"])
        historical_id = int(row["historical_instrument_id"])
        final_date = str(event.get("last_trade_date") or "")
        final_price = _price_on_exact_date(conn, historical_id, final_date) if final_date <= as_of else None
        successor_id = _successor_instrument_id(
            conn,
            event_key=event_key,
            successor_ticker=str(event.get("successor_ticker") or ""),
        )
        reference_date = str(event.get("successor_reference_date") or "")
        successor_price = (
            _successor_price(
                conn,
                successor_id,
                reference_date,
                as_of,
                int(row["max_reference_lag_calendar_days"]),
            )
            if successor_id is not None and reference_date and reference_date <= as_of
            else None
        )

        status = "pending"
        resolved = 0
        cash_component = stock_component = distribution_component = terminal_value = None
        if str(row["rule_status"]) == "pending_distribution_evidence":
            status = "pending_distribution_evidence"
        elif str(row["event_date"]) > as_of:
            status = "event_after_calculation_asof"
        elif final_price is None:
            status = "historical_final_quote_missing"
        elif outcome in {"stock_conversion", "mixed_prorated", "otc_continuation"} and successor_price is None:
            status = "successor_reference_quote_missing"
        else:
            cash_component, stock_component, distribution_component, terminal_value = (
                calculate_terminal_components(
                    outcome_class=outcome,
                    cash_weight=float(row["cash_weight"]),
                    stock_weight=float(row["stock_weight"]),
                    cash_consideration=_optional_float(event.get("cash_consideration")),
                    successor_share_ratio=_optional_float(event.get("successor_share_ratio")),
                    successor_close=(float(successor_price["close"]) if successor_price else None),
                    bankruptcy_distribution_value=_optional_float(
                        row["bankruptcy_distribution_value"]
                    ),
                )
            )
            if terminal_value is None:
                status = "terminal_terms_incomplete"
            else:
                status = f"resolved_{outcome}"
                resolved = 1

        evidence: dict[str, Any] = {
            "event_key": event_key,
            "calculation_asof_date": as_of,
            "outcome_class": outcome,
            "historical_final_price_date": str(final_price["bar_date"]) if final_price else None,
            "successor_reference_price_date": (
                str(successor_price["bar_date"]) if successor_price else None
            ),
            "cash_weight": float(row["cash_weight"]),
            "stock_weight": float(row["stock_weight"]),
            "cash_consideration": _optional_float(event.get("cash_consideration")),
            "successor_share_ratio": _optional_float(event.get("successor_share_ratio")),
            "bankruptcy_distribution_value": _optional_float(
                row["bankruptcy_distribution_value"]
            ),
            "terminal_value": terminal_value,
            "status": status,
            "resolved": resolved,
            "no_future_price_used": True,
            "market_snapshot_key": snapshot_key,
            "rule_contract_sha256": str(row["rule_contract_sha256"]),
        }
        evidence_payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        evidence_sha = hashlib.sha256(evidence_payload.encode("utf-8")).hexdigest()
        calculations.append(
            {
                "event_key": event_key,
                "calculation_asof_date": as_of,
                "historical_instrument_id": historical_id,
                "successor_instrument_id": successor_id,
                "historical_final_price_date": str(final_price["bar_date"]) if final_price else None,
                "historical_final_close": float(final_price["close"]) if final_price else None,
                "historical_final_adjusted_close": (
                    float(final_price["adjusted_close"]) if final_price else None
                ),
                "successor_reference_price_date": (
                    str(successor_price["bar_date"]) if successor_price else None
                ),
                "successor_reference_close": (
                    float(successor_price["close"]) if successor_price else None
                ),
                "successor_reference_adjusted_close": (
                    float(successor_price["adjusted_close"]) if successor_price else None
                ),
                "cash_component": cash_component,
                "stock_component": stock_component,
                "distribution_component": distribution_component,
                "terminal_value": terminal_value,
                "terminal_currency": str(row["distribution_currency"] or event.get("cash_currency") or "USD"),
                "calculation_status": status,
                "resolved": resolved,
                "no_future_price_used": 1,
                "fractional_share_treatment": str(row["fractional_share_treatment"]),
                "market_snapshot_key": snapshot_key,
                "rule_contract_sha256": str(row["rule_contract_sha256"]),
                "calculation_evidence_sha256": evidence_sha,
                "evidence_json": evidence_payload,
                "created_at_utc": now,
                "updated_at_utc": now,
            }
        )
        event["stage3_terminal_calculation"] = {
            "calculation_asof_date": as_of,
            "status": status,
            "resolved": str(resolved),
            "calculation_evidence_sha256": evidence_sha,
        }
        merged_event_evidence[event_key] = json.dumps(event, sort_keys=True)

    columns = tuple(calculations[0])
    conn.execute("BEGIN IMMEDIATE")
    try:
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"event_key", "calculation_asof_date", "created_at_utc"}
        )
        conn.executemany(
            f"INSERT INTO fact_terminal_return_calculation ({','.join(columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT(event_key, calculation_asof_date) "
            f"DO UPDATE SET {updates}",
            [tuple(item.values()) for item in calculations],
        )
        conn.executemany(
            """
            UPDATE fact_terminal_event_reconciliation
            SET resolved = ?, evidence_json = ?
            WHERE event_key = ?
            """,
            [
                (item["resolved"], merged_event_evidence[item["event_key"]], item["event_key"])
                for item in calculations
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    status_counts = Counter(str(item["calculation_status"]) for item in calculations)
    resolved_count = sum(int(item["resolved"]) for item in calculations)
    return {
        "calculation_rows": len(calculations),
        "resolved_terminal_events": resolved_count,
        "unresolved_terminal_events": len(calculations) - resolved_count,
        "calculation_status_counts": dict(sorted(status_counts.items())),
        "calibration_activated": False,
    }
