#!/usr/bin/env python3
"""Publish software-infrastructure LCR governance reports.

This stage is governance-only. It reads config, diagnostics, manifests, and the
latest score rows, then writes auditable report files without changing source
data, model scores, or production weights.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
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

from technology.core.calibrated_scoring import component_weight_specs, subfeature_weight_specs  # noqa: E402
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.scoring_features import SUBFEATURE_SPECS  # noqa: E402
from technology.software_infrastructure.calibrated_scoring import SETTINGS  # noqa: E402
from technology.software_infrastructure.optuna_calibration import write_csv  # noqa: E402


LOGGER = logging.getLogger("software_infrastructure_lcr_governance")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "software_infrastructure_governance_reports"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish software-infrastructure LCR governance reports.")
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
        return max(0, sum(1 for _ in csv.reader(handle)) - 1)


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
    raw = cfg_get(config, "software_infrastructure_optuna_calibration.subfeature_candidates", {}) or {}
    out: dict[str, set[str]] = {}
    if isinstance(raw, dict):
        for component, keys in raw.items():
            if isinstance(keys, list):
                out[str(component)] = {str(key) for key in keys}
    return out


def birthdate_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("signal") or ""): row for row in rows if row.get("signal")}


def stage8_weight_maps(stage8_weights: dict[str, Any]) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    component_weights = {
        str(key): float(value)
        for key, value in (stage8_weights.get("component_weights") or {}).items()
        if safe_float(value) is not None
    }
    subfeatures: dict[str, dict[str, float]] = {}
    for component, weights in (stage8_weights.get("subfeature_weights") or {}).items():
        if isinstance(weights, dict):
            subfeatures[str(component)] = {
                str(key): float(value)
                for key, value in weights.items()
                if safe_float(value) is not None
            }
    return component_weights, subfeatures


def weight_maps_from_config_key(config: dict[str, Any], config_key: str) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    raw_components = cfg_get(config, f"{config_key}.component_weights", {}) or {}
    raw_subfeatures = cfg_get(config, f"{config_key}.subfeature_weights", {}) or {}
    component_weights: dict[str, float] = {}
    if isinstance(raw_components, dict):
        component_weights = {
            str(key): float(value)
            for key, value in raw_components.items()
            if safe_float(value) is not None
        }
    subfeatures: dict[str, dict[str, float]] = {}
    if isinstance(raw_subfeatures, dict):
        for component, weights in raw_subfeatures.items():
            if isinstance(weights, dict):
                subfeatures[str(component)] = {
                    str(key): float(value)
                    for key, value in weights.items()
                    if safe_float(value) is not None
                }
    return component_weights, subfeatures


def decision_status(
    *,
    policy: str,
    production_effective_weight: float,
    challenger_effective_weight: float,
    stage8_effective_weight: float,
    keep_21: int,
    keep_63: int,
) -> str:
    policy = policy.strip().lower()
    if production_effective_weight > 0:
        return "production_locked"
    if policy.startswith("planned") or policy.startswith("optional"):
        return "planned_not_loaded"
    if policy == "measurement_only":
        return "measurement_only_review" if keep_21 or keep_63 else "measurement_only_blocked"
    if stage8_effective_weight > 0:
        return "stage8_research_candidate" if keep_21 or keep_63 else "stage8_candidate_weak_diagnostics"
    if challenger_effective_weight > 0:
        return "stage7_challenger_only"
    if policy.startswith("zero_weight"):
        return "zero_weight_locked"
    return "review"


def build_signal_registry_rows(
    *,
    registry: dict[str, Any],
    config: dict[str, Any],
    diagnostics_rows: list[dict[str, Any]],
    birthdate_rows: list[dict[str, Any]],
    stage8_weights: dict[str, Any],
) -> list[dict[str, Any]]:
    score_to_signal = score_to_signal_map()
    component_weights = component_weight_specs(config, SETTINGS)
    subfeature_weights = {
        component: {score_key: weight for score_key, weight in specs}
        for component, specs in subfeature_weight_specs(config, SETTINGS).items()
    }
    challenger_config_key = str(
        cfg_get(
            config,
            "software_infrastructure_governance_reports.stage7_challenger_config_key",
            "software_infrastructure_stage7_challenger_scoring",
        )
    )
    challenger_components, challenger_subfeatures = weight_maps_from_config_key(config, challenger_config_key)
    stage8_components, stage8_subfeatures = stage8_weight_maps(stage8_weights)
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
            production_subweight = float(subfeature_weights.get(component, {}).get(score_key, 0.0))
            production_component_weight = float(component_weights.get(component, 0.0))
            production_effective_weight = production_component_weight * production_subweight
            stage7_subweight = float(challenger_subfeatures.get(component, {}).get(score_key, 0.0))
            stage7_component_weight = float(challenger_components.get(component, 0.0))
            stage7_effective_weight = stage7_component_weight * stage7_subweight
            stage8_subweight = float(stage8_subfeatures.get(component, {}).get(score_key, 0.0))
            stage8_component_weight = float(stage8_components.get(component, 0.0))
            stage8_effective_weight = stage8_component_weight * stage8_subweight
            diag21, diag_group21 = fallback_diag(diag, signal, component, 21)
            diag63, diag_group63 = fallback_diag(diag, signal, component, 63)
            keep21 = safe_int((diag21 or {}).get("keep_candidate"))
            keep63 = safe_int((diag63 or {}).get("keep_candidate"))
            candidate_flag = int(score_key in stage8_candidates.get(component, set()))
            birth = birthdates.get(signal, {})
            policy = str(item.get("production_policy") or "")
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
                        production_effective_weight=production_effective_weight,
                        challenger_effective_weight=stage7_effective_weight,
                        stage8_effective_weight=stage8_effective_weight,
                        keep_21=keep21,
                        keep_63=keep63,
                    ),
                    "production_component_weight": production_component_weight,
                    "production_subfeature_weight": production_subweight,
                    "production_effective_weight": production_effective_weight,
                    "stage7_component_weight": stage7_component_weight,
                    "stage7_subfeature_weight": stage7_subweight,
                    "stage7_effective_weight": stage7_effective_weight,
                    "stage8_candidate_flag": candidate_flag,
                    "stage8_component_weight": stage8_component_weight,
                    "stage8_subfeature_weight": stage8_subweight,
                    "stage8_effective_weight": stage8_effective_weight,
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

    for item in registry.get("planned_signals", []) or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "registry_type": "planned_signal",
                "signal": item.get("signal", ""),
                "score_key": "",
                "component": "",
                "source_layer": item.get("source_layer", ""),
                "source_ids": "",
                "production_policy": item.get("production_policy", ""),
                "decision_status": "planned_not_loaded",
                "production_component_weight": "",
                "production_subfeature_weight": "",
                "production_effective_weight": "",
                "stage7_component_weight": "",
                "stage7_subfeature_weight": "",
                "stage7_effective_weight": "",
                "stage8_candidate_flag": 0,
                "stage8_component_weight": "",
                "stage8_subfeature_weight": "",
                "stage8_effective_weight": "",
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


def top_stage8_candidates(rows: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: safe_int(row.get("stage8_candidate_rank")) or 10**9)
    return ordered[:limit]


def matching_backtest(rows: list[dict[str, Any]], model_name: str, portfolio_name: str, weight_method: str, exposure_mode: str) -> dict[str, Any]:
    for row in rows:
        if (
            row.get("model_name") == model_name
            and row.get("portfolio_name") == portfolio_name
            and row.get("weight_method") == weight_method
            and row.get("exposure_mode") == exposure_mode
        ):
            return row
    return {}


def performance_delta(stage7: dict[str, Any], stage8: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in ("annualized_return", "sharpe", "max_drawdown", "avg_excess_return_vs_qqq", "avg_excess_return_vs_equal_weight"):
        left = safe_float(stage7.get(field))
        right = safe_float(stage8.get(field))
        out[f"{field}_delta"] = right - left if left is not None and right is not None else ""
    return out


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    registry_path = args.registry.expanduser().resolve() if args.registry else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.signal_registry_path", "software_infrastructure/data/software_infrastructure_signal_registry.yaml"),
        base_dir=base_dir,
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/software_infrastructure/governance"),
        base_dir=base_dir,
    )
    diagnostics_dir = resolve_path(cfg_get(config, "software_infrastructure_signal_diagnostics.output_dir"), base_dir=base_dir)
    scoring_dir = resolve_path("../output/technology_reports/software_infrastructure/scoring", base_dir=base_dir)
    optuna_dir = resolve_path(cfg_get(config, "software_infrastructure_optuna_calibration.output_dir"), base_dir=base_dir)
    backtest_dir = resolve_path(cfg_get(config, "software_infrastructure_portfolio_backtest.output_dir"), base_dir=base_dir)
    dashboard_dir = resolve_path(cfg_get(config, "software_infrastructure_dashboard_reports.output_dir"), base_dir=base_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    registry = load_yaml(registry_path)
    subfeature_rows = read_csv_rows(diagnostics_dir / "subfeature_ic.csv")
    birthdate_rows = read_csv_rows(diagnostics_dir / "signal_birthdates.csv")
    stage8_weights = read_json(optuna_dir / "stage8_best_weights.json")
    signal_rows = build_signal_registry_rows(
        registry=registry,
        config=config,
        diagnostics_rows=subfeature_rows,
        birthdate_rows=birthdate_rows,
        stage8_weights=stage8_weights,
    )

    signal_registry_csv = output_dir / "software_infrastructure_signal_registry.csv"
    signal_registry_json = output_dir / "software_infrastructure_signal_registry.json"
    write_csv(signal_registry_csv, signal_rows)
    signal_registry_json.write_text(
        json.dumps(
            {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "registry_version": registry.get("registry_version", ""),
                "model_family": registry.get("model_family", "software_infrastructure"),
                "rows": signal_rows,
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )

    stage7_source_id = str(cfg_get(config, "software_infrastructure_calibrated_scoring.source_id", "software_infrastructure_calibrated_score_v1"))
    model_family = str(cfg_get(config, "software_infrastructure_calibrated_scoring.model_family", "software_infrastructure"))
    with readonly_connect(db_path) as conn:
        stage7_summary = latest_stage7_summary(conn, stage7_source_id, model_family)
        top10_stage7 = top_ranked(conn, stage7_source_id, model_family, limit=10)

    walk_forward_summary = read_json(optuna_dir / "walk_forward" / "walk_forward_summary.json")
    stage8_candidate_scores = read_csv_rows(optuna_dir / "stage8_candidate_current_scores.csv")
    backtest_rows = read_csv_rows(backtest_dir / "software_infrastructure_portfolio_backtest_summary.csv")
    dashboard_manifest = read_json(dashboard_dir / "software_infrastructure_dashboard_manifest.json")
    risk_flags = read_csv_rows(dashboard_dir / "software_infrastructure_risk_flags.csv")
    production_model_name = str(cfg_get(config, "software_infrastructure_portfolio_backtest.production_model_name", "stage8_promoted_production_v1"))
    challenger_model_name = str(cfg_get(config, "software_infrastructure_portfolio_backtest.stage7_challenger_model_name", "stage7_challenger_v1"))
    challenger_scores_path = resolve_path(
        cfg_get(
            config,
            "software_infrastructure_stage7_challenger_scoring.output_csv",
            "../output/technology_reports/software_infrastructure/scoring/software_infrastructure_stage7_challenger_scores.csv",
        ),
        base_dir=config_path.parent,
    )
    challenger_validation_path = resolve_path(
        cfg_get(
            config,
            "software_infrastructure_stage7_challenger_scoring.validation_output_csv",
            "../output/technology_reports/software_infrastructure/scoring/software_infrastructure_stage7_challenger_validation.csv",
        ),
        base_dir=config_path.parent,
    )
    challenger_reference = matching_backtest(backtest_rows, challenger_model_name, "top_decile", "score_weight", "long_only")
    production_reference = matching_backtest(backtest_rows, production_model_name, "top_decile", "score_weight", "long_only")
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    artifacts = [
        artifact_row("technology_config", "input_config", config_path),
        artifact_row("signal_registry_source", "input_registry", registry_path),
        artifact_row("stage6_scoring_contract", "model_input", scoring_dir / "software_infrastructure_scoring_feature_contract.csv"),
        artifact_row("production_calibrated_scores", "production_scores", scoring_dir / "software_infrastructure_production_calibrated_scores.csv"),
        artifact_row("production_validation", "validation", scoring_dir / "software_infrastructure_production_validation.csv"),
        artifact_row("stage7_challenger_scores", "challenger_scores", challenger_scores_path),
        artifact_row("stage7_challenger_validation", "challenger_validation", challenger_validation_path),
        artifact_row("stage8a_summary", "diagnostics", diagnostics_dir / "stage8a_summary.json"),
        artifact_row("subfeature_ic", "diagnostics", diagnostics_dir / "subfeature_ic.csv"),
        artifact_row("component_ic", "diagnostics", diagnostics_dir / "component_ic.csv"),
        artifact_row("signal_birthdates", "diagnostics", diagnostics_dir / "signal_birthdates.csv"),
        artifact_row("stage8_best_weights", "research_calibration", optuna_dir / "stage8_best_weights.json"),
        artifact_row("stage8_best_summary", "research_calibration", optuna_dir / "stage8_best_summary.csv"),
        artifact_row("stage8_candidate_current_scores", "research_calibration", optuna_dir / "stage8_candidate_current_scores.csv"),
        artifact_row("walk_forward_summary", "research_calibration", optuna_dir / "walk_forward" / "walk_forward_summary.json"),
        artifact_row("portfolio_backtest_summary", "backtest", backtest_dir / "software_infrastructure_portfolio_backtest_summary.csv"),
        artifact_row("portfolio_backtest_manifest", "backtest", backtest_dir / "software_infrastructure_portfolio_backtest_manifest.json"),
        artifact_row("dashboard_manifest", "dashboard", dashboard_dir / "software_infrastructure_dashboard_manifest.json"),
        artifact_row("dashboard_final_rank_table", "dashboard", dashboard_dir / "software_infrastructure_final_rank_table.csv"),
        artifact_row("dashboard_stage8_candidate_rank_table", "dashboard", dashboard_dir / "software_infrastructure_stage8_candidate_rank_table.csv"),
        artifact_row("signal_registry_report", "governance", signal_registry_csv),
    ]

    production_model_status = str(cfg_get(config, f"{CONFIG_KEY}.production_model_status", "stage7_active"))
    configured_stage8_status = str(cfg_get(config, f"{CONFIG_KEY}.stage8_candidate_status", "manual_review_required"))
    manual_promotion_approved = int(bool(cfg_get(config, f"{CONFIG_KEY}.manual_promotion_approved", False)))
    stage8_is_current_production = int(
        production_model_status == "stage8_active"
        and configured_stage8_status == "promoted_to_production"
        and manual_promotion_approved == 1
    )
    latest_stage8_research_promotion_candidate = int(stage8_weights.get("promotion_candidate") or 0)
    latest_stage8_research_status = (
        "promotable_pending_manual_review"
        if latest_stage8_research_promotion_candidate
        else "report_only_not_promoted"
    )
    promotion_reason = (
        "Configured Stage 8 production model remains active from the recorded manual promotion. "
        "The latest Stage 8 research file is tracked separately and does not overwrite production state."
        if stage8_is_current_production
        else "Latest Stage 8 research candidate did not pass configured promotion gates; existing production model remains unchanged."
    )
    lockbox = {
        "generated_at_utc": generated_at,
        "snapshot_id": f"software_infrastructure_lockbox_{snapshot_stamp}",
        "database_path": str(db_path),
        "git_commit": git_commit(),
        "config_sha256": sha256_file(config_path),
        "registry_sha256": sha256_file(registry_path),
        "model_family": model_family,
        "production_source_id": stage7_source_id,
        "production_model_status": production_model_status,
        "production_model_name": production_model_name,
        "stage7_challenger_model_name": challenger_model_name,
        "stage7_challenger_status": cfg_get(config, f"{CONFIG_KEY}.stage7_challenger_status", "active_challenger"),
        "stage8_candidate_status": configured_stage8_status,
        "latest_stage8_research_candidate_status": latest_stage8_research_status,
        "automatic_promotion_applied": 0,
        "manual_promotion_approved": manual_promotion_approved,
        "promotion_effective_date": cfg_get(config, f"{CONFIG_KEY}.promotion_effective_date", ""),
        "recommended_stage8_use_case": cfg_get(config, f"{CONFIG_KEY}.recommended_stage8_use_case", "score_weighted_long_only"),
        "promotion_confidence": cfg_get(config, f"{CONFIG_KEY}.promotion_confidence", "moderate"),
        "promotion_decision": {
            "decision": "stage8_promoted_to_production" if stage8_is_current_production else "manual_review_required",
            "stage8_is_production": stage8_is_current_production,
            "latest_research_candidate_promoted": 0,
            "stage7_is_challenger": 1,
            "reason": promotion_reason,
        },
        "stage7_summary": stage7_summary,
        "top10_stage7_rank_ready": top10_stage7,
        "top10_stage8_candidate": top_stage8_candidates(stage8_candidate_scores, limit=10),
        "stage8_research_decision": {
            "promotion_candidate": stage8_weights.get("promotion_candidate", ""),
            "research_candidate_status": latest_stage8_research_status,
            "objective_improvement": stage8_weights.get("objective_improvement", ""),
            "fold_win_fraction": stage8_weights.get("fold_win_fraction", ""),
            "source_id": stage8_weights.get("source_id", ""),
            "component_weights": stage8_weights.get("component_weights", {}),
        },
        "walk_forward_decision": {
            "procedure_adds_value": walk_forward_summary.get("procedure_adds_value", ""),
            "refit_win_rate": walk_forward_summary.get("refit_win_rate", ""),
            "mean_objective_improvement": walk_forward_summary.get("mean_objective_improvement", ""),
            "improvement_paired_t": walk_forward_summary.get("improvement_paired_t", ""),
            "promotion_gate_pass_rate": walk_forward_summary.get("promotion_gate_pass_rate", ""),
        },
        "backtest_reference": {
            "stage7_challenger_top_decile_score_weight_long_only": challenger_reference,
            "stage8_production_top_decile_score_weight_long_only": production_reference,
            "production_minus_challenger": performance_delta(challenger_reference, production_reference),
        },
        "dashboard_manifest": dashboard_manifest,
        "risk_flag_summary": {
            "total_flags": len(risk_flags),
            "error_flags": sum(1 for row in risk_flags if row.get("severity") == "error"),
            "warning_flags": sum(1 for row in risk_flags if row.get("severity") == "warning"),
            "info_flags": sum(1 for row in risk_flags if row.get("severity") == "info"),
        },
        "signal_registry_summary": {
            "rows": len(signal_rows),
            "production_locked": sum(1 for row in signal_rows if row.get("decision_status") == "production_locked"),
            "stage8_candidate_flag_rows": sum(1 for row in signal_rows if safe_int(row.get("stage8_candidate_flag")) == 1),
            "stage8_positive_effective_weight_rows": sum(1 for row in signal_rows if (safe_float(row.get("stage8_effective_weight")) or 0.0) > 0),
            "stage8_research_candidate": sum(1 for row in signal_rows if row.get("decision_status") == "stage8_research_candidate"),
            "stage8_candidate_weak_diagnostics": sum(1 for row in signal_rows if row.get("decision_status") == "stage8_candidate_weak_diagnostics"),
            "zero_weight_locked": sum(1 for row in signal_rows if row.get("decision_status") == "zero_weight_locked"),
            "stage7_challenger_only": sum(1 for row in signal_rows if row.get("decision_status") == "stage7_challenger_only"),
            "review": sum(1 for row in signal_rows if row.get("decision_status") == "review"),
            "planned_not_loaded": sum(1 for row in signal_rows if row.get("decision_status") == "planned_not_loaded"),
        },
        "lockbox_policy": registry.get("lockbox_policy", {}),
        "artifact_count": len(artifacts),
        "missing_required_artifacts": [
            row["artifact_name"] for row in artifacts if row["required_flag"] and not row["exists_flag"]
        ],
        "artifacts": artifacts,
    }

    lockbox_json = output_dir / "software_infrastructure_lockbox_ledger.json"
    lockbox_csv = output_dir / "software_infrastructure_lockbox_ledger.csv"
    manifest_json = output_dir / "software_infrastructure_governance_manifest.json"
    snapshot_dir = output_dir / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_json = snapshot_dir / f"software_infrastructure_lockbox_ledger_{snapshot_stamp}.json"

    lockbox_json.write_text(json.dumps(lockbox, indent=2, sort_keys=True, default=str), encoding="utf-8")
    snapshot_json.write_text(json.dumps(lockbox, indent=2, sort_keys=True, default=str), encoding="utf-8")
    write_csv(lockbox_csv, artifacts)
    manifest = {
        "generated_at_utc": generated_at,
        "snapshot_id": lockbox["snapshot_id"],
        "database_path": str(db_path),
        "production_model_status": lockbox["production_model_status"],
        "production_model_name": lockbox["production_model_name"],
        "stage7_challenger_status": lockbox["stage7_challenger_status"],
        "stage7_challenger_model_name": lockbox["stage7_challenger_model_name"],
        "stage8_candidate_status": lockbox["stage8_candidate_status"],
        "latest_stage8_research_candidate_status": lockbox["latest_stage8_research_candidate_status"],
        "automatic_promotion_applied": 0,
        "manual_promotion_approved": lockbox["manual_promotion_approved"],
        "promotion_effective_date": lockbox["promotion_effective_date"],
        "outputs": {
            "signal_registry_csv": str(signal_registry_csv),
            "signal_registry_json": str(signal_registry_json),
            "lockbox_ledger_csv": str(lockbox_csv),
            "lockbox_ledger_json": str(lockbox_json),
            "lockbox_snapshot_json": str(snapshot_json),
        },
        "missing_required_artifacts": lockbox["missing_required_artifacts"],
        "signal_registry_summary": lockbox["signal_registry_summary"],
        "backtest_reference_delta": lockbox["backtest_reference"]["production_minus_challenger"],
    }
    manifest_json.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    LOGGER.info(
        "Published software-infrastructure LCR governance reports: missing_required=%d output=%s",
        len(lockbox["missing_required_artifacts"]),
        output_dir,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    return 1 if lockbox["missing_required_artifacts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
