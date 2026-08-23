from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_runner() -> ModuleType:
    script = PROJECT_ROOT / "portfolio_layer" / "macro" / "20_run_macro_serving.py"
    spec = importlib.util.spec_from_file_location("macro_serving_incremental_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_tables(
    db_path: Path,
    tables: tuple[str, ...],
    *,
    watermark: str,
    stale_table: str | None = None,
    stale_watermark: str = "",
) -> None:
    with sqlite3.connect(db_path) as conn:
        for table in tables:
            conn.execute(f'CREATE TABLE "{table}" (as_of_date TEXT NOT NULL)')
            value = stale_watermark if table == stale_table else watermark
            conn.execute(f'INSERT INTO "{table}" VALUES (?)', (value,))


def test_macro_incremental_start_uses_oldest_required_watermark(tmp_path: Path) -> None:
    module = _load_runner()
    db_path = tmp_path / "macro.sqlite"
    tables = module.CORE_WATERMARK_TABLES + module.OVERLAY_WATERMARK_TABLES
    _seed_tables(
        db_path,
        tables,
        watermark="2026-08-17",
        stale_table=module.OVERLAY_WATERMARK_TABLES[0],
        stale_watermark="2026-08-16",
    )

    assert module.incremental_start_date(
        serving_db=db_path,
        as_of="2026-08-19",
        include_overlays=True,
        explicit_start=None,
        full_history=False,
        rebuild_policies=False,
    ) == "2026-08-17"


def test_macro_incremental_start_recomputes_current_day(tmp_path: Path) -> None:
    module = _load_runner()
    db_path = tmp_path / "macro.sqlite"
    _seed_tables(
        db_path,
        module.CORE_WATERMARK_TABLES,
        watermark="2026-08-19",
    )

    assert module.incremental_start_date(
        serving_db=db_path,
        as_of="2026-08-19",
        include_overlays=False,
        explicit_start=None,
        full_history=False,
        rebuild_policies=False,
    ) == "2026-08-19"


def test_macro_incremental_start_falls_back_closed_when_table_missing(
    tmp_path: Path,
) -> None:
    module = _load_runner()
    db_path = tmp_path / "macro.sqlite"
    _seed_tables(
        db_path,
        module.CORE_WATERMARK_TABLES[:-1],
        watermark="2026-08-17",
    )

    assert module.incremental_start_date(
        serving_db=db_path,
        as_of="2026-08-19",
        include_overlays=False,
        explicit_start=None,
        full_history=False,
        rebuild_policies=False,
    ) is None


def test_policy_rebuild_disables_incremental_range(tmp_path: Path) -> None:
    module = _load_runner()

    assert module.incremental_start_date(
        serving_db=tmp_path / "missing.sqlite",
        as_of="2026-08-19",
        include_overlays=True,
        explicit_start=None,
        full_history=False,
        rebuild_policies=True,
    ) is None

def test_overlay_range_skips_between_weekly_cadence_dates(tmp_path: Path) -> None:
    module = _load_runner()
    db_path = tmp_path / "macro.sqlite"
    _seed_tables(
        db_path,
        module.OVERLAY_WATERMARK_TABLES,
        watermark="2026-08-17",
    )

    assert module.overlay_refresh_range(
        serving_db=db_path,
        as_of="2026-08-19",
        cadence="W-FRI",
        requested=True,
        explicit_start=None,
        full_history=False,
        rebuild_policies=False,
    ) == (None, None)


def test_overlay_range_advances_on_next_weekly_cadence_date(tmp_path: Path) -> None:
    module = _load_runner()
    db_path = tmp_path / "macro.sqlite"
    _seed_tables(
        db_path,
        module.OVERLAY_WATERMARK_TABLES,
        watermark="2026-08-17",
    )

    assert module.overlay_refresh_range(
        serving_db=db_path,
        as_of="2026-08-21",
        cadence="W-FRI",
        requested=True,
        explicit_start=None,
        full_history=False,
        rebuild_policies=False,
    ) == ("2026-08-18", "2026-08-21")


def test_overlay_range_catches_up_missed_weekly_date(tmp_path: Path) -> None:
    module = _load_runner()
    db_path = tmp_path / "macro.sqlite"
    _seed_tables(
        db_path,
        module.OVERLAY_WATERMARK_TABLES,
        watermark="2026-08-07",
    )

    assert module.overlay_refresh_range(
        serving_db=db_path,
        as_of="2026-08-19",
        cadence="W-FRI",
        requested=True,
        explicit_start=None,
        full_history=False,
        rebuild_policies=False,
    ) == ("2026-08-08", "2026-08-14")
