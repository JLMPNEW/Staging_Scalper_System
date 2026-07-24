#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.db import connect  # noqa: E402
from industrials.core.family_universe import validate_identity_contract  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate transportation ticker/CIK identity reconciliation.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = resolve_foundation(args.config, args.db)
    with connect(paths.db_path, timeout_sec=paths.timeout_sec) as conn:
        errors = validate_identity_contract(
            conn,
            model_family=MODEL_FAMILY,
            active_path=paths.active_path,
            delisted_path=paths.delisted_path,
        )
    print(json.dumps({"status": "PASS" if not errors else "FAIL", "errors": errors}, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
