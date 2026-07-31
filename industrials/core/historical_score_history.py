from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def benchmark_trading_dates(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    source_id: str,
    start_date: str,
    end_date: str,
) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT bar_date
            FROM fact_price_ohlcv
            WHERE ticker=? AND source_id=?
              AND bar_date>=? AND bar_date<=?
            ORDER BY bar_date
            """,
            (ticker, source_id, start_date, end_date),
        ).fetchall()
        if str(row[0] or "")
    ]


def select_dates(
    dates: Sequence[str],
    *,
    maximum: int = 0,
    selection: str = "oldest",
) -> list[str]:
    ordered = sorted(set(str(value) for value in dates if str(value)))
    if maximum <= 0 or maximum >= len(ordered):
        return ordered
    if selection == "oldest":
        return ordered[:maximum]
    if selection == "newest":
        return ordered[-maximum:]
    raise ValueError(f"unsupported date selection={selection!r}")


def run_logged(
    command: Sequence[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    environment: dict[str, str] | None = None,
) -> None:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        stdout_path.open("w", encoding="utf-8") as stdout_handle,
        stderr_path.open("w", encoding="utf-8") as stderr_handle,
    ):
        subprocess.run(
            list(command),
            cwd=str(cwd),
            env=environment or os.environ.copy(),
            stdout=stdout_handle,
            stderr=stderr_handle,
            check=True,
        )


def valid_score_snapshot(
    *,
    snapshot_dir: Path,
    rank_filename: str,
    sidecar_filename: str,
    rank_manifest_filename: str,
    validation_filename: str,
    scoring_manifest: Path,
    membership_mode: str,
    metric_snapshot_mode: str,
) -> bool:
    rank_path = snapshot_dir / rank_filename
    sidecar_path = snapshot_dir / sidecar_filename
    rank_manifest_path = snapshot_dir / rank_manifest_filename
    validation_path = snapshot_dir / validation_filename
    required = (
        rank_path,
        sidecar_path,
        rank_manifest_path,
        validation_path,
        scoring_manifest,
    )
    if not all(path.is_file() and path.stat().st_size > 0 for path in required):
        return False
    rank_manifest = read_json(rank_manifest_path)
    validation = read_json(validation_path)
    scoring = read_json(scoring_manifest)
    if (
        rank_manifest.get("acceptance") != "PASS"
        or validation.get("acceptance") != "PASS"
        or scoring.get("acceptance") != "PASS"
        or validation.get("membership_mode") != membership_mode
        or scoring.get("membership_mode") != membership_mode
        or scoring.get("metric_snapshot_mode") != metric_snapshot_mode
    ):
        return False
    return (
        str(rank_manifest.get("rank_table_sha256") or "")
        == sha256_file(rank_path)
        and str(
            rank_manifest.get(
                "stage11_survivorship_calibration_panel_sha256"
            )
            or ""
        )
        == sha256_file(sidecar_path)
        and int(validation.get("row_count") or 0)
        == int(validation.get("stage11_sidecar_row_count") or -1)
    )
