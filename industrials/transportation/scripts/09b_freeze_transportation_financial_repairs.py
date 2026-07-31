#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence, cast


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
from industrials.transportation.financial_repair_contract import (  # noqa: E402
    FINANCIAL_DEPENDENCY_FIELDS,
    FINANCIAL_REPAIR_PAIR_FIELDS,
    FINANCIAL_REPAIR_RULES,
    FINANCIAL_REPAIR_VERSION,
    build_financial_repair_contracts,
    summarize_financial_repair,
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze exact formula, source hierarchy, dependency, period, "
            "unit, and QA repair contracts for every missing transportation "
            "financial-derived pair. This command is database read-only."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _query_financial_state(
    *,
    foundation: Any,
    asof_date: str,
    tickers: list[str],
    ticker_placeholders: str,
    canonical_metric_ids: list[str],
    metric_placeholders: str,
    feature_by_ticker: dict[str, dict[str, object]],
    availability_by_key: dict[tuple[str, str], dict[str, object]],
    canonical_rows: list[dict[str, object]],
) -> None:
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        for row in connection.execute(
            f"""
            SELECT feature.*
            FROM feature_financial_statement AS feature
            WHERE feature.model_family=?
              AND feature.ticker IN ({ticker_placeholders})
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
            feature_by_ticker.setdefault(
                str(row["ticker"]),
                dict(row),
            )
        for row in connection.execute(
            f"""
            SELECT *
            FROM feature_financial_metric_availability
            WHERE model_family=?
              AND asof_date=?
              AND ticker IN ({ticker_placeholders})
            ORDER BY ticker, metric_name
            """,
            (MODEL_FAMILY, asof_date, *tickers),
        ):
            availability_by_key[
                (str(row["ticker"]), str(row["metric_name"]))
            ] = dict(row)
        canonical_rows.extend(
            dict(row)
            for row in connection.execute(
                f"""
                SELECT ticker, canonical_metric, period_start,
                       period_end, filing_date, source_id,
                       accession_number, form_type, taxonomy,
                       concept_name, unit, value, value_usd,
                       canonical_quality
                FROM fact_financial_statement_canonical
                WHERE model_family=?
                  AND filing_date<=?
                  AND ticker IN ({ticker_placeholders})
                  AND canonical_metric IN ({metric_placeholders})
                ORDER BY ticker, canonical_metric, period_end,
                         filing_date, source_id
                """,
                (
                    MODEL_FAMILY,
                    asof_date,
                    *tickers,
                    *canonical_metric_ids,
                ),
            )
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Financial repair freeze requires parser execution disabled"
        )
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    residual_path = (
        output_dir
        / "transportation_non_sec_residual_source_audit.csv"
    )
    residual_manifest_path = (
        output_dir
        / "transportation_non_sec_residual_source_manifest.json"
    )
    endpoint_path = (
        output_dir / "transportation_non_sec_endpoint_roots.csv"
    )
    endpoint_manifest_path = (
        output_dir / "transportation_non_sec_endpoint_manifest.json"
    )
    semantic_manifest_path = (
        output_dir
        / "transportation_semantic_fixture_freeze_manifest.json"
    )
    required = (
        residual_path,
        residual_manifest_path,
        endpoint_path,
        endpoint_manifest_path,
        semantic_manifest_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing financial-repair inputs: {missing}"
        )
    residual_manifest = _read_json(residual_manifest_path)
    endpoint_manifest = _read_json(endpoint_manifest_path)
    semantic_manifest = _read_json(semantic_manifest_path)
    if (
        residual_manifest.get("acceptance") != "PASS"
        or str(
            (residual_manifest.get("artifact") or {}).get("sha256")
            or ""
        )
        != file_sha256(residual_path)
    ):
        raise ValueError("Residual audit is not hash-sealed")
    if (
        endpoint_manifest.get("acceptance") != "PASS"
        or str(
            (
                endpoint_manifest.get("artifacts") or {}
            ).get("endpoint_roots", {}).get("sha256")
            or ""
        )
        != file_sha256(endpoint_path)
    ):
        raise ValueError("Endpoint root manifest is not hash-sealed")
    if semantic_manifest.get("acceptance") != "PASS":
        raise ValueError("Semantic fixture freeze has not passed")
    residual_rows = read_csv(residual_path)
    financial_rows = [
        row
        for row in residual_rows
        if row["source_lane"] == "FIN-D"
    ]
    tickers = sorted({row["ticker"] for row in financial_rows})
    ticker_placeholders = ",".join("?" for _ in tickers)
    canonical_metric_ids = sorted(
        {
            str(metric)
            for rule in FINANCIAL_REPAIR_RULES.values()
            for dependency in cast(
                Sequence[Mapping[str, object]],
                rule["dependencies"],
            )
            for metric in cast(
                Sequence[str],
                dependency["canonical_metrics"],
            )
        }
    )
    metric_placeholders = ",".join(
        "?" for _ in canonical_metric_ids
    )
    feature_by_ticker: dict[str, dict[str, object]] = {}
    availability_by_key: dict[
        tuple[str, str],
        dict[str, object],
    ] = {}
    canonical_rows: list[dict[str, object]] = []
    # An empty FIN-D ticker set would render `IN ()`, which sqlite rejects;
    # the contract is then legitimately empty and the queries are skipped.
    if tickers:
        _query_financial_state(
            foundation=foundation,
            asof_date=asof_date,
            tickers=tickers,
            ticker_placeholders=ticker_placeholders,
            canonical_metric_ids=canonical_metric_ids,
            metric_placeholders=metric_placeholders,
            feature_by_ticker=feature_by_ticker,
            availability_by_key=availability_by_key,
            canonical_rows=canonical_rows,
        )
    endpoints = {
        row["ticker"]: row for row in read_csv(endpoint_path)
    }
    pair_rows, dependency_rows, errors = (
        build_financial_repair_contracts(
            residual_rows=residual_rows,
            feature_rows=feature_by_ticker,
            availability_rows=availability_by_key,
            canonical_rows=canonical_rows,
            endpoint_rows=endpoints,
            asof_date=asof_date,
        )
    )
    expected_pairs = int(
        str(
            (
                residual_manifest.get("coverage_status_counts") or {}
            ).get("FINANCIAL_INPUTS_MISSING", 0)
        )
    )
    if len(pair_rows) != expected_pairs:
        errors.append(
            f"financial pairs={len(pair_rows)} expected={expected_pairs}"
        )
    output_pair_keys = {str(row["pair_key"]) for row in pair_rows}
    dependency_pair_keys = {
        str(row["pair_key"]) for row in dependency_rows
    }
    if output_pair_keys != dependency_pair_keys:
        errors.append(
            "not every financial repair pair has dependency requirements"
        )
    pair_path = (
        output_dir
        / "transportation_financial_repair_pair_contract.csv"
    )
    dependency_path = (
        output_dir
        / "transportation_financial_repair_dependency_contract.csv"
    )
    manifest_path = (
        output_dir
        / "transportation_financial_repair_freeze_manifest.json"
    )
    write_csv_atomic(
        pair_path,
        FINANCIAL_REPAIR_PAIR_FIELDS,
        pair_rows,
    )
    write_csv_atomic(
        dependency_path,
        FINANCIAL_DEPENDENCY_FIELDS,
        dependency_rows,
    )
    payload = {
        "acceptance": (
            "PASS" if pair_rows and not errors else "FAIL"
        ),
        "gate": "DP6M_FINANCIAL_INPUT_REPAIR_FREEZE",
        "repair_version": FINANCIAL_REPAIR_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        **summarize_financial_repair(pair_rows),
        "financial_dependency_row_count": len(dependency_rows),
        "formula_contract_count": len(FINANCIAL_REPAIR_RULES),
        "source_hierarchy_frozen": not errors,
        "period_unit_formula_contracts_frozen": not errors,
        "owning_workflow": "standalone_support_request",
        "decision_impact": (
            "prevents unsupported financial-derived values from entering "
            "the final specialized metric panel"
        ),
        "readiness_effect": "needs_targeted_fixes",
        "artifact_role": "standalone_support_artifact",
        "hidden_unless_requested": True,
        "database_read_only": True,
        "retrieval_authorized": False,
        "parser_execution_authorized": False,
        "feature_rebuild_authorized": False,
        "network_requests": 0,
        "retrieval_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
        "inputs": {
            "residual_audit": {
                "path": str(residual_path.resolve()),
                "sha256": file_sha256(residual_path),
            },
            "endpoint_roots": {
                "path": str(endpoint_path.resolve()),
                "sha256": file_sha256(endpoint_path),
            },
            "semantic_freeze_manifest": {
                "path": str(semantic_manifest_path.resolve()),
                "sha256": file_sha256(semantic_manifest_path),
            },
        },
        "artifacts": {
            "financial_repair_pair_contract": {
                "path": str(pair_path.resolve()),
                "row_count": len(pair_rows),
                "sha256": file_sha256(pair_path),
            },
            "financial_repair_dependency_contract": {
                "path": str(dependency_path.resolve()),
                "row_count": len(dependency_rows),
                "sha256": file_sha256(dependency_path),
            },
        },
        "next_gate": (
            "BUILD_ALL_INCLUSIVE_ONE_PASS_SOURCE_PREFLIGHT"
            if not errors
            else "REVIEW_FINANCIAL_REPAIR_FREEZE_ERRORS"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
