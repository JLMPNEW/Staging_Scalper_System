"""Fail-closed contracts for externally anchored prospective evidence.

This module deliberately does not expose a pluggable verifier.  Callers must
provide an Ed25519 authority whose identity was loaded from the code-governed
out-of-band registry.  Signal capture and capture-registry receipts are signed
before outcomes exist; outcome receipts are signed only after exact execution
data are available.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .official_calendar import (
    validate_official_xnys_calendar_bytes,
)
from .canonical_values import exact_utc
from .protocol import canonical_sha256, exact_sha256
from .trusted_receipts import PinnedEd25519Authority


CAPTURE_SCHEMA = "future_only_signal_capture_v3"
CAPTURE_RECEIPT_SCHEMA = "future_signal_capture_receipt_v3"
REGISTRY_SCHEMA = "future_capture_registry_v1"
REGISTRY_RECEIPT_SCHEMA = "future_capture_registry_receipt_v2"
PROSPECTIVE_ROLE = "prospective_future_only_capture"
RETURN_CONVENTION = "next_session_open_execution_total_return_v1"


@dataclass(frozen=True)
class ProspectiveContract:
    family: str
    policy_id: str
    effective_from: date
    first_signal_date: date
    horizons: tuple[int, ...]
    minimum_counts: Mapping[int, int]
    benchmark_ticker: str
    cadence_id: str
    minimum_ic: float
    minimum_efficacy: float
    minimum_top_minus_bottom: float
    minimum_hit_rate: float
    transaction_cost_bps: float
    top_minus_bottom_basis: str
    maximum_ic_sign_pvalue: float = 1.0

    def identity(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "policy_id": self.policy_id,
            "effective_from": self.effective_from.isoformat(),
            "first_signal_date": self.first_signal_date.isoformat(),
            "horizons": list(self.horizons),
            "minimum_counts": {
                str(key): int(value) for key, value in sorted(self.minimum_counts.items())
            },
            "benchmark_ticker": self.benchmark_ticker,
            "cadence_id": self.cadence_id,
            "minimum_ic": self.minimum_ic,
            "minimum_efficacy": self.minimum_efficacy,
            "minimum_top_minus_bottom": self.minimum_top_minus_bottom,
            "minimum_hit_rate": self.minimum_hit_rate,
            "transaction_cost_bps": self.transaction_cost_bps,
            "top_minus_bottom_basis": self.top_minus_bottom_basis,
            "maximum_ic_sign_pvalue": self.maximum_ic_sign_pvalue,
            "return_convention": RETURN_CONVENTION,
        }


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


def read_json_snapshot(
    path: Path,
    *,
    label: str,
    payload_snapshot_bytes: bytes | None = None,
) -> tuple[dict[str, Any], str, Path, int]:
    resolved = Path(path).expanduser().resolve()
    payload_bytes = (
        bytes(payload_snapshot_bytes)
        if payload_snapshot_bytes is not None
        else resolved.read_bytes()
    )
    digest = hashlib.sha256(payload_bytes).hexdigest()
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be one valid UTF-8 JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload, digest, resolved, len(payload_bytes)


def read_source_snapshots(paths: Mapping[str, Path]) -> dict[str, bytes]:
    """Read each named source exactly once for hash and semantic validation."""

    snapshots: dict[str, bytes] = {}
    for role, path in sorted(paths.items()):
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"source is missing: {role}={resolved}")
        snapshots[role] = resolved.read_bytes()
    return snapshots


def read_calendar_bytes(
    payload_bytes: bytes,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    try:
        text = payload_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("trading calendar must be valid UTF-8 CSV") from exc
    rows = [dict(row) for row in csv.DictReader(io.StringIO(text, newline=""))]
    required = {"session_date", "entry_execution_at_utc", "exit_execution_at_utc"}
    if not rows or not required <= set(rows[0]):
        raise ValueError("trading calendar lacks exact session/open/close timestamps")
    sessions = [
        _exact_date(row["session_date"], label="calendar session date") for row in rows
    ]
    if sessions != sorted(set(sessions)):
        raise ValueError("trading calendar sessions must be unique and strictly sorted")
    for row in rows:
        session = _exact_date(row["session_date"], label="calendar session date")
        entry = _utc(row["entry_execution_at_utc"], label="calendar entry execution")
        exit_at = _utc(row["exit_execution_at_utc"], label="calendar exit execution")
        if entry.date().isoformat() != session or exit_at.date().isoformat() != session:
            raise ValueError("calendar execution timestamps must fall on their session date")
        if not entry < exit_at:
            raise ValueError("calendar open execution must precede close execution")
    return rows, {value: index for index, value in enumerate(sessions)}


def read_calendar(path: Path) -> tuple[list[dict[str, str]], dict[str, int]]:
    return read_calendar_bytes(Path(path).expanduser().resolve().read_bytes())


def scheduled_asofs(
    contract: ProspectiveContract,
    *,
    calendar_rows: Sequence[Mapping[str, str]],
    complete_through_asof: str,
) -> list[str]:
    cutoff = date.fromisoformat(
        _exact_date(complete_through_asof, label="registry complete-through asof")
    )
    all_sessions = [
        date.fromisoformat(
            _exact_date(row["session_date"], label="calendar session date")
        )
        for row in calendar_rows
    ]
    # Determine true month ends from the complete bound calendar, then apply
    # the cutoff. Truncating first would let a mid-month session masquerade
    # as a completed month-end observation.
    full_month_ends: dict[tuple[int, int], date] = {}
    for session in all_sessions:
        full_month_ends[(session.year, session.month)] = session
    ordered_months = sorted(full_month_ends)
    # A terminal calendar month has no later-month session proving that its
    # apparent last row is the actual exchange month end, so exclude it.
    proven_month_ends = {
        month: full_month_ends[month] for month in ordered_months[:-1]
    }
    if contract.cadence_id == "monthly_true_month_end_v1":
        return [
            value.isoformat()
            for _, value in sorted(proven_month_ends.items())
            if contract.first_signal_date <= value <= cutoff
        ]
    if contract.cadence_id == "special_first_then_month_end_v1":
        if contract.first_signal_date.isoformat() not in {
            _exact_date(row["session_date"], label="calendar session date")
            for row in calendar_rows
        }:
            raise ValueError("Transportation first signal is absent from the bound calendar")
        result = [contract.first_signal_date]
        result.extend(
            value
            for month, value in sorted(proven_month_ends.items())
            if month
            != (contract.first_signal_date.year, contract.first_signal_date.month)
            and contract.first_signal_date < value <= cutoff
        )
        return [value.isoformat() for value in result if value <= cutoff]
    raise ValueError(f"unsupported prospective cadence={contract.cadence_id}")


def _source_identities(
    paths: Mapping[str, Path],
    expected_sha256: Mapping[str, str],
    *,
    source_snapshot_bytes: Mapping[str, bytes] | None = None,
) -> dict[str, dict[str, Any]]:
    if set(paths) != set(expected_sha256):
        raise ValueError("capture source paths and expected hashes have different roles")
    if source_snapshot_bytes is not None and set(source_snapshot_bytes) != set(paths):
        raise ValueError("capture source snapshot roles differ from source paths")
    snapshots = (
        {role: bytes(value) for role, value in source_snapshot_bytes.items()}
        if source_snapshot_bytes is not None
        else read_source_snapshots(paths)
    )
    identities: dict[str, dict[str, Any]] = {}
    for role, path in sorted(paths.items()):
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"capture source is missing: {role}={resolved}")
        payload_bytes = snapshots[role]
        actual = hashlib.sha256(payload_bytes).hexdigest()
        if actual != exact_sha256(expected_sha256[role], label=f"{role} sha256"):
            raise ValueError(f"capture source hash mismatch: {role}")
        identities[role] = {
            "role": role,
            "path": str(resolved),
            "bytes": len(payload_bytes),
            "sha256": actual,
        }
    return identities


def normalize_signal_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    asof_date: str,
) -> list[dict[str, Any]]:
    if not rows:
        raise ValueError("prospective capture needs at least one signal row")
    if type(asof_date) is not str:
        raise ValueError("capture asof must be an exact YYYY-MM-DD string")
    expected_asof = asof_date
    try:
        parsed_asof = date.fromisoformat(expected_asof)
    except ValueError as exc:
        raise ValueError("capture asof must be exact YYYY-MM-DD") from exc
    if parsed_asof.isoformat() != expected_asof:
        raise ValueError("capture asof must be exact YYYY-MM-DD")
    required = {
        "asof_date",
        "ticker",
        "sleeve_id",
        "group_id",
        "score",
        "rank",
        "ranking_mode",
        "eligible_flag",
        "selected_top_flag",
        "selected_bottom_flag",
    }
    forbidden = ("forward_", "outcome", "realized", "return", "exit_", "target_")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        if required - set(raw):
            raise ValueError(f"signal row missing fields={sorted(required - set(raw))}")
        if any(any(token in str(key).lower() for token in forbidden) for key in raw):
            raise ValueError("outcome/revealed fields are forbidden at signal capture")
        row = dict(raw)
        ticker = _canonical_ticker(row["ticker"], label="signal ticker")
        if not ticker or ticker in seen:
            raise ValueError("ticker must be globally unique within a capture")
        seen.add(ticker)
        if type(row["asof_date"]) is not str:
            raise ValueError("signal row asof must be an exact YYYY-MM-DD string")
        row_asof = row["asof_date"]
        try:
            parsed_row_asof = date.fromisoformat(row_asof)
        except ValueError as exc:
            raise ValueError("signal row asof must be exact YYYY-MM-DD") from exc
        if parsed_row_asof.isoformat() != row_asof or row_asof != expected_asof:
            raise ValueError("signal row asof differs from capture asof")
        if type(row["score"]) not in {int, float}:
            raise ValueError("signal score must be a canonical JSON number")
        score = float(row["score"])
        if type(row["rank"]) is not int:
            raise ValueError("signal rank must be a canonical integer")
        rank = row["rank"]
        if not math.isfinite(score) or rank < 1:
            raise ValueError("signal score/rank is invalid")
        flag_fields = ("eligible_flag", "selected_top_flag", "selected_bottom_flag")
        if any(type(row[field]) is not int for field in flag_fields):
            raise ValueError("signal flags must be canonical integers")
        flags = {field: row[field] for field in flag_fields}
        predictive_raw = row.get("predictive_eligible_flag", flags["eligible_flag"])
        if type(predictive_raw) is not int:
            raise ValueError("predictive eligibility must be a canonical integer")
        predictive = predictive_raw
        if any(value not in (0, 1) for value in (*flags.values(), predictive)):
            raise ValueError("signal flags must be strict 0/1")
        if flags["selected_top_flag"] and flags["selected_bottom_flag"]:
            raise ValueError("one signal cannot be selected in both tails")
        if (flags["selected_top_flag"] or flags["selected_bottom_flag"]) and not predictive:
            raise ValueError("a non-predictive signal cannot be selected in a rank tail")
        clean = {
            **row,
            "asof_date": expected_asof,
            "ticker": ticker,
            "sleeve_id": str(row["sleeve_id"]),
            "group_id": str(row["group_id"]),
            "score": score,
            "rank": rank,
            "ranking_mode": str(row["ranking_mode"]),
            **flags,
            "predictive_eligible_flag": predictive,
        }
        unhashed = dict(clean)
        unhashed.pop("signal_row_sha256", None)
        clean["signal_row_sha256"] = canonical_sha256(unhashed)
        normalized.append(clean)
    return sorted(normalized, key=lambda row: (row["sleeve_id"], row["group_id"], row["rank"], row["ticker"]))


def _verify_receipt(
    path: Path,
    *,
    expected_sha256: str,
    authority: PinnedEd25519Authority,
    receipt_snapshot_bytes: bytes | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = Path(path).expanduser().resolve()
    receipt_bytes = (
        bytes(receipt_snapshot_bytes)
        if receipt_snapshot_bytes is not None
        else resolved.read_bytes()
    )
    actual = hashlib.sha256(receipt_bytes).hexdigest()
    byte_count = len(receipt_bytes)
    try:
        payload = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("trusted receipt must be one valid UTF-8 JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("trusted receipt must be a JSON object")
    if actual != exact_sha256(expected_sha256, label="trusted receipt sha256"):
        raise ValueError("trusted receipt SHA-256 mismatch")
    authority.verify_snapshot(receipt_bytes, actual, payload)
    return payload, {
        "path": str(resolved),
        "sha256": actual,
        "bytes": byte_count,
        "authority": authority.identity(),
    }


def build_strict_capture(
    *,
    contract: ProspectiveContract,
    asof_date: str,
    signal_rows: Sequence[Mapping[str, Any]],
    source_paths: Mapping[str, Path],
    expected_source_sha256: Mapping[str, str],
    required_source_roles: Iterable[str],
    trading_calendar_path: Path,
    capture_receipt_path: Path,
    expected_capture_receipt_sha256: str,
    authority: PinnedEd25519Authority,
    domain_fields: Mapping[str, Any] | None = None,
    source_snapshot_bytes: Mapping[str, bytes] | None = None,
    trading_calendar_snapshot_bytes: bytes | None = None,
    capture_receipt_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    raw_asof = str(asof_date)
    try:
        asof = date.fromisoformat(raw_asof)
    except ValueError as exc:
        raise ValueError("capture asof must be exact YYYY-MM-DD") from exc
    if asof.isoformat() != raw_asof:
        raise ValueError("capture asof must be exact YYYY-MM-DD")
    if asof < contract.effective_from or asof < contract.first_signal_date:
        raise ValueError("pre-effective/pre-first-signal artifacts cannot start the clock")
    roles = set(required_source_roles)
    if set(source_paths) != roles:
        raise ValueError("capture source roles do not exactly match the domain contract")
    calendar_bytes = (
        bytes(trading_calendar_snapshot_bytes)
        if trading_calendar_snapshot_bytes is not None
        else Path(trading_calendar_path).expanduser().resolve().read_bytes()
    )
    validate_official_xnys_calendar_bytes(calendar_bytes)
    identities = _source_identities(
        source_paths,
        expected_source_sha256,
        source_snapshot_bytes=source_snapshot_bytes,
    )
    calendar_hash = hashlib.sha256(calendar_bytes).hexdigest()
    if not any(identity["sha256"] == calendar_hash for identity in identities.values()):
        raise ValueError("capture sources do not bind the exact trading calendar")
    rows = normalize_signal_rows(signal_rows, asof_date=asof.isoformat())
    rows_hash = canonical_sha256(rows)
    receipt, receipt_identity = _verify_receipt(
        capture_receipt_path,
        expected_sha256=expected_capture_receipt_sha256,
        authority=authority,
        receipt_snapshot_bytes=capture_receipt_snapshot_bytes,
    )
    expected_receipt = {
        "schema_version": CAPTURE_RECEIPT_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "family": contract.family,
        "policy_id": contract.policy_id,
        "asof_date": asof.isoformat(),
        "capture_date": asof.isoformat(),
        "source_sha256": {role: identity["sha256"] for role, identity in identities.items()},
        "signal_rows_sha256": rows_hash,
        "trading_calendar_sha256": calendar_hash,
        "horizons": list(contract.horizons),
        "benchmark_ticker": contract.benchmark_ticker,
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "cadence_id": contract.cadence_id,
        "return_convention": RETURN_CONVENTION,
    }
    for field, expected in expected_receipt.items():
        if receipt.get(field) != expected:
            raise ValueError(f"signed capture receipt changed canonical identity: {field}")
    calendar_rows, session_index = read_calendar_bytes(calendar_bytes)
    sessions = [
        _exact_date(row["session_date"], label="calendar session date")
        for row in calendar_rows
    ]
    if asof.isoformat() not in session_index:
        raise ValueError("capture asof is absent from the bound trading calendar")
    next_index = session_index[asof.isoformat()] + 1
    if next_index >= len(calendar_rows):
        raise ValueError("calendar has no next-session entry for this capture")
    next_row = calendar_rows[next_index]
    captured_at_text = receipt.get("captured_at_utc")
    cutoff_at_text = receipt.get("signal_information_cutoff_at_utc")
    entry_at_text = receipt.get("entry_execution_at_utc")
    source_max_at_text = receipt.get("source_max_information_at_utc")
    generated_at_text = receipt.get("source_generated_at_utc")
    captured_at = _utc(captured_at_text, label="captured_at_utc")
    cutoff_at = _utc(
        cutoff_at_text,
        label="signal_information_cutoff_at_utc",
    )
    entry_at = _utc(entry_at_text, label="entry_execution_at_utc")
    source_max_at = _utc(
        source_max_at_text,
        label="source_max_information_at_utc",
    )
    generated_at = _utc(
        generated_at_text,
        label="source_generated_at_utc",
    )
    if _exact_date(
        receipt.get("entry_session_date") or "",
        label="signed entry session date",
    ) != sessions[next_index]:
        raise ValueError("signed entry date is not the next bound trading session")
    if entry_at != _utc(next_row["entry_execution_at_utc"], label="calendar entry execution"):
        raise ValueError("signed entry timestamp differs from the bound calendar open")
    asof_close = _utc(
        calendar_rows[session_index[asof.isoformat()]]["exit_execution_at_utc"],
        label="asof official close",
    )
    if cutoff_at != asof_close:
        raise ValueError("signal information cutoff must equal the official asof close")
    if not source_max_at <= cutoff_at <= generated_at <= captured_at < entry_at:
        raise ValueError("capture was not externally anchored after cutoff and before entry")
    body: dict[str, Any] = {
        "schema_version": CAPTURE_SCHEMA,
        "state": "captured_pending_outcomes",
        "evidence_class": "prospective_future_only",
        **contract.identity(),
        "asof_date": asof.isoformat(),
        "capture_date": asof.isoformat(),
        "captured_at_utc": captured_at_text,
        "signal_rows": rows,
        "signal_rows_sha256": rows_hash,
        "source_identities": identities,
        "trusted_receipt": receipt_identity,
        "trusted_capture_timing": {
            "signal_information_cutoff_at_utc": cutoff_at_text,
            "source_max_information_at_utc": source_max_at_text,
            "source_generated_at_utc": generated_at_text,
            "captured_at_utc": captured_at_text,
            "entry_session_date": sessions[next_index],
            "entry_session_index": next_index,
            "entry_execution_at_utc": entry_at_text,
            "capture_before_entry_pass": True,
        },
        "outcomes_present_at_capture": False,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
    }
    domain = dict(domain_fields or {})
    reserved = set(body) | {"capture_id", "payload_sha256"}
    collisions = sorted(set(domain) & reserved)
    if collisions:
        raise ValueError(
            f"domain capture fields collide with canonical fields={collisions}"
        )
    body.update(domain)
    body["capture_id"] = canonical_sha256(body)
    body["payload_sha256"] = canonical_sha256(body)
    return body


def validate_strict_capture(
    payload: Mapping[str, Any],
    *,
    contract: ProspectiveContract,
    authority: PinnedEd25519Authority,
    trading_calendar_path: Path,
    source_snapshot_bytes: Mapping[str, bytes] | None = None,
    trading_calendar_snapshot_bytes: bytes | None = None,
    capture_receipt_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    capture = dict(payload)
    if capture.get("schema_version") != CAPTURE_SCHEMA:
        raise ValueError("legacy/self-hashed captures cannot satisfy the canonical future gate")
    for field, expected in contract.identity().items():
        if capture.get(field) != expected:
            raise ValueError(f"capture changed prospective contract identity: {field}")
    if capture.get("state") != "captured_pending_outcomes" or capture.get("evidence_class") != "prospective_future_only":
        raise ValueError("capture is not prospective pending-outcome evidence")
    for field in (
        "outcomes_present_at_capture",
        "historical_results_can_authorize_production",
        "production_activation_authorized",
        "portfolio_write_enabled",
    ):
        if capture.get(field) is not False:
            raise ValueError(f"capture fail-closed field changed: {field}")
    optimizer_cap = capture.get("optimizer_cap")
    if (
        type(optimizer_cap) not in {int, float}
        or not math.isfinite(float(optimizer_cap))
        or float(optimizer_cap) != 0.0
    ):
        raise ValueError("capture optimizer cap must remain zero")
    supplied_payload_hash = exact_sha256(capture.pop("payload_sha256", None), label="capture payload sha256")
    if canonical_sha256(capture) != supplied_payload_hash:
        raise ValueError("capture payload SHA-256 mismatch")
    capture["payload_sha256"] = supplied_payload_hash
    capture_id = exact_sha256(capture.get("capture_id"), label="capture id")
    id_body = dict(capture)
    id_body.pop("payload_sha256")
    id_body.pop("capture_id")
    if canonical_sha256(id_body) != capture_id:
        raise ValueError("capture id does not bind exact capture body")
    rows = normalize_signal_rows(capture.get("signal_rows") or [], asof_date=str(capture["asof_date"]))
    if rows != capture.get("signal_rows"):
        raise ValueError("capture signal rows are not canonical/hash-consistent")
    if canonical_sha256(rows) != exact_sha256(capture.get("signal_rows_sha256"), label="signal rows sha256"):
        raise ValueError("capture signal census hash mismatch")
    calendar_bytes = (
        bytes(trading_calendar_snapshot_bytes)
        if trading_calendar_snapshot_bytes is not None
        else Path(trading_calendar_path).expanduser().resolve().read_bytes()
    )
    validate_official_xnys_calendar_bytes(calendar_bytes)
    calendar_hash = hashlib.sha256(calendar_bytes).hexdigest()
    identities = capture.get("source_identities")
    if not isinstance(identities, dict):
        raise ValueError("capture source identities are missing")
    if source_snapshot_bytes is not None and set(source_snapshot_bytes) != set(identities):
        raise ValueError("capture source snapshot roles differ from signed identities")
    for role, identity in identities.items():
        if not isinstance(identity, dict):
            raise ValueError("capture source identity is invalid")
        source_path = Path(str(identity.get("path") or ""))
        payload_bytes = (
            bytes(source_snapshot_bytes[role])
            if source_snapshot_bytes is not None
            else source_path.read_bytes()
        )
        if hashlib.sha256(payload_bytes).hexdigest() != identity.get("sha256"):
            raise ValueError(f"archived capture source bytes changed: {role}")
    if not any(identity.get("sha256") == calendar_hash for identity in identities.values()):
        raise ValueError("capture does not bind the evaluator trading calendar")
    receipt_identity = capture.get("trusted_receipt")
    if not isinstance(receipt_identity, dict):
        raise ValueError("capture lacks an external receipt identity")
    receipt, verified_identity = _verify_receipt(
        Path(str(receipt_identity.get("path") or "")),
        expected_sha256=str(receipt_identity.get("sha256") or ""),
        authority=authority,
        receipt_snapshot_bytes=capture_receipt_snapshot_bytes,
    )
    if verified_identity["sha256"] != receipt_identity.get("sha256"):
        raise ValueError("capture receipt identity changed")
    expected_source_hashes = {
        role: identity["sha256"] for role, identity in identities.items()
    }
    expected_receipt = {
        "schema_version": CAPTURE_RECEIPT_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "family": contract.family,
        "policy_id": contract.policy_id,
        "asof_date": capture["asof_date"],
        "capture_date": capture["capture_date"],
        "source_sha256": expected_source_hashes,
        "signal_rows_sha256": capture["signal_rows_sha256"],
        "trading_calendar_sha256": calendar_hash,
        "horizons": list(contract.horizons),
        "benchmark_ticker": contract.benchmark_ticker,
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "cadence_id": contract.cadence_id,
        "return_convention": RETURN_CONVENTION,
    }
    for field, expected in expected_receipt.items():
        if receipt.get(field) != expected:
            raise ValueError(f"capture receipt no longer binds canonical field: {field}")
    timing = capture.get("trusted_capture_timing")
    if not isinstance(timing, dict):
        raise ValueError("capture lacks exact signed timing")
    for field in (
        "signal_information_cutoff_at_utc",
        "source_max_information_at_utc",
        "source_generated_at_utc",
        "captured_at_utc",
        "entry_session_date",
        "entry_execution_at_utc",
    ):
        if timing.get(field) != receipt.get(field):
            raise ValueError(f"capture timing differs from signed receipt: {field}")
    calendar_rows, calendar_index = read_calendar_bytes(calendar_bytes)
    asof = _exact_date(capture["asof_date"], label="capture asof")
    if asof not in calendar_index or calendar_index[asof] + 1 >= len(calendar_rows):
        raise ValueError("capture has no governed next-session entry")
    expected_entry = calendar_rows[calendar_index[asof] + 1]
    cutoff_at = _utc(
        timing["signal_information_cutoff_at_utc"],
        label="signal information cutoff",
    )
    captured_at = _utc(timing["captured_at_utc"], label="captured at")
    source_max_at = _utc(
        timing["source_max_information_at_utc"],
        label="source max information",
    )
    generated_at = _utc(timing["source_generated_at_utc"], label="source generated at")
    if _utc(capture.get("captured_at_utc"), label="top-level captured at") != captured_at:
        raise ValueError("top-level capture timestamp differs from signed timing")
    if (
        timing["entry_session_date"] != expected_entry["session_date"]
        or _utc(timing["entry_execution_at_utc"], label="signed entry")
        != _utc(expected_entry["entry_execution_at_utc"], label="calendar entry")
        or cutoff_at
        != _utc(
            calendar_rows[calendar_index[asof]]["exit_execution_at_utc"],
            label="asof official close",
        )
        or not source_max_at
        <= cutoff_at
        <= generated_at
        <= captured_at
        < _utc(timing["entry_execution_at_utc"], label="entry execution")
    ):
        raise ValueError("capture timing is not strictly pre-entry on the bound calendar")
    return capture


def validate_capture_registry(
    *,
    registry_path: Path,
    registry_receipt_path: Path,
    expected_registry_receipt_sha256: str,
    authority: PinnedEd25519Authority,
    contract: ProspectiveContract,
    capture_paths: Sequence[Path],
    trading_calendar_path: Path,
    capture_snapshots: Sequence[
        tuple[Mapping[str, Any], str, Path, int]
    ] | None = None,
    trading_calendar_snapshot_bytes: bytes | None = None,
    registry_snapshot_bytes: bytes | None = None,
    registry_receipt_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    registry, registry_sha, registry_resolved, _ = read_json_snapshot(
        registry_path,
        label="capture registry",
        payload_snapshot_bytes=registry_snapshot_bytes,
    )
    if registry.get("schema_version") != REGISTRY_SCHEMA:
        raise ValueError("a signed canonical capture registry is required")
    if registry.get("state") != "prospective_append_only_capture_registry":
        raise ValueError("capture registry is not append-only prospective state")
    for field, expected in contract.identity().items():
        if registry.get(field) != expected:
            raise ValueError(f"capture registry changed contract identity: {field}")
    for field in ("production_activation_authorized", "portfolio_write_enabled"):
        if registry.get(field) is not False:
            raise ValueError(f"capture registry fail-closed field changed: {field}")
    optimizer_cap = registry.get("optimizer_cap")
    if (
        type(optimizer_cap) not in {int, float}
        or not math.isfinite(float(optimizer_cap))
        or float(optimizer_cap) != 0.0
    ):
        raise ValueError("capture registry optimizer cap must remain zero")
    calendar_bytes = (
        bytes(trading_calendar_snapshot_bytes)
        if trading_calendar_snapshot_bytes is not None
        else Path(trading_calendar_path).expanduser().resolve().read_bytes()
    )
    validate_official_xnys_calendar_bytes(calendar_bytes)
    rows = registry.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("capture registry has no externally anchored captures")
    if registry.get("rows_sha256") != canonical_sha256(rows):
        raise ValueError("capture registry row hash mismatch")
    receipt, receipt_identity = _verify_receipt(
        registry_receipt_path,
        expected_sha256=expected_registry_receipt_sha256,
        authority=authority,
        receipt_snapshot_bytes=registry_receipt_snapshot_bytes,
    )
    expected_receipt = {
        "schema_version": REGISTRY_RECEIPT_SCHEMA,
        "family": contract.family,
        "policy_id": contract.policy_id,
        "capture_registry_sha256": registry_sha,
        "capture_rows_sha256": registry["rows_sha256"],
        "complete_through_asof": registry.get("complete_through_asof"),
        "horizons": list(contract.horizons),
        "benchmark_ticker": contract.benchmark_ticker,
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "cadence_id": contract.cadence_id,
        "return_convention": RETURN_CONVENTION,
    }
    for field, expected in expected_receipt.items():
        if receipt.get(field) != expected:
            raise ValueError(f"signed capture registry receipt mismatch: {field}")
    if receipt.get("capture_ids") != [row.get("capture_id") for row in rows]:
        raise ValueError("signed registry receipt does not bind ordered capture ids")
    asofs = [
        _exact_date(row.get("asof_date") or "", label="registry row asof")
        for row in rows
    ]
    if asofs != sorted(set(asofs)):
        raise ValueError("capture registry has duplicate/out-of-order signal dates")
    calendar_rows, _ = read_calendar_bytes(calendar_bytes)
    expected_asofs = scheduled_asofs(
        contract,
        calendar_rows=calendar_rows,
        complete_through_asof=str(registry.get("complete_through_asof") or ""),
    )
    if asofs != expected_asofs:
        raise ValueError("capture registry has a cadence gap, duplicate, or off-schedule capture")
    passed: dict[str, tuple[Path, str, dict[str, Any]]] = {}
    snapshots = (
        list(capture_snapshots)
        if capture_snapshots is not None
        else [
            read_json_snapshot(path, label="registered capture")
            for path in capture_paths
        ]
    )
    if len(snapshots) != len(capture_paths):
        raise ValueError("capture snapshot census differs from capture paths")
    expected_snapshot_paths = {
        Path(path).expanduser().resolve() for path in capture_paths
    }
    observed_snapshot_paths = {
        Path(snapshot[2]).expanduser().resolve() for snapshot in snapshots
    }
    if (
        len(expected_snapshot_paths) != len(capture_paths)
        or observed_snapshot_paths != expected_snapshot_paths
    ):
        raise ValueError("capture snapshots do not correspond to the exact capture paths")
    for payload, capture_sha, resolved_capture, _ in snapshots:
        capture_id = str(payload.get("capture_id") or "")
        if not capture_id or capture_id in passed:
            raise ValueError("passed capture set has duplicate/blank capture ids")
        passed[capture_id] = (resolved_capture, capture_sha, payload)
    registered_ids = [str(row.get("capture_id") or "") for row in rows]
    if set(passed) != set(registered_ids) or len(passed) != len(rows):
        raise ValueError("evaluation capture set is not the exact signed registry census")
    for row in rows:
        capture_id = str(row["capture_id"])
        path, capture_sha, payload = passed[capture_id]
        if capture_sha != exact_sha256(
            row.get("capture_sha256"),
            label="registered capture sha256",
        ):
            raise ValueError("registered capture bytes changed")
        if _exact_date(
            payload.get("asof_date") or "", label="registered capture asof"
        ) != _exact_date(row.get("asof_date") or "", label="registry row asof"):
            raise ValueError("registered capture asof identity mismatch")
        if str(payload.get("trusted_receipt", {}).get("sha256") or "") != str(
            row.get("capture_receipt_sha256") or ""
        ):
            raise ValueError("registry does not bind exact capture receipt")
    return {
        "capture_registry_path": str(registry_resolved),
        "capture_registry_sha256": registry_sha,
        "capture_registry_receipt": receipt_identity,
        "complete_through_asof": registry["complete_through_asof"],
        "scheduled_capture_count": len(rows),
        "capture_ids": registered_ids,
        "capture_sha256_by_id": {
            str(row["capture_id"]): str(row["capture_sha256"]) for row in rows
        },
        "cadence_complete_no_gaps_pass": True,
        "exact_capture_census_pass": True,
    }


__all__ = [
    "CAPTURE_RECEIPT_SCHEMA",
    "CAPTURE_SCHEMA",
    "PROSPECTIVE_ROLE",
    "ProspectiveContract",
    "REGISTRY_RECEIPT_SCHEMA",
    "REGISTRY_SCHEMA",
    "RETURN_CONVENTION",
    "build_strict_capture",
    "normalize_signal_rows",
    "read_calendar",
    "read_calendar_bytes",
    "read_json_snapshot",
    "read_source_snapshots",
    "scheduled_asofs",
    "validate_capture_registry",
    "validate_strict_capture",
]
