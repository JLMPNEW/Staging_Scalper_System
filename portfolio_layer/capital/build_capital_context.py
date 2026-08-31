#!/usr/bin/env python3
"""Build one immutable, report-only Portfolio capital-context artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.capital.context import (  # noqa: E402
    build_capital_context,
    write_capital_context_immutable,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an immutable Portfolio capital-context v1 artifact."
    )
    parser.add_argument("--account-aum-usd", required=True)
    parser.add_argument("--active-sector-count", required=True, type=int)
    parser.add_argument("--sector-cap-fraction", required=True)
    parser.add_argument("--asof-date", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = build_capital_context(
            account_aum_usd=args.account_aum_usd,
            active_sector_count=args.active_sector_count,
            sector_cap_fraction=args.sector_cap_fraction,
            asof_date=args.asof_date,
            source_id=args.source_id,
            source_sha256=args.source_sha256,
        )
        output = write_capital_context_immutable(args.output, payload)
    except (OSError, ValueError) as exc:
        print(f"capital-context build failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "acceptance": "PASS",
                "artifact_role": payload["artifact_role"],
                "output": str(output.resolve()),
                "payload_sha256": payload["payload_sha256"],
                "sector_cap_notional_usd": payload["sector_cap_notional_usd"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
