#!/usr/bin/env python3
"""Validate the bounded v5 PIT feature rebuild before any historical scoring."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.surface_freight_score_engine import (  # noqa: E402
    load_cohort_score_policy,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_CONTRACT = ROOT / "investable_v5" / "prebuild_contract" / "2026-08-15" / "transportation_v5_prebuild_contract.json"
DEFAULT_BUILD_MANIFEST = ROOT / "historical_features" / "transportation_pit_feature_history_build.json"
DEFAULT_BUILD_REPORT = ROOT / "historical_features" / "transportation_pit_feature_history_build.csv"
DEFAULT_OUTPUT_DIR = ROOT / "investable_v5" / "historical_rebuild" / "2026-08-15"
DEFAULT_SURFACE_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_surface_freight_score_policy_v3.yaml"
DEFAULT_TANKER_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_tanker_score_policy_v1.yaml"
OBSERVED = frozenset({"REPORTED", "DERIVED", "PROXY"})
DATE_FIELDS = (
    "cohort_id",
    "asof_date",
    "effective_ticker_count",
    "required_ready_count",
    "minimum_cross_section",
    "required_ready_gate",
    "retained_specialized_observed_count",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--build-manifest", type=Path, default=DEFAULT_BUILD_MANIFEST)
    parser.add_argument("--build-report", type=Path, default=DEFAULT_BUILD_REPORT)
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


def read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def scope_hash(tickers: list[str]) -> str:
    return hashlib.sha256(("\n".join(sorted(tickers)) + "\n").encode("utf-8")).hexdigest()


def verify_artifact_tree(payload: dict[str, Any], prefix: str = "") -> list[str]:
    errors: list[str] = []
    for artifact_id, raw in payload.items():
        qualified_id = f"{prefix}.{artifact_id}" if prefix else artifact_id
        if isinstance(raw, dict) and not {"path", "sha256"} <= set(raw):
            errors.extend(verify_artifact_tree(raw, qualified_id))
            continue
        item = dict(raw)
        path = Path(str(item.get("path") or ""))
        expected = str(item.get("sha256") or "")
        if not path.is_file():
            errors.append(f"missing pinned artifact={qualified_id}:{path}")
        elif file_sha256(path) != expected:
            errors.append(f"pinned artifact hash mismatch={qualified_id}:{path}")
    return errors


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    contract_path = args.contract.expanduser().resolve()
    build_manifest_path = args.build_manifest.expanduser().resolve()
    build_report_path = args.build_report.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    surface = load_cohort_score_policy(args.surface_policy.expanduser().resolve())
    tanker = load_cohort_score_policy(args.tanker_policy.expanduser().resolve())
    policies = (surface, tanker)
    contract = read_json(contract_path)
    build = read_json(build_manifest_path)
    report = read_csv(build_report_path)
    errors = verify_artifact_tree(dict(contract.get("artifacts") or {}))
    if contract.get("acceptance") != "PASS" or not contract.get(
        "historical_reconstruction_authorized"
    ):
        errors.append("prebuild contract did not authorize reconstruction")
    if build.get("acceptance") != "PASS":
        errors.append("historical build batch is not PASS")
    if build.get("completion_status") != "COMPLETE":
        errors.append(f"historical build is not complete={build.get('completion_status')}")

    scope_path = Path(
        str(contract["artifacts"]["bounded_rebuild_scope"]["path"])
    )
    scope_rows = read_csv(scope_path)
    tickers = sorted(str(row["ticker"]).upper() for row in scope_rows)
    expected_scope_hash = scope_hash(tickers)
    if len(tickers) != 44 or len(set(tickers)) != 44:
        errors.append(f"bounded scope count={len(tickers)} expected=44 unique")
    if str(build.get("ticker_scope_sha256") or "") != expected_scope_hash:
        errors.append("historical build ticker-scope hash mismatch")
    source_dates_path = Path(
        str(contract["artifacts"]["source_readiness_by_date"]["path"])
    )
    dates = sorted(
        {str(row["asof_date"]) for row in read_csv(source_dates_path)}
    )
    matching_report = {
        str(row.get("asof_date") or ""): row
        for row in report
        if str(row.get("ticker_scope_sha256") or "") == expected_scope_hash
        and str(row.get("asof_date") or "") in dates
    }
    if set(matching_report) != set(dates):
        errors.append(
            f"scope-matched build dates={len(matching_report)} expected={len(dates)}"
        )
    failed_report_dates = sorted(
        asof
        for asof, row in matching_report.items()
        if str(row.get("status") or "") != "PASS"
    )
    if failed_report_dates:
        errors.append(f"scope-matched failed build dates={failed_report_dates}")

    scope_by_ticker = {str(row["ticker"]).upper(): row for row in scope_rows}
    cohort_by_ticker = {
        ticker: str(row["cohort_id"]) for ticker, row in scope_by_ticker.items()
    }
    policy_by_cohort = {str(policy["cohort_id"]): policy for policy in policies}
    registry_path = Path(
        str(contract["artifacts"]["current_scores"]["path"])
    )
    current_score_manifest = read_json(registry_path)
    metric_registry_path = Path(
        str(current_score_manifest["artifacts"]["metric_registry"]["path"])
    )
    _, definitions = load_metric_registry(metric_registry_path)
    required_ids = {item.metric_id for item in definitions if item.required_for_rank}
    if len(required_ids) != 11:
        errors.append(f"registry-wide required metric count={len(required_ids)} expected=11")
    operating_required = {
        "ret_3m",
        "ret_6m",
        "relative_strength_3m",
        "realized_volatility_60d",
        "maximum_drawdown_12m",
        "average_dollar_volume_60d",
        "operating_margin",
        "fcf_margin",
        "capex_to_revenue",
    }
    metric_count = len(definitions)
    date_rows: list[dict[str, Any]] = []
    actual_ready_dates: Counter[str] = Counter()
    actual_contribution: dict[str, Counter[str]] = defaultdict(Counter)
    actual_missing_by_ticker: dict[str, Counter[str]] = defaultdict(Counter)
    actual_missing_by_metric: dict[str, Counter[str]] = defaultdict(Counter)
    database_errors: list[str] = []
    with read_only(db_path) as connection:
        for asof in dates:
            effective = [
                ticker
                for ticker, row in scope_by_ticker.items()
                if str(row["effective_from"]) <= asof <= str(row["effective_to"])
            ]
            marks = ",".join("?" for _ in effective)
            for table in ("feature_market_technical", "feature_financial_statement"):
                count = int(
                    connection.execute(
                        f"SELECT COUNT(DISTINCT ticker) FROM {table} "
                        f"WHERE model_family=? AND asof_date=? AND ticker IN ({marks})",
                        (MODEL_FAMILY, asof, *effective),
                    ).fetchone()[0]
                )
                if count != len(effective):
                    database_errors.append(
                        f"{asof}:{table}={count}/{len(effective)}"
                    )
            availability_rows = connection.execute(
                f"""
                SELECT ticker,metric_name,availability_status
                FROM feature_financial_metric_availability
                WHERE model_family=? AND asof_date=? AND ticker IN ({marks})
                """,
                (MODEL_FAMILY, asof, *effective),
            ).fetchall()
            availability_counts = Counter(str(row["ticker"]) for row in availability_rows)
            bad_counts = sorted(
                ticker
                for ticker in effective
                if availability_counts[ticker] != metric_count
            )
            if bad_counts:
                database_errors.append(
                    f"{asof}:availability_not_{metric_count}={bad_counts}"
                )
            statuses = {
                (str(row["ticker"]), str(row["metric_name"])): str(
                    row["availability_status"]
                )
                for row in availability_rows
            }
            for cohort_id, policy in policy_by_cohort.items():
                cohort_tickers = [
                    ticker for ticker in effective if cohort_by_ticker[ticker] == cohort_id
                ]
                ready = {
                    ticker
                    for ticker in cohort_tickers
                    if all(statuses.get((ticker, metric)) in OBSERVED for metric in operating_required)
                }
                for ticker in cohort_tickers:
                    for metric in operating_required:
                        if statuses.get((ticker, metric)) not in OBSERVED:
                            actual_missing_by_ticker[ticker][metric] += 1
                            actual_missing_by_metric[cohort_id][metric] += 1
                specialized = {
                    (ticker, metric)
                    for ticker in cohort_tickers
                    for metric in policy["score_construction"]["retained_specialized_metrics"]
                    if statuses.get((ticker, str(metric))) in OBSERVED
                }
                minimum = int(policy["minimum_active_cohort_size"])
                gate = len(ready) >= minimum
                actual_ready_dates[cohort_id] += int(gate)
                for ticker in ready:
                    actual_contribution[ticker][cohort_id] += 1
                date_rows.append(
                    {
                        "cohort_id": cohort_id,
                        "asof_date": asof,
                        "effective_ticker_count": len(cohort_tickers),
                        "required_ready_count": len(ready),
                        "minimum_cross_section": minimum,
                        "required_ready_gate": "PASS" if gate else "FAIL",
                        "retained_specialized_observed_count": len(specialized),
                    }
                )
    errors.extend(database_errors[:50])
    cohort_results: dict[str, Any] = {}
    for cohort_id, policy in policy_by_cohort.items():
        cohort_tickers = sorted(
            ticker for ticker in tickers if cohort_by_ticker[ticker] == cohort_id
        )
        required_dates = int(
            policy["historical_prebuild_gate"]["minimum_source_ready_dates"]
        )
        zero = sorted(
            ticker for ticker in cohort_tickers if actual_contribution[ticker][cohort_id] == 0
        )
        if actual_ready_dates[cohort_id] < required_dates:
            errors.append(
                f"{cohort_id}: actual ready dates={actual_ready_dates[cohort_id]} below {required_dates}"
            )
        if zero:
            errors.append(f"{cohort_id}: actual zero-contribution tickers={zero}")
        cohort_results[cohort_id] = {
            "actual_required_ready_date_count": actual_ready_dates[cohort_id],
            "minimum_required_ready_date_count": required_dates,
            "zero_contribution_tickers": zero,
            "contribution_dates_by_ticker": {
                ticker: actual_contribution[ticker][cohort_id]
                for ticker in cohort_tickers
            },
            "missing_required_dates_by_ticker": {
                ticker: dict(actual_missing_by_ticker[ticker])
                for ticker in cohort_tickers
                if actual_missing_by_ticker[ticker]
            },
            "missing_required_cells_by_metric": dict(
                actual_missing_by_metric[cohort_id]
            ),
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = output_dir / "transportation_v5_historical_rebuild_coverage.csv"
    output_path = output_dir / "transportation_v5_historical_rebuild_validation.json"
    write_csv_atomic(coverage_path, DATE_FIELDS, date_rows)
    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "contract_version": "transportation_v5_historical_rebuild_validation_v1",
        "historical_date_count": len(dates),
        "bounded_ticker_count": len(tickers),
        "metric_count": metric_count,
        "cohort_results": cohort_results,
        "network_requests": 0,
        "parser_invocations": 0,
        "historical_reconstruction_validated": not errors,
        "historical_scoring_authorized": not errors,
        "calibration_authorized": False,
        "production_activation_authorized": False,
        "artifacts": {
            "prebuild_contract": {"path": str(contract_path), "sha256": file_sha256(contract_path)},
            "build_manifest": {"path": str(build_manifest_path), "sha256": file_sha256(build_manifest_path)},
            "build_report": {"path": str(build_report_path), "sha256": file_sha256(build_report_path)},
            "coverage": {"path": str(coverage_path), "sha256": file_sha256(coverage_path)},
        },
        "errors": errors,
        "next_gate": (
            "BUILD_COHORT_ISOLATED_PIT_SCORE_HISTORY"
            if not errors
            else "REPAIR_BOUNDED_HISTORICAL_REBUILD"
        ),
    }
    write_text_atomic(output_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
