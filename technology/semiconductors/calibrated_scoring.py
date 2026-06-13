from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from technology.core.config import cfg_get, load_yaml, resolve_path
from technology.core.db import connect, finish_run, init_db, start_run, utc_now
from technology.core.logging_utils import configure_utc_logging
from technology.core.scoring_features import (
    COMPONENT_FIELD_MAP,
    CORE_COMPONENT_DEFS,
    DEFAULT_OVERLAY_COMPONENTS,
    SUBFEATURE_SPECS,
    add_issue,
    cfg_ticker_set,
    overlay_component_defs,
    percentile_scores,
    qmarks,
    safe_float,
    upsert_component_defs,
    upsert_component_rows,
    upsert_scoring_input,
    weighted_available_score,
)
from technology.core.source_registry import load_source_registry, upsert_source_registry
from technology.core.text_norm import normalize_ticker


LOGGER = logging.getLogger("semiconductor_calibrated_scoring")
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "semiconductor_calibrated_scoring"
RUN_TYPE = "build_semiconductor_calibrated_scores"
VALIDATION_RUN_TYPE = "validate_semiconductor_calibrated_scores"

OUTPUT_FIELDS = [
    "ticker",
    "asof_date",
    "final_rank",
    "final_percentile",
    "final_score",
    "core_score",
    "sector_overlay_score",
    "data_quality_confidence",
    "rank_ready_flag",
    "calibration_eligible_flag",
    "model_status",
    "review_reason",
    "valuation_score",
    "quality_score",
    "risk_control_score",
    "positioning_score",
    "market_behavior_score",
    "growth_score",
    "sector_overlay_quality",
]


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Scoring as-of date. Defaults to latest baseline scoring row.")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def normalize_weights(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        weight = safe_float(value)
        if weight is None or weight < 0:
            continue
        out[str(key)] = weight
    total = sum(out.values())
    if total > 0:
        return {key: value / total for key, value in out.items()}
    return out


KNOWN_COMPONENT_NAMES = {component["component_name"] for component in CORE_COMPONENT_DEFS} | set(DEFAULT_OVERLAY_COMPONENTS)
KNOWN_SUBFEATURE_SCORE_KEYS = {score_key for _, score_key, _, _ in SUBFEATURE_SPECS}


def component_weight_specs(config: dict[str, Any]) -> dict[str, float]:
    defaults = {
        "valuation": 0.30,
        "quality": 0.25,
        "risk_control": 0.25,
        "positioning": 0.10,
        "market_behavior": 0.10,
        "growth": 0.00,
    }
    raw = cfg_get(config, f"{CONFIG_KEY}.component_weights", defaults)
    if isinstance(raw, dict):
        unknown = sorted(set(map(str, raw)) - KNOWN_COMPONENT_NAMES)
        if unknown:
            raise ValueError(f"Unknown component names in {CONFIG_KEY}.component_weights: {unknown}")
    configured = normalize_weights(raw)
    if configured:
        for component in defaults:
            configured.setdefault(component, 0.0)
        return configured
    return normalize_weights(defaults)


def subfeature_weight_specs(config: dict[str, Any]) -> dict[str, list[tuple[str, float]]]:
    raw = cfg_get(config, f"{CONFIG_KEY}.subfeature_weights", {})
    out: dict[str, list[tuple[str, float]]] = {}
    if not isinstance(raw, dict):
        return out
    unknown_components = sorted(set(map(str, raw)) - KNOWN_COMPONENT_NAMES)
    if unknown_components:
        raise ValueError(f"Unknown component names in {CONFIG_KEY}.subfeature_weights: {unknown_components}")
    for component, raw_weights in raw.items():
        if isinstance(raw_weights, dict):
            unknown_keys = sorted(set(map(str, raw_weights)) - KNOWN_SUBFEATURE_SCORE_KEYS)
            if unknown_keys:
                raise ValueError(f"Unknown subfeature score keys in {CONFIG_KEY}.subfeature_weights.{component}: {unknown_keys}")
        weights = normalize_weights(raw_weights)
        out[str(component)] = [(key, weight) for key, weight in weights.items() if weight > 0]
    return out


def load_registry_into_db(conn: Any, config: dict[str, Any], base_dir: Path) -> None:
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    upsert_source_registry(conn, load_source_registry(registry_path))


def load_universe_tickers(conn: Any, model_family: str) -> list[str]:
    return [
        normalize_ticker(row["ticker"])
        for row in conn.execute(
            """
            SELECT c.ticker
            FROM dim_company c
            JOIN dim_technology_taxonomy t
              ON t.ticker = c.ticker
             AND t.model_family = ?
            WHERE c.is_active = 1
            ORDER BY c.ticker
            """,
            (model_family,),
        ).fetchall()
        if normalize_ticker(row["ticker"])
    ]


def latest_baseline_asof(conn: Any, baseline_source_id: str, model_family: str) -> str:
    row = conn.execute(
        """
        SELECT MAX(asof_date) AS asof_date
        FROM feature_scoring_input
        WHERE source_id = ?
          AND model_family = ?
        """,
        (baseline_source_id, model_family),
    ).fetchone()
    return str(row["asof_date"] or "") if row is not None else ""


def load_baseline_rows(conn: Any, baseline_source_id: str, model_family: str, asof: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM feature_scoring_input
        WHERE source_id = ?
          AND model_family = ?
          AND asof_date = ?
        ORDER BY ticker
        """,
        (baseline_source_id, model_family, asof),
    ).fetchall()
    return [dict(row) for row in rows]


def apply_subfeature_percentiles(rows: list[dict[str, Any]]) -> None:
    for raw_key, score_key, higher_is_better, valid in SUBFEATURE_SPECS:
        scores = percentile_scores(rows, raw_key, higher_is_better=higher_is_better, valid=valid)
        for row in rows:
            row[score_key] = scores.get(str(row["ticker"]))


def recalibrate_components(
    rows: list[dict[str, Any]],
    *,
    component_weights: dict[str, float],
    subfeature_specs: dict[str, list[tuple[str, float]]],
    neutral_score: float,
) -> None:
    component_names = [component["component_name"] for component in CORE_COMPONENT_DEFS] + DEFAULT_OVERLAY_COMPONENTS
    for row in rows:
        row["_component_meta"] = {}
        for component_name in component_names:
            if component_name in subfeature_specs:
                specs = subfeature_specs[component_name]
                if not specs:
                    score, quality, available, missing, missing_detail = neutral_score, 0.0, 0, 0, "neutralized_by_calibration"
                    status = "neutralized"
                    default_applied = 1
                else:
                    score, quality, available, missing, missing_detail = weighted_available_score(
                        row,
                        specs,
                        neutral_score=neutral_score,
                    )
                    status = "complete" if quality >= 0.75 else "partial" if quality > 0 else "missing"
                    default_applied = 0 if quality > 0 else 1
                row["_component_meta"][component_name] = {
                    "component_score": score,
                    "component_quality": quality,
                    "component_status": status,
                    "available_subfeature_count": available,
                    "missing_subfeature_count": missing,
                    "default_applied": default_applied,
                    "review_reason": missing_detail if quality < 1.0 else "",
                }
            elif component_name in DEFAULT_OVERLAY_COMPONENTS:
                score = safe_float(row.get(f"{component_name}_score"))
                quality = safe_float(row.get("sector_overlay_quality")) if component_name in {"sector_cycle", "big_tech_capex"} else 0.0
                status = str(row.get("sector_overlay_status") or "not_loaded") if component_name in {"sector_cycle", "big_tech_capex"} else "not_loaded"
                row["_component_meta"][component_name] = {
                    "component_score": clamp(score if score is not None else neutral_score),
                    "component_quality": max(0.0, min(1.0, quality or 0.0)),
                    "component_status": status,
                    "available_subfeature_count": 1 if quality and quality > 0 else 0,
                    "missing_subfeature_count": 0 if quality and quality > 0 else 1,
                    "default_applied": 0 if quality and quality > 0 else 1,
                    "review_reason": "",
                }
            else:
                stage6_score_field, stage6_quality_field = COMPONENT_FIELD_MAP.get(
                    component_name, (f"{component_name}_score", f"{component_name}_component_quality")
                )
                score = safe_float(row.get(stage6_score_field))
                quality = safe_float(row.get(stage6_quality_field))
                row["_component_meta"][component_name] = {
                    "component_score": clamp(score if score is not None else neutral_score),
                    "component_quality": max(0.0, min(1.0, quality or 0.0)),
                    "component_status": "complete" if quality and quality >= 0.75 else "partial" if quality and quality > 0 else "missing",
                    "available_subfeature_count": 1 if quality and quality > 0 else 0,
                    "missing_subfeature_count": 0 if quality and quality > 0 else 1,
                    "default_applied": 0 if quality and quality > 0 else 1,
                    "review_reason": "",
                }
        for component_name in component_weights:
            meta = row["_component_meta"].get(component_name)
            if meta is None:
                continue
            score_field, quality_field = COMPONENT_FIELD_MAP.get(component_name, (f"{component_name}_score", f"{component_name}_component_quality"))
            row[score_field] = meta["component_score"]
            row[quality_field] = meta["component_quality"]


def compute_model_outputs(
    rows: list[dict[str, Any]],
    *,
    source_id: str,
    baseline_source_id: str,
    model_family: str,
    model_version: str,
    component_weights: dict[str, float],
    overlay_weight: float,
    min_core_confidence: float,
    max_missing_positive_component_weight: float,
    rank_ready_exempt: set[str],
    neutral_score: float,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    positive_component_weight = sum(weight for weight in component_weights.values() if weight > 0)
    overlay_weight = max(0.0, min(1.0, overlay_weight))
    for row in rows:
        ticker = str(row["ticker"])
        available_weight = 0.0
        weighted_score = 0.0
        weighted_quality = 0.0
        missing_weight = 0.0
        available_component_count = 0
        missing_component_count = 0
        component_scores: dict[str, float] = {}
        component_quality: dict[str, float] = {}
        missing_components: list[str] = []
        for component_name, weight in component_weights.items():
            meta = row["_component_meta"].get(component_name, {})
            score = safe_float(meta.get("component_score"))
            quality = safe_float(meta.get("component_quality")) or 0.0
            component_scores[component_name] = float(score if score is not None else neutral_score)
            component_quality[component_name] = quality
            if weight <= 0:
                continue
            if score is None or quality <= 0:
                missing_weight += weight
                missing_component_count += 1
                missing_components.append(component_name)
                continue
            available_weight += weight
            weighted_score += score * weight
            weighted_quality += quality * weight
            available_component_count += 1
        core_score = weighted_score / available_weight if available_weight > 0 else neutral_score
        data_quality = weighted_quality / positive_component_weight if positive_component_weight > 0 else 0.0
        sector_overlay_score = safe_float(row.get("sector_overlay_score")) or neutral_score
        sector_overlay_quality = safe_float(row.get("sector_overlay_quality")) or 0.0
        applied_overlay_weight = overlay_weight if sector_overlay_quality > 0 else 0.0
        final_score = core_score * (1.0 - applied_overlay_weight) + sector_overlay_score * applied_overlay_weight
        baseline_rank_ready = int(row.get("rank_ready_flag") or 0)
        baseline_calibration_eligible = int(row.get("calibration_eligible_flag") or 0)
        rank_ready = int(
            baseline_rank_ready == 1
            and data_quality >= min_core_confidence
            and missing_weight <= max_missing_positive_component_weight
        )
        status = "complete" if rank_ready else "review"
        reasons: list[str] = []
        if ticker in rank_ready_exempt:
            status = "review"
            rank_ready = 0
            reasons.append("rank_ready_exempt")
        if baseline_rank_ready != 1:
            reasons.append("baseline_not_rank_ready")
        if data_quality < min_core_confidence:
            reasons.append(f"low_core_quality={data_quality:.2f}")
        if missing_weight > max_missing_positive_component_weight:
            reasons.append(f"missing_component_weight={missing_weight:.2f}:{','.join(missing_components)}")
        row["core_available_component_count"] = available_component_count
        row["core_missing_component_count"] = missing_component_count
        outputs.append(
            {
                "ticker": ticker,
                "asof_date": row["asof_date"],
                "source_id": source_id,
                "model_family": model_family,
                "model_version": model_version,
                "baseline_source_id": baseline_source_id,
                "core_score": clamp(core_score),
                "sector_overlay_score": clamp(sector_overlay_score),
                "final_score": clamp(final_score),
                "component_weights_json": json.dumps(component_weights, sort_keys=True),
                "component_scores_json": json.dumps(component_scores, sort_keys=True),
                "component_quality_json": json.dumps(component_quality, sort_keys=True),
                "data_quality_confidence": max(0.0, min(1.0, data_quality)),
                "rank_ready_flag": rank_ready,
                "calibration_eligible_flag": int(baseline_calibration_eligible == 1 and rank_ready == 1),
                "model_status": status,
                "review_reason": ";".join(reasons),
                "_row": row,
            }
        )
    rankable = sorted(
        [output for output in outputs if int(output["rank_ready_flag"]) == 1],
        key=lambda output: (-float(output["final_score"]), str(output["ticker"])),
    )
    n = len(rankable)
    for idx, output in enumerate(rankable, start=1):
        output["final_rank"] = idx
        output["final_percentile"] = 100.0 * (n - idx + 0.5) / n if n else None
    for output in outputs:
        output.setdefault("final_rank", None)
        output.setdefault("final_percentile", None)
    return outputs


def upsert_model_output(conn: Any, output: dict[str, Any]) -> None:
    now = utc_now()
    fields = [
        "ticker",
        "asof_date",
        "source_id",
        "model_family",
        "model_version",
        "baseline_source_id",
        "core_score",
        "sector_overlay_score",
        "final_score",
        "final_rank",
        "final_percentile",
        "component_weights_json",
        "component_scores_json",
        "component_quality_json",
        "data_quality_confidence",
        "rank_ready_flag",
        "calibration_eligible_flag",
        "model_status",
        "review_reason",
    ]
    values = [output.get(field) for field in fields] + [now, now]
    update_clause = ",\n                ".join(f"{field} = excluded.{field}" for field in fields[4:])
    conn.execute(
        f"""
        INSERT INTO feature_scoring_model_output(
            {", ".join(fields)}, created_at, updated_at
        )
        VALUES ({", ".join("?" for _ in values)})
        ON CONFLICT(ticker, asof_date, source_id, model_family) DO UPDATE SET
            {update_clause},
            updated_at = excluded.updated_at
        """,
        values,
    )


def write_csv_report(path: Path, outputs: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for output in sorted(outputs, key=lambda item: (item["final_rank"] is None, item["final_rank"] or 10**9, item["ticker"])):
            row = dict(output)
            source_row = output["_row"]
            for field in ("valuation_score", "quality_score", "risk_control_score", "positioning_score", "market_behavior_score", "growth_score", "sector_overlay_quality"):
                row[field] = source_row.get(field)
            writer.writerow(row)


def build_semiconductor_calibrated_scores() -> None:
    configure_utc_logging()
    args = parse_args("Build Stage 7 calibrated semiconductor scores.")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    source_id = str(cfg_get(config, f"{CONFIG_KEY}.source_id", "semiconductor_calibrated_score_v1"))
    baseline_source_id = str(cfg_get(config, f"{CONFIG_KEY}.baseline_source_id", "semiconductor_scoring_contract"))
    model_version = str(cfg_get(config, f"{CONFIG_KEY}.model_version", "semiconductor_stage7_calibrated_v1"))
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_csv"), base_dir=base_dir)
    neutral_score = float(cfg_get(config, f"{CONFIG_KEY}.neutral_score", 50.0))
    overlay_weight = float(cfg_get(config, f"{CONFIG_KEY}.overlay_weight", 0.05))
    min_core_confidence = float(cfg_get(config, f"{CONFIG_KEY}.min_core_data_quality_confidence", 0.50))
    max_missing_weight = float(cfg_get(config, f"{CONFIG_KEY}.max_missing_positive_component_weight", 0.35))
    rank_ready_exempt = cfg_ticker_set(cfg_get(config, f"{CONFIG_KEY}.rank_ready_exempt_tickers", []))
    component_weights = component_weight_specs(config)
    subfeature_specs = subfeature_weight_specs(config)
    component_defs = CORE_COMPONENT_DEFS + overlay_component_defs(DEFAULT_OVERLAY_COMPONENTS)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry_into_db(conn, config, base_dir)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            asof_text = args.asof or latest_baseline_asof(conn, baseline_source_id, model_family)
            if not parse_date(asof_text):
                raise ValueError(f"No baseline scoring rows found for source_id={baseline_source_id} model_family={model_family}")
            rows = load_baseline_rows(conn, baseline_source_id, model_family, asof_text)
            if not rows:
                raise ValueError(f"No baseline scoring rows found for asof={asof_text}")
            universe = load_universe_tickers(conn, model_family)
            if len(rows) < len(universe):
                raise ValueError(
                    f"Baseline asof={asof_text} has only {len(rows)}/{len(universe)} universe rows; "
                    "it looks like a development subset build. Rebuild Stage 6A for the full universe "
                    "or pass --asof for a full baseline date."
                )
            apply_subfeature_percentiles(rows)
            recalibrate_components(
                rows,
                component_weights=component_weights,
                subfeature_specs=subfeature_specs,
                neutral_score=neutral_score,
            )
            outputs = compute_model_outputs(
                rows,
                source_id=source_id,
                baseline_source_id=baseline_source_id,
                model_family=model_family,
                model_version=model_version,
                component_weights=component_weights,
                overlay_weight=overlay_weight,
                min_core_confidence=min_core_confidence,
                max_missing_positive_component_weight=max_missing_weight,
                rank_ready_exempt=rank_ready_exempt,
                neutral_score=neutral_score,
            )
            with conn:
                conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", (RUN_TYPE,))
                upsert_component_defs(conn, model_family=model_family, component_defs=component_defs, neutral_score=neutral_score, overlay_default_quality=0.0)
                for output in outputs:
                    row = output["_row"]
                    row["source_id"] = source_id
                    row["scoring_contract_version"] = model_version
                    row["core_data_quality_confidence"] = output["data_quality_confidence"]
                    overlay_quality = float(safe_float(row.get("sector_overlay_quality")) or 0.0)
                    # Mirror final_score: the overlay weight only applies when the
                    # overlay actually carries quality, so no-overlay tickers are
                    # not penalized in confidence either.
                    applied_overlay_weight = overlay_weight if overlay_quality > 0 else 0.0
                    row["full_data_quality_confidence"] = max(
                        0.0,
                        min(1.0, float(output["data_quality_confidence"]) * (1.0 - applied_overlay_weight) + overlay_quality * applied_overlay_weight),
                    )
                    row["rank_ready_flag"] = output["rank_ready_flag"]
                    row["calibration_eligible_flag"] = output["calibration_eligible_flag"]
                    row["feature_status"] = output["model_status"]
                    row["review_reason"] = output["review_reason"]
                    upsert_scoring_input(conn, row, source_id=source_id, model_family=model_family, contract_version=model_version)
                    upsert_model_output(conn, output)
                    if str(output["model_status"]) != "complete":
                        add_issue(conn, row, source_id=source_id, stage=RUN_TYPE, detail=str(output["review_reason"] or "review"))
                # Stage 7 owns its overlay component rows (always re-derived from the
                # baseline), so the Stage 6B preservation skip must not apply here.
                upsert_component_rows(
                    conn,
                    rows,
                    source_id=source_id,
                    model_family=model_family,
                    component_defs=component_defs,
                    preserve_loaded_overlays=False,
                )
            write_csv_report(output_csv, outputs)
            rank_ready = sum(1 for output in outputs if int(output["rank_ready_flag"]) == 1)
            finish_run(conn, run_id=run_id, status="success", row_count=len(outputs), message=f"asof={asof_text} rows={len(outputs)} rank_ready={rank_ready}")
            LOGGER.info("Built Stage 7 calibrated scores: asof=%s rows=%d rank_ready=%d output=%s", asof_text, len(outputs), rank_ready, output_csv)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


def scalar(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def fetchone_value(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def validate_semiconductor_calibrated_scores() -> int:
    configure_utc_logging()
    args = parse_args("Validate Stage 7 calibrated semiconductor scores.")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    source_id = str(cfg_get(config, f"{CONFIG_KEY}.source_id", "semiconductor_calibrated_score_v1"))
    baseline_source_id = str(cfg_get(config, f"{CONFIG_KEY}.baseline_source_id", "semiconductor_scoring_contract"))
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, f"{CONFIG_KEY}.validation_output_csv"), base_dir=base_dir)
    rank_ready_exempt = cfg_ticker_set(cfg_get(config, f"{CONFIG_KEY}.rank_ready_exempt_tickers", []))
    component_weights = component_weight_specs(config)
    positive_components = [name for name, weight in component_weights.items() if weight > 0]
    errors: list[str] = []
    report_rows: list[dict[str, Any]] = []

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry_into_db(conn, config, base_dir)
        source_status = fetchone_value(conn, "SELECT status FROM source_registry WHERE source_id = ?", (source_id,))
        if source_status != "active":
            errors.append(f"Source {source_id} is not active: {source_status!r}")
        asof_text = args.asof or fetchone_value(
            conn,
            "SELECT MAX(asof_date) FROM feature_scoring_model_output WHERE source_id = ? AND model_family = ?",
            (source_id, model_family),
        )
        if not parse_date(asof_text):
            errors.append(f"No calibrated model output rows found for source_id={source_id}")
            asof_text = date.today().isoformat()
        universe = load_universe_tickers(conn, model_family)
        if not universe:
            errors.append(f"No active universe rows found for model_family={model_family}.")
            universe = ["__none__"]
        ph = qmarks(universe)
        feature_rows = scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM feature_scoring_input
            WHERE source_id = ? AND model_family = ? AND asof_date = ? AND ticker IN ({ph})
            """,
            (source_id, model_family, asof_text, *universe),
        )
        output_rows = scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM feature_scoring_model_output
            WHERE source_id = ? AND model_family = ? AND asof_date = ? AND ticker IN ({ph})
            """,
            (source_id, model_family, asof_text, *universe),
        )
        if feature_rows != len(universe):
            errors.append(f"Calibrated feature rows mismatch: {feature_rows}/{len(universe)}")
        if output_rows != len(universe):
            errors.append(f"Calibrated output rows mismatch: {output_rows}/{len(universe)}")
        rank_ready = scalar(
            conn,
            "SELECT COUNT(*) FROM feature_scoring_model_output WHERE source_id = ? AND model_family = ? AND asof_date = ? AND rank_ready_flag = 1",
            (source_id, model_family, asof_text),
        )
        # Stage 7 output rows share the baseline asof, so the expectation is
        # computed at the SAME asof being validated. Tickers Stage 7 demotes via
        # its own stricter gates (quality floor / missing-weight cap) are
        # explained demotions and only warn; anything else is an error.
        exempt_list = sorted(rank_ready_exempt) or ["__none__"]
        baseline_ready = scalar(
            conn,
            "SELECT COUNT(*) FROM feature_scoring_input WHERE source_id = ? AND model_family = ? AND asof_date = ? AND rank_ready_flag = 1 AND ticker NOT IN ({})".format(qmarks(exempt_list)),
            (baseline_source_id, model_family, asof_text, *exempt_list),
        )
        demoted_rows = conn.execute(
            f"""
            SELECT o.ticker, o.review_reason
            FROM feature_scoring_model_output o
            JOIN feature_scoring_input b
              ON b.ticker = o.ticker
             AND b.asof_date = o.asof_date
             AND b.model_family = o.model_family
             AND b.source_id = ?
            WHERE o.source_id = ? AND o.model_family = ? AND o.asof_date = ?
              AND o.rank_ready_flag = 0
              AND b.rank_ready_flag = 1
              AND o.ticker NOT IN ({qmarks(exempt_list)})
            ORDER BY o.ticker
            """,
            (baseline_source_id, source_id, model_family, asof_text, *exempt_list),
        ).fetchall()
        explained_demotions: list[str] = []
        for row in demoted_rows:
            reason = str(row["review_reason"] or "")
            if "low_core_quality" in reason or "missing_component_weight" in reason:
                explained_demotions.append(str(row["ticker"]))
            else:
                errors.append(f"Ticker demoted from baseline rank-ready without a Stage 7 gate reason: {row['ticker']} ({reason})")
        if explained_demotions:
            LOGGER.warning("Stage 7 gates demoted %d baseline rank-ready tickers: %s", len(explained_demotions), explained_demotions)
        expected_rank_ready = baseline_ready - len(explained_demotions)
        if rank_ready < max(1, expected_rank_ready):
            errors.append(f"Rank-ready output too low: {rank_ready}/{expected_rank_ready}")
        score_stats = conn.execute(
            """
            SELECT MIN(final_score) AS min_score,
                   MAX(final_score) AS max_score,
                   AVG(final_score) AS avg_score,
                   COUNT(DISTINCT final_rank) AS distinct_ranks
            FROM feature_scoring_model_output
            WHERE source_id = ? AND model_family = ? AND asof_date = ? AND rank_ready_flag = 1
            """,
            (source_id, model_family, asof_text),
        ).fetchone()
        if score_stats is None or float(score_stats["max_score"] or 0.0) - float(score_stats["min_score"] or 0.0) <= 0:
            errors.append("Calibrated final scores have no cross-sectional variance.")
        if int(score_stats["distinct_ranks"] or 0) != rank_ready:
            errors.append(f"Final rank count mismatch: distinct_ranks={score_stats['distinct_ranks']} rank_ready={rank_ready}")
        component_rows = conn.execute(
            """
            SELECT component_name,
                   COUNT(*) AS rows,
                   COUNT(DISTINCT ticker) AS tickers,
                   AVG(component_score) AS avg_score,
                   AVG(component_quality) AS avg_quality,
                   SUM(CASE WHEN component_quality <= 0 THEN 1 ELSE 0 END) AS zero_quality_rows
            FROM feature_scoring_component
            WHERE source_id = ? AND model_family = ? AND asof_date = ?
            GROUP BY component_name
            ORDER BY component_name
            """,
            (source_id, model_family, asof_text),
        ).fetchall()
        component_by_name = {str(row["component_name"]): dict(row) for row in component_rows}
        max_dead_pct = float(cfg_get(config, f"{CONFIG_KEY}.max_dead_core_component_pct", 0.20))
        for component in positive_components:
            row = component_by_name.get(component)
            if row is None or int(row["tickers"] or 0) != len(universe):
                errors.append(f"Positive-weight component {component} coverage invalid: {row}")
            elif int(row["zero_quality_rows"] or 0) > len(universe) * max_dead_pct:
                errors.append(f"Positive-weight component {component} has excessive zero-quality rows: {row['zero_quality_rows']}/{len(universe)}")
        if abs(sum(component_weights.values()) - 1.0) > 0.0001:
            errors.append(f"Component weights do not sum to 1.0: {sum(component_weights.values())}")
        growth_weight = float(component_weights.get("growth", 0.0))
        if growth_weight > 0.001:
            errors.append(f"Growth component should remain neutralized in v1, configured weight={growth_weight}")
        report_rows.append(
            {
                "asof_date": asof_text,
                "universe": len(universe),
                "feature_rows": feature_rows,
                "output_rows": output_rows,
                "rank_ready": rank_ready,
                "expected_rank_ready": expected_rank_ready,
                "min_score": score_stats["min_score"] if score_stats else "",
                "max_score": score_stats["max_score"] if score_stats else "",
                "avg_score": score_stats["avg_score"] if score_stats else "",
                "distinct_ranks": score_stats["distinct_ranks"] if score_stats else "",
                "positive_components": ",".join(positive_components),
                "errors": ";".join(errors),
            }
        )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report_rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(report_rows)
    for row in report_rows:
        LOGGER.info("Stage 7 validation summary: %s", row)
    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info("Stage 7 calibrated score validation passed.")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "validate":
        sys.argv.pop(1)
        raise SystemExit(validate_semiconductor_calibrated_scores())
    build_semiconductor_calibrated_scores()
