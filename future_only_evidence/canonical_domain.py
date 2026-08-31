"""Cross-domain canonical trust, timestamp, and contract-binding helpers."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .canonical_trust import (
    CanonicalTrustBundle,
    validate_external_timestamp,
    validate_market_data_export_receipt,
)
from .official_calendar import (
    read_official_xnys_calendar_snapshot,
    validate_official_xnys_calendar_bytes,
)
from .canonical_values import exact_utc
from .protocol import canonical_sha256, exact_sha256
from .prospective_contracts import ProspectiveContract


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def _exact_date(value: Any, *, label: str) -> str:
    text = str(value)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be exact YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be exact YYYY-MM-DD")
    return text


def _canonical_ticker(value: Any, *, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or value.upper() != value
    ):
        raise ValueError(f"{label} must be a canonical uppercase ticker")
    return value


def _strict_int(value: Any, *, label: str, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a canonical integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} is below its minimum")
    return value


def _read_json_snapshot(
    path: Path,
    *,
    label: str,
    payload_bytes: bytes | None = None,
) -> tuple[dict[str, Any], str, bytes]:
    snapshot_bytes = (
        bytes(payload_bytes)
        if payload_bytes is not None
        else Path(path).expanduser().resolve().read_bytes()
    )
    digest = hashlib.sha256(snapshot_bytes).hexdigest()
    try:
        payload = json.loads(snapshot_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be one valid UTF-8 JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload, digest, snapshot_bytes


def _read_csv_snapshot(
    path: Path,
    *,
    label: str,
    payload_bytes: bytes | None = None,
) -> tuple[list[dict[str, str]], set[str], str]:
    snapshot_bytes = (
        bytes(payload_bytes)
        if payload_bytes is not None
        else Path(path).expanduser().resolve().read_bytes()
    )
    digest = hashlib.sha256(snapshot_bytes).hexdigest()
    try:
        text = snapshot_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = set(reader.fieldnames or [])
    return [dict(row) for row in reader], fields, digest


def _verified_receipt_snapshot(
    path: Path,
    *,
    expected_sha256: str,
    bundle: CanonicalTrustBundle,
    label: str,
    receipt_snapshot_bytes: bytes | None = None,
) -> tuple[dict[str, Any], str, bytes]:
    payload, digest, snapshot_bytes = _read_json_snapshot(
        path,
        label=label,
        payload_bytes=receipt_snapshot_bytes,
    )
    if digest != exact_sha256(expected_sha256, label=f"{label} sha256"):
        raise ValueError(f"{label} SHA-256 mismatch")
    bundle.evidence_seal.verify_snapshot(snapshot_bytes, digest, payload)
    return payload, digest, snapshot_bytes


def domain_contract_sha256(
    contract: ProspectiveContract,
    domain_contract: Mapping[str, Any],
) -> str:
    return canonical_sha256(
        {
            "prospective_contract": contract.identity(),
            "domain_contract": dict(domain_contract),
        }
    )


def first_proven_month_end_after(
    trading_calendar_path: Path,
    *,
    after_utc: datetime,
    trading_calendar_snapshot_bytes: bytes | None = None,
) -> str:
    if trading_calendar_snapshot_bytes is None:
        rows, _, _ = read_official_xnys_calendar_snapshot(trading_calendar_path)
    else:
        rows, _ = validate_official_xnys_calendar_bytes(
            bytes(trading_calendar_snapshot_bytes)
        )
    sessions = [
        _exact_date(row["session_date"], label="calendar session date") for row in rows
    ]
    months = [(value[:7], value) for value in sessions]
    proven: list[str] = []
    for month in sorted({month for month, _ in months}):
        later_month_exists = any(candidate > month for candidate, _ in months)
        if later_month_exists:
            proven.append(max(value for candidate, value in months if candidate == month))
    result = [value for value in proven if value > after_utc.date().isoformat()]
    if not result:
        raise ValueError("calendar does not prove a full future month-end after trust activation")
    return result[0]


def require_receipt_contract_binding(
    receipt_path: Path,
    *,
    expected_domain_contract_sha256: str,
    receipt_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    payload_bytes = (
        bytes(receipt_snapshot_bytes)
        if receipt_snapshot_bytes is not None
        else Path(receipt_path).expanduser().resolve().read_bytes()
    )
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("canonical receipt must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("canonical receipt must be a JSON object")
    if payload.get("domain_contract_sha256") != expected_domain_contract_sha256:
        raise ValueError("receipt does not bind the exact domain/acceptance contract")
    return payload


def validate_market_source_provenance(
    *,
    outcome_source_paths: Mapping[str, Path],
    market_export_attestation: Mapping[str, Any],
    expected_source_sha256: Mapping[str, str],
    bundle: CanonicalTrustBundle,
    expected_benchmark_ticker: str,
    outcome_source_snapshot_bytes: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    """Tie exact, single-read raw sources to the already verified attestation."""

    required_roles = {
        "total_return_bars",
        "asset_master",
        "corporate_actions",
        "terminal_events",
    }
    if (
        not required_roles <= set(outcome_source_paths)
        or not required_roles <= set(expected_source_sha256)
    ):
        raise ValueError("canonical outcome sources lack asset/corporate-action provenance")
    if (
        outcome_source_snapshot_bytes is not None
        and set(outcome_source_snapshot_bytes) != set(outcome_source_paths)
    ):
        raise ValueError("market provenance snapshots differ from outcome source roles")
    receipt = dict(market_export_attestation)
    if (
        receipt.get("market_data_export_attestation_pass") is not True
        or receipt.get("family") != bundle.family
        or receipt.get("source_sha256") != dict(expected_source_sha256)
    ):
        raise ValueError("market provenance did not receive the verified export attestation")
    snapshots = {
        role: _read_csv_snapshot(
            outcome_source_paths[role],
            label=f"market provenance {role}",
            payload_bytes=(
                outcome_source_snapshot_bytes[role]
                if outcome_source_snapshot_bytes is not None
                else None
            ),
        )
        for role in sorted(required_roles)
    }
    for role, (_, _, digest) in snapshots.items():
        if digest != expected_source_sha256[role]:
            raise ValueError(f"market provenance source differs from attested bytes: {role}")
    asset_rows, asset_fields, asset_digest = snapshots["asset_master"]
    asset_required = {
        "ticker",
        "asset_id",
        "provider_id",
        "dataset_id",
        "exchange_mic",
        "currency",
        "effective_from",
        "effective_to",
    }
    if not asset_rows or not asset_required <= asset_fields:
        raise ValueError("attested asset master has an invalid schema")
    assets: dict[str, dict[str, str]] = {}
    seen_asset_ids: set[str] = set()
    for row in asset_rows:
        ticker = _canonical_ticker(row["ticker"], label="asset-master ticker")
        if (
            not ticker
            or ticker in assets
            or not row["asset_id"]
            or row["asset_id"] in seen_asset_ids
            or not row["provider_id"]
            or not row["dataset_id"]
            or not row["exchange_mic"]
            or not row["currency"]
            or not row["effective_from"]
        ):
            raise ValueError("asset master has blank/duplicate ticker identity")
        if (
            row["provider_id"] != receipt.get("provider_id")
            or row["dataset_id"] != receipt.get("dataset_id")
            or row["currency"] != bundle.required_currency
        ):
            raise ValueError("asset master differs from the attested provider/currency policy")
        effective_from = date.fromisoformat(
            _exact_date(row["effective_from"], label="asset effective-from date")
        )
        effective_to_value = row.get("effective_to") or ""
        effective_to_raw = (
            _exact_date(effective_to_value, label="asset effective-to date")
            if effective_to_value
            else ""
        )
        if (
            effective_to_raw
            and date.fromisoformat(effective_to_raw) < effective_from
        ):
            raise ValueError("asset master has an inverted effective interval")
        assets[ticker] = row
        seen_asset_ids.add(row["asset_id"])
    receipt_assets = {
        _canonical_ticker(ticker, label="market receipt asset ticker"): str(asset_id)
        for ticker, asset_id in dict(receipt.get("asset_ids") or {}).items()
    }
    observed_assets = {ticker: row["asset_id"] for ticker, row in assets.items()}
    if receipt_assets != observed_assets:
        raise ValueError("market receipt asset-id census differs from the asset master")
    benchmark = _canonical_ticker(
        expected_benchmark_ticker, label="expected benchmark ticker"
    )
    if observed_assets.get(benchmark) != bundle.benchmark_asset_ids.get(benchmark):
        raise ValueError("benchmark ticker does not resolve to the pinned provider asset id")
    bars, bar_fields, _ = snapshots["total_return_bars"]
    bar_required = {
        "ticker",
        "asset_id",
        "provider_id",
        "dataset_id",
        "exchange_mic",
        "currency",
        "source_observation_id",
        "execution_at_utc",
    }
    if not bars or not bar_required <= bar_fields:
        raise ValueError("raw return bars lack immutable provider asset identity")
    observation_ids: set[str] = set()
    bar_tickers: set[str] = set()
    for row in bars:
        ticker = _canonical_ticker(row["ticker"], label="raw-bar ticker")
        asset = assets.get(ticker)
        observation_id = str(row["source_observation_id"])
        if asset is None or not observation_id or observation_id in observation_ids:
            raise ValueError("raw bars have unknown assets or duplicate observations")
        observation_ids.add(observation_id)
        for field in (
            "asset_id",
            "provider_id",
            "dataset_id",
            "exchange_mic",
            "currency",
        ):
            if row[field] != asset[field]:
                raise ValueError(f"raw bar differs from asset master: {ticker}/{field}")
        execution_date = _utc(row["execution_at_utc"], label="bar execution").date()
        effective_from = date.fromisoformat(
            _exact_date(asset["effective_from"], label="asset effective-from date")
        )
        effective_to_value = asset.get("effective_to") or ""
        effective_to_raw = (
            _exact_date(effective_to_value, label="asset effective-to date")
            if effective_to_value
            else ""
        )
        effective_to = (
            date.fromisoformat(effective_to_raw)
            if effective_to_raw
            else None
        )
        if execution_date < effective_from or (
            effective_to is not None and execution_date > effective_to
        ):
            raise ValueError(f"raw bar falls outside asset-master lifecycle: {ticker}")
        bar_tickers.add(ticker)
    actions, action_fields, action_digest = snapshots["corporate_actions"]
    required_action_fields = {
        "ticker",
        "asset_id",
        "action_id",
        "action_type",
        "terminal_event_status",
        "terminal_event_reason",
        "effective_at_utc",
        "source_observation_id",
    }
    if not required_action_fields <= action_fields:
        raise ValueError("corporate-action source has an invalid provenance schema")
    action_keys: set[str] = set()
    action_observation_ids: set[str] = set()
    terminal_action_keys: set[tuple[str, str, str, str]] = set()
    terminal_action_reasons = {
        "bankruptcy_terminal": "bankruptcy",
        "cash_liquidation": "cash_liquidation",
        "delisting": "delisting",
        "merger_cash": "cash_merger",
        "trading_halt_terminal": "exchange_halt_terminal",
    }
    for row in actions:
        ticker = _canonical_ticker(row["ticker"], label="corporate-action ticker")
        asset = assets.get(ticker)
        action_id = str(row["action_id"])
        source_observation_id = str(row["source_observation_id"])
        if (
            asset is None
            or row["asset_id"] != asset["asset_id"]
            or not action_id
            or action_id in action_keys
            or not row["action_type"]
            or not source_observation_id
            or source_observation_id in action_observation_ids
        ):
            raise ValueError("corporate action has invalid/duplicate asset identity")
        action_keys.add(action_id)
        action_observation_ids.add(source_observation_id)
        effective = _utc(row["effective_at_utc"], label="corporate action effective time")
        effective_from = date.fromisoformat(
            _exact_date(asset["effective_from"], label="asset effective-from date")
        )
        effective_to_value = asset.get("effective_to") or ""
        effective_to_raw = (
            _exact_date(effective_to_value, label="asset effective-to date")
            if effective_to_value
            else ""
        )
        effective_to = (
            date.fromisoformat(effective_to_raw)
            if effective_to_raw
            else None
        )
        if effective.date() < effective_from or (
            effective_to is not None and effective.date() > effective_to
        ):
            raise ValueError("corporate action falls outside asset-master lifecycle")
        if row["action_type"] in terminal_action_reasons:
            terminal_status = str(row["terminal_event_status"])
            terminal_reason = str(row["terminal_event_reason"])
            if (
                not terminal_status
                or terminal_status == "none"
                or terminal_reason != terminal_action_reasons[row["action_type"]]
            ):
                raise ValueError("terminal corporate action lacks governed terminal status")
            terminal_action_keys.add(
                (ticker, effective.isoformat(), terminal_status, terminal_reason)
            )
        elif (
            str(row["terminal_event_status"] or "") not in {"", "none"}
            or str(row["terminal_event_reason"] or "") not in {"", "none"}
        ):
            raise ValueError("non-terminal corporate action claims terminal identity")
    terminal_rows, terminal_fields, _ = snapshots["terminal_events"]
    terminal_required = {
        "ticker",
        "terminal_execution_at_utc",
        "terminal_event_status",
        "terminal_event_reason",
    }
    if not terminal_required <= terminal_fields:
        raise ValueError("terminal-event source has an invalid provenance schema")
    terminal_source_keys: set[tuple[str, str, str, str]] = set()
    terminal_tickers: set[str] = set()
    for row in terminal_rows:
        ticker = _canonical_ticker(row.get("ticker"), label="terminal-event ticker")
        execution = _utc(
            row.get("terminal_execution_at_utc"), label="terminal execution"
        ).isoformat()
        status = str(row.get("terminal_event_status") or "")
        reason = str(row.get("terminal_event_reason") or "")
        if (
            not ticker
            or ticker in terminal_tickers
            or ticker not in assets
            or not status
            or status == "none"
            or reason not in set(terminal_action_reasons.values())
        ):
            raise ValueError("terminal-event source has invalid/duplicate lifecycle identity")
        terminal_tickers.add(ticker)
        terminal_source_keys.add(
            (ticker, execution, status, reason)
        )
    if terminal_source_keys != terminal_action_keys:
        raise ValueError(
            "terminal-event source and terminal corporate-action census are not exact"
        )
    if set(assets) != bar_tickers | terminal_tickers:
        raise ValueError(
            "asset master is not the exact raw-bar plus terminal-event ticker census"
        )
    return {
        "asset_master_sha256": asset_digest,
        "corporate_actions_sha256": action_digest,
        "asset_count": len(assets),
        "bar_observation_count": len(bars),
        "corporate_action_count": len(actions),
        "terminal_event_count": len(terminal_rows),
        "exact_asset_census_pass": True,
        "exact_terminal_corporate_action_census_pass": True,
        "benchmark_ticker": benchmark,
        "benchmark_asset_id": observed_assets[benchmark],
        "immutable_asset_identity_pass": True,
    }


def attach_capture_timestamp(
    capture: Mapping[str, Any],
    *,
    capture_receipt_path: Path,
    capture_timestamp_receipt_path: Path,
    expected_capture_timestamp_receipt_sha256: str,
    expected_previous_log_head_sha256: str,
    expected_previous_log_sequence: int,
    expected_domain_contract_sha256: str,
    bundle: CanonicalTrustBundle,
    capture_receipt_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    receipt_bytes = (
        bytes(capture_receipt_snapshot_bytes)
        if capture_receipt_snapshot_bytes is not None
        else Path(capture_receipt_path).expanduser().resolve().read_bytes()
    )
    require_receipt_contract_binding(
        capture_receipt_path,
        expected_domain_contract_sha256=expected_domain_contract_sha256,
        receipt_snapshot_bytes=receipt_bytes,
    )
    audit = validate_external_timestamp(
        subject_path=capture_receipt_path,
        timestamp_receipt_path=capture_timestamp_receipt_path,
        expected_timestamp_receipt_sha256=expected_capture_timestamp_receipt_sha256,
        expected_subject_sha256=str(capture["trusted_receipt"]["sha256"]),
        bundle=bundle,
        expected_previous_log_head_sha256=expected_previous_log_head_sha256,
        expected_previous_log_sequence=expected_previous_log_sequence,
        expected_family=str(capture["family"]),
        expected_policy_id=str(capture["policy_id"]),
        expected_subject_role="signal_capture_receipt",
        expected_slot_id=(
            f"{capture['family']}:{capture['policy_id']}:capture:{capture['asof_date']}"
        ),
        subject_snapshot_bytes=receipt_bytes,
    )
    entry = _utc(
        capture["trusted_capture_timing"]["entry_execution_at_utc"],
        label="capture entry execution",
    )
    observed = _utc(audit["observed_at_utc"], label="external capture timestamp")
    claimed_capture = _utc(capture["captured_at_utc"], label="signed capture timestamp")
    if not claimed_capture <= observed < entry:
        raise ValueError("external capture timestamp is not strictly before entry execution")
    result = dict(capture)
    result.pop("capture_id", None)
    result.pop("payload_sha256", None)
    result["external_timestamp_audit"] = audit
    result["domain_contract_sha256"] = expected_domain_contract_sha256
    result["capture_id"] = canonical_sha256(result)
    result["payload_sha256"] = canonical_sha256(result)
    return result


def revalidate_capture_timestamp(
    capture: Mapping[str, Any],
    *,
    bundle: CanonicalTrustBundle,
    capture_receipt_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    audit = capture.get("external_timestamp_audit")
    receipt = capture.get("trusted_receipt")
    if not isinstance(audit, dict) or not isinstance(receipt, dict):
        raise ValueError("capture lacks external timestamp/capture-receipt identity")
    reproduced = validate_external_timestamp(
        subject_path=Path(str(receipt["path"])),
        timestamp_receipt_path=Path(str(audit["timestamp_receipt_path"])),
        expected_timestamp_receipt_sha256=str(audit["timestamp_receipt_sha256"]),
        expected_subject_sha256=str(receipt["sha256"]),
        bundle=bundle,
        expected_previous_log_head_sha256=str(audit["previous_log_head_sha256"]),
        expected_previous_log_sequence=(
            _strict_int(
                audit["log_sequence"],
                label="capture timestamp log sequence",
                minimum=1,
            )
            - 1
        ),
        expected_family=str(capture["family"]),
        expected_policy_id=str(capture["policy_id"]),
        expected_subject_role="signal_capture_receipt",
        expected_slot_id=(
            f"{capture['family']}:{capture['policy_id']}:capture:{capture['asof_date']}"
        ),
        subject_snapshot_bytes=capture_receipt_snapshot_bytes,
    )
    if reproduced != audit:
        raise ValueError("capture external timestamp audit is not reproducible")
    if not _utc(capture["captured_at_utc"], label="signed capture timestamp") <= _utc(
        audit["observed_at_utc"], label="capture timestamp"
    ) < _utc(capture["trusted_capture_timing"]["entry_execution_at_utc"], label="capture entry"):
        raise ValueError("capture external timestamp is not before entry")
    return reproduced


def validate_capture_timestamp_chain(
    captures: Sequence[Mapping[str, Any]],
    *,
    initial_anchor_audit: Mapping[str, Any],
) -> dict[str, Any]:
    ordered = sorted(captures, key=lambda row: str(row["asof_date"]))
    if not initial_anchor_audit.get("external_timestamp_pass"):
        raise ValueError("capture chain lacks a validated registration/activation anchor")
    previous = str(initial_anchor_audit["timestamp_receipt_sha256"])
    prior_sequence = _strict_int(
        initial_anchor_audit["log_sequence"],
        label="initial timestamp log sequence",
        minimum=0,
    )
    prior_observed = _utc(initial_anchor_audit["observed_at_utc"], label="initial anchor")
    for capture in ordered:
        audit = capture.get("external_timestamp_audit")
        if not isinstance(audit, dict):
            raise ValueError("capture is absent from the external timestamp chain")
        if audit.get("previous_log_head_sha256") != previous:
            raise ValueError("capture timestamp chain has a gap/fork")
        sequence = _strict_int(
            audit.get("log_sequence"),
            label="capture timestamp log sequence",
            minimum=0,
        )
        if sequence != prior_sequence + 1:
            raise ValueError("capture timestamp log sequence is not contiguous")
        observed = _utc(audit["observed_at_utc"], label="capture timestamp")
        if observed <= prior_observed:
            raise ValueError("capture timestamps are not strictly chronological")
        previous = str(audit["timestamp_receipt_sha256"])
        prior_sequence = sequence
        prior_observed = observed
    return {
        "capture_timestamp_chain_pass": bool(ordered),
        "capture_timestamp_count": len(ordered),
        "first_previous_log_head_sha256": initial_anchor_audit["timestamp_receipt_sha256"],
        "latest_log_head_sha256": previous,
        "latest_log_sequence": prior_sequence,
    }


def validate_canonical_outcome_attestations(
    *,
    outcome_receipt_path: Path,
    outcome_timestamp_receipt_path: Path,
    expected_outcome_timestamp_receipt_sha256: str,
    market_export_receipt_path: Path,
    expected_market_export_receipt_sha256: str,
    expected_outcome_receipt_sha256: str,
    source_sha256: Mapping[str, str],
    capture_registry_path: Path,
    expected_capture_registry_sha256: str,
    capture_registry_receipt_path: Path,
    expected_capture_registry_receipt_sha256: str,
    capture_registry_timestamp_receipt_path: Path,
    expected_capture_registry_timestamp_receipt_sha256: str,
    expected_domain_contract_sha256: str,
    expected_latest_capture_log_head_sha256: str,
    expected_latest_capture_log_sequence: int,
    bundle: CanonicalTrustBundle,
    evaluated_at_utc: str,
    latest_exit_execution_at_utc: str,
    capture_registry_snapshot_bytes: bytes | None = None,
    capture_registry_receipt_snapshot_bytes: bytes | None = None,
    outcome_receipt_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    registry_receipt, _, _ = _verified_receipt_snapshot(
        capture_registry_receipt_path,
        expected_sha256=expected_capture_registry_receipt_sha256,
        bundle=bundle,
        label="capture registry receipt",
        receipt_snapshot_bytes=capture_registry_receipt_snapshot_bytes,
    )
    if registry_receipt.get("domain_contract_sha256") != expected_domain_contract_sha256:
        raise ValueError("capture registry receipt changed the domain contract")
    registry, registry_sha, registry_bytes = _read_json_snapshot(
        capture_registry_path,
        label="capture registry",
        payload_bytes=capture_registry_snapshot_bytes,
    )
    if registry_sha != exact_sha256(
        expected_capture_registry_sha256,
        label="capture registry sha256",
    ):
        raise ValueError("capture registry bytes differ from the verified registry")
    registry_timestamp = validate_external_timestamp(
        subject_path=capture_registry_path,
        timestamp_receipt_path=capture_registry_timestamp_receipt_path,
        expected_timestamp_receipt_sha256=expected_capture_registry_timestamp_receipt_sha256,
        expected_subject_sha256=registry_sha,
        bundle=bundle,
        expected_previous_log_head_sha256=expected_latest_capture_log_head_sha256,
        expected_previous_log_sequence=expected_latest_capture_log_sequence,
        expected_family=bundle.family,
        expected_policy_id=str(registry.get("policy_id") or ""),
        expected_subject_role="capture_registry",
        expected_slot_id=(
            f"{bundle.family}:{registry.get('policy_id')}:registry:"
            f"{registry.get('complete_through_asof')}"
        ),
        subject_snapshot_bytes=registry_bytes,
    )
    outcome_receipt, outcome_receipt_sha, outcome_receipt_bytes = (
        _verified_receipt_snapshot(
        outcome_receipt_path,
        expected_sha256=expected_outcome_receipt_sha256,
        bundle=bundle,
        label="outcome receipt",
        receipt_snapshot_bytes=outcome_receipt_snapshot_bytes,
        )
    )
    if outcome_receipt.get("domain_contract_sha256") != expected_domain_contract_sha256:
        raise ValueError("outcome receipt changed the domain contract")
    expected = {
        "capture_registry_sha256": registry_sha,
        "capture_registry_timestamp_receipt_sha256": registry_timestamp[
            "timestamp_receipt_sha256"
        ],
        "latest_capture_log_head_sha256": expected_latest_capture_log_head_sha256,
    }
    for field, value in expected.items():
        if outcome_receipt.get(field) != value:
            raise ValueError(f"outcome receipt does not bind current append-only state: {field}")
    timestamp = validate_external_timestamp(
        subject_path=outcome_receipt_path,
        timestamp_receipt_path=outcome_timestamp_receipt_path,
        expected_timestamp_receipt_sha256=expected_outcome_timestamp_receipt_sha256,
        expected_subject_sha256=outcome_receipt_sha,
        bundle=bundle,
        expected_previous_log_head_sha256=registry_timestamp["timestamp_receipt_sha256"],
        expected_previous_log_sequence=_strict_int(
            registry_timestamp["log_sequence"],
            label="capture-registry timestamp log sequence",
            minimum=0,
        ),
        expected_family=bundle.family,
        expected_policy_id=str(registry.get("policy_id") or ""),
        expected_subject_role="outcome_receipt",
        expected_slot_id=(
            f"{bundle.family}:{registry.get('policy_id')}:outcome:"
            f"{registry.get('complete_through_asof')}"
        ),
        subject_snapshot_bytes=outcome_receipt_bytes,
    )
    if _utc(timestamp["observed_at_utc"], label="outcome timestamp") > _utc(
        evaluated_at_utc,
        label="evaluation timestamp",
    ):
        raise ValueError("outcome external timestamp is after evaluation")
    outcome_claimed_anchor = _utc(
        outcome_receipt.get("anchored_at_utc"), label="signed outcome anchor"
    )
    registry_observed = _utc(
        registry_timestamp["observed_at_utc"], label="capture-registry timestamp"
    )
    outcome_observed = _utc(timestamp["observed_at_utc"], label="outcome timestamp")
    if not registry_observed <= outcome_claimed_anchor <= outcome_observed:
        raise ValueError(
            "chronology must be registry timestamp <= signed outcome anchor <= outcome timestamp"
        )
    market = validate_market_data_export_receipt(
        source_sha256=source_sha256,
        receipt_path=market_export_receipt_path,
        expected_receipt_sha256=expected_market_export_receipt_sha256,
        bundle=bundle,
        expected_benchmark_ticker=str(outcome_receipt.get("benchmark_ticker") or ""),
        latest_exit_execution_at_utc=latest_exit_execution_at_utc,
        outcome_anchor_at_utc=outcome_claimed_anchor.isoformat(),
    )
    if _utc(market["exported_at_utc"], label="market export") > _utc(
        timestamp["observed_at_utc"], label="outcome timestamp"
    ):
        raise ValueError("market-data export was not sealed before outcome timestamp")
    return {
        "outcome_external_timestamp": timestamp,
        "capture_registry_external_timestamp": registry_timestamp,
        "market_data_export_attestation": market,
        "append_only_registry_head_bound_pass": True,
        "domain_contract_bound_pass": True,
    }


__all__ = [
    "attach_capture_timestamp",
    "domain_contract_sha256",
    "first_proven_month_end_after",
    "require_receipt_contract_binding",
    "revalidate_capture_timestamp",
    "validate_canonical_outcome_attestations",
    "validate_market_source_provenance",
    "validate_capture_timestamp_chain",
]
