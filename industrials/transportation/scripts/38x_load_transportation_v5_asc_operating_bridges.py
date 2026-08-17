#!/usr/bin/env python3
"""Load reviewed ASC annual operating-income bridges after dual reconciliation."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.reviewed_operand_repair import (  # noqa: E402
    ResolvedFact,
    persist_policy,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, resolve_foundation  # noqa: E402


POLICY_VERSION = "transportation_asc_operating_bridge_v1"
SOURCE_ID = "transportation_asc_operating_bridge_v1"
MODEL_FAMILY = "transportation"
DEFAULT_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "review_policies" / "transportation_asc_operating_bridge_v1.json"
DEFAULT_VALIDATION = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "asc_operating_bridge" / "2026-08-15" / "transportation_v5_asc_operating_bridge_validation.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "asc_operating_bridge_load"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    policy_path = args.policy.expanduser().resolve()
    validation_path = args.validation.expanduser().resolve()
    policy = read_json(policy_path)
    validation = read_json(validation_path)
    if policy.get("policy_version") != POLICY_VERSION or policy.get("review_status") != "ACCEPTED":
        raise ValueError("ASC operating bridge policy is not accepted")
    if policy.get("source_id") != SOURCE_ID or policy.get("model_family") != MODEL_FAMILY:
        raise ValueError("ASC operating bridge policy identity changed")
    if asof != str(policy.get("asof_date") or "")[:10]:
        raise ValueError("operator as-of does not match reviewed policy")
    controls = dict(policy.get("controls") or {})
    if controls.get("network_allowed") is not False or controls.get("reparse_allowed") is not False:
        raise ValueError("ASC bridge must remain network-free and no-reparse")
    if (
        validation.get("acceptance") != "PASS"
        or validation.get("review_status") != "ACCEPTED"
        or validation.get("contract_version") != policy.get("validation_contract")
        or validation.get("fy2025_exact_reconciliation") is not True
    ):
        raise ValueError("ASC operating bridge validation is not accepted")
    expected_periods = set(str(value) for value in policy["accepted_periods"])
    bridges = {str(row["report_date"]): dict(row) for row in validation["bridges"]}
    if expected_periods != set(bridges) - {"2025-12-31"}:
        raise ValueError("ASC reviewed bridge period set changed")
    known = dict(policy["known_value_crosscheck"])
    if not math.isclose(
        float(bridges[str(known["period_end"])]["operating_income"]),
        float(known["operating_income"]),
        rel_tol=0.0,
        abs_tol=0.5,
    ):
        raise ValueError("ASC known-value bridge cross-check changed")
    facts: list[ResolvedFact] = []
    for period_end in sorted(expected_periods):
        bridge = bridges[period_end]
        document_path = Path(str(bridge["document_path"])).resolve()
        document_path.relative_to(PROJECT_ROOT.resolve())
        if file_sha256(document_path) != str(bridge["document_sha256"]):
            raise ValueError(f"ASC source document hash changed={period_end}")
        value = float(bridge["operating_income"])
        components = list(bridge["components"])
        cross = list(bridge["cross_check_components"])
        if not math.isclose(sum(float(row["signed_value"]) for row in components), value, abs_tol=0.5):
            raise ValueError(f"ASC operating component sum changed={period_end}")
        if not math.isclose(sum(float(row["signed_value"]) for row in cross), value, abs_tol=0.5):
            raise ValueError(f"ASC independent cross-check changed={period_end}")
        year = int(period_end[:4])
        facts.append(ResolvedFact(
            repair_id=f"ASC_OPERATING_INCOME_FY{year}_V5",
            ticker="ASC",
            cik="0001577437",
            accession_number=str(bridge["accession_number"]),
            form_type=str(bridge["form_type"]),
            filing_date=str(bridge["filing_date"])[:10],
            accepted_at=str(bridge["accepted_at"]),
            fiscal_year=year,
            fiscal_period="FY",
            period_start=f"{year}-01-01",
            period_end=period_end,
            canonical_metric="operating_income",
            financial_statement="income_statement",
            period_type="duration",
            unit="USD",
            value=value,
            taxonomy="transportation-reviewed",
            concept_name="ReviewedOperatingIncomeComponentBridge",
            derivation_type="document_reviewed_formula",
            rationale="Reviewed ASC annual operating rows before interest; independently reconciled from pretax and financing rows.",
            provenance={
                "source_document": str(document_path),
                "content_sha256": str(bridge["document_sha256"]),
                "formula_components": components,
                "cross_check_components": cross,
                "validation_contract": validation["contract_version"],
            },
        ))
    foundation = resolve_foundation(args.config.expanduser().resolve(), args.db)
    persistence: dict[str, object] = {}
    mode = "plan_only"
    if args.execute:
        mode = "execute"
        with connect_database(foundation.db_path, timeout_seconds=foundation.timeout_sec) as connection:
            persistence = persist_policy(
                connection,
                facts=facts,
                overrides=[],
                policy_path=policy_path,
                source_priority=int(policy["source_priority"]),
                source_id=SOURCE_ID,
                policy_version=POLICY_VERSION,
                model_family=MODEL_FAMILY,
            )
    payload = {
        "acceptance": "PASS",
        "mode": mode,
        "asof_date": asof,
        "policy_version": POLICY_VERSION,
        "policy_path": str(policy_path),
        "policy_sha256": file_sha256(policy_path),
        "validation_path": str(validation_path),
        "validation_sha256": file_sha256(validation_path),
        "fact_count": len(facts),
        "periods": [{"period_end": fact.period_end, "value": fact.value, "filing_date": fact.filing_date} for fact in facts],
        "persistence": persistence,
        "network_requests": 0,
        "parser_invocations": 0,
    }
    output_dir = args.output_root.expanduser().resolve() / asof
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "transportation_v5_asc_operating_bridge_load.json"
    write_text_atomic(output_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"acceptance": "PASS", "mode": mode, "fact_count": len(facts), "persistence": persistence, "output": str(output_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
