#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from macro_probability_v2 import MODEL_VERSION_DEFAULT, PROBABILITY_V2_SPECS
from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_boolish,
    parse_iso_date,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path
from macro_serving_storage import init_db

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the shadow v2 independent-outcome macro calibration.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional validation date YYYY-MM-DD override.")
    parser.add_argument("--model-version", type=str, default=None, help="Optional model-version override.")
    return parser.parse_args()


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(rows: list[dict[str, str]], name: str, status: str, detail: str, *, hard: bool = True) -> None:
    rows.append({"gate": name, "status": status, "severity": "HARD" if hard else "WARN", "detail": detail})


def _finite_sequence(raw: str | None, expected_length: int) -> bool:
    if not raw:
        return False
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(values, list) and len(values) == expected_length and all(
        isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values
    )


def _json_list(raw: str | None) -> list[Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = cfg_get(cfg, "probability_v2", default={}) or {}
    model_version = str(args.model_version or cfg_get(layer_cfg, "model_version", default=MODEL_VERSION_DEFAULT)).strip()
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    rows: list[dict[str, str]] = []
    try:
        init_db(conn)
        requested_end = parse_iso_date(args.end_date)
        if requested_end is None:
            latest = conn.execute(
                "SELECT MAX(as_of_date) AS max_date FROM macro_probability_v2_daily WHERE model_version = ?",
                (model_version,),
            ).fetchone()
            if latest is None or not latest["max_date"]:
                raise ValueError(f"No v2 probability rows found for model_version={model_version}.")
            end_date = str(latest["max_date"])
        else:
            end_date = requested_end.isoformat()

        enabled = parse_boolish(cfg_get(layer_cfg, "enabled", default=None), default=False)
        shadow_only = parse_boolish(cfg_get(layer_cfg, "shadow_only", default=None), default=False)
        _record(
            rows,
            "shadow_only_isolation",
            "PASS" if enabled and shadow_only else "FAIL",
            f"model_version={model_version} enabled={enabled} shadow_only={shadow_only}",
        )

        expected_keys = {spec.probability_key for spec in PROBABILITY_V2_SPECS}
        target_sources = conn.execute(
            """
            SELECT DISTINCT probability_key, label_source
            FROM macro_probability_v2_target
            WHERE model_version = ?
            """,
            (model_version,),
        ).fetchall()
        source_by_key = {str(row["probability_key"]): str(row["label_source"]) for row in target_sources}
        source_ok = set(source_by_key) == expected_keys and all(
            source.startswith("us_real_gdp:first_release") or source.startswith("cpi_pce_4way:first_release")
            for source in source_by_key.values()
        )
        _record(
            rows,
            "independent_realized_targets",
            "PASS" if source_ok else "FAIL",
            json.dumps(source_by_key, sort_keys=True),
        )

        preknown_target_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM macro_probability_v2_target
                WHERE model_version = ?
                  AND label_available_date IS NOT NULL
                  AND label_available_date <= predictor_as_of_date
                """,
                (model_version,),
            ).fetchone()["n"]
        )
        _record(
            rows,
            "targets_unknown_at_prediction_time",
            "PASS" if preknown_target_count == 0 else "FAIL",
            f"targets_already_known_at_prediction={preknown_target_count}",
        )

        future_model_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM macro_probability_v2_model
                WHERE model_version = ?
                  AND max_label_available_date IS NOT NULL
                  AND max_label_available_date > calibration_as_of_date
                """,
                (model_version,),
            ).fetchone()["n"]
        )
        _record(
            rows,
            "pit_training_label_cutoff",
            "PASS" if future_model_count == 0 else "FAIL",
            f"models_with_future_labels={future_model_count}",
        )

        models = conn.execute(
            """
            SELECT *
            FROM macro_probability_v2_model
            WHERE model_version = ? AND calibration_as_of_date <= ?
            ORDER BY probability_key, calibration_as_of_date
            """,
            (model_version, end_date),
        ).fetchall()
        payload_failures: list[str] = []
        latest_ready: dict[str, sqlite3.Row] = {}
        for row in models:
            predictor_names = _json_list(row["predictor_names_json"])
            mandatory_predictors = _json_list(row["mandatory_predictors_json"])
            expected_length = len(predictor_names) if predictor_names is not None else 0
            std_values = _json_list(row["predictor_std_json"])
            valid = (
                expected_length > 0
                and mandatory_predictors is not None
                and set(mandatory_predictors).issubset(set(predictor_names or []))
                and _finite_sequence(row["predictor_mean_json"], expected_length)
                and _finite_sequence(row["predictor_std_json"], expected_length)
                and _finite_sequence(row["coefficients_json"], expected_length)
                and std_values is not None
                and all(float(value) > 0.0 for value in std_values)
                and row["intercept_value"] is not None
                and math.isfinite(float(row["intercept_value"]))
            )
            if not valid:
                payload_failures.append(f"{row['probability_key']}@{row['calibration_as_of_date']}")
            if int(row["calibration_ready_flag"]) == 1:
                latest_ready[str(row["probability_key"])] = row
        payload_ok = not payload_failures and set(latest_ready) == expected_keys
        _record(
            rows,
            "finite_model_payloads",
            "PASS" if payload_ok else "FAIL",
            f"models={len(models)} latest_ready={sorted(latest_ready)} failures={payload_failures[:10]}",
        )

        current_rows = conn.execute(
            """
            SELECT probability_key, probability_value, calibration_as_of_date, training_sample_count,
                   predictor_coverage_ratio, coverage_flag
            FROM macro_probability_v2_daily
            WHERE model_version = ? AND as_of_date = ?
            ORDER BY probability_key
            """,
            (model_version, end_date),
        ).fetchall()
        current_by_key = {str(row["probability_key"]): row for row in current_rows}
        current_ok = set(current_by_key) == expected_keys
        for row in current_rows:
            probability = row["probability_value"]
            current_ok = current_ok and (
                int(row["coverage_flag"]) == 1
                and probability is not None
                and math.isfinite(float(probability))
                and 0.0 < float(probability) < 1.0
                and str(row["calibration_as_of_date"]) <= end_date
            )
        _record(
            rows,
            "latest_probability_contract",
            "PASS" if current_ok else "FAIL",
            json.dumps(
                {
                    key: {
                        "probability": row["probability_value"],
                        "calibration_as_of": row["calibration_as_of_date"],
                        "training_samples": row["training_sample_count"],
                        "coverage": row["coverage_flag"],
                    }
                    for key, row in current_by_key.items()
                },
                sort_keys=True,
            ),
        )

        regime = conn.execute(
            "SELECT * FROM macro_regime_v2_daily WHERE model_version = ? AND as_of_date = ?",
            (model_version, end_date),
        ).fetchone()
        regime_ok = regime is not None and int(regime["coverage_flag"]) == 1 and int(regime["shadow_only_flag"]) == 1
        if regime is not None:
            current_sum = sum(
                float(regime[name])
                for name in (
                    "p_current_expansion_disinflation",
                    "p_current_heating_up",
                    "p_current_slow_growth",
                    "p_current_stagflation",
                )
                if regime[name] is not None
            )
            next_sum = sum(
                float(regime[name])
                for name in (
                    "p_next_expansion_disinflation",
                    "p_next_heating_up",
                    "p_next_slow_growth",
                    "p_next_stagflation",
                )
                if regime[name] is not None
            )
            regime_ok = regime_ok and abs(current_sum - 1.0) <= 1e-8 and abs(next_sum - 1.0) <= 1e-8
            regime_detail = (
                f"current={regime['current_regime']} next={regime['next_regime']} "
                f"current_sum={current_sum:.12f} next_sum={next_sum:.12f} energy_shock={regime['energy_shock_flag']}"
            )
        else:
            regime_detail = "missing regime row"
        _record(rows, "regime_probability_integrity", "PASS" if regime_ok else "FAIL", regime_detail)
        minimum_top_probability = float(cfg_get(layer_cfg, "decision_min_top_probability", default=0.50))
        minimum_confidence = float(cfg_get(layer_cfg, "decision_min_confidence", default=0.10))
        decision_confident = bool(
            regime is not None
            and regime["current_regime_probability"] is not None
            and regime["current_regime_confidence"] is not None
            and float(regime["current_regime_probability"]) >= minimum_top_probability
            and float(regime["current_regime_confidence"]) >= minimum_confidence
        )
        _record(
            rows,
            "current_regime_confidence",
            "PASS" if decision_confident else "WARN",
            (
                "missing regime"
                if regime is None
                else f"top={regime['current_regime_probability']} confidence={regime['current_regime_confidence']} "
                f"minimum_top={minimum_top_probability} minimum_confidence={minimum_confidence}"
            ),
            hard=False,
        )

        diagnostics = conn.execute(
            """
            SELECT *
            FROM macro_probability_v2_diagnostics
            WHERE model_version = ? AND diagnostic_as_of_date = ?
            ORDER BY probability_key
            """,
            (model_version, end_date),
        ).fetchall()
        diagnostic_keys = {str(row["probability_key"]) for row in diagnostics}
        diagnostics_ok = diagnostic_keys == expected_keys and all(
            int(row["oos_sample_count"]) == int(row["positive_sample_count"]) + int(row["negative_sample_count"])
            and str(row["evidence_status"]) in {"VALIDATED_SHADOW", "NOT_VALIDATED", "INSUFFICIENT_DATA"}
            and (
                row["oos_brier_score"] is None
                or (math.isfinite(float(row["oos_brier_score"])) and 0.0 <= float(row["oos_brier_score"]) <= 1.0)
            )
            for row in diagnostics
        )
        _record(
            rows,
            "oos_diagnostics_complete",
            "PASS" if diagnostics_ok else "FAIL",
            f"keys={sorted(diagnostic_keys)} expected={sorted(expected_keys)}",
        )
        evidence = {
            str(row["probability_key"]): {
                "status": row["evidence_status"],
                "oos_samples": row["oos_sample_count"],
                "auc": row["oos_auc"],
                "brier_skill": row["brier_skill_score"],
            }
            for row in diagnostics
        }
        all_validated = diagnostics_ok and all(item["status"] == "VALIDATED_SHADOW" for item in evidence.values())
        _record(
            rows,
            "promotion_evidence",
            "PASS" if all_validated else "WARN",
            json.dumps(evidence, sort_keys=True),
            hard=False,
        )

        output_root = resolve_path(
            config_path,
            str(cfg_get(layer_cfg, "output_dir", default="MacroLayer/out/regime_v2")),
        )
        if output_root is None:
            raise ValueError("Unable to resolve probability_v2.output_dir.")
        output_dir = output_root / end_date
        manifest_path = output_dir / "macro_regime_v2_manifest.json"
        provenance_errors: list[str] = []
        if not manifest_path.exists():
            provenance_errors.append("missing_manifest")
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                provenance_errors.append(f"invalid_manifest:{type(exc).__name__}")
                manifest = {}
            if manifest.get("model_version") != model_version:
                provenance_errors.append("model_version_mismatch")
            if manifest.get("build_end_date") != end_date:
                provenance_errors.append("build_end_date_mismatch")
            if manifest.get("config_sha256") != _sha256_file(config_path):
                provenance_errors.append("config_hash_mismatch")
            builder_path = Path(__file__).resolve().parent / "build_macro_probabilities_v2.py"
            if manifest.get("builder_sha256") != _sha256_file(builder_path):
                provenance_errors.append("builder_hash_mismatch")
            manifest_files = manifest.get("files")
            if not isinstance(manifest_files, dict) or not manifest_files:
                provenance_errors.append("missing_file_hashes")
            else:
                for filename, expected_hash in manifest_files.items():
                    artifact_path = output_dir / str(filename)
                    if not artifact_path.exists():
                        provenance_errors.append(f"missing_file:{filename}")
                    elif _sha256_file(artifact_path) != str(expected_hash):
                        provenance_errors.append(f"file_hash_mismatch:{filename}")
        _record(
            rows,
            "sealed_artifact_provenance",
            "PASS" if not provenance_errors else "FAIL",
            f"manifest={manifest_path} errors={provenance_errors}",
        )

        validation = pd.DataFrame(rows)
        hard_failed = bool(((validation["severity"] == "HARD") & (validation["status"] != "PASS")).any())
        _atomic_write_csv(output_dir / "macro_regime_v2_validation.csv", validation)
        summary = {
            "acceptance": "FAIL" if hard_failed else "PASS",
            "model_version": model_version,
            "validation_date": end_date,
            "shadow_only": shadow_only,
            "evidence": evidence,
            "created_at_utc": utc_now_iso(),
        }
        _atomic_write_text(output_dir / "macro_regime_v2_validation.json", json.dumps(summary, indent=2) + "\n")
        for row in rows:
            logger.info("[%s] %s -- %s", row["status"], row["gate"], row["detail"])
        logger.info("MACRO V2 ACCEPTANCE: %s", summary["acceptance"])
        if hard_failed:
            raise SystemExit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
