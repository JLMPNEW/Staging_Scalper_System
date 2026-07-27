#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import family_config, load_yaml, resolve_path  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)
from industrials.transportation.source_census import (  # noqa: E402
    validate_written_source_census,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the sealed transportation DP3 source-census artifacts "
            "without modifying the database or invoking the parser."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--verify-content-hashes", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)["dedicated_parser"]
    base_dir = config_path.parent
    errors = validate_written_source_census(
        census_path=resolve_path(parser_cfg["source_census_csv"], base_dir=base_dir),
        decisions_path=resolve_path(
            parser_cfg["source_decisions_csv"],
            base_dir=base_dir,
        ),
        gaps_path=resolve_path(
            parser_cfg["source_cache_gaps_csv"],
            base_dir=base_dir,
        ),
        manifest_path=resolve_path(
            parser_cfg["source_census_manifest_json"],
            base_dir=base_dir,
        ),
        verify_content_hashes=args.verify_content_hashes,
    )
    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "model_family": MODEL_FAMILY,
        "database_mode": "not_opened",
        "network_requests": 0,
        "parser_invocations": 0,
        "errors": errors,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
