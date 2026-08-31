#!/usr/bin/env python3
"""Validate a Portfolio capital-context artifact against an explicit SHA pin."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.capital.context import load_capital_context  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a Portfolio capital-context v1 artifact."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = load_capital_context(
            args.input,
            expected_payload_sha256=args.expected_sha256,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"capital-context validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "acceptance": "PASS",
                "artifact_role": payload["artifact_role"],
                "input": str(args.input.resolve()),
                "payload_sha256": payload["payload_sha256"],
                "portfolio_write_performed": payload["portfolio_write_performed"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
