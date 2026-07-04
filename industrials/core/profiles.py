"""Single source of truth for the industrials issuer reporting-profile vocabulary.

Every profile that ``scripts/07_sync_industrials_sec_fundamentals.py`` can emit
must be listed here, whether it is assigned organically by
``classify_reporting_profile`` or verbatim from a reporting-overrides CSV row
(07 copies ``override.reporting_profile`` into ``dim_issuer_reporting_profile``
without further validation). Validators import this set instead of maintaining
local copies, and the Stage 4 validator cross-checks that the scoring
eligibility policy CSV covers every profile in the set, so adding a profile
here without a policy row fails loudly rather than falling through to a
catch-all policy.
"""

from __future__ import annotations


VALID_REPORTING_PROFILES = frozenset(
    {
        # Organic classifications emitted by classify_reporting_profile in 07.
        "SEC_XBRL_US_GAAP",
        "SEC_XBRL_IFRS",
        "SEC_XBRL_US_GAAP_PARTIAL",
        "SEC_XBRL_IFRS_PARTIAL",
        "SEC_20F_METADATA_ONLY",
        "SEC_ARCHIVE_TEXT_TABLE",
        "SEC_ARCHIVE_TEXT_TABLE_PARTIAL",
        "FOREIGN_NEUTRAL_LOW_CONFIDENCE",
        "NO_FINANCIALS_REVIEW",
        # Archive-required profiles 07 recognizes in should_attempt_archive /
        # has_existing_sec_financial_state and re-emits from overrides.
        "SEC_RAW_ARCHIVE_REQUIRED",
        "FOREIGN_PRIVATE_ISSUER_ARCHIVE_REQUIRED",
        # Override-driven lifecycle/stub profiles.
        "RECENT_IPO_DEVELOPMENT_STAGE",
        "RECENT_PUBLIC_STUB",
        "FPI_HYBRID_STUB_LOADED",
        "FPI_HYBRID_LOADED",
        # Override-only structural handling profiles.
        "FOREIGN_VENDOR_FUNDAMENTALS",
        "PRIVATE_EXCLUDE",
        "PARENT_SEGMENT_NO_STANDALONE_SEC",
        "SPINOFF_SEGMENT_BRIDGE_REVIEW",
        "SPINOFF_SEGMENT_BRIDGE",
        "NON_FILING_OR_PENDING_REPORTING",
    }
)

FPI_HYBRID_PROFILES = frozenset({"FPI_HYBRID_STUB_LOADED", "FPI_HYBRID_LOADED"})

RECENT_STUB_PROFILES = frozenset({"RECENT_IPO_DEVELOPMENT_STAGE", "RECENT_PUBLIC_STUB"})
