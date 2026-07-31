from __future__ import annotations

import json
import runpy
from datetime import date
from pathlib import Path
from typing import Any

from dedicated_parser.contracts import (
    DocumentRef,
    FilingRef,
    NormalizedFact,
    WorkItem,
    file_sha256,
)
from industrials.machinery.dedicated_parser_adapter import (
    _CURRENT_HORIZON,
    _CURRENT_PERCENT,
    _TABLE_METRIC_PATTERNS,
    extract_metric_evidence,
    get_registry,
    map_normalized_facts,
)
from industrials.machinery.recoverable_coverage import (
    build_issuer_ir_recovery_requests,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = (
    "industrials.machinery.dedicated_parser_adapter:"
    "extract_metric_evidence"
)


def filing() -> FilingRef:
    return FilingRef(
        ticker="TEST",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-Q",
        filing_date="2026-04-30",
        accepted_at="2026-04-30T12:00:00Z",
        report_date="2026-03-31",
        primary_document="filing.htm",
        source_id="sec_submissions",
    )


def work_item(*, documents: tuple[DocumentRef, ...] = ()) -> WorkItem:
    return WorkItem(
        model_family="machinery",
        adapter_path=ADAPTER,
        adapter_version="test",
        filing=filing(),
        documents=documents,
        requested_metrics=get_registry().parser_metrics,
    )


def fact(
    concept_name: str,
    value: float,
    *,
    period_start: str,
    period_end: str = "2026-03-31",
    scope: str = "consolidated",
    dimensions: dict[str, str] | None = None,
) -> NormalizedFact:
    return NormalizedFact(
        taxonomy="us-gaap",
        concept_name=concept_name,
        value_text=str(value),
        numeric_value=value,
        unit="USD",
        period_start=period_start,
        period_end=period_end,
        context_id=f"{concept_name}-{scope}",
        dimensions_json=json.dumps(dimensions or {}),
        scope=scope,
        source_document="filing.htm",
        provider="arelle",
    )


def document_for(tmp_path: Path, text: str) -> DocumentRef:
    path = tmp_path / "filing.htm"
    path.write_text(f"<html><body><p>{text}</p></body></html>", encoding="utf-8")
    stat = path.stat()
    return DocumentRef(
        name=path.name,
        path=str(path),
        content_sha256=file_sha256(path),
        file_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        is_primary=True,
    )


def test_registry_routes_derived_financial_gaps_to_standard_operands() -> None:
    registry = get_registry()
    for metric in (
        "capital_raise_dependence",
        "cash_conversion_cycle_change",
        "cash_runway_years",
        "incremental_operating_margin",
        "interest_coverage",
        "net_debt_to_ebitda",
        "roic",
    ):
        assert registry.metric_dependencies[metric] == "financial_operands"
    for metric in (
        "debt_total",
        "interest_expense",
        "operating_cash_flow",
        "operating_income",
        "revenue",
    ):
        assert metric in registry.production_mappings


def test_standard_operands_accept_only_consolidated_period_valid_facts() -> None:
    evidence = map_normalized_facts(
        work_item(),
        (
            fact(
                "OperatingIncomeLoss",
                25_000_000.0,
                period_start="2026-01-01",
            ),
            fact(
                "InterestExpense",
                2_000_000.0,
                period_start="2026-01-01",
            ),
            fact(
                "OperatingIncomeLoss",
                8_000_000.0,
                period_start="2026-01-01",
                scope="dimensional",
                dimensions={"us-gaap:SegmentAxis": "test:EnergyMember"},
            ),
        ),
    )
    by_metric = {}
    for row in evidence:
        by_metric.setdefault(row.metric_name, []).append(row)
    assert by_metric["interest_expense"][0].status == "ACCEPTED"
    operating = by_metric["operating_income"]
    assert {row.status for row in operating} == {"ACCEPTED", "REVIEW_REQUIRED"}
    assert next(row for row in operating if row.status == "ACCEPTED").reason == (
        "exact_standard_taxonomy_consolidated_operand"
    )


def test_zero_debt_total_stays_review_only_but_zero_component_can_be_correlated() -> None:
    evidence = map_normalized_facts(
        work_item(),
        (
            fact("DebtAndFinanceLeaseObligations", 0.0, period_start=""),
            fact("DebtCurrent", 0.0, period_start=""),
            fact("LongTermDebtNoncurrent", 50_000_000.0, period_start=""),
        ),
    )
    total = next(row for row in evidence if row.metric_name == "debt_total")
    current = next(row for row in evidence if row.metric_name == "debt_current")
    noncurrent = next(row for row in evidence if row.metric_name == "debt_noncurrent")
    assert (total.status, total.reason) == (
        "REVIEW_REQUIRED",
        "explicit_zero_debt_fact_requires_review",
    )
    assert current.status == "ACCEPTED"
    assert noncurrent.status == "ACCEPTED"


def test_reverse_rpo_and_next_year_percentage_wording_are_recognized() -> None:
    rpo_text = (
        "Our total performance obligations that are unsatisfied were "
        "$1.0 billion as of March 31, 2026."
    )
    rpo_pattern = next(
        pattern
        for _, metric_name, pattern in _TABLE_METRIC_PATTERNS
        if metric_name == "remaining_performance_obligation"
    )
    assert rpo_pattern.search(rpo_text)
    horizon_text = "We expect to recognize 40 percent during the next year."
    assert _CURRENT_HORIZON.search(horizon_text)
    match = _CURRENT_PERCENT.search(horizon_text)
    assert match is not None
    assert match.group("percent_one_year") == "40"


def test_going_concern_prose_emits_explicit_boolean_without_ratio_inference(
    tmp_path: Path,
) -> None:
    positive = document_for(
        tmp_path,
        "These conditions raise substantial doubt about our ability to continue "
        "as a going concern.",
    )
    evidence = extract_metric_evidence(work_item(documents=(positive,)))
    flag = next(row for row in evidence if row.metric_name == "going_concern_flag")
    assert (flag.status, flag.value) == ("ACCEPTED", 1.0)
    assert flag.provenance["semantic_block_index"] >= 0
    assert all(
        row.metric_name not in {"cash_runway_years", "capital_raise_dependence"}
        for row in evidence
    )


def test_explicit_no_substantial_doubt_emits_zero(tmp_path: Path) -> None:
    resolved = document_for(
        tmp_path,
        "These conditions do not raise substantial doubt about our ability to "
        "continue as a going concern.",
    )
    evidence = extract_metric_evidence(work_item(documents=(resolved,)))
    flag = next(row for row in evidence if row.metric_name == "going_concern_flag")
    assert (flag.status, flag.value) == ("ACCEPTED", 0.0)


def test_hypothetical_going_concern_language_is_not_an_observation(
    tmp_path: Path,
) -> None:
    hypothetical = document_for(
        tmp_path,
        "Failure to obtain future financing may raise substantial doubt about "
        "our ability to continue as a going concern.",
    )
    evidence = extract_metric_evidence(work_item(documents=(hypothetical,)))
    assert all(row.metric_name != "going_concern_flag" for row in evidence)


def test_revenue_alignment_uses_ttm_at_disclosure_date_and_rejects_stale() -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "scripts"
            / "08_build_industrials_financial_features.py"
        )
    )
    result_type = namespace["TtmResult"]
    align = namespace["revenue_ttm_aligned_to_instant_metric"]
    rows: list[dict[str, Any]] = [
        {
            "canonical_metric": "revenue",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "fiscal_period": "FY",
            "value": 400.0,
        }
    ]
    current = result_type(
        500.0,
        "",
        window_start=date(2025, 7, 1),
        window_end=date(2026, 6, 30),
    )
    aligned, quality = align(
        rows,
        metric_period_end=date(2025, 12, 31),
        current_revenue=current,
    )
    assert aligned.value == 400.0
    assert quality == "aligned_to_disclosure_period"
    stale, quality = align(
        rows,
        metric_period_end=date(2024, 12, 31),
        current_revenue=current,
    )
    assert stale.value is None
    assert quality == "stale_instant_metric_revenue_alignment"


def test_issuer_ir_requests_are_grouped_without_fabricated_urls() -> None:
    rows = [
        {
            "ticker": "TEST",
            "asof_date": "2026-07-24",
            "metric_name": "orders",
            "source_metric": "orders",
            "source_lane": "ISSUER_IR",
        },
        {
            "ticker": "TEST",
            "asof_date": "2026-07-24",
            "metric_name": "book_to_bill",
            "source_metric": "orders",
            "source_lane": "ISSUER_IR",
        },
        {
            "ticker": "SKIP",
            "asof_date": "2026-07-24",
            "metric_name": "roic",
            "source_metric": "",
            "source_lane": "STANDARD_XBRL",
        },
    ]
    requests = build_issuer_ir_recovery_requests(rows)
    assert len(requests) == 1
    request = requests[0]
    assert request["ticker"] == "TEST"
    assert request["missing_cell_count"] == 2
    assert request["source_metrics"] == "orders"
    assert request["manifest_status"] == "RESEARCH_REQUIRED"
    assert request["source_policy"] == "official_issuer_domain_only"
    assert "url" not in request
