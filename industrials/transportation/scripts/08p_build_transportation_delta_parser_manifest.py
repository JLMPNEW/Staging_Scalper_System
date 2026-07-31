#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.transportation.delta_parser_manifest import (  # noqa: E402
    build_delta_parser_rows,
    write_delta_parser_manifest,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)
from industrials.transportation.source_exhaustion import (  # noqa: E402
    SOURCE_EXHAUSTION_VERSION,
)
from industrials.transportation.source_exhaustion_hydration import (  # noqa: E402
    read_csv,
    validate_sealed_csv_artifact,
)


ADAPTER = (
    "industrials.transportation.dedicated_parser_adapter:"
    "extract_metric_evidence"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Seal the cached DP6E document delta as a shared-parser source "
            "manifest. This command is local-only and invokes no parser."
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
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Delta manifest build requires "
            "parser_execution_authorized=false"
        )
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else (
            resolve_path(parser_cfg["output_root"], base_dir=base_dir)
            / str(parser_cfg["source_census_asof_date"])
        )
    )
    source_manifest_path = (
        output_dir / "transportation_source_exhaustion_manifest.json"
    )
    delta_path = (
        output_dir / "transportation_source_exhaustion_delta_candidates.csv"
    )
    source_manifest = json.loads(
        source_manifest_path.read_text(encoding="utf-8")
    )
    if (
        source_manifest.get("manifest_version")
        != SOURCE_EXHAUSTION_VERSION
        or source_manifest.get("metadata_exhaustion_complete") is not True
        or int(source_manifest.get("index_metadata_gap_count") or 0) != 0
        or int(source_manifest.get("database_registry_gap_count") or 0) != 0
    ):
        raise ValueError(
            "Delta parser manifest requires current DP6E metadata and "
            "registry exhaustion"
        )
    delta_rows = read_csv(delta_path)
    seal_errors = validate_sealed_csv_artifact(
        source_manifest=source_manifest,
        artifact_name="delta_candidates",
        path=delta_path,
        rows=delta_rows,
    )
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=base_dir,
    )
    rows, errors = build_delta_parser_rows(
        delta_rows=delta_rows,
        archive_cache_dir=cache_dir / "sec_archive_xbrl",
        source_id=str(
            cfg_get(config, "sec_fundamentals.submissions_source_id")
        ),
    )
    payload = write_delta_parser_manifest(
        rows=rows,
        errors=[*seal_errors, *errors],
        source_manifest_path=source_manifest_path,
        output_dir=output_dir,
        expected_metric_count=int(
            source_manifest.get("metric_count") or 0
        ),
        parser_metric_count=len(load_registry(ADAPTER).parser_metrics),
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
