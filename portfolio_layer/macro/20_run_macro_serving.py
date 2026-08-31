#!/usr/bin/env python3
"""Stage 6 helper - run the vendored MacroLayer serving DAG safely.

This is a convenience wrapper, not a dependency of the portfolio contract build. It delegates to
portfolio_layer/MacroLayer/run_macro_serving_pipeline.py with the final optimizer integration disabled
so MacroLayer cannot overwrite portfolio-layer Stage 1/3 artifacts.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402


LOGGER = logging.getLogger("run_macro_serving")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

CORE_WATERMARK_TABLES = (
    "macro_calendar_daily",
    "macro_observation_daily_pit",
    "macro_country_coverage_daily",
    "macro_feature_daily",
    "macro_composite_daily",
    "macro_probabilities_daily",
    "macro_probability_v2_daily",
    "macro_regime_raw_daily",
    "macro_regime_smoothed_daily",
    "macro_regime_decision_daily",
    "macro_regime_v2_decision_daily",
)
OVERLAY_WATERMARK_TABLES = (
    "sector_macro_fit_daily",
    "industry_macro_fit_daily",
    "stock_macro_fit_daily",
    "stock_selection_score_daily",
    "portfolio_inputs_daily",
    "stock_weight_score_daily",
    "stock_sleeve_target_summary",
    "foreign_sleeve_budget_daily",
)
WEEKDAY_BY_CADENCE = {
    "W-MON": 0,
    "W-TUE": 1,
    "W-WED": 2,
    "W-THU": 3,
    "W-FRI": 4,
    "W-SAT": 5,
    "W-SUN": 6,
}


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the vendored MacroLayer serving DAG without legacy optimizer writes.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, required=True)
    range_group = p.add_mutually_exclusive_group()
    range_group.add_argument(
        "--start-date",
        type=iso_date_arg,
        default=None,
        help="Explicit incremental build start (default: common serving-table watermark + 1 day).",
    )
    range_group.add_argument(
        "--full-history",
        action="store_true",
        help="Disable automatic incremental serving and rebuild the complete configured history.",
    )
    p.add_argument("--macro-config", type=Path, default=None)
    p.add_argument("--python-executable", type=str, default=sys.executable)
    p.add_argument("--refresh-industry-stock-foreign", action="store_true",
                   help="Also rebuild MacroLayer industry, stock overlay, portfolio inputs, stock sleeves, and foreign budget.")
    p.add_argument(
        "--historical-catchup",
        action="store_true",
        help=(
            "Build production V1 serving state for a historical recovery date without repeatedly "
            "retraining shadow probability candidates. The current target-date run remains "
            "responsible for updating every shadow candidate across the accumulated gap."
        ),
    )
    p.add_argument("--rebuild-policies", action="store_true")
    return p.parse_args()


def _table_watermark(serving_db: Path, tables: tuple[str, ...]) -> date | None:
    """Return the oldest table watermark, or None when a full build is required."""
    if not serving_db.exists():
        return None
    try:
        with sqlite3.connect(serving_db) as conn:
            existing = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            watermarks: list[date] = []
            for table in tables:
                if table not in existing:
                    return None
                raw = conn.execute(
                    f'SELECT MAX(as_of_date) FROM "{table}"'
                ).fetchone()[0]
                if raw is None:
                    return None
                watermarks.append(date.fromisoformat(str(raw)))
    except (OSError, sqlite3.Error, ValueError) as exc:
        LOGGER.warning(
            "Could not establish a safe MacroLayer watermark; using full history: %s",
            exc,
        )
        return None
    return min(watermarks) if watermarks else None


def common_serving_watermark(
    serving_db: Path,
    *,
    include_overlays: bool,
) -> date | None:
    tables = CORE_WATERMARK_TABLES + (
        OVERLAY_WATERMARK_TABLES if include_overlays else ()
    )
    return _table_watermark(serving_db, tables)


def exact_date_tables_complete(
    serving_db: Path,
    tables: tuple[str, ...],
    *,
    as_of: str,
) -> bool:
    """Return True only when every required table contains the requested date."""
    if not serving_db.exists() or not tables:
        return False
    try:
        date.fromisoformat(as_of)
        with sqlite3.connect(serving_db) as conn:
            existing = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            for table in tables:
                if table not in existing:
                    return False
                present = conn.execute(
                    f'SELECT 1 FROM "{table}" WHERE as_of_date = ? LIMIT 1',
                    (as_of,),
                ).fetchone()
                if present is None:
                    return False
    except (OSError, sqlite3.Error, ValueError) as exc:
        LOGGER.warning(
            "Could not verify exact-date MacroLayer completeness for %s: %s",
            as_of,
            exc,
        )
        return False
    return True


def latest_cadence_date(*, as_of: date, cadence: str) -> date:
    """Return the latest configured weekly observation date on or before as_of."""
    normalized = str(cadence).strip().upper()
    try:
        target_weekday = WEEKDAY_BY_CADENCE[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported MacroLayer overlay cadence {cadence!r}") from exc
    return as_of - timedelta(days=(as_of.weekday() - target_weekday) % 7)


def overlay_refresh_range(
    *,
    serving_db: Path,
    as_of: str,
    cadence: str,
    requested: bool,
    explicit_start: str | None,
    full_history: bool,
    rebuild_policies: bool,
) -> tuple[str | None, str | None]:
    """Resolve a cadence-specific range, or (None, None) when no overlay is due."""
    if not requested:
        return None, None
    end = latest_cadence_date(as_of=date.fromisoformat(as_of), cadence=cadence)
    watermark = _table_watermark(serving_db, OVERLAY_WATERMARK_TABLES)
    if not full_history and not rebuild_policies and watermark is not None and watermark >= end:
        return None, None
    start = date.fromisoformat(explicit_start) if explicit_start else None
    if start is None and not full_history and not rebuild_policies and watermark is not None:
        start = watermark + timedelta(days=1)
    if start is not None and start > end:
        return None, None
    return (start.isoformat() if start is not None else None, end.isoformat())

def incremental_start_date(
    *,
    serving_db: Path,
    as_of: str,
    include_overlays: bool,
    explicit_start: str | None,
    full_history: bool,
    rebuild_policies: bool,
) -> str | None:
    end = date.fromisoformat(as_of)
    if explicit_start is not None:
        start = date.fromisoformat(explicit_start)
        if start > end:
            raise ValueError(f"MacroLayer start date {start} is after end date {end}")
        return start.isoformat()
    if full_history or rebuild_policies:
        return None
    watermark = common_serving_watermark(
        serving_db,
        include_overlays=include_overlays,
    )
    if watermark is None:
        return None
    # Recompute the requested day when the DB is already current. This keeps
    # same-date restatements visible without rescanning the complete history.
    return min(watermark + timedelta(days=1), end).isoformat()


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    vendor_root = resolve_path(cfg_get(config, "macro.vendor_root", "MacroLayer"), base_dir=config_path.parent)
    vendor_root = ensure_not_prod_path(vendor_root, label="MacroLayer vendor root")
    script = vendor_root / "run_macro_serving_pipeline.py"
    if not script.exists():
        LOGGER.error("Missing MacroLayer serving wrapper: %s", script)
        return 1
    macro_config = args.macro_config or (vendor_root / "config_macro_raw.yaml")
    macro_config = ensure_not_prod_path(macro_config, label="MacroLayer config")
    macro_cfg = load_yaml(macro_config)
    overlay_cadence = str(
        cfg_get(
            macro_cfg,
            "macro_raw.serving.industry_macro_layer.cadence",
            "W-FRI",
        )
    )
    serving_db = ensure_not_prod_path(paths.macro_serving_db_path, label="macro serving db")
    try:
        start_date = incremental_start_date(
            serving_db=serving_db,
            as_of=args.as_of,
            include_overlays=False,
            explicit_start=args.start_date,
            full_history=args.full_history,
            rebuild_policies=args.rebuild_policies,
        )
        overlay_start_date, overlay_end_date = overlay_refresh_range(
            serving_db=serving_db,
            as_of=args.as_of,
            cadence=overlay_cadence,
            requested=args.refresh_industry_stock_foreign,
            explicit_start=args.start_date,
            full_history=args.full_history,
            rebuild_policies=args.rebuild_policies,
        )
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2

    reuse_historical_core = (
        args.historical_catchup
        and args.start_date is None
        and not args.full_history
        and not args.rebuild_policies
        and overlay_end_date is None
        and exact_date_tables_complete(
            serving_db,
            CORE_WATERMARK_TABLES,
            as_of=args.as_of,
        )
    )
    if reuse_historical_core:
        LOGGER.info(
            "MacroLayer exact-date serving state is complete for historical catch-up %s; "
            "reusing sealed DB rows because no overlay refresh is due.",
            args.as_of,
        )
        return 0

    cmd = [
        str(args.python_executable),
        str(script),
        "--config",
        str(macro_config),
        "--serving-db-path",
        str(serving_db),
        "--end-date",
        args.as_of,
        "--skip-final-optimizer",
        "--allow-shadow-failures",
    ]
    if start_date is not None:
        cmd.extend(["--start-date", start_date])
        LOGGER.info(
            "MacroLayer incremental serving range: %s..%s",
            start_date,
            args.as_of,
        )
    else:
        LOGGER.info("MacroLayer full-history serving requested or required")
    if args.rebuild_policies:
        cmd.append("--rebuild-policies")
    if args.historical_catchup:
        cmd.extend(
            [
                "--skip-probabilities-v2",
                "--skip-probabilities-v2-1",
                "--skip-probabilities-v2-2",
                "--skip-probabilities-v2-3",
                "--skip-probabilities-h1",
            ]
        )
        LOGGER.info(
            "Historical catch-up: production V1 macro serving remains active; "
            "shadow V2/V2.1/V2.2/V2.3/H1 candidates defer to the current-date run."
        )
    if overlay_end_date is not None:
        if overlay_start_date is not None:
            cmd.extend(["--overlay-start-date", overlay_start_date])
        cmd.extend(["--overlay-end-date", overlay_end_date])
        LOGGER.info(
            "MacroLayer %s overlay range: %s..%s",
            overlay_cadence,
            overlay_start_date or "full-history",
            overlay_end_date,
        )
    else:
        if args.refresh_industry_stock_foreign:
            LOGGER.info(
                "MacroLayer %s overlays are already current for %s; skipping overlay rebuild.",
                overlay_cadence,
                args.as_of,
            )
        cmd.extend([
            "--skip-industry-macro",
            "--skip-stock-macro-overlay",
            "--skip-portfolio-inputs",
            "--skip-stock-sleeve-targets",
            "--skip-foreign-sleeve-budget",
        ])
    LOGGER.info("Running MacroLayer serving DAG: %s", subprocess.list2cmdline(cmd))
    try:
        subprocess.run(cmd, cwd=str(vendor_root), check=True)
    except subprocess.CalledProcessError as exc:
        LOGGER.error("MacroLayer serving DAG failed with exit_code=%s", exc.returncode)
        return int(exc.returncode or 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
