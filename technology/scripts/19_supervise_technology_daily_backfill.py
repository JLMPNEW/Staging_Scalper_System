#!/usr/bin/env python3
"""Supervise technology daily dashboard backfill in restartable chunks."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNNER = PROJECT_ROOT / "technology" / "scripts" / "18_backfill_technology_historical_dashboard_reports.py"
DEFAULT_DB = Path("C:/Users/josel/Documents/STAGING/DB/technology.sqlite")
DEFAULT_LOG_DIR = PROJECT_ROOT / "output" / "technology_reports" / "historical_backfill" / "logs"
DEFAULT_START_DATE = "2019-01-04"
MARKET_SOURCE_ID = "yahoo_finance_adjusted"
CALENDAR_TICKER = "QQQ"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Supervise technology daily dashboard backfill in chunks.")
    parser.add_argument("--family", required=True, help="Technology family alias accepted by script 18.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default="")
    parser.add_argument("--chunk-size", type=int, default=20)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--step-timeout-sec", type=int, default=900)
    parser.add_argument(
        "--require-oos-score-valid",
        action="store_true",
        help="Pass through strict post-production OOS score validation to the historical backfill runner.",
    )
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--status-json", type=Path, default=None)
    return parser.parse_args()


def safe_token(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip().lower())


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def log_line(path: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def trading_dates(db_path: Path, start_date: str, end_date: str) -> list[str]:
    with sqlite3.connect(str(db_path.expanduser().resolve()), timeout=60.0) as conn:
        if not end_date:
            row = conn.execute(
                "SELECT MAX(bar_date) FROM fact_price_ohlcv WHERE source_id = ? AND ticker = ?",
                (MARKET_SOURCE_ID, CALENDAR_TICKER),
            ).fetchone()
            end_date = str(row[0] or "")
        if not end_date:
            raise RuntimeError(f"No calendar end date found for {CALENDAR_TICKER}.")
        rows = conn.execute(
            """
            SELECT DISTINCT bar_date
            FROM fact_price_ohlcv
            WHERE source_id = ?
              AND ticker = ?
              AND bar_date BETWEEN ? AND ?
            ORDER BY bar_date
            """,
            (MARKET_SOURCE_ID, CALENDAR_TICKER, start_date, end_date),
        ).fetchall()
    return [str(row[0]) for row in rows]


def chunks(values: Sequence[str], chunk_size: int) -> list[list[str]]:
    return [list(values[i : i + chunk_size]) for i in range(0, len(values), chunk_size)]


def write_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run_chunk(
    *,
    family: str,
    family_token: str,
    dates: list[str],
    db_path: Path,
    step_timeout_sec: int,
    require_oos_score_valid: bool,
    log_path: Path,
    attempt: int,
) -> int:
    chunk_id = f"{dates[0]}_to_{dates[-1]}_attempt{attempt}"
    child_log = log_path.parent / f"{family_token}_daily_chunk_{chunk_id}_{utc_stamp()}.log"
    cmd = [
        sys.executable,
        str(RUNNER),
        "--db",
        str(db_path.expanduser().resolve()),
        "--frequency",
        "daily",
        "--family",
        family,
        "--step-timeout-sec",
        str(step_timeout_sec),
        "--continue-on-error",
        "--log-file",
        str(child_log),
    ]
    if require_oos_score_valid:
        cmd.append("--require-oos-score-valid")
    for date_value in dates:
        cmd.extend(["--date", date_value])

    log_line(log_path, f"START chunk={dates[0]}..{dates[-1]} count={len(dates)} attempt={attempt} child_log={child_log}")
    completed = subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=False)
    log_line(log_path, f"END chunk={dates[0]}..{dates[-1]} attempt={attempt} returncode={completed.returncode}")
    return int(completed.returncode)


def main() -> int:
    args = parse_args()
    if args.chunk_size < 1:
        raise ValueError("--chunk-size must be >= 1")
    if args.max_retries < 0:
        raise ValueError("--max-retries must be >= 0")

    family = str(args.family).strip()
    family_token = safe_token(family)
    log_dir = args.log_dir.expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{family_token}_daily_supervisor_{args.start_date}_to_latest_{utc_stamp()}.log"
    status_path = (
        args.status_json.expanduser().resolve()
        if args.status_json
        else log_dir / f"{family_token}_daily_supervisor_{args.start_date}_status.json"
    )

    dates = trading_dates(args.db, str(args.start_date), str(args.end_date or ""))
    date_chunks = chunks(dates, int(args.chunk_size))
    status: dict[str, object] = {
        "status": "running",
        "family": family,
        "start_date": args.start_date,
        "end_date": dates[-1] if dates else "",
        "target_dates": len(dates),
        "chunk_size": args.chunk_size,
        "chunks": len(date_chunks),
        "log_path": str(log_path),
    }
    write_status(status_path, status)
    log_line(log_path, f"SUPERVISOR start family={family} dates={len(dates)} chunks={len(date_chunks)} range={args.start_date}..{status['end_date']}")

    if not dates:
        status.update({"status": "complete", "message": "No target dates selected."})
        write_status(status_path, status)
        log_line(log_path, "SUPERVISOR complete no target dates")
        return 0

    for index, date_chunk in enumerate(date_chunks, start=1):
        status.update(
            {
                "status": "running",
                "current_chunk": index,
                "current_chunk_start": date_chunk[0],
                "current_chunk_end": date_chunk[-1],
                "completed_chunks": index - 1,
                "completed_through": date_chunks[index - 2][-1] if index > 1 else "",
            }
        )
        write_status(status_path, status)

        returncode = 1
        for attempt in range(1, int(args.max_retries) + 2):
            returncode = run_chunk(
                family=family,
                family_token=family_token,
                dates=date_chunk,
                db_path=args.db,
                step_timeout_sec=int(args.step_timeout_sec),
                require_oos_score_valid=bool(args.require_oos_score_valid),
                log_path=log_path,
                attempt=attempt,
            )
            if returncode == 0:
                break
            time.sleep(10 * attempt)

        if returncode != 0 and len(date_chunk) > 1:
            log_line(log_path, f"SPLIT chunk={date_chunk[0]}..{date_chunk[-1]} after returncode={returncode}")
            for single_date in date_chunk:
                single_returncode = 1
                for attempt in range(1, int(args.max_retries) + 2):
                    single_returncode = run_chunk(
                        family=family,
                        family_token=family_token,
                        dates=[single_date],
                        db_path=args.db,
                        step_timeout_sec=int(args.step_timeout_sec),
                        require_oos_score_valid=bool(args.require_oos_score_valid),
                        log_path=log_path,
                        attempt=attempt,
                    )
                    if single_returncode == 0:
                        break
                    time.sleep(10 * attempt)
                if single_returncode != 0:
                    status.update({"status": "failed", "failed_date": single_date, "returncode": single_returncode})
                    write_status(status_path, status)
                    log_line(log_path, f"SUPERVISOR failed date={single_date} returncode={single_returncode}")
                    return int(single_returncode)
        elif returncode != 0:
            status.update({"status": "failed", "failed_date": date_chunk[0], "returncode": returncode})
            write_status(status_path, status)
            log_line(log_path, f"SUPERVISOR failed date={date_chunk[0]} returncode={returncode}")
            return int(returncode)

        status.update({"completed_chunks": index, "completed_through": date_chunk[-1]})
        write_status(status_path, status)

    status.update({"status": "complete", "completed_chunks": len(date_chunks), "completed_through": dates[-1]})
    write_status(status_path, status)
    log_line(log_path, f"SUPERVISOR complete family={family} through={dates[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
