from __future__ import annotations


CTGOV_EVENT_TYPES = frozenset({
    "new_trial_added",
    "status_changed",
    "entered_recruiting",
    "became_active_not_recruiting",
    "completed",
    "terminated",
    "withdrawn",
    "results_posted",
    "primary_completion_date_changed",
    "enrollment_changed",
})

SEC_EVENT_TYPES = frozenset({
    "financing_shelf",
    "shelf_registration",
    "atm_program",
    "public_offering",
    "pipe_financing",
    "going_concern",
    "going_concern_confirmed",
    "clinical_update_positive",
    "clinical_update_negative",
    "endpoint_met",
    "endpoint_missed",
    "clinical_hold",
    "partial_clinical_hold",
    "safety_signal",
    "partnership_signed",
    "partnership_license",
    "license_in",
    "license_out",
    "nda_bla_accepted",
    "pdufa_date",
    "regulatory_submission",
    "fda_feedback",
    "fda_approval",
    "fda_rejection",
    "complete_response_letter",
    "fast_track_designation",
})

POLARITIES = frozenset({"positive", "negative", "neutral", "mixed"})
