#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "asof_date",
    "company_id",
    "ticker",
    "institutional_ownership_delta_pct",
    "institutional_accumulation_score",
    "institutional_crowding_score",
    "institutional_breadth_score",
    "institutional_manager_count",
    "institutional_share_count",
    "institutional_market_value_usd",
    "data_quality_score",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device institutional-flow shadow features from 13F facts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def linear_score(value: float, points: list[tuple[float, float]]) -> float:
    points = sorted(points)
    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= value <= x1:
            return y0 + (y1 - y0) * (value - x0) / (x1 - x0) if x1 != x0 else y1
    return points[-1][1]


def normalize_pct_decimal(raw: object, default: float = 0.0) -> float:
    value = to_float(raw)
    if value is None:
        return default
    if abs(value) > 1.0:
        value /= 100.0
    return value


def parse_list(raw: object) -> set[str]:
    return {str(part).strip() for part in str(raw or "").split(",") if str(part).strip()}


def parse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").date()
    except ValueError:
        return None


def eligible_report_cutoff(asof: str, *, config: dict[str, Any]) -> str:
    asof_date = parse_date(asof) or date.today()
    lag_days = int(cfg_get(config, "institutional_flow_features.report_lag_days", 45))
    return (asof_date - timedelta(days=lag_days)).isoformat()


def institutional_crowding_signal(
    *,
    delta: float,
    manager_count: float,
    cohort: str,
    crowding_cohorts: set[str],
    smart_money_cohorts: set[str],
) -> dict[str, float]:
    accumulation_score = linear_score(
        delta,
        [(-0.20, 0.0), (-0.10, 20.0), (0.0, 50.0), (0.05, 70.0), (0.15, 92.0), (0.30, 100.0)],
    )
    breadth_score = linear_score(
        manager_count,
        [(0.0, 0.0), (5.0, 20.0), (15.0, 50.0), (40.0, 75.0), (100.0, 90.0), (200.0, 100.0)],
    )
    flow_direction = max(0.0, delta)
    crowding_intensity = clamp(
        0.60 * linear_score(flow_direction, [(0.0, 0.0), (0.05, 40.0), (0.15, 75.0), (0.30, 100.0)])
        + 0.40 * breadth_score
    )
    if cohort in crowding_cohorts:
        crowding_score = 100.0 - crowding_intensity
    elif cohort in smart_money_cohorts:
        crowding_score = clamp(0.60 * accumulation_score + 0.40 * breadth_score)
    else:
        crowding_score = 50.0
    return {
        "institutional_accumulation_score": round(accumulation_score, 4),
        "institutional_crowding_score": round(crowding_score, 4),
        "institutional_breadth_score": round(breadth_score, 4),
    }


def load_companies(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT c.company_id, c.ticker, COALESCE(t.calibration_cohort, '') AS calibration_cohort
        FROM dim_company c
        LEFT JOIN dim_company_model_taxonomy t ON t.company_id = c.company_id
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """
    ).fetchall()
    return [dict(row) for row in rows]


def load_quarterly_facts(conn: Any, *, cutoff: str) -> dict[int, list[dict[str, Any]]]:
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fact_sec_13f_holding'").fetchone():
        return {}
    rows = conn.execute(
        """
        SELECT
            company_id,
            ticker,
            report_date,
            SUM(COALESCE(shares, 0.0)) AS shares,
            SUM(COALESCE(market_value_usd, 0.0)) AS market_value_usd,
            CASE
                WHEN MAX(COALESCE(manager_count, 0.0)) > 1.0 THEN MAX(manager_count)
                ELSE COUNT(DISTINCT COALESCE(NULLIF(manager_cik, ''), NULLIF(manager_name, ''), accession_nodash))
            END AS reported_manager_count,
            MAX(institutional_ownership_pct) AS reported_ownership_pct,
            MAX(institutional_ownership_delta_pct) AS reported_delta_pct,
            COUNT(DISTINCT COALESCE(manager_cik, manager_name, accession_nodash)) AS distinct_manager_count
        FROM fact_sec_13f_holding
        WHERE report_date <= ?
          AND report_date >= date(?, '-3 years')
          AND company_id IS NOT NULL
          AND COALESCE(put_call, '') = ''
        GROUP BY company_id, ticker, report_date
        ORDER BY company_id, report_date DESC
        """,
        (cutoff, cutoff),
    ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(int(row["company_id"]), []).append(dict(row))
    return out


def score_company(
    company: dict[str, Any],
    facts: list[dict[str, Any]],
    *,
    asof: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    neutral = float(cfg_get(config, "institutional_flow_features.no_data_score", 50.0))
    if not facts:
        return {
            "asof_date": asof,
            "company_id": int(company["company_id"]),
            "ticker": normalize_ticker(company.get("ticker")),
            "institutional_ownership_delta_pct": 0.0,
            "institutional_accumulation_score": neutral,
            "institutional_crowding_score": neutral,
            "institutional_breadth_score": neutral,
            "institutional_manager_count": None,
            "institutional_share_count": None,
            "institutional_market_value_usd": None,
            "data_quality_score": float(cfg_get(config, "institutional_flow_features.no_data_quality_score", 0.0)),
            "payload_json": json.dumps({"source": "no_13f_facts"}, sort_keys=True),
        }
    latest = facts[0]
    previous = facts[1] if len(facts) > 1 else {}
    reported_delta = latest.get("reported_delta_pct")
    if reported_delta is not None:
        # Med-device 13F facts store quarter-over-quarter share delta in decimal
        # form. Do not reinterpret values above 1.0 as percentage units here;
        # that would turn a true +150% accumulation into +1.5%.
        delta = to_float(reported_delta) or 0.0
    else:
        latest_shares = to_float(latest.get("shares")) or 0.0
        previous_shares = to_float(previous.get("shares")) or 0.0
        delta = (latest_shares - previous_shares) / previous_shares if previous_shares > 0 else 0.0
    raw_delta = delta
    max_abs_delta = float(cfg_get(config, "institutional_flow_features.max_delta_abs_for_scoring", 1.5))
    delta_clipped = False
    if max_abs_delta > 0 and abs(delta) > max_abs_delta:
        delta = max(-max_abs_delta, min(max_abs_delta, delta))
        delta_clipped = True
    manager_count = to_float(latest.get("reported_manager_count")) or to_float(latest.get("distinct_manager_count")) or 0.0
    crowding_cohorts = parse_list(cfg_get(config, "institutional_flow_features.crowding_cohorts", ""))
    smart_money_cohorts = parse_list(cfg_get(config, "institutional_flow_features.smart_money_cohorts", ""))
    scores = institutional_crowding_signal(
        delta=delta,
        manager_count=manager_count,
        cohort=str(company.get("calibration_cohort") or ""),
        crowding_cohorts=crowding_cohorts,
        smart_money_cohorts=smart_money_cohorts,
    )
    high_quality_min_history = int(cfg_get(config, "institutional_flow_features.high_quality_min_history_points", 4))
    if len(facts) >= high_quality_min_history:
        data_quality = 85.0
    elif len(facts) > 1:
        data_quality = 70.0
    else:
        data_quality = 55.0
    if latest.get("reported_delta_pct") is not None:
        data_quality = min(100.0, data_quality + (10.0 if not delta_clipped else 0.0))
    if delta_clipped:
        data_quality = min(data_quality, 70.0)
    return {
        "asof_date": asof,
        "company_id": int(company["company_id"]),
        "ticker": normalize_ticker(company.get("ticker")),
        "institutional_ownership_delta_pct": round(delta, 6),
        **scores,
        "institutional_manager_count": manager_count,
        "institutional_share_count": to_float(latest.get("shares")),
        "institutional_market_value_usd": to_float(latest.get("market_value_usd")),
        "data_quality_score": data_quality,
        "payload_json": json.dumps(
            {
                "latest_report_date": latest.get("report_date"),
                "previous_report_date": previous.get("report_date"),
                "raw_institutional_ownership_delta_pct": raw_delta,
                "delta_clipped": delta_clipped,
                "fact_count": len(facts),
                "cohort_direction": "crowding"
                if str(company.get("calibration_cohort") or "") in crowding_cohorts
                else "smart_money"
                if str(company.get("calibration_cohort") or "") in smart_money_cohorts
                else "neutral",
            },
            sort_keys=True,
            ensure_ascii=True,
        ),
    }


def build_rows(conn: Any, *, asof: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    cutoff = eligible_report_cutoff(asof, config=config)
    facts = load_quarterly_facts(conn, cutoff=cutoff)
    return [score_company(company, facts.get(int(company["company_id"]), []), asof=asof, config=config) for company in load_companies(conn)]


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO feature_institutional_flow(
            asof_date, company_id, ticker, institutional_ownership_delta_pct,
            institutional_accumulation_score, institutional_crowding_score, institutional_breadth_score,
            institutional_manager_count, institutional_share_count, institutional_market_value_usd,
            data_quality_score, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            ticker = excluded.ticker,
            institutional_ownership_delta_pct = excluded.institutional_ownership_delta_pct,
            institutional_accumulation_score = excluded.institutional_accumulation_score,
            institutional_crowding_score = excluded.institutional_crowding_score,
            institutional_breadth_score = excluded.institutional_breadth_score,
            institutional_manager_count = excluded.institutional_manager_count,
            institutional_share_count = excluded.institutional_share_count,
            institutional_market_value_usd = excluded.institutional_market_value_usd,
            data_quality_score = excluded.data_quality_score,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["asof_date"],
                row["company_id"],
                row["ticker"],
                row["institutional_ownership_delta_pct"],
                row["institutional_accumulation_score"],
                row["institutional_crowding_score"],
                row["institutional_breadth_score"],
                row.get("institutional_manager_count"),
                row.get("institutional_share_count"),
                row.get("institutional_market_value_usd"),
                row["data_quality_score"],
                row.get("payload_json", "{}"),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    asof = args.asof.strip() or date.today().isoformat()
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "institutional_flow_features.output_csv"), base_dir=base_dir)
    )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="build_med_device_institutional_flow_features", input_path=config_path)
        try:
            rows = build_rows(conn, asof=asof, config=config)
            count = upsert_rows(conn, rows)
            write_csv(output_csv, rows)
            finish_run(conn, run_id=run_id, status="success", row_count=count, message=f"asof={asof} rows={count}")
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
