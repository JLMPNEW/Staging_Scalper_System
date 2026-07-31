from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from typing import Mapping, Sequence


BOUNDED_REPAIR_SCOPE_VERSION = (
    "transportation_dp6y_bounded_repair_scope_v1"
)
BOUNDED_REPAIR_EXECUTION_VERSION = (
    "transportation_dp6z_bounded_repair_execution_v1"
)

BOUNDED_REPAIR_SCOPE_FIELDS = (
    "scope_version",
    "repair_item_id",
    "repair_lane",
    "priority",
    "pair_key",
    "ticker",
    "metric_id",
    "content_sha256",
    "document_name",
    "evidence_key",
    "current_status",
    "requested_action",
    "input_basis",
    "terminal_if_unresolved",
)

FINANCIAL_EXECUTION_FIELDS = (
    "execution_version",
    "repair_id",
    "pair_key",
    "ticker",
    "metric_id",
    "repair_classification",
    "execution_status",
    "candidate_value",
    "unit_contract",
    "formula",
    "current_feature_period_end",
    "aligned_dependency_ids",
    "unresolved_dependency_ids",
    "coverage_override",
    "quality_flags",
    "provenance",
)

SOURCE_GAP_SEARCH_FIELDS = (
    "execution_version",
    "pair_key",
    "ticker",
    "metric_id",
    "searched_content_hash_count",
    "matched_content_hash_count",
    "matched_term_count",
    "numeric_proximity_count",
    "matched_terms",
    "matched_content_hashes",
    "execution_status",
    "required_next_action",
)

NO_VALUE_AUDIT_FIELDS = (
    "execution_version",
    "pair_key",
    "ticker",
    "metric_id",
    "stored_evidence_count",
    "non_numeric_evidence_count",
    "ambiguous_numeric_evidence_count",
    "short_label_only_count",
    "execution_status",
    "coverage_override",
    "required_next_action",
)

OCR_EXECUTION_FIELDS = (
    "execution_version",
    "content_sha256",
    "ticker_contexts",
    "document_name",
    "scoped_metric_ids",
    "ocr_engine",
    "execution_status",
    "coverage_override",
    "required_next_action",
)

CACHED_FINANCIAL_OVERRIDE_FIELDS = (
    "override_version",
    "pair_key",
    "ticker",
    "metric_id",
    "period_start",
    "period_end",
    "numerator_label",
    "numerator_value",
    "denominator_label",
    "denominator_value",
    "unit",
    "content_sha256",
    "document_name",
    "evidence_basis",
    "reviewed_by",
    "reviewed_at",
    "decision",
)


def _stable_id(prefix: str, payload: object) -> str:
    digest = hashlib.sha256(
        json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _pipe(value: object) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in str(value or "").split("|")
        if item.strip()
    )


def _scope_row(
    *,
    lane: str,
    priority: int,
    pair_key: str = "",
    ticker: str = "",
    metric_id: str = "",
    content_sha256: str = "",
    document_name: str = "",
    evidence_key: str = "",
    current_status: str,
    requested_action: str,
    input_basis: str,
    terminal_if_unresolved: bool,
) -> dict[str, object]:
    identity = {
        "lane": lane,
        "pair_key": pair_key,
        "ticker": ticker,
        "metric_id": metric_id,
        "content_sha256": content_sha256,
        "evidence_key": evidence_key,
    }
    return {
        "scope_version": BOUNDED_REPAIR_SCOPE_VERSION,
        "repair_item_id": _stable_id("trnbr", identity),
        "repair_lane": lane,
        "priority": priority,
        "pair_key": pair_key,
        "ticker": ticker,
        "metric_id": metric_id,
        "content_sha256": content_sha256,
        "document_name": document_name,
        "evidence_key": evidence_key,
        "current_status": current_status,
        "requested_action": requested_action,
        "input_basis": input_basis,
        "terminal_if_unresolved": int(terminal_if_unresolved),
    }


def build_bounded_repair_scope(
    *,
    financial_rows: Sequence[Mapping[str, str]],
    coverage_rows: Sequence[Mapping[str, str]],
    empty_context_rows: Sequence[Mapping[str, str]],
    adjudication_rows: Sequence[Mapping[str, str]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    financial_lane = {
        "ALIGNMENT_OR_FORMULA_PIPELINE_GAP": (
            "FINANCIAL_DETERMINISTIC_ALIGNMENT",
            1,
            "EVALUATE_ALIGNED_FEATURE_OPERANDS",
        ),
        "FORMULA_DEFINED_NOT_APPLICABLE": (
            "FINANCIAL_NOT_APPLICABLE",
            1,
            "RECLASSIFY_FORMULA_NOT_APPLICABLE",
        ),
        "SOURCE_OR_PERIOD_GAP": (
            "FINANCIAL_SOURCE_GAP",
            2,
            "SEARCH_EXISTING_CACHED_PRIMARY_DOCUMENTS",
        ),
    }
    for row in financial_rows:
        classification = str(row["repair_classification"])
        lane, priority, action = financial_lane[classification]
        output.append(
            _scope_row(
                lane=lane,
                priority=priority,
                pair_key=str(row["pair_key"]),
                ticker=str(row["ticker"]),
                metric_id=str(row["metric_id"]),
                current_status="FINANCIAL_INPUTS_MISSING",
                requested_action=action,
                input_basis=str(row["repair_id"]),
                terminal_if_unresolved=True,
            )
        )

    for row in coverage_rows:
        if str(row.get("coverage_status")) != "TEXT_HIT_NO_VALUE":
            continue
        output.append(
            _scope_row(
                lane="TEXT_HIT_NO_VALUE",
                priority=2,
                pair_key=(
                    f"{str(row['ticker']).upper()}|{row['metric_id']}"
                ),
                ticker=str(row["ticker"]).upper(),
                metric_id=str(row["metric_id"]),
                current_status="TEXT_HIT_NO_VALUE",
                requested_action=(
                    "AUDIT_STORED_EVIDENCE_FOR_STRICT_NUMERIC_RECOVERY"
                ),
                input_basis=(
                    f"run_id={row.get('run_id', '')};"
                    f"text_hits={row.get('text_hit_count', '0')}"
                ),
                terminal_if_unresolved=True,
            )
        )

    empty_by_hash: dict[str, list[Mapping[str, str]]] = {}
    for row in empty_context_rows:
        empty_by_hash.setdefault(
            str(row["content_sha256"]).lower(), []
        ).append(row)
    for content_hash, rows in sorted(empty_by_hash.items()):
        tickers = sorted(
            {str(row["ticker"]).upper() for row in rows}
        )
        metric_ids = sorted(
            {
                metric_id
                for row in rows
                for metric_id in _pipe(row["requested_metric_ids"])
            }
        )
        output.append(
            _scope_row(
                lane="EMPTY_PDF_OCR",
                priority=2,
                ticker="|".join(tickers),
                metric_id="|".join(metric_ids),
                content_sha256=content_hash,
                document_name=str(rows[0]["document_name"]),
                current_status="CACHE_VALIDATED_EMPTY_PYMUPDF",
                requested_action="OCR_ONLY_IF_LOCAL_ENGINE_AVAILABLE",
                input_basis="|".join(
                    sorted(
                        {
                            str(row.get("document_ids") or "")
                            for row in rows
                        }
                    )
                ),
                terminal_if_unresolved=True,
            )
        )

    for row in adjudication_rows:
        if str(row.get("review_decision")) != "DEFER":
            continue
        output.append(
            _scope_row(
                lane="STORED_EVIDENCE_REVIEW",
                priority=2
                + int(str(row.get("fixture_priority") or "2")),
                pair_key=str(row["pair_key"]),
                ticker=str(row["ticker"]).upper(),
                metric_id=str(row["metric_id"]),
                evidence_key=str(
                    row.get("representative_evidence_keys") or ""
                ),
                current_status="COVERED_REVIEW_REQUIRED",
                requested_action=(
                    "ADJUDICATE_STORED_EVIDENCE_WITHOUT_SOURCE_REPARSE"
                ),
                input_basis=str(
                    row.get("required_next_action") or ""
                ),
                terminal_if_unresolved=False,
            )
        )

    output.sort(
        key=lambda row: (
            int(str(row["priority"])),
            str(row["repair_lane"]),
            str(row["ticker"]),
            str(row["metric_id"]),
            str(row["content_sha256"]),
        )
    )
    ids = [str(row["repair_item_id"]) for row in output]
    if len(ids) != len(set(ids)):
        raise ValueError("Bounded repair scope item ids are duplicated")
    return output


def summarize_scope(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "repair_item_count": len(rows),
        "repair_lane_counts": dict(
            sorted(
                Counter(str(row["repair_lane"]) for row in rows).items()
            )
        ),
        "repair_ticker_count": len(
            {
                ticker
                for row in rows
                for ticker in _pipe(row.get("ticker"))
            }
        ),
        "repair_metric_count": len(
            {
                metric
                for row in rows
                for metric in _pipe(row.get("metric_id"))
            }
        ),
    }


def _finite(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _dependency_statuses(
    rows: Sequence[Mapping[str, str]],
) -> dict[str, str]:
    return {
        str(row["dependency_id"]): str(row["requirement_status"])
        for row in rows
    }


def execute_financial_repairs(
    *,
    pair_rows: Sequence[Mapping[str, str]],
    dependency_rows: Sequence[Mapping[str, str]],
    feature_rows: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    dependencies_by_pair: dict[
        str, list[Mapping[str, str]]
    ] = {}
    for row in dependency_rows:
        dependencies_by_pair.setdefault(
            str(row["pair_key"]), []
        ).append(row)
    output: list[dict[str, object]] = []
    for pair in pair_rows:
        pair_key = str(pair["pair_key"])
        ticker = str(pair["ticker"]).upper()
        metric_id = str(pair["metric_id"])
        classification = str(pair["repair_classification"])
        feature = feature_rows.get(ticker, {})
        dependencies = dependencies_by_pair.get(pair_key, [])
        statuses = _dependency_statuses(dependencies)
        aligned = sorted(
            dependency_id
            for dependency_id, status in statuses.items()
            if status == "PRESENT_IN_ALIGNED_FEATURE"
        )
        unresolved = sorted(set(statuses) - set(aligned))
        candidate: float | None = None
        coverage_override = ""
        quality_flags: list[str] = []

        if classification == "FORMULA_DEFINED_NOT_APPLICABLE":
            execution_status = "RECLASSIFIED_NOT_APPLICABLE"
            coverage_override = "NOT_APPLICABLE"
            unresolved = []
            quality_flags.append(
                "FORMULA_CONDITIONAL_APPLICABILITY_PROVEN"
            )
        elif classification == "SOURCE_OR_PERIOD_GAP":
            execution_status = "REQUIRES_CACHED_SOURCE_SEARCH"
        elif metric_id == "capital_raise_dependence":
            burn = _finite(feature.get("cash_burn_ttm_usd"))
            components: list[tuple[str, float]] = []
            for dependency_id, field in (
                (
                    "equity_issuance_ttm",
                    "equity_issuance_proceeds_ttm_usd",
                ),
                (
                    "debt_issuance_ttm",
                    "debt_issuance_proceeds_ttm_usd",
                ),
            ):
                value = _finite(feature.get(field))
                if (
                    value is not None
                    and statuses.get(dependency_id)
                    == "PRESENT_IN_ALIGNED_FEATURE"
                ):
                    components.append((dependency_id, value))
            if (
                burn is not None
                and statuses.get("cash_burn_ttm")
                == "PRESENT_IN_ALIGNED_FEATURE"
                and (burn <= 0 or components)
            ):
                candidate = (
                    0.0
                    if burn <= 0
                    else sum(value for _, value in components) / burn
                )
                execution_status = "RESOLVED_ALIGNED_FEATURE_FORMULA"
                coverage_override = "COVERED_FINANCIAL_DERIVED"
                unresolved = sorted(
                    set(statuses)
                    - {"cash_burn_ttm"}
                    - {dependency_id for dependency_id, _ in components}
                )
                if unresolved:
                    quality_flags.append(
                        "PARTIAL_CAPITAL_RAISE_COMPONENT_LOWER_BOUND"
                    )
            else:
                execution_status = "DEFERRED_PERIOD_ALIGNMENT_REQUIRED"
        elif metric_id == "cash_runway_years":
            cash = _finite(feature.get("cash_and_equivalents_usd"))
            burn = _finite(feature.get("cash_burn_ttm_usd"))
            if (
                cash is not None
                and burn is not None
                and burn > 0
                and not unresolved
            ):
                candidate = cash / burn
                execution_status = "RESOLVED_ALIGNED_FEATURE_FORMULA"
                coverage_override = "COVERED_FINANCIAL_DERIVED"
            else:
                execution_status = "DEFERRED_PERIOD_ALIGNMENT_REQUIRED"
        elif metric_id == "quarterly_cash_burn":
            burn = _finite(feature.get("cash_burn_ttm_usd"))
            if burn is not None and not unresolved:
                candidate = burn / 4.0
                execution_status = "RESOLVED_ALIGNED_FEATURE_FORMULA"
                coverage_override = "COVERED_FINANCIAL_DERIVED"
            else:
                execution_status = "DEFERRED_PERIOD_ALIGNMENT_REQUIRED"
        elif metric_id == "pre_revenue_flag":
            revenue = _finite(feature.get("revenue_ttm_usd"))
            if revenue is not None and not unresolved:
                candidate = 1.0 if revenue <= 0 else 0.0
                execution_status = "RESOLVED_ALIGNED_FEATURE_FORMULA"
                coverage_override = "COVERED_FINANCIAL_DERIVED"
            else:
                execution_status = "DEFERRED_PERIOD_ALIGNMENT_REQUIRED"
        elif metric_id == "stock_compensation_to_revenue":
            sbc = _finite(feature.get("stock_based_compensation"))
            revenue = _finite(feature.get("revenue"))
            periods = json.loads(
                str(pair.get("latest_dependency_periods_json") or "{}")
            )
            period_values = {
                str(value)
                for value in periods.values()
                if str(value)
            }
            if (
                sbc is not None
                and revenue is not None
                and revenue > 0
                and not unresolved
                and len(period_values) == 1
            ):
                candidate = sbc / revenue
                execution_status = "RESOLVED_ALIGNED_FEATURE_FORMULA"
                coverage_override = "COVERED_FINANCIAL_DERIVED"
            else:
                execution_status = "DEFERRED_PERIOD_ALIGNMENT_REQUIRED"
        else:
            execution_status = "DEFERRED_UNSUPPORTED_FORMULA"

        output.append(
            {
                "execution_version": BOUNDED_REPAIR_EXECUTION_VERSION,
                "repair_id": pair["repair_id"],
                "pair_key": pair_key,
                "ticker": ticker,
                "metric_id": metric_id,
                "repair_classification": classification,
                "execution_status": execution_status,
                "candidate_value": (
                    "" if candidate is None else candidate
                ),
                "unit_contract": pair["unit_contract"],
                "formula": pair["formula"],
                "current_feature_period_end": pair[
                    "current_feature_period_end"
                ],
                "aligned_dependency_ids": "|".join(aligned),
                "unresolved_dependency_ids": "|".join(unresolved),
                "coverage_override": coverage_override,
                "quality_flags": "|".join(sorted(quality_flags)),
                "provenance": (
                    "feature_financial_statement:"
                    f"{ticker}:2026-07-22"
                ),
            }
        )
    return output


_NUMBER = re.compile(
    r"(?<![A-Za-z])(?:[$€£]\s*)?"
    r"\(?-?\d[\d,]*(?:\.\d+)?\)?\s*%?"
)


def audit_no_value_pairs(
    *,
    coverage_rows: Sequence[Mapping[str, str]],
    evidence_by_pair: Mapping[
        tuple[str, str], Sequence[Mapping[str, object]]
    ],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in coverage_rows:
        if str(row.get("coverage_status")) != "TEXT_HIT_NO_VALUE":
            continue
        ticker = str(row["ticker"]).upper()
        metric_id = str(row["metric_id"])
        evidence = list(evidence_by_pair.get((ticker, metric_id), ()))
        nonnumeric = 0
        ambiguous = 0
        short_label = 0
        for item in evidence:
            text = str(item.get("evidence_text") or "").strip()
            numbers = _NUMBER.findall(text)
            if not numbers:
                nonnumeric += 1
            else:
                ambiguous += 1
            if len(text) <= 80:
                short_label += 1
        output.append(
            {
                "execution_version": BOUNDED_REPAIR_EXECUTION_VERSION,
                "pair_key": f"{ticker}|{metric_id}",
                "ticker": ticker,
                "metric_id": metric_id,
                "stored_evidence_count": len(evidence),
                "non_numeric_evidence_count": nonnumeric,
                "ambiguous_numeric_evidence_count": ambiguous,
                "short_label_only_count": short_label,
                "execution_status": (
                    "TERMINAL_STORED_EVIDENCE_AMBIGUOUS"
                    if ambiguous
                    else "TERMINAL_STORED_EVIDENCE_NON_NUMERIC"
                ),
                "coverage_override": "",
                "required_next_action": (
                    "MANUAL_TABLE_FIXTURE_ONLY_IF_METRIC_IS_PROMOTION_CRITICAL"
                    if short_label
                    else "NONE"
                ),
            }
        )
    return output


def apply_cached_financial_source_overrides(
    *,
    financial_rows: Sequence[Mapping[str, object]],
    override_rows: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, object]], int]:
    overrides = {
        str(row["pair_key"]): row
        for row in override_rows
        if str(row.get("decision") or "")
        == "ACCEPT_EXACT_SAME_PERIOD_UNIT"
    }
    if len(overrides) != len(
        [
            row
            for row in override_rows
            if str(row.get("decision") or "")
            == "ACCEPT_EXACT_SAME_PERIOD_UNIT"
        ]
    ):
        raise ValueError("Cached financial override pair keys are duplicated")
    output: list[dict[str, object]] = []
    applied: set[str] = set()
    for source in financial_rows:
        row = dict(source)
        pair_key = str(row["pair_key"])
        override = overrides.get(pair_key)
        if override is None:
            output.append(row)
            continue
        if (
            str(row["ticker"]).upper()
            != str(override["ticker"]).upper()
            or str(row["metric_id"]) != str(override["metric_id"])
            or str(row["execution_status"])
            != "REQUIRES_CACHED_SOURCE_SEARCH"
            or str(override["unit"]) != "ratio"
            or not str(override["period_end"])
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(override["content_sha256"]).lower(),
            )
        ):
            raise ValueError(
                f"Invalid cached financial override contract: {pair_key}"
            )
        numerator = _finite(override["numerator_value"])
        denominator = _finite(override["denominator_value"])
        if numerator is None or denominator is None or denominator <= 0:
            raise ValueError(
                f"Invalid cached financial override operands: {pair_key}"
            )
        candidate = numerator / denominator
        row.update(
            {
                "execution_status": (
                    "RESOLVED_CACHED_PRIMARY_SOURCE_FORMULA"
                ),
                "candidate_value": candidate,
                "current_feature_period_end": override["period_end"],
                "aligned_dependency_ids": (
                    f"{override['numerator_label']}|"
                    f"{override['denominator_label']}"
                ),
                "unresolved_dependency_ids": "",
                "coverage_override": "COVERED_FINANCIAL_DERIVED",
                "quality_flags": (
                    "MANUAL_EXACT_SAME_PERIOD_UNIT_ALIGNMENT"
                ),
                "provenance": (
                    "content_text_cache:"
                    f"{override['content_sha256']}:"
                    f"{override['period_end']}:"
                    f"{override['reviewed_by']}"
                ),
            }
        )
        applied.add(pair_key)
        output.append(row)
    missing = sorted(set(overrides) - applied)
    if missing:
        raise ValueError(
            f"Cached financial overrides did not match results: {missing}"
        )
    return output, len(applied)


def apply_financial_overrides(
    *,
    coverage_rows: Sequence[Mapping[str, str]],
    financial_rows: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    overrides = {
        str(row["pair_key"]): row
        for row in financial_rows
        if str(row.get("coverage_override") or "")
    }
    output: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    for source in coverage_rows:
        row: dict[str, object] = dict(source)
        key = f"{str(row['ticker']).upper()}|{row['metric_id']}"
        override = overrides.get(key)
        if override is None:
            output.append(row)
            continue
        status = str(override["coverage_override"])
        if status == "NOT_APPLICABLE":
            row.update(
                {
                    "applicability_status": "NOT_APPLICABLE",
                    "coverage_status": "NOT_APPLICABLE",
                    "derivation_basis": (
                        "bounded_financial_repair:"
                        "formula_defined_not_applicable"
                    ),
                    "text_hit_count": 0,
                    "value_candidate_count": 0,
                    "accepted_value_count": 0,
                    "review_value_count": 0,
                    "rejected_value_count": 0,
                    "parser_failure_count": 0,
                    "distinct_period_count": 0,
                    "first_period_end": "",
                    "last_period_end": "",
                }
            )
            counts["NOT_APPLICABLE"] += 1
        elif status == "COVERED_FINANCIAL_DERIVED":
            period = str(
                override.get("current_feature_period_end") or ""
            )
            row.update(
                {
                    "coverage_status": status,
                    "derivation_basis": (
                        "bounded_financial_repair:"
                        f"{override['repair_id']}"
                    ),
                    "text_hit_count": 1,
                    "value_candidate_count": 1,
                    "accepted_value_count": 1,
                    "review_value_count": 0,
                    "rejected_value_count": 0,
                    "parser_failure_count": 0,
                    "distinct_period_count": 1 if period else 0,
                    "first_period_end": period,
                    "last_period_end": period,
                }
            )
            counts["COVERED_FINANCIAL_DERIVED"] += 1
        else:
            raise ValueError(f"Unsupported financial override={status}")
        output.append(row)
    if len(overrides) != sum(counts.values()):
        raise ValueError("Not every financial override matched coverage")
    return output, dict(sorted(counts.items()))
