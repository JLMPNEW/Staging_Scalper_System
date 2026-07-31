#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.oos_outcomes import (  # noqa: E402
    OUTCOME_PANEL_VERSION,
    finite_float,
    parse_date,
    rank_usable_period_count,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_csv,
    read_json,
    sha256,
    verify_artifact,
    write_manifest,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    MODEL_FAMILY,
)


READINESS_FIELDS = (
    "metric_id",
    "calibration_cohort",
    "split_name",
    "applicable_rows",
    "metric_value_rows",
    "return_available_rows",
    "eligible_rows",
    "eligible_ticker_count",
    "eligible_snapshot_count",
    "rank_usable_period_count",
    "minimum_required_rank_usable_periods",
    "readiness_gate_pass",
    "readiness_reason",
)
REQUIRED_FIELDS = {
    "asof_date",
    "ticker",
    "model_family",
    "calibration_cohort",
    "universe_role",
    "metric_id",
    "metric_value",
    "direction",
    "direction_multiplier",
    "direction_adjusted_metric_value",
    "availability_date",
    "split_name",
    "price_ticker",
    "price_source_id",
    "price_asof_date",
    "price_asof_value",
    "price_forward_date",
    "price_forward_value",
    "forward_trading_days",
    "security_forward_session_count",
    "security_forward_return",
    "outcome_method",
    "terminal_type",
    "membership_end_date",
    "current_security_start_date",
    "structural_break_date",
    "IYT_forward_return",
    "forward_excess_return_vs_IYT",
    "IYT_price_asof_date",
    "IYT_price_asof_value",
    "IYT_price_forward_date",
    "IYT_price_forward_value",
    "XTN_forward_return",
    "forward_excess_return_vs_XTN",
    "XTN_price_asof_date",
    "XTN_price_asof_value",
    "XTN_price_forward_date",
    "XTN_price_forward_value",
    "SPY_forward_return",
    "forward_excess_return_vs_SPY",
    "SPY_price_asof_date",
    "SPY_price_asof_value",
    "SPY_price_forward_date",
    "SPY_price_forward_value",
    "security_return_available_flag",
    "all_benchmark_returns_available_flag",
    "return_available_flag",
    "return_unavailable_reason",
    "metric_value_available_flag",
    "panel_row_eligible_flag",
    "panel_row_eligible_reason",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate transportation walk-forward outcome lineage, arithmetic, "
            "survivorship controls, and metric-specific calibration readiness."
        )
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path(
            "output/industrials/transportation/historical_features/"
            "v3_conflict_resolved"
        ),
    )
    return parser.parse_args()


def _iter_panel(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        rows = [
            {str(key): str(value or "") for key, value in row.items()}
            for row in reader
        ]
    return header, rows


def _close(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


def main() -> int:
    args = parse_args()
    artifact_dir = args.artifact_dir.expanduser().resolve()
    build_path = (
        artifact_dir
        / "transportation_walk_forward_outcome_panel_manifest.json"
    )
    contract_path = (
        artifact_dir
        / "transportation_walk_forward_calibration_contract.json"
    )
    coverage_path = (
        artifact_dir / "transportation_v3_historical_coverage.csv"
    )
    for path in (build_path, contract_path, coverage_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    build = read_json(build_path)
    contract = read_json(contract_path)
    if build.get("acceptance") != "PASS":
        raise ValueError("DP11 outcome build did not pass")
    panel_reference = (
        build.get("artifacts") or {}
    ).get("outcome_panel") or {}
    panel_path = verify_artifact(
        panel_reference,
        label="outcome panel",
    )
    calendar_reference = (
        contract.get("artifacts") or {}
    ).get("evaluation_calendar") or {}
    calendar_path = verify_artifact(
        calendar_reference,
        label="evaluation calendar",
    )
    calendar_rows = read_csv(calendar_path)
    split_map = {
        row["asof_date"]: row["split_name"] for row in calendar_rows
    }
    header, rows = _iter_panel(panel_path)
    errors: list[str] = []
    missing_fields = sorted(REQUIRED_FIELDS - set(header))
    if missing_fields:
        errors.append(f"missing panel fields={missing_fields}")
    if build.get("panel_version") != OUTCOME_PANEL_VERSION:
        errors.append("outcome panel version mismatch")
    if build.get("model_family") != MODEL_FAMILY:
        errors.append("outcome model family mismatch")
    if len(rows) != int(panel_reference.get("row_count") or -1):
        errors.append("outcome row count differs from manifest")
    candidates = tuple(
        str(value) for value in contract.get("candidate_metric_ids", [])
    )
    if set(row.get("metric_id", "") for row in rows) != set(candidates):
        errors.append("outcome candidate set differs from DP10")
    keys = [
        (row.get("asof_date"), row.get("ticker"), row.get("metric_id"))
        for row in rows
    ]
    if len(keys) != len(set(keys)):
        errors.append("duplicate outcome panel keys")

    coverage = {
        row["metric_id"]: row
        for row in read_csv(coverage_path)
        if row.get("metric_id") in candidates
    }
    row_counts = Counter(row["metric_id"] for row in rows)
    value_counts = Counter(
        row["metric_id"]
        for row in rows
        if finite_float(row.get("metric_value")) is not None
    )
    for metric in candidates:
        expected = coverage.get(metric)
        if expected is None:
            errors.append(f"{metric}: missing historical coverage row")
            continue
        if row_counts[metric] != int(
            expected.get("applicable_membership_rows") or -1
        ):
            errors.append(f"{metric}: applicable row reconciliation failed")
        if value_counts[metric] != int(
            expected.get("value_membership_rows") or -1
        ):
            errors.append(f"{metric}: metric-value row reconciliation failed")

    bad_splits: list[str] = []
    future_values: list[str] = []
    bad_directions: list[str] = []
    bad_windows: list[str] = []
    bad_arithmetic: list[str] = []
    bad_eligibility: list[str] = []
    continuity_violations: list[str] = []
    terminal_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    for row in rows:
        key = (
            f"{row.get('asof_date')}:{row.get('ticker')}:"
            f"{row.get('metric_id')}"
        )
        asof = parse_date(row.get("asof_date"), field=f"{key}.asof")
        if split_map.get(row.get("asof_date", "")) != row.get(
            "split_name"
        ):
            bad_splits.append(key)
        availability = str(row.get("availability_date") or "").strip()
        if availability and parse_date(
            availability,
            field=f"{key}.availability",
        ) > asof:
            future_values.append(key)
        value = finite_float(row.get("metric_value"))
        adjusted = finite_float(
            row.get("direction_adjusted_metric_value")
        )
        direction = row.get("direction")
        multiplier = finite_float(row.get("direction_multiplier"))
        expected_multiplier = -1.0 if direction == "negative" else 1.0
        if (
            direction not in {"positive", "negative"}
            or multiplier != expected_multiplier
            or (
                value is not None
                and not _close(adjusted, value * expected_multiplier)
            )
        ):
            bad_directions.append(key)
        security_return = finite_float(row.get("security_forward_return"))
        anchor = finite_float(row.get("price_asof_value"))
        forward = finite_float(row.get("price_forward_value"))
        if security_return is not None:
            anchor_date = parse_date(
                row.get("price_asof_date"),
                field=f"{key}.anchor_date",
            )
            forward_date = parse_date(
                row.get("price_forward_date"),
                field=f"{key}.forward_date",
            )
            if anchor_date > asof or forward_date <= anchor_date:
                bad_windows.append(key)
            if anchor is None or anchor <= 0 or forward is None or forward < 0:
                bad_windows.append(key)
            elif not _close(security_return, forward / anchor - 1.0):
                bad_arithmetic.append(key)
            method = row.get("outcome_method")
            sessions = row.get("security_forward_session_count")
            if method == "standard_forward_sessions" and sessions != str(
                build.get("forward_trading_days")
            ):
                bad_windows.append(key)
            if method == "terminal_membership_exit":
                terminal = row.get("terminal_type", "")
                terminal_counts[terminal] += 1
                membership_end = parse_date(
                    row.get("membership_end_date"),
                    field=f"{key}.membership_end",
                )
                if not asof < membership_end:
                    bad_windows.append(key)
                if terminal == "wipeout" and forward != 0.0:
                    bad_windows.append(key)
                if terminal not in {
                    "acquisition",
                    "distressed_nonzero",
                    "wipeout",
                }:
                    bad_windows.append(key)
        for benchmark in ("IYT", "XTN", "SPY"):
            benchmark_return = finite_float(
                row.get(f"{benchmark}_forward_return")
            )
            benchmark_anchor = finite_float(
                row.get(f"{benchmark}_price_asof_value")
            )
            benchmark_forward = finite_float(
                row.get(f"{benchmark}_price_forward_value")
            )
            if benchmark_return is not None:
                benchmark_anchor_date = parse_date(
                    row.get(f"{benchmark}_price_asof_date"),
                    field=f"{key}.{benchmark}.anchor_date",
                )
                benchmark_forward_date = parse_date(
                    row.get(f"{benchmark}_price_forward_date"),
                    field=f"{key}.{benchmark}.forward_date",
                )
                if (
                    benchmark_anchor_date > asof
                    or benchmark_forward_date <= benchmark_anchor_date
                    or benchmark_anchor is None
                    or benchmark_anchor <= 0
                    or benchmark_forward is None
                    or benchmark_forward < 0
                ):
                    bad_windows.append(f"{key}:{benchmark}")
                elif not _close(
                    benchmark_return,
                    benchmark_forward / benchmark_anchor - 1.0,
                ):
                    bad_arithmetic.append(f"{key}:{benchmark}")
            excess = finite_float(
                row.get(f"forward_excess_return_vs_{benchmark}")
            )
            expected_excess = (
                security_return - benchmark_return
                if security_return is not None
                and benchmark_return is not None
                else None
            )
            if not _close(excess, expected_excess):
                bad_arithmetic.append(f"{key}:{benchmark}")
        return_available = row.get("return_available_flag") == "1"
        metric_available = row.get("metric_value_available_flag") == "1"
        security_available = (
            row.get("security_return_available_flag") == "1"
        )
        all_benchmarks_available = (
            row.get("all_benchmark_returns_available_flag") == "1"
        )
        expected_all_benchmarks = all(
            finite_float(row.get(f"{benchmark}_forward_return"))
            is not None
            for benchmark in ("IYT", "XTN", "SPY")
        )
        if (
            security_available != (security_return is not None)
            or all_benchmarks_available != expected_all_benchmarks
            or return_available
            != (security_return is not None and expected_all_benchmarks)
            or metric_available != (value is not None)
        ):
            bad_eligibility.append(f"{key}:availability_flags")
        eligible = row.get("panel_row_eligible_flag") == "1"
        reasons = row.get("panel_row_eligible_reason", "")
        reason_counts[reasons] += 1
        expected_eligible = (
            return_available
            and metric_available
            and row.get("split_name") != "embargo"
            and (
                not availability
                or parse_date(availability) <= asof
            )
            and "candidate_not_mapped_to_cohort" not in reasons
        )
        if eligible != expected_eligible:
            bad_eligibility.append(key)
        if eligible and reasons != "eligible":
            bad_eligibility.append(key)
        if not eligible and not reasons:
            bad_eligibility.append(key)
        if security_return is not None:
            anchor_date = parse_date(
                row.get("price_asof_date"),
                field=f"{key}.continuity_anchor",
            )
            forward_date = parse_date(
                row.get("price_forward_date"),
                field=f"{key}.continuity_forward",
            )
            security_start = str(
                row.get("current_security_start_date") or ""
            )
            structural_break = str(
                row.get("structural_break_date") or ""
            )
            if security_start and anchor_date < parse_date(security_start):
                continuity_violations.append(key)
            if structural_break:
                boundary = parse_date(structural_break)
                if anchor_date < boundary <= forward_date:
                    continuity_violations.append(key)
            if row.get("outcome_method") == "terminal_membership_exit":
                membership_end = parse_date(
                    row.get("membership_end_date"),
                    field=f"{key}.terminal_membership_end",
                )
                primary_horizon_end = parse_date(
                    row.get("IYT_price_forward_date"),
                    field=f"{key}.primary_horizon_end",
                )
                if (
                    not asof < membership_end <= primary_horizon_end
                    or forward_date > membership_end
                    or (
                        row.get("terminal_type") != "wipeout"
                        and (membership_end - forward_date).days > 10
                    )
                ):
                    continuity_violations.append(key)
            if (
                row.get("outcome_method")
                == "terminal_membership_exit"
                and row.get("history_treatment")
                in {
                    "separate_regime_no_return_stitch",
                    "hard_boundary_no_spac_price_stitch",
                }
                and structural_break
                and anchor_date
                < parse_date(structural_break)
                <= forward_date
            ):
                continuity_violations.append(key)
    for label, values in (
        ("split mismatches", bad_splits),
        ("future metric values", future_values),
        ("direction mismatches", bad_directions),
        ("invalid return windows", bad_windows),
        ("return arithmetic mismatches", bad_arithmetic),
        ("eligibility mismatches", bad_eligibility),
        ("continuity violations", continuity_violations),
    ):
        if values:
            errors.append(f"{label}={values[:20]}")

    overlay = {
        str(cohort): str(metric or "")
        for cohort, metric in (
            contract.get("cohort_specific_overlay") or {}
        ).items()
    }
    minimum_holdout = int(
        (contract.get("acceptance_gates") or {}).get(
            "minimum_holdout_periods_per_candidate_cohort",
            12,
        )
    )
    minimum_tickers = int(
        (contract.get("acceptance_gates") or {}).get(
            "minimum_candidate_value_tickers",
            3,
        )
    )
    metric_cohorts = {
        metric: cohort
        for cohort, metric in overlay.items()
        if metric
    }
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if metric_cohorts.get(row["metric_id"]) == row["calibration_cohort"]:
            grouped[(row["metric_id"], row["split_name"])].append(row)
    readiness_rows: list[dict[str, object]] = []
    metric_readiness: dict[str, bool] = {}
    for metric in candidates:
        metric_pass = True
        cohort = metric_cohorts.get(metric, "")
        if overlay.get(cohort) != metric:
            metric_pass = False
            errors.append(f"{metric}: cohort overlay mapping changed")
        for split in ("train", "validation", "holdout", "embargo"):
            members = grouped.get((metric, split), [])
            eligible_members = [
                row
                for row in members
                if row.get("panel_row_eligible_flag") == "1"
            ]
            usable_periods = rank_usable_period_count(
                eligible_members,
                minimum_tickers=minimum_tickers,
            )
            required = minimum_holdout if split == "holdout" else (
                1 if split in {"train", "validation"} else 0
            )
            passed = usable_periods >= required
            if split in {"train", "validation", "holdout"} and not passed:
                metric_pass = False
            readiness_rows.append(
                {
                    "metric_id": metric,
                    "calibration_cohort": cohort,
                    "split_name": split,
                    "applicable_rows": len(members),
                    "metric_value_rows": sum(
                        finite_float(row.get("metric_value")) is not None
                        for row in members
                    ),
                    "return_available_rows": sum(
                        row.get("return_available_flag") == "1"
                        for row in members
                    ),
                    "eligible_rows": len(eligible_members),
                    "eligible_ticker_count": len(
                        {row["ticker"] for row in eligible_members}
                    ),
                    "eligible_snapshot_count": len(
                        {row["asof_date"] for row in eligible_members}
                    ),
                    "rank_usable_period_count": usable_periods,
                    "minimum_required_rank_usable_periods": required,
                    "readiness_gate_pass": int(passed),
                    "readiness_reason": (
                        "pass"
                        if passed
                        else "insufficient_rank_usable_periods"
                    ),
                }
            )
        metric_readiness[metric] = metric_pass
        if not metric_pass:
            errors.append(f"{metric}: calibration readiness failed")

    readiness_path = (
        artifact_dir
        / "transportation_walk_forward_outcome_readiness.csv"
    )
    validation_path = (
        artifact_dir
        / "transportation_walk_forward_outcome_validation.json"
    )
    write_csv_atomic(
        readiness_path,
        READINESS_FIELDS,
        readiness_rows,
    )
    acceptance = "PASS" if not errors else "FAIL"
    payload: dict[str, Any] = {
        "acceptance": acceptance,
        "gate": "DP12_VALIDATE_WALK_FORWARD_OUTCOME_READINESS",
        "panel_version": OUTCOME_PANEL_VERSION,
        "model_family": MODEL_FAMILY,
        "candidate_metric_ids": list(candidates),
        "candidate_readiness": metric_readiness,
        "ready_candidate_metric_ids": [
            metric for metric in candidates if metric_readiness.get(metric)
        ],
        "ready_candidate_metric_count": sum(metric_readiness.values()),
        "outcome_panel_row_count": len(rows),
        "metric_value_row_count": sum(value_counts.values()),
        "return_available_row_count": sum(
            row.get("return_available_flag") == "1" for row in rows
        ),
        "eligible_row_count": sum(
            row.get("panel_row_eligible_flag") == "1" for row in rows
        ),
        "terminal_outcome_counts": dict(sorted(terminal_counts.items())),
        "eligibility_reason_counts": dict(sorted(reason_counts.items())),
        "minimum_holdout_rank_usable_periods": minimum_holdout,
        "minimum_tickers_per_rank_period": minimum_tickers,
        "artifacts": {
            "outcome_panel": {
                "path": str(panel_path),
                "sha256": sha256(panel_path),
                "row_count": len(rows),
            },
            "readiness_report": {
                "path": str(readiness_path),
                "sha256": sha256(readiness_path),
                "row_count": len(readiness_rows),
            },
        },
        "inputs": {
            "outcome_build_manifest": {
                "path": str(build_path),
                "sha256": sha256(build_path),
            },
            "calibration_contract": {
                "path": str(contract_path),
                "sha256": sha256(contract_path),
            },
            "historical_coverage": {
                "path": str(coverage_path),
                "sha256": sha256(coverage_path),
            },
            "evaluation_calendar": {
                "path": str(calendar_path),
                "sha256": sha256(calendar_path),
                "row_count": len(calendar_rows),
            },
        },
        "operations": {
            "database_writes": 0,
            "parser_invocations": 0,
            "network_requests": 0,
            "feature_rebuilds": 0,
            "membership_rebuilds": 0,
            "portfolio_writes": 0,
            "calibration_invocations": 0,
        },
        "calibration_executed": False,
        "single_calibration_authorized": acceptance == "PASS",
        "production_promotion_authorized": False,
        "errors": errors,
        "next_gate": (
            "RUN_SINGLE_BOUNDED_WALK_FORWARD_CALIBRATION"
            if acceptance == "PASS"
            else "REVIEW_OUTCOME_READINESS_FAILURES"
        ),
    }
    write_manifest(validation_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
