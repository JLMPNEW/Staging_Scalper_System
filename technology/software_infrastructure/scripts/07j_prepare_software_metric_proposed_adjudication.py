#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    open_read_only_database,
)
from technology.software_infrastructure.software_metric_proposed_adjudication import (  # noqa: E402
    build_proposed_rows,
)
from technology.software_infrastructure.software_metric_review import (  # noqa: E402
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
DEFAULT_QUEUE = SOFTWARE_ROOT / "data" / "software_metrics_v1_expansion_queue.csv"
DEFAULT_REVIEW = (
    SOFTWARE_ROOT
    / "review_policies"
    / "software_metrics_v3_adjudication_workbook.csv"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "dedicated_parser_governance"
    / "software_metrics_v3"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a structurally validated, proposal-only software metric "
            "adjudication workbook without modifying the official review."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--review-file", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _write_exception_report(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    selected = [
        row
        for row in rows
        if str(row["decision"]) == "CORRECTED"
        or str(row["calibration_eligible_flag"]) == "0"
        or str(row["proposal_review_priority"]) != "standard"
    ]
    fields = (
        "metric_family",
        "ticker",
        "accession_number",
        "form_type",
        "accepted_at",
        "source_document",
        "source_metric",
        "candidate_value",
        "unit",
        "period_end",
        "decision",
        "decision_reason",
        "effective_metric",
        "effective_value",
        "effective_unit",
        "effective_period_start",
        "effective_period_end",
        "effective_scope",
        "period_kind",
        "definition_variant",
        "calibration_eligible_flag",
        "proposal_confidence",
        "proposal_review_priority",
        "review_notes",
        "evidence_text",
        "source_evidence_key",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            {
                field: row.get(field, "")
                for field in fields
            }
            for row in selected
        )


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
    queue_path = args.queue.expanduser().resolve()
    review_path = args.review_file.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    queue_rows = load_csv_rows(queue_path)
    review_rows = load_csv_rows(review_path)
    proposed_at = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    proposed_rows = build_proposed_rows(
        review_rows,
        proposed_at_utc=proposed_at,
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        source = load_source_evidence(
            conn,
            (
                str(row["source_evidence_key"])
                for row in queue_rows
            ),
        )
        errors, structural_summary = validate_review_rows(
            proposed_rows,
            queue_rows=queue_rows,
            source_evidence=source,
        )
    proposed_path = (
        output_dir / "software_metrics_v3_proposed_adjudication_workbook.csv"
    )
    atomic_csv(proposed_path, proposed_rows)
    exception_path = (
        output_dir / "software_metrics_v3_proposed_adjudication_exceptions.csv"
    )
    _write_exception_report(exception_path, proposed_rows)
    family_decisions = Counter(
        (str(row["metric_family"]), str(row["decision"]))
        for row in proposed_rows
    )
    summary_rows = [
        {
            "metric_family": family,
            "decision": decision,
            "row_count": count,
        }
        for (family, decision), count in sorted(family_decisions.items())
    ]
    summary_path = (
        output_dir / "software_metrics_v3_proposed_adjudication_summary.csv"
    )
    atomic_csv(summary_path, summary_rows)
    manifest = {
        "manifest_version": "software_metric_proposed_adjudication_v1",
        "generated_at": proposed_at,
        "model_family": "software_infrastructure",
        "proposal_only_flag": 1,
        "human_approval_flag": 0,
        "ready_for_release_flag": 0,
        "official_review_modified_flag": 0,
        "production_facts_modified_flag": 0,
        "production_scores_modified_flag": 0,
        "queue_path": str(queue_path),
        "queue_sha256": file_sha256(queue_path),
        "official_review_path": str(review_path),
        "official_review_sha256": file_sha256(review_path),
        "proposed_workbook_path": str(proposed_path),
        "proposed_workbook_sha256": file_sha256(proposed_path),
        "exception_report_path": str(exception_path),
        "exception_report_sha256": file_sha256(exception_path),
        "summary_path": str(summary_path),
        "summary_sha256": file_sha256(summary_path),
        "proposal_row_count": len(proposed_rows),
        "decision_counts": dict(
            sorted(Counter(row["decision"] for row in proposed_rows).items())
        ),
        "calibration_eligible_count": sum(
            int(row["calibration_eligible_flag"])
            for row in proposed_rows
        ),
        "structural_validation_status": (
            "FAIL" if errors else "PASS"
        ),
        "structural_validation_errors": errors,
        "structural_validator_summary": structural_summary,
    }
    manifest_path = (
        output_dir / "software_metrics_v3_proposed_adjudication_manifest.json"
    )
    atomic_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
