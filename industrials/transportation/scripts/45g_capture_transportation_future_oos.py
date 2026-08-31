#!/usr/bin/env python3
"""Capture one calendar-bound canonical v8 Transportation signal."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from future_only_evidence.protocol import immutable_write_json  # noqa: E402
from industrials.transportation.legacy_production_routes import (  # noqa: E402
    block_legacy_route,
)
from industrials.transportation.future_oos_capture_v4 import capture_signal  # noqa: E402


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
    block_legacy_route("45g_capture_transportation_future_oos")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--source", action="append", default=[], required=True)
    parser.add_argument("--source-sha256", action="append", default=[], required=True)
    parser.add_argument("--capture-receipt", type=Path, required=True)
    parser.add_argument("--capture-receipt-sha256", required=True)
    parser.add_argument("--capture-verifier", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = capture_signal(
        asof_date=args.asof,
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
