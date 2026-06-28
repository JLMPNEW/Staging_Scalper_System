#!/usr/bin/env python3
"""Run daily med-device historical snapshots in small resumable chunks."""
from __future__ import annotations

import argparse
import csv
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.market_policy import scoring_market_sources  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_REQUIRED_REVIEW_PACK_FILES = (
    "med_device_daily_composite_scores.csv",
    "med_device_score_review_portfolio_candidates.csv",
    "med_device_score_review_tier1.csv",
    "med_device_score_review_safe_core.csv",
    "med_device_score_review_calibrated_baseline.csv",
)


@dataclass(frozen=True)
class Chunk:
    start_asof: str
    end_asof: str
    asofs: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily historical med-device snapshots in resumable chunks.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-asof", default="")
    parser.add_argument("--end-asof", default="")
    parser.add_argument("--chunk-size", type=int, default=7, help="Market dates per script-21 child run.")
    parser.add_argument("--resume", action="store_true", help="Skip chunks whose dated review-pack folders are complete.")
    parser.add_argument("--stop-after-chunks", type=int, default=0)
    parser.add_argument("--no-run-setup", action="store_true", help="Passed through to script 21 for every child chunk.")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def parse_date_text(raw: object) -> str:
    text = str(raw or "").strip()[:10]
    datetime.strptime(text, "%Y-%m-%d")
    return text


def latest_market_date_on_or_before(market_dates: list[str], target: str) -> str:
    selected = ""
    for item in market_dates:
        if item <= target:
            selected = item
        else:
            break
    if not selected:
        raise ValueError(f"No market date found on or before {target}.")
    return selected


def load_market_dates(db_path: Path, *, sources: list[str], benchmark_tickers: list[str]) -> list[str]:
    if not sources:
        raise ValueError("No scoring market sources configured.")
    tickers = [str(item or "").strip().upper() for item in benchmark_tickers if str(item or "").strip()]
    if not tickers:
        raise ValueError("med_devices_universe.benchmark_tickers is required for market-date discovery.")
    date_set: set[str] = set()
    with sqlite3.connect(db_path.expanduser().resolve()) as conn:
        conn.row_factory = sqlite3.Row
        for ticker in tickers:
            for source in sources:
                rows = conn.execute(
                    """
                    SELECT bar_date
                    FROM fact_price_ohlcv
                    WHERE ticker = ?
                      AND source_id = ?
                      AND COALESCE(adj_close, close) > 0
                    """,
                    (ticker, source),
                ).fetchall()
                for row in rows:
                    text = str(row["bar_date"] or "")[:10]
                    try:
                        parse_date_text(text)
                    except ValueError:
                        continue
                    date_set.add(text)
    return sorted(date_set)


def make_chunks(asofs: list[str], *, chunk_size: int) -> list[Chunk]:
    if chunk_size <= 0:
        raise ValueError("--chunk-size must be positive.")
    chunks: list[Chunk] = []
    for idx in range(0, len(asofs), chunk_size):
        items = tuple(asofs[idx : idx + chunk_size])
        if items:
            chunks.append(Chunk(start_asof=items[0], end_asof=items[-1], asofs=items))
    return chunks


def review_pack_complete(review_pack_base_dir: Path, asof: str) -> bool:
    output_dir = review_pack_base_dir / asof
    return all((output_dir / name).exists() and (output_dir / name).stat().st_size > 0 for name in DEFAULT_REQUIRED_REVIEW_PACK_FILES)


def chunk_complete(review_pack_base_dir: Path, chunk: Chunk) -> bool:
    return all(review_pack_complete(review_pack_base_dir, asof) for asof in chunk.asofs)


def append_manifest(path: Path, row: dict[str, Any]) -> None:
    fieldnames = ["chunk_index", "start_asof", "end_asof", "asof_count", "status", "started_at", "ended_at", "log_path", "message"]
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_chunk(
    *,
    chunk: Chunk,
    chunk_index: int,
    total_chunks: int,
    config_path: Path,
    db_path: Path | None,
    log_dir: Path,
    no_run_setup: bool,
) -> tuple[int, Path]:
    log_path = log_dir / f"daily_snapshot_chunk_{chunk.start_asof}_{chunk.end_asof}.log"
    command = [
        sys.executable,
        str(PACKAGE_ROOT / "scripts" / "21_backfill_med_device_historical_scores.py"),
        "--config",
        str(config_path),
        "--start-asof",
        chunk.start_asof,
        "--end-asof",
        chunk.end_asof,
        "--no-run-backtest",
        "--no-run-calibration",
    ]
    if no_run_setup:
        command.append("--no-run-setup")
    if db_path is not None:
        command.extend(["--db", str(db_path)])
    print(
        f"chunk {chunk_index}/{total_chunks} start={chunk.start_asof} end={chunk.end_asof} "
        f"asofs={len(chunk.asofs)} log={log_path}",
        flush=True,
    )
    with log_path.open("w", encoding="utf-8") as log_handle:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return int(completed.returncode), log_path


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    start_asof = parse_date_text(args.start_asof or cfg_get(config, "historical_backfill.start_asof", "2019-01-04"))
    end_raw = str(args.end_asof or cfg_get(config, "historical_backfill.end_asof", "latest_market_date")).strip()
    output_base = resolve_path(cfg_get(config, "paths.output_dir", "../output/med_devices_reports"), base_dir=base_dir)
    review_pack_base_dir = resolve_path(
        cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"),
        base_dir=base_dir,
    )
    log_dir = args.output_dir.expanduser().resolve() if args.output_dir else output_base / "historical_backfill" / "daily_chunks"

    market_dates = load_market_dates(
        db_path,
        sources=scoring_market_sources(config),
        benchmark_tickers=list(cfg_get(config, "med_devices_universe.benchmark_tickers", []) or []),
    )
    end_asof = latest_market_date_on_or_before(market_dates, "9999-12-31") if end_raw.lower() in {"latest", "latest_market_date"} else parse_date_text(end_raw)
    manifest_path = log_dir / f"daily_snapshot_chunk_manifest_{start_asof}_{end_asof}.csv"
    asofs = [item for item in market_dates if start_asof <= item <= end_asof]
    chunks = make_chunks(asofs, chunk_size=int(args.chunk_size))
    print(f"planned_daily_asofs={len(asofs)} chunks={len(chunks)} start={start_asof} end={asofs[-1] if asofs else ''}", flush=True)

    completed_chunks = 0
    for idx, chunk in enumerate(chunks, start=1):
        started_at = utc_timestamp()
        if args.resume and chunk_complete(review_pack_base_dir, chunk):
            append_manifest(
                manifest_path,
                {
                    "chunk_index": idx,
                    "start_asof": chunk.start_asof,
                    "end_asof": chunk.end_asof,
                    "asof_count": len(chunk.asofs),
                    "status": "skipped_complete",
                    "started_at": started_at,
                    "ended_at": utc_timestamp(),
                    "log_path": "",
                    "message": "all_required_review_pack_files_present",
                },
            )
            completed_chunks += 1
            continue
        returncode, log_path = run_chunk(
            chunk=chunk,
            chunk_index=idx,
            total_chunks=len(chunks),
            config_path=config_path,
            db_path=db_path,
            log_dir=log_dir,
            no_run_setup=bool(args.no_run_setup),
        )
        status = "success" if returncode == 0 and chunk_complete(review_pack_base_dir, chunk) else "failed"
        message = f"returncode={returncode}"
        append_manifest(
            manifest_path,
            {
                "chunk_index": idx,
                "start_asof": chunk.start_asof,
                "end_asof": chunk.end_asof,
                "asof_count": len(chunk.asofs),
                "status": status,
                "started_at": started_at,
                "ended_at": utc_timestamp(),
                "log_path": str(log_path),
                "message": message,
            },
        )
        if status != "success":
            print(f"chunk_failed index={idx} start={chunk.start_asof} end={chunk.end_asof} {message} log={log_path}", flush=True)
            return 1
        completed_chunks += 1
        if args.stop_after_chunks > 0 and completed_chunks >= args.stop_after_chunks:
            print(f"stopped_after_chunks={completed_chunks} manifest={manifest_path}", flush=True)
            return 0
    print(f"daily_snapshot_chunks_complete chunks={completed_chunks} manifest={manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
