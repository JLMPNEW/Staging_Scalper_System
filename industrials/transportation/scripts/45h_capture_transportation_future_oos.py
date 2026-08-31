#!/usr/bin/env python3
"""Capture one canonical Transportation v6 prospective signal snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from future_only_evidence.protocol import immutable_write_json  # noqa: E402
from industrials.transportation.future_oos_capture_v6 import (  # noqa: E402
    capture_signal_v6,
)


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
    parser.add_argument("--activation-plan", type=Path, required=True)
    parser.add_argument("--plan-source", action="append", required=True)
    parser.add_argument("--activation-receipt", type=Path, required=True)
    parser.add_argument("--activation-receipt-sha256", required=True)
    parser.add_argument("--activation-timestamp-receipt", type=Path, required=True)
    parser.add_argument("--activation-timestamp-receipt-sha256", required=True)
    parser.add_argument("--evidence-public-key", type=Path, required=True)
    parser.add_argument("--timestamp-public-key", type=Path, required=True)
    parser.add_argument("--market-data-public-key", type=Path, required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--source-sha256", action="append", required=True)
    parser.add_argument("--capture-receipt", type=Path, required=True)
    parser.add_argument("--capture-receipt-sha256", required=True)
    parser.add_argument("--capture-timestamp-receipt", type=Path, required=True)
    parser.add_argument("--capture-timestamp-receipt-sha256", required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--previous-capture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = capture_signal_v6(
        activation_plan_path=args.activation_plan,
        plan_source_paths={
            role: Path(path) for role, path in _pairs(args.plan_source).items()
        },
        activation_receipt_path=args.activation_receipt,
        expected_activation_receipt_sha256=args.activation_receipt_sha256,
        activation_timestamp_receipt_path=args.activation_timestamp_receipt,
        expected_activation_timestamp_receipt_sha256=(
            args.activation_timestamp_receipt_sha256
        ),
        evidence_public_key_path=args.evidence_public_key,
        timestamp_public_key_path=args.timestamp_public_key,
        market_data_public_key_path=args.market_data_public_key,
        asof_date=args.asof,
        capture_source_paths={
            role: Path(path) for role, path in _pairs(args.source).items()
        },
        expected_capture_source_sha256=_pairs(args.source_sha256),
        capture_receipt_path=args.capture_receipt,
        expected_capture_receipt_sha256=args.capture_receipt_sha256,
        capture_timestamp_receipt_path=args.capture_timestamp_receipt,
        expected_capture_timestamp_receipt_sha256=(
            args.capture_timestamp_receipt_sha256
        ),
        archive_root=args.archive_root,
        previous_capture_path=args.previous_capture,
    )
    immutable_write_json(args.output, payload)
    print(json.dumps({"output": str(args.output.resolve()), **payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
