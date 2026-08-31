"""Strict, immutable capital-context artifact owned by Portfolio Layer.

The contract deliberately contains no broker connector and imports no sector
package.  A caller supplies an already reviewed account AUM and the SHA-256 of
the source that authorized it.  The resulting artifact is report-only: it can
inform capacity calculations, but it cannot activate a sector or write weights.

Money and fractions are represented as canonical decimal strings.  This avoids
binary floating-point drift and lets the validator reproduce notional arithmetic
exactly with :class:`decimal.Decimal`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


CAPITAL_CONTEXT_SCHEMA_VERSION = "portfolio_capital_context_v1"
ARTIFACT_ROLE = "report_only_capital_context"
ALLOCATION_BASIS = "explicit_fraction_of_account_aum"
MAX_ACTIVE_SECTOR_COUNT = 10_000
MAX_FRACTION_DECIMAL_PLACES = 12

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "authority_owner",
        "artifact_role",
        "allocation_basis",
        "asof_date",
        "account_aum_usd",
        "active_sector_count",
        "equal_split_reference",
        "sector_cap_fraction",
        "sector_cap_notional_usd",
        "source_id",
        "source_sha256",
        "portfolio_write_performed",
        "payload_sha256",
    }
)
_EQUAL_SPLIT_KEYS = frozenset({"numerator", "denominator"})
_SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite decimal")
    text = str(value).strip()
    if not text or len(text) > 64:
        raise ValueError(f"{label} must be a finite decimal")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be a finite decimal")
    return parsed


def _fraction_decimal(value: object, *, label: str) -> Decimal:
    parsed = _parse_decimal(value, label=label)
    if not Decimal("0") < parsed <= Decimal("1"):
        raise ValueError(f"{label} must be in (0, 1]")
    decimal_places = max(0, -parsed.as_tuple().exponent)
    if decimal_places > MAX_FRACTION_DECIMAL_PLACES:
        raise ValueError(
            f"{label} cannot exceed {MAX_FRACTION_DECIMAL_PLACES} decimal places"
        )
    return parsed


def _aum_decimal(value: object, *, label: str) -> Decimal:
    parsed = _parse_decimal(value, label=label)
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    if parsed.as_tuple().exponent < -2:
        raise ValueError(f"{label} cannot contain fractions smaller than one cent")
    return parsed


def _canonical_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _canonical_money(value: Decimal) -> str:
    text = _canonical_decimal(value)
    if "." not in text:
        return f"{text}.00"
    whole, fractional = text.split(".", 1)
    if len(fractional) == 1:
        return f"{whole}.{fractional}0"
    return text


def _canonical_iso_date(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return value


def _positive_sector_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("active_sector_count must be an integer")
    if not 1 <= value <= MAX_ACTIVE_SECTOR_COUNT:
        raise ValueError(
            f"active_sector_count must be in [1, {MAX_ACTIVE_SECTOR_COUNT}]"
        )
    return value


def _validated_source_id(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_ID.fullmatch(value) is None:
        raise ValueError(
            "source_id must be 1-128 lowercase identifier characters "
            "([a-z0-9._:-])"
        )
    return value


def _validated_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _LOWER_SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def build_capital_context(
    *,
    account_aum_usd: object,
    active_sector_count: int,
    sector_cap_fraction: object,
    asof_date: str,
    source_id: str,
    source_sha256: str,
) -> dict[str, Any]:
    """Build a deterministic, self-hashed capital-context payload."""

    aum = _aum_decimal(account_aum_usd, label="account_aum_usd")
    count = _positive_sector_count(active_sector_count)
    cap = _fraction_decimal(sector_cap_fraction, label="sector_cap_fraction")
    payload: dict[str, Any] = {
        "schema_version": CAPITAL_CONTEXT_SCHEMA_VERSION,
        "authority_owner": "portfolio_layer",
        "artifact_role": ARTIFACT_ROLE,
        "allocation_basis": ALLOCATION_BASIS,
        "asof_date": _canonical_iso_date(asof_date, label="asof_date"),
        "account_aum_usd": _canonical_money(aum),
        "active_sector_count": count,
        "equal_split_reference": {"numerator": 1, "denominator": count},
        "sector_cap_fraction": _canonical_decimal(cap),
        "sector_cap_notional_usd": _canonical_money(aum * cap),
        "source_id": _validated_source_id(source_id),
        "source_sha256": _validated_sha256(source_sha256, label="source_sha256"),
        "portfolio_write_performed": False,
    }
    payload["payload_sha256"] = _canonical_payload_sha256(payload)
    return validate_capital_context(payload)


def validate_capital_context(
    payload: object,
    *,
    expected_payload_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate exact schema, arithmetic, role, provenance, and self-hash."""

    if not isinstance(payload, Mapping) or set(payload) != _ROOT_KEYS:
        raise ValueError("capital context has the wrong root schema")
    context = dict(payload)
    if context["schema_version"] != CAPITAL_CONTEXT_SCHEMA_VERSION:
        raise ValueError("unsupported capital context schema_version")
    if context["authority_owner"] != "portfolio_layer":
        raise ValueError("capital context authority_owner must be portfolio_layer")
    if context["artifact_role"] != ARTIFACT_ROLE:
        raise ValueError("capital context must remain report-only")
    if context["allocation_basis"] != ALLOCATION_BASIS:
        raise ValueError("capital context allocation_basis changed")
    if context["portfolio_write_performed"] is not False:
        raise ValueError("capital context cannot claim a Portfolio write")

    _canonical_iso_date(context["asof_date"], label="asof_date")
    count = _positive_sector_count(context["active_sector_count"])
    source_id = _validated_source_id(context["source_id"])
    source_sha = _validated_sha256(context["source_sha256"], label="source_sha256")
    payload_sha = _validated_sha256(context["payload_sha256"], label="payload_sha256")
    if source_id != context["source_id"] or source_sha != context["source_sha256"]:
        raise ValueError("capital context source provenance is not canonical")

    equal_split = context["equal_split_reference"]
    if not isinstance(equal_split, Mapping) or set(equal_split) != _EQUAL_SPLIT_KEYS:
        raise ValueError("equal_split_reference has the wrong schema")
    if (
        type(equal_split["numerator"]) is not int
        or type(equal_split["denominator"]) is not int
        or equal_split["numerator"] != 1
        or equal_split["denominator"] != count
    ):
        raise ValueError("equal_split_reference must be the exact rational 1/sector_count")

    if not isinstance(context["account_aum_usd"], str):
        raise ValueError("account_aum_usd must be a canonical decimal string")
    if not isinstance(context["sector_cap_fraction"], str):
        raise ValueError("sector_cap_fraction must be a canonical decimal string")
    if not isinstance(context["sector_cap_notional_usd"], str):
        raise ValueError("sector_cap_notional_usd must be a canonical decimal string")
    aum = _aum_decimal(context["account_aum_usd"], label="account_aum_usd")
    cap = _fraction_decimal(context["sector_cap_fraction"], label="sector_cap_fraction")
    notional = _parse_decimal(
        context["sector_cap_notional_usd"], label="sector_cap_notional_usd"
    )
    if context["account_aum_usd"] != _canonical_money(aum):
        raise ValueError("account_aum_usd is not canonical")
    if context["sector_cap_fraction"] != _canonical_decimal(cap):
        raise ValueError("sector_cap_fraction is not canonical")
    if context["sector_cap_notional_usd"] != _canonical_money(notional):
        raise ValueError("sector_cap_notional_usd is not canonical")
    if notional != aum * cap:
        raise ValueError("sector_cap_notional_usd does not equal AUM times cap fraction")

    calculated_sha = _canonical_payload_sha256(context)
    if calculated_sha != payload_sha:
        raise ValueError("capital context self-hash mismatch")
    if expected_payload_sha256 is not None:
        expected = _validated_sha256(
            expected_payload_sha256, label="expected_payload_sha256"
        )
        if payload_sha != expected:
            raise ValueError("capital context does not match the expected SHA-256 pin")
    return context


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"capital context duplicates key={key!r}")
        result[key] = value
    return result


def load_capital_context(
    path: str | Path,
    *,
    expected_payload_sha256: str | None = None,
) -> dict[str, Any]:
    """Load a regular, non-symlink JSON artifact and validate its SHA pin."""

    artifact = Path(path)
    if not artifact.is_file() or artifact.is_symlink():
        raise ValueError(f"capital context is missing or unsafe: {artifact}")
    payload = json.loads(
        artifact.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"capital context rejects non-finite JSON constant {value}")
        ),
    )
    return validate_capital_context(
        payload,
        expected_payload_sha256=expected_payload_sha256,
    )


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync; Windows does not expose a portable handle."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_capital_context_immutable(
    path: str | Path,
    payload: object,
) -> Path:
    """Atomically create a capital context without ever replacing an artifact.

    A fully flushed sibling temporary file is hard-linked into place.  Hard-link
    creation is atomic and fails if the destination already exists, closing the
    check-then-replace race that would otherwise violate immutability.
    """

    context = validate_capital_context(payload)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"capital context already exists: {destination}")
    encoded = (
        json.dumps(
            context,
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=".portfolio-capital-context-",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise FileExistsError(
                f"capital context already exists: {destination}"
            ) from exc
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


__all__ = [
    "CAPITAL_CONTEXT_SCHEMA_VERSION",
    "build_capital_context",
    "load_capital_context",
    "validate_capital_context",
    "write_capital_context_immutable",
]
