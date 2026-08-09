#!/usr/bin/env python3
"""Verify a Technology shadow campaign against packages and the root ledger."""

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
    settings_from_config,
    technology_shadow_provenance_files,
    validate_technology_factor_validation_shadow,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LOGGER = logging.getLogger("validate_technology_factor_validation_shadow")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify one governed Technology factor-validation shadow campaign."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output-root", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    try:
        settings = settings_from_config(args.config, output_root=args.output_root)
        report = validate_technology_factor_validation_shadow(
            settings.output_root,
            campaign_id=args.campaign_id,
            provenance_files=technology_shadow_provenance_files(settings),
        )
    except Exception:
        LOGGER.exception("Technology factor-validation shadow verification failed")
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
