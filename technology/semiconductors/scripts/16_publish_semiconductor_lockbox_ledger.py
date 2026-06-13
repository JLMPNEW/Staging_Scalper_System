#!/usr/bin/env python3
"""Publish semiconductor lockbox ledger and signal registry reports.

This stage is governance-only. It reads config, diagnostics, manifests, and the
latest production score rows, then writes auditable report files without
changing source data or model scores.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.scoring_features import SUBFEATURE_SPECS  # noqa: E402
from technology.semiconductors.calibrated_scoring import (  # noqa: E402
    component_weight_specs,
    subfeature_weight_specs,
)
from technology.semiconductors.optuna_calibration import write_csv  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "semiconductor_governance_reports"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish semiconductor lockbox ledger and signal registry.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def readonly_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db_path.expanduser().resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def safe_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"json_error": str(exc)}


def sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def csv_row_count(path: Path) -> int | str:
    if not path.exists() or path.suffix.lower() != ".csv":
        return ""
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    return max(0, len(rows) - 1)


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def rel_or_abs(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def artifact_row(name: str, artifact_type: str, path: Path, required: bool = True) -> dict[str, Any]:
    exists = path.exists() and path.is_file() and path.stat().st_size > 0
    return {
        "artifact_name": name,
        "artifact_type": artifact_type,
        "path": rel_or_abs(path),
        "required_flag": int(required),
        "exists_flag": int(exists),
        "size_bytes": path.stat().st_size if exists else 0,
        "sha256": sha256_file(path) if exists else "",
        "row_count": csv_row_count(path) if exists else "",
    }


def score_to_signal_map() -> dict[str, str]:
    return {score_key: signal for signal, score_key, _higher, _filter in SUBFEATURE_SPECS}


def diagnostics_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        signal = str(row.get("signal") or "")
        group = str(row.get("group") or "")
        horizon = safe_int(row.get("horizon_days"))
        if signal and group and horizon:
            out[(signal, group, horizon)] = row
    return out


def fallback_diag(
    index: dict[tuple[str, str, int], dict[str, Any]],
    signal: str,
    component: str,
    horizon: int,
) -> tuple[dict[str, Any] | None, str]:
    exact = index.get((signal, component, horizon))
    if exact:
        return exact, component
    for (diag_signal, diag_group, diag_horizon), row in index.items():
        if diag_signal == signal and diag_horizon == horizon:
            return row, diag_group
    return None, ""


def stage8_candidate_map(config: dict[str, Any]) -> dict[str, set[str]]:
    raw = cfg_get(config, "semiconductor_optuna_calibration.subfeature_candidates", {}) or {}
    out: dict[str, set[str]] = {}
    if isinstance(raw, dict):
        for component, keys in raw.items():
            if isinstance(keys, list):
                out[str(component)] = {str(key) for key in keys}
    return out


def birthdate_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("signal") or ""): row for row in rows if row.get("signal")}


def decision_status(
    *,
    policy: str,
    production_effective_weight: float,
    stage8_candidate: bool,
    keep_21: int,
    keep_63: int,
) -> str:
    policy = policy.strip().lower()
    if production_effective_weight > 0:
        return "production_locked"
    if policy == "measurement_only":
        return "measurement_only_review" if keep_21 and keep_63 else "measurement_only_blocked"
    if policy.startswith("blocked"):
        return "production_blocked"
    if policy.startswith("planned") or policy.startswith("optional"):
        return "planned_not_loaded"
    if policy.startswith("stage11"):
        return "stage11_candidate"
    if stage8_candidate and (keep_21 or keep_63):
        return "research_candidate"
    if policy.startswith("zero_weight"):
        return "zero_weight_locked"
    return "review"


def build_signal_registry_rows(
    *,
    registry: dict[str, Any],
    config: dict[str, Any],
    diagnostics_rows: list[dict[str, Any]],
    birthdate_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    score_to_signal = score_to_signal_map()
    component_weights = component_weight_specs(config)
    subfeature_weights = {
        component: {score_key: weight for score_key, weight in specs}
        for component, specs in subfeature_weight_specs(config).items()
    }
    stage8_candidates = stage8_candidate_map(config)
    diag = diagnostics_index(diagnostics_rows)
    birthdates = birthdate_map(birthdate_rows)
    rows: list[dict[str, Any]] = []

    for item in registry.get("signals", []) or []:
        if not isinstance(item, dict):
            continue
        signal = str(item.get("signal") or "")
        score_key = str(item.get("score_key") or "")
        components = item.get("components") or []
        if isinstance(components, str):
            components = [components]
        if not signal and score_key:
            signal = score_to_signal.get(score_key, score_key.removesuffix("_score"))
        for component in [str(value) for value in components]:
            weight = float(subfeature_weights.get(component, {}).get(score_key, 0.0))
            component_weight = float(component_weights.get(component, 0.0))
            effective_weight = component_weight * weight
            diag21, diag_group21 = fallback_diag(diag, signal, component, 21)
            diag63, diag_group63 = fallback_diag(diag, signal, component, 63)
            keep21 = safe_int((diag21 or {}).get("keep_candidate"))
            keep63 = safe_int((diag63 or {}).get("keep_candidate"))
            candidate_flag = score_key in stage8_candidates.get(component, set())
            policy = str(item.get("production_policy") or "")
            birth = birthdates.get(signal, {})
            rows.append(
                {
                    "registry_type": "subfeature_signal",
                    "signal": signal,
                    "score_key": score_key,
                    "component": component,
                    "source_layer": item.get("source_layer", ""),
                    "source_ids": ";".join(str(value) for value in (item.get("source_ids") or [])),
                    "production_policy": policy,
                    "decision_status": decision_status(
                        policy=policy,
                        production_effective_weight=effective_weight,
                        stage8_candidate=candidate_flag,
                        keep_21=keep21,
                        keep_63=keep63,
                    ),
                    "stage7_component_weight": component_weight,
                    "stage7_subfeature_weight": weight,
                    "stage7_effective_weight": effective_weight,
                    "stage8_candidate_flag": int(candidate_flag),
                    "birthdate": birth.get("birthdate", ""),
                    "birthdate_source_scope": birth.get("source_scope", ""),
                    "ic_21d": (diag21 or {}).get("mean_ic", ""),
                    "nw_t_21d": (diag21 or {}).get("ic_t_stat", ""),
                    "raw_t_21d": (diag21 or {}).get("raw_ic_t_stat", ""),
                    "keep_candidate_21d": keep21,
                    "diagnostic_group_21d": diag_group21,
                    "ic_63d": (diag63 or {}).get("mean_ic", ""),
                    "nw_t_63d": (diag63 or {}).get("ic_t_stat", ""),
                    "raw_t_63d": (diag63 or {}).get("raw_ic_t_stat", ""),
                    "keep_candidate_63d": keep63,
                    "diagnostic_group_63d": diag_group63,
                    "notes": item.get("notes", ""),
                }
            )

    for item in registry.get("overlay_components", []) or []:
        if not isinstance(item, dict):
            continue
        component = str(item.get("component") or "")
        if not component:
            continue
        rows.append(
            {
                "registry_type": "overlay_component",
                "signal": component,
                "score_key": "",
                "component": component,
                "source_layer": item.get("source_layer", ""),
                "source_ids": ";".join(str(value) for value in (item.get("source_ids") or [])),
                "production_policy": item.get("production_policy", ""),
                "decision_status": "overlay_loaded" if str(item.get("production_policy")) == "loaded_overlay" else "planned_not_loaded",
                "stage7_component_weight": "",
                "stage7_subfeature_weight": "",
                "stage7_effective_weight": "",
                "stage8_candidate_flag": 0,
                "birthdate": "",
                "birthdate_source_scope": "",
                "ic_21d": "",
                "nw_t_21d": "",
                "raw_t_21d": "",
                "keep_candidate_21d": "",
                "diagnostic_group_21d": "",
                "ic_63d": "",
                "nw_t_63d": "",
                "raw_t_63d": "",
                "keep_candidate_63d": "",
                "diagnostic_group_63d": "",
                "notes": item.get("notes", ""),
            }
        )

    return sorted(rows, key=lambda row: (str(row["registry_type"]), str(row["component"]), str(row["signal"])))


def latest_stage7_summary(conn: sqlite3.Connection, source_id: str, model_family: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT asof_date,
               COUNT(*) AS rows,
               SUM(CASE WHEN rank_ready_flag = 1 THEN 1 ELSE 0 END) AS rank_ready,
               MIN(final_score) AS min_score,
               MAX(final_score) AS max_score,
               AVG(final_score) AS avg_score,
               COUNT(DISTINCT final_rank) AS distinct_ranks
        FROM feature_scoring_model_output
        WHERE source_id = ?
          AND model_family = ?
          AND asof_date = (
              SELECT MAX(asof_date)
              FROM feature_scoring_model_output
              WHERE source_id = ? AND model_family = ?
          )
        """,
        (source_id, model_family, source_id, model_family),
    ).fetchone()
    return dict(row) if row is not None else {}


def top_ranked(conn: sqlite3.Connection, source_id: str, model_family: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ticker, asof_date, final_rank, final_score, rank_ready_flag, model_status
        FROM feature_scoring_model_output
        WHERE source_id = ?
          AND model_family = ?
          AND rank_ready_flag = 1
          AND asof_date = (
              SELECT MAX(asof_date)
              FROM feature_scoring_model_output
              WHERE source_id = ? AND model_family = ?
          )
        ORDER BY final_rank
        LIMIT ?
        """,
        (source_id, model_family, source_id, model_family, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def best_backtest_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    preferred = [
        row for row in rows
        if row.get("model_name") == "stage7_current_v1"
        and row.get("portfolio_name") == "top_decile"
        and row.get("weight_method") == "score_weight"
        and row.get("exposure_mode") == "long_only"
    ]
    if preferred:
        return preferred[0]
    if not rows:
        return {}
    return max(rows, key=lambda row: safe_float(row.get("annualized_return")) or -999.0)


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    registry_path = args.registry.expanduser().resolve() if args.registry else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.signal_registry_path", "semiconductors/data/semiconductor_signal_registry.yaml"),
        base_dir=base_dir,
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/governance"),
        base_dir=base_dir,
    )
    diagnostics_dir = resolve_path(
        cfg_get(config, "semiconductor_signal_diagnostics.output_dir", "../output/technology_reports/signal_diagnostics"),
        base_dir=base_dir,
    )
    scoring_dir = resolve_path("../output/technology_reports/scoring", base_dir=base_dir)
    optuna_dir = resolve_path(
        cfg_get(config, "semiconductor_optuna_calibration.output_dir", "../output/technology_reports/optuna_calibration"),
        base_dir=base_dir,
    )
    backtest_dir = resolve_path(
        cfg_get(config, "semiconductor_portfolio_backtest.output_dir", "../output/technology_reports/backtests"),
        base_dir=base_dir,
    )
    dashboard_dir = resolve_path(
        cfg_get(config, "semiconductor_dashboard_reports.output_dir", "../output/technology_reports/dashboard"),
        base_dir=base_dir,
    )
    audit_dir = resolve_path("../output/technology_reports/audits", base_dir=base_dir)
    market_dir = resolve_path("../output/technology_reports/market_data", base_dir=base_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    registry = load_yaml(registry_path)
    subfeature_rows = read_csv_rows(diagnostics_dir / "subfeature_ic.csv")
    birthdate_rows = read_csv_rows(diagnostics_dir / "signal_birthdates.csv")
    signal_rows = build_signal_registry_rows(
        registry=registry,
        config=config,
        diagnostics_rows=subfeature_rows,
        birthdate_rows=birthdate_rows,
    )

    signal_registry_csv = output_dir / "semiconductor_signal_registry.csv"
    signal_registry_json = output_dir / "semiconductor_signal_registry.json"
    write_csv(signal_registry_csv, signal_rows)
    signal_registry_json.write_text(
        json.dumps(
            {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "registry_version": registry.get("registry_version", ""),
                "model_family": registry.get("model_family", "semiconductors"),
                "rows": signal_rows,
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )

    stage7_source_id = str(cfg_get(config, "semiconductor_calibrated_scoring.source_id", "semiconductor_calibrated_score_v1"))
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    with readonly_connect(db_path) as conn:
        stage7_summary = latest_stage7_summary(conn, stage7_source_id, model_family)
        top10 = top_ranked(conn, stage7_source_id, model_family, limit=10)

    stage8_weights = read_json(optuna_dir / "stage8_best_weights.json")
    walk_forward_summary = read_json(optuna_dir / "walk_forward" / "walk_forward_summary.json")
    backtest_rows = read_csv_rows(backtest_dir / "semiconductor_portfolio_backtest_summary.csv")
    dashboard_manifest = read_json(dashboard_dir / "semiconductor_dashboard_manifest.json")
    audit_summary = read_json(audit_dir / "semiconductor_pipeline_audit.json")
    best_backtest = best_backtest_row(backtest_rows)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    artifacts = [
        artifact_row("technology_config", "input_config", config_path),
        artifact_row("signal_registry_source", "input_registry", registry_path),
        artifact_row("stage6_scoring_contract", "model_input", scoring_dir / "semiconductor_scoring_feature_contract.csv"),
        artifact_row("stage7_calibrated_scores", "production_scores", scoring_dir / "semiconductor_stage7_calibrated_scores.csv"),
        artifact_row("stage7_validation", "validation", scoring_dir / "semiconductor_stage7_validation.csv"),
        artifact_row("signal_registry_report", "governance", signal_registry_csv),
        artifact_row("component_ic", "diagnostics", diagnostics_dir / "component_ic.csv"),
        artifact_row("subfeature_ic", "diagnostics", diagnostics_dir / "subfeature_ic.csv"),
        artifact_row("signal_birthdates", "diagnostics", diagnostics_dir / "signal_birthdates.csv"),
        artifact_row("wsts_cycle_regime_ic", "diagnostics", diagnostics_dir / "wsts_cycle_regime_ic.csv", required=False),
        artifact_row("stage8_best_weights", "research_calibration", optuna_dir / "stage8_best_weights.json", required=False),
        artifact_row("stage8_trials", "research_calibration", optuna_dir / "stage8_trials.csv", required=False),
        artifact_row("walk_forward_summary", "research_calibration", optuna_dir / "walk_forward" / "walk_forward_summary.json", required=False),
        artifact_row("portfolio_backtest_summary", "backtest", backtest_dir / "semiconductor_portfolio_backtest_summary.csv"),
        artifact_row("portfolio_backtest_manifest", "backtest", backtest_dir / "semiconductor_portfolio_backtest_manifest.json"),
        artifact_row("dashboard_manifest", "dashboard", dashboard_dir / "semiconductor_dashboard_manifest.json"),
        artifact_row("pipeline_audit", "audit", audit_dir / "semiconductor_pipeline_audit.json"),
        artifact_row("research_hardening_audit", "audit", audit_dir / "semiconductor_research_hardening.csv"),
        artifact_row("norgate_delisted_price_import", "survivorship_backfill", market_dir / "norgate_delisted_price_import.csv", required=False),
    ]

    lockbox = {
        "generated_at_utc": generated_at,
        "snapshot_id": f"semiconductor_lockbox_{snapshot_stamp}",
        "database_path": str(db_path),
        "git_commit": git_commit(),
        "config_sha256": sha256_file(config_path),
        "registry_sha256": sha256_file(registry_path),
        "model_family": model_family,
        "production_source_id": stage7_source_id,
        "stage7_summary": stage7_summary,
        "top10_rank_ready": top10,
        "stage8_research_decision": {
            "promotion_candidate": stage8_weights.get("promotion_candidate", ""),
            "objective_improvement": stage8_weights.get("objective_improvement", ""),
            "fold_win_fraction": stage8_weights.get("fold_win_fraction", ""),
            "source_id": stage8_weights.get("source_id", ""),
        },
        "walk_forward_decision": {
            "procedure_adds_value": walk_forward_summary.get("procedure_adds_value", ""),
            "refit_win_rate": walk_forward_summary.get("refit_win_rate", ""),
            "mean_objective_improvement": walk_forward_summary.get("mean_objective_improvement", ""),
        },
        "backtest_reference": best_backtest,
        "dashboard_manifest": dashboard_manifest,
        "audit_check_summary": audit_summary.get("check_summary", {}),
        "signal_registry_summary": {
            "rows": len(signal_rows),
            "production_locked": sum(1 for row in signal_rows if row.get("decision_status") == "production_locked"),
            "research_candidate": sum(1 for row in signal_rows if row.get("decision_status") == "research_candidate"),
            "measurement_only_blocked": sum(1 for row in signal_rows if row.get("decision_status") == "measurement_only_blocked"),
            "planned_not_loaded": sum(1 for row in signal_rows if row.get("decision_status") == "planned_not_loaded"),
        },
        "lockbox_policy": registry.get("lockbox_policy", {}),
        "artifact_count": len(artifacts),
        "missing_required_artifacts": [
            row["artifact_name"] for row in artifacts if row["required_flag"] and not row["exists_flag"]
        ],
        "artifacts": artifacts,
    }

    lockbox_json = output_dir / "semiconductor_lockbox_ledger.json"
    lockbox_csv = output_dir / "semiconductor_lockbox_ledger.csv"
    manifest_json = output_dir / "semiconductor_governance_manifest.json"
    snapshot_dir = output_dir / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_json = snapshot_dir / f"semiconductor_lockbox_ledger_{snapshot_stamp}.json"

    lockbox_json.write_text(json.dumps(lockbox, indent=2, sort_keys=True, default=str), encoding="utf-8")
    snapshot_json.write_text(json.dumps(lockbox, indent=2, sort_keys=True, default=str), encoding="utf-8")
    write_csv(lockbox_csv, artifacts)
    manifest = {
        "generated_at_utc": generated_at,
        "snapshot_id": lockbox["snapshot_id"],
        "database_path": str(db_path),
        "outputs": {
            "signal_registry_csv": str(signal_registry_csv),
            "signal_registry_json": str(signal_registry_json),
            "lockbox_ledger_csv": str(lockbox_csv),
            "lockbox_ledger_json": str(lockbox_json),
            "lockbox_snapshot_json": str(snapshot_json),
        },
        "missing_required_artifacts": lockbox["missing_required_artifacts"],
        "signal_registry_summary": lockbox["signal_registry_summary"],
    }
    manifest_json.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    return 1 if lockbox["missing_required_artifacts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
