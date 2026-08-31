"""CLI for Transportation prospective operational preflight artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from industrials.transportation.future_oos_preflight_v3 import (
    build_operational_preflight as transportation_preflight,
)

from .protocol import immutable_write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--family",
        required=True,
        choices=("transportation",),
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--score", type=Path)
    parser.add_argument("--rank", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.score is None or args.rank is None:
        parser.error("--score and --rank are required for transportation")
    payload = transportation_preflight(
        preflight_date=args.asof,
        score_path=args.score,
        rank_path=args.rank,
        source_manifest_path=args.source_manifest,
    )
    immutable_write_json(args.output, payload)
    print(
        json.dumps(
            {"output": str(args.output.resolve()), **payload},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
