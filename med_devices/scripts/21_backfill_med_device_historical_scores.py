#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.market_policy import scoring_market_sources  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
DEFAULT_STAGES = (
    "financial",
    "fda",
    "reimbursement",
    "technical",
    "borrow",
    "short_interest",
    "institutional_flow",
    "insider_activity",
    "scores",
)
DEFAULT_SETUP_STAGES = (
    "init_db",
    "historical_membership",
    "taxonomy",
)
STAGE_SCRIPTS = {
    "financial": "06_build_med_device_financial_features.py",
    "fda": "10_build_med_device_fda_features.py",
    "reimbursement": "11_build_med_device_reimbursement_features.py",
    "technical": "12_build_med_device_technical_features.py",
    "borrow": "54_build_med_device_borrow_features.py",
    "short_interest": "56_build_med_device_short_interest_features.py",
    "institutional_flow": "58_build_med_device_institutional_flow_features.py",
    "insider_activity": "60_build_med_device_insider_activity_features.py",
    "scores": "13_build_med_device_daily_scores.py",
    "review_pack": "16_publish_med_device_score_review_pack.py",
}
SETUP_STAGE_SCRIPTS = {
    "init_db": "00_init_med_devices_db.py",
    "historical_membership": "01b_load_med_device_historical_membership.py",
    "taxonomy": "22_build_med_device_calibration_cohorts.py",
}


@dataclass(frozen=True)
class BackfillPolicy:
    start_asof: date
    end_asof_raw: str
    backtest_end_asof_raw: str
    frequency: str
    weekday: str
    stages: list[str]
    setup_stages: list[str]
    horizons: list[int]
    include_historical_members: bool
    skip_existing: bool
    run_setup: bool
    run_backtest: bool
    run_calibration: bool
    publish_review_packs: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill point-in-time med-device score snapshots.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument("--frequency", type=str, default="")
    parser.add_argument("--weekday", type=str, default="")
    parser.add_argument(
        "--asof-list",
        type=str,
        default="",
        help="Comma-separated explicit YYYY-MM-DD as-of dates. Skips market-calendar discovery.",
    )
    parser.add_argument(
        "--asof-list-csv",
        type=Path,
        default=None,
        help="CSV containing explicit as-of dates. Uses asof_date by default, or the first column.",
    )
    parser.add_argument("--stages", type=str, default="")
    parser.add_argument("--setup-stages", type=str, default="")
    parser.add_argument("--horizons", type=str, default="")
    parser.add_argument("--max-asofs", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="Rebuild as-of dates even when score rows already exist.")
    parser.add_argument("--dry-run", action="store_true", help="Print/write the planned as-of dates without running stages.")
    parser.add_argument("--run-backtest", action="store_true", default=None)
    parser.add_argument("--no-run-backtest", action="store_false", dest="run_backtest")
    parser.add_argument("--run-setup", action="store_true", default=None)
    parser.add_argument("--no-run-setup", action="store_false", dest="run_setup")
    parser.add_argument("--run-calibration", action="store_true", default=None)
    parser.add_argument("--no-run-calibration", action="store_false", dest="run_calibration")
    parser.add_argument("--publish-review-packs", action="store_true", default=None)
    parser.add_argument("--no-publish-review-packs", action="store_false", dest="publish_review_packs")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_bool(raw: object, default: bool) -> bool:
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def parse_csv_list(raw: object) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def parse_horizons(raw: object) -> list[int]:
    out: list[int] = []
    for item in parse_csv_list(raw):
        value = int(item)
        if value <= 0:
            raise ValueError("Historical backfill horizons must be positive integers.")
        out.append(value)
    return out


def parse_asof_list(raw: object) -> list[date]:
    dates: list[date] = []
    for item in parse_csv_list(raw):
        parsed = parse_date(item)
        if parsed is None:
            raise ValueError(f"Invalid as-of date in --asof-list: {item}")
        dates.append(parsed)
    return sorted(set(dates))


def load_asof_list_csv(path: Path) -> list[date]:
    dates: list[date] = []
    with path.expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        date_field = "asof_date" if "asof_date" in reader.fieldnames else reader.fieldnames[0]
        for row in reader:
            raw = row.get(date_field)
            parsed = parse_date(raw)
            if parsed is None:
                continue
            dates.append(parsed)
    return sorted(set(dates))


def policy_from_config(config: dict[str, Any], args: argparse.Namespace) -> BackfillPolicy:
    start_raw = args.start_asof.strip() or str(cfg_get(config, "historical_backfill.start_asof", "2024-01-05"))
    start_asof = parse_date(start_raw)
    if start_asof is None:
        raise ValueError(f"Invalid historical_backfill.start_asof: {start_raw}")

    end_raw = args.end_asof.strip() or str(cfg_get(config, "historical_backfill.end_asof", "auto_complete_max_horizon"))
    backtest_end_raw = str(
        cfg_get(config, "historical_backfill.backtest_end_asof", "auto_complete_max_horizon")
    )
    frequency = (args.frequency.strip() or str(cfg_get(config, "historical_backfill.frequency", "weekly"))).lower()
    weekday = (args.weekday.strip() or str(cfg_get(config, "historical_backfill.weekday", "friday"))).lower()
    if frequency not in {"daily", "weekly"}:
        raise ValueError("historical_backfill.frequency must be daily or weekly.")
    if weekday not in WEEKDAY_INDEX:
        raise ValueError(f"Invalid historical_backfill.weekday: {weekday}")

    stage_text = args.stages.strip() or str(cfg_get(config, "historical_backfill.stages", ",".join(DEFAULT_STAGES)))
    stages = parse_csv_list(stage_text)
    unknown_stages = sorted(set(stages) - set(STAGE_SCRIPTS))
    if unknown_stages:
        raise ValueError(f"Unknown historical backfill stages: {','.join(unknown_stages)}")
    setup_stage_text = args.setup_stages.strip() or str(
        cfg_get(config, "historical_backfill.setup_stages", ",".join(DEFAULT_SETUP_STAGES))
    )
    setup_stages = parse_csv_list(setup_stage_text)
    unknown_setup_stages = sorted(set(setup_stages) - set(SETUP_STAGE_SCRIPTS))
    if unknown_setup_stages:
        raise ValueError(f"Unknown historical setup stages: {','.join(unknown_setup_stages)}")

    horizon_text = args.horizons.strip() or str(cfg_get(config, "historical_backfill.horizons", "30,60,120"))
    horizons = parse_horizons(horizon_text)
    include_historical_members = parse_bool(cfg_get(config, "historical_backfill.include_historical_members", False), False)

    run_backtest = (
        parse_bool(cfg_get(config, "historical_backfill.run_backtest", True), True)
        if args.run_backtest is None
        else bool(args.run_backtest)
    )
    run_setup = (
        parse_bool(cfg_get(config, "historical_backfill.run_setup", True), True)
        if args.run_setup is None
        else bool(args.run_setup)
    )
    run_calibration = (
        parse_bool(cfg_get(config, "historical_backfill.run_calibration", True), True)
        if args.run_calibration is None
        else bool(args.run_calibration)
    )
    publish_review_packs = (
        parse_bool(cfg_get(config, "historical_backfill.publish_review_packs", False), False)
        if args.publish_review_packs is None
        else bool(args.publish_review_packs)
    )
    skip_existing = parse_bool(cfg_get(config, "historical_backfill.skip_existing", True), True) and not args.force
    if publish_review_packs and "review_pack" not in stages:
        stages = [*stages, "review_pack"]
    return BackfillPolicy(
        start_asof=start_asof,
        end_asof_raw=end_raw,
        backtest_end_asof_raw=backtest_end_raw,
        frequency=frequency,
        weekday=weekday,
        stages=stages,
        setup_stages=setup_stages,
        horizons=horizons,
        include_historical_members=include_historical_members,
        skip_existing=skip_existing,
        run_setup=run_setup,
        run_backtest=run_backtest,
        run_calibration=run_calibration,
        publish_review_packs=publish_review_packs,
    )


def load_market_dates(conn: Any, *, sources: list[str], benchmark_tickers: list[str]) -> list[date]:
    if not sources:
        raise ValueError("No scoring market sources configured.")
    tickers = [str(item or "").strip().upper() for item in benchmark_tickers if str(item or "").strip()]
    if not tickers:
        raise ValueError("historical backfill requires med_devices_universe.benchmark_tickers for calendar discovery.")
    date_set: set[date] = set()
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
                parsed = parse_date(row["bar_date"])
                if parsed is not None:
                    date_set.add(parsed)
    dates = sorted(date_set)
    if not dates:
        raise RuntimeError(
            "No market dates found for configured benchmark tickers and scoring sources: "
            f"tickers={tickers} sources={sources}"
        )
    return dates


def auto_end_asof(market_dates: list[date], *, max_horizon: int) -> date:
    if len(market_dates) <= max_horizon:
        raise RuntimeError(
            f"Need more than {max_horizon} market dates to auto-select a complete forward-return end as-of."
        )
    return market_dates[-(max_horizon + 1)]


def resolve_end_asof(raw: str, market_dates: list[date], *, max_horizon: int) -> date | None:
    text = raw.strip().lower()
    if text == "auto_complete_max_horizon":
        return auto_end_asof(market_dates, max_horizon=max_horizon)
    if text in {"latest_market_date", "latest"}:
        return market_dates[-1] if market_dates else None
    return parse_date(raw)


def latest_market_date_on_or_before(market_dates: list[date], target: date) -> date | None:
    selected: date | None = None
    for item in market_dates:
        if item <= target:
            selected = item
        else:
            break
    return selected


def weekly_asofs(market_dates: list[date], *, start: date, end: date, weekday: str) -> list[date]:
    weekday_idx = WEEKDAY_INDEX[weekday]
    first_target = start + timedelta(days=(weekday_idx - start.weekday()) % 7)
    out: list[date] = []
    seen: set[date] = set()
    target = first_target
    while target <= end:
        market_date = latest_market_date_on_or_before(market_dates, target)
        if market_date is not None and start <= market_date <= end and market_date not in seen:
            out.append(market_date)
            seen.add(market_date)
        target += timedelta(days=7)
    return out


def daily_asofs(market_dates: list[date], *, start: date, end: date) -> list[date]:
    return [item for item in market_dates if start <= item <= end]


def explicit_asofs_from_args(args: argparse.Namespace) -> list[date]:
    dates = parse_asof_list(args.asof_list)
    if args.asof_list_csv is not None:
        dates.extend(load_asof_list_csv(args.asof_list_csv))
    return sorted(set(dates))


def resolve_explicit_backtest_end(
    raw: str,
    asofs: list[date],
    *,
    max_horizon: int,
) -> date | None:
    if not asofs:
        return None
    text = raw.strip().lower()
    if text == "auto_complete_max_horizon":
        return auto_end_asof(asofs, max_horizon=max_horizon)
    if text in {"latest_market_date", "latest"}:
        return asofs[-1]
    return parse_date(raw)


def existing_score_status(conn: Any, asof: date) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM med_device_daily_scores
        WHERE asof_date = ?
        """,
        (asof.isoformat(),),
    ).fetchone()
    if row is None:
        return 0
    return int(row["n"] or 0)


def dated_output_dir(base_output_dir: Path, asof: date) -> Path:
    asof_text = asof.isoformat()
    return base_output_dir if base_output_dir.name == asof_text else base_output_dir / asof_text


def review_pack_complete(base_output_dir: Path, asof: date, required_files: list[str]) -> bool:
    output_dir = dated_output_dir(base_output_dir, asof)
    if not output_dir.exists():
        return False
    checks = required_files or ["med_device_daily_composite_scores.csv", "med_device_score_review_pack.md"]
    return all((output_dir / name).exists() and (output_dir / name).stat().st_size > 0 for name in checks)


def run_command(command: list[str]) -> None:
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def run_setup_stage(*, stage: str, config_path: Path, db_path: Path | None) -> None:
    script = PACKAGE_ROOT / "scripts" / SETUP_STAGE_SCRIPTS[stage]
    command = [sys.executable, str(script), "--config", str(config_path)]
    if db_path is not None:
        command.extend(["--db", str(db_path)])
    run_command(command)


@contextmanager
def read_connection(db_path: Path, *, timeout_sec: float) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path.expanduser().resolve(), timeout=timeout_sec)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as exc:
            if "unable to open database file" not in str(exc).lower():
                raise
        yield conn
    finally:
        conn.close()


def run_stage(
    *,
    stage: str,
    asof: date,
    config_path: Path,
    db_path: Path | None,
    include_historical_members: bool,
) -> None:
    script = PACKAGE_ROOT / "scripts" / STAGE_SCRIPTS[stage]
    command = [sys.executable, str(script), "--config", str(config_path), "--asof", asof.isoformat()]
    if db_path is not None:
        command.extend(["--db", str(db_path)])
    if include_historical_members and stage in {"financial", "fda", "reimbursement", "technical", "scores"}:
        command.append("--include-historical-members")
    elif include_historical_members and stage in {"borrow", "short_interest", "institutional_flow", "insider_activity"}:
        # Alternative-positioning sources are current-ticker feeds; their builders intentionally do not
        # score historical delisted members unless those scripts grow explicit PIT support.
        pass
    run_command(command)


def write_manifest(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["asof_date", "status", "started_at", "ended_at", "stages", "review_pack_dir", "message"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def validate_backtest_csv(path: Path, *, expected_start: date, expected_end: date) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"Calibration requires a populated backtest CSV: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        asofs = sorted({str(row.get("asof_date") or "") for row in reader if str(row.get("asof_date") or "")})
    if not asofs:
        raise RuntimeError(f"Calibration requires a non-empty backtest CSV: {path}")
    first_expected = expected_start.isoformat()
    last_expected = expected_end.isoformat()
    if asofs[0] > first_expected or asofs[-1] < last_expected:
        raise RuntimeError(
            "Calibration backtest CSV does not cover the requested as-of range: "
            f"{path} has {asofs[0]}..{asofs[-1]}, expected {first_expected}..{last_expected}"
        )


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    policy = policy_from_config(config, args)
    source_priority = scoring_market_sources(config)
    manifest_path = resolve_path(
        cfg_get(
            config,
            "historical_backfill.manifest_csv",
            "../output/med_devices_reports/historical_backfill/weekly_score_backfill_manifest.csv",
        ),
        base_dir=base_dir,
    )
    backtest_csv = resolve_path(
        cfg_get(config, "scoring.backtest_output_csv", "../output/med_devices_reports/med_device_score_backtest.csv"),
        base_dir=base_dir,
    )
    calibration_csv = resolve_path(
        cfg_get(config, "scoring.calibration_output_csv", "../output/med_devices_reports/med_device_score_calibration.csv"),
        base_dir=base_dir,
    )
    review_pack_base_dir = resolve_path(
        cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"),
        base_dir=base_dir,
    )
    review_pack_required_files = [
        str(item).strip()
        for item in cfg_get(config, "med_devices_production_qa.review_pack_required_files", []) or []
        if str(item or "").strip()
    ]

    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    explicit_asofs = explicit_asofs_from_args(args)
    if policy.run_setup and not args.dry_run:
        for stage in policy.setup_stages:
            run_setup_stage(stage=stage, config_path=config_path, db_path=db_path)
    if explicit_asofs:
        asofs = [asof for asof in explicit_asofs if asof >= policy.start_asof]
        end_asof = resolve_explicit_backtest_end(
            policy.end_asof_raw,
            asofs,
            max_horizon=max(policy.horizons),
        )
        if end_asof is not None:
            asofs = [asof for asof in asofs if asof <= end_asof]
        backtest_end_asof = (
            resolve_explicit_backtest_end(
                policy.backtest_end_asof_raw,
                asofs,
                max_horizon=max(policy.horizons),
            )
            if policy.run_backtest or policy.run_calibration
            else None
        )
        if backtest_end_asof is None:
            if policy.run_backtest or policy.run_calibration:
                raise ValueError("Explicit historical backfill requires at least one valid as-of date.")
        if args.max_asofs > 0:
            asofs = asofs[: args.max_asofs]
    else:
        with read_connection(db_path, timeout_sec=timeout_sec) as conn:
            market_dates = load_market_dates(
                conn,
                sources=source_priority,
                benchmark_tickers=list(cfg_get(config, "med_devices_universe.benchmark_tickers", []) or []),
            )
            end_asof = resolve_end_asof(policy.end_asof_raw, market_dates, max_horizon=max(policy.horizons))
            if end_asof is None:
                raise ValueError(f"Invalid historical_backfill.end_asof: {policy.end_asof_raw}")
            backtest_end_asof = resolve_end_asof(
                policy.backtest_end_asof_raw,
                market_dates,
                max_horizon=max(policy.horizons),
            )
            if backtest_end_asof is None:
                raise ValueError(f"Invalid historical_backfill.backtest_end_asof: {policy.backtest_end_asof_raw}")
            if policy.frequency == "daily":
                asofs = daily_asofs(market_dates, start=policy.start_asof, end=end_asof)
            else:
                asofs = weekly_asofs(market_dates, start=policy.start_asof, end=end_asof, weekday=policy.weekday)
            if args.max_asofs > 0:
                asofs = asofs[: args.max_asofs]
    manifest_rows: list[dict[str, Any]] = []
    if args.dry_run:
        for asof in asofs:
            manifest_rows.append(
                {
                    "asof_date": asof.isoformat(),
                    "status": "planned",
                    "started_at": "",
                    "ended_at": "",
                    "stages": ",".join(policy.stages),
                    "review_pack_dir": str(dated_output_dir(review_pack_base_dir, asof)) if policy.publish_review_packs else "",
                    "message": f"dry_run setup_stages={','.join(policy.setup_stages) if policy.run_setup else ''}",
                }
            )
        write_manifest(manifest_path, manifest_rows)
        print(f"historical_backfill_manifest={manifest_path} planned_asofs={len(asofs)} dry_run=1")
        return

    for asof in asofs:
        started_at = utc_now()
        stages_to_run = list(policy.stages)
        review_pack_dir = dated_output_dir(review_pack_base_dir, asof) if policy.publish_review_packs else None
        try:
            if policy.skip_existing:
                with read_connection(db_path, timeout_sec=timeout_sec) as conn:
                    existing_rows = existing_score_status(conn, asof)
                if existing_rows > 0:
                    if policy.publish_review_packs and not review_pack_complete(
                        review_pack_base_dir,
                        asof,
                        review_pack_required_files,
                    ):
                        stages_to_run = ["review_pack"]
                    else:
                        manifest_rows.append(
                            {
                                "asof_date": asof.isoformat(),
                                "status": "skipped_existing",
                                "started_at": started_at,
                                "ended_at": utc_now(),
                                "stages": ",".join(policy.stages),
                                "review_pack_dir": str(review_pack_dir or ""),
                                "message": f"existing_score_rows={existing_rows}",
                            }
                        )
                        continue
            for stage in stages_to_run:
                run_stage(
                    stage=stage,
                    asof=asof,
                    config_path=config_path,
                    db_path=db_path,
                    include_historical_members=policy.include_historical_members,
                )
            manifest_rows.append(
                {
                    "asof_date": asof.isoformat(),
                    "status": "success",
                    "started_at": started_at,
                    "ended_at": utc_now(),
                    "stages": ",".join(stages_to_run),
                    "review_pack_dir": str(review_pack_dir or ""),
                    "message": "published_missing_review_pack" if stages_to_run == ["review_pack"] else "",
                }
            )
        except subprocess.CalledProcessError as exc:
            manifest_rows.append(
                {
                    "asof_date": asof.isoformat(),
                    "status": "failed",
                    "started_at": started_at,
                    "ended_at": utc_now(),
                    "stages": ",".join(stages_to_run),
                    "review_pack_dir": str(review_pack_dir or ""),
                    "message": f"stage_command_failed exit_code={exc.returncode}",
                }
            )
            write_manifest(manifest_path, manifest_rows)
            raise
    write_manifest(manifest_path, manifest_rows)

    backtest_asofs = [asof for asof in asofs if backtest_end_asof is not None and asof <= backtest_end_asof]
    if backtest_asofs and policy.run_backtest:
        command = [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "17_backtest_med_device_scores.py"),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--asof-start",
            backtest_asofs[0].isoformat(),
            "--asof-end",
            backtest_asofs[-1].isoformat(),
            "--horizons",
            ",".join(str(item) for item in policy.horizons),
            "--output-csv",
            str(backtest_csv),
        ]
        run_command(command)
    if asofs and policy.run_calibration:
        if not backtest_asofs:
            raise RuntimeError("Calibration requested, but no as-of dates are eligible for backtesting.")
        validate_backtest_csv(backtest_csv, expected_start=backtest_asofs[0], expected_end=backtest_asofs[-1])
        command = [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "18_calibrate_med_device_scores.py"),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--input-csv",
            str(backtest_csv),
            "--output-csv",
            str(calibration_csv),
        ]
        run_command(command)
    print(
        f"historical_backfill_manifest={manifest_path} asofs={len(asofs)} "
        f"backtest_csv={backtest_csv if policy.run_backtest else ''} "
        f"calibration_csv={calibration_csv if policy.run_calibration else ''}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
