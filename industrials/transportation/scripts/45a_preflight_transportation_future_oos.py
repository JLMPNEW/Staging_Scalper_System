#!/usr/bin/env python3
"""Materialize a zero-cap governing-v7 Transportation future preflight."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from future_only_evidence.protocol import immutable_write_json  # noqa: E402
from industrials.transportation.legacy_production_routes import (  # noqa: E402
    block_legacy_route,
)
from industrials.transportation.future_oos_preflight_v1 import (  # noqa: E402
    build_operational_preflight,
)


def main() -> int:
    block_legacy_route("45a_preflight_transportation_future_oos")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--score", type=Path, required=True)
    parser.add_argument("--rank", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_operational_preflight(
        preflight_date=args.date,
        score_path=args.score,
        rank_path=args.rank,
        source_manifest_path=args.source_manifest,
    )
    immutable_write_json(args.output, payload)
    print(json.dumps({"output": str(args.output.resolve()), **payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
