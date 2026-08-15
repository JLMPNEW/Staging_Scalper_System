"""Crash-safe sealing and recovery for provider capture artifacts."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest


MANIFEST_SCHEMA_VERSION = "provider_capture_manifest_v2"
REPORT_ORDER_SCHEMA = "provider_endpoint_symbol_v1"
REPORT_FIELDS = (
    "provider",
    "endpoint_id",
    "provider_symbol",
    "ticker",
    "status",
    "http_status",
    "elapsed_ms",
    "provider_rows",
    "normalized_rows",
    "request_started_at_utc",
    "response_received_at_utc",
    "response_sha256",
    "detail",
)


def _report_identity(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    provider = str(row["provider"]).strip()
    endpoint_id = str(row["endpoint_id"]).strip()
    ticker = str(row["ticker"]).strip().upper()
    provider_symbol = str(row.get("provider_symbol", ticker)).strip().upper()
    if not provider or not endpoint_id or not ticker or not provider_symbol:
        raise ValueError("Capture report identity fields must be non-empty")
    return provider, endpoint_id, provider_symbol, ticker


def capture_report_order(requests: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    return [list(_report_identity(row)[:3]) for row in requests]


def capture_report_rows(requests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    report_rows: list[dict[str, Any]] = []
    for row in requests:
        provider, endpoint_id, provider_symbol, ticker = _report_identity(row)
        http_status_raw = row.get("http_status")
        report_rows.append(
            {
                "provider": provider,
                "endpoint_id": endpoint_id,
                "provider_symbol": provider_symbol,
                "ticker": ticker,
                "status": str(row["status"]),
                "http_status": "" if http_status_raw in (None, "") else int(http_status_raw),
                "elapsed_ms": int(row.get("elapsed_ms", 0)),
                "provider_rows": int(row.get("provider_row_count", 0)),
                "normalized_rows": len(row.get("normalized_rows", [])),
                "request_started_at_utc": str(row["request_started_at_utc"]),
                "response_received_at_utc": str(row["response_received_at_utc"]),
                "response_sha256": str(row.get("response_sha256", "")),
                "detail": str(row.get("detail", "")),
            }
        )
    return report_rows


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata_json", "{}")
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid capture-run metadata for {row.get('cycle_id', '')}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Capture-run metadata must be an object: {row.get('cycle_id', '')}")
    return payload


def capture_manifest_payload(
    *,
    row: Mapping[str, Any],
    member_count: int,
    providers: list[str],
    store_path: Path,
) -> dict[str, Any]:
    metadata = _metadata(row)
    contract = metadata.get("artifact_contract")
    if not isinstance(contract, dict):
        raise ValueError(f"Capture run lacks artifact recovery contract: {row['cycle_id']}")
    inputs = contract.get("inputs_sha256")
    if not isinstance(inputs, dict) or not inputs:
        raise ValueError(f"Capture run lacks sealed input hashes: {row['cycle_id']}")
    report_name = str(contract.get("report_name", ""))
    report_sha = str(contract.get("report_sha256", ""))
    if not report_name or not report_sha:
        raise ValueError(f"Capture run lacks report evidence: {row['cycle_id']}")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "acceptance": str(row["status"]),
        "cycle_id": str(row["cycle_id"]),
        "capture_phase": str(row["capture_phase"]),
        "actual_capture_date": str(row["actual_capture_date"]),
        "requested_portfolio_as_of": str(row["requested_portfolio_as_of"]),
        "universe_as_of": str(metadata.get("universe_as_of", "")),
        "universe_freshness": metadata.get("universe_freshness", {}),
        "universe_member_count": int(member_count),
        "providers": providers,
        "store_path": str(store_path),
        "store_result": {
            "idempotent": False,
            "run_id": str(row["run_id"]),
            "run_digest": str(row["run_digest"]),
            "status": str(row["status"]),
            "request_count": int(row["request_count"]),
            "normalized_row_count": int(row["normalized_row_count"]),
            "new_version_count": int(row["new_version_count"]),
            "unchanged_observation_count": int(row["unchanged_observation_count"]),
            "previous_pass_digest": str(row["previous_pass_digest"]),
        },
        "acceptance_diagnostics": metadata.get("acceptance_diagnostics", {}),
        "raw_payloads_retained": False,
        "inputs_sha256": {str(key): str(value) for key, value in inputs.items()},
        "outputs_sha256": {report_name: report_sha},
    }


def _repair_capture_report(
    conn: sqlite3.Connection,
    *,
    row: Mapping[str, Any],
    contract: Mapping[str, Any],
    report_path: Path,
    expected_sha: str,
) -> str | None:
    if contract.get("report_order_schema") != REPORT_ORDER_SCHEMA:
        return "report_order_contract_missing"
    raw_order = contract.get("report_order")
    if not isinstance(raw_order, list) or not raw_order:
        return "report_order_missing"
    order: list[tuple[str, str, str]] = []
    for value in raw_order:
        if not isinstance(value, list) or len(value) != 3 or any(not str(part).strip() for part in value):
            return "report_order_invalid"
        order.append((str(value[0]), str(value[1]), str(value[2]).strip().upper()))
    if len(order) != len(set(order)):
        return "report_order_duplicate"

    stored = conn.execute(
        "SELECT provider,endpoint_id,provider_symbol,ticker,status,http_status,elapsed_ms,"
        "provider_row_count,normalized_row_count,request_started_at_utc,"
        "response_received_at_utc,response_sha256,detail FROM capture_requests WHERE run_id=?",
        (str(row["run_id"]),),
    ).fetchall()
    by_identity = {
        (str(value["provider"]), str(value["endpoint_id"]), str(value["provider_symbol"]).upper()): value
        for value in stored
    }
    if len(by_identity) != len(stored) or set(order) != set(by_identity):
        return "report_order_db_mismatch"
    report_rows = [
        {
            "provider": by_identity[key]["provider"],
            "endpoint_id": by_identity[key]["endpoint_id"],
            "provider_symbol": by_identity[key]["provider_symbol"],
            "ticker": by_identity[key]["ticker"],
            "status": by_identity[key]["status"],
            "http_status": by_identity[key]["http_status"],
            "elapsed_ms": by_identity[key]["elapsed_ms"],
            "provider_rows": by_identity[key]["provider_row_count"],
            "normalized_rows": by_identity[key]["normalized_row_count"],
            "request_started_at_utc": by_identity[key]["request_started_at_utc"],
            "response_received_at_utc": by_identity[key]["response_received_at_utc"],
            "response_sha256": by_identity[key]["response_sha256"],
            "detail": by_identity[key]["detail"],
        }
        for key in order
    ]
    staging = report_path.with_name(f"{report_path.name}.{os.getpid()}.recovery.tmp")
    try:
        write_csv(staging, REPORT_FIELDS, report_rows)
        if sha256_file(staging) != expected_sha:
            return "report_reconstruction_hash_mismatch"
        staging.replace(report_path)
    except (OSError, ValueError) as exc:
        return f"report_reconstruction_failed:{type(exc).__name__}"
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
    return None


def ensure_capture_manifest(
    conn: sqlite3.Connection,
    *,
    row: Mapping[str, Any],
    output_root: Path | None = None,
    cycle_dir: Path | None = None,
    store_path: Path,
) -> tuple[Path | None, list[str]]:
    """Repair a missing manifest from DB-sealed evidence; never replace one."""
    cycle_id = str(row["cycle_id"])
    if (output_root is None) == (cycle_dir is None):
        raise ValueError("Specify exactly one of output_root or cycle_dir")
    if output_root is not None:
        cycle_root = output_root.resolve()
        cycle_dir = (cycle_root / cycle_id).resolve()
        try:
            cycle_dir.relative_to(cycle_root)
        except ValueError:
            return None, [f"cycle_path_outside_output_root:{cycle_id}"]
    else:
        assert cycle_dir is not None
        cycle_dir = cycle_dir.resolve()
    manifest_path = cycle_dir / "capture_manifest.json"
    try:
        metadata = _metadata(row)
    except ValueError as exc:
        return None, [f"artifact_recovery_metadata_invalid:{cycle_id}:{exc}"]
    contract = metadata.get("artifact_contract")
    if not isinstance(contract, dict):
        if manifest_path.is_file():
            return manifest_path, []
        return None, [f"artifact_recovery_contract_missing:{cycle_id}"]
    report_name = str(contract.get("report_name", ""))
    expected_sha = str(contract.get("report_sha256", ""))
    if not report_name or Path(report_name).name != report_name:
        return None, [f"artifact_recovery_report_path_invalid:{cycle_id}"]
    report_path = (cycle_dir / report_name).resolve()
    try:
        report_path.relative_to(cycle_dir)
    except ValueError:
        return None, [f"artifact_recovery_report_path_invalid:{cycle_id}"]
    if len(expected_sha) != 64 or any(character not in "0123456789abcdef" for character in expected_sha.casefold()):
        return None, [f"artifact_recovery_report_hash_invalid:{cycle_id}"]
    report_missing = not report_path.is_file()
    try:
        report_mismatch = not report_missing and sha256_file(report_path) != expected_sha
    except OSError:
        report_mismatch = True
    if report_missing or report_mismatch:
        repair_error = _repair_capture_report(
            conn,
            row=row,
            contract=contract,
            report_path=report_path,
            expected_sha=expected_sha,
        )
        if repair_error:
            condition = "missing" if report_missing else "hash_mismatch"
            return None, [f"artifact_recovery_report_{condition}:{cycle_id}:{repair_error}"]
    universe = conn.execute(
        "SELECT member_count FROM capture_universes WHERE universe_id=?",
        (str(row["universe_id"]),),
    ).fetchone()
    if universe is None:
        return None, [f"artifact_recovery_universe_missing:{cycle_id}"]
    providers = [
        str(value[0])
        for value in conn.execute(
            "SELECT DISTINCT provider FROM capture_requests WHERE run_id=? ORDER BY provider",
            (str(row["run_id"]),),
        ).fetchall()
    ]
    if not providers:
        return None, [f"artifact_recovery_providers_missing:{cycle_id}"]
    payload = capture_manifest_payload(
        row=row,
        member_count=int(universe["member_count"]),
        providers=providers,
        store_path=store_path,
    )
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, [f"artifact_existing_manifest_invalid:{cycle_id}:{type(exc).__name__}"]
        if existing != payload:
            return None, [f"artifact_existing_manifest_mismatch:{cycle_id}"]
        return manifest_path, []
    write_manifest(manifest_path, payload)
    return manifest_path, []
