#!/usr/bin/env python3
"""Capture one registered, fresh, calendar-bound Consumer signal."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from consumer_defensive.core.legacy_evidence_routes import (  # noqa: E402
    block_legacy_route,
)
from consumer_defensive.core.future_oos_capture_v2 import capture_signal  # noqa: E402
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


def _signals(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("signal JSON must be an array or {rows:[...]}")
    return [dict(row) for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--plan-source", action="append", default=[], required=True)
    parser.add_argument("--source", action="append", default=[], required=True)
    parser.add_argument("--source-sha256", action="append", default=[], required=True)
    parser.add_argument("--registration-receipt", type=Path, required=True)
    parser.add_argument("--registration-receipt-sha256", required=True)
    parser.add_argument("--registration-verifier", required=True)
    parser.add_argument("--capture-receipt", type=Path, required=True)
    parser.add_argument("--capture-receipt-sha256", required=True)
    parser.add_argument("--capture-verifier", required=True)
    parser.add_argument("--output", type=Path, required=True)
    if any(value in {"-h", "--help"} for value in sys.argv[1:]):
        parser.parse_args()
    block_legacy_route("26c_capture_consumer_defensive_future_oos")
    args = parser.parse_args()
    payload = capture_signal(
        plan_path=args.plan,
        plan_source_paths={role: Path(path) for role, path in _pairs(args.plan_source).items()},
        registration_receipt_path=args.registration_receipt,
        expected_registration_receipt_sha256=args.registration_receipt_sha256,
        registration_receipt_verifier=_verifier(args.registration_verifier),
        asof_date=args.asof,
        signal_rows=_signals(args.signals),
        capture_source_paths={role: Path(path) for role, path in _pairs(args.source).items()},
        expected_capture_source_sha256=_pairs(args.source_sha256),
        trusted_capture_receipt_path=args.capture_receipt,
        expected_trusted_capture_receipt_sha256=args.capture_receipt_sha256,
        trusted_capture_receipt_verifier=_verifier(args.capture_verifier),
    )
    immutable_write_json(args.output, payload)
    print(json.dumps({"output": str(args.output.resolve()), **payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
