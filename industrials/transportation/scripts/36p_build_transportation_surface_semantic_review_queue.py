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
from industrials.transportation.investable_universe import (  # noqa: E402
    SurfaceMetricDomainRule,
    load_investable_universe_policy,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DATA_ROOT = PROJECT_ROOT / "industrials" / "transportation" / "data"
SOURCE_MAP = DATA_ROOT / "transportation_surface_metric_source_map_v1.csv"
POLICY = DATA_ROOT / "transportation_investable_universe_v3.yaml"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
)
QUEUE_FIELDS = (
    "review_priority",
    "source_lane",
    "run_id",
    "asof_date",
    "ticker",
    "metric_id",
    "comparison_domain_ids",
    "candidate_domain_flag",
    "metric_domain_contracts_json",
    "source_posture",
    "candidate_value",
    "unit",
    "period_start",
    "period_end",
    "filing_date",
    "accepted_at",
    "form_type",
    "accession_number",
    "concept_name",
    "extraction_method",
    "status_reason",
    "confidence",
    "formula",
    "numerator_concept",
    "denominator_concept",
    "source_document",
    "source_path",
    "source_content_sha256",
    "evidence_key",
    "evidence_text",
    "provenance_json",
    "represented_candidate_count",
    "represented_period_count",
    "review_decision",
    "review_notes",
    "reviewed_by",
    "reviewed_at",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deduplicated, run-scoped semantic review queue for the "
            "surface-freight parser. This is read-only and never promotes evidence."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--fact-store-candidates", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _pipe(value: object) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value or "").split("|") if item.strip())


def _json_object(value: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalized(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def _priority(source_lane: str, extraction_method: str, reason: str) -> str:
    method = extraction_method.lower()
    if source_lane == "fact_store_ratio":
        return "HIGH" if "broad" not in reason.lower() else "MEDIUM"
    if "surface" in method and ("table" in method or "xbrl" in method):
        return "HIGH"
    if "table" in method:
        return "MEDIUM"
    return "LOW"


def _domain_contracts(rules: tuple[SurfaceMetricDomainRule, ...]) -> str:
    return json.dumps(
        [
            {
                "comparison_domain_id": rule.comparison_domain_id,
                "applicable_ticker_count": len(rule.applicable_tickers),
                "required_accepted_breadth": rule.minimum_accepted_breadth,
                "calibration_eligibility": rule.calibration_eligibility,
                "normalization_scope": rule.normalization_scope,
            }
            for rule in rules
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _domain_fields(rules: tuple[SurfaceMetricDomainRule, ...]) -> dict[str, object]:
    return {
        "comparison_domain_ids": "|".join(rule.comparison_domain_id for rule in rules),
        "candidate_domain_flag": int(any(rule.is_calibration_candidate for rule in rules)),
        "metric_domain_contracts_json": _domain_contracts(rules),
    }


def _domain_priority(
    source_lane: str,
    extraction_method: str,
    reason: str,
    rules: tuple[SurfaceMetricDomainRule, ...],
) -> str:
    if not any(rule.is_calibration_candidate for rule in rules):
        return "LOW"
    return _priority(source_lane, extraction_method, reason)


def _signature(row: Mapping[str, object]) -> tuple[str, ...]:
    return (
        str(row.get("source_lane") or ""),
        str(row.get("ticker") or ""),
        str(row.get("metric_id") or ""),
        _normalized(row.get("concept_name")),
        _normalized(row.get("unit")),
        _normalized(row.get("extraction_method")),
        _normalized(row.get("status_reason")),
        _normalized(row.get("formula")),
        _normalized(row.get("numerator_concept")),
        _normalized(row.get("denominator_concept")),
    )


def _representatives(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: defaultdict[tuple[str, ...], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[_signature(row)].append(row)
    output: list[dict[str, object]] = []
    for signature in sorted(grouped):
        candidates = grouped[signature]
        representative = max(
            candidates,
            key=lambda row: (
                float(row.get("confidence") or 0.0),
                str(row.get("filing_date") or ""),
                str(row.get("period_end") or ""),
                str(row.get("evidence_key") or ""),
            ),
        ).copy()
        representative["represented_candidate_count"] = len(candidates)
        representative["represented_period_count"] = len(
            {str(row.get("period_end") or "") for row in candidates}
        )
        output.append(representative)
    return output


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    fact_store_path = (
        args.fact_store_candidates.expanduser().resolve()
        if args.fact_store_candidates
        else output_dir / "transportation_surface_fact_store_ratio_candidates.csv"
    )

    source_rows = _csv_rows(SOURCE_MAP)
    source_by_metric = {row["metric_id"]: row for row in source_rows}
    policy = load_investable_universe_policy(POLICY)
    rules_by_pair: defaultdict[
        tuple[str, str], list[SurfaceMetricDomainRule]
    ] = defaultdict(list)
    for rule in policy.surface_metric_domain_rules:
        if rule.metric_id == "surface_volume_growth":
            continue
        for ticker in rule.applicable_tickers:
            rules_by_pair[(ticker, rule.metric_id)].append(rule)
    frozen_rules_by_pair = {
        key: tuple(sorted(rules, key=lambda item: item.comparison_domain_id))
        for key, rules in rules_by_pair.items()
    }

    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    run = connection.execute(
        "SELECT * FROM sec_parser_run WHERE run_id=?", (args.run_id,)
    ).fetchone()
    if (
        run is None
        or str(run["model_family"]) != "transportation"
        or str(run["asof_date"]) != args.asof
        or str(run["status"]) != "COMPLETED"
        or int(run["failed_work_count"] or 0) != 0
    ):
        raise ValueError("run must be a completed zero-failure transportation run at the requested asof")

    evidence_rows = connection.execute(
        "SELECT evidence.*, "
        "(SELECT catalog.source_path FROM sec_parser_document_catalog AS catalog "
        " WHERE catalog.cik=evidence.cik AND catalog.accession_number=evidence.accession_number "
        " AND catalog.document_name=evidence.source_document "
        " ORDER BY catalog.cataloged_at DESC LIMIT 1) AS source_path, "
        "(SELECT catalog.content_sha256 FROM sec_parser_document_catalog AS catalog "
        " WHERE catalog.cik=evidence.cik AND catalog.accession_number=evidence.accession_number "
        " AND catalog.document_name=evidence.source_document "
        " ORDER BY catalog.cataloged_at DESC LIMIT 1) AS source_content_sha256 "
        "FROM sec_parser_run_metric_evidence AS relation "
        "JOIN sec_parser_metric_evidence_shadow AS evidence "
        "ON evidence.evidence_key=relation.evidence_key "
        "WHERE relation.run_id=? AND evidence.candidate_value IS NOT NULL "
        "AND UPPER(evidence.candidate_status) IN ('REVIEW','REVIEW_REQUIRED','PENDING_REVIEW') "
        "ORDER BY evidence.ticker, evidence.metric_name, evidence.period_end, evidence.evidence_key",
        (args.run_id,),
    ).fetchall()

    raw_queue: list[dict[str, object]] = []
    for evidence in evidence_rows:
        ticker = str(evidence["ticker"])
        metric = str(evidence["metric_name"])
        if (ticker, metric) not in frozen_rules_by_pair:
            continue
        domain_rules = frozen_rules_by_pair[(ticker, metric)]
        provenance = _json_object(evidence["provenance_json"])
        method = str(evidence["extraction_method"] or "")
        reason = str(evidence["status_reason"] or "")
        raw_queue.append(
            {
                "review_priority": _domain_priority(
                    "parser_run_evidence", method, reason, domain_rules
                ),
                "source_lane": "parser_run_evidence",
                "run_id": args.run_id,
                "asof_date": args.asof,
                "ticker": ticker,
                "metric_id": metric,
                **_domain_fields(domain_rules),
                "source_posture": source_by_metric[metric]["source_posture"],
                "candidate_value": evidence["candidate_value"],
                "unit": evidence["unit"],
                "period_start": evidence["period_start"],
                "period_end": evidence["period_end"],
                "filing_date": evidence["filing_date"],
                "accepted_at": evidence["accepted_at"],
                "form_type": evidence["form_type"],
                "accession_number": evidence["accession_number"],
                "concept_name": evidence["concept_name"],
                "extraction_method": method,
                "status_reason": reason,
                "confidence": evidence["confidence"],
                "formula": provenance.get("formula", ""),
                "numerator_concept": provenance.get("numerator_concept", ""),
                "denominator_concept": provenance.get("denominator_concept", ""),
                "source_document": evidence["source_document"],
                "source_path": evidence["source_path"],
                "source_content_sha256": evidence["source_content_sha256"],
                "evidence_key": evidence["evidence_key"],
                "evidence_text": evidence["evidence_text"],
                "provenance_json": evidence["provenance_json"],
                "review_decision": "",
                "review_notes": "",
                "reviewed_by": "",
                "reviewed_at": "",
            }
        )

    fact_store_rows = _csv_rows(fact_store_path) if fact_store_path.exists() else []
    for row in fact_store_rows:
        ticker = row["ticker"]
        metric = row["metric_id"]
        if (ticker, metric) not in frozen_rules_by_pair:
            continue
        domain_rules = frozen_rules_by_pair[(ticker, metric)]
        method = "loaded_sec_fact_store_ratio"
        reason = row["reason"]
        raw_queue.append(
            {
                "review_priority": _domain_priority(
                    "fact_store_ratio", method, reason, domain_rules
                ),
                "source_lane": "fact_store_ratio",
                "run_id": args.run_id,
                "asof_date": args.asof,
                "ticker": ticker,
                "metric_id": metric,
                **_domain_fields(domain_rules),
                "source_posture": source_by_metric[metric]["source_posture"],
                "candidate_value": row["value"],
                "unit": row["unit"],
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "filing_date": row["filing_date"],
                "accepted_at": row["accepted_at"],
                "form_type": row["form_type"],
                "accession_number": row["accession_number"],
                "concept_name": row["numerator_concept"],
                "extraction_method": method,
                "status_reason": reason,
                "confidence": row["confidence"],
                "formula": row["formula"],
                "numerator_concept": row["numerator_concept"],
                "denominator_concept": row["denominator_concept"],
                "source_document": row["source_id"],
                "source_path": "",
                "source_content_sha256": "",
                "evidence_key": "",
                "evidence_text": "",
                "provenance_json": json.dumps(
                    {
                        "currency": row["currency"],
                        "numerator_value": row["numerator_value"],
                        "denominator_value": row["denominator_value"],
                        "fact_store_recovery_version": row["fact_store_recovery_version"],
                    },
                    sort_keys=True,
                ),
                "review_decision": "",
                "review_notes": "",
                "reviewed_by": "",
                "reviewed_at": "",
            }
        )

    representatives = _representatives(raw_queue)
    priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    representatives.sort(
        key=lambda row: (
            priority_order.get(str(row["review_priority"]), 9),
            str(row["metric_id"]),
            str(row["ticker"]),
            str(row["concept_name"]),
        )
    )
    queue_path = output_dir / "transportation_surface_semantic_review_queue.csv"
    write_csv_atomic(queue_path, QUEUE_FIELDS, representatives)

    summary: dict[str, Any] = {
        "acceptance": "PASS",
        "asof_date": args.asof,
        "run_id": args.run_id,
        "source_run_adapter_version": str(run["adapter_version"]),
        "surface_domain_policy_version": policy.surface_domain_policy_version,
        "surface_domain_mapping_sha256": file_sha256(
            policy.surface_metric_domain_mapping_path
        ),
        "surface_metric_source_map_sha256": file_sha256(
            policy.surface_metric_source_map_path
        ),
        "investable_universe_policy_sha256": file_sha256(POLICY),
        "raw_parser_review_candidate_count": len(evidence_rows),
        "raw_fact_store_candidate_count": len(fact_store_rows),
        "applicable_raw_candidate_count": len(raw_queue),
        "deduplicated_definition_review_count": len(representatives),
        "review_counts_by_priority": dict(
            sorted(Counter(str(row["review_priority"]) for row in representatives).items())
        ),
        "review_counts_by_metric": dict(
            sorted(Counter(str(row["metric_id"]) for row in representatives).items())
        ),
        "review_counts_by_source_lane": dict(
            sorted(Counter(str(row["source_lane"]) for row in representatives).items())
        ),
        "candidate_domain_definition_review_count": sum(
            int(row["candidate_domain_flag"]) for row in representatives
        ),
        "diagnostic_only_definition_review_count": sum(
            not int(row["candidate_domain_flag"]) for row in representatives
        ),
        "queue_csv": str(queue_path),
        "source_document_reparse_count": 0,
        "canonical_candidate_mutation": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "REVIEW_EACH_DEFINITION_ONCE_THEN_REPLAY_ACCEPTED_SIGNATURES",
    }
    write_text_atomic(
        output_dir / "transportation_surface_semantic_review_queue.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
