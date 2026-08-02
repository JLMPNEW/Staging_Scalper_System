#!/usr/bin/env python3
"""Refresh the Staging-owned MacroLayer raw database for a portfolio as-of date."""
from __future__ import annotations

import argparse
import logging
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path  # noqa: E402


LOGGER = logging.getLogger("run_macro_raw")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the vendored Staging MacroLayer raw database.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=iso_date_arg, required=True)
    parser.add_argument("--macro-config", type=Path, default=None)
    parser.add_argument("--python-executable", default=sys.executable)
    return parser.parse_args()


def _macro_raw_db_path(macro_config: Path) -> Path | None:
    """Resolve the vendored raw DB without importing the vendored package."""
    macro_payload = load_yaml(macro_config)
    macro_raw = macro_payload.get("macro_raw", macro_payload)
    if not isinstance(macro_raw, dict):
        return None
    raw_path = str(macro_raw.get("db_path") or "").strip()
    if not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        return ensure_not_prod_path(candidate, label="MacroLayer raw database")
    # MacroLayer resolves its paths from the portfolio_layer package root.
    return ensure_not_prod_path(
        (macro_config.parent.parent / candidate).resolve(),
        label="MacroLayer raw database",
    )


def _latest_completed_raw_run(db_path: Path | None) -> tuple[str, str]:
    if db_path is None or not db_path.is_file():
        return "", ""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=30.0) as conn:
            row: tuple[Any, ...] | None = conn.execute(
                """
                SELECT as_of_date, run_id
                FROM macro_ingest_run
                WHERE status IN ('completed', 'completed_with_errors')
                  AND completed_at_utc IS NOT NULL
                ORDER BY as_of_date DESC, completed_at_utc DESC, rowid DESC
                LIMIT 1
                """
            ).fetchone()
    except sqlite3.Error as exc:
        LOGGER.warning("Could not inspect MacroLayer raw coverage; running refresh: %s", exc)
        return "", ""
    return (str(row[0] or ""), str(row[1] or "")) if row else ("", "")


def _latest_completed_raw_as_of(db_path: Path | None) -> str:
    return _latest_completed_raw_run(db_path)[0]


def _raw_qa_passed(db_path: Path | None, ingest_run_id: str) -> bool:
    if db_path is None or not db_path.is_file() or not ingest_run_id:
        return False
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=30.0) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM macro_qa_run
                WHERE ingest_run_id = ?
                  AND status = 'passed'
                  AND completed_at_utc IS NOT NULL
                ORDER BY completed_at_utc DESC, rowid DESC
                LIMIT 1
                """,
                (ingest_run_id,),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _ensure_raw_qa(
    *,
    vendor_root: Path,
    macro_config: Path,
    db_path: Path | None,
    ingest_run_id: str,
    python_executable: str,
) -> bool:
    if _raw_qa_passed(db_path, ingest_run_id):
        return True
    qa_script = vendor_root / "qa_macro_raw.py"
    if not qa_script.is_file():
        LOGGER.error("Missing MacroLayer raw QA script: %s", qa_script)
        return False
    if not ingest_run_id:
        LOGGER.error("Cannot run MacroLayer raw QA without a completed ingest run id")
        return False
    command = [
        python_executable,
        str(qa_script),
        "--config",
        str(macro_config),
        "--run-id",
        ingest_run_id,
    ]
    LOGGER.info("Running MacroLayer raw QA: %s", subprocess.list2cmdline(command))
    completed = subprocess.run(command, cwd=str(vendor_root), check=False)
    if completed.returncode != 0:
        LOGGER.error("MacroLayer raw QA process failed with exit_code=%s", completed.returncode)
        return False
    if not _raw_qa_passed(db_path, ingest_run_id):
        LOGGER.error("MacroLayer raw QA did not persist a passed result for ingest_run_id=%s", ingest_run_id)
        return False
    return True


def _raw_refresh_needed(latest_completed: str, requested_as_of: str) -> bool:
    """Return whether the raw store lacks the requested PIT date."""
    return not latest_completed or latest_completed < requested_as_of


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    vendor_root = resolve_path(cfg_get(config, "macro.vendor_root", "MacroLayer"), base_dir=config_path.parent)
    vendor_root = ensure_not_prod_path(vendor_root, label="MacroLayer vendor root")
    script = vendor_root / "run_macro_raw_pipeline.py"
    if not script.is_file():
        LOGGER.error("Missing MacroLayer raw pipeline: %s", script)
        return 1
    macro_config = ensure_not_prod_path(
        args.macro_config or (vendor_root / "config_macro_raw.yaml"),
        label="MacroLayer config",
    )
    raw_db_path = _macro_raw_db_path(macro_config)
    latest_completed, latest_run_id = _latest_completed_raw_run(raw_db_path)
    if not _raw_refresh_needed(latest_completed, args.as_of):
        LOGGER.info(
            "Skipping historical raw refresh for %s: completed raw ingest %s already covers it; "
            "the serving DAG will reconstruct the requested PIT date.",
            args.as_of,
            latest_completed,
        )
        return 0 if _ensure_raw_qa(
            vendor_root=vendor_root,
            macro_config=macro_config,
            db_path=raw_db_path,
            ingest_run_id=latest_run_id,
            python_executable=str(args.python_executable),
        ) else 1
    command = [
        str(args.python_executable),
        str(script),
        "--config",
        str(macro_config),
        "--mode",
        "daily",
        "--as-of-date",
        args.as_of,
    ]
    LOGGER.info("Running MacroLayer raw DAG: %s", subprocess.list2cmdline(command))
    completed = subprocess.run(command, cwd=str(vendor_root), check=False)
    if completed.returncode != 0:
        LOGGER.error("MacroLayer raw DAG failed with exit_code=%s", completed.returncode)
        return int(completed.returncode)
    completed_as_of, completed_run_id = _latest_completed_raw_run(raw_db_path)
    if _raw_refresh_needed(completed_as_of, args.as_of):
        LOGGER.error(
            "MacroLayer raw DAG returned success but completed coverage=%s is older than requested=%s",
            completed_as_of or "MISSING",
            args.as_of,
        )
        return 1
    return 0 if _ensure_raw_qa(
        vendor_root=vendor_root,
        macro_config=macro_config,
        db_path=raw_db_path,
        ingest_run_id=completed_run_id,
        python_executable=str(args.python_executable),
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
