#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.reviewed_operand_repair import (  # noqa: E402
    POLICY_VERSION,
    PROJECT_ROOT as MODULE_PROJECT_ROOT,
    SOURCE_ID,
    load_policy,
    persist_policy,
    resolve_policy,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    resolve_foundation,
)


DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "review_policies"
    / "transportation_required_metric_operand_repairs.json"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "required_metric_repair"
)
AUDIT_FIELDS = (
    "record_type",
    "record_id",
    "ticker",
    "metric_name",
    "period_start",
    "period_end",
    "accession_number",
    "unit",
    "value",
    "status",
    "source_type",
    "rationale",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and load reviewed transportation required-metric "
            "operands from sealed parser evidence and hash-locked cached filings."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser.parse_args()


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {field: row.get(field, "") for field in AUDIT_FIELDS}
            )
    temporary.replace(path)


def _validate_prerequisites(
    *,
    output_dir: Path,
    asof_date: str,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    errors: list[str] = []
    evidence_audit_path = (
        output_dir / "transportation_required_metric_parser_evidence_audit.json"
    )
    clean_promotion_path = (
        output_dir / "transportation_required_metric_clean_promotion.json"
    )
    if not evidence_audit_path.is_file():
        raise FileNotFoundError("Run 09za evidence audit first")
    if not clean_promotion_path.is_file():
        raise FileNotFoundError("Run 09zb clean PBI promotion first")
    evidence_audit = _json(evidence_audit_path)
    clean_promotion = _json(clean_promotion_path)
    if evidence_audit.get("acceptance") != "PASS":
        errors.append("09za evidence audit is not PASS")
    if str(evidence_audit.get("asof_date") or "")[:10] != asof_date:
        errors.append("09za evidence audit as-of mismatch")
    if int(evidence_audit.get("run_id") or 0) != 68:
        errors.append("09za parser run id changed")
    if clean_promotion.get("acceptance") != "PASS":
        errors.append("09zb clean promotion is not PASS")
    if clean_promotion.get("mode") != "execute":
        errors.append("09zb clean promotion was not executed")
    if int((clean_promotion.get("promotion") or {}).get("promoted_count") or 0) <= 0:
        errors.append("09zb clean promotion contains no promoted PBI evidence")
    if int(clean_promotion.get("run_id") or 0) != 68:
        errors.append("09zb parser run id changed")
    return evidence_audit, clean_promotion, errors


def main() -> int:
    args = parse_args()
    asof_date = str(args.asof)[:10]
    policy_path = args.policy.expanduser().resolve()
    output_dir = args.output_root.expanduser().resolve() / asof_date
    output_dir.mkdir(parents=True, exist_ok=True)
    policy = load_policy(policy_path)
    errors: list[str] = []
    if asof_date != str(policy.get("asof_date") or "")[:10]:
        errors.append("operator as-of does not match reviewed policy as-of")
    evidence_audit, clean_promotion, prerequisite_errors = _validate_prerequisites(
        output_dir=output_dir,
        asof_date=asof_date,
    )
    errors.extend(prerequisite_errors)
    foundation = resolve_foundation(
        args.config.expanduser().resolve(),
        args.db,
    )
    facts = []
    overrides = []
    validated_document_count = 0
    if not errors:
        with connect_database(
            foundation.db_path,
            timeout_seconds=foundation.timeout_sec,
            readonly=True,
        ) as connection:
            facts, overrides, validated_document_count = resolve_policy(
                connection,
                policy,
                project_root=MODULE_PROJECT_ROOT,
            )
    persistence: dict[str, Any] = {}
    if args.execute and not errors:
        with connect_database(
            foundation.db_path,
            timeout_seconds=foundation.timeout_sec,
        ) as connection:
            persistence = persist_policy(
                connection,
                facts=facts,
                overrides=overrides,
                policy_path=policy_path,
                source_priority=int(policy["source_priority"]),
            )
    audit_rows: list[dict[str, Any]] = [
        {
            "record_type": "FACT",
            "record_id": fact.repair_id,
            "ticker": fact.ticker,
            "metric_name": fact.canonical_metric,
            "period_start": fact.period_start,
            "period_end": fact.period_end,
            "accession_number": fact.accession_number,
            "unit": fact.unit,
            "value": fact.value,
            "status": "VALIDATED",
            "source_type": fact.derivation_type,
            "rationale": fact.rationale,
        }
        for fact in facts
    ]
    audit_rows.extend(
        {
            "record_type": "AVAILABILITY_OVERRIDE",
            "record_id": override.override_id,
            "ticker": override.ticker,
            "metric_name": override.metric_name,
            "period_start": "",
            "period_end": "",
            "accession_number": "",
            "unit": "",
            "value": "",
            "status": override.availability_status,
            "source_type": "reviewed_structural_policy",
            "rationale": override.rationale,
        }
        for override in overrides
    )
    audit_csv = output_dir / "transportation_reviewed_required_metric_operands.csv"
    _write_csv(audit_csv, audit_rows)
    acceptance = "PASS" if not errors else "FAIL"
    manifest = {
        "acceptance": acceptance,
        "gate": "TRANSPORTATION_REVIEWED_REQUIRED_METRIC_OPERANDS",
        "mode": "execute" if args.execute else "plan_only",
        "asof_date": asof_date,
        "policy_version": POLICY_VERSION,
        "policy": {
            "path": str(policy_path),
            "sha256": file_sha256(policy_path),
        },
        "parser_run_id": int(policy.get("parser_run_id") or 0),
        "source_id": SOURCE_ID,
        "validated_fact_count": len(facts),
        "validated_override_count": len(overrides),
        "validated_cached_document_count": validated_document_count,
        "repaired_tickers": sorted({fact.ticker for fact in facts}),
        "deferred_scope": policy.get("deferred_scope") or [],
        "persistence": persistence,
        "audit_csv": {
            "path": str(audit_csv),
            "sha256": file_sha256(audit_csv),
        },
        "sealed_prerequisites": {
            "evidence_audit_sha256": file_sha256(
                output_dir
                / "transportation_required_metric_parser_evidence_audit.json"
            ),
            "clean_promotion_sha256": file_sha256(
                output_dir
                / "transportation_required_metric_clean_promotion.json"
            ),
            "evidence_audit_run_id": evidence_audit.get("run_id"),
            "clean_promotion_id": (
                clean_promotion.get("promotion") or {}
            ).get("promotion_id"),
        },
        "parser_invocations": 0,
        "network_requests": 0,
        "sec_fetch_invocations": 0,
        "feature_build_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_layer_invocations": 0,
        "errors": errors,
        "next_gate": (
            "ONE_CONSOLIDATED_FINANCIAL_REBUILD"
            if not errors and args.execute
            else "EXECUTE_REVIEWED_OPERAND_LOAD"
            if not errors
            else "REPAIR_REVIEWED_OPERAND_POLICY"
        ),
    }
    manifest_path = (
        output_dir / "transportation_reviewed_required_metric_operands.json"
    )
    write_text_atomic(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
