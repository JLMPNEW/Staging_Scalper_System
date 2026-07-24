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

# Manual FDA footprint states produced from curated footprint evidence. Only
# ivd_lab is included in MANUAL_FDA_REVIEW_STATES because those rows still carry
# LDT/IVD regulatory review risk; device, infrastructure, and service footprints
# are known non-blocking state labels.
MANUAL_FDA_FOOTPRINT_STATES = frozenset(
    {
        "manual_fda_footprint_device",
        "manual_fda_footprint_infrastructure",
        "manual_fda_footprint_ivd_lab",
        "manual_fda_footprint_service_or_non_cdrh",
    }
)

# Reviewed centralized laboratory/LDT footprints are not an FDA manual-review
# blocker by themselves. Any mapped hard-red, recall, warning-letter, or
# adverse-event evidence continues through the normal risk gates.
REVIEWED_FDA_FOOTPRINT_STATES = frozenset(
    {
        "reviewed_fda_footprint_ldt_clia",
    }
)

# Broader reporting set: manual blockers plus analyst watch states.
REGULATORY_RISK_STATES = frozenset({*MANUAL_FDA_REVIEW_STATES, "regulatory_watch"})

# States that should flow into the taxonomy regulatory_model column.
REGULATORY_MODEL_FDA_STATES = REGULATORY_RISK_STATES

# All known state labels. This is broader than MANUAL_FDA_REVIEW_STATES and is
# used by validators/report builders to detect truly unknown values without
# turning every manual footprint into a hard regulatory block.
FDA_REVIEW_KNOWN_STATES = frozenset(
    {
        "",
        "cleared",
        "no_mapped_fda_records",
        "regulatory_watch",
        *MANUAL_FDA_REVIEW_STATES,
        *MANUAL_FDA_FOOTPRINT_STATES,
        *REVIEWED_FDA_FOOTPRINT_STATES,
    }
)


def normalize_fda_state(raw: object) -> str:
    return str(raw or "").strip().lower()
