#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from macro_probability_v2 import MODEL_VERSION_DEFAULT, PROBABILITY_V2_SPECS, binary_auc, calibration_line
from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_iso_date,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path
from macro_serving_storage import init_db

logger = logging.getLogger(__name__)

V2_TO_V1 = {
    "P_G_NOW_V2": "P_G_NOW",
    "P_G_LEAD_V2": "P_G_LEAD",
    "P_PI_NOW_V2": "P_PI_NOW",
    "P_PI_LEAD_V2": "P_PI_LEAD",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate whether v2 has earned promotion over v1.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--serving-db-path", type=Path, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--model-version", type=str, default=None)
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def _resolve_end(conn: sqlite3.Connection, *, model_version: str, override: str | None) -> str:
    parsed = parse_iso_date(override)
    if parsed is not None:
        return parsed.isoformat()
    row = conn.execute(
        "SELECT MAX(as_of_date) AS max_date FROM macro_regime_v2_decision_daily WHERE model_version = ?",
        (model_version,),
    ).fetchone()
    if row is None or not row["max_date"]:
        raise ValueError(f"No v2 decisions exist for model_version={model_version}.")
    return str(row["max_date"])


def _load_evaluation_frame(conn: sqlite3.Connection, *, model_version: str, end_date: str) -> pd.DataFrame:
    targets = pd.read_sql_query(
        """
        SELECT probability_key, predictor_as_of_date, label_value, label_available_date
        FROM macro_probability_v2_target
        WHERE model_version = ?
          AND predictor_complete_flag = 1
          AND label_value IS NOT NULL
          AND label_available_date IS NOT NULL
          AND label_available_date <= ?
        """,
        conn,
        params=[model_version, end_date],
    )
    v2 = pd.read_sql_query(
        """
        SELECT as_of_date, probability_key, probability_value AS v2_probability,
               positive_rate AS climatology_probability, coverage_flag AS v2_coverage
        FROM macro_probability_v2_daily
        WHERE model_version = ? AND as_of_date <= ?
        """,
        conn,
        params=[model_version, end_date],
    )
    v1 = pd.read_sql_query(
        """
        SELECT as_of_date, probability_key, probability_value AS v1_probability,
               coverage_flag AS v1_coverage
        FROM macro_probabilities_daily
        WHERE as_of_date <= ?
        """,
        conn,
        params=[end_date],
    )
    if targets.empty or v2.empty or v1.empty:
        raise ValueError("Promotion evidence requires non-empty target, v1, and v2 probability histories.")
    targets["v1_probability_key"] = targets["probability_key"].map(V2_TO_V1)
    joined = targets.merge(
        v2,
        left_on=["predictor_as_of_date", "probability_key"],
        right_on=["as_of_date", "probability_key"],
        how="left",
        validate="one_to_one",
    ).drop(columns=["as_of_date"])
    joined = joined.merge(
        v1,
        left_on=["predictor_as_of_date", "v1_probability_key"],
        right_on=["as_of_date", "probability_key"],
        how="left",
        validate="one_to_one",
        suffixes=("", "_v1"),
    ).drop(columns=["as_of_date", "probability_key_v1"])
    numeric_columns = (
        "label_value",
        "v2_probability",
        "climatology_probability",
        "v1_probability",
        "v2_coverage",
        "v1_coverage",
    )
    for column in numeric_columns:
        joined[column] = pd.to_numeric(joined[column], errors="coerce")
    return joined


def _metric_bundle(y: np.ndarray, probability: np.ndarray) -> tuple[float, float, float | None]:
    clipped = np.clip(probability, 1e-6, 1.0 - 1e-6)
    brier = float(np.mean((probability - y) ** 2))
    log_loss = float(-np.mean(y * np.log(clipped) + (1.0 - y) * np.log(1.0 - clipped)))
    return brier, log_loss, binary_auc(y, probability)


def _minimum_samples(layer_cfg: dict[str, Any], probability_key: str) -> int:
    evidence = cfg_get(layer_cfg, "evidence", default={}) or {}
    if probability_key.startswith("P_G_"):
        return int(cfg_get(evidence, "growth_min_oos_samples", default=24))
    if probability_key == "P_PI_LEAD_V2":
        return int(cfg_get(evidence, "inflation_lead_min_oos_samples", default=16))
    return int(cfg_get(evidence, "inflation_min_oos_samples", default=60))


def _cell_evidence(
    frame: pd.DataFrame,
    *,
    probability_key: str,
    layer_cfg: dict[str, Any],
) -> dict[str, Any]:
    subset = frame[
        frame["probability_key"].eq(probability_key)
        & frame["v1_coverage"].eq(1)
        & frame["v2_coverage"].eq(1)
        & frame[["label_value", "v1_probability", "v2_probability", "climatology_probability"]].notna().all(axis=1)
    ].copy()
    y = subset["label_value"].to_numpy(dtype=float)
    v1_probability = subset["v1_probability"].to_numpy(dtype=float)
    v2_probability = subset["v2_probability"].to_numpy(dtype=float)
    climatology = subset["climatology_probability"].to_numpy(dtype=float)
    sample_count = int(len(subset))
    positive_count = int(np.sum(y == 1.0))
    negative_count = sample_count - positive_count
    minimum_samples = _minimum_samples(layer_cfg, probability_key)
    evidence_cfg = cfg_get(layer_cfg, "evidence", default={}) or {}
    minimum_class_samples = int(cfg_get(evidence_cfg, "minimum_oos_class_samples", default=6))
    minimum_auc = float(cfg_get(evidence_cfg, "minimum_auc", default=0.52))
    minimum_brier_skill = float(cfg_get(evidence_cfg, "minimum_brier_skill", default=0.0))
    minimum_brier_improvement = float(cfg_get(evidence_cfg, "minimum_brier_improvement_vs_v1", default=0.0))
    minimum_calibration_slope = float(cfg_get(evidence_cfg, "minimum_calibration_slope", default=0.50))
    maximum_calibration_slope = float(cfg_get(evidence_cfg, "maximum_calibration_slope", default=1.50))

    if sample_count:
        v1_brier, _, v1_auc = _metric_bundle(y, v1_probability)
        v2_brier, _, v2_auc = _metric_bundle(y, v2_probability)
        climatology_brier = float(np.mean((climatology - y) ** 2))
        v2_brier_skill = None if climatology_brier <= 1e-12 else float(1.0 - v2_brier / climatology_brier)
        calibration_intercept, calibration_slope = calibration_line(y, v2_probability)
        improvement = float(v1_brier - v2_brier)
    else:
        v1_brier = v2_brier = v1_auc = v2_auc = v2_brier_skill = improvement = None
        calibration_intercept = calibration_slope = None

    failures: list[str] = []
    if sample_count < minimum_samples:
        failures.append(f"samples={sample_count}<{minimum_samples}")
    if positive_count < minimum_class_samples:
        failures.append(f"positives={positive_count}<{minimum_class_samples}")
    if negative_count < minimum_class_samples:
        failures.append(f"negatives={negative_count}<{minimum_class_samples}")
    if v2_auc is None or v2_auc < minimum_auc:
        failures.append(f"v2_auc={v2_auc!r}<{minimum_auc}")
    if v2_brier_skill is None or v2_brier_skill < minimum_brier_skill:
        failures.append(f"brier_skill={v2_brier_skill!r}<{minimum_brier_skill}")
    if improvement is None or improvement < minimum_brier_improvement:
        failures.append(f"brier_improvement={improvement!r}<{minimum_brier_improvement}")
    if calibration_slope is None or not minimum_calibration_slope <= calibration_slope <= maximum_calibration_slope:
        failures.append(
            f"calibration_slope={calibration_slope!r} not_in_[{minimum_calibration_slope},{maximum_calibration_slope}]"
        )
    return {
        "probability_key": probability_key,
        "common_oos_sample_count": sample_count,
        "positive_sample_count": positive_count,
        "negative_sample_count": negative_count,
        "v1_brier_score": v1_brier,
        "v2_brier_score": v2_brier,
        "v2_brier_skill_score": v2_brier_skill,
        "brier_improvement_vs_v1": improvement,
        "v1_auc": v1_auc,
        "v2_auc": v2_auc,
        "v2_calibration_intercept": calibration_intercept,
        "v2_calibration_slope": calibration_slope,
        "cell_status": "VALIDATED" if not failures else "REJECTED",
        "cell_reason": "ok" if not failures else ";".join(failures),
    }


def _decision_comparison(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    end_date: str,
    decision_cfg: dict[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame]:
    v1 = pd.read_sql_query(
        """
        SELECT as_of_date, active_current_regime AS v1_regime, regime_switch_flag AS v1_switch, coverage_flag AS v1_coverage
        FROM macro_regime_decision_daily WHERE as_of_date <= ?
        """,
        conn,
        params=[end_date],
    )
    v2 = pd.read_sql_query(
        """
        SELECT as_of_date, active_current_regime AS v2_regime, regime_switch_flag AS v2_switch,
               current_top_probability, current_confidence, coverage_flag AS v2_coverage
        FROM macro_regime_v2_decision_daily
        WHERE model_version = ? AND as_of_date <= ?
        """,
        conn,
        params=[model_version, end_date],
    )
    joined = v1.merge(v2, on="as_of_date", how="inner", validate="one_to_one")
    common = joined[joined["v1_coverage"].eq(1) & joined["v2_coverage"].eq(1)].copy()
    if common.empty:
        raise ValueError("No common covered decision dates exist between v1 and v2.")
    current_rows = common[common["as_of_date"].eq(end_date)]
    if len(current_rows) != 1:
        raise ValueError(f"V2 promotion requires one covered v1/v2 decision row on {end_date}; found {len(current_rows)}.")
    current = current_rows.iloc[0]
    minimum_top = float(cfg_get(decision_cfg, "min_top_probability", default=0.50))
    minimum_confidence = float(cfg_get(decision_cfg, "min_confidence", default=0.10))
    if not (
        pd.notna(current["current_top_probability"])
        and pd.notna(current["current_confidence"])
        and np.isfinite(float(current["current_top_probability"]))
        and np.isfinite(float(current["current_confidence"]))
    ):
        raise ValueError(f"V2 decision probabilities are non-finite on {end_date}.")
    confident = int(
        float(current["current_top_probability"]) >= minimum_top
        and float(current["current_confidence"]) >= minimum_confidence
    )
    summary = {
        "common_decision_day_count": int(len(common)),
        "regime_disagreement_fraction": float((common["v1_regime"] != common["v2_regime"]).mean()),
        "v1_switch_count": int(pd.to_numeric(common["v1_switch"], errors="coerce").fillna(0).sum()),
        "v2_switch_count": int(pd.to_numeric(common["v2_switch"], errors="coerce").fillna(0).sum()),
        "current_candidate_confident_flag": confident,
        "current_v1_regime": str(current["v1_regime"]),
        "current_v2_regime": str(current["v2_regime"]),
        "current_v2_top_probability": float(current["current_top_probability"]),
        "current_v2_confidence": float(current["current_confidence"]),
    }
    return summary, common


def _verify_upstream(output_dir: Path, *, model_version: str, end_date: str) -> None:
    probability_validation = output_dir / "macro_regime_v2_validation.json"
    decision_manifest = output_dir / "macro_regime_v2_decision_manifest.json"
    errors: list[str] = []
    for path in (probability_validation, decision_manifest):
        if not path.exists():
            errors.append(f"missing:{path.name}")
    if not errors:
        probability_payload = json.loads(probability_validation.read_text(encoding="utf-8"))
        decision_payload = json.loads(decision_manifest.read_text(encoding="utf-8"))
        if probability_payload.get("acceptance") != "PASS":
            errors.append("probability_validation_not_pass")
        if probability_payload.get("model_version") != model_version:
            errors.append("probability_model_version_mismatch")
        if probability_payload.get("validation_date") != end_date:
            errors.append("probability_date_mismatch")
        if decision_payload.get("model_version") != model_version:
            errors.append("decision_model_version_mismatch")
        if decision_payload.get("build_end_date") != end_date:
            errors.append("decision_date_mismatch")
        for filename, expected_hash in (decision_payload.get("files") or {}).items():
            path = output_dir / str(filename)
            if not path.exists() or _sha256_file(path) != str(expected_hash):
                errors.append(f"decision_artifact_hash_mismatch:{filename}")
    if errors:
        raise ValueError(f"V2 promotion upstream seal failed: {errors}")


def _write_database(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    end_date: str,
    cells: pd.DataFrame,
    summary: dict[str, Any],
    manifest_path: Path,
) -> None:
    now = utc_now_iso()
    cell_rows = [
        (
            model_version,
            end_date,
            row["probability_key"],
            int(row["common_oos_sample_count"]),
            int(row["positive_sample_count"]),
            int(row["negative_sample_count"]),
            row["v1_brier_score"],
            row["v2_brier_score"],
            row["v2_brier_skill_score"],
            row["brier_improvement_vs_v1"],
            row["v1_auc"],
            row["v2_auc"],
            row["v2_calibration_intercept"],
            row["v2_calibration_slope"],
            row["cell_status"],
            row["cell_reason"],
            now,
        )
        for _, row in cells.iterrows()
    ]
    summary_row = (
        model_version,
        end_date,
        summary["acceptance"],
        int(summary["validated_cell_count"]),
        int(summary["required_cell_count"]),
        int(summary["common_decision_day_count"]),
        float(summary["regime_disagreement_fraction"]),
        int(summary["v1_switch_count"]),
        int(summary["v2_switch_count"]),
        int(summary["current_candidate_confident_flag"]),
        str(manifest_path),
        now,
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM macro_regime_v2_promotion_evidence WHERE model_version = ? AND evidence_as_of_date = ?",
            (model_version, end_date),
        )
        conn.execute(
            "DELETE FROM macro_regime_v2_promotion_summary WHERE model_version = ? AND evidence_as_of_date = ?",
            (model_version, end_date),
        )
        conn.executemany(
            """
            INSERT INTO macro_regime_v2_promotion_evidence (
                model_version,
                evidence_as_of_date,
                probability_key,
                common_oos_sample_count,
                positive_sample_count,
                negative_sample_count,
                v1_brier_score,
                v2_brier_score,
                v2_brier_skill_score,
                brier_improvement_vs_v1,
                v1_auc,
                v2_auc,
                v2_calibration_intercept,
                v2_calibration_slope,
                cell_status,
                cell_reason,
                updated_at_utc
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            cell_rows,
        )
        conn.execute(
            """
            INSERT INTO macro_regime_v2_promotion_summary (
                model_version,
                evidence_as_of_date,
                acceptance,
                validated_cell_count,
                required_cell_count,
                common_decision_day_count,
                regime_disagreement_fraction,
                v1_switch_count,
                v2_switch_count,
                current_candidate_confident_flag,
                artifact_manifest_path,
                updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            summary_row,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = cfg_get(cfg, "probability_v2", default={}) or {}
    model_version = str(args.model_version or cfg_get(layer_cfg, "model_version", default=MODEL_VERSION_DEFAULT)).strip()
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    try:
        init_db(conn)
        end_date = _resolve_end(conn, model_version=model_version, override=args.end_date)
        output_root = resolve_path(
            config_path,
            str(cfg_get(layer_cfg, "output_dir", default="MacroLayer/out/regime_v2")),
        )
        if output_root is None:
            raise ValueError("Unable to resolve probability_v2.output_dir.")
        output_dir = output_root / end_date
        _verify_upstream(output_dir, model_version=model_version, end_date=end_date)
        evaluation = _load_evaluation_frame(conn, model_version=model_version, end_date=end_date)
        cell_rows = [
            _cell_evidence(evaluation, probability_key=spec.probability_key, layer_cfg=layer_cfg)
            for spec in PROBABILITY_V2_SPECS
        ]
        cells = pd.DataFrame(cell_rows)
        regime_cfg = cfg_get(cfg, "regime_layer", default={}) or {}
        decision_cfg = cfg_get(regime_cfg, "decision", default={}) or {}
        decision_summary, decision_comparison = _decision_comparison(
            conn,
            model_version=model_version,
            end_date=end_date,
            decision_cfg=decision_cfg,
        )
        validated_count = int(cells["cell_status"].eq("VALIDATED").sum())
        required_count = len(PROBABILITY_V2_SPECS)
        promotable = validated_count == required_count
        summary = {
            "acceptance": "PROMOTABLE" if promotable else "NOT_PROMOTABLE",
            "acceptance_reason": (
                "all_probability_cells_validated"
                if promotable
                else f"validated_probability_cells={validated_count}/{required_count}"
            ),
            "model_version": model_version,
            "evidence_as_of_date": end_date,
            "validated_cell_count": validated_count,
            "required_cell_count": required_count,
            **decision_summary,
            "created_at_utc": utc_now_iso(),
        }
        cells_path = output_dir / "macro_regime_v2_promotion_cells.csv"
        comparison_path = output_dir / "macro_regime_v2_decision_comparison.csv"
        summary_path = output_dir / "macro_regime_v2_promotion_summary.json"
        manifest_path = output_dir / "macro_regime_v2_promotion_manifest.json"
        _atomic_write_csv(cells_path, cells)
        _atomic_write_csv(comparison_path, decision_comparison)
        _atomic_write_text(summary_path, json.dumps(summary, indent=2) + "\n")
        manifest = {
            **summary,
            "config_sha256": _sha256_file(config_path),
            "builder_sha256": _sha256_file(Path(__file__)),
            "upstream_files": {
                "macro_regime_v2_validation.json": _sha256_file(
                    output_dir / "macro_regime_v2_validation.json"
                ),
                "macro_regime_v2_decision_manifest.json": _sha256_file(
                    output_dir / "macro_regime_v2_decision_manifest.json"
                ),
            },
            "files": {
                cells_path.name: _sha256_file(cells_path),
                comparison_path.name: _sha256_file(comparison_path),
                summary_path.name: _sha256_file(summary_path),
            },
        }
        _atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
        _write_database(
            conn,
            model_version=model_version,
            end_date=end_date,
            cells=cells,
            summary=summary,
            manifest_path=manifest_path,
        )
        for row in cell_rows:
            logger.info("[%s] %s -- %s", row["cell_status"], row["probability_key"], row["cell_reason"])
        logger.info(
            "MACRO V2 PROMOTION: %s validated=%d/%d current_confident=%d",
            summary["acceptance"],
            validated_count,
            required_count,
            int(summary["current_candidate_confident_flag"]),
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
