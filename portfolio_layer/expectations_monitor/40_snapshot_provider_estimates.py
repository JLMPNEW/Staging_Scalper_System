#!/usr/bin/env python3
"""Fetch and append normalized FMP/Alpha estimate snapshots without retaining raw payloads."""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    append_estimate_snapshots,
    connect_monitor_db,
    utc_now,
    writer_lock,
)
from portfolio_layer.expectations_monitor.estimate_normalization import (  # noqa: E402
    capture_plan,
    normalize_estimates,
)
from portfolio_layer.expectations_monitor.provider_common import (  # noqa: E402
    ProviderPayloadResult,
    fetch_capability_payload,
    load_entitlements,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_ENTITLEMENTS = Path(__file__).with_name("provider_entitlements.yaml")
REPORT_FIELDS = [
    "provider",
    "endpoint_id",
    "symbol",
    "status",
    "http_status",
    "elapsed_ms",
    "provider_rows",
    "normalized_rows",
    "response_sha256",
    "detail",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--entitlements", type=Path, default=DEFAULT_ENTITLEMENTS)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--provider", choices=("both", "alpha_vantage", "fmp"), default="both")
    parser.add_argument("--symbols-file", type=Path)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--retrieval-cycle")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols_file is not None and args.symbols:
        raise ValueError("--symbols-file and --symbols are mutually exclusive")
    raw: list[str]
    if args.symbols_file is not None:
        with args.symbols_file.resolve().open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = {str(value).casefold(): str(value) for value in (reader.fieldnames or [])}
            key = fields.get("ticker") or fields.get("symbol")
            if key is None:
                raise ValueError("Symbols file must contain ticker or symbol")
            raw = [str(row.get(key, "")) for row in reader]
    else:
        raw = list(args.symbols or [])
    values = list(dict.fromkeys(value.strip().upper() for value in raw if value.strip()))
    if not values:
        raise ValueError("At least one symbol is required")
    if any(value == "CASH" or any(char.isspace() for char in value) for value in values):
        raise ValueError("Symbols contain CASH or whitespace")
    return values


def _run_id(provider: str, endpoint_id: str, retrieval_cycle: str) -> str:
    return hashlib.sha256(f"{provider}|{endpoint_id}|{retrieval_cycle}".encode("utf-8")).hexdigest()


def _capture_plan(provider: str) -> list[str]:
    return list(capture_plan(provider))


def _run_endpoint_id(provider: str) -> str:
    if provider == "alpha_vantage":
        return "earnings_estimates"
    if provider == "fmp":
        return "analyst_estimates_annual_and_quarterly"
    raise ValueError(f"Unsupported estimate provider: {provider}")


def _provider_run_status(provider: str, counts: Mapping[str, int]) -> str:
    del provider
    if int(counts.get("error", 0)) > 0:
        return "FAIL"
    if int(counts.get("available", 0)) <= 0:
        return "PASS_NO_COVERAGE"
    return "PASS"


def run_selftest() -> None:
    alpha = ProviderPayloadResult(
        "alpha_vantage",
        "earnings_estimates",
        "AAA",
        "2026-07-31T22:00:00+00:00",
        "2026-07-31T22:00:01+00:00",
        "AVAILABLE",
        200,
        1,
        "object.estimates",
        1,
        "date,eps_estimate_average,revenue_estimate_average",
        "ok",
        "a" * 64,
        {
            "estimates": [
                {
                    "date": "2026-12-31",
                    "horizon": "annual",
                    "eps_estimate_average": "2.5",
                    "eps_estimate_average_30_days_ago": "2.3",
                    "revenue_estimate_average": "100.0",
                }
            ]
        },
    )
    rows = normalize_estimates(
        alpha,
        snapshot_run_id="r1",
        retrieval_cycle="2026-07-31-eod",
        entitlement_version="provider_entitlements_v1:provisional_retention_v1",
    )
    assert len(rows) == 2
    assert rows[0]["estimate_average_30_days_ago"] == "2.3"
    assert "payload" not in rows[0]
    fmp_quarterly = ProviderPayloadResult(
        "fmp",
        "analyst_estimates_quarterly",
        "AAA",
        "2026-07-31T22:00:00+00:00",
        "2026-07-31T22:00:01+00:00",
        "AVAILABLE",
        200,
        1,
        "list",
        1,
        "date,epsAvg,revenueAvg",
        "ok",
        "b" * 64,
        [{"date": "2026-06-30", "epsAvg": "2.0", "revenueAvg": "100"}],
    )
    quarterly_rows = normalize_estimates(
        fmp_quarterly,
        snapshot_run_id="r2",
        retrieval_cycle="2026-07-31-quarterly",
        entitlement_version="provider_entitlements_v1:provisional_retention_v1",
    )
    assert {row["fiscal_period"] for row in quarterly_rows} == {"quarterly"}
    assert {row["estimate_type"] for row in quarterly_rows} == {
        "eps_quarterly",
        "revenue_quarterly",
    }
    assert (
        _provider_run_status("fmp", {"error": 0, "missing": 1})
        == "PASS_NO_COVERAGE"
    )
    assert (
        _provider_run_status("alpha_vantage", {"error": 0, "missing": 1})
        == "PASS_NO_COVERAGE"
    )
    print("provider estimate snapshot selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")
    symbols = _symbols(args)
    retrieval_cycle = str(args.retrieval_cycle or f"{args.as_of.isoformat()}-eod").strip()
    if not retrieval_cycle or any(char.isspace() for char in retrieval_cycle):
        raise ValueError("retrieval-cycle must be non-empty and contain no whitespace")

    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError("expectations_monitor config must be a mapping")
    retention_cfg = monitor_cfg.get("retention", {})
    if not isinstance(retention_cfg, dict):
        raise ValueError("expectations_monitor.retention must be a mapping")
    if not bool(retention_cfg.get("normalized_snapshots_enabled", False)):
        raise RuntimeError("Normalized provider snapshot retention is disabled")
    if bool(retention_cfg.get("raw_payload_retention_enabled", False)):
        raise RuntimeError("Raw provider payload retention must remain disabled")
    policy_version = str(retention_cfg.get("policy_version", "")).strip()
    if policy_version != "provisional_retention_v1":
        raise ValueError(f"Unsupported retention policy: {policy_version!r}")

    entitlements_path = args.entitlements.resolve()
    entitlements = load_entitlements(entitlements_path)
    probe_cfg = entitlements.get("probe", {})
    providers_cfg = entitlements["providers"]
    selected = ["alpha_vantage", "fmp"] if args.provider == "both" else [args.provider]
    max_caps = probe_cfg.get("max_symbols_by_provider", {})
    for provider in selected:
        if len(symbols) > int(max_caps.get(provider, 0)):
            raise ValueError(f"{provider} symbol count {len(symbols)} exceeds configured cap")
        provider_retention = providers_cfg[provider].get("retention", {})
        if provider_retention.get("status") != "provisional_user_authorized":
            raise RuntimeError(f"{provider} is not authorized for provisional normalized retention")
        if provider_retention.get("raw_payloads") != "do_not_retain":
            raise RuntimeError(f"{provider} raw-payload policy is not fail-closed")

    db_path = ensure_not_prod_path(
        args.db.resolve()
        if args.db
        else resolve_path(
            monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"),
            base_dir=config_path.parent,
        ),
        label="expectations monitor database",
    )
    timeout_sec = float(probe_cfg.get("timeout_sec", 30.0))
    max_bytes = int(probe_cfg.get("max_response_bytes", 2_000_000))
    max_retries = int(probe_cfg.get("max_retries", 1))
    db_timeout = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    entitlement_version = f"{entitlements['schema_version']}:{policy_version}"

    conn = connect_monitor_db(db_path, timeout_sec=db_timeout)
    try:
        for provider in selected:
            endpoint_id = _run_endpoint_id(provider)
            run_id = _run_id(provider, endpoint_id, retrieval_cycle)
            exists = conn.execute(
                "SELECT status FROM provider_snapshot_runs WHERE snapshot_run_id = ?", (run_id,)
            ).fetchone()
            if exists is not None:
                raise FileExistsError(
                    f"Provider snapshot cycle already exists for {provider}: "
                    f"{retrieval_cycle} status={exists['status']}"
                )
    finally:
        conn.close()

    reports: list[dict[str, Any]] = []
    rows_by_provider: dict[str, list[dict[str, Any]]] = {provider: [] for provider in selected}
    provider_started_at: dict[str, str] = {}
    counts: dict[str, dict[str, int]] = {provider: {"available": 0, "missing": 0, "error": 0} for provider in selected}
    for provider in selected:
        provider_started_at[provider] = utc_now()
        provider_cfg = providers_cfg[provider]
        endpoint_id = _run_endpoint_id(provider)
        capability_ids = _capture_plan(provider)
        pause_sec = float(provider_cfg.get("request_pause_sec", probe_cfg.get("request_pause_sec", 0.0)))
        run_id = _run_id(provider, endpoint_id, retrieval_cycle)
        for symbol in symbols:
            for capability_id in capability_ids:
                capability_cfg = provider_cfg["capabilities"][capability_id]
                result = fetch_capability_payload(
                    provider=provider,
                    provider_config=provider_cfg,
                    capability=capability_id,
                    capability_config=capability_cfg,
                    symbol=symbol,
                    as_of=args.as_of,
                    timeout_sec=timeout_sec,
                    max_response_bytes=max_bytes,
                    max_retries=max_retries,
                )
                normalized = normalize_estimates(
                    result,
                    snapshot_run_id=run_id,
                    retrieval_cycle=retrieval_cycle,
                    entitlement_version=entitlement_version,
                )
                if result.status == "AVAILABLE" and not normalized:
                    status = "NORMALIZATION_EMPTY"
                    counts[provider]["error"] += 1
                elif result.status == "AVAILABLE":
                    status = result.status
                    counts[provider]["available"] += 1
                    rows_by_provider[provider].extend(normalized)
                elif result.status == "EMPTY":
                    status = result.status
                    counts[provider]["missing"] += 1
                else:
                    status = result.status
                    counts[provider]["error"] += 1
                reports.append(
                    {
                        "provider": provider,
                        "endpoint_id": capability_id,
                        "symbol": symbol,
                        "status": status,
                        "http_status": "" if result.http_status is None else result.http_status,
                        "elapsed_ms": result.elapsed_ms,
                        "provider_rows": result.row_count,
                        "normalized_rows": len(normalized),
                        "response_sha256": result.response_sha256,
                        "detail": result.detail,
                    }
                )
                del result
                if pause_sec > 0:
                    time.sleep(pause_sec)

    lock_path = db_path.with_suffix(db_path.suffix + ".writer.lock")
    with writer_lock(lock_path, timeout_sec=db_timeout):
        conn = connect_monitor_db(db_path, timeout_sec=db_timeout)
        try:
            for provider in selected:
                endpoint_id = _run_endpoint_id(provider)
                run_id = _run_id(provider, endpoint_id, retrieval_cycle)
                provider_counts = counts[provider]
                run_status = _provider_run_status(provider, provider_counts)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO provider_snapshot_runs(
                            snapshot_run_id, provider, endpoint_id, retrieval_cycle,
                            started_at_utc, status, requested_count, available_count,
                            missing_count, error_count, entitlement_sha256, source_sha256
                        ) VALUES (?, ?, ?, ?, ?, 'WRITING', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            provider,
                            endpoint_id,
                            retrieval_cycle,
                            provider_started_at[provider],
                            len(symbols) * len(_capture_plan(provider)),
                            provider_counts["available"],
                            provider_counts["missing"],
                            provider_counts["error"],
                            sha256_file(entitlements_path),
                            sha256_file(Path(__file__).resolve()),
                        ),
                    )
                try:
                    inserted, duplicates = append_estimate_snapshots(conn, rows_by_provider[provider])
                    if duplicates:
                        raise RuntimeError(f"Unexpected duplicate normalized snapshots for {provider}: {duplicates}")
                except Exception as exc:
                    with conn:
                        conn.execute(
                            "UPDATE provider_snapshot_runs SET completed_at_utc=?, status='FAIL', "
                            "message=? WHERE snapshot_run_id=?",
                            (utc_now(), str(exc), run_id),
                        )
                    raise
                with conn:
                    conn.execute(
                        "UPDATE provider_snapshot_runs SET completed_at_utc=?, status=?, message=? "
                        "WHERE snapshot_run_id=?",
                        (
                            utc_now(),
                            run_status,
                            f"normalized_rows={inserted}; raw_payloads_retained=0",
                            run_id,
                        ),
                    )
        finally:
            conn.close()

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / "provider_snapshots" / retrieval_cycle
    )
    report_path = output_dir / "provider_snapshot_results.csv"
    manifest_path = output_dir / "provider_snapshot_manifest.json"
    write_csv(report_path, REPORT_FIELDS, reports)
    hard_errors = sum(value["error"] for value in counts.values())
    no_coverage = [
        provider
        for provider in selected
        if int(counts[provider].get("available", 0)) <= 0
    ]
    acceptance = (
        "FAIL"
        if hard_errors
        else "FAIL_NO_COVERAGE"
        if no_coverage
        else "PASS"
    )
    inputs = [
        config_path,
        entitlements_path,
        Path(__file__).resolve(),
        Path(__file__).with_name("provider_common.py"),
        Path(__file__).with_name("monitor_common.py"),
    ]
    if args.symbols_file is not None:
        inputs.append(args.symbols_file.resolve())
    write_manifest(
        manifest_path,
        {
            "schema_version": "provider_snapshot_manifest_v1",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "retrieval_cycle": retrieval_cycle,
            "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "providers": selected,
            "symbol_count": len(symbols),
            "counts": counts,
            "no_coverage_providers": no_coverage,
            "normalized_snapshot_counts": {provider: len(rows_by_provider[provider]) for provider in selected},
            "raw_payloads_retained": False,
            "retention_class": "provisional_user_authorized",
            "shadow_only": True,
            "inputs_sha256": {str(path): sha256_file(path) for path in inputs},
            "outputs_sha256": {report_path.name: sha256_file(report_path)},
        },
    )
    print(f"PROVIDER ESTIMATE SNAPSHOT: {acceptance}")
    print(f"counts: {counts}")
    print(f"report: {report_path}")
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
