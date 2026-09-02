#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from sec_form4_config import cfg_get, load_sec_form4_config

try:
    from datetime import UTC as utc_tz
except ImportError:  # pragma: no cover - Python < 3.11 compatibility.
    from datetime import timezone as _timezone

    utc_tz = _timezone.utc

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config_sec_form4.yaml"
DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\STAGING\DB\sec_insider.sqlite")


def parse_iso_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}'. Expected YYYY-MM-DD.") from exc


def previous_or_same_business_day(d: date) -> date:
    out = d
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def bdays_between(start: date, end: date) -> list[date]:
    out: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def weekly_fridays_between(start: date, end: date) -> list[date]:
    out: list[date] = []
    cur = start
    while cur.weekday() != 4:
        cur += timedelta(days=1)
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=7)
    return out


def subtract_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year - years)


def load_existing_asof_dates(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000;")
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_signal_snapshot_tier1' LIMIT 1"
        ).fetchone()
        if row is None:
            return set()
        rows = conn.execute("SELECT DISTINCT as_of_date FROM stock_signal_snapshot_tier1").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {str(r[0]) for r in rows if r and r[0]}


def run_update_first(*, py_exe: str, update_script: Path, config_path: Path, mode: str) -> None:
    cmd = [py_exe, str(update_script), "--config", str(config_path)]
    if mode:
        cmd.extend(["--mode", mode])
    subprocess.run(cmd, check=True)


def run_build_for_date(
    *,
    py_exe: str,
    build_script: Path,
    config_path: Path,
    db_path: Path,
    as_of_date: date,
    refresh_legacy_buy_table: bool,
) -> None:
    cmd = [
        py_exe,
        str(build_script),
        "--config",
        str(config_path),
        "--db-path",
        str(db_path),
        "--as-of-date",
        as_of_date.isoformat(),
    ]
    if refresh_legacy_buy_table:
        cmd.append("--refresh-legacy-buy-table")
    else:
        cmd.append("--no-refresh-legacy-buy-table")
    subprocess.run(cmd, check=True)


def run_reports(*, py_exe: str, report_script: Path, config_path: Path, db_path: Path) -> None:
    cmd = [
        py_exe,
        str(report_script),
        "--config",
        str(config_path),
        "--db-path",
        str(db_path),
    ]
    subprocess.run(cmd, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build Form 4 historical stock_signal_snapshot_tier1 as-of snapshots. "
            "Daily cadence uses business days; weekly cadence uses Fridays."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to Form 4 YAML config.",
    )
    parser.add_argument("--db-path", type=Path, default=None, help="Override Form 4 SQLite DB path.")
    parser.add_argument(
        "--cadence",
        choices=["daily", "weekly", "both"],
        default="both",
        help="Snapshot cadence to build.",
    )
    parser.add_argument("--end-date", type=str, default=None, help="End date YYYY-MM-DD (default: today UTC).")
    parser.add_argument(
        "--daily-start-date",
        type=str,
        default=None,
        help="Daily cadence start date YYYY-MM-DD (overrides --daily-lookback-days).",
    )
    parser.add_argument(
        "--weekly-start-date",
        type=str,
        default=None,
        help="Weekly cadence start date YYYY-MM-DD (overrides --weekly-lookback-years).",
    )
    parser.add_argument(
        "--daily-lookback-days",
        type=int,
        default=30,
        help="Daily cadence business-day lookback when --daily-start-date is not provided.",
    )
    parser.add_argument(
        "--weekly-lookback-years",
        type=int,
        default=7,
        help="Weekly cadence lookback years when --weekly-start-date is not provided.",
    )
    parser.add_argument(
        "--skip-existing",
        dest="skip_existing",
        action="store_true",
        help="Skip as_of dates already present in stock_signal_snapshot_tier1.",
    )
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="Regenerate as_of dates even when they already exist in stock_signal_snapshot_tier1.",
    )
    parser.set_defaults(skip_existing=False)
    parser.add_argument(
        "--max-dates",
        type=int,
        default=0,
        help="Optional cap on number of dates to build after filtering (0 = no cap).",
    )
    parser.add_argument(
        "--date-order",
        choices=["newest", "oldest"],
        default="oldest",
        help="Build newest dates first or oldest dates first.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue building remaining dates if one date fails.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned dates and exit.")
    parser.add_argument(
        "--run-daily-update-first",
        action="store_true",
        help="Run update_sec_form4_daily.py once before the historical snapshot loop.",
    )
    parser.add_argument(
        "--update-mode",
        choices=["", "daily", "weekly"],
        default="",
        help="Mode passed to update_sec_form4_daily.py when --run-daily-update-first is used.",
    )
    parser.add_argument(
        "--refresh-legacy-buy-table",
        action="store_true",
        help="Refresh form4_buy_events_v1 on each date build (slower).",
    )
    parser.add_argument(
        "--no-refresh-legacy-buy-table",
        dest="refresh_legacy_buy_table",
        action="store_false",
        help="Skip refreshing form4_buy_events_v1 on each date build.",
    )
    parser.set_defaults(refresh_legacy_buy_table=False)
    parser.add_argument(
        "--run-reports-at-end",
        dest="run_reports_at_end",
        action="store_true",
        help="Run report_form4_buy_events_v1.py once after all successful builds.",
    )
    parser.add_argument(
        "--no-run-reports-at-end",
        dest="run_reports_at_end",
        action="store_false",
        help="Skip report_form4_buy_events_v1.py after historical builds.",
    )
    parser.set_defaults(run_reports_at_end=False)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path, cfg = load_sec_form4_config(args.config)
    config_path = config_path.expanduser().resolve()

    db_path = Path(
        args.db_path if args.db_path is not None else cfg_get(cfg, "db_path", default=str(DEFAULT_DB_PATH))
    ).expanduser()
    end_date = parse_iso_date(args.end_date) or datetime.now(utc_tz).date()
    end_date = previous_or_same_business_day(end_date)

    dates_daily: list[date] = []
    dates_weekly: list[date] = []
    if args.cadence in {"daily", "both"}:
        if args.daily_start_date:
            daily_start = parse_iso_date(args.daily_start_date)
            assert daily_start is not None
        else:
            daily_start = end_date - timedelta(days=max(int(args.daily_lookback_days), 1) * 2)
            daily_candidates = bdays_between(daily_start, end_date)
            dates_daily = daily_candidates[-max(int(args.daily_lookback_days), 1) :]
        if not dates_daily:
            dates_daily = bdays_between(daily_start, end_date)

    if args.cadence in {"weekly", "both"}:
        if args.weekly_start_date:
            weekly_start = parse_iso_date(args.weekly_start_date)
            assert weekly_start is not None
        else:
            weekly_start = subtract_years(end_date, max(int(args.weekly_lookback_years), 1))
        dates_weekly = weekly_fridays_between(weekly_start, end_date)

    all_dates = sorted(set(dates_daily + dates_weekly))
    if not all_dates:
        print("No as_of dates selected. Nothing to run.")
        return

    if args.skip_existing:
        existing = load_existing_asof_dates(db_path)
        all_dates = [d for d in all_dates if d.isoformat() not in existing]

    if args.max_dates and args.max_dates > 0:
        all_dates = all_dates[-args.max_dates :] if args.date_order == "newest" else all_dates[: args.max_dates]

    all_dates = sorted(all_dates, reverse=args.date_order == "newest")

    if not all_dates:
        print("All selected as_of dates already exist. Nothing to run.")
        return

    oldest = min(all_dates)
    newest = max(all_dates)
    print(f"DB path: {db_path}")
    print(f"Total as_of dates queued: {len(all_dates):,}")
    print(f"Range: {oldest.isoformat()} -> {newest.isoformat()}")
    if args.dry_run:
        return

    py_exe = sys.executable
    helper_dir = Path(__file__).resolve().parent
    update_script = helper_dir / "update_sec_form4_daily.py"
    build_script = helper_dir / "build_form4_buy_events_v1.py"
    report_script = helper_dir / "report_form4_buy_events_v1.py"

    if args.run_daily_update_first:
        run_update_first(
            py_exe=py_exe,
            update_script=update_script,
            config_path=config_path,
            mode=str(args.update_mode or "").strip().lower(),
        )

    ok = 0
    failed: list[str] = []
    for i, d in enumerate(all_dates, start=1):
        print(f"[{i}/{len(all_dates)}] Building Form4 as_of_date={d.isoformat()} ...")
        try:
            run_build_for_date(
                py_exe=py_exe,
                build_script=build_script,
                config_path=config_path,
                db_path=db_path,
                as_of_date=d,
                refresh_legacy_buy_table=bool(args.refresh_legacy_buy_table),
            )
            ok += 1
        except subprocess.CalledProcessError as exc:
            failed.append(d.isoformat())
            print(f"  FAILED for {d.isoformat()} (exit={exc.returncode})")
            if not args.continue_on_error:
                break

    if args.run_reports_at_end and ok > 0:
        run_reports(
            py_exe=py_exe,
            report_script=report_script,
            config_path=config_path,
            db_path=db_path,
        )

    print("")
    print(f"Completed builds: {ok:,}")
    print(f"Failed builds: {len(failed):,}")
    if failed:
        print("Failed dates:")
        for d in failed:
            print(f"  - {d}")


if __name__ == "__main__":
    main()
