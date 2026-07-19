from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from typing import Any


DESPAC_BRIDGE_PROFILE = "SEC_XBRL_US_GAAP_DESPAC_BRIDGE"
DESPAC_BRIDGE_TAXONOMY = "sec-audited-predecessor"
REGISTRATION_FORMS = frozenset({"S-1", "S-1/A", "S-4", "S-4/A", "424B3", "424B4"})
BRIDGE_METRICS = frozenset(
    {
        "revenue",
        "cost_of_sales",
        "gross_profit",
        "operating_income",
        "net_income",
        "operating_cash_flow",
        "capex",
        "research_and_development",
        "stock_based_compensation",
        "depreciation_and_amortization",
        "interest_expense",
        "assets",
        "liabilities",
        "equity",
        "cash_and_equivalents",
        "total_debt",
    }
)
DURATION_METRICS = frozenset(
    {
        "revenue",
        "cost_of_sales",
        "gross_profit",
        "operating_income",
        "net_income",
        "operating_cash_flow",
        "capex",
        "research_and_development",
        "stock_based_compensation",
        "depreciation_and_amortization",
        "interest_expense",
    }
)
ALLOWED_PERIOD_CONFIDENCE = frozenset(
    {
        "table_column_month_day_year",
        "table_column_date",
        "table_column_year_default_dec31",
    }
)


def parse_iso_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def certified_predecessor_payload(raw: object) -> bool:
    try:
        payload = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        int(payload.get("historical_statement_flag") or 0) == 1
        and int(payload.get("projection_flag") or 0) == 0
        and str(payload.get("scale_confidence") or "") == "high"
        and str(payload.get("currency_confidence") or "") == "high"
        and str(payload.get("period_confidence") or "") in ALLOWED_PERIOD_CONFIDENCE
    )


def materially_equal(left: float, right: float) -> bool:
    tolerance = max(1.0, abs(left) * 1e-6, abs(right) * 1e-6)
    return abs(left - right) <= tolerance


def load_certified_predecessor_rows(
    conn: Any,
    *,
    ticker: str,
    source_id: str,
    asof: date,
) -> list[dict[str, Any]]:
    forms = sorted(REGISTRATION_FORMS)
    metrics = sorted(BRIDGE_METRICS)
    rows = conn.execute(
        f"""
        SELECT f.ticker, f.source_id, f.canonical_metric, f.period_start, f.period_end,
               f.filing_date, f.accepted_at, f.accession_number, f.form_type,
               f.fiscal_year, f.fiscal_period, f.concept_name, f.unit, f.value,
               f.source_priority, f.source_detail, r.payload_json
        FROM fact_sec_xbrl_fact f
        JOIN fact_sec_xbrl_fact_raw r ON r.raw_fact_id = f.raw_fact_id
        WHERE f.ticker = ? AND f.source_id = ? AND f.taxonomy = 'sec-text'
          AND f.source_detail = 'sec_archive_text_table_mapped'
          AND f.form_type IN ({','.join('?' for _ in forms)})
          AND f.canonical_metric IN ({','.join('?' for _ in metrics)})
          AND f.period_end <= ?
          AND COALESCE(substr(f.accepted_at, 1, 10), f.filing_date) <= ?
        ORDER BY f.filing_date DESC, f.accession_number DESC, f.period_end DESC
        """,
        (ticker, source_id, *forms, *metrics, asof.isoformat(), asof.isoformat()),
    ).fetchall()

    candidates: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw_row in rows:
        row = dict(raw_row)
        if not certified_predecessor_payload(row.get("payload_json")):
            continue
        metric = str(row.get("canonical_metric") or "")
        period_start = parse_iso_date(row.get("period_start"))
        period_end = parse_iso_date(row.get("period_end"))
        if period_end is None:
            continue
        if metric in DURATION_METRICS:
            if period_start is None:
                continue
            duration = (period_end - period_start).days
            if not 330 <= duration <= 390:
                continue
        value = row.get("value")
        if value is None:
            continue
        key = (
            str(row.get("accession_number") or ""),
            metric,
            period_end.isoformat(),
            str(row.get("unit") or ""),
        )
        candidates[key].append(row)

    certified: list[dict[str, Any]] = []
    for grouped_rows in candidates.values():
        values = [float(row["value"]) for row in grouped_rows]
        if any(not materially_equal(values[0], value) for value in values[1:]):
            continue
        row = grouped_rows[0]
        row.update(
            {
                "taxonomy": DESPAC_BRIDGE_TAXONOMY,
                "reporting_standard": "US_GAAP_DESPAC_BRIDGE",
                "fiscal_period": "FY",
                "source_priority": 150,
                "canonical_quality": "audited_predecessor_registration_statement",
            }
        )
        certified.append(row)

    by_period: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in certified:
        key = (
            str(row.get("accession_number") or ""),
            str(row.get("period_end") or ""),
            str(row.get("unit") or ""),
        )
        by_period[key][str(row.get("canonical_metric") or "")] = row
    for metric_rows in by_period.values():
        if "gross_profit" in metric_rows:
            continue
        revenue = metric_rows.get("revenue")
        cost_of_sales = metric_rows.get("cost_of_sales")
        if revenue is None or cost_of_sales is None:
            continue
        derived = dict(revenue)
        derived.update(
            {
                "canonical_metric": "gross_profit",
                "concept_name": "DerivedGrossProfit",
                "value": float(revenue["value"]) - float(cost_of_sales["value"]),
                "source_priority": 151,
                "canonical_quality": "audited_predecessor_registration_statement_derived_revenue_less_cost_of_sales",
            }
        )
        certified.append(derived)
    certified.sort(
        key=lambda row: (
            str(row.get("period_end") or ""),
            str(row.get("filing_date") or ""),
            str(row.get("accession_number") or ""),
            str(row.get("canonical_metric") or ""),
        ),
        reverse=True,
    )
    return certified
