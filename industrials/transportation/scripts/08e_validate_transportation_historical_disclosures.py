#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.disclosure_candidates import (  # noqa: E402
    ANNUAL_FORMS,
    EXTRACTION_METHOD,
    INTERIM_FORMS,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


FIELDS = [
    "ticker",
    "universe_role",
    "calibration_cohort",
    "eligible_filing_count",
    "scanned_filing_count",
    "missing_filing_count",
    "candidate_count",
    "accepted_candidate_count",
    "review_candidate_count",
    "first_scanned_filing_date",
    "last_scanned_filing_date",
    "missing_accessions",
    "status",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the resumable active-plus-inactive transportation "
            "historical specialized-disclosure backfill."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    specialized = family["specialized_disclosures"]
    universe = family["universe"]
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    start_date = str(
        args.start_date
        or specialized["historical_backfill_start_date"]
    )[:10]
    asof = str(args.asof)[:10]
    sync_json = resolve_path(
        specialized["historical_sync_output_json"], base_dir=base_dir
    )
    output_json = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else resolve_path(
            specialized["historical_validation_output_json"], base_dir=base_dir
        )
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else output_json.with_name(
            "transportation_historical_specialized_disclosure_coverage.csv"
        )
    )
    source_id = str(
        cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts")
    )
    submissions_source_id = str(
        cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions")
    )
    forms = sorted(ANNUAL_FORMS | INTERIM_FORMS)
    form_placeholders = ",".join("?" for _ in forms)
    errors: list[str] = []
    sync_summary = read_json(sync_json)
    if not sync_summary:
        errors.append(f"missing historical sync manifest: {sync_json}")
    else:
        if sync_summary.get("scan_mode") != "historical_backfill":
            errors.append("historical sync manifest has the wrong scan_mode")
        if str(sync_summary.get("extraction_method") or "") != EXTRACTION_METHOD:
            errors.append("historical sync manifest extraction method is stale")
        if str(sync_summary.get("start_date") or "") != start_date:
            errors.append("historical sync manifest start date mismatch")
        if str(sync_summary.get("asof_date") or "") != asof:
            errors.append("historical sync manifest as-of mismatch")

    with read_only_connection(db_path) as connection:
        members = [
            dict(row)
            for row in connection.execute(
                """
                SELECT t.ticker, t.calibration_cohort_id,
                       CASE WHEN EXISTS (
                         SELECT 1
                         FROM dim_universe_membership AS active
                         WHERE active.model_family=t.model_family
                           AND active.ticker=t.ticker
                           AND active.membership_source_id=?
                           AND active.membership_status='active'
                           AND active.start_date<=?
                           AND COALESCE(active.end_date, '9999-12-31')>=?
                       ) THEN 'active'
                       WHEN EXISTS (
                         SELECT 1
                         FROM dim_universe_membership AS historical
                         WHERE historical.model_family=t.model_family
                           AND historical.ticker=t.ticker
                           AND historical.membership_source_id=?
                       ) THEN 'delisted_usable'
                       ELSE 'delisted_excluded'
                       END AS universe_role
                FROM dim_industrials_taxonomy AS t
                WHERE t.model_family=?
                ORDER BY t.ticker
                """,
                (
                    str(universe["seed_source_id"]),
                    asof,
                    asof,
                    str(universe["historical_membership_source_id"]),
                    MODEL_FAMILY,
                ),
            ).fetchall()
        ]
        eligible_by_ticker: dict[str, set[str]] = {}
        for row in connection.execute(
            f"""
            SELECT ticker, accession_number
            FROM fact_sec_filing
            WHERE source_id=?
              AND filing_date>=? AND filing_date<=?
              AND UPPER(form_type) IN ({form_placeholders})
              AND (
                UPPER(form_type) NOT IN ('6-K', '6-K/A')
                OR EXISTS (
                  SELECT 1
                  FROM fact_sec_xbrl_fact_raw AS raw
                  WHERE raw.ticker=fact_sec_filing.ticker
                    AND raw.accession_number=fact_sec_filing.accession_number
                )
              )
              AND COALESCE(primary_document, '')<>''
            ORDER BY ticker, accession_number
            """,
            (submissions_source_id, start_date, asof, *forms),
        ).fetchall():
            eligible_by_ticker.setdefault(str(row["ticker"]), set()).add(
                str(row["accession_number"])
            )
        scan_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT ticker, accession_number, document_name, filing_date,
                       source_url, content_sha256, scan_status, candidate_count,
                       accepted_candidate_count, review_candidate_count
                FROM fact_sec_metric_disclosure_document_scan AS scan
                WHERE scan.model_family=? AND scan.source_id=?
                  AND scan.extraction_method=?
                  AND scan.filing_date>=? AND scan.filing_date<=?
                  AND (
                    UPPER(scan.form_type) NOT IN ('6-K', '6-K/A')
                    OR EXISTS (
                      SELECT 1
                      FROM fact_sec_xbrl_fact_raw AS raw
                      WHERE raw.ticker=scan.ticker
                        AND raw.accession_number=scan.accession_number
                    )
                  )
                ORDER BY ticker, accession_number
                """,
                (MODEL_FAMILY, source_id, EXTRACTION_METHOD, start_date, asof),
            ).fetchall()
        ]
        candidate_counts = {
            (str(row["ticker"]), str(row["accession_number"])): int(row["count"])
            for row in connection.execute(
                """
                SELECT ticker, accession_number, COUNT(*) AS count
                FROM fact_sec_metric_disclosure_candidate
                WHERE model_family=? AND source_id=? AND extraction_method=?
                  AND filing_date>=? AND filing_date<=?
                GROUP BY ticker, accession_number
                """,
                (MODEL_FAMILY, source_id, EXTRACTION_METHOD, start_date, asof),
            ).fetchall()
        }

    scanned_by_ticker: dict[str, set[str]] = {}
    scan_stats: dict[str, Counter[str]] = {}
    scan_dates: dict[str, list[str]] = {}
    for row in scan_rows:
        ticker = str(row["ticker"])
        accession = str(row["accession_number"])
        scanned_by_ticker.setdefault(ticker, set()).add(accession)
        stats = scan_stats.setdefault(ticker, Counter())
        stats["candidate"] += int(row["candidate_count"])
        stats["accepted"] += int(row["accepted_candidate_count"])
        stats["review"] += int(row["review_candidate_count"])
        filing_date = str(row["filing_date"] or "")
        if filing_date:
            scan_dates.setdefault(ticker, []).append(filing_date)
        if str(row["scan_status"]) != "PARSED":
            errors.append(f"{ticker}:{accession}: scan status is not PARSED")
        if len(str(row["content_sha256"] or "")) != 64:
            errors.append(f"{ticker}:{accession}: invalid content SHA-256")
        if not str(row["source_url"] or "").startswith(
            "https://www.sec.gov/Archives/"
        ):
            errors.append(f"{ticker}:{accession}: invalid SEC source URL")
        stored_count = int(row["candidate_count"])
        actual_count = candidate_counts.get((ticker, accession), 0)
        if stored_count != actual_count:
            errors.append(
                f"{ticker}:{accession}: checkpoint candidates={stored_count} "
                f"database candidates={actual_count}"
            )

    member_tickers = {str(row["ticker"]) for row in members}
    if len(members) != 160:
        errors.append(f"transportation taxonomy rows={len(members)} expected=160")
    orphan_scans = sorted(set(scanned_by_ticker) - member_tickers)
    if orphan_scans:
        errors.append(f"historical scans outside transportation universe={orphan_scans}")
    report: list[dict[str, Any]] = []
    for member in members:
        ticker = str(member["ticker"])
        eligible = eligible_by_ticker.get(ticker, set())
        scanned = scanned_by_ticker.get(ticker, set())
        missing = sorted(eligible - scanned)
        extra = sorted(scanned - eligible)
        if missing:
            errors.append(
                f"{ticker}: missing historical disclosure scans={missing[:10]}"
            )
        if extra:
            errors.append(f"{ticker}: scans outside eligible window={extra[:10]}")
        stats = scan_stats.get(ticker, Counter())
        dates = sorted(scan_dates.get(ticker, []))
        report.append(
            {
                "ticker": ticker,
                "universe_role": member["universe_role"],
                "calibration_cohort": member["calibration_cohort_id"],
                "eligible_filing_count": len(eligible),
                "scanned_filing_count": len(scanned),
                "missing_filing_count": len(missing),
                "candidate_count": stats["candidate"],
                "accepted_candidate_count": stats["accepted"],
                "review_candidate_count": stats["review"],
                "first_scanned_filing_date": dates[0] if dates else "",
                "last_scanned_filing_date": dates[-1] if dates else "",
                "missing_accessions": ";".join(missing),
                "status": "PASS" if not missing and not extra else "FAIL",
            }
        )

    eligible_total = sum(int(row["eligible_filing_count"]) for row in report)
    scanned_total = sum(int(row["scanned_filing_count"]) for row in report)
    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "model_family": MODEL_FAMILY,
        "extraction_method": EXTRACTION_METHOD,
        "start_date": start_date,
        "asof_date": asof,
        "universe_row_count": len(report),
        "active_ticker_count": sum(
            row["universe_role"] == "active" for row in report
        ),
        "inactive_ticker_count": sum(
            row["universe_role"] != "active" for row in report
        ),
        "eligible_filing_count": eligible_total,
        "scanned_filing_count": scanned_total,
        "missing_filing_count": eligible_total - scanned_total,
        "coverage_fraction": (
            round(scanned_total / eligible_total, 8) if eligible_total else 1.0
        ),
        "candidate_count": sum(int(row["candidate_count"]) for row in report),
        "accepted_candidate_count": sum(
            int(row["accepted_candidate_count"]) for row in report
        ),
        "review_candidate_count": sum(
            int(row["review_candidate_count"]) for row in report
        ),
        "output_csv": str(output_csv),
        "sync_manifest": str(sync_json),
        "errors": errors[:100],
    }
    write_csv_atomic(output_csv, FIELDS, report)
    write_manifest(output_json, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
