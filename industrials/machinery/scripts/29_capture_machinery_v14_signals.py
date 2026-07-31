#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.machinery.confirmatory_v14 import (  # noqa: E402
    DEFAULT_PROTOCOL_PATH,
    DEFAULT_V14_ROOT,
    capture_forward_signals,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture an immutable machinery v1.4 signal snapshot without "
            "reading or writing forward outcomes."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--rank-table", type=Path, default=None)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_V14_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rank_table = (
        args.rank_table.expanduser().resolve()
        if args.rank_table
        else (
            PROJECT_ROOT
            / "output"
            / "industrials"
            / "machinery"
            / "dashboard"
            / args.asof
            / "machinery_final_rank_table.csv"
        )
    )
    result = capture_forward_signals(
        asof=args.asof,
        rank_table=rank_table,
        protocol_path=args.protocol.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
