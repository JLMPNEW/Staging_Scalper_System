from __future__ import annotations

from typing import Iterable, Mapping, Protocol, TypeVar


BIOTECH_CALIBRATION_COHORTS = (
    "commercial_profitable_quality_or_mature",
    "commercial_turnaround_or_unprofitable_growth",
    "late_clinical_pivotal_or_registrational",
    "platform_partnered_modality_pipeline",
    "early_clinical_speculative_or_single_asset_pipeline",
)
BIOTECH_CALIBRATION_COHORT_SET = frozenset(BIOTECH_CALIBRATION_COHORTS)
BIOTECH_COHORT_DIRECTORY_NAMES = {
    cohort: f"c{index:02d}"
    for index, cohort in enumerate(BIOTECH_CALIBRATION_COHORTS, start=1)
}


class CohortScopedPolicy(Protocol):
    @property
    def allowed_primary_cohorts(self) -> tuple[str, ...]: ...

    @property
    def post_selection_allowed_primary_cohorts(self) -> tuple[str, ...]: ...


RowT = TypeVar("RowT", bound=Mapping[str, object])


def normalized_calibration_cohort(row: Mapping[str, object]) -> str:
    return str(
        row.get("biotech_primary_cohort")
        or row.get("biotech_calibration_cohort")
        or row.get("calibration_cohort")
        or ""
    ).strip()


def validate_calibration_cohorts(raw_cohorts: Iterable[object]) -> tuple[str, ...]:
    cohorts = tuple(dict.fromkeys(str(value).strip() for value in raw_cohorts if str(value).strip()))
    if not cohorts:
        raise ValueError("Cohort calibration requires at least one cohort")
    unknown = sorted(set(cohorts) - BIOTECH_CALIBRATION_COHORT_SET)
    if unknown:
        raise ValueError(f"Unsupported biotech calibration cohort(s): {unknown}")
    return cohorts


def cohort_output_directory_name(cohort: str) -> str:
    """Return a stable compact directory name for one calibration cohort.

    Cohort names are intentionally retained in contracts and output rows. The
    filesystem identifier is compact so deeply nested Windows output roots do
    not fail late in a long run when a long artifact filename is published.
    """

    validated = validate_calibration_cohorts((cohort,))[0]
    return BIOTECH_COHORT_DIRECTORY_NAMES[validated]


def rows_for_cohort(rows: Iterable[RowT], cohort: str) -> list[RowT]:
    validated = validate_calibration_cohorts((cohort,))[0]
    return [row for row in rows if normalized_calibration_cohort(row) == validated]


def policy_supports_cohort(policy: CohortScopedPolicy, cohort: str) -> bool:
    """Return whether a policy can select names from one isolated cohort.

    Pre- and post-selection allowlists are both hard scope restrictions. Policies
    without either allowlist are global candidates and may be calibrated inside
    every cohort independently.
    """

    validated = validate_calibration_cohorts((cohort,))[0]
    pre = {str(value).strip() for value in policy.allowed_primary_cohorts if str(value).strip()}
    post = {
        str(value).strip()
        for value in policy.post_selection_allowed_primary_cohorts
        if str(value).strip()
    }
    return (not pre or validated in pre) and (not post or validated in post)


def validate_cohort_budget_weights(
    cohorts: Iterable[str],
    raw_weights: Mapping[str, object],
) -> dict[str, float]:
    validated = validate_calibration_cohorts(cohorts)
    weights: dict[str, float] = {}
    for cohort in validated:
        try:
            weight = float(str(raw_weights.get(cohort, 0.0)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid portfolio budget weight for cohort {cohort!r}") from exc
        if not 0.0 <= weight <= 1.0:
            raise ValueError(f"Portfolio budget weight for cohort {cohort!r} must be in [0, 1]")
        weights[cohort] = weight
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"Biotech cohort portfolio budget weights sum to {total:.12f}; expected 1.0")
    return weights
