#!/usr/bin/env python3
"""Build sealed, provider-separated estimate and calendar diagnostics."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.estimate_policy import (  # noqa: E402
    CanonicalEstimate,
    canonicalize_snapshot,
    relative_difference,
)
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    artifact_snapshot_dependency_errors,
    connect_monitor_db,
    record_snapshot_dependencies,
    supersede_artifact_dependencies,
    writer_lock,
)
from portfolio_layer.provider_ingestion.health import (  # noqa: E402
    CONTINUITY_FIELDS,
    capture_continuity_rows,
    continuity_gaps,
    expected_capture_slots,
    validate_provider_ingestion_policy,
)
from portfolio_layer.provider_ingestion.store import (  # noqa: E402
    artifact_dependency_errors as provider_artifact_dependency_errors,
    connect_store,
    record_artifact_dependencies as record_provider_artifact_dependencies,
    supersede_artifact_dependencies as supersede_provider_artifact_dependencies,
    writer_lock as provider_writer_lock,
)

DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PROVIDERS = ("alpha_vantage", "fmp")
TIERS = ("tier0", "tier1", "tier2")
REVISION_FIELDS = [
    "as_of_date",
    "universe_as_of",
    "ticker",
    "tier",
    "sector",
    "source_pipeline",
    "provider",
    "metric",
    "canonical_period",
    "fiscal_period_end",
    "snapshot_id",
    "available_at_utc",
    "estimate_average",
    "analyst_count",
    "prior_30d_value",
    "prior_30d_source",
    "prior_30d_snapshot_id",
    "revision_30d_absolute",
    "revision_30d_relative",
    "revision_30d_direction",
    "revision_30d_basis_status",
    "prior_90d_value",
    "prior_90d_source",
    "prior_90d_snapshot_id",
    "revision_90d_absolute",
    "revision_90d_relative",
    "revision_90d_direction",
    "revision_90d_basis_status",
    "revision_up_30_days",
    "revision_down_30_days",
    "revision_count_net_30d",
    "revision_trend",
    "economic_use",
]
UNCERTAINTY_FIELDS = [
    "as_of_date",
    "universe_as_of",
    "ticker",
    "tier",
    "sector",
    "source_pipeline",
    "metric",
    "canonical_period",
    "fiscal_period_end",
    "provider_count",
    "alpha_snapshot_id",
    "alpha_estimate_average",
    "alpha_analyst_count",
    "fmp_snapshot_id",
    "fmp_estimate_average",
    "fmp_analyst_count",
    "relative_difference",
    "disagreement_flag",
    "coverage_confidence_status",
    "levels_uncertainty_penalty_status",
    "economic_use",
]
COVERAGE_FIELDS = [
    "as_of_date",
    "universe_as_of",
    "provider",
    "tier",
    "expected_tickers",
    "covered_tickers",
    "fresh_tickers",
    "missing_tickers",
    "stale_tickers",
    "coverage_fraction",
    "fresh_coverage_fraction",
    "snapshot_age_p50_calendar_days",
    "snapshot_age_p90_calendar_days",
    "snapshot_age_max_calendar_days",
    "maximum_age_calendar_days",
    "hard_floor",
    "warning_floor",
    "hard_fail_tier",
    "status",
    "detail",
]
EARNINGS_DRIFT_FIELDS = [
    "as_of_date",
    "universe_as_of",
    "ticker",
    "tier",
    "sector",
    "source_pipeline",
    "current_earnings_date",
    "prior_earnings_date",
    "date_change_days",
    "drift_class",
    "calendar_source",
    "source_confidence",
    "cross_provider_confirmation_status",
    "economic_use",
]
FISCAL_AUDIT_FIELDS = [
    "as_of_date",
    "metric",
    "provider",
    "status",
    "value",
    "detail",
    "economic_use",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--universe-as-of", type=date.fromisoformat)
    parser.add_argument("--earnings-history", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Provider timestamp lacks timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _cutoff_utc(
    as_of: date,
    timezone_name: str,
    *,
    now_utc: datetime | None = None,
) -> datetime:
    local_zone = ZoneInfo(timezone_name)
    current_utc = now_utc or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        raise ValueError("Provider diagnostics clock must include a timezone")
    current_utc = current_utc.astimezone(timezone.utc)
    if as_of > current_utc.astimezone(local_zone).date():
        raise ValueError("Provider diagnostics as-of date cannot be in the future")
    next_midnight = datetime.combine(as_of + timedelta(days=1), time.min, local_zone)
    return min(next_midnight.astimezone(timezone.utc), current_utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _revision_measure(
    current: float,
    prior: float | None,
    *,
    floor: float,
    flat_band: float,
) -> dict[str, float | str]:
    if floor <= 0 or flat_band < 0:
        raise ValueError("Revision floors must be positive and flat_band non-negative")
    if prior is None:
        return {
            "absolute": "",
            "relative": "",
            "direction": "unavailable",
            "basis_status": "unavailable",
        }
    absolute = current - prior
    relative = absolute / max(abs(current), abs(prior), floor)
    direction = "flat" if abs(relative) <= flat_band else "up" if absolute > 0 else "down"
    if current * prior < 0 or (current == 0) != (prior == 0):
        basis_status = "sign_crossing"
    elif max(abs(current), abs(prior)) < floor:
        basis_status = "small_denominator_floor"
    else:
        basis_status = "comparable"
    return {
        "absolute": absolute,
        "relative": relative,
        "direction": direction,
        "basis_status": basis_status,
    }


def _percentile(values: list[int], quantile: float) -> float | str:
    if not values:
        return ""
    if not 0 <= quantile <= 1:
        raise ValueError("Percentile quantile must be between zero and one")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _revision_trend(direction_30d: str, direction_90d: str) -> str:
    available = [value for value in (direction_30d, direction_90d) if value != "unavailable"]
    if not available:
        return "insufficient_history"
    if len(available) == 1:
        return f"single_horizon_{available[0]}"
    return f"sustained_{direction_30d}" if direction_30d == direction_90d else "mixed"


def _drift_class(current: str, prior: str) -> tuple[str, int | str]:
    if not current:
        return "missing_current_date", ""
    if not prior:
        return "first_observation", ""
    try:
        delta = (date.fromisoformat(current) - date.fromisoformat(prior)).days
    except ValueError:
        return "invalid_date", ""
    if delta > 0:
        return "delayed", delta
    if delta < 0:
        return "advanced", delta
    return "unchanged", 0


def _coverage_status(
    *,
    fresh_fraction: float,
    hard_floor: float,
    warning_floor: float,
    hard_fail_tier: bool,
) -> str:
    if not 0 <= hard_floor <= warning_floor <= 1:
        raise ValueError("Coverage floors must satisfy 0 <= hard <= warning <= 1")
    if hard_fail_tier and fresh_fraction < hard_floor:
        return "FAIL"
    return "WARN" if fresh_fraction < warning_floor else "PASS"


def _select_active_estimates(rows: list[sqlite3.Row], *, as_of: date) -> list[tuple[dict[str, Any], CanonicalEstimate]]:
    grouped: dict[tuple[str, str, str, str], list[tuple[dict[str, Any], CanonicalEstimate]]] = defaultdict(list)
    for raw in rows:
        row = dict(raw)
        canonical = canonicalize_snapshot(row)
        key = (
            canonical.provider,
            canonical.ticker,
            canonical.metric,
            canonical.canonical_period,
        )
        grouped[key].append((row, canonical))
    selected: list[tuple[dict[str, Any], CanonicalEstimate]] = []
    for candidates in grouped.values():
        future = [item for item in candidates if date.fromisoformat(item[1].fiscal_period_end) >= as_of]
        choice = (
            min(future, key=lambda item: item[1].fiscal_period_end)
            if future
            else max(candidates, key=lambda item: item[1].fiscal_period_end)
        )
        selected.append(choice)
    return sorted(
        selected,
        key=lambda item: (
            item[1].ticker,
            item[1].provider,
            item[1].metric,
            item[1].canonical_period,
        ),
    )


def _latest_snapshot_rows(
    conn: sqlite3.Connection,
    *,
    snapshot_table: str,
    universe_as_of: str,
    cutoff_utc: str,
    minimum_period_end: str,
    effective_as_of: str | None = None,
) -> list[sqlite3.Row]:
    effective_clause = " AND s.effective_trading_date<=? AND s.effective_from_utc<=?" if effective_as_of else ""
    params: tuple[str, ...] = (universe_as_of, cutoff_utc, minimum_period_end)
    if effective_as_of:
        params = (*params, effective_as_of, cutoff_utc)
    return conn.execute(
        "WITH ranked AS ("
        " SELECT s.*,ROW_NUMBER() OVER ("
        "  PARTITION BY s.provider,s.ticker,s.endpoint_id,s.fiscal_period,"
        "s.estimate_type,s.fiscal_period_end"
        "  ORDER BY s.available_at_utc DESC,s.snapshot_id DESC"
        " ) AS row_rank"
        f" FROM {snapshot_table} s"
        " JOIN monitor_universe u ON u.run_as_of=? AND u.ticker=s.ticker"
        " WHERE s.available_at_utc<? AND s.fiscal_period_end>=?"
        "   AND s.coverage_status='available'"
        f"{effective_clause}"
        ") SELECT * FROM ranked WHERE row_rank=1"
        " ORDER BY provider,ticker,endpoint_id,fiscal_period,estimate_type,fiscal_period_end,snapshot_id",
        params,
    ).fetchall()


def _local_prior(
    conn: sqlite3.Connection,
    *,
    snapshot_table: str,
    row: dict[str, Any],
    horizon_days: int,
    provider_first_seen: dict[str, datetime],
) -> tuple[float | None, str]:
    current_available = _parse_timestamp(str(row["available_at_utc"]))
    cutoff = current_available - timedelta(days=horizon_days)
    provider = str(row["provider"])
    if provider_first_seen.get(provider, current_available) > cutoff:
        return None, ""
    prior = conn.execute(
        f"SELECT snapshot_id,estimate_average FROM {snapshot_table} "
        "WHERE provider=? AND ticker=? AND estimate_type=? AND fiscal_period_end=? "
        "AND coverage_status='available' AND estimate_average IS NOT NULL "
        "AND available_at_utc<=? ORDER BY available_at_utc DESC,snapshot_id DESC LIMIT 1",
        (
            provider,
            str(row["ticker"]),
            str(row["estimate_type"]),
            str(row["fiscal_period_end"]),
            _iso_utc(cutoff),
        ),
    ).fetchone()
    if prior is None:
        return None, ""
    return _optional_float(prior["estimate_average"]), str(prior["snapshot_id"])


def _prior_value(
    conn: sqlite3.Connection,
    *,
    snapshot_table: str,
    row: dict[str, Any],
    horizon_days: int,
    provider_first_seen: dict[str, datetime],
) -> tuple[float | None, str, str]:
    local_value, local_snapshot = _local_prior(
        conn,
        snapshot_table=snapshot_table,
        row=row,
        horizon_days=horizon_days,
        provider_first_seen=provider_first_seen,
    )
    if local_value is not None:
        return local_value, "locally_observed_snapshot", local_snapshot
    embedded = _optional_float(row.get(f"estimate_average_{horizon_days}_days_ago"))
    if embedded is not None:
        return embedded, "provider_embedded_lookback", ""
    return None, "unavailable", ""


def _build_revision_rows(
    conn: sqlite3.Connection,
    *,
    snapshot_table: str,
    selected: list[tuple[dict[str, Any], CanonicalEstimate]],
    universe: dict[str, dict[str, Any]],
    as_of: str,
    universe_as_of: str,
    floors: dict[str, float],
    flat_band: float,
    provider_first_seen: dict[str, datetime],
) -> tuple[list[dict[str, Any]], set[str]]:
    output: list[dict[str, Any]] = []
    snapshot_ids: set[str] = set()
    for raw, estimate in selected:
        snapshot_ids.add(estimate.snapshot_id)
        current = estimate.estimate_average
        if current is None or estimate.quality_status == "FAIL":
            continue
        values: dict[int, tuple[float | None, str, str]] = {}
        measures: dict[int, dict[str, float | str]] = {}
        for horizon in (30, 90):
            values[horizon] = _prior_value(
                conn,
                snapshot_table=snapshot_table,
                row=raw,
                horizon_days=horizon,
                provider_first_seen=provider_first_seen,
            )
            prior, _, prior_snapshot_id = values[horizon]
            if prior_snapshot_id:
                snapshot_ids.add(prior_snapshot_id)
            measures[horizon] = _revision_measure(
                current,
                prior,
                floor=floors[estimate.metric],
                flat_band=flat_band,
            )
        info = universe[estimate.ticker]
        up_30 = _optional_int(raw.get("revision_up_30_days"))
        down_30 = _optional_int(raw.get("revision_down_30_days"))
        output.append(
            {
                "as_of_date": as_of,
                "universe_as_of": universe_as_of,
                "ticker": estimate.ticker,
                "tier": info["tier"],
                "sector": info["sector"],
                "source_pipeline": info["source_pipeline"],
                "provider": estimate.provider,
                "metric": estimate.metric,
                "canonical_period": estimate.canonical_period,
                "fiscal_period_end": estimate.fiscal_period_end,
                "snapshot_id": estimate.snapshot_id,
                "available_at_utc": str(raw["available_at_utc"]),
                "estimate_average": current,
                "analyst_count": "" if estimate.analyst_count is None else estimate.analyst_count,
                "prior_30d_value": "" if values[30][0] is None else values[30][0],
                "prior_30d_source": values[30][1],
                "prior_30d_snapshot_id": values[30][2],
                "revision_30d_absolute": measures[30]["absolute"],
                "revision_30d_relative": measures[30]["relative"],
                "revision_30d_direction": measures[30]["direction"],
                "revision_30d_basis_status": measures[30]["basis_status"],
                "prior_90d_value": "" if values[90][0] is None else values[90][0],
                "prior_90d_source": values[90][1],
                "prior_90d_snapshot_id": values[90][2],
                "revision_90d_absolute": measures[90]["absolute"],
                "revision_90d_relative": measures[90]["relative"],
                "revision_90d_direction": measures[90]["direction"],
                "revision_90d_basis_status": measures[90]["basis_status"],
                "revision_up_30_days": "" if up_30 is None else up_30,
                "revision_down_30_days": "" if down_30 is None else down_30,
                "revision_count_net_30d": ("" if up_30 is None or down_30 is None else up_30 - down_30),
                "revision_trend": _revision_trend(str(measures[30]["direction"]), str(measures[90]["direction"])),
                "economic_use": "diagnostic_only_not_les_or_levels",
            }
        )
    return output, snapshot_ids


def _build_uncertainty_rows(
    *,
    selected: list[tuple[dict[str, Any], CanonicalEstimate]],
    universe: dict[str, dict[str, Any]],
    as_of: str,
    universe_as_of: str,
    warn_relative: dict[str, float],
    floors: dict[str, float],
    minimum_analyst_count: int,
) -> list[dict[str, Any]]:
    paired: dict[tuple[str, str, str, str], dict[str, CanonicalEstimate]] = defaultdict(dict)
    for _, estimate in selected:
        if estimate.estimate_average is not None and estimate.quality_status != "FAIL":
            key = (
                estimate.ticker,
                estimate.metric,
                estimate.canonical_period,
                estimate.fiscal_period_end,
            )
            paired[key][estimate.provider] = estimate
    output: list[dict[str, Any]] = []
    for key, providers in sorted(paired.items()):
        ticker, metric, canonical_period, fiscal_period_end = key
        alpha = providers.get("alpha_vantage")
        fmp = providers.get("fmp")
        relative: float | str = ""
        disagreement = 0
        if alpha is not None and fmp is not None:
            assert alpha.estimate_average is not None
            assert fmp.estimate_average is not None
            relative = relative_difference(
                alpha.estimate_average,
                fmp.estimate_average,
                floor=floors[metric],
            )
            disagreement = int(relative >= warn_relative[metric])
        counts = [
            estimate.analyst_count
            for estimate in (alpha, fmp)
            if estimate is not None and estimate.analyst_count is not None
        ]
        if len(providers) == 2 and len(counts) == 2 and all(count >= minimum_analyst_count for count in counts):
            confidence_status = "two_providers_counts_above_minimum"
        elif len(providers) == 2:
            confidence_status = "two_providers_count_missing_or_below_minimum"
        else:
            confidence_status = "single_provider"
        info = universe[ticker]
        output.append(
            {
                "as_of_date": as_of,
                "universe_as_of": universe_as_of,
                "ticker": ticker,
                "tier": info["tier"],
                "sector": info["sector"],
                "source_pipeline": info["source_pipeline"],
                "metric": metric,
                "canonical_period": canonical_period,
                "fiscal_period_end": fiscal_period_end,
                "provider_count": len(providers),
                "alpha_snapshot_id": alpha.snapshot_id if alpha else "",
                "alpha_estimate_average": "" if alpha is None else alpha.estimate_average,
                "alpha_analyst_count": ("" if alpha is None or alpha.analyst_count is None else alpha.analyst_count),
                "fmp_snapshot_id": fmp.snapshot_id if fmp else "",
                "fmp_estimate_average": "" if fmp is None else fmp.estimate_average,
                "fmp_analyst_count": ("" if fmp is None or fmp.analyst_count is None else fmp.analyst_count),
                "relative_difference": relative,
                "disagreement_flag": disagreement,
                "coverage_confidence_status": confidence_status,
                "levels_uncertainty_penalty_status": "deferred_pending_calibration",
                "economic_use": "diagnostic_only_no_provider_averaging",
            }
        )
    return output


def _build_coverage_rows(
    *,
    selected: list[tuple[dict[str, Any], CanonicalEstimate]],
    universe: dict[str, dict[str, Any]],
    as_of: date,
    universe_as_of: str,
    timezone_name: str,
    maximum_age: dict[str, int],
    hard_floors: dict[str, float],
    warning_floors: dict[str, float],
    hard_fail_tiers: set[str],
) -> list[dict[str, Any]]:
    local_zone = ZoneInfo(timezone_name)
    latest: dict[tuple[str, str], datetime] = {}
    for raw, estimate in selected:
        if estimate.estimate_average is None or estimate.quality_status == "FAIL":
            continue
        key = (estimate.provider, estimate.ticker)
        available = _parse_timestamp(str(raw["available_at_utc"]))
        latest[key] = max(latest.get(key, available), available)
    by_tier: dict[str, set[str]] = defaultdict(set)
    for ticker, info in universe.items():
        by_tier[str(info["tier"])].add(ticker)
    output: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        for tier in TIERS:
            expected_names = by_tier[tier]
            expected = len(expected_names)
            covered = fresh = stale = 0
            ages: list[int] = []
            for ticker in expected_names:
                available = latest.get((provider, ticker))
                if available is None:
                    continue
                covered += 1
                age = (as_of - available.astimezone(local_zone).date()).days
                ages.append(age)
                if 0 <= age <= maximum_age[tier]:
                    fresh += 1
                else:
                    stale += 1
            coverage_fraction = covered / expected if expected else 1.0
            fresh_fraction = fresh / expected if expected else 1.0
            status = _coverage_status(
                fresh_fraction=fresh_fraction,
                hard_floor=hard_floors[tier],
                warning_floor=warning_floors[tier],
                hard_fail_tier=tier in hard_fail_tiers,
            )
            output.append(
                {
                    "as_of_date": as_of.isoformat(),
                    "universe_as_of": universe_as_of,
                    "provider": provider,
                    "tier": tier,
                    "expected_tickers": expected,
                    "covered_tickers": covered,
                    "fresh_tickers": fresh,
                    "missing_tickers": expected - covered,
                    "stale_tickers": stale,
                    "coverage_fraction": coverage_fraction,
                    "fresh_coverage_fraction": fresh_fraction,
                    "snapshot_age_p50_calendar_days": _percentile(ages, 0.50),
                    "snapshot_age_p90_calendar_days": _percentile(ages, 0.90),
                    "snapshot_age_max_calendar_days": max(ages) if ages else "",
                    "maximum_age_calendar_days": maximum_age[tier],
                    "hard_floor": hard_floors[tier],
                    "warning_floor": warning_floors[tier],
                    "hard_fail_tier": int(tier in hard_fail_tiers),
                    "status": status,
                    "detail": (
                        f"fresh={fresh}/{expected}; covered={covered}/{expected}; "
                        f"missing={expected - covered}; stale={stale}"
                    ),
                }
            )
    return output


def _build_earnings_drift_rows(
    path: Path,
    *,
    universe: dict[str, dict[str, Any]],
    as_of: date,
    universe_as_of: str,
) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, str]] = {}
    if path.is_file():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ticker = str(row.get("ticker", "")).strip().upper()
                run_date = str(row.get("run_as_of_date", "")).strip()
                if ticker not in universe or not run_date:
                    continue
                try:
                    if date.fromisoformat(run_date) > as_of:
                        continue
                except ValueError:
                    continue
                prior = latest.get(ticker)
                ordering = (run_date, str(row.get("fetched_at_utc", "")))
                prior_ordering = (
                    (
                        str(prior.get("run_as_of_date", "")),
                        str(prior.get("fetched_at_utc", "")),
                    )
                    if prior
                    else ("", "")
                )
                if ordering >= prior_ordering:
                    latest[ticker] = {str(key): str(value or "") for key, value in row.items()}
    output: list[dict[str, Any]] = []
    for ticker, info in sorted(universe.items()):
        row = latest.get(ticker, {})
        current = str(row.get("next_earnings_date", "")).strip()
        prior = str(row.get("prior_next_earnings_date", "")).strip()
        drift, delta = _drift_class(current, prior)
        output.append(
            {
                "as_of_date": as_of.isoformat(),
                "universe_as_of": universe_as_of,
                "ticker": ticker,
                "tier": info["tier"],
                "sector": info["sector"],
                "source_pipeline": info["source_pipeline"],
                "current_earnings_date": current,
                "prior_earnings_date": prior,
                "date_change_days": delta,
                "drift_class": drift,
                "calendar_source": str(row.get("source", "none")),
                "source_confidence": str(row.get("confidence", "")),
                "cross_provider_confirmation_status": ("unavailable_single_calendar_surface"),
                "economic_use": "diagnostic_only_no_delay_sign_assumption",
            }
        )
    return output


def _build_fiscal_audit_rows(conn: sqlite3.Connection, *, as_of: str, cutoff_utc: str) -> list[dict[str, Any]]:
    resolutions = conn.execute(
        "WITH ranked AS ("
        " SELECT *,ROW_NUMBER() OVER ("
        "  PARTITION BY source_provider,ticker,report_date,fiscal_period_end"
        "  ORDER BY available_at_utc DESC,resolution_id DESC"
        " ) row_rank FROM provider_fiscal_period_resolutions WHERE available_at_utc<?"
        ") SELECT * FROM ranked WHERE row_rank=1",
        (cutoff_utc,),
    ).fetchall()
    links = conn.execute(
        "WITH ranked AS ("
        " SELECT *,ROW_NUMBER() OVER ("
        "  PARTITION BY snapshot_id,outcome_id,resolution_id"
        "  ORDER BY linked_at_utc DESC,link_id DESC"
        " ) row_rank FROM provider_forecast_outcome_links_v3 WHERE linked_at_utc<?"
        ") SELECT * FROM ranked WHERE row_rank=1",
        (cutoff_utc,),
    ).fetchall()
    eligible_resolutions = sum(int(row["resolution_eligible"]) for row in resolutions)
    available_resolutions = sum(row["coverage_status"] == "available" for row in resolutions)
    eligible_links = sum(row["evaluation_status"] == "eligible" for row in links)
    mismatch_links = sum(
        any(token in str(row["ineligibility_reasons"]).casefold() for token in ("period", "fiscal")) for row in links
    )
    status = "PASS" if links else "DEFERRED_NO_LINKED_OUTCOMES"
    metrics = [
        ("resolution_rows", "alpha_vantage", "PASS", len(resolutions), "latest exact period mappings"),
        (
            "resolution_eligible_rows",
            "alpha_vantage",
            "PASS",
            eligible_resolutions,
            "mappings eligible under exact-period policy",
        ),
        (
            "resolution_available_rows",
            "alpha_vantage",
            "PASS",
            available_resolutions,
            "mappings with available coverage",
        ),
        ("forecast_outcome_links", "all", status, len(links), "provider-matched v3 links"),
        (
            "eligible_forecast_outcome_links",
            "all",
            status,
            eligible_links,
            "links eligible for realized provider accuracy",
        ),
        ("fiscal_period_mismatch_links", "all", status, mismatch_links, "links rejected for fiscal/period mismatch"),
    ]
    return [
        {
            "as_of_date": as_of,
            "metric": metric,
            "provider": provider,
            "status": metric_status,
            "value": value,
            "detail": detail,
            "economic_use": "quality_monitoring_only",
        }
        for metric, provider, metric_status, value, detail in metrics
    ]


def run_selftest() -> None:
    up = _revision_measure(11.0, 10.0, floor=0.01, flat_band=0.005)
    assert up["direction"] == "up" and math.isclose(float(up["relative"]), 1 / 11)
    assert up["basis_status"] == "comparable"
    assert _revision_measure(-1.0, 1.0, floor=0.01, flat_band=0.005)["basis_status"] == "sign_crossing"
    assert _revision_measure(10.001, 10.0, floor=0.01, flat_band=0.005)["direction"] == "flat"
    assert _revision_trend("down", "down") == "sustained_down"
    assert _revision_trend("down", "up") == "mixed"
    assert _drift_class("2026-08-10", "2026-08-05") == ("delayed", 5)
    assert _drift_class("2026-08-01", "2026-08-05") == ("advanced", -4)
    assert (
        _coverage_status(
            fresh_fraction=0.89,
            hard_floor=0.90,
            warning_floor=0.95,
            hard_fail_tier=True,
        )
        == "FAIL"
    )
    assert (
        _coverage_status(
            fresh_fraction=0.89,
            hard_floor=0.0,
            warning_floor=0.95,
            hard_fail_tier=False,
        )
        == "WARN"
    )
    assert _percentile([0, 1, 2, 3], 0.50) == 1.5
    assert _cutoff_utc(
        date(2026, 7, 31),
        "America/Chicago",
        now_utc=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
    ) == datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
    assert _cutoff_utc(
        date(2026, 8, 3),
        "America/Chicago",
        now_utc=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
    ) == datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    print("provider diagnostics selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None or args.universe_as_of is None:
        raise ValueError("--as-of and --universe-as-of are required")
    if args.universe_as_of > args.as_of:
        raise ValueError("Universe date cannot be after diagnostics as-of date")

    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError("expectations_monitor config must be a mapping")
    ingestion_cfg = cfg_get(config, "provider_ingestion", {})
    if not isinstance(ingestion_cfg, dict):
        raise ValueError("provider_ingestion config must be a mapping")
    validate_provider_ingestion_policy(ingestion_cfg)
    external_provider_store = (
        ingestion_cfg.get("enabled") is True
        and ingestion_cfg.get("network_owner") == "independent_service"
        and "estimates" in ingestion_cfg.get("managed_capabilities", [])
    )
    policy = monitor_cfg.get("provider_diagnostics", {})
    if not isinstance(policy, dict) or policy.get("policy_version") != "provider_diagnostics_v1":
        raise ValueError("provider_diagnostics_v1 config is required")
    if (
        policy.get("provider_separation_required") is not True
        or policy.get("economic_signal_activation") != "diagnostic_only"
        or policy.get("no_provider_averaging") is not True
    ):
        raise ValueError("Provider diagnostics must remain separated and diagnostic-only")
    timezone_name = str(policy.get("timezone", "America/Chicago"))
    ZoneInfo(timezone_name)
    coverage_policy = policy.get("coverage", {})
    revision_policy = policy.get("revision_momentum", {})
    if not isinstance(coverage_policy, dict) or not isinstance(revision_policy, dict):
        raise ValueError("Provider diagnostics coverage/revision policies must be mappings")
    maximum_age = {key: int(value) for key, value in dict(coverage_policy.get("maximum_age_calendar_days", {})).items()}
    hard_floors = {key: float(value) for key, value in dict(coverage_policy.get("hard_floor_by_tier", {})).items()}
    warning_floors = {
        key: float(value) for key, value in dict(coverage_policy.get("warning_floor_by_tier", {})).items()
    }
    if set(maximum_age) != set(TIERS) or set(hard_floors) != set(TIERS) or set(warning_floors) != set(TIERS):
        raise ValueError("Coverage policy must define every monitor tier exactly")
    hard_fail_tiers = {str(value) for value in coverage_policy.get("hard_fail_tiers", [])}
    if not hard_fail_tiers <= set(TIERS):
        raise ValueError("Unknown hard-fail monitor tier")
    if any(value < 0 for value in maximum_age.values()):
        raise ValueError("Maximum snapshot ages must be non-negative")
    for tier in TIERS:
        _coverage_status(
            fresh_fraction=1.0,
            hard_floor=hard_floors[tier],
            warning_floor=warning_floors[tier],
            hard_fail_tier=tier in hard_fail_tiers,
        )
    if revision_policy.get("economic_use") != "diagnostic_only":
        raise ValueError("Revision momentum cannot affect LES before promotion evidence")
    if revision_policy.get("horizons_calendar_days") != [30, 90]:
        raise ValueError("Revision diagnostics require the frozen 30/90-day horizons")
    flat_band = float(revision_policy.get("relative_flat_band", 0.005))
    reconciliation_policy = monitor_cfg.get("provider_reconciliation", {})
    if not isinstance(reconciliation_policy, dict):
        raise ValueError("provider_reconciliation config must be a mapping")
    floors = {
        key: float(value) for key, value in dict(reconciliation_policy.get("relative_difference_floor", {})).items()
    }
    warn_relative = {
        key: float(value) for key, value in dict(reconciliation_policy.get("disagreement_warn_relative", {})).items()
    }
    if set(floors) != {"eps", "revenue"} or set(warn_relative) != {"eps", "revenue"}:
        raise ValueError("Reconciliation thresholds must cover eps and revenue")
    minimum_analyst_count = int(reconciliation_policy.get("minimum_analyst_count_per_provider", 2))
    if minimum_analyst_count < 1:
        raise ValueError("minimum_analyst_count_per_provider must be positive")
    active_period_grace_days = int(reconciliation_policy.get("active_period_grace_days", 90))

    db_path = ensure_not_prod_path(
        args.db.resolve()
        if args.db
        else resolve_path(
            monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"),
            base_dir=config_path.parent,
        ),
        label="expectations monitor database",
    )
    provider_store_path = ensure_not_prod_path(
        resolve_path(
            ingestion_cfg.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    snapshot_table = (
        "provider_store.provider_estimate_snapshots" if external_provider_store else "provider_estimate_snapshots"
    )
    earnings_history = (
        args.earnings_history.resolve()
        if args.earnings_history is not None
        else paths.output_dir / "earnings_dates" / "earnings_calendar_history.csv"
    )
    timeout = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    cutoff = _cutoff_utc(args.as_of, timezone_name)
    cutoff_text = _iso_utc(cutoff)
    conn = connect_monitor_db(db_path, timeout_sec=timeout)
    try:
        if external_provider_store:
            if not provider_store_path.is_file():
                raise FileNotFoundError(f"Independent provider observation store is missing: {provider_store_path}")
            provider_store_uri = provider_store_path.resolve().as_uri() + "?mode=ro"
            conn.execute("ATTACH DATABASE ? AS provider_store", (provider_store_uri,))
        conn.execute("BEGIN")
        universe_rows = conn.execute(
            "SELECT ticker,tier,sector,source_pipeline FROM monitor_universe WHERE run_as_of=? ORDER BY ticker",
            (args.universe_as_of.isoformat(),),
        ).fetchall()
        if not universe_rows:
            raise ValueError(f"No monitor universe exists for {args.universe_as_of.isoformat()}")
        universe = {
            str(row["ticker"]): {
                "tier": str(row["tier"]),
                "sector": str(row["sector"]),
                "source_pipeline": str(row["source_pipeline"]),
            }
            for row in universe_rows
        }
        raw_rows = _latest_snapshot_rows(
            conn,
            snapshot_table=snapshot_table,
            universe_as_of=args.universe_as_of.isoformat(),
            cutoff_utc=cutoff_text,
            minimum_period_end=(args.as_of - timedelta(days=active_period_grace_days)).isoformat(),
            effective_as_of=args.as_of.isoformat() if external_provider_store else None,
        )
        selected = _select_active_estimates(raw_rows, as_of=args.as_of)
        first_seen_rows = conn.execute(
            "SELECT provider,MIN(available_at_utc) first_seen "
            f"FROM {snapshot_table} WHERE available_at_utc<? GROUP BY provider",
            (cutoff_text,),
        ).fetchall()
        provider_first_seen = {
            str(row["provider"]): _parse_timestamp(str(row["first_seen"])) for row in first_seen_rows
        }
        revision_rows, snapshot_ids = _build_revision_rows(
            conn,
            snapshot_table=snapshot_table,
            selected=selected,
            universe=universe,
            as_of=args.as_of.isoformat(),
            universe_as_of=args.universe_as_of.isoformat(),
            floors=floors,
            flat_band=flat_band,
            provider_first_seen=provider_first_seen,
        )
        uncertainty_rows = _build_uncertainty_rows(
            selected=selected,
            universe=universe,
            as_of=args.as_of.isoformat(),
            universe_as_of=args.universe_as_of.isoformat(),
            warn_relative=warn_relative,
            floors=floors,
            minimum_analyst_count=minimum_analyst_count,
        )
        coverage_rows = _build_coverage_rows(
            selected=selected,
            universe=universe,
            as_of=args.as_of,
            universe_as_of=args.universe_as_of.isoformat(),
            timezone_name=timezone_name,
            maximum_age=maximum_age,
            hard_floors=hard_floors,
            warning_floors=warning_floors,
            hard_fail_tiers=hard_fail_tiers,
        )
        fiscal_rows = _build_fiscal_audit_rows(
            conn,
            as_of=args.as_of.isoformat(),
            cutoff_utc=cutoff_text,
        )
        continuity_rows: list[dict[str, Any]] = []
        if external_provider_store:
            recovery_cfg = ingestion_cfg.get("recovery", {})
            if not isinstance(recovery_cfg, dict):
                raise ValueError("provider_ingestion.recovery must be a mapping")
            schedules = ingestion_cfg.get("schedules", {})
            if not isinstance(schedules, dict):
                raise ValueError("provider_ingestion.schedules must be a mapping")
            continuity_start = max(
                date.fromisoformat(str(recovery_cfg.get("service_started_on", args.as_of.isoformat()))),
                args.as_of - timedelta(days=int(recovery_cfg.get("continuity_lookback_calendar_days", 8))),
            )
            provider_timezone = str(ingestion_cfg.get("timezone", "America/New_York"))
            cutoff_hour, cutoff_minute = (
                int(value) for value in str(ingestion_cfg.get("decision_cutoff_local", "09:25")).split(":")
            )
            continuity_cutoff = datetime.combine(
                args.as_of,
                time(cutoff_hour, cutoff_minute),
                tzinfo=ZoneInfo(provider_timezone),
            ).astimezone(timezone.utc)
            slots = expected_capture_slots(
                start=continuity_start,
                end=args.as_of,
                now_utc=continuity_cutoff,
                schedules=schedules,
                timezone_name=provider_timezone,
                calendar_name=str(ingestion_cfg.get("exchange_calendar", "XNYS")),
                grace_minutes=ingestion_cfg.get(
                    "phase_grace_minutes",
                    int(ingestion_cfg.get("schedule_grace_minutes", 20)),
                ),
                service_started_on=date.fromisoformat(
                    str(recovery_cfg.get("service_started_on", args.as_of.isoformat()))
                ),
            )
            continuity_rows = capture_continuity_rows(
                conn,
                slots=slots,
                table_prefix="provider_store.",
            )
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()

    earnings_rows = _build_earnings_drift_rows(
        earnings_history,
        universe=universe,
        as_of=args.as_of,
        universe_as_of=args.universe_as_of.isoformat(),
    )
    hard_failures = [row for row in coverage_rows if row["status"] == "FAIL"]
    warnings = [row for row in coverage_rows if row["status"] == "WARN"]
    capture_gaps = continuity_gaps(continuity_rows)
    failure_reasons = ["tier0_1_provider_coverage"] if hard_failures else []
    warning_reasons = ["scheduled_provider_capture_gap"] if capture_gaps else []
    acceptance = "FAIL" if hard_failures else "PASS_WITH_WARNINGS" if warnings or capture_gaps else "PASS"

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else paths.output_dir / "provider_diagnostics" / args.as_of.isoformat()
    )
    revision_path = output_dir / "estimate_revision_momentum.csv"
    uncertainty_path = output_dir / "estimate_uncertainty_diagnostics.csv"
    coverage_path = output_dir / "provider_coverage_readiness.csv"
    continuity_path = output_dir / "provider_capture_continuity.csv"
    earnings_path = output_dir / "earnings_date_drift.csv"
    fiscal_path = output_dir / "fiscal_period_alignment_audit.csv"
    summary_path = output_dir / "provider_diagnostics_summary.json"
    manifest_path = output_dir / "provider_diagnostics_manifest.json"
    outputs = [
        revision_path,
        uncertainty_path,
        coverage_path,
        continuity_path,
        earnings_path,
        fiscal_path,
        summary_path,
        manifest_path,
    ]
    fail_if_exists(outputs, force=args.force)
    write_csv(revision_path, REVISION_FIELDS, revision_rows)
    write_csv(uncertainty_path, UNCERTAINTY_FIELDS, uncertainty_rows)
    write_csv(coverage_path, COVERAGE_FIELDS, coverage_rows)
    write_csv(continuity_path, CONTINUITY_FIELDS, continuity_rows)
    write_csv(earnings_path, EARNINGS_DRIFT_FIELDS, earnings_rows)
    write_csv(fiscal_path, FISCAL_AUDIT_FIELDS, fiscal_rows)
    summary = {
        "schema_version": "provider_diagnostics_summary_v1",
        "acceptance": acceptance,
        "as_of_date": args.as_of.isoformat(),
        "universe_as_of": args.universe_as_of.isoformat(),
        "as_of_cutoff_utc": cutoff_text,
        "policy_version": policy["policy_version"],
        "provider_separation_required": True,
        "no_provider_averaging": True,
        "economic_signal_activation": "diagnostic_only",
        "revision_row_count": len(revision_rows),
        "locally_observed_revision_count": sum(
            row["prior_30d_source"] == "locally_observed_snapshot"
            or row["prior_90d_source"] == "locally_observed_snapshot"
            for row in revision_rows
        ),
        "provider_embedded_revision_count": sum(
            row["prior_30d_source"] == "provider_embedded_lookback"
            or row["prior_90d_source"] == "provider_embedded_lookback"
            for row in revision_rows
        ),
        "uncertainty_row_count": len(uncertainty_rows),
        "two_provider_uncertainty_count": sum(int(row["provider_count"]) == 2 for row in uncertainty_rows),
        "disagreement_count": sum(int(row["disagreement_flag"]) for row in uncertainty_rows),
        "coverage_warning_count": len(warnings),
        "coverage_failure_count": len(hard_failures),
        "capture_continuity_slot_count": len(continuity_rows),
        "capture_gap_count": len(capture_gaps),
        "capture_gaps": capture_gaps,
        "missed_capture_policy": "flag_no_backfill",
        "failure_reasons": failure_reasons,
        "warning_reasons": warning_reasons,
        "earnings_drift_row_count": len(earnings_rows),
        "earnings_delay_count": sum(row["drift_class"] == "delayed" for row in earnings_rows),
        "earnings_advance_count": sum(row["drift_class"] == "advanced" for row in earnings_rows),
        "fiscal_audit_status": next(
            str(row["status"]) for row in fiscal_rows if row["metric"] == "forecast_outcome_links"
        ),
        "selected_snapshot_count": len(snapshot_ids),
        "selected_snapshot_digest": _digest(sorted(snapshot_ids)),
        "universe_digest": _digest([dict(row) for row in universe_rows]),
        "revision_digest": _digest(revision_rows),
        "uncertainty_digest": _digest(uncertainty_rows),
        "coverage_digest": _digest(coverage_rows),
        "capture_continuity_digest": _digest(continuity_rows),
        "earnings_drift_digest": _digest(earnings_rows),
        "fiscal_audit_digest": _digest(fiscal_rows),
    }
    write_manifest(summary_path, summary)
    inputs = [
        config_path,
        Path(__file__).resolve(),
        Path(__file__).with_name("estimate_policy.py"),
        Path(__file__).with_name("monitor_common.py"),
    ]
    if external_provider_store:
        inputs.append(PACKAGE_ROOT / "provider_ingestion" / "store.py")
        inputs.append(PACKAGE_ROOT / "provider_ingestion" / "health.py")
    if earnings_history.is_file():
        inputs.append(earnings_history)
    manifest_payload = {
        "schema_version": "provider_diagnostics_manifest_v1",
        "acceptance": acceptance,
        "as_of_date": args.as_of.isoformat(),
        "universe_as_of": args.universe_as_of.isoformat(),
        "shadow_only": True,
        "no_provider_averaging": True,
        "les_effect_authorized": False,
        "levels_effect_authorized": False,
        "adaptive_capture_authorized": False,
        "failure_reasons": failure_reasons,
        "warning_reasons": warning_reasons,
        "capture_gap_count": len(capture_gaps),
        "source_snapshot_count": len(snapshot_ids),
        "source_snapshot_digest": summary["selected_snapshot_digest"],
        "provider_snapshot_store": (
            "independent_observation_store" if external_provider_store else "legacy_monitor_store"
        ),
        "dependency_lineage_verified": True,
        "inputs_sha256": {str(path): sha256_file(path) for path in inputs},
        "outputs_sha256": {path.name: sha256_file(path) for path in outputs if path != manifest_path},
    }

    dependency_paths = [path for path in outputs if path != manifest_path and path.is_file()]
    if snapshot_ids and external_provider_store:
        lock_path = provider_store_path.with_suffix(provider_store_path.suffix + ".writer.lock")
        with provider_writer_lock(lock_path, timeout_sec=timeout):
            provider_conn = connect_store(provider_store_path, timeout_sec=timeout)
            try:
                for path in dependency_paths:
                    artifact_sha = sha256_file(path)
                    record_provider_artifact_dependencies(
                        provider_conn,
                        artifact_path=str(path),
                        artifact_sha256=artifact_sha,
                        observation_ids=sorted(snapshot_ids),
                    )
                    errors = provider_artifact_dependency_errors(
                        provider_conn,
                        artifact_path=str(path),
                        artifact_sha256=artifact_sha,
                    )
                    if errors:
                        raise RuntimeError(f"Invalid provider-diagnostics observation lineage for {path}: {errors}")
            finally:
                provider_conn.close()
    elif snapshot_ids:
        lock_path = db_path.with_suffix(db_path.suffix + ".writer.lock")
        with writer_lock(lock_path, timeout_sec=timeout):
            conn = connect_monitor_db(db_path, timeout_sec=timeout)
            try:
                for path in dependency_paths:
                    artifact_sha = sha256_file(path)
                    supersede_artifact_dependencies(
                        conn,
                        artifact_path=str(path),
                        current_artifact_sha256=artifact_sha,
                    )
                    record_snapshot_dependencies(
                        conn,
                        artifact_path=str(path),
                        artifact_sha256=artifact_sha,
                        snapshot_ids=sorted(snapshot_ids),
                    )
                    errors = artifact_snapshot_dependency_errors(
                        conn,
                        artifact_path=str(path),
                        artifact_sha256=artifact_sha,
                    )
                    if errors:
                        raise RuntimeError(f"Invalid provider-diagnostics snapshot lineage for {path}: {errors}")
            finally:
                conn.close()
    elif external_provider_store:
        lock_path = provider_store_path.with_suffix(provider_store_path.suffix + ".writer.lock")
        with provider_writer_lock(lock_path, timeout_sec=timeout):
            provider_conn = connect_store(provider_store_path, timeout_sec=timeout)
            try:
                for path in dependency_paths:
                    supersede_provider_artifact_dependencies(
                        provider_conn,
                        artifact_path=str(path),
                    )
            finally:
                provider_conn.close()
    else:
        lock_path = db_path.with_suffix(db_path.suffix + ".writer.lock")
        with writer_lock(lock_path, timeout_sec=timeout):
            conn = connect_monitor_db(db_path, timeout_sec=timeout)
            try:
                for path in dependency_paths:
                    supersede_artifact_dependencies(
                        conn,
                        artifact_path=str(path),
                        current_artifact_sha256=sha256_file(path),
                        reason="empty_provider_diagnostics_publication",
                    )
            finally:
                conn.close()
    # Publish acceptance only after every data artifact has verified snapshot lineage.
    write_manifest(manifest_path, manifest_payload)

    print(f"PROVIDER DIAGNOSTICS: {acceptance}")
    print(
        f"revisions={len(revision_rows)}; uncertainty={len(uncertainty_rows)}; "
        f"coverage_warnings={len(warnings)}; coverage_failures={len(hard_failures)}; "
        f"earnings_delays={summary['earnings_delay_count']}"
    )
    return 1 if hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
