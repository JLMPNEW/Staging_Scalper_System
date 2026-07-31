#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.promotion import (  # noqa: E402
    _conflicting_evidence_keys,
    _promotion_block_reason,
)
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.required_metric_repair import (  # noqa: E402
    STALE_FACT_MAX_LAG_DAYS,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    resolve_foundation,
)


ADAPTER = (
    "industrials.transportation.required_metric_parser_adapter:"
    "extract_metric_evidence"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "required_metric_repair"
)
EVIDENCE_FIELDS = (
    "run_id",
    "ticker",
    "metric_name",
    "recovery_class",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "period_start",
    "period_end",
    "concept_name",
    "candidate_value",
    "unit",
    "scope",
    "confidence",
    "candidate_status",
    "status_reason",
    "evidence_text",
    "extraction_method",
    "source_document",
    "evidence_key",
    "requested_dependency",
    "current_period",
    "promotion_preflight_action",
    "promotion_preflight_reason",
)
CANDIDATE_FIELDS = (
    "ticker",
    "metric_name",
    "recovery_class",
    "current_period",
    "accession_number",
    "form_type",
    "filing_date",
    "period_start",
    "period_end",
    "concept_name",
    "unit",
    "candidate_value_count",
    "candidate_values",
    "evidence_count",
    "consolidated_evidence_count",
    "review_required_evidence_count",
    "source_documents",
    "required_action",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the immutable evidence from the one-pass transportation "
            "required-metric parser before any canonical promotion."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--min-confidence", type=float, default=0.90)
    return parser.parse_args()


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "") for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _pipe(value: object) -> set[str]:
    return {
        item.strip()
        for item in str(value or "").split("|")
        if item.strip()
    }


def _short_number(value: object) -> str:
    try:
        return format(float(str(value)), ".12g")
    except (TypeError, ValueError):
        return str(value or "")


def main() -> int:
    args = parse_args()
    asof_date = str(args.asof)[:10]
    cutoff = (
        date.fromisoformat(asof_date)
        - timedelta(days=STALE_FACT_MAX_LAG_DAYS)
    ).isoformat()
    output_dir = args.output_root.expanduser().resolve() / asof_date
    execution_path = (
        output_dir
        / "transportation_required_metric_parser_execution.json"
    )
    source_path = (
        output_dir
        / "transportation_required_metric_parser_source_manifest.csv"
    )
    run_dir = output_dir / "required_metric_parser_run"
    recovery_path = run_dir / "dedicated_parser_recovery_assessment.csv"
    for path in (execution_path, source_path, recovery_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    execution = _json(execution_path)
    if execution.get("acceptance") != "PASS":
        raise ValueError("Required-metric parser execution is not PASS")
    if str(execution.get("asof_date") or "") != asof_date:
        raise ValueError("Parser execution as-of date does not match request")
    run_id = int(execution.get("run_id") or 0)
    if run_id <= 0:
        raise ValueError("Parser execution has no valid run_id")

    requested_by_ticker: dict[str, set[str]] = defaultdict(set)
    manifest_accessions: set[tuple[str, str, str]] = set()
    for row in _csv(source_path):
        ticker = row["ticker"].upper()
        requested_by_ticker[ticker].update(
            _pipe(row.get("requested_dependency_ids"))
        )
        manifest_accessions.add(
            (
                ticker,
                row["accession_number"],
                row["primary_document"],
            )
        )
    recovery_class = {
        (row["ticker"].upper(), row["metric_name"]): row["recovery_class"]
        for row in _csv(recovery_path)
    }

    foundation = resolve_foundation(
        args.config.expanduser().resolve(),
        args.db,
    )
    registry = load_registry(ADAPTER)
    errors: list[str] = []
    with connect_database(
        foundation.db_path,
        timeout_seconds=foundation.timeout_sec,
        readonly=True,
    ) as connection:
        run = connection.execute(
            "SELECT * FROM sec_parser_run WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise ValueError(f"run_id={run_id} does not exist")
        if str(run["model_family"]) != registry.model_family:
            errors.append("run model family does not match adapter")
        if str(run["adapter_version"]) != registry.adapter_version:
            errors.append("run adapter version does not match current adapter")
        if str(run["status"]) != "COMPLETED":
            errors.append("parser run is not COMPLETED")
        if int(run["planned_work_count"] or 0) != 178:
            errors.append("parser run planned work count is not 178")
        if int(run["completed_work_count"] or 0) != 178:
            errors.append("parser run completed work count is not 178")
        if int(run["failed_work_count"] or 0) != 0:
            errors.append("parser run has failed work")
        rows = connection.execute(
            """
            SELECT evidence.*
            FROM sec_parser_run_metric_evidence AS relation
            JOIN sec_parser_metric_evidence_shadow AS evidence
              ON evidence.evidence_key=relation.evidence_key
            WHERE relation.run_id=?
            ORDER BY evidence.ticker, evidence.metric_name,
                     evidence.period_end, evidence.period_start,
                     evidence.accession_number, evidence.evidence_key
            """,
            (run_id,),
        ).fetchall()
        conflicting_keys = _conflicting_evidence_keys(
            rows,
            registry=registry,
            asof_date=asof_date,
            min_confidence=args.min_confidence,
        )
        evidence_rows: list[dict[str, object]] = []
        for row in rows:
            ticker = str(row["ticker"]).upper()
            metric_name = str(row["metric_name"])
            extraction_method = str(row["extraction_method"] or "")
            candidate_status = str(row["candidate_status"] or "")
            manifest_key = (
                ticker,
                str(row["accession_number"]),
                str(row["source_document"]),
            )
            if manifest_key not in manifest_accessions:
                errors.append(
                    "evidence outside sealed source manifest="
                    f"{ticker}|{row['accession_number']}|"
                    f"{row['source_document']}"
                )
            if (
                "extension_candidate" in extraction_method
                and candidate_status != "REVIEW_REQUIRED"
            ):
                errors.append(
                    "issuer-extension evidence is not review-only="
                    f"{row['evidence_key']}"
                )
            reason = _promotion_block_reason(
                row,
                registry=registry,
                asof_date=asof_date,
                min_confidence=args.min_confidence,
                conflicting_keys=conflicting_keys,
            )
            evidence_rows.append(
                {
                    "run_id": run_id,
                    "ticker": ticker,
                    "metric_name": metric_name,
                    "recovery_class": recovery_class.get(
                        (ticker, metric_name), ""
                    ),
                    "accession_number": str(row["accession_number"]),
                    "form_type": str(row["form_type"] or ""),
                    "filing_date": str(row["filing_date"] or "")[:10],
                    "accepted_at": str(row["accepted_at"] or ""),
                    "period_start": str(row["period_start"] or "")[:10],
                    "period_end": str(row["period_end"] or "")[:10],
                    "concept_name": str(row["concept_name"] or ""),
                    "candidate_value": (
                        ""
                        if row["candidate_value"] is None
                        else row["candidate_value"]
                    ),
                    "unit": str(row["unit"] or ""),
                    "scope": str(row["scope"] or ""),
                    "confidence": row["confidence"],
                    "candidate_status": candidate_status,
                    "status_reason": str(row["status_reason"] or ""),
                    "evidence_text": str(row["evidence_text"] or ""),
                    "extraction_method": extraction_method,
                    "source_document": str(row["source_document"] or ""),
                    "evidence_key": str(row["evidence_key"]),
                    "requested_dependency": int(
                        metric_name in requested_by_ticker.get(ticker, set())
                    ),
                    "current_period": int(
                        str(row["period_end"] or "")[:10] >= cutoff
                    ),
                    "promotion_preflight_action": (
                        "PROMOTABLE" if not reason else "BLOCKED"
                    ),
                    "promotion_preflight_reason": (
                        "accepted_evidence_promotable" if not reason else reason
                    ),
                }
            )

    grouped: dict[
        tuple[str, str, str, str, str, str, str, str, str],
        list[dict[str, object]],
    ] = defaultdict(list)
    for row in evidence_rows:
        if row["candidate_status"] != "REVIEW_REQUIRED":
            continue
        if "extension_candidate" not in str(row["extraction_method"]):
            continue
        key = (
            str(row["ticker"]),
            str(row["metric_name"]),
            str(row["recovery_class"]),
            str(row["current_period"]),
            str(row["accession_number"]),
            str(row["form_type"]),
            str(row["filing_date"]),
            str(row["period_start"]),
            str(row["period_end"]),
        )
        grouped[key].append(row)
    candidate_rows: list[dict[str, object]] = []
    for key, candidates in sorted(grouped.items()):
        values = sorted(
            {
                _short_number(row["candidate_value"])
                for row in candidates
                if str(row["candidate_value"]) != ""
            }
        )
        candidate_rows.append(
            {
                "ticker": key[0],
                "metric_name": key[1],
                "recovery_class": key[2],
                "current_period": key[3],
                "accession_number": key[4],
                "form_type": key[5],
                "filing_date": key[6],
                "period_start": key[7],
                "period_end": key[8],
                "concept_name": "|".join(
                    sorted({str(row["concept_name"]) for row in candidates})
                ),
                "unit": "|".join(
                    sorted({str(row["unit"]) for row in candidates})
                ),
                "candidate_value_count": len(values),
                "candidate_values": "|".join(values),
                "evidence_count": len(candidates),
                "consolidated_evidence_count": sum(
                    str(row["scope"]) == "consolidated"
                    for row in candidates
                ),
                "review_required_evidence_count": len(candidates),
                "source_documents": "|".join(
                    sorted(
                        {str(row["source_document"]) for row in candidates}
                    )
                ),
                "required_action": (
                    "SEMANTIC_TIE_OUT_BEFORE_EXACT_POLICY"
                ),
            }
        )

    evidence_path = (
        run_dir
        / "transportation_required_metric_parser_evidence_audit.csv"
    )
    candidate_path = (
        run_dir
        / "transportation_required_metric_extension_candidate_audit.csv"
    )
    manifest_path = (
        output_dir
        / "transportation_required_metric_parser_evidence_audit.json"
    )
    write_csv_atomic(evidence_path, EVIDENCE_FIELDS, evidence_rows)
    write_csv_atomic(candidate_path, CANDIDATE_FIELDS, candidate_rows)

    status_counts = Counter(
        str(row["candidate_status"]) for row in evidence_rows
    )
    preflight_counts = Counter(
        str(row["promotion_preflight_action"]) for row in evidence_rows
    )
    recovery_counts = Counter(
        (
            str(row["ticker"]),
            str(row["metric_name"]),
            str(row["recovery_class"]),
        )
        for row in evidence_rows
        if str(row["recovery_class"])
    )
    clean_recoveries = sorted(
        {
            f"{ticker}|{metric}"
            for ticker, metric, classification in recovery_counts
            if classification == "RECOVERED_REPORTED"
        }
    )
    ambiguous_pairs = sorted(
        {
            f"{ticker}|{metric}"
            for ticker, metric, classification in recovery_counts
            if classification == "FOUND_AMBIGUOUS"
        }
    )
    extension_rows = [
        row
        for row in evidence_rows
        if "extension_candidate" in str(row["extraction_method"])
    ]
    accepted_extension_count = sum(
        row["candidate_status"] == "ACCEPTED" for row in extension_rows
    )
    if accepted_extension_count:
        errors.append(
            f"accepted issuer-extension evidence={accepted_extension_count}"
        )
    if clean_recoveries != ["PBI|costs_and_expenses"]:
        errors.append(
            "unexpected clean recovered pair set="
            f"{clean_recoveries}"
        )
    acceptance = "PASS" if not errors else "FAIL"
    payload = {
        "acceptance": acceptance,
        "gate": "TRANSPORTATION_REQUIRED_METRIC_PARSER_EVIDENCE_AUDIT",
        "asof_date": asof_date,
        "stale_period_cutoff": cutoff,
        "run_id": run_id,
        "adapter": ADAPTER,
        "adapter_version": registry.adapter_version,
        "min_confidence": args.min_confidence,
        "evidence_count": len(evidence_rows),
        "candidate_status_counts": dict(sorted(status_counts.items())),
        "promotion_preflight_counts": dict(
            sorted(preflight_counts.items())
        ),
        "conflicting_accepted_evidence_count": len(conflicting_keys),
        "extension_candidate_evidence_count": len(extension_rows),
        "accepted_extension_candidate_count": accepted_extension_count,
        "clean_recovered_pairs": clean_recoveries,
        "ambiguous_pairs": ambiguous_pairs,
        "ambiguous_pair_count": len(ambiguous_pairs),
        "extension_candidate_group_count": len(candidate_rows),
        "source_document_open_count": 0,
        "arelle_invocation_count": 0,
        "edgartools_invocation_count": 0,
        "ocr_invocation_count": 0,
        "network_requests": 0,
        "artifacts": {
            "evidence_audit": {
                "path": str(evidence_path.resolve()),
                "sha256": file_sha256(evidence_path),
            },
            "extension_candidate_audit": {
                "path": str(candidate_path.resolve()),
                "sha256": file_sha256(candidate_path),
            },
        },
        "sealed_inputs": {
            "parser_execution": {
                "path": str(execution_path.resolve()),
                "sha256": file_sha256(execution_path),
            },
            "source_manifest": {
                "path": str(source_path.resolve()),
                "sha256": file_sha256(source_path),
            },
            "recovery_assessment": {
                "path": str(recovery_path.resolve()),
                "sha256": file_sha256(recovery_path),
            },
        },
        "errors": errors,
        "next_gate": (
            "PROMOTE_CLEAN_STANDARD_RECOVERY_AND_REBUILD_ONCE"
            if not errors
            else "REPAIR_EVIDENCE_AUDIT"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
