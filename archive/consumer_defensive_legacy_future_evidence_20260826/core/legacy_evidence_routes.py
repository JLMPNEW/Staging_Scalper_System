"""Fail-closed registry for superseded Consumer evidence CLIs."""

from __future__ import annotations

from typing import Final


ROUTE_STATUS: Final = "SUPERSEDED_FAIL_CLOSED"
CANONICAL_CAPTURE_SCRIPT: Final = (
    "consumer_defensive/scripts/26e_capture_consumer_defensive_future_oos.py"
)
CANONICAL_EVALUATION_SCRIPT: Final = (
    "consumer_defensive/scripts/26f_evaluate_consumer_defensive_future_oos.py"
)
CANONICAL_REQUIREMENTS: Final[tuple[str, ...]] = (
    "out_of_band_pinned_ed25519_authority",
    "immutable_preregistered_plan_and_exact_source_hashes",
    "rank_derived_signed_pre_entry_capture",
    "complete_cadence_registry",
    "exact_exchange_sessions_and_execution_time_total_return_inputs",
    "point_in_time_membership_and_terminal_security_cash_carry",
    "signed_raw_outcome_recomputation_after_horizon_maturity",
    "independent_predictive_verdict_and_activation_review",
)


class LegacyConsumerEvidenceRouteDisabled(RuntimeError):
    """Raised whenever a superseded Consumer evidence route is invoked."""


def route_diagnostic(route_id: str) -> dict[str, object]:
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
    raise LegacyConsumerEvidenceRouteDisabled(
        f"{route_id} is {ROUTE_STATUS}; it cannot create Consumer promotion "
        "evidence or authorize capital. Use "
        f"{CANONICAL_CAPTURE_SCRIPT} and {CANONICAL_EVALUATION_SCRIPT}. "
        f"Required controls={','.join(CANONICAL_REQUIREMENTS)}"
    )
