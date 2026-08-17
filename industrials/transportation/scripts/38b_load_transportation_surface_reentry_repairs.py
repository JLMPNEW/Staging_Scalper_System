#!/usr/bin/env python3
"""Validate and load the bounded HTLD/MRTN surface re-entry fact repairs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.reviewed_operand_repair import (  # noqa: E402
    load_policy,
    persist_policy,
    resolve_policy,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    resolve_foundation,
)


POLICY_VERSION = "transportation_surface_reentry_operand_repairs_v1"
SOURCE_ID = "transportation_surface_reentry_operand_v1"
MODEL_FAMILY = "transportation"
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "review_policies"
    / "transportation_surface_reentry_operand_repairs_v1.json"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v4"
    / "surface_reentry_repairs"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate or load the outcome-blind surface re-entry repairs from "
            "sealed parser evidence and a hash-locked cached filing."
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


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    policy_path = args.policy.expanduser().resolve()
    policy = load_policy(policy_path)
    if asof != str(policy.get("asof_date") or "")[:10]:
        raise ValueError("operator as-of does not match reviewed policy as-of")
    foundation = resolve_foundation(args.config.expanduser().resolve(), args.db)
    with connect_database(
        foundation.db_path,
        timeout_seconds=foundation.timeout_sec,
        readonly=True,
    ) as connection:
        facts, overrides, document_count = resolve_policy(
            connection,
            policy,
            project_root=PROJECT_ROOT,
            policy_version=POLICY_VERSION,
            source_id=SOURCE_ID,
            model_family=MODEL_FAMILY,
            require_availability_overrides=False,
        )
    persistence: dict[str, object] = {}
    mode = "plan_only"
    if args.execute:
        mode = "execute"
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
                source_id=SOURCE_ID,
                policy_version=POLICY_VERSION,
                model_family=MODEL_FAMILY,
            )
    payload = {
        "acceptance": "PASS",
        "asof_date": asof,
        "mode": mode,
        "model_family": MODEL_FAMILY,
        "policy_version": POLICY_VERSION,
        "policy_path": str(policy_path),
        "policy_sha256": file_sha256(policy_path),
        "source_id": SOURCE_ID,
        "validated_document_count": document_count,
        "facts": [
            {
                "repair_id": fact.repair_id,
                "ticker": fact.ticker,
                "metric": fact.canonical_metric,
                "period_start": fact.period_start,
                "period_end": fact.period_end,
                "value": fact.value,
                "unit": fact.unit,
                "derivation_type": fact.derivation_type,
            }
            for fact in facts
        ],
        "persistence": persistence,
        "network_used": False,
        "parser_run_executed": False,
    }
    output_dir = args.output_root.expanduser().resolve() / asof
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "transportation_surface_reentry_repairs.json"
    write_text_atomic(output_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        f"PASS: validated {len(facts)} surface re-entry fact repairs; "
        f"mode={mode}; output={output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
