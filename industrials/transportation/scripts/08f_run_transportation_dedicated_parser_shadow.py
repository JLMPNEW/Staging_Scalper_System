#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.cli import main as parser_main  # noqa: E402
from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


ADAPTER = (
    "industrials.transportation.dedicated_parser_adapter:"
    "extract_metric_evidence"
)
SYNC_SCRIPT = (
    PROJECT_ROOT
    / "industrials"
    / "scripts"
    / "07_sync_industrials_sec_fundamentals.py"
)
CENSUS_BUILD_SCRIPT = Path(__file__).with_name(
    "00c_build_transportation_source_census.py"
)
CENSUS_VALIDATE_SCRIPT = Path(__file__).with_name(
    "00c_validate_transportation_source_census.py"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the gated transportation DP3-DP5 dedicated-parser sequence. "
            "Hydration is exact-gap and cache-only; parsing is sealed-manifest "
            "only and remains shadow/non-production."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=0)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--hydrate-missing-cache", action="store_true")
    modes.add_argument("--plan-only", action="store_true")
    modes.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def _write_json(path: Path, payload: object) -> None:
    write_text_atomic(
        path,
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {
                str(key): str(value or "").strip()
                for key, value in row.items()
            }
            for row in csv.DictReader(handle)
        ]


def _artifact_paths(
    *,
    parser_cfg: Mapping[str, Any],
    base_dir: Path,
    asof_date: str,
) -> dict[str, Path]:
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    return {
        "census": resolve_path(
            parser_cfg["source_census_csv"],
            base_dir=base_dir,
        ),
        "gaps": resolve_path(
            parser_cfg["source_cache_gaps_csv"],
            base_dir=base_dir,
        ),
        "manifest": resolve_path(
            parser_cfg["source_census_manifest_json"],
            base_dir=base_dir,
        ),
        "output_dir": output_dir,
        "hydration_sync": (
            output_dir / "transportation_dp3_cache_hydration_sync.csv"
        ),
        "hydration_manifest": (
            output_dir / "transportation_dp3_cache_hydration.json"
        ),
        "plan": (
            output_dir / "transportation_dedicated_parser_plan.json"
        ),
        "plan_gate": (
            output_dir / "transportation_dp4_plan_gate.json"
        ),
        "shadow_run": (
            output_dir / "transportation_dedicated_parser_shadow_run.json"
        ),
        "cache_gate": (
            output_dir / "transportation_dedicated_parser_cache_gate.json"
        ),
    }


def build_hydration_command(
    *,
    config_path: Path,
    db_path: Path,
    asof_date: str,
    gaps_path: Path,
    output_csv: Path,
    tickers: list[str],
    workers: int,
) -> list[str]:
    if not tickers:
        raise ValueError("Exact-gap hydration requires at least one ticker")
    registry = load_registry(ADAPTER)
    return [
        sys.executable,
        str(SYNC_SCRIPT),
        "--config",
        str(config_path),
        "--db",
        str(db_path),
        "--model-family",
        MODEL_FAMILY,
        "--asof",
        asof_date,
        "--tickers",
        ",".join(sorted(set(tickers))),
        "--include-historical",
        "--archive-selected",
        "--archive-bootstrap",
        "--archive-cache-only",
        "--archive-scan-all-documents",
        "--archive-max-filings-per-ticker",
        "0",
        "--archive-max-documents-per-filing",
        "0",
        "--archive-cache-workers",
        str(workers),
        "--archive-document-keywords",
        ",".join(registry.document_keywords),
        "--archive-accession-scope-csv",
        str(gaps_path),
        "--allow-partial",
        "--skip-source-registry",
        "--output-csv",
        str(output_csv),
    ]


def _run_command(command: list[str]) -> int:
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
    ).returncode


def _reseal_census(*, config_path: Path, db_path: Path) -> tuple[int, int]:
    build_code = _run_command(
        [
            sys.executable,
            str(CENSUS_BUILD_SCRIPT),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
        ]
    )
    if build_code not in {0, 2}:
        return build_code, -1
    validate_code = _run_command(
        [
            sys.executable,
            str(CENSUS_VALIDATE_SCRIPT),
            "--config",
            str(config_path),
            "--verify-content-hashes",
        ]
    )
    return build_code, validate_code


def hydrate_exact_gaps(
    *,
    config_path: Path,
    db_path: Path,
    parser_cfg: Mapping[str, Any],
    base_dir: Path,
    paths: Mapping[str, Path],
    workers: int,
) -> int:
    maximum_passes = int(parser_cfg.get("hydration_passes") or 3)
    if maximum_passes < 1:
        raise ValueError("transportation hydration_passes must be at least 1")
    records: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "model_family": MODEL_FAMILY,
        "process_id": os.getpid(),
        "mode": "exact_gap_cache_only",
        "network_scope": "sealed_dp3_gaps_only",
        "financial_fact_writes": False,
        "feature_build_invocations": 0,
        "calibration_invocations": 0,
        "passes": records,
    }
    for pass_number in range(1, maximum_passes + 1):
        manifest = _read_json(paths["manifest"])
        gaps = _read_csv(paths["gaps"])
        unresolved = [
            row for row in gaps if not row.get("gap_disposition")
        ]
        before = len(unresolved)
        if before == 0:
            _, validation_code = _reseal_census(
                config_path=config_path,
                db_path=db_path,
            )
            final = _read_json(paths["manifest"])
            result.update(
                {
                    "status": (
                        "CACHE_COMPLETE"
                        if validation_code == 0
                        and final.get("acceptance") == "PASS"
                        else "RESEAL_FAILED"
                    ),
                    "initial_gap_count": (
                        records[0]["before_gap_count"] if records else 0
                    ),
                    "remaining_gap_count": int(
                        final.get("unresolved_gap_count") or 0
                    ),
                    "source_census_acceptance": final.get("acceptance"),
                    "source_census_sha256": file_sha256(paths["census"]),
                }
            )
            _write_json(paths["hydration_manifest"], result)
            return 0 if result["status"] == "CACHE_COMPLETE" else 2
        invalid = [
            row
            for row in unresolved
            if row.get("gap_type") != "SOURCE_DOCUMENT"
            or row.get("required_action") != "HYDRATE_SEALED_DOCUMENT"
            or not row.get("ticker")
            or not row.get("accession_number")
        ]
        if invalid:
            raise ValueError(
                "DP3 gaps include rows outside the exact document-hydration "
                f"contract: {invalid[:3]}"
            )
        tickers = sorted({row["ticker"].upper() for row in unresolved})
        command = build_hydration_command(
            config_path=config_path,
            db_path=db_path,
            asof_date=str(manifest["asof_date"]),
            gaps_path=paths["gaps"],
            output_csv=paths["hydration_sync"],
            tickers=tickers,
            workers=workers,
        )
        record: dict[str, Any] = {
            "pass_number": pass_number,
            "before_gap_count": before,
            "ticker_count": len(tickers),
            "command": command,
        }
        records.append(record)
        result["status"] = "HYDRATING"
        _write_json(paths["hydration_manifest"], result)
        record["sync_return_code"] = _run_command(command)
        build_code, validation_code = _reseal_census(
            config_path=config_path,
            db_path=db_path,
        )
        final = _read_json(paths["manifest"])
        after = int(final.get("unresolved_gap_count") or 0)
        record.update(
            {
                "census_build_return_code": build_code,
                "census_validation_return_code": validation_code,
                "after_gap_count": after,
                "hydrated_gap_count": before - after,
            }
        )
        if build_code not in {0, 2} or validation_code != 0:
            result["status"] = "RESEAL_FAILED"
            break
        if after == 0 and final.get("acceptance") == "PASS":
            result["status"] = "CACHE_COMPLETE"
            break
        if after >= before:
            result["status"] = "NO_PROGRESS"
            break
    final = _read_json(paths["manifest"])
    result.update(
        {
            "initial_gap_count": (
                records[0]["before_gap_count"] if records else 0
            ),
            "remaining_gap_count": int(
                final.get("unresolved_gap_count") or 0
            ),
            "source_census_acceptance": final.get("acceptance"),
            "source_census_sha256": file_sha256(paths["census"]),
        }
    )
    _write_json(paths["hydration_manifest"], result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "CACHE_COMPLETE" else 2


def _parser_args(
    *,
    db_path: Path,
    cache_dir: Path,
    parser_cfg: Mapping[str, Any],
    base_dir: Path,
    paths: Mapping[str, Path],
    asof_date: str,
    workers: int,
    output_path: Path,
) -> list[str]:
    args = [
        "--db",
        str(db_path),
        "--cache-dir",
        str(cache_dir),
        "--adapter",
        ADAPTER,
        "--asof",
        asof_date,
        "--source-manifest",
        str(paths["census"]),
        "--workers",
        str(workers),
        "--max-filings-per-ticker",
        "0",
        "--max-documents-per-filing",
        "0",
        "--provider-state-dir",
        str(
            resolve_path(
                parser_cfg["provider_state_dir"],
                base_dir=base_dir,
            )
        ),
        "--max-pdf-pages",
        # 0 means unlimited in the extraction stack; only a missing key may
        # fall back, never a configured zero.
        str(
            int(parser_cfg["max_pdf_pages"])
            if parser_cfg.get("max_pdf_pages") is not None
            else 250
        ),
        "--max-pdf-bytes",
        str(
            int(parser_cfg["max_pdf_bytes"])
            if parser_cfg.get("max_pdf_bytes") is not None
            else 25_000_000
        ),
        "--pdf-extraction-timeout-seconds",
        str(
            float(parser_cfg["pdf_extraction_timeout_seconds"])
            if parser_cfg.get("pdf_extraction_timeout_seconds") is not None
            else 30.0
        ),
        "--all-metrics",
        "--require-complete-cache",
        "--skip-adjudication-skeleton",
        "--output-json",
        str(output_path),
        "--cache-gate-output-json",
        str(paths["cache_gate"]),
    ]
    # The global pdf_ocr_enabled key was repurposed for the later DP6S batch;
    # the sealed DP3/DP4/DP5 contract rejects OCR, so this stage only honors
    # an explicit stage-scoped opt-in.
    if bool(parser_cfg.get("dp5_pdf_ocr_enabled")):
        args.append("--enable-pdf-ocr")
    return args


def _validate_dp3_ready(
    *,
    manifest: Mapping[str, Any],
    paths: Mapping[str, Path],
) -> None:
    if manifest.get("acceptance") != "PASS":
        raise ValueError(
            "DP3 source census is not PASS; hydrate or adjudicate exact gaps "
            "before DP4"
        )
    if int(manifest.get("unresolved_gap_count") or 0) != 0:
        raise ValueError("DP3 source census still has unresolved gaps")
    if int(manifest.get("selected_identity_count") or 0) != int(
        manifest.get("identity_count") or 0
    ):
        raise ValueError(
            "DP3 does not contain selected parser sources for every identity"
        )
    if manifest.get("identities_without_selected_sources"):
        raise ValueError(
            "DP3 lists identities without selected parser sources"
        )
    census_rows = _read_csv(paths["census"])
    if len(census_rows) != int(
        manifest.get("selected_document_row_count") or 0
    ):
        raise ValueError("DP3 census row count does not match its manifest")
    if any(row.get("cache_status") != "CACHED_HASHED" for row in census_rows):
        raise ValueError("DP3 census includes an unsealed source document")


def validate_plan_payload(
    *,
    payload: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
    source_manifest_sha256: str,
    parser_metric_count: int,
) -> list[str]:
    errors: list[str] = []
    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        return ["plan payload has no summary object"]
    execution = summary.get("execution_scope")
    if not isinstance(execution, Mapping):
        return ["plan payload has no execution_scope object"]
    expected_accessions = int(
        source_manifest.get("selected_accession_count") or 0
    )
    expected_documents = int(
        source_manifest.get("selected_document_row_count") or 0
    )
    scheduled = int(summary.get("scheduled_accessions") or 0)
    skipped = int(summary.get("skipped_completed_accessions") or 0)
    if payload.get("mode") != "plan_only":
        errors.append("parser mode is not plan_only")
    if int(summary.get("requested_tickers") or 0) != int(
        source_manifest.get("selected_identity_count") or 0
    ):
        errors.append(
            "requested ticker count does not equal the DP3 selected-identity "
            "count"
        )
    if int(summary.get("missing_cache_accessions") or 0) != 0:
        errors.append("plan has missing or manifest-mismatched accessions")
    if scheduled + skipped != expected_accessions:
        errors.append(
            "scheduled plus exact resume-completed accessions does not equal "
            "the DP3 accession count"
        )
    if skipped == 0 and int(summary.get("scheduled_documents") or 0) != (
        expected_documents
    ):
        errors.append("scheduled document count does not equal DP3 document count")
    work_keys = payload.get("work_keys")
    if not isinstance(work_keys, list) or len(work_keys) != scheduled:
        errors.append("work-key count does not equal scheduled accession count")
    if not execution.get("all_metrics"):
        errors.append("plan does not request all parser metrics")
    if int(execution.get("max_filings_per_ticker", -1)) != 0:
        errors.append("plan filing limit is not unlimited")
    if int(execution.get("max_documents_per_filing", -1)) != 0:
        errors.append("plan document limit is not unlimited")
    if not execution.get("enable_arelle"):
        errors.append("Arelle provider is disabled")
    if not execution.get("enable_edgartools"):
        errors.append("EdgarTools provider is disabled")
    if execution.get("enable_pdf_ocr"):
        errors.append("PDF OCR is outside the sealed DP3 contract")
    source = execution.get("source_manifest")
    if not isinstance(source, Mapping):
        errors.append("plan is not source-manifest scoped")
    else:
        if source.get("sha256") != source_manifest_sha256:
            errors.append("plan source-manifest hash does not match DP3")
        if int(source.get("row_count") or 0) != expected_documents:
            errors.append("plan source-manifest row count does not match DP3")
    if int(source_manifest.get("parser_metric_count") or 0) != (
        parser_metric_count
    ):
        errors.append("adapter parser-metric count does not match DP3")
    return errors


def run_plan_only(
    *,
    db_path: Path,
    cache_dir: Path,
    parser_cfg: Mapping[str, Any],
    base_dir: Path,
    paths: Mapping[str, Path],
    workers: int,
) -> int:
    source_manifest = _read_json(paths["manifest"])
    _validate_dp3_ready(manifest=source_manifest, paths=paths)
    code = parser_main(
        [
            *_parser_args(
                db_path=db_path,
                cache_dir=cache_dir,
                parser_cfg=parser_cfg,
                base_dir=base_dir,
                paths=paths,
                asof_date=str(source_manifest["asof_date"]),
                workers=workers,
                output_path=paths["plan"],
            ),
            "--plan-only",
        ]
    )
    payload = _read_json(paths["plan"])
    registry = load_registry(ADAPTER)
    manifest_hash = file_sha256(paths["census"])
    errors = validate_plan_payload(
        payload=payload,
        source_manifest=source_manifest,
        source_manifest_sha256=manifest_hash,
        parser_metric_count=len(registry.parser_metrics),
    )
    gate = {
        "acceptance": "PASS" if code == 0 and not errors else "FAIL",
        "model_family": MODEL_FAMILY,
        "gate": "DP4_OFFLINE_COMPLETE_CACHE_MANIFEST_ONLY_PLAN",
        "parser_return_code": code,
        "source_manifest_sha256": manifest_hash,
        "adapter_version": registry.adapter_version,
        "parser_metric_count": len(registry.parser_metrics),
        "selected_accession_count": source_manifest.get(
            "selected_accession_count"
        ),
        "selected_document_row_count": source_manifest.get(
            "selected_document_row_count"
        ),
        "errors": errors,
    }
    _write_json(paths["plan_gate"], gate)
    print(json.dumps(gate, indent=2, sort_keys=True))
    return 0 if gate["acceptance"] == "PASS" else 2


def run_exhaustive_search(
    *,
    db_path: Path,
    cache_dir: Path,
    parser_cfg: Mapping[str, Any],
    base_dir: Path,
    paths: Mapping[str, Path],
    workers: int,
) -> int:
    if not bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "DP5 is fail-closed: set transportation "
            "dedicated_parser.parser_execution_authorized=true only after a "
            "current DP4 PASS"
        )
    source_manifest = _read_json(paths["manifest"])
    _validate_dp3_ready(manifest=source_manifest, paths=paths)
    gate = _read_json(paths["plan_gate"])
    registry = load_registry(ADAPTER)
    expected_hash = file_sha256(paths["census"])
    if (
        gate.get("acceptance") != "PASS"
        or gate.get("source_manifest_sha256") != expected_hash
        or gate.get("adapter_version") != registry.adapter_version
        or int(gate.get("parser_metric_count") or 0)
        != len(registry.parser_metrics)
    ):
        raise ValueError(
            "DP5 requires a current DP4 PASS for the same source manifest, "
            "adapter version, and 84-metric registry"
        )
    return parser_main(
        _parser_args(
            db_path=db_path,
            cache_dir=cache_dir,
            parser_cfg=parser_cfg,
            base_dir=base_dir,
            paths=paths,
            asof_date=str(source_manifest["asof_date"]),
            workers=workers,
            output_path=paths["shadow_run"],
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family.get("dedicated_parser")
    if not isinstance(parser_cfg, Mapping):
        raise KeyError(
            "model_families.transportation.dedicated_parser must be a mapping"
        )
    foundation = resolve_foundation(config_path, args.db)
    base_dir = config_path.parent
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=base_dir,
    )
    asof_date = str(parser_cfg["source_census_asof_date"])
    paths = _artifact_paths(
        parser_cfg=parser_cfg,
        base_dir=base_dir,
        asof_date=asof_date,
    )
    workers = args.workers or int(parser_cfg.get("workers") or 4)
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if args.hydrate_missing_cache:
        return hydrate_exact_gaps(
            config_path=config_path,
            db_path=foundation.db_path,
            parser_cfg=parser_cfg,
            base_dir=base_dir,
            paths=paths,
            workers=workers,
        )
    if args.plan_only:
        return run_plan_only(
            db_path=foundation.db_path,
            cache_dir=cache_dir,
            parser_cfg=parser_cfg,
            base_dir=base_dir,
            paths=paths,
            workers=workers,
        )
    return run_exhaustive_search(
        db_path=foundation.db_path,
        cache_dir=cache_dir,
        parser_cfg=parser_cfg,
        base_dir=base_dir,
        paths=paths,
        workers=workers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
