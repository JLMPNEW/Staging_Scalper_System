#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)
from industrials.transportation.source_census import (  # noqa: E402
    build_source_census,
    read_only_connection,
    write_source_census,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the read-only transportation DP3 accession/document census. "
            "This command performs no hydration and does not invoke the parser."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    universe = family["universe"]
    foundation = resolve_foundation(config_path, args.db)
    base_dir = config_path.parent
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=base_dir,
    )
    submissions_cache_dir = cache_dir / "sec_submissions"
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with read_only_connection(
        foundation.db_path,
        timeout_sec=timeout_sec,
    ) as connection:
        census_rows, decisions, gaps, summary = build_source_census(
            connection,
            cache_dir=cache_dir,
            submissions_cache_dir=submissions_cache_dir,
            final_scope_path=resolve_path(
                parser_cfg["scope_manifest_csv"],
                base_dir=base_dir,
            ),
            support_scope_path=resolve_path(
                parser_cfg["supporting_scope_manifest_csv"],
                base_dir=base_dir,
            ),
            listing_dates_path=resolve_path(
                universe["listing_dates_csv"],
                base_dir=base_dir,
            ),
            continuity_path=resolve_path(
                universe["security_continuity_overrides_csv"],
                base_dir=base_dir,
            ),
            dp0_manifest_path=resolve_path(
                parser_cfg["dp0_manifest_json"],
                base_dir=base_dir,
            ),
            gap_override_path=resolve_path(
                parser_cfg["source_gap_overrides_csv"],
                base_dir=base_dir,
            ),
            manifest_version=str(parser_cfg["source_census_version"]),
            source_id=str(cfg_get(config, "sec_fundamentals.submissions_source_id")),
            active_source_id=str(universe["seed_source_id"]),
            historical_source_id=str(universe["historical_membership_source_id"]),
            start_date=str(parser_cfg["source_census_start_date"]),
            asof_date=str(parser_cfg["source_census_asof_date"]),
            expected_identity_count=int(parser_cfg["source_census_expected_identity_count"]),
            expected_base_accession_count=int(parser_cfg["source_census_expected_base_accessions"]),
        )
    payload = write_source_census(
        census_rows=census_rows,
        decisions=decisions,
        gaps=gaps,
        summary=summary,
        census_path=resolve_path(
            parser_cfg["source_census_csv"],
            base_dir=base_dir,
        ),
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
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
