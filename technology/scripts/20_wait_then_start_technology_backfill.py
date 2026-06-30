#!/usr/bin/env python3
"""Wait for one technology backfill status, then launch the next family backfill."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = PROJECT_ROOT / "technology" / "scripts" / "19_supervise_technology_daily_backfill.py"
DEFAULT_DB = Path("C:/Users/josel/Documents/STAGING/DB/technology.sqlite")
DEFAULT_LOG_DIR = PROJECT_ROOT / "output" / "technology_reports" / "historical_backfill" / "logs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for a status JSON to complete, then launch a technology backfill.")
    parser.add_argument("--wait-status-json", type=Path, required=True)
    parser.add_argument("--next-family", required=True)
    parser.add_argument("--next-start-date", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--chunk-size", type=int, default=20)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--step-timeout-sec", type=int, default=900)
    parser.add_argument("--poll-sec", type=int, default=60)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--next-status-json", type=Path, default=None)
    parser.add_argument("--chain-status-json", type=Path, default=None)
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def log_line(path: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def main() -> int:
    args = parse_args()
    if args.poll_sec < 5:
        raise ValueError("--poll-sec must be >= 5")

    log_dir = args.log_dir.expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"technology_backfill_handoff_{utc_stamp()}.log"
    wait_status_path = args.wait_status_json.expanduser().resolve()
    chain_status_path = (
        args.chain_status_json.expanduser().resolve()
        if args.chain_status_json
        else log_dir / "technology_backfill_handoff_status.json"
    )
    next_status_path = (
        args.next_status_json.expanduser().resolve()
        if args.next_status_json
        else log_dir / f"{args.next_family}_daily_supervisor_status.json"
    )

    chain_status: dict[str, Any] = {
        "status": "waiting",
        "wait_status_json": str(wait_status_path),
        "next_family": args.next_family,
        "next_start_date": args.next_start_date,
        "next_status_json": str(next_status_path),
        "log_path": str(log_path),
    }
    write_json(chain_status_path, chain_status)
    log_line(log_path, f"WAIT start wait_status={wait_status_path} next_family={args.next_family} next_start_date={args.next_start_date}")

    while True:
        if wait_status_path.exists():
            payload = read_json(wait_status_path)
            status = str(payload.get("status", "")).lower()
            completed_through = str(payload.get("completed_through", ""))
            chain_status.update(
                {
                    "status": "waiting",
                    "wait_status": status,
                    "wait_completed_through": completed_through,
                    "wait_checked_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
            write_json(chain_status_path, chain_status)
            if status == "complete":
                break
            if status == "failed":
                chain_status.update({"status": "blocked", "reason": "wait_status_failed", "wait_payload": payload})
                write_json(chain_status_path, chain_status)
                log_line(log_path, f"WAIT failed payload={payload}")
                return 1
        else:
            chain_status.update({"status": "waiting", "wait_status": "missing"})
            write_json(chain_status_path, chain_status)
        time.sleep(int(args.poll_sec))

    log_line(log_path, f"WAIT complete; starting next_family={args.next_family}")
    cmd = [
        sys.executable,
        str(SUPERVISOR),
        "--db",
        str(args.db.expanduser().resolve()),
        "--family",
        str(args.next_family),
        "--start-date",
        str(args.next_start_date),
        "--chunk-size",
        str(args.chunk_size),
        "--max-retries",
        str(args.max_retries),
        "--step-timeout-sec",
        str(args.step_timeout_sec),
        "--status-json",
        str(next_status_path),
    ]
    chain_status.update({"status": "running_next", "next_command": " ".join(cmd)})
    write_json(chain_status_path, chain_status)
    completed = subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=False)
    final_status = "complete" if completed.returncode == 0 else "failed"
    chain_status.update({"status": final_status, "next_returncode": int(completed.returncode)})
    write_json(chain_status_path, chain_status)
    log_line(log_path, f"NEXT finished status={final_status} returncode={completed.returncode}")
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
