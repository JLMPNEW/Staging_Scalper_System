#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.csv_utils import read_csv_flexible, row_get  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.core.text_norm import as_bool, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("load_defense_ticker_aliases")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "load_defense_ticker_aliases"
LOAD_STAGE = "defense_ticker_alias_load"
REQUIRED_COLUMNS = {
    "contract_ticker",
    "active_ticker",
    "predecessor_ticker",
    "effective_date",
    "price_history_csv",
    "issuer_id",
    "reason",
    "source",
    "verified_flag",
    "notes",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load defense active ticker aliases and corporate actions.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--aliases-csv", type=Path, default=None)
    parser.add_argument("--skip-source-registry", action="store_true")
    return parser.parse_args()


def parse_iso_date(raw: object, *, ticker: str) -> str:
    text = str(raw or "").strip()[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{ticker}: invalid effective_date={raw!r}; expected YYYY-MM-DD") from exc
    return text


def source_id_or_none(conn: Any, source_id: str) -> str | None:
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    return source_id if row is not None else None


def add_issue(
    conn: Any,
    *,
    ticker: str,
    issue_type: str,
    issue_detail: str,
    severity: str = "warning",
    source_id: str | None = None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, LOAD_STAGE, ticker, source_id, issue_type, issue_detail, now, now),
    )


def validate_header(path: Path) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = handle.readline().strip().split(",")
    missing = sorted(REQUIRED_COLUMNS.difference(header))
    if missing:
        raise ValueError(f"{path} missing required alias column(s): {missing}")


def load_aliases(conn: Any, *, path: Path, source_id: str) -> int:
    validate_header(path)
    rows = read_csv_flexible(path)
    now = utc_now()
    count = 0
    active_source_id = source_id_or_none(conn, source_id)
    if active_source_id is None:
        raise ValueError(f"Source registry is missing required source_id={source_id}")
    conn.execute(
        "DELETE FROM fact_corporate_action WHERE source_id = ? AND action_type = 'ticker_alias'",
        (active_source_id,),
    )
    conn.execute("DELETE FROM dim_ticker_alias WHERE source_id = ?", (active_source_id,))
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        contract_ticker = normalize_ticker(row_get(raw, "contract_ticker"))
        active_ticker = normalize_ticker(row_get(raw, "active_ticker"))
        predecessor_ticker = normalize_ticker(row_get(raw, "predecessor_ticker"))
        if not any((contract_ticker, active_ticker, predecessor_ticker, row_get(raw, "effective_date"))):
            continue
        if not contract_ticker or not active_ticker:
            raise ValueError(f"Alias row must include contract_ticker and active_ticker: {raw}")
        effective_date = parse_iso_date(row_get(raw, "effective_date"), ticker=contract_ticker)
        key = (contract_ticker, effective_date)
        if key in seen:
            raise ValueError(f"Duplicate alias row for {contract_ticker} effective {effective_date}")
        seen.add(key)
        verified_flag = 1 if as_bool(row_get(raw, "verified_flag")) else 0
        issuer_id = row_get(raw, "issuer_id") or contract_ticker
        reason = row_get(raw, "reason") or "ticker_alias"
        notes = row_get(raw, "notes")
        conn.execute(
            """
            INSERT INTO dim_ticker_alias(
                contract_ticker, active_ticker, predecessor_ticker, effective_date,
                price_history_csv, issuer_id, reason, source, verified_flag, notes,
                source_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(contract_ticker, effective_date) DO UPDATE SET
                active_ticker = excluded.active_ticker,
                predecessor_ticker = excluded.predecessor_ticker,
                price_history_csv = excluded.price_history_csv,
                issuer_id = excluded.issuer_id,
                reason = excluded.reason,
                source = excluded.source,
                verified_flag = excluded.verified_flag,
                notes = excluded.notes,
                source_id = excluded.source_id,
                updated_at = excluded.updated_at
            """,
            (
                contract_ticker,
                active_ticker,
                predecessor_ticker,
                effective_date,
                row_get(raw, "price_history_csv"),
                issuer_id,
                reason,
                row_get(raw, "source"),
                verified_flag,
                notes,
                active_source_id,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO fact_corporate_action(
                issuer_id, ticker, related_ticker, action_type, action_date,
                source_id, reason, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, 'ticker_alias', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, related_ticker, action_type, action_date) DO UPDATE SET
                issuer_id = excluded.issuer_id,
                source_id = excluded.source_id,
                reason = excluded.reason,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                issuer_id,
                contract_ticker,
                active_ticker,
                effective_date,
                active_source_id,
                reason,
                notes,
                now,
                now,
            ),
        )
        if verified_flag != 1:
            add_issue(
                conn,
                ticker=contract_ticker,
                source_id=active_source_id,
                issue_type="unverified_ticker_alias",
                issue_detail=f"Alias {contract_ticker}->{active_ticker} effective {effective_date} is not verified.",
                severity="warning",
            )
        count += 1
    return count


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    aliases_csv = args.aliases_csv.expanduser().resolve() if args.aliases_csv else resolve_path(cfg_get(config, "industrials_universe.ticker_aliases_csv"), base_dir=base_dir)
    source_id = str(cfg_get(config, "industrials_universe.ticker_aliases_source_id", "defense_ticker_alias_seed"))

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        if not args.skip_source_registry:
            registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
            upsert_source_registry(conn, load_source_registry(registry_path))
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=aliases_csv)
        try:
            with conn:
                conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", (LOAD_STAGE,))
                row_count = load_aliases(conn, path=aliases_csv, source_id=source_id)
            finish_run(conn, run_id=run_id, status="success", row_count=row_count, message=f"aliases={row_count}")
            LOGGER.info("Loaded defense ticker aliases: rows=%d", row_count)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
