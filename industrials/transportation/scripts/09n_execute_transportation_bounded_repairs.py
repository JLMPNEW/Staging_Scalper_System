#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.bounded_repair import (  # noqa: E402
    BOUNDED_REPAIR_EXECUTION_VERSION,
    BOUNDED_REPAIR_SCOPE_VERSION,
    FINANCIAL_EXECUTION_FIELDS,
    NO_VALUE_AUDIT_FIELDS,
    OCR_EXECUTION_FIELDS,
    SOURCE_GAP_SEARCH_FIELDS,
    apply_cached_financial_source_overrides,
    audit_no_value_pairs,
    execute_financial_repairs,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
    read_only_connection,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


_NUMBER = re.compile(
    r"(?<![A-Za-z])(?:[$€£]\s*)?"
    r"\(?-?\d[\d,]*(?:\.\d+)?\)?\s*%?"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Execute only the hash-sealed transportation bounded repairs: "
            "safe aligned financial formulas, existing-cache source search, "
            "stored-evidence audit, and local OCR capability detection."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--coverage-prefix",
        default="transportation_all_source_union",
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _artifact_is_sealed(
    manifest: Mapping[str, Any],
    name: str,
    path: Path,
) -> bool:
    artifact = (manifest.get("artifacts") or {}).get(name) or {}
    return (
        str(artifact.get("path") or "") == str(path.resolve())
        and str(artifact.get("sha256") or "") == file_sha256(path)
    )


def _latest_features(
    connection: Any,
    *,
    tickers: Sequence[str],
    asof_date: str,
) -> dict[str, dict[str, object]]:
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    output: dict[str, dict[str, object]] = {}
    for row in connection.execute(
        f"""
        SELECT feature.*
        FROM feature_financial_statement AS feature
        WHERE feature.model_family=?
          AND feature.ticker IN ({placeholders})
          AND feature.asof_date=(
              SELECT MAX(candidate.asof_date)
              FROM feature_financial_statement AS candidate
              WHERE candidate.model_family=feature.model_family
                AND candidate.ticker=feature.ticker
                AND candidate.asof_date<=?
          )
        ORDER BY feature.ticker, feature.source_id
        """,
        (MODEL_FAMILY, *tickers, asof_date),
    ):
        output.setdefault(str(row["ticker"]).upper(), dict(row))
    return output


def _stored_evidence(
    connection: Any,
    *,
    evaluation_id: int,
    run_ids: Sequence[int],
) -> dict[tuple[str, str], list[dict[str, object]]]:
    by_key: dict[str, dict[str, object]] = {}
    for row in connection.execute(
        """
        SELECT evaluated_evidence_key AS evidence_key, ticker,
               metric_name, evidence_text, candidate_value,
               candidate_status, source_document
        FROM sec_parser_review_evidence
        WHERE evaluation_id=?
        ORDER BY ticker, metric_name, evaluated_evidence_key
        """,
        (evaluation_id,),
    ):
        by_key[str(row["evidence_key"])] = dict(row)
    if run_ids:
        placeholders = ",".join("?" for _ in run_ids)
        for row in connection.execute(
            f"""
            SELECT evidence.evidence_key, evidence.ticker,
                   evidence.metric_name, evidence.evidence_text,
                   evidence.candidate_value,
                   evidence.candidate_status,
                   evidence.source_document
            FROM sec_parser_run_metric_evidence AS relation
            JOIN sec_parser_metric_evidence_shadow AS evidence
              ON evidence.evidence_key=relation.evidence_key
            WHERE relation.run_id IN ({placeholders})
              AND evidence.model_family=?
            ORDER BY evidence.ticker, evidence.metric_name,
                     evidence.evidence_key
            """,
            (*run_ids, MODEL_FAMILY),
        ):
            by_key.setdefault(str(row["evidence_key"]), dict(row))
    output: dict[
        tuple[str, str], list[dict[str, object]]
    ] = defaultdict(list)
    for row in by_key.values():
        output[
            (
                str(row["ticker"]).upper(),
                str(row["metric_name"]),
            )
        ].append(row)
    return dict(output)


def _load_cache_text(
    path: Path,
    *,
    expected_hash: str,
) -> str:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if (
        not isinstance(payload, dict)
        or str(payload.get("content_sha256") or "").lower()
        != expected_hash.lower()
    ):
        return ""
    return str(payload.get("text") or "")


def _search_cached_sources(
    *,
    pair_rows: Sequence[Mapping[str, str]],
    source_rows: Sequence[Mapping[str, str]],
    cache_rows: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, object]], int]:
    source_hashes: dict[str, set[str]] = defaultdict(set)
    for row in source_rows:
        source_hashes[str(row["ticker"]).upper()].add(
            str(row["content_sha256"]).lower()
        )
    cache_paths = {
        str(row["content_sha256"]).lower(): Path(row["cache_path"])
        for row in cache_rows
        if str(row.get("cache_path") or "")
        and str(row.get("cache_status") or "")
        != "CACHE_VALIDATED_EMPTY_PYMUPDF"
    }
    text_cache: dict[str, str] = {}
    output: list[dict[str, object]] = []
    for pair in pair_rows:
        if pair["repair_classification"] != "SOURCE_OR_PERIOD_GAP":
            continue
        ticker = str(pair["ticker"]).upper()
        terms = tuple(
            term.strip().lower()
            for term in str(pair.get("search_terms") or "").split("|")
            if term.strip()
        )
        searched: list[str] = []
        matched_hashes: set[str] = set()
        matched_terms: set[str] = set()
        numeric_proximity_count = 0
        for content_hash in sorted(source_hashes.get(ticker, ())):
            cache_file = cache_paths.get(content_hash)
            if cache_file is None or not cache_file.is_file():
                continue
            searched.append(content_hash)
            if content_hash not in text_cache:
                text_cache[content_hash] = _load_cache_text(
                    cache_file,
                    expected_hash=content_hash,
                )
            text = text_cache[content_hash]
            lower = text.lower()
            for term in terms:
                start = 0
                while True:
                    index = lower.find(term, start)
                    if index < 0:
                        break
                    matched_hashes.add(content_hash)
                    matched_terms.add(term)
                    left = max(0, index - 250)
                    right = min(len(text), index + len(term) + 250)
                    if _NUMBER.search(text[left:right]):
                        numeric_proximity_count += 1
                    start = index + max(1, len(term))
        if numeric_proximity_count:
            status = "CACHED_PRIMARY_SOURCE_CANDIDATE_REVIEW"
            next_action = (
                "MANUAL_PERIOD_UNIT_ALIGNMENT_BEFORE_ANY_ACCEPTANCE"
            )
        else:
            status = "SEARCHED_CACHED_PRIMARY_SOURCES_NOT_FOUND"
            next_action = "TERMINAL_FOR_CURRENT_STORED_CORPUS"
        output.append(
            {
                "execution_version": BOUNDED_REPAIR_EXECUTION_VERSION,
                "pair_key": pair["pair_key"],
                "ticker": ticker,
                "metric_id": pair["metric_id"],
                "searched_content_hash_count": len(searched),
                "matched_content_hash_count": len(matched_hashes),
                "matched_term_count": len(matched_terms),
                "numeric_proximity_count": numeric_proximity_count,
                "matched_terms": "|".join(sorted(matched_terms)),
                "matched_content_hashes": "|".join(
                    sorted(matched_hashes)
                ),
                "execution_status": status,
                "required_next_action": next_action,
            }
        )
    return output, len(text_cache)


def _ocr_capability_rows(
    scope_rows: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, object]], str]:
    engine = shutil.which("tesseract") or ""
    output: list[dict[str, object]] = []
    for row in scope_rows:
        if row["repair_lane"] != "EMPTY_PDF_OCR":
            continue
        status = (
            "OCR_ENGINE_AVAILABLE_EXECUTION_NOT_AUTHORIZED"
            if engine
            else "OCR_ENGINE_UNAVAILABLE"
        )
        output.append(
            {
                "execution_version": BOUNDED_REPAIR_EXECUTION_VERSION,
                "content_sha256": row["content_sha256"],
                "ticker_contexts": row["ticker"],
                "document_name": row["document_name"],
                "scoped_metric_ids": row["metric_id"],
                "ocr_engine": engine,
                "execution_status": status,
                "coverage_override": "",
                "required_next_action": (
                    "AUTHORIZE_BOUNDED_LOCAL_OCR_ONLY_IF_PROMOTION_CRITICAL"
                    if engine
                    else "INSTALL_LOCAL_OCR_ONLY_IF_EXPECTED_VALUE_JUSTIFIES_COST"
                ),
            }
        )
    return output, engine


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    coverage_prefix = str(args.coverage_prefix).strip()
    if (
        not coverage_prefix
        or "/" in coverage_prefix
        or "\\" in coverage_prefix
    ):
        raise ValueError("--coverage-prefix must be a filename prefix")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Bounded repair execution requires general parser disabled"
        )
    foundation = resolve_foundation(config_path, args.db)
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=config_path.parent)
        / asof_date
    )
    scope_path = (
        output_dir / "transportation_bounded_repair_scope.csv"
    )
    scope_manifest_path = (
        output_dir / "transportation_bounded_repair_scope_manifest.json"
    )
    coverage_path = (
        output_dir / f"{coverage_prefix}_ticker_metric_coverage.csv"
    )
    coverage_manifest_path = (
        output_dir / f"{coverage_prefix}_coverage_manifest.json"
    )
    financial_path = (
        output_dir
        / "transportation_financial_repair_pair_contract.csv"
    )
    dependency_path = (
        output_dir
        / "transportation_financial_repair_dependency_contract.csv"
    )
    financial_manifest_path = (
        output_dir
        / "transportation_financial_repair_freeze_manifest.json"
    )
    source_path = (
        output_dir
        / "transportation_non_sec_direct_delta_source_manifest.csv"
    )
    cache_path = (
        output_dir / "transportation_content_text_cache_results.csv"
    )
    override_path = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "review_policies"
        / "transportation_bounded_financial_source_overrides.csv"
    )
    required = (
        scope_path,
        scope_manifest_path,
        coverage_path,
        coverage_manifest_path,
        financial_path,
        dependency_path,
        financial_manifest_path,
        source_path,
        cache_path,
        override_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing bounded-execution inputs: {missing}"
        )

    scope_manifest = _read_json(scope_manifest_path)
    coverage_manifest = _read_json(coverage_manifest_path)
    financial_manifest = _read_json(financial_manifest_path)
    errors: list[str] = []
    if (
        scope_manifest.get("acceptance") != "PASS"
        or scope_manifest.get("scope_version")
        != BOUNDED_REPAIR_SCOPE_VERSION
        or not _artifact_is_sealed(
            scope_manifest, "bounded_repair_scope", scope_path
        )
    ):
        errors.append("bounded repair scope is not sealed and passing")
    if (
        coverage_manifest.get("acceptance") != "PASS"
        or not _artifact_is_sealed(
            coverage_manifest, "ticker_metric_coverage", coverage_path
        )
    ):
        errors.append("base coverage is not sealed and passing")
    if (
        financial_manifest.get("acceptance") != "PASS"
        or not _artifact_is_sealed(
            financial_manifest,
            "financial_repair_pair_contract",
            financial_path,
        )
        or not _artifact_is_sealed(
            financial_manifest,
            "financial_repair_dependency_contract",
            dependency_path,
        )
    ):
        errors.append("financial repair inputs are not sealed")

    scope_rows = read_csv(scope_path)
    coverage_rows = read_csv(coverage_path)
    financial_pairs = read_csv(financial_path)
    dependencies = read_csv(dependency_path)
    source_rows = read_csv(source_path)
    cache_rows = read_csv(cache_path)
    override_rows = read_csv(override_path)
    tickers = sorted(
        {str(row["ticker"]).upper() for row in financial_pairs}
    )
    run_ids = sorted(
        {
            run_id
            for value in (
                coverage_manifest.get("delta_run_id"),
                coverage_manifest.get("repair_run_id"),
                coverage_manifest.get("direct_document_run_id"),
                *(coverage_manifest.get("additional_run_ids") or ()),
            )
            if (run_id := int(str(value or 0))) > 0
        }
    )
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        feature_rows = _latest_features(
            connection,
            tickers=tickers,
            asof_date=asof_date,
        )
        evidence_by_pair = _stored_evidence(
            connection,
            evaluation_id=int(
                coverage_manifest.get("base_review_evaluation_id") or 0
            ),
            run_ids=run_ids,
        )

    financial_results = execute_financial_repairs(
        pair_rows=financial_pairs,
        dependency_rows=dependencies,
        feature_rows=feature_rows,
    )
    financial_results, cached_override_count = (
        apply_cached_financial_source_overrides(
            financial_rows=financial_results,
            override_rows=override_rows,
        )
    )
    no_value_results = audit_no_value_pairs(
        coverage_rows=coverage_rows,
        evidence_by_pair=evidence_by_pair,
    )
    source_results, source_document_open_count = (
        _search_cached_sources(
            pair_rows=financial_pairs,
            source_rows=source_rows,
            cache_rows=cache_rows,
        )
    )
    applied_override_pairs = {
        str(row["pair_key"])
        for row in financial_results
        if str(row["execution_status"])
        == "RESOLVED_CACHED_PRIMARY_SOURCE_FORMULA"
    }
    for row in source_results:
        if str(row["pair_key"]) in applied_override_pairs:
            row["execution_status"] = (
                "RESOLVED_BY_REVIEWED_EXACT_SOURCE_OVERRIDE"
            )
            row["required_next_action"] = "NONE"
    ocr_results, ocr_engine = _ocr_capability_rows(scope_rows)

    if len(financial_results) != 45:
        errors.append(
            f"financial result rows={len(financial_results)} expected=45"
        )
    if len(source_results) != 13:
        errors.append(
            f"source search rows={len(source_results)} expected=13"
        )
    if len(no_value_results) != 100:
        errors.append(
            f"no-value audit rows={len(no_value_results)} expected=100"
        )
    if len(ocr_results) != 34:
        errors.append(
            f"OCR capability rows={len(ocr_results)} expected=34"
        )
    financial_status_counts = Counter(
        str(row["execution_status"]) for row in financial_results
    )
    override_counts = Counter(
        str(row["coverage_override"])
        for row in financial_results
        if str(row["coverage_override"])
    )
    if override_counts != {
        "COVERED_FINANCIAL_DERIVED": 6,
        "NOT_APPLICABLE": 9,
    }:
        errors.append(
            "safe financial overrides changed: "
            f"observed={dict(override_counts)}"
        )
    if any(str(row["coverage_override"]) for row in no_value_results):
        errors.append("no-value audit attempted an unreviewed override")
    if any(str(row["coverage_override"]) for row in ocr_results):
        errors.append("OCR capability audit attempted an override")

    financial_result_path = (
        output_dir / "transportation_bounded_financial_repairs.csv"
    )
    source_result_path = (
        output_dir
        / "transportation_bounded_financial_source_search.csv"
    )
    no_value_path = (
        output_dir / "transportation_bounded_no_value_audit.csv"
    )
    ocr_path = (
        output_dir / "transportation_bounded_ocr_execution.csv"
    )
    manifest_path = (
        output_dir
        / "transportation_bounded_repair_execution_manifest.json"
    )
    write_csv_atomic(
        financial_result_path,
        FINANCIAL_EXECUTION_FIELDS,
        financial_results,
    )
    write_csv_atomic(
        source_result_path,
        SOURCE_GAP_SEARCH_FIELDS,
        source_results,
    )
    write_csv_atomic(
        no_value_path,
        NO_VALUE_AUDIT_FIELDS,
        no_value_results,
    )
    write_csv_atomic(
        ocr_path,
        OCR_EXECUTION_FIELDS,
        ocr_results,
    )
    limitations = []
    if not ocr_engine:
        limitations.append(
            "local Tesseract OCR engine is not installed; 34 validated-empty "
            "PDF hashes remain explicitly terminal for the current toolchain"
        )
    acceptance = (
        "FAIL"
        if errors
        else (
            "PASS_WITH_EXPLICIT_LIMITATIONS"
            if limitations
            else "PASS"
        )
    )
    payload = {
        "acceptance": acceptance,
        "gate": "DP6Z_BOUNDED_CACHE_ONLY_REPAIR_EXECUTION",
        "execution_version": BOUNDED_REPAIR_EXECUTION_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "coverage_prefix": coverage_prefix,
        "financial_result_count": len(financial_results),
        "financial_execution_status_counts": dict(
            sorted(financial_status_counts.items())
        ),
        "financial_coverage_override_counts": dict(
            sorted(override_counts.items())
        ),
        "reviewed_cached_source_override_count": (
            cached_override_count
        ),
        "financial_source_search_count": len(source_results),
        "financial_source_search_status_counts": dict(
            sorted(
                Counter(
                    str(row["execution_status"])
                    for row in source_results
                ).items()
            )
        ),
        "no_value_audit_count": len(no_value_results),
        "no_value_audit_status_counts": dict(
            sorted(
                Counter(
                    str(row["execution_status"])
                    for row in no_value_results
                ).items()
            )
        ),
        "ocr_scope_count": len(ocr_results),
        "ocr_engine": ocr_engine,
        "ocr_status_counts": dict(
            sorted(
                Counter(
                    str(row["execution_status"])
                    for row in ocr_results
                ).items()
            )
        ),
        "limitations": limitations,
        "database_read_only": True,
        "full_parser_batch_authorized": False,
        "scope_expansion_authorized": False,
        "network_requests": 0,
        "retrieval_invocations": 0,
        "parser_invocations": 0,
        "source_document_open_count": source_document_open_count,
        "source_document_mode": "EXISTING_GZIP_TEXT_CACHE_ONLY",
        "ocr_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
        "inputs": {
            "bounded_scope": {
                "path": str(scope_path.resolve()),
                "sha256": file_sha256(scope_path),
            },
            "base_coverage": {
                "path": str(coverage_path.resolve()),
                "sha256": file_sha256(coverage_path),
            },
            "financial_pair_contract": {
                "path": str(financial_path.resolve()),
                "sha256": file_sha256(financial_path),
            },
            "financial_dependency_contract": {
                "path": str(dependency_path.resolve()),
                "sha256": file_sha256(dependency_path),
            },
            "direct_source_manifest": {
                "path": str(source_path.resolve()),
                "sha256": file_sha256(source_path),
            },
            "content_text_cache": {
                "path": str(cache_path.resolve()),
                "sha256": file_sha256(cache_path),
            },
            "reviewed_cached_financial_source_overrides": {
                "path": str(override_path.resolve()),
                "sha256": file_sha256(override_path),
            },
        },
        "artifacts": {
            "financial_repairs": {
                "path": str(financial_result_path.resolve()),
                "row_count": len(financial_results),
                "sha256": file_sha256(financial_result_path),
            },
            "financial_source_search": {
                "path": str(source_result_path.resolve()),
                "row_count": len(source_results),
                "sha256": file_sha256(source_result_path),
            },
            "no_value_audit": {
                "path": str(no_value_path.resolve()),
                "row_count": len(no_value_results),
                "sha256": file_sha256(no_value_path),
            },
            "ocr_execution": {
                "path": str(ocr_path.resolve()),
                "row_count": len(ocr_results),
                "sha256": file_sha256(ocr_path),
            },
        },
        "next_gate": (
            "REBUILD_BOUNDED_REPAIR_COVERAGE_ONCE"
            if not errors
            else "REVIEW_BOUNDED_REPAIR_EXECUTION_ERRORS"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
