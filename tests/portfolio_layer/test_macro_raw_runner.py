from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_runner() -> ModuleType:
    path = PROJECT_ROOT / "portfolio_layer" / "macro" / "20a_run_macro_raw.py"
    spec = importlib.util.spec_from_file_location("portfolio_macro_raw_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_raw_refresh_skips_completed_same_or_later_date() -> None:
    runner = _load_runner()
    needed = runner._raw_refresh_needed
    assert needed("2026-07-24", "2026-07-24") is False
    assert needed("2026-07-25", "2026-07-24") is False


def test_raw_refresh_runs_for_missing_or_older_coverage() -> None:
    runner = _load_runner()
    needed = runner._raw_refresh_needed
    assert needed("", "2026-07-24") is True
    assert needed("2026-07-23", "2026-07-24") is True


def test_raw_qa_passed_requires_completed_pass_for_ingest_run(tmp_path: Path) -> None:
    runner = _load_runner()
    db_path = tmp_path / "macro_raw.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE macro_qa_run (
                qa_run_id TEXT PRIMARY KEY,
                ingest_run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                completed_at_utc TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO macro_qa_run VALUES ('qa-failed', 'run-1', 'failed', '2026-08-01T00:00:00Z')"
        )
        conn.commit()
    assert runner._raw_qa_passed(db_path, "run-1") is False
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO macro_qa_run VALUES ('qa-pass', 'run-1', 'passed', '2026-08-01T00:01:00Z')"
        )
        conn.commit()
    assert runner._raw_qa_passed(db_path, "run-1") is True
    assert runner._raw_qa_passed(db_path, "run-other") is False
