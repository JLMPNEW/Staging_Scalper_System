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
    DEFAULT_DEFENSE_PANEL,
    DEFAULT_PROTOCOL_PATH,
    DEFAULT_V14_ROOT,
    assess_defense_compatibility,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Assess defense compatibility read-only; write results only "
            "under the machinery v1.4 output root."
        )
    )
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    parser.add_argument(
        "--defense-panel",
        type=Path,
        default=DEFAULT_DEFENSE_PANEL,
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_V14_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = assess_defense_compatibility(
        protocol_path=args.protocol.expanduser().resolve(),
        defense_panel=args.defense_panel.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
