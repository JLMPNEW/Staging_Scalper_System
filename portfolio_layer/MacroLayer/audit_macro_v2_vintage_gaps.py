from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import logging
import os
import sqlite3
import uuid
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from macro_http import HttpClient, RateLimiter, RequestSettings
from macro_probability_v2 import MODEL_VERSION_DEFAULT, PROBABILITY_V2_SPECS, ProbabilityV2Spec
from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    getenv_str,
    load_macro_raw_config,
    parse_boolish,
    parse_iso_date,
    resolve_db_path,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path

logger = logging.getLogger(__name__)

V2_TO_V1 = {
    "P_G_NOW_V2": "P_G_NOW",
    "P_G_LEAD_V2": "P_G_LEAD",
    "P_PI_NOW_V2": "P_PI_NOW",
    "P_PI_LEAD_V2": "P_PI_LEAD",
}
INFLATION_METRICS = ("us_headline_cpi", "us_core_cpi", "us_headline_pce", "us_core_pce")
FRED_VINTAGE_URL = "https://api.stlouisfed.org/fred/series/vintagedates"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit historical PIT/vintage gaps that delay macro v2 promotion.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--raw-db-path", type=Path, default=None)
    parser.add_argument("--serving-db-path", type=Path, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--model-version", type=str, default=None)
    parser.add_argument("--preferred-history-start", type=str, default=None)
    parser.add_argument(
        "--probe-fred",
        action="store_true",
        help="Query FRED/ALFRED for the earliest provider vintage of locally deficient FRED series.",
    )
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


def _atomic_write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - max(0, int(months))
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def subtract_periods(value: date, periods: int, frequency: str) -> date:
    count = max(0, int(periods))
    normalized = str(frequency or "").strip().lower()
    if normalized in {"quarterly", "quarter", "q"}:
        return _subtract_months(value, count * 3)
    if normalized in {"monthly", "month", "m"}:
        return _subtract_months(value, count)
    if normalized in {"weekly", "week", "w"}:
        return value - timedelta(days=count * 7)
    if normalized in {"annual", "yearly", "year", "a", "y"}:
        return _subtract_months(value, count * 12)
    return value - timedelta(days=count)


def required_first_oos_date(eligible_dates: Iterable[date], required_samples: int) -> date | None:
    ordered = sorted(set(eligible_dates))
    required = int(required_samples)
    if required <= 0 or len(ordered) < required:
        return None
    return ordered[-required]


def recovery_status(
    *,
    required_start: date,
    local_earliest: date | None,
    source_name: str,
    probe_status: str,
    provider_earliest: date | None,
) -> str:
    if local_earliest is not None and local_earliest <= required_start:
        return "LOCAL_HISTORY_SUFFICIENT"
    if source_name != "fred_alfred":
        return "SOURCE_SPECIFIC_ARCHIVE_REVIEW"
    if probe_status == "NOT_RUN":
        return "PROVIDER_PROBE_REQUIRED"
    if probe_status != "PASS":
        return "PROVIDER_PROBE_FAILED"
    if provider_earliest is None:
        return "PROVIDER_HISTORY_UNAVAILABLE"
    if provider_earliest <= required_start:
        return "PROVIDER_BACKFILL_AVAILABLE"
    return "PROVIDER_ARCHIVE_STARTS_LATE"


def _resolve_end(conn: sqlite3.Connection, *, model_version: str, override: str | None) -> str:
    parsed = parse_iso_date(override)
    if parsed is not None:
        end_date = parsed.isoformat()
    else:
        row = conn.execute(
            """
            SELECT MAX(evidence_as_of_date) AS evidence_as_of_date
            FROM macro_regime_v2_promotion_summary
            WHERE model_version = ?
            """,
            (model_version,),
        ).fetchone()
        if row is None or not row["evidence_as_of_date"]:
            raise ValueError(f"No v2 promotion evidence exists for model_version={model_version}.")
        end_date = str(row["evidence_as_of_date"])
    row = conn.execute(
        """
        SELECT 1
        FROM macro_regime_v2_promotion_summary
        WHERE model_version = ? AND evidence_as_of_date = ?
        """,
        (model_version, end_date),
    ).fetchone()
    if row is None:
        raise ValueError(f"Run v2 promotion validation first; no evidence exists on {end_date}.")
    return end_date


def _minimum_oos_samples(spec: ProbabilityV2Spec, layer_cfg: dict[str, Any]) -> int:
    evidence = cfg_get(layer_cfg, "evidence", default={}) or {}
    if spec.target_kind == "growth":
        return int(cfg_get(evidence, "growth_min_oos_samples", default=24))
    if spec.target_horizon == "lead":
        return int(cfg_get(evidence, "inflation_lead_min_oos_samples", default=16))
    return int(cfg_get(evidence, "inflation_min_oos_samples", default=60))


def _minimum_training_samples(spec: ProbabilityV2Spec, layer_cfg: dict[str, Any]) -> int:
    if spec.target_kind == "growth":
        return int(cfg_get(layer_cfg, "growth_min_training_samples", default=40))
    if spec.target_horizon == "lead":
        return int(cfg_get(layer_cfg, "lead_min_training_samples", default=40))
    return int(cfg_get(layer_cfg, "inflation_min_training_samples", default=60))


def _promotion_evidence(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    end_date: str,
) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT *
        FROM macro_regime_v2_promotion_evidence
        WHERE model_version = ? AND evidence_as_of_date = ?
        """,
        (model_version, end_date),
    ).fetchall()
    result = {str(row["probability_key"]): row for row in rows}
    expected = {spec.probability_key for spec in PROBABILITY_V2_SPECS}
    if set(result) != expected:
        raise ValueError(f"Promotion evidence cells mismatch: actual={sorted(result)} expected={sorted(expected)}")
    return result


def _eligible_target_rows(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    spec: ProbabilityV2Spec,
    end_date: str,
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT t.predictor_as_of_date, t.label_available_date, t.label_value
            FROM macro_probability_v2_target t
            JOIN macro_probabilities_daily v1
              ON v1.as_of_date = t.predictor_as_of_date
             AND v1.probability_key = ?
            WHERE t.model_version = ?
              AND t.probability_key = ?
              AND t.predictor_complete_flag = 1
              AND t.label_value IS NOT NULL
              AND t.label_available_date IS NOT NULL
              AND t.label_available_date <= ?
              AND v1.coverage_flag = 1
            ORDER BY t.predictor_as_of_date
            """,
            (V2_TO_V1[spec.probability_key], model_version, spec.probability_key, end_date),
        ).fetchall()
    )


def _first_ready_date(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    probability_key: str,
    end_date: str,
) -> str:
    row = conn.execute(
        """
        SELECT MIN(calibration_as_of_date) AS first_ready
        FROM macro_probability_v2_model
        WHERE model_version = ? AND probability_key = ?
          AND calibration_as_of_date <= ? AND calibration_ready_flag = 1
        """,
        (model_version, probability_key, end_date),
    ).fetchone()
    return str(row["first_ready"] or "") if row is not None else ""


def _training_stats_before(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    probability_key: str,
    cutoff: date,
) -> tuple[int, int, int, date | None]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS sample_count,
               SUM(CASE WHEN label_value = 1 THEN 1 ELSE 0 END) AS positive_count,
               SUM(CASE WHEN label_value = 0 THEN 1 ELSE 0 END) AS negative_count,
               MIN(predictor_as_of_date) AS first_complete
        FROM macro_probability_v2_target
        WHERE model_version = ? AND probability_key = ?
          AND predictor_complete_flag = 1
          AND label_value IS NOT NULL AND label_available_date IS NOT NULL
          AND label_available_date <= ?
        """,
        (model_version, probability_key, cutoff.isoformat()),
    ).fetchone()
    if row is None:
        return 0, 0, 0, None
    return (
        int(row["sample_count"] or 0),
        int(row["positive_count"] or 0),
        int(row["negative_count"] or 0),
        parse_iso_date(row["first_complete"]),
    )


def _cell_rows(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    end_date: str,
    layer_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence_by_key = _promotion_evidence(conn, model_version=model_version, end_date=end_date)
    evidence_cfg = cfg_get(layer_cfg, "evidence", default={}) or {}
    min_oos_class = int(cfg_get(evidence_cfg, "minimum_oos_class_samples", default=6))
    min_train_positive = int(cfg_get(layer_cfg, "minimum_positive_samples", default=8))
    min_train_negative = int(cfg_get(layer_cfg, "minimum_negative_samples", default=8))
    rows: list[dict[str, Any]] = []
    for spec in PROBABILITY_V2_SPECS:
        evidence = evidence_by_key[spec.probability_key]
        required_oos = _minimum_oos_samples(spec, layer_cfg)
        targets = _eligible_target_rows(conn, model_version=model_version, spec=spec, end_date=end_date)
        target_dates = [date.fromisoformat(str(row["predictor_as_of_date"])) for row in targets]
        desired_first_oos = required_first_oos_date(target_dates, required_oos)
        min_train = _minimum_training_samples(spec, layer_cfg)
        if desired_first_oos is None:
            train_count = train_positive = train_negative = 0
            first_complete = None
        else:
            train_count, train_positive, train_negative, first_complete = _training_stats_before(
                conn,
                model_version=model_version,
                probability_key=spec.probability_key,
                cutoff=desired_first_oos,
            )
        train_sample_deficit = max(0, min_train - train_count)
        train_positive_deficit = max(0, min_train_positive - train_positive)
        train_negative_deficit = max(0, min_train_negative - train_negative)
        extra_periods = max(train_sample_deficit, train_positive_deficit + train_negative_deficit)
        estimated_history_start = (
            subtract_periods(first_complete, extra_periods, spec.training_frequency)
            if first_complete is not None
            else None
        )
        current_samples = int(evidence["common_oos_sample_count"] or 0)
        current_positive = int(evidence["positive_sample_count"] or 0)
        current_negative = int(evidence["negative_sample_count"] or 0)
        rows.append(
            {
                "model_version": model_version,
                "evidence_as_of_date": end_date,
                "probability_key": spec.probability_key,
                "target_kind": spec.target_kind,
                "target_horizon": spec.target_horizon,
                "training_frequency": spec.training_frequency,
                "current_first_ready_date": _first_ready_date(
                    conn,
                    model_version=model_version,
                    probability_key=spec.probability_key,
                    end_date=end_date,
                ),
                "current_oos_samples": current_samples,
                "required_oos_samples": required_oos,
                "oos_sample_deficit": max(0, required_oos - current_samples),
                "current_positive_samples": current_positive,
                "current_negative_samples": current_negative,
                "minimum_oos_class_samples": min_oos_class,
                "oos_positive_deficit": max(0, min_oos_class - current_positive),
                "oos_negative_deficit": max(0, min_oos_class - current_negative),
                "eligible_historical_target_count": len(target_dates),
                "required_first_oos_date": desired_first_oos.isoformat() if desired_first_oos else "",
                "minimum_training_samples": min_train,
                "training_samples_before_required_oos": train_count,
                "minimum_training_positive_samples": min_train_positive,
                "training_positive_before_required_oos": train_positive,
                "minimum_training_negative_samples": min_train_negative,
                "training_negative_before_required_oos": train_negative,
                "additional_complete_training_periods_needed": extra_periods,
                "current_first_complete_target_date": first_complete.isoformat() if first_complete else "",
                "estimated_complete_history_start_needed": (
                    estimated_history_start.isoformat() if estimated_history_start else ""
                ),
                "historical_sample_acceleration_possible": int(desired_first_oos is not None),
                "current_v2_auc": evidence["v2_auc"],
                "current_v2_brier_skill": evidence["v2_brier_skill_score"],
                "current_brier_improvement_vs_v1": evidence["brier_improvement_vs_v1"],
                "current_v2_calibration_slope": evidence["v2_calibration_slope"],
                "current_cell_status": str(evidence["cell_status"]),
                "current_cell_reason": str(evidence["cell_reason"]),
            }
        )
    return rows


def _policy_paths(config_path: Path, cfg: dict[str, Any]) -> dict[str, Path]:
    defaults = {
        "registry": "MacroLayer/macro_metric_registry_full.csv",
        "feature": "MacroLayer/macro_feature_policy.csv",
        "composite": "MacroLayer/macro_composite_policy.csv",
    }
    keys = {
        "registry": "registry_csv",
        "feature": "feature_policy_csv",
        "composite": "composite_policy_csv",
    }
    result: dict[str, Path] = {}
    for label, default in defaults.items():
        path = resolve_path(config_path, str(cfg_get(cfg, keys[label], default=default)))
        if path is None or not path.exists():
            raise FileNotFoundError(f"Missing {label} policy file: {path}")
        result[label] = path
    return result


def _dependency_map(
    composite_rows: list[dict[str, str]],
) -> dict[str, list[tuple[str, str, str]]]:
    required_by_composite: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row in composite_rows:
        if str(row.get("required_flag") or "").strip() not in {"1", "1.0", "true", "True"}:
            continue
        required_by_composite[str(row.get("composite_key") or "")].append(
            (
                str(row.get("metric_key") or ""),
                str(row.get("feature_name") or ""),
                "mandatory_composite_component",
            )
        )
    return required_by_composite


def _metric_dependencies(
    *,
    spec: ProbabilityV2Spec,
    required_by_composite: dict[str, list[tuple[str, str, str]]],
) -> list[tuple[str, str, str, str]]:
    dependencies: set[tuple[str, str, str, str]] = set()
    for predictor in spec.mandatory_predictors:
        if predictor in required_by_composite:
            for metric_key, feature_name, role in required_by_composite[predictor]:
                dependencies.add((metric_key, feature_name, role, predictor))
        elif predictor == "inflation_level_yoy":
            for metric_key in INFLATION_METRICS:
                dependencies.add((metric_key, "yoy_pct", "mandatory_predictor_group_3_of_4", predictor))
    if spec.target_kind == "growth":
        dependencies.add(("us_real_gdp", "qoq_ann_pct", "target_label_first_release", "real_gdp_qoq_ann"))
    else:
        for metric_key in INFLATION_METRICS:
            dependencies.add((metric_key, "yoy_pct", "target_label_component_4_of_4", "cpi_pce_4way_yoy"))
    return sorted(dependencies)


def _raw_spans(conn: sqlite3.Connection, metric_keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    keys = sorted({str(item) for item in metric_keys if str(item)})
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    registry_rows = conn.execute(
        f"""
        SELECT metric_key, registry_key
        FROM macro_metric_registry
        WHERE metric_key IN ({placeholders})
        """,
        keys,
    ).fetchall()
    registry_keys: dict[str, list[str]] = defaultdict(list)
    for row in registry_rows:
        registry_keys[str(row["metric_key"])].append(str(row["registry_key"]))

    latest_qa = conn.execute(
        """
        SELECT qa_run_id, as_of_date
        FROM macro_qa_run
        WHERE status = 'passed' AND completed_at_utc IS NOT NULL
        ORDER BY completed_at_utc DESC
        LIMIT 1
        """
    ).fetchone()
    qa_counts: dict[str, int] = defaultdict(int)
    qa_as_of = ""
    if latest_qa is not None:
        qa_as_of = str(latest_qa["as_of_date"] or "")
        count_rows = conn.execute(
            f"""
            SELECT metric_key, SUM(observation_count) AS observation_count
            FROM macro_metric_span_summary
            WHERE qa_run_id = ? AND metric_key IN ({placeholders})
            GROUP BY metric_key
            """,
            [str(latest_qa["qa_run_id"]), *keys],
        ).fetchall()
        for row in count_rows:
            qa_counts[str(row["metric_key"])] = int(row["observation_count"] or 0)

    def boundary(metric_key: str, column: str, direction: str) -> str:
        row = conn.execute(
            f"""
            SELECT {column}
            FROM macro_observation_raw
            WHERE metric_key = ? AND {column} IS NOT NULL AND TRIM({column}) <> ''
            ORDER BY {column} {direction}
            LIMIT 1
            """,
            (metric_key,),
        ).fetchone()
        return str(row[0]) if row is not None else ""

    spans: dict[str, dict[str, Any]] = {}
    for metric_key in keys:
        earliest_observation = boundary(metric_key, "observation_date", "ASC")
        latest_observation = boundary(metric_key, "observation_date", "DESC")
        earliest_release = boundary(metric_key, "release_date", "ASC")
        latest_release = boundary(metric_key, "release_date", "DESC")

        earliest_vintages: list[str] = []
        latest_vintages: list[str] = []
        for registry_key in registry_keys.get(metric_key, []):
            earliest = conn.execute(
                """
                SELECT vintage_date
                FROM macro_observation_raw
                WHERE registry_key = ? AND vintage_date IS NOT NULL AND TRIM(vintage_date) <> ''
                ORDER BY vintage_date ASC
                LIMIT 1
                """,
                (registry_key,),
            ).fetchone()
            latest = conn.execute(
                """
                SELECT vintage_date
                FROM macro_observation_raw
                WHERE registry_key = ? AND vintage_date IS NOT NULL AND TRIM(vintage_date) <> ''
                ORDER BY vintage_date DESC
                LIMIT 1
                """,
                (registry_key,),
            ).fetchone()
            if earliest is not None:
                earliest_vintages.append(str(earliest[0]))
            if latest is not None:
                latest_vintages.append(str(latest[0]))
        earliest_vintage = min(earliest_vintages, default="")
        latest_vintage = max(latest_vintages, default="")

        # Availability follows release first, then vintage, then observation date.
        # This is deliberately conservative for rows carrying old observation dates.
        earliest_effective = earliest_release or earliest_vintage or earliest_observation
        latest_effective = latest_release or latest_vintage or latest_observation
        spans[metric_key] = {
            "metric_key": metric_key,
            "earliest_observation": earliest_observation,
            "earliest_release": earliest_release,
            "earliest_vintage": earliest_vintage,
            "earliest_effective": earliest_effective,
            "latest_effective": latest_effective,
            "raw_rows": qa_counts.get(metric_key, 0),
            "raw_rows_as_of_date": qa_as_of if metric_key in qa_counts else "",
        }
    return spans


def _feature_spans(
    conn: sqlite3.Connection,
    metric_feature_pairs: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    pairs = sorted({(str(metric), str(feature)) for metric, feature in metric_feature_pairs if metric and feature})
    if not pairs:
        return {}
    metric_keys = sorted({metric for metric, _ in pairs})
    placeholders = ",".join("?" for _ in metric_keys)
    rows = conn.execute(
        f"""
        SELECT metric_key, feature_name,
               MIN(CASE WHEN coverage_flag = 1 AND transformed_value IS NOT NULL THEN as_of_date END)
                   AS earliest_transformed,
               MIN(CASE WHEN coverage_flag = 1 AND standardized_value IS NOT NULL THEN as_of_date END)
                   AS earliest_standardized,
               MAX(CASE WHEN coverage_flag = 1 AND transformed_value IS NOT NULL THEN as_of_date END)
                   AS latest_transformed
        FROM macro_feature_event
        WHERE metric_key IN ({placeholders})
        GROUP BY metric_key, feature_name
        """,
        metric_keys,
    ).fetchall()
    allowed = set(pairs)
    return {
        (str(row["metric_key"]), str(row["feature_name"])): dict(row)
        for row in rows
        if (str(row["metric_key"]), str(row["feature_name"])) in allowed
    }


def _provider_probe_client(cfg: dict[str, Any]) -> tuple[HttpClient | None, str | None]:
    source_cfg = cfg_get(cfg, "sources", "fred_alfred", default={}) or {}
    env_name = str(cfg_get(source_cfg, "api_key_env", default="FRED_API_KEY") or "FRED_API_KEY")
    api_key = getenv_str(env_name) or str(cfg_get(source_cfg, "api_key", default="") or "").strip()
    if not api_key:
        return None, None
    request_cfg = cfg_get(cfg, "request", default={}) or {}
    settings = RequestSettings(
        timeout_seconds=int(cfg_get(request_cfg, "timeout_seconds", default=60)),
        max_retries=int(cfg_get(request_cfg, "max_retries", default=3)),
        backoff_base_seconds=float(cfg_get(request_cfg, "backoff_base_seconds", default=1.0)),
        backoff_cap_seconds=float(cfg_get(request_cfg, "backoff_cap_seconds", default=30.0)),
        user_agent=str(cfg_get(request_cfg, "user_agent", default="macro-v2-vintage-audit/1.0")),
    )
    limiter = RateLimiter(float(cfg_get(source_cfg, "min_interval_seconds", default=1.0)))
    return HttpClient(settings, limiter=limiter), api_key


def _probe_fred_vintages(
    *,
    series_ids: Iterable[str],
    cfg: dict[str, Any],
) -> dict[str, tuple[str, str]]:
    client, api_key = _provider_probe_client(cfg)
    unique = sorted({str(item).strip() for item in series_ids if str(item).strip()})
    if client is None or api_key is None:
        return {series_id: ("MISSING_CREDENTIAL", "") for series_id in unique}
    result: dict[str, tuple[str, str]] = {}
    for series_id in unique:
        try:
            response = client.get(
                FRED_VINTAGE_URL,
                params={
                    "series_id": series_id,
                    "api_key": api_key,
                    "file_type": "json",
                    "realtime_start": "1776-07-04",
                    "realtime_end": "9999-12-31",
                    "sort_order": "asc",
                    "limit": "1",
                },
            )
            payload = response.json()
            dates = payload.get("vintage_dates") or []
            result[series_id] = ("PASS", str(dates[0]) if dates else "")
        except Exception as exc:  # noqa: BLE001 - provider errors are recorded without leaking request credentials
            logger.warning("FRED vintage probe failed for series_id=%s error=%s", series_id, type(exc).__name__)
            result[series_id] = (f"ERROR:{type(exc).__name__}", "")
    return result


def _input_rows(
    *,
    raw_conn: sqlite3.Connection,
    serving_conn: sqlite3.Connection,
    cell_rows: list[dict[str, Any]],
    registry_rows: list[dict[str, str]],
    feature_rows: list[dict[str, str]],
    composite_rows: list[dict[str, str]],
    preferred_history_start: date,
    cfg: dict[str, Any],
    probe_fred: bool,
) -> list[dict[str, Any]]:
    registry = {str(row.get("metric_key") or ""): row for row in registry_rows}
    feature_policy = {
        (str(row.get("metric_key") or ""), str(row.get("feature_name") or "")): row for row in feature_rows
    }
    required_by_composite = _dependency_map(composite_rows)
    pending: list[dict[str, Any]] = []
    for cell in cell_rows:
        spec = next(item for item in PROBABILITY_V2_SPECS if item.probability_key == cell["probability_key"])
        estimated = parse_iso_date(str(cell.get("estimated_complete_history_start_needed") or ""))
        complete_history_start = estimated or preferred_history_start
        for metric_key, feature_name, role, parent in _metric_dependencies(
            spec=spec,
            required_by_composite=required_by_composite,
        ):
            registry_row = registry.get(metric_key, {})
            feature_row = feature_policy.get((metric_key, feature_name), {})
            is_predictor = role.startswith("mandatory_")
            warmup_periods = 0
            if is_predictor:
                warmup_periods = max(0, int(float(feature_row.get("lookback_periods") or 0))) + max(
                    0, int(float(feature_row.get("min_history_periods") or 0))
                )
            frequency = str(feature_row.get("frequency") or registry_row.get("frequency") or "daily")
            required_raw_start = subtract_periods(complete_history_start, warmup_periods, frequency)
            preferred_raw_start = subtract_periods(preferred_history_start, warmup_periods, frequency)
            vintage_policy = str(registry_row.get("vintage_policy") or "")
            source_name = str(registry_row.get("source_name") or "")
            series_id = str(registry_row.get("source_series_id") or "")
            pending.append(
                {
                    "probability_key": spec.probability_key,
                    "target_kind": spec.target_kind,
                    "target_horizon": spec.target_horizon,
                    "metric_key": metric_key,
                    "feature_name": feature_name,
                    "dependency_role": role,
                    "parent_predictor_or_target": parent,
                    "source_name": source_name,
                    "source_series_id": series_id,
                    "frequency": frequency,
                    "vintage_policy": vintage_policy,
                    "registry_history_start_date": str(registry_row.get("history_start_date") or ""),
                    "feature_lookback_periods": int(float(feature_row.get("lookback_periods") or 0)),
                    "feature_min_history_periods": int(float(feature_row.get("min_history_periods") or 0)),
                    "required_complete_predictor_start": complete_history_start.isoformat(),
                    "preferred_complete_predictor_start": preferred_history_start.isoformat(),
                    "required_raw_history_start": required_raw_start.isoformat(),
                    "preferred_raw_history_start": preferred_raw_start.isoformat(),
                }
            )
    raw_spans = _raw_spans(raw_conn, (str(row["metric_key"]) for row in pending))
    serving_spans = _feature_spans(
        serving_conn,
        ((str(row["metric_key"]), str(row["feature_name"])) for row in pending),
    )
    fred_series_to_probe: set[str] = set()
    for row in pending:
        metric_key = str(row["metric_key"])
        feature_key = (metric_key, str(row["feature_name"]))
        raw_span = raw_spans.get(metric_key, {})
        serving_span = serving_spans.get(feature_key, {})
        row.update(
            {
                "local_earliest_observation": str(raw_span.get("earliest_observation") or ""),
                "local_earliest_release": str(raw_span.get("earliest_release") or ""),
                "local_earliest_vintage": str(raw_span.get("earliest_vintage") or ""),
                "local_earliest_effective": str(raw_span.get("earliest_effective") or ""),
                "local_latest_effective": str(raw_span.get("latest_effective") or ""),
                "local_raw_rows": int(raw_span.get("raw_rows") or 0),
                "local_raw_rows_as_of_date": str(raw_span.get("raw_rows_as_of_date") or ""),
                "serving_earliest_transformed": str(serving_span.get("earliest_transformed") or ""),
                "serving_earliest_standardized": str(serving_span.get("earliest_standardized") or ""),
                "serving_latest_transformed": str(serving_span.get("latest_transformed") or ""),
            }
        )
        local_text = (
            str(row["local_earliest_vintage"])
            if row["vintage_policy"] == "true_vintage"
            else str(row["local_earliest_effective"])
        )
        local_earliest = parse_iso_date(local_text)
        required_raw_start = date.fromisoformat(str(row["required_raw_history_start"]))
        row["local_gap_days"] = (
            max(0, (local_earliest - required_raw_start).days) if local_earliest is not None else ""
        )
        if local_earliest is None or local_earliest > required_raw_start:
            if row["source_name"] == "fred_alfred" and row["source_series_id"]:
                fred_series_to_probe.add(str(row["source_series_id"]))
    probe_results = _probe_fred_vintages(series_ids=fred_series_to_probe, cfg=cfg) if probe_fred else {}
    for row in pending:
        series_id = str(row["source_series_id"])
        probe_status, provider_earliest_text = probe_results.get(series_id, ("NOT_RUN", ""))
        required_start = date.fromisoformat(str(row["required_raw_history_start"]))
        local_text = (
            str(row["local_earliest_vintage"])
            if row["vintage_policy"] == "true_vintage"
            else str(row["local_earliest_effective"])
        )
        local_earliest = parse_iso_date(local_text)
        provider_earliest = parse_iso_date(provider_earliest_text)
        row["provider_probe_status"] = probe_status
        row["provider_earliest_vintage"] = provider_earliest_text
        row["provider_gap_days"] = (
            max(0, (provider_earliest - required_start).days) if provider_earliest is not None else ""
        )
        row["recovery_status"] = recovery_status(
            required_start=required_start,
            local_earliest=local_earliest,
            source_name=str(row["source_name"]),
            probe_status=probe_status,
            provider_earliest=provider_earliest,
        )
    return sorted(pending, key=lambda row: (str(row["probability_key"]), str(row["metric_key"]), str(row["dependency_role"])))


def _summary(
    *,
    model_version: str,
    end_date: str,
    preferred_history_start: date,
    probe_fred: bool,
    cell_rows: list[dict[str, Any]],
    input_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    statuses: dict[str, int] = defaultdict(int)
    for row in input_rows:
        statuses[str(row["recovery_status"])] += 1
    sample_recoverable = all(int(row["historical_sample_acceleration_possible"]) == 1 for row in cell_rows)
    if not sample_recoverable:
        assessment = "MORE_TARGET_HISTORY_REQUIRED"
    elif statuses.get("PROVIDER_ARCHIVE_STARTS_LATE", 0) or statuses.get("PROVIDER_HISTORY_UNAVAILABLE", 0):
        assessment = "V2_1_REDESIGN_REQUIRED"
    elif statuses.get("PROVIDER_PROBE_REQUIRED", 0):
        assessment = "PROVIDER_PROBE_REQUIRED"
    elif statuses.get("PROVIDER_PROBE_FAILED", 0):
        assessment = "PROVIDER_PROBE_FAILED"
    elif statuses.get("SOURCE_SPECIFIC_ARCHIVE_REVIEW", 0):
        assessment = "SOURCE_ARCHIVE_REVIEW_REQUIRED"
    else:
        assessment = "HISTORICAL_BACKFILL_CANDIDATE"
    return {
        "audit_status": "PASS",
        "model_version": model_version,
        "evidence_as_of_date": end_date,
        "preferred_history_start_date": preferred_history_start.isoformat(),
        "fred_provider_probe_enabled": probe_fred,
        "cell_count": len(cell_rows),
        "input_dependency_count": len(input_rows),
        "cells_with_historical_sample_path": sum(
            int(row["historical_sample_acceleration_possible"]) for row in cell_rows
        ),
        "recovery_status_counts": dict(sorted(statuses.items())),
        "promotion_acceleration_assessment": assessment,
        "interpretation": (
            "The audit only establishes whether valid historical evidence can be constructed. "
            "Promotion still requires every OOS quality and class-balance gate to pass."
        ),
        "created_at_utc": utc_now_iso(),
    }


CELL_FIELDS = [
    "model_version",
    "evidence_as_of_date",
    "probability_key",
    "target_kind",
    "target_horizon",
    "training_frequency",
    "current_first_ready_date",
    "current_oos_samples",
    "required_oos_samples",
    "oos_sample_deficit",
    "current_positive_samples",
    "current_negative_samples",
    "minimum_oos_class_samples",
    "oos_positive_deficit",
    "oos_negative_deficit",
    "eligible_historical_target_count",
    "required_first_oos_date",
    "minimum_training_samples",
    "training_samples_before_required_oos",
    "minimum_training_positive_samples",
    "training_positive_before_required_oos",
    "minimum_training_negative_samples",
    "training_negative_before_required_oos",
    "additional_complete_training_periods_needed",
    "current_first_complete_target_date",
    "estimated_complete_history_start_needed",
    "historical_sample_acceleration_possible",
    "current_v2_auc",
    "current_v2_brier_skill",
    "current_brier_improvement_vs_v1",
    "current_v2_calibration_slope",
    "current_cell_status",
    "current_cell_reason",
]

INPUT_FIELDS = [
    "probability_key",
    "target_kind",
    "target_horizon",
    "metric_key",
    "feature_name",
    "dependency_role",
    "parent_predictor_or_target",
    "source_name",
    "source_series_id",
    "frequency",
    "vintage_policy",
    "registry_history_start_date",
    "feature_lookback_periods",
    "feature_min_history_periods",
    "required_complete_predictor_start",
    "preferred_complete_predictor_start",
    "required_raw_history_start",
    "preferred_raw_history_start",
    "local_earliest_observation",
    "local_earliest_release",
    "local_earliest_vintage",
    "local_earliest_effective",
    "local_latest_effective",
    "local_raw_rows",
    "local_raw_rows_as_of_date",
    "serving_earliest_transformed",
    "serving_earliest_standardized",
    "serving_latest_transformed",
    "local_gap_days",
    "provider_probe_status",
    "provider_earliest_vintage",
    "provider_gap_days",
    "recovery_status",
]


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = cfg_get(cfg, "probability_v2", default={}) or {}
    audit_cfg = cfg_get(layer_cfg, "vintage_audit", default={}) or {}
    model_version = str(args.model_version or cfg_get(layer_cfg, "model_version", default=MODEL_VERSION_DEFAULT)).strip()
    raw_db_path = resolve_db_path(cfg, config_path, override=args.raw_db_path)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    preferred_history_start = parse_iso_date(
        args.preferred_history_start
        or str(cfg_get(audit_cfg, "preferred_history_start_date", default="2001-01-01"))
    )
    if preferred_history_start is None:
        raise ValueError("A valid probability_v2.vintage_audit.preferred_history_start_date is required.")
    probe_fred = bool(args.probe_fred) or parse_boolish(
        cfg_get(audit_cfg, "probe_fred_by_default", default=False),
        default=False,
    )
    policy_paths = _policy_paths(config_path, cfg)
    raw_conn = _connect_read_only(raw_db_path)
    serving_conn = _connect_read_only(serving_db_path)
    try:
        end_date = _resolve_end(serving_conn, model_version=model_version, override=args.end_date)
        cell_rows = _cell_rows(
            serving_conn,
            model_version=model_version,
            end_date=end_date,
            layer_cfg=layer_cfg,
        )
        input_rows = _input_rows(
            raw_conn=raw_conn,
            serving_conn=serving_conn,
            cell_rows=cell_rows,
            registry_rows=_read_csv(policy_paths["registry"]),
            feature_rows=_read_csv(policy_paths["feature"]),
            composite_rows=_read_csv(policy_paths["composite"]),
            preferred_history_start=preferred_history_start,
            cfg=cfg,
            probe_fred=probe_fred,
        )
    finally:
        raw_conn.close()
        serving_conn.close()

    summary = _summary(
        model_version=model_version,
        end_date=end_date,
        preferred_history_start=preferred_history_start,
        probe_fred=probe_fred,
        cell_rows=cell_rows,
        input_rows=input_rows,
    )
    output_root = resolve_path(config_path, str(cfg_get(layer_cfg, "output_dir", default="MacroLayer/out/regime_v2")))
    if output_root is None:
        raise ValueError("Unable to resolve probability_v2.output_dir.")
    output_dir = output_root / end_date
    cells_path = output_dir / "macro_v2_vintage_gap_cells.csv"
    inputs_path = output_dir / "macro_v2_vintage_gap_inputs.csv"
    summary_path = output_dir / "macro_v2_vintage_gap_summary.json"
    manifest_path = output_dir / "macro_v2_vintage_gap_manifest.json"
    _atomic_write_csv(cells_path, cell_rows, CELL_FIELDS)
    _atomic_write_csv(inputs_path, input_rows, INPUT_FIELDS)
    _atomic_write_text(summary_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    promotion_manifest = output_dir / "macro_regime_v2_promotion_manifest.json"
    manifest_inputs = {
        "config_macro_raw.yaml": config_path,
        "macro_metric_registry_full.csv": policy_paths["registry"],
        "macro_feature_policy.csv": policy_paths["feature"],
        "macro_composite_policy.csv": policy_paths["composite"],
        "audit_macro_v2_vintage_gaps.py": Path(__file__),
    }
    if promotion_manifest.exists():
        manifest_inputs[promotion_manifest.name] = promotion_manifest
    manifest = {
        **summary,
        "raw_db_path": str(raw_db_path),
        "serving_db_path": str(serving_db_path),
        "inputs_sha256": {name: _sha256_file(path) for name, path in manifest_inputs.items()},
        "files": {
            cells_path.name: _sha256_file(cells_path),
            inputs_path.name: _sha256_file(inputs_path),
            summary_path.name: _sha256_file(summary_path),
        },
    }
    _atomic_write_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    logger.info(
        "MACRO V2 VINTAGE AUDIT: PASS assessment=%s cells=%d dependencies=%d -> %s",
        summary["promotion_acceleration_assessment"],
        len(cell_rows),
        len(input_rows),
        manifest_path,
    )


if __name__ == "__main__":
    main()
