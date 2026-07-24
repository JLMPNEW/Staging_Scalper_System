#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db, utc_now  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.machinery.disclosure_candidates import (  # noqa: E402
    DisclosureCandidate,
    extract_machinery_prose_candidates,
    reconcile_machinery_disclosure_facts,
    replace_document_candidates_and_facts,
    resolve_machinery_disclosure_candidates,
)
from industrials.machinery.disclosure_documents import (  # noqa: E402
    DocumentText,
    extract_document_text,
)
from industrials.machinery.issuer_ir import (  # noqa: E402
    ISSUER_IR_SOURCE_DETAIL,
    ISSUER_IR_SOURCE_ID,
    IssuerIRDocument,
    apply_issuer_ir_policy,
    document_known_by_asof,
    ensure_issuer_ir_schema,
    issuer_ir_filing,
    load_issuer_ir_manifest,
    upsert_issuer_ir_document,
    validate_final_url,
)
from industrials.machinery.scoring import parse_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OUTPUT_FIELDS = (
    "ticker",
    "document_type",
    "published_at",
    "period_end",
    "source_url",
    "final_url",
    "content_sha256",
    "extraction_method",
    "candidate_count",
    "promoted_count",
    "retrieval_status",
    "status_reason",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load reviewed issuer-IR machinery disclosures with PIT provenance."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


def _cache_path(cache_dir: Path, document: IssuerIRDocument) -> Path:
    suffix = Path(document.document_name).suffix.lower()
    if suffix not in {".htm", ".html", ".xhtml", ".txt", ".pdf"}:
        suffix = ".bin"
    return cache_dir / document.ticker / f"{document.document_key}{suffix}"


def _fetch_document(
    document: IssuerIRDocument,
    *,
    cache_path: Path,
    offline: bool,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_document_bytes: int,
) -> tuple[bytes, str, str, bool]:
    if cache_path.exists() and not force:
        return cache_path.read_bytes(), document.url, "", False
    if offline:
        raise FileNotFoundError(f"Issuer IR cache miss in offline mode: {cache_path}")
    try:
        import requests  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Package 'requests' is required for issuer IR sync.") from exc

    response = requests.get(
        document.url,
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        timeout=timeout_sec,
        allow_redirects=True,
    )
    response.raise_for_status()
    validate_final_url(document, str(response.url))
    payload = bytes(response.content)
    if len(payload) > max_document_bytes:
        raise ValueError(
            f"Issuer IR document exceeds max_document_bytes={max_document_bytes}: "
            f"{document.url}"
        )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, cache_path)
    return payload, str(response.url), str(response.headers.get("Content-Type") or ""), True


def _source_is_active(conn: sqlite3.Connection, source_id: str) -> bool:
    row = conn.execute(
        "SELECT status FROM source_registry WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    return row is not None and str(row["status"] or "").lower() == "active"


def _company_rows(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    return {
        str(row["ticker"]): {
            "cik": str(row["cik"] or ""),
            "currency": str(row["currency"] or "USD").upper(),
        }
        for row in conn.execute(
            """
            SELECT DISTINCT c.ticker, c.cik, c.currency
            FROM dim_company c
            JOIN dim_universe_membership m ON m.company_id = c.company_id
            WHERE m.model_family = 'machinery'
            """
        ).fetchall()
    }


def _known_date_sql(alias: str) -> str:
    return f"""
        CASE
            WHEN COALESCE({alias}.accepted_at, '') GLOB '????-??-??*'
                THEN SUBSTR({alias}.accepted_at, 1, 10)
            WHEN COALESCE({alias}.accepted_at, '') GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'
                THEN SUBSTR({alias}.accepted_at, 1, 4) || '-'
                   || SUBSTR({alias}.accepted_at, 5, 2) || '-'
                   || SUBSTR({alias}.accepted_at, 7, 2)
            ELSE COALESCE(NULLIF({alias}.filing_date, ''), '9999-12-31')
        END
    """


def _apply_existing_sec_precedence(
    conn: sqlite3.Connection,
    candidates: Iterable[DisclosureCandidate],
    *,
    ticker: str,
    known_date: str,
    sec_source_id: str,
) -> list[DisclosureCandidate]:
    output: list[DisclosureCandidate] = []
    for candidate in candidates:
        if candidate.candidate_status != "ACCEPTED":
            output.append(candidate)
            continue
        existing = conn.execute(
            f"""
            SELECT value
            FROM fact_sec_xbrl_fact f
            WHERE ticker = ? AND source_id = ? AND canonical_metric = ?
              AND COALESCE(period_start, '') = COALESCE(?, '')
              AND period_end = ?
              AND ({_known_date_sql('f')}) <= ?
            ORDER BY source_priority ASC, accepted_at DESC, fact_id DESC
            LIMIT 1
            """,
            (
                ticker,
                sec_source_id,
                candidate.metric_name,
                candidate.period_start,
                candidate.period_end,
                known_date,
            ),
        ).fetchone()
        if existing is None:
            output.append(candidate)
            continue
        existing_value = float(existing["value"])
        tolerance = max(1.0, abs(candidate.value) * 1e-9)
        if abs(existing_value - candidate.value) <= tolerance:
            output.append(
                replace(
                    candidate,
                    candidate_status="SUPPRESSED_SEC_PRIMARY_DUPLICATE",
                    status_reason="equivalent_sec_fact_known_before_issuer_ir_publication",
                )
            )
        else:
            output.append(
                replace(
                    candidate,
                    candidate_status="REVIEW_REQUIRED",
                    status_reason="issuer_ir_conflicts_with_prior_sec_fact",
                    confidence=min(candidate.confidence, 0.55),
                )
            )
    return output


def _start_ingestion_run(conn: sqlite3.Connection) -> int:
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO ingestion_runs(source_id, started_at, status, created_at)
        VALUES (?, ?, 'running', ?)
        """,
        (ISSUER_IR_SOURCE_ID, now, now),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Issuer IR ingestion run did not return a run ID")
    return int(cursor.lastrowid)


def _finish_ingestion_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    request_count: int,
    row_count: int,
    message: str,
) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
        SET completed_at = ?, status = ?, request_count = ?, row_count = ?, message = ?
        WHERE ingestion_run_id = ?
        """,
        (utc_now(), status, request_count, row_count, message, run_id),
    )


def _record_raw_response(
    conn: sqlite3.Connection,
    *,
    document: IssuerIRDocument,
    final_url: str,
    content_sha256: str,
    extracted: DocumentText,
    run_id: int,
) -> None:
    now = utc_now()
    payload = json.dumps(
        {
            "content_sha256": content_sha256,
            "document_key": document.document_key,
            "document_type": document.document_type,
            "extracted_text": extracted.text,
            "extraction_method": extracted.extraction_method,
            "published_at": document.published_at,
        },
        sort_keys=True,
    )
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc,
            response_status, response_hash, asof_date, payload_text,
            ingestion_run_id, created_at
        ) VALUES (?, ?, '{}', ?, 200, ?, ?, ?, ?, ?)
        """,
        (
            ISSUER_IR_SOURCE_ID,
            final_url,
            now,
            content_sha256,
            document.published_at[:10],
            payload,
            run_id,
            now,
        ),
    )


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    manifest_path = (
        args.manifest.expanduser().resolve()
        if args.manifest
        else resolve_path(
            cfg_get(
                config,
                "issuer_ir.manifest_csv",
                "system_csvs/machinery_issuer_ir_documents.csv",
            ),
            base_dir=base_dir,
        )
    )
    output_path = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "issuer_ir.output_csv",
                "../../output/industrials/machinery/stage4/issuer_ir_sync.csv",
            ),
            base_dir=base_dir,
        )
    )
    cache_dir = resolve_path(
        cfg_get(
            config,
            "issuer_ir.cache_dir",
            "../../output/industrials_cache/machinery/issuer_ir",
        ),
        base_dir=base_dir,
    )
    timeout_sec = float(cfg_get(config, "issuer_ir.timeout_sec", 30.0) or 30.0)
    max_document_bytes = int(
        cfg_get(config, "issuer_ir.max_document_bytes", 50_000_000) or 50_000_000
    )
    enable_pdf_ocr = bool(cfg_get(config, "issuer_ir.pdf_ocr_enabled", True))
    max_pdf_pages = int(cfg_get(config, "issuer_ir.max_pdf_pages", 250) or 250)
    pdf_extraction_timeout_sec = float(
        cfg_get(config, "issuer_ir.pdf_extraction_timeout_sec", 30.0) or 30.0
    )
    user_agent = str(
        cfg_get(config, "issuer_ir.user_agent", "Machinery research pipeline")
    ).strip()
    sec_source_id = str(
        cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts")
    )
    documents = [
        document
        for document in load_issuer_ir_manifest(manifest_path)
        if document_known_by_asof(document, asof=asof)
    ]

    report_rows: list[dict[str, Any]] = []
    failures: list[str] = []
    request_count = 0
    promoted_total = 0
    with connect(db_path) as conn:
        init_db(conn)
        ensure_issuer_ir_schema(conn)
        if not _source_is_active(conn, ISSUER_IR_SOURCE_ID):
            raise ValueError(f"Source {ISSUER_IR_SOURCE_ID} must be active in source_registry")
        companies = _company_rows(conn)
        unknown_tickers = sorted({document.ticker for document in documents} - set(companies))
        if unknown_tickers:
            raise ValueError(f"Issuer IR manifest contains non-machinery tickers={unknown_tickers}")
        with conn:
            run_id = _start_ingestion_run(conn)
        for document in documents:
            cache_path = _cache_path(cache_dir, document)
            final_url = document.url
            content_sha256 = ""
            extracted = DocumentText("", "not_attempted")
            candidate_count = 0
            promoted_count = 0
            retrieval_status = "FAILED"
            status_reason = ""
            try:
                payload, final_url, content_type, network_fetch = _fetch_document(
                    document,
                    cache_path=cache_path,
                    offline=bool(args.offline),
                    force=bool(args.force),
                    user_agent=user_agent,
                    timeout_sec=timeout_sec,
                    max_document_bytes=max_document_bytes,
                )
                request_count += int(network_fetch)
                content_sha256 = hashlib.sha256(payload).hexdigest()
                if document.expected_sha256 and content_sha256 != document.expected_sha256:
                    raise ValueError(
                        f"Content hash mismatch expected={document.expected_sha256} "
                        f"actual={content_sha256}"
                    )
                extracted = extract_document_text(
                    payload,
                    document_name=document.document_name,
                    content_type=content_type,
                    enable_pdf_ocr=enable_pdf_ocr,
                    max_pdf_pages=max_pdf_pages,
                    max_pdf_bytes=max_document_bytes,
                    pdf_extraction_timeout_sec=pdf_extraction_timeout_sec,
                )
                if not extracted.text.strip():
                    raise ValueError(
                        f"Document produced no text: method={extracted.extraction_method} "
                        f"warning={extracted.warning}"
                    )
                filing = issuer_ir_filing(document)
                candidates = extract_machinery_prose_candidates(
                    extracted.text,
                    filing=filing,
                    company_currency=companies[document.ticker]["currency"],
                )
                candidates = resolve_machinery_disclosure_candidates(
                    candidates,
                    ticker=document.ticker,
                    filing=filing,
                )
                candidates = apply_issuer_ir_policy(candidates, document=document)
                candidates = _apply_existing_sec_precedence(
                    conn,
                    candidates,
                    ticker=document.ticker,
                    known_date=document.published_at[:10],
                    sec_source_id=sec_source_id,
                )
                candidate_count = len(candidates)
                with conn:
                    _, raw_count, mapped_count = replace_document_candidates_and_facts(
                        conn,
                        ticker=document.ticker,
                        cik=companies[document.ticker]["cik"],
                        source_id=ISSUER_IR_SOURCE_ID,
                        model_family="machinery",
                        filing=filing,
                        document_name=document.document_name,
                        candidates=candidates,
                        now=utc_now(),
                        source_detail=ISSUER_IR_SOURCE_DETAIL,
                        taxonomy="issuer-ir",
                        source_priority_floor=240,
                    )
                    reconciliation = reconcile_machinery_disclosure_facts(
                        conn,
                        ticker=document.ticker,
                        source_id=ISSUER_IR_SOURCE_ID,
                        model_family="machinery",
                        now=utc_now(),
                        prose_source_detail=ISSUER_IR_SOURCE_DETAIL,
                    )
                    promoted_count = max(
                        0,
                        min(raw_count, mapped_count)
                        - reconciliation["mapped_facts_deleted"],
                    )
                    _record_raw_response(
                        conn,
                        document=document,
                        final_url=final_url,
                        content_sha256=content_sha256,
                        extracted=extracted,
                        run_id=run_id,
                    )
                    upsert_issuer_ir_document(
                        conn,
                        document=document,
                        final_url=final_url,
                        content_type=content_type,
                        content_sha256=content_sha256,
                        cache_path=cache_path,
                        extraction_method=extracted.extraction_method,
                        page_count=extracted.page_count,
                        ocr_used=extracted.ocr_used,
                        candidate_count=candidate_count,
                        promoted_count=promoted_count,
                        retrieval_status="PASS",
                        status_reason=extracted.warning,
                    )
                retrieval_status = "PASS"
                status_reason = extracted.warning
                promoted_total += promoted_count
            except Exception as exc:
                status_reason = f"{type(exc).__name__}:{exc}"
                failures.append(f"{document.ticker}:{document.url}:{status_reason}")
                with conn:
                    upsert_issuer_ir_document(
                        conn,
                        document=document,
                        final_url=final_url,
                        content_type="",
                        content_sha256=content_sha256,
                        cache_path=cache_path,
                        extraction_method=extracted.extraction_method,
                        page_count=extracted.page_count,
                        ocr_used=extracted.ocr_used,
                        candidate_count=0,
                        promoted_count=0,
                        retrieval_status="FAILED",
                        status_reason=status_reason,
                    )
                if not args.allow_partial:
                    with conn:
                        _finish_ingestion_run(
                            conn,
                            run_id=run_id,
                            status="failed",
                            request_count=request_count,
                            row_count=promoted_total,
                            message=status_reason,
                        )
                    raise
            report_rows.append(
                {
                    "ticker": document.ticker,
                    "document_type": document.document_type,
                    "published_at": document.published_at,
                    "period_end": document.period_end,
                    "source_url": document.url,
                    "final_url": final_url,
                    "content_sha256": content_sha256,
                    "extraction_method": extracted.extraction_method,
                    "candidate_count": candidate_count,
                    "promoted_count": promoted_count,
                    "retrieval_status": retrieval_status,
                    "status_reason": status_reason,
                }
            )
        with conn:
            _finish_ingestion_run(
                conn,
                run_id=run_id,
                status="partial" if failures else "complete",
                request_count=request_count,
                row_count=promoted_total,
                message=(";".join(failures[:10]) if failures else f"documents={len(documents)}"),
            )
    write_csv_atomic(output_path, OUTPUT_FIELDS, report_rows)
    print(
        f"PASS: issuer IR documents={len(report_rows)} promoted={promoted_total} "
        f"failures={len(failures)} output={output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
