#!/usr/bin/env python3
"""Build technology financial features in sequential, recoverable batches."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
BUILDER = PACKAGE_ROOT / "scripts" / "08_build_technology_financial_features.py"
SUPPORTED_MODEL_FAMILIES = frozenset(
    {"semiconductors", "software_infrastructure", "technology_hardware"}
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", required=True)
    parser.add_argument("--tickers", default="")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--batch-timeout-sec", type=float, default=1800.0)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--work-dir", type=Path, default=None)
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chunks(values: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("batch-size must be positive")
    return [values[index : index + size] for index in range(0, len(values), size)]


def default_output_csv(config: dict[str, Any], *, base_dir: Path, model_family: str) -> Path:
    if model_family == "semiconductors":
        return resolve_path(cfg_get(config, "sec_fundamentals.feature_output_csv"), base_dir=base_dir)
    return PROJECT_ROOT / "output" / "technology_reports" / model_family / "sec_fundamentals" / "sec_financial_feature_coverage.csv"


def load_tickers(
    db_path: Path,
    *,
    model_family: str,
    ticker_filter: set[str],
    include_historical: bool,
) -> list[str]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if include_historical:
            rows = conn.execute(
                """
                SELECT DISTINCT c.ticker
                FROM dim_company c
                JOIN dim_universe_membership m
                  ON m.ticker = c.ticker
                 AND m.model_family = ?
                 AND (m.is_current_member = 1 OR m.point_in_time_flag = 1)
                ORDER BY c.ticker
                """,
                (model_family,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT c.ticker
                FROM dim_company c
                JOIN dim_technology_taxonomy t
                  ON t.ticker = c.ticker
                 AND t.model_family = ?
                WHERE c.is_active = 1
                ORDER BY c.ticker
                """,
                (model_family,),
            ).fetchall()
    tickers = [normalize_ticker(row["ticker"]) for row in rows]
    tickers = [ticker for ticker in tickers if ticker]
    return [ticker for ticker in tickers if not ticker_filter or ticker in ticker_filter]


def run_batch(
    tickers: list[str],
    *,
    config_path: Path,
    db_path: Path,
    model_family: str,
    output_csv: Path,
    log_path: Path,
    timeout_sec: float,
) -> tuple[int, str]:
    command = [
        sys.executable,
        str(BUILDER),
        "--config",
        str(config_path),
        "--db",
        str(db_path),
        "--model-family",
        model_family,
        "--tickers",
        ",".join(tickers),
        "--output-csv",
        str(output_csv),
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", newline="") as log:
        log.write(f"started_utc={utc_now()}\ncommand={' '.join(command)}\n")
        log.flush()
        try:
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.write(f"\nTIMEOUT after {timeout_sec:.1f} seconds\n")
            return 124, "timeout"
    return int(result.returncode), "success" if result.returncode == 0 else "process_failed"


def read_report(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def write_report_atomic(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    if not fields:
        raise ValueError("Cannot publish a financial-feature report without a schema")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
    temp_path.replace(path)


def append_report(
    path: Path,
    *,
    fields: list[str],
    rows: list[dict[str, str]],
) -> list[str]:
    batch_fields, batch_rows = read_report(path)
    for field in batch_fields:
        if field not in fields:
            fields.append(field)
    rows.extend(batch_rows)
    return batch_fields


def record_outcomes(
    outcomes: list[dict[str, Any]],
    tickers: list[str],
    *,
    status: str,
    return_code: int,
    report_path: Path,
    log_path: Path,
) -> None:
    for ticker in tickers:
        outcomes.append(
            {
                "ticker": ticker,
                "status": status,
                "return_code": return_code,
                "report_path": str(report_path),
                "log_path": str(log_path),
            }
        )


def main() -> int:
    args = parse_args()
    if args.batch_size <= 0:
        raise ValueError("batch-size must be positive")
    if args.batch_timeout_sec <= 0:
        raise ValueError("batch-timeout-sec must be positive")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=base_dir
    )
    model_family = str(args.model_family or "").strip()
    if model_family not in SUPPORTED_MODEL_FAMILIES:
        raise ValueError(
            f"Unsupported model-family {model_family!r}; expected one of {sorted(SUPPORTED_MODEL_FAMILIES)}"
        )
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else default_output_csv(
        config, base_dir=base_dir, model_family=model_family
    )
    run_id = datetime.now(timezone.utc).strftime("financial_features_%Y%m%dT%H%M%SZ")
    work_dir = args.work_dir.expanduser().resolve() if args.work_dir else (
        output_csv.parent / "batch_runs" / run_id
    )
    work_dir.mkdir(parents=True, exist_ok=False)

    ticker_filter = {normalize_ticker(value) for value in args.tickers.split(",") if normalize_ticker(value)}
    include_historical = str(
        cfg_get(config, "sec_fundamentals.include_historical_members", True)
    ).strip().lower() in {"1", "true", "yes", "y"}
    tickers = load_tickers(
        db_path,
        model_family=model_family,
        ticker_filter=ticker_filter,
        include_historical=include_historical,
    )
    if not tickers:
        raise ValueError(f"No financial-feature tickers found for model_family={model_family}")

    fields: list[str] = []
    report_rows: list[dict[str, str]] = []
    outcomes: list[dict[str, Any]] = []
    failed_tickers: list[str] = []
    for batch_number, batch in enumerate(chunks(tickers, args.batch_size), start=1):
        stem = f"batch_{batch_number:03d}"
        batch_csv = work_dir / f"{stem}.csv"
        batch_log = work_dir / f"{stem}.log"
        return_code, status = run_batch(
            batch,
            config_path=config_path,
            db_path=db_path,
            model_family=model_family,
            output_csv=batch_csv,
            log_path=batch_log,
            timeout_sec=args.batch_timeout_sec,
        )
        if return_code == 0:
            if not append_report(batch_csv, fields=fields, rows=report_rows):
                status = "missing_report_schema"
                return_code = 1
            else:
                record_outcomes(
                    outcomes,
                    batch,
                    status=status,
                    return_code=return_code,
                    report_path=batch_csv,
                    log_path=batch_log,
                )
                continue

        for retry_number, ticker in enumerate(batch, start=1):
            retry_stem = f"{stem}_retry_{retry_number:02d}_{ticker}"
            retry_csv = work_dir / f"{retry_stem}.csv"
            retry_log = work_dir / f"{retry_stem}.log"
            retry_code, retry_status = run_batch(
                [ticker],
                config_path=config_path,
                db_path=db_path,
                model_family=model_family,
                output_csv=retry_csv,
                log_path=retry_log,
                timeout_sec=args.batch_timeout_sec,
            )
            if retry_code == 0 and append_report(retry_csv, fields=fields, rows=report_rows):
                record_outcomes(
                    outcomes,
                    [ticker],
                    status="recovered_after_batch_failure",
                    return_code=retry_code,
                    report_path=retry_csv,
                    log_path=retry_log,
                )
            else:
                failed_tickers.append(ticker)
                record_outcomes(
                    outcomes,
                    [ticker],
                    status=retry_status if retry_code else "missing_report_schema",
                    return_code=retry_code or 1,
                    report_path=retry_csv,
                    log_path=retry_log,
                )

    manifest = {
        "run_id": run_id,
        "generated_at_utc": utc_now(),
        "model_family": model_family,
        "database_path": str(db_path),
        "output_csv": str(output_csv),
        "ticker_count": len(tickers),
        "batch_size": args.batch_size,
        "batch_timeout_sec": args.batch_timeout_sec,
        "failed_tickers": sorted(set(failed_tickers)),
        "outcomes": outcomes,
    }
    manifest_path = work_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if failed_tickers:
        print(f"Financial feature build failed for {len(set(failed_tickers))} ticker(s): {sorted(set(failed_tickers))}")
        print(f"Manifest: {manifest_path}")
        return 1

    write_report_atomic(output_csv, fields, report_rows)
    print(
        f"Financial feature batches complete: family={model_family} "
        f"tickers={len(tickers)} report_rows={len(report_rows)} output={output_csv}"
    )
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
