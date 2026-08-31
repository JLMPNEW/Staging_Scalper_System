"""Fail-closed registry for superseded Transportation evidence routes.

The generic-OOS production chain and early prospective-evidence CLIs predate
the externally anchored, calendar-bound evidence contract.  They are retained
only so old runbooks fail with an actionable explanation instead of silently
creating a production lock or an apparently authoritative verdict.
"""

from __future__ import annotations

from typing import Final


ROUTE_STATUS: Final = "SUPERSEDED_FAIL_CLOSED"
CANONICAL_CAPTURE_SCRIPT: Final = (
    "industrials/transportation/scripts/45h_capture_transportation_future_oos.py"
)
CANONICAL_EVALUATION_SCRIPT: Final = (
    "industrials/transportation/scripts/45i_evaluate_transportation_future_oos.py"
)
CANONICAL_REQUIREMENTS: Final[tuple[str, ...]] = (
    "out_of_band_pinned_ed25519_authority",
    "immutable_preregistered_plan_and_exact_source_hashes",
    "signed_pre_entry_capture_and_complete_cadence_registry",
    "exact_exchange_sessions_and_execution_time_total_return_inputs",
    "point_in_time_membership_and_terminal_security_cash_carry",
    "signed_raw_outcome_recomputation_after_horizon_maturity",
    "independent_group_level_predictive_verdicts",
    "separate_cryptographic_production_activation_review",
)


class LegacyTransportationRouteDisabled(ValueError):
    """Raised whenever a superseded route is invoked."""


def route_diagnostic(route_id: str) -> dict[str, object]:
    """Return the immutable, non-authorizing status of a legacy route."""
    return {
        "route_id": str(route_id),
        "route_status": ROUTE_STATUS,
        "production_promotion_eligible": False,
        "production_activation_authorized": False,
        "portfolio_allocation_authorized": False,
        "canonical_capture_script": CANONICAL_CAPTURE_SCRIPT,
        "canonical_evaluation_script": CANONICAL_EVALUATION_SCRIPT,
        "canonical_requirements": list(CANONICAL_REQUIREMENTS),
    }


def block_legacy_route(route_id: str) -> None:
    """Unconditionally reject a superseded promotion/evidence entry point."""
    raise LegacyTransportationRouteDisabled(
        f"{route_id} is {ROUTE_STATUS}; activation is fail-closed and it cannot "
        "promote, activate, package, "
        "or authorize Transportation capital. Use "
        f"{CANONICAL_CAPTURE_SCRIPT} and {CANONICAL_EVALUATION_SCRIPT}, then "
        "obtain a separate cryptographic production-activation review. "
        f"Required controls={','.join(CANONICAL_REQUIREMENTS)}"
    )
