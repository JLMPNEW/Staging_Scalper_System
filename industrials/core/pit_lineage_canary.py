from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from industrials.core.financial_filing_lineage import (
    build_financial_filing_lineage,
)
from industrials.core.reports import write_csv_atomic, write_text_atomic
from industrials.core.text_norm import normalize_ticker
from orchestration_contracts.financial_lineage import (
    LINEAGE_FIELDS,
    evaluate_financial_lineage_rows,
    evaluation_manifest,
    policy_for_model_family,
)


CANARY_FIELDS = (
    "asof_date",
    "ticker",
    "model_family",
    "membership_row_count",
    "membership_statuses",
    "membership_start_date",
    "membership_end_date",
    "feature_snapshot_present",
    "latest_material_availability_date",
    "incorporated_availability_date",
    *LINEAGE_FIELDS,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def open_readonly_database(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    uri = f"{resolved.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def representative_dates(*, start_year: int, asof: str) -> list[str]:
    terminal = date.fromisoformat(asof)
    if start_year > terminal.year:
        raise ValueError("start_year cannot be after the as-of year")
    dates = [f"{year}-12-31" for year in range(start_year, terminal.year)]
    completed_quarters = (
        date(terminal.year, 3, 31),
        date(terminal.year, 6, 30),
        date(terminal.year, 9, 30),
        date(terminal.year, 12, 31),
    )
    dates.extend(item.isoformat() for item in completed_quarters if item <= terminal)
    dates.append(terminal.isoformat())
    return sorted(set(dates))


def _membership_rows(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ticker, company_id, membership_status, start_date,
               COALESCE(end_date, '') AS end_date, membership_source_id
        FROM dim_universe_membership
        WHERE model_family = ?
          AND point_in_time_flag = 1
          AND start_date <= ?
          AND (end_date IS NULL OR end_date = '' OR end_date >= ?)
        ORDER BY ticker, start_date, membership_source_id
        """,
        (model_family, asof, asof),
    ).fetchall()
    return [dict(row) for row in rows]


def _membership_snapshot(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof: str,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    issues: list[str] = []
    for row in _membership_rows(conn, model_family=model_family, asof=asof):
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker:
            issues.append(f"{model_family}:{asof}:blank_membership_ticker")
            continue
        grouped[ticker].append(row)

    snapshot: dict[str, dict[str, str]] = {}
    for ticker, rows in sorted(grouped.items()):
        company_ids = {str(row.get("company_id") or "") for row in rows}
        statuses = {str(row.get("membership_status") or "") for row in rows}
        if len(company_ids) != 1:
            issues.append(f"{model_family}:{asof}:{ticker}:conflicting_membership_company_ids")
        if len(statuses) != 1:
            issues.append(f"{model_family}:{asof}:{ticker}:conflicting_membership_statuses")
        end_dates = [str(row.get("end_date") or "") for row in rows]
        snapshot[ticker] = {
            "membership_row_count": str(len(rows)),
            "membership_statuses": ";".join(sorted(statuses)),
            "membership_start_date": min(str(row["start_date"]) for row in rows),
            "membership_end_date": max(end_dates) if all(end_dates) else "",
        }
    if not snapshot:
        issues.append(f"{model_family}:{asof}:empty_point_in_time_membership")
    return snapshot, issues


def _feature_tickers(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof: str,
) -> set[str]:
    return {
        normalize_ticker(row[0])
        for row in conn.execute(
            """
            SELECT ticker
            FROM feature_financial_statement
            WHERE model_family = ? AND asof_date = ?
            """,
            (model_family, asof),
        )
        if normalize_ticker(row[0])
    }


def _availability_by_accession(
    conn: sqlite3.Connection,
    accessions: Iterable[str],
) -> dict[str, str]:
    normalized = sorted({str(value).strip() for value in accessions if str(value).strip()})
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        f"""
        SELECT accession_number,
               SUBSTR(COALESCE(NULLIF(accepted_at, ''), filing_date), 1, 10)
                   AS availability_date
        FROM fact_sec_filing
        WHERE accession_number IN ({placeholders})
        """,
        normalized,
    ).fetchall()
    return {str(row["accession_number"]): str(row["availability_date"] or "") for row in rows}


def build_canary_snapshot(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    date.fromisoformat(asof)
    membership, membership_issues = _membership_snapshot(conn, model_family=model_family, asof=asof)
    tickers = sorted(membership)
    lineage = build_financial_filing_lineage(
        conn,
        model_family=model_family,
        asof=asof,
        tickers=tickers,
    )
    feature_tickers = _feature_tickers(conn, model_family=model_family, asof=asof)
    accessions = {
        str(row.get(field) or "")
        for row in lineage.values()
        for field in (
            "latest_material_financial_accession",
            "incorporated_financial_accession",
        )
    }
    availability = _availability_by_accession(conn, accessions)

    rows: list[dict[str, str]] = []
    future_issues: list[str] = []
    for ticker in tickers:
        lineage_row = lineage[ticker]
        latest_accession = str(lineage_row.get("latest_material_financial_accession") or "")
        incorporated_accession = str(lineage_row.get("incorporated_financial_accession") or "")
        latest_availability = availability.get(latest_accession, "")
        incorporated_availability = availability.get(incorporated_accession, "")
        for label, value in (
            ("latest_material_availability_date", latest_availability),
            ("incorporated_availability_date", incorporated_availability),
        ):
            if value and value > asof:
                future_issues.append(f"{model_family}:{asof}:{ticker}:future_{label}={value}")
        rows.append(
            {
                "asof_date": asof,
                "ticker": ticker,
                "model_family": model_family,
                **membership[ticker],
                "feature_snapshot_present": "1" if ticker in feature_tickers else "0",
                "latest_material_availability_date": latest_availability,
                "incorporated_availability_date": incorporated_availability,
                **{field: str(lineage_row.get(field) or "") for field in LINEAGE_FIELDS},
            }
        )

    policy = policy_for_model_family(model_family)
    evaluation = evaluate_financial_lineage_rows(
        rows,
        policy_mode=policy.mode_for("historical"),
        expected_asof=asof,
        min_core_metric_count=policy.min_core_metric_count,
    )
    issues = [*membership_issues, *future_issues, *evaluation.errors]
    manifest = {
        "asof_date": asof,
        "model_family": model_family,
        "membership_ticker_count": len(tickers),
        "membership_issue_count": len(membership_issues),
        "feature_snapshot_count": sum(row["feature_snapshot_present"] == "1" for row in rows),
        "future_availability_issue_count": len(future_issues),
        **evaluation_manifest(evaluation, policy=policy, context="historical"),
        "acceptance": "PASS" if not issues else "FAIL",
        "issues": issues[:100],
    }
    return rows, manifest


def run_pit_lineage_canary(
    *,
    db_path: Path,
    output_dir: Path,
    model_families: Sequence[str],
    dates: Sequence[str],
) -> dict[str, Any]:
    normalized_families = tuple(dict.fromkeys(str(value).strip() for value in model_families if str(value).strip()))
    normalized_dates = tuple(sorted({date.fromisoformat(str(value).strip()).isoformat() for value in dates}))
    if not normalized_families:
        raise ValueError("At least one model family is required")
    if not normalized_dates:
        raise ValueError("At least one PIT date is required")

    all_rows: list[dict[str, str]] = []
    snapshots: list[dict[str, Any]] = []
    with open_readonly_database(db_path) as conn:
        for model_family in normalized_families:
            for asof in normalized_dates:
                rows, manifest = build_canary_snapshot(conn, model_family=model_family, asof=asof)
                all_rows.extend(rows)
                snapshots.append(manifest)

    output_dir.mkdir(parents=True, exist_ok=True)
    rows_path = output_dir / "industrials_pit_financial_lineage_canary.csv"
    manifest_path = output_dir / "industrials_pit_financial_lineage_canary.json"
    write_csv_atomic(rows_path, CANARY_FIELDS, all_rows)
    rows_sha256 = hashlib.sha256(rows_path.read_bytes()).hexdigest()
    acceptance = "PASS" if snapshots and all(row["acceptance"] == "PASS" for row in snapshots) else "FAIL"
    manifest = {
        "schema_version": "industrials_pit_financial_lineage_canary_v1",
        "generated_at_utc": utc_now(),
        "database_path": str(db_path.expanduser().resolve()),
        "database_access": "sqlite_read_only_query_only",
        "output_path": str(rows_path.resolve()),
        "output_sha256": rows_sha256,
        "model_families": list(normalized_families),
        "dates": list(normalized_dates),
        "snapshot_count": len(snapshots),
        "row_count": len(all_rows),
        "acceptance": acceptance,
        "snapshots": snapshots,
    }
    write_text_atomic(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return manifest
