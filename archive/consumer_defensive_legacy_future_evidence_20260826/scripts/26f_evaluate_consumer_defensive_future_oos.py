#!/usr/bin/env python3
"""Evaluate canonical Consumer v5 prospective evidence without activating production."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from consumer_defensive.core.future_oos_protocol_v5 import evaluate_v5  # noqa: E402
from future_only_evidence.protocol import immutable_write_json  # noqa: E402


def _pairs(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        role, separator, item = value.partition("=")
        if not separator or not role or not item or role in result:
            raise ValueError(f"expected unique role=value, received {value!r}")
        result[role] = item
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-source", action="append", required=True)
    parser.add_argument("--registration-receipt", type=Path, required=True)
    parser.add_argument("--registration-receipt-sha256", required=True)
    parser.add_argument("--registration-timestamp-receipt", type=Path, required=True)
    parser.add_argument("--registration-timestamp-receipt-sha256", required=True)
    parser.add_argument("--evidence-public-key", type=Path, required=True)
    parser.add_argument("--timestamp-public-key", type=Path, required=True)
    parser.add_argument("--market-data-public-key", type=Path, required=True)
    parser.add_argument("--capture", type=Path, action="append", required=True)
    parser.add_argument("--capture-registry", type=Path, required=True)
    parser.add_argument("--capture-registry-receipt", type=Path, required=True)
    parser.add_argument("--capture-registry-receipt-sha256", required=True)
    parser.add_argument("--capture-registry-timestamp-receipt", type=Path, required=True)
    parser.add_argument("--capture-registry-timestamp-receipt-sha256", required=True)
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--outcome-source", action="append", required=True)
    parser.add_argument("--outcome-receipt", type=Path, required=True)
    parser.add_argument("--outcome-receipt-sha256", required=True)
    parser.add_argument("--outcome-timestamp-receipt", type=Path, required=True)
    parser.add_argument("--outcome-timestamp-receipt-sha256", required=True)
    parser.add_argument("--market-export-receipt", type=Path, required=True)
    parser.add_argument("--market-export-receipt-sha256", required=True)
    parser.add_argument("--trading-calendar", type=Path, required=True)
    parser.add_argument(
        "--evaluated-at-utc",
        default=datetime.now(timezone.utc).isoformat(),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = evaluate_v5(
        plan_path=args.plan,
        plan_source_paths={
            role: Path(path) for role, path in _pairs(args.plan_source).items()
        },
        registration_receipt_path=args.registration_receipt,
        expected_registration_receipt_sha256=args.registration_receipt_sha256,
        registration_timestamp_receipt_path=args.registration_timestamp_receipt,
        expected_registration_timestamp_receipt_sha256=(
            args.registration_timestamp_receipt_sha256
        ),
        evidence_public_key_path=args.evidence_public_key,
        timestamp_public_key_path=args.timestamp_public_key,
        market_data_public_key_path=args.market_data_public_key,
        capture_paths=args.capture,
        capture_registry_path=args.capture_registry,
        capture_registry_receipt_path=args.capture_registry_receipt,
        expected_capture_registry_receipt_sha256=(
            args.capture_registry_receipt_sha256
        ),
        capture_registry_timestamp_receipt_path=(
            args.capture_registry_timestamp_receipt
        ),
        expected_capture_registry_timestamp_receipt_sha256=(
            args.capture_registry_timestamp_receipt_sha256
        ),
        outcome_path=args.outcomes,
        outcome_source_paths={
            role: Path(path) for role, path in _pairs(args.outcome_source).items()
        },
        outcome_receipt_path=args.outcome_receipt,
        expected_outcome_receipt_sha256=args.outcome_receipt_sha256,
        outcome_timestamp_receipt_path=args.outcome_timestamp_receipt,
        expected_outcome_timestamp_receipt_sha256=(
            args.outcome_timestamp_receipt_sha256
        ),
        market_export_receipt_path=args.market_export_receipt,
        expected_market_export_receipt_sha256=args.market_export_receipt_sha256,
        trading_calendar_path=args.trading_calendar,
        evaluated_at_utc=args.evaluated_at_utc,
    )
    immutable_write_json(args.output, payload)
    print(json.dumps({"output": str(args.output.resolve()), **payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
