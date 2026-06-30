#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
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
    "insider_net_buy_score",
    "insider_cluster_buy_score",
    "insider_selling_pressure_score",
    "insider_activity_score",
    "net_purchase_value_90d",
    "open_market_buy_count_90d",
    "open_market_sell_count_90d",
    "unique_buyer_count_90d",
    "unique_seller_count_90d",
    "data_quality_score",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device insider-activity shadow features from Form 4 facts.")
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


def parse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_payload_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    parsed = parse_date(text[:10])
    if parsed is not None:
        return parsed
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return datetime.strptime(text[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return None


def safe_json_loads(raw: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def form4_availability_date(tx: dict[str, Any], *, fallback_lag_days: int) -> date | None:
    transaction_date = parse_payload_date(tx.get("transaction_date"))
    payload = safe_json_loads(tx.get("payload_json"))
    filing_date = None
    for key in (
        "filing_date",
        "filingDate",
        "filed_date",
        "accepted_at",
        "acceptedAt",
        "acceptance_datetime",
        "ACCEPTANCE_DATETIME",
    ):
        filing_date = parse_payload_date(payload.get(key))
        if filing_date is not None:
            break
    if transaction_date is None:
        return filing_date
    if filing_date is None:
        filing_date = transaction_date + timedelta(days=max(0, fallback_lag_days))
    return max(transaction_date, filing_date)


def load_companies(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
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
    return [dict(row) for row in rows]


def load_transactions(conn: Any, *, asof: str, lookback_days: int, config: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fact_sec_form4_transaction'").fetchone():
        return {}
    asof_date = parse_date(asof) or datetime.now(timezone.utc).date()
    fallback_lag_days = int(cfg_get(config, "insider_activity_features.filing_lag_days", 2))
    start = (asof_date - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        """
        SELECT *
        FROM fact_sec_form4_transaction
        WHERE company_id IS NOT NULL
          AND transaction_date <= ?
          AND transaction_date > ?
          AND COALESCE(derivative_flag, 0) = 0
        """,
        (asof, start),
    ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        available_date = form4_availability_date(item, fallback_lag_days=fallback_lag_days)
        if available_date is None or available_date > asof_date:
            continue
        out.setdefault(int(row["company_id"]), []).append(item)
    return out


def score_company(company: dict[str, Any], txs: list[dict[str, Any]], *, asof: str, config: dict[str, Any]) -> dict[str, Any]:
    neutral = float(cfg_get(config, "insider_activity_features.no_data_score", 50.0))
    buys = [tx for tx in txs if str(tx.get("transaction_code") or "").upper() == "P"]
    sells = [tx for tx in txs if str(tx.get("transaction_code") or "").upper() == "S"]
    buy_value = sum(to_float(tx.get("transaction_value_usd")) or 0.0 for tx in buys)
    sell_value = sum(to_float(tx.get("transaction_value_usd")) or 0.0 for tx in sells)
    net_purchase = buy_value - sell_value
    unique_buyers = {str(tx.get("reporting_owner_cik") or tx.get("reporting_owner_name") or "") for tx in buys}
    unique_sellers = {str(tx.get("reporting_owner_cik") or tx.get("reporting_owner_name") or "") for tx in sells}
    unique_buyers.discard("")
    unique_sellers.discard("")
    if not buys and not sells:
        return {
            "asof_date": asof,
            "company_id": int(company["company_id"]),
            "ticker": normalize_ticker(company.get("ticker")),
            "insider_net_buy_score": neutral,
            "insider_cluster_buy_score": neutral,
            "insider_selling_pressure_score": neutral,
            "insider_activity_score": neutral,
            "net_purchase_value_90d": 0.0,
            "open_market_buy_count_90d": 0,
            "open_market_sell_count_90d": 0,
            "unique_buyer_count_90d": 0,
            "unique_seller_count_90d": 0,
            "data_quality_score": float(cfg_get(config, "insider_activity_features.no_data_quality_score", 0.0)),
            "payload_json": json.dumps({"source": "no_recent_form4_transactions"}, sort_keys=True),
        }
    net_buy_score = linear_score(
        net_purchase,
        [(-5_000_000.0, 0.0), (-1_000_000.0, 20.0), (0.0, 50.0), (250_000.0, 70.0), (1_000_000.0, 88.0), (5_000_000.0, 100.0)],
    )
    cluster_buy_score = linear_score(
        len(unique_buyers),
        [(0.0, 35.0), (1.0, 55.0), (2.0, 72.0), (3.0, 85.0), (5.0, 100.0)],
    )
    selling_pressure_score = linear_score(
        sell_value,
        [(0.0, 10.0), (250_000.0, 35.0), (1_000_000.0, 60.0), (5_000_000.0, 90.0), (20_000_000.0, 100.0)],
    )
    activity_score = clamp(0.45 * net_buy_score + 0.35 * cluster_buy_score + 0.20 * (100.0 - selling_pressure_score))
    return {
        "asof_date": asof,
        "company_id": int(company["company_id"]),
        "ticker": normalize_ticker(company.get("ticker")),
        "insider_net_buy_score": round(net_buy_score, 2),
        "insider_cluster_buy_score": round(cluster_buy_score, 2),
        "insider_selling_pressure_score": round(clamp(100.0 - selling_pressure_score), 2),
        "insider_activity_score": round(activity_score, 2),
        "net_purchase_value_90d": net_purchase,
        "open_market_buy_count_90d": len(buys),
        "open_market_sell_count_90d": len(sells),
        "unique_buyer_count_90d": len(unique_buyers),
        "unique_seller_count_90d": len(unique_sellers),
        "data_quality_score": clamp(
            float(cfg_get(config, "insider_activity_features.transaction_data_quality_score", 90.0))
        ),
        "payload_json": json.dumps({"buy_value": buy_value, "sell_value": sell_value}, sort_keys=True, ensure_ascii=True),
    }


def build_rows(conn: Any, *, asof: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    lookback_days = int(cfg_get(config, "insider_activity_features.lookback_days", 90))
    tx_by_company = load_transactions(conn, asof=asof, lookback_days=lookback_days, config=config)
    return [
        score_company(company, tx_by_company.get(int(company["company_id"]), []), asof=asof, config=config)
        for company in load_companies(conn)
    ]


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO feature_insider_activity(
            asof_date, company_id, ticker, insider_net_buy_score, insider_cluster_buy_score,
            insider_selling_pressure_score, insider_activity_score, net_purchase_value_90d,
            open_market_buy_count_90d, open_market_sell_count_90d, unique_buyer_count_90d,
            unique_seller_count_90d, data_quality_score, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            ticker = excluded.ticker,
            insider_net_buy_score = excluded.insider_net_buy_score,
            insider_cluster_buy_score = excluded.insider_cluster_buy_score,
            insider_selling_pressure_score = excluded.insider_selling_pressure_score,
            insider_activity_score = excluded.insider_activity_score,
            net_purchase_value_90d = excluded.net_purchase_value_90d,
            open_market_buy_count_90d = excluded.open_market_buy_count_90d,
            open_market_sell_count_90d = excluded.open_market_sell_count_90d,
            unique_buyer_count_90d = excluded.unique_buyer_count_90d,
            unique_seller_count_90d = excluded.unique_seller_count_90d,
            data_quality_score = excluded.data_quality_score,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["asof_date"],
                row["company_id"],
                row["ticker"],
                row["insider_net_buy_score"],
                row["insider_cluster_buy_score"],
                row["insider_selling_pressure_score"],
                row["insider_activity_score"],
                row["net_purchase_value_90d"],
                row["open_market_buy_count_90d"],
                row["open_market_sell_count_90d"],
                row["unique_buyer_count_90d"],
                row["unique_seller_count_90d"],
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
        else resolve_path(cfg_get(config, "insider_activity_features.output_csv"), base_dir=base_dir)
    )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="build_med_device_insider_activity_features", input_path=config_path)
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
