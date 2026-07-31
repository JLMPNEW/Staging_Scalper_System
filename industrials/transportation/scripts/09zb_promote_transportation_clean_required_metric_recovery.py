#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
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
    promote_run,
)
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    resolve_foundation,
)


ADAPTER = (
    "industrials.transportation.required_metric_parser_adapter:"
    "extract_metric_evidence"
)
SOURCE_ID = "dedicated_parser_transportation_required_metric_repair_v1"
PROMOTION_METRIC = "costs_and_expenses"
EXPECTED_TICKER = "PBI"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "required_metric_repair"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Promote only the clean standards-based PBI cost recovery from "
            "the sealed transportation required-metric parser run."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--min-confidence", type=float, default=0.90)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser.parse_args()


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def main() -> int:
    args = parse_args()
    asof_date = str(args.asof)[:10]
    output_dir = args.output_root.expanduser().resolve() / asof_date
    audit_path = (
        output_dir
        / "transportation_required_metric_parser_evidence_audit.json"
    )
    if not audit_path.is_file():
        raise FileNotFoundError("Run 09za evidence audit first")
    audit = _json(audit_path)
    errors: list[str] = []
    if audit.get("acceptance") != "PASS":
        errors.append("09za evidence audit is not PASS")
    if audit.get("clean_recovered_pairs") != [
        f"{EXPECTED_TICKER}|{PROMOTION_METRIC}"
    ]:
        errors.append("sealed clean-recovery set changed")
    if int(audit.get("accepted_extension_candidate_count") or 0):
        errors.append("09za audit contains accepted extension evidence")
    for contract in (audit.get("artifacts") or {}).values():
        path = Path(str(contract.get("path") or ""))
        if (
            not path.is_file()
            or file_sha256(path) != str(contract.get("sha256") or "")
        ):
            errors.append(f"09za artifact missing or changed={path}")

    foundation = resolve_foundation(
        args.config.expanduser().resolve(),
        args.db,
    )
    full_registry = load_registry(ADAPTER)
    mapping = full_registry.production_mappings.get(PROMOTION_METRIC)
    if mapping is None:
        errors.append(f"adapter has no mapping for {PROMOTION_METRIC}")
        scoped_registry = full_registry
    else:
        scoped_registry = replace(
            full_registry,
            production_mappings={PROMOTION_METRIC: mapping},
        )
    run_id = int(audit.get("run_id") or 0)
    promotable_keys: list[str] = []
    promotable_tickers: set[str] = set()
    conflicting_keys: set[str] = set()
    evidence_count = 0
    if not errors:
        with connect_database(
            foundation.db_path,
            timeout_seconds=foundation.timeout_sec,
            readonly=True,
        ) as connection:
            run = connection.execute(
                "SELECT * FROM sec_parser_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or str(run["status"]) != "COMPLETED"
                or int(run["failed_work_count"] or 0) != 0
            ):
                errors.append("source parser run is not completed/zero-failure")
            rows = connection.execute(
                """
                SELECT evidence.*
                FROM sec_parser_run_metric_evidence AS relation
                JOIN sec_parser_metric_evidence_shadow AS evidence
                  ON evidence.evidence_key=relation.evidence_key
                WHERE relation.run_id=?
                ORDER BY evidence.ticker, evidence.metric_name,
                         evidence.period_end, evidence.evidence_key
                """,
                (run_id,),
            ).fetchall()
            evidence_count = len(rows)
            conflicting_keys = _conflicting_evidence_keys(
                rows,
                registry=scoped_registry,
                asof_date=asof_date,
                min_confidence=args.min_confidence,
            )
            for row in rows:
                reason = _promotion_block_reason(
                    row,
                    registry=scoped_registry,
                    asof_date=asof_date,
                    min_confidence=args.min_confidence,
                    conflicting_keys=conflicting_keys,
                )
                if reason:
                    continue
                promotable_keys.append(str(row["evidence_key"]))
                promotable_tickers.add(str(row["ticker"]).upper())
                if str(row["metric_name"]) != PROMOTION_METRIC:
                    errors.append("preflight admitted a metric outside scope")
                if "extension_candidate" in str(
                    row["extraction_method"] or ""
                ):
                    errors.append("preflight admitted issuer-extension evidence")
    if not promotable_keys:
        errors.append("no clean standards-based PBI cost evidence is promotable")
    if promotable_tickers != {EXPECTED_TICKER}:
        errors.append(
            f"promotable ticker set changed={sorted(promotable_tickers)}"
        )

    promotion: dict[str, Any] = {}
    if args.execute and not errors:
        with connect_database(
            foundation.db_path,
            timeout_seconds=foundation.timeout_sec,
        ) as connection:
            promotion = promote_run(
                connection,
                run_id=run_id,
                registry=scoped_registry,
                source_id=SOURCE_ID,
                min_confidence=args.min_confidence,
            )
            promotion_id = int(promotion["promotion_id"])
            promoted = connection.execute(
                """
                SELECT evidence.ticker, evidence.metric_name,
                       evidence.extraction_method, production.evidence_key
                FROM sec_parser_production_evidence AS production
                JOIN sec_parser_metric_evidence_shadow AS evidence
                  ON evidence.evidence_key=production.evidence_key
                WHERE production.promotion_id=?
                  AND production.action='PROMOTED'
                ORDER BY production.evidence_key
                """,
                (promotion_id,),
            ).fetchall()
            if len(promoted) != len(promotable_keys):
                errors.append("executed promoted count differs from preflight")
            if any(
                str(row["ticker"]).upper() != EXPECTED_TICKER
                or str(row["metric_name"]) != PROMOTION_METRIC
                or "extension_candidate"
                in str(row["extraction_method"] or "")
                for row in promoted
            ):
                errors.append("executed promotion escaped sealed clean scope")

    acceptance = "PASS" if not errors else "FAIL"
    payload = {
        "acceptance": acceptance,
        "gate": "TRANSPORTATION_CLEAN_REQUIRED_METRIC_PROMOTION",
        "mode": "execute" if args.execute else "plan_only",
        "asof_date": asof_date,
        "run_id": run_id,
        "source_id": SOURCE_ID,
        "promotion_scope": {
            "ticker": EXPECTED_TICKER,
            "metric_name": PROMOTION_METRIC,
            "standards_based_only": True,
        },
        "source_evidence_count": evidence_count,
        "promotable_evidence_count": len(promotable_keys),
        "scoped_conflicting_evidence_count": len(conflicting_keys),
        "promotable_tickers": sorted(promotable_tickers),
        "promotion": promotion,
        "parser_invocations": 0,
        "document_open_count": 0,
        "network_requests": 0,
        "feature_build_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_layer_invocations": 0,
        "extension_promotion_authorized": False,
        "sealed_evidence_audit": {
            "path": str(audit_path.resolve()),
            "sha256": file_sha256(audit_path),
        },
        "errors": errors,
        "next_gate": (
            "REVIEW_EXTENSION_CAPEX_WITHOUT_REPARSING"
            if not errors
            else "REPAIR_CLEAN_PROMOTION"
        ),
    }
    output_path = (
        output_dir
        / "transportation_required_metric_clean_promotion.json"
    )
    write_text_atomic(
        output_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
