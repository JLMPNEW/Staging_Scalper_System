from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dedicated_parser.catalog import accession_directory, relevant_document_names
from dedicated_parser.contracts import FilingRef, file_sha256
from technology.software_infrastructure.dedicated_parser_baseline import (
    MODEL_FAMILY,
    MetricDefinition,
    MetricRegistry,
    UniverseMember,
    table_columns,
)


def _placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("At least one value is required")
    return ",".join("?" for _ in values)


def _canonical_coverage(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    metrics: tuple[MetricDefinition, ...],
    asof_date: str,
) -> dict[str, set[str]]:
    canonical_to_targets: dict[str, set[str]] = defaultdict(set)
    for metric in metrics:
        for canonical in metric.canonical_metrics:
            canonical_to_targets[canonical.lower()].add(metric.metric_name)
    coverage: dict[str, set[str]] = defaultdict(set)
    if not canonical_to_targets:
        return coverage
    canonical_names = sorted(canonical_to_targets)
    rows = conn.execute(
        f"""
        SELECT DISTINCT ticker, LOWER(metric_name) AS metric_name
        FROM fact_sec_xbrl_fact
        WHERE ticker IN ({_placeholders(tickers)})
          AND LOWER(metric_name) IN ({_placeholders(canonical_names)})
          AND COALESCE(filing_date, '') <= ?
          AND value IS NOT NULL
        """,
        (*tickers, *canonical_names, asof_date),
    ).fetchall()
    for row in rows:
        ticker = str(row["ticker"]).upper()
        for target in canonical_to_targets[str(row["metric_name"])]:
            coverage[target].add(ticker)
    return coverage


def _raw_concept_coverage(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    metrics: tuple[MetricDefinition, ...],
    asof_date: str,
) -> dict[str, set[str]]:
    coverage: dict[str, set[str]] = defaultdict(set)
    rows = conn.execute(
        f"""
        SELECT DISTINCT ticker, LOWER(concept) AS concept
        FROM fact_sec_xbrl_fact_raw
        WHERE ticker IN ({_placeholders(tickers)})
          AND COALESCE(filing_date, '') <= ?
          AND value IS NOT NULL
        """,
        (*tickers, asof_date),
    )
    token_map = {
        metric.metric_name: metric.concept_tokens
        for metric in metrics
        if metric.concept_tokens
    }
    for row in rows:
        ticker = str(row["ticker"]).upper()
        concept = str(row["concept"] or "").lower()
        for metric_name, tokens in token_map.items():
            if any(token in concept for token in tokens):
                coverage[metric_name].add(ticker)
    return coverage


def _feature_coverage(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    metrics: tuple[MetricDefinition, ...],
    asof_date: str,
) -> dict[str, set[str]]:
    coverage: dict[str, set[str]] = defaultdict(set)
    available_columns = table_columns(conn, "feature_financial_statement")
    for metric in metrics:
        fields = [
            field
            for field in metric.feature_fields
            if field in available_columns and field.isidentifier()
        ]
        if not fields:
            continue
        non_null_clause = " OR ".join(f"{field} IS NOT NULL" for field in fields)
        rows = conn.execute(
            f"""
            SELECT DISTINCT ticker
            FROM feature_financial_statement
            WHERE model_family = ?
              AND ticker IN ({_placeholders(tickers)})
              AND asof_date <= ?
              AND ({non_null_clause})
            """,
            (MODEL_FAMILY, *tickers, asof_date),
        ).fetchall()
        coverage[metric.metric_name].update(str(row["ticker"]).upper() for row in rows)
    return coverage


def build_metric_gap_census(
    conn: sqlite3.Connection,
    *,
    registry: MetricRegistry,
    members: list[UniverseMember],
    asof_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tickers = [member.ticker for member in members]
    if not tickers:
        raise RuntimeError("Software-infrastructure historical universe is empty")
    canonical = _canonical_coverage(
        conn,
        tickers=tickers,
        metrics=registry.metrics,
        asof_date=asof_date,
    )
    raw = _raw_concept_coverage(
        conn,
        tickers=tickers,
        metrics=registry.metrics,
        asof_date=asof_date,
    )
    feature = _feature_coverage(
        conn,
        tickers=tickers,
        metrics=registry.metrics,
        asof_date=asof_date,
    )
    detail: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    member_by_ticker = {member.ticker: member for member in members}
    for metric in registry.metrics:
        canonical_tickers = canonical[metric.metric_name]
        raw_tickers = raw[metric.metric_name]
        feature_tickers = feature[metric.metric_name]
        satisfied = canonical_tickers | raw_tickers | feature_tickers
        parser_candidates = set(tickers) - satisfied
        for ticker in tickers:
            member = member_by_ticker[ticker]
            sources: list[str] = []
            if ticker in canonical_tickers:
                sources.append("canonical_xbrl")
            if ticker in raw_tickers:
                sources.append("raw_xbrl_concept")
            if ticker in feature_tickers:
                sources.append("financial_feature")
            detail.append(
                {
                    "asof_date": asof_date,
                    "model_family": MODEL_FAMILY,
                    "registry_version": registry.registry_version,
                    "ticker": ticker,
                    "cik": member.cik,
                    "cohort_id": member.cohort_id,
                    "membership_status": member.membership_status,
                    "point_in_time_flag": member.point_in_time_flag,
                    "metric_name": metric.metric_name,
                    "definition_version": metric.definition_version,
                    "tier": metric.tier,
                    "extraction_policy": metric.extraction_policy,
                    "baseline_available_flag": int(bool(sources)),
                    "baseline_sources": "|".join(sources),
                    "parser_candidate_flag": int(ticker in parser_candidates),
                    "baseline_status": "available" if sources else "parser_candidate",
                }
            )
        total = len(tickers)
        summary.append(
            {
                "asof_date": asof_date,
                "model_family": MODEL_FAMILY,
                "registry_version": registry.registry_version,
                "metric_name": metric.metric_name,
                "definition_version": metric.definition_version,
                "tier": metric.tier,
                "extraction_policy": metric.extraction_policy,
                "historical_universe_ticker_count": total,
                "canonical_xbrl_ticker_count": len(canonical_tickers),
                "raw_xbrl_concept_ticker_count": len(raw_tickers),
                "financial_feature_ticker_count": len(feature_tickers),
                "any_baseline_ticker_count": len(satisfied),
                "missing_ticker_count": total - len(satisfied),
                "parser_candidate_ticker_count": len(parser_candidates),
                "baseline_coverage_pct": round(100.0 * len(satisfied) / total, 4),
            }
        )
    return summary, detail


def build_applicability_census(
    *,
    applicability_rows: list[dict[str, str]],
    members: list[UniverseMember],
    asof_date: str,
) -> list[dict[str, Any]]:
    cohort_counts = Counter(member.cohort_id for member in members)
    return [
        {
            "asof_date": asof_date,
            "model_family": MODEL_FAMILY,
            **row,
            "historical_universe_ticker_count": cohort_counts[row["cohort_id"]],
        }
        for row in applicability_rows
    ]


def build_filing_census(
    conn: sqlite3.Connection,
    *,
    members: list[UniverseMember],
    registry: MetricRegistry,
    history_start_date: str,
    asof_date: str,
) -> list[dict[str, Any]]:
    tickers = [member.ticker for member in members]
    forms = list(registry.filing_forms)
    rows = conn.execute(
        f"""
        SELECT
            UPPER(form_type) AS form_type,
            COUNT(*) AS filing_count,
            COUNT(DISTINCT ticker) AS ticker_count,
            MIN(filing_date) AS first_filing_date,
            MAX(filing_date) AS latest_filing_date
        FROM fact_sec_filing
        WHERE ticker IN ({_placeholders(tickers)})
          AND UPPER(form_type) IN ({_placeholders(forms)})
          AND filing_date BETWEEN ? AND ?
        GROUP BY UPPER(form_type)
        ORDER BY UPPER(form_type)
        """,
        (*tickers, *forms, history_start_date, asof_date),
    ).fetchall()
    return [
        {
            "asof_date": asof_date,
            "model_family": MODEL_FAMILY,
            "form_type": str(row["form_type"]),
            "filing_count": int(row["filing_count"]),
            "ticker_count": int(row["ticker_count"]),
            "first_filing_date": str(row["first_filing_date"] or ""),
            "latest_filing_date": str(row["latest_filing_date"] or ""),
        }
        for row in rows
    ]


def _atomic_write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"Refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def write_baseline_outputs(
    *,
    output_dir: Path,
    registry: MetricRegistry,
    asof_date: str,
    members: list[UniverseMember],
    metric_summary: list[dict[str, Any]],
    metric_detail: list[dict[str, Any]],
    applicability_census: list[dict[str, Any]],
    filing_census: list[dict[str, Any]],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "metric_gap_census": output_dir / "software_metric_gap_census.csv",
        "metric_ticker_census": output_dir / "software_metric_ticker_census.csv",
        "metric_applicability_census": output_dir / "software_metric_applicability_census.csv",
        "filing_scope_census": output_dir / "software_filing_scope_census.csv",
    }
    _atomic_write_csv(files["metric_gap_census"], metric_summary)
    _atomic_write_csv(files["metric_ticker_census"], metric_detail)
    _atomic_write_csv(files["metric_applicability_census"], applicability_census)
    _atomic_write_csv(files["filing_scope_census"], filing_census)
    manifest = {
        "manifest_version": "software_parser_baseline_manifest_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asof_date": asof_date,
        "model_family": MODEL_FAMILY,
        "registry_version": registry.registry_version,
        "execution_mode": "read_only_baseline_census",
        "production_facts_modified_flag": 0,
        "production_scores_modified_flag": 0,
        "historical_universe_ticker_count": len(members),
        "historical_member_ticker_count": sum(
            member.membership_status != "active" for member in members
        ),
        "metric_count": len(registry.metrics),
        "files": {
            name: {
                "path": str(path.resolve()),
                "sha256": file_sha256(path),
            }
            for name, path in files.items()
        },
    }
    manifest_path = output_dir / "software_parser_baseline_manifest.json"
    _atomic_write_json(manifest_path, manifest)
    manifest["manifest_path"] = str(manifest_path.resolve())
    return manifest


def _load_scoped_filings(
    conn: sqlite3.Connection,
    *,
    members: list[UniverseMember],
    registry: MetricRegistry,
    history_start_date: str,
    asof_date: str,
) -> list[sqlite3.Row]:
    tickers = [member.ticker for member in members]
    forms = list(registry.filing_forms)
    return conn.execute(
        f"""
        SELECT
            ticker,
            cik,
            accession_number,
            source_id,
            UPPER(form_type) AS form_type,
            COALESCE(filing_date, '') AS filing_date,
            COALESCE(report_date, '') AS report_date,
            COALESCE(acceptance_datetime, '') AS accepted_at,
            COALESCE(primary_document, '') AS primary_document
        FROM fact_sec_filing
        WHERE ticker IN ({_placeholders(tickers)})
          AND UPPER(form_type) IN ({_placeholders(forms)})
          AND filing_date BETWEEN ? AND ?
          AND SUBSTR(COALESCE(acceptance_datetime, filing_date), 1, 10) <= ?
        ORDER BY ticker, filing_date, accession_number
        """,
        (*tickers, *forms, history_start_date, asof_date, asof_date),
    ).fetchall()


def build_source_scope_rows(
    conn: sqlite3.Connection,
    *,
    cache_dir: Path,
    registry: MetricRegistry,
    members: list[UniverseMember],
    history_start_date: str,
    asof_date: str,
) -> list[dict[str, Any]]:
    filings = _load_scoped_filings(
        conn,
        members=members,
        registry=registry,
        history_start_date=history_start_date,
        asof_date=asof_date,
    )
    output: list[dict[str, Any]] = []
    keyword_set = tuple(
        dict.fromkeys(
            token
            for metric in registry.metrics
            for token in (metric.metric_name, *metric.concept_tokens)
        )
    )
    for row in filings:
        filing = FilingRef(
            ticker=str(row["ticker"]).upper(),
            cik=str(row["cik"] or "").strip().zfill(10),
            accession_number=str(row["accession_number"] or "").strip(),
            form_type=str(row["form_type"]),
            filing_date=str(row["filing_date"]),
            accepted_at=str(row["accepted_at"]),
            report_date=str(row["report_date"]),
            primary_document=str(row["primary_document"]),
            source_id=str(row["source_id"]),
        )
        accession_dir = accession_directory(cache_dir, filing)
        document_names: tuple[str, ...] = ()
        if accession_dir.is_dir():
            document_names = relevant_document_names(
                accession_dir,
                filing=filing,
                keywords=keyword_set,
            )
        if document_names:
            for document_name in document_names:
                document_path = accession_dir / document_name
                output.append(
                    {
                        "model_family": MODEL_FAMILY,
                        "asof_date": asof_date,
                        "ticker": filing.ticker,
                        "cik": filing.cik,
                        "accession_number": filing.accession_number,
                        "form_type": filing.form_type,
                        "filing_date": filing.filing_date,
                        "accepted_at": filing.accepted_at,
                        "document_name": document_name,
                        "source_path": str(document_path.resolve()),
                        "content_sha256": file_sha256(document_path),
                        "cache_status": "CACHED_HASHED",
                        "parser_ready_flag": 1,
                        "scope_status": "SEALED_SOURCE_DOCUMENT",
                    }
                )
            continue
        expected_document = filing.primary_document or "__DOCUMENT_DISCOVERY_REQUIRED__"
        output.append(
            {
                "model_family": MODEL_FAMILY,
                "asof_date": asof_date,
                "ticker": filing.ticker,
                "cik": filing.cik,
                "accession_number": filing.accession_number,
                "form_type": filing.form_type,
                "filing_date": filing.filing_date,
                "accepted_at": filing.accepted_at,
                "document_name": expected_document,
                "source_path": str((accession_dir / expected_document).resolve()),
                "content_sha256": "",
                "cache_status": "MISSING_CACHE",
                "parser_ready_flag": 0,
                "scope_status": "UNSEALED_SOURCE_SCOPE",
            }
        )
    return output


def write_source_scope_outputs(
    *,
    output_dir: Path,
    registry: MetricRegistry,
    members: list[UniverseMember],
    rows: list[dict[str, Any]],
    asof_date: str,
    cache_dir: Path,
) -> dict[str, Any]:
    if not rows:
        raise RuntimeError("No in-scope SEC filings were found for the software universe")
    output_dir.mkdir(parents=True, exist_ok=True)
    scope_path = output_dir / "software_infrastructure_parser_source_scope.csv"
    _atomic_write_csv(scope_path, rows)
    cache_counts = Counter(str(row["cache_status"]) for row in rows)
    manifest = {
        "manifest_version": "software_parser_source_scope_manifest_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asof_date": asof_date,
        "model_family": MODEL_FAMILY,
        "registry_version": registry.registry_version,
        "scope_status": (
            "SEALED_SOURCE_MANIFEST"
            if cache_counts["MISSING_CACHE"] == 0
            else "UNSEALED_SOURCE_SCOPE"
        ),
        "parser_execution_allowed_flag": int(cache_counts["MISSING_CACHE"] == 0),
        "production_facts_modified_flag": 0,
        "production_scores_modified_flag": 0,
        "historical_universe_ticker_count": len(members),
        "filing_count": len({(row["ticker"], row["accession_number"]) for row in rows}),
        "document_row_count": len(rows),
        "cached_hashed_document_count": cache_counts["CACHED_HASHED"],
        "missing_cache_document_count": cache_counts["MISSING_CACHE"],
        "cache_dir": str(cache_dir.resolve()),
        "source_scope_path": str(scope_path.resolve()),
        "source_scope_sha256": file_sha256(scope_path),
    }
    manifest_path = output_dir / "software_infrastructure_parser_source_scope_manifest.json"
    _atomic_write_json(manifest_path, manifest)
    manifest["manifest_path"] = str(manifest_path.resolve())
    return manifest
