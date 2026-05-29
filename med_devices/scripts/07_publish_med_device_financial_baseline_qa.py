#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from pathlib import Path
from statistics import median, pstdev
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import DEFAULT_NEUTRAL_SCORE, cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("publish_med_device_financial_baseline_qa")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_KEY_METRICS = [
    "revenue_ttm",
    "gross_margin_ttm",
    "operating_margin_ttm",
    "fcf_margin_ttm",
    "rd_to_revenue_ttm",
    "market_cap",
    "enterprise_value",
    "ev_to_sales",
    "price_to_sales",
    "fcf_yield",
    "fundamental_quality_score_v1",
    "valuation_score_v1",
    "value_trap_score",
]
SUMMARY_FIELDS = ["asof_date", "section", "metric", "value", "detail"]
DELTA_FIELDS = [
    "asof_date",
    "ticker",
    "company_name",
    "previous_stage4_baseline_score",
    "current_stage4_baseline_score",
    "score_delta",
    "alert_flag",
]
RANKED_FIELDS = [
    "asof_date",
    "rank",
    "ticker",
    "company_name",
    "subsector",
    "calibration_bucket",
    "data_quality_status",
    "stage4_baseline_score",
    "fundamental_quality_score_v1",
    "valuation_score_v1",
    "value_trap_score",
    "data_confidence_score",
    "revenue_ttm",
    "revenue_yoy_growth",
    "gross_margin_ttm",
    "fcf_margin_ttm",
    "rd_to_revenue_ttm",
    "ev_to_sales",
    "price_to_sales",
    "fcf_yield",
    "missing_fields",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish Stage 4 med-device financial/valuation QA reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--ranked-csv", type=Path, default=None)
    parser.add_argument("--delta-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Feature as-of date. Defaults to latest available.")
    return parser.parse_args()


def parse_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def cfg_float(config: dict[str, Any], dotted_key: str, default: float) -> float:
    value = parse_float(cfg_get(config, dotted_key, default))
    if value is None:
        raise ValueError(f"Config value must be numeric: {dotted_key}")
    return value


def latest_feature_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM feature_financial_valuation").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    if not asof:
        raise ValueError("No feature_financial_valuation rows found; run script 06 first.")
    return asof


def load_feature_rows(conn: Any, *, asof: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM feature_financial_valuation
        WHERE asof_date = ?
        ORDER BY ticker
        """,
        (asof,),
    ).fetchall()
    return [dict(row) for row in rows]


def score_or_neutral(raw: Any, neutral_score: float) -> float:
    value = parse_float(raw)
    return value if value is not None else neutral_score


def baseline_score(
    row: dict[str, Any],
    *,
    fundamental_weight: float,
    valuation_weight: float,
    neutral_score: float,
) -> float:
    fundamental = score_or_neutral(row.get("fundamental_quality_score_v1"), neutral_score)
    valuation = score_or_neutral(row.get("valuation_score_v1"), neutral_score)
    total = max(1e-12, fundamental_weight + valuation_weight)
    return round((fundamental * fundamental_weight + valuation * valuation_weight) / total, 2)


def metric_values(rows: list[dict[str, Any]], metric: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        value = parse_float(row.get(metric))
        if value is not None:
            out.append(value)
    return out


def percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * pct
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def add_distribution_rows(
    out: list[dict[str, Any]],
    *,
    asof: str,
    section: str,
    metric: str,
    values: list[float],
) -> None:
    if not values:
        return
    ordered = sorted(values)

    def add(stat: str, value: float | None) -> None:
        if value is not None:
            out.append({"asof_date": asof, "section": section, "metric": f"{metric}_{stat}", "value": round(value, 6), "detail": ""})

    add("min", ordered[0])
    add("p25", percentile(ordered, 0.25))
    add("median", median(ordered))
    add("p75", percentile(ordered, 0.75))
    add("max", ordered[-1])
    add("stddev", pstdev(ordered) if len(ordered) > 1 else 0.0)


def summary_rows(
    rows: list[dict[str, Any]],
    *,
    asof: str,
    key_metrics: list[str],
    top_bottom_n: int,
    fundamental_weight: float,
    valuation_weight: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    total = len(rows)

    def add(section: str, metric: str, value: object, detail: str = "") -> None:
        out.append({"asof_date": asof, "section": section, "metric": metric, "value": value, "detail": detail})

    add("coverage", "feature_rows", total)
    add("coverage", "tickers", total)
    add("score_config", "stage4_fundamental_weight", fundamental_weight)
    add("score_config", "stage4_valuation_weight", valuation_weight)
    for field in ("data_quality_status", "calibration_bucket", "subsector"):
        counts: dict[str, int] = {}
        for row in rows:
            key = str(row.get(field) or "unknown")
            counts[key] = counts.get(key, 0) + 1
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            add(field, key, count, f"{count / total:.1%}" if total else "")

    for metric in key_metrics:
        values = metric_values(rows, metric)
        missing = total - len(values)
        add("missingness", metric, missing, f"{missing / total:.1%}" if total else "")
        add_distribution_rows(out, asof=asof, section="distribution", metric=metric, values=values)
        buckets = sorted({str(row.get("calibration_bucket") or "unknown") for row in rows})
        for bucket in buckets:
            bucket_rows = [row for row in rows if str(row.get("calibration_bucket") or "unknown") == bucket]
            add_distribution_rows(
                out,
                asof=asof,
                section=f"distribution_bucket_{bucket}",
                metric=metric,
                values=metric_values(bucket_rows, metric),
            )

    ranked = sorted(rows, key=lambda row: parse_float(row.get("stage4_baseline_score")) or 0.0, reverse=True)
    for label, selected in (("top", ranked[:top_bottom_n]), ("bottom", list(reversed(ranked[-top_bottom_n:])))):
        for idx, row in enumerate(selected, start=1):
            add(
                f"{label}_stage4",
                str(idx),
                row.get("ticker", ""),
                (
                    f"score={row.get('stage4_baseline_score')} "
                    f"fund={row.get('fundamental_quality_score_v1')} val={row.get('valuation_score_v1')}"
                ),
            )
    return out


def read_previous_ranked_scores(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    out: dict[str, float] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ticker = str(row.get("ticker") or "").strip().upper()
            score = parse_float(row.get("stage4_baseline_score"))
            if ticker and score is not None:
                out[ticker] = score
    return out


def delta_rows(rows: list[dict[str, Any]], previous_scores: dict[str, float], *, asof: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        current = parse_float(row.get("stage4_baseline_score"))
        previous = previous_scores.get(ticker)
        if not ticker or current is None or previous is None:
            continue
        delta = round(current - previous, 2)
        out.append(
            {
                "asof_date": asof,
                "ticker": ticker,
                "company_name": row.get("company_name", ""),
                "previous_stage4_baseline_score": previous,
                "current_stage4_baseline_score": current,
                "score_delta": delta,
                "alert_flag": 1 if abs(delta) >= 15.0 else 0,
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fieldnames} for row in rows])


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    summary_csv = (
        args.summary_csv.expanduser().resolve()
        if args.summary_csv
        else resolve_path(
            cfg_get(config, "financial_baseline_qa.summary_csv", "../output/med_devices_reports/med_device_financial_baseline_qa_summary.csv"),
            base_dir=base_dir,
        )
    )
    ranked_csv = (
        args.ranked_csv.expanduser().resolve()
        if args.ranked_csv
        else resolve_path(
            cfg_get(config, "financial_baseline_qa.ranked_csv", "../output/med_devices_reports/med_device_financial_baseline_qa_ranked.csv"),
            base_dir=base_dir,
        )
    )
    delta_csv = (
        args.delta_csv.expanduser().resolve()
        if args.delta_csv
        else resolve_path(
            cfg_get(config, "financial_baseline_qa.delta_csv", "../output/med_devices_reports/med_device_financial_baseline_qa_delta.csv"),
            base_dir=base_dir,
        )
    )
    key_metrics_raw = cfg_get(config, "financial_baseline_qa.key_metrics", DEFAULT_KEY_METRICS)
    key_metrics = [str(value).strip() for value in key_metrics_raw] if isinstance(key_metrics_raw, list) else list(DEFAULT_KEY_METRICS)
    top_bottom_n = max(1, int(cfg_get(config, "financial_baseline_qa.top_bottom_n", 20)))
    fundamental_weight = cfg_float(config, "financial_baseline_qa.baseline_score_weights.fundamental_quality", 0.55)
    valuation_weight = cfg_float(config, "financial_baseline_qa.baseline_score_weights.valuation", 0.45)
    neutral_score = cfg_float(config, "financial_features.neutral_component_score", DEFAULT_NEUTRAL_SCORE)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        asof = args.asof.strip() if args.asof else latest_feature_asof(conn)
        rows = load_feature_rows(conn, asof=asof)
        if not rows:
            raise ValueError(f"No feature_financial_valuation rows found for asof={asof}")
        run_id = start_run(conn, run_type="publish_med_device_financial_baseline_qa", input_path=config_path)
        try:
            for row in rows:
                row["stage4_baseline_score"] = baseline_score(
                    row,
                    fundamental_weight=fundamental_weight,
                    valuation_weight=valuation_weight,
                    neutral_score=neutral_score,
                )
            previous_scores = read_previous_ranked_scores(ranked_csv)
            ranked = sorted(rows, key=lambda row: parse_float(row.get("stage4_baseline_score")) or 0.0, reverse=True)
            for idx, row in enumerate(ranked, start=1):
                row["rank"] = idx
            summary = summary_rows(
                ranked,
                asof=asof,
                key_metrics=key_metrics,
                top_bottom_n=top_bottom_n,
                fundamental_weight=fundamental_weight,
                valuation_weight=valuation_weight,
            )
            deltas = delta_rows(ranked, previous_scores, asof=asof)
            write_csv(summary_csv, summary, SUMMARY_FIELDS)
            write_csv(ranked_csv, ranked, RANKED_FIELDS)
            write_csv(delta_csv, deltas, DELTA_FIELDS)
            alerts = sum(1 for row in deltas if int(row.get("alert_flag") or 0) == 1)
            message = f"asof={asof} rows={len(rows)} delta_alerts={alerts} summary={summary_csv} ranked={ranked_csv} delta={delta_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=message)
            LOGGER.info("Financial baseline QA complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
