from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Iterable, Mapping

from med_devices.core.text_norm import normalize_cik, normalize_ticker
from orchestration_contracts.financial_lineage import LINEAGE_FIELDS


PERIODIC_FORMS = frozenset(
    {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
)
SUPPLEMENTAL_FORMS = frozenset({"6-K", "6-K/A", "8-K", "8-K/A"})
CORE_COLUMNS = (
    "revenue",
    "operating_income",
    "operating_cash_flow",
    "total_assets",
    "cash_and_investments",
)
SOURCE_INCORPORATION_FIELDS = (
    *LINEAGE_FIELDS,
    "sec_live_discovery_status",
    "sec_live_discovery_time",
    "selected_financial_accessions",
    "financial_feature_updated_at",
    "fda_source_status",
    "fda_source_sealed_at",
    "fda_feature_updated_at",
    "reimbursement_feature_updated_at",
    "technical_feature_updated_at",
    "score_updated_at",
    "source_incorporation_status",
    "source_incorporation_reason",
)


def _timestamp(raw: object) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _accession(raw: object) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(raw or "")).upper()


def _json_object(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _feature_row(
    conn: sqlite3.Connection,
    table: str,
    *,
    company_id: int,
    asof: str,
) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError(f"Unsafe SQLite table identifier: {table!r}")
    row = conn.execute(
        f"""
        SELECT *
        FROM {table}
        WHERE company_id = ? AND asof_date = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (company_id, asof),
    ).fetchone()
    return dict(row) if row is not None else None


def _core_count(
    conn: sqlite3.Connection,
    *,
    accession: str,
    asof: str,
) -> int:
    rows = conn.execute(
        f"""
        SELECT {", ".join(CORE_COLUMNS)}
        FROM fact_financial_statement
        WHERE accession_nodash = ?
          AND COALESCE(NULLIF(filed_date, ''), period_end) <= ?
        """,
        (accession, asof),
    ).fetchall()
    return sum(any(row[column] is not None for row in rows) for column in CORE_COLUMNS)


def _latest_material_filing(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    asof: str,
    min_core_metric_count: int,
) -> tuple[dict[str, Any] | None, int]:
    rows = conn.execute(
        """
        SELECT accession_nodash, form, filing_date, report_date
        FROM fact_sec_filing
        WHERE company_id = ? AND filing_date <= ?
          AND form IN ('10-K', '10-K/A', '10-Q', '10-Q/A',
                       '20-F', '20-F/A', '40-F', '40-F/A',
                       '6-K', '6-K/A', '8-K', '8-K/A')
        ORDER BY filing_date DESC, accession_nodash DESC
        """,
        (company_id, asof),
    ).fetchall()
    for source in rows:
        filing = dict(source)
        form = str(filing.get("form") or "").upper()
        count = _core_count(
            conn,
            accession=str(filing.get("accession_nodash") or ""),
            asof=asof,
        )
        if form in PERIODIC_FORMS or (
            form in SUPPLEMENTAL_FORMS and count >= min_core_metric_count
        ):
            return filing, count
    return None, 0


def _live_sec_time(
    conn: sqlite3.Connection,
    *,
    cik: object,
    asof: str,
) -> datetime | None:
    cik_value = normalize_cik(cik)
    if not cik_value:
        return None
    rows = conn.execute(
        """
        SELECT query_params_json, request_time_utc
        FROM raw_api_responses
        WHERE source_id = 'sec_submissions'
          AND asof_date = ?
          AND response_status = 200
          AND endpoint LIKE ?
        ORDER BY request_time_utc DESC
        """,
        (asof, f"%/CIK{cik_value}.json"),
    ).fetchall()
    for row in rows:
        metadata = _json_object(row["query_params_json"])
        if (
            str(metadata.get("payload_source") or "").lower() == "fetched"
            and str(metadata.get("response_kind") or "").lower() == "root_submissions"
        ):
            parsed = _timestamp(row["request_time_utc"])
            if parsed is not None:
                return parsed
    return None


def _fda_seal_time(
    conn: sqlite3.Connection,
    *,
    asof: str,
    source_id: str,
) -> datetime | None:
    row = conn.execute(
        """
        SELECT s.sealed_at
        FROM ingestion_run_seals AS s
        JOIN ingestion_runs AS r ON r.ingestion_run_id = s.ingestion_run_id
        WHERE s.source_id = ? AND s.asof_date = ?
          AND s.response_count > 0 AND r.status = 'success'
        ORDER BY s.sealed_at DESC
        LIMIT 1
        """,
        (source_id, asof),
    ).fetchone()
    return _timestamp(row["sealed_at"]) if row is not None else None


def _selected_accessions(feature: Mapping[str, Any] | None) -> set[str]:
    payload = _json_object((feature or {}).get("payload_json"))
    values = payload.get("selected_financial_accessions")
    if not isinstance(values, list):
        return set()
    return {normalized for value in values if (normalized := _accession(value))}


def _ordered(*values: datetime | None) -> bool:
    if not values or any(value is None for value in values):
        return False
    concrete = [value for value in values if value is not None]
    return all(left <= right for left, right in zip(concrete, concrete[1:]))


def build_med_device_source_incorporation(
    conn: sqlite3.Connection,
    *,
    asof: str,
    score_rows: Iterable[Mapping[str, Any]],
    fda_source_id: str = "openfda_device",
    min_core_metric_count: int = 2,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    companies = {
        normalize_ticker(row["ticker"]): dict(row)
        for row in conn.execute("SELECT company_id, ticker, cik FROM dim_company")
        if normalize_ticker(row["ticker"])
    }
    fda_seal = _fda_seal_time(conn, asof=asof, source_id=fda_source_id)
    gated_rows: list[dict[str, str]] = []
    evidence_rows: list[dict[str, str]] = []

    for source in score_rows:
        original = {key: str(value or "") for key, value in dict(source).items()}
        row = dict(original)
        ticker = normalize_ticker(row.get("ticker"))
        company = companies.get(ticker)
        reasons: list[str] = []
        filing: dict[str, Any] | None = None
        core_count = 0
        selected: set[str] = set()
        sec_time = financial_time = fda_time = None
        reimbursement_time = technical_time = score_time = None

        if company is None:
            reasons.append("company_identity_missing")
        else:
            company_id = int(company["company_id"])
            filing, core_count = _latest_material_filing(
                conn,
                company_id=company_id,
                asof=asof,
                min_core_metric_count=min_core_metric_count,
            )
            financial = _feature_row(
                conn, "feature_financial_valuation", company_id=company_id, asof=asof
            )
            fda = _feature_row(
                conn, "feature_fda_product_risk", company_id=company_id, asof=asof
            )
            reimbursement = _feature_row(
                conn, "feature_reimbursement", company_id=company_id, asof=asof
            )
            technical = _feature_row(
                conn, "feature_technical_entry", company_id=company_id, asof=asof
            )
            score = _feature_row(
                conn, "med_device_daily_scores", company_id=company_id, asof=asof
            )
            selected = _selected_accessions(financial)
            sec_time = _live_sec_time(conn, cik=company.get("cik"), asof=asof)
            financial_time = _timestamp((financial or {}).get("updated_at"))
            fda_time = _timestamp((fda or {}).get("updated_at"))
            reimbursement_time = _timestamp((reimbursement or {}).get("updated_at"))
            technical_time = _timestamp((technical or {}).get("updated_at"))
            score_time = _timestamp((score or {}).get("updated_at"))

            if filing is None:
                reasons.append("material_financial_filing_missing")
            else:
                latest_accession = _accession(filing.get("accession_nodash"))
                if latest_accession not in selected:
                    reasons.append(
                        f"latest_financial_accession_not_selected:{latest_accession}"
                    )
            checks = (
                (sec_time is not None, "live_sec_submissions_discovery_missing"),
                (financial is not None, "financial_feature_missing_for_asof"),
                (fda_seal is not None, "successful_fda_source_seal_missing_for_asof"),
                (fda is not None, "fda_feature_missing_for_asof"),
                (reimbursement is not None, "reimbursement_feature_missing_for_asof"),
                (technical is not None, "technical_feature_missing_for_asof"),
                (score is not None, "score_row_missing_for_asof"),
                (_ordered(sec_time, financial_time, score_time), "sec_financial_score_timestamp_order_invalid"),
                (_ordered(fda_seal, fda_time, score_time), "fda_feature_score_timestamp_order_invalid"),
                (_ordered(reimbursement_time, score_time), "reimbursement_feature_score_timestamp_order_invalid"),
                (_ordered(technical_time, score_time), "technical_feature_score_timestamp_order_invalid"),
            )
            reasons.extend(reason for passed, reason in checks if not passed)

        incorporated = not reasons
        latest_accession = str((filing or {}).get("accession_nodash") or "")
        reason_text = "all_required_sources_incorporated" if incorporated else ";".join(reasons)
        evidence = {
            "financial_lineage_checked_asof_date": asof,
            "financial_lineage_status": "INCORPORATED" if incorporated else "REVIEW_REQUIRED",
            "financial_lineage_gate": "1" if incorporated else "0",
            "financial_lineage_classification": "INCORPORATED" if incorporated else "SOURCE_INCORPORATION_GAP",
            "latest_material_financial_filing_date": str((filing or {}).get("filing_date") or "")[:10],
            "latest_material_financial_form": str((filing or {}).get("form") or ""),
            "latest_material_financial_accession": latest_accession,
            "latest_material_financial_report_date": str((filing or {}).get("report_date") or "")[:10],
            "incorporated_financial_filing_date": str((filing or {}).get("filing_date") or "")[:10] if incorporated else "",
            "incorporated_financial_accession": latest_accession if incorporated else "",
            "incorporated_financial_report_date": str((filing or {}).get("report_date") or "")[:10] if incorporated else "",
            "incorporated_financial_core_metric_count": str(core_count) if incorporated else "0",
            "financial_lineage_reason": "latest_sources_incorporated_before_scoring" if incorporated else reason_text,
            "sec_live_discovery_status": "LIVE" if sec_time is not None else "MISSING",
            "sec_live_discovery_time": sec_time.isoformat() if sec_time is not None else "",
            "selected_financial_accessions": ";".join(sorted(selected)),
            "financial_feature_updated_at": financial_time.isoformat() if financial_time is not None else "",
            "fda_source_status": "SEALED_SUCCESS" if fda_seal is not None else "MISSING",
            "fda_source_sealed_at": fda_seal.isoformat() if fda_seal is not None else "",
            "fda_feature_updated_at": fda_time.isoformat() if fda_time is not None else "",
            "reimbursement_feature_updated_at": reimbursement_time.isoformat() if reimbursement_time is not None else "",
            "technical_feature_updated_at": technical_time.isoformat() if technical_time is not None else "",
            "score_updated_at": score_time.isoformat() if score_time is not None else "",
            "source_incorporation_status": "PASS" if incorporated else "REVIEW_REQUIRED",
            "source_incorporation_reason": reason_text,
        }
        row.update(evidence)
        evidence_row = dict(original)
        evidence_row.update(evidence)
        evidence_rows.append(evidence_row)
        if not incorporated and str(row.get("portfolio_candidate_gate") or "") == "1":
            row["portfolio_candidate_gate"] = "0"
            row["portfolio_candidate_status"] = "data_review_required"
            row["portfolio_candidate_reason"] = f"source_incorporation_required:{reason_text}"
        gated_rows.append(row)

    return gated_rows, evidence_rows
