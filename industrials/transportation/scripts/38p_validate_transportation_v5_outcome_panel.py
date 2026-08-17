#!/usr/bin/env python3
"""Independently reconcile every available v5 outcome-panel return."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.oos_price_lineage import audit_panel_return_lineage  # noqa: E402
from industrials.core.oos_research import finite_float, parse_date  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.surface_freight_score_engine import (  # noqa: E402
    load_cohort_score_policy,
)


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_INPUT_DIR = ROOT / "investable_v5" / "outcome_panel" / "2026-08-15"
DEFAULT_OUTPUT_DIR = ROOT / "investable_v5" / "outcome_validation" / "2026-08-15"
DEFAULT_SURFACE_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_surface_freight_score_policy_v3.yaml"
DEFAULT_TANKER_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_tanker_score_policy_v1.yaml"
COVERAGE_FIELDS = (
    "cohort_id",
    "horizon_sessions",
    "eligible_row_count",
    "available_row_count",
    "outcome_coverage",
    "cross_section_ready_date_count",
    "minimum_ready_date_count",
    "coverage_gate",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--surface-policy", type=Path, default=DEFAULT_SURFACE_POLICY)
    parser.add_argument("--tanker-policy", type=Path, default=DEFAULT_TANKER_POLICY)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    manifest_path = input_dir / "transportation_v5_outcome_panel_manifest.json"
    panel_path = input_dir / "transportation_v5_outcome_panel.csv"
    price_path = input_dir / "transportation_v5_normalized_price_slice.csv"
    source_path = input_dir / "transportation_v5_outcome_source_index.csv"
    identity_path = input_dir / "transportation_v5_outcome_identity.json"
    for path in (manifest_path, panel_path, price_path, source_path, identity_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    manifest = read_json(manifest_path)
    rows = read_csv(panel_path)
    price_rows = read_csv(price_path)
    source_rows = read_csv(source_path)
    policies = (
        load_cohort_score_policy(args.surface_policy.expanduser().resolve()),
        load_cohort_score_policy(args.tanker_policy.expanduser().resolve()),
    )
    policy_by_cohort = {str(policy["cohort_id"]): policy for policy in policies}
    issues: list[str] = []
    if manifest.get("acceptance") != "PASS":
        issues.append("source outcome manifest is not PASS")
    if manifest.get("return_basis") != "next_session_open_execution_excess":
        issues.append("return basis is not D+1 adjusted-open execution excess")
    if manifest.get("survivorship_corrected") is not True:
        issues.append("outcome panel is not survivorship corrected")
    if manifest.get("cohort_isolated") is not True:
        issues.append("outcome panel is not cohort isolated")
    if manifest.get("historical_results_can_authorize_production") is not False:
        issues.append("historical outcome panel improperly authorizes production")
    hash_contract = {
        panel_path: "panel_sha256",
        price_path: "normalized_price_slice_sha256",
        source_path: "source_index_sha256",
        identity_path: "identity_sha256",
    }
    for path, field in hash_contract.items():
        if manifest.get(field) != file_sha256(path):
            issues.append(f"artifact hash mismatch={path.name}")
    if int(manifest.get("panel_row_count") or -1) != len(rows):
        issues.append("panel row count mismatch")
    if int(manifest.get("normalized_price_slice_row_count") or -1) != len(price_rows):
        issues.append("normalized price-slice row count mismatch")
    raw_price_path = Path(str(manifest.get("pinned_raw_price_slice_path") or ""))
    if (
        not raw_price_path.is_file()
        or manifest.get("pinned_raw_price_slice_sha256") != file_sha256(raw_price_path)
    ):
        issues.append("pinned raw price-slice hash mismatch")
    validation_path = Path(str(manifest.get("score_history_validation_path") or ""))
    protocol_path = Path(str(manifest.get("research_protocol_path") or ""))
    if (
        not validation_path.is_file()
        or manifest.get("score_history_validation_sha256") != file_sha256(validation_path)
    ):
        issues.append("score-history validation lineage mismatch")
    if (
        not protocol_path.is_file()
        or manifest.get("research_protocol_sha256") != file_sha256(protocol_path)
    ):
        issues.append("research-protocol lineage mismatch")
    else:
        protocol = read_json(protocol_path)
        if (protocol.get("evidence_governance") or {}).get(
            "historical_results_can_authorize_production"
        ) is not False:
            issues.append("research protocol does not fail closed")

    source_by_date = {str(row["asof_date"]): row for row in source_rows}
    for source in source_rows:
        for path_field, hash_field in (
            ("score_path", "score_sha256"),
            ("calibration_sidecar_path", "calibration_sidecar_sha256"),
            ("manifest_path", "manifest_sha256"),
        ):
            path = Path(str(source[path_field]))
            if not path.is_file() or file_sha256(path) != str(source[hash_field]):
                issues.append(f"{source['asof_date']}: source hash mismatch={path_field}")
    keys: set[tuple[str, str, str]] = set()
    eligible = Counter()
    available = Counter()
    ready_by_date: dict[tuple[str, str, str], int] = defaultdict(int)
    contribution: dict[tuple[str, str], int] = Counter()
    unavailable_reasons: Counter[str] = Counter()
    for row in rows:
        key = (
            str(row.get("asof_date") or ""),
            str(row.get("ticker") or ""),
            str(row.get("horizon_sessions") or ""),
        )
        if key in keys:
            issues.append(f"duplicate panel key={key}")
        keys.add(key)
        asof = parse_date(key[0], field="panel asof")
        entry = parse_date(row["entry_date"]) if row.get("entry_date") else None
        exit_date = parse_date(row["exit_date"]) if row.get("exit_date") else None
        if entry is not None and entry <= asof:
            issues.append(f"{key}: entry is not after signal")
        if entry is not None and exit_date is not None and exit_date <= entry:
            issues.append(f"{key}: exit is not after entry")
        if str(row.get("current_portfolio_eligibility_authorized") or "") != "0":
            issues.append(f"{key}: diagnostic row authorizes current portfolio")
        cohort = str(row.get("calibration_cohort") or "")
        horizon = str(row.get("horizon_sessions") or "")
        if cohort not in policy_by_cohort:
            issues.append(f"{key}: unknown cohort={cohort}")
            continue
        is_eligible = str(row.get("calibration_eligible_flag") or "") == "1"
        is_available = str(row.get("outcome_available_flag") or "") == "1"
        bucket = (cohort, horizon)
        eligible[bucket] += int(is_eligible)
        available[bucket] += int(is_eligible and is_available)
        ready_by_date[(cohort, horizon, key[0])] += int(is_eligible and is_available)
        contribution[(str(row.get("ticker") or ""), cohort)] += int(
            is_eligible and is_available
        )
        if is_available and (
            finite_float(row.get("security_forward_return")) is None
            or finite_float(row.get("benchmark_forward_return")) is None
            or finite_float(row.get("forward_excess_return")) is None
        ):
            issues.append(f"{key}: available outcome has missing return")
        if not is_available:
            unavailable_reasons[str(row.get("outcome_unavailable_reason") or "")] += 1
        source = source_by_date.get(key[0])
        if source is None:
            issues.append(f"{key}: missing source index row")
        elif (
            row.get("source_score_sha256") != source["score_sha256"]
            or row.get("source_calibration_sidecar_sha256")
            != source["calibration_sidecar_sha256"]
            or row.get("source_snapshot_manifest_sha256") != source["manifest_sha256"]
        ):
            issues.append(f"{key}: source lineage hash mismatch")

    return_audit = audit_panel_return_lineage(rows, price_rows)
    issues.extend(
        f"return reconstruction: {item}"
        for item in list(return_audit.get("issues") or [])
    )
    coverage_rows: list[dict[str, Any]] = []
    cohort_results: dict[str, Any] = {}
    for cohort, policy in policy_by_cohort.items():
        minimum_cross_section = int(policy["minimum_active_cohort_size"])
        minimum_dates = int(policy["historical_prebuild_gate"]["minimum_source_ready_dates"])
        cohort_tickers = sorted(
            {
                str(row["ticker"])
                for row in rows
                if str(row["calibration_cohort"]) == cohort
            }
        )
        historical_only = {
            str(ticker).upper()
            for ticker in (policy.get("historical_calibration_only") or {})
        }
        zero_current = sorted(
            ticker
            for ticker in cohort_tickers
            if contribution[(ticker, cohort)] == 0 and ticker not in historical_only
        )
        noncontributing_historical_only = sorted(
            ticker
            for ticker in cohort_tickers
            if contribution[(ticker, cohort)] == 0 and ticker in historical_only
        )
        horizon_results: dict[str, Any] = {}
        for horizon in ("21", "63"):
            bucket = (cohort, horizon)
            fraction = available[bucket] / eligible[bucket] if eligible[bucket] else 0.0
            ready_dates = sum(
                count >= minimum_cross_section
                for (row_cohort, row_horizon, _), count in ready_by_date.items()
                if row_cohort == cohort and row_horizon == horizon
            )
            gate = fraction >= 0.80 and ready_dates >= minimum_dates
            if not gate:
                issues.append(
                    f"{cohort}/{horizon}: outcome coverage or ready-date gate failed"
                )
            coverage_rows.append(
                {
                    "cohort_id": cohort,
                    "horizon_sessions": horizon,
                    "eligible_row_count": eligible[bucket],
                    "available_row_count": available[bucket],
                    "outcome_coverage": round(fraction, 6),
                    "cross_section_ready_date_count": ready_dates,
                    "minimum_ready_date_count": minimum_dates,
                    "coverage_gate": "PASS" if gate else "FAIL",
                }
            )
            horizon_results[horizon] = {
                "eligible_row_count": eligible[bucket],
                "available_row_count": available[bucket],
                "outcome_coverage": fraction,
                "cross_section_ready_date_count": ready_dates,
                "minimum_ready_date_count": minimum_dates,
                "gate": "PASS" if gate else "FAIL",
            }
        if zero_current:
            issues.append(
                f"{cohort}: current tickers with zero outcome contribution="
                f"{zero_current}"
            )
        cohort_results[cohort] = {
            "horizons": horizon_results,
            "zero_current_outcome_contributors": zero_current,
            "noncontributing_historical_only_tickers": noncontributing_historical_only,
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = output_dir / "transportation_v5_outcome_coverage.csv"
    output_path = output_dir / "transportation_v5_outcome_panel_validation.json"
    write_csv_atomic(coverage_path, COVERAGE_FIELDS, coverage_rows)
    result = {
        "acceptance": "PASS" if not issues else "FAIL",
        "contract_version": "transportation_v5_outcome_panel_validation_v1",
        "panel_row_count": len(rows),
        "cohort_results": cohort_results,
        "outcome_unavailable_reasons": dict(unavailable_reasons),
        "return_reconstruction": {
            key: value for key, value in return_audit.items() if key != "issues"
        },
        "historical_diagnostic_calibration_authorized": not issues,
        "production_activation_authorized": False,
        "artifacts": {
            "panel": {"path": str(panel_path), "sha256": file_sha256(panel_path)},
            "manifest": {"path": str(manifest_path), "sha256": file_sha256(manifest_path)},
            "normalized_price_slice": {"path": str(price_path), "sha256": file_sha256(price_path)},
            "coverage": {"path": str(coverage_path), "sha256": file_sha256(coverage_path)},
        },
        "issues": issues[:200],
        "next_gate": (
            "RUN_COHORT_SEPARATED_DIAGNOSTIC_CALIBRATION"
            if not issues
            else "REPAIR_V5_OUTCOME_PANEL"
        ),
    }
    write_text_atomic(output_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
