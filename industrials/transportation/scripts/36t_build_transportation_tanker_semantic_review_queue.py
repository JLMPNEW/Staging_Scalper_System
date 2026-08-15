#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.investable_universe import load_investable_universe_policy  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DATA_ROOT = PROJECT_ROOT / "industrials" / "transportation" / "data"
SOURCE_MAP = DATA_ROOT / "transportation_tanker_metric_source_map_v1.csv"
POLICY = DATA_ROOT / "transportation_investable_universe_v3.yaml"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v3" / "tanker_delta"
QUEUE_FIELDS = (
    "review_priority", "source_lane", "run_id", "asof_date", "ticker", "metric_id",
    "comparison_domain_ids", "candidate_domain_flag", "metric_domain_contracts_json",
    "source_posture", "candidate_value", "unit", "period_start", "period_end",
    "filing_date", "accepted_at", "form_type", "accession_number", "concept_name",
    "extraction_method", "status_reason", "confidence", "formula", "numerator_concept",
    "denominator_concept", "source_document", "source_path", "source_content_sha256",
    "evidence_key", "evidence_text", "provenance_json", "represented_candidate_count",
    "represented_period_count", "review_decision", "review_notes", "reviewed_by", "reviewed_at",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a deduplicated semantic review queue for tanker metrics.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _json(value: object) -> dict[str, object]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalized(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def _signature(row: Mapping[str, object]) -> tuple[str, ...]:
    return (
        str(row.get("source_lane") or ""), str(row.get("ticker") or ""),
        str(row.get("metric_id") or ""), _normalized(row.get("concept_name")),
        _normalized(row.get("unit")), _normalized(row.get("extraction_method")),
        _normalized(row.get("status_reason")), _normalized(row.get("formula")),
        _normalized(row.get("numerator_concept")), _normalized(row.get("denominator_concept")),
    )


def _priority(method: str, reason: str) -> str:
    value = method.casefold()
    if "transportation_table_derivation" in value or "strict" in reason.casefold():
        return "HIGH"
    if "table" in value or "xbrl" in value:
        return "MEDIUM"
    return "LOW"


def _representatives(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: defaultdict[tuple[str, ...], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[_signature(row)].append(row)
    output: list[dict[str, object]] = []
    for signature in sorted(grouped):
        candidates = grouped[signature]
        representative = max(candidates, key=lambda row: (
            float(row.get("confidence") or 0.0), str(row.get("filing_date") or ""),
            str(row.get("period_end") or ""), str(row.get("evidence_key") or ""),
        )).copy()
        representative["represented_candidate_count"] = len(candidates)
        representative["represented_period_count"] = len({str(row.get("period_end") or "") for row in candidates})
        output.append(representative)
    return output


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else DEFAULT_OUTPUT_ROOT / args.asof
    output_dir.mkdir(parents=True, exist_ok=True)
    policy = load_investable_universe_policy(POLICY)
    tickers = set(policy.tanker_tickers)
    metrics = set(policy.direct_tanker_metrics)
    minimum_breadth = next(group.minimum_specialized_breadth for group in policy.groups if group.group_id == "oil_tanker_operators")

    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    run = connection.execute("SELECT * FROM sec_parser_run WHERE run_id=?", (args.run_id,)).fetchone()
    if (
        run is None or str(run["model_family"]) != "transportation"
        or str(run["asof_date"]) != args.asof or str(run["status"]) != "COMPLETED"
        or int(run["failed_work_count"] or 0) != 0
    ):
        raise ValueError("run must be a completed zero-failure transportation run at the requested asof")

    evidence_rows = connection.execute(
        "SELECT evidence.*, "
        "(SELECT catalog.source_path FROM sec_parser_document_catalog AS catalog "
        " WHERE catalog.cik=evidence.cik AND catalog.accession_number=evidence.accession_number "
        " AND catalog.document_name=evidence.source_document ORDER BY catalog.cataloged_at DESC LIMIT 1) AS source_path, "
        "(SELECT catalog.content_sha256 FROM sec_parser_document_catalog AS catalog "
        " WHERE catalog.cik=evidence.cik AND catalog.accession_number=evidence.accession_number "
        " AND catalog.document_name=evidence.source_document ORDER BY catalog.cataloged_at DESC LIMIT 1) AS source_content_sha256 "
        "FROM sec_parser_run_metric_evidence AS relation "
        "JOIN sec_parser_metric_evidence_shadow AS evidence ON evidence.evidence_key=relation.evidence_key "
        "WHERE relation.run_id=? AND evidence.candidate_value IS NOT NULL "
        "AND UPPER(evidence.candidate_status) IN ('REVIEW','REVIEW_REQUIRED','PENDING_REVIEW') "
        "ORDER BY evidence.ticker, evidence.metric_name, evidence.period_end, evidence.evidence_key",
        (args.run_id,),
    ).fetchall()
    domain_contract = json.dumps({
        "comparison_domain_id": "oil_tanker_operators",
        "applicable_ticker_count": len(tickers),
        "required_accepted_breadth": minimum_breadth,
        "calibration_eligibility": "CANDIDATE",
        "normalization_scope": "within_oil_tanker_operators",
    }, sort_keys=True, separators=(",", ":"))
    queue: list[dict[str, object]] = []
    for evidence in evidence_rows:
        ticker = str(evidence["ticker"])
        metric = str(evidence["metric_name"])
        if ticker not in tickers or metric not in metrics:
            continue
        provenance = _json(evidence["provenance_json"])
        method = str(evidence["extraction_method"] or "")
        reason = str(evidence["status_reason"] or "")
        queue.append({
            "review_priority": _priority(method, reason), "source_lane": "parser_run_evidence",
            "run_id": args.run_id, "asof_date": args.asof, "ticker": ticker, "metric_id": metric,
            "comparison_domain_ids": "oil_tanker_operators", "candidate_domain_flag": 1,
            "metric_domain_contracts_json": domain_contract,
            "source_posture": "sec_filing_audited_or_operating_disclosure",
            "candidate_value": evidence["candidate_value"], "unit": evidence["unit"],
            "period_start": evidence["period_start"], "period_end": evidence["period_end"],
            "filing_date": evidence["filing_date"], "accepted_at": evidence["accepted_at"],
            "form_type": evidence["form_type"], "accession_number": evidence["accession_number"],
            "concept_name": evidence["concept_name"], "extraction_method": method,
            "status_reason": reason, "confidence": evidence["confidence"],
            "formula": provenance.get("formula", ""),
            "numerator_concept": provenance.get("numerator_concept", ""),
            "denominator_concept": provenance.get("denominator_concept", ""),
            "source_document": evidence["source_document"], "source_path": evidence["source_path"],
            "source_content_sha256": evidence["source_content_sha256"],
            "evidence_key": evidence["evidence_key"], "evidence_text": evidence["evidence_text"],
            "provenance_json": evidence["provenance_json"], "review_decision": "",
            "review_notes": "", "reviewed_by": "", "reviewed_at": "",
        })

    representatives = _representatives(queue)
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    representatives.sort(key=lambda row: (
        order.get(str(row["review_priority"]), 9), str(row["metric_id"]),
        str(row["ticker"]), str(row["concept_name"]),
    ))
    queue_path = output_dir / "transportation_tanker_semantic_review_queue.csv"
    write_csv_atomic(queue_path, QUEUE_FIELDS, representatives)
    summary: dict[str, Any] = {
        "acceptance": "PASS", "asof_date": args.asof, "run_id": args.run_id,
        "source_run_adapter_version": str(run["adapter_version"]),
        "tanker_metric_source_map_sha256": file_sha256(SOURCE_MAP),
        "investable_universe_policy_sha256": file_sha256(POLICY),
        "raw_parser_review_candidate_count": len(evidence_rows),
        "applicable_raw_candidate_count": len(queue),
        "deduplicated_definition_review_count": len(representatives),
        "review_counts_by_priority": dict(sorted(Counter(str(row["review_priority"]) for row in representatives).items())),
        "review_counts_by_metric": dict(sorted(Counter(str(row["metric_id"]) for row in representatives).items())),
        "queue_csv": str(queue_path), "source_document_reparse_count": 0,
        "canonical_candidate_mutation": False, "calibration_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "REVIEW_EACH_TANKER_DEFINITION_ONCE_THEN_REPLAY_ACCEPTED_SIGNATURES",
    }
    write_text_atomic(output_dir / "transportation_tanker_semantic_review_queue.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
