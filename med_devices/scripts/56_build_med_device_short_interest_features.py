#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
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
    "short_interest_score",
    "short_pressure_score",
    "short_squeeze_score",
    "short_volume_score",
    "short_interest_velocity_score",
    "days_to_cover_score",
    "short_interest",
    "short_interest_pct_float",
    "days_to_cover",
    "short_volume_ratio_20d",
    "short_volume_ratio_delta_20d",
    "data_quality_score",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device short-interest and short-volume shadow features.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
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


def parse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").date()
    except ValueError:
        return None


def normalize_pct(raw: float | None) -> float | None:
    if raw is None:
        return None
    value = float(raw)
    if abs(value) > 1.0:
        value /= 100.0
    return value


def load_companies(conn: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT c.company_id, c.ticker
            FROM dim_company c
            WHERE c.is_active = 1
              AND EXISTS (
                    SELECT 1
                    FROM dim_company_model_taxonomy t
                    WHERE t.company_id = c.company_id
                      AND t.model_family = 'med_devices'
              )
            ORDER BY ticker
            """
        ).fetchall()
    ]


def load_short_interest(conn: Any, *, asof: str) -> dict[int, list[dict[str, Any]]]:
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fact_short_interest'").fetchone():
        return {}
    rows = conn.execute(
        """
        SELECT si.*, dc.company_id AS mapped_company_id
        FROM fact_short_interest si
        LEFT JOIN dim_company dc ON dc.ticker = si.ticker
        WHERE si.settlement_date <= ?
        ORDER BY
            COALESCE(si.company_id, dc.company_id),
            si.settlement_date DESC,
            CASE WHEN si.source_id = 'finra_equity_short_interest' THEN 0 ELSE 1 END,
            si.source_id
        """,
        (asof,),
    ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {}
    seen_dates: dict[int, set[str]] = {}
    for row in rows:
        item = dict(row)
        company_id = item.get("company_id") or item.get("mapped_company_id")
        if company_id is None:
            continue
        company_id_int = int(company_id)
        settlement_date = str(item.get("settlement_date") or "")
        if settlement_date in seen_dates.setdefault(company_id_int, set()):
            continue
        seen_dates[company_id_int].add(settlement_date)
        out.setdefault(company_id_int, []).append(item)
    return out


def load_short_volume_stats(conn: Any, *, asof: str, lookback_days: int) -> dict[int, dict[str, float]]:
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fact_finra_short_volume'").fetchone():
        return {}
    asof_date = parse_date(asof)
    if asof_date is None:
        return {}
    cur_start = (asof_date - timedelta(days=lookback_days)).isoformat()
    prior_start = (asof_date - timedelta(days=2 * lookback_days)).isoformat()
    rows = conn.execute(
        """
        SELECT v.*, dc.company_id AS mapped_company_id
        FROM fact_finra_short_volume v
        LEFT JOIN dim_company dc ON dc.ticker = v.ticker
        WHERE v.trade_date <= ?
          AND v.trade_date > ?
        """,
        (asof, prior_start),
    ).fetchall()
    current: dict[int, list[float]] = {}
    prior: dict[int, list[float]] = {}
    for row in rows:
        item = dict(row)
        company_id = item.get("company_id") or item.get("mapped_company_id")
        ratio = to_float(item.get("short_volume_ratio"))
        if company_id is None or ratio is None:
            continue
        bucket = current if str(item.get("trade_date")) >= cur_start else prior
        bucket.setdefault(int(company_id), []).append(ratio)
    out: dict[int, dict[str, float]] = {}
    for company_id, values in current.items():
        cur = statistics.mean(values) if values else None
        prev_values = prior.get(company_id, [])
        prev = statistics.mean(prev_values) if prev_values else None
        out[company_id] = {
            "short_volume_ratio_20d": float(cur or 0.0),
            "short_volume_ratio_delta_20d": float((cur or 0.0) - (prev or cur or 0.0)),
        }
    return out


def score_company(
    company: dict[str, Any],
    *,
    asof: str,
    short_interest_rows: list[dict[str, Any]],
    short_volume_stats: dict[str, float] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    neutral = float(cfg_get(config, "short_interest_features.no_data_score", 50.0))
    latest = short_interest_rows[0] if short_interest_rows else {}
    previous = short_interest_rows[1] if len(short_interest_rows) > 1 else {}
    short_interest = to_float(latest.get("short_interest"))
    pct_float = normalize_pct(to_float(latest.get("short_interest_pct_float")))
    if pct_float is None:
        float_shares = to_float(latest.get("float_shares"))
        pct_float = short_interest / float_shares if short_interest is not None and float_shares and float_shares > 0 else None
    days_to_cover = to_float(latest.get("days_to_cover"))
    prev_pct = normalize_pct(to_float(previous.get("short_interest_pct_float")))
    if prev_pct is None:
        prev_short = to_float(previous.get("short_interest"))
        prev_float = to_float(previous.get("float_shares")) or to_float(latest.get("float_shares"))
        prev_pct = prev_short / prev_float if prev_short is not None and prev_float and prev_float > 0 else None
    velocity = (pct_float - prev_pct) if pct_float is not None and prev_pct is not None else None
    volume_ratio = short_volume_stats.get("short_volume_ratio_20d") if short_volume_stats else None
    volume_delta = short_volume_stats.get("short_volume_ratio_delta_20d") if short_volume_stats else None
    short_risk = linear_score(pct_float or 0.0, [(0.0, 0.0), (0.05, 35.0), (0.10, 60.0), (0.20, 85.0), (0.35, 100.0)])
    dtc_risk = linear_score(days_to_cover or 0.0, [(0.0, 0.0), (2.0, 25.0), (5.0, 60.0), (10.0, 85.0), (20.0, 100.0)])
    volume_risk = linear_score(volume_ratio or 0.0, [(0.0, 0.0), (0.35, 30.0), (0.50, 55.0), (0.65, 80.0), (0.80, 100.0)])
    velocity_risk = linear_score(velocity or 0.0, [(-0.05, 5.0), (0.0, 50.0), (0.03, 75.0), (0.08, 100.0)])
    short_pressure = clamp(0.35 * short_risk + 0.25 * dtc_risk + 0.25 * volume_risk + 0.15 * velocity_risk)
    short_squeeze = clamp(0.50 * short_risk + 0.35 * dtc_risk + 0.15 * volume_risk)
    populated = sum(value is not None for value in (pct_float, days_to_cover, volume_ratio, velocity))
    data_quality = round(100.0 * populated / 4.0, 2) if populated else float(cfg_get(config, "short_interest_features.no_data_quality_score", 0.0))
    if populated == 0:
        short_pressure = neutral
        short_squeeze = neutral
        short_risk = 100.0 - neutral
        dtc_risk = neutral
        volume_risk = neutral
        velocity_risk = neutral
    return {
        "asof_date": asof,
        "company_id": int(company["company_id"]),
        "ticker": normalize_ticker(company.get("ticker")),
        "short_interest_score": round(clamp(100.0 - short_risk), 2),
        "short_pressure_score": round(clamp(100.0 - short_pressure), 2),
        "short_squeeze_score": round(clamp(100.0 - short_squeeze), 2),
        "short_volume_score": round(clamp(100.0 - volume_risk), 2),
        "short_interest_velocity_score": round(clamp(100.0 - velocity_risk), 2),
        "days_to_cover_score": round(clamp(100.0 - dtc_risk), 2),
        "short_interest": short_interest,
        "short_interest_pct_float": pct_float,
        "days_to_cover": days_to_cover,
        "short_volume_ratio_20d": volume_ratio,
        "short_volume_ratio_delta_20d": volume_delta,
        "data_quality_score": data_quality,
        "payload_json": json.dumps(
            {
                "latest_settlement_date": latest.get("settlement_date"),
                "previous_settlement_date": previous.get("settlement_date"),
                "short_interest_pct_velocity": velocity,
            },
            sort_keys=True,
            ensure_ascii=True,
        ),
    }


def build_rows(conn: Any, *, asof: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    companies = load_companies(conn)
    si_by_company = load_short_interest(conn, asof=asof)
    lookback = int(cfg_get(config, "short_interest_features.short_volume_lookback_days", 30))
    sv_by_company = load_short_volume_stats(conn, asof=asof, lookback_days=lookback)
    return [
        score_company(
            company,
            asof=asof,
            short_interest_rows=si_by_company.get(int(company["company_id"]), []),
            short_volume_stats=sv_by_company.get(int(company["company_id"])),
            config=config,
        )
        for company in companies
    ]


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO feature_short_interest(
            asof_date, company_id, ticker, short_interest_score, short_pressure_score,
            short_squeeze_score, short_volume_score, short_interest_velocity_score,
            days_to_cover_score, short_interest, short_interest_pct_float, days_to_cover,
            short_volume_ratio_20d, short_volume_ratio_delta_20d, data_quality_score,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            ticker = excluded.ticker,
            short_interest_score = excluded.short_interest_score,
            short_pressure_score = excluded.short_pressure_score,
            short_squeeze_score = excluded.short_squeeze_score,
            short_volume_score = excluded.short_volume_score,
            short_interest_velocity_score = excluded.short_interest_velocity_score,
            days_to_cover_score = excluded.days_to_cover_score,
            short_interest = excluded.short_interest,
            short_interest_pct_float = excluded.short_interest_pct_float,
            days_to_cover = excluded.days_to_cover,
            short_volume_ratio_20d = excluded.short_volume_ratio_20d,
            short_volume_ratio_delta_20d = excluded.short_volume_ratio_delta_20d,
            data_quality_score = excluded.data_quality_score,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["asof_date"],
                row["company_id"],
                row["ticker"],
                row["short_interest_score"],
                row["short_pressure_score"],
                row["short_squeeze_score"],
                row["short_volume_score"],
                row["short_interest_velocity_score"],
                row["days_to_cover_score"],
                row.get("short_interest"),
                row.get("short_interest_pct_float"),
                row.get("days_to_cover"),
                row.get("short_volume_ratio_20d"),
                row.get("short_volume_ratio_delta_20d"),
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
    asof = args.asof.strip() or datetime.now(timezone.utc).date().isoformat()
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "short_interest_features.output_csv"), base_dir=base_dir)
    )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="build_med_device_short_interest_features", input_path=config_path)
        try:
            rows = build_rows(conn, asof=asof, config=config)
            count = upsert_rows(conn, rows)
            write_csv(output_csv, rows)
            finish_run(conn, run_id=run_id, status="success", row_count=count, message=f"asof={asof} rows={count}")
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    raise SystemExit(main())
