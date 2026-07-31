#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import load_yaml  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_csv,
    read_json,
    sha256,
    verify_artifact,
    write_manifest,
)

MODEL_FAMILY = "transportation"
EXPECTED_ACTIVE = 112
EXPECTED_BENCHMARKS = ("IYT", "XTN", "SPY")
EXPECTED_CANDIDATES = (
    "fleet_utilization",
    "operating_ratio",
    "passenger_load_factor",
)
REPORT_FIELDS = ("gate_id", "status", "actual", "expected", "detail")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the completed transportation implementation and its "
            "exactly-once historical calibration without future outcomes, "
            "recalibration, or production promotion."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def price_gate(
    rows: Sequence[Mapping[str, str]],
    *,
    asof: str,
    expected_active: int = EXPECTED_ACTIVE,
    expected_benchmarks: Sequence[str] = EXPECTED_BENCHMARKS,
) -> tuple[bool, dict[str, Any]]:
    benchmarks = [
        row for row in rows if str(row.get("is_benchmark") or "") == "1"
    ]
    active = [
        row for row in rows if str(row.get("is_benchmark") or "") != "1"
    ]
    symbols = {str(row.get("ticker") or "").upper() for row in benchmarks}
    failures = [
        str(row.get("ticker") or "")
        for row in rows
        if str(row.get("status") or "").lower()
        not in {"success", "already_current"}
        or str(row.get("last_bar_date") or "") != asof
    ]
    passed = (
        len(rows) == expected_active + len(expected_benchmarks)
        and len(active) == expected_active
        and symbols == set(expected_benchmarks)
        and not failures
    )
    return passed, {
        "rows": len(rows),
        "active": len(active),
        "benchmarks": sorted(symbols),
        "right_edge_failures": failures,
    }


def calibration_errors(
    manifest: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    operations = manifest.get("operations") or {}
    if (
        manifest.get("acceptance") != "PASS"
        or manifest.get("calibration_executed") is not True
        or int(operations.get("calibration_invocations") or 0) != 1
        or manifest.get("holdout_used_for_selection") is not False
    ):
        errors.append("calibration manifest is not a passing exactly-once run")
    for key in (
        "database_writes",
        "feature_rebuilds",
        "membership_rebuilds",
        "network_requests",
        "parser_invocations",
        "portfolio_writes",
        "production_config_writes",
    ):
        if int(operations.get(key) or 0) != 0:
            errors.append(f"calibration operation must remain zero={key}")
    decisions = manifest.get("candidate_decisions") or {}
    if set(decisions) != set(EXPECTED_CANDIDATES):
        errors.append("calibration candidate set changed")
    for metric in EXPECTED_CANDIDATES:
        item = decisions.get(metric) or {}
        if (
            float(item.get("validation_selected_weight") or 0.0) != 0.10
            or float(item.get("final_research_weight") or 0.0) != 0.0
            or item.get("decision") != "RETAIN_ZERO_OVERLAY"
        ):
            errors.append(f"frozen zero-overlay decision changed={metric}")
    weights = validation.get("final_research_weights") or {}
    if (
        validation.get("acceptance") != "PASS"
        or validation.get("holdout_used_for_selection") is not False
        or validation.get("confirmed_research_metric_count") != 0
        or set(weights) != set(EXPECTED_CANDIDATES)
        or any(float(value) != 0.0 for value in weights.values())
    ):
        errors.append("independent calibration validation changed")
    return errors


def _reference(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": sha256(path)}


def _artifact_errors(
    manifest: Mapping[str, Any],
    *,
    label: str,
) -> list[str]:
    errors: list[str] = []
    for name, reference in (manifest.get("artifacts") or {}).items():
        try:
            verify_artifact(reference, label=f"{label}:{name}")
        except (FileNotFoundError, ValueError) as error:
            errors.append(str(error))
    return errors


def _portfolio_source(path: Path) -> tuple[bool, dict[str, Any]]:
    config = load_yaml(path)
    sources = [
        source
        for source in (
            (config.get("score_contract") or {}).get("sectors") or []
        )
        if str(source.get("model_family") or "") == MODEL_FAMILY
    ]
    source = sources[0] if len(sources) == 1 else {}
    passed = len(sources) == 1 and (
        source.get("adapter") == "industrial_family"
        and source.get("enabled") is True
        and source.get("required") is False
        and source.get("require_oos_score_valid") is True
    )
    return passed, {
        "count": len(sources),
        "adapter": source.get("adapter"),
        "enabled": source.get("enabled"),
        "required": source.get("required"),
        "require_oos_score_valid": source.get("require_oos_score_valid"),
    }


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    root = PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY
    current = root / "current_panels" / asof
    dashboard = root / "dashboard" / asof
    frozen = root / "historical_features" / "v3_conflict_resolved"
    output = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else root / "implementation" / asof
    )
    paths = {
        "stage0_4": root / "production_readiness"
        / "transportation_stage0_4_production_readiness.json",
        "prices": root / "stage3" / "yahoo_adjusted_price_coverage.csv",
        "market_audit": root / "stage3" / "market_data_policy_audit.csv",
        "pit": current / "transportation_current_pit_build_manifest.json",
        "metrics": root / "stage4" / "transportation_metric_validation.json",
        "scoring": root / "stage6"
        / "transportation_scoring_features.manifest.json",
        "scoring_validation": root / "stage6"
        / "transportation_scoring_validation.json",
        "rank_manifest": dashboard
        / "transportation_final_rank_table_manifest.json",
        "rank_validation": dashboard
        / "transportation_final_rank_table_validation.json",
        "portfolio_validation": dashboard
        / "transportation_portfolio_adapter_validation.json",
        "current_panel": current
        / "transportation_current_complete_panel_manifest.json",
        "historical_panel": frozen / "transportation_v3_panel_manifest.json",
        "calibration": frozen
        / "transportation_walk_forward_calibration_manifest.json",
        "calibration_validation": frozen
        / "transportation_walk_forward_calibration_validation.json",
        "dp15": frozen
        / "transportation_zero_overlay_portfolio_shadow_gate.json",
        "monitor": root / "zero_overlay_monitoring"
        / "transportation_zero_overlay_monitor_status.json",
        "portfolio_config": PROJECT_ROOT / "portfolio_layer" / "config.yaml",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing completion inputs={missing}")

    data = {
        key: read_json(path)
        for key, path in paths.items()
        if path.suffix == ".json"
    }
    price_rows = read_csv(paths["prices"])
    market_rows = read_csv(paths["market_audit"])
    report: list[dict[str, object]] = []
    failures: list[str] = []

    def add(
        gate_id: str,
        passed: bool,
        actual: object,
        expected: object,
        detail: str,
    ) -> None:
        report.append(
            {
                "gate_id": gate_id,
                "status": "PASS" if passed else "FAIL",
                "actual": json.dumps(actual, sort_keys=True)
                if isinstance(actual, (dict, list))
                else str(actual),
                "expected": str(expected),
                "detail": detail,
            }
        )
        if not passed:
            failures.append(gate_id)

    stage = data["stage0_4"]
    add(
        "stage0_4_foundation",
        stage.get("acceptance") == "PASS"
        and stage.get("asof_date") == asof
        and int(stage.get("required_failure_count", -1)) == 0,
        {
            "asof": stage.get("asof_date"),
            "checks": stage.get("check_count"),
            "failures": stage.get("required_failure_count"),
        },
        f"PASS at {asof} with zero required failures",
        "read-only universe, identity, market, financial, and PIT checks",
    )
    passed, detail = price_gate(price_rows, asof=asof)
    add(
        "current_price_right_edge",
        passed,
        detail,
        f"112 active plus IYT/XTN/SPY through {asof}",
        "all active and benchmark adjusted-price series are current",
    )
    bad_market = [
        str(row.get("ticker") or "")
        for row in market_rows
        if str(row.get("status") or "") not in {"success", "review"}
        or str(row.get("latest_bar_date") or "") != asof
    ]
    add(
        "market_policy_audit",
        len(market_rows) == 115 and not bad_market,
        {
            "rows": len(market_rows),
            "reviews": sum(row.get("status") == "review" for row in market_rows),
            "failures": bad_market,
        },
        "115 exact-date rows and zero failures",
        "review may denote short history but not missing current data",
    )
    pit = data["pit"]
    add(
        "exact_date_pit_snapshot",
        pit.get("acceptance") == "PASS"
        and pit.get("completed_dates") == [asof]
        and int(pit.get("metric_count") or 0) == 39,
        {"dates": pit.get("completed_dates"), "metrics": pit.get("metric_count")},
        "one 39-metric exact-date snapshot",
        "current PIT build remains isolated from frozen history",
    )
    metrics = data["metrics"]
    required = metrics.get("required_coverage") or {}
    add(
        "metric_availability",
        metrics.get("acceptance") == "PASS"
        and metrics.get("asof_date") == asof
        and int(metrics.get("row_count") or 0) == 4368
        and int(required.get("review_required") or 0) == 0,
        {
            "rows": metrics.get("row_count"),
            "coverage_bps": required.get("coverage_bps"),
            "required_review": required.get("review_required"),
        },
        "4,368 explicit states and zero required review rows",
        "missing, not-applicable, reported, and derived stay distinct",
    )
    score_payloads = [
        data["scoring"],
        data["scoring_validation"],
        data["rank_manifest"],
        data["rank_validation"],
    ]
    add(
        "scoring_and_rank",
        all(
            item.get("acceptance") == "PASS"
            and int(item.get("row_count") or 0) == 112
            for item in score_payloads
        ),
        {
            "scoring_rows": data["scoring"].get("row_count"),
            "rank_rows": data["rank_validation"].get("row_count"),
            "rank_ready": data["rank_validation"].get("rank_ready_count"),
        },
        "112 validated scoring and rank rows",
        "deterministic current shadow publication",
    )
    panel = data["current_panel"]
    panel_errors = _artifact_errors(panel, label="current_panel")
    complete_ref = (panel.get("artifacts") or {}).get("complete_panel") or {}
    add(
        "current_complete_panel",
        panel.get("acceptance") == "PASS"
        and panel.get("asof_date") == asof
        and panel.get("panel_status") == "CURRENT_ONLY_HASH_SEALED"
        and int(panel.get("membership_row_count") or 0) == 112
        and int(panel.get("complete_metric_count") or 0) == 108
        and int(complete_ref.get("row_count") or 0) == 12096
        and not panel_errors,
        {
            "members": panel.get("membership_row_count"),
            "metrics": panel.get("complete_metric_count"),
            "rows": complete_ref.get("row_count"),
            "errors": panel_errors,
        },
        "112 x 108 = 12,096 hash-sealed rows",
        "current all-metric panel is complete",
    )
    history = data["historical_panel"]
    history_errors = _artifact_errors(history, label="historical_panel")
    add(
        "historical_panel_freeze",
        history.get("acceptance") == "PASS"
        and history.get("panel_status") == "HASH_FROZEN"
        and int(history.get("snapshot_date_count") or 0) == 92
        and int(history.get("historical_membership_row_count") or 0) == 9496
        and not history_errors,
        {
            "dates": history.get("snapshot_date_count"),
            "memberships": history.get("historical_membership_row_count"),
            "errors": history_errors,
        },
        "92 dates and 9,496 survivorship-corrected memberships",
        "one frozen historical materialization",
    )
    cal = data["calibration"]
    cal_validation = data["calibration_validation"]
    cal_errors = calibration_errors(cal, cal_validation)
    cal_errors.extend(_artifact_errors(cal, label="calibration"))
    cal_errors.extend(
        _artifact_errors(cal_validation, label="calibration_validation")
    )
    add(
        "walk_forward_calibration",
        not cal_errors,
        {
            "invocations": (cal.get("operations") or {}).get(
                "calibration_invocations"
            ),
            "eligible_rows": cal.get("eligible_observation_row_count"),
            "period_rows": cal.get("period_result_row_count"),
            "final_weights": cal_validation.get("final_research_weights"),
            "errors": cal_errors,
        },
        "one validated run; failed overlays retain zero",
        "holdout was confirmatory and did not select weights",
    )
    dp15 = data["dp15"]
    add(
        "zero_overlay_shadow_gate",
        dp15.get("acceptance") == "PASS"
        and dp15.get("zero_overlay_decision_sealed") is True
        and dp15.get("production_promotion_authorized") is False,
        {
            "weights": dp15.get("final_research_weights"),
            "promoted": dp15.get("production_promotion_authorized"),
        },
        "sealed zero overlays and no production promotion",
        "failed research cannot alter portfolio weights or OOS flags",
    )
    source_pass, source = _portfolio_source(paths["portfolio_config"])
    portfolio = data["portfolio_validation"]
    add(
        "portfolio_connection",
        source_pass
        and portfolio.get("acceptance") == "PASS"
        and portfolio.get("source_asof_date") == asof
        and int(portfolio.get("rows") or 0) == 112
        and int(portfolio.get("investable_rows") or 0) == 0
        and int(portfolio.get("oos_score_valid_rows") or 0) == 0,
        {**source, "validation": portfolio},
        "enabled optional source, fail-closed at 112 rows",
        "portfolio_layer integration is complete without allocation",
    )
    monitor = data["monitor"]
    add(
        "outcome_blind_monitor",
        monitor.get("acceptance") == "PASS"
        and monitor.get("asof_date") == asof
        and monitor.get("outcomes_accessed") is False
        and monitor.get("optimizer_executed") is False
        and monitor.get("calibration_executed") is False
        and monitor.get("production_promotion_authorized") is False,
        {
            "acceptance": monitor.get("acceptance"),
            "signals": monitor.get("valid_signal_date_count_by_metric"),
            "next_gate": monitor.get("next_gate"),
        },
        "PASS with no future outcomes or recalibration",
        "future signal capture is an operation, not unfinished implementation",
    )

    report_path = output / "transportation_implementation_gates.csv"
    manifest_path = output / "transportation_implementation_completion_manifest.json"
    write_csv_atomic(report_path, REPORT_FIELDS, report)
    acceptance = "PASS" if not failures else "FAIL"
    payload: dict[str, Any] = {
        "acceptance": acceptance,
        "gate": "TRANSPORTATION_IMPLEMENTATION_AND_CALIBRATION_COMPLETION",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "implementation_complete": acceptance == "PASS",
        "historical_calibration_complete": acceptance == "PASS",
        "calibration_scope": (
            "bounded_cohort_specialized_overlays_over_frozen_generic_control"
        ),
        "calibration_result": "RETAIN_ZERO_OVERLAY",
        "final_specialized_overlay_weights": (
            cal_validation.get("final_research_weights") or {}
        ),
        "daily_current_refresh_implemented": True,
        "portfolio_integration_complete": acceptance == "PASS",
        "shadow_model_operational": acceptance == "PASS",
        "remaining_implementation_steps": [],
        "production_model_promoted": False,
        "production_decision": "NOT_PROMOTED_HOLDOUT_GATES_FAILED",
        "future_monitoring_is_implementation_blocker": False,
        "gate_count": len(report),
        "failed_gate_count": len(failures),
        "failed_gates": failures,
        "report": _reference(report_path),
        "artifacts": {
            name: _reference(path) for name, path in paths.items()
        },
        "operations": {
            "network_requests": 0,
            "parser_invocations": 0,
            "historical_materializations": 0,
            "calibration_invocations_during_finalization": 0,
            "outcome_accesses": 0,
            "portfolio_writes": 0,
            "production_config_writes": 0,
        },
        "next_operational_action": (
            "capture the first eligible outcome-blind month-end signal "
            "after its completed market date becomes available"
        ),
        "errors": failures,
    }
    write_manifest(manifest_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
