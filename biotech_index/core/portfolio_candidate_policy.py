from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence, TypeVar


RowT = TypeVar("RowT", bound=Mapping[str, object])

NON_ALLOCATABLE_UNIVERSE_STATUSES = frozenset(
    {
        "calibration_only",
        "delisted_calibration",
        "excluded",
        "inactive",
        "remove",
        "review",
    }
)


@dataclass(frozen=True)
class PortfolioCandidatePolicy:
    name: str
    selection_order: str
    rank_top_n: int
    allowed_primary_cohorts: tuple[str, ...]
    cohort_top_k_per_cohort: int
    total_max: int
    min_selected_names: int
    below_min_selected_action: str
    min_score_pct_of_top: float
    selected_reason: str
    excluded_reason: str
    below_min_selected_reason: str

    def __post_init__(self) -> None:
        if self.selection_order not in {"global_then_cohort", "cohort_then_rank"}:
            raise ValueError(
                "Unsupported portfolio candidate selection_order="
                f"{self.selection_order!r}"
            )
        for field_name in (
            "rank_top_n",
            "cohort_top_k_per_cohort",
            "total_max",
            "min_selected_names",
        ):
            if int(getattr(self, field_name)) < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if self.min_selected_names > 0 and self.below_min_selected_action != "cash":
            raise ValueError("below_min_selected_action must be 'cash' when minimum breadth is enabled")
        if not 0.0 <= self.min_score_pct_of_top <= 100.0:
            raise ValueError("min_score_pct_of_top must be within [0, 100]")


@dataclass(frozen=True)
class PortfolioCandidateSelection:
    selected_rows: tuple[Mapping[str, object], ...]
    selected_tickers_by_date: Mapping[str, tuple[str, ...]]
    breadth_cash_fallback_by_date: Mapping[str, bool]


def _finite_float(raw: object, default: float = 0.0) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def policy_from_mapping(raw: Mapping[str, object]) -> PortfolioCandidatePolicy:
    allowed_raw = raw.get("allowed_primary_cohorts") or ()
    if isinstance(allowed_raw, str):
        allowed_values: Iterable[object] = allowed_raw.split(",")
    elif isinstance(allowed_raw, Iterable):
        allowed_values = allowed_raw
    else:
        allowed_values = ()
    allowed = tuple(
        dict.fromkeys(str(value).strip() for value in allowed_values if str(value).strip())
    )
    name = str(raw.get("name") or raw.get("policy_name") or "portfolio_candidate_policy").strip()
    return PortfolioCandidatePolicy(
        name=name,
        selection_order=str(raw.get("selection_order") or "global_then_cohort").strip().lower(),
        rank_top_n=max(0, int(_finite_float(raw.get("rank_top_n"), 0.0))),
        allowed_primary_cohorts=allowed,
        cohort_top_k_per_cohort=max(
            0,
            int(_finite_float(raw.get("cohort_top_k_per_cohort"), 0.0)),
        ),
        total_max=max(0, int(_finite_float(raw.get("total_max"), 0.0))),
        min_selected_names=max(0, int(_finite_float(raw.get("min_selected_names"), 0.0))),
        below_min_selected_action=str(raw.get("below_min_selected_action") or "cash").strip().lower(),
        min_score_pct_of_top=max(
            0.0,
            min(100.0, _finite_float(raw.get("min_score_pct_of_top"), 0.0)),
        ),
        selected_reason=str(raw.get("selected_reason") or name).strip() or name,
        excluded_reason=(
            str(raw.get("excluded_reason") or f"excluded_by_{name}").strip()
            or f"excluded_by_{name}"
        ),
        below_min_selected_reason=(
            str(
                raw.get("below_min_selected_reason")
                or "cash_fallback_insufficient_promoted_policy_breadth"
            ).strip()
            or "cash_fallback_insufficient_promoted_policy_breadth"
        ),
    )


def candidate_score(
    row: Mapping[str, object],
    *,
    score_fields: Sequence[str] = (
        "candidate_selection_score",
        "native_score_value",
        "opportunity_score",
        "portfolio_candidate_score",
    ),
) -> float:
    for field in score_fields:
        raw = row.get(field)
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return 0.0


def portfolio_candidate_base_eligible(
    row: Mapping[str, object],
    *,
    score_fields: Sequence[str] = (
        "candidate_selection_score",
        "native_score_value",
        "opportunity_score",
        "portfolio_candidate_score",
    ),
) -> bool:
    """Apply the pre-ranking live eligibility contract shared by scorer and replay."""
    ticker = str(row.get("ticker") or "").strip().upper()
    universe_status = str(row.get("universe_status") or "").strip().lower()
    price_available = bool(
        str(row.get("price_data_asof_date") or row.get("latest_price_date") or "").strip()
    )
    return bool(
        ticker
        and candidate_score(row, score_fields=score_fields) > 0.0
        and _finite_float(row.get("score_zero_is_missing_flag"), 0.0) <= 0.0
        and _finite_float(row.get("biotech_cohort_investible_flag"), 1.0) > 0.0
        and universe_status not in NON_ALLOCATABLE_UNIVERSE_STATUSES
        and _finite_float(row.get("core_structural_veto_flag"), 0.0) <= 0.0
        and _finite_float(row.get("rank_quality_cap_vetoed"), 0.0) <= 0.0
        and price_available
    )


def select_portfolio_candidates(
    rows: Iterable[RowT],
    policy: PortfolioCandidatePolicy,
    *,
    date_field: str = "asof_date",
    ticker_field: str = "ticker",
    cohort_field: str = "biotech_primary_cohort",
    score_fields: Sequence[str] = (
        "candidate_selection_score",
        "native_score_value",
        "opportunity_score",
        "portfolio_candidate_score",
    ),
) -> PortfolioCandidateSelection:
    """Apply the exact live rank/cohort/breadth order to pre-eligible rows.

    Eligibility is intentionally owned by the caller. This function owns only
    deterministic ranking and portfolio-candidate policy semantics so live
    scoring and historical calibration cannot drift.
    """
    grouped: dict[str, list[RowT]] = {}
    for row in rows:
        asof_date = str(row.get(date_field) or "").strip()
        grouped.setdefault(asof_date, []).append(row)

    allowed = set(policy.allowed_primary_cohorts)
    selected: list[Mapping[str, object]] = []
    selected_tickers_by_date: dict[str, tuple[str, ...]] = {}
    breadth_fallback_by_date: dict[str, bool] = {}
    for asof_date, date_rows in sorted(grouped.items()):
        pool: list[RowT] = sorted(
            date_rows,
            key=lambda row: (
                -candidate_score(row, score_fields=score_fields),
                str(row.get(ticker_field) or "").strip().upper(),
            ),
        )
        if policy.selection_order == "cohort_then_rank" and allowed:
            pool = [
                row
                for row in pool
                if str(row.get(cohort_field) or "").strip() in allowed
            ]
        if policy.rank_top_n > 0:
            pool = pool[: policy.rank_top_n]
        if policy.selection_order == "global_then_cohort" and allowed:
            pool = [
                row
                for row in pool
                if str(row.get(cohort_field) or "").strip() in allowed
            ]
        if 0.0 < policy.min_score_pct_of_top <= 100.0 and pool:
            score_floor = (
                candidate_score(pool[0], score_fields=score_fields)
                * policy.min_score_pct_of_top
                / 100.0
            )
            pool = [
                row
                for row in pool
                if candidate_score(row, score_fields=score_fields) >= score_floor
            ]
        if policy.cohort_top_k_per_cohort > 0:
            cohort_counts: dict[str, int] = {}
            capped: list[RowT] = []
            for row in pool:
                cohort = str(row.get(cohort_field) or "").strip()
                count = cohort_counts.get(cohort, 0)
                if count >= policy.cohort_top_k_per_cohort:
                    continue
                cohort_counts[cohort] = count + 1
                capped.append(row)
            pool = capped
        if policy.total_max > 0:
            pool = pool[: policy.total_max]

        breadth_fallback = policy.min_selected_names > 0 and len(pool) < policy.min_selected_names
        if breadth_fallback:
            pool = []
        selected.extend(pool)
        selected_tickers_by_date[asof_date] = tuple(
            str(row.get(ticker_field) or "").strip().upper() for row in pool
        )
        breadth_fallback_by_date[asof_date] = breadth_fallback

    return PortfolioCandidateSelection(
        selected_rows=tuple(selected),
        selected_tickers_by_date=selected_tickers_by_date,
        breadth_cash_fallback_by_date=breadth_fallback_by_date,
    )
