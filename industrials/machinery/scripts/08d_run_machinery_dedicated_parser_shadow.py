#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.benchmark import load_cohort_tickers  # noqa: E402
from dedicated_parser.cli import main as parser_main  # noqa: E402
from dedicated_parser.contracts import PlanSummary  # noqa: E402
from dedicated_parser.planner import audit_cache_completeness  # noqa: E402
from dedicated_parser.storage import (  # noqa: E402
    connect_database,
    load_run,
)
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
ADAPTER = (
    "industrials.machinery.dedicated_parser_adapter:"
    "extract_metric_evidence"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the dedicated SEC parser in machinery shadow mode."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--ticker-cohort", type=Path, default=None)
    parser.add_argument(
        "--artifact-label",
        default="",
        help="Optional safe subdirectory for an isolated benchmark run.",
    )
    parser.add_argument("--accessions", default="")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--max-filings-per-ticker", type=int, default=None)
    parser.add_argument("--max-documents-per-filing", type=int, default=None)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--all-metrics",
        action="store_true",
        help=(
            "Evaluate all selected source metrics while resuming completed "
            "parser work."
        ),
    )
    parser.add_argument("--require-complete-cache", action="store_true")
    parser.add_argument("--hydrate-missing-cache", action="store_true")
    parser.add_argument("--hydration-only", action="store_true")
    parser.add_argument("--reassess-run-id", type=int, default=0)
    parser.add_argument(
        "--sec-sync-python",
        type=Path,
        default=None,
        help="Python executable used for the existing machinery SEC synchronizer.",
    )
    parser.add_argument("--disable-arelle", action="store_true")
    parser.add_argument("--disable-edgartools", action="store_true")
    parser.add_argument("--disable-pdf-ocr", action="store_true")
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
    return sorted(
        {
            value.strip().upper()
            for value in str(raw or "").split(",")
            if value.strip()
        }
    )


def resolve_limit(requested: int | None, configured: int) -> int:
    value = configured if requested is None else requested
    if value < 0:
        raise ValueError("Parser limits must be zero or positive")
    return value


def build_hydration_command(
    *,
    python_executable: Path,
    config_path: Path,
    db_path: Path,
    asof_date: str,
    tickers: list[str],
) -> list[str]:
    if not tickers:
        raise ValueError("Cache hydration requires at least one ticker")
    return [
        str(python_executable),
        str(
            PACKAGE_ROOT
            / "scripts"
            / "07_sync_machinery_sec_fundamentals.py"
        ),
        "--config",
        str(config_path),
        "--db",
        str(db_path),
        "--asof",
        asof_date,
        "--tickers",
        ",".join(sorted(set(tickers))),
        "--include-historical",
        "--archive-bootstrap",
        "--allow-partial",
    ]


def _preflight_plan(
    *,
    db_path: Path,
    cache_dir: Path,
    asof_date: str,
    tickers: list[str],
    accessions: list[str],
    max_filings: int,
    max_documents: int,
    force: bool,
    all_metrics: bool,
) -> PlanSummary:
    registry = load_registry(ADAPTER)
    with connect_database(db_path) as conn:
        summary = audit_cache_completeness(
            conn,
            registry=registry,
            adapter_path=ADAPTER,
            asof_date=asof_date,
            cache_dir=cache_dir,
            tickers=tickers or None,
            accessions=accessions or None,
            max_filings_per_ticker=max_filings,
            max_documents_per_filing=max_documents,
            force=force,
            all_metrics=all_metrics,
        )
    return summary


def _validate_modes(args: argparse.Namespace) -> None:
    if args.hydration_only:
        args.hydrate_missing_cache = True
    if args.reassess_run_id and (
        args.hydrate_missing_cache
        or args.hydration_only
        or args.plan_only
        or args.force
        or args.all_metrics
    ):
        raise ValueError(
            "--reassess-run-id cannot be combined with hydration, "
            "--plan-only, --force, or --all-metrics"
        )
    if args.tickers and args.ticker_cohort is not None:
        raise ValueError("--tickers and --ticker-cohort are mutually exclusive")
    if args.artifact_label and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*",
        args.artifact_label,
    ):
        raise ValueError(
            "--artifact-label must contain only letters, numbers, '.', '_', "
            "or '-' and must start with a letter or number"
        )
    if not args.reassess_run_id and not args.asof:
        raise ValueError("--asof is required unless --reassess-run-id is used")


def validate_cache_window_config(
    *,
    parser_max_filings: int,
    archive_max_filings: int,
) -> None:
    if (
        parser_max_filings > 0
        and archive_max_filings > 0
        and archive_max_filings < parser_max_filings
    ):
        raise ValueError(
            "sec_archive.max_filings_per_ticker must be zero/unlimited or "
            "at least dedicated_parser.max_filings_per_ticker; "
            f"received archive={archive_max_filings}, "
            f"parser={parser_max_filings}"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _validate_modes(args)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
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
            "../../output/industrials/machinery/dedicated_parser",
        ),
        base_dir=base_dir,
    )
    provider_state_dir = resolve_path(
        cfg_get(
            config,
            "dedicated_parser.provider_state_dir",
            "../../tmp/edgartools",
        ),
        base_dir=base_dir,
    )
    workers = args.workers or int(
        cfg_get(config, "dedicated_parser.workers", 4)
    )
    max_filings = resolve_limit(
        args.max_filings_per_ticker,
        int(cfg_get(config, "dedicated_parser.max_filings_per_ticker", 8)),
    )
    max_documents = resolve_limit(
        args.max_documents_per_filing,
        int(
            cfg_get(
                config,
                "dedicated_parser.max_documents_per_filing",
                16,
            )
        ),
    )
    validate_cache_window_config(
        parser_max_filings=max_filings,
        archive_max_filings=int(
            cfg_get(config, "sec_archive.max_filings_per_ticker", 0)
        ),
    )
    max_pdf_pages = int(
        cfg_get(config, "dedicated_parser.max_pdf_pages", 250)
    )
    max_pdf_bytes = int(
        cfg_get(
            config,
            "dedicated_parser.max_pdf_bytes",
            25_000_000,
        )
    )
    pdf_timeout = float(
        cfg_get(
            config,
            "dedicated_parser.pdf_extraction_timeout_seconds",
            30.0,
        )
    )
    pdf_ocr_enabled = bool(
        cfg_get(config, "dedicated_parser.pdf_ocr_enabled", False)
    ) and not args.disable_pdf_ocr
    asof_date = args.asof
    if args.reassess_run_id and not asof_date:
        with connect_database(db_path) as conn:
            asof_date = str(
                load_run(conn, run_id=args.reassess_run_id)["asof_date"]
            )
    output_dir = output_root / asof_date
    if args.artifact_label:
        output_dir /= args.artifact_label
    output_filename = (
        "dedicated_parser_assessment_only_run.json"
        if args.reassess_run_id
        else "dedicated_parser_plan.json"
        if args.plan_only
        else "dedicated_parser_shadow_run.json"
    )

    if args.hydrate_missing_cache:
        tickers = (
            load_cohort_tickers(args.ticker_cohort)
            if args.ticker_cohort is not None
            else _split_values(args.tickers)
        )
        accessions = _split_values(args.accessions)
        before = _preflight_plan(
            db_path=db_path,
            cache_dir=cache_dir,
            asof_date=asof_date,
            tickers=tickers,
            accessions=accessions,
            max_filings=max_filings,
            max_documents=max_documents,
            force=args.force,
            all_metrics=args.all_metrics,
        )
        missing_tickers = sorted(
            {
                str(row.get("ticker") or "").strip().upper()
                for row in before.missing_cache_details
                if str(row.get("ticker") or "").strip()
            }
        )
        manifest_path = (
            output_dir / "dedicated_parser_cache_hydration.json"
        )
        manifest: dict[str, object] = {
            "asof_date": asof_date,
            "status": (
                "CACHE_COMPLETE"
                if not before.missing_cache_accessions
                else "HYDRATION_PENDING"
            ),
            "before": asdict(before),
            "affected_tickers": missing_tickers,
            "sync_command": [],
        }
        if missing_tickers:
            sync_command = build_hydration_command(
                python_executable=(
                    args.sec_sync_python.expanduser().resolve()
                    if args.sec_sync_python
                    else Path(sys.executable).resolve()
                ),
                config_path=config_path,
                db_path=db_path,
                asof_date=asof_date,
                tickers=missing_tickers,
            )
            manifest["sync_command"] = sync_command
            _write_json(manifest_path, manifest)
            result = subprocess.run(
                sync_command,
                cwd=PROJECT_ROOT,
                check=False,
            )
            manifest["sync_return_code"] = result.returncode
            if result.returncode:
                manifest["status"] = "SYNC_FAILED"
                _write_json(manifest_path, manifest)
                print(json.dumps(manifest, indent=2, sort_keys=True))
                return 3
        after = _preflight_plan(
            db_path=db_path,
            cache_dir=cache_dir,
            asof_date=asof_date,
            tickers=tickers,
            accessions=accessions,
            max_filings=max_filings,
            max_documents=max_documents,
            force=args.force,
            all_metrics=args.all_metrics,
        )
        manifest["after"] = asdict(after)
        manifest["status"] = (
            "CACHE_COMPLETE"
            if not after.missing_cache_accessions
            else "CACHE_INCOMPLETE"
        )
        _write_json(manifest_path, manifest)
        if args.hydration_only:
            print(json.dumps(manifest, indent=2, sort_keys=True))
            return 0 if not after.missing_cache_accessions else 2

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
        str(max_pdf_pages),
        "--max-pdf-bytes",
        str(max_pdf_bytes),
        "--pdf-extraction-timeout-seconds",
        str(pdf_timeout),
        "--output-json",
        str(output_dir / output_filename),
        "--cache-gate-output-json",
        str(output_dir / "dedicated_parser_cache_gate.json"),
    ]
    if asof_date:
        forwarded.extend(["--asof", asof_date])
    if args.tickers:
        forwarded.extend(["--tickers", args.tickers])
    if args.ticker_cohort is not None:
        forwarded.extend(
            ["--ticker-cohort", str(args.ticker_cohort.expanduser().resolve())]
        )
    if args.accessions:
        forwarded.extend(["--accessions", args.accessions])
    if args.plan_only:
        forwarded.append("--plan-only")
    if args.force:
        forwarded.append("--force")
    if args.all_metrics:
        forwarded.append("--all-metrics")
    if args.require_complete_cache:
        forwarded.append("--require-complete-cache")
    if args.reassess_run_id:
        forwarded.extend(
            ["--reassess-run-id", str(args.reassess_run_id)]
        )
    if args.disable_arelle:
        forwarded.append("--disable-arelle")
    if args.disable_edgartools:
        forwarded.append("--disable-edgartools")
    if pdf_ocr_enabled:
        forwarded.append("--enable-pdf-ocr")
    return parser_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
