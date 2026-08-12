from __future__ import annotations

import csv
import json
import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from consumer_defensive.core.config import expand_env_vars
from consumer_defensive.core.atomic_io import atomic_text_writer, atomic_write_text
from consumer_defensive.core.db import execute_schema_script, utc_now
from consumer_defensive.core.universe import MODEL_FAMILY, active_universe_tickers, normalize_ticker, read_yaml


YAHOO_SOURCE_ID = "yahoo_finance_adjusted"
NORGATE_SOURCE_ID = "norgate_us_equities_total_return"
SELECTION_PURPOSE = "scoring_return_series"

STAGE3_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dim_price_series_selection (
    ticker TEXT NOT NULL,
    purpose TEXT NOT NULL,
    selected_source_id TEXT NOT NULL,
    selection_asof_date TEXT NOT NULL,
    first_bar_date TEXT NOT NULL,
    last_bar_date TEXT NOT NULL,
    bar_count INTEGER NOT NULL,
    adjustment_basis TEXT NOT NULL,
    selection_reason TEXT NOT NULL,
    expected_start_date TEXT,
    expected_end_date TEXT,
    coverage_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, purpose),
    FOREIGN KEY(selected_source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_market_data_audit (
    audit_asof_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    security_status TEXT NOT NULL,
    expected_start_date TEXT,
    expected_end_date TEXT,
    yahoo_first_date TEXT,
    yahoo_last_date TEXT,
    yahoo_rows INTEGER NOT NULL DEFAULT 0,
    norgate_first_date TEXT,
    norgate_last_date TEXT,
    norgate_rows INTEGER NOT NULL DEFAULT 0,
    selected_source_id TEXT,
    coverage_status TEXT NOT NULL,
    issue_detail TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(audit_asof_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_cd_price_selection_source
    ON dim_price_series_selection(selected_source_id, coverage_status);
CREATE INDEX IF NOT EXISTS idx_cd_market_audit_status
    ON fact_market_data_audit(audit_asof_date, coverage_status);
"""


@dataclass(frozen=True)
class MarketDataPolicy:
    path: Path
    payload: dict[str, Any]

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def resolve(self, dotted_key: str) -> Path:
        value: Any = self.payload
        for key in dotted_key.split("."):
            if not isinstance(value, dict) or key not in value:
                raise ValueError(f"Market-data policy path {dotted_key!r} is missing.")
            value = value[key]
        raw = Path(expand_env_vars(value)).expanduser()
        return raw.resolve() if raw.is_absolute() else (self.base_dir / raw).resolve()


@dataclass(frozen=True)
class PriceBar:
    ticker: str
    bar_date: str
    source_id: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adjusted_close: float
    volume: float | None
    dividend: float | None
    split_factor: float | None
    total_return_basis: str
    source_timestamp: str


@dataclass(frozen=True)
class CorporateAction:
    ticker: str
    action_date: str
    source_id: str
    action_type: str
    action_value: float | None
    action_currency: str
    details: dict[str, Any]


def load_market_policy(path: Path) -> MarketDataPolicy:
    resolved = path.expanduser().resolve()
    payload = read_yaml(resolved)
    required = {
        "policy_version": "consumer_defensive_market_data_v1",
        "model_family": MODEL_FAMILY,
        "history_start": "2017-11-28",
        "requested_snapshot_start": "2019-01-02",
    }
    for key, expected in required.items():
        if payload.get(key) != expected:
            raise ValueError(f"Market-data policy {key} must be {expected!r}; got {payload.get(key)!r}.")
    sources = payload.get("sources") or {}
    expected_sources = {
        "active_primary": YAHOO_SOURCE_ID,
        "historical_delisted_primary": NORGATE_SOURCE_ID,
        "active_whole_ticker_fallback": NORGATE_SOURCE_ID,
        "source_selection_granularity": "ticker",
        "cross_source_date_splicing_allowed": False,
    }
    for key, expected in expected_sources.items():
        if sources.get(key) != expected:
            raise ValueError(f"Market-data sources.{key} must be {expected!r}.")
    benchmarks = payload.get("benchmarks") or {}
    if benchmarks != {"sector": "XLP", "broad": "SPY", "required_source": YAHOO_SOURCE_ID}:
        raise ValueError("Market-data benchmarks must be XLP/SPY from Yahoo.")
    history_buffer = payload.get("history_buffer_calendar_days")
    if isinstance(history_buffer, bool) or not isinstance(history_buffer, int) or history_buffer < 1:
        raise ValueError("Market-data history_buffer_calendar_days must be a positive integer.")
    selection = payload.get("selection") or {}
    numeric_contract = {
        "maximum_missing_trading_day_ratio": float,
        "missing_trading_day_warning_ratio": float,
        "maximum_consecutive_missing_trading_days": int,
    }
    for key, expected_type in numeric_contract.items():
        value = selection.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Market-data selection.{key} must be numeric.")
        if expected_type is int and (not isinstance(value, int) or value < 1):
            raise ValueError(f"Market-data selection.{key} must be a positive integer.")
    maximum_missing = float(selection["maximum_missing_trading_day_ratio"])
    warning_missing = float(selection["missing_trading_day_warning_ratio"])
    if not math.isfinite(maximum_missing) or not 0.0 <= maximum_missing <= 1.0:
        raise ValueError(
            "Market-data selection.maximum_missing_trading_day_ratio must be between 0 and 1."
        )
    if (
        not math.isfinite(warning_missing)
        or not 0.0 <= warning_missing <= maximum_missing
    ):
        raise ValueError(
            "Market-data selection.missing_trading_day_warning_ratio must be between 0 "
            "and maximum_missing_trading_day_ratio."
        )
    return MarketDataPolicy(path=resolved, payload=payload)


def ensure_stage3_schema(conn: sqlite3.Connection) -> None:
    execute_schema_script(conn, STAGE3_SCHEMA_SQL)


def safe_float(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def upsert_price_bars(conn: sqlite3.Connection, bars: Iterable[PriceBar]) -> int:
    now = utc_now()
    rows = list(bars)
    conn.executemany(
        """
        INSERT INTO fact_price_ohlcv(
            ticker, bar_date, source_id, open, high, low, close, adjusted_close,
            volume, dividend, split_factor, total_return_basis, source_timestamp, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, bar_date, source_id) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, adjusted_close=excluded.adjusted_close,
            volume=excluded.volume, dividend=excluded.dividend,
            split_factor=excluded.split_factor,
            total_return_basis=excluded.total_return_basis,
            source_timestamp=excluded.source_timestamp,
            created_at=excluded.created_at
        """,
        [
            (
                bar.ticker,
                bar.bar_date,
                bar.source_id,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.adjusted_close,
                bar.volume,
                bar.dividend,
                bar.split_factor,
                bar.total_return_basis,
                bar.source_timestamp,
                now,
            )
            for bar in rows
        ],
    )
    return len(rows)


def upsert_corporate_actions(conn: sqlite3.Connection, actions: Iterable[CorporateAction]) -> int:
    now = utc_now()
    rows = list(actions)
    conn.executemany(
        """
        INSERT INTO fact_corporate_action(
            ticker, action_date, source_id, action_type, action_value,
            action_currency, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, action_date, source_id, action_type) DO UPDATE SET
            action_value=excluded.action_value,
            action_currency=excluded.action_currency,
            details_json=excluded.details_json,
            created_at=excluded.created_at
        """,
        [
            (
                action.ticker,
                action.action_date,
                action.source_id,
                action.action_type,
                action.action_value,
                action.action_currency,
                json.dumps(action.details, sort_keys=True),
                now,
            )
            for action in rows
        ],
    )
    return len(rows)


def current_tickers(conn: sqlite3.Connection) -> list[str]:
    return active_universe_tickers(conn)


def security_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT s.security_id, s.ticker, s.provider_price_symbol, s.listing_status,
                   s.listing_start_date, s.listing_end_date, s.currency,
                   c.is_active, t.calibration_cohort_id,
                   (
                       SELECT MIN(u.start_date)
                       FROM dim_universe_membership u
                       WHERE u.security_id=s.security_id AND u.model_family=?
                   ) AS first_recognized_membership_date,
                   (
                       SELECT MIN(u.start_date)
                       FROM dim_universe_membership u
                       WHERE u.security_id=s.security_id AND u.model_family=?
                         AND u.historical_calibration_eligible_flag=1
                   ) AS first_calibration_eligible_date
            FROM dim_security s
            JOIN dim_company c ON c.company_id=s.company_id
            JOIN dim_consumer_defensive_taxonomy t ON t.security_id=s.security_id
            ORDER BY s.ticker
            """,
            (MODEL_FAMILY, MODEL_FAMILY),
        ).fetchall()
    ]


def required_price_coverage_window(
    security: dict[str, Any],
    policy: MarketDataPolicy,
    *,
    as_of: str,
) -> tuple[str, str] | None:
    """Return the PIT-relevant whole-ticker history window for a security.

    The coverage anchor is the first calibration-eligible membership interval.
    Reviewed terminal-event exclusions such as WBA still require auditable price
    history, so a recognized but research-ineligible interval is the explicit
    fallback. Securities whose first interval is after ``as_of`` are future-only
    and cannot make an earlier historical audit fail.
    """
    recognized = _date(str(security.get("first_recognized_membership_date") or ""))
    eligible = _date(str(security.get("first_calibration_eligible_date") or ""))
    anchor = eligible or recognized
    as_of_date = _date(as_of)
    history_start = _date(str(policy.payload.get("history_start") or ""))
    listing_start = _date(str(security.get("listing_start_date") or ""))
    listing_end = _date(str(security.get("listing_end_date") or ""))
    if anchor is None or as_of_date is None or history_start is None:
        return None
    if anchor > as_of_date:
        return None
    buffer_days = int(policy.payload["history_buffer_calendar_days"])
    required_start = max(
        value
        for value in (history_start, listing_start, anchor - timedelta(days=buffer_days))
        if value is not None
    )
    required_end = min(value for value in (as_of_date, listing_end) if value is not None)
    if required_start > required_end:
        return None
    return required_start.isoformat(), required_end.isoformat()


def price_coverage(
    conn: sqlite3.Connection,
    ticker: str,
    source_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    include_dates: bool = False,
) -> dict[str, Any]:
    clauses = ["ticker=?", "source_id=?"]
    params: list[Any] = [ticker, source_id]
    if start is not None:
        clauses.append("bar_date>=?")
        params.append(start)
    if end is not None:
        clauses.append("bar_date<=?")
        params.append(end)
    rows = conn.execute(
        f"""
        SELECT bar_date, adjusted_close
        FROM fact_price_ohlcv WHERE {' AND '.join(clauses)}
        ORDER BY bar_date
        """,
        params,
    ).fetchall()
    dates = tuple(str(row[0]) for row in rows)
    result = {
        "first": dates[0] if dates else "",
        "last": dates[-1] if dates else "",
        "rows": len(dates),
        "invalid_adjusted": sum(
            1 for row in rows if row[1] is None or float(row[1]) <= 0
        ),
    }
    if include_dates:
        result['observed_dates'] = dates
    return result


def _date(raw: str) -> date | None:
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def trading_calendar_coverage(
    coverage: dict[str, Any],
    *,
    expected_start: str,
    expected_end: str,
    expected_dates: tuple[str, ...] | None,
) -> dict[str, Any]:
    expected = [
        value
        for value in (expected_dates or ())
        if expected_start <= value <= expected_end
    ]
    observed = set(str(value) for value in coverage.get("observed_dates") or ())
    missing_positions = [
        position for position, value in enumerate(expected) if value not in observed
    ]
    longest_gap = 0
    current_gap = 0
    missing = set(missing_positions)
    for position in range(len(expected)):
        current_gap = current_gap + 1 if position in missing else 0
        longest_gap = max(longest_gap, current_gap)
    missing_count = len(missing_positions)
    return {
        "expected_trading_days": len(expected),
        "observed_trading_days": len(expected) - missing_count,
        "missing_trading_days": missing_count,
        "missing_trading_day_ratio": missing_count / len(expected) if expected else 0.0,
        "longest_consecutive_missing_trading_days": longest_gap,
    }


def coverage_policy_kwargs(policy: MarketDataPolicy) -> dict[str, Any]:
    settings = policy.payload["selection"]
    return {
        "maximum_missing_trading_day_ratio": float(
            settings["maximum_missing_trading_day_ratio"]
        ),
        "maximum_consecutive_missing_trading_days": int(
            settings["maximum_consecutive_missing_trading_days"]
        ),
    }


def coverage_qualifies(
    coverage: dict[str, Any],
    *,
    expected_start: str,
    expected_end: str,
    start_tolerance_days: int,
    end_tolerance_days: int,
    minimum_rows: int,
    expected_dates: tuple[str, ...] | None = None,
    maximum_missing_trading_day_ratio: float = 0.01,
    maximum_consecutive_missing_trading_days: int = 5,
) -> bool:
    first = _date(str(coverage.get("first") or ""))
    last = _date(str(coverage.get("last") or ""))
    expected_first = _date(expected_start)
    expected_last = _date(expected_end)
    if first is None or last is None or expected_first is None or expected_last is None:
        return False
    if int(coverage.get("invalid_adjusted") or 0) > 0 or int(coverage.get("rows") or 0) < minimum_rows:
        return False
    if not (
        first <= expected_first + timedelta(days=start_tolerance_days)
        and last >= expected_last - timedelta(days=end_tolerance_days)
    ):
        return False
    if expected_dates:
        diagnostics = trading_calendar_coverage(
            coverage,
            expected_start=expected_start,
            expected_end=expected_end,
            expected_dates=expected_dates,
        )
        if (
            float(diagnostics["missing_trading_day_ratio"])
            > maximum_missing_trading_day_ratio
        ):
            return False
        if (
            int(diagnostics["longest_consecutive_missing_trading_days"])
            > maximum_consecutive_missing_trading_days
        ):
            return False
    return True


def select_price_sources(
    conn: sqlite3.Connection,
    policy: MarketDataPolicy,
    *,
    as_of: str,
) -> dict[str, Any]:
    ensure_stage3_schema(conn)
    settings = policy.payload["selection"]
    history_start = str(policy.payload["history_start"])
    minimum_partial = int(settings["minimum_rows_partial"])
    start_tolerance = int(settings["start_tolerance_calendar_days"])
    active_staleness = int(settings["active_max_staleness_calendar_days"])
    delisted_tolerance = int(settings["delisted_end_tolerance_calendar_days"])
    coverage_kwargs = coverage_policy_kwargs(policy)
    warning_ratio = float(settings["missing_trading_day_warning_ratio"])
    audit_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    selected_counts: dict[str, int] = {YAHOO_SOURCE_ID: 0, NORGATE_SOURCE_ID: 0}
    now = utc_now()

    candidates: list[dict[str, Any]] = []
    for security in security_rows(conn):
        recognized = str(security.get("first_recognized_membership_date") or "")
        if not recognized:
            errors.append(
                f"{security['ticker']}: no recognized membership interval is available for coverage."
            )
            continue
        window = required_price_coverage_window(security, policy, as_of=as_of)
        if window is None:
            continue
        candidates.append({**security, "required_coverage_window": window})
    for benchmark in ("XLP", "SPY"):
        candidates.append(
            {
                "ticker": benchmark,
                "listing_status": "benchmark",
                "listing_start_date": history_start,
                "listing_end_date": None,
                "is_active": 1,
                "required_coverage_window": (history_start, as_of),
            }
        )
    trading_calendar = tuple(
        str(row[0])
        for row in conn.execute(
            '''SELECT trading_date FROM (
                   SELECT bar_date AS trading_date FROM fact_price_ohlcv
                   WHERE ticker='SPY' AND source_id=?
                     AND bar_date>=? AND bar_date<=?
                   UNION
                   SELECT listing_date AS trading_date
                   FROM fact_major_exchange_listing_daily
                   WHERE listing_date>=? AND listing_date<=?
               ) ORDER BY trading_date''',
            (YAHOO_SOURCE_ID, history_start, as_of, history_start, as_of),
        )
    )
    with conn:
        conn.execute("DELETE FROM fact_market_data_audit WHERE audit_asof_date=?", (as_of,))
        conn.execute("DELETE FROM dim_price_series_selection WHERE purpose=?", (SELECTION_PURPOSE,))
        for security in candidates:
            ticker = normalize_ticker(security["ticker"])
            status = str(security["listing_status"])
            expected_start, expected_end = security["required_coverage_window"]
            expected_weekdays = sum(
                1
                for offset in range((date.fromisoformat(expected_end) - date.fromisoformat(expected_start)).days + 1)
                if (date.fromisoformat(expected_start) + timedelta(days=offset)).weekday() < 5
            )
            minimum_required = (
                minimum_partial
                if expected_weekdays >= minimum_partial
                else int(settings["first_snapshot_minimum_observations"])
            )
            yahoo = price_coverage(
                conn,
                ticker,
                YAHOO_SOURCE_ID,
                start=expected_start,
                end=expected_end,
                include_dates=True,
            )
            norgate = price_coverage(
                conn,
                ticker,
                NORGATE_SOURCE_ID,
                start=expected_start,
                end=expected_end,
                include_dates=True,
            )
            is_active = bool(int(security.get("is_active") or 0)) or status == "benchmark"
            yahoo_ok = coverage_qualifies(
                yahoo,
                expected_start=expected_start,
                expected_end=expected_end,
                start_tolerance_days=start_tolerance,
                end_tolerance_days=active_staleness if is_active else delisted_tolerance,
                minimum_rows=minimum_required,
                expected_dates=trading_calendar,
                **coverage_kwargs,
            )
            norgate_ok = coverage_qualifies(
                norgate,
                expected_start=expected_start,
                expected_end=expected_end,
                start_tolerance_days=start_tolerance,
                end_tolerance_days=active_staleness if is_active else delisted_tolerance,
                minimum_rows=minimum_required,
                expected_dates=trading_calendar,
                **coverage_kwargs,
            )
            yahoo_calendar = trading_calendar_coverage(
                yahoo,
                expected_start=expected_start,
                expected_end=expected_end,
                expected_dates=trading_calendar,
            )
            norgate_calendar = trading_calendar_coverage(
                norgate,
                expected_start=expected_start,
                expected_end=expected_end,
                expected_dates=trading_calendar,
            )
            selected = ""
            reason = ""
            coverage_status = "missing"
            if status == "benchmark":
                if yahoo_ok:
                    selected, reason, coverage_status = YAHOO_SOURCE_ID, "required_benchmark_source", "complete"
                else:
                    errors.append(f"{ticker}: Yahoo benchmark coverage is incomplete.")
            elif is_active:
                if yahoo_ok:
                    selected, reason, coverage_status = YAHOO_SOURCE_ID, "active_primary_complete", "complete"
                elif norgate_ok:
                    selected, reason, coverage_status = NORGATE_SOURCE_ID, "active_whole_ticker_fallback", "fallback"
                    warnings.append(f"{ticker}: selected Norgate because Yahoo coverage is incomplete.")
                else:
                    errors.append(f"{ticker}: neither Yahoo nor Norgate has qualifying active coverage.")
            else:
                if norgate_ok:
                    selected, reason, coverage_status = NORGATE_SOURCE_ID, "delisted_primary_complete", "complete"
                else:
                    errors.append(f"{ticker}: mandatory Norgate historical/delisted series is incomplete.")
            chosen = yahoo if selected == YAHOO_SOURCE_ID else norgate
            if selected:
                selected_counts[selected] += 1
                basis_row = conn.execute(
                    """
                    SELECT total_return_basis FROM fact_price_ohlcv
                    WHERE ticker=? AND source_id=? AND adjusted_close IS NOT NULL
                    ORDER BY bar_date DESC LIMIT 1
                    """,
                    (ticker, selected),
                ).fetchone()
                basis = str(basis_row[0] or "") if basis_row else ""
                conn.execute(
                    """
                    INSERT INTO dim_price_series_selection(
                        ticker, purpose, selected_source_id, selection_asof_date,
                        first_bar_date, last_bar_date, bar_count, adjustment_basis,
                        selection_reason, expected_start_date, expected_end_date,
                        coverage_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ticker, purpose) DO UPDATE SET
                        selected_source_id=excluded.selected_source_id,
                        selection_asof_date=excluded.selection_asof_date,
                        first_bar_date=excluded.first_bar_date,
                        last_bar_date=excluded.last_bar_date,
                        bar_count=excluded.bar_count,
                        adjustment_basis=excluded.adjustment_basis,
                        selection_reason=excluded.selection_reason,
                        expected_start_date=excluded.expected_start_date,
                        expected_end_date=excluded.expected_end_date,
                        coverage_status=excluded.coverage_status,
                        updated_at=excluded.updated_at
                    """,
                    (
                        ticker,
                        SELECTION_PURPOSE,
                        selected,
                        as_of,
                        chosen["first"],
                        chosen["last"],
                        chosen["rows"],
                        basis,
                        reason,
                        expected_start,
                        expected_end,
                        coverage_status,
                        now,
                        now,
                    ),
                )
            chosen_calendar = (
                yahoo_calendar if selected == YAHOO_SOURCE_ID else norgate_calendar
            )
            issue = "" if selected else "no_qualifying_whole_ticker_source"
            if selected and float(chosen_calendar["missing_trading_day_ratio"]) > warning_ratio:
                issue = (
                    "sparse_trading_calendar_coverage:"
                    f"missing={chosen_calendar['missing_trading_days']}/"
                    f"{chosen_calendar['expected_trading_days']};"
                    f"ratio={float(chosen_calendar['missing_trading_day_ratio']):.6f};"
                    "longest_gap="
                    f"{chosen_calendar['longest_consecutive_missing_trading_days']}"
                )
                warnings.append(f"{ticker}: {issue}")
            persisted_row = {
                "audit_asof_date": as_of,
                "ticker": ticker,
                "security_status": status,
                "expected_start_date": expected_start,
                "expected_end_date": expected_end,
                "yahoo_first_date": yahoo["first"],
                "yahoo_last_date": yahoo["last"],
                "yahoo_rows": yahoo["rows"],
                "norgate_first_date": norgate["first"],
                "norgate_last_date": norgate["last"],
                "norgate_rows": norgate["rows"],
                "selected_source_id": selected,
                "coverage_status": coverage_status,
                "issue_detail": issue,
                "created_at": now,
            }
            row = {
                **persisted_row,
                **{f"yahoo_{key}": value for key, value in yahoo_calendar.items()},
                **{f"norgate_{key}": value for key, value in norgate_calendar.items()},
            }
            audit_rows.append(row)
            conn.execute(
                """
                INSERT INTO fact_market_data_audit(
                    audit_asof_date, ticker, security_status, expected_start_date,
                    expected_end_date, yahoo_first_date, yahoo_last_date, yahoo_rows,
                    norgate_first_date, norgate_last_date, norgate_rows,
                    selected_source_id, coverage_status, issue_detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(persisted_row.values()),
            )
    return {
        "status": "PASS" if not errors else "FAIL",
        "as_of": as_of,
        "errors": errors,
        "warnings": warnings,
        "selected_source_counts": selected_counts,
        "rows": audit_rows,
    }


def selected_price_rows(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    as_of: str,
) -> tuple[str, list[sqlite3.Row]]:
    selection = conn.execute(
        """
        SELECT selected_source_id FROM dim_price_series_selection
        WHERE ticker=? AND purpose=? AND selection_asof_date=?
        """,
        (ticker, SELECTION_PURPOSE, as_of),
    ).fetchone()
    if selection is None:
        return "", []
    source_id = str(selection[0])
    rows = conn.execute(
        """
        SELECT bar_date, adjusted_close, close, volume
        FROM fact_price_ohlcv
        WHERE ticker=? AND source_id=? AND bar_date<=? AND adjusted_close>0
        ORDER BY bar_date
        """,
        (ticker, source_id, as_of),
    ).fetchall()
    return source_id, rows


def _return(values: list[float], days: int) -> float | None:
    if len(values) <= days or values[-days - 1] <= 0:
        return None
    return values[-1] / values[-days - 1] - 1.0


def point_in_time_feature_tickers(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    max_staleness_calendar_days: int,
) -> list[str]:
    """Return the recognized, exchange-listed calibration universe at ``as_of``.

    Membership series are trading-day series, so a weekend/holiday snapshot may
    use only the most recent provider date within the configured staleness bound.
    Historical eligibility is read from the compressed membership contract,
    which terminal-event reconciliation keeps consistent for delisted names.
    """
    cutoff = (date.fromisoformat(as_of) - timedelta(days=max_staleness_calendar_days)).isoformat()
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT security_id, MAX(membership_date) AS membership_date
            FROM fact_recognized_vehicle_membership_daily
            WHERE membership_date<=?
            GROUP BY security_id
        ), union_on_date AS (
            SELECT m.security_id, m.membership_date, MAX(m.member_flag) AS any_member
            FROM fact_recognized_vehicle_membership_daily m
            JOIN latest l ON l.security_id=m.security_id
                         AND l.membership_date=m.membership_date
            GROUP BY m.security_id, m.membership_date
        )
        SELECT s.ticker
        FROM union_on_date u
        JOIN fact_major_exchange_listing_daily x
          ON x.security_id=u.security_id AND x.listing_date=u.membership_date
        JOIN dim_security s ON s.security_id=u.security_id
        JOIN dim_consumer_defensive_taxonomy t ON t.security_id=s.security_id
        WHERE t.model_family=? AND u.any_member=1
          AND x.major_exchange_listed_flag=1 AND u.membership_date>=?
          AND EXISTS (
              SELECT 1 FROM dim_universe_membership d
              WHERE d.security_id=u.security_id AND d.model_family=?
                AND d.historical_calibration_eligible_flag=1
                AND d.start_date<=u.membership_date AND d.end_date>=u.membership_date
          )
        ORDER BY s.ticker
        """,
        (as_of, MODEL_FAMILY, cutoff, MODEL_FAMILY),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _dated_return(rows: list[sqlite3.Row], days: int) -> tuple[float | None, str, str]:
    if len(rows) <= days:
        return None, "", ""
    start, end = rows[-days - 1], rows[-1]
    start_value, end_value = float(start[1]), float(end[1])
    if start_value <= 0 or end_value <= 0:
        return None, "", ""
    return end_value / start_value - 1.0, str(start[0]), str(end[0])


def _aligned_residual_return(
    rows: list[sqlite3.Row], benchmark_by_date: dict[str, float], days: int
) -> float | None:
    ticker_return, start_date, end_date = _dated_return(rows, days)
    if ticker_return is None or start_date not in benchmark_by_date or end_date not in benchmark_by_date:
        return None
    start_value, end_value = benchmark_by_date[start_date], benchmark_by_date[end_date]
    if start_value <= 0 or end_value <= 0:
        return None
    return ticker_return - (end_value / start_value - 1.0)


def _annualized_volatilities(values: list[float], days: int) -> tuple[float | None, float | None]:
    returns = [
        math.log(values[i] / values[i - 1])
        for i in range(max(1, len(values) - days), len(values))
        if values[i - 1] > 0 and values[i] > 0
    ]
    if len(returns) < 20:
        return None, None
    realized = statistics.stdev(returns) * math.sqrt(252.0)
    downside = math.sqrt(statistics.fmean(min(value, 0.0) ** 2 for value in returns)) * math.sqrt(252.0)
    return realized, downside


def build_market_features(
    conn: sqlite3.Connection,
    policy: MarketDataPolicy,
    *,
    as_of: str,
) -> dict[str, Any]:
    benchmark = str(policy.payload["features"]["benchmark"])
    _, benchmark_rows = selected_price_rows(conn, benchmark, as_of=as_of)
    benchmark_by_date = {str(row[0]): float(row[1]) for row in benchmark_rows}
    if not benchmark_by_date:
        raise RuntimeError(f"No selected benchmark series for {benchmark}.")
    settings = policy.payload["features"]
    adv_days = int(settings["adv_days"])
    short_days = int(settings["momentum_short_days"])
    long_days = int(settings["momentum_long_days"])
    vol_days = int(settings["volatility_days"])
    drawdown_days = int(settings["drawdown_days"])
    full_rows = int(policy.payload["selection"]["minimum_rows_full"])
    partial_rows = int(policy.payload["selection"]["minimum_rows_partial"])
    now = utc_now()
    written = 0
    statuses: dict[str, int] = {}
    tickers = point_in_time_feature_tickers(
        conn,
        as_of=as_of,
        max_staleness_calendar_days=int(policy.payload["selection"]["active_max_staleness_calendar_days"]),
    )
    with conn:
        conn.execute(
            "DELETE FROM feature_market_technical WHERE model_family=? AND asof_date=?",
            (MODEL_FAMILY, as_of),
        )
        for ticker in tickers:
            source_id, rows = selected_price_rows(conn, ticker, as_of=as_of)
            if not source_id or not rows:
                continue
            adjusted = [float(row[1]) for row in rows]
            close = [float(row[2] or row[1]) for row in rows]
            volume = [float(row[3] or 0.0) for row in rows]
            dollar_volume = [c * v for c, v in zip(close, volume, strict=True)]
            adv = statistics.fmean(dollar_volume[-adv_days:]) if len(dollar_volume) >= adv_days else None
            residual_short = _aligned_residual_return(rows, benchmark_by_date, short_days)
            residual_long = _aligned_residual_return(rows, benchmark_by_date, long_days)
            realized, downside = _annualized_volatilities(adjusted, vol_days)
            window = adjusted[-drawdown_days:]
            peak = 0.0
            max_drawdown = 0.0
            for value in window:
                peak = max(peak, value)
                if peak > 0:
                    max_drawdown = min(max_drawdown, value / peak - 1.0)
            if len(rows) >= full_rows:
                quality = "full"
            elif len(rows) >= partial_rows:
                quality = "partial_history"
            else:
                quality = "insufficient_history"
            statuses[quality] = statuses.get(quality, 0) + 1
            conn.execute(
                """
                INSERT INTO feature_market_technical(
                    model_family, ticker, asof_date, source_id, adjusted_close,
                    avg_dollar_volume_63d, residual_momentum_63d,
                    residual_momentum_126d, realized_volatility_63d,
                    downside_volatility_63d, max_drawdown_252d, history_days,
                    quality_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_family, ticker, asof_date, source_id) DO UPDATE SET
                    adjusted_close=excluded.adjusted_close,
                    avg_dollar_volume_63d=excluded.avg_dollar_volume_63d,
                    residual_momentum_63d=excluded.residual_momentum_63d,
                    residual_momentum_126d=excluded.residual_momentum_126d,
                    realized_volatility_63d=excluded.realized_volatility_63d,
                    downside_volatility_63d=excluded.downside_volatility_63d,
                    max_drawdown_252d=excluded.max_drawdown_252d,
                    history_days=excluded.history_days,
                    quality_status=excluded.quality_status,
                    created_at=excluded.created_at
                """,
                (
                    MODEL_FAMILY,
                    ticker,
                    as_of,
                    source_id,
                    adjusted[-1],
                    adv,
                    residual_short,
                    residual_long,
                    realized,
                    downside,
                    max_drawdown,
                    len(rows),
                    quality,
                    now,
                ),
            )
            written += 1
    return {"as_of": as_of, "eligible_tickers": len(tickers), "features_written": written, "quality_counts": statuses}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    if not columns:
        columns = ["status"]
    with atomic_text_writer(path, encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
