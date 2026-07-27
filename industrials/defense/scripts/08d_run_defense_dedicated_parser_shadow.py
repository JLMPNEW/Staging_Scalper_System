#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.cli import main as parser_main  # noqa: E402
from dedicated_parser.contracts import PlanSummary, file_sha256  # noqa: E402
from dedicated_parser.planner import audit_cache_completeness  # noqa: E402
from dedicated_parser.storage import connect_database, load_run  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT.parent / "config.yaml"
ADAPTER = "industrials.defense.dedicated_parser_adapter:extract_metric_evidence"
HYDRATION_SCOPE_FIELDS = [
    "ticker",
    "accession_number",
    "form_type",
    "filing_date",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Run the shared SEC parser over current and historical defense members in isolated shadow mode.")
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--max-filings-per-ticker", type=int, default=None)
    parser.add_argument("--max-documents-per-filing", type=int, default=None)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--all-metrics", action="store_true")
    parser.add_argument("--require-complete-cache", action="store_true")
    parser.add_argument("--hydrate-missing-cache", action="store_true")
    parser.add_argument(
        "--exhaustive-hydration",
        action="store_true",
        help=(
            "Hydrate all supported filings and eligible documents with "
            "unlimited parser/archive windows before shadow extraction."
        ),
    )
    parser.add_argument(
        "--hydration-passes",
        type=int,
        default=3,
        help=(
            "Maximum exhaustive cache passes. Cached files are reused; "
            "remaining gaps are retried with configured SEC retries."
        ),
    )
    parser.add_argument("--hydration-only", action="store_true")
    parser.add_argument("--reassess-run-id", type=int, default=0)
    parser.add_argument("--disable-arelle", action="store_true")
    parser.add_argument("--disable-edgartools", action="store_true")
    parser.add_argument("--disable-pdf-ocr", action="store_true")
    parser.add_argument(
        "--write-evidence-adjudication-skeleton",
        action="store_true",
        help=("Also write the large evidence-level skeleton. Defense uses the pair-level 08h queue by default."),
    )
    return parser.parse_args(argv)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _split_values(raw: object) -> list[str]:
    return sorted({value.strip().upper() for value in str(raw or "").split(",") if value.strip()})


def _limit(requested: int | None, configured: int) -> int:
    value = configured if requested is None else requested
    if value < 0:
        raise ValueError("Dedicated parser limits must be zero or positive")
    return value


def _preflight(
    *,
    db_path: Path,
    cache_dir: Path,
    asof_date: str,
    tickers: list[str],
    max_filings: int,
    max_documents: int,
    force: bool,
    all_metrics: bool,
) -> PlanSummary:
    registry = load_registry(ADAPTER)
    with connect_database(db_path) as conn:
        return audit_cache_completeness(
            conn,
            registry=registry,
            adapter_path=ADAPTER,
            asof_date=asof_date,
            cache_dir=cache_dir,
            tickers=tickers or None,
            max_filings_per_ticker=max_filings,
            max_documents_per_filing=max_documents,
            force=force,
            all_metrics=all_metrics,
        )


def build_hydration_command(
    *,
    config_path: Path,
    db_path: Path,
    asof_date: str,
    tickers: list[str],
    output_csv: Path,
    exhaustive: bool,
    cache_workers: int,
    accession_scope_csv: Path | None = None,
) -> list[str]:
    if not tickers:
        raise ValueError("Cache hydration requires at least one ticker")
    command = [
        sys.executable,
        str(PACKAGE_ROOT / "scripts" / "07_sync_defense_sec_fundamentals.py"),
        "--config",
        str(config_path),
        "--db",
        str(db_path),
        "--asof",
        asof_date,
        "--tickers",
        ",".join(tickers),
        "--include-historical",
        "--archive-selected",
        "--archive-bootstrap",
        "--output-csv",
        str(output_csv),
    ]
    if not exhaustive:
        command.append("--allow-partial")
    if exhaustive:
        registry = load_registry(ADAPTER)
        command.extend(
            [
                "--archive-cache-only",
                "--archive-scan-all-documents",
                "--archive-max-filings-per-ticker",
                "0",
                "--archive-max-documents-per-filing",
                "0",
                "--archive-cache-workers",
                str(cache_workers),
                "--archive-document-keywords",
                ",".join(registry.document_keywords),
            ]
        )
    if accession_scope_csv is not None:
        command.extend(
            [
                "--archive-accession-scope-csv",
                str(accession_scope_csv),
            ]
        )
    return command


def build_filing_catalog_command(
    *,
    config_path: Path,
    db_path: Path,
    asof_date: str,
    tickers: list[str],
    output_csv: Path,
    start_date: str,
    forms: tuple[str, ...],
) -> list[str]:
    if not tickers:
        raise ValueError("Filing cataloging requires at least one ticker")
    if not forms:
        raise ValueError("Filing cataloging requires at least one form")
    return [
        sys.executable,
        str(PACKAGE_ROOT / "scripts" / "07_sync_defense_sec_fundamentals.py"),
        "--config",
        str(config_path),
        "--db",
        str(db_path),
        "--asof",
        asof_date,
        "--tickers",
        ",".join(tickers),
        "--include-historical",
        "--filing-catalog-cache-only",
        "--filing-catalog-fetch-missing",
        "--filing-catalog-forms",
        ",".join(forms),
        "--filing-catalog-start-date",
        start_date,
        "--skip-source-registry",
        "--output-csv",
        str(output_csv),
    ]


def _validate_args(args: argparse.Namespace) -> None:
    if args.hydration_only:
        args.hydrate_missing_cache = True
    if args.exhaustive_hydration:
        args.hydrate_missing_cache = True
        args.all_metrics = True
        args.max_filings_per_ticker = 0
        args.max_documents_per_filing = 0
    if args.hydration_passes < 1:
        raise ValueError("--hydration-passes must be at least 1")
    if args.reassess_run_id and any(
        (
            args.plan_only,
            args.force,
            args.all_metrics,
            args.hydrate_missing_cache,
        )
    ):
        raise ValueError("--reassess-run-id cannot be combined with planning, force, all-metrics, or hydration modes")
    if not args.reassess_run_id and not args.asof:
        raise ValueError("--asof is required unless --reassess-run-id is used")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _validate_args(args)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=base_dir,
        )
    )
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=base_dir,
    )
    output_root = resolve_path(
        cfg_get(
            config,
            "dedicated_parser.output_root",
            "../output/industrials/defense/dedicated_parser",
        ),
        base_dir=base_dir,
    )
    provider_state_dir = resolve_path(
        cfg_get(
            config,
            "dedicated_parser.provider_state_dir",
            "../tmp/edgartools/defense",
        ),
        base_dir=base_dir,
    )
    max_filings = _limit(
        args.max_filings_per_ticker,
        int(cfg_get(config, "dedicated_parser.max_filings_per_ticker", 40)),
    )
    max_documents = _limit(
        args.max_documents_per_filing,
        int(
            cfg_get(
                config,
                "dedicated_parser.max_documents_per_filing",
                32,
            )
        ),
    )
    archive_max = int(cfg_get(config, "sec_archive.max_filings_per_ticker", 0))
    if max_filings > 0 and archive_max > 0 and archive_max < max_filings:
        raise ValueError(
            "sec_archive.max_filings_per_ticker must be zero/unlimited or "
            "at least dedicated_parser.max_filings_per_ticker"
        )
    asof_date = str(args.asof or "")
    if args.reassess_run_id and not asof_date:
        with connect_database(db_path) as conn:
            asof_date = str(load_run(conn, run_id=args.reassess_run_id)["asof_date"])
    output_dir = output_root / asof_date
    tickers = _split_values(args.tickers)

    if args.hydrate_missing_cache:
        scope_before_catalog = _preflight(
            db_path=db_path,
            cache_dir=cache_dir,
            asof_date=asof_date,
            tickers=tickers,
            max_filings=max_filings,
            max_documents=max_documents,
            force=args.force,
            all_metrics=args.all_metrics,
        )
        catalog_record: dict[str, object] = {
            "status": "NOT_REQUESTED",
        }
        if args.exhaustive_hydration:
            catalog_tickers = tickers if tickers else list(scope_before_catalog.selected_tickers)
            catalog_output = output_dir / "dedicated_parser_event_filing_catalog.csv"
            event_forms = tuple(
                sorted(
                    {
                        str(form).strip().upper()
                        for form in (
                            cfg_get(
                                config,
                                "sec_archive.supplemental_forms",
                                ["8-K", "8-K/A"],
                            )
                            or []
                        )
                        if str(form).strip().upper() in {"8-K", "8-K/A"}
                    }
                )
            )
            event_start_date = str(
                cfg_get(
                    config,
                    "dedicated_parser.event_filing_start_date",
                    "2018-01-01",
                )
                or "2018-01-01"
            )
            catalog_command = build_filing_catalog_command(
                config_path=config_path,
                db_path=db_path,
                asof_date=asof_date,
                tickers=catalog_tickers,
                output_csv=catalog_output,
                start_date=event_start_date,
                forms=event_forms,
            )
            catalog_result = subprocess.run(
                catalog_command,
                cwd=PROJECT_ROOT,
                check=False,
            )
            catalog_record = {
                "status": ("COMPLETED" if catalog_result.returncode == 0 else "FAILED"),
                "return_code": catalog_result.returncode,
                "command": catalog_command,
                "output_csv": str(catalog_output),
                "forms": list(event_forms),
                "start_date": event_start_date,
                "ticker_count": len(catalog_tickers),
                "scope_before_catalog": asdict(scope_before_catalog),
            }
            if catalog_result.returncode:
                _write_json(
                    output_dir / "dedicated_parser_cache_hydration.json",
                    {
                        "asof_date": asof_date,
                        "status": "CATALOG_FAILED",
                        "catalog": catalog_record,
                    },
                )
                return 3
            with catalog_output.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                catalog_rows = list(csv.DictReader(handle))
            duplicate_tickers = sorted(
                ticker
                for ticker, count in Counter(
                    str(row.get("ticker") or "").strip().upper() for row in catalog_rows
                ).items()
                if ticker and count > 1
            )
            invalid_catalog_rows = [row for row in catalog_rows if str(row.get("status") or "") != "cataloged"]
            if len(catalog_rows) != len(catalog_tickers) or duplicate_tickers or invalid_catalog_rows:
                catalog_record.update(
                    {
                        "status": "VALIDATION_FAILED",
                        "catalog_row_count": len(catalog_rows),
                        "duplicate_tickers": duplicate_tickers,
                        "invalid_status_rows": invalid_catalog_rows[:20],
                    }
                )
                _write_json(
                    output_dir / "dedicated_parser_cache_hydration.json",
                    {
                        "asof_date": asof_date,
                        "status": "CATALOG_FAILED",
                        "catalog": catalog_record,
                    },
                )
                return 3
            catalog_record.update(
                {
                    "output_csv_sha256": file_sha256(catalog_output),
                    "catalog_row_count": len(catalog_rows),
                    "cataloged_filing_count": sum(int(row.get("cataloged_filing_count") or 0) for row in catalog_rows),
                    "tickers_with_event_filings": sum(
                        int(row.get("cataloged_filing_count") or 0) > 0 for row in catalog_rows
                    ),
                    "missing_history_cache_count": sum(
                        int(row.get("missing_history_cache_count") or 0) for row in catalog_rows
                    ),
                }
            )
        before = _preflight(
            db_path=db_path,
            cache_dir=cache_dir,
            asof_date=asof_date,
            tickers=tickers,
            max_filings=max_filings,
            max_documents=max_documents,
            force=args.force,
            all_metrics=args.all_metrics,
        )
        initial_missing_tickers = sorted(
            {
                str(row.get("ticker") or "").strip().upper()
                for row in before.missing_cache_details
                if str(row.get("ticker") or "").strip()
            }
        )
        pass_records: list[dict[str, object]] = []
        hydration_manifest: dict[str, object] = {
            "asof_date": asof_date,
            "before": asdict(before),
            "affected_tickers": initial_missing_tickers,
            "exhaustive": bool(args.exhaustive_hydration),
            "max_filings_per_ticker": max_filings,
            "max_documents_per_filing": max_documents,
            "configured_hydration_passes": (args.hydration_passes if args.exhaustive_hydration else 1),
            "catalog": catalog_record,
            "passes": pass_records,
        }
        current = before
        maximum_passes = args.hydration_passes if args.exhaustive_hydration else 1
        for pass_number in range(1, maximum_passes + 1):
            if not current.missing_cache_accessions:
                break
            accession_scope_csv = output_dir / "dedicated_parser_cache_hydration_scope.csv"
            write_csv_atomic(
                accession_scope_csv,
                HYDRATION_SCOPE_FIELDS,
                current.missing_cache_details,
            )
            missing_tickers = sorted(
                {
                    str(row.get("ticker") or "").strip().upper()
                    for row in current.missing_cache_details
                    if str(row.get("ticker") or "").strip()
                }
            )
            command = build_hydration_command(
                config_path=config_path,
                db_path=db_path,
                asof_date=asof_date,
                tickers=missing_tickers,
                output_csv=(output_dir / "dedicated_parser_cache_hydration_sync.csv"),
                exhaustive=bool(args.exhaustive_hydration),
                cache_workers=(
                    args.workers
                    or int(
                        cfg_get(
                            config,
                            "dedicated_parser.workers",
                            4,
                        )
                    )
                ),
                accession_scope_csv=accession_scope_csv,
            )
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                check=False,
            )
            before_missing_accessions = current.missing_cache_accessions
            pass_record: dict[str, object] = {
                "pass_number": pass_number,
                "before_missing_accessions": before_missing_accessions,
                "affected_ticker_count": len(missing_tickers),
                "sync_command": command,
                "sync_return_code": result.returncode,
            }
            pass_records.append(pass_record)
            if result.returncode:
                hydration_manifest["status"] = "SYNC_FAILED"
                _write_json(
                    output_dir / "dedicated_parser_cache_hydration.json",
                    hydration_manifest,
                )
                return 3
            current = _preflight(
                db_path=db_path,
                cache_dir=cache_dir,
                asof_date=asof_date,
                tickers=tickers,
                max_filings=max_filings,
                max_documents=max_documents,
                force=args.force,
                all_metrics=args.all_metrics,
            )
            pass_record["after_missing_accessions"] = current.missing_cache_accessions
            pass_record["hydrated_accessions"] = before_missing_accessions - current.missing_cache_accessions
        after = current
        cache_complete = not after.missing_cache_accessions
        attempts_exhausted = bool(args.exhaustive_hydration and len(pass_records) == maximum_passes)
        review_ready = bool(cache_complete or attempts_exhausted)
        hydration_manifest["after"] = asdict(after)
        hydration_manifest["review_ready"] = review_ready
        hydration_manifest["remaining_source_gap_count"] = after.missing_cache_accessions
        hydration_manifest["status"] = (
            "CACHE_COMPLETE"
            if cache_complete
            else "EXHAUSTIVE_SOURCE_GAPS_RECORDED"
            if attempts_exhausted
            else "CACHE_INCOMPLETE"
        )
        _write_json(
            output_dir / "dedicated_parser_cache_hydration.json",
            hydration_manifest,
        )
        if args.hydration_only:
            return 0 if review_ready else 2

    workers = args.workers or int(cfg_get(config, "dedicated_parser.workers", 4))
    output_filename = (
        "dedicated_parser_assessment_only_run.json"
        if args.reassess_run_id
        else "dedicated_parser_plan.json"
        if args.plan_only
        else "dedicated_parser_shadow_run.json"
    )
    forwarded = [
        "--db",
        str(db_path),
        "--cache-dir",
        str(cache_dir),
        "--adapter",
        ADAPTER,
        "--workers",
        str(workers),
        "--max-filings-per-ticker",
        str(max_filings),
        "--max-documents-per-filing",
        str(max_documents),
        "--provider-state-dir",
        str(provider_state_dir),
        "--max-pdf-pages",
        str(int(cfg_get(config, "dedicated_parser.max_pdf_pages", 250))),
        "--max-pdf-bytes",
        str(
            int(
                cfg_get(
                    config,
                    "dedicated_parser.max_pdf_bytes",
                    25_000_000,
                )
            )
        ),
        "--pdf-extraction-timeout-seconds",
        str(
            float(
                cfg_get(
                    config,
                    "dedicated_parser.pdf_extraction_timeout_seconds",
                    30.0,
                )
            )
        ),
        "--output-json",
        str(output_dir / output_filename),
        "--cache-gate-output-json",
        str(output_dir / "dedicated_parser_cache_gate.json"),
    ]
    if asof_date:
        forwarded.extend(["--asof", asof_date])
    if args.tickers:
        forwarded.extend(["--tickers", args.tickers])
    if args.plan_only:
        forwarded.append("--plan-only")
    if args.force:
        forwarded.append("--force")
    if args.all_metrics:
        forwarded.append("--all-metrics")
    if args.require_complete_cache:
        forwarded.append("--require-complete-cache")
    if not args.write_evidence_adjudication_skeleton:
        forwarded.append("--skip-adjudication-skeleton")
    if args.reassess_run_id:
        forwarded.extend(["--reassess-run-id", str(args.reassess_run_id)])
    if args.disable_arelle:
        forwarded.append("--disable-arelle")
    if args.disable_edgartools:
        forwarded.append("--disable-edgartools")
    if bool(cfg_get(config, "dedicated_parser.pdf_ocr_enabled", False)) and not args.disable_pdf_ocr:
        forwarded.append("--enable-pdf-ocr")
    return parser_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
