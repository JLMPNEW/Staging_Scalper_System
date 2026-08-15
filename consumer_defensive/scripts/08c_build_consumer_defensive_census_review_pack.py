#!/usr/bin/env python3
"""Build and validate a stratified Consumer Defensive census review pack."""

from __future__ import annotations

# ruff: noqa: E402

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.db import connect
from consumer_defensive.core.market_data import write_csv, write_json
from consumer_defensive.core.script_runtime import iso_date, stage4_output_dir
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage4 import (
    DISCLOSURE_SOURCE,
    MODEL_FAMILY,
    _cache_only_sec_preflight,
    _issuer_rows,
    _issuer_scope_sha256,
    _sec_ingestion_config_sha256,
    _sealed_cache_lookup,
    bootstrap_stage4,
)

DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
VERDICTS = {
    "true_positive", "false_positive", "true_negative", "false_negative",
    "unavailable", "not_applicable",
}
ACTIONS = {"retain", "narrow", "expand", "prohibit", "no_change"}
ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--adjudications",
        type=Path,
        default=None,
        help="Optional completed ledger to validate against the generated sample.",
    )
    return parser.parse_args()


def _metric_family(metric_id: str) -> str:
    if any(token in metric_id for token in ("leverage", "debt", "fixed_charge")):
        return "leverage"
    if any(token in metric_id for token in ("margin", "cost", "shrink", "tax", "advertising", "gross_profit")):
        return "margin_cost"
    if any(token in metric_id for token in ("distribution", "store", "square_foot", "capacity")):
        return "distribution_store"
    if any(token in metric_id for token in ("customer", "traffic", "representative", "digital")):
        return "customer_channel"
    if any(token in metric_id for token in ("price", "mix", "ticket", "market_share")):
        return "pricing_mix"
    return "demand_volume"


def _stable_hash(values: list[Any]) -> str:
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _candidate_rows(
    conn: Any,
    *,
    asof_date: str,
    parser_version: str,
    sealed_logical_paths: set[str],
) -> list[dict[str, str]]:
    cutoff = asof_date + "T23:59:59Z"
    latest_documents: dict[str, tuple[str, str, str]] = {}
    for row in conn.execute(
        """SELECT d.issuer_ticker,b.form_type,d.accepted_at,d.accession_number,
                  d.issuer_cik,d.primary_document
           FROM bridge_sec_filing_document_company d
           JOIN bridge_sec_filing_company b
             ON b.accession_number=d.accession_number
            AND b.issuer_company_id=d.issuer_company_id
           WHERE d.hydration_status='hydrated' AND d.accepted_at<=?
             AND COALESCE((SELECT e.event_type
                 FROM sec_filing_company_association_event e
                 WHERE e.accession_number=b.accession_number
                   AND e.issuer_company_id=b.issuer_company_id
                   AND e.effective_asof<=?
                 ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                 CASE WHEN b.association_status='active' THEN 'observed' ELSE 'retired' END)
                 IN ('observed','reactivated')
           ORDER BY d.issuer_ticker,d.accepted_at DESC,d.accession_number DESC""",
        (cutoff, cutoff),
    ):
        logical_path = (
            f"filings/{str(row[4]).zfill(10)}/{str(row[3])}/{str(row[5])}"
        )
        if logical_path not in sealed_logical_paths:
            continue
        latest_documents.setdefault(
            str(row[0]), (str(row[1]), str(row[2]), str(row[3]))
        )
    evidence: dict[tuple[str, str], tuple[str, str, str, str, str]] = {}
    for row in conn.execute(
        """SELECT ticker,metric_id,form_type,accepted_at,accession_number,
                  evidence_json,matched_terms_json
           FROM fact_specialized_metric_disclosure_census
           WHERE parser_version=? AND source_id=? AND accepted_at<=?
           ORDER BY ticker,metric_id,hit_count DESC,accepted_at DESC,
                    accession_number DESC""",
        (parser_version, DISCLOSURE_SOURCE, cutoff),
    ):
        evidence.setdefault(
            (str(row[0]), str(row[1])),
            (str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6])),
        )
    result: list[dict[str, str]] = []
    summary_rows = conn.execute(
        """SELECT s.ticker,s.metric_id,s.calibration_cohort_id,
                  s.applicability_subtype,s.disclosure_status,c.is_active,
                  p.foreign_issuer_flag
           FROM fact_specialized_metric_disclosure_summary s
           JOIN dim_consumer_defensive_taxonomy t ON t.ticker=s.ticker
           JOIN dim_company c ON c.company_id=t.company_id
           JOIN dim_issuer_reporting_profile p ON p.ticker=s.ticker
           WHERE t.model_family=? AND s.asof_date=? AND s.parser_version=?
             AND s.source_id=?
             AND s.disclosure_status IN ('applicable_term_hit','applicable_no_term_hit')
           ORDER BY s.calibration_cohort_id,s.disclosure_status,s.ticker,s.metric_id""",
        (MODEL_FAMILY, asof_date, parser_version, DISCLOSURE_SOURCE),
    ).fetchall()
    for row in summary_rows:
        ticker, metric_id = str(row[0]), str(row[1])
        identity = evidence.get((ticker, metric_id))
        if identity:
            form, accepted_at, accession, evidence_json, matched_terms = identity
        else:
            form, accepted_at, accession = latest_documents.get(ticker, ("", "", ""))
            evidence_json, matched_terms = "[]", "[]"
        status = str(row[4])
        result.append({
            "asof_date": asof_date,
            "ticker": ticker,
            "security_role": "active" if int(row[5]) else "historical_delisted",
            "cohort_id": str(row[2]),
            "applicability_subtype": str(row[3]),
            "metric_id": metric_id,
            "metric_family": _metric_family(metric_id),
            "reporting_profile": "foreign_private_issuer" if int(row[6]) else "domestic_issuer",
            "form": form,
            "form_family": "annual" if form.upper() in ANNUAL_FORMS else "interim",
            "accepted_at": accepted_at,
            "accession_number": accession,
            "census_status": status,
            "matched_terms_json": matched_terms,
            "evidence_hash": _stable_hash([
                asof_date, ticker, metric_id, status, accession, accepted_at,
                evidence_json, matched_terms,
            ]),
            "review_verdict": "",
            "term_action": "",
            "review_notes": "",
        })
    return result


def _stratified_sample(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    keys: set[tuple[str, str]] = set()

    def add(row: dict[str, str]) -> None:
        key = (row["ticker"], row["metric_id"])
        if key not in keys:
            keys.add(key)
            selected.append(row)

    cohorts = sorted({row["cohort_id"] for row in candidates})
    for cohort in cohorts:
        for status in ("applicable_term_hit", "applicable_no_term_hit"):
            matches = [
                row for row in candidates
                if row["cohort_id"] == cohort and row["census_status"] == status
            ]
            if matches:
                matches.sort(key=lambda row: (
                    row["security_role"] != "historical_delisted",
                    row["reporting_profile"] != "foreign_private_issuer",
                    row["ticker"], row["metric_id"],
                ))
                add(matches[0])
    required_dimensions = {
        "metric_family": sorted({_metric_family(row["metric_id"]) for row in candidates}),
        "security_role": ["active", "historical_delisted"],
        "reporting_profile": ["domestic_issuer", "foreign_private_issuer"],
        "form_family": ["annual", "interim"],
    }
    for field, values in required_dimensions.items():
        for value in values:
            if any(row[field] == value for row in selected):
                continue
            matches = [row for row in candidates if row[field] == value]
            if matches:
                matches.sort(key=lambda row: (
                    row["cohort_id"], row["census_status"], row["ticker"], row["metric_id"]
                ))
                add(matches[0])
    selected.sort(key=lambda row: (
        row["cohort_id"], row["census_status"], row["ticker"], row["metric_id"]
    ))
    return selected


def _read_adjudications(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{key: str(value or "").strip() for key, value in row.items()} for row in csv.DictReader(handle)]


def _validate_adjudications(
    sample: list[dict[str, str]], adjudications: list[dict[str, str]]
) -> dict[str, Any]:
    expected = {(row["ticker"], row["metric_id"], row["evidence_hash"]) for row in sample}
    actual = {(row.get("ticker", ""), row.get("metric_id", ""), row.get("evidence_hash", "")) for row in adjudications}
    errors: list[str] = []
    if actual != expected:
        errors.append("adjudication_keyset_does_not_match_review_sample")
    if len(adjudications) != len(actual):
        errors.append("duplicate_adjudication_key")
    if len(adjudications) != len(sample):
        errors.append("adjudication_row_count_does_not_match_review_sample")
    for row in adjudications:
        if row.get("review_verdict") not in VERDICTS:
            errors.append(f"invalid_review_verdict:{row.get('ticker')}:{row.get('metric_id')}")
        if row.get("term_action") not in ACTIONS:
            errors.append(f"invalid_term_action:{row.get('ticker')}:{row.get('metric_id')}")
        if not row.get("review_notes"):
            errors.append(f"missing_review_notes:{row.get('ticker')}:{row.get('metric_id')}")
    verdicts = Counter(row.get("review_verdict", "") for row in adjudications)
    actions = Counter(row.get("term_action", "") for row in adjudications)
    return {
        "status": "PASS" if not errors else "FAIL",
        "rows": len(adjudications),
        "errors": sorted(set(errors)),
        "verdict_counts": dict(sorted(verdicts.items())),
        "term_action_counts": dict(sorted(actions.items())),
    }


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    output_dir = stage4_output_dir(bundle, as_of=args.as_of, override=args.output_dir)
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        bootstrap_stage4(conn, bundle)
        issuers = _issuer_rows(conn, None)
        settings = cfg_get(bundle.payload, "sec_fundamentals")
        _cache_only_sec_preflight(
            conn,
            asof_date=args.as_of,
            ingestion_config_sha256=_sec_ingestion_config_sha256(settings),
            issuer_scope_sha256=_issuer_scope_sha256(issuers),
            scope_issuer_count=len(issuers),
        )
        cache_root = resolve_path(settings["cache_dir"], base_dir=bundle.base_dir)
        sealed = _sealed_cache_lookup(conn, cache_root, args.as_of)
        parser_version = str(cfg_get(
            bundle.payload, "specialized_disclosure_census.parser_version"
        ))
        candidates = _candidate_rows(
            conn,
            asof_date=args.as_of,
            parser_version=parser_version,
            sealed_logical_paths=set(sealed),
        )
        sample = _stratified_sample(candidates)
    if not sample:
        raise RuntimeError("Census review sample is empty.")
    sample_path = output_dir / "census_terminology_review_sample.csv"
    template_path = output_dir / "census_terminology_adjudication_template.csv"
    write_csv(sample_path, sample)
    write_csv(template_path, sample)
    manifest = {
        "status": "PENDING_ADJUDICATION",
        "as_of": args.as_of,
        "database": str(db_path),
        "parser_version": parser_version,
        "candidate_rows": len(candidates),
        "sample_rows": len(sample),
        "sealed_input_files": len(sealed),
        "sample_sha256": hashlib.sha256(sample_path.read_bytes()).hexdigest(),
        "sample": str(sample_path),
        "adjudication_template": str(template_path),
        "coverage": {
            field: dict(sorted(Counter(row[field] for row in sample).items()))
            for field in (
                "cohort_id", "census_status", "security_role",
                "reporting_profile", "form_family", "metric_family",
            )
        },
    }
    exit_code = 0
    if args.adjudications:
        adjudication_path = args.adjudications.resolve(strict=True)
        validation = _validate_adjudications(
            sample, _read_adjudications(adjudication_path)
        )
        manifest["adjudications"] = str(adjudication_path)
        manifest["adjudications_sha256"] = hashlib.sha256(
            adjudication_path.read_bytes()
        ).hexdigest()
        manifest["adjudication_validation"] = validation
        manifest["status"] = "ADJUDICATED" if validation["status"] == "PASS" else "INVALID_ADJUDICATIONS"
        exit_code = 0 if validation["status"] == "PASS" else 1
    manifest_path = output_dir / "census_terminology_review_manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps({**manifest, "manifest": str(manifest_path)}, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
