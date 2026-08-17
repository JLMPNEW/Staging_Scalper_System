#!/usr/bin/env python3
"""Validate the complete cohort-isolated v5 PIT score history."""
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

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.surface_freight_score_engine import (  # noqa: E402
    load_cohort_score_policy,
)


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_CONTRACT = ROOT / "investable_v5" / "prebuild_contract" / "2026-08-15" / "transportation_v5_prebuild_contract.json"
DEFAULT_REBUILD_VALIDATION = ROOT / "investable_v5" / "historical_rebuild" / "2026-08-15" / "transportation_v5_historical_rebuild_validation.json"
DEFAULT_SCORE_ROOT = ROOT / "investable_v5" / "pit_score_history" / "2026-08-15"
DEFAULT_OUTPUT_DIR = ROOT / "investable_v5" / "pit_score_validation" / "2026-08-15"
DEFAULT_SURFACE_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_surface_freight_score_policy_v3.yaml"
DEFAULT_TANKER_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_tanker_score_policy_v1.yaml"
DATE_FIELDS = (
    "asof_date",
    "cohort_id",
    "effective_ticker_count",
    "rank_ready_count",
    "calibration_ready_count",
    "minimum_cross_section",
    "cohort_date_ready_gate",
    "positioning_populated_count",
    "positioning_fraction",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--rebuild-validation", type=Path, default=DEFAULT_REBUILD_VALIDATION)
    parser.add_argument("--score-root", type=Path, default=DEFAULT_SCORE_ROOT)
    parser.add_argument("--surface-policy", type=Path, default=DEFAULT_SURFACE_POLICY)
    parser.add_argument("--tanker-policy", type=Path, default=DEFAULT_TANKER_POLICY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def governed_cohort_id(
    score: dict[str, str], policies_by_pool: dict[str, dict[str, Any]]
) -> str:
    pool = str(score.get("calibration_cohort") or "")
    if pool not in policies_by_pool:
        raise KeyError(pool)
    return str(policies_by_pool[pool]["cohort_id"])


def main() -> int:
    args = parse_args()
    contract_path = args.contract.expanduser().resolve()
    rebuild_path = args.rebuild_validation.expanduser().resolve()
    score_root = args.score_root.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    surface_path = args.surface_policy.expanduser().resolve()
    tanker_path = args.tanker_policy.expanduser().resolve()
    contract = read_json(contract_path)
    rebuild = read_json(rebuild_path)
    build_path = score_root / "transportation_v5_pit_score_history_build.json"
    build = read_json(build_path)
    policies = (
        load_cohort_score_policy(surface_path),
        load_cohort_score_policy(tanker_path),
    )
    policy_by_cohort = {str(item["cohort_id"]): item for item in policies}
    policy_by_pool = {str(item["calibration_pool"]): item for item in policies}
    if len(policy_by_pool) != len(policies):
        raise ValueError("cohort score policies require unique calibration pools")
    errors: list[str] = []
    if rebuild.get("acceptance") != "PASS" or not rebuild.get(
        "historical_scoring_authorized"
    ):
        errors.append("historical rebuild validation is not PASS")
    if build.get("acceptance") != "PASS" or build.get("completion_status") != "COMPLETE":
        errors.append("PIT score-history build is not complete PASS")
    if build.get("rebuild_validation_sha256") != file_sha256(rebuild_path):
        errors.append("score-history build does not pin the current rebuild validation")
    expected_policy_hashes = {
        str(policy["policy_version"]): file_sha256(path)
        for policy, path in zip(policies, (surface_path, tanker_path))
    }
    if build.get("policy_sha256") != expected_policy_hashes:
        errors.append("score-history policy hash mismatch")

    scope_path = Path(str(contract["artifacts"]["bounded_rebuild_scope"]["path"]))
    date_path = Path(str(contract["artifacts"]["source_readiness_by_date"]["path"]))
    scope_rows = read_csv(scope_path)
    dates = sorted({str(row["asof_date"]) for row in read_csv(date_path)})
    scope_by_ticker = {str(row["ticker"]).upper(): row for row in scope_rows}
    historical_only_by_cohort = {
        cohort: {
            str(ticker).upper()
            for ticker in (policy.get("historical_calibration_only") or {})
        }
        for cohort, policy in policy_by_cohort.items()
    }
    contribution: dict[str, Counter[str]] = defaultdict(Counter)
    ready_dates: Counter[str] = Counter()
    positioning_eligible_dates: Counter[str] = Counter()
    date_rows: list[dict[str, Any]] = []
    total_rows = 0
    total_historical_only_rows = 0
    for asof in dates:
        snapshot_dir = score_root / "snapshots" / asof
        score_path = snapshot_dir / "scoring_features.csv"
        sidecar_path = snapshot_dir / "calibration_eligibility.csv"
        manifest_path = snapshot_dir / "manifest.json"
        if not all(path.is_file() for path in (score_path, sidecar_path, manifest_path)):
            errors.append(f"{asof}: missing score snapshot artifacts")
            continue
        manifest = read_json(manifest_path)
        scores = read_csv(score_path)
        sidecar = read_csv(sidecar_path)
        expected = sorted(
            ticker
            for ticker, row in scope_by_ticker.items()
            if str(row["effective_from"]) <= asof <= str(row["effective_to"])
        )
        score_tickers = sorted(str(row.get("ticker") or "").upper() for row in scores)
        sidecar_tickers = sorted(str(row.get("ticker") or "").upper() for row in sidecar)
        if score_tickers != expected or sidecar_tickers != expected:
            errors.append(f"{asof}: exact PIT score scope mismatch")
            continue
        if manifest.get("acceptance") != "PASS":
            errors.append(f"{asof}: snapshot manifest is not PASS")
        if manifest.get("score_sha256") != file_sha256(score_path):
            errors.append(f"{asof}: score hash mismatch")
        if manifest.get("calibration_sidecar_sha256") != file_sha256(sidecar_path):
            errors.append(f"{asof}: calibration sidecar hash mismatch")
        scores_by_ticker = {str(row["ticker"]).upper(): row for row in scores}
        sidecar_by_ticker = {str(row["ticker"]).upper(): row for row in sidecar}
        total_rows += len(scores)
        for ticker in expected:
            score = scores_by_ticker[ticker]
            gate = sidecar_by_ticker[ticker]
            try:
                cohort = governed_cohort_id(score, policy_by_pool)
            except KeyError:
                errors.append(f"{asof}:{ticker}: unknown calibration pool")
                continue
            if cohort != str(gate.get("cohort_id") or ""):
                errors.append(f"{asof}:{ticker}: cohort mismatch")
                continue
            expected_historical = ticker in historical_only_by_cohort.get(cohort, set())
            actual_historical = str(gate.get("historical_calibration_only_flag") or "0") == "1"
            if expected_historical != actual_historical:
                errors.append(f"{asof}:{ticker}: historical-only flag mismatch")
            total_historical_only_rows += int(actual_historical)
            if str(gate.get("current_portfolio_eligibility_authorized") or "") != "0":
                errors.append(f"{asof}:{ticker}: historical row authorized for portfolio")
            rank_ready = str(score.get("rank_ready_flag") or "0") == "1"
            calibration_ready = str(gate.get("calibration_input_ready_flag") or "0") == "1"
            cohort_date_ready = str(gate.get("cohort_date_ready_flag") or "0") == "1"
            if calibration_ready != (rank_ready and cohort_date_ready):
                errors.append(f"{asof}:{ticker}: calibration readiness mismatch")
            if calibration_ready:
                contribution[ticker][cohort] += 1
        for cohort, policy in policy_by_cohort.items():
            cohort_scores = [
                row
                for row in scores
                if str(row.get("calibration_cohort") or "")
                == str(policy["calibration_pool"])
            ]
            cohort_sidecar = [
                row for row in sidecar if str(row.get("cohort_id") or "") == cohort
            ]
            rank_count = sum(str(row.get("rank_ready_flag") or "0") == "1" for row in cohort_scores)
            ready_count = sum(
                str(row.get("calibration_input_ready_flag") or "0") == "1"
                for row in cohort_sidecar
            )
            minimum = int(policy["minimum_active_cohort_size"])
            gate = ready_count >= minimum
            ready_dates[cohort] += int(gate)
            positioning_count = sum(
                bool(str(row.get("positioning_score") or "")) for row in cohort_scores
            )
            fraction = positioning_count / len(cohort_scores) if cohort_scores else 0.0
            position_gate = policy.get("positioning_history_gate") or {}
            minimum_positioning_cross_section = int(
                position_gate.get("minimum_date_cross_section") or minimum
            )
            positioning_eligible_dates[cohort] += int(
                positioning_count >= minimum_positioning_cross_section
            )
            date_rows.append(
                {
                    "asof_date": asof,
                    "cohort_id": cohort,
                    "effective_ticker_count": len(cohort_scores),
                    "rank_ready_count": rank_count,
                    "calibration_ready_count": ready_count,
                    "minimum_cross_section": minimum,
                    "cohort_date_ready_gate": "PASS" if gate else "FAIL",
                    "positioning_populated_count": positioning_count,
                    "positioning_fraction": round(fraction, 6),
                }
            )

    cohort_results: dict[str, Any] = {}
    for cohort, policy in policy_by_cohort.items():
        cohort_tickers = sorted(
            ticker
            for ticker, row in scope_by_ticker.items()
            if str(row["cohort_id"]) == cohort
        )
        required_dates = int(policy["historical_prebuild_gate"]["minimum_source_ready_dates"])
        zero_current_contributors = sorted(
            ticker for ticker in cohort_tickers if contribution[ticker][cohort] == 0
            and ticker not in historical_only_by_cohort[cohort]
        )
        noncontributing_historical_only = sorted(
            ticker for ticker in cohort_tickers if contribution[ticker][cohort] == 0
            and ticker in historical_only_by_cohort[cohort]
        )
        if ready_dates[cohort] < required_dates:
            errors.append(
                f"{cohort}: score-ready dates={ready_dates[cohort]} below {required_dates}"
            )
        if zero_current_contributors:
            errors.append(
                f"{cohort}: current tickers with zero score-ready contribution="
                f"{zero_current_contributors}"
            )
        cohort_date_count = sum(
            str(row["cohort_id"]) == cohort and int(row["effective_ticker_count"]) > 0
            for row in date_rows
        )
        position_gate = policy.get("positioning_history_gate") or {}
        minimum_eligible_fraction = float(
            position_gate.get("minimum_eligible_date_fraction") or 0.0
        )
        actual_position_fraction = (
            positioning_eligible_dates[cohort] / cohort_date_count
            if cohort_date_count
            else 0.0
        )
        positioning_pass = actual_position_fraction >= minimum_eligible_fraction
        cohort_results[cohort] = {
            "score_ready_date_count": ready_dates[cohort],
            "minimum_score_ready_date_count": required_dates,
            "zero_current_score_ready_contributors": zero_current_contributors,
            "noncontributing_historical_only_tickers": noncontributing_historical_only,
            "contribution_dates_by_ticker": {
                ticker: contribution[ticker][cohort] for ticker in cohort_tickers
            },
            "positioning_eligible_date_count": positioning_eligible_dates[cohort],
            "positioning_eligible_date_fraction": actual_position_fraction,
            "minimum_positioning_eligible_date_fraction": minimum_eligible_fraction,
            "positioning_candidate_history_gate": "PASS" if positioning_pass else "FAIL",
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = output_dir / "transportation_v5_pit_score_history_coverage.csv"
    output_path = output_dir / "transportation_v5_pit_score_history_validation.json"
    write_csv_atomic(coverage_path, DATE_FIELDS, date_rows)
    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "contract_version": "transportation_v5_pit_score_history_validation_v1",
        "historical_date_count": len(dates),
        "score_row_count": total_rows,
        "historical_calibration_only_row_count": total_historical_only_rows,
        "cohort_results": cohort_results,
        "network_requests": 0,
        "parser_invocations": 0,
        "historical_score_history_validated": not errors,
        "bounded_research_calibration_authorized": not errors,
        "production_activation_authorized": False,
        "artifacts": {
            "prebuild_contract": {"path": str(contract_path), "sha256": file_sha256(contract_path)},
            "rebuild_validation": {"path": str(rebuild_path), "sha256": file_sha256(rebuild_path)},
            "score_history_build": {"path": str(build_path), "sha256": file_sha256(build_path)},
            "coverage": {"path": str(coverage_path), "sha256": file_sha256(coverage_path)},
        },
        "errors": errors,
        "next_gate": (
            "BUILD_RECONCILED_FORWARD_OUTCOME_PANEL_AND_RUN_PRE_REGISTERED_CANDIDATES"
            if not errors
            else "REPAIR_PIT_SCORE_HISTORY"
        ),
    }
    write_text_atomic(output_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
