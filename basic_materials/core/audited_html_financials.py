"""Deterministic audited-HTML statement extraction for governed SEC fallbacks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from io import BytesIO
import json
import math
import re
from typing import Any, Iterable, Sequence

import pandas as pd


class AuditedHtmlFinancialError(RuntimeError):
    """Raised when an audited HTML statement cannot be extracted or tied out."""


@dataclass(frozen=True)
class AuditedHtmlObservation:
    canonical_metric: str
    taxonomy: str
    concept: str
    period_type: str
    period_start: str
    period_end: str
    unit: str
    numeric_value: float
    source_detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_PARSER_VERSION = "basic_materials_ogc_audited_html_v1"
_YEARS = (2024, 2025)


def _normal(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = str(value)
    text = (
        text.replace("\u00a0", " ")
        .replace("\ufffd", "-")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
    )
    return re.sub(r"\s+", " ", text).strip().casefold()


def _number(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    text = str(value).strip()
    if not text:
        return None
    if text in {"-", "\u2014", "\u2013", "\ufffd"}:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    normalized = (
        text.strip("()")
        .replace(",", "")
        .replace("$", "")
        .replace("US", "")
        .replace(" ", "")
        .replace("\u2212", "-")
    )
    try:
        numeric = float(normalized)
    except ValueError:
        return None
    if negative:
        numeric = -abs(numeric)
    return numeric if math.isfinite(numeric) else None


def _labels(table: pd.DataFrame) -> list[str]:
    return [_normal(value) for value in table.iloc[:, 0].tolist()]


def _find_table(
    tables: Sequence[pd.DataFrame], required_labels: Iterable[str]
) -> pd.DataFrame:
    required = {_normal(value) for value in required_labels}
    candidates = []
    for table in tables:
        labels = set(_labels(table))
        if required <= labels:
            candidates.append(table)
    if len(candidates) != 1:
        raise AuditedHtmlFinancialError(
            f"Expected one audited statement table for {sorted(required)}, "
            f"found {len(candidates)}"
        )
    return candidates[0]


def _year_columns(table: pd.DataFrame, year: int) -> list[int]:
    wanted = str(year)
    columns = []
    for column in range(table.shape[1]):
        values = {
            _normal(table.iloc[row, column])
            for row in range(min(4, table.shape[0]))
        }
        if wanted in values or f"{wanted}.0" in values:
            columns.append(column)
    if not columns:
        raise AuditedHtmlFinancialError(
            f"No {year} value columns found in audited statement"
        )
    return columns


def _row_value(
    table: pd.DataFrame,
    *,
    label: str,
    year: int,
    occurrence: int = 0,
) -> float:
    wanted = _normal(label)
    matches = [index for index, value in enumerate(_labels(table)) if value == wanted]
    if occurrence >= len(matches):
        raise AuditedHtmlFinancialError(
            f"Missing occurrence {occurrence} of {label!r} in audited statement"
        )
    row_index = matches[occurrence]
    values = [
        _number(table.iloc[row_index, column])
        for column in _year_columns(table, year)
    ]
    numeric = [value for value in values if value is not None]
    if not numeric:
        raise AuditedHtmlFinancialError(f"No {year} numeric value for {label!r}")
    first = numeric[0]
    if any(
        not math.isclose(first, value, rel_tol=0, abs_tol=1e-9)
        for value in numeric[1:]
    ):
        raise AuditedHtmlFinancialError(
            f"Conflicting repeated {year} values for {label!r}: {numeric}"
        )
    return first


def _detail(metric: str, method: str, labels: Iterable[str]) -> str:
    return json.dumps(
        {
            "parser_version": _PARSER_VERSION,
            "canonical_metric": metric,
            "extraction_method": method,
            "source_rows": list(labels),
            "source_scale": "millions",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_ogc_audited_html(payload: bytes) -> list[AuditedHtmlObservation]:
    """Extract and tie out OGC's 2025/2024 SEC-hosted audited IFRS statements."""

    try:
        tables = pd.read_html(BytesIO(payload))
    except (ValueError, ImportError) as exc:
        raise AuditedHtmlFinancialError(
            "OGC audited HTML contains no parseable tables"
        ) from exc
    balance = _find_table(
        tables,
        (
            "Cash and cash equivalents",
            "TOTAL ASSETS",
            "TOTAL LIABILITIES",
            "TOTAL SHAREHOLDERS' EQUITY",
        ),
    )
    income = _find_table(
        tables,
        ("Revenue", "Operating profit", "Profit before income tax", "Net profit"),
    )
    cash_flow = _find_table(
        tables,
        (
            "Net cash provided by operating activities",
            "Payment for property, plant and equipment",
            "Payment for mining assets",
            "Cash and cash equivalents at the end of the year",
        ),
    )
    shares = _find_table(
        tables,
        (
            "Basic weighted average number of shares (in millions)",
            "Diluted weighted average number of shares (in millions)",
        ),
    )
    capital = _find_table(tables, ("Total debt", "Total equity", "Net (cash) / debt"))
    metric_specs = {
        "revenue": ("Revenue", "ifrs-full", "Revenue", "duration", income),
        "cost_of_revenue": (
            "Cost of sales, excluding depreciation and amortization",
            "ifrs-full",
            "CostOfSales",
            "duration",
            income,
        ),
        "operating_income": (
            "Operating profit",
            "ifrs-full",
            "ProfitLossFromOperatingActivities",
            "duration",
            income,
        ),
        "depreciation_depletion_amortization": (
            "Depreciation and amortization",
            "ifrs-full",
            "DepreciationAndAmortisationExpense",
            "duration",
            income,
        ),
        "interest_expense": (
            "Interest expense and finance costs",
            "ifrs-full",
            "FinanceCosts",
            "duration",
            income,
        ),
        "income_tax_expense": (
            "Income tax expense",
            "ifrs-full",
            "IncomeTaxExpense",
            "duration",
            income,
        ),
        "pretax_income": (
            "Profit before income tax",
            "ifrs-full",
            "ProfitLossBeforeTax",
            "duration",
            income,
        ),
        "net_income": ("Net profit", "ifrs-full", "ProfitLoss", "duration", income),
        "operating_cash_flow": (
            "Net cash provided by operating activities",
            "ifrs-full",
            "CashFlowsFromUsedInOperatingActivities",
            "duration",
            cash_flow,
        ),
        "cash_and_equivalents": (
            "Cash and cash equivalents",
            "ifrs-full",
            "CashAndCashEquivalents",
            "instant",
            balance,
        ),
        "assets": ("TOTAL ASSETS", "ifrs-full", "Assets", "instant", balance),
        "accounts_payable": (
            "Trade and other payables",
            "ifrs-full",
            "TradeAndOtherCurrentPayables",
            "instant",
            balance,
        ),
        "diluted_shares": (
            "Diluted weighted average number of shares (in millions)",
            "ifrs-full",
            "WeightedAverageNumberOfSharesOutstandingDiluted",
            "duration",
            shares,
        ),
        "dividends_paid": (
            "Dividends paid to equity holders of the Company",
            "ifrs-full",
            "DividendsPaid",
            "duration",
            cash_flow,
        ),
        "share_repurchases": (
            "Share buybacks",
            "ifrs-full",
            "PaymentsForRepurchaseOfEquity",
            "duration",
            cash_flow,
        ),
    }
    observations: list[AuditedHtmlObservation] = []
    scale = 1_000_000.0
    for year in _YEARS:
        values: dict[str, float] = {}
        details: dict[str, tuple[str, tuple[str, ...]]] = {}
        for metric, (label, taxonomy, concept, period_type, table) in metric_specs.items():
            values[metric] = _row_value(table, label=label, year=year)
            details[metric] = ("single_row", (label,))
        values["accounts_receivable"] = sum(
            _row_value(
                balance,
                label="Trade and other receivables",
                year=year,
                occurrence=occurrence,
            )
            for occurrence in (0, 1)
        )
        details["accounts_receivable"] = (
            "sum_rows",
            ("Current trade and other receivables", "Non-current trade and other receivables"),
        )
        values["inventory"] = sum(
            _row_value(
                balance,
                label="Inventories",
                year=year,
                occurrence=occurrence,
            )
            for occurrence in (0, 1)
        )
        details["inventory"] = (
            "sum_rows",
            ("Current inventories", "Non-current inventories"),
        )
        values["capital_expenditures"] = sum(
            _row_value(cash_flow, label=label, year=year)
            for label in (
                "Payment for property, plant and equipment",
                "Payment for mining assets",
            )
        )
        details["capital_expenditures"] = (
            "sum_rows",
            ("Payment for property, plant and equipment", "Payment for mining assets"),
        )
        current_debt = _row_value(balance, label="Debt", year=year)
        total_debt = _row_value(capital, label="Total debt", year=year)
        noncurrent_debt = total_debt - current_debt
        if noncurrent_debt < -1e-9:
            raise AuditedHtmlFinancialError(
                f"OGC {year} current debt exceeds source-reported total debt"
            )
        values["debt_current"] = current_debt
        values["debt_noncurrent"] = max(noncurrent_debt, 0.0)
        details["debt_current"] = ("single_row", ("Current liabilities: Debt",))
        details["debt_noncurrent"] = (
            "difference_rows",
            ("Total debt", "Current liabilities: Debt"),
        )
        values["equity"] = _row_value(capital, label="Total equity", year=year)
        details["equity"] = ("single_row", ("Total equity",))
        total_liabilities = _row_value(balance, label="TOTAL LIABILITIES", year=year)
        if not math.isclose(
            values["assets"],
            total_liabilities + values["equity"],
            rel_tol=0,
            abs_tol=0.11,
        ):
            raise AuditedHtmlFinancialError(f"OGC {year} balance sheet does not tie")
        ending_cash = _row_value(
            cash_flow,
            label="Cash and cash equivalents at the end of the year",
            year=year,
        )
        if not math.isclose(
            ending_cash, values["cash_and_equivalents"], rel_tol=0, abs_tol=0.01
        ):
            raise AuditedHtmlFinancialError(f"OGC {year} ending cash does not tie")
        cash_flow_profit = _row_value(cash_flow, label="Net profit", year=year)
        if not math.isclose(
            cash_flow_profit, values["net_income"], rel_tol=0, abs_tol=0.01
        ):
            raise AuditedHtmlFinancialError(f"OGC {year} net profit does not tie")
        derived_specs = {
            "accounts_receivable": (
                "ifrs-full",
                "TradeAndOtherCurrentReceivables",
                "instant",
            ),
            "inventory": ("ifrs-full", "Inventories", "instant"),
            "capital_expenditures": (
                "ifrs-full",
                "PurchaseOfPropertyPlantAndEquipment",
                "duration",
            ),
            "debt_current": ("ifrs-full", "CurrentBorrowings", "instant"),
            "debt_noncurrent": ("ifrs-full", "NoncurrentBorrowings", "instant"),
            "equity": ("ifrs-full", "Equity", "instant"),
        }
        all_specs = {
            metric: (taxonomy, concept, period_type)
            for metric, (_, taxonomy, concept, period_type, _) in metric_specs.items()
        } | derived_specs
        if set(values) != set(all_specs) or len(values) != 21:
            raise AuditedHtmlFinancialError(
                "OGC audited extraction metric census changed"
            )
        for metric in sorted(values):
            taxonomy, concept, period_type = all_specs[metric]
            period_end = f"{year}-12-31"
            period_start = f"{year}-01-01" if period_type == "duration" else period_end
            method, labels = details[metric]
            observations.append(
                AuditedHtmlObservation(
                    canonical_metric=metric,
                    taxonomy=taxonomy,
                    concept=concept,
                    period_type=period_type,
                    period_start=period_start,
                    period_end=period_end,
                    unit="shares" if metric == "diluted_shares" else "USD",
                    numeric_value=values[metric] * scale,
                    source_detail=_detail(metric, method, labels),
                )
            )
    if len(observations) != 42:
        raise AuditedHtmlFinancialError(
            f"Expected 42 OGC audited observations, found {len(observations)}"
        )
    return observations
