from __future__ import annotations


"""Shared database-value constants.

These are normalized database field values, not user-facing reason codes.
Reason codes such as ``going_concern_confirmed`` are emitted by scoring
scripts and configured in ``config.yaml``.
"""

GOING_CONCERN_STATUS_COL = "going_concern_status"

GOING_CONCERN_HARD_STATUSES = frozenset(
    {
        "confirmed",
        "confirmed_going_concern",
        "going_concern_confirmed",
        "google_confirmed_going_concern",
    }
)
GOING_CONCERN_SOFT_STATUSES = frozenset(
    {"possible", "substantial_doubt", "going_concern", "going_concern_warning"}
)

CORE_HARD_WEAKNESS_REASONS = frozenset(
    {
        "cash_runway_lt_9m",
        "severe_runway_flag",
        "going_concern_confirmed",
        "reverse_split_history",
        "no_active_trial_no_business_anchor",
        "illiquid",
    }
)
EVENT_HARD_WEAKNESS_REASONS = frozenset({"repeated_dilution", "negative_clinical_event"})
SOFT_WEAKNESS_REASONS = frozenset(
    {
        "cash_runway_9_to_12m_clinical",
        "going_concern_warning",
        "single_dilution_event",
        "low_financial_data_quality",
        "high_commercial_fragility",
        "high_tier1_risk_score",
        "recent_nt_filing",
        "early_stage_or_unadvanced_trial_anchor",
        "burn_acceleration",
    }
)
TOXIC_SOFT_WEAKNESS_REASONS = frozenset(
    {
        "cash_runway_9_to_12m_clinical",
        "going_concern_warning",
        "low_financial_data_quality",
        "high_commercial_fragility",
        "recent_nt_filing",
        "burn_acceleration",
    }
)
MILD_SOFT_WEAKNESS_REASONS = SOFT_WEAKNESS_REASONS - TOXIC_SOFT_WEAKNESS_REASONS
