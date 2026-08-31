#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import json
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.disclosure_candidates import (  # noqa: E402
    EXTRACTION_METHOD,
    SUPPORTED_METRICS_BY_COHORT,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


REVIEW_FIELDS = [
    "ticker",
    "calibration_cohort",
    "industry",
    "filing_date",
    "form_type",
    "accession_number",
    "document_name",
    "metric_name",
    "concept_name",
    "candidate_value",
    "unit",
    "confidence",
    "candidate_status",
    "status_reason",
    "evidence_text",
    "source_url",
    "content_sha256",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate bounded transportation specialized-disclosure recovery."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--review-csv", type=Path, default=None)
    return parser.parse_args()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [
            {str(key): str(value or "") for key, value in row.items() if key is not None}
            for row in reader
        ]


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(str(value).strip()))
    except (TypeError, ValueError):
        return False


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _expected_universe_roles(
    conn: sqlite3.Connection,
    *,
    asof: str,
    active_source_id: str,
    historical_source_id: str,
    include_historical: bool,
) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT t.ticker,
               CASE
                 WHEN EXISTS (
                   SELECT 1 FROM dim_universe_membership AS active
                   WHERE active.ticker=t.ticker
                     AND active.model_family=t.model_family
                     AND active.membership_source_id=?
                     AND active.membership_status='active'
                     AND active.start_date<=?
                     AND COALESCE(active.end_date, '9999-12-31')>=?
                 ) THEN 'active'
                 WHEN EXISTS (
                   SELECT 1 FROM dim_universe_membership AS historical
                   WHERE historical.ticker=t.ticker
                     AND historical.model_family=t.model_family
                     AND historical.membership_source_id=?
                 ) THEN 'delisted_usable'
                 ELSE 'delisted_excluded'
               END AS universe_role
        FROM dim_industrials_taxonomy AS t
        WHERE t.model_family=?
        ORDER BY t.ticker
        """,
        (
            active_source_id,
            asof,
            asof,
            historical_source_id,
            MODEL_FAMILY,
        ),
    ).fetchall()
    roles = {str(row["ticker"]): str(row["universe_role"]) for row in rows}
    if include_historical:
        return roles
    return {ticker: role for ticker, role in roles.items() if role == "active"}


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    specialized = family.get("specialized_disclosures")
    if not isinstance(specialized, dict):
        raise KeyError("model_families.transportation.specialized_disclosures is required")
    universe = family["universe"]
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    input_path = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(specialized["sync_output_csv"], base_dir=base_dir)
    )
    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else resolve_path(specialized["validation_output_json"], base_dir=base_dir)
    )
    review_path = (
        args.review_csv.expanduser().resolve()
        if args.review_csv
        else resolve_path(specialized["review_output_csv"], base_dir=base_dir)
    )
    minimum_active_document_coverage = float(
        specialized.get("minimum_active_document_coverage", 0.95)
    )
    minimum_all_document_coverage = float(
        specialized.get("minimum_all_document_coverage", 0.90)
    )
    minimum_scale_coverage = float(
        specialized.get("minimum_accepted_ticker_coverage_for_historical_scale", 0.25)
    )
    submissions_source_id = str(
        cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions")
    )
    rows = _read_csv(input_path)
    errors: list[str] = []
    warnings: list[str] = []
    gates: dict[str, dict[str, Any]] = {}
    include_historical = bool(specialized.get("include_historical", True))
    with closing(_read_only_connection(db_path)) as conn:
        expected_roles = _expected_universe_roles(
            conn,
            asof=args.asof[:10],
            active_source_id=str(universe["seed_source_id"]),
            historical_source_id=str(
                universe["historical_membership_source_id"]
            ),
            include_historical=include_historical,
        )
    tickers = [str(row.get("ticker") or "") for row in rows]
    if len(set(tickers)) != len(tickers) or not all(tickers):
        errors.append("sync coverage must contain unique non-blank tickers")
    observed_roles = {
        str(row.get("ticker") or ""): str(row.get("universe_role") or "")
        for row in rows
        if str(row.get("ticker") or "")
    }
    missing_tickers = sorted(set(expected_roles) - set(observed_roles))
    unexpected_tickers = sorted(set(observed_roles) - set(expected_roles))
    role_mismatches = sorted(
        ticker
        for ticker in set(expected_roles).intersection(observed_roles)
        if observed_roles[ticker] != expected_roles[ticker]
    )
    if missing_tickers or unexpected_tickers or role_mismatches:
        errors.append(
            "sync universe differs from governed taxonomy: "
            f"missing={missing_tickers[:20]} "
            f"unexpected={unexpected_tickers[:20]} "
            f"role_mismatch={role_mismatches[:20]}"
        )
    active_rows = [row for row in rows if row.get("universe_role") == "active"]
    expected_active = {
        ticker for ticker, role in expected_roles.items() if role == "active"
    }
    observed_active = {
        str(row.get("ticker") or "") for row in active_rows
    }
    if observed_active != expected_active:
        errors.append(
            "active sync coverage differs from governed membership: "
            f"missing={sorted(expected_active - observed_active)[:20]} "
            f"unexpected={sorted(observed_active - expected_active)[:20]}"
        )
    generic_active = sorted(
        row["ticker"]
        for row in active_rows
        if row.get("industry") in {"", "Transportation"}
    )
    if generic_active:
        errors.append(
            f"active detailed industry taxonomy missing tickers={generic_active[:20]}"
        )
    gates["universe_and_taxonomy_contract"] = {
        "status": "PASS" if not errors else "FAIL",
        "row_count": len(rows),
        "active_row_count": len(active_rows),
        "generic_active_industry_tickers": generic_active,
    }
    document_tickers = {
        row["ticker"] for row in rows if int(row.get("fetched_document_count") or 0) > 0
    }
    active_document_tickers = {
        row["ticker"]
        for row in active_rows
        if int(row.get("fetched_document_count") or 0) > 0
    }
    active_document_coverage = _ratio(len(active_document_tickers), len(active_rows))
    all_document_coverage = _ratio(len(document_tickers), len(rows))
    document_gate_errors: list[str] = []
    if active_document_coverage < minimum_active_document_coverage:
        document_gate_errors.append(
            f"active document coverage={active_document_coverage:.4f} "
            f"minimum={minimum_active_document_coverage:.4f}"
        )
    if all_document_coverage < minimum_all_document_coverage:
        document_gate_errors.append(
            f"all-universe document coverage={all_document_coverage:.4f} "
            f"minimum={minimum_all_document_coverage:.4f}"
        )
    errors.extend(document_gate_errors)
    gates["bounded_document_recovery"] = {
        "status": "PASS" if not document_gate_errors else "FAIL",
        "active_document_ticker_count": len(active_document_tickers),
        "active_document_coverage": round(active_document_coverage, 6),
        "all_document_ticker_count": len(document_tickers),
        "all_document_coverage": round(all_document_coverage, 6),
        "failed_tickers": sorted(set(tickers) - document_tickers),
    }

    with _read_only_connection(db_path) as conn:
        member_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT t.ticker, t.industry, t.calibration_cohort_id,
                       CASE WHEN EXISTS (
                         SELECT 1 FROM dim_universe_membership AS m
                         WHERE m.ticker=t.ticker AND m.model_family=t.model_family
                           AND m.membership_source_id=?
                           AND m.membership_status='active'
                           AND m.start_date<=?
                           AND COALESCE(m.end_date,'9999-12-31')>=?
                       ) THEN 1 ELSE 0 END AS active_flag
                FROM dim_industrials_taxonomy AS t
                WHERE t.model_family=?
                ORDER BY t.ticker
                """,
                (str(universe["seed_source_id"]), args.asof, args.asof, MODEL_FAMILY),
            ).fetchall()
        ]
        candidates = [
            dict(row)
            for row in conn.execute(
                """
                WITH selected_filings AS (
                  SELECT ticker, accession_number,
                         ROW_NUMBER() OVER (
                           PARTITION BY ticker,
                             CASE
                               WHEN UPPER(form_type) IN (
                                 '10-K', '10-K/A', '10-12B', '10-12B/A',
                                 '20-F', '20-F/A', '40-F', '40-F/A'
                               ) THEN 'annual'
                               ELSE 'interim'
                             END
                           ORDER BY filing_date DESC, accession_number DESC
                         ) AS filing_rank
                  FROM fact_sec_filing
                  WHERE source_id=? AND filing_date<=?
                    AND UPPER(form_type) IN (
                      '10-K', '10-K/A', '10-12B', '10-12B/A',
                      '20-F', '20-F/A', '40-F', '40-F/A',
                      '10-Q', '10-Q/A', '6-K', '6-K/A'
                    )
                    AND COALESCE(primary_document, '')<>''
                )
                SELECT c.*, t.industry, t.calibration_cohort_id
                FROM fact_sec_metric_disclosure_candidate AS c
                JOIN selected_filings AS selected
                  ON selected.ticker=c.ticker
                 AND selected.accession_number=c.accession_number
                 AND selected.filing_rank=1
                JOIN dim_industrials_taxonomy AS t
                  ON t.ticker=c.ticker AND t.model_family=c.model_family
                WHERE c.model_family=? AND c.extraction_method=?
                  AND c.filing_date<=?
                ORDER BY c.ticker, c.metric_name, c.filing_date DESC,
                         c.confidence DESC, c.candidate_key
                """,
                (
                    submissions_source_id,
                    args.asof,
                    MODEL_FAMILY,
                    EXTRACTION_METHOD,
                    args.asof,
                ),
            ).fetchall()
        ]
    provenance_errors: list[str] = []
    review_rows: list[dict[str, Any]] = []
    candidate_tickers: set[str] = set()
    accepted_tickers: set[str] = set()
    accepted_tickers_by_cohort: dict[str, set[str]] = {}
    candidate_tickers_by_cohort: dict[str, set[str]] = {}
    accepted_metrics_by_cohort: dict[str, set[str]] = {}
    for candidate in candidates:
        ticker = str(candidate["ticker"])
        cohort = str(candidate["calibration_cohort_id"])
        metric = str(candidate["metric_name"])
        status = str(candidate["candidate_status"])
        candidate_tickers.add(ticker)
        candidate_tickers_by_cohort.setdefault(cohort, set()).add(ticker)
        supported = SUPPORTED_METRICS_BY_COHORT.get(cohort, frozenset())
        if metric not in supported:
            provenance_errors.append(
                f"{ticker}:{metric}: metric is not supported for cohort={cohort}"
            )
        if status == "PARSER_FAILURE":
            provenance_errors.append(f"{ticker}:{metric}: parser failure candidate")
        if status == "ACCEPTED":
            if not _finite(candidate.get("candidate_value")):
                provenance_errors.append(
                    f"{ticker}:{metric}: accepted candidate has no finite value"
                )
            accepted_tickers.add(ticker)
            accepted_tickers_by_cohort.setdefault(cohort, set()).add(ticker)
            accepted_metrics_by_cohort.setdefault(cohort, set()).add(metric)
        try:
            provenance = json.loads(str(candidate.get("provenance_json") or "{}"))
        except json.JSONDecodeError:
            provenance = {}
        if not isinstance(provenance, dict):
            provenance = {}
        if not str(provenance.get("source_url") or "").startswith(
            "https://www.sec.gov/Archives/"
        ):
            provenance_errors.append(f"{ticker}:{metric}: missing SEC archive source URL")
        if len(str(provenance.get("content_sha256") or "")) != 64:
            provenance_errors.append(f"{ticker}:{metric}: missing content SHA-256")
        if not str(candidate.get("evidence_text") or "").strip():
            provenance_errors.append(f"{ticker}:{metric}: missing evidence text")
        if status != "ACCEPTED":
            review_rows.append(
                {
                    "ticker": ticker,
                    "calibration_cohort": cohort,
                    "industry": candidate["industry"],
                    "filing_date": candidate["filing_date"],
                    "form_type": candidate["form_type"],
                    "accession_number": candidate["accession_number"],
                    "document_name": candidate["document_name"],
                    "metric_name": metric,
                    "concept_name": candidate["concept_name"],
                    "candidate_value": (
                        candidate["candidate_value"]
                        if candidate["candidate_value"] is not None
                        else ""
                    ),
                    "unit": candidate["unit"],
                    "confidence": candidate["confidence"],
                    "candidate_status": status,
                    "status_reason": candidate["status_reason"],
                    "evidence_text": candidate["evidence_text"],
                    "source_url": provenance.get("source_url", ""),
                    "content_sha256": provenance.get("content_sha256", ""),
                }
            )
    errors.extend(provenance_errors)
    gates["candidate_provenance_and_status"] = {
        "status": "PASS" if not provenance_errors else "FAIL",
        "candidate_count": len(candidates),
        "candidate_ticker_count": len(candidate_tickers),
        "accepted_candidate_count": sum(
            row["candidate_status"] == "ACCEPTED" for row in candidates
        ),
        "review_candidate_count": len(review_rows),
        "errors": provenance_errors[:50],
    }
    cohort_coverage: dict[str, dict[str, Any]] = {}
    cohort_gate_errors: list[str] = []
    active_by_cohort: dict[str, set[str]] = {}
    for member in member_rows:
        if int(member["active_flag"]):
            active_by_cohort.setdefault(
                str(member["calibration_cohort_id"]), set()
            ).add(str(member["ticker"]))
    for cohort in sorted(SUPPORTED_METRICS_BY_COHORT):
        active = active_by_cohort.get(cohort, set())
        candidate_active = candidate_tickers_by_cohort.get(cohort, set()) & active
        accepted_active = accepted_tickers_by_cohort.get(cohort, set()) & active
        observed_signal = (
            len(accepted_active)
            if cohort != "development_stage_and_speculative_transport"
            else len(candidate_active)
        )
        if observed_signal == 0:
            cohort_gate_errors.append(f"{cohort}: zero supported candidate signal")
        cohort_coverage[cohort] = {
            "active_ticker_count": len(active),
            "candidate_ticker_count": len(candidate_active),
            "accepted_ticker_count": len(accepted_active),
            "candidate_ticker_coverage": round(_ratio(len(candidate_active), len(active)), 6),
            "accepted_ticker_coverage": round(_ratio(len(accepted_active), len(active)), 6),
            "accepted_metrics": sorted(accepted_metrics_by_cohort.get(cohort, set())),
        }
    errors.extend(cohort_gate_errors)
    gates["cohort_metric_signal"] = {
        "status": "PASS" if not cohort_gate_errors else "FAIL",
        "coverage": cohort_coverage,
        "errors": cohort_gate_errors,
    }
    scale_failures: list[str] = []
    for cohort in (
        "surface_freight_and_logistics",
        "air_transport_and_aviation_services",
        "marine_shipping_and_maritime",
    ):
        coverage = float(cohort_coverage[cohort]["accepted_ticker_coverage"])
        if coverage < minimum_scale_coverage:
            scale_failures.append(
                f"{cohort}: accepted ticker coverage={coverage:.4f} "
                f"minimum={minimum_scale_coverage:.4f}"
            )
    scale_ready = (
        not scale_failures
        and active_document_coverage >= minimum_active_document_coverage
        and not errors
    )
    if scale_failures:
        warnings.append(
            "Do not begin full historical specialized-disclosure parsing until "
            "parser expansion or reviewed aliases close the scale-readiness gaps."
        )
    gates["historical_scale_decision"] = {
        "status": "READY_FOR_BOUNDED_HISTORICAL_BACKFILL"
        if scale_ready
        else "PARSER_EXPANSION_REQUIRED",
        "minimum_accepted_ticker_coverage": minimum_scale_coverage,
        "failures": scale_failures,
    }
    status_counts = Counter(str(row.get("status") or "") for row in rows)
    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "asof_date": args.asof,
        "model_family": MODEL_FAMILY,
        "database_path": str(db_path),
        "sync_output_csv": str(input_path),
        "review_output_csv": str(review_path),
        "sync_status_counts": dict(sorted(status_counts.items())),
        "gates": gates,
        "errors": errors,
        "warnings": warnings,
    }
    write_csv_atomic(review_path, REVIEW_FIELDS, review_rows)
    write_manifest(output_path, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
