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
    DEFAULT_V13_ROOT,
    DEFAULT_V14_ROOT,
    run_v13_defect_audit,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the non-tuning, defect-only machinery v1.3 audit."
    )
    parser.add_argument("--v13-root", type=Path, default=DEFAULT_V13_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_V14_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_v13_defect_audit(
        source_root=args.v13_root.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
