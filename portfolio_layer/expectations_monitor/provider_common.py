from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import requests

from portfolio_layer.core.config import load_yaml


ALLOWED_BASE_URLS = {
    "alpha_vantage": "https://www.alphavantage.co",
    "fmp": "https://financialmodelingprep.com",
    "tiingo": "https://api.tiingo.com",
}
ACCESS_STATUSES = frozenset({"AVAILABLE", "EMPTY"})


@dataclass(frozen=True)
class ProbeResult:
    provider: str
    capability: str
    symbol: str
    requested_at_utc: str
    status: str
    http_status: int | None
    elapsed_ms: int
    payload_kind: str
    row_count: int
    field_names: str
    detail: str

    def as_row(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "capability": self.capability,
            "symbol": self.symbol,
            "requested_at_utc": self.requested_at_utc,
            "status": self.status,
            "http_status": "" if self.http_status is None else self.http_status,
            "elapsed_ms": self.elapsed_ms,
            "payload_kind": self.payload_kind,
            "row_count": self.row_count,
            "field_names": self.field_names,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ProviderPayloadResult:
    provider: str
    capability: str
    symbol: str
    requested_at_utc: str
    response_received_at_utc: str
    status: str
    http_status: int | None
    elapsed_ms: int
    payload_kind: str
    row_count: int
    field_names: str
    detail: str
    response_sha256: str
    payload: Any


def load_entitlements(path: Path) -> dict[str, Any]:
    config = load_yaml(path)
    if str(config.get("schema_version", "")) != "provider_entitlements_v1":
        raise ValueError(f"Unsupported provider entitlement schema in {path}")
    providers = config.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ValueError("Provider entitlements must define a non-empty providers mapping")
    return config


def provider_key(provider_config: Mapping[str, Any]) -> tuple[str, str | None]:
    env_name = str(provider_config.get("api_key_env", "")).strip()
    if not env_name:
        raise ValueError("Provider api_key_env must be configured")
    value = os.environ.get(env_name, "").strip()
    return env_name, value or None


def probe_dates(as_of: date) -> dict[str, str]:
    return {
        "start_date": (as_of - timedelta(days=45)).isoformat(),
        "end_date": as_of.isoformat(),
        "history_start_date": (as_of - timedelta(days=550)).isoformat(),
    }


def render_endpoint(
    *,
    provider: str,
    provider_config: Mapping[str, Any],
    capability_config: Mapping[str, Any],
    symbol: str,
    as_of: date,
) -> str:
    configured_base = str(provider_config.get("base_url", "")).rstrip("/")
    allowed_base = ALLOWED_BASE_URLS.get(provider)
    if configured_base != allowed_base:
        raise ValueError(f"Refusing untrusted {provider} base URL")
    values = {"symbol": urllib.parse.quote(symbol, safe=""), **probe_dates(as_of)}
    path = str(capability_config.get("path", "")).format(**values)
    if not path.startswith("/") or "://" in path:
        raise ValueError(f"Invalid capability path for {provider}")
    query_raw = capability_config.get("query", {})
    if not isinstance(query_raw, dict):
        raise ValueError(f"Capability query for {provider} must be a mapping")
    query = {str(key): str(value).format(**values) for key, value in query_raw.items()}
    return f"{configured_base}{path}?{urllib.parse.urlencode(query)}"


def request_auth(provider_config: Mapping[str, Any], key: str) -> tuple[dict[str, str], dict[str, str]]:
    """Build request authentication without adding credentials to the rendered endpoint."""
    auth = provider_config.get("auth")
    if not isinstance(auth, dict):
        raise ValueError("Provider auth configuration must be a mapping")
    mode = str(auth.get("mode", "header")).strip().casefold()
    headers = {
        "Accept": "application/json",
        "User-Agent": "staging-portfolio-provider-probe/1.0",
    }
    if mode == "header":
        header = str(auth.get("header", "")).strip()
        if not header or any(char in header for char in "\r\n:"):
            raise ValueError("Provider auth header is invalid")
        prefix = str(auth.get("prefix", ""))
        headers[header] = f"{prefix}{key}"
        return headers, {}
    if mode == "query":
        parameter = str(auth.get("query_parameter", "")).strip()
        if not parameter or any(char in parameter for char in "\r\n&=?#"):
            raise ValueError("Provider auth query parameter is invalid")
        return headers, {parameter: key}
    raise ValueError(f"Unsupported provider authentication mode: {mode}")


def _payload_rows(payload: Any) -> tuple[str, list[Mapping[str, Any]]]:
    if isinstance(payload, list):
        return "list", [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in (
            "data",
            "results",
            "historical",
            "estimates",
            "quarterlyEarnings",
        ):
            nested = payload.get(key)
            if isinstance(nested, list):
                return f"object.{key}", [row for row in nested if isinstance(row, dict)]
        return "object", [payload]
    return type(payload).__name__, []


def classify_payload(
    *,
    http_status: int,
    payload: Any,
    required_any_fields: list[str],
) -> tuple[str, str, int, str, str]:
    if http_status in {401, 402, 403}:
        return "PLAN_RESTRICTED_OR_UNAUTHORIZED", "unknown", 0, "", f"http_{http_status}"
    if http_status == 429:
        return "RATE_LIMITED", "unknown", 0, "", "http_429"
    if http_status < 200 or http_status >= 300:
        return "HTTP_ERROR", "unknown", 0, "", f"http_{http_status}"

    payload_kind, rows = _payload_rows(payload)
    provider_message_keys = ("Error Message", "Information", "Note", "error", "message")
    if isinstance(payload, dict) and any(key in payload for key in provider_message_keys):
        message_text = " ".join(str(payload.get(key, "")) for key in provider_message_keys if key in payload).casefold()
        status = (
            "RATE_LIMITED_MESSAGE"
            if any(token in message_text for token in ("rate limit", "call frequency", "calls per"))
            else "PROVIDER_MESSAGE"
        )
        return status, payload_kind, len(rows), ",".join(sorted(payload)), "provider_message"
    if not rows:
        return "EMPTY", payload_kind, 0, "", "no_rows"

    fields = sorted({str(key) for row in rows[:5] for key in row})
    if required_any_fields and not any(field in fields for field in required_any_fields):
        return "SCHEMA_MISMATCH", payload_kind, len(rows), ",".join(fields), "required_fields_absent"
    return "AVAILABLE", payload_kind, len(rows), ",".join(fields), "ok"


def _decode_json(body: bytes) -> Any:
    if not body:
        return []
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def fetch_capability_payload(
    *,
    provider: str,
    provider_config: Mapping[str, Any],
    capability: str,
    capability_config: Mapping[str, Any],
    symbol: str,
    as_of: date,
    timeout_sec: float,
    max_response_bytes: int,
    max_retries: int,
) -> ProviderPayloadResult:
    """Fetch one provider payload in memory without logging or persisting its content."""
    import hashlib

    initial_requested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    env_name, key = provider_key(provider_config)
    if key is None:
        return ProviderPayloadResult(
            provider,
            capability,
            symbol,
            initial_requested_at,
            initial_requested_at,
            "KEY_MISSING",
            None,
            0,
            "none",
            0,
            "",
            env_name,
            "",
            None,
        )
    url = render_endpoint(
        provider=provider,
        provider_config=provider_config,
        capability_config=capability_config,
        symbol=symbol,
        as_of=as_of,
    )
    headers, auth_params = request_auth(provider_config, key)
    required = capability_config.get("required_any_fields", [])
    required_fields = [str(value) for value in required] if isinstance(required, list) else []
    last_result: ProviderPayloadResult | None = None
    for attempt in range(max_retries + 1):
        requested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        started = time.monotonic()
        try:
            response = requests.get(
                url,
                headers=headers,
                params=auth_params,
                timeout=timeout_sec,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            received_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            result = ProviderPayloadResult(
                provider,
                capability,
                symbol,
                requested_at,
                received_at,
                "REQUEST_ERROR",
                None,
                int((time.monotonic() - started) * 1000),
                "none",
                0,
                "",
                type(exc).__name__,
                "",
                None,
            )
            if attempt < max_retries:
                last_result = result
                time.sleep(0.5 * (attempt + 1))
                continue
            return result
        body = response.content[: max_response_bytes + 1]
        elapsed = int((time.monotonic() - started) * 1000)
        received_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if len(body) > max_response_bytes:
            return ProviderPayloadResult(
                provider,
                capability,
                symbol,
                requested_at,
                received_at,
                "RESPONSE_TOO_LARGE",
                int(response.status_code),
                elapsed,
                "unknown",
                0,
                "",
                f"limit_{max_response_bytes}",
                "",
                None,
            )
        response_sha256 = hashlib.sha256(body).hexdigest()
        payload = _decode_json(body) if 200 <= response.status_code < 300 else []
        if payload is None:
            result = ProviderPayloadResult(
                provider,
                capability,
                symbol,
                requested_at,
                received_at,
                "NON_JSON_RESPONSE",
                int(response.status_code),
                elapsed,
                "unknown",
                0,
                "",
                "body_not_retained",
                response_sha256,
                None,
            )
        else:
            status, kind, row_count, fields, detail = classify_payload(
                http_status=int(response.status_code),
                payload=payload,
                required_any_fields=required_fields,
            )
            result = ProviderPayloadResult(
                provider,
                capability,
                symbol,
                requested_at,
                received_at,
                status,
                int(response.status_code),
                elapsed,
                kind,
                row_count,
                fields,
                detail,
                response_sha256,
                payload,
            )
        retryable = result.status in {"RATE_LIMITED", "RATE_LIMITED_MESSAGE"} or (
            result.http_status in {408, 425, 500, 502, 503, 504}
        )
        if retryable and attempt < max_retries:
            last_result = result
            time.sleep(0.5 * (attempt + 1))
            continue
        return result
    if last_result is None:
        raise AssertionError("Provider payload retry loop produced no result")
    return last_result


def probe_capability(
    *,
    provider: str,
    provider_config: Mapping[str, Any],
    capability: str,
    capability_config: Mapping[str, Any],
    symbol: str,
    as_of: date,
    timeout_sec: float,
    max_response_bytes: int,
    max_retries: int,
) -> ProbeResult:
    requested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    env_name, key = provider_key(provider_config)
    if key is None:
        return ProbeResult(provider, capability, symbol, requested_at, "KEY_MISSING", None, 0, "none", 0, "", env_name)

    url = render_endpoint(
        provider=provider,
        provider_config=provider_config,
        capability_config=capability_config,
        symbol=symbol,
        as_of=as_of,
    )
    headers, auth_params = request_auth(provider_config, key)
    required = capability_config.get("required_any_fields", [])
    required_fields = [str(value) for value in required] if isinstance(required, list) else []
    last_result: ProbeResult | None = None

    for attempt in range(max_retries + 1):
        started = time.monotonic()
        http_status: int | None = None
        body = b""
        try:
            response = requests.get(
                url,
                headers=headers,
                params=auth_params,
                timeout=timeout_sec,
                allow_redirects=False,
            )
            http_status = int(response.status_code)
            body = response.content[: max_response_bytes + 1]
        except requests.RequestException as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            last_result = ProbeResult(
                provider,
                capability,
                symbol,
                requested_at,
                "REQUEST_ERROR",
                None,
                elapsed,
                "none",
                0,
                "",
                type(exc).__name__,
            )
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return last_result

        elapsed = int((time.monotonic() - started) * 1000)
        if len(body) > max_response_bytes:
            return ProbeResult(
                provider,
                capability,
                symbol,
                requested_at,
                "RESPONSE_TOO_LARGE",
                http_status,
                elapsed,
                "unknown",
                0,
                "",
                f"limit_{max_response_bytes}",
            )
        if http_status is None:
            raise AssertionError("HTTP response completed without a status code")
        if http_status < 200 or http_status >= 300:
            status, kind, row_count, fields, detail = classify_payload(
                http_status=http_status,
                payload=[],
                required_any_fields=required_fields,
            )
            result = ProbeResult(
                provider,
                capability,
                symbol,
                requested_at,
                status,
                http_status,
                elapsed,
                kind,
                row_count,
                fields,
                detail,
            )
        else:
            payload = _decode_json(body)
            if payload is None:
                result = ProbeResult(
                    provider,
                    capability,
                    symbol,
                    requested_at,
                    "NON_JSON_RESPONSE",
                    http_status,
                    elapsed,
                    "unknown",
                    0,
                    "",
                    "body_not_retained",
                )
            else:
                status, kind, row_count, fields, detail = classify_payload(
                    http_status=http_status,
                    payload=payload,
                    required_any_fields=required_fields,
                )
                result = ProbeResult(
                    provider,
                    capability,
                    symbol,
                    requested_at,
                    status,
                    http_status,
                    elapsed,
                    kind,
                    row_count,
                    fields,
                    detail,
                )
        retryable_http = result.status == "RATE_LIMITED" or result.http_status in {408, 425, 500, 502, 503, 504}
        if retryable_http and attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))
            last_result = result
            continue
        return result

    if last_result is None:
        raise AssertionError("Probe retry loop produced no result")
    return last_result


def provider_has_access(results: list[ProbeResult], provider: str) -> bool:
    return any(row.provider == provider and row.status in ACCESS_STATUSES for row in results)


def run_selftest() -> None:
    available = classify_payload(
        http_status=200,
        payload=[{"symbol": "TEST", "epsAvg": 1.25}],
        required_any_fields=["epsAvg", "revenueAvg"],
    )
    assert available[0] == "AVAILABLE"
    assert "1.25" not in available[3]

    mismatch = classify_payload(
        http_status=200,
        payload=[{"symbol": "TEST", "unexpected": 1}],
        required_any_fields=["epsAvg"],
    )
    assert mismatch[0] == "SCHEMA_MISMATCH"
    assert classify_payload(http_status=403, payload={}, required_any_fields=[])[0] == (
        "PLAN_RESTRICTED_OR_UNAUTHORIZED"
    )
    assert classify_payload(http_status=402, payload="upgrade", required_any_fields=[])[0] == (
        "PLAN_RESTRICTED_OR_UNAUTHORIZED"
    )
    assert classify_payload(http_status=200, payload=[], required_any_fields=[])[0] == "EMPTY"

    rendered = render_endpoint(
        provider="fmp",
        provider_config={"base_url": ALLOWED_BASE_URLS["fmp"]},
        capability_config={"path": "/stable/test", "query": {"symbol": "{symbol}"}},
        symbol="BRK.B",
        as_of=date(2026, 7, 30),
    )
    assert "BRK.B" in rendered
    assert "apikey" not in rendered.casefold()

    alpha_payload = classify_payload(
        http_status=200,
        payload={
            "symbol": "TEST",
            "estimates": [
                {
                    "date": "2026-12-31",
                    "eps_estimate_average": "1.25",
                    "revenue_estimate_average": "100.0",
                }
            ],
        },
        required_any_fields=["eps_estimate_average", "revenue_estimate_average"],
    )
    assert alpha_payload[0] == "AVAILABLE"
    assert alpha_payload[1] == "object.estimates"

    earnings_payload = classify_payload(
        http_status=200,
        payload={
            "symbol": "TEST",
            "quarterlyEarnings": [
                {
                    "fiscalDateEnding": "2026-06-30",
                    "reportedDate": "2026-07-30",
                    "reportedEPS": "2.5",
                }
            ],
        },
        required_any_fields=["fiscalDateEnding", "reportedDate", "reportedEPS"],
    )
    assert earnings_payload[0] == "AVAILABLE"
    assert earnings_payload[1] == "object.quarterlyEarnings"

    query_headers, query_params = request_auth({"auth": {"mode": "query", "query_parameter": "apikey"}}, "secret-value")
    assert "secret-value" not in json.dumps(query_headers)
    assert query_params == {"apikey": "secret-value"}

    from unittest.mock import Mock, patch

    response = Mock(status_code=200, content=b'[{"epsAvg":1.25}]')
    with (
        patch.dict(os.environ, {"PROVIDER_SELFTEST_KEY": "secret-value"}),
        patch.object(requests, "get", return_value=response) as request,
    ):
        fetched = fetch_capability_payload(
            provider="fmp",
            provider_config={
                "base_url": ALLOWED_BASE_URLS["fmp"],
                "api_key_env": "PROVIDER_SELFTEST_KEY",
                "auth": {"mode": "header", "header": "apikey", "prefix": ""},
            },
            capability="test",
            capability_config={
                "path": "/stable/test",
                "query": {"symbol": "{symbol}"},
                "required_any_fields": ["epsAvg"],
            },
            symbol="TEST",
            as_of=date(2026, 7, 31),
            timeout_sec=1.0,
            max_response_bytes=1000,
            max_retries=0,
        )
    assert fetched.status == "AVAILABLE"
    assert fetched.response_sha256
    called_url = str(request.call_args.args[0])
    called_headers = request.call_args.kwargs["headers"]
    assert "secret-value" not in called_url
    assert called_headers["apikey"] == "secret-value"
