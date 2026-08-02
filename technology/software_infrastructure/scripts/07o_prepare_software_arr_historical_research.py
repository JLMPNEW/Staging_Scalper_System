#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import date
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
from technology.software_infrastructure.software_arr_census_adjudication import (  # noqa: E402
    apply_arr_review_overrides,
    build_arr_proposals,
    load_arr_review_overrides,
)
from technology.software_infrastructure.software_arr_release import (  # noqa: E402
    AUTO_STRICT_RESEARCH_ONLY,
    HUMAN_APPROVED,
    RESEARCH_POLICY_ID,
    RESEARCH_RELEASE_ID,
    build_arr_policy,
    source_keys,
    utc_timestamp,
    validate_arr_rows,
)
from technology.software_infrastructure.software_metric_review import (  # noqa: E402
    load_csv_rows,
    load_source_evidence,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)
from technology.software_infrastructure.software_specialized_metrics import (  # noqa: E402
    load_policy,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOFTWARE_ROOT = PACKAGE_ROOT / "software_infrastructure"
DEFAULT_APPROVED_POLICY = (
    SOFTWARE_ROOT / "review_policies" / "software_arr_policy_v1.json"
)
DEFAULT_REVIEW_OVERRIDES = (
    SOFTWARE_ROOT
    / "review_policies"
    / "software_arr_census_review_overrides_v1.yaml"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "arr_historical_research"
    / date.today().isoformat()
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a measurement-only ARR historical research extension from "
            "strict parser candidates for the human-approved issuer set."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--approved-policy", type=Path, default=DEFAULT_APPROVED_POLICY
    )
    parser.add_argument(
        "--review-overrides", type=Path, default=DEFAULT_REVIEW_OVERRIDES
    )
    parser.add_argument("--start-date", default="2018-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _load_arr_evidence(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT *
        FROM sec_parser_metric_evidence_shadow
        WHERE model_family = 'software_infrastructure'
          AND metric_name = 'annual_recurring_revenue'
          AND candidate_value IS NOT NULL
          AND candidate_status IN ('REVIEW_REQUIRED', 'REJECTED_POLICY')
          AND ticker IN ({placeholders})
          AND substr(accepted_at, 1, 10) BETWEEN ? AND ?
        ORDER BY ticker, accepted_at, accession_number, evidence_key
        """,
        (*tickers, start_date, end_date),
    ).fetchall()
    return [dict(row) for row in rows]


def _iso(value: str, *, field: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def main() -> int:
    args = parse_args()
    start_date = _iso(args.start_date, field="start-date")
    end_date = _iso(args.end_date, field="end-date")
    if start_date > end_date:
        raise ValueError("start-date cannot be after end-date")
    approved_policy_path = args.approved_policy.expanduser().resolve()
    approved_policy = load_policy(approved_policy_path)
    approved_workbook_path = Path(
        str(approved_policy["approved_workbook_path"])
    ).resolve()
    approved_hash = str(approved_policy["approved_workbook_sha256"])
    if file_sha256(approved_workbook_path) != approved_hash:
        raise ValueError("Approved ARR workbook hash no longer matches policy")
    approved_rows = load_csv_rows(approved_workbook_path)
    approved_keys = set(source_keys(approved_rows))
    tickers = sorted(
        {str(row["ticker"]) for row in approved_policy["decisions"]}
    )
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
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        all_evidence = _load_arr_evidence(
            conn,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
        )
        proposals = build_arr_proposals(all_evidence)
        proposals, override_summary = apply_arr_review_overrides(
            proposals,
            load_arr_review_overrides(args.review_overrides.resolve()),
        )
        strict_rows = [
            row
            for row in proposals
            if int(row["canonical_candidate_flag"]) == 1
            and str(row["proposal_decision"]) in {"ACCEPTED", "CORRECTED"}
        ]
        extension_rows = [
            row
            for row in strict_rows
            if str(row["evidence_key"]) not in approved_keys
        ]
        combined_rows = sorted(
            [*approved_rows, *extension_rows],
            key=lambda row: (
                str(row.get("ticker") or ""),
                str(row.get("effective_period_end") or ""),
                str(row.get("accepted_at") or ""),
                str(row.get("evidence_key") or ""),
            ),
        )
        source = load_source_evidence(conn, source_keys(combined_rows))
    errors = validate_arr_rows(
        combined_rows,
        source_evidence=source,
        expected_count=None,
    )
    if errors:
        raise ValueError(
            "Historical ARR extension validation failed: "
            + "; ".join(errors[:10])
        )
    governance = {
        key: HUMAN_APPROVED for key in approved_keys
    }
    governance.update(
        {
            str(row["evidence_key"]): AUTO_STRICT_RESEARCH_ONLY
            for row in extension_rows
        }
    )
    output_dir = args.output_dir.expanduser().resolve()
    workbook_path = output_dir / "software_arr_historical_research_workbook.csv"
    policy_path = output_dir / "software_arr_historical_research_policy.json"
    coverage_path = output_dir / "software_arr_historical_research_coverage.csv"
    manifest_path = output_dir / "software_arr_historical_research_manifest.json"
    workbook_rows = [
        {**row, "governance_status": governance[str(row["evidence_key"])]}
        for row in combined_rows
    ]
    atomic_csv(workbook_path, workbook_rows)
    policy = build_arr_policy(
        rows=combined_rows,
        source_evidence=source,
        release_id=RESEARCH_RELEASE_ID,
        policy_id=RESEARCH_POLICY_ID,
        approved_workbook_path=workbook_path,
        approved_workbook_sha256=file_sha256(workbook_path),
        reviewer="ARR strict historical research automation",
        reviewed_at_utc=utc_timestamp(),
        governance_status_by_key=governance,
        registry_path=(
            SOFTWARE_ROOT
            / "data"
            / "software_infrastructure_specialized_metric_registry.yaml"
        ),
        adapter_path=SOFTWARE_ROOT / "dedicated_parser_adapter.py",
    )
    atomic_json(policy_path, policy)
    rows_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for row in combined_rows:
        rows_by_ticker.setdefault(str(row["ticker"]), []).append(row)
    coverage = [
        {
            "ticker": ticker,
            "observation_count": len(rows),
            "first_period_end": min(
                str(row["effective_period_end"]) for row in rows
            ),
            "last_period_end": max(
                str(row["effective_period_end"]) for row in rows
            ),
            "longitudinal_flag": int(len(rows) >= 2),
            "human_approved_observation_count": sum(
                str(row["evidence_key"]) in approved_keys for row in rows
            ),
            "auto_strict_research_observation_count": sum(
                str(row["evidence_key"]) not in approved_keys for row in rows
            ),
        }
        for ticker, rows in sorted(rows_by_ticker.items())
    ]
    atomic_csv(coverage_path, coverage)
    manifest = {
        "manifest_version": "software_arr_historical_research_v1",
        "start_date": start_date,
        "end_date": end_date,
        "approved_policy_path": str(approved_policy_path),
        "approved_policy_sha256": file_sha256(approved_policy_path),
        "approved_workbook_sha256": approved_hash,
        "source_evidence_count": len(all_evidence),
        "proposal_count": len(proposals),
        "strict_candidate_count": len(strict_rows),
        "human_approved_observation_count": len(approved_rows),
        "auto_strict_research_observation_count": len(extension_rows),
        "combined_observation_count": len(combined_rows),
        "issuer_count": len(rows_by_ticker),
        "longitudinal_issuer_count": sum(
            int(row["longitudinal_flag"]) for row in coverage
        ),
        "governance_counts": dict(
            sorted(Counter(governance.values()).items())
        ),
        "auto_strict_rows_production_eligible_flag": 0,
        "measurement_only_flag": 1,
        "production_weight_modified_flag": 0,
        "policy_path": str(policy_path),
        "policy_sha256": file_sha256(policy_path),
        "workbook_path": str(workbook_path),
        "workbook_sha256": file_sha256(workbook_path),
        "coverage_path": str(coverage_path),
        "coverage_sha256": file_sha256(coverage_path),
        **override_summary,
    }
    atomic_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
