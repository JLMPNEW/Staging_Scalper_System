#!/usr/bin/env python3
"""Evaluate calendar-bound Consumer future outcomes after they mature."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from consumer_defensive.core.legacy_evidence_routes import (  # noqa: E402
    block_legacy_route,
)
from consumer_defensive.core.future_oos_protocol_v2 import evaluate  # noqa: E402
from future_only_evidence.protocol import immutable_write_json  # noqa: E402


def _pairs(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        role, separator, item = value.partition("=")
        if not separator or not role or not item or role in result:
            raise ValueError(f"expected unique role=value, received {value!r}")
        result[role] = item
    return result


def _verifier(value: str) -> Callable[..., bool]:
    module_name, separator, attribute = value.partition(":")
    if not separator:
        raise ValueError("verifier must be module:function")
    candidate = getattr(importlib.import_module(module_name), attribute)
    if not callable(candidate):
        raise ValueError("verifier target is not callable")
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-source", action="append", default=[], required=True)
    parser.add_argument("--registration-receipt", type=Path, required=True)
    parser.add_argument("--registration-receipt-sha256", required=True)
    parser.add_argument("--registration-verifier", required=True)
    parser.add_argument("--capture", type=Path, action="append", required=True)
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--trading-calendar", type=Path, required=True)
    parser.add_argument(
        "--evaluated-at-utc",
        default=datetime.now(timezone.utc).isoformat(),
    )
    parser.add_argument("--output", type=Path, required=True)
    if any(value in {"-h", "--help"} for value in sys.argv[1:]):
        parser.parse_args()
    block_legacy_route("26d_evaluate_consumer_defensive_future_oos")
    args = parser.parse_args()
    payload = evaluate(
        plan_path=args.plan,
        plan_source_paths={role: Path(path) for role, path in _pairs(args.plan_source).items()},
        registration_receipt_path=args.registration_receipt,
        expected_registration_receipt_sha256=args.registration_receipt_sha256,
        registration_receipt_verifier=_verifier(args.registration_verifier),
        capture_paths=args.capture,
        outcome_path=args.outcomes,
        trading_calendar_path=args.trading_calendar,
        evaluation_at_utc=args.evaluated_at_utc,
    )
    immutable_write_json(args.output, payload)
    print(json.dumps({"output": str(args.output.resolve()), **payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
