#!/usr/bin/env python3
"""Preflight, capture, or evaluate governing v7 Transportation evidence."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from future_only_evidence.protocol import immutable_write_json  # noqa: E402
from industrials.transportation.legacy_production_routes import (  # noqa: E402
    block_legacy_route,
)
from industrials.transportation.future_oos_protocol_v1 import (  # noqa: E402
    build_preflight,
    capture_signal,
    evaluate,
)


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
    preflight.add_argument("--date", required=True)
    preflight.add_argument("--score", type=Path, required=True)
    preflight.add_argument("--rank", type=Path, required=True)
    preflight.add_argument("--source-manifest", type=Path)
    preflight.add_argument("--output", type=Path, required=True)

    capture = subparsers.add_parser("capture")
    capture.add_argument("--asof", required=True)
    capture.add_argument("--source", action="append", default=[], required=True)
    capture.add_argument("--source-sha256", action="append", default=[], required=True)
    capture.add_argument("--capture-receipt", type=Path, required=True)
    capture.add_argument("--capture-receipt-sha256", required=True)
    capture.add_argument("--capture-verifier", required=True)
    capture.add_argument("--output", type=Path, required=True)

    evaluation = subparsers.add_parser("evaluate")
    evaluation.add_argument("--capture", type=Path, action="append", required=True)
    evaluation.add_argument("--outcomes", type=Path, required=True)
    evaluation.add_argument(
        "--evaluated-at-utc",
        default=datetime.now(timezone.utc).isoformat(),
    )
    evaluation.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    block_legacy_route("45_run_transportation_future_oos_protocol")
    args = _arguments()
    if args.command == "preflight":
        payload = build_preflight(
            preflight_date=args.date,
            score_path=args.score,
            rank_path=args.rank,
            source_manifest_path=args.source_manifest,
        )
    elif args.command == "capture":
        payload = capture_signal(
            asof_date=args.asof,
            capture_source_paths={role: Path(path) for role, path in _pairs(args.source).items()},
            expected_capture_source_sha256=_pairs(args.source_sha256),
            trusted_capture_receipt_path=args.capture_receipt,
            expected_trusted_capture_receipt_sha256=args.capture_receipt_sha256,
            trusted_capture_receipt_verifier=_verifier(args.capture_verifier),
        )
    else:
        payload = evaluate(
            capture_paths=args.capture,
            outcome_path=args.outcomes,
            evaluation_at_utc=args.evaluated_at_utc,
        )
    immutable_write_json(args.output, payload)
    print(json.dumps({"output": str(args.output.resolve()), **payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
