#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.financial_filing_lineage import (  # noqa: E402
    PERIODIC_FINANCIAL_FORMS,
    build_financial_filing_lineage,
)
from industrials.core.reports import write_csv_atomic  # noqa: E402
from orchestration_contracts.financial_lineage import policy_for_model_family  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SCOPE_FIELDS = (
    "ticker",
    "accession_number",
    "form_type",
    "filing_date",
    "report_date",
    "scope_reason",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run bounded exact-accession recovery for unresolved industrial "
            "financial-filing lineage."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--max-accessions-per-ticker", type=int, default=4)
    parser.add_argument("--scope-output-csv", type=Path, default=None)
    parser.add_argument("--manifest-output-json", type=Path, default=None)
    parser.add_argument("--sec-output-csv", type=Path, default=None)
    return parser.parse_args()


def active_family_tickers(conn: sqlite3.Connection, *, model_family: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT c.ticker
            FROM dim_company c
            JOIN dim_industrials_taxonomy t ON t.company_id = c.company_id
            WHERE c.is_active = 1 AND t.model_family = ?
            ORDER BY c.ticker
            """,
            (model_family,),
        ).fetchall()
    ]


def _available_asof(row: Mapping[str, Any], asof: str) -> bool:
    available = str(row.get("accepted_at") or row.get("filing_date") or "").strip()[:10]
    return bool(available and available <= asof)


def _filing_priority(
    filing: Mapping[str, Any],
    *,
    latest: Mapping[str, Any],
) -> tuple[int, str, str]:
    report_date = str(filing.get("report_date") or "")[:10]
    filing_date = str(filing.get("filing_date") or "")[:10]
    latest_report = str(latest.get("report_date") or "")[:10]
    latest_filing = str(latest.get("filing_date") or "")[:10]
    if report_date and report_date == latest_report:
        priority = 0
    elif filing_date and filing_date == latest_filing:
        priority = 1
    else:
        priority = 2
    return priority, filing_date, str(filing.get("accession_number") or "")


def _companion_reason(
    filing: Mapping[str, Any],
    *,
    latest: Mapping[str, Any],
) -> str:
    report_date = str(filing.get("report_date") or "")[:10]
    filing_date = str(filing.get("filing_date") or "")[:10]
    if report_date and report_date == str(latest.get("report_date") or "")[:10]:
        return "same_report_date_companion"
    if filing_date and filing_date == str(latest.get("filing_date") or "")[:10]:
        return "same_filing_date_companion"
    return "bounded_periodic_companion"


def build_recovery_scope(
    conn: sqlite3.Connection,
    *,
    lineage_rows: Iterable[Mapping[str, Any]],
    asof: str,
    max_accessions_per_ticker: int,
) -> list[dict[str, str]]:
    """Select only unresolved material accessions and bounded companions."""
    if max_accessions_per_ticker < 1:
        raise ValueError("max_accessions_per_ticker must be at least 1")
    scope: list[dict[str, str]] = []
    for lineage in sorted(lineage_rows, key=lambda row: str(row.get("ticker") or "")):
        if str(lineage.get("financial_lineage_classification") or "") != "CANONICALIZATION_GAP":
            continue
        ticker = str(lineage.get("ticker") or "").strip().upper()
        latest_accession = str(lineage.get("latest_material_financial_accession") or "").strip()
        if not ticker or not latest_accession:
            continue
        filings = [
            dict(row)
            for row in conn.execute(
                """
                SELECT ticker, accession_number, form_type, filing_date,
                       accepted_at, report_date, primary_document
                FROM fact_sec_filing
                WHERE ticker = ?
                ORDER BY COALESCE(NULLIF(accepted_at, ''), filing_date) DESC,
                         accession_number DESC
                """,
                (ticker,),
            ).fetchall()
        ]
        filings = [row for row in filings if _available_asof(row, asof)]
        latest = next(
            (row for row in filings if str(row.get("accession_number") or "") == latest_accession),
            {
                "ticker": ticker,
                "accession_number": latest_accession,
                "form_type": lineage.get("latest_material_financial_form") or "",
                "filing_date": lineage.get("latest_material_financial_filing_date") or "",
                "report_date": lineage.get("latest_material_financial_report_date") or "",
            },
        )
        candidates: list[dict[str, Any]] = [dict(latest)]
        latest_report = str(latest.get("report_date") or "")[:10]
        latest_filing = str(latest.get("filing_date") or "")[:10]
        latest_form = str(latest.get("form_type") or "").strip().upper()
        for filing in filings:
            accession = str(filing.get("accession_number") or "")
            if accession == latest_accession:
                continue
            candidate_report = str(filing.get("report_date") or "")[:10]
            candidate_filing = str(filing.get("filing_date") or "")[:10]
            same_report = bool(latest_report and candidate_report == latest_report)
            same_filing = bool(latest_filing and candidate_filing == latest_filing)
            bounded_periodic = False
            if latest_form in {"8-K", "8-K/A"} and str(filing.get("form_type") or "").upper() in PERIODIC_FINANCIAL_FORMS:
                try:
                    gap_days = (date.fromisoformat(latest_filing) - date.fromisoformat(candidate_filing)).days
                except ValueError:
                    gap_days = -1
                bounded_periodic = 0 <= gap_days <= 2
            if same_report or same_filing or bounded_periodic:
                candidates.append(filing)
        companions = sorted(
            candidates[1:],
            key=lambda row: _filing_priority(row, latest=latest),
        )
        selected = [candidates[0], *companions[: max_accessions_per_ticker - 1]]
        for index, filing in enumerate(selected):
            scope.append(
                {
                    "ticker": ticker,
                    "accession_number": str(filing.get("accession_number") or ""),
                    "form_type": str(filing.get("form_type") or ""),
                    "filing_date": str(filing.get("filing_date") or "")[:10],
                    "report_date": str(filing.get("report_date") or "")[:10],
                    "scope_reason": (
                        "latest_material_canonicalization_gap"
                        if index == 0
                        else _companion_reason(filing, latest=latest)
                    ),
                }
            )
    return scope


def classification_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                str(row.get("financial_lineage_classification") or "UNKNOWN")
                for row in rows
            ).items()
        )
    )


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    args = parse_args()
    try:
        asof = date.fromisoformat(str(args.asof)).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid --asof={args.asof!r}; expected YYYY-MM-DD") from exc
    family = str(args.model_family or "").strip()
    policy = policy_for_model_family(family)
    if not policy.enabled:
        raise ValueError(f"Financial-lineage recovery is not enabled for model_family={family!r}")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    output_root = PROJECT_ROOT / "output" / "industrials" / family / "stage4"
    scope_path = (
        args.scope_output_csv.expanduser().resolve()
        if args.scope_output_csv is not None
        else output_root / "financial_lineage_recovery_scope.csv"
    )
    manifest_path = (
        args.manifest_output_json.expanduser().resolve()
        if args.manifest_output_json is not None
        else output_root / "financial_lineage_recovery_manifest.json"
    )
    sec_output_path = (
        args.sec_output_csv.expanduser().resolve()
        if args.sec_output_csv is not None
        else output_root / "financial_lineage_recovery_sec.csv"
    )

    with sqlite3.connect(db_path, timeout=120.0) as conn:
        conn.row_factory = sqlite3.Row
        tickers = active_family_tickers(conn, model_family=family)
        before = build_financial_filing_lineage(
            conn,
            model_family=family,
            asof=asof,
            tickers=tickers,
        )
        before_rows = [before[ticker] for ticker in tickers]
        scope_rows = build_recovery_scope(
            conn,
            lineage_rows=before_rows,
            asof=asof,
            max_accessions_per_ticker=args.max_accessions_per_ticker,
        )
    write_csv_atomic(scope_path, list(SCOPE_FIELDS), scope_rows)

    return_code = 0
    if scope_rows:
        selected_tickers = sorted({row["ticker"] for row in scope_rows})
        command = [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "07_sync_industrials_sec_fundamentals.py"),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--model-family",
            family,
            "--tickers",
            ",".join(selected_tickers),
            "--archive-selected",
            "--archive-accession-scope-csv",
            str(scope_path),
            "--archive-scan-all-documents",
            "--asof",
            asof,
            "--skip-source-registry",
            "--output-csv",
            str(sec_output_path),
        ]
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        return_code = int(result.returncode)

    with sqlite3.connect(db_path, timeout=120.0) as conn:
        conn.row_factory = sqlite3.Row
        after = build_financial_filing_lineage(
            conn,
            model_family=family,
            asof=asof,
            tickers=tickers,
        )
        after_rows = [after[ticker] for ticker in tickers]
    before_gaps = sum(row["financial_lineage_gate"] != "1" for row in before_rows)
    after_gaps = sum(row["financial_lineage_gate"] != "1" for row in after_rows)
    manifest = {
        "acceptance": "PASS" if return_code == 0 else "FAIL",
        "asof_date": asof,
        "before_classification_counts": classification_counts(before_rows),
        "before_unresolved_count": before_gaps,
        "after_classification_counts": classification_counts(after_rows),
        "after_unresolved_count": after_gaps,
        "database_path": str(db_path),
        "model_family": family,
        "policy_version": policy.policy_version,
        "recovery_status": (
            "NO_WORK"
            if not scope_rows
            else "RECOVERED_ALL"
            if after_gaps == 0
            else "REMAINING_GAPS"
        ),
        "scope_accession_count": len(scope_rows),
        "scope_path": str(scope_path),
        "scope_ticker_count": len({row["ticker"] for row in scope_rows}),
        "sec_return_code": return_code,
    }
    write_json_atomic(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
