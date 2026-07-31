from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from industrials.transportation.reviewed_operand_repair import SOURCE_ID


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCOPE_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "review_policies"
    / "transportation_required_metric_repair_scope.csv"
)
CANONICAL_SOURCE_ID = "sec_companyfacts"
SUPPORTED_METRICS = frozenset(
    {
        "capex_to_revenue",
        "fcf_margin",
        "operating_margin",
        "cash_runway_years",
        "capital_raise_dependence",
    }
)
ANCHOR_METRIC = {
    "capex_to_revenue": "capex",
    "fcf_margin": "capex",
    "operating_margin": "operating_income",
    "cash_runway_years": "capex",
    "capital_raise_dependence": "capex",
}
DURATION_DEPENDENCIES = {
    "capex_to_revenue": ("revenue", "capex"),
    "fcf_margin": ("revenue", "operating_cash_flow", "capex"),
    "operating_margin": ("revenue", "operating_income"),
    "cash_runway_years": ("operating_cash_flow", "capex"),
    "capital_raise_dependence": ("operating_cash_flow", "capex"),
}


@dataclass(frozen=True)
class AnnualMetricResolution:
    ticker: str
    metric_name: str
    availability_status: str
    metric_value: float | None
    unit: str
    accession_number: str
    filing_date: str
    period_start: str
    period_end: str
    taxonomy: str
    concept_name: str
    confidence: float
    status_reason: str
    provenance_json: str


def load_scope_pairs(
    path: Path = DEFAULT_SCOPE_PATH,
) -> frozenset[tuple[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    pairs = {
        (
            str(row.get("ticker") or "").strip().upper(),
            str(row.get("metric_name") or "").strip(),
        )
        for row in rows
    }
    if ("", "") in pairs or not pairs:
        raise ValueError(f"{path}: invalid or empty required-metric repair scope")
    return frozenset(pairs)


def _finite(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _duration_days(row: Mapping[str, Any]) -> int | None:
    try:
        return (
            date.fromisoformat(str(row["period_end"])[:10])
            - date.fromisoformat(str(row["period_start"])[:10])
        ).days
    except (KeyError, TypeError, ValueError):
        return None


def _annual(row: Mapping[str, Any]) -> bool:
    days = _duration_days(row)
    return days is not None and 300 <= days <= 380


def _best_row(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    candidates = [dict(row) for row in rows]
    if not candidates:
        return None
    minimum_priority = min(int(row.get("source_priority") or 100) for row in candidates)
    preferred = [
        row
        for row in candidates
        if int(row.get("source_priority") or 100) == minimum_priority
    ]
    return max(
        preferred,
        key=lambda row: (
            str(row.get("filing_date") or ""),
            str(row.get("accepted_at") or ""),
            str(row.get("accession_number") or ""),
            str(row.get("concept_name") or ""),
        ),
    )


def _fact_provenance(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "canonical_metric": row["canonical_metric"],
        "value": float(row["value"]),
        "unit": str(row["unit"]).upper(),
        "accession_number": str(row.get("accession_number") or ""),
        "filing_date": str(row.get("filing_date") or "")[:10],
        "accepted_at": str(row.get("accepted_at") or ""),
        "taxonomy": str(row.get("taxonomy") or ""),
        "concept_name": str(row.get("concept_name") or ""),
        "source_priority": int(row.get("source_priority") or 100),
    }


def _unresolved(
    *,
    ticker: str,
    metric_name: str,
    anchor: Mapping[str, Any],
    reason: str,
    provenance: Mapping[str, Any],
) -> AnnualMetricResolution:
    return AnnualMetricResolution(
        ticker=ticker,
        metric_name=metric_name,
        availability_status="NOT_DISCLOSED",
        metric_value=None,
        unit="ratio",
        accession_number=str(anchor.get("accession_number") or ""),
        filing_date=str(anchor.get("filing_date") or "")[:10],
        period_start=str(anchor.get("period_start") or "")[:10],
        period_end=str(anchor.get("period_end") or "")[:10],
        taxonomy=str(anchor.get("taxonomy") or ""),
        concept_name=f"reviewed_aligned_annual_{metric_name}",
        confidence=0.0,
        status_reason=reason,
        provenance_json=json.dumps(
            dict(provenance),
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _calculate(
    metric_name: str,
    operands: Mapping[str, Mapping[str, Any]],
) -> tuple[str, float | None, str]:
    values = {
        metric: _finite(row.get("value"))
        for metric, row in operands.items()
    }
    capex = values.get("capex")
    operating_cash_flow = values.get("operating_cash_flow")
    revenue = values.get("revenue")
    if metric_name == "capex_to_revenue":
        if capex is None or revenue is None or revenue <= 0:
            return "NOT_DISCLOSED", None, "reviewed_annual_formula_operand_invalid"
        return (
            "DERIVED",
            abs(capex) / revenue,
            "reviewed_same_period_annual_abs_capex_over_revenue",
        )
    if metric_name == "fcf_margin":
        if (
            capex is None
            or operating_cash_flow is None
            or revenue is None
            or revenue <= 0
        ):
            return "NOT_DISCLOSED", None, "reviewed_annual_formula_operand_invalid"
        return (
            "DERIVED",
            (operating_cash_flow - abs(capex)) / revenue,
            "reviewed_same_period_annual_fcf_over_revenue",
        )
    if metric_name == "operating_margin":
        operating_income = values.get("operating_income")
        if operating_income is None or revenue is None or revenue <= 0:
            return "NOT_DISCLOSED", None, "reviewed_annual_formula_operand_invalid"
        return (
            "DERIVED",
            operating_income / revenue,
            "reviewed_same_period_annual_operating_income_over_revenue",
        )
    if capex is None or operating_cash_flow is None:
        return "NOT_DISCLOSED", None, "reviewed_annual_formula_operand_invalid"
    cash_burn = max(-(operating_cash_flow - abs(capex)), 0.0)
    if metric_name == "cash_runway_years":
        if cash_burn <= 0:
            return (
                "NOT_APPLICABLE",
                None,
                "reviewed_same_period_annual_nonpositive_cash_burn",
            )
        cash = values.get("cash_and_equivalents")
        if cash is None:
            return "NOT_DISCLOSED", None, "reviewed_annual_cash_balance_missing"
        return (
            "DERIVED",
            cash / cash_burn,
            "reviewed_same_period_annual_cash_over_cash_burn",
        )
    if metric_name == "capital_raise_dependence":
        if cash_burn <= 0:
            return (
                "DERIVED",
                0.0,
                "reviewed_same_period_annual_nonpositive_cash_burn",
            )
        proceeds = [
            value
            for key in ("equity_issuance_proceeds", "debt_issuance_proceeds")
            if (value := values.get(key)) is not None
        ]
        if not proceeds:
            return "NOT_DISCLOSED", None, "reviewed_annual_issuance_proceeds_missing"
        return (
            "DERIVED",
            sum(proceeds) / cash_burn,
            "reviewed_same_period_annual_capital_raised_over_cash_burn",
        )
    raise ValueError(f"Unsupported reviewed annual metric={metric_name}")


def load_reviewed_annual_resolutions(
    connection: sqlite3.Connection,
    *,
    asof: str,
    scope_pairs: frozenset[tuple[str, str]],
    reviewed_source_id: str = SOURCE_ID,
    canonical_source_id: str = CANONICAL_SOURCE_ID,
) -> dict[tuple[str, str], AnnualMetricResolution]:
    anchors = [
        dict(row)
        for row in connection.execute(
            """
            SELECT ticker, canonical_metric, period_start, period_end, unit,
                   value, accession_number, filing_date, accepted_at, taxonomy,
                   concept_name, source_priority
            FROM fact_sec_xbrl_fact
            WHERE source_id=?
              AND period_start<>''
              AND period_end<=?
              AND SUBSTR(COALESCE(NULLIF(accepted_at, ''), filing_date), 1, 10)
                  <=?
            ORDER BY ticker, canonical_metric, period_end DESC,
                     filing_date DESC, accession_number DESC
            """,
            (reviewed_source_id, asof, asof),
        ).fetchall()
        if _annual(row)
    ]
    anchors_by_metric: dict[tuple[str, str], dict[str, Any]] = {}
    for anchor in anchors:
        key = (str(anchor["ticker"]).upper(), str(anchor["canonical_metric"]))
        if key not in anchors_by_metric:
            anchors_by_metric[key] = anchor

    resolutions: dict[tuple[str, str], AnnualMetricResolution] = {}
    for ticker, metric_name in sorted(scope_pairs):
        if metric_name not in SUPPORTED_METRICS:
            continue
        anchor_metric = ANCHOR_METRIC[metric_name]
        anchor = anchors_by_metric.get((ticker, anchor_metric))
        if anchor is None:
            continue
        dependencies = set(DURATION_DEPENDENCIES[metric_name])
        if metric_name == "cash_runway_years":
            dependencies.add("cash_and_equivalents")
        elif metric_name == "capital_raise_dependence":
            dependencies.update(
                {"equity_issuance_proceeds", "debt_issuance_proceeds"}
            )
        placeholders = ",".join("?" for _ in dependencies)
        rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT ticker, canonical_metric, period_start, period_end, unit,
                       value, accession_number, filing_date, accepted_at,
                       taxonomy, concept_name, source_priority
                FROM fact_financial_statement_canonical
                WHERE ticker=?
                  AND model_family='transportation'
                  AND source_id=?
                  AND canonical_metric IN ({placeholders})
                  AND period_end=?
                  AND LOWER(unit)=LOWER(?)
                  AND SUBSTR(
                        COALESCE(NULLIF(accepted_at, ''), filing_date), 1, 10
                      )<=?
                ORDER BY canonical_metric, source_priority ASC,
                         filing_date DESC, accepted_at DESC,
                         accession_number DESC, concept_name ASC
                """,
                (
                    ticker,
                    canonical_source_id,
                    *sorted(dependencies),
                    anchor["period_end"],
                    anchor["unit"],
                    asof,
                ),
            ).fetchall()
        ]
        duration_rows = [
            row
            for row in rows
            if row["period_start"] == anchor["period_start"] and _annual(row)
        ]
        operands = {
            dependency: _best_row(
                row
                for row in duration_rows
                if row["canonical_metric"] == dependency
            )
            for dependency in DURATION_DEPENDENCIES[metric_name]
        }
        anchor_canonical = operands.get(anchor_metric)
        anchor_matches = (
            anchor_canonical is not None
            and _finite(anchor_canonical.get("value")) is not None
            and math.isclose(
                float(anchor_canonical["value"]),
                float(anchor["value"]),
                rel_tol=1e-10,
                abs_tol=max(1e-6, abs(float(anchor["value"])) * 1e-10),
            )
        )
        provenance: dict[str, Any] = {
            "reviewed_source_id": reviewed_source_id,
            "canonical_source_id": canonical_source_id,
            "reviewed_anchor": _fact_provenance(
                {**anchor, "canonical_metric": anchor_metric}
            ),
            "required_duration_dependencies": list(
                DURATION_DEPENDENCIES[metric_name]
            ),
        }
        if not anchor_matches:
            resolutions[(ticker, metric_name)] = _unresolved(
                ticker=ticker,
                metric_name=metric_name,
                anchor=anchor,
                reason="reviewed_annual_anchor_not_materialized_in_canonical",
                provenance=provenance,
            )
            continue
        missing = [
            dependency
            for dependency, row in operands.items()
            if row is None
        ]
        if missing:
            provenance["missing_dependencies"] = missing
            resolutions[(ticker, metric_name)] = _unresolved(
                ticker=ticker,
                metric_name=metric_name,
                anchor=anchor,
                reason=(
                    "reviewed_annual_window_missing_aligned_dependencies:"
                    + ",".join(missing)
                ),
                provenance=provenance,
            )
            continue
        if metric_name == "cash_runway_years":
            operands["cash_and_equivalents"] = _best_row(
                row
                for row in rows
                if row["canonical_metric"] == "cash_and_equivalents"
            )
        elif metric_name == "capital_raise_dependence":
            for dependency in (
                "equity_issuance_proceeds",
                "debt_issuance_proceeds",
            ):
                operands[dependency] = _best_row(
                    row
                    for row in duration_rows
                    if row["canonical_metric"] == dependency
                )
        concrete_operands = {
            metric: row
            for metric, row in operands.items()
            if row is not None
        }
        status, metric_value, reason = _calculate(
            metric_name,
            concrete_operands,
        )
        provenance["operands"] = {
            metric: _fact_provenance(row)
            for metric, row in concrete_operands.items()
        }
        resolution = AnnualMetricResolution(
            ticker=ticker,
            metric_name=metric_name,
            availability_status=status,
            metric_value=metric_value,
            unit="years" if metric_name == "cash_runway_years" else "ratio",
            accession_number=str(anchor["accession_number"]),
            filing_date=str(anchor["filing_date"])[:10],
            period_start=str(anchor["period_start"])[:10],
            period_end=str(anchor["period_end"])[:10],
            taxonomy=str(anchor["taxonomy"]),
            concept_name=f"reviewed_aligned_annual_{metric_name}",
            confidence=0.95 if status in {"DERIVED", "NOT_APPLICABLE"} else 0.0,
            status_reason=reason,
            provenance_json=json.dumps(
                provenance,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        resolutions[(ticker, metric_name)] = resolution
    return resolutions
