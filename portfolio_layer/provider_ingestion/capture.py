#!/usr/bin/env python3
"""Capture current FMP/Alpha estimates into the independent observation store."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.estimate_normalization import (  # noqa: E402
    capture_plan,
    normalize_estimates,
)
from portfolio_layer.expectations_monitor.provider_common import (  # noqa: E402
    fetch_capability_payload,
    load_entitlements,
)
from portfolio_layer.provider_ingestion.store import (  # noqa: E402
    connect_store,
    digest,
    freeze_universe,
    load_provider_universe,
    persist_capture,
    register_provider_universe,
    reject_historical_current_capture,
    utc_now,
    verify_store,
    writer_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_ENTITLEMENTS = PACKAGE_ROOT / "expectations_monitor" / "provider_entitlements.yaml"
PHASES = ("sunday_baseline", "premarket", "priority_refresh", "intraday", "postclose")
REPORT_FIELDS = (
    "provider",
    "endpoint_id",
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--entitlements", type=Path, default=DEFAULT_ENTITLEMENTS)
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--portfolio-as-of", type=date.fromisoformat)
    parser.add_argument("--cycle-id")
    parser.add_argument("--providers", nargs="+", choices=("alpha_vantage", "fmp"))
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("provider_ingestion.batch_size must be positive")
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _phase_tiers(phase: str, *, weekday: int) -> set[str]:
    if phase == "sunday_baseline":
        return {"tier0", "tier1"}
    if phase in {"premarket", "priority_refresh", "intraday"}:
        return {"tier0"}
    if phase == "postclose":
        return {"tier0", "tier1", "tier2"} if weekday == 4 else {"tier0", "tier1"}
    raise ValueError(f"Unsupported capture phase: {phase}")


def _sealed_universe_candidate(
    output_root: Path,
    *,
    actual_date: date,
    output_subdir: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return the newest hash-valid universe handoff, without requiring one to exist."""
    runs_root = output_root / "runs"
    if not runs_root.is_dir():
        return None, ["monitor_runs_root_missing"]
    errors: list[str] = []
    candidates = sorted(
        (
            path
            for path in runs_root.iterdir()
            if path.is_dir()
            and len(path.name) == 10
            and path.name <= actual_date.isoformat()
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    for run_dir in candidates:
        universe_dir = run_dir / output_subdir
        manifest_path = universe_dir / "monitor_universe_manifest.json"
        universe_path = universe_dir / "monitor_universe.csv"
        if not manifest_path.is_file() and not universe_path.is_file():
            continue
        if not manifest_path.is_file() or not universe_path.is_file():
            errors.append(f"incomplete_handoff:{run_dir.name}")
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid_manifest:{run_dir.name}:{exc}")
            continue
        outputs = manifest.get("outputs_sha256", {})
        expected_sha = (
            str(outputs.get("monitor_universe.csv", ""))
            if isinstance(outputs, dict)
            else ""
        )
        actual_sha = sha256_file(universe_path)
        if (
            not isinstance(manifest, dict)
            or manifest.get("acceptance") != "PASS"
            or manifest.get("run_as_of") != run_dir.name
            or not expected_sha
            or expected_sha != actual_sha
        ):
            errors.append(f"invalid_or_unsealed_handoff:{run_dir.name}")
            continue
        with universe_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        required = {"ticker", "tier", "sector", "source_pipeline"}
        if not rows or not required <= set(rows[0]):
            errors.append(f"invalid_universe_schema:{run_dir.name}")
            continue
        return {
            "source_run_as_of": run_dir.name,
            "source_artifact_path": str(universe_path.resolve()),
            "source_artifact_sha256": actual_sha,
            "members": rows,
        }, errors
    return None, errors


def _load_independent_universe(
    *,
    store_path: Path,
    output_root: Path,
    output_subdir: str,
    phase: str,
    actual_date: date,
    timeout_sec: float,
    providers: Sequence[str],
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Refresh opportunistically, then read only the provider-owned registry."""
    candidate, sync_errors = _sealed_universe_candidate(
        output_root,
        actual_date=actual_date,
        output_subdir=output_subdir,
    )
    lock_path = store_path.with_suffix(store_path.suffix + ".writer.lock")
    with writer_lock(lock_path, timeout_sec=timeout_sec):
        conn = connect_store(store_path, timeout_sec=timeout_sec)
        try:
            prior = conn.execute(
                "SELECT source_run_as_of,source_artifact_sha256 "
                "FROM provider_universe_registry "
                "ORDER BY source_run_as_of DESC, rowid DESC LIMIT 1"
            ).fetchone()
            prior_as_of = "" if prior is None else str(prior["source_run_as_of"])
            prior_sha = "" if prior is None else str(prior["source_artifact_sha256"])
            sync_status = "NO_NEW_HANDOFF"
            candidate_is_new = (
                candidate is not None
                and (
                    str(candidate["source_run_as_of"]) > prior_as_of
                    or (
                        str(candidate["source_run_as_of"]) == prior_as_of
                        and str(candidate["source_artifact_sha256"]) != prior_sha
                    )
                )
            )
            if candidate_is_new and candidate is not None:
                register_provider_universe(
                    conn,
                    source_run_as_of=str(candidate["source_run_as_of"]),
                    members=list(candidate["members"]),
                    providers=providers,
                    source_artifact_path=str(candidate["source_artifact_path"]),
                    source_artifact_sha256=str(candidate["source_artifact_sha256"]),
                    activated_at_utc=utc_now(),
                )
                sync_status = "INITIALIZED" if not prior_as_of else "UPDATED"
            elif candidate is not None:
                sync_status = "UNCHANGED"
            registry, members = load_provider_universe(
                conn,
                tiers=_phase_tiers(phase, weekday=actual_date.weekday()),
                actual_date=actual_date,
            )
        finally:
            conn.close()
    health = {
        "status": "ACTIVE_REGISTRY",
        "capture_independent": True,
        "registry_id": str(registry["registry_id"]),
        "source_universe_as_of": str(registry["source_run_as_of"]),
        "source_artifact_path": str(registry["source_artifact_path"]),
        "source_artifact_sha256": str(registry["source_artifact_sha256"]),
        "registry_member_count": int(registry["member_count"]),
        "sync_status": sync_status,
        "sync_diagnostics": sync_errors,
    }
    return str(registry["source_run_as_of"]), members, health


def _explicit_universe(symbols: Sequence[str]) -> tuple[str, list[dict[str, str]]]:
    values = sorted({value.strip().upper() for value in symbols if value.strip()})
    if not values or "CASH" in values:
        raise ValueError("Explicit capture symbols must be non-empty equities")
    return "explicit", [
        {"ticker": ticker, "tier": "tier0", "sector": "", "source_pipeline": "explicit"}
        for ticker in values
    ]


def _provider_status(records: Sequence[Mapping[str, Any]], providers: Sequence[str]) -> str:
    no_coverage = [
        provider
        for provider in providers
        if not any(
            str(row["provider"]) == provider and str(row["status"]) == "AVAILABLE"
            for row in records
        )
    ]
    if no_coverage:
        return "FAIL"
    if any(str(row["status"]) not in {"AVAILABLE", "EMPTY"} for row in records):
        return "PASS_WITH_WARNINGS"
    return "PASS"


def _source_hashes() -> dict[str, str]:
    files = (
        Path(__file__).resolve(),
        Path(__file__).with_name("store.py").resolve(),
        PACKAGE_ROOT / "expectations_monitor" / "estimate_normalization.py",
        PACKAGE_ROOT / "expectations_monitor" / "provider_common.py",
    )
    return {str(path): sha256_file(path) for path in files}


def run_selftest() -> None:
    assert _phase_tiers("sunday_baseline", weekday=6) == {"tier0", "tier1"}
    assert _phase_tiers("premarket", weekday=0) == {"tier0"}
    assert _phase_tiers("postclose", weekday=3) == {"tier0", "tier1"}
    assert _phase_tiers("postclose", weekday=4) == {"tier0", "tier1", "tier2"}
    assert _chunks(["A", "B", "C"], 2) == [["A", "B"], ["C"]]
    assert _provider_status(
        [
            {"provider": "fmp", "status": "AVAILABLE"},
            {"provider": "alpha_vantage", "status": "AVAILABLE"},
        ],
        ["fmp", "alpha_vantage"],
    ) == "PASS"
    try:
        reject_historical_current_capture(
            requested_portfolio_as_of=date(2026, 7, 31),
            now_utc=datetime(2026, 8, 2, 15, tzinfo=timezone.utc),
            timezone_name="America/New_York",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Historical current-snapshot capture was not rejected")
    print("provider capture selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.phase is None:
        raise ValueError("--phase is required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    ingestion = cfg_get(config, "provider_ingestion", {})
    monitor = cfg_get(config, "expectations_monitor", {})
    if not isinstance(ingestion, dict) or not isinstance(monitor, dict):
        raise ValueError("provider_ingestion and expectations_monitor config must be mappings")
    if ingestion.get("network_owner") != "independent_service":
        raise ValueError("provider_ingestion.network_owner must be independent_service")
    if ingestion.get("raw_payload_retention_enabled") is not False:
        raise ValueError("Raw provider payload retention must be false")
    recovery_cfg = ingestion.get("recovery", {})
    if not isinstance(recovery_cfg, dict):
        raise ValueError("provider_ingestion.recovery must be a mapping")
    expected_registry_contract = {
        "universe_registry_policy": "provider_owned_append_only",
        "universe_source_refresh": "opportunistic_sealed_handoff",
        "universe_source_age_is_capture_warning": False,
    }
    configured_registry_contract = {
        key: recovery_cfg.get(key) for key in expected_registry_contract
    }
    if configured_registry_contract != expected_registry_contract:
        raise ValueError(
            "Independent provider universe registry contract mismatch: "
            f"{configured_registry_contract}"
        )
    timezone_name = str(ingestion.get("timezone", "America/New_York"))
    calendar_name = str(ingestion.get("exchange_calendar", "XNYS"))
    decision_cutoff = str(ingestion.get("decision_cutoff_local", "09:25"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    actual_date = now.astimezone(ZoneInfo(timezone_name)).date()
    reject_historical_current_capture(
        requested_portfolio_as_of=args.portfolio_as_of,
        now_utc=now,
        timezone_name=timezone_name,
    )
    providers = list(args.providers or ingestion.get("providers", ["alpha_vantage", "fmp"]))
    if not providers or len(set(providers)) != len(providers):
        raise ValueError("Provider list must be non-empty and unique")
    entitlement_path = args.entitlements.resolve()
    entitlements = load_entitlements(entitlement_path)
    providers_cfg = entitlements["providers"]
    for provider in providers:
        retention = providers_cfg[provider].get("retention", {})
        if retention.get("status") != "provisional_user_authorized":
            raise RuntimeError(f"{provider} normalized retention is not authorized")
        if retention.get("raw_payloads") != "do_not_retain":
            raise RuntimeError(f"{provider} raw-payload policy is not fail-closed")
    timeout = float(ingestion.get("writer_lock_timeout_sec", 30.0))
    store_path = ensure_not_prod_path(
        resolve_path(
            ingestion.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    if args.symbols:
        universe_as_of, members = _explicit_universe(args.symbols)
        universe_health: dict[str, Any] = {
            "status": "EXPLICIT",
            "capture_independent": True,
            "source_universe_as_of": universe_as_of,
            "sync_status": "EXPLICIT",
            "sync_diagnostics": [],
        }
    else:
        universe_as_of, members, universe_health = _load_independent_universe(
            store_path=store_path,
            output_root=paths.output_dir,
            output_subdir=str(monitor.get("output_subdir", "expectations_monitor")),
            phase=args.phase,
            actual_date=actual_date,
            timeout_sec=timeout,
            providers=providers,
        )
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("--limit must be positive")
        members = members[: args.limit]
    if not members:
        raise ValueError("Capture universe is empty")
    cycle_id = str(
        args.cycle_id
        or f"{now.strftime('%Y%m%dT%H%M%SZ')}-{args.phase}-{digest([row['ticker'] for row in members])[:10]}"
    )
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / str(ingestion.get("output_subdir", "provider_ingestion")) / cycle_id
    )
    if args.dry_run:
        write_manifest(
            output_dir / "capture_manifest.json",
            {
                "schema_version": "provider_capture_manifest_v1",
                "acceptance": "DRY_RUN",
                "cycle_id": cycle_id,
                "capture_phase": args.phase,
                "actual_capture_date": actual_date.isoformat(),
                "requested_portfolio_as_of": "" if args.portfolio_as_of is None else args.portfolio_as_of.isoformat(),
                "universe_as_of": universe_as_of,
                "universe_freshness": universe_health,
                "providers": providers,
                "tickers": [row["ticker"] for row in members],
                "raw_payloads_retained": False,
            },
        )
        print(f"PROVIDER CAPTURE: DRY_RUN; tickers={len(members)}; cycle={cycle_id}")
        return 0
    probe = entitlements.get("probe", {})
    timeout_sec = float(probe.get("timeout_sec", 30.0))
    max_bytes = int(probe.get("max_response_bytes", 2_000_000))
    max_retries = int(probe.get("max_retries", 1))
    batch_size = int(ingestion.get("batch_size", 50))
    requests: list[dict[str, Any]] = []
    started_at = utc_now()
    service_lock = store_path.with_suffix(store_path.suffix + ".capture.lock")
    with writer_lock(service_lock, timeout_sec=timeout):
        preflight_conn = connect_store(store_path, timeout_sec=timeout)
        try:
            prior_cycle = preflight_conn.execute(
                "SELECT status FROM capture_runs WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
        finally:
            preflight_conn.close()
        if prior_cycle is not None:
            prior_status = str(prior_cycle["status"])
            if prior_status in {"PASS", "PASS_WITH_WARNINGS", "MIGRATED"}:
                print(f"PROVIDER CAPTURE: PASS_NOOP; cycle={cycle_id}; status={prior_status}")
                return 0
            raise RuntimeError(
                f"Provider capture cycle {cycle_id!r} previously failed; use a new attempt ID"
            )
        for batch in _chunks([str(row["ticker"]) for row in members], batch_size):
            for provider in providers:
                provider_cfg = providers_cfg[provider]
                pause = float(provider_cfg.get("request_pause_sec", probe.get("request_pause_sec", 0)))
                for ticker in batch:
                    for endpoint in capture_plan(provider):
                        result = fetch_capability_payload(
                            provider=provider,
                            provider_config=provider_cfg,
                            capability=endpoint,
                            capability_config=provider_cfg["capabilities"][endpoint],
                            symbol=ticker,
                            as_of=actual_date,
                            timeout_sec=timeout_sec,
                            max_response_bytes=max_bytes,
                            max_retries=max_retries,
                        )
                        normalized = normalize_estimates(
                            result,
                            snapshot_run_id=cycle_id,
                            retrieval_cycle=cycle_id,
                            entitlement_version=f"{entitlements['schema_version']}:provisional_retention_v1",
                        )
                        status = result.status
                        if status == "AVAILABLE" and not normalized:
                            status = "NORMALIZATION_EMPTY"
                        requests.append(
                            {
                                "provider": provider,
                                "endpoint_id": endpoint,
                                "ticker": ticker,
                                "provider_symbol": ticker,
                                "status": status,
                                "http_status": result.http_status,
                                "elapsed_ms": result.elapsed_ms,
                                "provider_row_count": result.row_count,
                                "normalized_rows": normalized,
                                "request_started_at_utc": result.requested_at_utc,
                                "response_received_at_utc": result.response_received_at_utc,
                                "response_sha256": result.response_sha256,
                                "detail": result.detail,
                            }
                        )
                        del result
                        if pause > 0:
                            time.sleep(pause)
        completed_at = utc_now()
        acceptance = _provider_status(requests, providers)

        conn = connect_store(store_path, timeout_sec=timeout)
        try:
            source_hashes = _source_hashes()
            universe_id = freeze_universe(
                conn,
                source_run_as_of=universe_as_of,
                capture_phase=args.phase,
                members=members,
                providers=providers,
                created_at_utc=started_at,
            )
            result_summary = persist_capture(
                conn,
                cycle_id=cycle_id,
                capture_phase=args.phase,
                requested_portfolio_as_of=(
                    "" if args.portfolio_as_of is None else args.portfolio_as_of.isoformat()
                ),
                actual_capture_date=actual_date.isoformat(),
                universe_id=universe_id,
                started_at_utc=started_at,
                completed_at_utc=completed_at,
                request_records=requests,
                source_code_digest=digest(source_hashes),
                config_digest=sha256_file(config_path),
                timezone_name=timezone_name,
                calendar_name=calendar_name,
                decision_cutoff_local=decision_cutoff,
                status=acceptance,
                metadata={
                    "raw_payloads_retained": False,
                    "universe_as_of": universe_as_of,
                    "universe_freshness": universe_health,
                },
            )
            store_errors = verify_store(conn)
        finally:
            conn.close()
    if store_errors:
        raise RuntimeError(f"Provider observation store verification failed: {store_errors}")
    report_path = output_dir / "capture_requests.csv"
    report_rows = [
        {
            key: (
                len(row["normalized_rows"])
                if key == "normalized_rows"
                else row.get("provider_row_count", "")
                if key == "provider_rows"
                else row.get(key, "")
            )
            for key in REPORT_FIELDS
        }
        for row in requests
    ]
    write_csv(report_path, REPORT_FIELDS, report_rows)
    manifest_path = output_dir / "capture_manifest.json"
    write_manifest(
        manifest_path,
        {
            "schema_version": "provider_capture_manifest_v1",
            "acceptance": acceptance,
            "cycle_id": cycle_id,
            "capture_phase": args.phase,
            "actual_capture_date": actual_date.isoformat(),
            "requested_portfolio_as_of": "" if args.portfolio_as_of is None else args.portfolio_as_of.isoformat(),
            "universe_as_of": universe_as_of,
            "universe_freshness": universe_health,
            "universe_member_count": len(members),
            "providers": providers,
            "store_path": str(store_path),
            "store_result": result_summary,
            "raw_payloads_retained": False,
            "inputs_sha256": {
                str(config_path): sha256_file(config_path),
                str(entitlement_path): sha256_file(entitlement_path),
                **source_hashes,
            },
            "outputs_sha256": {report_path.name: sha256_file(report_path)},
        },
    )
    print(f"PROVIDER CAPTURE: {acceptance}; requests={len(requests)}; cycle={cycle_id}")
    return 0 if acceptance in {"PASS", "PASS_WITH_WARNINGS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
