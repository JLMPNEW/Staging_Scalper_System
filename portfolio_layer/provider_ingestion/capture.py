#!/usr/bin/env python3
"""Capture current FMP/Alpha estimates into the independent observation store."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.runtime_env import (  # noqa: E402
    hydrate_missing_user_environment,
)
from portfolio_layer.provider_ingestion.artifacts import (  # noqa: E402
    REPORT_FIELDS,
    REPORT_ORDER_SCHEMA,
    capture_report_order,
    capture_report_rows,
    ensure_capture_manifest,
)
from portfolio_layer.provider_ingestion.health import (  # noqa: E402
    session_dates,
    validate_provider_ingestion_policy,
)
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
    connect_store_readonly,
    digest,
    freeze_universe,
    load_provider_universe,
    persist_capture,
    register_provider_universe,
    reject_historical_current_capture,
    require_scheduled_dispatch,
    utc_now,
    verify_store,
    writer_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_ENTITLEMENTS = PACKAGE_ROOT / "expectations_monitor" / "provider_entitlements.yaml"
PHASES = ("sunday_baseline", "premarket", "priority_refresh", "intraday", "postclose")


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


def _phase_tiers(
    phase: str,
    *,
    actual_date: date,
    calendar_name: str,
) -> set[str]:
    if phase == "sunday_baseline":
        return {"tier0", "tier1"}
    if phase in {"premarket", "priority_refresh", "intraday"}:
        return {"tier0"}
    if phase == "postclose":
        week_end = actual_date + timedelta(days=6 - actual_date.weekday())
        remaining = session_dates(calendar_name, actual_date, week_end)
        final_week_session = bool(remaining) and remaining[-1] == actual_date
        return {"tier0", "tier1", "tier2"} if final_week_session else {"tier0", "tier1"}
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
    candidates: list[Path] = []
    for path in runs_root.iterdir():
        if not path.is_dir():
            continue
        try:
            run_date = date.fromisoformat(path.name)
        except ValueError:
            continue
        if run_date <= actual_date:
            candidates.append(path)
    candidates.sort(key=lambda path: path.name, reverse=True)
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
        expected_sha = str(outputs.get("monitor_universe.csv", "")) if isinstance(outputs, dict) else ""
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
    calendar_name: str = "XNYS",
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
            candidate_is_new = candidate is not None and (
                str(candidate["source_run_as_of"]) > prior_as_of
                or (
                    str(candidate["source_run_as_of"]) == prior_as_of
                    and str(candidate["source_artifact_sha256"]) != prior_sha
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
                tiers=_phase_tiers(phase, actual_date=actual_date, calendar_name=calendar_name),
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
        {"ticker": ticker, "tier": "tier0", "sector": "", "source_pipeline": "explicit"} for ticker in values
    ]


CLEAN_REQUEST_STATUSES = frozenset({"AVAILABLE", "EMPTY"})
COUNT_FIELDS = frozenset(
    {
        "analyst_count",
        "revision_up_7_days",
        "revision_down_7_days",
        "revision_up_30_days",
        "revision_down_30_days",
    }
)
NORMALIZED_NUMBER_FIELDS = (
    "estimate_average",
    "estimate_high",
    "estimate_low",
    "analyst_count",
    "estimate_average_7_days_ago",
    "estimate_average_30_days_ago",
    "estimate_average_60_days_ago",
    "estimate_average_90_days_ago",
    "revision_up_7_days",
    "revision_down_7_days",
    "revision_up_30_days",
    "revision_down_30_days",
)


def _provider_acceptance(
    records: Sequence[Mapping[str, Any]],
    providers: Sequence[str],
    policy: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    minimum_clean = float(policy.get("minimum_clean_request_fraction", 0.90))
    provider_available = policy.get("minimum_available_request_fraction", {})
    if not 0.0 <= minimum_clean <= 1.0:
        raise ValueError("minimum_clean_request_fraction must be in [0,1]")
    if not isinstance(provider_available, Mapping):
        raise ValueError("minimum_available_request_fraction must be a mapping")
    metrics: dict[str, dict[str, Any]] = {}
    hard_failures: list[str] = []
    warning_reasons: list[str] = []
    for provider in providers:
        provider_rows = [row for row in records if str(row["provider"]) == provider]
        total = len(provider_rows)
        available = sum(str(row["status"]) == "AVAILABLE" for row in provider_rows)
        clean = sum(str(row["status"]) in CLEAN_REQUEST_STATUSES for row in provider_rows)
        errors = total - clean
        clean_fraction = clean / total if total else 0.0
        available_fraction = available / total if total else 0.0
        minimum_available = float(provider_available.get(provider, 0.50))
        if not 0.0 <= minimum_available <= 1.0:
            raise ValueError(f"minimum available fraction for {provider} must be in [0,1]")
        metrics[provider] = {
            "request_count": total,
            "available_count": available,
            "clean_count": clean,
            "error_count": errors,
            "clean_fraction": clean_fraction,
            "available_fraction": available_fraction,
            "minimum_clean_fraction": minimum_clean,
            "minimum_available_fraction": minimum_available,
        }
        if total == 0:
            hard_failures.append(f"{provider}:no_requests")
        if clean_fraction < minimum_clean:
            hard_failures.append(f"{provider}:clean_fraction")
        if available_fraction < minimum_available:
            hard_failures.append(f"{provider}:available_fraction")
        if errors:
            warning_reasons.append(f"{provider}:request_errors")
    status = "FAIL" if hard_failures else "PASS_WITH_WARNINGS" if warning_reasons else "PASS"
    return status, {
        "provider_metrics": metrics,
        "hard_failures": hard_failures,
        "warning_reasons": warning_reasons,
    }


def _validated_normalized_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    by_natural_key: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        candidate = dict(row)
        fiscal_period_end = str(candidate.get("fiscal_period_end", ""))
        date.fromisoformat(fiscal_period_end)
        identity_fields = ("provider", "endpoint_id", "ticker", "fiscal_period", "estimate_type")
        missing_identity = [field for field in identity_fields if not str(candidate.get(field, "")).strip()]
        if missing_identity:
            raise ValueError(f"Normalized provider identity is incomplete: {missing_identity}")
        for field in NORMALIZED_NUMBER_FIELDS:
            value = candidate.get(field)
            if value is None or str(value).strip() == "":
                continue
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"Non-finite normalized value: {field}")
            if field in COUNT_FIELDS and (number < 0 or not number.is_integer()):
                raise ValueError(f"Invalid normalized count: {field}")
        natural_key = (
            str(candidate["provider"]),
            str(candidate["endpoint_id"]),
            str(candidate["ticker"]).strip().upper(),
            fiscal_period_end,
            str(candidate["fiscal_period"]),
            str(candidate["estimate_type"]),
            str(candidate.get("currency", "")),
        )
        existing = by_natural_key.get(natural_key)
        if existing is not None:
            if existing != candidate:
                raise ValueError(f"Conflicting normalized provider estimate key: {natural_key}")
            continue
        by_natural_key[natural_key] = candidate
        validated.append(candidate)
    return validated


def _validated_cycle_id(value: str) -> str:
    cycle_id = value.strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", cycle_id) is None:
        raise ValueError(f"Invalid provider capture cycle ID: {value!r}")
    return cycle_id


def _valid_response_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.casefold())


def _payload_kind_matches(*, expected: str, actual: str) -> bool:
    normalized = expected.strip().casefold()
    observed = actual.strip().casefold()
    if normalized == "rows":
        return observed == "list"
    return observed == normalized


def _capture_request(
    *,
    provider: str,
    provider_cfg: Mapping[str, Any],
    endpoint: str,
    ticker: str,
    actual_date: date,
    cycle_id: str,
    entitlement_version: str,
    timeout_sec: float,
    max_bytes: int,
    max_retries: int,
) -> dict[str, Any]:
    started = time.monotonic()
    fallback_started = utc_now()
    try:
        capabilities = provider_cfg.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise ValueError(f"{provider} capabilities must be a mapping")
        capability_cfg = capabilities.get(endpoint)
        if not isinstance(capability_cfg, Mapping):
            raise ValueError(f"{provider}.{endpoint} capability must be a mapping")
        result = fetch_capability_payload(
            provider=provider,
            provider_config=provider_cfg,
            capability=endpoint,
            capability_config=capability_cfg,
            symbol=ticker,
            as_of=actual_date,
            timeout_sec=timeout_sec,
            max_response_bytes=max_bytes,
            max_retries=max_retries,
        )
    except Exception as exc:
        received = utc_now()
        return {
            "provider": provider,
            "endpoint_id": endpoint,
            "ticker": ticker,
            "provider_symbol": ticker,
            "status": "REQUEST_EXCEPTION",
            "http_status": None,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "provider_row_count": 0,
            "normalized_rows": [],
            "request_started_at_utc": fallback_started,
            "response_received_at_utc": received,
            "response_sha256": "",
            "detail": f"unhandled_fetch_exception:{type(exc).__name__}",
        }
    try:
        if result.status in CLEAN_REQUEST_STATUSES and not _valid_response_sha256(result.response_sha256):
            raise ValueError("Provider HTTP response lacks a valid content digest")
        expected_kind = str(capability_cfg.get("expected_payload", "")).strip()
        if not expected_kind:
            raise ValueError(f"{provider}.{endpoint} expected_payload is missing")
        if result.status == "AVAILABLE" and not _payload_kind_matches(
            expected=expected_kind,
            actual=result.payload_kind,
        ):
            normalized = []
            status = "SCHEMA_MISMATCH"
            detail = f"payload_kind:{result.payload_kind};expected:{expected_kind}"
        else:
            normalized = _validated_normalized_rows(
                normalize_estimates(
                    result,
                    snapshot_run_id=cycle_id,
                    retrieval_cycle=cycle_id,
                    entitlement_version=entitlement_version,
                )
            )
            status = result.status
            if status == "AVAILABLE" and not normalized:
                status = "NORMALIZATION_EMPTY"
            detail = result.detail
    except Exception as exc:
        normalized = []
        status = "NORMALIZATION_ERROR"
        detail = f"normalization_exception:{type(exc).__name__}"
    return {
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
        "detail": detail,
    }


def _source_hashes() -> dict[str, str]:
    files = (
        Path(__file__).resolve(),
        Path(__file__).with_name("store.py").resolve(),
        Path(__file__).with_name("artifacts.py").resolve(),
        PACKAGE_ROOT / "expectations_monitor" / "estimate_normalization.py",
        Path(__file__).with_name("health.py").resolve(),
        PACKAGE_ROOT / "expectations_monitor" / "provider_common.py",
    )
    return {str(path): sha256_file(path) for path in files}


def run_selftest() -> None:
    assert _phase_tiers("sunday_baseline", actual_date=date(2026, 8, 2), calendar_name="XNYS") == {"tier0", "tier1"}
    assert _phase_tiers("premarket", actual_date=date(2026, 8, 3), calendar_name="XNYS") == {"tier0"}
    assert _phase_tiers("postclose", actual_date=date(2026, 8, 5), calendar_name="XNYS") == {"tier0", "tier1"}
    assert _phase_tiers("postclose", actual_date=date(2026, 8, 7), calendar_name="XNYS") == {"tier0", "tier1", "tier2"}
    assert _phase_tiers("postclose", actual_date=date(2026, 4, 2), calendar_name="XNYS") == {"tier0", "tier1", "tier2"}
    assert _chunks(["A", "B", "C"], 2) == [["A", "B"], ["C"]]
    policy = {
        "minimum_clean_request_fraction": 0.90,
        "minimum_available_request_fraction": {
            "fmp": 0.50,
            "alpha_vantage": 0.50,
        },
    }
    acceptance, diagnostics = _provider_acceptance(
        [
            {"provider": "fmp", "status": "AVAILABLE"},
            {"provider": "alpha_vantage", "status": "AVAILABLE"},
        ],
        ["fmp", "alpha_vantage"],
        policy,
    )
    assert acceptance == "PASS"
    assert not diagnostics["hard_failures"]
    acceptance, diagnostics = _provider_acceptance(
        [{"provider": "fmp", "status": "AVAILABLE"}]
        + [{"provider": "fmp", "status": "REQUEST_ERROR"} for _ in range(9)],
        ["fmp"],
        policy,
    )
    assert acceptance == "FAIL"
    assert "fmp:clean_fraction" in diagnostics["hard_failures"]
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
    hydrate_missing_user_environment()
    if args.phase is None:
        raise ValueError("--phase is required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    ingestion = cfg_get(config, "provider_ingestion", {})
    monitor = cfg_get(config, "expectations_monitor", {})
    if not isinstance(ingestion, dict) or not isinstance(monitor, dict):
        raise ValueError("provider_ingestion and expectations_monitor config must be mappings")
    acceptance_policy = ingestion.get("provider_acceptance", {})
    if not isinstance(acceptance_policy, dict):
        raise ValueError("provider_ingestion.provider_acceptance must be a mapping")

    validate_provider_ingestion_policy(ingestion)
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
        provider_cfg = providers_cfg.get(provider)
        if not isinstance(provider_cfg, dict):
            raise ValueError(f"{provider} entitlement configuration is missing")
        if provider_cfg.get("enabled") is not True:
            raise RuntimeError(f"{provider} is disabled in the entitlement contract")
        pause = float(provider_cfg.get("request_pause_sec", entitlements["probe"].get("request_pause_sec", 0)))
        if pause < 0:
            raise ValueError(f"{provider} request_pause_sec must be non-negative")
        retention = provider_cfg.get("retention", {})
        if retention.get("status") != "provisional_user_authorized":
            raise RuntimeError(f"{provider} normalized retention is not authorized")
        if retention.get("raw_payloads") != "do_not_retain":
            raise RuntimeError(f"{provider} raw-payload policy is not fail-closed")
        capabilities = providers_cfg[provider].get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise ValueError(f"{provider} capabilities must be a mapping")
        missing_endpoints = [endpoint for endpoint in capture_plan(provider) if endpoint not in capabilities]
        if missing_endpoints:
            raise ValueError(f"{provider} is missing configured capabilities: {missing_endpoints}")
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
            calendar_name=calendar_name,
        )
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("--limit must be positive")
        members = members[: args.limit]
    if not members:
        raise ValueError("Capture universe is empty")
    cycle_id = _validated_cycle_id(
        str(
            args.cycle_id
            or f"{now.strftime('%Y%m%dT%H%M%SZ')}-{args.phase}-{digest([row['ticker'] for row in members])[:10]}"
        )
    )
    output_dir = ensure_not_prod_path(
        (
            args.output_dir
            if args.output_dir
            else paths.output_dir / str(ingestion.get("output_subdir", "provider_ingestion")) / cycle_id
        ),
        label="provider capture output path",
    )
    if args.dry_run:
        write_manifest(
            output_dir / "capture_manifest.json",
            {
                "schema_version": "provider_capture_manifest_v2",
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
    if not isinstance(probe, dict):
        raise ValueError("Provider entitlement probe configuration must be a mapping")
    timeout_sec = float(probe.get("timeout_sec", 30.0))
    max_bytes = int(probe.get("max_response_bytes", 2_000_000))
    max_retries = int(probe.get("max_retries", 1))
    batch_size = int(ingestion.get("batch_size", 50))
    requests: list[dict[str, Any]] = []
    started_at = utc_now()
    source_hashes = _source_hashes()
    inputs_sha256 = {
        str(config_path): sha256_file(config_path),
        str(entitlement_path): sha256_file(entitlement_path),
        **source_hashes,
    }
    report_path = output_dir / "capture_requests.csv"
    service_lock = store_path.with_suffix(store_path.suffix + ".capture.lock")
    writer_path = store_path.with_suffix(store_path.suffix + ".writer.lock")
    with writer_lock(service_lock, timeout_sec=timeout):
        with writer_lock(writer_path, timeout_sec=timeout):
            initialization_conn = connect_store(store_path, timeout_sec=timeout)
            initialization_conn.close()
        preflight_conn = connect_store_readonly(store_path, timeout_sec=timeout)
        try:
            prior_cycle = preflight_conn.execute("SELECT * FROM capture_runs WHERE cycle_id=?", (cycle_id,)).fetchone()
            if prior_cycle is None:
                require_scheduled_dispatch(
                    preflight_conn,
                    cycle_id=cycle_id,
                    actual_capture_date=actual_date.isoformat(),
                    capture_phase=args.phase,
                )
        finally:
            preflight_conn.close()
        if prior_cycle is not None:
            prior_status = str(prior_cycle["status"])
            if prior_status in {"PASS", "PASS_WITH_WARNINGS", "MIGRATED"}:
                repair_conn = connect_store_readonly(store_path, timeout_sec=timeout)
                try:
                    _, artifact_errors = ensure_capture_manifest(
                        repair_conn,
                        row=dict(prior_cycle),
                        cycle_dir=output_dir,
                        store_path=store_path,
                    )
                finally:
                    repair_conn.close()
                if artifact_errors:
                    raise RuntimeError(f"Accepted provider capture is missing sealed artifacts: {artifact_errors}")
                print(f"PROVIDER CAPTURE: PASS_NOOP; cycle={cycle_id}; status={prior_status}")
                return 0
            raise RuntimeError(f"Provider capture cycle {cycle_id!r} previously failed; use a new attempt ID")
        entitlement_version = f"{entitlements['schema_version']}:provisional_retention_v1"
        for batch in _chunks([str(row["ticker"]) for row in members], batch_size):
            for provider in providers:
                provider_cfg = providers_cfg[provider]
                pause = float(provider_cfg.get("request_pause_sec", probe.get("request_pause_sec", 0)))
                for ticker in batch:
                    for endpoint in capture_plan(provider):
                        requests.append(
                            _capture_request(
                                provider=provider,
                                provider_cfg=provider_cfg,
                                endpoint=endpoint,
                                ticker=ticker,
                                actual_date=actual_date,
                                cycle_id=cycle_id,
                                entitlement_version=entitlement_version,
                                timeout_sec=timeout_sec,
                                max_bytes=max_bytes,
                                max_retries=max_retries,
                            )
                        )
                        if pause > 0:
                            time.sleep(pause)
        completed_at = utc_now()
        acceptance, acceptance_diagnostics = _provider_acceptance(requests, providers, acceptance_policy)
        report_rows = capture_report_rows(requests)
        write_csv(report_path, REPORT_FIELDS, report_rows)
        report_sha256 = sha256_file(report_path)
        with writer_lock(writer_path, timeout_sec=timeout):
            conn = connect_store(store_path, timeout_sec=timeout)
            try:
                universe_id = freeze_universe(
                    conn,
                    source_run_as_of=universe_as_of,
                    capture_phase=args.phase,
                    members=members,
                    providers=providers,
                    created_at_utc=started_at,
                )
                persist_capture(
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
                        "acceptance_diagnostics": acceptance_diagnostics,
                        "artifact_contract": {
                            "report_name": report_path.name,
                            "report_sha256": report_sha256,
                            "report_order_schema": REPORT_ORDER_SCHEMA,
                            "report_order": capture_report_order(requests),
                            "inputs_sha256": inputs_sha256,
                        },
                    },
                )
                run_row = conn.execute("SELECT * FROM capture_runs WHERE cycle_id=?", (cycle_id,)).fetchone()
                if run_row is None:
                    raise RuntimeError(f"Persisted provider run is missing: {cycle_id}")
                _, artifact_errors = ensure_capture_manifest(
                    conn,
                    row=dict(run_row),
                    cycle_dir=output_dir,
                    store_path=store_path,
                )
                if artifact_errors:
                    raise RuntimeError(f"Provider capture artifact sealing failed: {artifact_errors}")
                store_errors = verify_store(conn)
            finally:
                conn.close()
    if store_errors:
        raise RuntimeError(f"Provider observation store verification failed: {store_errors}")
    print(f"PROVIDER CAPTURE: {acceptance}; requests={len(requests)}; cycle={cycle_id}")
    return 0 if acceptance in {"PASS", "PASS_WITH_WARNINGS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
