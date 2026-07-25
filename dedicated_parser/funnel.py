from __future__ import annotations

import csv
import json
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.storage import load_run


@dataclass
class _EvidenceGroup:
    count: int = 0
    tickers: set[str] = field(default_factory=set)
    accessions: set[str] = field(default_factory=set)


def _json_object(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _extraction_stage(
    *,
    method: str,
    provenance_json: str,
) -> str:
    normalized = method.lower()
    provenance = provenance_json.lower()
    if "ocr" in normalized or "ocr" in provenance or ".pdf" in provenance:
        return "pdf_ocr"
    if "arelle" in normalized or "xbrl" in normalized:
        return "xbrl_mapping"
    if "semantic_html_table" in normalized:
        return "semantic_table"
    if "explicit_rpo" in normalized:
        return "semantic_text_derivation"
    return "prose_text"


def extraction_funnel_rows(
    conn: sqlite3.Connection,
    *,
    run_id: int,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], _EvidenceGroup] = {}
    evidence_rows = conn.execute(
        """
        SELECT e.ticker, e.accession_number, e.metric_name,
               e.candidate_status, e.extraction_method, e.provenance_json
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS e
          ON e.evidence_key = relation.evidence_key
        WHERE relation.run_id = ?
        """,
        (run_id,),
    ).fetchall()
    for row in evidence_rows:
        method = str(row["extraction_method"] or "")
        stage = _extraction_stage(
            method=method,
            provenance_json=str(row["provenance_json"] or ""),
        )
        key = (
            stage,
            method,
            str(row["metric_name"]),
            str(row["candidate_status"]),
        )
        group = groups.setdefault(key, _EvidenceGroup())
        group.count += 1
        group.tickers.add(str(row["ticker"]))
        group.accessions.add(str(row["accession_number"]))

    output: list[dict[str, Any]] = []
    for (stage, method, metric, status), group in sorted(groups.items()):
        output.append(
            {
                "run_id": run_id,
                "stage": stage,
                "extraction_method": method,
                "metric_name": metric,
                "candidate_status": status,
                "evidence_count": group.count,
                "distinct_tickers": len(group.tickers),
                "distinct_accessions": len(group.accessions),
            }
        )
    return output


def build_extraction_funnel(
    conn: sqlite3.Connection,
    *,
    run_id: int,
) -> dict[str, Any]:
    run = load_run(conn, run_id=run_id)
    metadata = _json_object(run.get("metadata_json"))
    plan = metadata.get("plan")
    plan = plan if isinstance(plan, dict) else {}

    work_counts = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT ledger.status, COUNT(*) AS count
            FROM sec_parser_run_work AS relation
            JOIN sec_parser_work_ledger AS ledger
              ON ledger.work_key = relation.work_key
            WHERE relation.run_id = ?
            GROUP BY ledger.status
            """,
            (run_id,),
        )
    }
    provider_counts = {
        str(row["provider"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT fact.provider, COUNT(*) AS count
            FROM sec_parser_run_normalized_fact AS relation
            JOIN sec_parser_normalized_fact_shadow AS fact
              ON fact.fact_fingerprint = relation.fact_fingerprint
            WHERE relation.run_id = ?
            GROUP BY fact.provider
            """,
            (run_id,),
        )
    }
    evidence_rows = extraction_funnel_rows(conn, run_id=run_id)
    evidence_stage_counts: Counter[str] = Counter()
    evidence_status_counts: Counter[str] = Counter()
    metric_status_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in evidence_rows:
        count = int(row["evidence_count"])
        evidence_stage_counts[str(row["stage"])] += count
        evidence_status_counts[str(row["candidate_status"])] += count
        metric_status_counts[str(row["metric_name"])][
            str(row["candidate_status"])
        ] += count

    assessment_counts = {
        str(row["recovery_class"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT recovery_class, COUNT(*) AS count
            FROM sec_parser_recovery_assessment
            WHERE run_id = ?
            GROUP BY recovery_class
            """,
            (run_id,),
        )
    }
    return {
        "run": {
            key: run[key]
            for key in (
                "run_id",
                "model_family",
                "asof_date",
                "parser_release",
                "adapter_version",
                "mode",
                "status",
                "planned_work_count",
                "completed_work_count",
                "failed_work_count",
            )
        },
        "cache": {
            "scheduled_accessions": int(
                plan.get("scheduled_accessions") or 0
            ),
            "scheduled_documents": int(
                plan.get("scheduled_documents") or 0
            ),
            "missing_cache_accessions": int(
                plan.get("missing_cache_accessions") or 0
            ),
            "missing_cache_details": list(
                plan.get("missing_cache_details") or []
            ),
            "complete": not bool(
                int(plan.get("missing_cache_accessions") or 0)
            ),
        },
        "work_status_counts": dict(sorted(work_counts.items())),
        "normalized_fact_provider_counts": dict(
            sorted(provider_counts.items())
        ),
        "evidence_stage_counts": dict(
            sorted(evidence_stage_counts.items())
        ),
        "evidence_status_counts": dict(
            sorted(evidence_status_counts.items())
        ),
        "metric_status_counts": {
            metric: dict(sorted(counts.items()))
            for metric, counts in sorted(metric_status_counts.items())
        },
        "recovery_class_counts": dict(sorted(assessment_counts.items())),
        "detail_rows": evidence_rows,
    }


def write_funnel_csv(
    path: Path,
    rows: Iterable[dict[str, Any]],
) -> None:
    records = list(rows)
    columns = [
        "run_id",
        "stage",
        "extraction_method",
        "metric_name",
        "candidate_status",
        "evidence_count",
        "distinct_tickers",
        "distinct_accessions",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)
    os.replace(temporary, path)
