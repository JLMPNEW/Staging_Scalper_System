#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)
from industrials.transportation.source_exhaustion import (  # noqa: E402
    SOURCE_EXHAUSTION_VERSION,
)
from industrials.transportation.source_exhaustion_hydration import (  # noqa: E402
    read_csv,
    validate_sealed_csv_artifact,
)
from industrials.transportation.source_registry_load import (  # noqa: E402
    apply_source_registry_load,
    plan_source_registry_load,
    write_source_registry_load,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Load only missing DP6E SEC filing metadata into "
            "fact_sec_filing. Existing rows are preserved; documents, parser "
            "facts, features, calibration, and portfolio data are untouched."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Registry loading requires parser_execution_authorized=false"
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
    filing_path = (
        output_dir / "transportation_source_exhaustion_filing_inventory.csv"
    )
    if not source_manifest_path.is_file() or not filing_path.is_file():
        raise FileNotFoundError(
            "DP6E source manifest and filing inventory are required"
        )
    source_manifest = json.loads(
        source_manifest_path.read_text(encoding="utf-8")
    )
    source_manifest_sha256 = file_sha256(source_manifest_path)
    if (
        source_manifest.get("acceptance") != "PASS_WITH_REQUIRED_DELTA"
        or source_manifest.get("manifest_version")
        != SOURCE_EXHAUSTION_VERSION
        or source_manifest.get("metadata_exhaustion_complete") is not True
    ):
        raise ValueError(
            "Registry load requires a current, metadata-complete DP6E v2 "
            "source manifest"
        )
    filing_rows = read_csv(filing_path)
    seal_errors = validate_sealed_csv_artifact(
        source_manifest=source_manifest,
        artifact_name="filing_inventory",
        path=filing_path,
        rows=filing_rows,
    )
    if seal_errors:
        raise ValueError(
            "DP6E filing inventory seal failed: "
            + "; ".join(seal_errors)
        )
    foundation = resolve_foundation(config_path, args.db)
    source_id = str(
        cfg_get(config, "sec_fundamentals.submissions_source_id")
    )
    if not foundation.db_path.is_file():
        raise FileNotFoundError(foundation.db_path)
    mode = "rw" if args.execute else "ro"
    uri = f"file:{foundation.db_path.as_posix()}?mode={mode}"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=foundation.timeout_sec,
    )
    connection.row_factory = sqlite3.Row
    try:
        planned, errors = plan_source_registry_load(
            connection,
            filing_rows=filing_rows,
            source_id=source_id,
        )
        inserted_count = 0
        if args.execute and not errors:
            with connection:
                inserted_count = apply_source_registry_load(
                    connection,
                    planned_rows=planned,
                )
                if (
                    file_sha256(source_manifest_path)
                    != source_manifest_sha256
                    or validate_sealed_csv_artifact(
                        source_manifest=source_manifest,
                        artifact_name="filing_inventory",
                        path=filing_path,
                        rows=filing_rows,
                    )
                ):
                    raise ValueError(
                        "DP6E source inputs changed during registry load; "
                        "transaction rolled back"
                    )
            if inserted_count != sum(
                str(row["load_action"]) == "INSERT_MISSING"
                for row in planned
            ):
                errors.append(
                    "inserted filing count does not equal the sealed gap count"
                )
        payload = write_source_registry_load(
            rows=planned,
            errors=errors,
            execute=args.execute,
            inserted_count=inserted_count,
            source_manifest_path=source_manifest_path,
            output_dir=output_dir,
        )
    finally:
        connection.close()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
