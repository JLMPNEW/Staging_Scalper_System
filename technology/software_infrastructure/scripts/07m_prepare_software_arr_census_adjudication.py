#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    open_read_only_database,
    parse_iso_date,
)
from technology.software_infrastructure.software_arr_census_adjudication import (  # noqa: E402
    apply_arr_review_overrides,
    build_arr_proposals,
    load_census_arr_evidence,
    load_arr_review_overrides,
    summarize_arr_proposals,
)
from technology.software_infrastructure.software_metric_review import (  # noqa: E402
    load_csv_rows,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CENSUS_ROOT = PROJECT_ROOT / "output" / "technology_reports" / "software_infrastructure" / "disclosure_census"
DEFAULT_REVIEW_OVERRIDES = (
    PACKAGE_ROOT / "software_infrastructure" / "review_policies" / "software_arr_census_review_overrides_v1.yaml"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a proposal-only, canonical ARR adjudication workbook from the completed software disclosure census."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--census-root", type=Path, default=DEFAULT_CENSUS_ROOT)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--minimum-cross-section", type=int, default=30)
    parser.add_argument(
        "--review-overrides",
        type=Path,
        default=DEFAULT_REVIEW_OVERRIDES,
        help="Versioned human-review corrections applied with source assertions.",
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
    asof_date = parse_iso_date(args.asof, field_name="asof")
    census_dir = args.census_root.expanduser().resolve() / asof_date
    accession_path = census_dir / "software_disclosure_census_accessions.csv"
    if not accession_path.is_file():
        raise FileNotFoundError(f"Census accession file not found: {accession_path}")
    accession_rows = load_csv_rows(accession_path)
    accessions = {str(row["accession_number"]) for row in accession_rows}
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        evidence = load_census_arr_evidence(conn, accessions=accessions)
    proposals = build_arr_proposals(evidence)
    review_override_path = args.review_overrides.expanduser().resolve()
    review_overrides = load_arr_review_overrides(review_override_path)
    expected_canonical_count = 0
    review_policy_suffix = review_override_path.suffix.lower()
    if review_policy_suffix in {".json", ".yaml", ".yml"}:
        review_policy = (
            json.loads(review_override_path.read_text(encoding="utf-8"))
            if review_policy_suffix == ".json"
            else load_yaml(review_override_path)
        )
        expected_canonical_count = int(review_policy.get("expected_corrected_canonical_count") or 0)
    proposals, override_summary = apply_arr_review_overrides(
        proposals,
        review_overrides,
    )
    ticker_rows, summary = summarize_arr_proposals(
        proposals,
        minimum_cross_section=max(1, args.minimum_cross_section),
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else census_dir / "arr_adjudication"
    output_dir.mkdir(parents=True, exist_ok=True)
    workbook_path = output_dir / "software_arr_proposed_adjudication_workbook.csv"
    canonical_path = output_dir / "software_arr_proposed_canonical_review.csv"
    ticker_path = output_dir / "software_arr_proposed_ticker_coverage.csv"
    summary_path = output_dir / "software_arr_proposed_adjudication_summary.json"
    canonical_rows = [row for row in proposals if int(row["canonical_candidate_flag"]) == 1]
    if expected_canonical_count and len(canonical_rows) != expected_canonical_count:
        raise ValueError(
            "Corrected ARR canonical count does not match reviewed policy: "
            f"expected={expected_canonical_count}, actual={len(canonical_rows)}"
        )
    atomic_csv(workbook_path, proposals)
    atomic_csv(canonical_path, canonical_rows)
    atomic_csv(ticker_path, ticker_rows)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "manifest_version": "software_arr_census_adjudication_v1",
        "generated_at": generated_at,
        "asof_date": asof_date,
        "model_family": "software_infrastructure",
        "metric_name": "annual_recurring_revenue",
        "proposal_only_flag": 1,
        "human_approval_flag": 0,
        "official_policy_modified_flag": 0,
        "production_facts_modified_flag": 0,
        "production_scores_modified_flag": 0,
        "census_accessions_path": str(accession_path),
        "census_accessions_sha256": file_sha256(accession_path),
        "review_override_policy_path": str(review_override_path),
        "review_override_policy_sha256": file_sha256(review_override_path),
        "review_expected_canonical_count": expected_canonical_count,
        "source_evidence_row_count": len(evidence),
        "proposal_row_count": len(proposals),
        "workbook_path": str(workbook_path),
        "workbook_sha256": file_sha256(workbook_path),
        "canonical_review_path": str(canonical_path),
        "canonical_review_sha256": file_sha256(canonical_path),
        "ticker_coverage_path": str(ticker_path),
        "ticker_coverage_sha256": file_sha256(ticker_path),
        **override_summary,
        **summary,
    }
    atomic_json(summary_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
