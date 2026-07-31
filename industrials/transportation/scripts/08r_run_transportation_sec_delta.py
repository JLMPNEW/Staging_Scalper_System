#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.cli import main as parser_main  # noqa: E402
from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402
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
from industrials.transportation.sec_delta_execution import (  # noqa: E402
    SEC_DELTA_EXECUTION_VERSION,
    validate_execution_payload,
    validate_execution_preflight,
)


ADAPTER = (
    "industrials.transportation.dedicated_parser_adapter:"
    "extract_metric_evidence"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Execute exactly the hash-sealed transportation SEC delta. "
            "The command is resumable, writes shadow parser evidence only, "
            "and cannot build features, calibrate, rank, or publish."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--base-run-id", type=int, default=58)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--status", action="store_true")
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
    )


def _parser_args(
    *,
    db_path: Path,
    cache_dir: Path,
    parser_cfg: Mapping[str, Any],
    base_dir: Path,
    source_csv: Path,
    run_dir: Path,
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
        str(parser_cfg["source_census_asof_date"]),
        "--source-manifest",
        str(source_csv),
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
        # 0 means unlimited; only a missing key may fall back.
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
            float(
                parser_cfg["pdf_extraction_timeout_seconds"]
                if parser_cfg.get("pdf_extraction_timeout_seconds")
                is not None
                else 30.0
            )
        ),
        "--all-metrics",
        "--require-complete-cache",
        "--skip-adjudication-skeleton",
        "--output-json",
        str(run_dir / "transportation_sec_delta_run.json"),
        "--cache-gate-output-json",
        str(run_dir / "transportation_sec_delta_cache_gate.json"),
    ]


def _status(
    *,
    db_path: Path,
    gate_path: Path,
    source_csv_sha256: str,
) -> int:
    gate = _read_json(gate_path) if gate_path.is_file() else {}
    if gate.get("acceptance") == "PASS":
        payload = {**gate, "status_query": "COMPLETED_GATE"}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    status: dict[str, Any] = {
        "gate": "DP6F_SEC_DELTA_EXECUTION_STATUS",
        "execution_gate_status": gate.get("acceptance") or "NOT_STARTED",
        "source_manifest_sha256": source_csv_sha256,
        "process_id": gate.get("process_id"),
        "run_status": "PLANNING_OR_NOT_STARTED",
    }
    with contextlib.closing(
        connect_database(db_path, readonly=True)
    ) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM sec_parser_run
            WHERE model_family=?
            ORDER BY run_id DESC
            LIMIT 20
            """,
            (MODEL_FAMILY,),
        ).fetchall()
        matched = None
        for row in rows:
            candidate = dict(row)
            try:
                metadata = json.loads(
                    str(candidate.get("metadata_json") or "{}")
                )
            except json.JSONDecodeError:
                continue
            plan = metadata.get("plan") or {}
            execution = plan.get("execution_scope") or {}
            source = execution.get("source_manifest") or {}
            if str(source.get("sha256") or "") == source_csv_sha256:
                matched = candidate
                break
        if matched is not None:
            run_id = int(matched["run_id"])
            counts = connection.execute(
                """
                SELECT ledger.status, COUNT(*) AS row_count
                FROM sec_parser_run_work AS relation
                JOIN sec_parser_work_ledger AS ledger
                  ON ledger.work_key=relation.work_key
                WHERE relation.run_id=?
                GROUP BY ledger.status
                """,
                (run_id,),
            ).fetchall()
            status.update(
                {
                    "run_id": run_id,
                    "run_status": matched["status"],
                    "planned_work_count": matched[
                        "planned_work_count"
                    ],
                    "completed_work_count": matched[
                        "completed_work_count"
                    ],
                    "failed_work_count": matched["failed_work_count"],
                    "ledger_status_counts": {
                        str(row["status"]): int(row["row_count"])
                        for row in counts
                    },
                }
            )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "The general transportation parser switch must remain false; "
            "08r authorizes only the current sealed SEC delta."
        )
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / str(parser_cfg["source_census_asof_date"])
    )
    run_dir = output_dir / "sec_delta_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    source_csv = (
        output_dir / "transportation_delta_parser_source_manifest.csv"
    )
    source_json = (
        output_dir / "transportation_delta_parser_source_manifest.json"
    )
    plan_path = output_dir / "transportation_delta_parser_plan.json"
    plan_gate_path = (
        output_dir / "transportation_delta_parser_plan_gate.json"
    )
    gate_path = (
        output_dir / "transportation_sec_delta_execution_gate.json"
    )
    result_path = run_dir / "transportation_sec_delta_run.json"
    for path in (
        source_csv,
        source_json,
        plan_path,
        plan_gate_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    source = _read_json(source_json)
    plan = _read_json(plan_path)
    plan_gate = _read_json(plan_gate_path)
    registry = load_registry(ADAPTER)
    source_hash = file_sha256(source_csv)
    preflight_errors = validate_execution_preflight(
        source_manifest=source,
        source_csv_sha256=source_hash,
        plan_gate=plan_gate,
        plan_payload=plan,
        adapter_version=registry.adapter_version,
        parser_metric_count=len(registry.parser_metrics),
    )
    if preflight_errors:
        raise ValueError(
            "SEC delta execution preflight failed: "
            + "; ".join(preflight_errors)
        )
    if args.status:
        return _status(
            db_path=foundation.db_path,
            gate_path=gate_path,
            source_csv_sha256=source_hash,
        )
    if gate_path.is_file():
        prior = _read_json(gate_path)
        if (
            prior.get("acceptance") == "PASS"
            and prior.get("source_manifest_sha256") == source_hash
            and prior.get("adapter_version")
            == registry.adapter_version
        ):
            print(
                json.dumps(
                    {**prior, "idempotent_reuse": True},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    preflight = {
        "acceptance": "PASS_PREFLIGHT",
        "gate": "DP6F_SEC_DELTA_EXECUTION_PREFLIGHT",
        "execution_version": SEC_DELTA_EXECUTION_VERSION,
        "model_family": MODEL_FAMILY,
        "base_run_id": args.base_run_id,
        "source_manifest_path": str(source_csv.resolve()),
        "source_manifest_sha256": source_hash,
        "source_document_count": int(
            source.get("selected_document_row_count") or 0
        ),
        "source_accession_count": int(
            source.get("selected_accession_count") or 0
        ),
        "adapter_version": registry.adapter_version,
        "parser_metric_count": len(registry.parser_metrics),
        "review_policy_sha256": (
            file_sha256(
                Path(registry.review_policy_path).expanduser().resolve()
            )
            if registry.review_policy_path
            else ""
        ),
        "plan_sha256": file_sha256(plan_path),
        "missing_cache_accessions": 0,
        "general_parser_execution_authorized": False,
        "one_shot_execution_authorized": bool(args.execute),
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": [],
    }
    if not args.execute:
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 0
    workers = args.workers or int(parser_cfg.get("workers") or 4)
    if workers < 1:
        raise ValueError("workers must be at least 1")
    running = {
        **preflight,
        "acceptance": "RUNNING",
        "gate": "DP6F_SEC_DELTA_EXECUTION",
        "process_id": os.getpid(),
        "workers": workers,
        "parser_invocations": 1,
    }
    _write_json(gate_path, running)
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=base_dir,
    )
    parser_stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(parser_stdout):
            code = parser_main(
                _parser_args(
                    db_path=foundation.db_path,
                    cache_dir=cache_dir,
                    parser_cfg=parser_cfg,
                    base_dir=base_dir,
                    source_csv=source_csv,
                    run_dir=run_dir,
                    workers=workers,
                )
            )
        result = _read_json(result_path)
        errors = validate_execution_payload(
            payload=result,
            source_manifest=source,
            source_csv_sha256=source_hash,
            parser_return_code=code,
        )
    except BaseException as exc:
        _write_json(
            gate_path,
            {
                **running,
                "acceptance": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise
    summary = result.get("summary") or {}
    gate = {
        **running,
        "acceptance": "PASS" if not errors else "FAIL",
        "parser_return_code": code,
        "run_id": int(result.get("run_id") or 0),
        "newly_executed_work_count": int(
            result.get("completed_work_count") or 0
        ),
        "resume_linked_work_count": int(
            summary.get("linked_completed_work_count") or 0
        ),
        "failed_work_count": int(
            result.get("failed_work_count") or 0
        ),
        "effective_completed_work_count": (
            int(result.get("completed_work_count") or 0)
            + int(summary.get("linked_completed_work_count") or 0)
        ),
        "captured_parser_stdout_character_count": len(
            parser_stdout.getvalue()
        ),
        "result_path": str(result_path.resolve()),
        "result_sha256": file_sha256(result_path),
        "errors": errors,
        "next_gate": (
            "BUILD_SEC_UNION_COVERAGE_ONLY"
            if not errors
            else "RESUME_SEALED_SEC_DELTA"
        ),
    }
    _write_json(gate_path, gate)
    print(json.dumps(gate, indent=2, sort_keys=True))
    return 0 if gate["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
