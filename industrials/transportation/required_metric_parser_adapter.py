from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from dedicated_parser.contracts import (
    AdapterRegistry,
    MetricEvidence,
    MetricRequest,
    MetricRequirement,
    NormalizedFact,
    ProductionMetricMapping,
    WorkItem,
)
from industrials.core.db import XBRL_CONCEPT_MAP_SEED
from industrials.machinery.dedicated_parser_adapter import (
    map_normalized_facts as map_standard_financial_facts,
)


ADAPTER_VERSION = "transportation_required_metric_operands_v1"
TARGET_METRICS = frozenset(
    {
        "capex",
        "cash_and_equivalents",
        "costs_and_expenses",
        "debt_issuance_proceeds",
        "equity_issuance_proceeds",
        "operating_cash_flow",
        "operating_income",
        "pretax_income",
        "revenue",
    }
)
SUPPORTED_FORMS = (
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
)
_STANDARD_ROWS = tuple(
    row
    for row in XBRL_CONCEPT_MAP_SEED
    if str(row["taxonomy"]).lower() in {"ifrs-full", "us-gaap"}
    and str(row["canonical_metric"]) in TARGET_METRICS
    and str(row["sign_policy"]) != "expense_from_net"
)
_STANDARD_BY_CONCEPT = {
    (
        str(row["taxonomy"]).lower(),
        str(row["concept_name"]).lower(),
    ): row
    for row in _STANDARD_ROWS
}
_EXTENSION_PATTERNS: dict[str, tuple[str, ...]] = {
    "capex": (
        r"(?:payment|purchase|acquisition|addition|capital expenditure|"
        r"capital spending).*(?:property plant|equipment|productive asset|"
        r"vessel|ship|newbuild|railcar|aircraft|concession)",
        r"(?:vessel|ship|newbuild|railcar|aircraft|concession).*(?:payment|"
        r"purchase|acquisition|addition|capital expenditure|capital spending)",
    ),
    "equity_issuance_proceeds": (
        r"proceeds.*(?:issuance|offering).*(?:common stock|ordinary share|"
        r"equity|share capital|initial public offering)",
    ),
    "debt_issuance_proceeds": (
        r"proceeds.*(?:debt|borrow|loan|credit facilit|note issuance|"
        r"secured financ)",
    ),
    "operating_cash_flow": (
        r"(?:net cash|cash flows?).*(?:provided by|used in|from).*"
        r"operating activit",
    ),
    "operating_income": (
        r"(?:operating income|income from operations|profit from operating "
        r"activities|operating profit)",
    ),
    "costs_and_expenses": (
        r"(?:total )?(?:operating )?(?:costs and expenses|"
        r"expenses and costs|operating expenses)",
    ),
    "pretax_income": (
        r"(?:income|profit|loss).*(?:before income tax|before tax|pretax)",
    ),
    "cash_and_equivalents": (
        r"cash and cash equivalents",
    ),
    "revenue": (
        r"(?:total |operating )?revenue",
    ),
}
_REQUEST_PATTERNS: dict[str, tuple[str, ...]] = {}
for _metric in sorted(TARGET_METRICS):
    exact = sorted(
        {
            str(row["concept_name"])
            for row in _STANDARD_ROWS
            if str(row["canonical_metric"]) == _metric
        }
    )
    exact_pattern = (
        r"^(?:" + "|".join(re.escape(value) for value in exact) + r")\b"
        if exact
        else r"(?!x)x"
    )
    _REQUEST_PATTERNS[_metric] = (
        exact_pattern,
        *tuple(f"(?i){value}" for value in _EXTENSION_PATTERNS[_metric]),
    )


def _production_mappings() -> dict[str, ProductionMetricMapping]:
    output: dict[str, ProductionMetricMapping] = {}
    for metric_name in sorted(TARGET_METRICS):
        rows = [
            row
            for row in _STANDARD_ROWS
            if str(row["canonical_metric"]) == metric_name
        ]
        if not rows:
            continue
        best = min(rows, key=lambda row: int(str(row["priority"])))
        output[metric_name] = ProductionMetricMapping(
            metric_name,
            str(best["financial_statement"]),
            str(best["period_type"]),
            160,
            sign_policy=str(best["sign_policy"]),
        )
    return output


def get_registry() -> AdapterRegistry:
    return AdapterRegistry(
        model_family="transportation",
        adapter_version=ADAPTER_VERSION,
        supported_forms=SUPPORTED_FORMS,
        source_metrics=tuple(
            MetricRequest(metric, _REQUEST_PATTERNS[metric])
            for metric in sorted(TARGET_METRICS)
        ),
        metric_dependencies={},
        metric_requirements={
            metric: MetricRequirement(metric)
            for metric in sorted(TARGET_METRICS)
        },
        production_mappings=_production_mappings(),
        document_keywords=(
            "capital expenditures",
            "cash flows",
            "costs and expenses",
            "operating income",
            "operating profit",
            "proceeds",
            "revenues",
            "vessels",
        ),
    )


def select_tickers(
    connection: sqlite3.Connection,
    asof_date: str,
) -> list[str]:
    return [
        str(row["ticker"])
        for row in connection.execute(
            """
            SELECT DISTINCT ticker
            FROM dim_universe_membership
            WHERE model_family='transportation' AND start_date<=?
              AND COALESCE(end_date, '9999-12-31')>=?
            ORDER BY ticker
            """,
            (asof_date, asof_date),
        )
    ]


def extract_metric_evidence(
    item: WorkItem,
) -> tuple[MetricEvidence, ...]:
    # This bounded adapter is XBRL-only. Arelle reads the already-cached
    # primary filing once; prose/table evidence remains in the existing
    # transportation specialized adapter and is not duplicated here.
    return ()


def _metadata(fact: NormalizedFact) -> dict[str, Any]:
    try:
        payload = json.loads(fact.concept_metadata_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _taxonomy(fact: NormalizedFact) -> str:
    taxonomy = fact.taxonomy.lower()
    if taxonomy in {"ifrs-full", "us-gaap"}:
        return taxonomy
    namespace = str(_metadata(fact).get("namespace_uri") or "").lower()
    if "fasb.org/us-gaap" in namespace:
        return "us-gaap"
    if "xbrl.ifrs.org" in namespace:
        return "ifrs-full"
    return taxonomy


def _semantic_text(fact: NormalizedFact) -> str:
    metadata = _metadata(fact)
    return " ".join(
        (
            fact.concept_name,
            str(metadata.get("label") or ""),
            str(metadata.get("documentation") or ""),
        )
    )


def _candidate_metric(fact: NormalizedFact) -> str:
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", _semantic_text(fact))
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    for metric in (
        "capex",
        "equity_issuance_proceeds",
        "debt_issuance_proceeds",
        "operating_cash_flow",
        "operating_income",
        "costs_and_expenses",
        "pretax_income",
        "cash_and_equivalents",
        "revenue",
    ):
        if any(
            re.search(pattern, normalized)
            for pattern in _EXTENSION_PATTERNS[metric]
        ):
            return metric
    return ""


def _valid_candidate_period(
    fact: NormalizedFact,
    *,
    metric_name: str,
) -> bool:
    if not fact.period_end or fact.numeric_value is None:
        return False
    if metric_name != "cash_and_equivalents" and not fact.period_start:
        return False
    return fact.unit.strip().lower() not in {
        "",
        "%",
        "percent",
        "pure",
        "share",
        "shares",
    }


def map_normalized_facts(
    item: WorkItem,
    facts: tuple[NormalizedFact, ...],
) -> tuple[MetricEvidence, ...]:
    standard = [
        row
        for row in map_standard_financial_facts(item, facts)
        if row.metric_name in TARGET_METRICS
    ]
    emitted = {
        (
            row.metric_name,
            row.concept_name,
            row.period_start,
            row.period_end,
            row.source_document,
        )
        for row in standard
    }
    candidates: list[MetricEvidence] = []
    for fact in facts:
        if (
            _STANDARD_BY_CONCEPT.get(
                (_taxonomy(fact), fact.concept_name.lower())
            )
            is not None
        ):
            continue
        metric_name = _candidate_metric(fact)
        key = (
            metric_name,
            fact.concept_name,
            fact.period_start,
            fact.period_end,
            fact.source_document,
        )
        if (
            not metric_name
            or key in emitted
            or not _valid_candidate_period(fact, metric_name=metric_name)
        ):
            continue
        candidates.append(
            MetricEvidence(
                metric_name=metric_name,
                concept_name=fact.concept_name,
                value=fact.numeric_value,
                unit=fact.unit,
                period_start=fact.period_start,
                period_end=fact.period_end,
                scope=fact.scope,
                confidence=0.72,
                status="REVIEW_REQUIRED",
                reason="issuer_extension_financial_operand_requires_review",
                evidence_text=(
                    f"{fact.taxonomy}:{fact.concept_name} "
                    f"label={_metadata(fact).get('label', '')} "
                    f"context={fact.context_id} "
                    f"dimensions={fact.dimensions_json}"
                )[:1000],
                source_document=fact.source_document,
                extraction_method="dedicated_parser:arelle_extension_candidate",
                provenance={
                    "adapter_version": ADAPTER_VERSION,
                    "candidate_metric": metric_name,
                    "taxonomy": fact.taxonomy,
                    "namespace_uri": _metadata(fact).get(
                        "namespace_uri", ""
                    ),
                    "automatic_promotion_allowed": False,
                },
            )
        )
        emitted.add(key)
    unique: dict[str, MetricEvidence] = {}
    for row in (*standard, *candidates):
        unique[
            row.evidence_key(
                model_family=item.model_family,
                filing=item.filing,
            )
        ] = row
    return tuple(
        sorted(
            unique.values(),
            key=lambda row: (
                row.metric_name,
                row.period_end,
                row.period_start,
                row.concept_name,
                row.value if row.value is not None else float("-inf"),
                row.status,
            ),
        )
    )
