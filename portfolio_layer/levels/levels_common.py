from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


VALUATION_CONTRACT_VERSION = "sector_valuation_inputs_v3"
LEVELS_MODEL_VERSION = "advisory_long_levels_v3_raw_nominal"
LEVEL_RESOLUTION_VERSION = "level_resolution_v2"
SUPPORTED_VALUATION_METHODS = {
    "eps_multiple",
    "fcf_yield",
    "fcf_yield_ttm",
    "ev_ebitda",
    "sector_specialist",
}
DIRECT_MARKET_PRICE_FIELDS = {
    "latest_price",
    "latest_close",
    "market_price",
    "market_reference",
    "close",
    "adjusted_close",
    "adj_close",
}
FCF_YIELD_FIELDS = {"fcf_yield_ttm", "fcf_yield"}
FCF_RECONSTRUCTION_MARKER = "transform:fcf_yield_times_same_row_price"
VALUATION_FIELDS = [
    "as_of_date", "available_at_utc", "ticker", "source_pipeline", "company_type",
    "currency", "fiscal_period_end", "revenue_forward", "eps_forward", "fcf_forward",
    "ebitda_forward", "net_debt", "senior_claims", "diluted_shares",
    "fcf_yield_ttm", "fcf_per_share_ttm",
    "sector_valuation_low", "sector_valuation_base", "sector_valuation_high",
    "sector_valuation_method", "sector_valuation_confidence",
    "sector_valuation_available_at_utc",
    "normalized_cyclical_flag", "method_allowlist", "valuation_input_lineage_json",
    "avg_dollar_volume_60d",
    "avg_dollar_volume_source",
    "next_catalyst_date", "next_catalyst_type",
    "input_freshness_json", "source_artifact_path", "source_artifact_sha256",
    "valuation_contract_version", "contract_status", "contract_reason",
]
LEVEL_FIELDS = [
    "as_of_date", "available_at_utc", "ticker", "source_pipeline", "universe_tier",
    "is_holding", "is_target", "investable_eligible", "valuation_status", "valuation_low",
    "valuation_base", "valuation_high", "valuation_methods_json", "anchor_disagreement",
    "valuation_confidence", "valuation_currency", "price_basis", "band_basis",
    "band_reference_status", "market_reference", "market_structure_json", "base_margin",
    "uncertainty_penalty", "financial_risk_penalty", "liquidity_penalty",
    "event_gap_penalty", "margin_of_safety", "long_entry_ceiling", "starter_band_low",
    "starter_band_high", "add_band_low", "add_band_high", "trim_band_low",
    "trim_band_high", "level_status", "inactive_reason", "expectations_state",
    "internal_expectations_state", "event_state", "recommended_state", "data_freshness_json",
    "valuation_contract_version", "levels_model_version", "input_digest",
]

LEVEL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS level_publication_ledger (
    row_sequence INTEGER PRIMARY KEY CHECK (row_sequence > 0),
    previous_row_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL UNIQUE,
    level_id TEXT NOT NULL UNIQUE,
    published_as_of TEXT NOT NULL,
    published_at_utc TEXT NOT NULL,
    ticker TEXT NOT NULL,
    band_type TEXT NOT NULL,
    band_low REAL,
    band_high REAL,
    level_status TEXT NOT NULL,
    inactive_reason TEXT NOT NULL,
    market_price_at_publish REAL,
    model_version TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    input_manifest_sha256 TEXT NOT NULL,
    code_sha256 TEXT NOT NULL,
    UNIQUE(ticker,published_as_of,band_type)
);
CREATE TRIGGER IF NOT EXISTS level_publication_no_delete
BEFORE DELETE ON level_publication_ledger BEGIN
    SELECT RAISE(ABORT, 'level publication ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS level_publication_no_update
BEFORE UPDATE ON level_publication_ledger BEGIN
    SELECT RAISE(ABORT, 'level publication ledger is append-only');
END;

CREATE TABLE IF NOT EXISTS level_publication_source_aliases (
    publication_row_sha256 TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    input_manifest_sha256 TEXT NOT NULL,
    code_sha256 TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    PRIMARY KEY (
        publication_row_sha256,config_sha256,input_manifest_sha256,code_sha256
    )
);
CREATE TRIGGER IF NOT EXISTS level_publication_alias_no_delete
BEFORE DELETE ON level_publication_source_aliases BEGIN
    SELECT RAISE(ABORT, 'level publication aliases are append-only');
END;
CREATE TRIGGER IF NOT EXISTS level_publication_alias_no_update
BEFORE UPDATE ON level_publication_source_aliases BEGIN
    SELECT RAISE(ABORT, 'level publication aliases are append-only');
END;

CREATE TABLE IF NOT EXISTS level_resolution_ledger (
    row_sequence INTEGER PRIMARY KEY CHECK (row_sequence > 0),
    previous_row_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL UNIQUE,
    publication_row_sha256 TEXT NOT NULL UNIQUE,
    level_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    published_as_of TEXT NOT NULL,
    band_type TEXT NOT NULL,
    resolved_through TEXT NOT NULL,
    first_touch_date TEXT NOT NULL,
    trading_days_to_touch INTEGER,
    touched_flag INTEGER NOT NULL CHECK (touched_flag IN (0,1)),
    maximum_favorable_excursion REAL NOT NULL,
    maximum_adverse_excursion REAL NOT NULL,
    resolution_schema_version TEXT NOT NULL DEFAULT 'level_resolution_v1',
    resolution_status TEXT NOT NULL DEFAULT 'resolved_legacy_v1',
    first_executable_fill_date TEXT NOT NULL DEFAULT '',
    entry_price_assumption TEXT NOT NULL DEFAULT '{}',
    forward_returns_by_horizon TEXT NOT NULL DEFAULT '{}',
    spread_and_cost_assumptions TEXT NOT NULL DEFAULT '{}',
    expectations_state_changes TEXT NOT NULL DEFAULT '[]',
    event_occurrences TEXT NOT NULL DEFAULT '[]',
    resolution_available_at_utc TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS level_resolution_no_delete
BEFORE DELETE ON level_resolution_ledger BEGIN
    SELECT RAISE(ABORT, 'level resolution ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS level_resolution_no_update
BEFORE UPDATE ON level_resolution_ledger BEGIN
    SELECT RAISE(ABORT, 'level resolution ledger is append-only');
END;

CREATE TABLE IF NOT EXISTS level_retirement_ledger (
    row_sequence INTEGER PRIMARY KEY CHECK (row_sequence > 0),
    previous_row_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL UNIQUE,
    publication_row_sha256 TEXT NOT NULL UNIQUE,
    level_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    published_as_of TEXT NOT NULL,
    band_type TEXT NOT NULL,
    retired_through TEXT NOT NULL,
    last_market_date TEXT NOT NULL,
    retirement_reason TEXT NOT NULL,
    retirement_available_at_utc TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS level_retirement_no_delete
BEFORE DELETE ON level_retirement_ledger BEGIN
    SELECT RAISE(ABORT, 'level retirement ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS level_retirement_no_update
BEFORE UPDATE ON level_retirement_ledger BEGIN
    SELECT RAISE(ABORT, 'level retirement ledger is append-only');
END;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def numeric_series(values: Any, *, index: Any = None) -> pd.Series:
    resolved_index = getattr(values, "index", index)
    return pd.Series(pd.to_numeric(values, errors="coerce"), index=resolved_index, dtype=float)


def first_number(row: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        value = optional_float(row.get(name))
        if value is not None:
            return value
    return None


def first_number_with_source(
    row: dict[str, Any], names: tuple[str, ...]
) -> tuple[float | None, str]:
    for name in names:
        value = optional_float(row.get(name))
        if value is not None:
            return value, name
    return None, ""


def valuation_lineage_errors(row: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        methods_raw = json.loads(str(row.get("method_allowlist", "[]")))
        lineage_raw = json.loads(
            str(row.get("valuation_input_lineage_json", "{}"))
        )
    except (TypeError, json.JSONDecodeError):
        return ["invalid_valuation_lineage_json"]
    if not isinstance(methods_raw, list) or not isinstance(lineage_raw, dict):
        return ["invalid_valuation_lineage_shape"]
    methods = {str(value) for value in methods_raw}
    if not methods <= SUPPORTED_VALUATION_METHODS:
        errors.append("unsupported_valuation_method")
    if set(lineage_raw) != methods:
        errors.append("method_lineage_mismatch")
    for method, raw_fields in lineage_raw.items():
        if not isinstance(raw_fields, list) or not raw_fields:
            errors.append(f"missing_method_lineage:{method}")
            continue
        fields = {str(value).strip() for value in raw_fields}
        if "" in fields:
            errors.append(f"blank_method_lineage:{method}")
        market_fields = fields & DIRECT_MARKET_PRICE_FIELDS
        reconstructed_ttm = (
            method == "fcf_yield_ttm"
            and FCF_RECONSTRUCTION_MARKER in fields
            and bool(fields & FCF_YIELD_FIELDS)
            and bool(market_fields)
        )
        if market_fields and not reconstructed_ttm:
            errors.append(f"direct_market_price_input:{method}")
        if (
            method == "fcf_yield_ttm"
            and not reconstructed_ttm
            and not fields
            & {"fcf_per_share_ttm", "free_cash_flow_per_share_ttm"}
        ):
            errors.append("ttm_fcf_missing_valid_numerator_source")
    return errors


def _source_date(value: str) -> date:
    return _source_instant(value).date()


def _source_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _specialist_fields_present(raw: dict[str, Any]) -> bool:
    return any(
        str(raw.get(field, "")).strip()
        for field in (
            "sector_valuation_low",
            "sector_valuation_base",
            "sector_valuation_high",
            "sector_valuation_method",
            "sector_valuation_confidence",
            "sector_valuation_available_at_utc",
        )
    )


def company_type(row: dict[str, Any], source_pipeline: str) -> str:
    cohort = str(
        row.get("calibration_cohort")
        or row.get("subsector")
        or row.get("development_stage")
        or ""
    ).casefold()
    if source_pipeline == "biotech" or "clinical" in cohort or "pre_revenue" in cohort:
        return "unprofitable_growth"
    if any(token in cohort for token in ("cyclical", "machinery", "transport")):
        return "cyclical"
    margin = first_number(row, ("operating_margin", "fcf_margin"))
    if margin is not None and margin > 0:
        return "stable_profitable"
    if margin is not None and margin <= 0:
        return "unprofitable_growth"
    return "growth_profitable"


def build_valuation_contract_row(
    *,
    as_of: str,
    ticker: str,
    source_pipeline: str,
    raw: dict[str, Any],
    source_path: Path,
    source_sha: str,
    valuation_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = valuation_policy or {}
    as_of_date = date.fromisoformat(as_of)
    source_time = str(
        raw.get("available_at_utc")
        or raw.get("snapshot_generated_at_utc")
        or raw.get("generated_at_utc")
        or raw.get("source_snapshot_asof_date")
        or raw.get("financial_feature_asof_date")
        or raw.get("asof_date")
        or raw.get("source_asof_date")
        or ""
    ).strip()
    source_time_missing = not source_time
    parsed_source_date = _source_date(source_time) if source_time else date.max
    pit_available = bool(source_time) and parsed_source_date <= as_of_date
    revenue, _revenue_source = first_number_with_source(
        raw, ("revenue_forward", "revenue_ntm", "forward_revenue_midpoint")
    )
    eps, eps_source = first_number_with_source(
        raw, ("eps_forward", "eps_ntm", "forward_eps_midpoint")
    )
    fcf, fcf_source = first_number_with_source(
        raw, ("fcf_forward", "free_cash_flow_forward")
    )
    ebitda, ebitda_source = first_number_with_source(
        raw, ("ebitda_forward", "ebitda_ntm", "forward_ebitda_midpoint")
    )
    net_debt, net_debt_source = first_number_with_source(
        raw, ("net_debt", "net_debt_usd")
    )
    senior_claims, senior_claims_source = first_number_with_source(
        raw, ("senior_claims", "senior_claims_usd")
    )
    shares, shares_source = first_number_with_source(
        raw,
        (
            "diluted_shares", "current_shares_outstanding",
            "diluted_weighted_average_shares",
        ),
    )
    fcf_yield_ttm, fcf_yield_source = first_number_with_source(
        raw, ("fcf_yield_ttm", "fcf_yield")
    )
    fcf_per_share_ttm, fcf_per_share_source = first_number_with_source(
        raw, ("fcf_per_share_ttm", "free_cash_flow_per_share_ttm")
    )
    latest_price, latest_price_source = first_number_with_source(
        raw, ("latest_price", "latest_close")
    )
    ttm_pipelines = {
        str(value).strip()
        for value in policy.get("ttm_fcf_reconstruction_pipelines", [])
        if str(value).strip()
    }
    max_fcf_yield = optional_float(policy.get("maximum_source_fcf_yield", 1.0))
    allow_ttm_reconstruction = bool(
        policy.get("allow_ttm_fcf_per_share_reconstruction", False)
    )
    price_available = str(raw.get("price_data_asof_date", "")).strip() or source_time
    price_pit_available = (
        bool(price_available) and _source_date(price_available) <= as_of_date
    )
    ttm_reconstructed = False
    if (
        fcf_per_share_ttm is None
        and allow_ttm_reconstruction
        and source_pipeline in ttm_pipelines
        and price_pit_available
        and fcf_yield_ttm is not None
        and 0 < fcf_yield_ttm <= (max_fcf_yield or 0.0)
        and latest_price is not None
        and latest_price > 0
    ):
        fcf_per_share_ttm = fcf_yield_ttm * latest_price
        ttm_reconstructed = True

    specialist_low = first_number(raw, ("sector_valuation_low",))
    specialist_base = first_number(raw, ("sector_valuation_base",))
    specialist_high = first_number(raw, ("sector_valuation_high",))
    specialist_method = str(raw.get("sector_valuation_method", "")).strip()
    specialist_confidence = optional_float(raw.get("sector_valuation_confidence"))
    specialist_available = str(
        raw.get("sector_valuation_available_at_utc", "")
    ).strip()
    specialist_present = _specialist_fields_present(raw)
    specialist_allowlists_raw = policy.get("sector_specialist_method_allowlist", {})
    specialist_allowlists = (
        specialist_allowlists_raw
        if isinstance(specialist_allowlists_raw, dict)
        else {}
    )
    allowed_specialist_methods = {
        str(value).strip()
        for value in specialist_allowlists.get(source_pipeline, [])
        if str(value).strip()
    }
    specialist_error = ""
    specialist_valid = False
    if specialist_present:
        specialist_numbers_complete = all(
            value is not None
            for value in (
                specialist_low,
                specialist_base,
                specialist_high,
                specialist_confidence,
            )
        )
        if (
            not specialist_numbers_complete
            or not specialist_method
            or not specialist_available
        ):
            specialist_error = "incomplete_sector_specialist_valuation"
        else:
            assert specialist_low is not None
            assert specialist_base is not None
            assert specialist_high is not None
            assert specialist_confidence is not None
            if not 0 < specialist_low <= specialist_base <= specialist_high:
                specialist_error = "invalid_sector_specialist_range"
            elif not 0 <= specialist_confidence <= 1:
                specialist_error = "invalid_sector_specialist_confidence"
            elif specialist_method not in allowed_specialist_methods:
                specialist_error = "sector_specialist_method_not_allowlisted"
            elif _source_date(specialist_available) > as_of_date:
                specialist_error = "sector_specialist_available_after_as_of"
            else:
                specialist_valid = True
    methods: list[str] = []
    if eps is not None and eps > 0:
        methods.append("eps_multiple")
    if fcf is not None and fcf > 0 and shares is not None and shares > 0:
        methods.append("fcf_yield")
    if fcf_per_share_ttm is not None and fcf_per_share_ttm > 0:
        methods.append("fcf_yield_ttm")
    if (
        ebitda is not None
        and ebitda > 0
        and net_debt is not None
        and senior_claims is not None
        and shares is not None
        and shares > 0
    ):
        methods.append("ev_ebitda")
    if specialist_valid:
        methods.append("sector_specialist")
    lineage: dict[str, list[str]] = {}
    if "eps_multiple" in methods:
        lineage["eps_multiple"] = [eps_source, "config:pe"]
    if "fcf_yield" in methods:
        lineage["fcf_yield"] = [fcf_source, shares_source, "config:fcf_yield"]
    if "fcf_yield_ttm" in methods:
        lineage["fcf_yield_ttm"] = (
            [
                fcf_yield_source,
                latest_price_source,
                FCF_RECONSTRUCTION_MARKER,
                "config:fcf_yield",
            ]
            if ttm_reconstructed
            else [fcf_per_share_source, "config:fcf_yield"]
        )
    if "ev_ebitda" in methods:
        lineage["ev_ebitda"] = [
            ebitda_source,
            net_debt_source,
            senior_claims_source,
            shares_source,
            "config:ev_ebitda",
        ]
    if "sector_specialist" in methods:
        lineage["sector_specialist"] = [
            "sector_valuation_low",
            "sector_valuation_base",
            "sector_valuation_high",
            "sector_valuation_method",
        ]
    effective_available = source_time
    if ttm_reconstructed and _source_instant(price_available) >= _source_instant(
        effective_available
    ):
        effective_available = price_available
    if (
        specialist_valid
        and _source_instant(specialist_available) >= _source_instant(source_time)
    ):
        effective_available = specialist_available
    if source_time_missing:
        reason = "missing_source_available_at"
    elif not pit_available:
        reason = "source_available_after_as_of"
    elif specialist_error:
        reason = specialist_error
    elif not methods:
        reason = "no_supported_absolute_valuation_method"
    else:
        reason = "ok"
    return {
        "as_of_date": as_of,
        "available_at_utc": effective_available,
        "ticker": ticker,
        "source_pipeline": source_pipeline,
        "company_type": company_type(raw, source_pipeline),
        "currency": str(raw.get("currency", raw.get("reported_currency", ""))).strip().upper(),
        "fiscal_period_end": str(raw.get("fiscal_period_end", "")).strip(),
        "revenue_forward": revenue,
        "eps_forward": eps,
        "fcf_forward": fcf,
        "ebitda_forward": ebitda,
        "net_debt": net_debt,
        "senior_claims": senior_claims,
        "diluted_shares": shares,
        "fcf_yield_ttm": fcf_yield_ttm,
        "fcf_per_share_ttm": fcf_per_share_ttm,
        "sector_valuation_low": specialist_low,
        "sector_valuation_base": specialist_base,
        "sector_valuation_high": specialist_high,
        "sector_valuation_method": specialist_method,
        "sector_valuation_confidence": specialist_confidence,
        "sector_valuation_available_at_utc": specialist_available,
        "normalized_cyclical_flag": int(
            str(raw.get("normalized_cyclical_flag", "0")).strip().casefold()
            in {"1", "1.0", "true", "yes"}
        ),
        "method_allowlist": canonical_json(methods),
        "valuation_input_lineage_json": canonical_json(lineage),
        "avg_dollar_volume_60d": first_number(
            raw, ("avg_dollar_volume_60d", "median_addv20")
        ),
        "avg_dollar_volume_source": (
            "avg_dollar_volume_60d"
            if first_number(raw, ("avg_dollar_volume_60d",)) is not None
            else "median_addv20"
            if first_number(raw, ("median_addv20",)) is not None
            else ""
        ),
        "next_catalyst_date": str(
            raw.get("next_catalyst_date") or raw.get("forward_catalyst_date") or ""
        ).strip(),
        "next_catalyst_type": str(
            raw.get("next_catalyst_type") or raw.get("forward_catalyst_type") or ""
        ).strip(),
        "input_freshness_json": canonical_json(
            {
                key: str(raw.get(key, "")).strip()
                for key in (
                    "source_snapshot_asof_date", "financial_data_asof_date",
                    "feature_data_asof_date", "price_data_asof_date",
                )
            }
        ),
        "source_artifact_path": str(source_path.resolve()),
        "source_artifact_sha256": source_sha,
        "valuation_contract_version": VALUATION_CONTRACT_VERSION,
        "contract_status": (
            "valid" if methods and pit_available and not specialist_error else "invalid"
        ),
        "contract_reason": reason,
    }


def valuation_methods(
    row: dict[str, Any],
    multiples: dict[str, dict[str, float]],
) -> dict[str, float]:
    company = str(row["company_type"])
    params = multiples.get(company, multiples.get("default", {}))
    shares = optional_float(row.get("diluted_shares"))
    methods = json.loads(str(row.get("method_allowlist", "[]")))
    values: dict[str, float] = {}
    if "eps_multiple" in methods:
        eps = optional_float(row.get("eps_forward"))
        pe = optional_float(params.get("pe"))
        if eps is not None and eps > 0 and pe is not None and pe > 0:
            values["eps_multiple"] = eps * pe
    if "fcf_yield" in methods:
        fcf = optional_float(row.get("fcf_forward"))
        yield_target = optional_float(params.get("fcf_yield"))
        if fcf is not None and fcf > 0 and shares and yield_target and yield_target > 0:
            values["fcf_yield"] = (fcf / shares) / yield_target
    if "fcf_yield_ttm" in methods:
        fcf_per_share = optional_float(row.get("fcf_per_share_ttm"))
        yield_target = optional_float(params.get("fcf_yield"))
        if (
            fcf_per_share is not None
            and fcf_per_share > 0
            and yield_target is not None
            and yield_target > 0
        ):
            values["fcf_yield_ttm"] = fcf_per_share / yield_target
    if "ev_ebitda" in methods:
        ebitda = optional_float(row.get("ebitda_forward"))
        net_debt = optional_float(row.get("net_debt"))
        claims = optional_float(row.get("senior_claims"))
        multiple = optional_float(params.get("ev_ebitda"))
        if ebitda is not None and shares and net_debt is not None and claims is not None and multiple:
            equity = ebitda * multiple - net_debt - claims
            if equity > 0:
                values["ev_ebitda"] = equity / shares
    if "sector_specialist" in methods:
        specialist = optional_float(row.get("sector_valuation_base"))
        specialist_method = str(row.get("sector_valuation_method", "")).strip()
        if specialist is not None and specialist > 0 and specialist_method:
            values[f"sector_specialist:{specialist_method}"] = specialist
    return values


def robust_valuation(values: dict[str, float]) -> tuple[float | None, float | None, float | None, float, float]:
    valid = np.asarray([value for value in values.values() if math.isfinite(value) and value > 0], dtype=float)
    if not valid.size:
        return None, None, None, 0.0, 0.0
    base = float(np.median(valid))
    low = float(valid.min())
    high = float(valid.max())
    disagreement = float((valid.max() - valid.min()) / base) if base > 0 and valid.size > 1 else 0.0
    confidence = min(1.0, 0.35 + 0.25 * valid.size) * max(0.0, 1.0 - min(disagreement, 1.0))
    return low, base, high, disagreement, confidence


def valuation_range(
    row: dict[str, Any],
    multiples: dict[str, dict[str, float]],
) -> tuple[dict[str, float], float | None, float | None, float | None, float, float]:
    if str(row.get("contract_status", "")) != "valid":
        return {}, None, None, None, 0.0, 0.0
    values = valuation_methods(row, multiples)
    low, base, high, disagreement, confidence = robust_valuation(values)
    methods = json.loads(str(row.get("method_allowlist", "[]")))
    if "sector_specialist" not in methods or base is None:
        return values, low, base, high, disagreement, confidence
    specialist_low = optional_float(row.get("sector_valuation_low"))
    specialist_high = optional_float(row.get("sector_valuation_high"))
    specialist_confidence = optional_float(row.get("sector_valuation_confidence"))
    if (
        specialist_low is None
        or specialist_high is None
        or specialist_confidence is None
    ):
        return {}, None, None, None, 0.0, 0.0
    assert low is not None and high is not None
    low = min(low, specialist_low)
    high = max(high, specialist_high)
    disagreement = (high - low) / base if base > 0 else 1.0
    anchor_count = len(values)
    reliability_ceiling = min(1.0, 0.35 + 0.25 * anchor_count)
    confidence = min(specialist_confidence, reliability_ceiling) / (
        1.0 + disagreement
    )
    return values, low, base, high, disagreement, confidence


def uncertainty_penalty(
    valuation: dict[str, Any],
    *,
    anchor_count: int,
    disagreement: float,
    as_of: date,
    margins: dict[str, Any],
) -> float:
    available_text = str(valuation.get("available_at_utc", "")).strip()
    age_days = (
        max(0, (as_of - date.fromisoformat(available_text[:10])).days)
        if len(available_text) >= 10
        else 10_000
    )
    minimum_anchors = int(margins.get("minimum_corroborating_anchors", 2))
    missing_anchors = max(0, minimum_anchors - anchor_count)
    staleness = min(
        float(margins.get("maximum_staleness_penalty", 0.05)),
        (age_days / 90.0)
        * float(margins.get("staleness_penalty_per_90_days", 0.01)),
    )
    raw = (
        float(margins.get("base_uncertainty_penalty", 0.02))
        + float(margins.get("disagreement_weight", 0.10)) * disagreement
        + float(margins.get("missing_anchor_penalty", 0.04)) * missing_anchors
        + staleness
    )
    return min(float(margins.get("maximum_uncertainty_penalty", 0.15)), raw)


def financial_risk_penalty(
    valuation: dict[str, Any], margins: dict[str, Any]
) -> float:
    ebitda = optional_float(valuation.get("ebitda_forward"))
    net_debt = optional_float(valuation.get("net_debt"))
    if ebitda is None or ebitda <= 0 or net_debt is None:
        return float(margins.get("unknown_financial_risk_penalty", 0.05))
    ratio = max(0.0, net_debt / ebitda)
    tiers_raw = margins.get("net_debt_to_ebitda_penalties", {})
    tiers = dict(tiers_raw) if isinstance(tiers_raw, dict) else {}
    if ratio <= float(tiers.get("low_max", 1.0)):
        return float(tiers.get("low", 0.0))
    if ratio <= float(tiers.get("medium_max", 2.5)):
        return float(tiers.get("medium", 0.02))
    if ratio <= float(tiers.get("high_max", 4.0)):
        return float(tiers.get("high", 0.05))
    return float(tiers.get("extreme", 0.10))


def band_geometry(
    *,
    center: float,
    trim_anchor: float,
    latest_price: float,
    market: dict[str, Any],
    geometry: dict[str, Any],
    intrinsic: bool,
    expectations_state: str,
) -> dict[str, float]:
    raw_volatility = float(market.get("volatility_unit") or 0.0)
    floor = latest_price * float(geometry.get("minimum_volatility_unit_pct", 0.01))
    regime_ratio = float(market.get("volatility_regime_ratio") or 1.0)
    scale = min(
        float(geometry.get("maximum_volatility_regime_scale", 1.75)),
        max(1.0, regime_ratio),
    )
    volatility = max(raw_volatility, floor) * scale
    starter_high = center
    if bool(geometry.get("support_snap_enabled", True)):
        try:
            supports = [
                float(value)
                for value in json.loads(
                    str(market.get("support_candidates_json", "[]"))
                )
                if math.isfinite(float(value)) and 0 < float(value) <= center
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            supports = []
        if supports:
            starter_high = max(supports)
    starter_low = max(
        0.0,
        starter_high
        - float(geometry.get("starter_width_vol", 0.40)) * volatility,
    )
    add_high = max(
        0.0, center - float(geometry.get("add_near_vol", 0.80)) * volatility
    )
    add_low = max(
        0.0, center - float(geometry.get("add_far_vol", 1.50)) * volatility
    )
    if bool(geometry.get("drawdown_percentile_add_enabled", True)):
        q25 = optional_float(market.get("drawdown_63d_q25"))
        q10 = optional_float(market.get("drawdown_63d_q10"))
        if q25 is not None and -1.0 < q25 < 0:
            add_high = min(add_high, latest_price * (1.0 + q25))
        if q10 is not None and -1.0 < q10 < 0:
            add_low = min(add_low, latest_price * (1.0 + q10))
    add_low, add_high = sorted((max(0.0, add_low), max(0.0, add_high)))
    trim_near = float(geometry.get("trim_near_vol", 0.80))
    if expectations_state in {"watch", "deteriorating", "broken"}:
        trim_near = max(
            0.0,
            trim_near
            - float(geometry.get("deteriorating_trim_tightening_vol", 0.25)),
        )
    trim_low = trim_anchor + trim_near * volatility
    trim_high = trim_anchor + float(geometry.get("trim_far_vol", 2.00)) * volatility
    if intrinsic:
        starter_high = min(starter_high, center)
        starter_low = min(starter_low, starter_high)
        add_high = min(add_high, center)
        add_low = min(add_low, add_high)
    return {
        "starter_band_low": starter_low,
        "starter_band_high": starter_high,
        "add_band_low": add_low,
        "add_band_high": add_high,
        "trim_band_low": trim_low,
        "trim_band_high": max(trim_low, trim_high),
        "effective_volatility_unit": volatility,
    }


def _tail_stat(series: pd.Series, window: int, operation: str) -> float | None:
    tail = series.iloc[-window:]
    if len(tail) < window or int(tail.notna().sum()) < window:
        return None
    if operation == "mean":
        return float(tail.mean())
    if operation == "std":
        return float(tail.std(ddof=1))
    raise ValueError(f"Unsupported tail operation: {operation}")


def market_structure(frame: pd.DataFrame) -> dict[str, float | str | None]:
    ordered = frame.sort_index()
    # Fundamental values are nominal per-share amounts. Raw OHLC is therefore the
    # only dimensionally consistent price basis; adjusted prices are reserved for
    # return calculations elsewhere.
    close = numeric_series(ordered["close"])
    valid_close = close.dropna()
    if valid_close.empty:
        raise ValueError("Market structure requires raw close")
    volume = numeric_series(ordered["raw_volume"]).reindex(close.index)
    tail63 = (
        pd.concat([close.rename("close"), volume.rename("volume")], axis=1)
        .dropna()
        .iloc[-63:]
    )
    tail60_dollar_volume = (
        (close * volume).dropna().iloc[-60:]
    )
    avg_dollar_volume_60d = (
        float(tail60_dollar_volume.mean())
        if len(tail60_dollar_volume) == 60
        else None
    )
    volume_weighted = (
        float((tail63["close"] * tail63["volume"]).sum() / tail63["volume"].sum())
        if not tail63.empty and float(tail63["volume"].sum()) > 0
        else float(valid_close.iloc[-63:].mean())
    )
    ma50 = _tail_stat(close, 50, "mean")
    ma200 = _tail_stat(close, 200, "mean")
    high = numeric_series(ordered["high"]).reindex(close.index)
    low = numeric_series(ordered["low"]).reindex(close.index)
    prior = close.shift(1)
    true_range = pd.concat([(high - low).abs(), (high - prior).abs(), (low - prior).abs()], axis=1).max(axis=1)
    atr20 = _tail_stat(true_range, 20, "mean")
    atr60 = _tail_stat(true_range, 60, "mean")
    returns = close.pct_change(fill_method=None)
    sigma20 = _tail_stat(returns, 20, "std")
    latest = float(valid_close.iloc[-1])
    volatility_unit = max(
        value
        for value in (
            atr20 or 0.0,
            0.5 * (atr60 or 0.0),
            (sigma20 or 0.0) * latest,
        )
    )
    references = [value for value in (volume_weighted, ma50, ma200) if value is not None]
    if len(references) == 3:
        assert ma50 is not None and ma200 is not None
        reference = 0.50 * volume_weighted + 0.25 * ma50 + 0.25 * ma200
    else:
        reference = float(np.mean(references)) if references else latest
    rolling_high = close.rolling(63, min_periods=40).max()
    drawdowns = (close / rolling_high - 1.0).dropna()
    recent_drawdowns = drawdowns.iloc[-252:]
    drawdown_q25 = (
        float(recent_drawdowns.quantile(0.25)) if not recent_drawdowns.empty else None
    )
    drawdown_q10 = (
        float(recent_drawdowns.quantile(0.10)) if not recent_drawdowns.empty else None
    )
    support_candidates = sorted(
        {
            float(value)
            for value in (volume_weighted, ma50, ma200)
            if value is not None and value > 0
        }
    )
    return_5d: float | None = None
    if len(valid_close) >= 6:
        latest_date = pd.Timestamp(str(valid_close.index[-1]))
        prior_date = pd.Timestamp(str(valid_close.index[-6]))
        if (latest_date - prior_date).days <= 10:
            return_5d = float(valid_close.iloc[-1] / valid_close.iloc[-6] - 1.0)
    return {
        "last_market_date": str(valid_close.index[-1])[:10],
        "price_basis": "raw_unadjusted_nominal",
        "latest_price": latest,
        "volume_weighted_daily_price_63": volume_weighted,
        "avg_dollar_volume_60d": avg_dollar_volume_60d,
        "ma50": ma50,
        "ma200": ma200,
        "atr20": atr20,
        "atr60": atr60,
        "sigma20": sigma20,
        "volatility_unit": volatility_unit,
        "market_reference": reference,
        "return_5d": return_5d,
        "drawdown_63d_q25": drawdown_q25,
        "drawdown_63d_q10": drawdown_q10,
        "support_candidates_json": canonical_json(support_candidates),
        "volatility_regime_ratio": (
            atr20 / atr60 if atr20 is not None and atr60 is not None and atr60 > 0 else None
        ),
    }


def _ensure_level_schema_migrations(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(level_resolution_ledger)")
    }
    additions = {
        "resolution_schema_version": (
            "TEXT NOT NULL DEFAULT 'level_resolution_v1'"
        ),
        "resolution_status": "TEXT NOT NULL DEFAULT 'resolved_legacy_v1'",
        "first_executable_fill_date": "TEXT NOT NULL DEFAULT ''",
        "entry_price_assumption": "TEXT NOT NULL DEFAULT '{}'",
        "forward_returns_by_horizon": "TEXT NOT NULL DEFAULT '{}'",
        "spread_and_cost_assumptions": "TEXT NOT NULL DEFAULT '{}'",
        "expectations_state_changes": "TEXT NOT NULL DEFAULT '[]'",
        "event_occurrences": "TEXT NOT NULL DEFAULT '[]'",
    }
    with conn:
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE level_resolution_ledger ADD COLUMN {name} {definition}"
                )


def connect_levels_db(path: Path, *, timeout_sec: float) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout_sec * 1000)}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(LEVEL_SCHEMA_SQL)
    _ensure_level_schema_migrations(conn)
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO level_publication_source_aliases(
                publication_row_sha256,config_sha256,input_manifest_sha256,
                code_sha256,recorded_at_utc
            )
            SELECT row_sha256,config_sha256,input_manifest_sha256,
                   code_sha256,published_at_utc
            FROM level_publication_ledger
            """
        )
    return conn


def append_level_publications(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    last = conn.execute(
        "SELECT row_sequence,row_sha256 FROM level_publication_ledger ORDER BY row_sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(last["row_sequence"]) if last is not None else 0
    previous = str(last["row_sha256"]) if last is not None else "0" * 64
    inserted = 0
    duplicates = 0
    with conn:
        for raw in sorted(rows, key=lambda value: (value["published_as_of"], value["ticker"], value["band_type"])):
            existing = conn.execute(
                "SELECT * FROM level_publication_ledger WHERE ticker=? AND published_as_of=? AND band_type=?",
                (raw["ticker"], raw["published_as_of"], raw["band_type"]),
            ).fetchone()
            if existing is not None:
                expected = {
                    key: raw[key]
                    for key in (
                        "published_as_of", "ticker", "band_type", "band_low", "band_high",
                        "level_status", "inactive_reason", "market_price_at_publish",
                        "model_version",
                    )
                }
                actual = {key: existing[key] for key in expected}
                if actual != expected:
                    raise RuntimeError(
                        f"First-write-wins level publication drift for {raw['ticker']} "
                        f"{raw['published_as_of']} {raw['band_type']}"
                    )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO level_publication_source_aliases(
                        publication_row_sha256,config_sha256,input_manifest_sha256,
                        code_sha256,recorded_at_utc
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        str(existing["row_sha256"]), str(raw["config_sha256"]),
                        str(raw["input_manifest_sha256"]), str(raw["code_sha256"]),
                        utc_now(),
                    ),
                )
                duplicates += 1
                continue
            sequence += 1
            level_id = digest({"ticker": raw["ticker"], "as_of": raw["published_as_of"], "band_type": raw["band_type"]})
            payload = {
                "row_sequence": sequence,
                "level_id": level_id,
                **{key: raw[key] for key in (
                    "published_as_of", "published_at_utc", "ticker", "band_type", "band_low",
                    "band_high", "level_status", "inactive_reason", "market_price_at_publish",
                    "model_version", "config_sha256", "input_manifest_sha256", "code_sha256",
                )},
            }
            row_hash = digest({"previous_row_sha256": previous, **payload})
            conn.execute(
                """
                INSERT INTO level_publication_ledger(
                    row_sequence,previous_row_sha256,row_sha256,level_id,published_as_of,
                    published_at_utc,ticker,band_type,band_low,band_high,level_status,
                    inactive_reason,market_price_at_publish,model_version,config_sha256,
                    input_manifest_sha256,code_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sequence, previous, row_hash, level_id, payload["published_as_of"],
                    payload["published_at_utc"], payload["ticker"], payload["band_type"],
                    payload["band_low"], payload["band_high"], payload["level_status"],
                    payload["inactive_reason"], payload["market_price_at_publish"],
                    payload["model_version"], payload["config_sha256"],
                    payload["input_manifest_sha256"], payload["code_sha256"],
                ),
            )
            conn.execute(
                """
                INSERT INTO level_publication_source_aliases(
                    publication_row_sha256,config_sha256,input_manifest_sha256,
                    code_sha256,recorded_at_utc
                ) VALUES(?,?,?,?,?)
                """,
                (
                    row_hash, payload["config_sha256"],
                    payload["input_manifest_sha256"], payload["code_sha256"], utc_now(),
                ),
            )
            previous = row_hash
            inserted += 1
    return inserted, duplicates


def verify_level_chain(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    previous = "0" * 64
    expected_sequence = 1
    for row in conn.execute("SELECT * FROM level_publication_ledger ORDER BY row_sequence").fetchall():
        payload = {
            "row_sequence": int(row["row_sequence"]),
            "level_id": str(row["level_id"]),
            **{key: row[key] for key in (
                "published_as_of", "published_at_utc", "ticker", "band_type", "band_low",
                "band_high", "level_status", "inactive_reason", "market_price_at_publish",
                "model_version", "config_sha256", "input_manifest_sha256", "code_sha256",
            )},
        }
        if int(row["row_sequence"]) != expected_sequence:
            errors.append(f"sequence_gap:{expected_sequence}")
        if str(row["previous_row_sha256"]) != previous:
            errors.append(f"previous_hash_mismatch:{expected_sequence}")
        if digest({"previous_row_sha256": previous, **payload}) != str(row["row_sha256"]):
            errors.append(f"row_hash_mismatch:{expected_sequence}")
        previous = str(row["row_sha256"])
        expected_sequence += 1
    missing_aliases = conn.execute(
        """
        SELECT COUNT(*)
        FROM level_publication_ledger publication
        LEFT JOIN level_publication_source_aliases alias
          ON alias.publication_row_sha256=publication.row_sha256
         AND alias.config_sha256=publication.config_sha256
         AND alias.input_manifest_sha256=publication.input_manifest_sha256
         AND alias.code_sha256=publication.code_sha256
        WHERE alias.publication_row_sha256 IS NULL
        """
    ).fetchone()[0]
    if int(missing_aliases):
        errors.append(f"publication_source_alias_missing:{missing_aliases}")
    return errors


def append_level_resolutions(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    last = conn.execute(
        "SELECT row_sequence,row_sha256 FROM level_resolution_ledger ORDER BY row_sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(last["row_sequence"]) if last is not None else 0
    previous = str(last["row_sha256"]) if last is not None else "0" * 64
    inserted = 0
    duplicates = 0
    legacy_fields = (
        "publication_row_sha256", "level_id", "ticker", "published_as_of",
        "band_type", "resolved_through", "first_touch_date",
        "trading_days_to_touch", "touched_flag", "maximum_favorable_excursion",
        "maximum_adverse_excursion",
    )
    v2_fields = (
        *legacy_fields,
        "resolution_schema_version", "resolution_status",
        "first_executable_fill_date", "entry_price_assumption",
        "forward_returns_by_horizon", "spread_and_cost_assumptions",
        "expectations_state_changes", "event_occurrences",
    )
    columns = (
        "row_sequence", "previous_row_sha256", "row_sha256", "publication_row_sha256",
        "level_id", "ticker", "published_as_of", "band_type", "resolved_through",
        "first_touch_date", "trading_days_to_touch", "touched_flag",
        "maximum_favorable_excursion", "maximum_adverse_excursion",
        "resolution_schema_version", "resolution_status",
        "first_executable_fill_date", "entry_price_assumption",
        "forward_returns_by_horizon", "spread_and_cost_assumptions",
        "expectations_state_changes", "event_occurrences",
        "resolution_available_at_utc",
    )
    with conn:
        for raw in sorted(
            rows,
            key=lambda value: (
                str(value["published_as_of"]), str(value["ticker"]), str(value["band_type"])
            ),
        ):
            publication_sha = str(raw["publication_row_sha256"])
            existing = conn.execute(
                "SELECT * FROM level_resolution_ledger WHERE publication_row_sha256=?",
                (publication_sha,),
            ).fetchone()
            expected = {key: raw[key] for key in v2_fields}
            if existing is not None:
                existing_version = str(existing["resolution_schema_version"])
                compare_fields = (
                    v2_fields
                    if existing_version == LEVEL_RESOLUTION_VERSION
                    else legacy_fields
                )
                actual = {key: existing[key] for key in compare_fields}
                expected_existing = {key: expected[key] for key in compare_fields}
                if actual != expected_existing:
                    raise RuntimeError(
                        f"First-write-wins level resolution drift for {raw['level_id']}"
                    )
                duplicates += 1
                continue
            sequence += 1
            payload = {
                "row_sequence": sequence,
                **expected,
                "resolution_available_at_utc": str(raw["resolution_available_at_utc"]),
            }
            row_hash = digest({"previous_row_sha256": previous, **payload})
            values = {
                **payload,
                "previous_row_sha256": previous,
                "row_sha256": row_hash,
            }
            conn.execute(
                f"INSERT INTO level_resolution_ledger({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            previous = row_hash
            inserted += 1
    return inserted, duplicates


def verify_level_resolution_chain(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    previous = "0" * 64
    expected_sequence = 1
    for row in conn.execute(
        "SELECT * FROM level_resolution_ledger ORDER BY row_sequence"
    ).fetchall():
        legacy_fields = (
            "publication_row_sha256", "level_id", "ticker", "published_as_of",
            "band_type", "resolved_through", "first_touch_date",
            "trading_days_to_touch", "touched_flag", "maximum_favorable_excursion",
            "maximum_adverse_excursion",
        )
        v2_fields = (
            *legacy_fields,
            "resolution_schema_version", "resolution_status",
            "first_executable_fill_date", "entry_price_assumption",
            "forward_returns_by_horizon", "spread_and_cost_assumptions",
            "expectations_state_changes", "event_occurrences",
        )
        fields = (
            v2_fields
            if str(row["resolution_schema_version"]) == LEVEL_RESOLUTION_VERSION
            else (*legacy_fields, "resolution_available_at_utc")
        )
        payload = {
            "row_sequence": int(row["row_sequence"]),
            **{key: row[key] for key in fields},
        }
        if fields == v2_fields:
            payload["resolution_available_at_utc"] = row[
                "resolution_available_at_utc"
            ]
        if int(row["row_sequence"]) != expected_sequence:
            errors.append(f"resolution_sequence_gap:{expected_sequence}")
        if str(row["previous_row_sha256"]) != previous:
            errors.append(f"resolution_previous_hash_mismatch:{expected_sequence}")
        if digest({"previous_row_sha256": previous, **payload}) != str(row["row_sha256"]):
            errors.append(f"resolution_row_hash_mismatch:{expected_sequence}")
        previous = str(row["row_sha256"])
        expected_sequence += 1
    return errors


def append_level_retirements(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    last = conn.execute(
        "SELECT row_sequence,row_sha256 FROM level_retirement_ledger "
        "ORDER BY row_sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(last["row_sequence"]) if last is not None else 0
    previous = str(last["row_sha256"]) if last is not None else "0" * 64
    identity_fields = (
        "publication_row_sha256", "level_id", "ticker", "published_as_of",
        "band_type", "retired_through", "last_market_date",
        "retirement_reason",
    )
    fields = (*identity_fields, "retirement_available_at_utc")
    columns = ("row_sequence", "previous_row_sha256", "row_sha256", *fields)
    inserted = 0
    duplicates = 0
    with conn:
        for raw in sorted(
            rows,
            key=lambda value: (
                str(value["published_as_of"]),
                str(value["ticker"]),
                str(value["band_type"]),
            ),
        ):
            publication_sha = str(raw["publication_row_sha256"])
            existing = conn.execute(
                "SELECT * FROM level_retirement_ledger "
                "WHERE publication_row_sha256=?",
                (publication_sha,),
            ).fetchone()
            expected = {key: raw[key] for key in identity_fields}
            if existing is not None:
                actual = {key: existing[key] for key in identity_fields}
                if actual != expected:
                    raise RuntimeError(
                        f"First-write-wins level retirement drift for {raw['level_id']}"
                    )
                duplicates += 1
                continue
            sequence += 1
            payload = {
                "row_sequence": sequence,
                **expected,
                "retirement_available_at_utc": str(
                    raw["retirement_available_at_utc"]
                ),
            }
            row_hash = digest({"previous_row_sha256": previous, **payload})
            values = {
                **payload,
                "previous_row_sha256": previous,
                "row_sha256": row_hash,
            }
            conn.execute(
                f"INSERT INTO level_retirement_ledger({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            previous = row_hash
            inserted += 1
    return inserted, duplicates


def verify_level_retirement_chain(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    previous = "0" * 64
    expected_sequence = 1
    fields = (
        "publication_row_sha256", "level_id", "ticker", "published_as_of",
        "band_type", "retired_through", "last_market_date",
        "retirement_reason", "retirement_available_at_utc",
    )
    for row in conn.execute(
        "SELECT * FROM level_retirement_ledger ORDER BY row_sequence"
    ).fetchall():
        payload = {
            "row_sequence": int(row["row_sequence"]),
            **{key: row[key] for key in fields},
        }
        if int(row["row_sequence"]) != expected_sequence:
            errors.append(f"retirement_sequence_gap:{expected_sequence}")
        if str(row["previous_row_sha256"]) != previous:
            errors.append(f"retirement_previous_hash_mismatch:{expected_sequence}")
        if digest({"previous_row_sha256": previous, **payload}) != str(
            row["row_sha256"]
        ):
            errors.append(f"retirement_row_hash_mismatch:{expected_sequence}")
        previous = str(row["row_sha256"])
        expected_sequence += 1
    return errors
