from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from industrials.core.csv_utils import read_csv_flexible
from industrials.core.db import utc_now
from industrials.core.text_norm import normalize_ticker


MODEL_FAMILY = "transportation"
SOURCE_ID = "transportation_security_continuity_policy"
REQUIRED_COLUMNS = frozenset(
    {
        "ticker",
        "company_name",
        "current_exchange",
        "current_security_start_date",
        "continuity_policy",
        "structural_break_date",
        "related_price_symbols",
        "related_exchanges",
        "related_currencies",
        "history_treatment",
        "required_fx_pair",
        "primary_source_url",
        "secondary_source_url",
        "evidence_label",
        "review_status",
        "confidence",
        "notes",
    }
)
CONTINUITY_POLICIES = frozenset(
    {
        "STRUCTURAL_BREAK_NO_STITCH",
        "CROSS_LISTING_SEPARATE_SECURITY",
        "SPAC_RECAPITALIZATION_NO_PRICE_STITCH",
    }
)
HISTORY_TREATMENTS = frozenset(
    {
        "separate_regime_no_return_stitch",
        "separate_listing_optional_issuer_proxy",
        "hard_boundary_no_spac_price_stitch",
    }
)


@dataclass(frozen=True)
class SecurityContinuityPolicy:
    ticker: str
    company_name: str
    current_exchange: str
    current_security_start_date: str
    continuity_policy: str
    structural_break_date: str
    related_price_symbols: str
    related_exchanges: str
    related_currencies: str
    history_treatment: str
    required_fx_pair: str
    primary_source_url: str
    secondary_source_url: str
    evidence_label: str
    review_status: str
    confidence: float
    notes: str


def parse_date(raw: object, *, field: str, ticker: str, allow_blank: bool = False) -> str:
    text = str(raw or "").strip()
    if not text and allow_blank:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid {field}={raw!r} for ticker={ticker}") from exc


def load_security_continuity_policies(path: Path) -> dict[str, SecurityContinuityPolicy]:
    rows = read_csv_flexible(path)
    if not rows:
        raise ValueError(f"Security-continuity policy cannot be empty: {path}")
    missing = REQUIRED_COLUMNS - set(rows[0])
    if missing:
        raise ValueError(f"Security-continuity policy missing columns={sorted(missing)}")
    output: dict[str, SecurityContinuityPolicy] = {}
    for row in rows:
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker or ticker in output:
            raise ValueError(f"Invalid or duplicate security-continuity ticker={ticker!r}")
        policy = str(row.get("continuity_policy") or "").strip().upper()
        treatment = str(row.get("history_treatment") or "").strip()
        review_status = str(row.get("review_status") or "").strip().lower()
        evidence_label = str(row.get("evidence_label") or "").strip()
        if policy not in CONTINUITY_POLICIES:
            raise ValueError(f"Unsupported continuity_policy={policy!r} ticker={ticker}")
        if treatment not in HISTORY_TREATMENTS:
            raise ValueError(f"Unsupported history_treatment={treatment!r} ticker={ticker}")
        if review_status != "primary_source_verified":
            raise ValueError(
                f"Security-continuity policy must be primary_source_verified: ticker={ticker}"
            )
        if evidence_label != "fact_source_reported":
            raise ValueError(
                f"Security-continuity evidence must be fact_source_reported: ticker={ticker}"
            )
        confidence = float(str(row.get("confidence") or ""))
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"Invalid confidence={confidence} ticker={ticker}")
        primary_url = str(row.get("primary_source_url") or "").strip()
        if not primary_url.startswith("https://"):
            raise ValueError(f"Primary source URL must be HTTPS: ticker={ticker}")
        related_symbols = str(row.get("related_price_symbols") or "").strip()
        required_fx_pair = str(row.get("required_fx_pair") or "").strip().upper()
        if policy == "CROSS_LISTING_SEPARATE_SECURITY" and not related_symbols:
            raise ValueError(f"Cross-listing policy requires related symbols: ticker={ticker}")
        if "NOK" in str(row.get("related_currencies") or "").upper() and required_fx_pair != "NOKUSD":
            raise ValueError(f"Oslo history requires NOKUSD: ticker={ticker}")
        output[ticker] = SecurityContinuityPolicy(
            ticker=ticker,
            company_name=str(row.get("company_name") or "").strip(),
            current_exchange=str(row.get("current_exchange") or "").strip(),
            current_security_start_date=parse_date(
                row.get("current_security_start_date"),
                field="current_security_start_date",
                ticker=ticker,
            ),
            continuity_policy=policy,
            structural_break_date=parse_date(
                row.get("structural_break_date"),
                field="structural_break_date",
                ticker=ticker,
                allow_blank=True,
            ),
            related_price_symbols=related_symbols,
            related_exchanges=str(row.get("related_exchanges") or "").strip(),
            related_currencies=str(row.get("related_currencies") or "").strip().upper(),
            history_treatment=treatment,
            required_fx_pair=required_fx_pair,
            primary_source_url=primary_url,
            secondary_source_url=str(row.get("secondary_source_url") or "").strip(),
            evidence_label=evidence_label,
            review_status=review_status,
            confidence=confidence,
            notes=str(row.get("notes") or "").strip(),
        )
    return output


def upsert_security_continuity_policies(
    connection: Any,
    *,
    policies: dict[str, SecurityContinuityPolicy],
    source_id: str = SOURCE_ID,
) -> int:
    now = utc_now()
    connection.execute(
        "DELETE FROM dim_security_continuity_policy WHERE model_family=? AND source_id=?",
        (MODEL_FAMILY, source_id),
    )
    for policy in policies.values():
        connection.execute(
            """
            INSERT INTO dim_security_continuity_policy(
                ticker, model_family, current_exchange, current_security_start_date,
                continuity_policy, structural_break_date, related_price_symbols,
                related_exchanges, related_currencies, history_treatment,
                required_fx_pair, primary_source_url, secondary_source_url,
                evidence_label, review_status, confidence, notes, source_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?, NULLIF(?, ''),
                      ?, NULLIF(?, ''), ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, model_family) DO UPDATE SET
                current_exchange=excluded.current_exchange,
                current_security_start_date=excluded.current_security_start_date,
                continuity_policy=excluded.continuity_policy,
                structural_break_date=excluded.structural_break_date,
                related_price_symbols=excluded.related_price_symbols,
                related_exchanges=excluded.related_exchanges,
                related_currencies=excluded.related_currencies,
                history_treatment=excluded.history_treatment,
                required_fx_pair=excluded.required_fx_pair,
                primary_source_url=excluded.primary_source_url,
                secondary_source_url=excluded.secondary_source_url,
                evidence_label=excluded.evidence_label,
                review_status=excluded.review_status,
                confidence=excluded.confidence,
                notes=excluded.notes,
                source_id=excluded.source_id,
                updated_at=excluded.updated_at
            """,
            (
                policy.ticker,
                MODEL_FAMILY,
                policy.current_exchange,
                policy.current_security_start_date,
                policy.continuity_policy,
                policy.structural_break_date,
                policy.related_price_symbols,
                policy.related_exchanges,
                policy.related_currencies,
                policy.history_treatment,
                policy.required_fx_pair,
                policy.primary_source_url,
                policy.secondary_source_url,
                policy.evidence_label,
                policy.review_status,
                policy.confidence,
                policy.notes,
                source_id,
                now,
                now,
            ),
        )
    return len(policies)
