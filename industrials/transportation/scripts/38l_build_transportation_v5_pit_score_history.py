#!/usr/bin/env python3
"""Build the bounded monthly v5 PIT score history after rebuild validation.

This stage consumes only the already-loaded, exact-date feature panel.  It
materializes positioning through the shared industrials adapter, scores the
surface-freight and tanker cohorts independently, and emits an explicit
calibration-readiness sidecar.  It cannot authorize calibration or production.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.core.historical_score_history import run_logged, select_dates  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256, write_scoring_rows  # noqa: E402
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.scoring import build_scoring_rows  # noqa: E402
from industrials.transportation.surface_freight_score_engine import (  # noqa: E402
    load_cohort_score_policy,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_CONTRACT = ROOT / "investable_v5" / "prebuild_contract" / "2026-08-15" / "transportation_v5_prebuild_contract.json"
DEFAULT_REBUILD_VALIDATION = ROOT / "investable_v5" / "historical_rebuild" / "2026-08-15" / "transportation_v5_historical_rebuild_validation.json"
DEFAULT_OUTPUT_ROOT = ROOT / "investable_v5" / "pit_score_history" / "2026-08-15"
DEFAULT_SURFACE_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_surface_freight_score_policy_v3.yaml"
DEFAULT_TANKER_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_tanker_score_policy_v1.yaml"
REPORT_FIELDS = (
    "asof_date",
    "ticker_scope_sha256",
    "expected_ticker_count",
    "score_row_count",
    "positioning_row_count",
    "calibration_ready_count",
    "surface_freight_ready_count",
    "tanker_ready_count",
    "status",
    "elapsed_seconds",
    "message",
)
SIDECAR_FIELDS = (
    "asof_date",
    "ticker",
    "cohort_id",
    "historical_calibration_only_flag",
    "rank_ready_flag",
    "cohort_date_ready_flag",
    "calibration_input_ready_flag",
    "calibration_input_ready_reason",
    "current_portfolio_eligibility_authorized",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--rebuild-validation", type=Path, default=DEFAULT_REBUILD_VALIDATION)
    parser.add_argument("--surface-policy", type=Path, default=DEFAULT_SURFACE_POLICY)
    parser.add_argument("--tanker-policy", type=Path, default=DEFAULT_TANKER_POLICY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument("--selection", choices=("oldest", "newest"), default="oldest")
    parser.add_argument("--rebuild-existing", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def scope_hash(tickers: list[str]) -> str:
    payload = "\n".join(sorted(set(tickers))) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_report(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    return {str(row["asof_date"]): row for row in read_csv(path)}


def snapshot_valid(
    path: Path,
    *,
    asof: str,
    expected_tickers: list[str],
    validation_sha256: str,
    policy_hashes: dict[str, str],
) -> bool:
    score_path = path / "scoring_features.csv"
    sidecar_path = path / "calibration_eligibility.csv"
    manifest_path = path / "manifest.json"
    if not all(item.is_file() for item in (score_path, sidecar_path, manifest_path)):
        return False
    try:
        payload = read_json(manifest_path)
        return bool(
            payload.get("acceptance") == "PASS"
            and payload.get("asof_date") == asof
            and payload.get("ticker_scope_sha256") == scope_hash(expected_tickers)
            and payload.get("rebuild_validation_sha256") == validation_sha256
            and payload.get("policy_sha256") == policy_hashes
            and payload.get("score_row_count") == len(expected_tickers)
            and payload.get("score_sha256") == file_sha256(score_path)
            and payload.get("calibration_sidecar_sha256") == file_sha256(sidecar_path)
        )
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return False


def positioning_count(
    connection: sqlite3.Connection,
    *,
    asof: str,
    tickers: list[str],
) -> int:
    if not tickers:
        return 0
    marks = ",".join("?" for _ in tickers)
    return int(
        connection.execute(
            f"SELECT COUNT(DISTINCT ticker) FROM feature_positioning "
            f"WHERE model_family=? AND asof_date=? AND ticker IN ({marks})",
            (MODEL_FAMILY, asof, *tickers),
        ).fetchone()[0]
    )


def policy_by_calibration_pool(
    policies: tuple[dict[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    mapping = {str(policy["calibration_pool"]): policy for policy in policies}
    if len(mapping) != len(policies):
        raise ValueError("cohort score policies require unique calibration pools")
    return mapping


def governed_cohort_id(
    row: dict[str, Any], policies_by_pool: dict[str, dict[str, Any]]
) -> str:
    pool = str(row.get("calibration_cohort") or "")
    if pool not in policies_by_pool:
        raise KeyError(pool)
    return str(policies_by_pool[pool]["cohort_id"])


def main() -> int:
    args = parse_args()
    if args.max_dates < 0:
        raise ValueError("--max-dates cannot be negative")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    financial = family["financial"]
    scoring = family["scoring"]
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    contract_path = args.contract.expanduser().resolve()
    validation_path = args.rebuild_validation.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    surface_path = args.surface_policy.expanduser().resolve()
    tanker_path = args.tanker_policy.expanduser().resolve()
    contract = read_json(contract_path)
    policy_asof = str(contract.get("end_date") or "")[:10]
    if not policy_asof:
        raise ValueError("prebuild contract is missing its policy lock date")
    validation = read_json(validation_path)
    if validation.get("acceptance") != "PASS" or not validation.get(
        "historical_scoring_authorized"
    ):
        raise ValueError("historical rebuild validation has not authorized scoring")
    validation_sha256 = file_sha256(validation_path)
    policies = (
        load_cohort_score_policy(surface_path),
        load_cohort_score_policy(tanker_path),
    )
    policy_by_cohort = {str(item["cohort_id"]): item for item in policies}
    policy_by_pool = policy_by_calibration_pool(policies)
    surface_cohort_id = str(policies[0]["cohort_id"])
    tanker_cohort_id = str(policies[1]["cohort_id"])
    policy_hashes = {
        str(item["policy_version"]): file_sha256(path)
        for item, path in zip(policies, (surface_path, tanker_path))
    }
    scope_path = Path(str(contract["artifacts"]["bounded_rebuild_scope"]["path"]))
    date_path = Path(str(contract["artifacts"]["source_readiness_by_date"]["path"]))
    scope_rows = read_csv(scope_path)
    dates = sorted({str(row["asof_date"]) for row in read_csv(date_path)})
    scope_by_ticker = {str(row["ticker"]).upper(): row for row in scope_rows}
    all_tickers = sorted(scope_by_ticker)
    if len(all_tickers) != 44:
        raise ValueError(f"expected 44 frozen-scope tickers, found {len(all_tickers)}")
    report_path = output_root / "transportation_v5_pit_score_history_build.csv"
    result_path = output_root / "transportation_v5_pit_score_history_build.json"
    report_by_date = read_report(report_path)

    def effective_tickers(asof: str) -> list[str]:
        return sorted(
            ticker
            for ticker, row in scope_by_ticker.items()
            if str(row["effective_from"]) <= asof <= str(row["effective_to"])
        )

    valid_before = {
        asof
        for asof in dates
        if snapshot_valid(
            output_root / "snapshots" / asof,
            asof=asof,
            expected_tickers=effective_tickers(asof),
            validation_sha256=validation_sha256,
            policy_hashes=policy_hashes,
        )
    }
    pending_all = [
        asof for asof in dates if args.rebuild_existing or asof not in valid_before
    ]
    pending = select_dates(pending_all, maximum=args.max_dates, selection=args.selection)
    registry_path = resolve_path(financial["metric_registry"], base_dir=config_path.parent)
    registry_version, definitions = load_metric_registry(registry_path)
    eligibility_policy_path = resolve_path(
        cfg_get(config, "scoring_policy.families.transportation.eligibility_policy_csv"),
        base_dir=config_path.parent,
    )
    weights = {str(key): float(value) for key, value in scoring["component_weights"].items()}
    overlay_weights = {
        str(key): float(value)
        for key, value in (scoring.get("specialized_overlay_weights") or {}).items()
    }
    failures: list[str] = []
    output_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["INDUSTRIALS_HISTORICAL_APPEND"] = "1"
    for asof in pending:
        started = time.monotonic()
        expected = effective_tickers(asof)
        snapshot_dir = output_root / "snapshots" / asof
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        status = "PASS"
        message = ""
        rows: list[dict[str, str]] = []
        sidecar: list[dict[str, Any]] = []
        try:
            positioning_output = snapshot_dir / "positioning_features.csv"
            with sqlite3.connect(db_path) as check_connection:
                existing_positioning = positioning_count(
                    check_connection, asof=asof, tickers=expected
                )
            if existing_positioning < len(expected) or not positioning_output.is_file():
                command = [
                    sys.executable,
                    str(PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "09_import_transportation_positioning.py"),
                    "--db",
                    str(db_path),
                    "--asof",
                    asof,
                    "--include-historical-members",
                    "--feature-membership-mode",
                    "pit",
                    "--features-only",
                    "--tickers",
                    ",".join(expected),
                    "--snapshot-output-csv",
                    str(positioning_output),
                ]
                run_logged(
                    command,
                    cwd=PROJECT_ROOT,
                    stdout_path=snapshot_dir / "positioning.stdout.log",
                    stderr_path=snapshot_dir / "positioning.stderr.log",
                    environment=environment,
                )
            with connect(
                db_path,
                timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)),
            ) as connection:
                init_db(connection)
                rows = build_scoring_rows(
                    connection,
                    asof=asof,
                    active_source_id=str(universe["seed_source_id"]),
                    membership_mode="pit",
                    historical_source_id=str(universe.get("historical_membership_source_id") or ""),
                    delisted_source_id=str(universe.get("delisted_source_id") or ""),
                    metric_snapshot_mode="exact",
                    definitions=definitions,
                    registry_version=registry_version,
                    policy_path=eligibility_policy_path,
                    policy_asof=policy_asof,
                    component_weights=weights,
                    max_staleness_days=int(scoring["max_staleness_days"]),
                    minimum_avg_dollar_volume=float(scoring["minimum_avg_dollar_volume_60d"]),
                    minimum_score_confidence=float(scoring["minimum_score_confidence"]),
                    minimum_specialized_coverage=float(scoring["minimum_specialized_coverage"]),
                    positioning_source_id=str(scoring["positioning_feature_source_id"]),
                    minimum_positioning_input_coverage=float(scoring["minimum_positioning_input_coverage"]),
                    specialized_overlay_weights=overlay_weights,
                    classification_overlays_path=resolve_path(
                        universe["classification_overlays_csv"], base_dir=config_path.parent
                    ),
                    cohort_score_policies=list(policies),
                )
                pos_count = positioning_count(connection, asof=asof, tickers=expected)
            row_tickers = sorted(str(row["ticker"]).upper() for row in rows)
            if row_tickers != expected:
                raise ValueError(
                    f"score scope mismatch expected={expected} actual={row_tickers}"
                )
            ready_by_cohort = Counter(
                governed_cohort_id(row, policy_by_pool)
                for row in rows
                if str(row["rank_ready_flag"]) == "1"
            )
            cohort_gate = {
                cohort: ready_by_cohort[cohort] >= int(policy["minimum_active_cohort_size"])
                for cohort, policy in policy_by_cohort.items()
            }
            for row in rows:
                ticker = str(row["ticker"]).upper()
                cohort = governed_cohort_id(row, policy_by_pool)
                policy = policy_by_cohort[cohort]
                historical_only = ticker in (policy.get("historical_calibration_only") or {})
                rank_ready = str(row["rank_ready_flag"]) == "1"
                date_ready = cohort_gate[cohort]
                calibration_ready = rank_ready and date_ready
                reasons: list[str] = []
                if not rank_ready:
                    reasons.append(str(row.get("rank_ready_reason") or "rank_not_ready"))
                if not date_ready:
                    reasons.append("cohort_cross_section_below_policy_minimum")
                sidecar.append(
                    {
                        "asof_date": asof,
                        "ticker": ticker,
                        "cohort_id": cohort,
                        "historical_calibration_only_flag": int(historical_only),
                        "rank_ready_flag": int(rank_ready),
                        "cohort_date_ready_flag": int(date_ready),
                        "calibration_input_ready_flag": int(calibration_ready),
                        "calibration_input_ready_reason": "ok" if calibration_ready else ";".join(reasons),
                        "current_portfolio_eligibility_authorized": 0,
                    }
                )
            score_path = snapshot_dir / "scoring_features.csv"
            sidecar_path = snapshot_dir / "calibration_eligibility.csv"
            write_scoring_rows(score_path, rows)
            write_csv_atomic(sidecar_path, SIDECAR_FIELDS, sidecar)
            manifest = {
                "acceptance": "PASS",
                "contract_version": "transportation_v5_monthly_pit_score_snapshot_v1",
                "asof_date": asof,
                "membership_mode": "pit",
                "metric_snapshot_mode": "exact",
                "policy_asof": policy_asof,
                "ticker_scope_sha256": scope_hash(expected),
                "expected_ticker_count": len(expected),
                "score_row_count": len(rows),
                "positioning_row_count": pos_count,
                "calibration_ready_count": sum(
                    int(item["calibration_input_ready_flag"]) for item in sidecar
                ),
                "calibration_ready_count_by_cohort": dict(
                    Counter(
                        str(item["cohort_id"])
                        for item in sidecar
                        if int(item["calibration_input_ready_flag"]) == 1
                    )
                ),
                "historical_calibration_only_count": sum(
                    int(item["historical_calibration_only_flag"]) for item in sidecar
                ),
                "rebuild_validation_path": str(validation_path),
                "rebuild_validation_sha256": validation_sha256,
                "policy_sha256": policy_hashes,
                "score_sha256": file_sha256(score_path),
                "calibration_sidecar_sha256": file_sha256(sidecar_path),
                "calibration_authorized": False,
                "production_activation_authorized": False,
            }
            write_text_atomic(
                snapshot_dir / "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            )
        except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
            status = "FAIL"
            message = f"{type(exc).__name__}: {exc}"
            failures.append(f"{asof}:{message}")
            pos_count = 0
        counts = Counter(
            str(item.get("cohort_id") or "")
            for item in sidecar
            if int(item.get("calibration_input_ready_flag") or 0) == 1
        )
        report_by_date[asof] = {
            "asof_date": asof,
            "ticker_scope_sha256": scope_hash(expected),
            "expected_ticker_count": len(expected),
            "score_row_count": len(rows),
            "positioning_row_count": pos_count,
            "calibration_ready_count": sum(counts.values()),
            "surface_freight_ready_count": counts.get(surface_cohort_id, 0),
            "tanker_ready_count": counts.get(tanker_cohort_id, 0),
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "message": message,
        }
        write_csv_atomic(
            report_path,
            REPORT_FIELDS,
            [report_by_date[key] for key in sorted(report_by_date)],
        )
        if status == "FAIL":
            break

    valid_after = {
        asof
        for asof in dates
        if snapshot_valid(
            output_root / "snapshots" / asof,
            asof=asof,
            expected_tickers=effective_tickers(asof),
            validation_sha256=validation_sha256,
            policy_hashes=policy_hashes,
        )
    }
    pending_dates = sorted(set(dates) - valid_after)
    result = {
        "acceptance": "PASS" if not failures else "FAIL",
        "contract_version": "transportation_v5_monthly_pit_score_history_build_v1",
        "completion_status": "COMPLETE" if not pending_dates and not failures else "PARTIAL",
        "historical_date_count": len(dates),
        "valid_date_count": len(valid_after),
        "pending_date_count": len(pending_dates),
        "pending_dates": pending_dates,
        "bounded_ticker_count": len(all_tickers),
        "bounded_ticker_scope_sha256": scope_hash(all_tickers),
        "membership_mode": "pit",
        "metric_snapshot_mode": "exact",
        "observation_cadence": "month_end",
        "network_requests": 0,
        "parser_invocations": 0,
        "rebuild_validation_path": str(validation_path),
        "rebuild_validation_sha256": validation_sha256,
        "policy_sha256": policy_hashes,
        "historical_scoring_materialized": not pending_dates and not failures,
        "calibration_authorized": False,
        "production_activation_authorized": False,
        "report_path": str(report_path),
        "report_sha256": file_sha256(report_path),
        "failures": failures,
        "next_gate": (
            "VALIDATE_COHORT_ISOLATED_PIT_SCORE_HISTORY"
            if not pending_dates and not failures
            else "RESUME_COHORT_ISOLATED_PIT_SCORE_HISTORY"
        ),
    }
    write_text_atomic(result_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
