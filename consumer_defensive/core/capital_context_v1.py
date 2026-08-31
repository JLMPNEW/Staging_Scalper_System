"""Independent validator for Portfolio Layer's immutable capital context.

Consumer Defensive intentionally does not import Portfolio Layer internals.
This module mirrors only the public JSON contract needed to authenticate and
normalize allocation context at the promotion bridge.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


PORTFOLIO_CAPITAL_CONTEXT_SCHEMA = "portfolio_capital_context_v1"
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
_MAX_ACTIVE_SECTOR_COUNT = 10_000
_MAX_FRACTION_DECIMAL_PLACES = 12


def canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decimal(value: Any, *, label: str) -> Decimal:
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
    return f"{whole}.{fractional}0" if len(fractional) == 1 else text


def _canonical_date(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return value


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _LOWER_SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def validate_portfolio_capital_context(
    payload: Mapping[str, Any],
    *,
    expected_payload_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate exact schema, provenance, canonical values, and arithmetic."""

    if not isinstance(payload, Mapping) or set(payload) != _ROOT_KEYS:
        raise ValueError("Portfolio capital context has the wrong root schema")
    context = dict(payload)
    if context["schema_version"] != PORTFOLIO_CAPITAL_CONTEXT_SCHEMA:
        raise ValueError("unsupported Portfolio capital context schema")
    if (
        context["authority_owner"] != "portfolio_layer"
        or context["artifact_role"] != "report_only_capital_context"
        or context["allocation_basis"] != "explicit_fraction_of_account_aum"
    ):
        raise ValueError("Portfolio capital context policy changed")
    if context["portfolio_write_performed"] is not False:
        raise ValueError("Portfolio capital context cannot claim a Portfolio write")
    _canonical_date(context["asof_date"], label="capital context asof_date")

    count = context["active_sector_count"]
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= _MAX_ACTIVE_SECTOR_COUNT
    ):
        raise ValueError("active_sector_count is outside its supported range")
    equal_split = context["equal_split_reference"]
    if not isinstance(equal_split, Mapping) or set(equal_split) != _EQUAL_SPLIT_KEYS:
        raise ValueError("equal_split_reference has the wrong schema")
    if (
        isinstance(equal_split["numerator"], bool)
        or isinstance(equal_split["denominator"], bool)
        or equal_split["numerator"] != 1
        or equal_split["denominator"] != count
    ):
        raise ValueError("equal_split_reference must equal 1/active_sector_count")

    if not isinstance(context["account_aum_usd"], str):
        raise ValueError("account_aum_usd must be a canonical decimal string")
    if not isinstance(context["sector_cap_fraction"], str):
        raise ValueError("sector_cap_fraction must be a canonical decimal string")
    if not isinstance(context["sector_cap_notional_usd"], str):
        raise ValueError("sector_cap_notional_usd must be a canonical decimal string")
    aum = _decimal(context["account_aum_usd"], label="account_aum_usd")
    fraction = _decimal(context["sector_cap_fraction"], label="sector_cap_fraction")
    notional = _decimal(
        context["sector_cap_notional_usd"],
        label="sector_cap_notional_usd",
    )
    if aum <= 0 or aum.as_tuple().exponent < -2:
        raise ValueError("account_aum_usd must be positive with cent precision")
    if not Decimal("0") < fraction <= Decimal("1"):
        raise ValueError("sector_cap_fraction must be in (0, 1]")
    if max(0, -fraction.as_tuple().exponent) > _MAX_FRACTION_DECIMAL_PLACES:
        raise ValueError("sector_cap_fraction has excessive decimal precision")
    if context["account_aum_usd"] != _canonical_money(aum):
        raise ValueError("account_aum_usd is not canonical")
    if context["sector_cap_fraction"] != _canonical_decimal(fraction):
        raise ValueError("sector_cap_fraction is not canonical")
    if context["sector_cap_notional_usd"] != _canonical_money(notional):
        raise ValueError("sector_cap_notional_usd is not canonical")
    if notional != aum * fraction:
        raise ValueError("sector_cap_notional_usd does not equal AUM times cap fraction")

    source_id = context["source_id"]
    if not isinstance(source_id, str) or _SOURCE_ID.fullmatch(source_id) is None:
        raise ValueError("source_id is not canonical")
    _digest(context["source_sha256"], label="source_sha256")
    payload_sha = _digest(context["payload_sha256"], label="payload_sha256")
    if canonical_payload_sha256(context) != payload_sha:
        raise ValueError("Portfolio capital context self-hash mismatch")
    if expected_payload_sha256 is not None and payload_sha != _digest(
        expected_payload_sha256,
        label="expected_payload_sha256",
    ):
        raise ValueError("Portfolio capital context payload hash is not trusted")
    return context


__all__ = [
    "PORTFOLIO_CAPITAL_CONTEXT_SCHEMA",
    "canonical_payload_sha256",
    "validate_portfolio_capital_context",
]
