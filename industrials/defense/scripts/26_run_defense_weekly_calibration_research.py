#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.defense.research_artifacts import parse_required_date, select_weekly_snapshot_dates  # noqa: E402


DEFAULT_WEEKLY_START = "2026-01-04"
REPORT_FIELDS = [
    "step",
    "status",
    "returncode",
    "command",
    "stdout_tail",
    "stderr_tail",
]


@dataclass(frozen=True)
class StepResult:
    step: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def status(self) -> str:
        return "pass" if self.returncode == 0 else "fail"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the defense weekly Stage 8/9 calibration research chain.")
    parser.add_argument("--start-date", default=DEFAULT_WEEKLY_START)
    parser.add_argument("--end-date", default="")
    parser.add_argument("--weekly-selection", choices=["first", "last"], default="last")
    parser.add_argument(
        "--membership-mode",
        choices=["pit", "current"],
        default="pit",
        help="Weekly research defaults to PIT membership; current is for compatibility/debug runs only.",
    )
    parser.add_argument(
        "--policy-asof",
        default="",
        help="Eligibility-policy lock date for historical research snapshot publishing. Defaults to the latest selected weekly date.",
    )
    parser.add_argument("--forward-days", type=int, default=63)
    parser.add_argument("--embargo-days", type=int, default=21)
    parser.add_argument("--trials", type=int, default=64)
    parser.add_argument(
        "--backfill-features",
        action="store_true",
        help="Build Stage 3/4/5 features before publishing snapshots or calibration artifacts.",
    )
    parser.add_argument(
        "--feature-cadence",
        choices=["weekly", "daily"],
        default="weekly",
        help="Cadence used for feature backfill. Calibration artifacts remain weekly.",
    )
    parser.add_argument(
        "--feature-backfill-only",
        action="store_true",
        help="Run only the Stage 3/4/5 feature backfill steps, then stop.",
    )
    parser.add_argument(
        "--skip-existing-features",
        action="store_true",
        help="During feature backfill, skip dates whose PIT Stage 3/4/5 feature coverage is already complete.",
    )
    parser.add_argument(
        "--max-backfill-weeks",
        type=int,
        default=0,
        help="Optional safety cap for feature backfill weeks; 0 means all selected weeks.",
    )
    parser.add_argument(
        "--max-backfill-days",
        type=int,
        default=0,
        help="Optional safety cap for daily feature backfill dates; 0 means all selected daily dates.",
    )
    parser.add_argument(
        "--backfill-order",
        choices=["oldest", "newest"],
        default="oldest",
        help="When a backfill cap is set, choose the oldest or newest remaining incomplete dates.",
    )
    parser.add_argument(
        "--publish-daily-snapshots",
        action="store_true",
        help="Publish PIT daily dashboard rank-table snapshots after feature backfill.",
    )
    parser.add_argument(
        "--skip-weekly-calibration",
        action="store_true",
        help="Run feature backfill and/or daily snapshot publishing without the weekly Stage 8/9 calibration chain.",
    )
    parser.add_argument(
        "--daily-snapshot-root",
        type=Path,
        default=None,
        help="Daily PIT snapshot root. Defaults to configured output/industrials/defense/dashboard.",
    )
    parser.add_argument(
        "--max-daily-snapshot-dates",
        type=int,
        default=0,
        help="Optional safety cap for daily snapshot publishing; 0 means all publishable dates in range.",
    )
    parser.add_argument(
        "--allow-overwrite-snapshots",
        action="store_true",
        help="Rebuild sealed weekly research rank snapshots after upstream data or scoring fixes.",
    )
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def tail(text: str, max_chars: int = 1200) -> str:
    cleaned = text.strip().replace("\r\n", "\n")
    return cleaned[-max_chars:] if len(cleaned) > max_chars else cleaned


def run_step(step: str, command: list[str], *, dry_run: bool) -> StepResult:
    if dry_run:
        return StepResult(step=step, command=command, returncode=0, stdout="dry_run", stderr="")
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    return StepResult(
        step=step,
        command=command,
        returncode=int(completed.returncode),
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def report_row(result: StepResult) -> dict[str, str | int]:
    return {
        "step": result.step,
        "status": result.status,
        "returncode": result.returncode,
        "command": " ".join(result.command),
        "stdout_tail": tail(result.stdout),
        "stderr_tail": tail(result.stderr),
    }


def selected_market_dates(
    *,
    start_date: str,
    end_date: str,
    cadence: str,
    weekly_selection: str,
) -> list[str]:
    config_path = PACKAGE_ROOT / "config.yaml"
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"Industrials DB does not exist: {db_path}")
    source_id = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted") or "")
    benchmark = str(cfg_get(config, "industrials_universe.benchmark_ticker", "XAR") or "XAR")
    start = parse_required_date(start_date, field="start_date")
    explicit_end = parse_required_date(end_date, field="end_date") if end_date else None
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        max_row = conn.execute(
            """
            SELECT MAX(bar_date) AS max_date
            FROM fact_price_ohlcv
            WHERE ticker = ? AND source_id = ?
            """,
            (benchmark, source_id),
        ).fetchone()
        max_bar = parse_required_date(max_row["max_date"], field="max_bar_date")
        end = explicit_end or max_bar
        if end < start:
            raise ValueError(f"end_date {end.isoformat()} is before start_date {start.isoformat()}")
        rows = conn.execute(
            """
            SELECT bar_date
            FROM fact_price_ohlcv
            WHERE ticker = ?
              AND source_id = ?
              AND bar_date >= ?
              AND bar_date <= ?
            ORDER BY bar_date
            """,
            (benchmark, source_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    dates = [str(row["bar_date"]) for row in rows]
    if cadence == "daily":
        return dates
    if cadence == "weekly":
        return select_weekly_snapshot_dates(
            dates,
            weekly_start_date=start.isoformat(),
            selection=weekly_selection,
        )
    raise ValueError(f"Unsupported feature cadence: {cadence}")


def parse_source_list(raw: object) -> list[str]:
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list | tuple):
        values = [str(part).strip() for part in raw]
    else:
        values = []
    return [value for value in values if value]


def source_priority_list(primary_source: str, fallback_sources: list[str]) -> list[str]:
    out: list[str] = []
    for source_id in [primary_source, *fallback_sources]:
        if source_id and source_id not in out:
            out.append(source_id)
    if not out:
        raise ValueError("At least one source_id is required")
    return out


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def complete_feature_dates(
    dates: list[str],
    *,
    membership_mode: str,
) -> set[str]:
    if not dates:
        return set()
    config_path = PACKAGE_ROOT / "config.yaml"
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    market_source = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted"))
    market_sources = source_priority_list(
        market_source,
        parse_source_list(cfg_get(config, "market_data_policy.scoring_fallback_sources", [])),
    )
    financial_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    positioning_source = str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite"))

    def member_count(conn: sqlite3.Connection, asof: str) -> int:
        if membership_mode == "pit":
            return int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT m.ticker) AS n
                    FROM dim_universe_membership m
                    JOIN dim_industrials_taxonomy t
                      ON t.company_id = m.company_id
                     AND t.model_family = m.model_family
                    WHERE m.model_family = 'defense'
                      AND m.point_in_time_flag = 1
                      AND m.start_date <= ?
                      AND COALESCE(m.end_date, '9999-12-31') >= ?
                    """,
                    (asof, asof),
                ).fetchone()["n"]
                or 0
            )
        return int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT c.ticker) AS n
                FROM dim_company c
                JOIN dim_industrials_taxonomy t
                  ON t.company_id = c.company_id
                 AND t.model_family = 'defense'
                WHERE c.is_active = 1
                """
            ).fetchone()["n"]
            or 0
        )

    def coverage(conn: sqlite3.Connection, table: str, source_ids: list[str], asof: str) -> int:
        if membership_mode == "pit":
            return int(
                conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT f.ticker) AS n
                    FROM {table} f
                    JOIN dim_universe_membership m
                      ON m.ticker = f.ticker
                     AND m.model_family = f.model_family
                    JOIN dim_industrials_taxonomy t
                      ON t.company_id = m.company_id
                     AND t.model_family = m.model_family
                    WHERE f.model_family = 'defense'
                      AND f.source_id IN ({placeholders(source_ids)})
                      AND f.asof_date = ?
                      AND m.point_in_time_flag = 1
                      AND m.start_date <= ?
                      AND COALESCE(m.end_date, '9999-12-31') >= ?
                    """,
                    (*source_ids, asof, asof, asof),
                ).fetchone()["n"]
                or 0
            )
        return int(
            conn.execute(
                f"""
                SELECT COUNT(DISTINCT f.ticker) AS n
                FROM {table} f
                JOIN dim_company c ON c.ticker = f.ticker AND c.is_active = 1
                JOIN dim_industrials_taxonomy t
                  ON t.company_id = c.company_id
                 AND t.model_family = f.model_family
                WHERE f.model_family = 'defense'
                  AND f.source_id IN ({placeholders(source_ids)})
                  AND f.asof_date = ?
                """,
                (*source_ids, asof),
            ).fetchone()["n"]
            or 0
        )

    complete: set[str] = set()
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for asof in dates:
            members = member_count(conn, asof)
            if members <= 0:
                continue
            counts = [
                coverage(conn, "feature_market_technical", market_sources, asof),
                coverage(conn, "feature_financial_statement", [financial_source], asof),
                coverage(conn, "feature_positioning", [positioning_source], asof),
            ]
            if all(count >= members for count in counts):
                complete.add(asof)
    return complete


def feature_backfill_steps(
    *,
    dates: list[str],
    python: str,
    cadence: str,
) -> list[tuple[str, list[str]]]:
    steps: list[tuple[str, list[str]]] = []
    backfill_dir = f"{cadence}_backfill"

    def feature_report(stage: str, prefix: str, asof: str) -> Path:
        filename = f"{prefix}_{asof}.csv" if cadence == "weekly" else f"{prefix}_latest.csv"
        return PROJECT_ROOT / "output" / "industrials" / "defense" / stage / backfill_dir / filename

    if dates:
        latest_asof = dates[-1]
        steps.append(
            (
                f"refresh_positioning_facts_{latest_asof}",
                [
                    python,
                    "industrials/scripts/09_import_industrials_positioning.py",
                    "--model-family",
                    "defense",
                    "--asof",
                    latest_asof,
                    "--include-historical-members",
                    "--output-csv",
                    str(
                        feature_report(
                            "stage5",
                            "positioning_fact_refresh",
                            latest_asof,
                        )
                    ),
                ],
            )
        )
    for asof in dates:
        steps.extend(
            [
                (
                    f"backfill_market_features_{asof}",
                    [
                        python,
                        "industrials/defense/scripts/05_build_defense_market_features.py",
                        "--asof",
                        asof,
                        "--output-csv",
                        str(feature_report("stage3", "market_feature_coverage", asof)),
                    ],
                ),
                (
                    f"backfill_financial_features_{asof}",
                    [
                        python,
                        "industrials/defense/scripts/08_build_defense_financial_features.py",
                        "--asof",
                        asof,
                        "--output-csv",
                        str(feature_report("stage4", "financial_feature_coverage", asof)),
                    ],
                ),
                (
                    f"backfill_positioning_features_{asof}",
                    [
                        python,
                        "industrials/scripts/09_import_industrials_positioning.py",
                        "--model-family",
                        "defense",
                        "--asof",
                        asof,
                        "--features-only",
                        "--feature-membership-mode",
                        "pit",
                        "--output-csv",
                        str(feature_report("stage5", "positioning_feature_coverage", asof)),
                    ],
                ),
            ]
        )
    return steps


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    if args.forward_days <= 0:
        raise ValueError("--forward-days must be positive")
    if args.embargo_days < 0:
        raise ValueError("--embargo-days cannot be negative")
    if args.trials <= 0:
        raise ValueError("--trials must be positive")
    if args.feature_backfill_only and not args.backfill_features:
        raise ValueError("--feature-backfill-only requires --backfill-features")

    python = sys.executable
    stage8_root = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8"
    stage9_root = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage9"
    panel_dir = stage8_root / "oos_calibration_panel_weekly"
    weekly_snapshot_root = stage8_root / "weekly_rank_snapshots"
    config_path = PACKAGE_ROOT / "config.yaml"
    config = load_yaml(config_path)
    daily_snapshot_root = (
        args.daily_snapshot_root.expanduser().resolve()
        if args.daily_snapshot_root
        else resolve_path(
            str(
                cfg_get(
                    config,
                    "oos_calibration_standards.families.defense.snapshot_history_root",
                    "../output/industrials/defense/dashboard",
                )
            ),
            base_dir=PACKAGE_ROOT,
        )
    )
    panel_csv = panel_dir / "defense_oos_calibration_panel.csv"
    splits_csv = panel_dir / "defense_oos_calibration_splits.csv"
    panel_manifest = panel_dir / "defense_oos_calibration_panel_manifest.json"
    validation_report = stage8_root / "weekly_oos_calibration_artifact_validation_report.csv"
    calibration_dir = stage8_root / "optuna_calibration_weekly"
    calibration_summary = calibration_dir / "defense_optuna_calibration_summary.csv"
    backtest_dir = stage9_root / "score_backtest_weekly"
    history_report = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage6" / "weekly_shadow_snapshot_history_build_report.csv"
    run_report = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else stage8_root / "weekly_calibration_research_run_report.csv"
    )

    date_args = ["--start-date", args.start_date]
    if args.end_date:
        date_args.extend(["--end-date", args.end_date])

    feature_dates: list[str] = []
    if args.backfill_features:
        feature_dates = selected_market_dates(
            start_date=args.start_date,
            end_date=args.end_date,
            cadence=args.feature_cadence,
            weekly_selection=args.weekly_selection,
        )
        if not feature_dates:
            raise ValueError(f"No {args.feature_cadence} market dates found for feature backfill")
        if args.skip_existing_features:
            complete = complete_feature_dates(feature_dates, membership_mode=args.membership_mode)
            before = len(feature_dates)
            feature_dates = [asof for asof in feature_dates if asof not in complete]
            print(
                f"Skipping complete {args.feature_cadence} feature dates: "
                f"skipped={before - len(feature_dates)} remaining={len(feature_dates)}"
            )
        max_backfill_dates = args.max_backfill_days if args.feature_cadence == "daily" else args.max_backfill_weeks
        if max_backfill_dates > 0:
            if args.backfill_order == "newest":
                feature_dates = feature_dates[-max_backfill_dates:]
            else:
                feature_dates = feature_dates[:max_backfill_dates]
        if feature_dates:
            print(
                f"{args.feature_cadence.title()} feature backfill dates: "
                f"{feature_dates[0]} to {feature_dates[-1]} count={len(feature_dates)}"
            )
        else:
            print(f"{args.feature_cadence.title()} feature backfill dates: none remaining after skip-existing check")
    policy_asof = str(args.policy_asof or "").strip()
    if not policy_asof and feature_dates:
        policy_asof = feature_dates[-1]

    feature_steps = feature_backfill_steps(dates=feature_dates, python=python, cadence=args.feature_cadence)
    daily_publish_steps: list[tuple[str, list[str]]] = []
    if args.publish_daily_snapshots:
        daily_publish_steps.append(
            (
                "publish_daily_pit_dashboard_snapshots",
                [
                    python,
                    "industrials/defense/scripts/19_build_defense_shadow_snapshot_history.py",
                    *date_args,
                    "--cadence",
                    "daily",
                    "--membership-mode",
                    args.membership_mode,
                    "--snapshot-root",
                    str(daily_snapshot_root),
                    "--date-order",
                    args.backfill_order,
                    *(
                        ["--max-dates", str(args.max_daily_snapshot_dates)]
                        if args.max_daily_snapshot_dates > 0
                        else []
                    ),
                    *(
                        ["--allow-overwrite"]
                        if args.allow_overwrite_snapshots
                        else []
                    ),
                    *(
                        []
                        if args.allow_overwrite_snapshots
                        else ["--skip-existing"]
                    ),
                    *(
                        ["--policy-asof", policy_asof]
                        if policy_asof
                        else []
                    ),
                    "--output-csv",
                    str(PROJECT_ROOT / "output" / "industrials" / "defense" / "stage6" / "daily_pit_dashboard_snapshot_history_build_report.csv"),
                ],
            )
        )
    downstream_steps = [
        (
            "publish_weekly_shadow_snapshots",
            [
                python,
                "industrials/defense/scripts/19_build_defense_shadow_snapshot_history.py",
                *date_args,
                "--cadence",
                "weekly",
                "--weekly-start-date",
                args.start_date,
                "--weekly-selection",
                args.weekly_selection,
                "--membership-mode",
                args.membership_mode,
                "--snapshot-root",
                str(weekly_snapshot_root),
                *(
                    ["--allow-overwrite"]
                    if args.allow_overwrite_snapshots
                    else []
                ),
                *(
                    ["--policy-asof", policy_asof]
                    if policy_asof
                    else []
                ),
                "--output-csv",
                str(history_report),
            ],
        ),
        (
            "build_weekly_oos_panel",
            [
                python,
                "industrials/defense/scripts/22_build_defense_oos_calibration_panel.py",
                *date_args,
                "--cadence",
                "weekly",
                "--weekly-start-date",
                args.start_date,
                "--weekly-selection",
                args.weekly_selection,
                "--snapshot-root",
                str(weekly_snapshot_root),
                "--forward-days",
                str(args.forward_days),
                "--embargo-days",
                str(args.embargo_days),
                "--output-dir",
                str(panel_dir),
                "--allow-overwrite",
            ],
        ),
        (
            "validate_weekly_oos_panel",
            [
                python,
                "industrials/defense/scripts/23_validate_defense_oos_calibration_artifacts.py",
                "--panel-csv",
                str(panel_csv),
                "--splits-csv",
                str(splits_csv),
                "--manifest",
                str(panel_manifest),
                "--output-csv",
                str(validation_report),
            ],
        ),
        (
            "run_weekly_optuna_calibration",
            [
                python,
                "industrials/defense/scripts/24_run_defense_optuna_calibration.py",
                "--panel-csv",
                str(panel_csv),
                "--output-dir",
                str(calibration_dir),
                "--trials",
                str(args.trials),
                "--allow-overwrite",
            ],
        ),
        (
            "run_weekly_score_backtest",
            [
                python,
                "industrials/defense/scripts/25_backtest_defense_scores.py",
                "--panel-csv",
                str(panel_csv),
                "--calibration-summary-csv",
                str(calibration_summary),
                "--output-dir",
                str(backtest_dir),
                "--allow-overwrite",
            ],
        ),
    ]
    if args.feature_backfill_only:
        steps = feature_steps
    else:
        steps = [*feature_steps, *daily_publish_steps]
        if not args.skip_weekly_calibration:
            steps.extend(downstream_steps)

    results: list[StepResult] = []
    for step, command in steps:
        result = run_step(step, command, dry_run=bool(args.dry_run))
        results.append(result)
        print(f"{result.status.upper()} {step}: returncode={result.returncode}")
        if result.stdout.strip():
            print(tail(result.stdout, 600))
        if result.returncode != 0:
            break

    write_csv_atomic(run_report, REPORT_FIELDS, [report_row(result) for result in results])
    print(f"Wrote {run_report}")
    return 0 if all(result.returncode == 0 for result in results) and len(results) == len(steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
