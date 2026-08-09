#!/usr/bin/env python3
"""Run the manual, production-disabled Technology factor-validation pilot."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.adapters.factor_validation_shadow import (  # noqa: E402
    run_technology_factor_validation_shadow,
    settings_from_config,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LOGGER = logging.getLogger("technology_factor_validation_shadow")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish reconciled shared factor evidence in Technology shadow mode."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Optional isolated output-root override; never a portfolio path.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    try:
        settings = settings_from_config(args.config, output_root=args.output_root)
        report = run_technology_factor_validation_shadow(settings)
    except Exception:
        LOGGER.exception("Technology factor-validation shadow pilot failed")
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
