from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

import consumer_defensive.core.specialized_metrics as specialized_metrics
from consumer_defensive.adapters.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    extract_metric_evidence,
    get_registry,
    policy_manifest,
)
from consumer_defensive.core.db import connect, init_db, utc_now
from consumer_defensive.core.metric_registry import (
    load_metric_registry,
    upsert_metric_registry,
)
from consumer_defensive.core.scoring_features import (
    component_observation_id,
    input_observation_id,
)
from consumer_defensive.core.source_registry import (
    load_source_registry,
    upsert_source_registry,
)
from consumer_defensive.core.specialized_metrics import (
    DEFINITION_VERSION,
    SOURCE_ID,
    apply_stage6b_measurement_overlays,
    specialized_observation_sha256,
)
from consumer_defensive.core.stage6a_schema import ensure_stage6a_schema
from consumer_defensive.core.stage6b_schema import (
    STAGE6B_MIGRATION_SHA256,
    STAGE6B_V3_MIGRATION_SHA256,
    STAGE6B_V4_MIGRATION_SHA256,
    STAGE6B_V5_MIGRATION_SHA256,
    STAGE6B_V6_MIGRATION_SHA256,
    STAGE6B_V1_MIGRATION_SHA256,
    ensure_stage6b_schema,
)
from consumer_defensive.core.financial_pipeline import FinancialFeatureBundle
from consumer_defensive.core.financial_semantics import FinancialValue, FlowSelection
from dedicated_parser.contracts import DocumentRef, FilingRef, MetricRequest, WorkItem


ROOT = Path(__file__).resolve().parents[2]
METRICS = (
    ROOT
    / 'consumer_defensive'
    / 'data'
    / 'consumer_defensive_specialized_metric_registry.yaml'
)
SOURCES = ROOT / 'consumer_defensive' / 'data' / 'free_source_registry.yaml'


def _work_item(
    tmp_path: Path,
    text: str,
    *metric_names: str,
) -> WorkItem:
    path = tmp_path / 'filing.htm'
    path.write_text(f'<html><body>{text}</body></html>', encoding='utf-8')
    import hashlib

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    filing = FilingRef(
        ticker='TEST',
        cik='0000000001',
        archive_cik='0000000001',
        accession_number='0000000001-24-000001',
        form_type='10-K',
        filing_date='2025-02-15',
        accepted_at='2025-02-15T12:00:00Z',
        report_date='2024-12-31',
        primary_document='filing.htm',
        source_id='sec_submissions',
        company_currency='USD',
    )
    document = DocumentRef(
        name='filing.htm',
        path=str(path),
        content_sha256=digest,
        file_size=path.stat().st_size,
        modified_ns=path.stat().st_mtime_ns,
        is_primary=True,
        source_kind='stage4_sealed_cas',
    )
    return WorkItem(
        model_family='consumer_defensive',
        adapter_path=(
            'consumer_defensive.adapters.dedicated_parser_adapter:'
            'extract_metric_evidence'
        ),
        adapter_version=ADAPTER_VERSION,
        filing=filing,
        documents=(document,),
        requested_metrics=tuple(MetricRequest(name) for name in metric_names),
        enable_arelle=False,
        enable_edgartools=False,
    )


def test_adapter_registry_is_consumer_defensive_owned_and_exact() -> None:
    registry = get_registry()
    manifest = policy_manifest()
    assert registry.model_family == 'consumer_defensive'
    assert registry.adapter_version == ADAPTER_VERSION
    assert len(registry.source_metrics) == 38
    assert manifest['metric_count'] == 38
    assert (
        manifest['term_registry_version']
        == 'consumer_defensive_stage6b_extraction_terms_v8'
    )
    assert {
        value['source_availability_class']
        for value in manifest['metrics'].values()
    } == {
        'sec_direct', 'sec_derived', 'sec_direct_or_derived',
        'sec_selective', 'non_sec',
    }
    assert set(manifest['metrics']) == {
        request.metric_name for request in registry.source_metrics
    }


def test_contextual_aliases_expand_recall_without_generic_false_positives(
    tmp_path: Path,
) -> None:
    positive = _work_item(
        tmp_path,
        '<p>Pricing increased net sales by 4.2% versus the prior year.</p>',
        'price_mix_growth_pct',
    )
    accepted = [
        row for row in extract_metric_evidence(positive)
        if row.status == 'ACCEPTED'
    ]
    assert [(row.metric_name, row.value) for row in accepted] == [
        ('price_mix_growth_pct', pytest.approx(4.2))
    ]

    negative = _work_item(
        tmp_path,
        '<p>Pricing of debt securities increased 4.2% under the contract.</p>',
        'price_mix_growth_pct',
    )
    assert not extract_metric_evidence(negative)


@pytest.mark.parametrize(
    ('metric_id', 'disclosure', 'expected'),
    (
        (
            'price_mix_growth_pct',
            'PVM contributed 3.2% to net sales growth versus prior year.',
            3.2,
        ),
        (
            'traffic_growth_pct',
            'Comparable customer count increased 2.1% year over year.',
            2.1,
        ),
        (
            'production_volume_growth_pct',
            'Manufacturing volume grew 6.0% versus the prior year.',
            6.0,
        ),
        (
            'revenue_per_unit_growth_pct',
            'Net revenue per hectoliter increased 4.5% versus prior year.',
            4.5,
        ),
        (
            'alcohol_depletion_growth_pct',
            'Case depletions decreased 2.0% versus the prior year.',
            -2.0,
        ),
    ),
)
def test_v8_contextual_aliases_extract_quantified_period_changes(
    tmp_path: Path,
    metric_id: str,
    disclosure: str,
    expected: float,
) -> None:
    item = _work_item(tmp_path, f'<p>{disclosure}</p>', metric_id)
    accepted = [
        row for row in extract_metric_evidence(item)
        if row.status == 'ACCEPTED'
    ]
    assert [(row.metric_name, row.value) for row in accepted] == [
        (metric_id, pytest.approx(expected))
    ]


@pytest.mark.parametrize(
    ('metric_id', 'disclosure'),
    (
        (
            'price_mix_growth_pct',
            'The PVM securities valuation increased 3.2% under the contract.',
        ),
        (
            'traffic_growth_pct',
            'Customer count for credit loan accounts increased 2.1%.',
        ),
        (
            'production_volume_growth_pct',
            'The manufacturing volume capacity plan assumes 6.0% growth.',
        ),
        (
            'revenue_per_unit_growth_pct',
            'Revenue per liter was $3.50 for the year.',
        ),
    ),
)
def test_v8_contextual_aliases_reject_wrong_or_unquantified_context(
    tmp_path: Path,
    metric_id: str,
    disclosure: str,
) -> None:
    item = _work_item(tmp_path, f'<p>{disclosure}</p>', metric_id)
    assert extract_metric_evidence(item) == ()


def test_organic_revenue_verb_morphology_is_quantified_without_level_leakage(
    tmp_path: Path,
) -> None:
    positive = _work_item(
        tmp_path,
        '<p>Organic revenues (non-GAAP) grew 5% for the full year.</p>',
        'organic_revenue_growth_pct',
    )
    accepted = [
        row for row in extract_metric_evidence(positive)
        if row.status == 'ACCEPTED'
    ]
    assert [(row.metric_name, row.value) for row in accepted] == [
        ('organic_revenue_growth_pct', pytest.approx(5.0))
    ]

    level_only = _work_item(
        tmp_path,
        '<p>Organic revenue was $5 billion for the year.</p>',
        'organic_revenue_growth_pct',
    )
    assert not extract_metric_evidence(level_only)

    risk_factor = _work_item(
        tmp_path,
        '<p>Risk factors could cause organic revenues to decrease 5%.</p>',
        'organic_revenue_growth_pct',
    )
    assert not extract_metric_evidence(risk_factor)


def test_inequality_and_looking_ahead_values_do_not_qualify(
    tmp_path: Path,
) -> None:
    for qualified_value, expected_reason in (
        ('more than 30%', 'inequality_value_is_not_an_exact_measurement'),
        ('around 20%', 'approximate_value_requires_review'),
        ('over 30%', 'inequality_value_is_not_an_exact_measurement'),
    ):
        bounded = _work_item(
            tmp_path,
            f'<p>Global brand organic sales increased {qualified_value}.</p>',
            'organic_revenue_growth_pct',
        )
        evidence = extract_metric_evidence(bounded)
        assert evidence
        assert all(row.status != 'ACCEPTED' for row in evidence)
        assert any(row.reason == expected_reason for row in evidence)

    forward = _work_item(
        tmp_path,
        '<h2>Looking Ahead to Fiscal 2026</h2>'
        '<p>Pricing momentum is off to a strong start with a 16.9% '
        'year-over-year sales increase.</p>',
        'price_mix_growth_pct',
    )
    assert not extract_metric_evidence(forward)


def test_preceding_segment_heading_scopes_prose_measurement(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<h2>Health Care Segment</h2>'
        '<p>Personal Health Care organic sales grew 20% versus year ago.</p>',
        'organic_revenue_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].scope == 'segment'
    assert evidence[0].status == 'REVIEW_REQUIRED'
    assert evidence[0].reason == 'segment_scope_requires_definition_review'


def test_processed_volume_growth_alias_requires_change_context(
    tmp_path: Path,
) -> None:
    positive = _work_item(
        tmp_path,
        '<p>Processed volumes increased 6.2% versus the prior year.</p>',
        'production_volume_growth_pct',
    )
    accepted = [
        row for row in extract_metric_evidence(positive)
        if row.status == 'ACCEPTED'
    ]
    assert [(row.metric_name, row.value) for row in accepted] == [
        ('production_volume_growth_pct', pytest.approx(6.2))
    ]
    level_only = _work_item(
        tmp_path,
        '<p>Processed volumes were 2.4 million tons.</p>',
        'production_volume_growth_pct',
    )
    assert not extract_metric_evidence(level_only)


def test_immediately_preceding_kpi_value_beats_later_metric(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<p>We delivered 28% year-over-year volume growth, while the '
        'category declined 0.5%.</p>',
        'volume_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].status == 'ACCEPTED'
    assert evidence[0].value == pytest.approx(28.0)


def test_numeric_binding_does_not_cross_kpi_conjunction(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<p>Core growth included low single digit volume growth and '
        'approximately 10% benefit from price and mix.</p>',
        'volume_growth_pct',
    )
    assert extract_metric_evidence(item) == ()


def test_high_value_review_candidates_have_definition_complete_paths(
    tmp_path: Path,
) -> None:
    advertising = _work_item(
        tmp_path,
        '<p>Advertising and sales promotion spending was about 10% of net '
        'sales for the period.</p>',
        'advertising_promotion_pct_sales',
    )
    advertising_rows = extract_metric_evidence(advertising)
    assert [
        (row.status, row.value, row.reason) for row in advertising_rows
    ] == [(
        'ACCEPTED', pytest.approx(10.0),
        'explicit_approximate_point_estimate',
    )]

    production = _work_item(
        tmp_path,
        '<p>Owned avocado production volume increased 38% versus the prior '
        'year.</p>',
        'production_volume_growth_pct',
    )
    production_rows = extract_metric_evidence(production)
    assert [
        (row.status, row.value, row.scope) for row in production_rows
    ] == [('ACCEPTED', pytest.approx(38.0), 'segment_avocados')]

    organic = _work_item(
        tmp_path,
        '<p>The Retail and Wholesale (“R&amp;W”) Products Group’s revenues '
        'reached $72.8 million, a 3% increase over the prior year. This gain '
        'was driven by the acquisition of Ultra Pet, which contributed $4.8 '
        'million in sales from both branded and private label crystal litter '
        'products. Excluding the impact of the acquisition, organic sales '
        'within the operating segment decreased by 4%.</p>',
        'organic_revenue_growth_pct',
    )
    organic_rows = extract_metric_evidence(organic)
    assert [
        (row.status, row.value, row.scope) for row in organic_rows
    ] == [(
        'ACCEPTED', pytest.approx(-4.0),
        'segment_retail_wholesale_products',
    )]

    comparable = _work_item(
        tmp_path,
        '<p>OXXO Mexico same-store sales increased 9.5% in the quarter.</p>',
        'comparable_sales_growth_pct',
    )
    comparable_rows = extract_metric_evidence(comparable)
    assert [
        (row.status, row.value, row.scope) for row in comparable_rows
    ] == [('ACCEPTED', pytest.approx(9.5), 'segment_oxxo_mexico')]


def test_consolidated_volume_and_label_before_value_bind_correctly(
    tmp_path: Path,
) -> None:
    consolidated = _work_item(
        tmp_path,
        '<p>Results from continuing operations reflected sales growth in '
        'several reportable segments and lower sales in other reportable '
        'segments. Volume increased by 5% due to higher shipments in the Lifestyle and '
        'Cleaning reportable segments.</p>',
        'volume_growth_pct',
    )
    consolidated_rows = extract_metric_evidence(consolidated)
    assert [
        (row.status, row.value, row.scope) for row in consolidated_rows
    ] == [('ACCEPTED', pytest.approx(5.0), 'consolidated')]

    traffic = _work_item(
        tmp_path,
        '<p>Q2 Highlights - Sales Growth +7.4% Comparable Sales '
        '+6.7% Adjusted Comparable '
        'Sales +3.1% Comparable Traffic +22.6% Digitally-Enabled Comparable '
        'Sales. Segment Reporting: US Canada Other International.</p>',
        'traffic_growth_pct',
    )
    traffic_rows = extract_metric_evidence(traffic)
    assert [
        (row.status, row.value, row.reason) for row in traffic_rows
    ] == [(
        'ACCEPTED', pytest.approx(3.1),
        'explicit_metric_term_unit_and_plausible_value',
    )]


def test_known_review_misbindings_are_rejected_systemically(
    tmp_path: Path,
) -> None:
    gross_margin_pricing = _work_item(
        tmp_path,
        '<p>Gross profit margin expanded 40 basis points to 47.5% driven '
        'mainly by our pricing initiatives and lower sweetener costs.</p>',
        'price_mix_growth_pct',
    )
    assert extract_metric_evidence(gross_margin_pricing) == ()

    acquisition_contribution = _work_item(
        tmp_path,
        '<p>Net revenue decreased 2%, reflecting a decline in volume, '
        'partially offset by effective net pricing, as well as acquisitions '
        'which positively contributed 1 percentage point.</p>',
        'price_mix_growth_pct',
    )
    assert extract_metric_evidence(acquisition_contribution) == ()

    dense_slide = _work_item(
        tmp_path,
        '<p>+120 bps 6.9% Logistics Costs +13% Total Distribution Points '
        '-30 bps 28.6% Input Costs 16.7% Ecommerce Share of Sales +50 bps '
        '2.5% Quality Costs.</p>',
        'commodity_cost_impact_bps',
    )
    assert extract_metric_evidence(dense_slide) == ()

    volume_contribution = _work_item(
        tmp_path,
        '<p>Approximately 2.5 percentage points of the volume decline '
        'reflect market share losses.</p>',
        'market_share_change_bps',
    )
    assert extract_metric_evidence(volume_contribution) == ()

    volume_mix = _work_item(
        tmp_path,
        '<p>International and Away From Home segment net sales decreased, '
        'primarily reflecting a 2 percentage point decline from volume/mix '
        'resulting from increased shipments in the prior year.</p>',
        'volume_growth_pct',
    )
    assert extract_metric_evidence(volume_mix) == ()


def test_explicit_approximate_point_estimate_retains_precision(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<p>Net sales for e-commerce represented approximately 7% of '
        'total net sales in 2025.</p>',
        'digital_sales_mix_pct',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].status == 'ACCEPTED'
    assert evidence[0].value == pytest.approx(7.0)
    assert evidence[0].reason == 'explicit_approximate_point_estimate'
    assert evidence[0].provenance['measurement_precision'] == 'approximate'

    ticket = _work_item(
        tmp_path,
        '<p>Comparable sales were positively impacted by an increase of '
        'approximately 4% in average ticket.</p>',
        'average_ticket_growth_pct',
    )
    ticket_evidence = extract_metric_evidence(ticket)
    assert ticket_evidence[0].status == 'ACCEPTED'
    assert ticket_evidence[0].value == pytest.approx(4.0)
    assert ticket_evidence[0].provenance['measurement_precision'] == 'approximate'


def test_static_or_unrelated_levels_are_not_growth_candidates(
    tmp_path: Path,
) -> None:
    feed = _work_item(
        tmp_path,
        '<p>Feed costs account for approximately 60% of hog production '
        'raising cost.</p>',
        'livestock_feed_cost_change_pct',
    )
    transactions = _work_item(
        tmp_path,
        '<p>Point of sale transactions represented approximately 93% of '
        'net sales.</p>',
        'traffic_growth_pct',
    )
    assert extract_metric_evidence(feed) == ()
    assert extract_metric_evidence(transactions) == ()


def test_consolidated_heading_and_named_segment_scope_qualify(
    tmp_path: Path,
) -> None:
    consolidated = _work_item(
        tmp_path,
        '<h2>Consolidated Results from Continuing Operations</h2>'
        '<p>Volume increased 5% due to higher shipments in the Lifestyle '
        'and Cleaning reportable segments.</p>',
        'volume_growth_pct',
    )
    consolidated_evidence = extract_metric_evidence(consolidated)
    segment = _work_item(
        tmp_path,
        '<h2>Beef North America Segment</h2>'
        '<p>Net revenue growth was partially offset by a 6.7% decrease '
        'in sales volume.</p>',
        'volume_growth_pct',
    )
    segment_evidence = extract_metric_evidence(segment)
    assert consolidated_evidence[0].status == 'ACCEPTED'
    assert consolidated_evidence[0].scope == 'consolidated'
    assert consolidated_evidence[0].value == pytest.approx(5.0)
    assert segment_evidence[0].status == 'ACCEPTED'
    assert segment_evidence[0].scope == 'segment_beef_north_america'
    assert segment_evidence[0].value == pytest.approx(-6.7)


def test_named_operating_segments_qualify_without_generic_segment_relaxation(
    tmp_path: Path,
) -> None:
    cases = (
        (
            '<h2>Gold pineapple</h2><p>Pricing increased 7%.</p>',
            'price_mix_growth_pct', 'segment_gold_pineapple', 7.0,
        ),
        (
            '<h2>North America Foodservice Segment Results</h2>'
            '<p>Contributions from volume growth were 7%.</p>',
            'volume_growth_pct', 'segment_north_america_foodservice', 7.0,
        ),
        (
            '<h2>North America</h2><p>Volume growth was 2%.</p>',
            'volume_growth_pct', 'segment_north_america', 2.0,
        ),
        (
            '<h2>SS&T segment</h2><p>Sales volume increased 10.3%.</p>',
            'volume_growth_pct', 'segment_sst', 10.3,
        ),
    )
    for html, metric, expected_scope, expected_value in cases:
        item = _work_item(tmp_path, html, metric)
        evidence = extract_metric_evidence(item)
        assert len(evidence) == 1
        assert evidence[0].status == 'ACCEPTED'
        assert evidence[0].scope == expected_scope
        assert evidence[0].value == pytest.approx(expected_value)


def test_pricing_does_not_bind_gross_profit_and_expansion_volume_is_segmented(
    tmp_path: Path,
) -> None:
    gross_profit = _work_item(
        tmp_path,
        '<p>Adjusted gross profit increased 16.9% on pricing actions and '
        'productivity.</p>',
        'price_mix_growth_pct',
    )
    assert extract_metric_evidence(gross_profit) == ()

    expansion = _work_item(
        tmp_path,
        '<p>Growth accelerated in Expansion Geographies with retail volume '
        'up 8.9%.</p>',
        'volume_growth_pct',
    )
    evidence = extract_metric_evidence(expansion)
    assert len(evidence) == 1
    assert evidence[0].scope == 'segment_expansion_geographies'
    assert evidence[0].status == 'REVIEW_REQUIRED'


def test_market_share_comparative_levels_derive_basis_point_change(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<h2>Results of Operations</h2><table>'
        '<tr><th>Metric</th><th>Current Year</th><th>Prior Year</th></tr>'
        '<tr><td>Market share</td><td>22.4%</td><td>21.8%</td></tr>'
        '</table>',
        'market_share_change_bps',
    )
    accepted = [
        row for row in extract_metric_evidence(item)
        if row.status == 'ACCEPTED'
    ]
    assert len(accepted) == 1
    assert accepted[0].value == pytest.approx(60.0)
    assert accepted[0].unit == 'basis_points'
    assert accepted[0].reason == (
        'comparative_market_share_levels_table_derivation'
    )


def test_quantified_shrink_context_is_required(tmp_path: Path) -> None:
    positive = _work_item(
        tmp_path,
        '<p>Lower shrink benefited gross margin by 35 basis points.</p>',
        'shrink_change_bps',
    )
    assert any(
        row.status == 'ACCEPTED' and row.value == pytest.approx(-35.0)
        for row in extract_metric_evidence(positive)
    )
    negative = _work_item(
        tmp_path,
        '<p>Packaging shrink-wrap costs increased 35 basis points.</p>',
        'shrink_change_bps',
    )
    assert not extract_metric_evidence(negative)


def test_semantic_table_headers_and_section_context_are_retained(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<h2>Results of Operations</h2><p>Net sales bridge</p>'
        '<table><tr><th>Driver</th><th>Change vs. LY</th></tr>'
        '<tr><td>Price/Mix</td><td>3.5%</td></tr></table>',
        'price_mix_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    accepted = [row for row in evidence if row.status == 'ACCEPTED']
    assert accepted and accepted[0].value == pytest.approx(3.5)
    assert 'Results of Operations' in accepted[0].provenance['row_cells'][0]


def test_matched_scope_growth_derivation_never_overrides_direct_metric() -> None:
    _, metrics = load_metric_registry(METRICS)
    metric_by_id = {metric.metric_id: metric for metric in metrics}
    taxonomy = {'TEST': {'cohort_id': 'beverages', 'subtype': 'non_alcohol'}}
    base = {
        'ticker': 'TEST', 'period_end': '2024-12-31',
        'accepted_at': '2025-02-15T12:00:00Z', 'unit': 'percent',
        'scope': 'consolidated', 'period_start': '',
    }
    rows = [
        {**base, 'metric_id': 'organic_revenue_growth_pct',
         'numeric_value': 8.0, 'observation_sha256': 'a' * 64},
        {**base, 'metric_id': 'volume_growth_pct',
         'numeric_value': 3.0, 'observation_sha256': 'b' * 64},
    ]
    derived = specialized_metrics._derived_parser_growth_observations(
        rows, metric_by_id=metric_by_id, taxonomy=taxonomy
    )
    observed = {row['metric_id']: row for row in derived}
    assert observed['price_mix_growth_pct']['numeric_value'] == pytest.approx(
        (1.08 / 1.03 - 1.0) * 100.0
    )
    assert observed['revenue_per_unit_growth_pct']['scope'] == 'consolidated'

    rows.append({
        **base, 'metric_id': 'price_mix_growth_pct',
        'numeric_value': 6.0, 'observation_sha256': 'c' * 64,
    })
    derived = specialized_metrics._derived_parser_growth_observations(
        rows, metric_by_id=metric_by_id, taxonomy=taxonomy
    )
    assert {row['metric_id'] for row in derived} == {
        'revenue_per_unit_growth_pct'
    }


def test_parser_conflicts_are_isolated_by_scope_and_unit() -> None:
    _, metrics = load_metric_registry(METRICS)
    metric = next(
        item for item in metrics if item.metric_id == 'organic_revenue_growth_pct'
    )
    base = {
        'ticker': 'TEST', 'metric_name': metric.metric_id,
        'period_start': '', 'period_end': '2024-12-31',
        'accepted_at': '2025-02-15T12:00:00Z', 'confidence': 0.95,
        'candidate_status': 'ACCEPTED', 'unit': 'percent',
        'evidence_key': 'a' * 64, 'work_key': 'w',
        'accession_number': '0000000001-25-000001', 'form_type': '10-K',
        'concept_name': 'OrganicRevenueGrowth', 'status_reason': 'explicit',
        'parser_release': 'test', 'adapter_version': ADAPTER_VERSION,
        'provenance_json': '{}', 'source_document': 'filing.htm',
        'extraction_method': 'dedicated_parser:test',
    }

    class FakeConnection:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, *_args, **_kwargs):
            return iter(self.rows)

    taxonomy = {'TEST': {'cohort_id': 'beverages', 'subtype': 'non_alcohol'}}
    rows = [
        {**base, 'candidate_value': 5.0, 'scope': 'consolidated'},
        {**base, 'candidate_value': 8.0, 'scope': 'segment',
         'evidence_key': 'b' * 64},
    ]
    observations, conflicts = specialized_metrics._parser_observations(
        FakeConnection(rows), parser_run_id=1, as_of='2025-12-31',
        metric_by_id={metric.metric_id: metric}, taxonomy=taxonomy,
        minimum_confidence=0.85,
    )
    assert len(observations) == 2
    assert conflicts == []

    rows[1] = {**rows[1], 'scope': 'consolidated'}
    observations, conflicts = specialized_metrics._parser_observations(
        FakeConnection(rows), parser_run_id=1, as_of='2025-12-31',
        metric_by_id={metric.metric_id: metric}, taxonomy=taxonomy,
        minimum_confidence=0.85,
    )
    assert observations == []
    assert conflicts[0]['scope'] == 'consolidated'


def test_pdf_bytes_never_fall_through_to_plain_text(tmp_path: Path) -> None:
    item = _work_item(
        tmp_path, '<p>Organic revenue growth was 5%.</p>',
        'organic_revenue_growth_pct',
    )
    path = tmp_path / 'filing.pdf'
    path.write_bytes(b'%PDF-1.4\nnot a valid PDF\n%%EOF')
    digest = __import__('hashlib').sha256(path.read_bytes()).hexdigest()
    document = DocumentRef(
        name='filing.pdf', path=str(path), content_sha256=digest,
        file_size=path.stat().st_size, modified_ns=path.stat().st_mtime_ns,
        is_primary=True, source_kind='stage6b_event_sealed_cas',
    )
    evidence = extract_metric_evidence(WorkItem(
        **{**vars(item), 'documents': (document,)}
    ))
    assert len(evidence) == 1
    assert evidence[0].status == 'PARSER_FAILURE'
    assert 'PDF' in evidence[0].evidence_text


def test_image_only_pdf_ocr_is_bounded_provenanced_and_review_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pymupdf = pytest.importorskip('pymupdf')
    item = _work_item(
        tmp_path, '<p>unused</p>', 'organic_revenue_growth_pct'
    )
    path = tmp_path / 'image-only.pdf'
    document_builder = pymupdf.open()
    document_builder.new_page(width=120, height=80)
    path.write_bytes(document_builder.tobytes())
    document_builder.close()
    fake_ocr = ModuleType('pytesseract')
    fake_ocr.get_tesseract_version = lambda: 'test-5.0'  # type: ignore[attr-defined]

    def image_to_string(image, *, timeout, config):
        assert image.width * image.height <= 100_000
        assert timeout == 2.0
        assert config.startswith('--psm 6')
        return 'Organic revenue growth was 5%.'

    fake_ocr.image_to_string = image_to_string  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'pytesseract', fake_ocr)
    digest = __import__('hashlib').sha256(path.read_bytes()).hexdigest()
    document = DocumentRef(
        name=path.name, path=str(path), content_sha256=digest,
        file_size=path.stat().st_size, modified_ns=path.stat().st_mtime_ns,
        is_primary=True, source_kind='stage6b_event_sealed_cas',
    )
    evidence = extract_metric_evidence(WorkItem(
        **{
            **vars(item),
            'documents': (document,),
            'enable_pdf_ocr': True,
            'max_ocr_pages': 1,
            'ocr_dpi': 72,
            'ocr_page_timeout_seconds': 2.0,
            'max_ocr_pixels_per_page': 100_000,
        }
    ))
    assert len(evidence) == 1
    assert evidence[0].status == 'REVIEW_REQUIRED'
    assert evidence[0].reason == 'ocr_derived_requires_review'
    assert evidence[0].confidence <= 0.80
    assert evidence[0].provenance['document_method'] == 'pdf_ocr'
    assert evidence[0].provenance['ocr_used'] is True
    assert evidence[0].provenance['ocr_engine'] == 'tesseract'
    assert evidence[0].provenance['ocr_engine_version'] == 'test-5.0'
    assert evidence[0].provenance['ocr_tessdata_binding'] in {
        'runtime_default', 'environment', 'conda_environment', 'conda_library',
    }
    assert evidence[0].provenance['ocr_page_indices'] == [1]


def test_image_only_pdf_ocr_rejects_page_over_pixel_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pymupdf = pytest.importorskip('pymupdf')
    item = _work_item(
        tmp_path, '<p>unused</p>', 'organic_revenue_growth_pct'
    )
    path = tmp_path / 'too-large.pdf'
    document_builder = pymupdf.open()
    document_builder.new_page(width=120, height=80)
    path.write_bytes(document_builder.tobytes())
    document_builder.close()
    fake_ocr = ModuleType('pytesseract')
    fake_ocr.get_tesseract_version = lambda: 'test-5.0'  # type: ignore[attr-defined]
    fake_ocr.image_to_string = lambda *args, **kwargs: 'should not run'  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'pytesseract', fake_ocr)
    digest = __import__('hashlib').sha256(path.read_bytes()).hexdigest()
    document = DocumentRef(
        name=path.name, path=str(path), content_sha256=digest,
        file_size=path.stat().st_size, modified_ns=path.stat().st_mtime_ns,
        is_primary=True, source_kind='stage6b_event_sealed_cas',
    )
    evidence = extract_metric_evidence(WorkItem(
        **{
            **vars(item), 'documents': (document,), 'enable_pdf_ocr': True,
            'max_ocr_pages': 1, 'ocr_dpi': 72,
            'max_ocr_pixels_per_page': 10,
        }
    ))
    assert len(evidence) == 1
    assert evidence[0].status == 'PARSER_FAILURE'
    assert 'max_ocr_pixels_per_page' in evidence[0].evidence_text


def test_retail_fixed_charge_and_lease_leverage_use_exact_annual_facts() -> None:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(
        '''CREATE TABLE feature_financial_statement(
               ticker TEXT,lineage_json TEXT
           );
           CREATE TABLE fact_financial_statement_canonical(
               canonical_fact_id INTEGER PRIMARY KEY,
               ticker TEXT,canonical_metric TEXT,canonical_component TEXT,
               taxonomy TEXT,reported_currency TEXT,period_start TEXT,
               period_end TEXT,accepted_at TEXT,reported_value REAL,
               source_observation_id TEXT,accession_number TEXT,
               source_concept TEXT
           );
           CREATE TABLE fact_sec_xbrl_fact_raw(
               raw_fact_id INTEGER PRIMARY KEY,ticker TEXT,concept TEXT,
               taxonomy TEXT,unit TEXT,period_start TEXT,period_end TEXT,
               accepted_at TEXT,numeric_value REAL,dimensions_json TEXT,
               source_observation_id TEXT,accession_number TEXT
           );'''
    )
    basis = {
        'basis': {
            'period_start': '2024-01-01',
            'period_end': '2024-12-31',
            'taxonomy': 'us-gaap',
            'reported_currency': 'USD',
        }
    }
    conn.execute(
        'INSERT INTO feature_financial_statement VALUES (\'TEST\',?)',
        (json.dumps(basis),),
    )
    canonical = (
        ('operating_income', 100.0),
        ('depreciation_amortization', 20.0),
        ('debt_current', 10.0),
        ('debt_noncurrent', 50.0),
        ('cash', 5.0),
    )
    for position, (metric, value) in enumerate(canonical, start=1):
        instant = metric in {'debt_current', 'debt_noncurrent', 'cash'}
        conn.execute(
            '''INSERT INTO fact_financial_statement_canonical VALUES(
                   ?,?,?,?,?,?,?,?,?,?,?,?,?
               )''',
            (
                position, 'TEST', metric, 'total', 'us-gaap', 'USD',
                None if instant else '2024-01-01', '2024-12-31',
                '2025-02-15T12:00:00Z', value, f'canonical-{position}',
                '0000000001-25-000001', metric,
            ),
        )
    raw = (
        ('InterestExpenseNonoperating', '2024-01-01', 10.0),
        ('OperatingLeaseCost', '2024-01-01', 15.0),
        ('OperatingLeaseLiability', None, 30.0),
    )
    for position, (concept, period_start, value) in enumerate(raw, start=1):
        conn.execute(
            '''INSERT INTO fact_sec_xbrl_fact_raw VALUES(
                   ?,?,?,?,?,?,?,?,?,?,?,?
               )''',
            (
                position, 'TEST', concept, 'us-gaap', 'USD', period_start,
                '2024-12-31', '2025-02-15T12:00:00Z', value, '{}',
                f'raw-{position}', '0000000001-25-000001',
            ),
        )
    feature_row = conn.execute(
        'SELECT * FROM feature_financial_statement'
    ).fetchone()
    _, metrics = load_metric_registry(METRICS)
    observations = specialized_metrics._retail_fixed_observations(
        conn,
        as_of='2025-02-15',
        feature_row=feature_row,
        member={
            'cohort_id': 'consumer_staples_distribution_retail',
            'subtype': 'retail',
        },
        metric_by_id={metric.metric_id: metric for metric in metrics},
    )
    by_metric = {row['metric_id']: row for row in observations}
    assert by_metric['fixed_charge_coverage']['numeric_value'] == pytest.approx(4.6)
    assert by_metric['lease_adjusted_net_leverage']['numeric_value'] == pytest.approx(
        85.0 / 120.0
    )
    assert all(row['production_status'] == 'measurement_only' for row in observations)


def test_prose_extraction_accepts_explicit_metric_and_unit(tmp_path: Path) -> None:
    item = _work_item(
        tmp_path,
        '<p>Overall comparable sales increased 4.2% for the year.</p>',
        'comparable_sales_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].metric_name == 'comparable_sales_growth_pct'
    assert evidence[0].value == pytest.approx(4.2)
    assert evidence[0].unit == 'percent'
    assert evidence[0].status == 'ACCEPTED'
    assert evidence[0].period_end == '2024-12-31'


def test_controlled_like_for_like_synonym_is_supported(tmp_path: Path) -> None:
    item = _work_item(
        tmp_path,
        '<p>Consolidated like-for-like sales increased 3.4% for the year.</p>',
        'comparable_sales_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(3.4)
    assert evidence[0].status == 'ACCEPTED'


@pytest.mark.parametrize(
    ('metric_id', 'disclosure', 'expected'),
    [
        (
            'organic_revenue_growth_pct',
            'Base business net sales decreased 2.7% for the year.',
            -2.7,
        ),
        ('volume_growth_pct', 'Consolidated volume was down 10% for the year.', -10.0),
        ('price_mix_growth_pct', 'Pricing growth was 3.4% for the year.', 3.4),
        (
            'branded_sales_mix_pct',
            'Branded products represented 78.9% of total sales for the year.',
            78.9,
        ),
        (
            'digital_sales_mix_pct',
            'Net sales for e-commerce represented 7% of total sales for the year.',
            7.0,
        ),
    ],
)
def test_observed_controlled_terminology_variants_are_supported(
    tmp_path: Path,
    metric_id: str,
    disclosure: str,
    expected: float,
) -> None:
    item = _work_item(tmp_path, f'<p>{disclosure}</p>', metric_id)
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(expected)
    assert evidence[0].status == 'ACCEPTED'


def test_covenant_ratio_limit_is_not_misreported_as_actual(tmp_path: Path) -> None:
    limit = _work_item(
        tmp_path,
        '<p>The covenant net leverage ratio must not exceed 4.50x.</p>',
        'net_debt_to_ebitda',
    )
    assert extract_metric_evidence(limit) == ()

    actual = _work_item(
        tmp_path,
        '<p>Our net funded debt to adjusted EBITDA was 2.80x.</p>',
        'net_debt_to_ebitda',
    )
    evidence = extract_metric_evidence(actual)
    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(2.8)
    assert evidence[0].status == 'ACCEPTED'


def test_operated_location_levels_support_store_growth_derivation(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '''
        <table>
          <tr><th>Total operated locations</th><th>2024</th><th>2023</th></tr>
          <tr><td>Number of locations</td><td>110</td><td>100</td></tr>
        </table>
        ''',
        'net_store_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    accepted = [row for row in evidence if row.status == 'ACCEPTED']
    assert len(accepted) == 1
    assert accepted[0].value == pytest.approx(10.0)


def test_basis_point_conversion_and_direction_are_explicit(tmp_path: Path) -> None:
    item = _work_item(
        tmp_path,
        '<p>Consolidated gross margin decreased 1.2 percentage points.</p>',
        'gross_margin_change_bps',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(-120.0)
    assert evidence[0].unit == 'basis_points'
    assert evidence[0].status == 'ACCEPTED'


def test_segment_scope_and_conflicting_values_require_review(tmp_path: Path) -> None:
    segment = _work_item(
        tmp_path,
        '<p>North America segment organic sales growth was 3.1%.</p>',
        'organic_revenue_growth_pct',
    )
    assert extract_metric_evidence(segment)[0].status == 'REVIEW_REQUIRED'

    conflict = _work_item(
        tmp_path,
        '<p>Comparable sales growth was 4.2%. Comparable sales growth was 5.1%.</p>',
        'comparable_sales_growth_pct',
    )
    evidence = extract_metric_evidence(conflict)
    assert evidence
    assert all(row.status == 'REVIEW_REQUIRED' for row in evidence), [
        (row.value, row.status, row.reason, row.scope) for row in evidence
    ]
    assert {
        row.reason for row in evidence
    } == {'conflicting_values_same_metric_period_scope_document'}


def test_definition_complete_operational_segment_measurements_are_accepted(
    tmp_path: Path,
) -> None:
    crush = _work_item(
        tmp_path,
        '<p>Ag Services segment executed soy crush margins increased '
        'approximately $15 per ton.</p>',
        'agricultural_processing_margin',
    )
    crush_evidence = extract_metric_evidence(crush)
    assert [(row.status, row.value, row.unit, row.scope) for row in crush_evidence] == [
        ('ACCEPTED', pytest.approx(15.0), 'USD_per_ton', 'segment')
    ]

    productivity = _work_item(
        tmp_path,
        '<h2>Dollar Tree Segment Information</h2>'
        '<table><tr><th>Metric</th><th>Current</th><th>Prior</th></tr>'
        '<tr><td>Sales per Square Foot</td><td>$242</td><td>$235</td>'
        '</tr></table>',
        'sales_per_square_foot',
    )
    productivity_evidence = extract_metric_evidence(productivity)
    assert [
        (row.status, row.value, row.unit, row.scope)
        for row in productivity_evidence
    ] == [
        ('ACCEPTED', pytest.approx(242.0), 'USD_per_square_foot',
         'segment_dollar_tree')
    ]


def test_value_and_volume_share_measurements_keep_distinct_scopes(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<p>Personal Care categories value market share was up 20 basis '
        'points, while volume share was up 90 basis points.</p>',
        'market_share_change_bps',
    )
    evidence = extract_metric_evidence(item)
    assert {
        (row.status, row.value, row.unit, row.scope) for row in evidence
    } == {
        ('ACCEPTED', 20.0, 'basis_points', 'reported_scope_value_share'),
        ('ACCEPTED', 90.0, 'basis_points', 'reported_scope_volume_share'),
    }


def test_structured_table_value_resolves_lower_precision_prose_conflict(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        (
            '<table><tr><td>Comparable sales</td><td>4.2%</td></tr></table>'
            '<p>Comparable sales growth was 5.1%.</p>'
        ),
        'comparable_sales_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    accepted = [row for row in evidence if row.status == 'ACCEPTED']
    review = [row for row in evidence if row.status == 'REVIEW_REQUIRED']
    assert [(row.value, row.reason) for row in accepted] == [(
        pytest.approx(4.2),
        'structured_table_value_resolves_lower_precision_conflict',
    )]
    assert [(row.value, row.reason) for row in review] == [(
        pytest.approx(5.1),
        'superseded_by_unique_structured_table_value',
    )]


def test_forward_looking_table_value_cannot_resolve_actual_conflict(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        (
            '<table><tr><td>Target comparable sales</td><td>4.2%</td></tr>'
            '</table><p>Comparable sales growth was 5.1%.</p>'
        ),
        'comparable_sales_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    assert [(row.status, row.value) for row in evidence] == [
        ('ACCEPTED', pytest.approx(5.1))
    ]


def test_stage6b_parser_handles_explicit_plus_decimal_comma_and_unfavorable(
    tmp_path: Path,
) -> None:
    plus = _work_item(
        tmp_path,
        '<p>Latin America South volume growth was (+0.6%).</p>',
        'volume_growth_pct',
    )
    assert extract_metric_evidence(plus)[0].value == pytest.approx(0.6)

    decimal_comma = _work_item(
        tmp_path,
        '<p>Consolidated volume growth declined 0,9% versus prior year.</p>',
        'volume_growth_pct',
    )
    assert extract_metric_evidence(decimal_comma)[0].value == pytest.approx(-0.9)

    unfavorable = _work_item(
        tmp_path,
        '<p>Unfavorable price/mix of 1.5% reduced net sales.</p>',
        'price_mix_growth_pct',
    )
    assert extract_metric_evidence(unfavorable)[0].value == pytest.approx(-1.5)


def test_stage6b_parser_does_not_cross_bullet_or_column_boundaries(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<p>Net sales growth 17.6% \u25cf Price/mix growth 2.7% \u25cf Volume growth 3.0%.</p>',
        'price_mix_growth_pct',
        'volume_growth_pct',
    )
    evidence = {
        row.metric_name: row.value
        for row in extract_metric_evidence(item)
        if row.status == 'ACCEPTED'
    }
    assert evidence == {
        'price_mix_growth_pct': pytest.approx(2.7),
        'volume_growth_pct': pytest.approx(3.0),
    }


def test_stage6b_parser_rejects_level_denominator_and_forecast_false_positives(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        (
            '<p>Unit case volume represented 0.38% of total volume.</p>'
            '<p>Cost of sales per unit case increased 2.5%.</p>'
            '<p>Devices represented 7% of RRP net revenues.</p>'
            '<p>We expect tobacco shipment volume growth of 9% next year.</p>'
        ),
        'volume_growth_pct',
        'non_alcohol_unit_case_growth_pct',
        'reduced_risk_sales_mix_pct',
        'tobacco_shipment_volume_growth_pct',
    )
    assert extract_metric_evidence(item) == ()


def test_stage6b_gross_margin_requires_two_levels_and_separates_basis(
    tmp_path: Path,
) -> None:
    ambiguous = _work_item(
        tmp_path,
        '<table><tr><td>Gross margin</td><td>42%</td><td>41%</td><td>39%</td></tr></table>',
        'gross_margin_change_bps',
    )
    assert extract_metric_evidence(ambiguous) == ()

    item = _work_item(
        tmp_path,
        (
            '<p>Consolidated gross margin increased 50 basis points.</p>'
            '<p>Adjusted gross margin increased 560 basis points.</p>'
        ),
        'gross_margin_change_bps',
    )
    evidence = {
        row.scope: row.value
        for row in extract_metric_evidence(item)
        if row.status == 'ACCEPTED'
    }
    assert evidence == {
        'gaap_consolidated': pytest.approx(50.0),
        'adjusted_reported_scope': pytest.approx(560.0),
    }


def test_stage6b_comparable_sales_respectively_binding(tmp_path: Path) -> None:
    item = _work_item(
        tmp_path,
        '<p>Total and comparable sales grew 11.6% and 8.8%, respectively.</p>',
        'comparable_sales_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(8.8)


def test_stage6b_prefers_value_after_label_in_dense_metric_sequence(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<p>Q1 FY 2025 Organic Sales Growth +2% Organic Volume Growth +1% Core EPS Growth +5%.</p>',
        'volume_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(1.0)
    assert evidence[0].period_start == ''
    assert evidence[0].period_end == item.filing.report_date


def test_stage6b_rejects_incentive_weight_and_dense_unbound_slide(
    tmp_path: Path,
) -> None:
    incentive = _work_item(
        tmp_path,
        '<p>The annual incentive plan was based on EBITDA (60%) and same-store sales growth (40%).</p>',
        'comparable_sales_growth_pct',
    )
    assert extract_metric_evidence(incentive) == ()

    slide = _work_item(
        tmp_path,
        '<p>17.6% Net Sales Bridge 14.9% 2.7% 17.6% Volume Price/Mix Net Sales Growth.</p>',
        'price_mix_growth_pct',
    )
    assert extract_metric_evidence(slide) == ()

    dense_depletion_slide = _work_item(
        tmp_path,
        '<p>Consecutive quarters of depletions growth +7.5% +6.3% +8.8% '
        '+9.8% +12.0% +12.6% +6.0%.</p>',
        'alcohol_depletion_growth_pct',
    )
    assert extract_metric_evidence(dense_depletion_slide) == ()


def test_stage6b_rejects_cross_metric_slide_and_malformed_table_bindings(
    tmp_path: Path,
) -> None:
    htu_slide = _work_item(
        tmp_path,
        '<p>Higher HTU shipment volume ⎼ 6.4% Net Adjusted Revenues.</p>',
        'tobacco_shipment_volume_growth_pct',
    )
    assert extract_metric_evidence(htu_slide) == ()

    margin_bridge = _work_item(
        tmp_path,
        '<p>3.7% Price/Mix Inflation 0.2% SG&amp;A Productivity 0.3% Volume '
        '-6.8% — 4Q21 Adjusted EBITDA Margin Bridge.</p>',
        'price_mix_growth_pct',
    )
    assert extract_metric_evidence(margin_bridge) == ()

    malformed_segment_table = _work_item(
        tmp_path,
        '<table><tr><td>Net sales in the first quarter increased. Organic '
        'sales were driven by pricing and mix. Organic volume had a neutral '
        'impact on consolidated sales for the quarter and no numeric value '
        'is present in this narrative cell.</td><td>Baby, Feminine &amp; '
        'Family Care</td><td>1%</td><td>—%</td></tr></table>',
        'volume_growth_pct',
    )
    assert extract_metric_evidence(malformed_segment_table) == ()

    mixed_period_table = _work_item(
        tmp_path,
        '<table><tr><td>Fourth Quarter and Full-Year 2020 Financial '
        'Highlights</td><td>Adjusted Gross Profit Margin</td><td>36.7%</td>'
        '<td>35.2%</td><td>155 bps</td><td>37.9%</td><td>35.2%</td>'
        '<td>267 bps</td></tr></table>',
        'gross_margin_change_bps',
    )
    assert extract_metric_evidence(mixed_period_table) == ()


def test_stage6b_rejects_flattened_bridge_and_preserves_distinct_pg_scopes(
    tmp_path: Path,
) -> None:
    flattened_bridge = _work_item(
        tmp_path,
        '<p>Divestiture impact of (2.5%). 1Q24 Net Sales YoY Growth '
        'Decomposition Price 1.1% Volume/Mix 1Q24 Organic Net Sales Growth '
        '-0.4% Brand Divestitures 1Q24 Total Net Sales Growth 0.4% 1.5% '
        '-1.4%.</p>',
        'organic_revenue_growth_pct',
    )
    assert extract_metric_evidence(flattened_bridge) == ()

    grooming = _work_item(
        tmp_path,
        '<table><tr><td>Organic sales growth</td><td>Grooming</td>'
        '<td>4%</td></tr></table>',
        'organic_revenue_growth_pct',
    )
    grooming_rows = extract_metric_evidence(grooming)
    assert len(grooming_rows) == 1
    assert grooming_rows[0].scope == 'segment_grooming'

    stacked = _work_item(
        tmp_path,
        '<table><tr><td>Two-Year Stacked Growth</td><td>Organic Sales '
        'Growth</td><td>Past Two Years Stacked</td><td>10%</td>'
        '</tr></table>',
        'organic_revenue_growth_pct',
    )
    stacked_rows = extract_metric_evidence(stacked)
    assert len(stacked_rows) == 1
    assert stacked_rows[0].scope == 'two_year_stacked'


def test_stage6b_rejects_estimated_outlook_metric(tmp_path: Path) -> None:
    item = _work_item(
        tmp_path,
        '<p>Estimated 2024 Financial Impacts from Recent Disposition '
        'Transactions. Organic Net Sales approximately 3% or better growth '
        'driven by volume growth. Fiscal 2024 Outlook.</p>',
        'organic_revenue_growth_pct',
    )
    assert extract_metric_evidence(item) == ()


def test_stage6b_keeps_quantified_market_share_geographies_distinct(
    tmp_path: Path,
) -> None:
    united_states = _work_item(
        tmp_path,
        '<p>Budweiser lost 35 bps of total market share in the United '
        'States versus the prior year.</p>',
        'market_share_change_bps',
    )
    us_rows = extract_metric_evidence(united_states)
    assert len(us_rows) == 1
    assert us_rows[0].status == 'ACCEPTED'
    assert us_rows[0].scope == 'segment_united_states'

    mexico = _work_item(
        tmp_path,
        '<p>In Mexico, volume growth resulted in a market share gain of '
        '60 bps versus the prior year.</p>',
        'market_share_change_bps',
    )
    mexico_rows = extract_metric_evidence(mexico)
    assert len(mexico_rows) == 1
    assert mexico_rows[0].status == 'ACCEPTED'
    assert mexico_rows[0].scope == 'segment_mexico'


def test_stage6b_keeps_market_share_portfolio_scopes_distinct(
    tmp_path: Path,
) -> None:
    excluding_fmb = _work_item(
        tmp_path,
        '<p>Our market share excluding FMBs declined by 10 bps, while '
        'the above-core portfolio gained share according to IRI.</p>',
        'market_share_change_bps',
    )
    excluding_rows = extract_metric_evidence(excluding_fmb)
    assert len(excluding_rows) == 1
    assert excluding_rows[0].status == 'ACCEPTED'
    assert excluding_rows[0].scope == (
        'segment_excluding_flavored_malt_beverages'
    )

    mainstream = _work_item(
        tmp_path,
        '<p>Within the mainstream segment, our market share declined by '
        '15 bps in 2019.</p>',
        'market_share_change_bps',
    )
    mainstream_rows = extract_metric_evidence(mainstream)
    assert len(mainstream_rows) == 1
    assert mainstream_rows[0].status == 'ACCEPTED'
    assert mainstream_rows[0].scope == 'segment_mainstream'


def test_stage6b_geographic_scope_precedes_retail_data_provider(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<p>In the United States, total market share declined 35 bps '
        'according to IRI.</p>',
        'market_share_change_bps',
    )
    rows = extract_metric_evidence(item)
    assert len(rows) == 1
    assert rows[0].status == 'ACCEPTED'
    assert rows[0].scope == 'segment_united_states'


def test_stage6b_market_share_uses_nearest_section_geography(
    tmp_path: Path,
) -> None:
    united_states = _work_item(
        tmp_path,
        '<p>Our United States portfolio remained under pressure. Bud Light '
        'lost 35 bps of total market share. Overall market share in the '
        'United States declined 40 bps. In Canada, volume declined.</p>',
        'market_share_change_bps',
    )
    us_rows = extract_metric_evidence(united_states)
    assert any(
        row.value == pytest.approx(35.0)
        and row.scope == 'segment_united_states'
        for row in us_rows
    )

    mexico = _work_item(
        tmp_path,
        '<p>Freight costs increased. Mexico was our best performing market. '
        'We gained market share of 60 bps. Revenue grew by double digits.</p>',
        'market_share_change_bps',
    )
    mexico_rows = extract_metric_evidence(mexico)
    assert len(mexico_rows) == 1
    assert mexico_rows[0].scope == 'segment_mexico'


def test_stage6b_final_conflict_guards_are_systemic(tmp_path: Path) -> None:
    incentive = _work_item(
        tmp_path,
        '<p>The 2024 Annual Plan will be based on EBITDA (60%) and '
        'same-store sales growth (40%).</p>',
        'comparable_sales_growth_pct',
    )
    assert extract_metric_evidence(incentive) == ()

    market_share = _work_item(
        tmp_path,
        '<p>PMI volume share in nicotine pouches was 15% in the period.</p>',
        'reduced_risk_sales_mix_pct',
    )
    assert extract_metric_evidence(market_share) == ()
    valid_mix = _work_item(
        tmp_path,
        '<p>Smoke-free products accounted for 35.4% of PMI total net '
        'revenues.</p>',
        'reduced_risk_sales_mix_pct',
    )
    assert [row.value for row in extract_metric_evidence(valid_mix)] == [
        pytest.approx(35.4)
    ]

    forecast = _work_item(
        tmp_path,
        '<p>+2% to 3% Organic Net Sales Growth.</p>',
        'organic_revenue_growth_pct',
    )
    assert extract_metric_evidence(forecast) == ()
    marketing = _work_item(
        tmp_path,
        '<p>Higher marketing spend supported volume growth, a 72% '
        'year-over-year increase.</p>',
        'volume_growth_pct',
    )
    assert extract_metric_evidence(marketing) == ()


def test_stage6b_scopes_annual_and_third_party_evidence(
    tmp_path: Path,
) -> None:
    annual = _work_item(
        tmp_path,
        '<p>Organic sales growth was 4.2% compared with last year.</p>',
        'organic_revenue_growth_pct',
    )
    annual = replace(
        annual, filing=replace(annual.filing, form_type='20-F')
    )
    annual_evidence = extract_metric_evidence(annual)
    assert annual_evidence[0].scope == 'annual_reported_scope'

    third_party = _work_item(
        tmp_path,
        '<p>Circana MULO-C retail volume growth was 4.3%.</p>',
        'volume_growth_pct',
    )
    third_party_evidence = extract_metric_evidence(third_party)
    assert third_party_evidence[0].scope == 'segment_third_party_retail'
    assert third_party_evidence[0].status == 'REVIEW_REQUIRED'

    product_family = _work_item(
        tmp_path,
        '<p>Branded Salty Snacks Organic Net Sales increased 5.2%.</p>',
        'organic_revenue_growth_pct',
    )
    family_evidence = extract_metric_evidence(product_family)
    assert family_evidence[0].scope == 'segment_branded_salty_snacks'
    assert family_evidence[0].status == 'REVIEW_REQUIRED'


def test_stage6b_future_report_date_falls_back_to_filing_date(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<p>Consolidated volumes increased 6.6% from prior year.</p>',
        'volume_growth_pct',
    )
    item = replace(
        item,
        filing=replace(
            item.filing,
            form_type='6-K',
            filing_date='2019-05-09',
            report_date='2019-06-30',
        ),
    )
    evidence = extract_metric_evidence(item)
    assert evidence[0].period_end == '2019-05-09'


def test_stage6b_rejects_multi_value_growth_table_without_column_binding(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        '<table><tr><td>Organic sales growth</td><td>(11)%</td>'
        '<td>(1)%</td><td>4%</td><td>6%</td></tr></table>',
        'organic_revenue_growth_pct',
    )
    assert extract_metric_evidence(item) == ()


def test_stage6b_scope_separates_adjusted_basis_and_named_segments(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        (
            '<p>Gross margin improved 30 basis points and on an adjusted basis '
            'increased 190 basis points.</p>'
            '<p>U.S. retail comp sales growth was 6.2%.</p>'
        ),
        'gross_margin_change_bps',
        'comparable_sales_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    gross = [row for row in evidence if row.metric_name == 'gross_margin_change_bps']
    retail = [row for row in evidence if row.metric_name == 'comparable_sales_growth_pct']
    assert gross and all(row.scope.startswith('adjusted_') for row in gross)
    assert retail[0].scope == 'segment_us_retail'
    assert retail[0].status == 'REVIEW_REQUIRED'


def test_parser_conflicts_are_isolated_by_period_start_and_amendments_win() -> None:
    _, metrics = load_metric_registry(METRICS)
    metric = next(
        item for item in metrics if item.metric_id == 'organic_revenue_growth_pct'
    )
    base = {
        'ticker': 'TEST', 'metric_name': metric.metric_id,
        'period_end': '2024-12-31',
        'accepted_at': '2025-02-15T12:00:00Z', 'confidence': 0.95,
        'candidate_status': 'ACCEPTED', 'unit': 'ratio',
        'evidence_key': 'a' * 64, 'work_key': 'w',
        'accession_number': '0000000001-25-000001', 'form_type': '10-K',
        'concept_name': 'OrganicRevenueGrowth', 'status_reason': 'explicit',
        'parser_release': 'test', 'adapter_version': ADAPTER_VERSION,
        'provenance_json': '{}', 'source_document': 'filing.htm',
        'extraction_method': 'dedicated_parser:test', 'scope': 'consolidated',
    }

    class FakeConnection:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, *_args, **_kwargs):
            return iter(self.rows)

    taxonomy = {'TEST': {'cohort_id': 'beverages', 'subtype': 'non_alcohol'}}
    rows = [
        {**base, 'period_start': '2024-01-01', 'candidate_value': 2.2},
        {**base, 'period_start': '2024-10-01', 'candidate_value': 2.3,
         'evidence_key': 'b' * 64},
    ]
    observations, conflicts = specialized_metrics._parser_observations(
        FakeConnection(rows), parser_run_id=1, as_of='2025-12-31',
        metric_by_id={metric.metric_id: metric}, taxonomy=taxonomy,
        minimum_confidence=0.85,
    )
    assert len(observations) == 2
    assert conflicts == []

    rows[1] = {
        **rows[1], 'period_start': '2024-01-01', 'form_type': '10-K/A'
    }
    observations, conflicts = specialized_metrics._parser_observations(
        FakeConnection(rows), parser_run_id=1, as_of='2025-12-31',
        metric_by_id={metric.metric_id: metric}, taxonomy=taxonomy,
        minimum_confidence=0.85,
    )
    assert conflicts == []
    assert [row['numeric_value'] for row in observations] == [pytest.approx(2.3)]


def test_table_rows_accept_split_parenthesis_before_percent_cell(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        (
            '<table><tr><td>Change in active representatives</td>'
            '<td>(5</td><td>)%</td><td>(3</td><td>)%</td></tr></table>'
        ),
        'active_representative_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(-5.0)
    assert evidence[0].status == 'ACCEPTED'


def test_level_metric_plausibility_rejects_impossible_percentage(tmp_path: Path) -> None:
    item = _work_item(
        tmp_path,
        '<p>Overall digital sales were 130% of sales.</p>',
        'digital_sales_mix_pct',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].status == 'REJECTED_POLICY'


def test_sales_leaders_requires_representative_context(tmp_path: Path) -> None:
    valid = _work_item(
        tmp_path,
        '<p>Active sales leaders in our representative sales force grew 6.2%.</p>',
        'active_representative_growth_pct',
    )
    evidence = extract_metric_evidence(valid)
    assert len(evidence) == 1
    assert evidence[0].status == 'ACCEPTED'
    assert evidence[0].value == pytest.approx(6.2)

    unrelated = _work_item(
        tmp_path,
        '<p>Our sales leaders increased general leadership training by 8%.</p>',
        'active_representative_growth_pct',
    )
    assert extract_metric_evidence(unrelated) == ()

    adjacent_segment = _work_item(
        tmp_path,
        (
            '<p>Southeast Asia/Pacific segment revenue declined. During the quarter, '
            'sales force incentives drove a 2% increase in Sales Leaders.</p>'
        ),
        'active_representative_growth_pct',
    )
    evidence = extract_metric_evidence(adjacent_segment)
    assert len(evidence) == 1
    assert evidence[0].status == 'ACCEPTED'
    assert evidence[0].scope == 'reported_scope'


def test_table_rows_bind_terms_to_their_own_cells(tmp_path: Path) -> None:
    item = _work_item(
        tmp_path,
        (
            '<table><tr><th>Metric</th><th>Current</th><th>Prior</th></tr>'
            '<tr><td>Identical sales excluding fuel</td><td>(0.8)</td>'
            '<td>%</td><td>2.8</td><td>%</td></tr>'
            '<tr><td>Revenue growth</td><td>103%</td>'
            '<td>Organic volume growth</td><td>2.0%</td></tr></table>'
        ),
        'comparable_sales_growth_pct',
        'volume_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    observed = {
        row.metric_name: (row.value, row.status, row.extraction_method)
        for row in evidence
    }
    assert observed['comparable_sales_growth_pct'] == (
        pytest.approx(-0.8),
        'ACCEPTED',
        'dedicated_parser:consumer_defensive_filing_table_v2',
    )
    assert observed['volume_growth_pct'] == (
        pytest.approx(2.0),
        'ACCEPTED',
        'dedicated_parser:consumer_defensive_filing_table_v2',
    )


def test_prose_does_not_borrow_other_metric_values(tmp_path: Path) -> None:
    item = _work_item(
        tmp_path,
        (
            '<p>Operating profit increased 103%, primarily reflecting '
            'productivity savings, the organic volume growth and a '
            '10-percentage-point impact of lower commodity costs.</p>'
        ),
        'volume_growth_pct',
    )
    assert extract_metric_evidence(item) == ()


def test_table_rows_extract_unit_growth_and_derive_comparatives(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        (
            '<table>'
            '<tr><td>Revenue per unit case</td><td>5.38</td><td>5.32</td>'
            '<td>0.4</td><td>%</td></tr>'
            '<tr><td>Consolidated gross margin</td><td>40.0%</td>'
            '<td>38.0%</td></tr>'
            '<tr><td>Ending store count</td><td>2,002</td>'
            '<td>1,981</td></tr>'
            '</table>'
        ),
        'revenue_per_unit_growth_pct',
        'gross_margin_change_bps',
        'net_store_growth_pct',
    )
    evidence = extract_metric_evidence(item)
    observed = {row.metric_name: row for row in evidence}
    assert observed['revenue_per_unit_growth_pct'].value == pytest.approx(0.4)
    assert observed['gross_margin_change_bps'].value == pytest.approx(200.0)
    assert observed['net_store_growth_pct'].value == pytest.approx(
        (2002.0 / 1981.0 - 1.0) * 100.0
    )
    assert all(row.status == 'ACCEPTED' for row in observed.values())


def test_table_rows_accept_split_currency_and_value_cells(tmp_path: Path) -> None:
    item = _work_item(
        tmp_path,
        (
            '<table><tr><th>Metric</th><th>Current</th><th>Prior</th></tr>'
            '<tr><td>Average sales per square foot</td><td>$</td>'
            '<td>270</td><td>$</td><td>263</td></tr></table>'
        ),
        'sales_per_square_foot',
    )
    evidence = extract_metric_evidence(item)
    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(270.0)
    assert evidence[0].unit == 'USD_per_square_foot'
    assert evidence[0].status == 'ACCEPTED'


def test_level_mix_metric_rejects_growth_percentage_without_mix_context(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        (
            '<table><tr><td>Private label sales</td><td>100</td>'
            '<td>80</td><td>25%</td></tr></table>'
        ),
        'private_label_sales_mix_pct',
    )
    assert extract_metric_evidence(item) == ()


def test_prose_mix_metric_requires_sales_share_context(tmp_path: Path) -> None:
    manufacturing_share = _work_item(
        tmp_path,
        (
            '<p>Approximately 20% of Our Brands units sold in our stores '
            'are produced in company-owned plants.</p>'
        ),
        'private_label_sales_mix_pct',
    )
    assert extract_metric_evidence(manufacturing_share) == ()

    sales_mix = _work_item(
        tmp_path,
        '<p>Our Brands represented 25% of total company sales.</p>',
        'private_label_sales_mix_pct',
    )
    evidence = extract_metric_evidence(sales_mix)
    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(25.0)
    assert evidence[0].status == 'ACCEPTED'


def test_bullet_list_does_not_bind_comparable_sales_to_digital_mix(
    tmp_path: Path,
) -> None:
    item = _work_item(
        tmp_path,
        (
            '<p>Fiscal highlights included common stock dividends and '
            'repurchases. " Identical sales increased 2.0% '
            '" Digital sales increased 21% " Loyalty members increased 12%.'
            '</p>'
        ),
        'comparable_sales_growth_pct',
        'digital_sales_mix_pct',
    )
    evidence = extract_metric_evidence(item)
    assert [
        (row.metric_name, row.value)
        for row in evidence
        if row.status == 'ACCEPTED'
    ] == [('comparable_sales_growth_pct', pytest.approx(2.0))]

    valid_mix = _work_item(
        tmp_path,
        '<p>Digital sales represented 18% of total company sales.</p>',
        'digital_sales_mix_pct',
    )
    valid_evidence = extract_metric_evidence(valid_mix)
    assert len(valid_evidence) == 1
    assert valid_evidence[0].value == pytest.approx(18.0)


def test_advertising_derivation_tries_next_concept_when_period_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def execute(self, _sql: str, params: tuple[object, ...]):
            class Result(list):
                def fetchone(self):
                    return self[0] if self else None

            if 'fact_financial_statement_canonical' in _sql:
                return Result([{
                    'reported_value': 100.0,
                    'accepted_at': '2026-02-02T00:00:00Z',
                    'source_observation_id': 'revenue:1',
                    'accession_number': '0000000000-26-000001',
                    'source_concept': 'RevenueFromContractWithCustomer',
                }])
            return Result([{
                'numeric_value': 10.0,
                'period_start': '2025-01-01',
                'period_end': '2025-12-31',
                'accepted_at': '2026-02-01T00:00:00Z',
                'accession_number': '0000000000-26-000001',
                'taxonomy': str(params[2]),
                'unit': str(params[3]),
                'concept': str(params[1]),
                'source_observation_id': f'observation:{params[1]}',
                'raw_fact_id': 1,
            }])

    def fake_select(facts, *, as_of):
        fact = facts[0]
        period_end = (
            '2024-12-31'
            if fact.concept == 'MarketingAndAdvertisingExpense'
            else '2025-12-31'
        )
        return FlowSelection(
            status='selected',
            selected=FinancialValue(
                metric=fact.metric,
                value=fact.value,
                period_start=fact.period_start,
                period_end=period_end,
                accepted_at=fact.accepted_at,
                taxonomy=fact.taxonomy,
                currency=fact.currency,
                basis='annual',
                lineage=(str(fact.raw_fact_id),),
            ),
            quality_flags=(),
        )

    monkeypatch.setattr(specialized_metrics, 'select_safe_flow_value', fake_select)
    _, metrics = load_metric_registry(METRICS)
    metric = next(
        item for item in metrics
        if item.metric_id == 'advertising_promotion_pct_sales'
    )
    observation = specialized_metrics._advertising_ratio_observation(
        FakeConnection(),
        as_of='2026-08-14',
        feature_row={
            'ticker': 'KO',
            'revenue_ttm_usd': 100.0,
            'basis_period_end': '2025-12-31',
            'lineage_json': json.dumps({
                'basis': {'taxonomy': 'us-gaap', 'reported_currency': 'USD'}
            }),
        },
        member={
            'cohort_id': 'packaged_foods_agricultural_products',
            'subtype': 'branded_food',
        },
        metric=metric,
    )
    assert observation is not None
    assert observation['numeric_value'] == pytest.approx(10.0)
    assert json.loads(observation['lineage_json'])['advertising_concept'] == (
        'AdvertisingExpense'
    )


def test_historical_financial_derivations_are_rebuilt_point_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = [
        {
            'canonical_fact_id': 1,
            'canonical_metric': 'revenue',
            'accepted_at': '2020-02-01T12:00:00Z',
        },
        {
            'canonical_fact_id': 2,
            'canonical_metric': 'revenue',
            'accepted_at': '2021-02-01T12:00:00Z',
        },
    ]

    class FakeConnection:
        def execute(self, _sql: str, _params: tuple[object, ...]):
            return canonical

    snapshots: list[tuple[str, tuple[int, ...]]] = []

    def fake_bundle(rows, *, as_of: str, **_kwargs):
        materialized = tuple(rows)
        snapshots.append((
            as_of,
            tuple(int(row['canonical_fact_id']) for row in materialized),
        ))
        current = as_of == '2021-02-01'
        return FinancialFeatureBundle(
            values={
                'inventory_turnover': 3.5 if current else 3.0,
                'net_debt_to_ebitda': 3.7 if current else 4.0,
            },
            basis_period_end='2020-12-31' if current else '2019-12-31',
            feature_definition_version='test',
            lineage={'source_ids': [str(row['canonical_fact_id']) for row in materialized]},
            quality_status='partial',
            quality_reasons=(),
        )

    monkeypatch.setattr(
        specialized_metrics, 'build_financial_feature_bundle', fake_bundle
    )
    monkeypatch.setattr(
        specialized_metrics, '_advertising_ratio_observation',
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        specialized_metrics, '_gross_margin_change_observation',
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        specialized_metrics, '_retail_fixed_observations',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        specialized_metrics, '_financial_accepted_at',
        lambda _conn, *, period_end, **_kwargs: (
            '2021-02-01T12:00:00Z'
            if period_end == '2020-12-31'
            else '2020-02-01T12:00:00Z'
        ),
    )
    _, metrics = load_metric_registry(METRICS)
    observations = specialized_metrics._historical_financial_observations(
        FakeConnection(),
        as_of='2021-12-31',
        history_start='2019-01-02',
        metric_by_id={metric.metric_id: metric for metric in metrics},
        taxonomy={
            'GIS': {
                'cohort_id': 'packaged_foods_agricultural_products',
                'subtype': 'branded_food',
            }
        },
    )

    assert snapshots == [
        ('2020-02-01', (1,)),
        ('2021-02-01', (1, 2)),
    ]
    assert {
        (row['metric_id'], row['period_end'], row['numeric_value'])
        for row in observations
    } == {
        ('inventory_turnover', '2019-12-31', 3.0),
        ('net_debt_to_ebitda', '2019-12-31', 4.0),
        ('inventory_turnover', '2020-12-31', 3.5),
        ('net_debt_to_ebitda', '2020-12-31', 3.7),
    }
    assert all(
        json.loads(row['lineage_json'])['channel']
        == 'stage4_historical_pit_financial_feature_derivation'
        for row in observations
    )


def test_number_of_stores_derivation_uses_comparable_year_ago_count() -> None:
    class FakeConnection:
        def execute(self, _sql: str, _params: tuple[object, ...]):
            return [
                {
                    'raw_fact_id': 1,
                    'source_observation_id': 'store:current',
                    'accession_number': '0000000001-26-000001',
                    'taxonomy': 'us-gaap',
                    'concept': 'NumberOfStores',
                    'unit': 'store',
                    'period_end': '2026-05-02',
                    'accepted_at': '2026-05-28T20:08:14Z',
                    'numeric_value': 264.0,
                },
                {
                    'raw_fact_id': 2,
                    'source_observation_id': 'store:prior',
                    'accession_number': '0000000001-25-000001',
                    'taxonomy': 'us-gaap',
                    'concept': 'NumberOfStores',
                    'unit': 'warehouse_club',
                    'period_end': '2025-05-03',
                    'accepted_at': '2025-05-29T20:38:31Z',
                    'numeric_value': 255.0,
                },
                {
                    'raw_fact_id': 3,
                    'source_observation_id': 'fuel:current',
                    'accession_number': '0000000001-26-000001',
                    'taxonomy': 'us-gaap',
                    'concept': 'NumberOfStores',
                    'unit': 'gas_station',
                    'period_end': '2026-05-02',
                    'accepted_at': '2026-05-28T20:08:14Z',
                    'numeric_value': 190.0,
                },
            ]

    _, metrics = load_metric_registry(METRICS)
    metric = next(
        item for item in metrics if item.metric_id == 'net_store_growth_pct'
    )
    observation = specialized_metrics._net_store_growth_observation(
        FakeConnection(),
        as_of='2026-08-14',
        ticker='BJ',
        member={
            'cohort_id': 'consumer_staples_distribution_retail',
            'subtype': 'retail',
        },
        metric=metric,
    )
    assert observation is not None
    assert observation['numeric_value'] == pytest.approx(
        (264.0 / 255.0 - 1.0) * 100.0
    )
    lineage = json.loads(observation['lineage_json'])
    assert lineage['current']['source_observation_id'] == 'store:current'
    assert lineage['prior']['source_observation_id'] == 'store:prior'


def test_excise_tax_derivation_uses_exact_annual_gross_revenue_pairs() -> None:
    class FakeConnection:
        def execute(self, _sql: str, _params: tuple[object, ...]):
            rows = []
            for raw_id, concept, start, end, value in (
                (
                    1, 'ExciseAndSalesTaxes',
                    '2025-05-01', '2026-04-30', 1_154.0,
                ),
                (
                    2, 'RevenueFromContractWithCustomerIncludingAssessedTax',
                    '2025-05-01', '2026-04-30', 5_082.0,
                ),
                (
                    3, 'ExciseAndSalesTaxes',
                    '2024-05-01', '2025-04-30', 1_081.0,
                ),
                (
                    4, 'RevenueFromContractWithCustomerIncludingAssessedTax',
                    '2024-05-01', '2025-04-30', 5_056.0,
                ),
            ):
                rows.append({
                    'raw_fact_id': raw_id,
                    'source_observation_id': f'excise:{raw_id}',
                    'accession_number': '0000000001-26-000001',
                    'taxonomy': 'us-gaap',
                    'concept': concept,
                    'unit': 'USD',
                    'period_start': start,
                    'period_end': end,
                    'accepted_at': '2026-06-12T20:01:49Z',
                    'numeric_value': value,
                })
            return rows

    _, metrics = load_metric_registry(METRICS)
    metric = next(
        item for item in metrics if item.metric_id == 'excise_tax_impact_bps'
    )
    observation = specialized_metrics._excise_tax_burden_change_observation(
        FakeConnection(),
        as_of='2026-08-14',
        ticker='MO',
        member={
            'cohort_id': 'household_personal_tobacco',
            'subtype': 'tobacco',
        },
        metric=metric,
    )
    assert observation is not None
    assert observation['numeric_value'] == pytest.approx(
        (1154.0 / 5082.0 - 1081.0 / 5056.0) * 10_000.0
    )
    lineage = json.loads(observation['lineage_json'])
    assert lineage['current']['excise_source_observation_id'] == 'excise:1'
    assert lineage['prior']['revenue_source_observation_id'] == 'excise:4'


def test_run_observation_manifest_rejects_stale_or_missing_rows() -> None:
    digest = 'a' * 64
    run = {
        'metadata_json': json.dumps({'observation_sha256s': [digest]}),
        'accepted_observation_count': 1,
    }

    class FakeConnection:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, _sql: str, _params: tuple[object, ...]):
            return self.rows

    current = {
        'metric_id': 'volume_growth_pct',
        'ticker': 'KO',
        'period_end': '2025-12-31',
        'accepted_at': '2026-02-01T00:00:00Z',
        'observation_sha256': digest,
        'evidence_status': 'accepted_measurement_only',
        'numeric_value': 2.0,
    }
    observed = specialized_metrics._run_observations(
        FakeConnection([current]),
        as_of='2026-08-14',
        run=run,
    )
    assert specialized_metrics._measurement_tickers(observed) == {
        'volume_growth_pct': {'KO'}
    }
    with pytest.raises(RuntimeError, match='does not match stored'):
        specialized_metrics._run_observations(
            FakeConnection([]),
            as_of='2026-08-14',
            run=run,
        )


def test_stage6b_schema_is_checksummed_and_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / 'stage6b.sqlite')
    try:
        init_db(conn)
        ensure_stage6b_schema(conn)
        ensure_stage6b_schema(conn)
        rows = list(conn.execute(
            '''SELECT migration_version,migration_sha256
               FROM stage6b_schema_migrations
               ORDER BY migration_version'''
        ))
        assert [tuple(row) for row in rows] == [
            (1, STAGE6B_V1_MIGRATION_SHA256),
            (2, STAGE6B_MIGRATION_SHA256),
            (3, STAGE6B_V3_MIGRATION_SHA256),
            (4, STAGE6B_V4_MIGRATION_SHA256),
            (5, STAGE6B_V5_MIGRATION_SHA256),
            (6, STAGE6B_V6_MIGRATION_SHA256),
        ]
        columns = {
            str(item[1])
            for item in conn.execute(
                'PRAGMA table_info(fact_specialized_metric_observation)'
            )
        }
        assert {
            'confidence', 'extraction_method', 'scope', 'lineage_json',
            'observation_sha256', 'production_status', 'parser_run_id',
        } <= columns
        assert {
            'source_availability_class'
        } <= {
            str(item[1])
            for item in conn.execute(
                'PRAGMA table_info(dim_specialized_metric)'
            )
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='stage6b_historical_filing_inventory'"
        ).fetchone()[0] == 1
        table_sql = str(conn.execute(
            '''SELECT sql FROM sqlite_master
               WHERE type='table'
                 AND name='fact_specialized_metric_observation' '''
        ).fetchone()[0])
        assert 'UNIQUE(ticker, metric_id, period_end, accepted_at' not in table_sql
        upsert_source_registry(conn, load_source_registry(SOURCES))
        version, metrics = load_metric_registry(METRICS)
        upsert_metric_registry(conn, registry_version=version, metrics=metrics)
        now = utc_now()
        base = (
            'TEST', 'comparable_sales_growth_pct', '', '2024-12-31',
            '2025-02-01T12:00:00Z', 4.2, 'percent', DEFINITION_VERSION,
            'applicable', 'accepted_measurement_only', 'evidence', SOURCE_ID,
            'filing.htm', now, 0.95, 'test', 'reported_scope', '{}',
        )
        with conn:
            conn.executemany(
                '''INSERT INTO fact_specialized_metric_observation(
                       ticker,metric_id,period_start,period_end,accepted_at,
                       numeric_value,unit,definition_version,
                       applicability_status,evidence_status,evidence_key,
                       source_id,source_document,created_at,confidence,
                       extraction_method,scope,lineage_json,
                       observation_sha256,production_status,parser_run_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [
                    (*base, 'a' * 64, 'measurement_only', 1),
                    (*base, 'b' * 64, 'measurement_only', 2),
                ],
            )
        assert conn.execute(
            '''SELECT COUNT(*) FROM fact_specialized_metric_observation
               WHERE ticker='TEST' '''
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='stage6b_historical_document_snapshot'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_measurement_overlay_preserves_zero_weight_and_null_score(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / 'overlay.sqlite')
    try:
        init_db(conn)
        ensure_stage6a_schema(conn)
        ensure_stage6b_schema(conn)
        upsert_source_registry(conn, load_source_registry(SOURCES))
        version, metrics = load_metric_registry(METRICS)
        upsert_metric_registry(conn, registry_version=version, metrics=metrics)
        now = utc_now()
        component = {
            'ticker': 'TEST',
            'asof_date': '2024-12-31',
            'calibration_cohort_id': 'consumer_staples_distribution_retail',
            'component_name': 'specialized:comparable_sales_growth_pct',
            'raw_value': None,
            'normalized_value': None,
            'component_score': None,
            'component_weight': 0.0,
            'availability_status': 'not_loaded',
            'source_asof_date': None,
            'quality_status': 'not_loaded',
            'component_group': 'specialized',
            'direction': 'higher',
            'rank_requirement': 'specialized',
            'unit': 'percent',
            'definition_version': 'test',
            'contract_sha256': 'c' * 64,
            'source_id': None,
            'source_table': 'fact_specialized_metric_observation',
            'source_field': 'comparable_sales_growth_pct',
            'exclusion_reason': 'stage6b_extraction_not_promoted',
            'lineage_json': json.dumps({'metric_id': 'comparable_sales_growth_pct'}),
            'production_status': 'research_candidate',
        }
        component['component_observation_id'] = component_observation_id(component)
        with conn:
            conn.execute(
                '''INSERT INTO feature_scoring_component(
                       ticker,asof_date,component_name,raw_value,normalized_value,
                       component_score,component_weight,availability_status,
                       source_asof_date,quality_status,created_at,component_group,
                       direction,rank_requirement,unit,definition_version,
                       contract_sha256,source_id,source_table,source_field,
                       exclusion_reason,lineage_json,component_observation_id,
                       production_status
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    component['ticker'], component['asof_date'],
                    component['component_name'], None, None, None, 0.0,
                    component['availability_status'], None,
                    component['quality_status'], now, component['component_group'],
                    component['direction'], component['rank_requirement'],
                    component['unit'], component['definition_version'],
                    component['contract_sha256'], None, component['source_table'],
                    component['source_field'], component['exclusion_reason'],
                    component['lineage_json'], component['component_observation_id'],
                    component['production_status'],
                ),
            )
            canary = {
                **component,
                'component_name': 'zz_lineage_sort_canary',
                'component_group': 'market',
                'source_table': 'fixture',
                'source_field': 'fixture',
                'component_observation_id': '0' * 64,
            }
            conn.execute(
                '''INSERT INTO feature_scoring_component(
                       ticker,asof_date,component_name,raw_value,normalized_value,
                       component_score,component_weight,availability_status,
                       source_asof_date,quality_status,created_at,component_group,
                       direction,rank_requirement,unit,definition_version,
                       contract_sha256,source_id,source_table,source_field,
                       exclusion_reason,lineage_json,component_observation_id,
                       production_status
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    canary['ticker'], canary['asof_date'],
                    canary['component_name'], None, None, None, 0.0,
                    canary['availability_status'], None,
                    canary['quality_status'], now, canary['component_group'],
                    canary['direction'], canary['rank_requirement'],
                    canary['unit'], canary['definition_version'],
                    canary['contract_sha256'], None, canary['source_table'],
                    canary['source_field'], canary['exclusion_reason'],
                    canary['lineage_json'], canary['component_observation_id'],
                    canary['production_status'],
                ),
            )
            input_row = {
                'ticker': 'TEST',
                'asof_date': '2024-12-31',
                'calibration_cohort_id': 'consumer_staples_distribution_retail',
                'rank_ready_flag': 0,
                'review_reason': 'fixture',
                'source_id': 'consumer_defensive_scoring_contract',
                'feature_status': 'review_required',
                'calibration_eligible_flag': 1,
                'core_available_component_count': 0,
                'core_missing_component_count': 17,
                'core_data_quality_confidence': 0.0,
                'full_data_quality_confidence': 0.0,
                'definition_version': 'test',
                'contract_sha256': 'c' * 64,
                'lineage_json': json.dumps({
                    'component_observation_ids': [
                        component['component_observation_id'],
                        canary['component_observation_id'],
                    ]
                }),
            }
            input_row['input_observation_id'] = input_observation_id(input_row)
            conn.execute(
                '''INSERT INTO feature_scoring_input(
                       ticker,asof_date,calibration_cohort_id,rank_ready_flag,
                       review_reason,created_at,source_id,feature_status,
                       calibration_eligible_flag,core_available_component_count,
                       core_missing_component_count,core_data_quality_confidence,
                       full_data_quality_confidence,definition_version,
                       contract_sha256,lineage_json,input_observation_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    input_row['ticker'], input_row['asof_date'],
                    input_row['calibration_cohort_id'], 0, 'fixture', now,
                    input_row['source_id'], input_row['feature_status'], 1, 0, 17,
                    0.0, 0.0, 'test', 'c' * 64, input_row['lineage_json'],
                    input_row['input_observation_id'],
                ),
            )
        observation = {
            'ticker': 'TEST',
            'metric_id': 'comparable_sales_growth_pct',
            'period_start': '',
            'period_end': '2024-12-31',
            'accepted_at': '2024-12-31T12:00:00Z',
            'numeric_value': 4.2,
            'unit': 'percent',
            'definition_version': DEFINITION_VERSION,
            'applicability_status': 'applicable',
            'evidence_status': 'accepted_measurement_only',
            'evidence_key': 'e' * 64,
            'source_id': SOURCE_ID,
            'source_document': 'filing.htm',
            'confidence': 0.91,
            'extraction_method': 'test',
            'scope': 'reported_scope',
            'lineage_json': '{}',
            'production_status': 'measurement_only',
            'parser_run_id': None,
        }
        observation['observation_sha256'] = specialized_observation_sha256(
            observation
        )
        future_observation = {
            **observation,
            'period_end': '2025-01-31',
            'accepted_at': '2024-12-30T12:00:00Z',
            'numeric_value': 88.0,
            'evidence_key': 'f' * 64,
        }
        future_observation['observation_sha256'] = (
            specialized_observation_sha256(future_observation)
        )
        unsealed_observation = {
            **observation,
            'accepted_at': '2024-12-31T23:00:00Z',
            'numeric_value': 99.0,
            'evidence_key': 'd' * 64,
        }
        unsealed_observation['observation_sha256'] = (
            specialized_observation_sha256(unsealed_observation)
        )
        with conn:
            conn.executemany(
                '''INSERT INTO fact_specialized_metric_observation(
                       ticker,metric_id,period_start,period_end,accepted_at,
                       numeric_value,unit,definition_version,applicability_status,
                       evidence_status,evidence_key,source_id,source_document,
                       created_at,confidence,extraction_method,scope,lineage_json,
                       observation_sha256,production_status,parser_run_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [
                    (
                        item['ticker'], item['metric_id'], '', item['period_end'],
                        item['accepted_at'], item['numeric_value'], 'percent',
                        DEFINITION_VERSION, 'applicable',
                        'accepted_measurement_only', item['evidence_key'],
                        SOURCE_ID, 'filing.htm', now, 0.91, 'test',
                        'reported_scope', '{}', item['observation_sha256'],
                        'measurement_only', None,
                    )
                    for item in (
                        observation, future_observation, unsealed_observation
                    )
                ],
            )
            conn.execute(
                '''INSERT INTO stage6b_specialized_run(
                       asof_date,parser_run_id,adapter_version,policy_sha256,
                       source_manifest_sha256,seal_manifest_sha256,
                       ingestion_config_sha256,issuer_scope_sha256,started_at,
                       completed_at,status,inventory_document_count,
                       accepted_observation_count,metadata_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    '2024-12-31', None, 'test', 'a' * 64, 'b' * 64,
                    'c' * 64, 'd' * 64, 'e' * 64, now, now,
                    'measurement_only_complete', 1, 2,
                    json.dumps({
                        'observation_sha256s': sorted([
                            observation['observation_sha256'],
                            future_observation['observation_sha256'],
                        ])
                    }),
                ),
            )
        result = apply_stage6b_measurement_overlays(
            conn, as_of='2024-12-31'
        )
        assert result['stage6b_run_id'] == 1
        assert result['updated_component_count'] == 1
        row = conn.execute(
            '''SELECT * FROM feature_scoring_component
               WHERE ticker='TEST'
                 AND component_name='specialized:comparable_sales_growth_pct' '''
        ).fetchone()
        assert row['raw_value'] == pytest.approx(4.2)
        assert row['normalized_value'] is None
        assert row['component_score'] is None
        assert row['component_weight'] == 0.0
        assert row['availability_status'] == 'measurement_only'
        assert row['production_status'] == 'measurement_only'
        component_lineage = json.loads(row['lineage_json'])
        assert component_lineage['stage6b_overlay']['stage6b_run_id'] == 1
        assert component_lineage['stage6b_overlay']['observation_sha256'] == (
            observation['observation_sha256']
        )
        input_after = conn.execute(
            'SELECT * FROM feature_scoring_input WHERE ticker=\'TEST\''
        ).fetchone()
        assert input_after['full_data_quality_confidence'] == pytest.approx(1 / 18)
        input_lineage = json.loads(input_after['lineage_json'])
        expected_component_ids = sorted(
            str(row[0])
            for row in conn.execute(
                '''SELECT component_observation_id
                   FROM feature_scoring_component WHERE ticker='TEST' '''
            )
        )
        assert input_lineage['component_observation_ids'] == expected_component_ids
        assert input_lineage['specialized_applicable_count'] == 1
        assert input_lineage['specialized_available_count'] == 1
        assert input_lineage['specialized_missing_count'] == 0
        assert input_lineage['specialized_missing_value_policy'] == (
            'neutral_zero_contribution_no_weight_redistribution'
        )
        assert input_lineage['stage6b_run_id'] == 1
        assert input_lineage['specialized_nonapplicable_policy'] == (
            'excluded_from_denominator'
        )
        assert input_lineage['specialized_weight_activation_policy'] == (
            'shared_factor_validation_acceptance_required'
        )
    finally:
        conn.close()
