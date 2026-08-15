#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.cli import main as parser_main  # noqa: E402
from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    load_investable_universe_policy,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


ADAPTER = "industrials.transportation.dedicated_parser_adapter:extract_metric_evidence"
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_investable_universe_v3.yaml"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute the exact 11-tanker, 16-direct-metric parser "
            "batch against the versioned complete-cache source census."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=None)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan-only", action="store_true")
    modes.add_argument("--execute", action="store_true")
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _parser_args(
    *,
    db_path: Path,
    cache_dir: Path,
    source_manifest: Path,
    output_json: Path,
    cache_gate_json: Path,
    provider_state_dir: Path,
    asof: str,
    workers: int,
) -> list[str]:
    return [
        "--db",
        str(db_path),
        "--cache-dir",
        str(cache_dir),
        "--adapter",
        ADAPTER,
        "--asof",
        asof,
        "--source-manifest",
        str(source_manifest),
        "--workers",
        str(workers),
        "--max-filings-per-ticker",
        "0",
        "--max-documents-per-filing",
        "0",
        "--provider-state-dir",
        str(provider_state_dir),
        "--max-pdf-pages",
        "0",
        "--max-pdf-bytes",
        "75000000",
        "--pdf-extraction-timeout-seconds",
        "120",
        "--all-metrics",
        "--require-complete-cache",
        "--skip-adjudication-skeleton",
        "--enable-pdf-ocr",
        "--output-json",
        str(output_json),
        "--cache-gate-output-json",
        str(cache_gate_json),
    ]


PARSER_SOURCE_FIELDS = (
    "ticker",
    "accession_number",
    "document_name",
    "content_sha256",
    "cache_status",
    "local_path",
    "cik",
    "form_type",
    "filing_date",
    "accepted_at",
    "report_date",
    "primary_document",
    "source_id",
    "company_currency",
    "source_kind",
    "is_primary",
    "is_full_submission",
    "requested_metric_ids",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _flag(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def _build_parser_source_manifest(
    *,
    census_path: Path,
    output_path: Path,
    db_path: Path,
    direct_metric_ids: tuple[str, ...],
) -> int:
    census_rows = _read_csv(census_path)
    if not census_rows:
        raise ValueError("tanker source census has no document rows")
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    tickers: set[str] = set()
    for row in census_rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        accession = str(row.get("accession_number") or "").strip()
        if not ticker or not accession:
            raise ValueError("census source row is missing ticker/accession")
        groups.setdefault((ticker, accession), []).append(row)
        tickers.add(ticker)

    placeholders = ",".join("?" for _ in tickers)
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        currency_by_ticker = {
            str(row[0]).upper(): str(row[1] or "USD").upper()
            for row in connection.execute(
                f"SELECT ticker, COALESCE(NULLIF(currency, ''), 'USD') "
                f"FROM dim_company WHERE ticker IN ({placeholders})",
                tuple(sorted(tickers)),
            )
        }
    finally:
        connection.close()

    allowed_metrics = set(direct_metric_ids)
    parser_rows: list[dict[str, object]] = []
    for key in sorted(groups):
        rows = groups[key]
        primary_names = {
            str(row.get("document_name") or "").strip()
            for row in rows
            if _flag(row.get("is_primary"))
        }
        if len(primary_names) > 1:
            raise ValueError(f"multiple primary documents for {key}: {sorted(primary_names)}")
        if primary_names:
            primary_document = next(iter(primary_names))
        else:
            candidates = sorted(
                rows,
                key=lambda row: (
                    _flag(row.get("is_full_submission")),
                    str(row.get("document_name") or ""),
                ),
            )
            primary_document = str(candidates[0].get("document_name") or "").strip()
        metadata = {
            (
                str(row.get("cik") or "").strip(),
                str(row.get("form_type") or "").strip(),
                str(row.get("filing_date") or "").strip(),
                str(row.get("accepted_at") or "").strip(),
                str(row.get("report_date") or "").strip(),
                str(row.get("source_id") or "").strip(),
            )
            for row in rows
        }
        if len(metadata) != 1:
            raise ValueError(f"inconsistent filing metadata in census for {key}")
        for row in rows:
            local_path = Path(str(row.get("local_path") or "")).expanduser().resolve()
            if not local_path.is_file() or local_path.stat().st_size <= 0:
                raise FileNotFoundError(f"missing cached census document: {local_path}")
            requested = sorted(
                allowed_metrics
                & {
                    value.strip()
                    for value in str(row.get("applicable_metric_ids") or "").split("|")
                    if value.strip()
                }
            )
            if not requested:
                raise ValueError(f"no allowed direct metrics for {key}/{local_path.name}")
            ticker = key[0]
            parser_rows.append(
                {
                    "ticker": ticker,
                    "accession_number": key[1],
                    "document_name": str(row.get("document_name") or "").strip(),
                    "content_sha256": str(row.get("content_sha256") or "").strip(),
                    "cache_status": str(row.get("cache_status") or "").strip(),
                    "local_path": str(local_path),
                    "cik": str(row.get("cik") or "").strip(),
                    "form_type": str(row.get("form_type") or "").strip(),
                    "filing_date": str(row.get("filing_date") or "").strip(),
                    "accepted_at": str(row.get("accepted_at") or "").strip(),
                    "report_date": str(row.get("report_date") or "").strip(),
                    "primary_document": primary_document,
                    "source_id": str(row.get("source_id") or "").strip(),
                    "company_currency": currency_by_ticker.get(ticker, "USD"),
                    "source_kind": str(row.get("document_kind") or "sec_archive_document").strip(),
                    "is_primary": int(str(row.get("document_name") or "").strip() == primary_document),
                    "is_full_submission": int(_flag(row.get("is_full_submission"))),
                    "requested_metric_ids": "|".join(requested),
                }
            )
    write_csv_atomic(output_path, PARSER_SOURCE_FIELDS, parser_rows)
    return len(parser_rows)


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    foundation = resolve_foundation(config_path, args.db)
    policy = load_investable_universe_policy(args.policy)
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            parser_cfg["tanker_delta_output_root"], base_dir=base_dir
        )
        / args.asof
    )
    census_source_manifest = output_dir / "transportation_tanker_delta_source_census.csv"
    source_manifest = output_dir / "transportation_tanker_delta_parser_source_manifest.csv"
    census_manifest_path = output_dir / "transportation_tanker_delta_census_manifest.json"
    plan_path = output_dir / "transportation_tanker_delta_parser_plan.json"
    plan_gate_path = output_dir / "transportation_tanker_delta_parser_plan_gate.json"
    run_path = output_dir / "transportation_tanker_delta_parser_run.json"
    cache_gate_path = output_dir / "transportation_tanker_delta_parser_cache_gate.json"
    census = _read_json(census_manifest_path)
    if census.get("acceptance") != "PASS" or int(census.get("unresolved_gap_count") or 0):
        raise ValueError("tanker parser requires a PASS census with zero cache gaps")
    if set(census.get("execution_scope", {}).get("tickers", ())) != set(
        policy.tanker_tickers
    ):
        raise ValueError("census tanker scope does not match investable policy")
    registry = load_registry(ADAPTER)
    registry_names = {request.metric_name for request in registry.parser_metrics}
    if not set(policy.direct_tanker_metrics) <= registry_names:
        raise ValueError("adapter registry is missing direct tanker metrics")
    parser_source_row_count = _build_parser_source_manifest(
        census_path=census_source_manifest,
        output_path=source_manifest,
        db_path=foundation.db_path,
        direct_metric_ids=policy.direct_tanker_metrics,
    )
    if parser_source_row_count != int(census.get("selected_document_row_count") or -1):
        raise ValueError("normalized parser manifest row count does not match census")
    workers = args.workers or int(parser_cfg.get("workers") or 4)
    common = _parser_args(
        db_path=foundation.db_path,
        cache_dir=resolve_path(
            cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir
        ),
        source_manifest=source_manifest,
        output_json=plan_path if args.plan_only else run_path,
        cache_gate_json=cache_gate_path,
        provider_state_dir=(
            PROJECT_ROOT / "tmp" / "edgartools" / "transportation_tanker_v3"
        ),
        asof=args.asof,
        workers=workers,
    )
    if args.plan_only:
        code = parser_main([*common, "--plan-only"])
        plan = _read_json(plan_path)
        summary = plan.get("summary") or {}
        execution = summary.get("execution_scope") or {}
        errors: list[str] = []
        if code != 0:
            errors.append(f"parser plan return code={code}")
        if plan.get("mode") != "plan_only":
            errors.append("parser did not produce plan_only mode")
        if int(summary.get("requested_tickers") or 0) != len(
            policy.tanker_tickers
        ):
            errors.append("plan requested ticker count is not 11")
        if int(summary.get("missing_cache_accessions") or 0) != 0:
            errors.append("plan contains missing cache accessions")
        if not execution.get("all_metrics"):
            errors.append("plan did not request all manifest-scoped metrics")
        scope = execution.get("source_manifest") or {}
        if scope.get("sha256") != file_sha256(source_manifest):
            errors.append("plan source-manifest hash mismatch")
        gate = {
            "acceptance": "PASS" if not errors else "FAIL",
            "gate": "TRANSPORTATION_TANKER_V3_COMPLETE_CACHE_PLAN",
            "asof_date": args.asof,
            "source_manifest_sha256": file_sha256(source_manifest),
            "policy_sha256": file_sha256(policy.path),
            "adapter_version": registry.adapter_version,
            "ticker_count": len(policy.tanker_tickers),
            "direct_metric_count": len(policy.direct_tanker_metrics),
            "scheduled_accessions": int(summary.get("scheduled_accessions") or 0),
            "scheduled_documents": int(summary.get("scheduled_documents") or 0),
            "errors": errors,
            "historical_reconstruction_authorized": False,
            "calibration_authorized": False,
            "production_promotion_authorized": False,
        }
        write_text_atomic(
            plan_gate_path,
            json.dumps(gate, indent=2, sort_keys=True) + "\n",
        )
        print(json.dumps(gate, indent=2, sort_keys=True))
        return 0 if not errors else 2

    gate = _read_json(plan_gate_path)
    if (
        gate.get("acceptance") != "PASS"
        or gate.get("source_manifest_sha256") != file_sha256(source_manifest)
        or gate.get("policy_sha256") != file_sha256(policy.path)
        or gate.get("adapter_version") != registry.adapter_version
    ):
        raise ValueError("execute requires a current PASS plan for the same inputs")
    before = sqlite3.connect(foundation.db_path).execute(
        """
        SELECT COUNT(*) FROM fact_sec_metric_disclosure_candidate
        WHERE model_family='transportation' AND ticker IN ({})
        """.format(",".join("?" for _ in policy.tanker_tickers)),
        policy.tanker_tickers,
    ).fetchone()[0]
    code = parser_main(common)
    after = sqlite3.connect(foundation.db_path).execute(
        """
        SELECT COUNT(*) FROM fact_sec_metric_disclosure_candidate
        WHERE model_family='transportation' AND ticker IN ({})
        """.format(",".join("?" for _ in policy.tanker_tickers)),
        policy.tanker_tickers,
    ).fetchone()[0]
    run = _read_json(run_path)
    run["bounded_batch_contract"] = {
        "policy_version": policy.path.stem,
        "ticker_count": len(policy.tanker_tickers),
        "direct_metric_count": len(policy.direct_tanker_metrics),
        "candidate_rows_before": int(before),
        "candidate_rows_after": int(after),
        "candidate_row_delta": int(after - before),
        "parser_return_code": code,
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
    }
    write_text_atomic(run_path, json.dumps(run, indent=2, sort_keys=True) + "\n")
    print(json.dumps(run["bounded_batch_contract"], indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
