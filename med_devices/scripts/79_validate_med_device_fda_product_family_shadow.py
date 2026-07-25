#!/usr/bin/env python3
"""Validate the ABT FDA product-family shadow signal on matured OOS observations.

Besides measuring predictive quality (Spearman IC + tercile monotonicity), this
validator enforces the shadow contract: the config kill-switch must keep the
shadow at zero production weight, scoring model_version must match the pinned
config version, shadow rows must satisfy their construction invariants, and the
live FDA columns must not have been overwritten by shadow-adjusted values.
Contract violations are data-integrity failures and exit non-zero; genuinely
immature-but-healthy data still exits zero.
"""
from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import sys
from bisect import bisect_right
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.market_policy import (  # noqa: E402
    calibration_market_sources,
    is_adjusted_price_row,
)
from med_devices.core.point_in_time import parse_iso_date  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "fda_product_family_review.shadow_score"
EXPECTED_PRODUCTION_USAGE = "shadow_only_until_oos_validation"
SAFETY_IDENTITY_TOLERANCE = 0.01
OUTPUT_FIELDS = [
    "generated_asof",
    "ticker",
    "horizon_days",
    "shadow_first_asof",
    "shadow_score_rows",
    "oos_valid_score_rows",
    "unparseable_oos_row_count",
    "mature_oos_observations",
    "minimum_required_observations",
    "raw_safety_unique_values",
    "shadow_safety_unique_values",
    "minimum_required_unique_values",
    "raw_safety_spearman_ic",
    "shadow_safety_spearman_ic",
    "shadow_minus_raw_ic",
    "low_shadow_bucket_count",
    "high_shadow_bucket_count",
    "low_shadow_bucket_median_return",
    "high_shadow_bucket_median_return",
    "high_minus_low_median_return",
    "monotonicity_status",
    "validation_status",
    "promotion_eligible_flag",
    "validation_reason",
]
CHECK_FIELDS = [
    "generated_asof",
    "ticker",
    "check_id",
    "severity",
    "status",
    "observed",
    "expected",
    "details",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate product-family FDA shadow safety against raw FDA safety."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--asof",
        default="",
        help=(
            "Validation cutoff, YYYY-MM-DD. Defaults to the ticker's latest "
            "scored asof_date in med_device_daily_scores (never wall-clock)."
        ),
    )
    parser.add_argument("--ticker", default="ABT")
    parser.add_argument("--horizons", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def as_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fractional_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[index][1]:
            end += 1
        rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[ordered[position][0]] = rank
        index = end + 1
    return ranks


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 5 or len(xs) != len(ys):
        return None
    x_mean = mean(xs)
    y_mean = mean(ys)
    covariance = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    x_scale = math.sqrt(sum((x - x_mean) ** 2 for x in xs))
    y_scale = math.sqrt(sum((y - y_mean) ** 2 for y in ys))
    if x_scale <= 1e-12 or y_scale <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, covariance / (x_scale * y_scale)))


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return correlation(fractional_ranks(xs), fractional_ranks(ys))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def dated_sibling(path: Path, generated_asof: date) -> Path:
    return path.with_name(f"{path.stem}_{generated_asof.isoformat()}{path.suffix}")


def load_prices(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    sources: list[str],
    cutoff: date,
) -> tuple[list[date], list[float]]:
    placeholders = ",".join("?" for _ in sources)
    rows = conn.execute(
        f"""
        SELECT ticker, bar_date, source_id, close, adj_close, is_adjusted,
               price_adjustment
        FROM fact_price_ohlcv
        WHERE UPPER(ticker) = ?
          AND bar_date <= ?
          AND LOWER(source_id) IN ({placeholders})
        ORDER BY bar_date, source_id
        """,
        (ticker, cutoff.isoformat(), *sources),
    ).fetchall()
    source_priority = {source: index for index, source in enumerate(sources)}
    selected: dict[date, tuple[int, float]] = {}
    for raw in rows:
        row = dict(raw)
        if not is_adjusted_price_row(row):
            continue
        bar_date = parse_iso_date(row.get("bar_date"))
        price = as_float(row.get("adj_close"))
        if price is None:
            price = as_float(row.get("close"))
        if bar_date is None or price is None or price <= 0:
            continue
        source = str(row.get("source_id") or "").strip().lower()
        priority = source_priority.get(source, len(source_priority))
        previous = selected.get(bar_date)
        if previous is None or priority < previous[0]:
            selected[bar_date] = (priority, price)
    dates = sorted(selected)
    return dates, [selected[item][1] for item in dates]


def latest_scored_asof(conn: sqlite3.Connection, *, ticker: str) -> date | None:
    row = conn.execute(
        """
        SELECT MAX(s.asof_date)
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        WHERE UPPER(c.ticker) = ?
        """,
        (ticker,),
    ).fetchone()
    return parse_iso_date(row[0]) if row and row[0] else None


def load_score_rows(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    cutoff: date,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT s.asof_date, s.fda_product_family_shadow_oos_valid_flag,
                   s.fda_event_risk_score,
                   s.fda_safety_score,
                   s.fda_event_risk_product_family_adjusted_score,
                   s.fda_safety_product_family_adjusted_score,
                   s.fda_product_family_shadow_available_flag,
                   s.fda_product_family_adjustment_applied_flag,
                   s.fda_product_family_shadow_status,
                   s.scoring_model_version
            FROM med_device_daily_scores s
            JOIN dim_company c ON c.company_id = s.company_id
            WHERE UPPER(c.ticker) = ?
              AND s.asof_date <= ?
              AND s.fda_product_family_shadow_available_flag = 1
            ORDER BY s.asof_date
            """,
            (ticker, cutoff.isoformat()),
        ).fetchall()
    ]


def row_is_unparseable(row: dict[str, Any]) -> bool:
    """A shadow-available row missing any required scoring field is corrupt.

    Every field below is unconditionally written by scripts 13/78 for
    shadow-available rows, so an unparseable value is data corruption, not
    immaturity (VD-3): it must be counted and fail loud instead of being
    silently reclassified as an insufficient-observation outcome.
    """
    return (
        parse_iso_date(row.get("asof_date")) is None
        or as_float(row.get("fda_event_risk_score")) is None
        or as_float(row.get("fda_safety_score")) is None
        or as_float(row.get("fda_event_risk_product_family_adjusted_score")) is None
        or as_float(row.get("fda_safety_product_family_adjusted_score")) is None
    )


def add_check(
    checks: list[dict[str, Any]],
    *,
    generated_asof: date,
    ticker: str,
    check_id: str,
    severity: str,
    passed: bool,
    observed: object,
    expected: object,
    details: str,
) -> None:
    checks.append(
        {
            "generated_asof": generated_asof.isoformat(),
            "ticker": ticker,
            "check_id": check_id,
            "severity": severity,
            "status": "PASS" if passed else "FAIL",
            "observed": observed,
            "expected": expected,
            "details": details,
        }
    )


def contract_checks(
    *,
    config: dict[str, Any],
    generated_asof: date,
    ticker: str,
    score_rows: list[dict[str, Any]],
    price_dates: list[date],
) -> tuple[list[dict[str, Any]], int]:
    """Shadow-contract and data-integrity checks (validator-75 style rows).

    Returns the check rows plus the unparseable shadow-row count. Any CRITICAL
    FAIL is a data-integrity failure and drives a non-zero exit (VD-1/VD-2).
    """
    checks: list[dict[str, Any]] = []
    target_tickers = {
        str(item or "").strip().upper()
        for item in (
            cfg_get(config, "fda_product_family_review.target_tickers", []) or []
        )
        if str(item or "").strip()
    }
    governed = ticker in target_tickers

    def check(
        check_id: str,
        *,
        passed: bool,
        observed: object,
        expected: object,
        details: str,
        severity: str = "CRITICAL",
    ) -> None:
        add_check(
            checks,
            generated_asof=generated_asof,
            ticker=ticker,
            check_id=check_id,
            severity=severity,
            passed=passed,
            observed=observed,
            expected=expected,
            details=details,
        )

    # (VD-1c) Config kill-switch: while the feature is shadow-only the
    # production weight must be pinned to exactly zero.
    production_usage = str(
        cfg_get(config, "fda_product_family_review.production_usage", "") or ""
    ).strip()
    check(
        "production_usage_shadow_only",
        passed=production_usage == EXPECTED_PRODUCTION_USAGE,
        observed=production_usage,
        expected=EXPECTED_PRODUCTION_USAGE,
        details=(
            "fda_product_family_review.production_usage must remain shadow-only; "
            "any other value requires an explicit governance promotion decision."
        ),
    )
    production_weight = as_float(
        cfg_get(config, f"{CONFIG_KEY}.production_weight", 0.0)
    )
    check(
        "shadow_production_weight_zero",
        passed=production_weight == 0.0,
        observed=production_weight,
        expected=0.0,
        details=(
            f"{CONFIG_KEY}.production_weight is the shadow kill-switch and must "
            "be 0.0 while production_usage is shadow-only."
        ),
    )

    # (VD-2a) A governed target ticker must have shadow rows once 78 has run;
    # an empty result for any requested ticker means there is nothing to
    # validate, which is a failure rather than a silent exit-0.
    check(
        "shadow_rows_present",
        passed=bool(score_rows),
        observed=len(score_rows),
        expected=">= 1 shadow-available score row",
        details=(
            f"Ticker {ticker} is {'a governed' if governed else 'not a governed'} "
            "fda_product_family_review target; zero shadow-available rows means "
            "script 78 is not writing the shadow feature."
        ),
    )

    # (VD-2c) Without price history no observation can ever mature; that is a
    # broken input, not immaturity.
    check(
        "price_history_present",
        passed=bool(price_dates),
        observed=len(price_dates),
        expected=">= 1 adjusted price bar",
        details="Calibration-source adjusted price history is empty for the ticker.",
    )

    # (VD-1a) Model-version pin: every shadow row must carry a scoring model
    # version and the latest rows must match the pinned config version, so a
    # composite change without a version bump cannot pass silently.
    pinned_version = str(cfg_get(config, "scoring.model_version", "") or "").strip()
    missing_version_rows = sum(
        1
        for row in score_rows
        if not str(row.get("scoring_model_version") or "").strip()
    )
    check(
        "scoring_model_version_populated",
        passed=missing_version_rows == 0,
        observed=missing_version_rows,
        expected=0,
        details="Shadow-available score rows with an empty scoring_model_version.",
    )
    latest_asof = max(
        (str(row.get("asof_date") or "") for row in score_rows), default=""
    )
    latest_versions = sorted(
        {
            str(row.get("scoring_model_version") or "").strip()
            for row in score_rows
            if str(row.get("asof_date") or "") == latest_asof
        }
    )
    check(
        "latest_scoring_model_version_pinned",
        passed=not score_rows
        or (bool(pinned_version) and latest_versions == [pinned_version]),
        observed=",".join(latest_versions) or "",
        expected=pinned_version,
        details=(
            "scoring_model_version on the latest shadow rows must equal the "
            "pinned scoring.model_version config value."
        ),
    )

    # (VD-3) Unparseable shadow rows are corruption, not immaturity.
    unparseable_count = sum(1 for row in score_rows if row_is_unparseable(row))
    check(
        "shadow_row_fields_parseable",
        passed=unparseable_count == 0,
        observed=unparseable_count,
        expected=0,
        details=(
            "Shadow-available rows whose asof_date, live FDA scores, or "
            "product-family adjusted scores are null/unparseable."
        ),
    )

    parseable = [row for row in score_rows if not row_is_unparseable(row)]

    # (VD-4) Construction invariant from fda_product_family_review core:
    # safety_score = round(100 - event_risk_score, 2) for available rows.
    identity_violations = sum(
        1
        for row in parseable
        if abs(
            (as_float(row.get("fda_safety_product_family_adjusted_score")) or 0.0)
            - (
                100.0
                - (
                    as_float(
                        row.get("fda_event_risk_product_family_adjusted_score")
                    )
                    or 0.0
                )
            )
        )
        > SAFETY_IDENTITY_TOLERANCE
    )
    check(
        "shadow_safety_identity",
        passed=identity_violations == 0,
        observed=identity_violations,
        expected=0,
        details=(
            "Rows violating shadow safety == 100 - shadow event risk "
            f"(tolerance {SAFETY_IDENTITY_TOLERANCE})."
        ),
    )

    # Construction invariant: available_flag=1 rows always carry
    # adjustment_applied_flag=1 (core sets both together); the flag documents
    # shadow computation only and must never gate production columns.
    flag_violations = sum(
        1
        for row in score_rows
        if int(as_float(row.get("fda_product_family_adjustment_applied_flag")) or 0)
        != 1
    )
    check(
        "shadow_flags_consistent",
        passed=flag_violations == 0,
        observed=flag_violations,
        expected=0,
        details=(
            "Shadow-available rows must carry "
            "fda_product_family_adjustment_applied_flag=1 by construction."
        ),
    )

    # (VD-1b) Leak tripwire: the live FDA columns come from a different model
    # than the product-family shadow, so systematic equality across every
    # shadow row means script 13 overwrote live columns with shadow values.
    # Equality at the score clamp boundaries (0/100) is uninformative — both
    # models legitimately saturate there (observed live for ABT) — so a leak
    # is only declared when every pair is equal AND at least one equal pair
    # sits strictly inside the (0, 100) score range.
    def at_clamp_boundary(value: float) -> bool:
        return abs(value) <= 1e-9 or abs(value - 100.0) <= 1e-9

    def systematically_equal(live_key: str, shadow_key: str) -> tuple[bool, int]:
        pairs = [
            (as_float(row.get(live_key)), as_float(row.get(shadow_key)))
            for row in parseable
        ]
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        if not pairs:
            return False, 0
        all_equal = all(abs(a - b) <= 1e-9 for a, b in pairs)
        informative_equal = any(
            abs(a - b) <= 1e-9 and not at_clamp_boundary(a) for a, b in pairs
        )
        return all_equal and informative_equal, len(pairs)

    safety_equal, safety_pairs = systematically_equal(
        "fda_safety_score", "fda_safety_product_family_adjusted_score"
    )
    check(
        "live_safety_not_overwritten_by_shadow",
        passed=not safety_equal,
        observed=(
            f"all_{safety_pairs}_rows_equal_off_boundary"
            if safety_equal
            else "no_informative_systematic_equality"
        ),
        expected="live fda_safety_score independent of shadow-adjusted safety",
        details=(
            "Every shadow row has live fda_safety_score identical to the "
            "shadow-adjusted safety score off the clamp boundary; the shadow "
            "leaked into production columns."
            if safety_equal
            else (
                "Live and shadow safety values are not systematically "
                "identical off the 0/100 clamp boundaries."
            )
        ),
    )
    risk_equal, risk_pairs = systematically_equal(
        "fda_event_risk_score", "fda_event_risk_product_family_adjusted_score"
    )
    check(
        "live_event_risk_not_overwritten_by_shadow",
        passed=not risk_equal,
        observed=(
            f"all_{risk_pairs}_rows_equal_off_boundary"
            if risk_equal
            else "no_informative_systematic_equality"
        ),
        expected="live fda_event_risk_score independent of shadow-adjusted risk",
        details=(
            "Every shadow row has live fda_event_risk_score identical to the "
            "shadow-adjusted event risk off the clamp boundary; the shadow "
            "leaked into production columns."
            if risk_equal
            else (
                "Live and shadow event-risk values are not systematically "
                "identical off the 0/100 clamp boundaries."
            )
        ),
    )

    return checks, unparseable_count


def horizon_rows(
    *,
    score_rows: list[dict[str, Any]],
    price_dates: list[date],
    prices: list[float],
    horizon: int,
) -> list[tuple[float, float, float]]:
    observations: list[tuple[float, float, float]] = []
    for row in score_rows:
        if (
            int(
                as_float(
                    row.get("fda_product_family_shadow_oos_valid_flag")
                )
                or 0
            )
            != 1
        ):
            continue
        # Unparseable rows are counted and failed by contract_checks (VD-3);
        # this loop only measures maturity on rows that parse cleanly.
        if row_is_unparseable(row):
            continue
        score_date = parse_iso_date(row.get("asof_date"))
        raw_event_risk = as_float(row.get("fda_event_risk_score"))
        shadow_safety = as_float(
            row.get("fda_safety_product_family_adjusted_score")
        )
        if score_date is None or raw_event_risk is None or shadow_safety is None:
            continue
        base_index = bisect_right(price_dates, score_date) - 1
        forward_index = base_index + horizon
        if (
            base_index < 0
            or forward_index >= len(price_dates)
            or prices[base_index] <= 0
        ):
            continue
        forward_return = prices[forward_index] / prices[base_index] - 1.0
        observations.append((100.0 - raw_event_risk, shadow_safety, forward_return))
    return observations


def validation_row(
    *,
    generated_asof: date,
    ticker: str,
    horizon: int,
    score_rows: list[dict[str, Any]],
    observations: list[tuple[float, float, float]],
    min_observations: int,
    min_unique_values: int,
    unparseable_count: int,
) -> dict[str, Any]:
    raw_scores = [row[0] for row in observations]
    shadow_scores = [row[1] for row in observations]
    returns = [row[2] for row in observations]
    raw_unique = len(set(raw_scores))
    shadow_unique = len(set(shadow_scores))
    raw_ic = spearman(raw_scores, returns)
    shadow_ic = spearman(shadow_scores, returns)
    eligible = len(observations) >= min_observations
    unique_enough = shadow_unique >= min_unique_values

    ordered = sorted(observations, key=lambda item: item[1])
    bucket_size = max(1, len(ordered) // 3) if ordered else 0
    low = ordered[:bucket_size] if bucket_size else []
    high = ordered[-bucket_size:] if bucket_size else []
    low_median = median([item[2] for item in low]) if low else None
    high_median = median([item[2] for item in high]) if high else None
    spread = (
        high_median - low_median
        if high_median is not None and low_median is not None
        else None
    )

    if not eligible:
        status = "insufficient_mature_oos_observations"
        reason = (
            f"Only {len(observations)} matured OOS observations; "
            f"{min_observations} required."
        )
    elif not unique_enough:
        status = "insufficient_shadow_score_variation"
        reason = (
            f"Only {shadow_unique} unique shadow values; "
            f"{min_unique_values} required."
        )
    elif shadow_ic is None or raw_ic is None or spread is None:
        status = "insufficient_metric_support"
        reason = "IC or monotonicity metrics could not be estimated."
    elif shadow_ic <= raw_ic or shadow_ic <= 0 or spread <= 0:
        status = "validation_failed"
        reason = (
            "Shadow safety did not exceed raw-safety IC with positive "
            "high-minus-low return monotonicity."
        )
    else:
        status = "validation_passed"
        reason = "Shadow safety passed the OOS IC and monotonicity guardrails."

    # Tri-state (VD-5): "not_evaluated" when no spread could be computed, so
    # an unevaluated test can never be misread as an evaluated failure.
    if spread is None:
        monotonicity_status = "not_evaluated"
    elif spread > 0:
        monotonicity_status = "pass"
    else:
        monotonicity_status = "fail"

    first_asof = str(score_rows[0].get("asof_date") or "") if score_rows else ""
    return {
        "generated_asof": generated_asof.isoformat(),
        "ticker": ticker,
        "horizon_days": horizon,
        "shadow_first_asof": first_asof,
        "shadow_score_rows": len(score_rows),
        "oos_valid_score_rows": sum(
            int(
                as_float(
                    row.get("fda_product_family_shadow_oos_valid_flag")
                )
                or 0
            )
            == 1
            for row in score_rows
        ),
        "unparseable_oos_row_count": unparseable_count,
        "mature_oos_observations": len(observations),
        "minimum_required_observations": min_observations,
        "raw_safety_unique_values": raw_unique,
        "shadow_safety_unique_values": shadow_unique,
        "minimum_required_unique_values": min_unique_values,
        "raw_safety_spearman_ic": "" if raw_ic is None else round(raw_ic, 6),
        "shadow_safety_spearman_ic": (
            "" if shadow_ic is None else round(shadow_ic, 6)
        ),
        "shadow_minus_raw_ic": (
            ""
            if shadow_ic is None or raw_ic is None
            else round(shadow_ic - raw_ic, 6)
        ),
        "low_shadow_bucket_count": len(low),
        "high_shadow_bucket_count": len(high),
        "low_shadow_bucket_median_return": (
            "" if low_median is None else round(low_median, 8)
        ),
        "high_shadow_bucket_median_return": (
            "" if high_median is None else round(high_median, 8)
        ),
        "high_minus_low_median_return": (
            "" if spread is None else round(spread, 8)
        ),
        "monotonicity_status": monotonicity_status,
        "validation_status": status,
        "promotion_eligible_flag": int(status == "validation_passed"),
        "validation_reason": reason,
    }


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    output_path = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                f"{CONFIG_KEY}.validation_output_csv",
                "../output/med_devices_reports/calibration/"
                "med_device_abt_fda_product_family_oos_validation.csv",
            ),
            base_dir=base_dir,
        )
    )
    checks_path = output_path.with_name(
        f"{output_path.stem}_contract_checks{output_path.suffix}"
    )
    configured_horizons = cfg_get(
        config,
        f"{CONFIG_KEY}.validation_horizons_days",
        [60, 120],
    )
    raw_horizons = (
        args.horizons.split(",")
        if args.horizons
        else configured_horizons
        if isinstance(configured_horizons, list)
        else str(configured_horizons).split(",")
    )
    horizons = sorted(
        {int(value) for value in raw_horizons if str(value).strip()}
    )
    if not horizons or any(value <= 0 for value in horizons):
        raise ValueError("At least one positive validation horizon is required")
    min_observations = int(
        cfg_get(config, f"{CONFIG_KEY}.promotion_min_oos_observations", 20)
    )
    min_unique_values = int(
        cfg_get(config, f"{CONFIG_KEY}.promotion_min_unique_score_values", 3)
    )
    ticker = str(args.ticker or "ABT").strip().upper()
    with connect(
        db_path,
        timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0)),
    ) as conn:
        init_db(conn)
        if args.asof:
            cutoff = parse_iso_date(args.asof)
            if cutoff is None:
                raise ValueError("--asof must be an ISO date")
        else:
            # VD-6: derive the default cutoff from the latest scored asof_date
            # instead of wall-clock UTC, so ad-hoc runs cannot stamp
            # generated_asof ahead of the pipeline's trading date.
            cutoff = latest_scored_asof(conn, ticker=ticker)
            if cutoff is None:
                raise ValueError(
                    f"No scored rows exist for {ticker} in med_device_daily_scores; "
                    "pass --asof explicitly."
                )
        score_rows = load_score_rows(conn, ticker=ticker, cutoff=cutoff)
        price_dates, prices = load_prices(
            conn,
            ticker=ticker,
            sources=calibration_market_sources(config),
            cutoff=cutoff,
        )
    checks, unparseable_count = contract_checks(
        config=config,
        generated_asof=cutoff,
        ticker=ticker,
        score_rows=score_rows,
        price_dates=price_dates,
    )
    output = [
        validation_row(
            generated_asof=cutoff,
            ticker=ticker,
            horizon=horizon,
            score_rows=score_rows,
            observations=horizon_rows(
                score_rows=score_rows,
                price_dates=price_dates,
                prices=prices,
                horizon=horizon,
            ),
            min_observations=min_observations,
            min_unique_values=min_unique_values,
            unparseable_count=unparseable_count,
        )
        for horizon in horizons
    ]
    # Dated artifacts first, then the fixed names, so an interrupted run can
    # never leave the latest artifact newer than its dated twin (VD-7).
    write_csv(dated_sibling(output_path, cutoff), output, OUTPUT_FIELDS)
    write_csv(output_path, output, OUTPUT_FIELDS)
    write_csv(dated_sibling(checks_path, cutoff), checks, CHECK_FIELDS)
    write_csv(checks_path, checks, CHECK_FIELDS)
    critical_failures = [
        row
        for row in checks
        if row["severity"] == "CRITICAL" and row["status"] == "FAIL"
    ]
    for row in critical_failures:
        print(
            "fda_product_family_shadow_contract FAIL "
            f"check={row['check_id']} observed={row['observed']} "
            f"expected={row['expected']} details={row['details']}"
        )
    promotion_ready = all(
        int(row["promotion_eligible_flag"]) == 1 for row in output
    )
    print(
        "fda_product_family_oos_validation "
        f"ticker={ticker} asof={cutoff.isoformat()} horizons={horizons} "
        f"promotion_ready={int(promotion_ready)} "
        f"contract_checks={len(checks)} "
        f"contract_failures={len(critical_failures)} "
        f"unparseable_rows={unparseable_count} "
        f"output={output_path} checks_output={checks_path}"
    )
    # VD-2: data-integrity/contract failures exit non-zero; genuinely
    # immature-but-healthy shadow data still exits zero.
    return 1 if critical_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
