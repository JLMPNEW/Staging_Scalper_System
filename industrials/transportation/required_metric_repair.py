from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence


REPAIR_SCOPE_VERSION = "transportation_required_metric_repair_v1"
STALE_FACT_MAX_LAG_DAYS = 400
FINANCIAL_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "6-K",
        "6-K/A",
        "8-K",
        "8-K/A",
    }
)
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})
INTERIM_FORMS = frozenset({"10-Q", "10-Q/A", "6-K", "6-K/A", "8-K", "8-K/A"})
QUARTER_ENDS = frozenset({"03-31", "06-30", "09-30", "12-31"})
OUTPUT_FEATURES = {
    "operating_margin": "operating_margin",
    "fcf_margin": "fcf_margin",
    "capex_to_revenue": "capex_to_revenue",
    "cash_runway_years": "cash_runway_years",
    "capital_raise_dependence": "capital_raise_dependence",
}
PAIR_FIELDS = (
    "scope_version",
    "pair_key",
    "ticker",
    "metric_name",
    "source_type",
    "availability_status",
    "availability_reason",
    "current_metric_value",
    "required_dependencies",
    "missing_dependencies",
    "stale_dependencies",
    "latest_dependency_periods_json",
    "repair_classification",
    "repair_objective",
    "include_in_filing_pass",
    "required_action",
    "notes",
)
DEPENDENCY_FIELDS = (
    "scope_version",
    "pair_key",
    "ticker",
    "metric_name",
    "dependency_id",
    "fact_count",
    "distinct_period_count",
    "latest_period_end",
    "latest_filing_date",
    "latest_accession_number",
    "latest_form_type",
    "latest_taxonomy",
    "latest_concept_name",
    "freshness_status",
    "requirement_status",
    "required_action",
)
ACCESSION_FIELDS = (
    "scope_version",
    "ticker",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "report_date",
    "primary_document",
    "source_id",
    "selection_reason",
    "requested_metric_names",
    "requested_dependency_ids",
)


def read_scope(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]
    if not rows:
        raise ValueError(f"{path}: required-metric repair scope is empty")
    required = {
        "scope_version",
        "ticker",
        "metric_name",
        "source_type",
        "required_dependencies",
        "repair_objective",
        "include_in_filing_pass",
        "notes",
    }
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"{path}: missing columns={sorted(missing)}")
    pairs: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        if row["scope_version"] != REPAIR_SCOPE_VERSION:
            raise ValueError(
                f"{path}:{line_number}: unexpected scope_version="
                f"{row['scope_version']!r}"
            )
        row["ticker"] = row["ticker"].upper()
        pair_key = f"{row['ticker']}|{row['metric_name']}"
        if pair_key in pairs:
            raise ValueError(f"{path}:{line_number}: duplicate pair={pair_key}")
        pairs.add(pair_key)
        if row["source_type"] not in {"financial", "market"}:
            raise ValueError(
                f"{path}:{line_number}: invalid source_type={row['source_type']}"
            )
        if row["include_in_filing_pass"] not in {"0", "1"}:
            raise ValueError(
                f"{path}:{line_number}: include_in_filing_pass must be 0 or 1"
            )
    tickers = {row["ticker"] for row in rows}
    financial_tickers = {
        row["ticker"] for row in rows if row["source_type"] == "financial"
    }
    if len(rows) != 32 or len(tickers) != 19 or len(financial_tickers) != 18:
        raise ValueError(
            f"{path}: expected 32 pairs/19 tickers/18 financial tickers; "
            f"observed={len(rows)}/{len(tickers)}/{len(financial_tickers)}"
        )
    return rows


def _pipe(value: object) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in str(value or "").split("|") if item.strip()
    )


def _number(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _availability(
    connection: Any,
    *,
    ticker: str,
    metric_name: str,
    asof_date: str,
) -> Mapping[str, object]:
    row = connection.execute(
        """
        SELECT availability_status, metric_value, status_reason
        FROM feature_financial_metric_availability
        WHERE model_family='transportation' AND ticker=?
          AND metric_name=? AND asof_date<=?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (ticker, metric_name, asof_date),
    ).fetchone()
    return dict(row) if row is not None else {}


def _latest_feature(
    connection: Any,
    *,
    ticker: str,
    asof_date: str,
) -> Mapping[str, object]:
    row = connection.execute(
        """
        SELECT *
        FROM feature_financial_statement
        WHERE model_family='transportation' AND ticker=? AND asof_date<=?
        ORDER BY asof_date DESC, fiscal_period_end DESC, source_id
        LIMIT 1
        """,
        (ticker, asof_date),
    ).fetchone()
    return dict(row) if row is not None else {}


def _dependency_rows(
    connection: Any,
    *,
    ticker: str,
    dependencies: Sequence[str],
    asof_date: str,
) -> dict[str, list[Mapping[str, object]]]:
    if not dependencies:
        return {}
    placeholders = ",".join("?" for _ in dependencies)
    output: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    rows = connection.execute(
        f"""
        SELECT canonical_metric, period_end, filing_date, accession_number,
               form_type, taxonomy, concept_name, source_id
        FROM fact_financial_statement_canonical
        WHERE model_family='transportation' AND ticker=?
          AND filing_date<=?
          AND canonical_metric IN ({placeholders})
        ORDER BY canonical_metric, period_end DESC, filing_date DESC,
                 source_priority ASC, source_id
        """,
        (ticker, asof_date, *dependencies),
    ).fetchall()
    for row in rows:
        output[str(row["canonical_metric"])].append(dict(row))
    return dict(output)


def build_repair_contract(
    connection: Any,
    *,
    scope_rows: Sequence[Mapping[str, str]],
    asof_date: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    cutoff = (
        date.fromisoformat(asof_date) - timedelta(days=STALE_FACT_MAX_LAG_DAYS)
    ).isoformat()
    pair_rows: list[dict[str, object]] = []
    dependency_rows: list[dict[str, object]] = []
    for scope in sorted(
        scope_rows, key=lambda row: (row["ticker"], row["metric_name"])
    ):
        ticker = scope["ticker"]
        metric_name = scope["metric_name"]
        pair_key = f"{ticker}|{metric_name}"
        dependencies = _pipe(scope["required_dependencies"])
        availability = _availability(
            connection,
            ticker=ticker,
            metric_name=metric_name,
            asof_date=asof_date,
        )
        if scope["source_type"] == "market":
            price = connection.execute(
                """
                SELECT COUNT(*) AS observation_count, MIN(bar_date) AS first_date,
                       MAX(bar_date) AS last_date
                FROM fact_price_ohlcv
                WHERE ticker=? AND bar_date<=?
                  AND adj_close IS NOT NULL AND adj_close>0
                """,
                (ticker, asof_date),
            ).fetchone()
            observation_count = int(price["observation_count"] or 0)
            current_value = _number(availability.get("metric_value"))
            classification = (
                "ALREADY_RESOLVED"
                if current_value is not None
                else "INSUFFICIENT_MARKET_HISTORY"
            )
            pair_rows.append(
                {
                    "scope_version": REPAIR_SCOPE_VERSION,
                    "pair_key": pair_key,
                    "ticker": ticker,
                    "metric_name": metric_name,
                    "source_type": "market",
                    "availability_status": availability.get(
                        "availability_status", ""
                    ),
                    "availability_reason": availability.get("status_reason", ""),
                    "current_metric_value": (
                        "" if current_value is None else current_value
                    ),
                    "required_dependencies": scope["required_dependencies"],
                    "missing_dependencies": (
                        "" if observation_count >= 252 else "adjusted_price_history"
                    ),
                    "stale_dependencies": "",
                    "latest_dependency_periods_json": json.dumps(
                        {
                            "adjusted_price_history": {
                                "observation_count": observation_count,
                                "first_date": str(price["first_date"] or ""),
                                "last_date": str(price["last_date"] or ""),
                                "minimum_required": 252,
                            }
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "repair_classification": classification,
                    "repair_objective": scope["repair_objective"],
                    "include_in_filing_pass": 0,
                    "required_action": (
                        "NONE"
                        if classification == "ALREADY_RESOLVED"
                        else "WAIT_FOR_THREE_ADDITIONAL_VALID_ADJUSTED_BARS"
                    ),
                    "notes": scope["notes"],
                }
            )
            continue

        feature = _latest_feature(
            connection, ticker=ticker, asof_date=asof_date
        )
        facts_by_dependency = _dependency_rows(
            connection,
            ticker=ticker,
            dependencies=dependencies,
            asof_date=asof_date,
        )
        missing_dependencies: list[str] = []
        stale_dependencies: list[str] = []
        latest_periods: dict[str, str] = {}
        for dependency in dependencies:
            facts = facts_by_dependency.get(dependency, [])
            periods = {
                str(row.get("period_end") or "")[:10]
                for row in facts
                if str(row.get("period_end") or "")[:10]
            }
            latest = facts[0] if facts else {}
            latest_period = str(latest.get("period_end") or "")[:10]
            latest_periods[dependency] = latest_period
            if not facts:
                freshness = "missing"
                requirement_status = "MISSING_REQUIRED_SOURCE"
                required_action = "RETRIEVE_IN_BOUNDED_FILING_PASS"
                missing_dependencies.append(dependency)
            elif not latest_period or latest_period < cutoff:
                freshness = "stale"
                requirement_status = "STALE_REQUIRED_SOURCE"
                required_action = "RECOVER_CURRENT_PERIOD_IN_BOUNDED_FILING_PASS"
                stale_dependencies.append(dependency)
            else:
                freshness = "current"
                requirement_status = "PRESENT_REQUIRES_ALIGNMENT_CHECK"
                required_action = "REUSE_AND_VALIDATE_PERIOD_ALIGNMENT"
            dependency_rows.append(
                {
                    "scope_version": REPAIR_SCOPE_VERSION,
                    "pair_key": pair_key,
                    "ticker": ticker,
                    "metric_name": metric_name,
                    "dependency_id": dependency,
                    "fact_count": len(facts),
                    "distinct_period_count": len(periods),
                    "latest_period_end": latest_period,
                    "latest_filing_date": latest.get("filing_date", ""),
                    "latest_accession_number": latest.get(
                        "accession_number", ""
                    ),
                    "latest_form_type": latest.get("form_type", ""),
                    "latest_taxonomy": latest.get("taxonomy", ""),
                    "latest_concept_name": latest.get("concept_name", ""),
                    "freshness_status": freshness,
                    "requirement_status": requirement_status,
                    "required_action": required_action,
                }
            )
        current_value = _number(availability.get("metric_value"))
        if current_value is None:
            current_value = _number(
                feature.get(OUTPUT_FEATURES.get(metric_name, metric_name))
            )
        if current_value is not None:
            classification = "ALREADY_RESOLVED"
            required_action = "REUSE_EXISTING_VALUE_NO_RETRIEVAL"
        elif missing_dependencies or stale_dependencies:
            classification = "SOURCE_OR_PERIOD_GAP"
            required_action = "RETRIEVE_AND_PARSE_ONCE"
        else:
            classification = "TTM_ALIGNMENT_OR_FORMULA_GAP"
            required_action = "REPAIR_FROM_EXISTING_FACTS_BEFORE_RETRIEVAL"
        pair_rows.append(
            {
                "scope_version": REPAIR_SCOPE_VERSION,
                "pair_key": pair_key,
                "ticker": ticker,
                "metric_name": metric_name,
                "source_type": "financial",
                "availability_status": availability.get(
                    "availability_status", ""
                ),
                "availability_reason": availability.get("status_reason", ""),
                "current_metric_value": (
                    "" if current_value is None else current_value
                ),
                "required_dependencies": scope["required_dependencies"],
                "missing_dependencies": "|".join(sorted(missing_dependencies)),
                "stale_dependencies": "|".join(sorted(stale_dependencies)),
                "latest_dependency_periods_json": json.dumps(
                    latest_periods,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "repair_classification": classification,
                "repair_objective": scope["repair_objective"],
                "include_in_filing_pass": int(
                    scope["include_in_filing_pass"]
                ),
                "required_action": required_action,
                "notes": scope["notes"],
            }
        )
    return pair_rows, dependency_rows


def _is_quarter_end(value: object) -> bool:
    text = str(value or "")[:10]
    return len(text) == 10 and text[5:] in QUARTER_ENDS


def _selection_reason(row: Mapping[str, object]) -> str:
    form = str(row.get("form_type") or "").upper()
    if form in ANNUAL_FORMS:
        return "AUDITED_ANNUAL_STATEMENT"
    if form in {"10-Q", "10-Q/A"}:
        return "DOMESTIC_INTERIM_STATEMENT"
    if form in {"6-K", "6-K/A"} and _is_quarter_end(row.get("report_date")):
        return "FPI_QUARTER_END_FINANCIAL_STATEMENT"
    primary = str(row.get("primary_document") or "").lower()
    if form in {"6-K", "6-K/A", "8-K", "8-K/A"} and any(
        token in primary for token in ("earn", "result", "financial")
    ):
        return "EARNINGS_OR_FINANCIAL_RESULTS"
    return ""


def build_accession_manifest(
    connection: Any,
    *,
    pair_rows: Sequence[Mapping[str, object]],
    asof_date: str,
    annual_limit: int = 3,
    interim_limit: int = 8,
) -> list[dict[str, object]]:
    requests: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"metrics": set(), "dependencies": set()}
    )
    for row in pair_rows:
        if (
            str(row["source_type"]) != "financial"
            or int(str(row["include_in_filing_pass"])) != 1
            or str(row["repair_classification"]) == "ALREADY_RESOLVED"
        ):
            continue
        ticker = str(row["ticker"]).upper()
        requests[ticker]["metrics"].add(str(row["metric_name"]))
        requests[ticker]["dependencies"].update(
            _pipe(row["required_dependencies"])
        )
    output: list[dict[str, object]] = []
    for ticker in sorted(requests):
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT ticker, cik, source_id, accession_number, form_type,
                       filing_date, accepted_at, report_date, primary_document
                FROM fact_sec_filing
                WHERE ticker=? AND filing_date<=?
                ORDER BY filing_date DESC, accession_number DESC
                """,
                (ticker, asof_date),
            ).fetchall()
            if str(row["form_type"] or "").upper() in FINANCIAL_FORMS
            and (
                not str(row["report_date"] or "")[:10]
                or str(row["report_date"] or "")[:10] <= asof_date
            )
        ]
        annual = [
            row for row in rows if str(row["form_type"]).upper() in ANNUAL_FORMS
        ][:annual_limit]
        interim_candidates = [
            row
            for row in rows
            if str(row["form_type"]).upper() in INTERIM_FORMS
            and _selection_reason(row)
        ]
        interim = interim_candidates[:interim_limit]
        selected: dict[str, Mapping[str, object]] = {}
        for row in (*annual, *interim):
            selected.setdefault(str(row["accession_number"]), row)
        for accession, row in sorted(
            selected.items(),
            key=lambda item: (
                str(item[1].get("filing_date") or ""),
                item[0],
            ),
            reverse=True,
        ):
            output.append(
                {
                    "scope_version": REPAIR_SCOPE_VERSION,
                    "ticker": ticker,
                    "accession_number": accession,
                    "form_type": row.get("form_type", ""),
                    "filing_date": row.get("filing_date", ""),
                    "accepted_at": row.get("accepted_at", ""),
                    "report_date": row.get("report_date", ""),
                    "primary_document": row.get("primary_document", ""),
                    "source_id": row.get("source_id", ""),
                    "selection_reason": _selection_reason(row),
                    "requested_metric_names": "|".join(
                        sorted(requests[ticker]["metrics"])
                    ),
                    "requested_dependency_ids": "|".join(
                        sorted(requests[ticker]["dependencies"])
                    ),
                }
            )
    return output


def summarize_contract(
    *,
    pair_rows: Sequence[Mapping[str, object]],
    dependency_rows: Sequence[Mapping[str, object]],
    accession_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "pair_count": len(pair_rows),
        "ticker_count": len({str(row["ticker"]) for row in pair_rows}),
        "financial_ticker_count": len(
            {
                str(row["ticker"])
                for row in pair_rows
                if str(row["source_type"]) == "financial"
            }
        ),
        "market_ticker_count": len(
            {
                str(row["ticker"])
                for row in pair_rows
                if str(row["source_type"]) == "market"
            }
        ),
        "pair_classification_counts": dict(
            sorted(
                Counter(
                    str(row["repair_classification"]) for row in pair_rows
                ).items()
            )
        ),
        "dependency_count": len(dependency_rows),
        "dependency_requirement_counts": dict(
            sorted(
                Counter(
                    str(row["requirement_status"])
                    for row in dependency_rows
                ).items()
            )
        ),
        "accession_count": len(accession_rows),
        "accession_ticker_count": len(
            {str(row["ticker"]) for row in accession_rows}
        ),
        "accession_selection_counts": dict(
            sorted(
                Counter(
                    str(row["selection_reason"]) for row in accession_rows
                ).items()
            )
        ),
    }
