#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    open_read_only_database,
)
from technology.software_infrastructure.software_metric_review import (  # noqa: E402
    build_review_rows,
    load_csv_rows,
    load_source_evidence,
    validate_review_rows,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOFTWARE_ROOT = PACKAGE_ROOT / "software_infrastructure"
DEFAULT_QUEUE = (
    SOFTWARE_ROOT / "data" / "software_metrics_v1_expansion_queue.csv"
)
DEFAULT_REVIEW = (
    SOFTWARE_ROOT
    / "review_policies"
    / "software_metrics_v2_adjudication_workbook.csv"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "dedicated_parser_governance"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or validate the tamper-evident human adjudication "
            "workbook for the software metric expansion corpus."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--review-file", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--prepare",
        action="store_true",
        help=(
            "Create or refresh the workbook while preserving decisions whose "
            "source seals have not changed."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    queue_path = args.queue.expanduser().resolve()
    review_path = args.review_file.expanduser().resolve()
    queue_rows = load_csv_rows(queue_path)
    existing_rows = (
        load_csv_rows(review_path) if review_path.is_file() else []
    )
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        source = load_source_evidence(
            conn,
            (
                str(row["source_evidence_key"])
                for row in queue_rows
            ),
        )
        if args.prepare or not review_path.is_file():
            review_rows = build_review_rows(
                queue_rows,
                source_evidence=source,
                existing_rows=existing_rows,
            )
            atomic_csv(review_path, review_rows)
        else:
            review_rows = existing_rows
        errors, summary = validate_review_rows(
            review_rows,
            queue_rows=queue_rows,
            source_evidence=source,
        )
    manifest = {
        "manifest_version": "software_metric_expansion_review_manifest_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "model_family": "software_infrastructure",
        "execution_mode": "prepare" if args.prepare else "validate",
        "queue_path": str(queue_path),
        "queue_sha256": file_sha256(queue_path),
        "review_path": str(review_path),
        "review_sha256": file_sha256(review_path),
        "production_facts_modified_flag": 0,
        "production_scores_modified_flag": 0,
        **summary,
    }
    output_path = (
        args.output_dir.expanduser().resolve()
        / "software_metrics_v2_review_manifest.json"
    )
    atomic_json(output_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
