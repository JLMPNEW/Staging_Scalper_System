from __future__ import annotations

import html
import json
import math
import os
import re
import sqlite3
import sys
import time
from dataclasses import replace
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any

from dedicated_parser.contracts import (
    AdapterRegistry,
    MetricEvidence,
    MetricRequest,
    MetricRequirement,
    NormalizedFact,
    WorkItem,
)
from dedicated_parser.path_io import open_path
from dedicated_parser.semantic import parse_semantic_document

from consumer_defensive.core.metric_registry import load_metric_registry


ADAPTER_VERSION = 'consumer_defensive_specialized_metrics_v3.18'
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_METRIC_REGISTRY_PATH = (
    _PACKAGE_ROOT / 'data' / 'consumer_defensive_specialized_metric_registry.yaml'
)
_TERM_REGISTRY_PATH = (
    _PACKAGE_ROOT / 'data' / 'consumer_defensive_stage6b_extraction_terms.yaml'
)
_SUPPORTED_FORMS = (
    '10-K', '10-K/A', '10-KT', '10-Q', '10-Q/A', '10-QT',
    '8-K', '8-K/A', '20-F', '20-F/A', '40-F', '40-F/A',
    '6-K', '6-K/A',
)
_PERCENT_LEVEL_METRICS = frozenset({
    'advertising_promotion_pct_sales',
    'private_label_sales_mix_pct',
    'digital_sales_mix_pct',
    'independent_customer_mix_pct',
    'innovation_sales_mix_pct',
    'reduced_risk_sales_mix_pct',
    'branded_sales_mix_pct',
    'capacity_utilization_pct',
})
_BASIS_POINT_METRICS = frozenset({
    'gross_margin_change_bps',
    'market_share_change_bps',
    'shrink_change_bps',
    'excise_tax_impact_bps',
    'commodity_cost_impact_bps',
})
_RATIO_METRICS = frozenset({
    'inventory_turnover',
    'lease_adjusted_net_leverage',
    'fixed_charge_coverage',
    'net_debt_to_ebitda',
})
_CURRENCY_METRICS = frozenset({
    'sales_per_square_foot',
    'gross_profit_per_case',
    'agricultural_processing_margin',
})
_DECLINE_PATTERN = re.compile(
    r'\b(?:declin(?:e|ed)|decreas(?:e|ed)|fell|fall|down|contract(?:ed|ion)|'
    r'headwind|negative|lower|unfavorable)\b',
    re.IGNORECASE,
)
_INCREASE_PATTERN = re.compile(
    r'\b(?:increas(?:e|ed)|grew|growth|rose|up|expand(?:ed|sion)|'
    r'tailwind|positive|higher)\b',
    re.IGNORECASE,
)
_SEGMENT_PATTERN = re.compile(
    r'\b(?:segment|geographic region|north america|latin america|emea|'
    r'asia pacific|international business|family dollar|dollar tree|'
    r'beer brazil|latin america south|las|brazil|canada|chile|argentina|'
    r'dominican republic|battery volume|branded salty snacks)\b',
    re.IGNORECASE,
)
_TOTAL_PATTERN = re.compile(
    r'\b(?:consolidated|total company|companywide|company-wide|overall)\b',
    re.IGNORECASE,
)
_NUMBER_UNIT_PATTERN = re.compile(
    r'''(?P<open>\()?(?P<sign>[+-])?\s*
        (?P<value>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)
        \s*(?P<unit>
            percentage\s+points?|percent(?:age)?|%|
            basis\s+points?|bps?|
            turns?|times|x
        )\s*(?P<close>\))?''',
    re.IGNORECASE | re.VERBOSE,
)
_CURRENCY_PER_PATTERN = re.compile(
    r'''(?P<currency>US\$|C\$|CA\$|\$|USD|CAD|EUR|GBP)?\s*
        (?P<open>\()?(?P<sign>[+-])?\s*
        (?P<value>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)
        \s*(?P<close>\))?\s*
        (?:dollars?\s+)?per\s+
        (?P<denominator>square\s+foot|sq\.?\s*ft\.?|case|unit|head|ton|bushel)
    ''',
    re.IGNORECASE | re.VERBOSE,
)
_TABLE_NUMBER_UNIT_PATTERN = re.compile(
    r'''(?P<open>\()?(?P<sign>[+-])?\s*
        (?P<value>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)
        \s*(?P<unit>
            percentage\s+points?|percent(?:age)?|%|
            points?|pts?|
            basis\s+points?|bps?|
            turns?|times|x
        )\s*(?P<close>\))?''',
    re.IGNORECASE | re.VERBOSE,
)
_TABLE_BARE_NUMBER_PATTERN = re.compile(
    r'''(?P<open>\()?(?P<sign>[+-])?\s*
        (?P<value>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)
        \s*(?P<close>\))?''',
    re.IGNORECASE | re.VERBOSE,
)
_TABLE_LEVEL_CONTEXT_PATTERN = re.compile(
    r'\b(?:mix|penetration|accounted\s+for|represented|comprised|'
    r'constituted)\b|'
    r'\b(?:sales|revenue|volume|case|category|brand|product|portfolio)'
    r'\s+share\b|'
    r'\bshare\s+of\s+(?:total\s+|net\s+|company\s+)*'
    r'(?:sales|revenue|volume|cases|portfolio)\b|'
    r'(?:percent(?:age)?|%)\s+of\s+(?:total\s+|net\s+|company\s+)*'
    r'(?:sales|revenue|volume|cases|portfolio)\b',
    re.IGNORECASE,
)
_TABLE_EXCLUSION_PATTERN = re.compile(
    r'\b(?:sensitivity|assumption|illustrative|hypothetical|'
    r'expected\s+volume\s+growth|compound\s+annual\s+growth\s+rate|'
    r'contributions?\s+from)\b',
    re.IGNORECASE,
)
_FORWARD_LOOKING_DISCLOSURE_PATTERN = re.compile(
    r'\b(?:target(?:ed|ing|s)?|guidance|outlook|forecast(?:ed|s)?|'
    r'project(?:ed|ion|ions)?|estimat(?:e|ed|es|ing)|'
    r'expect(?:ed|s|ing)?|anticipat(?:e|ed|es|ing))\b',
    re.IGNORECASE,
)
_ADJACENT_FORWARD_HEADING_PATTERN = re.compile(
    r'\b(?:estimated\s+(?:fiscal\s+)?\d{4}\s+(?:financial\s+)?impacts?|'
    r'(?:fiscal\s+)?\d{4}\s+outlook|looking\s+ahead)\b',
    re.IGNORECASE,
)
_INEQUALITY_VALUE_PREFIX_PATTERN = re.compile(
    r'\b(?:(?:more|less|greater)\s+than|at\s+least|at\s+most|over|under'
    r')\s*$',
    re.IGNORECASE,
)
_APPROXIMATION_VALUE_PREFIX_PATTERN = re.compile(
    r'\b(?:around|approximately|about|nearly)\s*$',
    re.IGNORECASE,
)
_APPROXIMATE_DIRECT_TERMS = {
    'advertising_promotion_pct_sales': frozenset({
        'advertising and promotion', 'advertising and promotional expense',
        'advertising and sales promotion', 'brand and marketing investment',
    }),
    'average_ticket_growth_pct': frozenset({
        'average ticket', 'average transaction', 'average transaction value',
        'average order value', 'basket size', 'average basket',
    }),
    'digital_sales_mix_pct': frozenset({
        'digital sales', 'digital sales mix', 'digital penetration',
        'digitally originated sales', 'net sales for e-commerce',
        'e-commerce represented', 'e-commerce sales',
        'e-commerce penetration', 'ecommerce sales mix', 'online sales',
    }),
    'volume_growth_pct': frozenset({
        'volume growth', 'organic volume growth', 'shipment volume growth',
        'underlying volume growth', 'physical volume growth',
    }),
}
_BOUNDARY_SEPARATOR_PATTERN = re.compile(
    r'[;|\u2022\uf0b7\u25e6\u25cf\u23bc]'
)
_CHANGE_LANGUAGE_PATTERN = re.compile(
    r'\b(?:growth|grew|increase(?:d)?|decrease(?:d)?|decline(?:d)?|'
    r'change(?:d)?|up|down|higher|lower|favorable|unfavorable|'
    r'versus|vs\.?|year[- ]over[- ]year|yoy)\b',
    re.IGNORECASE,
)
_LEVEL_OR_SHARE_PATTERN = re.compile(
    r'\b(?:represented|representing|accounted\s+for|comprised|made\s+up|'
    r'constituted|corresponded\s+to|share\s+of|percent(?:age)?\s+of)\b',
    re.IGNORECASE,
)
_HISTORICAL_RATE_PATTERN = re.compile(
    r'\b(?:compound\s+annual\s+growth\s+rate|cagr|over\s+the\s+(?:last|prior)|'
    r'five[- ]year|three[- ]year)\b',
    re.IGNORECASE,
)
_ADJUSTED_GROSS_MARGIN_PATTERN = re.compile(
    r'\badjusted\s+gross\s+margin\b', re.IGNORECASE
)
_GROSS_MARGIN_COMPONENT_PATTERN = re.compile(
    r'\b(?:impact(?:ed|ing)?|contribution|benefit|headwind|tailwind|'
    r'foreign\s+currenc(?:y|ies)|favorable|unfavorable)\b.{0,90}'
    r'\b(?:adjusted\s+)?gross\s+margin\b|'
    r'\b(?:adjusted\s+)?gross\s+margin\b.{0,90}'
    r'\b(?:impact(?:ed|ing)?|contribution|benefit|headwind|tailwind|'
    r'foreign\s+currenc(?:y|ies)|favorable|unfavorable)\b',
    re.IGNORECASE,
)
_STRONG_TABLE_CONFLICT_SELECTIONS = frozenset({
    'explicit_percentage_table_row',
    'explicit_growth_percentage_points_table_row',
    'explicit_basis_point_table_row',
    'explicit_percentage_point_table_row',
    'explicit_ratio_table_row',
    'explicit_currency_per_unit_table_row',
})
_RATIO_LIMIT_PATTERN = re.compile(
    r'\b(?:maximum|minimum|required|requirement|must\s+not\s+exceed|'
    r'not\s+greater\s+than|not\s+less\s+than|covenant\s+limit|'
    r'permitted\s+ratio)\b',
    re.IGNORECASE,
)
_RATIO_ACTUAL_PATTERN = re.compile(
    r'\b(?:actual|was|were|is|equaled|measured|reported|stood\s+at)\b',
    re.IGNORECASE,
)
_TABLE_LEVEL_DERIVABLE_GROWTH = frozenset({
    'volume_growth_pct',
    'alcohol_depletion_growth_pct',
    'non_alcohol_unit_case_growth_pct',
    'case_volume_growth_pct',
    'active_customer_growth_pct',
    'active_representative_growth_pct',
    'tobacco_shipment_volume_growth_pct',
    'production_volume_growth_pct',
    'net_store_growth_pct',
})
_TABLE_LEVEL_CONTEXT_REQUIRED = frozenset({
    'advertising_promotion_pct_sales',
    'private_label_sales_mix_pct',
    'digital_sales_mix_pct',
    'independent_customer_mix_pct',
    'innovation_sales_mix_pct',
    'reduced_risk_sales_mix_pct',
    'branded_sales_mix_pct',
})
_SCOPE_QUALIFIED_SEGMENT_METRICS = frozenset({
    'agricultural_processing_margin',
    'case_volume_growth_pct',
    'comparable_sales_growth_pct',
    'market_share_change_bps',
    'organic_revenue_growth_pct',
    'price_mix_growth_pct',
    'production_volume_growth_pct',
    'sales_per_square_foot',
    'tobacco_shipment_volume_growth_pct',
    'volume_growth_pct',
})


class _VisibleTextParser(HTMLParser):
    _BLOCK_TAGS = frozenset({
        'address', 'article', 'aside', 'blockquote', 'br', 'caption', 'div',
        'dd', 'dl', 'dt', 'figcaption', 'footer', 'h1', 'h2', 'h3', 'h4',
        'h5', 'h6', 'header', 'hr', 'li', 'main', 'p', 'pre', 'section',
        'table', 'td', 'th', 'tr', 'ul',
    })
    _IGNORED_TAGS = frozenset({'script', 'style', 'noscript'})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0
        self.table_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        lowered = tag.lower()
        if lowered in self._IGNORED_TAGS:
            self.ignored_depth += 1
        elif lowered == 'table':
            self.table_depth += 1
            if not self.ignored_depth:
                self.parts.append('\n')
        elif (
            lowered in self._BLOCK_TAGS
            and not self.ignored_depth
            and not self.table_depth
        ):
            self.parts.append('\n')

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._IGNORED_TAGS:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        elif lowered == 'table':
            self.table_depth = max(0, self.table_depth - 1)
            if not self.ignored_depth:
                self.parts.append('\n')
        elif (
            lowered in self._BLOCK_TAGS
            and not self.ignored_depth
            and not self.table_depth
        ):
            self.parts.append('\n')

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth and not self.table_depth:
            self.parts.append(data)


class _TableTextParser(HTMLParser):
    _IGNORED_TAGS = frozenset({'script', 'style', 'noscript'})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[tuple[str, ...], ...]] = []
        self.table_depth = 0
        self.ignored_depth = 0
        self.in_row = False
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.row: list[str] = []
        self.table: list[tuple[str, ...]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        lowered = tag.lower()
        if lowered in self._IGNORED_TAGS:
            self.ignored_depth += 1
        elif lowered == 'table':
            self.table_depth += 1
            if self.table_depth == 1:
                self.table = []
        elif self.table_depth == 1 and lowered == 'tr':
            self.in_row = True
            self.row = []
        elif (
            self.table_depth == 1
            and self.in_row
            and lowered in {'td', 'th'}
        ):
            self.in_cell = True
            self.cell_parts = []
        elif self.in_cell and lowered == 'br':
            self.cell_parts.append(' ')

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._IGNORED_TAGS:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        elif (
            lowered in {'td', 'th'}
            and self.table_depth == 1
            and self.in_cell
        ):
            self.row.append(_normalize_visible_text(''.join(self.cell_parts)))
            self.in_cell = False
            self.cell_parts = []
        elif lowered == 'tr' and self.table_depth == 1 and self.in_row:
            if any(self.row):
                self.table.append(tuple(self.row))
            self.in_row = False
            self.row = []
        elif lowered == 'table' and self.table_depth:
            if self.table_depth == 1 and self.table:
                self.tables.append(tuple(self.table))
                self.table = []
            self.table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_cell and not self.ignored_depth:
            self.cell_parts.append(data)


def _load_terms() -> tuple[
    str,
    dict[str, tuple[str, ...]],
    dict[tuple[str, str], tuple[frozenset[str], frozenset[str]]],
]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError('PyYAML is required for the Consumer Defensive adapter.') from exc
    payload = yaml.safe_load(_TERM_REGISTRY_PATH.read_text(encoding='utf-8')) or {}
    raw_metrics = payload.get('metrics')
    if not isinstance(raw_metrics, dict):
        raise ValueError('Consumer Defensive disclosure terms must contain metrics.')
    terms: dict[str, tuple[str, ...]] = {}
    for metric_name, raw_terms in raw_metrics.items():
        if not isinstance(raw_terms, list) or not raw_terms:
            raise ValueError(f'Metric {metric_name!r} requires disclosure terms.')
        normalized = tuple(
            str(term).strip().lower() for term in raw_terms if str(term).strip()
        )
        if not normalized:
            raise ValueError(f'Metric {metric_name!r} has no usable disclosure terms.')
        terms[str(metric_name)] = normalized
    context_rules: dict[
        tuple[str, str], tuple[frozenset[str], frozenset[str]]
    ] = {}
    raw_contextual = payload.get('contextual_aliases') or {}
    if not isinstance(raw_contextual, dict):
        raise ValueError('contextual_aliases must be a mapping.')
    for metric_name, raw_rules in raw_contextual.items():
        metric = str(metric_name)
        if metric not in terms or not isinstance(raw_rules, list):
            raise ValueError(f'Invalid contextual rules for {metric!r}.')
        additions: list[str] = list(terms[metric])
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                raise ValueError(f'Contextual rule for {metric!r} must be an object.')
            term = str(raw_rule.get('term') or '').strip().lower()
            required = frozenset(
                str(value).strip().lower()
                for value in (raw_rule.get('require_any') or [])
                if str(value).strip()
            )
            excluded = frozenset(
                str(value).strip().lower()
                for value in (raw_rule.get('exclude_any') or [])
                if str(value).strip()
            )
            if not term or not required:
                raise ValueError(
                    f'Contextual alias for {metric!r} requires term and require_any.'
                )
            key = (metric, term)
            if key in context_rules:
                raise ValueError(f'Duplicate contextual alias: {key!r}')
            context_rules[key] = (required, excluded)
            additions.append(term)
        terms[metric] = tuple(dict.fromkeys(additions))
    return str(payload.get('parser_version') or ''), terms, context_rules


_REGISTRY_VERSION, _METRICS = load_metric_registry(_METRIC_REGISTRY_PATH)
_TERM_VERSION, _METRIC_TERMS, _CONTEXT_RULES = _load_terms()
_METRIC_TERMS['active_representative_growth_pct'] = tuple(sorted({
    *_METRIC_TERMS['active_representative_growth_pct'],
    'sales leaders',
}))
_METRIC_BY_ID = {metric.metric_id: metric for metric in _METRICS}
if set(_METRIC_BY_ID) != set(_METRIC_TERMS):
    raise RuntimeError('Consumer Defensive metric and term registries differ.')
_TERM_TO_METRICS: dict[str, set[str]] = {}
for _metric_name, _terms in _METRIC_TERMS.items():
    for _term in _terms:
        _TERM_TO_METRICS.setdefault(_term.lower(), set()).add(_metric_name)
_ALL_TERM_PATTERN = re.compile(
    r'(?<![a-z0-9])(?:'
    + '|'.join(
        re.escape(term)
        for term in sorted(_TERM_TO_METRICS, key=lambda value: (-len(value), value))
    )
    + r')(?![a-z0-9])',
    re.IGNORECASE,
)


def _term_context_allowed(
    metric_name: str,
    term: str,
    context: str,
) -> bool:
    rule = _CONTEXT_RULES.get((metric_name, term.lower()))
    if rule is None:
        return True
    required, excluded = rule
    lowered = context.lower()
    return (
        any(token in lowered for token in required)
        and not any(token in lowered for token in excluded)
    )


def _metric_context_allowed(metric_name: str, context: str) -> bool:
    lowered = context.lower()
    if metric_name == 'traffic_growth_pct':
        if re.search(
            r'\bpoint\s+of\s+sale\s+transactions?\b', lowered
        ) or not _CHANGE_LANGUAGE_PATTERN.search(context):
            return False
    if (
        metric_name == 'livestock_feed_cost_change_pct'
        and not _CHANGE_LANGUAGE_PATTERN.search(context)
    ):
        return False
    if metric_name == 'volume_growth_pct' and re.search(
        r'\bnet\s+sales\b.{0,140}\b(?:reduced|decline|decreased)\b'
        r'.{0,100}\bvolume\s*/\s*mix\b',
        lowered,
    ):
        return False
    if metric_name == 'reduced_risk_sales_mix_pct':
        if re.search(
            r'\b(?:proposal|weighting|incentive|performance\s+metric|'
            r'compensation)\b', lowered
        ) or not re.search(
            r'\b(?:sales|revenue|volume|mix|share|represented|'
            r'accounted\s+for)\b', lowered
        ):
            return False
    if metric_name == 'market_share_change_bps' and re.search(
        r'percentage\s+points?\s+of\s+(?:the\s+)?'
        r'(?:volume|sales|revenue)\s+(?:decline|increase|change)|'
        r'percentage\s+points?\s+of\s+(?:the\s+)?(?:decline|increase|change)'
        r'\s+(?:reflect|reflects|reflecting)\s+market\s+share',
        lowered,
    ):
        return False
    if metric_name == 'price_mix_growth_pct' and re.search(
        r'\bgross\s+profit\s+margin\b.{0,120}\b(?:pricing|price)'
        r'\s+initiatives?\b',
        lowered,
    ):
        return False
    if re.search(
        r'\bnet\s+sales\s+(?:yoy\s+)?growth\s+decomposition\b',
        lowered,
    ):
        # Slide/chart text extraction does not retain the spatial mapping
        # between labels and values in these bridge diagrams. Use a retained
        # table or explicit narrative disclosure instead.
        return False
    if _FORWARD_LOOKING_DISCLOSURE_PATTERN.search(context):
        return False
    if re.search(
        r'\b(?:annual\s+incentive\s+plan|compensation\s+committee|'
        r'annual\s+plan|base\s+salary|payout\s+at\s+target|'
        r'performance[- ]based\s+award|named\s+executive\s+officers?|neos?)\b',
        lowered,
    ):
        return False
    if re.search(r'\bwill\s+be\s+based\s+on\b', lowered):
        return False
    if metric_name.endswith('_growth_pct') and re.search(
        r'[+-]?\d+(?:\.\d+)?\s*%\s+to\s+'
        r'[+-]?\d+(?:\.\d+)?\s*%',
        lowered,
    ):
        return False
    if metric_name.endswith('_growth_pct') and _HISTORICAL_RATE_PATTERN.search(context):
        return False
    if metric_name == 'organic_revenue_growth_pct' and re.search(
        r'\b(?:earnings\s+per\s+share|diluted\s+eps|eps|'
        r'(?:net\s+)?revenue\s+per\s+(?:unit|case|hectoliter|lit(?:er|re)))\b',
        lowered,
    ):
        return False
    if metric_name == 'price_mix_growth_pct' and re.search(
        r'\b(?:adjusted\s+)?(?:ebitda|operating\s+income|gross)\s+'
        r'margin\s+(?:bridge|decomposition)\b',
        lowered,
    ):
        return False
    if metric_name == 'price_mix_growth_pct' and re.search(
        r'\b(?:gross profit|operating income|net income|ebitda)\b[^.]{0,100}'
        r'\d+(?:\.\d+)?\s*(?:%|percent)[^.]{0,30}'
        r'\b(?:on|from|due to)\s+pricing\b',
        lowered,
    ):
        return False
    if metric_name in {
        'volume_growth_pct',
        'non_alcohol_unit_case_growth_pct',
        'alcohol_depletion_growth_pct',
        'case_volume_growth_pct',
        'tobacco_shipment_volume_growth_pct',
        'production_volume_growth_pct',
    }:
        if re.search(r'\bcost\s+of\s+sales\s+per\s+unit\s+case\b', lowered):
            return False
        if re.search(r'\bmarketing\s+spend\b', lowered):
            return False
        if re.search(
            r'\b\d+(?:\.\d+)?\s*%\s+of\s+(?:our\s+)?(?:sales\s+)?volume\b|'
            r'\bsales\s+volume\s+(?:is|are|was|were)\s+derived\b',
            lowered,
        ):
            return False
        if _LEVEL_OR_SHARE_PATTERN.search(context) and not _CHANGE_LANGUAGE_PATTERN.search(
            context
        ):
            return False
        if metric_name == 'tobacco_shipment_volume_growth_pct' and re.search(
            r'\b(?:%|percent)\s+of\s+(?:total\s+)?(?:shipment|volume)',
            lowered,
        ):
            return False
    if metric_name == 'reduced_risk_sales_mix_pct':
        if re.search(r'\b(?:device|devices)\b', lowered) and re.search(
            r'\b(?:rrp|reduced[- ]risk)\s+(?:net\s+)?revenue', lowered
        ):
            return False
        if not re.search(
            r"\b(?:total|company|consolidated|pmi(?:\u2019s|'s)?)\s+"
            r'(?:net\s+)?(?:sales|revenue|revenues)\b',
            lowered,
        ):
            return False
    if metric_name == 'comparable_sales_growth_pct' and re.search(
        r'\b(?:adjusted\s+)?(?:diluted\s+)?eps\s+growth\b', lowered
    ):
        return False
    if metric_name == 'traffic_growth_pct' and re.search(
        r'\b(?:tax|taxation|jurisdiction|subsidiar(?:y|ies)|securities|'
        r'merger|acquisition)\b',
        lowered,
    ) and not re.search(
        r'\b(?:customer|store|retail|shopper|visit|foot\s+traffic|'
        r'number\s+of\s+transactions)\b',
        lowered,
    ):
        return False
    if (
        metric_name == 'gross_margin_change_bps'
        and _GROSS_MARGIN_COMPONENT_PATTERN.search(context)
    ):
        return False
    return True


def _period_bounds(item: WorkItem, _context: str) -> tuple[str, str]:
    fallback = item.filing.report_date or item.filing.filing_date
    # Flattened filing prose and tables routinely contain current, YTD, and
    # comparative dates together. A nearby date therefore cannot safely bind
    # the selected value without retained row/column coordinates. Preserve
    # the filing report-date proxy instead of fabricating cross-filing joins.
    if item.filing.filing_date and fallback > item.filing.filing_date:
        fallback = item.filing.filing_date
    return '', fallback


def _filing_scope(item: WorkItem, scope: str) -> str:
    base_form = item.filing.form_type.upper().removesuffix('/A')
    if base_form in {'20-F', '40-F'}:
        return f'annual_{scope}'
    return scope


def _concept_patterns(metric_name: str) -> tuple[str, ...]:
    patterns: list[str] = []
    for term in _METRIC_TERMS[metric_name]:
        words = re.findall(r'[a-z0-9]+', term)
        if words:
            patterns.append('.*'.join(re.escape(word.title()) for word in words))
    return tuple(sorted(set(patterns)))


_SOURCE_METRICS = tuple(
    MetricRequest(metric.metric_id, _concept_patterns(metric.metric_id))
    for metric in _METRICS
)


def get_registry() -> AdapterRegistry:
    return AdapterRegistry(
        model_family='consumer_defensive',
        adapter_version=ADAPTER_VERSION,
        supported_forms=_SUPPORTED_FORMS,
        source_metrics=_SOURCE_METRICS,
        metric_dependencies={},
        metric_requirements={
            metric.metric_id: MetricRequirement(metric.metric_id)
            for metric in _METRICS
        },
        metric_freshness_days={
            metric.metric_id: 550 for metric in _METRICS
        },
        document_keywords=tuple(
            sorted({term for terms in _METRIC_TERMS.values() for term in terms})
        ),
    )


def select_tickers(conn: sqlite3.Connection, asof_date: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            '''SELECT DISTINCT t.ticker
               FROM dim_consumer_defensive_taxonomy t
               JOIN dim_universe_membership m
                 ON m.ticker=t.ticker AND m.model_family=t.model_family
               WHERE t.model_family='consumer_defensive'
                 AND m.start_date<=?
                 AND COALESCE(m.end_date,'9999-12-31')>=?
               ORDER BY t.ticker''',
            (asof_date, asof_date),
        )
    ]


def _normalize_visible_text(value: str) -> str:
    value = html.unescape(value).replace('\xa0', ' ').replace('\u2212', '-')
    value = re.sub(
        r'(?<!\d)(\d{1,3}),(\d{1,2})(?=\s*(?:%|percent|bps|basis\s+points?))',
        r'\1.\2',
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r'[ \t\f\v]+', ' ', value)
    value = re.sub(r'\r?\n\s*', '\n', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()


def _document_content(
    raw: bytes,
    *,
    suffix: str,
    source_document: str = '',
    enable_pdf_ocr: bool = False,
    max_pdf_pages: int = 250,
    max_pdf_bytes: int = 50_000_000,
    pdf_timeout_seconds: float = 30.0,
    max_ocr_pages: int = 12,
    ocr_dpi: int = 144,
    ocr_page_timeout_seconds: float = 8.0,
    max_ocr_pixels_per_page: int = 20_000_000,
) -> tuple[
    str,
    tuple[tuple[tuple[str, ...], ...], ...],
    dict[str, Any],
]:
    if suffix.lower() == '.pdf':
        if len(raw) > max_pdf_bytes:
            raise ValueError(
                f'PDF exceeds max_pdf_bytes={max_pdf_bytes}: {len(raw)}'
            )
        started = time.monotonic()
        page_text: list[str] = []
        page_count = 0
        native_engine = 'pypdf'
        pypdf_error: Exception | None = None
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except ImportError:
            PdfReader = None
        if PdfReader is not None:
            try:
                reader = PdfReader(BytesIO(raw), strict=False)
                page_count = len(reader.pages)
                if page_count > max_pdf_pages:
                    raise ValueError(
                        f'PDF exceeds max_pdf_pages={max_pdf_pages}: {page_count}'
                    )
                for page in reader.pages:
                    if time.monotonic() - started > pdf_timeout_seconds:
                        raise TimeoutError('PDF native text extraction timed out.')
                    page_text.append(page.extract_text() or '')
            except (TimeoutError, ValueError):
                raise
            except Exception as exc:  # library-specific malformed PDF errors
                pypdf_error = exc
                page_text = []
        if PdfReader is None or pypdf_error is not None:
            native_engine = 'pymupdf'
            try:
                import pymupdf  # type: ignore[import-not-found]
            except ImportError as exc:
                if pypdf_error is not None:
                    raise RuntimeError(
                        'PDF native text extraction failed with pypdf and '
                        'PyMuPDF is unavailable.'
                    ) from pypdf_error
                raise RuntimeError(
                    'PDF native extraction requires pypdf or PyMuPDF.'
                ) from exc
            try:
                with pymupdf.open(stream=raw, filetype='pdf') as document:
                    page_count = len(document)
                    if page_count > max_pdf_pages:
                        raise ValueError(
                            f'PDF exceeds max_pdf_pages={max_pdf_pages}: '
                            f'{page_count}'
                        )
                    for page in document:
                        if time.monotonic() - started > pdf_timeout_seconds:
                            raise TimeoutError(
                                'PDF native text extraction timed out.'
                            )
                        page_text.append(page.get_text('text') or '')
            except (TimeoutError, ValueError):
                raise
            except Exception as exc:  # library-specific malformed PDF errors
                detail = (
                    f'pypdf={type(pypdf_error).__name__}; '
                    if pypdf_error is not None
                    else ''
                )
                raise RuntimeError(
                    'PDF native text extraction failed: '
                    f'{detail}pymupdf={type(exc).__name__}'
                ) from exc
        decoded = _normalize_visible_text('\n\n'.join(page_text))
        if decoded or not enable_pdf_ocr:
            if not decoded:
                raise RuntimeError('PDF contains no extractable native text.')
            return decoded, (), {
                'document_method': 'pdf_native_text',
                'native_pdf_engine': native_engine,
                'ocr_used': False,
                'page_count': page_count,
            }
        if page_count > max_ocr_pages:
            raise ValueError(
                f'PDF OCR exceeds max_ocr_pages={max_ocr_pages}: {page_count}'
            )
        try:
            import pymupdf  # type: ignore[import-not-found]
            import pytesseract  # type: ignore[import-not-found]
            from PIL import Image  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError('PDF OCR dependencies are unavailable.') from exc
        runtime_binding = 'system_path'
        configured_tesseract = str(
            os.environ.get('TESSERACT_CMD') or ''
        ).strip()
        runtime_candidates = []
        if configured_tesseract:
            runtime_candidates.append(('environment', Path(configured_tesseract)))
        environment_root = Path(sys.executable).resolve().parent
        runtime_candidates.extend((
            (
                'conda_environment',
                environment_root / 'Library' / 'bin' / 'tesseract.exe',
            ),
            ('python_environment', environment_root / 'bin' / 'tesseract'),
        ))
        command_module = getattr(pytesseract, 'pytesseract', None)
        for binding, candidate in runtime_candidates:
            if candidate.is_file():
                if command_module is not None:
                    command_module.tesseract_cmd = str(candidate)
                runtime_binding = binding
                break
        if configured_tesseract and runtime_binding != 'environment':
            raise RuntimeError('Configured Tesseract executable is unavailable.')
        tessdata_binding = 'runtime_default'
        tessdata_dir: Path | None = None
        configured_tessdata = str(
            os.environ.get('TESSDATA_PREFIX') or ''
        ).strip()
        tessdata_candidates: list[tuple[str, Path]] = []
        if configured_tessdata:
            tessdata_candidates.append((
                'environment', Path(configured_tessdata)
            ))
        tessdata_candidates.extend((
            ('conda_environment', environment_root / 'share' / 'tessdata'),
            (
                'conda_library',
                environment_root / 'Library' / 'share' / 'tessdata',
            ),
        ))
        for binding, candidate in tessdata_candidates:
            if (candidate / 'eng.traineddata').is_file():
                tessdata_binding = binding
                tessdata_dir = candidate
                break
        if configured_tessdata and tessdata_binding != 'environment':
            raise RuntimeError(
                'Configured Tesseract language-data directory is unavailable.'
            )
        if tessdata_dir is not None:
            os.environ['TESSDATA_PREFIX'] = str(tessdata_dir)
        tesseract_config = '--psm 6'
        try:
            ocr_version = str(pytesseract.get_tesseract_version())
        except Exception as exc:
            raise RuntimeError('PDF OCR runtime is unavailable.') from exc
        ocr_text: list[str] = []
        ocr_page_indices: list[int] = []
        scale = float(ocr_dpi) / 72.0
        with pymupdf.open(stream=raw, filetype='pdf') as document:
            if len(document) > max_ocr_pages:
                raise ValueError(
                    f'PDF OCR exceeds max_ocr_pages={max_ocr_pages}: '
                    f'{len(document)}'
                )
            for page_index, page in enumerate(document):
                if time.monotonic() - started > pdf_timeout_seconds:
                    raise TimeoutError('PDF OCR timed out.')
                pixel_width = max(1, math.ceil(float(page.rect.width) * scale))
                pixel_height = max(1, math.ceil(float(page.rect.height) * scale))
                pixel_count = pixel_width * pixel_height
                if pixel_count > max_ocr_pixels_per_page:
                    raise ValueError(
                        'PDF OCR page exceeds max_ocr_pixels_per_page='
                        f'{max_ocr_pixels_per_page}: page={page_index + 1} '
                        f'pixels={pixel_count}'
                    )
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale), alpha=False
                )
                image = Image.open(BytesIO(pixmap.tobytes('png')))
                try:
                    extracted = pytesseract.image_to_string(
                        image,
                        timeout=ocr_page_timeout_seconds,
                        config=tesseract_config,
                    )
                except RuntimeError as exc:
                    if 'timeout' in str(exc).casefold():
                        raise TimeoutError(
                            f'PDF OCR page {page_index + 1} timed out.'
                        ) from exc
                    raise RuntimeError(
                        f'PDF OCR page {page_index + 1} failed.'
                    ) from exc
                ocr_text.append(extracted)
                ocr_page_indices.append(page_index + 1)
        decoded = _normalize_visible_text('\n\n'.join(ocr_text))
        if not decoded:
            raise RuntimeError('PDF OCR returned no text.')
        return decoded, (), {
            'document_method': 'pdf_ocr',
            'native_pdf_engine': native_engine,
            'ocr_used': True,
            'ocr_engine': 'tesseract',
            'ocr_engine_version': ocr_version,
            'ocr_runtime_binding': runtime_binding,
            'ocr_tessdata_binding': tessdata_binding,
            'ocr_dpi': ocr_dpi,
            'ocr_page_count': len(ocr_page_indices),
            'ocr_page_indices': ocr_page_indices,
            'page_count': page_count,
        }
    decoded = raw.decode('utf-8', errors='replace')
    tables: tuple[tuple[tuple[str, ...], ...], ...] = ()
    if suffix.lower() in {'.htm', '.html', '.xhtml', '.xml'}:
        semantic = parse_semantic_document(
            decoded, source_document=source_document or 'SEC document'
        )
        decoded = '\n'.join(
            block.search_text
            for block in semantic.blocks
            if block.kind != 'table_row' and block.search_text
        )
        table_groups: dict[int, list[tuple[str, ...]]] = {}
        for block in semantic.table_rows:
            prefix = ' | '.join(
                value for value in (
                    *block.section_path,
                    block.preamble_text,
                    *block.header_cells,
                ) if value
            )
            cells = block.cells
            if prefix:
                cells = (prefix, *cells)
            table_groups.setdefault(int(block.table_id or 0), []).append(cells)
        tables = tuple(
            tuple(table_groups[key]) for key in sorted(table_groups)
        )
    return _normalize_visible_text(decoded), tables, {
        'document_method': (
            'semantic_html'
            if suffix.lower() in {'.htm', '.html', '.xhtml', '.xml'}
            else 'plain_text'
        ),
        'ocr_used': False,
    }


def _document_text(raw: bytes, *, suffix: str) -> str:
    return _document_content(raw, suffix=suffix)[0]


def _context(text: str, start: int, end: int) -> tuple[str, int]:
    left = max(
        text.rfind('\n', 0, max(0, start - 450)),
        text.rfind('. ', 0, max(0, start - 450)),
        start - 450,
    )
    right_candidates = [
        value for value in (
            text.find('\n', min(len(text), end + 450)),
            text.find('. ', min(len(text), end + 450)),
        ) if value >= 0
    ]
    right = min(right_candidates) + 1 if right_candidates else min(len(text), end + 450)
    raw = text[max(0, left):right]
    prefix = text[max(0, left):start]
    context = re.sub(r'\s+', ' ', raw).strip()[:1500]
    prefix_length = len(re.sub(r'\s+', ' ', prefix).strip())
    return context, context.lower().find(
        text[start:end].lower(),
        max(0, prefix_length - (end - start) - 4),
    )


def _signed_value(match: re.Match[str], context: str) -> float | None:
    try:
        value = float(match.group('value').replace(',', ''))
    except (AttributeError, ValueError):
        return None
    explicit_sign = match.groupdict().get('sign')
    if explicit_sign == '-':
        value = -value
    elif explicit_sign == '+':
        pass
    elif match.groupdict().get('open') and match.groupdict().get('close'):
        value = -value
    else:
        before = context[max(0, match.start() - 100):match.start()]
        decline_matches = list(_DECLINE_PATTERN.finditer(before))
        increase_matches = list(_INCREASE_PATTERN.finditer(before))
        if decline_matches and (
            not increase_matches
            or decline_matches[-1].start() > increase_matches[-1].start()
        ):
            value = -value
    return value if math.isfinite(value) else None


def _sentence_bounds(context: str, term_start: int, term_end: int) -> tuple[int, int]:
    sentence_left_values = [0]
    prior_period = context.rfind('. ', 0, term_start)
    while (
        prior_period >= 0
        and context[max(0, prior_period - 3):prior_period + 1].upper()
        in {'U.S.', 'U.K.'}
    ):
        prior_period = context.rfind('. ', 0, prior_period)
    prior_newline = context.rfind('\n', 0, term_start)
    if prior_period >= 0:
        sentence_left_values.append(prior_period + 2)
    if prior_newline >= 0:
        sentence_left_values.append(prior_newline + 1)
    sentence_left = max(sentence_left_values)
    sentence_right_values = [
        position for position in (
            context.find('. ', term_end),
            context.find('\n', term_end),
        ) if position >= 0
    ]
    sentence_right = (
        min(sentence_right_values) + 1
        if sentence_right_values
        else len(context)
    )
    return sentence_left, sentence_right


def _parse_candidate(
    metric_name: str,
    context: str,
    term_start: int,
    term_end: int,
    *,
    company_currency: str,
) -> tuple[float, str, str] | None:
    sentence_left, sentence_right = _sentence_bounds(
        context, term_start, term_end
    )
    sentence = context[sentence_left:sentence_right]
    if metric_name == 'comparable_sales_growth_pct' and re.search(
        r'\btotal(?:\s+sales)?\s+and\s+comparable\s+sales\b',
        sentence,
        re.IGNORECASE,
    ) and re.search(r'\brespectively\b', sentence, re.IGNORECASE):
        parallel = list(_NUMBER_UNIT_PATTERN.finditer(sentence))
        if len(parallel) >= 2:
            selected = parallel[1]
            value = _signed_value(selected, sentence)
            if value is not None:
                return value, 'percent', selected.group(0)
    if metric_name == 'traffic_growth_pct':
        preceding = [
            match for match in _NUMBER_UNIT_PATTERN.finditer(sentence)
            if sentence_left + match.end() <= term_start
        ]
        following = next((
            match for match in _NUMBER_UNIT_PATTERN.finditer(sentence)
            if sentence_left + match.start() >= term_end
        ), None)
        if preceding and following is not None:
            selected = preceding[-1]
            selected_end = sentence_left + selected.end()
            following_start = sentence_left + following.start()
            following_end = sentence_left + following.end()
            prior_bridge = context[selected_end:term_start]
            next_bridge = context[term_end:following_start]
            next_label = context[following_end:min(len(context), following_end + 80)]
            raw_unit = re.sub(r'\s+', ' ', selected.group('unit').lower())
            if (
                len(prior_bridge) <= 8
                and not prior_bridge.strip()
                and len(next_bridge) <= 24
                and not next_bridge.strip()
                and raw_unit in {'%', 'percent', 'percentage'}
                and re.search(
                    r'\b(?:e-?comm(?:erce)?|digitally[- ]enabled|digital sales)\b',
                    next_label,
                    re.IGNORECASE,
                )
            ):
                value = _signed_value(selected, sentence)
                if value is not None:
                    return value, 'percent', selected.group(0)
    if (
        metric_name in _RATIO_METRICS
        and _RATIO_LIMIT_PATTERN.search(context)
        and not _RATIO_ACTUAL_PATTERN.search(context)
    ):
        return None
    candidates: list[tuple[int, int, float, str, str]] = []

    def candidate_rank(match: re.Match[str]) -> tuple[int, int]:
        if match.end() <= term_start:
            distance = term_start - match.end()
            bridge = context[match.end():term_start]
            preceding_measurement = (
                re.search(
                    r'\b(?:year[- ]over[- ]year|yoy|increase|increased|'
                    r'decrease|decreased|decline|declined|change|changed)\b',
                    bridge,
                    re.IGNORECASE,
                ) is not None
            )
            return (0 if preceding_measurement else 3), distance
        distance = match.start() - term_end
        return (0 if distance <= 40 else 2), distance

    def in_term_sentence(match: re.Match[str]) -> bool:
        return match.start() >= sentence_left and match.end() <= sentence_right

    def directly_linked(match: re.Match[str]) -> bool:
        if match.start() >= term_end:
            bridge = context[term_end:match.start()]
            if metric_name == 'price_mix_growth_pct' and re.search(
                r'\b(?:ebitda|inflation|margin|acquisition|acquisitions)\b',
                bridge,
                re.IGNORECASE,
            ):
                return False
            if metric_name in _BASIS_POINT_METRICS:
                intervening = list(_NUMBER_UNIT_PATTERN.finditer(bridge))
                if intervening and not _CHANGE_LANGUAGE_PATTERN.search(
                    bridge[intervening[-1].end():]
                ):
                    return False
            return (
                len(bridge) <= 120
                and not _BOUNDARY_SEPARATOR_PATTERN.search(bridge)
                and not re.search(
                    r'\b(?:and|but|while|as\s+well\s+as)\b|'
                    r'\b(?:reflecting|driven\s+by|offset\s+by)\b',
                    bridge,
                    re.IGNORECASE,
                )
            )
        bridge = context[match.end():term_start]
        if metric_name in _BASIS_POINT_METRICS:
            intervening = list(_NUMBER_UNIT_PATTERN.finditer(bridge))
            if intervening and not _CHANGE_LANGUAGE_PATTERN.search(
                bridge[intervening[-1].end():]
            ):
                return False
        if metric_name == 'comparable_sales_growth_pct' and re.search(
            r'\b(?:eps|earnings|margin)\b', bridge, re.IGNORECASE
        ):
            return False
        if metric_name == 'price_mix_growth_pct' and re.search(
            r'\b(?:ebitda|inflation|gross\s+profit|operating\s+income|'
            r'net\s+income|net\s+sales|revenue)\b',
            bridge,
            re.IGNORECASE,
        ):
            return False
        if metric_name.endswith('volume_growth_pct') and re.search(
            r'\b(?:dollar|sales|share|price)\b', bridge, re.IGNORECASE
        ):
            return False
        return (
            len(bridge) <= 80
            and not re.search(
                r'[,;|\u2022\uf0b7\u25e6\u25cf]|'
                r'\b(?:and|but|while)\b|'
                r'\b(?:reflecting|driven\s+by|offset\s+by)\b',
                bridge,
                re.IGNORECASE,
            )
        )

    if metric_name in _CURRENCY_METRICS:
        for match in _CURRENCY_PER_PATTERN.finditer(context):
            if not in_term_sentence(match) or not directly_linked(match):
                continue
            value = _signed_value(match, context)
            if value is None:
                continue
            raw_currency = str(match.group('currency') or '').upper()
            currency = {
                '$': company_currency,
                'US$': 'USD',
                'C$': 'CAD',
                'CA$': 'CAD',
            }.get(raw_currency, raw_currency or company_currency)
            denominator = re.sub(r'\s+', '_', match.group('denominator').lower())
            rank, distance = candidate_rank(match)
            candidates.append((
                rank,
                distance,
                value,
                f'{currency}_per_{denominator}',
                match.group(0),
            ))
    for match in _NUMBER_UNIT_PATTERN.finditer(context):
        if not in_term_sentence(match) or not directly_linked(match):
            continue
        value = _signed_value(match, context)
        if value is None:
            continue
        if match.end() <= term_start:
            bridge = context[match.end():term_start]
            if re.search(r'\b(?:decrease|decreased|decline|declined)\b', bridge, re.I):
                value = -abs(value)
            elif re.search(r'\b(?:increase|increased|grew|growth)\b', bridge, re.I):
                value = abs(value)
        raw_unit = re.sub(r'\s+', ' ', match.group('unit').lower())
        if metric_name in _BASIS_POINT_METRICS:
            if raw_unit in {
                '%', 'percent', 'percentage', 'percentage point',
                'percentage points',
            }:
                if 'point' not in raw_unit:
                    continue
                value *= 100.0
            elif raw_unit not in {'bp', 'bps', 'basis point', 'basis points'}:
                continue
            unit = 'basis_points'
        elif metric_name in _RATIO_METRICS:
            if raw_unit not in {'x', 'time', 'times', 'turn', 'turns'}:
                continue
            unit = 'ratio'
        elif metric_name not in _CURRENCY_METRICS:
            if raw_unit not in {
                '%', 'percent', 'percentage', 'percentage point',
                'percentage points',
            }:
                continue
            unit = 'percent'
        else:
            continue
        rank, distance = candidate_rank(match)
        candidates.append((
            rank,
            distance,
            value,
            unit,
            match.group(0),
        ))
    if not candidates:
        return None
    _, _, value, unit, raw = min(
        candidates, key=lambda row: (row[0], row[1], abs(row[2]))
    )
    return value, unit, raw


def _plausible(metric_name: str, value: float) -> bool:
    if metric_name in _PERCENT_LEVEL_METRICS:
        return 0.0 <= value <= 100.0
    if metric_name in _BASIS_POINT_METRICS:
        return -10_000.0 <= value <= 10_000.0
    if metric_name in _RATIO_METRICS:
        return 0.0 <= value <= 100.0
    if metric_name in _CURRENCY_METRICS:
        return -100_000.0 <= value <= 100_000.0
    return -100.0 <= value <= 300.0


def _scope(
    context: str,
    metric_name: str = '',
    matched_term: str = '',
    anchor_offset: int | None = None,
) -> str:
    if re.search(r'\btwo[- ]year\s+stacked\b', context, re.IGNORECASE):
        return 'two_year_stacked'
    if re.search(r'\bexcluding\s+indonesia\b', context, re.IGNORECASE):
        return 'segment_excluding_indonesia'
    third_party_retail = bool(re.search(
        r'\b(?:circana|nielsen|iri|niq|mulo(?:-c)?)\b', context, re.IGNORECASE
    ))
    if metric_name == 'traffic_growth_pct' and matched_term.casefold() == (
        'comparable traffic'
    ):
        following = _NUMBER_UNIT_PATTERN.search(
            context,
            anchor_offset + len(matched_term) if anchor_offset is not None else 0,
        )
        if following is not None and re.search(
            r'\b(?:e-?comm(?:erce)?|digitally[- ]enabled|digital sales)\b',
            context[following.end():min(len(context), following.end() + 80)],
            re.IGNORECASE,
        ):
            return 'consolidated'
    segment_labels = (
        (
            'excluding_flavored_malt_beverages',
            r'\bexcluding\s+(?:fmbs?|flavored malt beverages?)\b',
        ),
        ('mainstream', r'\bmainstream\s+(?:brands?|segment)\b'),
        ('us_retail', r'(?:\bu\.s\.|\bunited states)\s+retail\b'),
        ('us_pharmacy', r'(?:\bu\.s\.|\bunited states)\s+pharmacy\b'),
        ('family_dollar', r'\bfamily dollar\b'),
        ('dollar_tree', r'\bdollar tree\b'),
        ('beer_brazil', r'\bbeer brazil\b'),
        ('grooming', r'\bgrooming\b'),
        ('branded_salty_snacks', r'\bbranded salty snacks\b'),
        (
            'international_away_from_home',
            r'\binternational\s+and\s+away\s+from\s+home\b',
        ),
        ('oxxo_mexico', r'\boxxo(?:\s+proximity)?\s+mexico\b'),
        ('beef_north_america', r'\bbeef\s+north\s+america\b'),
        ('packaged_meats', r'\bpackaged\s+meats?\b'),
        ('stoker_products', r'\bstoker\S*s\s+products\b'),
        (
            'retail_wholesale_products',
            r'\b(?:retail\s+and\s+wholesale|r\s*&\s*w)'
            r'(?:\s+products(?:\s+group)?)?\b',
        ),
        ('fresh_fruit', r'\bfresh\s+fruit\b'),
        ('fresh_cut_fruit', r'\bfresh[- ]cut\s+fruit\b'),
        ('fresh_cut_vegetables', r'\bfresh[- ]cut\s+vegetables?\b'),
        ('gold_pineapple', r'\bgold\s+pineapple\b'),
        ('avocados', r'\bavocados?\b'),
        ('bananas', r'\bbananas?\b'),
        ('north_america_foodservice', r'\bnorth\s+america\s+foodservice\b'),
        ('north_america', r'(?<!beef )\bnorth\s+america(?:\s+segment)?\b'),
        ('international', r'\binternational\s+segment\b'),
        ('sst', r'\bss&t(?:\s+segment)?\b'),
        ('expansion_geographies', r'\bexpansion\s+geograph(?:y|ies)\b'),
        ('military', r'\bmilitary\s+segment\b'),
        ('wholesale', r'\bwholesale\s+segment\b'),
        ('consumer', r'\bconsumer\s+segment\b'),
        ('latin_america_south', r'\b(?:latin america south|las)\b'),
        ('united_states', r'\b(?:u\.s\.|united states)\b'),
        ('mexico', r'\bmexico\b'),
        ('colombia', r'\bcolombia\b'),
        ('brazil', r'\bbrazil\b'),
        ('canada', r'\bcanada\b'),
        ('chile', r'\bchile\b'),
    )
    segment_matches = [
        (label, match)
        for label, pattern in segment_labels
        for match in re.finditer(pattern, context, re.IGNORECASE)
    ]
    if segment_matches and anchor_offset is not None:
        segment = min(
            segment_matches,
            key=lambda row: min(
                abs(row[1].start() - anchor_offset),
                abs(row[1].end() - anchor_offset),
            ),
        )[0]
    else:
        segment = segment_matches[0][0] if segment_matches else ''
    direct_volume_change = re.search(
        r'\bvolume\s+(?:increased|decreased|grew|declined)\s+(?:by\s+)?'
        r'[+-]?\d+(?:\.\d+)?\s*%',
        context,
        re.IGNORECASE,
    )
    sentence_level_total_volume = re.search(
        r'(?:^|[.!?]\s+)volume\s+'
        r'(?:increased|decreased|grew|declined)\s+(?:by\s+)?'
        r'[+-]?\d+(?:\.\d+)?\s*%',
        context,
        re.IGNORECASE,
    )
    if (
        metric_name == 'volume_growth_pct'
        and direct_volume_change is not None
        and (
            _TOTAL_PATTERN.search(context) is not None
            or sentence_level_total_volume is not None
        )
    ):
        return 'consolidated'
    if metric_name == 'gross_margin_change_bps':
        basis = (
            'adjusted_excluding_out_of_period'
            if re.search(r'\bexcluding\s+out[- ]of[- ]period\b', context, re.I)
            else 'adjusted'
            if _ADJUSTED_GROSS_MARGIN_PATTERN.search(context)
            or re.search(r'\badjusted\b', context, re.IGNORECASE)
            else 'gaap'
        )
        if segment:
            return f'{basis}_segment_{segment}'
        if _SEGMENT_PATTERN.search(context) and not _TOTAL_PATTERN.search(context):
            return f'{basis}_segment'
        if _TOTAL_PATTERN.search(context):
            return f'{basis}_consolidated'
        return f'{basis}_reported_scope'
    share_basis = ''
    if metric_name == 'market_share_change_bps':
        normalized_term = matched_term.casefold()
        if normalized_term in {'volume share', 'volume market share'}:
            share_basis = 'volume_share'
        elif normalized_term in {
            'value share', 'dollar share', 'value market share',
        } or (
            normalized_term == 'market share'
            and re.search(r'\bvalue\s+market\s+share\b', context, re.I)
        ):
            share_basis = 'value_share'
    if segment:
        base_scope = f'segment_{segment}'
        return f'{base_scope}_{share_basis}' if share_basis else base_scope
    if third_party_retail:
        return (
            f'segment_third_party_retail_{share_basis}'
            if share_basis else 'segment_third_party_retail'
        )
    if _SEGMENT_PATTERN.search(context) and not _TOTAL_PATTERN.search(context):
        return f'segment_{share_basis}' if share_basis else 'segment'
    if _TOTAL_PATTERN.search(context):
        return f'consolidated_{share_basis}' if share_basis else 'consolidated'
    return f'reported_scope_{share_basis}' if share_basis else 'reported_scope'


def _segment_measurement_is_definition_complete(
    metric_name: str,
    *,
    scope: str,
    unit: str,
    selection_reason: str,
) -> bool:
    if 'segment' not in scope or metric_name not in _SCOPE_QUALIFIED_SEGMENT_METRICS:
        return False
    if 'third_party_retail' in scope:
        return False
    if metric_name == 'agricultural_processing_margin':
        return '_per_' in unit and selection_reason in {
            'explicit_metric_term_unit_and_plausible_value',
            'explicit_currency_per_unit_table_row',
        }
    if metric_name == 'market_share_change_bps':
        return unit == 'basis_points' and selection_reason in {
            'explicit_metric_term_unit_and_plausible_value',
            'explicit_basis_point_table_row',
            'explicit_percentage_point_table_row',
        }
    if metric_name in {
        'case_volume_growth_pct',
        'comparable_sales_growth_pct',
        'organic_revenue_growth_pct',
        'price_mix_growth_pct',
        'production_volume_growth_pct',
        'tobacco_shipment_volume_growth_pct',
        'volume_growth_pct',
    }:
        allowed_scopes = {
            'case_volume_growth_pct': {
                'segment_military', 'segment_wholesale',
            },
            'organic_revenue_growth_pct': {
                'segment_retail_wholesale_products',
            },
            'comparable_sales_growth_pct': {
                'segment_oxxo_mexico',
            },
            'price_mix_growth_pct': set(),
            'tobacco_shipment_volume_growth_pct': {
                'segment_stoker_products',
            },
            'volume_growth_pct': {
                'segment_beef_north_america',
                'segment_packaged_meats',
                'segment_international_away_from_home',
                'segment_fresh_fruit',
                'segment_military',
                'segment_wholesale',
                'segment_consumer',
                'segment_north_america_foodservice',
                'segment_north_america',
                'segment_international',
                'segment_sst',
                'segment_united_states',
            },
            'production_volume_growth_pct': {
                'segment_avocados',
            },
        }
        allowed_scopes['price_mix_growth_pct'].update({
            'segment_fresh_cut_fruit',
            'segment_fresh_cut_vegetables',
            'segment_gold_pineapple',
            'segment_avocados',
            'segment_bananas',
        })
        return (
            scope in allowed_scopes[metric_name]
            and unit == 'percent'
            and selection_reason in {
                'explicit_metric_term_unit_and_plausible_value',
                'explicit_percentage_table_row',
                'explicit_growth_percentage_points_table_row',
                'same_row_current_prior_level_growth_derivation',
            }
        )
    return (
        metric_name == 'sales_per_square_foot'
        and unit.endswith('_per_square_foot')
        and selection_reason == 'explicit_currency_per_unit_table_row'
    )


def _next_nonempty_cell(
    cells: tuple[str, ...],
    start: int,
    *,
    maximum_distance: int = 3,
) -> tuple[int, str] | None:
    for index in range(start, min(len(cells), start + maximum_distance)):
        if cells[index].strip():
            return index, cells[index].strip()
    return None


def _table_number_candidates(
    cells: tuple[str, ...],
    *,
    start: int,
) -> list[tuple[int, float, str, str]]:
    output: list[tuple[int, float, str, str]] = []
    for index in range(start, len(cells)):
        value_text = cells[index].strip()
        if not value_text:
            continue
        combined = value_text
        next_cell = _next_nonempty_cell(cells, index + 1)
        if next_cell is not None and re.fullmatch(
            r'\)?(?:%|percent(?:age)?|percentage\s+points?|points?|pts?|'
            r'basis\s+points?|bps?|turns?|times|x)\)?',
            next_cell[1],
            re.IGNORECASE,
        ):
            unit_cell = next_cell[1]
            if value_text.startswith('('):
                numeric = value_text[:-1] if value_text.endswith(')') else value_text
                unit = unit_cell[1:] if unit_cell.startswith(')') else unit_cell
                combined = numeric + ' ' + unit + ')'
            else:
                combined = value_text + ' ' + unit_cell
        for match in _TABLE_NUMBER_UNIT_PATTERN.finditer(combined):
            value = _signed_value(match, combined)
            if value is None:
                continue
            raw_unit = re.sub(r'\s+', ' ', match.group('unit').lower())
            if raw_unit in {'bp', 'bps', 'basis point', 'basis points'}:
                unit_kind = 'basis_points'
            elif raw_unit in {
                'percentage point', 'percentage points', 'point', 'points',
                'pt', 'pts',
            }:
                unit_kind = 'percentage_points'
            elif raw_unit in {'x', 'time', 'times', 'turn', 'turns'}:
                unit_kind = 'ratio'
            else:
                unit_kind = 'percent'
            output.append((index, value, unit_kind, match.group(0)))
    return output


def _table_bare_numbers(
    cells: tuple[str, ...],
    *,
    start: int,
) -> list[tuple[int, float, str]]:
    output: list[tuple[int, float, str]] = []
    for index in range(start, len(cells)):
        cell = cells[index].strip()
        next_cell = _next_nonempty_cell(cells, index + 1)
        if (
            not cell
            or '$' in cell
            or '%' in cell
            or next_cell is not None
            and re.fullmatch(
                r'(?:%|percent(?:age)?|points?|pts?|basis\s+points?|bps?)\)?',
                next_cell[1],
                re.IGNORECASE,
            )
        ):
            continue
        match = re.fullmatch(
            r'\s*(?P<open>\()?(?P<sign>[+-])?\s*'
            r'(?P<value>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)'
            r'\s*(?P<close>\))?\s*',
            cell,
        )
        if match is None:
            continue
        value = _signed_value(match, cell)
        if (
            value is None
            or not math.isfinite(value)
        ):
            continue
        output.append((index, value, match.group(0)))
    return output


def _table_currency_candidate(
    metric_name: str,
    cells: tuple[str, ...],
    *,
    start: int,
    company_currency: str,
) -> tuple[float, str, str] | None:
    row_text = ' '.join(cells[start:])
    direct = next(_CURRENCY_PER_PATTERN.finditer(row_text), None)
    if direct is not None:
        value = _signed_value(direct, row_text)
        if value is not None:
            raw_currency = str(direct.group('currency') or '').upper()
            currency = {
                '$': company_currency,
                'US$': 'USD',
                'C$': 'CAD',
                'CA$': 'CAD',
            }.get(raw_currency, raw_currency or company_currency)
            denominator = re.sub(
                r'\s+', '_', direct.group('denominator').lower()
            )
            return value, f'{currency}_per_{denominator}', direct.group(0)
    denominator = (
        'case'
        if metric_name == 'gross_profit_per_case'
        else 'square_foot'
        if metric_name == 'sales_per_square_foot'
        else ''
    )
    if not denominator or 'per ' + denominator.replace('_', ' ') not in row_text.lower():
        return None
    pending_currency = ''
    for cell in cells[start + 1:]:
        stripped = cell.strip()
        currency_only = re.fullmatch(
            r'(US\$|C\$|CA\$|\$|USD|CAD|EUR|GBP)',
            stripped,
            re.IGNORECASE,
        )
        if currency_only is not None:
            pending_currency = currency_only.group(1).upper()
            continue
        match = re.search(
            r'(?P<currency>US\$|C\$|CA\$|\$|USD|CAD|EUR|GBP)\s*'
            r'(?P<open>\()?(?P<sign>[+-])?\s*'
            r'(?P<value>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)'
            r'\s*(?P<close>\))?',
            cell,
            re.IGNORECASE,
        )
        if match is None and pending_currency:
            match = re.fullmatch(
                r'\s*(?P<open>\()?(?P<sign>[+-])?\s*'
                r'(?P<value>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)'
                r'\s*(?P<close>\))?\s*',
                stripped,
            )
        if match is None:
            pending_currency = ''
            continue
        value = _signed_value(match, cell)
        if value is None:
            pending_currency = ''
            continue
        raw_currency = str(
            match.groupdict().get('currency') or pending_currency
        ).upper()
        currency = {
            '$': company_currency,
            'US$': 'USD',
            'C$': 'CAD',
            'CA$': 'CAD',
        }.get(raw_currency, raw_currency or company_currency)
        return value, f'{currency}_per_{denominator}', match.group(0)
    return None


def _table_candidate(
    metric_name: str,
    cells: tuple[str, ...],
    *,
    term_cell: int,
    term_start: int,
    term_end: int,
    table_context: str,
    company_currency: str,
) -> tuple[float, str, str, str, float] | None:
    row_text = ' | '.join(cells)
    semantic_context = table_context + ' | ' + row_text
    if _TABLE_EXCLUSION_PATTERN.search(semantic_context):
        return None
    if not _metric_context_allowed(metric_name, row_text):
        return None
    if (
        metric_name in _RATIO_METRICS
        and _RATIO_LIMIT_PATTERN.search(semantic_context)
        and not _RATIO_ACTUAL_PATTERN.search(row_text)
    ):
        return None
    if (
        metric_name in _TABLE_LEVEL_CONTEXT_REQUIRED
        and not _TABLE_LEVEL_CONTEXT_PATTERN.search(semantic_context)
    ):
        return None
    same_cell = cells[term_cell]
    same_cell_numbers = _table_number_candidates((same_cell,), start=0)
    if metric_name == 'traffic_growth_pct':
        preceding = [
            match for match in _NUMBER_UNIT_PATTERN.finditer(same_cell)
            if match.end() <= term_start
            and not same_cell[match.end():term_start].strip()
        ]
        if preceding:
            selected = preceding[-1]
            raw_unit = re.sub(r'\s+', ' ', selected.group('unit').lower())
            if raw_unit in {'%', 'percent', 'percentage'}:
                value = _signed_value(selected, same_cell)
                if value is not None:
                    return (
                        value,
                        'percent',
                        selected.group(0),
                        'explicit_value_immediately_precedes_table_label',
                        0.97,
                    )
    if metric_name.endswith('_growth_pct') and len([
        value for value in same_cell_numbers if value[2] == 'percent'
    ]) >= 3:
        return None
    duration_count = sum(
        bool(re.search(pattern, semantic_context, re.IGNORECASE))
        for pattern in (
            r'\bthree\s+months?\b', r'\bsix\s+months?\b',
            r'\bnine\s+months?\b', r'\btwelve\s+months?\b',
        )
    )
    if metric_name.endswith('_growth_pct') and duration_count >= 2:
        return None
    sentence_candidate = _parse_candidate(
        metric_name,
        same_cell,
        term_start,
        term_end,
        company_currency=company_currency,
    )
    if sentence_candidate is not None:
        value, unit, raw = sentence_candidate
        return (
            value,
            unit,
            raw,
            'explicit_metric_term_and_value_in_same_table_cell',
            0.97,
        )
    if len(same_cell) > 200:
        # SEC exhibits sometimes place a full narrative paragraph in the
        # first cell of a malformed table and unrelated segment values in
        # later cells. Without a same-cell value, cross-cell binding is not
        # deterministic.
        return None
    if (
        re.search(r'\bfourth\s+quarter\b', row_text, re.IGNORECASE)
        and re.search(r'\bfull[- ]year\b', row_text, re.IGNORECASE)
    ):
        # A flattened row containing both quarterly and annual columns needs
        # retained column headers before any value can be period-bound.
        return None
    if metric_name in _CURRENCY_METRICS:
        candidate = _table_currency_candidate(
            metric_name,
            cells,
            start=term_cell,
            company_currency=company_currency,
        )
        if candidate is None:
            return None
        return (
            candidate[0], candidate[1], candidate[2],
            'explicit_currency_per_unit_table_row',
            0.97,
        )
    numbers = _table_number_candidates(cells, start=term_cell + 1)
    if metric_name in _BASIS_POINT_METRICS:
        for _, value, unit_kind, raw in numbers:
            if unit_kind == 'basis_points':
                return (
                    value, 'basis_points', raw,
                    'explicit_basis_point_table_row',
                    0.97,
                )
            if unit_kind == 'percentage_points':
                return (
                    value * 100.0, 'basis_points', raw,
                    'explicit_percentage_point_table_row',
                    0.97,
                )
        if metric_name in {
            'gross_margin_change_bps', 'market_share_change_bps',
        }:
            levels = [
                (value, raw) for _, value, unit_kind, raw in numbers
                if unit_kind == 'percent'
            ]
            if len(levels) == 2:
                reason = (
                    'comparative_market_share_levels_table_derivation'
                    if metric_name == 'market_share_change_bps'
                    else 'comparative_gross_margin_levels_table_derivation'
                )
                return (
                    (levels[0][0] - levels[1][0]) * 100.0,
                    'basis_points',
                    f'{levels[0][1]} versus {levels[1][1]}',
                    reason,
                    0.95,
                )
        return None
    if metric_name in _RATIO_METRICS:
        for _, value, unit_kind, raw in numbers:
            if unit_kind == 'ratio':
                return (
                    value, 'ratio', raw,
                    'explicit_ratio_table_row',
                    0.97,
                )
        return None
    for _, value, unit_kind, raw in numbers:
        if unit_kind == 'percent':
            if len([
                item for item in numbers if item[2] == 'percent'
            ]) >= 3:
                return None
            return (
                value, 'percent', raw,
                'explicit_percentage_table_row',
                0.97,
            )
        if (
            unit_kind == 'percentage_points'
            and 'growth' in row_text.lower()
            and not re.search(r'\bcontributions?\b', row_text, re.IGNORECASE)
        ):
            return (
                value, 'percent', raw,
                'explicit_growth_percentage_points_table_row',
                0.95,
            )
    if metric_name not in _TABLE_LEVEL_DERIVABLE_GROWTH:
        return None
    lowered_row = row_text.lower()
    if (
        metric_name == 'net_store_growth_pct'
        and (
            'beginning store count' in lowered_row
            or not re.search(
                r'\b(?:ending|total|number\s+of)\s+'
                r'(?:store|stores|location|locations|retail\s+units?)',
                lowered_row,
            )
        )
    ):
        return None
    levels = _table_bare_numbers(cells, start=term_cell + 1)
    if len(levels) < 2 or levels[0][1] <= 0.0 or levels[1][1] <= 0.0:
        return None
    if (
        1900.0 <= levels[0][1] <= 2100.0
        and 1900.0 <= levels[1][1] <= 2100.0
        and abs(levels[0][1] - levels[1][1]) <= 5.0
        and metric_name != 'net_store_growth_pct'
    ):
        return None
    value = (levels[0][1] / levels[1][1] - 1.0) * 100.0
    return (
        value,
        'percent',
        f'{levels[0][2]} versus {levels[1][2]}',
        'same_row_current_prior_level_growth_derivation',
        0.94,
    )


def _table_evidence(
    item: WorkItem,
    *,
    requested: set[str],
    tables: tuple[tuple[tuple[str, ...], ...], ...],
    source_document: str,
    document_sha256: str,
    document_provenance: dict[str, Any],
) -> list[MetricEvidence]:
    output: list[MetricEvidence] = []
    for table_index, table in enumerate(tables):
        leading_rows = table[:5]
        for row_index, cells in enumerate(table):
            prior_rows = table[max(0, row_index - 2):row_index]
            context_rows = (*leading_rows, *prior_rows, cells)
            table_context = _normalize_visible_text(
                ' | '.join(
                    cell for context_row in context_rows for cell in context_row
                )
            )[:3000]
            for cell_index, cell in enumerate(cells):
                lowered_cell = cell.lower()
                for match in _ALL_TERM_PATTERN.finditer(lowered_cell):
                    term = match.group(0).lower()
                    for metric_name in sorted(
                        _TERM_TO_METRICS.get(term, set()) & requested
                    ):
                        if not _term_context_allowed(
                            metric_name, term, table_context
                        ):
                            continue
                        row_text = _normalize_visible_text(' | '.join(cells))
                        if (
                            metric_name.endswith('_growth_pct')
                            and metric_name != 'traffic_growth_pct'
                            and len(re.findall(
                                r'(?:\d+(?:\.\d+)?|\))\s*%', row_text
                            )) >= 3
                        ):
                            continue
                        candidate = _table_candidate(
                            metric_name,
                            cells,
                            term_cell=cell_index,
                            term_start=match.start(),
                            term_end=match.end(),
                            table_context=table_context,
                            company_currency=item.filing.company_currency,
                        )
                        if candidate is None:
                            continue
                        value, unit, raw, reason, confidence = candidate
                        scope = _filing_scope(
                            item, _scope(table_context, metric_name, term)
                        )
                        status = 'ACCEPTED'
                        if not _plausible(metric_name, value):
                            status = 'REJECTED_POLICY'
                            reason = 'value_outside_metric_plausibility_bounds'
                            confidence = 0.99
                        elif bool(document_provenance.get('ocr_used')):
                            status = 'REVIEW_REQUIRED'
                            reason = 'ocr_derived_requires_review'
                            confidence = min(confidence, 0.80)
                        elif (
                            'segment' in scope
                            and not _segment_measurement_is_definition_complete(
                                metric_name,
                                scope=scope,
                                unit=unit,
                                selection_reason=reason,
                            )
                        ):
                            status = 'REVIEW_REQUIRED'
                            reason = 'segment_scope_requires_definition_review'
                            confidence = min(confidence, 0.80)
                        elif not item.filing.report_date:
                            status = 'REVIEW_REQUIRED'
                            reason = 'report_period_missing'
                            confidence = min(confidence, 0.75)
                        evidence_text = _normalize_visible_text(
                            ' | '.join(cells)
                        )[:1500]
                        period_start, period_end = _period_bounds(
                            item, table_context
                        )
                        output.append(MetricEvidence(
                            metric_name=metric_name,
                            concept_name='FilingTable:' + re.sub(
                                r'[^A-Za-z0-9]+', '', term.title()
                            ),
                            value=value,
                            unit=unit,
                            period_start=period_start,
                            period_end=period_end,
                            scope=scope,
                            confidence=confidence,
                            status=status,
                            reason=reason,
                            evidence_text=evidence_text,
                            source_document=source_document,
                            extraction_method=(
                                'dedicated_parser:'
                                'consumer_defensive_filing_table_v2'
                            ),
                            provenance={
                                **document_provenance,
                                'adapter_version': ADAPTER_VERSION,
                                'registry_version': _REGISTRY_VERSION,
                                'term_registry_version': _TERM_VERSION,
                                'matched_term': term,
                                'matched_numeric_text': raw,
                                'document_sha256': document_sha256,
                                'table_index': table_index,
                                'row_index': row_index,
                                'row_cells': list(cells),
                                'selection_reason': reason,
                            },
                        ))
    return output


def _text_evidence(
    item: WorkItem,
    *,
    metric_name: str,
    text: str,
    source_document: str,
    document_sha256: str,
    document_provenance: dict[str, Any],
    term_matches: tuple[tuple[str, re.Match[str]], ...] | None = None,
) -> list[MetricEvidence]:
    output: list[MetricEvidence] = []
    lowered = text.lower()
    matches = term_matches
    if matches is None:
        matches = tuple(
            (term, match)
            for term in _METRIC_TERMS[metric_name]
            for match in re.finditer(
                r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])',
                lowered,
            )
        )
    for term, match in matches:
            context, local_term = _context(text, match.start(), match.end())
            if local_term < 0:
                continue
            sentence_left, sentence_right = _sentence_bounds(
                context, local_term, local_term + len(term)
            )
            sentence = context[sentence_left:sentence_right]
            forward_window = context[
                max(0, local_term - 180):local_term + len(term)
            ]
            if _ADJACENT_FORWARD_HEADING_PATTERN.search(forward_window):
                continue
            if not _term_context_allowed(metric_name, term, sentence):
                continue
            if not _metric_context_allowed(metric_name, sentence):
                continue
            numeric_matches = list(_NUMBER_UNIT_PATTERN.finditer(sentence))
            if len(numeric_matches) >= 3:
                sentence_term_end = local_term - sentence_left + len(term)
                after_term = [
                    numeric for numeric in numeric_matches
                    if numeric.start() >= sentence_term_end
                    and numeric.start() - sentence_term_end <= 40
                    and not _BOUNDARY_SEPARATOR_PATTERN.search(
                        sentence[sentence_term_end:numeric.start()]
                    )
                ]
                if not after_term:
                    continue
                if len(after_term) > 1:
                    next_label = sentence[
                        after_term[0].end():after_term[1].start()
                    ]
                    if not re.search(
                        r'\b(?:[a-z][a-z0-9/&.-]*\s+){1,4}'
                        r'(?:growth|change|margin|mix|sales|volume|pricing|'
                        r'traffic|share|eps)\b',
                        next_label,
                        re.IGNORECASE,
                    ):
                        continue
            if term == 'sales leaders' and not re.search(
                r'\b(?:representative|representatives|distributor|distributors|'
                r'direct selling|sales force)\b',
                sentence,
                re.IGNORECASE,
            ):
                continue
            candidate = _parse_candidate(
                metric_name,
                context,
                local_term,
                local_term + len(term),
                company_currency=item.filing.company_currency,
            )
            if candidate is None:
                continue
            if (
                metric_name in _TABLE_LEVEL_CONTEXT_REQUIRED
                and not _TABLE_LEVEL_CONTEXT_PATTERN.search(sentence)
            ):
                continue
            value, unit, raw_value = candidate
            raw_offset = sentence.find(raw_value)
            inequality_value = (
                raw_offset >= 0
                and _INEQUALITY_VALUE_PREFIX_PATTERN.search(
                    sentence[max(0, raw_offset - 32):raw_offset]
                ) is not None
            )
            approximate_value = (
                raw_offset >= 0
                and _APPROXIMATION_VALUE_PREFIX_PATTERN.search(
                    sentence[max(0, raw_offset - 32):raw_offset]
                ) is not None
            )
            if metric_name == 'market_share_change_bps':
                scope_context = context
                scope_anchor = local_term
            elif metric_name == 'gross_margin_change_bps':
                scope_context = sentence
                scope_anchor = local_term - sentence_left
            elif metric_name == 'organic_revenue_growth_pct':
                ordinary_scope_start = max(0, local_term - 240)
                ordinary_scope_end = min(len(context), sentence_right + 80)
                ordinary_scope_context = context[
                    ordinary_scope_start:ordinary_scope_end
                ]
                needs_named_segment_lookback = re.search(
                    r'\borganic\s+(?:net\s+)?sales\s+within\s+(?:the\s+)?'
                    r'operating\s+segment\b',
                    ordinary_scope_context,
                    re.IGNORECASE,
                ) is not None
                scope_start = max(
                    0,
                    local_term - (440 if needs_named_segment_lookback else 240),
                )
                scope_end = min(len(context), sentence_right + 80)
                scope_context = context[scope_start:scope_end]
                scope_anchor = local_term - scope_start
            elif metric_name in _SCOPE_QUALIFIED_SEGMENT_METRICS:
                scope_start = max(0, local_term - 260)
                scope_end = min(
                    len(context), local_term + len(term) + 260
                )
                scope_context = context[scope_start:scope_end]
                scope_anchor = local_term - scope_start
            else:
                scope_context = sentence
                scope_anchor = local_term - sentence_left
            scope = _filing_scope(
                item,
                _scope(
                    scope_context,
                    metric_name,
                    term,
                    anchor_offset=scope_anchor,
                ),
            )
            status = 'ACCEPTED'
            reason = 'explicit_metric_term_unit_and_plausible_value'
            confidence = 0.91
            if not _plausible(metric_name, value):
                status = 'REJECTED_POLICY'
                reason = 'value_outside_metric_plausibility_bounds'
                confidence = 0.99
            elif inequality_value and metric_name not in _CURRENCY_METRICS:
                status = 'REVIEW_REQUIRED'
                reason = 'inequality_value_is_not_an_exact_measurement'
                confidence = 0.70
            elif approximate_value and metric_name not in _CURRENCY_METRICS:
                if term in _APPROXIMATE_DIRECT_TERMS.get(metric_name, ()):
                    reason = 'explicit_approximate_point_estimate'
                    confidence = 0.86
                else:
                    status = 'REVIEW_REQUIRED'
                    reason = 'approximate_value_requires_review'
                    confidence = 0.70
            elif bool(document_provenance.get('ocr_used')):
                status = 'REVIEW_REQUIRED'
                reason = 'ocr_derived_requires_review'
                confidence = min(confidence, 0.80)
            elif (
                'segment' in scope
                and not _segment_measurement_is_definition_complete(
                    metric_name,
                    scope=scope,
                    unit=unit,
                    selection_reason=reason,
                )
            ):
                status = 'REVIEW_REQUIRED'
                reason = 'segment_scope_requires_definition_review'
                confidence = 0.76
            elif not item.filing.report_date:
                status = 'REVIEW_REQUIRED'
                reason = 'report_period_missing'
                confidence = 0.70
            period_start, period_end = _period_bounds(item, context)
            output.append(MetricEvidence(
                metric_name=metric_name,
                concept_name='FilingProse:' + re.sub(
                    r'[^A-Za-z0-9]+', '', term.title()
                ),
                value=value,
                unit=unit,
                period_start=period_start,
                period_end=period_end,
                scope=scope,
                confidence=confidence,
                status=status,
                reason=reason,
                evidence_text=context,
                source_document=source_document,
                extraction_method='dedicated_parser:consumer_defensive_prose_v1',
                provenance={
                    **document_provenance,
                    'adapter_version': ADAPTER_VERSION,
                    'registry_version': _REGISTRY_VERSION,
                    'term_registry_version': _TERM_VERSION,
                    'matched_term': term,
                    'matched_numeric_text': raw_value,
                    'document_sha256': document_sha256,
                    'measurement_precision': (
                        'approximate' if approximate_value else 'reported_exact'
                    ),
                },
            ))
    return output


def _deduplicate(
    item: WorkItem,
    rows: list[MetricEvidence],
) -> tuple[MetricEvidence, ...]:
    grouped: dict[tuple[str, str, str, str, str], list[MetricEvidence]] = {}
    for row in rows:
        key = (
            row.metric_name,
            row.period_end,
            row.unit.upper(),
            row.scope,
            row.source_document,
        )
        grouped.setdefault(key, []).append(row)
    output: list[MetricEvidence] = []
    status_rank = {
        'ACCEPTED': 4,
        'REVIEW_REQUIRED': 3,
        'REJECTED_POLICY': 2,
        'PARSER_FAILURE': 1,
    }
    for candidates in grouped.values():
        accepted_values = {
            round(float(row.value), 8)
            for row in candidates
            if row.status == 'ACCEPTED' and row.value is not None
        }
        if len(accepted_values) > 1:
            strong_rows: list[MetricEvidence] = []
            for row in candidates:
                selection_reason = str(
                    row.provenance.get('selection_reason') or ''
                )
                if (
                    row.status == 'ACCEPTED'
                    and row.value is not None
                    and 'consumer_defensive_filing_table' in row.extraction_method
                    and selection_reason in _STRONG_TABLE_CONFLICT_SELECTIONS
                    and not _FORWARD_LOOKING_DISCLOSURE_PATTERN.search(
                        row.evidence_text
                    )
                ):
                    strong_rows.append(row)
            strong_values = {
                round(float(row.value), 8) for row in strong_rows
                if row.value is not None
            }
            if len(strong_values) == 1:
                selected_value = next(iter(strong_values))
                winner = max(
                    (
                        row for row in strong_rows
                        if row.value is not None
                        and round(float(row.value), 8) == selected_value
                    ),
                    key=lambda row: (
                        row.confidence,
                        row.concept_name,
                        row.evidence_text,
                    ),
                )
                output.append(replace(
                    winner,
                    reason=(
                        'structured_table_value_resolves_lower_precision_conflict'
                    ),
                    provenance={
                        **winner.provenance,
                        'conflict_resolution': (
                            'unique_non_forward_looking_structured_table_value'
                        ),
                        'conflicting_values': sorted(accepted_values),
                    },
                ))
                output.extend(
                    replace(
                        row,
                        status='REVIEW_REQUIRED',
                        confidence=min(row.confidence, 0.70),
                        reason=(
                            'superseded_by_unique_structured_table_value'
                        ),
                    )
                    for row in candidates
                    if row is not winner
                    and (
                        row.value is None
                        or round(float(row.value), 8) != selected_value
                    )
                )
                continue
            output.extend(
                replace(
                    row,
                    status='REVIEW_REQUIRED',
                    confidence=min(row.confidence, 0.70),
                    reason='conflicting_values_same_metric_period_scope_document',
                )
                for row in candidates
            )
        else:
            output.append(max(
                candidates,
                key=lambda row: (
                    status_rank.get(row.status, 0),
                    row.confidence,
                    row.concept_name,
                    row.evidence_text,
                ),
            ))
    unique = {
        row.evidence_key(model_family=item.model_family, filing=item.filing): row
        for row in output
    }
    return tuple(sorted(
        unique.values(),
        key=lambda row: (
            row.metric_name,
            row.period_end,
            row.source_document,
            row.concept_name,
        ),
    ))


def extract_metric_evidence(item: WorkItem) -> tuple[MetricEvidence, ...]:
    requested = {
        request.metric_name for request in item.requested_metrics
        if request.metric_name in _METRIC_BY_ID
    }
    output: list[MetricEvidence] = []
    for document in item.documents:
        if document.is_full_submission:
            continue
        try:
            with open_path(Path(document.path), 'rb') as handle:
                raw = handle.read()
            text, tables, document_provenance = _document_content(
                raw,
                suffix=Path(document.name).suffix,
                source_document=document.name,
                enable_pdf_ocr=item.enable_pdf_ocr,
                max_pdf_pages=item.max_pdf_pages,
                max_pdf_bytes=item.max_pdf_bytes,
                pdf_timeout_seconds=item.pdf_extraction_timeout_seconds,
                max_ocr_pages=item.max_ocr_pages,
                ocr_dpi=item.ocr_dpi,
                ocr_page_timeout_seconds=item.ocr_page_timeout_seconds,
                max_ocr_pixels_per_page=item.max_ocr_pixels_per_page,
            )
        except (OSError, UnicodeError, RuntimeError, TimeoutError, ValueError) as exc:
            output.extend(
                MetricEvidence(
                    metric_name=metric_name,
                    concept_name='DocumentReadFailure',
                    value=None,
                    unit='',
                    period_start='',
                    period_end=item.filing.report_date,
                    scope='unknown',
                    confidence=0.0,
                    status='PARSER_FAILURE',
                    reason=f'document_read_failed:{type(exc).__name__}',
                    evidence_text=str(exc)[:500],
                    source_document=document.name,
                    extraction_method=(
                        'dedicated_parser:consumer_defensive_document_read'
                    ),
                    provenance={
                        'adapter_version': ADAPTER_VERSION,
                        'ocr_requested': item.enable_pdf_ocr,
                    },
                )
                for metric_name in sorted(requested)
            )
            continue
        output.extend(_table_evidence(
            item,
            requested=requested,
            tables=tables,
            source_document=document.name,
            document_sha256=document.content_sha256,
            document_provenance=document_provenance,
        ))
        matches_by_metric: dict[str, list[tuple[str, re.Match[str]]]] = {}
        for match in _ALL_TERM_PATTERN.finditer(text):
            term = match.group(0).lower()
            for matched_metric in _TERM_TO_METRICS.get(term, set()):
                if matched_metric in requested:
                    matches_by_metric.setdefault(matched_metric, []).append(
                        (term, match)
                    )
        for metric_name in sorted(requested):
            output.extend(_text_evidence(
                item,
                metric_name=metric_name,
                text=text,
                source_document=document.name,
                document_sha256=document.content_sha256,
                document_provenance=document_provenance,
                term_matches=tuple(matches_by_metric.get(metric_name, ())),
            ))
    return _deduplicate(item, output)


def _fact_text(fact: NormalizedFact) -> str:
    values = [fact.taxonomy, fact.concept_name]
    try:
        metadata = json.loads(fact.concept_metadata_json or '{}')
    except json.JSONDecodeError:
        metadata = {}
    if isinstance(metadata, dict):
        values.extend(
            str(value) for value in metadata.values() if isinstance(value, str)
        )
    return ' '.join(values).lower()


def _fact_metric(fact: NormalizedFact) -> str | None:
    normalized = re.sub(r'[^a-z0-9]+', '', _fact_text(fact))
    matches: list[tuple[int, str]] = []
    for metric_name, terms in _METRIC_TERMS.items():
        for term in terms:
            needle = re.sub(r'[^a-z0-9]+', '', term)
            if needle and needle in normalized:
                matches.append((len(needle), metric_name))
    return max(matches)[1] if matches else None


def map_normalized_facts(
    item: WorkItem,
    facts: tuple[NormalizedFact, ...],
) -> tuple[MetricEvidence, ...]:
    requested = {request.metric_name for request in item.requested_metrics}
    output: list[MetricEvidence] = []
    for fact in facts:
        metric_name = _fact_metric(fact)
        if (
            metric_name is None
            or metric_name not in requested
            or fact.numeric_value is None
        ):
            continue
        value = float(fact.numeric_value)
        unit_text = str(fact.unit or '').upper()
        expected = _METRIC_BY_ID[metric_name].unit_family
        unit = (
            'percent'
            if unit_text in {'PERCENT', '%', 'PURE'}
            and expected.startswith('percent')
            else 'basis_points'
            if unit_text in {'BPS', 'BASISPOINTS'}
            and expected == 'basis_points'
            else 'ratio'
            if unit_text in {'PURE', 'RATIO', 'X'}
            and expected == 'ratio'
            else unit_text
        )
        accepted_unit = (
            expected.startswith('percent') and unit == 'percent'
            or expected == 'basis_points' and unit == 'basis_points'
            or expected == 'ratio' and unit == 'ratio'
        )
        status = 'REVIEW_REQUIRED'
        reason = 'extension_fact_requires_definition_or_unit_review'
        confidence = 0.78
        if fact.scope != 'consolidated':
            reason = 'dimensional_or_segment_fact_requires_scope_review'
        elif not _plausible(metric_name, value):
            status = 'REJECTED_POLICY'
            reason = 'value_outside_metric_plausibility_bounds'
            confidence = 0.99
        elif accepted_unit:
            status = 'ACCEPTED'
            reason = 'explicit_consolidated_specialized_extension_fact'
            confidence = 0.93
        output.append(MetricEvidence(
            metric_name=metric_name,
            concept_name=fact.concept_name,
            value=value,
            unit=unit,
            period_start=fact.period_start,
            period_end=fact.period_end,
            scope=fact.scope,
            confidence=confidence,
            status=status,
            reason=reason,
            evidence_text=(
                f'{fact.taxonomy}:{fact.concept_name}={value:g} {fact.unit}'
            ),
            source_document=fact.source_document,
            extraction_method=f'dedicated_parser:{fact.provider}:normalized_fact',
            provenance={
                'adapter_version': ADAPTER_VERSION,
                'context_id': fact.context_id,
                'dimensions_json': fact.dimensions_json,
                'expected_unit_family': expected,
            },
        ))
    return _deduplicate(item, output)


def postprocess_metric_evidence(
    item: WorkItem,
    evidence: tuple[MetricEvidence, ...],
) -> tuple[MetricEvidence, ...]:
    return _deduplicate(item, list(evidence))


def policy_manifest() -> dict[str, Any]:
    return {
        'adapter_version': ADAPTER_VERSION,
        'metric_registry_version': _REGISTRY_VERSION,
        'term_registry_version': _TERM_VERSION,
        'supported_forms': list(_SUPPORTED_FORMS),
        'metric_count': len(_METRICS),
        'metrics': {
            metric.metric_id: {
                'unit_family': metric.unit_family,
                'source_availability_class': (
                    metric.source_availability_class
                ),
                'cohorts': list(metric.cohorts),
                'applicability_subtypes': list(metric.applicability_subtypes),
                'terms': list(_METRIC_TERMS[metric.metric_id]),
            }
            for metric in _METRICS
        },
    }
