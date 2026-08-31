from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from biotech_index.core.calibration_metrics import finite_float
from biotech_index.core.cohort_calibration import (
    BIOTECH_CALIBRATION_COHORTS,
    validate_calibration_cohorts,
    validate_cohort_budget_weights,
)


NO_CHALLENGER_CANDIDATE_IDS = frozenset(
    {
        "xbi_benchmark_fallback",
        "production_incumbent_fallback",
    }
)
LIVE_PORTABLE_SELECTION_POLICIES = frozenset({"raw_legacy_score", "core_structural_veto"})


@dataclass(frozen=True)
class CohortPromotionStatus:
    cohort: str
    statistical_authorized: bool
    profitability_authorized: bool
    live_portable: bool
    candidate_id: str
    candidate_name: str
    selection_policy_name: str
    authorized: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "calibration_cohort": self.cohort,
            "statistical_promotion_authorized": self.statistical_authorized,
            "profitability_promotion_authorized": self.profitability_authorized,
            "live_portable": self.live_portable,
            "candidate_id": self.candidate_id,
            "candidate_name": self.candidate_name,
            "selection_policy_name": self.selection_policy_name,
            "cohort_promotion_authorized": self.authorized,
            "reason_codes": "|".join(self.reason_codes),
        }


def cohort_promotion_status(
    cohort: str,
    *,
    statistical_decision: Mapping[str, object],
    profitability_decision: Mapping[str, object],
    fold_contract: Mapping[str, object],
) -> CohortPromotionStatus:
    validated = validate_calibration_cohorts((cohort,))[0]
    candidate_id = str(fold_contract.get("candidate_id") or "").strip()
    spec = fold_contract.get("candidate_spec") or {}
    policy = fold_contract.get("selection_policy") or {}
    if not isinstance(spec, Mapping) or not isinstance(policy, Mapping):
        raise ValueError(f"Invalid candidate contract payload for cohort={validated}")
    candidate_name = str(spec.get("candidate_name") or "").strip()
    policy_name = str(policy.get("policy_name") or "").strip()
    statistical_authorized = statistical_decision.get("production_promotion_authorized") is True
    profitability_authorized = profitability_decision.get("profitability_promotion_authorized") is True
    has_challenger = bool(candidate_id) and candidate_id not in NO_CHALLENGER_CANDIDATE_IDS
    live_portable = has_challenger and policy_name in LIVE_PORTABLE_SELECTION_POLICIES
    reasons: list[str] = []
    if not statistical_authorized:
        reasons.append("statistical_gate_failed")
    if not profitability_authorized:
        reasons.append("profitability_gate_failed")
    if not has_challenger:
        reasons.append("no_challenger_incumbent_retained")
    elif not live_portable:
        reasons.append("selection_policy_not_live_portable")
    authorized = statistical_authorized and profitability_authorized and live_portable
    return CohortPromotionStatus(
        cohort=validated,
        statistical_authorized=statistical_authorized,
        profitability_authorized=profitability_authorized,
        live_portable=live_portable,
        candidate_id=candidate_id,
        candidate_name=candidate_name,
        selection_policy_name=policy_name,
        authorized=authorized,
        reason_codes=tuple(reasons) if reasons else ("authorized",),
    )


_FOLD_ALIGNMENT_FIELDS = (
    "fold_id",
    "horizon_bars",
    "train_start",
    "train_end",
    "validation_start",
    "validation_end",
    "test_start",
    "test_end",
    "embargo_days",
    "support_status",
)


def aligned_fold_manifest(
    manifests_by_cohort: Mapping[str, Iterable[Mapping[str, object]]],
) -> list[dict[str, object]]:
    cohorts = validate_calibration_cohorts(manifests_by_cohort)
    if set(cohorts) != set(BIOTECH_CALIBRATION_COHORTS):
        missing = sorted(set(BIOTECH_CALIBRATION_COHORTS) - set(cohorts))
        extra = sorted(set(cohorts) - set(BIOTECH_CALIBRATION_COHORTS))
        raise ValueError(f"Combined calibration requires all five cohorts; missing={missing} extra={extra}")
    canonical: dict[str, tuple[object, ...]] | None = None
    canonical_rows: list[dict[str, object]] = []
    for cohort in BIOTECH_CALIBRATION_COHORTS:
        rows = [dict(row) for row in manifests_by_cohort[cohort]]
        indexed: dict[str, tuple[object, ...]] = {}
        for row in rows:
            fold_id = str(row.get("fold_id") or "").strip()
            if not fold_id or fold_id in indexed:
                raise ValueError(f"Duplicate or blank fold_id for cohort={cohort}: {fold_id!r}")
            indexed[fold_id] = tuple(row.get(field) for field in _FOLD_ALIGNMENT_FIELDS)
        if canonical is None:
            canonical = indexed
            canonical_rows = rows
        elif indexed != canonical:
            raise ValueError(f"Walk-forward folds are not aligned for cohort={cohort}")
    return [
        {**row, "calibration_scope": "combined_cohort_portfolio", "calibration_cohort": "ALL"}
        for row in canonical_rows
    ]


def _primary_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    split: str,
) -> list[dict[str, object]]:
    return [dict(row) for row in rows if str(row.get("evaluation_split") or "") == split]


def _rows_by_fold_date(
    rows: Iterable[Mapping[str, object]],
) -> dict[tuple[str, str], list[dict[str, object]]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for raw_row in rows:
        row = dict(raw_row)
        fold_id = str(row.get("fold_id") or "").strip()
        asof_date = str(row.get("asof_date") or "").strip()
        ticker = str(row.get("ticker") or "").strip().upper()
        if not fold_id or not asof_date or not ticker:
            raise ValueError("Selection rows require fold_id, asof_date, and ticker")
        row["ticker"] = ticker
        grouped[(fold_id, asof_date)].append(row)
    return grouped


def _primary_comparison_by_fold(
    rows: Iterable[Mapping[str, object]],
    *,
    primary_horizon: int,
) -> dict[str, dict[str, object]]:
    output: dict[str, dict[str, object]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        horizon = int(finite_float(row.get("horizon_days")) or 0)
        if horizon != primary_horizon:
            continue
        fold_id = str(row.get("fold_id") or "").strip()
        if not fold_id or fold_id in output:
            raise ValueError(f"Duplicate or blank primary comparison fold: {fold_id!r}")
        output[fold_id] = row
    return output


def _primary_sleeve_by_fold_date(
    rows: Iterable[Mapping[str, object]],
    *,
    primary_horizon: int,
) -> dict[tuple[str, str], dict[str, object]]:
    output: dict[tuple[str, str], dict[str, object]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        horizon = int(finite_float(row.get("horizon_days")) or 0)
        if horizon != primary_horizon:
            continue
        key = (
            str(row.get("fold_id") or "").strip(),
            str(row.get("asof_date") or "").strip(),
        )
        if not all(key) or key in output:
            raise ValueError(f"Duplicate or incomplete primary sleeve row: {key}")
        output[key] = row
    return output


def combine_cohort_selection_rows(
    *,
    selected_rows_by_cohort: Mapping[str, Iterable[Mapping[str, object]]],
    sleeve_rows_by_cohort: Mapping[str, Iterable[Mapping[str, object]]],
    comparison_rows_by_cohort: Mapping[str, Iterable[Mapping[str, object]]],
    promotion_status_by_cohort: Mapping[str, CohortPromotionStatus],
    cohort_budget_weights: Mapping[str, object],
    primary_horizon: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    cohorts = validate_calibration_cohorts(selected_rows_by_cohort)
    expected = set(BIOTECH_CALIBRATION_COHORTS)
    for label, values in (
        ("selected", set(cohorts)),
        ("sleeve", set(sleeve_rows_by_cohort)),
        ("comparison", set(comparison_rows_by_cohort)),
        ("promotion", set(promotion_status_by_cohort)),
    ):
        if values != expected:
            raise ValueError(f"{label} cohort set mismatch: missing={sorted(expected - values)} extra={sorted(values - expected)}")
    budgets = validate_cohort_budget_weights(BIOTECH_CALIBRATION_COHORTS, cohort_budget_weights)
    combined_candidate: list[dict[str, object]] = []
    combined_incumbent: list[dict[str, object]] = []
    all_fold_dates: set[tuple[str, str]] = set()

    for cohort in BIOTECH_CALIBRATION_COHORTS:
        selected_rows = list(selected_rows_by_cohort[cohort])
        candidate_by_date = _rows_by_fold_date(_primary_rows(selected_rows, split="outer_test_candidate"))
        incumbent_by_date = _rows_by_fold_date(_primary_rows(selected_rows, split="outer_test_incumbent"))
        sleeve_by_date = _primary_sleeve_by_fold_date(
            sleeve_rows_by_cohort[cohort],
            primary_horizon=primary_horizon,
        )
        comparison_by_fold = _primary_comparison_by_fold(
            comparison_rows_by_cohort[cohort],
            primary_horizon=primary_horizon,
        )
        fold_dates = set(sleeve_by_date)
        if not fold_dates:
            raise ValueError(f"No primary-horizon sleeve dates for cohort={cohort}")
        all_fold_dates.update(fold_dates)
        status = promotion_status_by_cohort[cohort]
        budget = budgets[cohort]

        for key in sorted(fold_dates):
            fold_id, asof_date = key
            incumbent_rows = incumbent_by_date.get(key, [])
            if incumbent_rows:
                incumbent_weight = budget / len(incumbent_rows)
                for row in incumbent_rows:
                    combined_incumbent.append(
                        {
                            **row,
                            "evaluation_split": "outer_test_incumbent",
                            "calibration_cohort": cohort,
                            "cohort_budget_weight": budget,
                            "cohort_active_weight": 1.0,
                            "portfolio_target_weight": incumbent_weight,
                            "combined_selection_source": "production_incumbent",
                        }
                    )

            comparison = comparison_by_fold.get(fold_id)
            if comparison is None:
                raise ValueError(f"Missing primary comparison for cohort={cohort} fold={fold_id}")
            fold_candidate_id = str(comparison.get("candidate_id") or "")
            use_challenger = status.authorized and fold_candidate_id not in NO_CHALLENGER_CANDIDATE_IDS
            chosen_rows = candidate_by_date.get(key, []) if use_challenger else incumbent_rows
            sleeve = sleeve_by_date[key]
            raw_active_weight = finite_float(sleeve.get("active_stock_selection_weight"))
            active_weight = (
                max(0.0, min(1.0, raw_active_weight if raw_active_weight is not None else 0.0))
                if use_challenger
                else 1.0
            )
            if chosen_rows and active_weight > 0.0:
                target_weight = budget * active_weight / len(chosen_rows)
                for row in chosen_rows:
                    combined_candidate.append(
                        {
                            **row,
                            "evaluation_split": "outer_test_candidate",
                            "calibration_cohort": cohort,
                            "cohort_budget_weight": budget,
                            "cohort_active_weight": active_weight,
                            "portfolio_target_weight": target_weight,
                            "combined_selection_source": (
                                "authorized_cohort_challenger" if use_challenger else "production_incumbent_retained"
                            ),
                        }
                    )

    ticker_cohort: dict[tuple[str, str, str], str] = {}
    for row in (*combined_candidate, *combined_incumbent):
        key = (
            str(row.get("evaluation_split") or ""),
            str(row.get("asof_date") or ""),
            str(row.get("ticker") or ""),
        )
        cohort = str(row.get("calibration_cohort") or "")
        previous = ticker_cohort.setdefault(key, cohort)
        if previous != cohort:
            raise ValueError(f"Ticker appears in multiple cohorts for the same strategy/date: {key}")

    candidate_weight_by_date: dict[tuple[str, str], float] = defaultdict(float)
    candidate_count_by_date: dict[tuple[str, str], int] = defaultdict(int)
    for row in combined_candidate:
        key = (str(row["fold_id"]), str(row["asof_date"]))
        target_weight = finite_float(row.get("portfolio_target_weight"))
        if target_weight is None:
            raise ValueError(f"Combined selection row lacks a finite target weight: {key}")
        candidate_weight_by_date[key] += target_weight
        candidate_count_by_date[key] += 1
    sleeve_rows = []
    for fold_id, asof_date in sorted(all_fold_dates):
        stock_weight = candidate_weight_by_date.get((fold_id, asof_date), 0.0)
        if stock_weight > 1.0 + 1e-9:
            raise ValueError(
                f"Combined cohort stock weight exceeds 100% on {asof_date}: {stock_weight:.12f}"
            )
        sleeve_rows.append(
            {
                "fold_id": fold_id,
                "horizon_days": primary_horizon,
                "asof_date": asof_date,
                "selected_name_count": candidate_count_by_date.get((fold_id, asof_date), 0),
                "reliability_class": "cohort_specific_combined",
                "active_stock_selection_weight": round(stock_weight, 10),
                "xbi_residual_weight": round(1.0 - stock_weight, 10),
                "sleeve_weight_sum": 1.0,
            }
        )
    return [*combined_candidate, *combined_incumbent], sleeve_rows
