#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date, datetime, timezone
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
    "borrow_availability_score",
    "borrow_fee_score",
    "borrow_squeeze_risk_score",
    "borrow_pressure_score",
    "shortable_status",
    "shortable_shares",
    "borrow_fee_rate",
    "data_quality_score",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device borrow-risk shadow features.")
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
    if not points:
        return 50.0
    points = sorted(points)
    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (value - x0) / (x1 - x0)
    return points[-1][1]


def normalize_fee_rate(raw: float | None) -> float | None:
    if raw is None:
        return None
    value = abs(float(raw))
    if value > 1.0:
        value /= 100.0
    return value


def parse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").date()
    except ValueError:
        return None


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


def load_latest_snapshots(conn: Any, *, asof: str) -> dict[int, dict[str, Any]]:
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fact_ibkr_borrow_snapshot'").fetchone():
        return {}
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT
                s.*,
                ROW_NUMBER() OVER (
                    PARTITION BY s.company_id
                    ORDER BY s.asof_date DESC, s.rowid DESC
                ) AS rn
            FROM fact_ibkr_borrow_snapshot s
            WHERE s.company_id IS NOT NULL
              AND s.asof_date <= ?
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        """,
        (asof,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows if row["company_id"] is not None}


def score_snapshot(snapshot: dict[str, Any] | None, *, asof: str, config: dict[str, Any]) -> dict[str, Any]:
    neutral = float(cfg_get(config, "borrow_features.no_data_score", 50.0))
    if not snapshot:
        return {
            "borrow_availability_score": neutral,
            "borrow_fee_score": neutral,
            "borrow_squeeze_risk_score": neutral,
            "borrow_pressure_score": neutral,
            "shortable_status": None,
            "shortable_shares": None,
            "borrow_fee_rate": None,
            "data_quality_score": float(cfg_get(config, "borrow_features.no_data_quality_score", 0.0)),
            "payload": {"source": "no_borrow_snapshot"},
        }
    asof_date = parse_date(asof)
    snapshot_date = parse_date(str(snapshot.get("asof_date") or ""))
    max_stale = int(cfg_get(config, "borrow_features.max_snapshot_staleness_days", 10))
    stale = asof_date is None or snapshot_date is None or (asof_date - snapshot_date).days > max_stale or (asof_date - snapshot_date).days < 0
    shortable_status = to_float(snapshot.get("shortable_status"))
    shortable_shares = to_float(snapshot.get("shortable_shares"))
    fee_rate = normalize_fee_rate(to_float(snapshot.get("borrow_fee_rate")))
    status_score = (
        linear_score(shortable_status, [(0.0, 10.0), (1.0, 35.0), (2.0, 70.0), (3.0, 95.0)])
        if shortable_status is not None
        else neutral
    )
    min_full = float(cfg_get(config, "borrow_features.min_shortable_shares_full_credit", 1_000_000))
    shares_score = (
        linear_score(math.log1p(max(0.0, shortable_shares)), [(0.0, 5.0), (math.log1p(10_000), 35.0), (math.log1p(min_full), 90.0)])
        if shortable_shares is not None
        else neutral
    )
    high_fee = float(cfg_get(config, "borrow_features.high_borrow_fee_rate", 0.10))
    extreme_fee = float(cfg_get(config, "borrow_features.extreme_borrow_fee_rate", 0.30))
    fee_risk = linear_score(fee_rate or 0.0, [(0.0, 0.0), (0.02, 20.0), (high_fee, 70.0), (extreme_fee, 100.0)])
    availability_score = clamp(0.55 * status_score + 0.45 * shares_score)
    fee_score = clamp(100.0 - fee_risk)
    squeeze_risk_score = clamp(0.55 * (100.0 - availability_score) + 0.45 * fee_risk)
    pressure_score = 100.0 - squeeze_risk_score
    populated = sum(value is not None for value in (shortable_status, shortable_shares, fee_rate))
    data_quality = 0.0 if stale else round(100.0 * populated / 3.0, 2)
    return {
        "borrow_availability_score": round(availability_score if not stale else neutral, 2),
        "borrow_fee_score": round(fee_score if not stale else neutral, 2),
        "borrow_squeeze_risk_score": round(squeeze_risk_score if not stale else neutral, 2),
        "borrow_pressure_score": round(pressure_score if not stale else neutral, 2),
        "shortable_status": shortable_status,
        "shortable_shares": shortable_shares,
        "borrow_fee_rate": fee_rate,
        "data_quality_score": data_quality,
        "payload": {"source_asof_date": snapshot.get("asof_date"), "stale": stale},
    }


def build_rows(conn: Any, *, asof: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    companies = load_companies(conn)
    snapshots = load_latest_snapshots(conn, asof=asof)
    rows: list[dict[str, Any]] = []
    for company in companies:
        company_id = int(company["company_id"])
        scored = score_snapshot(snapshots.get(company_id), asof=asof, config=config)
        rows.append(
            {
                "asof_date": asof,
                "company_id": company_id,
                "ticker": normalize_ticker(company.get("ticker")),
                **{key: scored[key] for key in FIELDNAMES if key in scored},
                "payload_json": json.dumps(scored["payload"], sort_keys=True, ensure_ascii=True),
            }
        )
    return rows


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO feature_borrow_risk(
            asof_date, company_id, ticker, borrow_availability_score, borrow_fee_score,
            borrow_squeeze_risk_score, borrow_pressure_score, shortable_status, shortable_shares,
            borrow_fee_rate, data_quality_score, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            ticker = excluded.ticker,
            borrow_availability_score = excluded.borrow_availability_score,
            borrow_fee_score = excluded.borrow_fee_score,
            borrow_squeeze_risk_score = excluded.borrow_squeeze_risk_score,
            borrow_pressure_score = excluded.borrow_pressure_score,
            shortable_status = excluded.shortable_status,
            shortable_shares = excluded.shortable_shares,
            borrow_fee_rate = excluded.borrow_fee_rate,
            data_quality_score = excluded.data_quality_score,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["asof_date"],
                row["company_id"],
                row["ticker"],
                row["borrow_availability_score"],
                row["borrow_fee_score"],
                row["borrow_squeeze_risk_score"],
                row["borrow_pressure_score"],
                row.get("shortable_status"),
                row.get("shortable_shares"),
                row.get("borrow_fee_rate"),
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
        else resolve_path(cfg_get(config, "borrow_features.output_csv"), base_dir=base_dir)
    )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="build_med_device_borrow_features", input_path=config_path)
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
