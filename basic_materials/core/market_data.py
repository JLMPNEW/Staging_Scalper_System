"""Coverage audits, technical features, and Stage 3 market-data validation."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import pandas as pd

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.atomic_io import atomic_write_csv, atomic_write_json
from basic_materials.core.db import assert_database_identity, database_counts, utc_now
from basic_materials.core.market_data_contract import MarketDataManifest, MarketDataPolicy


@dataclass(frozen=True)
class MarketValidationIssue:
    severity: str
    issue_code: str
    message: str
    ticker: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class MarketValidationReport:
    passed: bool
    validated_at_utc: str
    as_of_date: str
    policy_version: str
    manifest_checksum: str
    snapshot_key: str
    expected_counts: Mapping[str, int]
    actual_counts: Mapping[str, int]
    coverage_status_counts: Mapping[str, int]
    feature_quality_counts: Mapping[str, int]
    current_gate_ratio: float
    resolved_terminal_events: int
    unresolved_terminal_events: int
    issues: tuple[MarketValidationIssue, ...]

    def summary_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "issues": [issue.as_dict() for issue in self.issues],
            "error_count": sum(issue.severity == "error" for issue in self.issues),
            "warning_count": sum(issue.severity == "warning" for issue in self.issues),
        }


def latest_market_snapshot(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
) -> sqlite3.Row:
    query = "SELECT * FROM fact_market_provider_snapshot WHERE status = 'loaded'"
    parameters: tuple[Any, ...] = ()
    if as_of is not None:
        date.fromisoformat(as_of)
        query += " AND extraction_asof_date <= ?"
        parameters = (as_of,)
    query += " ORDER BY extraction_asof_date DESC, created_at_utc DESC LIMIT 1"
    row = conn.execute(query, parameters).fetchone()
    if row is None:
        raise RuntimeError("No loaded Stage 3 market snapshot is available")
    return row


def _longest_missing_gap(expected: list[str], actual: set[str]) -> int:
    longest = 0
    current = 0
    for session in expected:
        if session in actual:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def build_market_coverage(
    conn: sqlite3.Connection,
    *,
    policy: MarketDataPolicy,
    as_of: str,
    snapshot_key: str,
) -> dict[str, Any]:
    """Audit every governed role against the SPY trading-calendar proxy."""

    assert_database_identity(conn)
    date.fromisoformat(as_of)
    coverage_policy = policy.payload["coverage"]
    calendar = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT session_date FROM dim_trading_calendar_session
            WHERE calendar_code = 'XNYS_PROXY_SPY' AND session_date <= ?
            ORDER BY session_date
            """,
            (as_of,),
        ).fetchall()
    ]
    if not calendar:
        raise RuntimeError("SPY trading-calendar sessions must be loaded before coverage audit")
    roles = conn.execute(
        """
        SELECT r.*, i.provider_source_id, i.provider_first_quoted_date,
               i.provider_last_quoted_date
        FROM bridge_market_instrument_role AS r
        JOIN dim_market_instrument AS i ON i.instrument_id = r.instrument_id
        WHERE r.required_for_stage3 = 1
        ORDER BY r.role_key
        """
    ).fetchall()
    now = utc_now()
    results: list[dict[str, Any]] = []
    for role in roles:
        expected_start = str(role["expected_start_date"])
        expected_end = min(str(role["expected_end_date"] or as_of), as_of)
        expected_sessions = [item for item in calendar if expected_start <= item <= expected_end]
        price_rows = conn.execute(
            """
            SELECT bar_date, close, adjusted_close, volume
            FROM fact_adjusted_price_bar
            WHERE instrument_id = ? AND bar_date BETWEEN ? AND ?
            ORDER BY bar_date
            """,
            (role["instrument_id"], expected_start, expected_end),
        ).fetchall()
        actual_dates = {str(item["bar_date"]) for item in price_rows}
        invalid = sum(
            item["close"] is None
            or float(item["close"]) <= 0
            or item["adjusted_close"] is None
            or float(item["adjusted_close"]) <= 0
            or (item["volume"] is not None and float(item["volume"]) < 0)
            for item in price_rows
        )
        missing = [item for item in expected_sessions if item not in actual_dates]
        ratio = len(missing) / len(expected_sessions) if expected_sessions else 1.0
        longest = _longest_missing_gap(expected_sessions, actual_dates)
        first = str(price_rows[0]["bar_date"]) if price_rows else ""
        last = str(price_rows[-1]["bar_date"]) if price_rows else ""
        reasons: list[str] = []
        end_gap = 10**9
        end_limit = int(coverage_policy["active_max_staleness_calendar_days"])
        if not price_rows:
            status = "missing"
            reasons.append("no_price_rows")
        else:
            start_gap = (date.fromisoformat(first) - date.fromisoformat(expected_start)).days
            end_gap = (date.fromisoformat(expected_end) - date.fromisoformat(last)).days
            if start_gap > int(coverage_policy["start_tolerance_calendar_days"]):
                reasons.append(f"late_start:{start_gap}")
            end_limit = (
                int(coverage_policy["active_max_staleness_calendar_days"])
                if role["expected_end_date"] is None
                else int(coverage_policy["historical_end_tolerance_calendar_days"])
            )
            if end_gap > end_limit:
                reasons.append(f"stale_end:{end_gap}")
            if ratio > float(coverage_policy["maximum_missing_session_ratio"]):
                reasons.append(f"missing_ratio:{ratio:.6f}")
            if longest > int(coverage_policy["maximum_consecutive_missing_sessions"]):
                reasons.append(f"longest_missing_gap:{longest}")
            if invalid:
                reasons.append(f"invalid_bars:{invalid}")
            recent_listing = (
                role["role_type"] == "current_universe"
                and first == str(role["provider_first_quoted_date"])
                and len(price_rows) < int(coverage_policy["minimum_rows_full"])
            )
            if invalid:
                status = "failed"
            elif recent_listing and end_gap <= end_limit:
                status = "recent_listing_short_history"
            elif reasons:
                status = "partial"
            elif len(price_rows) >= int(coverage_policy["minimum_rows_full"]):
                status = "complete"
            else:
                status = "partial"
        rank_ready = status == "complete" or (
            status == "recent_listing_short_history"
            and bool(coverage_policy["recent_listing_short_history_is_rank_ready"])
        ) or (
            role["role_type"] == "current_universe"
            and len(price_rows) >= int(coverage_policy["sparse_history_rank_minimum_observations"])
            and invalid == 0
            and end_gap <= end_limit
            and ratio <= float(coverage_policy["sparse_history_rank_maximum_missing_session_ratio"])
            and longest
            <= int(coverage_policy["sparse_history_rank_maximum_consecutive_missing_sessions"])
        )
        results.append(
            {
                "audit_asof_date": as_of,
                "role_key": str(role["role_key"]),
                "instrument_id": int(role["instrument_id"]),
                "expected_start_date": expected_start,
                "expected_end_date": expected_end,
                "first_bar_date": first or None,
                "last_bar_date": last or None,
                "bar_count": len(price_rows),
                "expected_session_count": len(expected_sessions),
                "missing_session_count": len(missing),
                "missing_session_ratio": ratio,
                "longest_missing_session_gap": longest,
                "invalid_bar_count": invalid,
                "coverage_status": status,
                "rank_ready": int(rank_ready),
                "issue_detail": ";".join(reasons),
                "provider_source_id": str(role["provider_source_id"]),
                "snapshot_key": snapshot_key,
                "created_at_utc": now,
            }
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            """
            INSERT INTO fact_market_data_coverage (
                audit_asof_date, role_key, instrument_id, expected_start_date,
                expected_end_date, first_bar_date, last_bar_date, bar_count,
                expected_session_count, missing_session_count, missing_session_ratio,
                longest_missing_session_gap, invalid_bar_count, coverage_status,
                rank_ready, issue_detail, provider_source_id, snapshot_key, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(audit_asof_date, role_key) DO UPDATE SET
                instrument_id = excluded.instrument_id,
                expected_start_date = excluded.expected_start_date,
                expected_end_date = excluded.expected_end_date,
                first_bar_date = excluded.first_bar_date,
                last_bar_date = excluded.last_bar_date,
                bar_count = excluded.bar_count,
                expected_session_count = excluded.expected_session_count,
                missing_session_count = excluded.missing_session_count,
                missing_session_ratio = excluded.missing_session_ratio,
                longest_missing_session_gap = excluded.longest_missing_session_gap,
                invalid_bar_count = excluded.invalid_bar_count,
                coverage_status = excluded.coverage_status,
                rank_ready = excluded.rank_ready,
                issue_detail = excluded.issue_detail,
                provider_source_id = excluded.provider_source_id,
                snapshot_key = excluded.snapshot_key,
                created_at_utc = excluded.created_at_utc
            """,
            [tuple(row.values()) for row in results],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    gated_keys = {str(role["role_key"]) for role in roles if int(role["required_for_current_gate"])}
    gated = [row for row in results if row["role_key"] in gated_keys]
    gate_ratio = sum(row["rank_ready"] for row in gated) / len(gated) if gated else 0.0
    return {
        "coverage_rows": len(results),
        "coverage_status_counts": dict(sorted(Counter(row["coverage_status"] for row in results).items())),
        "current_gate_rows": len(gated),
        "current_gate_ready_rows": sum(row["rank_ready"] for row in gated),
        "current_gate_ratio": gate_ratio,
    }


def _price_frame(conn: sqlite3.Connection, instrument_id: int, as_of: str) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT bar_date, close, adjusted_close, volume
        FROM fact_adjusted_price_bar
        WHERE instrument_id = ? AND bar_date <= ?
        ORDER BY bar_date
        """,
        (instrument_id, as_of),
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["close", "adjusted_close", "volume"])
    frame = pd.DataFrame([dict(row) for row in rows])
    frame["bar_date"] = pd.to_datetime(frame["bar_date"])
    return frame.set_index("bar_date")


def _window_return(series: pd.Series, sessions: int) -> float | None:
    clean = series.dropna()
    if len(clean) <= sessions:
        return None
    return float(clean.iloc[-1] / clean.iloc[-sessions - 1] - 1.0)


def _finite_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def build_market_features(
    conn: sqlite3.Connection,
    *,
    policy: MarketDataPolicy,
    as_of: str,
    snapshot_key: str,
) -> dict[str, Any]:
    """Build one point-in-time technical feature row per current security."""

    assert_database_identity(conn)
    date.fromisoformat(as_of)
    features_policy = policy.payload["features"]
    coverage_policy = policy.payload["coverage"]
    benchmark_rows = conn.execute(
        """
        SELECT role_type, instrument_id
        FROM bridge_market_instrument_role
        WHERE role_type IN ('sector_benchmark', 'broad_benchmark')
        """
    ).fetchall()
    benchmark_ids = {str(row["role_type"]): int(row["instrument_id"]) for row in benchmark_rows}
    if set(benchmark_ids) != {"sector_benchmark", "broad_benchmark"}:
        raise RuntimeError("Both XLB and SPY benchmark roles are required")
    xlb = _price_frame(conn, benchmark_ids["sector_benchmark"], as_of)["adjusted_close"]
    spy = _price_frame(conn, benchmark_ids["broad_benchmark"], as_of)["adjusted_close"]

    roles = conn.execute(
        """
        SELECT r.security_id, r.instrument_id, r.model_ticker, i.provider_source_id
        FROM bridge_market_instrument_role AS r
        JOIN dim_market_instrument AS i ON i.instrument_id = r.instrument_id
        WHERE r.role_type = 'current_universe'
        ORDER BY r.model_ticker
        """
    ).fetchall()
    now = utc_now()
    output: list[dict[str, Any]] = []
    for role in roles:
        frame = _price_frame(conn, int(role["instrument_id"]), as_of)
        if frame.empty:
            raise RuntimeError(f"No market history for current ticker {role['model_ticker']}")
        adjusted = frame["adjusted_close"].astype(float)
        close = frame["close"].astype(float)
        history_days = len(frame)
        last_price_date = frame.index[-1].date().isoformat()
        staleness = (date.fromisoformat(as_of) - date.fromisoformat(last_price_date)).days
        reasons: list[str] = []
        if staleness > int(coverage_policy["active_max_staleness_calendar_days"]):
            quality = "stale"
            reasons.append(f"stale_calendar_days:{staleness}")
        elif history_days >= int(coverage_policy["minimum_rows_full"]):
            quality = "full"
        elif history_days >= int(coverage_policy["minimum_rows_partial"]):
            quality = "partial_history"
            reasons.append(f"history_days:{history_days}")
        else:
            quality = "insufficient_history"
            reasons.append(f"history_days:{history_days}")

        returns = adjusted.pct_change(fill_method=None).dropna()
        vol_window = int(features_policy["volatility_days"])
        recent_returns = returns.tail(vol_window)
        realized_vol = (
            float(recent_returns.std(ddof=1) * math.sqrt(252))
            if len(recent_returns) >= vol_window
            else None
        )
        downside_vol = (
            float(math.sqrt(float((recent_returns.clip(upper=0) ** 2).mean())) * math.sqrt(252))
            if len(recent_returns) >= vol_window
            else None
        )
        drawdown_days = int(features_policy["drawdown_days"])
        drawdown_prices = adjusted.tail(drawdown_days + 1)
        max_drawdown = (
            float((drawdown_prices / drawdown_prices.cummax() - 1.0).min())
            if len(drawdown_prices) >= 2
            else None
        )
        short_days = int(features_policy["short_moving_average_days"])
        long_days = int(features_policy["long_moving_average_days"])
        ma50 = float(adjusted.tail(short_days).mean()) if history_days >= short_days else None
        ma200 = float(adjusted.tail(long_days).mean()) if history_days >= long_days else None
        trend = int(ma50 > ma200) if ma50 is not None and ma200 is not None else None
        high_window = adjusted.tail(drawdown_days)
        distance_high = (
            float(adjusted.iloc[-1] / high_window.max() - 1.0) if len(high_window) else None
        )
        adv_days = int(features_policy["average_dollar_volume_days"])
        dollar_volume = close * frame["volume"].astype(float)
        adv = float(dollar_volume.tail(adv_days).mean()) if len(dollar_volume.dropna()) >= adv_days else None

        beta_days = int(features_policy["beta_days"])
        aligned = pd.concat(
            [returns.rename("stock"), spy.pct_change(fill_method=None).rename("spy")],
            axis=1,
            join="inner",
        ).dropna().tail(beta_days)
        beta = None
        if len(aligned) >= max(63, beta_days // 2):
            variance = float(aligned["spy"].var(ddof=1))
            if variance > 0:
                beta = float(aligned["stock"].cov(aligned["spy"]) / variance)

        return_126 = _window_return(adjusted, 126)
        spy_return_126 = _window_return(spy, int(features_policy["beta_residual_momentum_days"]))
        xlb_return_126 = _window_return(xlb, 126)
        beta_residual = (
            return_126 - beta * spy_return_126
            if return_126 is not None and beta is not None and spy_return_126 is not None
            else None
        )
        xlb_residual = (
            return_126 - xlb_return_126
            if return_126 is not None and xlb_return_126 is not None
            else None
        )
        momentum_days = int(features_policy["momentum_12m_days"])
        skip_days = int(features_policy["momentum_skip_days"])
        momentum = None
        if len(adjusted) > momentum_days:
            momentum = float(adjusted.iloc[-skip_days - 1] / adjusted.iloc[-momentum_days - 1] - 1.0)

        output.append(
            {
                "security_id": int(role["security_id"]),
                "instrument_id": int(role["instrument_id"]),
                "ticker": str(role["model_ticker"]),
                "asof_date": as_of,
                "provider_source_id": str(role["provider_source_id"]),
                "snapshot_key": snapshot_key,
                "adjusted_close": float(adjusted.iloc[-1]),
                "return_21d": _window_return(adjusted, 21),
                "return_63d": _window_return(adjusted, 63),
                "return_126d": return_126,
                "return_252d": _window_return(adjusted, 252),
                "momentum_12m_ex_1m": momentum,
                "xlb_residual_momentum": xlb_residual,
                "spy_beta_252d": beta,
                "spy_beta_residual_momentum_126d": beta_residual,
                "realized_volatility_63d": realized_vol,
                "downside_volatility_63d": downside_vol,
                "max_drawdown_252d": max_drawdown,
                "distance_from_52_week_high": distance_high,
                "moving_average_50d": ma50,
                "moving_average_200d": ma200,
                "trend_50_over_200": trend,
                "average_dollar_volume_63d": adv,
                "history_days": history_days,
                "history_start_date": frame.index[0].date().isoformat(),
                "last_price_date": last_price_date,
                "quality_status": quality,
                "quality_reasons_json": json.dumps(reasons, sort_keys=True),
                "feature_definition_version": str(features_policy["feature_definition_version"]),
                "created_at_utc": now,
                "updated_at_utc": now,
            }
        )

    columns = tuple(output[0]) if output else ()
    conn.execute("BEGIN IMMEDIATE")
    try:
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"security_id", "asof_date", "created_at_utc"}
        )
        conn.executemany(
            f"INSERT INTO feature_market_technical ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(security_id, asof_date) DO UPDATE SET {updates}",
            [tuple(_finite_or_none(value) if isinstance(value, float) else value for value in row.values()) for row in output],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "feature_rows": len(output),
        "feature_quality_counts": dict(sorted(Counter(row["quality_status"] for row in output).items())),
        "feature_definition_version": str(features_policy["feature_definition_version"]),
    }


def validate_market_stage(
    conn: sqlite3.Connection,
    *,
    policy: MarketDataPolicy,
    manifest: MarketDataManifest,
    as_of: str,
    snapshot_key: str | None = None,
) -> MarketValidationReport:
    """Validate the complete Stage 3 snapshot without mutating the database."""

    assert_database_identity(conn)
    date.fromisoformat(as_of)
    snapshot = (
        conn.execute(
            "SELECT * FROM fact_market_provider_snapshot WHERE snapshot_key = ?",
            (snapshot_key,),
        ).fetchone()
        if snapshot_key
        else latest_market_snapshot(conn, as_of=as_of)
    )
    if snapshot is None:
        raise RuntimeError("Requested market snapshot does not exist")
    selected_snapshot = str(snapshot["snapshot_key"])
    issues: list[MarketValidationIssue] = []
    expected_counts = {
        "market_instruments": policy.expected_unique_instruments,
        "market_roles": policy.files["market_instruments"].expected_rows,
        "terminal_rules": policy.files["terminal_return_rules"].expected_rows,
        "coverage_rows": policy.files["market_instruments"].expected_rows,
        "feature_rows": policy.expected_role_counts["current_universe"],
        "terminal_calculations": policy.files["terminal_return_rules"].expected_rows,
    }
    actual_counts = {
        "market_instruments": int(conn.execute("SELECT COUNT(*) FROM dim_market_instrument").fetchone()[0]),
        "market_roles": int(conn.execute("SELECT COUNT(*) FROM bridge_market_instrument_role").fetchone()[0]),
        "terminal_rules": int(conn.execute("SELECT COUNT(*) FROM dim_terminal_return_rule").fetchone()[0]),
        "coverage_rows": int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_market_data_coverage WHERE audit_asof_date = ?",
                (as_of,),
            ).fetchone()[0]
        ),
        "feature_rows": int(
            conn.execute(
                "SELECT COUNT(*) FROM feature_market_technical WHERE asof_date = ?",
                (as_of,),
            ).fetchone()[0]
        ),
        "terminal_calculations": int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_terminal_return_calculation WHERE calculation_asof_date = ?",
                (as_of,),
            ).fetchone()[0]
        ),
    }
    for name, expected in expected_counts.items():
        if actual_counts[name] != expected:
            issues.append(
                MarketValidationIssue(
                    "error",
                    "STAGE3_COUNT_MISMATCH",
                    f"{name} expected {expected} rows and found {actual_counts[name]}",
                )
            )

    if str(snapshot["contract_manifest_sha256"]) != manifest.checksum:
        issues.append(
            MarketValidationIssue(
                "error",
                "MARKET_SNAPSHOT_CONTRACT_MISMATCH",
                "Provider snapshot was not built from the current governed manifest",
            )
        )
    if int(snapshot["instrument_count"]) != policy.expected_unique_instruments:
        issues.append(
            MarketValidationIssue(
                "error",
                "MARKET_SNAPSHOT_INSTRUMENT_MISMATCH",
                "Provider snapshot instrument count differs from policy",
            )
        )
    snapshot_bars = int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_adjusted_price_bar WHERE snapshot_key = ?",
            (selected_snapshot,),
        ).fetchone()[0]
    )
    if snapshot_bars != int(snapshot["bar_count"]):
        issues.append(
            MarketValidationIssue(
                "error",
                "MARKET_SNAPSHOT_BAR_MISMATCH",
                f"Snapshot records {snapshot['bar_count']} bars but {snapshot_bars} are linked",
            )
        )

    current_gate = conn.execute(
        """
        SELECT COUNT(*) AS total, COALESCE(SUM(c.rank_ready), 0) AS ready
        FROM bridge_market_instrument_role AS r
        LEFT JOIN fact_market_data_coverage AS c
          ON c.role_key = r.role_key AND c.audit_asof_date = ?
        WHERE r.required_for_current_gate = 1
        """,
        (as_of,),
    ).fetchone()
    gate_total = int(current_gate["total"])
    gate_ready = int(current_gate["ready"])
    gate_ratio = gate_ready / gate_total if gate_total else 0.0
    if gate_ratio < float(policy.payload["coverage"]["current_gate_minimum_ratio"]):
        issues.append(
            MarketValidationIssue(
                "error",
                "CURRENT_MARKET_COVERAGE_GATE_FAILED",
                f"Rank-ready current and benchmark roles are {gate_ready}/{gate_total} ({gate_ratio:.2%})",
            )
        )
    failed_gate_rows = conn.execute(
        """
        SELECT r.model_ticker, c.coverage_status, c.issue_detail
        FROM bridge_market_instrument_role AS r
        JOIN fact_market_data_coverage AS c
          ON c.role_key = r.role_key AND c.audit_asof_date = ?
        WHERE r.required_for_current_gate = 1 AND c.rank_ready = 0
        ORDER BY r.model_ticker
        """,
        (as_of,),
    ).fetchall()
    for row in failed_gate_rows:
        issues.append(
            MarketValidationIssue(
                "warning",
                "CURRENT_MARKET_ROLE_NOT_RANK_READY",
                f"{row['coverage_status']}: {row['issue_detail']}",
                str(row["model_ticker"]),
            )
        )

    future_prices = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM fact_terminal_return_calculation
            WHERE calculation_asof_date = ?
              AND (COALESCE(historical_final_price_date, '') > calculation_asof_date
                   OR COALESCE(successor_reference_price_date, '') > calculation_asof_date
                   OR no_future_price_used <> 1)
            """,
            (as_of,),
        ).fetchone()[0]
    )
    if future_prices:
        issues.append(
            MarketValidationIssue(
                "error",
                "TERMINAL_LOOKAHEAD_DETECTED",
                f"Found {future_prices} terminal calculations with a future-price violation",
            )
        )
    terminal = conn.execute(
        """
        SELECT COALESCE(SUM(resolved), 0) AS resolved, COUNT(*) AS total
        FROM fact_terminal_return_calculation
        WHERE calculation_asof_date = ?
        """,
        (as_of,),
    ).fetchone()
    resolved = int(terminal["resolved"])
    terminal_total = int(terminal["total"])
    unresolved = terminal_total - resolved
    expected_resolved = int(
        conn.execute(
            "SELECT COUNT(*) FROM dim_terminal_return_rule WHERE rule_status = 'ready_for_calculation'"
        ).fetchone()[0]
    )
    if terminal_total == expected_counts["terminal_calculations"] and resolved != expected_resolved:
        issues.append(
            MarketValidationIssue(
                "error",
                "TERMINAL_RESOLUTION_INCOMPLETE",
                f"Expected {expected_resolved} calculable terminal events and resolved {resolved}",
            )
        )
    unresolved_reconciliations = int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_terminal_event_reconciliation WHERE resolved = 0"
        ).fetchone()[0]
    )
    if unresolved_reconciliations != unresolved:
        issues.append(
            MarketValidationIssue(
                "error",
                "TERMINAL_RECONCILIATION_STATE_MISMATCH",
                "Stage 2B reconciliation flags differ from Stage 3 calculations",
            )
        )

    calibration_rows = int(
        conn.execute(
            "SELECT COUNT(*) FROM dim_universe_membership WHERE calibration_eligible <> 0"
        ).fetchone()[0]
    )
    if calibration_rows:
        issues.append(
            MarketValidationIssue(
                "error",
                "CALIBRATION_GATE_OPEN",
                f"Found {calibration_rows} calibration-eligible memberships",
            )
        )
    bad_contract_hashes = int(
        conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM dim_market_instrument WHERE contract_sha256 <> ?) +
              (SELECT COUNT(*) FROM bridge_market_instrument_role WHERE contract_sha256 <> ?) +
              (SELECT COUNT(*) FROM dim_terminal_return_rule WHERE contract_sha256 <> ?)
            """,
            (
                manifest.artifacts["market_instruments"].sha256,
                manifest.artifacts["market_instruments"].sha256,
                manifest.artifacts["terminal_return_rules"].sha256,
            ),
        ).fetchone()[0]
    )
    if bad_contract_hashes:
        issues.append(
            MarketValidationIssue(
                "error",
                "STAGE3_CONTRACT_HASH_MISMATCH",
                f"Found {bad_contract_hashes} rows with stale governed-contract hashes",
            )
        )
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        issues.append(
            MarketValidationIssue(
                "error",
                "FOREIGN_KEY_FAILURE",
                f"Found {len(foreign_keys)} foreign-key violations",
            )
        )
    if unresolved:
        issues.append(
            MarketValidationIssue(
                "warning",
                "BANKRUPTCY_DISTRIBUTIONS_PENDING",
                f"{unresolved} terminal events remain excluded pending old-equity distribution evidence",
            )
        )
    issues.append(
        MarketValidationIssue(
            "warning",
            "CALIBRATION_GATE_CLOSED",
            "Stage 3 engineering outputs do not activate historical calibration or portfolio use",
        )
    )
    coverage_counts = {
        str(row["coverage_status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT coverage_status, COUNT(*) AS count
            FROM fact_market_data_coverage WHERE audit_asof_date = ?
            GROUP BY coverage_status ORDER BY coverage_status
            """,
            (as_of,),
        ).fetchall()
    }
    feature_counts = {
        str(row["quality_status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT quality_status, COUNT(*) AS count
            FROM feature_market_technical WHERE asof_date = ?
            GROUP BY quality_status ORDER BY quality_status
            """,
            (as_of,),
        ).fetchall()
    }
    return MarketValidationReport(
        passed=not any(issue.severity == "error" for issue in issues),
        validated_at_utc=utc_now(),
        as_of_date=as_of,
        policy_version=policy.policy_version,
        manifest_checksum=manifest.checksum,
        snapshot_key=selected_snapshot,
        expected_counts=expected_counts,
        actual_counts=actual_counts,
        coverage_status_counts=coverage_counts,
        feature_quality_counts=feature_counts,
        current_gate_ratio=gate_ratio,
        resolved_terminal_events=resolved,
        unresolved_terminal_events=unresolved,
        issues=tuple(issues),
    )


def _query_dicts(conn: sqlite3.Connection, query: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, parameters).fetchall()]


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_market_validation_reports(
    conn: sqlite3.Connection,
    report: MarketValidationReport,
    *,
    report_dir: str | Path,
) -> dict[str, str]:
    """Publish a deterministic Stage 3 evidence pack atomically."""

    target = Path(report_dir).resolve(strict=False)
    target.mkdir(parents=True, exist_ok=True)
    as_of = report.as_of_date
    coverage = _query_dicts(
        conn,
        """
        SELECT c.*, r.role_type, r.model_ticker
        FROM fact_market_data_coverage AS c
        JOIN bridge_market_instrument_role AS r ON r.role_key = c.role_key
        WHERE c.audit_asof_date = ? ORDER BY r.role_type, r.model_ticker, c.role_key
        """,
        (as_of,),
    )
    features = _query_dicts(
        conn,
        "SELECT * FROM feature_market_technical WHERE asof_date = ? ORDER BY ticker",
        (as_of,),
    )
    terminal = _query_dicts(
        conn,
        """
        SELECT c.*, r.outcome_class, r.rule_status
        FROM fact_terminal_return_calculation AS c
        JOIN dim_terminal_return_rule AS r ON r.event_key = c.event_key
        WHERE c.calculation_asof_date = ? ORDER BY c.event_key
        """,
        (as_of,),
    )
    written: dict[str, Path] = {}
    written["summary"] = atomic_write_json(target / "market_validation_summary.json", report.summary_dict())
    written["issues"] = atomic_write_csv(
        target / "market_validation_issues.csv",
        (issue.as_dict() for issue in report.issues),
        ("severity", "issue_code", "message", "ticker"),
    )
    if coverage:
        written["coverage"] = atomic_write_csv(
            target / "market_coverage.csv", coverage, tuple(coverage[0])
        )
    if features:
        written["features"] = atomic_write_csv(
            target / "market_features.csv", features, tuple(features[0])
        )
    if terminal:
        written["terminal_returns"] = atomic_write_csv(
            target / "terminal_return_calculations.csv", terminal, tuple(terminal[0])
        )
    artifacts = {
        name: {
            "path": str(path),
            "sha256": _file_sha256(path),
            "byte_size": path.stat().st_size,
        }
        for name, path in written.items()
    }
    written["artifact_manifest"] = atomic_write_json(
        target / "artifact_manifest.json",
        {
            "model_family": MODEL_FAMILY,
            "sector": SECTOR,
            "stage": "stage_3_adjusted_market_data",
            "as_of_date": as_of,
            "generated_at_utc": report.validated_at_utc,
            "database_counts": database_counts(conn),
            "artifacts": artifacts,
        },
    )
    return {name: str(path) for name, path in written.items()}
