from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Mapping, Sequence

from industrials.transportation.parser_coverage import (
    PARSER_DERIVATIONS,
)


SEMANTIC_FIXTURE_FREEZE_VERSION = (
    "transportation_dp6l_semantic_fixture_freeze_v1"
)
SEMANTIC_METRIC_CONTRACT_FIELDS = (
    "freeze_version",
    "metric_id",
    "metric_pack",
    "source_lane",
    "component",
    "applicability_tags",
    "unit_contract",
    "period_type",
    "max_staleness_days",
    "bounds_policy",
    "formula",
    "source_metric_ids",
    "source_unit_contracts",
    "search_aliases",
    "semantic_class",
    "positive_fixture_rule",
    "prohibited_fixture_rule",
    "scope_rule",
    "period_rule",
    "unit_rule",
    "derived_dependency_rule",
    "acceptance_policy",
    "has_deferred_fixture_pairs",
    "semantic_contract_sha256",
)
SEMANTIC_PAIR_CONTRACT_FIELDS = (
    "freeze_version",
    "fixture_id",
    "pair_key",
    "fixture_priority",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "primary_archetype",
    "metric_id",
    "metric_pack",
    "source_lane",
    "source_metric_ids",
    "semantic_contract_sha256",
    "representative_evidence_count",
    "representative_evidence_keys",
    "evidence_bundle_sha256",
    "fixture_status",
    "downstream_parser_treatment",
    "acceptance_authorized",
    "policy_mutation_authorized",
    "retrieval_eligible_after_freeze",
)
SEMANTIC_EVIDENCE_FIELDS = (
    "freeze_version",
    "fixture_id",
    "evidence_row_sha256",
    "pair_key",
    "fixture_priority",
    "ticker",
    "metric_id",
    "source_metric_id",
    "source_stage",
    "evidence_key",
    "candidate_status",
    "candidate_value",
    "unit",
    "period_end",
    "scope",
    "confidence",
    "accession_number",
    "form_type",
    "filing_date",
    "source_document",
    "extraction_method",
    "status_reason",
    "evidence_text",
)


def stable_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _semantic_class(
    metric: Mapping[str, str],
) -> str:
    metric_id = metric["metric_id"]
    unit = metric["unit_contract"].lower()
    bounds = metric["bounds_policy"].lower()
    if metric["source_lane"] == "DP-D":
        return "DERIVED_FROM_ACCEPTED_DEPENDENCIES"
    if metric_id == "going_concern_flag":
        return "GOING_CONCERN_BOOLEAN"
    if metric_id in {
        "commercialization_stage",
        "regulatory_certification_stage",
    }:
        return "EVIDENCE_ANCHORED_ORDINAL_STAGE"
    if "growth" in metric_id or bounds == "growth_ratio":
        return "COMPARABLE_PERIOD_GROWTH"
    if "date" in unit:
        return "NAMED_MILESTONE_DATE"
    if "count" in unit or "integer" in bounds:
        return "NAMED_ASSET_OR_ACTIVITY_COUNT"
    if "currency_per" in unit or unit.endswith("_per_day"):
        return "EXPLICIT_UNIT_RATE"
    if "currency" in unit:
        return "REPORTED_CURRENCY_VALUE"
    if "ratio" in unit or bounds.startswith("ratio"):
        return "EXPLICIT_RATIO_OR_PERCENTAGE"
    if unit in {"days", "years", "hours_per_day"}:
        return "REPORTED_DURATION_OR_UTILIZATION"
    return "REPORTED_SPECIALIZED_KPI"


def _semantic_rules(
    metric: Mapping[str, str],
) -> dict[str, str]:
    metric_id = metric["metric_id"]
    semantic_class = _semantic_class(metric)
    positive = (
        "issuer-specific reported value is textually linked to the exact "
        "metric subject, reporting period, unit, and applicable archetype"
    )
    prohibited = (
        "reject peer or industry statistics, risk-factor hypotheticals, "
        "unrealized targets, unrelated table percentages, and values whose "
        "subject, period, unit, or scope is ambiguous"
    )
    if semantic_class == "GOING_CONCERN_BOOLEAN":
        positive = (
            "active auditor or management substantial-doubt conclusion "
            "about the issuer as of the filing date"
        )
        prohibited = (
            "reject generic liquidity risk, historical resolved doubt, "
            "third-party doubt, and boilerplate going-concern language"
        )
    elif semantic_class == "EVIDENCE_ANCHORED_ORDINAL_STAGE":
        positive = (
            "completed named milestone maps to the frozen ordinal stage; "
            "plans or intentions do not advance the stage"
        )
        prohibited = (
            "reject aspirational targets, unnamed progress claims, peer "
            "milestones, and stages inferred only from elapsed time"
        )
    elif semantic_class == "COMPARABLE_PERIOD_GROWTH":
        positive = (
            "explicit reported growth or two accepted same-definition values "
            "with aligned periods, units, scope, and asset or activity class"
        )
        prohibited = (
            "reject mix percentages, market growth, target growth, unmatched "
            "periods, and growth across changed definitions or units"
        )
    elif semantic_class == "NAMED_ASSET_OR_ACTIVITY_COUNT":
        positive = (
            "issuer count identifies the named asset or activity class and "
            "the reporting date or period"
        )
        prohibited = (
            "reject orders, options, capacity, peer fleets, partial segment "
            "counts, or commitments unless the metric explicitly requests them"
        )
    elif semantic_class == "EXPLICIT_UNIT_RATE":
        positive = (
            "issuer reports the exact numerator-per-denominator rate, or both "
            "accepted operands have matching period, scope, and unit lineage"
        )
        prohibited = (
            "reject total revenue or expense, rates with a different "
            "denominator, targets, and values missing currency or unit lineage"
        )
    elif semantic_class == "EXPLICIT_RATIO_OR_PERCENTAGE":
        positive = (
            "issuer reports the exact ratio definition, or accepted numerator "
            "and denominator share period, scope, and definition"
        )
        prohibited = (
            "reject component shares, unrelated percentages, targets, and "
            "ratios reconstructed from noncomparable operands"
        )
    elif semantic_class == "NAMED_MILESTONE_DATE":
        positive = (
            "date is explicitly tied to the same named certification, "
            "production, delivery, or commercialization milestone"
        )
        prohibited = (
            "reject filing dates, historical event dates, generic schedule "
            "references, and dates for a different milestone"
        )
    elif semantic_class == "REPORTED_CURRENCY_VALUE":
        positive = (
            "issuer value names the exact obligation, backlog, deposit, "
            "revenue, or commitment and preserves currency and reporting date"
        )
        prohibited = (
            "reject enterprise value, market estimates, unsigned options, "
            "peer values, and amounts lacking currency or subject lineage"
        )
    elif semantic_class == "REPORTED_DURATION_OR_UTILIZATION":
        positive = (
            "issuer reports the named average duration, utilization, age, "
            "dwell, off-hire, or operating-days measure with its time basis"
        )
        prohibited = (
            "reject calendar years, filing age, targets, maximums, and "
            "durations for a different asset or operating class"
        )
    if metric_id in {
        "aircraft_orderbook_commitments",
        "newbuild_capacity_commitments",
        "capex_commitments",
    }:
        positive = (
            "reported firm commitment for the issuer with obligation type, "
            "amount or units, currency where applicable, and measurement date"
        )
        prohibited = (
            "reject delivered assets, nonbinding options, management targets, "
            "general capex plans, and commitments of customers or peers"
        )
    if metric["source_lane"] == "DP-D":
        positive = (
            "all formula-required dependencies are accepted and aligned under "
            "the frozen dependency rule"
        )
        prohibited = (
            "reject derivation when any dependency is review-required, "
            "rejected, missing, stale, unit-incompatible, or period-misaligned"
        )
    return {
        "semantic_class": semantic_class,
        "positive_fixture_rule": positive,
        "prohibited_fixture_rule": prohibited,
        "scope_rule": (
            "prefer consolidated issuer scope; segment scope is allowed only "
            "when the metric comparison population and segment are explicit"
        ),
        "period_rule": (
            "period_end or point-in-time date must be source-supported; never "
            "infer a fiscal date from a quarter label alone"
        ),
        "unit_rule": (
            f"normalize only to {metric['unit_contract']}; preserve source "
            "unit, scale, currency, and denominator lineage"
        ),
        "derived_dependency_rule": (
            "all dependencies require accepted evidence with formula-specific "
            "period and unit compatibility"
            if metric["source_lane"] == "DP-D"
            else "not_applicable"
        ),
        "acceptance_policy": (
            "fixture freeze is schema-only; evidence remains REVIEW_REQUIRED "
            "until a hash-exact policy or reviewed semantic decision exists"
        ),
    }


def build_semantic_metric_contracts(
    *,
    final_metric_rows: Sequence[Mapping[str, str]],
    supporting_metric_rows: Sequence[Mapping[str, str]],
    search_aliases: Mapping[str, Sequence[str]],
    deferred_metric_ids: set[str],
) -> tuple[list[dict[str, object]], list[str]]:
    final = {
        row["metric_id"]: row
        for row in final_metric_rows
        if row.get("source_lane") in {"DP", "DP-D"}
    }
    support = {
        row["support_metric_id"]: row
        for row in supporting_metric_rows
    }
    source_contracts: dict[str, Mapping[str, str]] = {
        **final,
        **support,
    }
    output: list[dict[str, object]] = []
    errors: list[str] = []
    for metric_id, metric in sorted(final.items()):
        if metric["source_lane"] == "DP":
            source_metric_ids = (metric_id,)
        else:
            rule = PARSER_DERIVATIONS.get(metric_id)
            dependencies = (
                rule.get("dependencies") if rule is not None else None
            )
            if not isinstance(dependencies, (tuple, list)):
                errors.append(
                    f"{metric_id}: missing parser derivation dependencies"
                )
                continue
            source_metric_ids = tuple(str(value) for value in dependencies)
        missing = [
            source_metric
            for source_metric in source_metric_ids
            if source_metric not in source_contracts
        ]
        if missing:
            errors.append(
                f"{metric_id}: missing source contracts {missing}"
            )
            continue
        alias_candidates = list(search_aliases.get(metric_id, ()))
        if not alias_candidates and metric["source_lane"] == "DP-D":
            alias_candidates = [
                alias
                for source_metric in source_metric_ids
                for alias in search_aliases.get(source_metric, ())
            ]
        aliases = tuple(
            dict.fromkeys(
                value.strip()
                for value in alias_candidates
                if value.strip()
            )
        )
        if not aliases:
            errors.append(f"{metric_id}: no frozen search aliases")
        base: dict[str, object] = {
            "freeze_version": SEMANTIC_FIXTURE_FREEZE_VERSION,
            "metric_id": metric_id,
            "metric_pack": metric["metric_pack"],
            "source_lane": metric["source_lane"],
            "component": metric["component"],
            "applicability_tags": metric["applicability_tags"],
            "unit_contract": metric["unit_contract"],
            "period_type": metric["period_type"],
            "max_staleness_days": metric["max_staleness_days"],
            "bounds_policy": metric["bounds_policy"],
            "formula": metric.get("formula") or "",
            "source_metric_ids": "|".join(source_metric_ids),
            "source_unit_contracts": "|".join(
                f"{source_metric}="
                f"{source_contracts[source_metric]['unit_contract']}"
                for source_metric in source_metric_ids
            ),
            "search_aliases": "|".join(aliases),
            **_semantic_rules(metric),
            "has_deferred_fixture_pairs": int(
                metric_id in deferred_metric_ids
            ),
        }
        base["semantic_contract_sha256"] = stable_sha256(base)
        output.append(base)
    return output, errors


def build_semantic_pair_contracts(
    *,
    adjudication_rows: Sequence[Mapping[str, str]],
    fixture_evidence_rows: Sequence[Mapping[str, str]],
    metric_contract_rows: Sequence[Mapping[str, object]],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[str],
]:
    contracts = {
        str(row["metric_id"]): row for row in metric_contract_rows
    }
    by_pair: dict[str, list[Mapping[str, str]]] = {}
    for row in fixture_evidence_rows:
        by_pair.setdefault(row["pair_key"], []).append(row)
    pair_output: list[dict[str, object]] = []
    evidence_output: list[dict[str, object]] = []
    errors: list[str] = []
    for adjudication in sorted(
        adjudication_rows,
        key=lambda row: int(row["queue_rank"]),
    ):
        pair_key = adjudication["pair_key"]
        if adjudication["review_decision"] != "DEFER":
            errors.append(f"{pair_key}: expected DEFER adjudication")
            continue
        metric_id = adjudication["metric_id"]
        metric_contract = contracts.get(metric_id)
        if metric_contract is None:
            errors.append(f"{pair_key}: missing semantic metric contract")
            continue
        evidence_rows = sorted(
            by_pair.get(pair_key, ()),
            key=lambda row: row["evidence_key"],
        )
        if not evidence_rows:
            errors.append(f"{pair_key}: no representative evidence")
            continue
        expected_keys = {
            value
            for value in adjudication[
                "representative_evidence_keys"
            ].split("|")
            if value
        }
        actual_keys = {row["evidence_key"] for row in evidence_rows}
        if expected_keys != actual_keys:
            errors.append(
                f"{pair_key}: representative evidence keys changed"
            )
            continue
        evidence_bundle_sha256 = stable_sha256(evidence_rows)
        fixture_id = "trnsfx_" + stable_sha256(
            {
                "version": SEMANTIC_FIXTURE_FREEZE_VERSION,
                "pair_key": pair_key,
                "metric_contract_sha256": metric_contract[
                    "semantic_contract_sha256"
                ],
                "evidence_bundle_sha256": evidence_bundle_sha256,
            }
        )[:24]
        pair_output.append(
            {
                "freeze_version": SEMANTIC_FIXTURE_FREEZE_VERSION,
                "fixture_id": fixture_id,
                "pair_key": pair_key,
                "fixture_priority": adjudication[
                    "fixture_priority"
                ],
                "ticker": adjudication["ticker"],
                "universe_role": adjudication["universe_role"],
                "calibration_cohort": adjudication[
                    "calibration_cohort"
                ],
                "primary_archetype": adjudication[
                    "primary_archetype"
                ],
                "metric_id": metric_id,
                "metric_pack": adjudication["metric_pack"],
                "source_lane": adjudication["source_lane"],
                "source_metric_ids": adjudication[
                    "source_metric_ids"
                ],
                "semantic_contract_sha256": metric_contract[
                    "semantic_contract_sha256"
                ],
                "representative_evidence_count": len(evidence_rows),
                "representative_evidence_keys": "|".join(
                    sorted(actual_keys)
                ),
                "evidence_bundle_sha256": evidence_bundle_sha256,
                "fixture_status": "FROZEN_REVIEW_REQUIRED",
                "downstream_parser_treatment": (
                    "RETAIN_REVIEW_REQUIRED_UNTIL_REVIEWED_DECISION"
                ),
                "acceptance_authorized": 0,
                "policy_mutation_authorized": 0,
                "retrieval_eligible_after_freeze": 1,
            }
        )
        for row in evidence_rows:
            normalized = {
                field: row.get(field, "")
                for field in SEMANTIC_EVIDENCE_FIELDS
                if field
                not in {
                    "freeze_version",
                    "fixture_id",
                    "evidence_row_sha256",
                }
            }
            evidence_output.append(
                {
                    "freeze_version": SEMANTIC_FIXTURE_FREEZE_VERSION,
                    "fixture_id": fixture_id,
                    "evidence_row_sha256": stable_sha256(normalized),
                    **normalized,
                }
            )
    orphan_pairs = sorted(set(by_pair) - {
        str(row["pair_key"]) for row in pair_output
    })
    if orphan_pairs:
        errors.append(
            f"orphan fixture evidence pairs={orphan_pairs[:10]}"
        )
    return pair_output, evidence_output, errors


def summarize_semantic_freeze(
    *,
    metric_rows: Sequence[Mapping[str, object]],
    pair_rows: Sequence[Mapping[str, object]],
    evidence_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "parser_addressable_metric_contract_count": len(metric_rows),
        "deferred_metric_contract_count": sum(
            int(str(row["has_deferred_fixture_pairs"]))
            for row in metric_rows
        ),
        "semantic_fixture_pair_count": len(pair_rows),
        "semantic_fixture_evidence_row_count": len(evidence_rows),
        "fixture_priority_counts": dict(
            sorted(
                Counter(
                    str(row["fixture_priority"]) for row in pair_rows
                ).items()
            )
        ),
        "source_lane_counts": dict(
            sorted(
                Counter(
                    str(row["source_lane"]) for row in pair_rows
                ).items()
            )
        ),
        "acceptance_authorized_count": sum(
            int(str(row["acceptance_authorized"]))
            for row in pair_rows
        ),
        "retrieval_eligible_after_freeze_count": sum(
            int(str(row["retrieval_eligible_after_freeze"]))
            for row in pair_rows
        ),
    }
