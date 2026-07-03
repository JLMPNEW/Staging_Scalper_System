#!/usr/bin/env python3
"""Mark strict-OOS provenance on med_device_daily_scores rows.

Script 13 always publishes oos_score_valid_flag=0: scoring never self-certifies.
This promoter is the sole writer of oos_score_valid_flag=1 /
calibration_sample_role='strict_oos'. For each as-of date it requires evidence
that already exists on disk before promoting anything:

1. the dated snapshot came from the PIT-validated historical backfill path
   (a terminal 'success' record in the script 21 backfill manifest);
2. script 75 validated the as-of with zero strict CRITICAL failures,
   including the panel-level 'ALL' checks;
3. every promoted row certifies the survivorship-corrected panel; and
4. no row in the snapshot carries publisher-defaulted Stage 11 metadata.

Only rows meeting the full row-level Stage 11 eligibility contract are
promoted; everything else keeps the fail-closed default written by script 13.
The script is idempotent (re-runs promote zero additional rows) and supports
--dry-run. Promotions are mirrored into the dated review-pack snapshot CSV so
the portfolio-layer adapters see the same provenance as the database.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("mark_med_device_oos_provenance")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STAGE11_CALIBRATION_PANEL_SOURCE = "med_devices_survivorship_corrected_score_review_pack"
DAILY_SNAPSHOT_FILENAME = "med_device_daily_composite_scores.csv"
SUMMARY_FILENAME = "med_device_oos_provenance_summary.csv"
MANIFEST_TERMINAL_OK_STATUSES = {"success", "skipped_manifest", "skipped_existing"}
REQUIRED_SCORE_COLUMNS = {
    "asof_date",
    "company_id",
    "oos_score_valid_flag",
    "calibration_sample_role",
    "calibration_only",
    "score_zero_is_missing_flag",
    "native_score_value",
    "composite_score",
    "source_snapshot_asof_date",
    "research_calibration_input_eligible_flag",
    "research_calibration_status",
    "research_calibration_reason",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
    "updated_at",
}
# Row-level strict-OOS contract. A row may only be promoted when script 13
# published it as a fully eligible Stage 11 research-calibration input from the
# survivorship-corrected panel, the snapshot provenance matches the folder
# as-of date, and it is not a calibration-only synthetic member. The role
# check keeps the UPDATE idempotent and refuses rows script 13 excluded.
STRICT_OOS_ROW_CRITERIA_TEMPLATE = """
    {p}research_calibration_input_eligible_flag = 1
    AND {p}research_calibration_status = 'eligible'
    AND {p}research_calibration_reason = 'valid_research_calibration_input'
    AND {p}stage11_calibration_input_eligible_flag = 1
    AND {p}stage11_calibration_input_reason = 'ok'
    AND {p}stage11_calibration_panel_source = ?
    AND {p}survivorship_corrected_panel_flag = 1
    AND COALESCE({p}calibration_only, 0) = 0
    AND COALESCE({p}score_zero_is_missing_flag, 0) = 0
    AND COALESCE({p}native_score_value, 0.0) > 0.0
    AND COALESCE({p}composite_score, 0.0) > 0.0
    AND TRIM(COALESCE({p}source_snapshot_asof_date, '')) = {p}asof_date
    AND {p}calibration_sample_role IN ('research_calibration_input', 'strict_oos')
"""


def strict_oos_row_criteria(prefix: str = "") -> str:
    return STRICT_OOS_ROW_CRITERIA_TEMPLATE.format(p=prefix)
SUMMARY_FIELDS = [
    "asof_date",
    "evaluated_at_utc",
    "dry_run",
    "asof_promoted",
    "skip_reason",
    "manifest_status",
    "validator_checks",
    "validator_critical_failures",
    "publisher_defaulted_rows",
    "candidate_rows",
    "newly_promoted_rows",
    "previously_promoted_rows",
    "stale_flagged_rows",
    "snapshot_csv_rows_updated",
    "snapshot_csv_note",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Promote validated med-device daily score rows to strict-OOS provenance."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Comma-separated as-of dates; default discovers from the score table.")
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument("--reports-root", type=Path, default=None)
    parser.add_argument("--oos-validation-csv", type=Path, default=None)
    parser.add_argument("--manifest-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument(
        "--skip-snapshot-csv",
        action="store_true",
        help="Do not mirror promotions into the dated review-pack snapshot CSVs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be promoted without writing the database, snapshots, or summary CSV.",
    )
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [str(field) for field in (reader.fieldnames or [])]
        return fieldnames, [{str(key): str(value or "") for key, value in row.items()} for row in reader]


def write_csv_atomic(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def discover_asofs(conn: Any, *, explicit: str, start: str, end: str) -> list[str]:
    if explicit.strip():
        dates = [item.strip() for item in explicit.split(",") if item.strip()]
    else:
        dates = [
            str(row["asof_date"] or "")
            for row in conn.execute("SELECT DISTINCT asof_date FROM med_device_daily_scores").fetchall()
        ]
    start_date = parse_date(start)
    end_date = parse_date(end)
    out: list[str] = []
    for item in dates:
        parsed = parse_date(item)
        if parsed is None:
            continue
        if start_date is not None and parsed < start_date:
            continue
        if end_date is not None and parsed > end_date:
            continue
        out.append(parsed.isoformat())
    return sorted(set(out))


def ensure_score_columns(conn: Any) -> None:
    existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(med_device_daily_scores)").fetchall()}
    missing = sorted(REQUIRED_SCORE_COLUMNS - existing)
    if missing:
        raise RuntimeError(
            "med_device_daily_scores is missing strict-OOS provenance columns "
            f"({', '.join(missing)}); rerun script 13 to migrate the table before promoting."
        )


def load_validator_results(path: Path) -> tuple[dict[str, dict[str, int]], int, bool]:
    """Per-asof strict check counts from script 75, plus panel-level ('ALL') critical failures."""
    if not path.exists():
        return {}, 0, False
    _, rows = read_csv_rows(path)
    per_asof: dict[str, dict[str, int]] = {}
    panel_critical_failures = 0
    for row in rows:
        asof = str(row.get("asof_date") or "").strip()
        is_critical_failure = (
            str(row.get("severity") or "").strip() == "CRITICAL"
            and str(row.get("status") or "").strip() == "FAIL"
        )
        if asof == "ALL":
            panel_critical_failures += int(is_critical_failure)
            continue
        if not DATE_RE.match(asof):
            continue
        bucket = per_asof.setdefault(asof, {"checks": 0, "critical_failures": 0})
        bucket["checks"] += 1
        bucket["critical_failures"] += int(is_critical_failure)
    return per_asof, panel_critical_failures, True


def load_manifest_statuses(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    _, rows = read_csv_rows(path)
    out: dict[str, list[str]] = {}
    for row in rows:
        asof = str(row.get("asof_date") or "").strip()
        if not DATE_RE.match(asof):
            continue
        out.setdefault(asof, []).append(str(row.get("status") or "").strip())
    return out


def manifest_gate(statuses: list[str]) -> tuple[bool, str]:
    """The backfill path counts as validated only when the as-of finished successfully.

    Resume skips ('skipped_manifest'/'skipped_existing') are accepted only when an
    actual 'success' record exists; a trailing failure fails closed.
    """
    if not statuses:
        return False, "missing"
    terminal = statuses[-1]
    if terminal not in MANIFEST_TERMINAL_OK_STATUSES:
        return False, terminal or "unknown"
    if "success" not in statuses:
        return False, f"{terminal}_without_success"
    return True, terminal


def count_rows(conn: Any, sql: str, params: tuple[Any, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def evaluate_asof(
    conn: Any,
    *,
    asof: str,
    manifest_statuses: dict[str, list[str]],
    validator_per_asof: dict[str, dict[str, int]],
    validator_available: bool,
    panel_critical_failures: int,
    strict_oos_start: date | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "asof_date": asof,
        "skip_reason": "",
        "manifest_status": "",
        "validator_checks": 0,
        "validator_critical_failures": 0,
        "publisher_defaulted_rows": 0,
        "candidate_rows": 0,
        "newly_promoted_rows": 0,
        "previously_promoted_rows": 0,
        "stale_flagged_rows": 0,
    }
    reasons: list[str] = []
    manifest_ok, manifest_status = manifest_gate(manifest_statuses.get(asof, []))
    result["manifest_status"] = manifest_status
    if not manifest_ok:
        reasons.append(f"backfill_manifest_not_success:{manifest_status}")
    if not validator_available:
        reasons.append("oos_validation_csv_missing")
    else:
        checks = validator_per_asof.get(asof)
        if checks is None or checks["checks"] <= 0:
            reasons.append("asof_not_validated_by_script_75")
        else:
            result["validator_checks"] = checks["checks"]
            result["validator_critical_failures"] = checks["critical_failures"] + panel_critical_failures
            if result["validator_critical_failures"] > 0:
                reasons.append("oos_validation_critical_failures")
    if strict_oos_start is not None:
        asof_day = parse_date(asof)
        if asof_day is None or asof_day < strict_oos_start:
            reasons.append("before_strict_oos_start_date")
    publisher_defaulted = count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM med_device_daily_scores
        WHERE asof_date = ?
          AND (TRIM(COALESCE(stage11_calibration_panel_source, '')) = ''
               OR TRIM(COALESCE(research_calibration_status, '')) = '')
        """,
        (asof,),
    )
    result["publisher_defaulted_rows"] = publisher_defaulted
    if publisher_defaulted > 0:
        reasons.append("publisher_defaulted_stage11_metadata")
    result["candidate_rows"] = count_rows(
        conn,
        f"SELECT COUNT(*) FROM med_device_daily_scores WHERE asof_date = ? AND {strict_oos_row_criteria()}",
        (asof, STAGE11_CALIBRATION_PANEL_SOURCE),
    )
    result["previously_promoted_rows"] = count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM med_device_daily_scores
        WHERE asof_date = ? AND oos_score_valid_flag = 1 AND calibration_sample_role = 'strict_oos'
        """,
        (asof,),
    )
    flagged_meeting_criteria = count_rows(
        conn,
        f"""
        SELECT COUNT(*)
        FROM med_device_daily_scores
        WHERE asof_date = ? AND oos_score_valid_flag = 1 AND {strict_oos_row_criteria()}
        """,
        (asof, STAGE11_CALIBRATION_PANEL_SOURCE),
    )
    flagged_total = count_rows(
        conn,
        "SELECT COUNT(*) FROM med_device_daily_scores WHERE asof_date = ? AND oos_score_valid_flag = 1",
        (asof,),
    )
    result["stale_flagged_rows"] = flagged_total - flagged_meeting_criteria
    if result["stale_flagged_rows"] > 0:
        LOGGER.warning(
            "asof=%s has %d previously promoted row(s) no longer meeting the strict-OOS contract; "
            "not demoting (rebuild the snapshot with script 13 and re-run scripts 75/76 to recertify).",
            asof,
            result["stale_flagged_rows"],
        )
    result["skip_reason"] = ";".join(dict.fromkeys(reasons))
    return result


def promote_asof(conn: Any, *, asof: str, dry_run: bool) -> int:
    pending_sql = f"""
        asof_date = ?
          AND {strict_oos_row_criteria()}
          AND (oos_score_valid_flag <> 1 OR calibration_sample_role <> 'strict_oos')
    """
    if dry_run:
        return count_rows(
            conn,
            f"SELECT COUNT(*) FROM med_device_daily_scores WHERE {pending_sql}",
            (asof, STAGE11_CALIBRATION_PANEL_SOURCE),
        )
    cursor = conn.execute(
        f"""
        UPDATE med_device_daily_scores
        SET oos_score_valid_flag = 1,
            calibration_sample_role = 'strict_oos',
            updated_at = ?
        WHERE {pending_sql}
        """,
        (utc_now(), asof, STAGE11_CALIBRATION_PANEL_SOURCE),
    )
    return int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0)


def promoted_tickers(conn: Any, *, asof: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT UPPER(TRIM(c.ticker)) AS ticker
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        WHERE s.asof_date = ? AND s.oos_score_valid_flag = 1 AND s.calibration_sample_role = 'strict_oos'
        """,
        (asof,),
    ).fetchall()
    return {str(row["ticker"] or "").strip() for row in rows if str(row["ticker"] or "").strip()}


def candidate_tickers(conn: Any, *, asof: str) -> set[str]:
    rows = conn.execute(
        f"""
        SELECT UPPER(TRIM(c.ticker)) AS ticker
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        WHERE s.asof_date = ? AND {strict_oos_row_criteria("s.")}
        """,
        (asof, STAGE11_CALIBRATION_PANEL_SOURCE),
    ).fetchall()
    return {str(row["ticker"] or "").strip() for row in rows if str(row["ticker"] or "").strip()}


def sync_snapshot_csv(path: Path, *, tickers: set[str], dry_run: bool) -> tuple[int, str]:
    """Mirror database promotions into the dated review-pack snapshot CSV.

    The portfolio-layer adapters read the dated CSV, not the database, so the
    two projections must agree. Only the two provenance columns are touched and
    only for tickers the database certifies as strict_oos.
    """
    if not tickers:
        return 0, "no_promoted_tickers"
    if not path.exists():
        return 0, "snapshot_csv_missing"
    fieldnames, rows = read_csv_rows(path)
    if "oos_score_valid_flag" not in fieldnames or "calibration_sample_role" not in fieldnames:
        return 0, "snapshot_csv_missing_provenance_columns"
    updated = 0
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker not in tickers:
            continue
        if str(row.get("oos_score_valid_flag") or "").strip() == "1" and str(
            row.get("calibration_sample_role") or ""
        ).strip() == "strict_oos":
            continue
        row["oos_score_valid_flag"] = "1"
        row["calibration_sample_role"] = "strict_oos"
        updated += 1
    if updated and not dry_run:
        write_csv_atomic(path, fieldnames, rows)
    return updated, "ok"


def write_summary(path: Path, new_rows: list[dict[str, Any]]) -> None:
    processed = {str(row.get("asof_date") or "") for row in new_rows}
    existing: list[dict[str, Any]] = []
    if path.exists():
        _, previous = read_csv_rows(path)
        existing = [row for row in previous if str(row.get("asof_date") or "") not in processed]
    merged = existing + [dict(row) for row in new_rows]
    merged.sort(key=lambda row: str(row.get("asof_date") or ""))
    write_csv_atomic(path, SUMMARY_FIELDS, merged)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    reports_root = (
        args.reports_root.expanduser().resolve()
        if args.reports_root
        else resolve_path(
            cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"),
            base_dir=base_dir,
        )
    )
    oos_validation_csv = (
        args.oos_validation_csv.expanduser().resolve()
        if args.oos_validation_csv
        else resolve_path(
            cfg_get(
                config,
                "historical_backfill.oos_validation_csv",
                "../output/med_devices_reports/oos_validation/med_device_historical_snapshot_oos_validation.csv",
            ),
            base_dir=base_dir,
        )
    )
    manifest_csv = (
        args.manifest_csv.expanduser().resolve()
        if args.manifest_csv
        else resolve_path(
            cfg_get(
                config,
                "historical_backfill.manifest_csv",
                "../output/med_devices_reports/historical_backfill/weekly_score_backfill_manifest.csv",
            ),
            base_dir=base_dir,
        )
    )
    summary_raw = str(cfg_get(config, "historical_backfill.oos_provenance_summary_csv", "") or "").strip()
    summary_csv = (
        args.summary_csv.expanduser().resolve()
        if args.summary_csv
        else resolve_path(summary_raw, base_dir=base_dir)
        if summary_raw
        else reports_root / SUMMARY_FILENAME
    )
    strict_oos_start_raw = str(cfg_get(config, "historical_backfill.strict_oos_start_date", "") or "").strip()
    strict_oos_start = parse_date(strict_oos_start_raw)
    if strict_oos_start_raw and strict_oos_start is None:
        raise RuntimeError(
            "Invalid historical_backfill.strict_oos_start_date: "
            f"{strict_oos_start_raw!r}. Use YYYY-MM-DD or leave blank."
        )
    dry_run = bool(args.dry_run)
    validator_per_asof, panel_critical_failures, validator_available = load_validator_results(oos_validation_csv)
    manifest_statuses = load_manifest_statuses(manifest_csv)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_score_columns(conn)
        asofs = discover_asofs(conn, explicit=args.asof, start=args.start_asof, end=args.end_asof)
        if not asofs:
            raise RuntimeError(
                "OOS provenance promotion found zero as-of dates. "
                f"explicit_asof={args.asof!r} start={args.start_asof!r} end={args.end_asof!r}"
            )
        run_id = None
        if not dry_run:
            run_id = start_run(conn, run_type="mark_med_device_oos_provenance", input_path=config_path)
        try:
            evaluated_at = utc_now()
            summary_rows: list[dict[str, Any]] = []
            promoted_asofs = 0
            total_promoted = 0
            for asof in asofs:
                result = evaluate_asof(
                    conn,
                    asof=asof,
                    manifest_statuses=manifest_statuses,
                    validator_per_asof=validator_per_asof,
                    validator_available=validator_available,
                    panel_critical_failures=panel_critical_failures,
                    strict_oos_start=strict_oos_start,
                )
                snapshot_updated = 0
                snapshot_note = "skipped"
                if not result["skip_reason"]:
                    result["newly_promoted_rows"] = promote_asof(conn, asof=asof, dry_run=dry_run)
                    promoted_asofs += 1
                    total_promoted += result["newly_promoted_rows"]
                    if not args.skip_snapshot_csv:
                        tickers = promoted_tickers(conn, asof=asof)
                        if dry_run:
                            # The database was not updated; plan against the post-promotion set.
                            tickers |= candidate_tickers(conn, asof=asof)
                        snapshot_updated, snapshot_note = sync_snapshot_csv(
                            reports_root / asof / DAILY_SNAPSHOT_FILENAME,
                            tickers=tickers,
                            dry_run=dry_run,
                        )
                else:
                    LOGGER.info("asof=%s not promoted: %s", asof, result["skip_reason"])
                summary_rows.append(
                    {
                        "asof_date": asof,
                        "evaluated_at_utc": evaluated_at,
                        "dry_run": int(dry_run),
                        "asof_promoted": int(not result["skip_reason"]),
                        "skip_reason": result["skip_reason"],
                        "manifest_status": result["manifest_status"],
                        "validator_checks": result["validator_checks"],
                        "validator_critical_failures": result["validator_critical_failures"],
                        "publisher_defaulted_rows": result["publisher_defaulted_rows"],
                        "candidate_rows": result["candidate_rows"],
                        "newly_promoted_rows": result["newly_promoted_rows"],
                        "previously_promoted_rows": result["previously_promoted_rows"],
                        "stale_flagged_rows": result["stale_flagged_rows"],
                        "snapshot_csv_rows_updated": snapshot_updated,
                        "snapshot_csv_note": snapshot_note,
                    }
                )
            if not dry_run:
                write_summary(summary_csv, summary_rows)
            message = (
                f"asofs={len(asofs)} promoted_asofs={promoted_asofs} "
                f"newly_promoted_rows={total_promoted} summary={summary_csv} dry_run={int(dry_run)}"
            )
            if run_id is not None:
                finish_run(conn, run_id=run_id, status="success", row_count=total_promoted, message=message)
            print(f"oos_provenance_summary={summary_csv} {message}")
        except BaseException as exc:
            try:
                conn.rollback()
            except Exception:
                LOGGER.exception("Rollback failed while recording failed OOS provenance run")
            if run_id is not None:
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
