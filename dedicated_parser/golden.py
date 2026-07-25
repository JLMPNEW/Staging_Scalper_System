from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def load_corpus(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("expectations"), list):
        raise ValueError(f"Invalid golden corpus structure: {path}")
    return payload


def validate_corpus(
    conn: sqlite3.Connection,
    *,
    corpus_path: Path,
    table: str,
    run_id: int | None = None,
    value_tolerance: float = 1e-6,
    tickers: set[str] | None = None,
) -> list[str]:
    if table not in {
        "fact_sec_metric_disclosure_candidate",
        "sec_parser_metric_evidence_shadow",
    }:
        raise ValueError(f"Unsupported golden corpus table: {table}")
    corpus = load_corpus(corpus_path)
    errors: list[str] = []
    for expectation in corpus["expectations"]:
        if tickers is not None and str(expectation["ticker"]) not in tickers:
            continue
        alias = "c"
        clauses = [
            f"{alias}.ticker = ?",
            f"{alias}.accession_number = ?",
            (
                f"{alias}.document_name = ?"
                if table.startswith("fact_")
                else f"{alias}.source_document = ?"
            ),
            f"{alias}.metric_name = ?",
            f"{alias}.candidate_status = ?",
        ]
        params: list[Any] = [
            expectation["ticker"],
            expectation["accession_number"],
            expectation["document_name"],
            expectation["metric_name"],
            expectation["candidate_status"],
        ]
        from_clause = f"{table} AS {alias}"
        if (
            run_id is not None
            and table == "sec_parser_metric_evidence_shadow"
        ):
            from_clause += (
                " JOIN sec_parser_run_metric_evidence AS run_evidence"
                " ON run_evidence.evidence_key = c.evidence_key"
            )
            clauses.append("run_evidence.run_id = ?")
            params.append(run_id)
        rows = conn.execute(
            f"""
            SELECT c.candidate_value, c.unit, c.period_start, c.period_end,
                   c.status_reason
            FROM {from_clause}
            WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchall()
        expected_value = expectation.get("candidate_value")
        matched = False
        for row in rows:
            actual_value = row["candidate_value"]
            value_matches = (
                actual_value is None
                if expected_value is None
                else actual_value is not None
                and abs(float(actual_value) - float(expected_value))
                <= max(value_tolerance, abs(float(expected_value)) * 1e-9)
            )
            if not value_matches:
                continue
            if str(row["unit"] or "") != str(expectation.get("unit") or ""):
                continue
            if str(row["period_start"] or "") != str(
                expectation.get("period_start") or ""
            ):
                continue
            if str(row["period_end"] or "") != str(
                expectation.get("period_end") or ""
            ):
                continue
            reason_contains = str(expectation.get("reason_contains") or "")
            if reason_contains and reason_contains not in str(
                row["status_reason"] or ""
            ):
                continue
            matched = True
            break
        expect_absent = bool(expectation.get("expect_absent", False))
        if expect_absent and matched:
            errors.append(
                f"{expectation['id']}: prohibited row found in {table}"
            )
        elif not expect_absent and not matched:
            errors.append(
                f"{expectation['id']}: expected row not found in {table}"
            )
    return errors
