from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from dedicated_parser.contracts import file_sha256
from industrials.core.reports import write_csv_atomic, write_text_atomic


SOURCE_REGISTRY_LOAD_VERSION = "transportation_dp6e_registry_load_v1"
REGISTRY_LOAD_FIELDS = (
    "load_version",
    "ticker",
    "cik",
    "source_id",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "report_date",
    "primary_document",
    "filing_url",
    "source_detail",
    "preexisting",
    "managed_by_loader",
    "load_action",
)


def _filing_url(*, cik: str, accession: str, document: str) -> str:
    base = (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{accession.replace('-', '')}/"
    )
    return f"{base}{document}" if document else base


def plan_source_registry_load(
    connection: sqlite3.Connection,
    *,
    filing_rows: Sequence[Mapping[str, str]],
    source_id: str,
) -> tuple[list[dict[str, object]], list[str]]:
    tickers = sorted(
        {
            str(row.get("ticker") or "").strip().upper()
            for row in filing_rows
            if str(row.get("ticker") or "").strip()
        }
    )
    placeholders = ",".join("?" for _ in tickers)
    existing_rows = (
        connection.execute(
            f"""
            SELECT ticker, cik, accession_number, form_type, source_detail
            FROM fact_sec_filing
            WHERE source_id=? AND ticker IN ({placeholders})
            """,
            (source_id, *tickers),
        ).fetchall()
        if tickers
        else []
    )
    existing = {
        (
            str(row["ticker"]).upper(),
            str(row["accession_number"]),
        ): {
            "cik": str(row["cik"] or ""),
            "form_type": str(row["form_type"] or "").upper(),
            "source_detail": str(row["source_detail"] or ""),
        }
        for row in existing_rows
    }
    planned: list[dict[str, object]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for source in filing_rows:
        ticker = str(source.get("ticker") or "").strip().upper()
        accession = str(
            source.get("accession_number") or ""
        ).strip()
        raw_cik = str(source.get("cik") or "").strip()
        # Validate before zfill: a blank CIK must fail the completeness
        # check instead of becoming a well-formed 0000000000.
        cik = raw_cik.zfill(10) if raw_cik else ""
        form_type = str(source.get("form_type") or "").strip().upper()
        filing_date = str(source.get("filing_date") or "").strip()
        if not all((ticker, accession, cik, form_type, filing_date)):
            errors.append(
                f"incomplete filing metadata: {ticker}|{accession}"
            )
            continue
        key = (ticker, accession)
        if key in seen:
            errors.append(f"duplicate filing key: {ticker}|{accession}")
            continue
        seen.add(key)
        current = existing.get(key)
        managed_by_loader = bool(
            current
            and current["source_detail"].startswith(
                "sec_submissions_source_exhaustion:"
            )
        )
        if current and (
            current["cik"].zfill(10) != cik
            or current["form_type"] != form_type
        ):
            errors.append(
                "existing filing identity mismatch: "
                f"{ticker}|{accession}|{current['cik']}|"
                f"{current['form_type']} vs {cik}|{form_type}"
            )
        primary_document = str(
            source.get("primary_document") or ""
        ).strip()
        source_file = str(
            source.get("submissions_source_file") or ""
        ).strip()
        planned.append(
            {
                "load_version": SOURCE_REGISTRY_LOAD_VERSION,
                "ticker": ticker,
                "cik": cik,
                "source_id": source_id,
                "accession_number": accession,
                "form_type": form_type,
                "filing_date": filing_date,
                "accepted_at": str(
                    source.get("accepted_at") or ""
                ).strip(),
                "report_date": str(
                    source.get("report_date") or ""
                ).strip(),
                "primary_document": primary_document,
                "filing_url": _filing_url(
                    cik=cik,
                    accession=accession,
                    document=primary_document,
                ),
                "source_detail": (
                    "sec_submissions_source_exhaustion:"
                    f"{source_file or 'unknown'}"
                ),
                "preexisting": int(current is not None),
                "managed_by_loader": int(managed_by_loader),
                "load_action": (
                    (
                        "KEEP_LOADER_INSERT"
                        if managed_by_loader
                        else "KEEP_EXISTING"
                    )
                    if current is not None
                    else "INSERT_MISSING"
                ),
            }
        )
    planned.sort(
        key=lambda row: (
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
        )
    )
    return planned, errors


def apply_source_registry_load(
    connection: sqlite3.Connection,
    *,
    planned_rows: Iterable[Mapping[str, object]],
) -> int:
    rows = [
        row
        for row in planned_rows
        if str(row.get("load_action") or "") == "INSERT_MISSING"
    ]
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    before = connection.total_changes
    connection.executemany(
        """
        INSERT INTO fact_sec_filing(
            ticker, cik, source_id, accession_number, form_type,
            filing_date, accepted_at, report_date, fiscal_year,
            fiscal_period, primary_document, filing_url, source_detail,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, accession_number, source_id) DO NOTHING
        """,
        [
            (
                str(row["ticker"]),
                str(row["cik"]),
                str(row["source_id"]),
                str(row["accession_number"]),
                str(row["form_type"]),
                str(row["filing_date"]),
                str(row["accepted_at"]),
                str(row["report_date"]),
                str(row["primary_document"]),
                str(row["filing_url"]),
                str(row["source_detail"]),
                now,
                now,
            )
            for row in rows
        ],
    )
    return connection.total_changes - before


def write_source_registry_load(
    *,
    rows: Sequence[Mapping[str, object]],
    errors: Sequence[str],
    execute: bool,
    inserted_count: int,
    source_manifest_path: Path,
    output_dir: Path,
) -> dict[str, object]:
    artifact_stem = (
        "transportation_source_registry_load"
        if execute
        else "transportation_source_registry_load_dry_run"
    )
    result_path = output_dir / f"{artifact_stem}.csv"
    manifest_path = output_dir / f"{artifact_stem}_manifest.json"
    write_csv_atomic(result_path, REGISTRY_LOAD_FIELDS, rows)
    planned_insert_count = sum(
        str(row.get("load_action") or "") == "INSERT_MISSING"
        for row in rows
    )
    payload = {
        "acceptance": (
            "FAIL"
            if errors
            else ("PASS" if execute else "DRY_RUN")
        ),
        "gate": "DP6E_APPEND_ONLY_FILING_REGISTRY_LOAD",
        "load_version": SOURCE_REGISTRY_LOAD_VERSION,
        "execute": execute,
        "source_manifest_path": str(source_manifest_path.resolve()),
        "source_manifest_sha256": file_sha256(source_manifest_path),
        "filing_count": len(rows),
        "preexisting_count": sum(
            str(row.get("preexisting") or "0") == "1"
            for row in rows
        ),
        "managed_existing_count": sum(
            str(row.get("managed_by_loader") or "0") == "1"
            for row in rows
        ),
        "planned_insert_count": planned_insert_count,
        "inserted_count": inserted_count,
        "cumulative_loader_insert_count": (
            inserted_count
            + sum(
                str(row.get("managed_by_loader") or "0") == "1"
                for row in rows
            )
        ),
        "updated_count": 0,
        "deleted_count": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "errors": list(errors),
        "result_artifact": {
            "path": str(result_path.resolve()),
            "row_count": len(rows),
            "sha256": file_sha256(result_path),
        },
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return payload
