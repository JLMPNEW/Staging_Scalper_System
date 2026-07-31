from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from industrials.core.config import load_yaml
from industrials.core.reports import write_csv_atomic, write_text_atomic
from industrials.transportation.selected_feature_history import sha256
from industrials.transportation.walk_forward_calibration import overlay_score


MONITORING_VERSION = "transportation_zero_overlay_monitoring_v1"
FORBIDDEN_FIELD_TOKENS = (
    "return",
    "outcome",
    "forward_date",
    "exit_price",
    "benchmark_price",
)
SOURCE_FIELDS = (
    "asof_date",
    "ticker",
    "metric_id",
    "calibration_cohort",
    "baseline_score",
    "specialized_percentile",
)
SIGNAL_FIELDS = (
    "policy_version",
    "policy_sha256",
    "asof_date",
    "ticker",
    "metric_id",
    "calibration_cohort",
    "baseline_score",
    "specialized_percentile",
    "portfolio_overlay_weight",
    "research_challenger_weight",
    "baseline_rank",
    "challenger_score",
    "challenger_rank",
    "cross_section_count",
    "source_snapshot_sha256",
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _parse_date(value: object, *, field: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field}: expected ISO date") from error


def _finite(value: object, *, field: str) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field}: expected finite number") from error
    if not math.isfinite(parsed):
        raise ValueError(f"{field}: expected finite number")
    return parsed


def _rank(
    rows: Sequence[Mapping[str, object]],
    *,
    field: str,
) -> dict[str, int]:
    ordered = sorted(
        rows,
        key=lambda row: (
            -float(str(row[field])),
            str(row["ticker"]),
        ),
    )
    return {
        str(row["ticker"]): index
        for index, row in enumerate(ordered, start=1)
    }


def load_monitoring_policy(path: Path) -> dict[str, Any]:
    policy = load_yaml(path)
    if policy.get("model_family") != "transportation":
        raise ValueError("monitoring policy model_family changed")
    if policy.get("policy_version") != MONITORING_VERSION:
        raise ValueError("monitoring policy version changed")
    candidates = policy.get("candidate_cohorts") or {}
    directions = policy.get("candidate_directions") or {}
    portfolio = policy.get("portfolio_overlay_weights") or {}
    challengers = policy.get("research_challenger_weights") or {}
    if set(candidates) != set(portfolio) or set(candidates) != set(challengers):
        raise ValueError("monitoring candidate mappings differ")
    if set(candidates) != set(directions) or any(
        int(value) not in {-1, 1} for value in directions.values()
    ):
        raise ValueError("monitoring candidate directions differ")
    if not candidates:
        raise ValueError("monitoring candidate set is empty")
    if any(float(value) != 0.0 for value in portfolio.values()):
        raise ValueError("monitoring portfolio weights must remain zero")
    if any(not 0 < float(value) <= 0.10 for value in challengers.values()):
        raise ValueError("research challenger weights must be within (0, 0.10]")
    if policy.get("outcome_access_during_capture") != "prohibited":
        raise ValueError("outcome access must remain prohibited")
    if policy.get("optimizer_during_monitoring") != "disabled":
        raise ValueError("monitoring optimizer must remain disabled")
    if policy.get("production_promotion_during_monitoring") != "prohibited":
        raise ValueError("monitoring promotion must remain prohibited")
    if int(policy.get("minimum_new_monthly_signals_per_candidate") or 0) < 12:
        raise ValueError("monitoring requires at least 12 new monthly signals")
    if int(policy.get("minimum_cross_section_per_candidate") or 0) < 3:
        raise ValueError("monitoring cross-section minimum cannot be below 3")
    origin_hash = str(policy.get("origin_dp15_sha256") or "")
    if len(origin_hash) != 64 or any(
        value not in "0123456789abcdef" for value in origin_hash
    ):
        raise ValueError("origin_dp15_sha256 must be a lowercase SHA-256")
    _parse_date(policy["origin_asof_date"], field="origin_asof_date")
    first = _parse_date(policy["first_signal_date"], field="first_signal_date")
    review = _parse_date(
        policy["earliest_outcome_review_date"],
        field="earliest_outcome_review_date",
    )
    if review <= first:
        raise ValueError("earliest outcome review must follow first signal")
    return policy


def signal_paths(output_root: Path, asof: str) -> tuple[Path, Path]:
    directory = output_root / "signals" / asof
    return (
        directory / "transportation_candidate_shadow_signals.csv",
        directory / "transportation_candidate_shadow_signals_manifest.json",
    )


def validate_signal_snapshot(
    *,
    signal_path: Path,
    manifest_path: Path,
    policy_path: Path,
) -> dict[str, Any]:
    issues: list[str] = []
    if not signal_path.is_file() or not manifest_path.is_file():
        return {
            "acceptance": "FAIL",
            "issues": ["signal snapshot or manifest is missing"],
        }
    policy = load_monitoring_policy(policy_path)
    rows = _read_csv(signal_path)
    manifest = _read_json(manifest_path)
    with signal_path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = tuple(csv.DictReader(handle).fieldnames or ())
    if header != SIGNAL_FIELDS:
        issues.append("signal snapshot schema mismatch")
    forbidden = [
        field
        for field in header
        if any(token in field.lower() for token in FORBIDDEN_FIELD_TOKENS)
    ]
    if forbidden:
        issues.append(f"signal snapshot contains outcome fields={forbidden}")
    if manifest.get("signal_snapshot_sha256") != sha256(signal_path):
        issues.append("signal snapshot hash mismatch")
    if manifest.get("policy_sha256") != sha256(policy_path):
        issues.append("monitoring policy hash mismatch")
    if manifest.get("outcomes_accessed") is not False:
        issues.append("signal manifest does not prove outcome exclusion")
    if manifest.get("optimizer_executed") is not False:
        issues.append("signal manifest claims optimizer execution")
    if manifest.get("production_promotion_performed") is not False:
        issues.append("signal manifest claims production promotion")
    if int(manifest.get("row_count") or -1) != len(rows):
        issues.append("signal row count mismatch")
    candidates = policy["candidate_cohorts"]
    counts: dict[str, int] = defaultdict(int)
    keys: set[tuple[str, str]] = set()
    asof_values: set[str] = set()
    for row in rows:
        key = (row["metric_id"], row["ticker"])
        if key in keys:
            issues.append(f"duplicate signal key={key}")
        keys.add(key)
        asof_values.add(row["asof_date"])
        metric = row["metric_id"]
        if candidates.get(metric) != row["calibration_cohort"]:
            issues.append(f"candidate cohort mismatch={key}")
        counts[metric] += 1
        if float(row["portfolio_overlay_weight"]) != 0.0:
            issues.append(f"nonzero portfolio weight={key}")
        if float(row["research_challenger_weight"]) != float(
            policy["research_challenger_weights"].get(metric, -1)
        ):
            issues.append(f"challenger weight mismatch={key}")
        for field in (
            "baseline_score",
            "specialized_percentile",
            "challenger_score",
        ):
            value = _finite(row[field], field=f"{key}:{field}")
            if not 0 <= value <= 100:
                issues.append(f"score outside 0..100={key}:{field}")
    if len(asof_values) != 1:
        issues.append("signal snapshot must contain exactly one asof date")
    minimum = int(policy["minimum_cross_section_per_candidate"])
    for metric in candidates:
        if counts.get(metric, 0) < minimum:
            issues.append(f"candidate cross-section below minimum={metric}")
    return {
        "acceptance": "PASS" if not issues else "FAIL",
        "asof_date": next(iter(asof_values), ""),
        "row_count": len(rows),
        "candidate_row_counts": dict(sorted(counts.items())),
        "outcomes_accessed": False,
        "issues": issues,
    }


def capture_signal_snapshot(
    *,
    asof: str,
    source_snapshot: Path,
    policy_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    policy = load_monitoring_policy(policy_path)
    asof_date = _parse_date(asof, field="asof")
    first_signal = _parse_date(
        policy["first_signal_date"],
        field="first_signal_date",
    )
    if asof_date < first_signal:
        raise ValueError(
            f"signal date {asof} precedes frozen start {first_signal}"
        )
    if not source_snapshot.is_file():
        raise FileNotFoundError(source_snapshot)
    with source_snapshot.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        header = tuple(csv.DictReader(handle).fieldnames or ())
    if header != SOURCE_FIELDS:
        raise ValueError(
            f"source snapshot schema mismatch actual={header} "
            f"expected={SOURCE_FIELDS}"
        )
    forbidden = [
        field
        for field in header
        if any(token in field.lower() for token in FORBIDDEN_FIELD_TOKENS)
    ]
    if forbidden:
        raise ValueError(f"source snapshot contains outcome fields={forbidden}")
    source_rows = _read_csv(source_snapshot)
    candidates = policy["candidate_cohorts"]
    by_metric: dict[str, list[dict[str, object]]] = defaultdict(list)
    keys: set[tuple[str, str]] = set()
    for row in source_rows:
        if row["asof_date"] != asof:
            raise ValueError("source snapshot asof does not match request")
        key = (row["metric_id"], row["ticker"])
        if key in keys:
            raise ValueError(f"duplicate source signal key={key}")
        keys.add(key)
        metric = row["metric_id"]
        if candidates.get(metric) != row["calibration_cohort"]:
            raise ValueError(f"source candidate cohort mismatch={key}")
        baseline = _finite(row["baseline_score"], field=f"{key}:baseline")
        specialized = _finite(
            row["specialized_percentile"],
            field=f"{key}:specialized",
        )
        if not 0 <= baseline <= 100 or not 0 <= specialized <= 100:
            raise ValueError(f"source scores outside 0..100={key}")
        challenger_weight = float(
            policy["research_challenger_weights"][metric]
        )
        by_metric[metric].append(
            {
                **row,
                "baseline_score": baseline,
                "specialized_percentile": specialized,
                "challenger_score": overlay_score(
                    baseline,
                    specialized,
                    challenger_weight,
                ),
            }
        )
    if set(by_metric) != set(candidates):
        raise ValueError("source snapshot does not contain every candidate")
    minimum = int(policy["minimum_cross_section_per_candidate"])
    policy_hash = sha256(policy_path)
    source_hash = sha256(source_snapshot)
    output_rows: list[dict[str, object]] = []
    for metric, rows in sorted(by_metric.items()):
        if len(rows) < minimum:
            raise ValueError(
                f"{metric}: cross-section {len(rows)} below {minimum}"
            )
        baseline_ranks = _rank(rows, field="baseline_score")
        challenger_ranks = _rank(rows, field="challenger_score")
        for row in sorted(rows, key=lambda item: str(item["ticker"])):
            ticker = str(row["ticker"])
            output_rows.append(
                {
                    "policy_version": MONITORING_VERSION,
                    "policy_sha256": policy_hash,
                    "asof_date": asof,
                    "ticker": ticker,
                    "metric_id": metric,
                    "calibration_cohort": str(
                        row["calibration_cohort"]
                    ),
                    "baseline_score": f"{float(str(row['baseline_score'])):.12g}",
                    "specialized_percentile": (
                        f"{float(str(row['specialized_percentile'])):.12g}"
                    ),
                    "portfolio_overlay_weight": "0",
                    "research_challenger_weight": (
                        f"{float(policy['research_challenger_weights'][metric]):.12g}"
                    ),
                    "baseline_rank": baseline_ranks[ticker],
                    "challenger_score": (
                        f"{float(str(row['challenger_score'])):.12g}"
                    ),
                    "challenger_rank": challenger_ranks[ticker],
                    "cross_section_count": len(rows),
                    "source_snapshot_sha256": source_hash,
                }
            )
    signal_path, manifest_path = signal_paths(output_root, asof)
    if signal_path.exists() or manifest_path.exists():
        existing = validate_signal_snapshot(
            signal_path=signal_path,
            manifest_path=manifest_path,
            policy_path=policy_path,
        )
        existing_manifest = (
            _read_json(manifest_path) if manifest_path.is_file() else {}
        )
        if (
            existing["acceptance"] == "PASS"
            and existing_manifest.get("source_snapshot_sha256") == source_hash
        ):
            return existing
        raise FileExistsError(
            f"refusing to overwrite non-identical signal snapshot={signal_path}"
        )
    write_csv_atomic(signal_path, SIGNAL_FIELDS, output_rows)
    manifest = {
        "acceptance": "PASS",
        "artifact_family": "transportation_candidate_shadow_signals",
        "policy_version": MONITORING_VERSION,
        "policy_sha256": policy_hash,
        "asof_date": asof,
        "row_count": len(output_rows),
        "candidate_row_counts": {
            metric: len(rows) for metric, rows in sorted(by_metric.items())
        },
        "signal_snapshot_path": str(signal_path.resolve()),
        "signal_snapshot_sha256": sha256(signal_path),
        "source_snapshot_path": str(source_snapshot.resolve()),
        "source_snapshot_sha256": source_hash,
        "outcomes_accessed": False,
        "outcome_fields_written": False,
        "optimizer_executed": False,
        "production_promotion_performed": False,
        "portfolio_writes": 0,
        "database_writes": 0,
    }
    _write_json(manifest_path, manifest)
    return validate_signal_snapshot(
        signal_path=signal_path,
        manifest_path=manifest_path,
        policy_path=policy_path,
    )


def audit_monitoring_state(
    *,
    asof: str,
    policy_path: Path,
    dp15_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    policy = load_monitoring_policy(policy_path)
    dp15 = _read_json(dp15_path)
    errors: list[str] = []
    if (
        dp15.get("acceptance") != "PASS"
        or dp15.get("gate")
        != "DP15_FINALIZE_ZERO_OVERLAY_PORTFOLIO_SHADOW"
        or dp15.get("zero_overlay_decision_sealed") is not True
        or dp15.get("production_promotion_authorized") is not False
    ):
        errors.append("DP15 zero-overlay origin gate is invalid")
    if sha256(dp15_path) != policy["origin_dp15_sha256"]:
        errors.append("DP15 hash differs from frozen monitoring origin")
    final_weights = dp15.get("final_research_weights") or {}
    if (
        set(final_weights) != set(policy["candidate_cohorts"])
        or any(float(value) != 0.0 for value in final_weights.values())
    ):
        errors.append("DP15 final weights do not match zero-overlay policy")
    if dp15.get("asof_date") != policy["origin_asof_date"]:
        errors.append("DP15 origin date differs from monitoring policy")
    calibration_reference = (
        (dp15.get("artifacts") or {}).get("calibration_validation") or {}
    )
    calibration_path = Path(
        str(calibration_reference.get("path") or "")
    ).resolve()
    if (
        not calibration_path.is_file()
        or sha256(calibration_path)
        != str(calibration_reference.get("sha256") or "")
    ):
        errors.append("DP15 calibration-validation lineage is invalid")
    else:
        calibration = _read_json(calibration_path)
        selected_weights = {
            str(metric): float(value)
            for metric, value in (
                calibration.get("validation_selected_weights") or {}
            ).items()
        }
        expected_challengers = {
            str(metric): float(value)
            for metric, value in policy[
                "research_challenger_weights"
            ].items()
        }
        if (
            calibration.get("acceptance") != "PASS"
            or selected_weights != expected_challengers
        ):
            errors.append(
                "research challengers differ from DP14-selected weights"
            )

    signal_root = output_root / "signals"
    valid_dates_by_metric: dict[str, list[str]] = defaultdict(list)
    invalid_snapshots: list[str] = []
    seen_dates: set[str] = set()
    if signal_root.is_dir():
        for directory in sorted(
            path for path in signal_root.iterdir() if path.is_dir()
        ):
            signal_path, manifest_path = signal_paths(
                output_root,
                directory.name,
            )
            result = validate_signal_snapshot(
                signal_path=signal_path,
                manifest_path=manifest_path,
                policy_path=policy_path,
            )
            if result["acceptance"] != "PASS":
                invalid_snapshots.append(directory.name)
                continue
            signal_date = str(result["asof_date"])
            if signal_date in seen_dates:
                invalid_snapshots.append(signal_date)
                continue
            seen_dates.add(signal_date)
            if _parse_date(signal_date, field="signal.asof") < _parse_date(
                policy["first_signal_date"],
                field="first_signal_date",
            ):
                invalid_snapshots.append(signal_date)
                continue
            for metric, count in result["candidate_row_counts"].items():
                if int(count) >= int(
                    policy["minimum_cross_section_per_candidate"]
                ):
                    valid_dates_by_metric[metric].append(signal_date)
    if invalid_snapshots:
        errors.append(f"invalid signal snapshots={invalid_snapshots}")
    minimum_signals = int(
        policy["minimum_new_monthly_signals_per_candidate"]
    )
    counts = {
        metric: len(set(valid_dates_by_metric.get(metric, [])))
        for metric in sorted(policy["candidate_cohorts"])
    }
    history_gate = all(count >= minimum_signals for count in counts.values())
    date_gate = _parse_date(asof, field="asof") >= _parse_date(
        policy["earliest_outcome_review_date"],
        field="earliest_outcome_review_date",
    )
    ready_for_separate_outcome_audit = (
        not errors and history_gate and date_gate
    )
    acceptance = "PASS" if not errors else "FAIL"
    return {
        "acceptance": acceptance,
        "gate": "DP16_ZERO_OVERLAY_SHADOW_MONITOR",
        "policy_version": MONITORING_VERSION,
        "asof_date": str(asof)[:10],
        "origin_dp15_path": str(dp15_path.resolve()),
        "origin_dp15_sha256": sha256(dp15_path),
        "policy_path": str(policy_path.resolve()),
        "policy_sha256": sha256(policy_path),
        "portfolio_overlay_weights": {
            metric: float(value)
            for metric, value in policy["portfolio_overlay_weights"].items()
        },
        "research_challenger_weights": {
            metric: float(value)
            for metric, value in policy[
                "research_challenger_weights"
            ].items()
        },
        "valid_signal_dates": sorted(seen_dates),
        "valid_signal_date_count_by_metric": counts,
        "minimum_new_monthly_signals_per_candidate": minimum_signals,
        "history_gate_pass": history_gate,
        "earliest_outcome_review_date": policy[
            "earliest_outcome_review_date"
        ],
        "calendar_gate_pass": date_gate,
        "ready_for_separate_outcome_audit": (
            ready_for_separate_outcome_audit
        ),
        "outcomes_accessed": False,
        "optimizer_executed": False,
        "calibration_executed": False,
        "recalibration_authorized": False,
        "production_promotion_authorized": False,
        "operations": {
            "database_writes": 0,
            "parser_invocations": 0,
            "network_requests": 0,
            "feature_rebuilds": 0,
            "historical_rebuilds": 0,
            "calibration_invocations": 0,
            "portfolio_writes": 0,
            "production_config_writes": 0,
        },
        "errors": errors,
        "next_gate": (
            (
                "REQUEST_SEPARATE_OUTCOME_AUDIT_PROTOCOL"
                if ready_for_separate_outcome_audit
                else "CONTINUE_ZERO_OVERLAY_SHADOW_MONITORING"
            )
            if acceptance == "PASS"
            else "REVIEW_ZERO_OVERLAY_MONITOR_FAILURES"
        ),
    }
