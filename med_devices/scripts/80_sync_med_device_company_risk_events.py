#!/usr/bin/env python3
"""Load governed company risk events into the med-devices event fact table."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.point_in_time import parse_iso_date, row_is_effective_asof  # noqa: E402
from med_devices.core.text_norm import as_bool, normalize_ticker  # noqa: E402

LOGGER = logging.getLogger("sync_med_device_company_risk_events")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REQUIRED_FIELDS = {
    "event_id",
    "event_time_utc",
    "ticker",
    "source_name",
    "title",
    "url",
    "event_type",
    "severity",
    "valid_from",
    "reviewed_at",
    "active",
}
EVIDENCE_TEXT_FIELDS = (
    "evidence_layer",
    "recall_event_id",
    "recall_numbers",
    "remediation_status",
    "raw_mdr_signal_status",
)
EVIDENCE_COUNT_FIELDS = (
    "confirmed_injuries",
    "confirmed_deaths",
    "raw_mdr_signal_count",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load effective-dated reviewed company risk events."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--asof", default="")
    return parser.parse_args()


def resolve_asof(raw: str) -> str:
    text = str(raw or "").strip() or datetime.now(timezone.utc).date().isoformat()
    parsed = parse_iso_date(text)
    if parsed is None:
        raise ValueError(f"Invalid as-of date: {text}")
    return parsed.isoformat()


def row_value(row: dict[str, str], name: str) -> str:
    return str(row.get(name) or "").strip()


def parse_tone(raw: str) -> float | None:
    if not raw:
        return None
    value = float(raw)
    if not math.isfinite(value) or value < -1.0 or value > 1.0:
        raise ValueError(f"tone must be finite and within [-1, 1], got {raw!r}")
    return value


def optional_int(raw: str) -> int | None:
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError as exc:
        raise ValueError(
            f"Expected integer risk-event evidence value, got {raw!r}"
        ) from exc


def load_rows(path: Path, *, asof: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = {str(name or "").strip() for name in (reader.fieldnames or [])}
        missing = sorted(REQUIRED_FIELDS - fieldnames)
        if missing:
            raise ValueError(f"Missing required risk-event columns: {missing}")
        rows: list[dict[str, str]] = []
        seen_event_ids: set[str] = set()
        for line_number, raw in enumerate(reader, start=2):
            row = {str(key): str(value or "").strip() for key, value in raw.items()}
            if not row_is_effective_asof(row, asof, include_missing=False):
                continue
            if not as_bool(row_value(row, "active"), default=False):
                continue
            event_id = row_value(row, "event_id")
            if not event_id or event_id in seen_event_ids:
                raise ValueError(
                    f"Missing or duplicate event_id at {path}:{line_number}: {event_id!r}"
                )
            event_date = parse_iso_date(row_value(row, "event_time_utc")[:10])
            if event_date is None:
                raise ValueError(
                    f"Invalid event_time_utc at {path}:{line_number}: "
                    f"{row_value(row, 'event_time_utc')!r}"
                )
            if event_date.isoformat() > asof:
                continue
            ticker = normalize_ticker(row_value(row, "ticker"))
            if not ticker:
                raise ValueError(f"Missing ticker at {path}:{line_number}")
            if not row_value(row, "url").startswith("https://"):
                raise ValueError(f"Risk-event URL must be HTTPS at {path}:{line_number}")
            row["ticker"] = ticker
            seen_event_ids.add(event_id)
            rows.append(row)
    return rows


def company_id_for_ticker(conn: Any, ticker: str) -> int:
    row = conn.execute(
        """
        SELECT company_id
        FROM dim_company
        WHERE UPPER(ticker) = ?
        ORDER BY company_id
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Risk event references unknown med-devices ticker: {ticker}")
    return int(row["company_id"])


def upsert_event(conn: Any, row: dict[str, str]) -> None:
    ticker = row["ticker"]
    company_id = company_id_for_ticker(conn, ticker)
    now = utc_now()
    payload = {
        "event_id": row_value(row, "event_id"),
        "event_type": row_value(row, "event_type"),
        "severity": row_value(row, "severity"),
        "amount_usd": float(row_value(row, "amount_usd"))
        if row_value(row, "amount_usd")
        else None,
        "agency": row_value(row, "agency"),
        "allegations_only": as_bool(row_value(row, "allegations_only"), default=False),
        "valid_from": row_value(row, "valid_from"),
        "reviewed_at": row_value(row, "reviewed_at"),
        "notes": row_value(row, "notes"),
        "manual_governed_event": True,
    }
    payload.update({field: row_value(row, field) for field in EVIDENCE_TEXT_FIELDS})
    payload.update(
        {field: optional_int(row_value(row, field)) for field in EVIDENCE_COUNT_FIELDS}
    )
    existing = conn.execute(
        """
        SELECT news_event_id
        FROM fact_news_event
        WHERE UPPER(ticker) = ?
          AND url = ?
        ORDER BY news_event_id
        """,
        (ticker, row_value(row, "url")),
    ).fetchall()
    values = (
        row_value(row, "event_time_utc"),
        company_id,
        ticker,
        row_value(row, "source_name"),
        row_value(row, "title"),
        row_value(row, "url"),
        parse_tone(row_value(row, "tone")),
        row_value(row, "event_tags"),
        json.dumps(payload, sort_keys=True),
        now,
    )
    if existing:
        news_event_id = int(existing[0]["news_event_id"])
        conn.execute(
            """
            UPDATE fact_news_event
            SET event_time_utc = ?,
                company_id = ?,
                ticker = ?,
                source_name = ?,
                title = ?,
                url = ?,
                tone = ?,
                event_tags = ?,
                payload_json = ?,
                updated_at = ?
            WHERE news_event_id = ?
            """,
            (*values, news_event_id),
        )
        duplicate_ids = [int(item["news_event_id"]) for item in existing[1:]]
        if duplicate_ids:
            placeholders = ",".join("?" for _ in duplicate_ids)
            conn.execute(
                f"DELETE FROM fact_news_event WHERE news_event_id IN ({placeholders})",
                duplicate_ids,
            )
        return
    conn.execute(
        """
        INSERT INTO fact_news_event(
            event_time_utc, company_id, ticker, source_name, title, url,
            tone, event_tags, source_id, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (*values[:-1], now, now),
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    input_path = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(
            cfg_get(
                config,
                "company_risk_events.input_csv",
                "data/company_risk_events.csv",
            ),
            base_dir=base_dir,
        )
    )
    asof = resolve_asof(args.asof)
    rows = load_rows(input_path, asof=asof)
    with connect(
        db_path,
        timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0)),
    ) as conn:
        init_db(conn)
        run_id = start_run(
            conn,
            run_type="sync_med_device_company_risk_events",
            input_path=input_path,
        )
        try:
            with conn:
                for row in rows:
                    upsert_event(conn, row)
            message = f"asof={asof} effective_events={len(rows)} input={input_path}"
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(rows),
                message=message,
            )
            LOGGER.info("Company risk-event sync complete: %s", message)
        except BaseException as exc:
            finish_run(
                conn,
                run_id=run_id,
                status="failed",
                row_count=0,
                message=f"{type(exc).__name__}: {exc}",
            )
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
