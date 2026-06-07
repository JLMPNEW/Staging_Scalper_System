from __future__ import annotations


# FDA review states that must block automatic production eligibility.
MANUAL_FDA_REVIEW_STATES = frozenset(
    {
        "confirmed_hard_red",
        "regulatory_review_required",
        "manual_review_required",
        "manual_fda_footprint_ivd_lab",
        "mapping_review_required",
        "duplicate_cleanup_required",
    }
)

# Broader reporting set: manual blockers plus analyst watch states.
REGULATORY_RISK_STATES = frozenset({*MANUAL_FDA_REVIEW_STATES, "regulatory_watch"})

# States that should flow into the taxonomy regulatory_model column.
REGULATORY_MODEL_FDA_STATES = REGULATORY_RISK_STATES


def normalize_fda_state(raw: object) -> str:
    return str(raw or "").strip().lower()
