from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Mapping, Sequence

from industrials.transportation.parser_coverage import PARSER_DERIVATIONS


FIXTURE_REVIEW_VERSION = "transportation_dp6z_fixture_review_v2"
REVIEWED_BY = "codex_transportation_semantic_fixture_review_v2"

EVIDENCE_DECISION_FIELDS = (
    "review_version",
    "review_order",
    "phase_rank",
    "review_phase",
    "pair_key",
    "fixture_id",
    "ticker",
    "metric_id",
    "source_lane",
    "source_metric_id",
    "source_stage",
    "evidence_key",
    "candidate_value",
    "value_override",
    "unit",
    "period_end",
    "scope",
    "accession_number",
    "source_document",
    "semantic_decision",
    "fixture_polarity",
    "rule_id",
    "decision_reason",
    "policy_eligible",
    "reviewed_by",
    "reviewed_at",
    "evidence_row_sha256",
    "evidence_text",
)

PAIR_DECISION_FIELDS = (
    "review_version",
    "review_order",
    "phase_rank",
    "review_phase",
    "pair_key",
    "fixture_id",
    "ticker",
    "metric_id",
    "source_lane",
    "review_route",
    "pair_decision",
    "decision_reason",
    "numeric_evidence_count",
    "accepted_evidence_count",
    "rejected_evidence_count",
    "deferred_evidence_count",
    "accepted_evidence_keys",
    "rejected_evidence_keys",
    "deferred_evidence_keys",
    "dependency_validation_status",
    "policy_eligible_evidence_count",
    "reviewed_by",
    "reviewed_at",
)

_NUMBER = re.compile(
    r"(?P<currency>US\$|C\$|A\$|\$|€|£)?\s*"
    r"(?P<value>-?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>billions?|millions?|thousands?|bn|mm|[bmk])?\s*"
    r"(?P<percent>%|percent)?",
    re.IGNORECASE,
)
_ISSUER = re.compile(
    r"\b(?:our|we|us|the company(?:'s)?|company-wide|fleetwide)\b",
    re.IGNORECASE,
)
_NONISSUER = re.compile(
    r"\b(?:world|global|industry|market|peer|competitor|customer fleet|"
    r"orderbook represented|on the water fleet)\b",
    re.IGNORECASE,
)
_FORWARD = re.compile(
    r"\b(?:target|expected|expects|projected|forecast|estimate|"
    r"intend|plan to|future minimum)\b",
    re.IGNORECASE,
)
_EXPLICIT_PERIOD = re.compile(
    r"\b(?:as of|year ended|quarter ended|months ended|period ended)\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_MONTH_DATE = re.compile(
    r"\b("
    r"January|February|March|April|May|June|July|August|"
    r"September|October|November|December"
    r")\s+(\d{1,2}),\s*((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_MONTH_DAY = re.compile(
    r"\b("
    r"January|February|March|April|May|June|July|August|"
    r"September|October|November|December"
    r")\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b((?:19|20)\d{2}-\d{2}-\d{2})\b")
_QUARTER = re.compile(
    r"\b(first|second|third|fourth)\s+quarter(?:\s+of)?\s+"
    r"((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_HALF_YEAR = re.compile(
    r"\b(first|second)\s+half(?:\s+of)?\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)

_POLICY_ELIGIBLE_REJECT_RULES = frozenset(
    {
        "revenue_days_bounds",
        "revenue_days_prohibited_definition_or_assumption",
        "revenue_days_year_as_value",
        "average_length_of_haul_bounds",
        "vessel_count_bounds",
        "fleet_capacity_bounds",
        "fleet_capacity_prohibited_range_or_orderbook",
        "tce_day_rate_bounds",
        "tce_day_rate_year_as_value",
        "tce_day_rate_prohibited_estimate",
        "tce_day_rate_prohibited_contract_or_delta",
        "fuel_surcharge_ratio_bounds",
    }
)


def _float(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _scale(value: str) -> float:
    normalized = value.lower()
    if normalized in {"billion", "billions", "bn", "b"}:
        return 1_000_000_000.0
    if normalized in {"million", "millions", "mm", "m"}:
        return 1_000_000.0
    if normalized in {"thousand", "thousands", "k"}:
        return 1_000.0
    return 1.0


def _number_spans(
    text: str,
    *,
    candidate: float,
    unit: str,
) -> list[tuple[int, int, str]]:
    output: list[tuple[int, int, str]] = []
    for match in _NUMBER.finditer(text):
        try:
            number = float(match.group("value").replace(",", ""))
        except ValueError:
            continue
        number *= _scale(str(match.group("scale") or ""))
        if match.group("percent") and unit == "ratio":
            number /= 100.0
        tolerance = max(1e-6, abs(candidate) * 1e-8)
        if abs(number - candidate) <= tolerance:
            output.append((match.start(), match.end(), match.group(0)))
    return output


def _alias_pattern(alias: str) -> re.Pattern[str] | None:
    tokens = re.findall(r"[A-Za-z0-9]+", alias)
    if not tokens:
        return None
    return re.compile(
        r"\b" + r"[\s/_-]*".join(map(re.escape, tokens)) + r"\b",
        re.IGNORECASE,
    )


def _alias_spans(
    text: str,
    aliases: Sequence[str],
) -> list[tuple[int, int]]:
    output: list[tuple[int, int]] = []
    for alias in aliases:
        pattern = _alias_pattern(alias)
        if pattern is None:
            continue
        output.extend(
            (match.start(), match.end())
            for match in pattern.finditer(text)
        )
    return output


def _nearest_context(
    text: str,
    *,
    number_spans: Sequence[tuple[int, int, str]],
    alias_spans: Sequence[tuple[int, int]],
    max_distance: int = 240,
) -> tuple[str, str] | None:
    candidates: list[tuple[int, int, int, str]] = []
    for number_start, number_end, raw in number_spans:
        for alias_start, alias_end in alias_spans:
            distance = max(
                0,
                alias_start - number_end,
                number_start - alias_end,
            )
            if distance <= max_distance:
                candidates.append(
                    (distance, number_start, number_end, raw)
                )
    if not candidates:
        return None
    _, start, end, raw = min(candidates)
    return text[max(0, start - 260) : end + 260], raw


def _period_consistent(context: str, period_end: str) -> bool:
    expected = str(period_end or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expected):
        return False
    expected_year = expected[:4]
    years = {match.group(0) for match in _YEAR.finditer(context)}
    if years and expected_year not in years:
        return False
    explicit_dates = set(_ISO_DATE.findall(context))
    for match in _MONTH_DATE.finditer(context):
        try:
            parsed = datetime.strptime(
                " ".join(match.groups()),
                "%B %d %Y",
            ).date()
        except ValueError:
            continue
        explicit_dates.add(parsed.isoformat())
    if explicit_dates:
        return expected in explicit_dates

    quarter_ends = {
        "first": "03-31",
        "second": "06-30",
        "third": "09-30",
        "fourth": "12-31",
    }
    explicit_quarters = {
        f"{match.group(2)}-{quarter_ends[match.group(1).lower()]}"
        for match in _QUARTER.finditer(context)
    }
    if explicit_quarters:
        return expected in explicit_quarters

    half_ends = {"first": "06-30", "second": "12-31"}
    explicit_halves = {
        f"{match.group(2)}-{half_ends[match.group(1).lower()]}"
        for match in _HALF_YEAR.finditer(context)
    }
    if explicit_halves:
        return expected in explicit_halves

    expected_date = datetime.strptime(expected, "%Y-%m-%d")
    month_days = {
        (match.group(1).lower(), int(match.group(2)))
        for match in _MONTH_DAY.finditer(context)
    }
    if month_days:
        return (
            expected_date.strftime("%B").lower(),
            expected_date.day,
        ) in month_days

    if not _EXPLICIT_PERIOD.search(context):
        return True
    return not years or expected_year in years


def _period_has_lineage(context: str) -> bool:
    return bool(
        _ISO_DATE.search(context)
        or _MONTH_DATE.search(context)
        or _MONTH_DAY.search(context)
        or _QUARTER.search(context)
        or _HALF_YEAR.search(context)
        or _YEAR.search(context)
    )


def _direct_result(
    decision: str,
    rule_id: str,
    reason: str,
) -> tuple[str, str, str]:
    return decision, rule_id, reason


def _review_revenue_days(
    *,
    candidate: float,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if 1990 <= candidate <= date.today().year + 2:
        return _direct_result(
            "REJECT",
            "revenue_days_year_as_value",
            "calendar_year_was_misparsed_as_revenue_days",
        )
    if (
        candidate < 30
        or candidate > 100_000
        or abs(candidate - round(candidate)) > 1e-6
    ):
        return _direct_result(
            "REJECT",
            "revenue_days_bounds",
            "candidate_is_not_a_plausible_revenue_day_count",
        )
    if re.search(
        r"\b365\s+revenue days per annum\b|"
        r"\brevenue days are the total\b|future minimum|"
        r"\boff-?hire days\b|\bloss of hire\b|"
        r"\bbooked\b.{0,40}\bpercent of (?:its|our) revenue days\b|"
        r"\brevenue days lost\b",
        context,
        re.IGNORECASE,
    ):
        return _direct_result(
            "REJECT",
            "revenue_days_prohibited_definition_or_assumption",
            "definition_or_future_assumption_is_not_reported_revenue_days",
        )
    value = re.escape(raw.strip())
    if not re.search(
        rf"\btotal revenue days\b.{{0,60}}{value}|"
        rf"\b(?:we|the company)\s+(?:had|recorded|generated)\s+"
        rf"{value}\s+revenue days\b",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "REJECT",
            "revenue_days_not_total_or_issuer_reported",
            "candidate_is_segmental_assumed_or_not_explicit_total_revenue_days",
        )
    if not _period_consistent(context, period_end):
        return _direct_result(
            "DEFER",
            "revenue_days_period_mismatch",
            "reported_value_is_plausible_but_period_requires_correction",
        )
    return _direct_result(
        "ACCEPT",
        "revenue_days_explicit_reported_count",
        "explicit_issuer_revenue_days_for_the_reporting_period",
    )


def _review_average_length_of_haul(
    *,
    candidate: float,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if not 25 <= candidate <= 5_000:
        return _direct_result(
            "REJECT",
            "average_length_of_haul_bounds",
            "candidate_is_not_a_plausible_distance",
        )
    if not re.search(r"\b(?:miles?|kilomet(?:er|re)s?)\b", context, re.I):
        return _direct_result(
            "REJECT",
            "average_length_of_haul_missing_distance_unit",
            "candidate_lacks_explicit_distance_unit",
        )
    if not re.search(
        r"\baverage length of haul\b.{0,180}"
        + re.escape(raw.strip()),
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "REJECT",
            "average_length_of_haul_unlinked_number",
            "candidate_is_not_linked_to_the_average_length_table_row",
        )
    if not _period_consistent(context, period_end):
        return _direct_result(
            "DEFER",
            "average_length_of_haul_period_mismatch",
            "distance_is_plausible_but_period_requires_correction",
        )
    return _direct_result(
        "ACCEPT",
        "average_length_of_haul_explicit_distance",
        "explicit_average_length_of_haul_with_distance_unit",
    )


def _review_vessel_count(
    *,
    candidate: float,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if (
        not 1 <= candidate <= 500
        or abs(candidate - round(candidate)) > 1e-6
    ):
        return _direct_result(
            "REJECT",
            "vessel_count_bounds",
            "candidate_is_not_a_plausible_vessel_count",
        )
    value = re.escape(raw.strip())
    positive = (
        rf"\b(?:our|the company(?:'s)?)\s+"
        rf"(?:(?:owned|consolidated)\s+)?fleet\s+"
        rf"(?:consisted|consists|comprised)\s+of\s+"
        rf"(?:a\s+total\s+of\s+|\(i\)\s*)?{value}\s+"
        rf"(?:operating\s+)?vessels?\b|"
        rf"\b(?:our|the company(?:'s)?)\s+fleet\s+of\s+"
        rf"{value}\s+vessels?\b"
    )
    if not re.search(positive, context, re.IGNORECASE | re.DOTALL):
        return _direct_result(
            "REJECT",
            "vessel_count_nonissuer_or_unlinked",
            "candidate_is_not_an_explicit_issuer_fleet_vessel_count",
        )
    if not _period_consistent(context, period_end):
        return _direct_result(
            "DEFER",
            "vessel_count_period_mismatch",
            "vessel_count_is_plausible_but_period_requires_correction",
        )
    return _direct_result(
        "ACCEPT",
        "vessel_count_explicit_issuer_fleet",
        "explicit_issuer_fleet_count_with_reporting_date",
    )


def _review_fleet_capacity(
    *,
    candidate: float,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if candidate < 1_000:
        return _direct_result(
            "REJECT",
            "fleet_capacity_bounds",
            "candidate_is_a_ratio_age_or_count_not_fleet_capacity",
        )
    if not re.search(
        r"\b(?:dwt|deadweight|teu|cubic meters?|cbm|passenger seats?)\b",
        context,
        re.IGNORECASE,
    ):
        return _direct_result(
            "REJECT",
            "fleet_capacity_missing_native_unit",
            "candidate_lacks_fleet_capacity_unit",
        )
    if re.search(
        r"\b(?:between|ranging from)\b.{0,100}"
        r"\b(?:dwt|deadweight|teu|cubic meters?|cbm)\b|"
        r"\b(?:newbuild|newbuilding|on order|orderbook|"
        r"to acquire|under construction)\b",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "REJECT",
            "fleet_capacity_prohibited_range_or_orderbook",
            "individual_vessel_range_or_forward_orderbook_is_not_total_fleet_capacity",
        )
    value = re.escape(raw.strip())
    if not re.search(
        rf"\b(?:total|combined|aggregate)\s+"
        rf"(?:(?:fleet|cargo|carrying)\s+)*capacity\b.{{0,80}}{value}"
        rf".{{0,30}}\b(?:dwt|deadweight|teu|cubic meters?|cbm)\b|"
        rf"\btotal\s+(?:dwt|deadweight|teu|cubic meters?|cbm)\s+"
        rf"capacity\b.{{0,40}}{value}|"
        rf"\bfleet\b.{{0,100}}\bwith\s+an?\s+aggregate\s+"
        rf"(?:cargo\s+|carrying\s+)?capacity\s+of\s+{value}"
        rf".{{0,30}}\b(?:dwt|deadweight|teu|cubic meters?|cbm)\b",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "REJECT",
            "fleet_capacity_unlinked_number",
            "candidate_is_not_linked_to_issuer_fleet_capacity",
        )
    if _NONISSUER.search(context) and not _ISSUER.search(context):
        return _direct_result(
            "REJECT",
            "fleet_capacity_nonissuer",
            "market_or_world_fleet_capacity_is_prohibited",
        )
    if not _period_consistent(context, period_end):
        return _direct_result(
            "DEFER",
            "fleet_capacity_period_mismatch",
            "capacity_is_plausible_but_period_requires_correction",
        )
    return _direct_result(
        "ACCEPT",
        "fleet_capacity_explicit_native_capacity",
        "explicit_issuer_total_fleet_capacity_with_native_unit",
    )


def _review_tce_day_rate(
    *,
    candidate: float,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if 1990 <= candidate <= date.today().year + 2:
        return _direct_result(
            "REJECT",
            "tce_day_rate_year_as_value",
            "calendar_year_was_misparsed_as_tce_rate",
        )
    if not 500 <= candidate <= 500_000:
        return _direct_result(
            "REJECT",
            "tce_day_rate_bounds",
            "candidate_is_not_a_plausible_daily_tce_rate",
        )
    if re.search(
        r"\b(?:impairment analysis|estimated daily|break even rate)\b",
        context,
        re.IGNORECASE,
    ):
        return _direct_result(
            "REJECT",
            "tce_day_rate_prohibited_estimate",
            "estimated_impairment_or_break_even_rate_is_not_reported_tce",
        )
    if re.search(
        r"\b(?:freight derivative|economic hedge|fixing the equivalent|"
        r"contracted rate)\b|"
        + re.escape(raw.strip())
        + r".{0,45}\b(?:lower|higher|increase|decrease)\b",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "REJECT",
            "tce_day_rate_prohibited_contract_or_delta",
            "contract_hedge_or_rate_delta_is_not_reported_operating_tce",
        )
    if not re.search(
        r"\b(?:tce|time charter equivalent)\b",
        context,
        re.IGNORECASE,
    ):
        return _direct_result(
            "REJECT",
            "tce_day_rate_missing_metric_label",
            "candidate_lacks_tce_label",
        )
    value = re.escape(raw.strip())
    linked = re.search(
        rf"\b(?:average\s+|daily\s+|blended\s+average\s+|"
        rf"fleetwide\s+)?(?:tce|time charter equivalent)\s+rate"
        rf"(?:\s+achieved)?\s*(?:of|was|at|:|\|)?\s*"
        rf"{value}(?:.{{0,35}}\bper day\b)?|"
        rf"\b(?:achieved|earning|earned)\b.{{0,80}}"
        rf"\b(?:average\s+|daily\s+|blended\s+average\s+|"
        rf"fleetwide\s+)?(?:tce|time charter equivalent)\s+rate"
        rf".{{0,35}}{value}",
        context,
        re.IGNORECASE | re.DOTALL,
    )
    if not linked:
        return _direct_result(
            "REJECT",
            "tce_day_rate_unlinked_number",
            "candidate_is_not_linked_to_reported_tce_rate",
        )
    if not _period_consistent(context, period_end):
        return _direct_result(
            "DEFER",
            "tce_day_rate_period_mismatch",
            "reported_tce_is_plausible_but_period_requires_correction",
        )
    return _direct_result(
        "ACCEPT",
        "tce_day_rate_explicit_reported_rate",
        "explicit_issuer_tce_rate_for_the_reporting_period",
    )


def _review_fuel_surcharge_ratio(
    *,
    candidate: float,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if not 0 <= candidate <= 1:
        return _direct_result(
            "REJECT",
            "fuel_surcharge_ratio_bounds",
            "candidate_is_not_a_ratio",
        )
    value = re.escape(raw.strip())
    if not re.search(
        rf"\bfuel surcharge revenues?\b.{{0,90}}"
        rf"(?:accounted for|represented|were)\b.{{0,40}}{value}",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "REJECT",
            "fuel_surcharge_ratio_wrong_percentage",
            "growth_or_pricing_percentage_is_not_fuel_surcharge_share",
        )
    if not re.search(r"\b(?:total|freight)?\s*revenues?\b", context, re.I):
        return _direct_result(
            "REJECT",
            "fuel_surcharge_ratio_missing_denominator",
            "fuel_surcharge_percentage_lacks_revenue_denominator",
        )
    if not _period_consistent(context, period_end):
        return _direct_result(
            "DEFER",
            "fuel_surcharge_ratio_period_mismatch",
            "ratio_is_plausible_but_period_requires_correction",
        )
    return _direct_result(
        "ACCEPT",
        "fuel_surcharge_ratio_explicit_revenue_share",
        "explicit_fuel_surcharge_revenue_share",
    )


def _explicit_percent(raw: str) -> bool:
    return "%" in raw or "percent" in raw.lower()


def _standard_fiscal_period_end(period_end: str) -> bool:
    return str(period_end or "")[4:10] in {
        "-03-31",
        "-06-30",
        "-09-30",
        "-12-31",
    }


def _trusted_filing_period(row: Mapping[str, object]) -> bool:
    period_end = str(row.get("period_end") or "")[:10]
    filing_date = str(row.get("filing_date") or "")[:10]
    return (
        str(row.get("form_type") or "").upper()
        in {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F"}
        and _standard_fiscal_period_end(period_end)
        and bool(str(row.get("accession_number") or ""))
        and not str(row.get("accession_number") or "").startswith("NONSEC-")
        and (not filing_date or filing_date >= period_end)
    )


def _respectively_current_value(
    *,
    context: str,
    period_end: str,
) -> float | None:
    match = re.search(
        r"\bDuring\s+"
        r"(?P<years>(?:(?:19|20)\d{2}\s*,?\s*(?:and\s+)?){2,5})"
        r"\s*,?\s*we had\s+(?:an?\s+)?(?:total\s+)?"
        r"fleet utilization(?:\s+rate)?\s+of\s+"
        r"(?P<values>.{0,130}?)\s*,?\s*respectively\b",
        context,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return None
    years = _YEAR.findall(match.group("years"))
    values = [
        float(value) / 100.0
        for value in re.findall(
            r"(-?\d+(?:\.\d+)?)\s*(?:%|percent)",
            match.group("values"),
            re.IGNORECASE,
        )
    ]
    expected_year = str(period_end or "")[:4]
    if (
        len(years) != len(values)
        or expected_year not in years
        or not values
    ):
        return None
    return values[years.index(expected_year)]


def _review_passenger_load_factor(
    *,
    candidate: float,
    context: str,
    raw: str,
    period_end: str,
    metadata_period_confirmed: bool = False,
) -> tuple[str, str, str]:
    if not 0.2 <= candidate <= 1.0 or not _explicit_percent(raw):
        return _direct_result(
            "REJECT",
            "passenger_load_factor_bounds_or_unit",
            "candidate_is_not_an_explicit_load_factor_percentage",
        )
    if re.search(
        r"\b(?:outlook|guidance|anticipated|anticipates|forecast|expected)\b",
        context,
        re.IGNORECASE,
    ):
        return _direct_result(
            "DEFER",
            "passenger_load_factor_forward_looking",
            "outlook_or_guidance_is_not_a_reported_period_load_factor",
        )
    value = re.escape(raw.strip())
    if re.search(
        value + r"\s*(?:percentage\s+)?(?:pts?|points?)\b",
        context,
        re.IGNORECASE,
    ):
        return _direct_result(
            "REJECT",
            "passenger_load_factor_change_not_level",
            "percentage_point_change_is_not_the_load_factor_level",
        )
    if re.search(
        r"\bload factor\b.{0,45}\bpercentage points?\b|"
        r"\bcargo load factor\b",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "REJECT",
            "passenger_load_factor_change_or_cargo_scope",
            "percentage_point_change_or_cargo_scope_is_not_passenger_load_level",
        )
    if re.search(
        rf"\bload factor\b.{{0,80}}\bfrom\s+{value}.{{0,100}}\bto\b",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "DEFER",
            "passenger_load_factor_prior_comparator",
            "candidate_is_the_prior_comparator_not_the_current_period_value",
        )
    if re.search(
        r"\b(?:domestic|international|regional)\s+load factor\b|"
        r"\bload factor\b.{0,70}\b(?:month of|seven day|holiday)\b|"
        r"\bdomestic operations\b.{0,180}\bload factor\b",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "DEFER",
            "passenger_load_factor_subperiod_or_segment",
            "monthly_or_segment_load_factor_is_not_the_fiscal_period_total",
        )
    if re.search(
        rf"\bload factor\b.{{0,45}}\b(?:increased|decreased|improved|"
        rf"declined)\b.{{0,35}}{value}\s*(?:pts?|points?)\b",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "REJECT",
            "passenger_load_factor_change_not_level",
            "percentage_point_change_is_not_the_load_factor_level",
        )
    linked = re.search(
        rf"\b(?:(?:system|passenger|scheduled service|average)\s+)?"
        rf"load factor\b.{{0,65}}"
        rf"(?:was|of|at|to|came in at|averaged|:|\|)?\s*{value}",
        context,
        re.IGNORECASE | re.DOTALL,
    )
    if not linked:
        return _direct_result(
            "DEFER",
            "passenger_load_factor_exact_relation_unconfirmed",
            "candidate_is_near_load_factor_but_not_the_explicit_period_level",
        )
    if (
        not metadata_period_confirmed
        and not _period_consistent(context, period_end)
    ):
        return _direct_result(
            "DEFER",
            "passenger_load_factor_period_mismatch",
            "load_factor_level_is_explicit_but_period_requires_correction",
        )
    return _direct_result(
        "ACCEPT",
        "passenger_load_factor_explicit_period_level",
        "explicit_fiscal_period_passenger_load_factor_level",
    )


def _review_capacity_growth(
    *,
    candidate: float,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if not -1.0 <= candidate <= 2.0 or not _explicit_percent(raw):
        return _direct_result(
            "REJECT",
            "capacity_growth_bounds_or_unit",
            "candidate_is_not_an_explicit_capacity_growth_percentage",
        )
    if _FORWARD.search(context) or re.search(
        r"\b(?:outlook|guidance|anticipated|anticipates)\b",
        context,
        re.IGNORECASE,
    ):
        return _direct_result(
            "DEFER",
            "capacity_growth_forward_looking",
            "guidance_or_expected_capacity_is_not_reported_period_growth",
        )
    if re.search(
        r"\bper\s+(?:available seat mile|asm)\b.{0,45}"
        r"\b(?:increased|decreased|grew|declined)\b",
        context,
        re.IGNORECASE,
    ):
        return _direct_result(
            "REJECT",
            "capacity_growth_per_asm_false_positive",
            "per_asm_change_is_not_capacity_growth",
        )
    value = re.escape(raw.strip())
    if (
        candidate >= 0
        and re.search(
            rf"\b(?:capacity|available seat miles?|asms?)\b.{{0,80}}"
            rf"\b(?:down|decreased|declined)\b.{{0,30}}{value}",
            context,
            re.IGNORECASE | re.DOTALL,
        )
    ):
        return _direct_result(
            "DEFER",
            "capacity_growth_sign_correction_required",
            "stored_positive_candidate_represents_a_reported_capacity_decline",
        )
    linked = re.search(
        rf"\bcapacity growth\b\s*(?:of|was|at|:)?\s*{value}|"
        rf"{value}\s+(?:year-over-year\s+)?capacity growth\b|"
        rf"\bcapacity(?:\s+as measured by\s+(?:available seat miles?|"
        rf"asms?))?\s+(?:was\s+)?(?:increased|grew)\s+"
        rf"(?:by\s+)?{value}",
        context,
        re.IGNORECASE | re.DOTALL,
    )
    if not linked:
        return _direct_result(
            "DEFER",
            "capacity_growth_exact_relation_unconfirmed",
            "candidate_is_not_explicitly_linked_to_reported_capacity_growth",
        )
    if not _period_consistent(context, period_end):
        return _direct_result(
            "DEFER",
            "capacity_growth_period_mismatch",
            "capacity_growth_is_explicit_but_period_requires_correction",
        )
    return _direct_result(
        "ACCEPT",
        "capacity_growth_explicit_period_change",
        "explicit_reported_capacity_growth_for_the_fiscal_period",
    )


def _review_equipment_utilization(
    *,
    candidate: float,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if not 0.5 <= candidate <= 1.0 or not _explicit_percent(raw):
        return _direct_result(
            "DEFER",
            "equipment_utilization_exact_level_absent",
            "candidate_is_not_an_explicit_equipment_utilization_level",
        )
    value = re.escape(raw.strip())
    if not re.search(
        rf"\bequipment utilization(?:\s+rate)?\b.{{0,55}}"
        rf"(?:was|of|at|to|remained|:|\|)?\s*{value}",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "DEFER",
            "equipment_utilization_exact_relation_unconfirmed",
            "fleet_segment_or_unlinked_percentage_is_not_equipment_utilization",
        )
    if not _period_consistent(context, period_end):
        return _direct_result(
            "DEFER",
            "equipment_utilization_period_mismatch",
            "utilization_level_is_explicit_but_period_requires_correction",
        )
    return _direct_result(
        "ACCEPT",
        "equipment_utilization_explicit_period_level",
        "explicit_issuer_equipment_utilization_for_the_fiscal_period",
    )


def _review_fleet_utilization(
    *,
    candidate: float,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if not 0.5 <= candidate <= 1.0 or not _explicit_percent(raw):
        return _direct_result(
            "REJECT",
            "fleet_utilization_bounds_or_unit",
            "candidate_is_not_an_explicit_fleet_utilization_percentage",
        )
    value = re.escape(raw.strip())
    if re.search(
        r"\bduring the (?:two|three|four|five) years ended\b",
        context,
        re.IGNORECASE,
    ):
        return _direct_result(
            "DEFER",
            "fleet_utilization_multiyear_average",
            "multi_year_average_is_not_a_single_fiscal_period_observation",
        )
    respectively_value = _respectively_current_value(
        context=context,
        period_end=period_end,
    )
    if (
        respectively_value is not None
        and abs(candidate - respectively_value) > 1e-6
    ):
        return _direct_result(
            "DEFER",
            "fleet_utilization_respectively_mapping_mismatch",
            "candidate_does_not_map_to_the_current_period_in_the_value_list",
        )
    if re.search(
        rf"\bfleet utilization(?:\s+rate)?\b.{{0,85}}"
        rf"\bfrom\s+{value}.{{0,100}}\bto\b",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "DEFER",
            "fleet_utilization_prior_comparator",
            "candidate_is_the_prior_comparator_not_the_current_period_value",
        )
    if re.search(
        rf"\b(?:operational|commercial)\s+fleet utilization"
        rf"(?:\s+rate)?\b.{{0,55}}{value}",
        context,
        re.IGNORECASE | re.DOTALL,
    ):
        return _direct_result(
            "DEFER",
            "fleet_utilization_scope_variant",
            "operational_or_commercial_variant_is_not_comparable_total_fleet_scope",
        )
    linked = re.search(
        rf"\b(?:total\s+)?fleet utilization(?:\s+rate)?\b.{{0,70}}"
        rf"(?:was|of|at|to|increased to|decreased to|remained|:|\|)?"
        rf"\s*{value}",
        context,
        re.IGNORECASE | re.DOTALL,
    )
    if not linked:
        return _direct_result(
            "DEFER",
            "fleet_utilization_exact_relation_unconfirmed",
            "candidate_is_not_the_explicit_comparable_fleet_utilization_level",
        )
    if not _period_consistent(context, period_end):
        return _direct_result(
            "DEFER",
            "fleet_utilization_period_mismatch",
            "fleet_utilization_is_explicit_but_period_requires_correction",
        )
    return _direct_result(
        "ACCEPT",
        "fleet_utilization_explicit_period_level",
        "explicit_comparable_fleet_utilization_for_the_fiscal_period",
    )


def _generic_review(
    *,
    metric_id: str,
    candidate: float,
    unit: str,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if _NONISSUER.search(context) and not _ISSUER.search(context):
        return _direct_result(
            "REJECT",
            "generic_nonissuer_scope",
            "industry_market_or_peer_value_is_prohibited",
        )
    if (
        1990 <= candidate <= date.today().year + 2
        and unit not in {"days"}
    ):
        return _direct_result(
            "REJECT",
            "generic_year_as_value",
            "calendar_year_was_misparsed_as_metric_value",
        )
    if _FORWARD.search(context):
        return _direct_result(
            "DEFER",
            "generic_forward_looking_context",
            "target_estimate_or_forward_context_requires_manual_fixture",
        )
    if not _period_consistent(context, period_end):
        return _direct_result(
            "DEFER",
            "generic_period_mismatch",
            "metric_value_is_plausible_but_period_requires_correction",
        )

    escaped = re.escape(raw.strip())
    if unit == "ratio":
        if "%" not in raw and "percent" not in raw.lower():
            return _direct_result(
                "REJECT",
                "generic_ratio_missing_percent",
                "ratio_candidate_lacks_explicit_percentage",
            )
        if "growth" in metric_id and not re.search(
            r"\b(?:increased|decreased|grew|declined|growth|change)\b",
            context,
            re.IGNORECASE,
        ):
            return _direct_result(
                "REJECT",
                "generic_growth_missing_comparison",
                "growth_ratio_lacks_explicit_period_comparison",
            )
        return _direct_result(
            "ACCEPT",
            "generic_explicit_ratio",
            "exact_metric_alias_is_linked_to_explicit_percentage",
        )
    if unit in {"count", "count_and_currency"}:
        if abs(candidate - round(candidate)) > 1e-6 or candidate < 0:
            return _direct_result(
                "REJECT",
                "generic_count_noninteger",
                "count_metric_candidate_is_not_a_nonnegative_integer",
            )
        return _direct_result(
            "ACCEPT",
            "generic_explicit_count",
            "exact_metric_alias_is_linked_to_explicit_count",
        )
    if unit in {"years", "days", "hours_per_day", "distance"}:
        marker = {
            "years": r"\byears?\b",
            "days": r"\bdays?\b",
            "hours_per_day": r"\bhours?.{0,15}(?:day|daily)\b",
            "distance": r"\b(?:miles?|kilomet(?:er|re)s?)\b",
        }[unit]
        if not re.search(marker, context, re.IGNORECASE):
            return _direct_result(
                "REJECT",
                "generic_duration_or_distance_missing_unit",
                "candidate_lacks_required_duration_or_distance_unit",
            )
        return _direct_result(
            "ACCEPT",
            "generic_explicit_duration_or_distance",
            "exact_metric_alias_is_linked_to_explicit_unit",
        )
    if "currency" in unit or "_per_" in unit:
        if not re.search(r"(?:US\$|C\$|A\$|\$|€|£)", raw):
            return _direct_result(
                "REJECT",
                "generic_currency_missing_symbol",
                "currency_metric_candidate_lacks_currency_lineage",
            )
        if "_per_" in unit and not re.search(
            r"\bper\b", context, re.IGNORECASE
        ):
            return _direct_result(
                "REJECT",
                "generic_rate_missing_denominator",
                "rate_candidate_lacks_explicit_denominator",
            )
        return _direct_result(
            "ACCEPT",
            "generic_explicit_currency_or_rate",
            "exact_metric_alias_is_linked_to_currency_and_denominator",
        )
    if unit == "segment_native_capacity":
        if candidate < 1_000 or not re.search(
            r"\b(?:dwt|deadweight|teu|cubic meters?|cbm)\b",
            context,
            re.IGNORECASE,
        ):
            return _direct_result(
                "REJECT",
                "generic_capacity_missing_native_unit",
                "capacity_candidate_lacks_native_capacity_unit",
            )
        return _direct_result(
            "ACCEPT",
            "generic_explicit_native_capacity",
            "exact_metric_alias_is_linked_to_native_capacity",
        )
    if escaped and len(context) > 0:
        return _direct_result(
            "DEFER",
            "generic_unsupported_unit_contract",
            "semantic_contract_requires_metric_specific_fixture",
        )
    return _direct_result(
        "DEFER",
        "generic_no_decision",
        "insufficient_semantic_information",
    )


def _review_single_value_specific(
    *,
    metric_id: str,
    candidate: float,
    text: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if (
        not _period_has_lineage(text)
        or not _period_consistent(text, period_end)
    ):
        return _direct_result(
            "DEFER",
            f"{metric_id}_period_lineage_unconfirmed",
            "explicit_single_value_metric_lacks_exact_reporting_period",
        )
    value = re.escape(raw.strip())
    patterns = {
        "charter_coverage_next_12m": (
            rf"(?:representing\s+)?{value}\s+charter coverage\b|"
            rf"\bcharter coverage\b.{{0,50}}{value}"
        ),
        "completion_factor": (
            rf"\b(?:system\s+)?completion factor\b.{{0,35}}"
            rf"(?:decreased|increased)?\s*(?:to|was|of)?\s*{value}"
        ),
        "fleet_age": (
            rf"\b(?:our|the company(?:'s)?)\s+average\s+"
            rf"(?:\w+\s+)?fleet age\b.{{0,45}}{value}\s+years?\b"
        ),
        "rail_intermodal_volume_growth": (
            rf"\bintermodal volume growth\s+of\s+{value}"
        ),
    }
    pattern = patterns[metric_id]
    if (
        metric_id == "charter_coverage_next_12m"
        and (
            re.search(r"\b(?:minimum|target|goal)\b", text, re.I)
            or not re.search(
                r"\b(?:next|following)\s+(?:twelve|12)\s+months\b",
                text,
                re.I,
            )
        )
    ):
        return _direct_result(
            "DEFER",
            "charter_coverage_horizon_not_exact",
            "coverage_is_not_reported_actual_for_the_next_twelve_months",
        )
    if (
        metric_id == "rail_intermodal_volume_growth"
        and re.search(r"\brespectively\b", text, re.I)
    ):
        return _direct_result(
            "DEFER",
            "rail_intermodal_respectively_mapping_ambiguous",
            "multiple_growth_values_require_column_or_order_resolution",
        )
    if not re.search(pattern, text, re.IGNORECASE | re.DOTALL):
        return _direct_result(
            "DEFER",
            f"{metric_id}_exact_relation_unconfirmed",
            "candidate_is_near_the_metric_but_not_in_an_exact_relation",
        )
    return _direct_result(
        "ACCEPT",
        f"{metric_id}_explicit_single_value",
        "exact_metric_value_and_reporting_period_are_explicit",
    )


def _conservative_generic_review(
    *,
    metric_id: str,
    candidate: float,
    unit: str,
    context: str,
    raw: str,
    period_end: str,
) -> tuple[str, str, str]:
    if _FORWARD.search(context):
        rule_id = "generic_forward_looking_context"
        reason = "target_estimate_or_forward_context_requires_manual_fixture"
    elif not _period_consistent(context, period_end):
        rule_id = "generic_period_mismatch"
        reason = "metric_value_is_plausible_but_period_requires_correction"
    elif _NONISSUER.search(context) and not _ISSUER.search(context):
        rule_id = "generic_nonissuer_scope"
        reason = "generic_metric_requires_an_exact_nonissuer_fixture"
    else:
        rule_id = "generic_metric_specific_fixture_required"
        reason = (
            "non_top_six_metric_requires_explicit_metric_specific_fixture;"
            f"metric_id={metric_id};unit={unit};candidate={candidate};raw={raw}"
        )
    return _direct_result("DEFER", rule_id, reason)


def review_fixture_evidence(
    row: Mapping[str, object],
    *,
    aliases: Sequence[str],
) -> tuple[str, str, str]:
    value = _float(row.get("candidate_value"))
    if value is None:
        return _direct_result(
            "DEFER",
            "non_numeric_context_only",
            "text_only_evidence_cannot_receive_exact_numeric_policy",
        )
    text = str(row.get("evidence_text") or "")
    unit = str(row.get("unit") or "")
    alias_candidates = tuple(
        dict.fromkeys(
            (
                str(row.get("source_metric_id") or "").replace("_", " "),
                *aliases,
            )
        )
    )
    number_spans = _number_spans(
        text,
        candidate=value,
        unit=unit,
    )
    alias_spans = _alias_spans(text, alias_candidates)
    nearest = _nearest_context(
        text,
        number_spans=number_spans,
        alias_spans=alias_spans,
    )
    if nearest is None:
        return _direct_result(
            "REJECT",
            "unlinked_numeric_candidate",
            "candidate_number_is_not_near_a_frozen_metric_alias",
        )
    context, raw = nearest
    metric_id = str(row.get("source_metric_id") or "")
    if metric_id in {
        "charter_coverage_next_12m",
        "completion_factor",
        "fleet_age",
        "rail_intermodal_volume_growth",
    }:
        return _review_single_value_specific(
            metric_id=metric_id,
            candidate=value,
            text=text,
            raw=raw,
            period_end=str(row.get("period_end") or ""),
        )
    if metric_id in {
        "passenger_load_factor",
        "capacity_growth",
        "equipment_utilization",
        "fleet_utilization",
        "revenue_days",
        "average_length_of_haul",
        "vessel_count",
        "fleet_capacity",
        "tce_day_rate",
        "fuel_surcharge_revenue_ratio",
    } and (
        not _period_has_lineage(text)
        or not _period_consistent(
            text,
            str(row.get("period_end") or ""),
        )
    ) and not (
        metric_id == "passenger_load_factor"
        and _trusted_filing_period(row)
    ):
        return _direct_result(
            "DEFER",
            f"{metric_id}_period_lineage_unconfirmed",
            "candidate_value_is_plausible_but_reporting_period_is_not_exact",
        )
    if metric_id in {
        "passenger_load_factor",
        "capacity_growth",
        "equipment_utilization",
        "fleet_utilization",
        "fuel_surcharge_revenue_ratio",
    } and not _standard_fiscal_period_end(
        str(row.get("period_end") or "")
    ):
        return _direct_result(
            "DEFER",
            f"{metric_id}_nonstandard_fiscal_period_end",
            "filing_or_release_date_cannot_substitute_for_fiscal_period_end",
        )
    if metric_id == "passenger_load_factor":
        return _review_passenger_load_factor(
            candidate=value,
            context=context,
            raw=raw,
            period_end=str(row.get("period_end") or ""),
            metadata_period_confirmed=_trusted_filing_period(row),
        )
    kwargs = {
        "candidate": value,
        "context": context,
        "raw": raw,
        "period_end": str(row.get("period_end") or ""),
    }
    if metric_id == "revenue_days":
        return _review_revenue_days(**kwargs)
    if metric_id == "average_length_of_haul":
        return _review_average_length_of_haul(**kwargs)
    if metric_id == "vessel_count":
        return _review_vessel_count(**kwargs)
    if metric_id == "fleet_capacity":
        return _review_fleet_capacity(**kwargs)
    if metric_id == "tce_day_rate":
        return _review_tce_day_rate(**kwargs)
    if metric_id == "fuel_surcharge_revenue_ratio":
        return _review_fuel_surcharge_ratio(**kwargs)
    if metric_id == "capacity_growth":
        return _review_capacity_growth(**kwargs)
    if metric_id == "equipment_utilization":
        return _review_equipment_utilization(**kwargs)
    if metric_id == "fleet_utilization":
        return _review_fleet_utilization(**kwargs)
    return _conservative_generic_review(
        metric_id=metric_id,
        candidate=value,
        unit=unit,
        context=context,
        raw=raw,
        period_end=str(row.get("period_end") or ""),
    )


def _fixture_value_override(
    row: Mapping[str, object],
) -> float | None:
    if str(row.get("source_metric_id") or "") != "fleet_utilization":
        return None
    candidate = _float(row.get("candidate_value"))
    if candidate is None:
        return None
    current_value = _respectively_current_value(
        context=str(row.get("evidence_text") or ""),
        period_end=str(row.get("period_end") or ""),
    )
    if (
        current_value is None
        or not 0.5 <= current_value <= 1.0
        or abs(candidate - current_value) <= 1e-6
    ):
        return None
    return current_value


def _derived_pair_accepts(
    metric_id: str,
    accepted_rows: Sequence[Mapping[str, object]],
) -> tuple[bool, str]:
    rule = PARSER_DERIVATIONS[metric_id]
    dependencies = tuple(str(value) for value in rule["dependencies"])
    periods: dict[str, set[str]] = defaultdict(set)
    for row in accepted_rows:
        period_end = str(row.get("period_end") or "")[:10]
        if period_end:
            periods[str(row.get("source_metric_id") or "")].add(
                period_end
            )
    mode = str(rule["mode"])
    if mode == "any":
        passed = any(periods.get(metric) for metric in dependencies)
        return passed, "any_dependency_accepted" if passed else "no_dependency_accepted"
    if mode == "all":
        if not all(periods.get(metric) for metric in dependencies):
            return False, "required_dependency_missing"
        aligned = set.intersection(
            *(set(periods[metric]) for metric in dependencies)
        )
        return bool(aligned), (
            "all_dependencies_period_aligned"
            if aligned
            else "dependency_periods_not_aligned"
        )
    minimum_periods = int(str(rule.get("minimum_periods") or 1))
    count = len(periods.get(dependencies[0], set()))
    return count >= minimum_periods, (
        "minimum_dependency_periods_met"
        if count >= minimum_periods
        else f"accepted_dependency_periods={count}_required={minimum_periods}"
    )


def build_fixture_review_decisions(
    *,
    pair_rows: Sequence[Mapping[str, object]],
    evidence_rows: Sequence[Mapping[str, object]],
    aliases: Mapping[str, Sequence[str]],
    reviewed_at: str,
    review_version: str = FIXTURE_REVIEW_VERSION,
    reviewed_by: str = REVIEWED_BY,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
    list[str],
]:
    selected_pairs = {
        str(row["pair_key"]): row
        for row in pair_rows
        if int(str(row["phase_rank"])) <= 3
    }
    by_pair: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in evidence_rows:
        if str(row["pair_key"]) in selected_pairs:
            by_pair[str(row["pair_key"])].append(row)
    errors: list[str] = []
    evidence_output: list[dict[str, object]] = []
    pair_output: list[dict[str, object]] = []
    for pair_key, pair in sorted(
        selected_pairs.items(),
        key=lambda item: int(str(item[1]["review_order"])),
    ):
        reviewed: list[dict[str, object]] = []
        for row in sorted(
            by_pair.get(pair_key, ()),
            key=lambda item: (
                not bool(str(item.get("candidate_value") or "")),
                str(item["evidence_key"]),
            ),
        ):
            source_metric_id = str(row["source_metric_id"])
            decision, rule_id, reason = review_fixture_evidence(
                row,
                aliases=aliases.get(source_metric_id, ()),
            )
            value_override = _fixture_value_override(row)
            if (
                value_override is not None
                and decision == "DEFER"
                and rule_id
                == "fleet_utilization_respectively_mapping_mismatch"
            ):
                decision = "ACCEPT"
                rule_id = (
                    "fleet_utilization_respectively_current_value_override"
                )
                reason = (
                    "current_period_value_is_deterministically_mapped_from_"
                    "the_year_and_value_lists"
                )
            fixture_polarity = {
                "ACCEPT": "POSITIVE",
                "REJECT": "PROHIBITED",
                "DEFER": "UNRESOLVED",
            }[decision]
            output_row: dict[str, object] = {
                "review_version": review_version,
                "review_order": pair["review_order"],
                "phase_rank": pair["phase_rank"],
                "review_phase": pair["review_phase"],
                "pair_key": pair_key,
                "fixture_id": pair["fixture_id"],
                "ticker": row["ticker"],
                "metric_id": row["metric_id"],
                "source_lane": pair["source_lane"],
                "source_metric_id": source_metric_id,
                "source_stage": row["source_stage"],
                "evidence_key": row["evidence_key"],
                "candidate_value": row["candidate_value"],
                "value_override": (
                    "" if value_override is None else value_override
                ),
                "unit": row["unit"],
                "period_end": row["period_end"],
                "scope": row["scope"],
                "accession_number": row["accession_number"],
                "source_document": row["source_document"],
                "semantic_decision": decision,
                "fixture_polarity": fixture_polarity,
                "rule_id": rule_id,
                "decision_reason": reason,
                "policy_eligible": int(
                    bool(str(row["candidate_value"]))
                    and (
                        decision == "ACCEPT"
                        or (
                            decision == "REJECT"
                            and rule_id
                            in _POLICY_ELIGIBLE_REJECT_RULES
                        )
                    )
                ),
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
                "evidence_row_sha256": row["evidence_row_sha256"],
                "evidence_text": row["evidence_text"],
                # Retained internally for dependency validation.
                "_period_end": row["period_end"],
                "_source_metric_id": source_metric_id,
            }
            reviewed.append(output_row)
            evidence_output.append(output_row)
        numeric_reviewed = [
            row
            for row in reviewed
            if bool(str(row.get("candidate_value") or ""))
        ]
        accepted = [
            row
            for row in numeric_reviewed
            if row["semantic_decision"] == "ACCEPT"
        ]
        rejected = [
            row
            for row in numeric_reviewed
            if row["semantic_decision"] == "REJECT"
        ]
        deferred = [
            row
            for row in numeric_reviewed
            if row["semantic_decision"] == "DEFER"
        ]
        source_lane = str(pair["source_lane"])
        dependency_status = "NOT_APPLICABLE"
        if source_lane == "DP-D":
            passed, dependency_status = _derived_pair_accepts(
                str(pair["metric_id"]),
                accepted,
            )
            if passed:
                pair_decision = "ACCEPT"
                pair_reason = "reviewed_dependencies_satisfy_derivation"
            elif deferred or accepted:
                pair_decision = "DEFER"
                pair_reason = "derivation_dependencies_incomplete"
            else:
                pair_decision = "REJECT"
                pair_reason = "all_reviewed_dependencies_prohibited"
        elif accepted:
            pair_decision = "ACCEPT"
            pair_reason = "at_least_one_positive_exact_semantic_fixture"
        elif deferred:
            pair_decision = "DEFER"
            pair_reason = "semantic_fixture_requires_manual_resolution"
        elif rejected and len(rejected) == len(numeric_reviewed):
            pair_decision = "REJECT"
            pair_reason = "all_numeric_fixture_candidates_prohibited"
        else:
            pair_decision = "DEFER"
            pair_reason = "no_policy_eligible_numeric_fixture"
        pair_output.append(
            {
                "review_version": review_version,
                "review_order": pair["review_order"],
                "phase_rank": pair["phase_rank"],
                "review_phase": pair["review_phase"],
                "pair_key": pair_key,
                "fixture_id": pair["fixture_id"],
                "ticker": pair["ticker"],
                "metric_id": pair["metric_id"],
                "source_lane": source_lane,
                "review_route": pair["review_route"],
                "pair_decision": pair_decision,
                "decision_reason": pair_reason,
                "numeric_evidence_count": len(numeric_reviewed),
                "accepted_evidence_count": len(accepted),
                "rejected_evidence_count": len(rejected),
                "deferred_evidence_count": len(deferred),
                "accepted_evidence_keys": "|".join(
                    str(row["evidence_key"]) for row in accepted
                ),
                "rejected_evidence_keys": "|".join(
                    str(row["evidence_key"]) for row in rejected
                ),
                "deferred_evidence_keys": "|".join(
                    str(row["evidence_key"]) for row in deferred
                ),
                "dependency_validation_status": dependency_status,
                "policy_eligible_evidence_count": len(accepted)
                + len(rejected),
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
            }
        )
    expected_pairs = len(selected_pairs)
    if len(pair_output) != expected_pairs:
        errors.append(
            f"reviewed pairs={len(pair_output)} expected={expected_pairs}"
        )
    public_evidence = [
        {
            key: value
            for key, value in row.items()
            if not key.startswith("_")
        }
        for row in evidence_output
    ]
    summary = {
        "selected_pair_count": expected_pairs,
        "reviewed_evidence_row_count": len(public_evidence),
        "numeric_reviewed_evidence_count": sum(
            bool(str(row.get("candidate_value") or ""))
            for row in public_evidence
        ),
        "pair_decision_counts": dict(
            sorted(
                Counter(
                    str(row["pair_decision"]) for row in pair_output
                ).items()
            )
        ),
        "evidence_decision_counts": dict(
            sorted(
                Counter(
                    str(row["semantic_decision"])
                    for row in public_evidence
                ).items()
            )
        ),
        "phase_pair_decision_counts": dict(
            sorted(
                Counter(
                    f"{row['review_phase']}|{row['pair_decision']}"
                    for row in pair_output
                ).items()
            )
        ),
        "policy_eligible_evidence_count": sum(
            int(str(row["policy_eligible"]))
            for row in public_evidence
        ),
    }
    return pair_output, public_evidence, summary, errors
