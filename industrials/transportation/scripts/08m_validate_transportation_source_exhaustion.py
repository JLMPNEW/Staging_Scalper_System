#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)
from industrials.transportation.source_exhaustion import (  # noqa: E402
    validate_written_source_exhaustion,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the written transportation DP6E source-universe audit "
            "without opening the database or using the network."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)["dedicated_parser"]
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else (
            resolve_path(parser_cfg["output_root"], base_dir=base_dir)
            / str(parser_cfg["source_census_asof_date"])
        )
    )
    errors = validate_written_source_exhaustion(
        output_dir=output_dir,
        expected_identity_count=int(
            parser_cfg["source_census_expected_identity_count"]
        ),
        expected_metric_count=90,
    )
    payload = {
        "acceptance": "PASS" if not errors else "FAIL",
        "gate": "DP6E_SOURCE_UNIVERSE_EXHAUSTION_VALIDATION",
        "model_family": MODEL_FAMILY,
        "database_mode": "not_opened",
        "network_requests": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "calibration_invocations": 0,
        "errors": errors,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    # Gate-FAIL is 2 across the DP chain; 1 is reserved for crashes.
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
