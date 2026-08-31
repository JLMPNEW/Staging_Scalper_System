#!/usr/bin/env python3
"""Preflight, capture, or evaluate Consumer prospective-only evidence."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from consumer_defensive.core.legacy_evidence_routes import (  # noqa: E402
    block_legacy_route,
)
from consumer_defensive.core.future_oos_protocol_v1 import (  # noqa: E402
    build_preflight,
    capture_signal,
    evaluate,
)
from future_only_evidence.protocol import immutable_write_json  # noqa: E402


def _pairs(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item or key in result:
            raise ValueError(f"expected unique role=value, received {value!r}")
        result[key] = item
    return result


def _verifier(value: str | None) -> Callable[..., bool] | None:
    if not value:
        return None
    module_name, separator, attribute = value.partition(":")
    if not separator:
        raise ValueError("verifier must be module:function")
    candidate = getattr(importlib.import_module(module_name), attribute)
    if not callable(candidate):
        raise ValueError("verifier target is not callable")
    return candidate


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--plan", type=Path, required=True)
    preflight.add_argument("--asof", required=True)
    preflight.add_argument("--output", type=Path, required=True)
    preflight.add_argument("--plan-source", action="append", default=[])
    preflight.add_argument("--registration-receipt", type=Path)
    preflight.add_argument("--registration-receipt-sha256", default="")
    preflight.add_argument("--registration-verifier")

    capture = subparsers.add_parser("capture")
    capture.add_argument("--plan", type=Path, required=True)
    capture.add_argument("--asof", required=True)
    capture.add_argument("--signal-json", type=Path, required=True)
    capture.add_argument("--plan-source", action="append", default=[], required=True)
    capture.add_argument("--capture-source", action="append", default=[], required=True)
    capture.add_argument("--capture-source-sha256", action="append", default=[], required=True)
    capture.add_argument("--registration-receipt", type=Path, required=True)
    capture.add_argument("--registration-receipt-sha256", required=True)
    capture.add_argument("--registration-verifier", required=True)
    capture.add_argument("--capture-receipt", type=Path, required=True)
    capture.add_argument("--capture-receipt-sha256", required=True)
    capture.add_argument("--capture-verifier", required=True)
    capture.add_argument("--output", type=Path, required=True)

    evaluation = subparsers.add_parser("evaluate")
    evaluation.add_argument("--plan", type=Path, required=True)
    evaluation.add_argument("--plan-source", action="append", default=[], required=True)
    evaluation.add_argument("--registration-receipt", type=Path, required=True)
    evaluation.add_argument("--registration-receipt-sha256", required=True)
    evaluation.add_argument("--registration-verifier", required=True)
    evaluation.add_argument("--capture", type=Path, action="append", required=True)
    evaluation.add_argument("--outcomes", type=Path, required=True)
    evaluation.add_argument(
        "--evaluated-at-utc",
        default=datetime.now(timezone.utc).isoformat(),
    )
    evaluation.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _signal_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("signal JSON must be an array of objects or {rows:[...]}")
    return [dict(row) for row in rows]


def main() -> int:
    # Preserve a usable CLI inventory while ensuring every executable action
    # fails before parsing inputs or touching evidence.  argparse exits after
    # rendering help, so this branch cannot reach any legacy implementation.
    if any(value in {"-h", "--help"} for value in sys.argv[1:]):
        _arguments()
    block_legacy_route("26_run_consumer_defensive_future_oos_protocol")
    args = _arguments()
    if args.command == "preflight":
        pairs = _pairs(args.plan_source)
        payload = build_preflight(
            plan_path=args.plan,
            asof_date=args.asof,
            source_paths={role: Path(path) for role, path in pairs.items()} if pairs else None,
            registration_receipt_path=args.registration_receipt,
            expected_registration_receipt_sha256=args.registration_receipt_sha256,
            registration_receipt_verifier=_verifier(args.registration_verifier),
        )
    elif args.command == "capture":
        payload = capture_signal(
            plan_path=args.plan,
            plan_source_paths={role: Path(path) for role, path in _pairs(args.plan_source).items()},
            registration_receipt_path=args.registration_receipt,
            expected_registration_receipt_sha256=args.registration_receipt_sha256,
            registration_receipt_verifier=_verifier(args.registration_verifier),
            asof_date=args.asof,
            signal_rows=_signal_rows(args.signal_json),
            capture_source_paths={role: Path(path) for role, path in _pairs(args.capture_source).items()},
            expected_capture_source_sha256=_pairs(args.capture_source_sha256),
            trusted_capture_receipt_path=args.capture_receipt,
            expected_trusted_capture_receipt_sha256=args.capture_receipt_sha256,
            trusted_capture_receipt_verifier=_verifier(args.capture_verifier),
        )
    else:
        payload = evaluate(
            plan_path=args.plan,
            plan_source_paths={role: Path(path) for role, path in _pairs(args.plan_source).items()},
            registration_receipt_path=args.registration_receipt,
            expected_registration_receipt_sha256=args.registration_receipt_sha256,
            registration_receipt_verifier=_verifier(args.registration_verifier),
            capture_paths=args.capture,
            outcome_path=args.outcomes,
            evaluation_at_utc=args.evaluated_at_utc,
        )
    immutable_write_json(args.output, payload)
    print(json.dumps({"output": str(args.output.resolve()), **payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
