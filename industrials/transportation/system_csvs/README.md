# Transportation System CSVs

These files are controlled pipeline inputs. Changes require seed validation,
reviewed provenance, and regeneration of downstream artifacts. Generated
reports and rank tables do not belong here.

- `transportation_tickers.csv`: active source-of-truth universe.
- `transportation_delisted.csv`: curated delisted calibration seed.
- `transportation_listing_dates.csv`: PIT eligibility bounds.
- `transportation_security_continuity_overrides.csv`: primary-source-verified U.S. listing
  boundaries, structural breaks, and separate-listing proxy policy. Related local/OTC histories
  must never be appended directly to the current U.S. return series.
- `transportation_historical_membership.csv`: additional explicit PIT intervals.
- `transportation_ticker_aliases.csv`: verified effective-dated ticker lineage.
- `transportation_cik_ticker_overrides.csv`: reviewed identity exceptions.
- `transportation_sec_reporting_overrides.csv`: effective-dated issuer reporting-profile
  exceptions, archive-recovery routes, and reviewed FPI hybrid states.
- `transportation_xbrl_concept_aliases.csv`: reviewed family-specific XBRL mappings. Only the
  VLRS total transport-services revenue concept is approved; passenger and cargo/mail concepts
  remain components and are not mapped as total revenue.
- `transportation_reporting_profile_graduations.csv`: reviewed profile promotions; the header-only
  file means no manual graduation is currently approved.
- `transportation_scoring_eligibility_policy.csv`: effective-dated reporting-profile and lifecycle
  rules controlling financial confidence, rank readiness, and calibration eligibility.
- `transportation_norgate_symbol_map.csv`: reviewed active/delisted provider lineage and
  calibration eligibility.
- `transportation_norgate_symbol_overrides.csv`: analyst-reviewed symbol aliases and explicit
  fail-closed exclusions. `override_end_date` is an eligibility boundary; an
  `eligibility_end_basis` of `reviewed_economic_terminal_event` must remain separate from the
  provider's `last_quoted_date`.
