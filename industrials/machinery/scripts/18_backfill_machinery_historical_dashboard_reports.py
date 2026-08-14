#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
from contextlib import contextmanager
import json
import logging
import os
import runpy
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterator


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.core.financial_filing_lineage import (  # noqa: E402
    apply_financial_lineage_gate,
    build_financial_filing_lineage,
)
from industrials.core.policy_loader import load_eligibility_policy  # noqa: E402
from industrials.core.refresh_lock import RefreshLock  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.machinery.build_contract import (  # noqa: E402
    DISCLOSURE_PARSER_VERSION,
    HISTORICAL_BUILD_METADATA_FILENAME,
    historical_build_metadata,
)
from industrials.machinery.financial_contract import required_metric_names  # noqa: E402
from industrials.machinery.historical_coverage import (  # noqa: E402
    build_combined_historical_coverage,
    expected_tickers_by_date,
    load_validated_sidecar,
)
from industrials.machinery.scoring import (  # noqa: E402
    build_scoring_feature_rows,
    finalize_rank_rows,
    parse_asof,
    publish_dashboard,
    read_rows,
    survivorship_sidecar,
    write_json_atomic,
)
from portfolio_layer.scores.adapters import run_adapter  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REPORT_FIELDS = [
    "asof_date",
    "policy_lock_date",
    "status",
    "row_count",
    "rank_ready_count",
    "portfolio_adapter_row_count",
    "feature_rebuilt_flag",
    "existing_output_reused_flag",
    "working_features_compacted_flag",
    "historical_build_signature",
    "disclosure_parser_version",
    "reuse_validation",
    "output_dir",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build survivorship-corrected machinery rank files for portfolio calibration history."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument(
        "--exclude-end-date",
        action="store_true",
        help="Omit the exact end date so the current-date publisher can own that immutable snapshot.",
    )
    parser.add_argument("--frequency", choices=("daily", "weekly"), default="daily")
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument("--rebuild-features", action="store_true")
    parser.add_argument(
        "--resume-existing",
        action="store_true",
        help="Validate and reuse already-published dates, rebuilding only missing or invalid dates.",
    )
    parser.add_argument(
        "--retain-stage-reports",
        action="store_true",
        help="Retain verbose date-scoped feature reports. The default reuses a compact scratch directory.",
    )
    parser.add_argument(
        "--compact-working-features",
        action="store_true",
        help="Delete newly-created date-local feature rows after the immutable dashboard files pass validation.",
    )
    parser.add_argument(
        "--in-process-stages",
        action="store_true",
        help="Reuse one Python interpreter for feature stages while retaining per-stage DB transactions.",
    )
    parser.add_argument("--allow-zero-eligible", action="store_true")
    parser.add_argument(
        "--coverage-only",
        action="store_true",
        help="Rebuild the global historical coverage index from existing validated sidecars only.",
    )
    parser.add_argument(
        "--repair-membership-sidecars",
        action="store_true",
        help="Reconcile membership metadata and hashes in existing historical files without rebuilding features.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    return parser.parse_args()


def available_dates(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    benchmark: str,
    primary_source: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT bar_date
        FROM fact_price_ohlcv
        WHERE ticker = ? AND source_id = ? AND bar_date BETWEEN ? AND ?
        ORDER BY bar_date
        """,
        (benchmark, primary_source, start_date, end_date),
    ).fetchall()
    dates = [str(row["bar_date"]) for row in rows]
    if dates:
        return dates
    fallback = conn.execute(
        """
        SELECT DISTINCT asof_date
        FROM feature_market_technical
        WHERE model_family = ? AND asof_date BETWEEN ? AND ?
        ORDER BY asof_date
        """,
        ("machinery", start_date, end_date),
    ).fetchall()
    return [str(row["asof_date"]) for row in fallback]


def weekly_dates(dates: list[str]) -> list[str]:
    by_week: dict[tuple[int, int], str] = {}
    for raw in dates:
        parsed = date.fromisoformat(raw)
        iso_year, iso_week, _ = parsed.isocalendar()
        by_week[(iso_year, iso_week)] = raw
    return [by_week[key] for key in sorted(by_week)]


def published_dashboard_dates(
    dashboard_root: Path,
    *,
    start_date: str,
    end_date: str,
) -> list[str]:
    dates: list[str] = []
    if not dashboard_root.exists():
        return dates
    for path in dashboard_root.iterdir():
        if not path.is_dir():
            continue
        try:
            asof = parse_asof(path.name)
        except ValueError:
            continue
        if (
            start_date <= asof <= end_date
            and (path / "machinery_stage11_survivorship_calibration_panel.csv").exists()
        ):
            dates.append(asof)
    return sorted(set(dates))


def validate_existing_membership(
    conn: sqlite3.Connection,
    *,
    asof: str,
    rows: list[dict[str, str]],
) -> None:
    expected = expected_tickers_by_date(conn, [asof])[asof]["combined"]
    actual = {str(row.get("ticker") or "") for row in rows}
    if actual != expected:
        raise ValueError(
            f"historical membership mismatch missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )


MEMBERSHIP_FIELDS = (
    "membership_source_id",
    "membership_basis",
    "membership_start_date",
    "membership_end_date",
    "membership_status",
    "membership_confidence",
)


def expected_membership_metadata(
    conn: sqlite3.Connection,
    *,
    asof: str,
) -> dict[str, dict[str, str]]:
    rows = conn.execute(
        """
        SELECT *
        FROM (
            SELECT
                m.ticker,
                m.membership_source_id,
                m.membership_basis,
                m.start_date AS membership_start_date,
                COALESCE(m.end_date, '') AS membership_end_date,
                m.membership_status,
                m.confidence AS membership_confidence,
                ROW_NUMBER() OVER (
                    PARTITION BY m.ticker
                    ORDER BY
                        CASE WHEN m.membership_basis = 'survivorship_corrected_pit_contract' THEN 0 ELSE 1 END,
                        m.confidence DESC,
                        m.start_date DESC
                ) AS membership_row_number
            FROM dim_universe_membership m
            WHERE m.model_family = 'machinery'
              AND m.start_date <= ?
              AND (m.end_date IS NULL OR m.end_date = '' OR m.end_date >= ?)
        )
        WHERE membership_row_number = 1
        ORDER BY ticker
        """,
        (asof, asof),
    ).fetchall()
    output: dict[str, dict[str, str]] = {}
    for row in rows:
        confidence = row["membership_confidence"]
        output[str(row["ticker"])] = {
            "membership_source_id": str(row["membership_source_id"] or ""),
            "membership_basis": str(row["membership_basis"] or ""),
            "membership_start_date": str(row["membership_start_date"] or ""),
            "membership_end_date": str(row["membership_end_date"] or ""),
            "membership_status": str(row["membership_status"] or ""),
            "membership_confidence": (
                f"{float(confidence):g}" if confidence is not None else ""
            ),
        }
    return output


def reconcile_membership_metadata(
    rows: list[dict[str, str]],
    *,
    expected: dict[str, dict[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    actual_tickers = {str(row.get("ticker") or "") for row in rows}
    expected_tickers = set(expected)
    if actual_tickers != expected_tickers:
        raise ValueError(
            f"historical membership mismatch missing={sorted(expected_tickers - actual_tickers)} "
            f"extra={sorted(actual_tickers - expected_tickers)}"
        )
    updated: list[dict[str, str]] = []
    changed: list[str] = []
    for source in rows:
        row = dict(source)
        ticker = str(row["ticker"])
        metadata = expected[ticker]
        if any(str(row.get(field) or "") != metadata[field] for field in MEMBERSHIP_FIELDS):
            row.update(metadata)
            changed.append(ticker)
        updated.append(row)
    return updated, sorted(changed)


def repair_membership_sidecars(
    conn: sqlite3.Connection,
    *,
    dashboard_root: Path,
    dates: list[str],
) -> dict[str, Any]:
    repaired_dates: list[str] = []
    repaired_tickers: set[str] = set()
    for asof in dates:
        output_dir = dashboard_root / asof
        rank_path = output_dir / "machinery_final_rank_table.csv"
        rows = read_rows(rank_path)
        expected = expected_membership_metadata(conn, asof=asof)
        updated, changed = reconcile_membership_metadata(rows, expected=expected)
        if not changed:
            continue
        lineage = build_financial_filing_lineage(
            conn,
            model_family="machinery",
            asof=asof,
            tickers=(str(row.get("ticker") or "") for row in updated),
        )
        updated = apply_financial_lineage_gate(updated, lineage)
        dashboard_manifest = publish_dashboard(
            output_dir=output_dir,
            rows=updated,
            asof=asof,
            allow_overwrite=True,
        )
        if dashboard_manifest.get("acceptance") != "PASS":
            raise ValueError(
                f"Historical machinery lineage policy failed for {asof}: "
                f"{dashboard_manifest['financial_filing_lineage'].get('blocking_issues', [])[:10]}"
            )
        repaired_dates.append(asof)
        repaired_tickers.update(changed)
    return {
        "acceptance": "PASS",
        "validated_date_count": len(dates),
        "repaired_date_count": len(repaired_dates),
        "repaired_dates": repaired_dates,
        "repaired_tickers": sorted(repaired_tickers),
    }


_SHARED_STAGE_NAMES = {
    "07_sync_machinery_sec_fundamentals.py": "07_sync_industrials_sec_fundamentals.py",
    "05_build_machinery_market_features.py": "05_build_industrials_market_features.py",
    "08_build_machinery_financial_features.py": "08_build_industrials_financial_features.py",
    "09_import_machinery_positioning.py": "09_import_industrials_positioning.py",
}
_IN_PROCESS_STAGE_MAINS: dict[str, Any] = {}


def _run_stage_in_process(
    *,
    wrapper_script: str,
    config_path: Path,
    db_path: Path,
    extra: list[str],
) -> None:
    shared_name = _SHARED_STAGE_NAMES[Path(wrapper_script).name]
    shared_path = PROJECT_ROOT / "industrials" / "scripts" / shared_name
    stage_main = _IN_PROCESS_STAGE_MAINS.get(shared_name)
    if stage_main is None:
        namespace = runpy.run_path(
            str(shared_path),
            run_name=f"_machinery_historical_{shared_path.stem}",
        )
        stage_main = namespace.get("main")
        if not callable(stage_main):
            raise RuntimeError(f"Shared stage does not expose main(): {shared_path}")
        _IN_PROCESS_STAGE_MAINS[shared_name] = stage_main

    previous_argv = sys.argv
    previous_fast_init = os.environ.get("INDUSTRIALS_FAST_INIT")
    previous_append = os.environ.get("INDUSTRIALS_HISTORICAL_APPEND")
    previous_logging_disable = logging.root.manager.disable
    try:
        sys.argv = [
            str(shared_path),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            *extra,
            "--model-family",
            "machinery",
        ]
        os.environ["INDUSTRIALS_FAST_INIT"] = "1"
        os.environ["INDUSTRIALS_HISTORICAL_APPEND"] = "1"
        logging.disable(logging.INFO)
        result = stage_main()
        if isinstance(result, int) and result != 0:
            raise RuntimeError(f"Shared stage returned exit code {result}: {shared_path}")
    finally:
        logging.disable(previous_logging_disable)
        sys.argv = previous_argv
        if previous_fast_init is None:
            os.environ.pop("INDUSTRIALS_FAST_INIT", None)
        else:
            os.environ["INDUSTRIALS_FAST_INIT"] = previous_fast_init
        if previous_append is None:
            os.environ.pop("INDUSTRIALS_HISTORICAL_APPEND", None)
        else:
            os.environ["INDUSTRIALS_HISTORICAL_APPEND"] = previous_append


def rebuild_features(
    *,
    config_path: Path,
    db_path: Path,
    asof: str,
    report_root: Path,
    stage_log_path: Path | None = None,
    rebuild_profiles: bool = True,
    profile_tickers: set[str] | None = None,
    in_process_stages: bool = False,
) -> None:
    report_root.mkdir(parents=True, exist_ok=True)
    scripts: list[tuple[str, list[str]]] = []
    if rebuild_profiles:
        profile_args = [
            "--asof",
            asof,
            "--profiles-only",
            "--include-historical",
            "--output-csv",
            str(report_root / "reporting_profile_snapshot.csv"),
        ]
        if profile_tickers:
            profile_args.extend(["--tickers", ",".join(sorted(profile_tickers))])
        scripts.append(
            (
            "industrials/machinery/scripts/07_sync_machinery_sec_fundamentals.py",
            profile_args,
            )
        )
    scripts.extend(
        [
        (
            "industrials/machinery/scripts/05_build_machinery_market_features.py",
            ["--asof", asof, "--output-csv", str(report_root / "market_feature_coverage.csv")],
        ),
        (
            "industrials/machinery/scripts/08_build_machinery_financial_features.py",
            [
                "--asof",
                asof,
                "--output-csv",
                str(report_root / "financial_feature_coverage.csv"),
                "--availability-output-csv",
                str(report_root / "financial_metric_availability.csv"),
                "--suppress-data-quality-issues",
            ],
        ),
        (
            "industrials/machinery/scripts/09_import_machinery_positioning.py",
            [
                "--asof",
                asof,
                # PIT membership, matching stages 05/08: without this the
                # positioning rebuild covers only currently-active tickers and
                # silently zeroes out delisted members' positioning features in
                # the survivorship panel — the exact rows the panel exists for.
                "--feature-membership-mode",
                "pit",
                "--features-only",
                "--output-csv",
                str(report_root / "positioning_import_coverage.csv"),
            ],
        ),
        ]
    )
    stage_log = None
    try:
        if stage_log_path is not None:
            stage_log_path.parent.mkdir(parents=True, exist_ok=True)
            stage_log = stage_log_path.open("w", encoding="utf-8")
        for script, extra in scripts:
            command = [
                sys.executable,
                script,
                "--config",
                str(config_path),
                "--db",
                str(db_path),
                *extra,
            ]
            if stage_log is not None:
                stage_log.write(f"$ {' '.join(command)}\n")
                stage_log.flush()
            if in_process_stages:
                _run_stage_in_process(
                    wrapper_script=script,
                    config_path=config_path,
                    db_path=db_path,
                    extra=extra,
                )
                continue
            subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                check=True,
                env={
                    **os.environ,
                    "INDUSTRIALS_FAST_INIT": "1",
                    "INDUSTRIALS_HISTORICAL_APPEND": "1",
                },
                stdout=stage_log,
                stderr=subprocess.STDOUT if stage_log is not None else None,
            )
    finally:
        if stage_log is not None:
            stage_log.close()


@contextmanager
def stage_report_workspace(
    *,
    report_root: Path,
    asof: str,
    retain_stage_reports: bool,
) -> Iterator[Path]:
    if retain_stage_reports:
        yield report_root / "stage_reports" / asof
        return
    with tempfile.TemporaryDirectory(
        prefix=f"machinery_history_{asof}_",
    ) as temporary_dir:
        yield Path(temporary_dir)


def existing_feature_dates(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT asof_date FROM feature_market_technical WHERE model_family = 'machinery'
        UNION
        SELECT asof_date FROM feature_financial_statement WHERE model_family = 'machinery'
        UNION
        SELECT asof_date FROM feature_financial_metric_availability WHERE model_family = 'machinery'
        UNION
        SELECT asof_date FROM feature_positioning WHERE model_family = 'machinery'
        """
    ).fetchall()
    return {str(row["asof_date"]) for row in rows}


def profile_rebuild_tickers(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
) -> dict[str, set[str]]:
    if not dates:
        return {}
    events = [
        (str(row["event_date"]), str(row["ticker"]))
        for row in conn.execute(
            """
            SELECT DISTINCT ticker,
                   SUBSTR(COALESCE(NULLIF(accepted_at, ''), filing_date), 1, 10) AS event_date
            FROM fact_sec_filing
            WHERE ticker IN (
                SELECT DISTINCT ticker
                FROM dim_universe_membership
                WHERE model_family = 'machinery'
            )
              AND SUBSTR(COALESCE(NULLIF(accepted_at, ''), filing_date), 1, 10) BETWEEN ? AND ?
            UNION
            SELECT DISTINCT ticker, start_date AS event_date
            FROM dim_universe_membership
            WHERE model_family = 'machinery'
              AND start_date BETWEEN ? AND ?
            """,
            (dates[0], dates[-1], dates[0], dates[-1]),
        ).fetchall()
        if str(row["event_date"] or "") and str(row["ticker"] or "")
    ]
    output: dict[str, set[str]] = defaultdict(set)
    intervals: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT ticker, start_date, COALESCE(end_date, '') AS end_date
        FROM dim_universe_membership
        WHERE model_family = 'machinery'
        """
    ).fetchall():
        intervals[str(row["ticker"])].append(
            (str(row["start_date"]), str(row["end_date"] or ""))
        )
    for event_date, ticker in events:
        index = bisect.bisect_left(dates, event_date)
        if index >= len(dates):
            continue
        mapped_date = dates[index]
        if any(
            start_date <= mapped_date and (not end_date or end_date >= mapped_date)
            for start_date, end_date in intervals.get(ticker, [])
        ):
            output[mapped_date].add(ticker)
    initial_members = conn.execute(
        """
        SELECT DISTINCT ticker
        FROM dim_universe_membership
        WHERE model_family = 'machinery'
          AND start_date <= ?
          AND COALESCE(end_date, '9999-12-31') >= ?
        """,
        (dates[0], dates[0]),
    ).fetchall()
    output[dates[0]].update(str(row["ticker"]) for row in initial_members)
    existing_snapshots = {
        (str(row["profile_asof_date"]), str(row["ticker"]))
        for row in conn.execute(
            """
            SELECT profile_asof_date, ticker
            FROM dim_issuer_reporting_profile_history
            WHERE model_family = 'machinery'
              AND profile_asof_date BETWEEN ? AND ?
            """,
            (dates[0], dates[-1]),
        ).fetchall()
    }
    for asof in list(output):
        output[asof] = {
            ticker for ticker in output[asof] if (asof, ticker) not in existing_snapshots
        }
        if not output[asof]:
            del output[asof]
    return output


def compact_date_features(
    conn: sqlite3.Connection,
    *,
    asof: str,
    preserve_dates: set[str],
) -> bool:
    if asof in preserve_dates:
        return False
    with conn:
        for table in (
            "feature_market_technical",
            "feature_financial_statement",
            "feature_financial_metric_availability",
            "feature_positioning",
        ):
            conn.execute(
                f"DELETE FROM {table} WHERE model_family = ? AND asof_date = ?",
                ("machinery", asof),
            )
    return True


def validate_historical_build_metadata(
    *,
    output_dir: Path,
    asof: str,
    expected_build_signature: str,
) -> None:
    build_metadata_path = output_dir / HISTORICAL_BUILD_METADATA_FILENAME
    if not build_metadata_path.exists():
        raise ValueError(
            f"Existing machinery history lacks build metadata: {build_metadata_path}"
        )
    build_metadata = json.loads(build_metadata_path.read_text(encoding="utf-8"))
    if str(build_metadata.get("asof_date") or "") != asof:
        raise ValueError(
            f"Historical build metadata asof mismatch: {build_metadata_path}"
        )
    if (
        str(build_metadata.get("historical_build_signature") or "")
        != expected_build_signature
    ):
        raise ValueError(
            f"Historical build signature is stale: {build_metadata_path}"
        )


def existing_output_rows(
    *,
    output_dir: Path,
    sector_output_root: Path,
    asof: str,
    expected_build_signature: str,
) -> tuple[list[dict[str, str]], int]:
    validate_historical_build_metadata(
        output_dir=output_dir,
        asof=asof,
        expected_build_signature=expected_build_signature,
    )
    manifest_path = output_dir / "machinery_final_rank_table_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing machinery dashboard manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("acceptance") or "") != "PASS":
        raise ValueError(f"Existing machinery dashboard manifest did not pass: {manifest_path}")
    rows = load_validated_sidecar(output_dir, asof=asof)
    adapter_count = validate_portfolio_handoff(
        sector_output_root=sector_output_root,
        asof=asof,
    )
    if adapter_count != len(rows):
        raise ValueError(
            f"Existing machinery adapter rows={adapter_count} sidecar rows={len(rows)} asof={asof}"
        )
    return rows, adapter_count


def checkpoint_report(
    *,
    report_csv: Path,
    report: list[dict[str, Any]],
    planned_dates: int,
    start_date: str,
    end_date: str,
    frequency: str,
    end_date_excluded: bool,
    build_metadata: dict[str, Any],
    final: bool,
) -> dict[str, object]:
    write_csv_atomic(report_csv, REPORT_FIELDS, report)
    failures = [row for row in report if row["status"] == "FAIL"]
    complete = len(report) == planned_dates
    acceptance = "PASS" if final and complete and not failures else "FAIL" if failures else "RUNNING"
    summary: dict[str, object] = {
        "acceptance": acceptance,
        "start_date": start_date,
        "end_date": end_date,
        "frequency": frequency,
        "end_date_excluded": end_date_excluded,
        "planned_dates": planned_dates,
        "completed_dates": len(report),
        "passed_dates": sum(str(row["status"]).startswith("PASS") for row in report),
        "reused_dates": sum(row["existing_output_reused_flag"] == 1 for row in report),
        "rebuilt_dates": sum(row["feature_rebuilt_flag"] == 1 for row in report),
        "failed_dates": len(failures),
        "historical_build_signature": build_metadata[
            "historical_build_signature"
        ],
        "historical_build_contract_version": build_metadata[
            "historical_build_contract_version"
        ],
        "disclosure_parser_version": build_metadata["disclosure_parser_version"],
        "report_csv": str(report_csv),
    }
    write_json_atomic(report_csv.with_suffix(".json"), summary)
    return summary


def validate_portfolio_handoff(*, sector_output_root: Path, asof: str) -> int:
    result = run_adapter(
        {
            "model_family": "machinery",
            "adapter": "industrial_family",
            "file_mode": "dated",
            "file_path": "industrials/machinery/dashboard/{yyyy-mm-dd}/machinery_final_rank_table.csv",
            "sector": "Industrials",
            "industry": "Machinery",
            "industry_aggregate": "Machinery",
            "require_oos_score_valid": True,
        },
        sector_output_root,
        asof,
    )
    if not result.rows:
        raise ValueError("Portfolio adapter returned no machinery rows")
    if result.source_asof_date != asof:
        raise ValueError(
            f"Portfolio adapter source_asof_date={result.source_asof_date} expected={asof}"
        )
    if any(row.investable_eligible for row in result.rows):
        raise ValueError("Shadow machinery rows must not be investable")
    if any(row.oos_score_valid_flag for row in result.rows):
        raise ValueError("Shadow machinery rows must not be OOS-valid")
    return len(result.rows)


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    start_date = parse_asof(args.start_date or str(cfg_get(config, "machinery_scoring.history_start_date", "2019-01-02")))
    end_date = parse_asof(args.end_date)
    if end_date < start_date:
        raise ValueError("end-date must be on or after start-date")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    dashboard_root = resolve_path(cfg_get(config, "machinery_scoring.dashboard_root"), base_dir=base_dir)
    report_root = dashboard_root.parent / "historical_backfill"
    report_root.mkdir(parents=True, exist_ok=True)
    if args.coverage_only and args.repair_membership_sidecars:
        raise ValueError("--coverage-only and --repair-membership-sidecars are mutually exclusive")
    if args.repair_membership_sidecars:
        repair_dates = published_dashboard_dates(
            dashboard_root,
            start_date=start_date,
            end_date=end_date,
        )
        if not repair_dates:
            raise ValueError(
                f"No machinery historical sidecars from {start_date} through {end_date}"
            )
        with connect(
            db_path,
            timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)),
        ) as conn:
            summary = repair_membership_sidecars(
                conn,
                dashboard_root=dashboard_root,
                dates=repair_dates,
            )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if args.coverage_only:
        coverage_dates = published_dashboard_dates(
            dashboard_root,
            start_date=start_date,
            end_date=end_date,
        )
        if not coverage_dates:
            raise ValueError(
                f"No validated machinery historical sidecars from {start_date} through {end_date}"
            )
        with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
            summary = build_combined_historical_coverage(
                conn,
                dates=coverage_dates,
                dashboard_root=dashboard_root,
                report_root=report_root,
                start_date=start_date,
                end_date=end_date,
            )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary["acceptance"] == "PASS" else 1
    sector_output_root = dashboard_root.parents[2]
    benchmark = str(cfg_get(config, "industrials_universe.benchmark_ticker", "XLI"))
    primary_source = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted"))
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        dates = available_dates(
            conn,
            start_date=start_date,
            end_date=end_date,
            benchmark=benchmark,
            primary_source=primary_source,
        )
    # Exclude the end date BEFORE weekly downsampling: if the end date is a
    # week's representative (last trading day), excluding it afterwards would
    # drop that week entirely instead of falling back to an earlier day.
    if args.exclude_end_date:
        dates = [asof for asof in dates if asof != end_date]
    if args.frequency == "weekly":
        dates = weekly_dates(dates)
    if args.max_dates > 0:
        dates = dates[: args.max_dates]
    if not dates:
        raise ValueError(
            f"No {benchmark} price dates or machinery feature dates available from {start_date} through {end_date}"
        )
    weights = cfg_get(config, "machinery_scoring.component_weights", {}) or {}
    if not isinstance(weights, dict):
        raise ValueError("machinery_scoring.component_weights must be a mapping")
    policy_path = resolve_path(
        cfg_get(config, "scoring_policy.families.machinery.eligibility_policy_csv"),
        base_dir=base_dir,
    )
    policy_lock_date_raw = str(cfg_get(config, "machinery_scoring.historical_policy_lock_date", "") or "").strip()
    if not policy_lock_date_raw:
        # Falling back to end_date (which defaults to today) would make
        # backfills silently non-reproducible across run days.
        raise ValueError(
            "machinery_scoring.historical_policy_lock_date must be set for reproducible backfills"
        )
    policy_lock_date = parse_asof(policy_lock_date_raw)
    eligibility_policies = load_eligibility_policy(policy_path, asof=policy_lock_date)
    if not eligibility_policies:
        raise ValueError(
            f"No machinery scoring eligibility policies are effective at lock date {policy_lock_date}"
        )
    build_metadata = historical_build_metadata(
        config,
        policy_lock_date=policy_lock_date,
        required_metrics=required_metric_names(),
    )
    build_signature = str(build_metadata["historical_build_signature"])
    market_sources = tuple(
        dict.fromkeys(
            [
                str(cfg_get(config, "market_data_policy.scoring_primary_source", "") or "").strip(),
                *[
                    str(value or "").strip()
                    for value in (cfg_get(config, "market_data_policy.scoring_fallback_sources", []) or [])
                ],
            ]
        )
    )
    market_sources = tuple(source for source in market_sources if source)
    report: list[dict[str, Any]] = []
    report_csv = report_root / f"machinery_history_{start_date}_{end_date}_{args.frequency}.csv"
    with connect(db_path, timeout_sec=timeout) as conn:
        initialized_feature_dates = existing_feature_dates(conn)
        preserve_feature_dates = (
            {max(initialized_feature_dates)}
            if args.compact_working_features and initialized_feature_dates
            else initialized_feature_dates
        )
        reporting_profile_tickers = profile_rebuild_tickers(conn, dates=dates)
    for asof in dates:
        output_dir = dashboard_root / asof
        try:
            reused = False
            feature_rebuilt = False
            compacted = False
            reuse_validation = "not_requested"
            historical_rows: list[dict[str, str]]
            portfolio_adapter_row_count: int
            if args.resume_existing and not args.force and output_dir.exists():
                try:
                    historical_rows, portfolio_adapter_row_count = existing_output_rows(
                        output_dir=output_dir,
                        sector_output_root=sector_output_root,
                        asof=asof,
                        expected_build_signature=build_signature,
                    )
                    with connect(db_path, timeout_sec=timeout) as conn:
                        validate_existing_membership(
                            conn,
                            asof=asof,
                            rows=historical_rows,
                        )
                    reused = True
                    reuse_validation = "build_signature_match"
                except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                    reused = False
                    reuse_validation = f"rebuild_required:{type(exc).__name__}"
            elif args.force:
                reuse_validation = "force_rebuild"
            if (
                not reused
                and reuse_validation.startswith("rebuild_required:")
                and not args.rebuild_features
            ):
                raise ValueError(
                    "Historical output is stale and --rebuild-features was not supplied"
                )
            if not reused and args.rebuild_features:
                with stage_report_workspace(
                    report_root=report_root,
                    asof=asof,
                    retain_stage_reports=bool(args.retain_stage_reports),
                ) as stage_report_root:
                    rebuild_features(
                        config_path=config_path,
                        db_path=db_path,
                        asof=asof,
                        report_root=stage_report_root,
                        stage_log_path=stage_report_root / "stages.log",
                        rebuild_profiles=asof in reporting_profile_tickers,
                        profile_tickers=reporting_profile_tickers.get(asof),
                        in_process_stages=args.in_process_stages,
                    )
                feature_rebuilt = True
            if not reused:
                with connect(db_path, timeout_sec=timeout) as conn:
                    feature_rows = build_scoring_feature_rows(
                        conn,
                        asof=asof,
                        eligibility_policies=eligibility_policies,
                        market_source_priority=market_sources,
                        financial_source_priority=(
                            str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts")),
                        ),
                        positioning_source_priority=(
                            str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite")),
                        ),
                        component_weights=weights,
                        min_score_confidence=float(cfg_get(config, "machinery_scoring.min_score_confidence", 0.40)),
                        max_staleness_days=int(cfg_get(config, "market_data_policy.max_staleness_days", 7)),
                        min_avg_dollar_volume=float(
                            cfg_get(config, "market_data_policy.min_avg_dollar_volume_60d_for_full_features", 5000000)
                        ),
                        negative_profit_valuation_score_cap=float(
                            cfg_get(
                                config,
                                "machinery_scoring.negative_profit_valuation_score_cap",
                                25.0,
                            )
                        ),
                    )
                rank_rows = finalize_rank_rows(
                    feature_rows,
                    score_model_version=str(cfg_get(config, "machinery_scoring.score_model_version")),
                    model_version=str(cfg_get(config, "machinery_scoring.model_version")),
                    scoring_contract_version=str(cfg_get(config, "machinery_scoring.contract_version")),
                )
                historical_rows = survivorship_sidecar(rank_rows)
                rank_ready_count = sum(row["rank_ready_flag"] == "1" for row in historical_rows)
                with connect(db_path, timeout_sec=timeout) as conn:
                    lineage = build_financial_filing_lineage(
                        conn,
                        model_family="machinery",
                        asof=asof,
                        tickers=(str(row.get("ticker") or "") for row in historical_rows),
                    )
                historical_rows = apply_financial_lineage_gate(historical_rows, lineage)
                if rank_ready_count == 0 and not args.allow_zero_eligible:
                    raise ValueError("No rank-ready machinery rows; build point-in-time source features before publishing")
                dashboard_manifest = publish_dashboard(
                    output_dir=output_dir,
                    rows=historical_rows,
                    asof=asof,
                    allow_overwrite=(
                        args.force
                        or (args.resume_existing and output_dir.exists())
                    ),
                )
                if dashboard_manifest.get("acceptance") != "PASS":
                    raise ValueError(
                        f"Historical machinery lineage policy failed for {asof}: "
                        f"{dashboard_manifest['financial_filing_lineage'].get('blocking_issues', [])[:10]}"
                    )
                portfolio_adapter_row_count = validate_portfolio_handoff(
                    sector_output_root=sector_output_root,
                    asof=asof,
                )
                write_json_atomic(
                    output_dir / HISTORICAL_BUILD_METADATA_FILENAME,
                    {
                        **build_metadata,
                        "acceptance": "PASS",
                        "asof_date": asof,
                    },
                )
            rank_ready_count = sum(row["rank_ready_flag"] == "1" for row in historical_rows)
            if args.compact_working_features and (feature_rebuilt or reused):
                with connect(db_path, timeout_sec=timeout) as conn:
                    compacted = compact_date_features(
                        conn,
                        asof=asof,
                        preserve_dates=preserve_feature_dates,
                    )
            report.append(
                {
                    "asof_date": asof,
                    "policy_lock_date": policy_lock_date,
                    "status": "PASS_EXISTING" if reused else "PASS",
                    "row_count": len(historical_rows),
                    "rank_ready_count": rank_ready_count,
                    "portfolio_adapter_row_count": portfolio_adapter_row_count,
                    "feature_rebuilt_flag": int(feature_rebuilt),
                    "existing_output_reused_flag": int(reused),
                    "working_features_compacted_flag": int(compacted),
                    "historical_build_signature": build_signature,
                    "disclosure_parser_version": DISCLOSURE_PARSER_VERSION,
                    "reuse_validation": reuse_validation,
                    "output_dir": str(output_dir),
                    "error": "",
                }
            )
        except Exception as exc:
            report.append(
                {
                    "asof_date": asof,
                    "policy_lock_date": policy_lock_date,
                    "status": "FAIL",
                    "row_count": 0,
                    "rank_ready_count": 0,
                    "portfolio_adapter_row_count": 0,
                    "feature_rebuilt_flag": 0,
                    "existing_output_reused_flag": 0,
                    "working_features_compacted_flag": 0,
                    "historical_build_signature": build_signature,
                    "disclosure_parser_version": DISCLOSURE_PARSER_VERSION,
                    "reuse_validation": reuse_validation,
                    "output_dir": str(output_dir),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if not args.continue_on_error:
                break
        checkpoint_report(
            report_csv=report_csv,
            report=report,
            planned_dates=len(dates),
            start_date=start_date,
            end_date=end_date,
            frequency=args.frequency,
            end_date_excluded=bool(args.exclude_end_date),
            build_metadata=build_metadata,
            final=False,
        )
        if len(report) % 25 == 0 or len(report) == len(dates):
            print(
                json.dumps(
                    {
                        "progress": f"{len(report)}/{len(dates)}",
                        "asof_date": asof,
                        "status": report[-1]["status"],
                    },
                    sort_keys=True,
                )
            )
    summary = checkpoint_report(
        report_csv=report_csv,
        report=report,
        planned_dates=len(dates),
        start_date=start_date,
        end_date=end_date,
        frequency=args.frequency,
        end_date_excluded=bool(args.exclude_end_date),
        build_metadata=build_metadata,
        final=True,
    )
    if summary["acceptance"] == "PASS":
        coverage_start_date = parse_asof(
            str(cfg_get(config, "machinery_scoring.history_start_date", "2019-01-02"))
        )
        coverage_dates = published_dashboard_dates(
            dashboard_root,
            start_date=coverage_start_date,
            end_date=end_date,
        )
        with connect(db_path, timeout_sec=timeout) as conn:
            combined = build_combined_historical_coverage(
                conn,
                dates=coverage_dates,
                dashboard_root=dashboard_root,
                report_root=report_root,
                start_date=coverage_start_date,
                end_date=end_date,
            )
        summary["combined_coverage_acceptance"] = combined["acceptance"]
        summary["combined_coverage_json"] = str(
            report_root / "machinery_combined_historical_coverage.json"
        )
        if combined["acceptance"] != "PASS":
            summary["acceptance"] = "FAIL"
        write_json_atomic(report_csv.with_suffix(".json"), summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["acceptance"] == "PASS" else 1


def locked_main() -> int:
    if os.environ.get("INDUSTRIALS_REFRESH_LOCK_HELD") == "1":
        return main()
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    known, _ = preliminary.parse_known_args()
    config_path = known.config.expanduser().resolve()
    config = load_yaml(config_path)
    dashboard_root = resolve_path(
        cfg_get(config, "machinery_scoring.dashboard_root"),
        base_dir=config_path.parent,
    )
    lock_path = dashboard_root.parent.parent / ".industrials_refresh.lock"
    with RefreshLock(lock_path):
        previous = os.environ.get("INDUSTRIALS_REFRESH_LOCK_HELD")
        os.environ["INDUSTRIALS_REFRESH_LOCK_HELD"] = "1"
        try:
            return main()
        finally:
            if previous is None:
                os.environ.pop("INDUSTRIALS_REFRESH_LOCK_HELD", None)
            else:
                os.environ["INDUSTRIALS_REFRESH_LOCK_HELD"] = previous


if __name__ == "__main__":
    raise SystemExit(locked_main())
