#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from pathlib import Path
from statistics import median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
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


def baseline_score(row: dict[str, Any], *, fundamental_weight: float, valuation_weight: float) -> float:
    fundamental = parse_float(row.get("fundamental_quality_score_v1")) or 0.0
    valuation = parse_float(row.get("valuation_score_v1")) or 0.0
    total = max(1e-12, fundamental_weight + valuation_weight)
    return round((fundamental * fundamental_weight + valuation * valuation_weight) / total, 2)


def metric_values(rows: list[dict[str, Any]], metric: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        value = parse_float(row.get(metric))
        if value is not None:
            out.append(value)
    return out


def summary_rows(rows: list[dict[str, Any]], *, asof: str, key_metrics: list[str], top_bottom_n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    total = len(rows)

    def add(section: str, metric: str, value: object, detail: str = "") -> None:
        out.append({"asof_date": asof, "section": section, "metric": metric, "value": value, "detail": detail})

    add("coverage", "feature_rows", total)
    add("coverage", "tickers", total)
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
        if not values:
            continue
        add("distribution", f"{metric}_min", round(min(values), 6))
        add("distribution", f"{metric}_median", round(median(values), 6))
        add("distribution", f"{metric}_max", round(max(values), 6))

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
    key_metrics_raw = cfg_get(config, "financial_baseline_qa.key_metrics", DEFAULT_KEY_METRICS)
    key_metrics = [str(value).strip() for value in key_metrics_raw] if isinstance(key_metrics_raw, list) else list(DEFAULT_KEY_METRICS)
    top_bottom_n = max(1, int(cfg_get(config, "financial_baseline_qa.top_bottom_n", 20)))
    fundamental_weight = cfg_float(config, "financial_baseline_qa.baseline_score_weights.fundamental_quality", 0.55)
    valuation_weight = cfg_float(config, "financial_baseline_qa.baseline_score_weights.valuation", 0.45)

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
                )
            ranked = sorted(rows, key=lambda row: parse_float(row.get("stage4_baseline_score")) or 0.0, reverse=True)
            for idx, row in enumerate(ranked, start=1):
                row["rank"] = idx
            summary = summary_rows(ranked, asof=asof, key_metrics=key_metrics, top_bottom_n=top_bottom_n)
            write_csv(summary_csv, summary, SUMMARY_FIELDS)
            write_csv(ranked_csv, ranked, RANKED_FIELDS)
            message = f"asof={asof} rows={len(rows)} summary={summary_csv} ranked={ranked_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=message)
            LOGGER.info("Financial baseline QA complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
