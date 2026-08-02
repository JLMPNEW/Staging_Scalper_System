#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.oos_research import artifact_sha256  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402
from industrials.transportation.share_source_evidence import (  # noqa: E402
    ANNUAL_AND_REGISTRATION_FORMS,
    extract_cover_share_counts,
    extract_listing_evidence,
    html_to_text,
    load_submissions_filings,
    locate_primary_document,
    sec_archive_url,
)


DEFAULT_ASOF = "2026-07-30"
EVIDENCE_FIELDS = [
    "ticker",
    "cik",
    "source_evaluation_date",
    "source_disposition",
    "filing_date",
    "form",
    "accession_number",
    "primary_document",
    "candidate_instrument",
    "candidate_ratio",
    "evidence_status",
    "evidence_text",
    "share_count_candidates_json",
    "source_url",
    "local_path",
]
SUMMARY_FIELDS = [
    "ticker",
    "cik",
    "source_evaluation_date",
    "source_disposition",
    "cached_filing_count",
    "exact_adr_filing_count",
    "exact_direct_filing_count",
    "candidate_ratios",
    "candidate_status",
    "candidate_instrument",
    "candidate_ratio",
    "source_url",
    "evidence_text",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract bounded, source-linked ADR/direct-share and cover-share evidence "
            "from the already-cached SEC archive for valuation-source blockers."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", default=DEFAULT_ASOF)
    parser.add_argument("--max-filings-per-ticker", type=int, default=12)
    return parser.parse_args()


def _evaluation_date(row: dict[str, str], *, asof: date) -> date:
    end_text = str(row.get("membership_end_date") or "").strip()[:10]
    if not end_text:
        return asof
    return min(asof, date.fromisoformat(end_text))


def _candidate_summary(
    ticker: str,
    rows: list[dict[str, Any]],
    *,
    cik: str,
    evaluation_date: date,
    source_disposition: str,
) -> dict[str, Any]:
    exact_adr = [row for row in rows if row["evidence_status"] == "EXACT_ADR_RATIO"]
    exact_direct = [
        row for row in rows if row["evidence_status"] == "EXACT_DIRECT_LISTED_CLASS"
    ]
    ratios = sorted({float(row["candidate_ratio"]) for row in exact_adr})
    candidate_status = "NO_EXACT_EVIDENCE"
    instrument = ""
    ratio: object = ""
    chosen: dict[str, Any] | None = None
    if len(ratios) == 1 and not exact_direct:
        candidate_status = "READY_ADR_CANDIDATE"
        instrument = "ADR_ADS"
        ratio = ratios[0]
        chosen = exact_adr[0]
    elif not ratios and exact_direct:
        candidate_status = "READY_DIRECT_CANDIDATE"
        instrument = "DIRECT_SHARE"
        ratio = 1.0
        chosen = exact_direct[0]
    elif ratios or exact_direct:
        candidate_status = "REGIME_OR_CONFLICT_REVIEW"
        chosen = (exact_adr or exact_direct)[0]
    return {
        "ticker": ticker,
        "cik": cik,
        "source_evaluation_date": evaluation_date.isoformat(),
        "source_disposition": source_disposition,
        "cached_filing_count": len(rows),
        "exact_adr_filing_count": len(exact_adr),
        "exact_direct_filing_count": len(exact_direct),
        "candidate_ratios": "|".join(f"{value:g}" for value in ratios),
        "candidate_status": candidate_status,
        "candidate_instrument": instrument,
        "candidate_ratio": ratio,
        "source_url": str(chosen.get("source_url") or "") if chosen else "",
        "evidence_text": str(chosen.get("evidence_text") or "") if chosen else "",
    }


def main() -> int:
    args = parse_args()
    if args.max_filings_per_ticker <= 0:
        raise ValueError("max-filings-per-ticker must be positive")
    asof = date.fromisoformat(str(args.asof)[:10])
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, "transportation")
    financial = family["financial"]
    audit_path = resolve_path(financial["valuation_source_audit_output_csv"], base_dir=base_dir)
    evidence_path = resolve_path(financial["share_source_evidence_output_csv"], base_dir=base_dir)
    summary_path = resolve_path(financial["share_source_evidence_summary_csv"], base_dir=base_dir)
    manifest_path = resolve_path(financial["share_source_evidence_output_json"], base_dir=base_dir)
    cache_root = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir)
    submissions_root = cache_root / "sec_submissions"
    archive_root = cache_root / "sec_archive_xbrl"
    blocked = [
        row
        for row in read_rows(audit_path)
        if row.get("required_for_rebuild") == "1" and row.get("readiness_status") != "READY"
    ]
    evidence_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for audit_row in blocked:
        ticker = str(audit_row["ticker"]).strip().upper()
        cik = f"{int(str(audit_row['cik'])):010d}"
        evaluation_date = _evaluation_date(audit_row, asof=asof)
        submissions_path = submissions_root / f"CIK{cik}.json"
        issuer_archive = archive_root / f"CIK{cik}"
        ticker_rows: list[dict[str, Any]] = []
        if not submissions_path.is_file():
            errors.append(f"{ticker}:missing_submissions_cache")
        else:
            filings = [
                filing
                for filing in load_submissions_filings(
                    submissions_path,
                    asof=evaluation_date,
                )
                if filing.form in ANNUAL_AND_REGISTRATION_FORMS
            ][: args.max_filings_per_ticker]
            for filing in filings:
                local_path = locate_primary_document(issuer_archive, filing=filing)
                if local_path is None:
                    continue
                try:
                    text = html_to_text(
                        local_path.read_text(encoding="utf-8", errors="ignore")
                    )
                except OSError as exc:
                    errors.append(f"{ticker}:{filing.accession_number}:{exc}")
                    continue
                listing = extract_listing_evidence(text, ticker=ticker)
                counts = extract_cover_share_counts(text)
                row = {
                    "ticker": ticker,
                    "cik": cik,
                    "source_evaluation_date": evaluation_date.isoformat(),
                    "source_disposition": audit_row["disposition"],
                    "filing_date": filing.filing_date.isoformat(),
                    "form": filing.form,
                    "accession_number": filing.accession_number,
                    "primary_document": filing.primary_document,
                    **listing,
                    "share_count_candidates_json": json.dumps(
                        counts,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "source_url": sec_archive_url(cik=cik, filing=filing),
                    "local_path": str(local_path),
                }
                ticker_rows.append(row)
                evidence_rows.append(row)
        summary_rows.append(
            _candidate_summary(
                ticker,
                ticker_rows,
                cik=cik,
                evaluation_date=evaluation_date,
                source_disposition=str(audit_row["disposition"]),
            )
        )
    evidence_rows.sort(
        key=lambda row: (
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
        ),
        reverse=False,
    )
    summary_rows.sort(key=lambda row: str(row["ticker"]))
    write_csv_atomic(evidence_path, EVIDENCE_FIELDS, evidence_rows)
    write_csv_atomic(summary_path, SUMMARY_FIELDS, summary_rows)
    status_counts: dict[str, int] = {}
    for row in summary_rows:
        status = str(row["candidate_status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    result = {
        "artifact_family": "transportation_share_source_evidence",
        "model_family": "transportation",
        "asof_date": asof.isoformat(),
        "acceptance": "PASS" if not errors else "FAIL",
        "errors": errors,
        "blocker_count": len(blocked),
        "evidence_row_count": len(evidence_rows),
        "candidate_status_counts": dict(sorted(status_counts.items())),
        "audit_path": str(audit_path),
        "audit_sha256": artifact_sha256(audit_path),
        "evidence_path": str(evidence_path),
        "evidence_sha256": artifact_sha256(evidence_path),
        "summary_path": str(summary_path),
        "summary_sha256": artifact_sha256(summary_path),
        "max_filings_per_ticker": args.max_filings_per_ticker,
    }
    write_text_atomic(manifest_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
