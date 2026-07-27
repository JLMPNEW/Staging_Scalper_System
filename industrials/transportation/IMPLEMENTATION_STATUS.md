# Transportation Implementation Status

Status date: 2026-07-26

## Implemented Batches

- [x] Family-scoped fail-closed configuration resolver.
- [x] Active seed: 112 reviewed rows.
- [x] Delisted seed: 48 curated rows.
- [x] Four-cohort taxonomy and universe policy.
- [x] Seed, active, historical/delisted, alias, universe, and identity scripts.
- [x] Shared family-scoped universe implementation.
- [x] Cross-family isolation tests.
- [x] Norgate symbol reconciliation: 160 reviewed mappings, 158 calibration-usable.
- [x] Exact provider final-date contracts for 46 of 48 delisted seeds.
- [x] Explicit fail-closed exclusions for CGI and RRTS while Norgate classifies them as current/OTC.
- [x] Family-pinned price sync, policy audit, market-feature, and market validation wrappers.
- [x] Explicit-map Norgate delisted price importer and portfolio-layer price/event exporter.
- [x] Optional portfolio-layer shadow-source configuration with `require_oos_score_valid=true`.
- [x] Family-pinned SEC financial, financial-feature, financial-validation, eligibility, and FX wrappers.
- [x] Reporting override/graduation inputs and a reviewed 31-rule scoring eligibility policy.
- [x] Cohort/industry-aware 39-metric registry with explicit availability and provenance states.
- [x] Specialized metric matrix builder and independent coverage/status validator.
- [x] Cohort-relative scoring with required-input, liquidity, staleness, financial-confidence,
  specialized-coverage, and total-confidence gates.
- [x] Deterministic 112-row shadow rank publisher and independent rank validator.
- [x] End-to-end `industrial_family` portfolio-adapter validator.
- [x] Production foundation load in the configured shared industrials database.
- [x] Historical adjusted prices for all 112 active names and three benchmarks.
- [x] Norgate total-return histories for 46 calibration-usable delisted names.
- [x] Portfolio-layer delisted price/event contract export.
- [x] Historical SEC submissions and CompanyFacts load for all 160 active-plus-delisted names.
- [x] Nine-pair transportation FX contract, including NOKUSD, plus post-SEC currency discovery.
- [x] Read-only raw historical coverage validator and machine-readable acceptance manifest.
- [x] Primary-source security-continuity policies for AZUL, ECO, HAFN, HSHP, LTM, and PSIG.
- [x] Targeted SEC recovery for all ten reviewed issuers, including 10-12B/A and legacy
  complete-submission fixed-width statement parsing.
- [x] Reviewed VLRS IFRS revenue alias and SB/VLRS raw-tag audit; both now have canonical
  revenue and assets.
- [x] Bounded specialized-disclosure recovery for all 160 issuers, source-linked candidate
  persistence, review queue, current availability materialization, and independent acceptance
  gates.
- [x] Current-date market and financial feature materialization, metric coverage report,
  cohort-relative scoring build, deterministic shadow rank publication, and portfolio-adapter
  validation for 2026-07-22.
- [x] Air-cohort period/scope parser expansion, bounded 160-issuer rerun, historical-scale gate,
  required-financial-metric gap audit, and generic financial-parser rule freeze.
- [x] Resumable historical specialized-disclosure backfill with per-document checkpoints and an
  independent exact-coverage gate.
- [x] Active-plus-inactive point-in-time market, financial, reporting-profile, and specialized
  feature history with a frozen 92-date evidence manifest.
- [x] Dedicated-parser DP0 discovery contract: 90 unique specialized metrics, 160 reviewed
  operating-archetype assignments, 14,400 explicit ticker/metric applicability rows, immutable
  v2 baseline hashes, deterministic rebuild, and independent fail-closed validation.
- [x] Dedicated-parser operand correction: seven non-scoring supporting extraction metrics and
  a deterministic 1,120-row ticker/operand scope, closing all one-pass DP-D input gaps.
- [x] Transportation dedicated-parser adapter phase A: 84 frozen search metrics, fail-closed
  archetype applicability, one semantic parse per document, normalized-fact discovery,
  review-only broad candidates, and positive/prohibited offline fixtures for every search metric.
- [x] DP1 sector-neutral policy replay: immutable evaluation overlays, explicit CLI, exact
  policy application/materialization, base-hash preservation, idempotency, and persisted
  zero-document/provider/OCR counters without modifying existing dirty shared-parser files.
- [ ] Positioning, daily orchestration, walk-forward OOS calibration, and production promotion.

## Verification

- Configured production database: `C:\Users\josel\Documents\STAGING\DB\industrials.sqlite`.
- Foundation: 112 active, 158 calibration-usable, and 48 delisted seeds; universe and identity
  gates PASS. The 158 usable rows comprise all active names and 46 delisted names; CGI and RRTS
  remain explicit provider-identity exclusions.
- Active prices: 172,377 adjusted bars across all 112 active names through 2026-07-22.
- Benchmarks: IYT, XTN, and SPY all pass the 2026-07-22 current-through gate.
- Corrected market policy audit: 115 rows, zero failures, and four short-history reviews
  (AZUL, ELOG, FDXF, RUBI).
- Delisted prices: 178,217 Norgate total-return bars across all 46 usable delisted names.
- FX: all nine explicitly pinned transportation pairs pass, including NOKUSD; JPYUSD is also
  retained from reporting-currency discovery.
- SEC: 160/160 reporting profiles and filing coverage, 21,147 filings, 1,704,895 raw facts,
  and 379,886 canonical mapped facts. Profiles include seven legacy/archive text-table issuers,
  two reviewed FPI hybrids, 15 IFRS issuers, and 136 US-GAAP issuers.
- Read-only raw-load gate: `PASS`, zero errors/warnings, and all 160 ticker rows PASS.
- Security continuity: six primary-source policies loaded; all former price-start reviews now
  pass without stitching structurally different or venue-specific return series.
- SB/VLRS tag audit: both have canonical revenue and assets; receivables, passenger revenue, and
  cargo/mail revenue remain excluded from total-revenue aliasing.
- Current financial build: 112/112 active rows, 111 complete and one review (`PAL:
  revenue_not_annual`). Reviewed capex and IFRS borrowing aliases were remapped for the affected
  issuers before rebuilding Stage 4.
- Required-financial-metric gap audit: `PASS` and parser freeze `READY`; all 47 remaining gaps are
  source/period gaps (39) or TTM-alignment gaps (8), with zero reusable mapping reviews and zero
  approved aliases waiting to be remapped.
- Specialized bounded scan: 160/160 issuers have recovered source documents; 373 candidates
  across 78 issuers. Conservative review controls leave 82 accepted candidates and 291
  review-required candidates rather than promoting period-ambiguous or non-issuer values.
- Current metric availability: 4,368 active ticker/metric rows under registry
  `transportation_metrics_v2_shadow`; 1,459 `REPORTED`, 70 `DERIVED`, 52
  `DISCLOSED_UNPARSED`, 697 `NOT_DISCLOSED`, and 2,090 `NOT_APPLICABLE`.
- Current metric coverage: 1,529/2,278 applicable metrics observed (67.12%); required rank
  coverage is 909/970 (93.71%). Specialized coverage is 133/552 (24.09%). Cash-generative
  development issuers are excluded from the cash-runway denominator instead of being counted as
  missing.
- Specialized scale decision: `READY_FOR_BOUNDED_HISTORICAL_BACKFILL`. Accepted active-ticker
  coverage is 25.0% surface, 40.9% air, 52.4% marine, and 13.8% development-stage. Every mature
  cohort clears the 25% gate; development-stage remains measured but is not a mature-cohort scale
  blocker.
- Historical specialized-disclosure gate: `PASS`; 3,019/3,019 eligible annual, quarterly, and
  raw-XBRL-linked 6-K filings scanned across 112 active and 48 inactive tickers, zero missing
  filings, and 4,450 source-linked candidates (1,130 accepted; 3,320 review-required).
- Point-in-time feature-history gate: `PASS`, panel status `FROZEN`; 92 observations from
  2019-01-02 through 2026-07-22, 9,496 exact membership rows, 370,344 metric-availability rows,
  all 39 metrics per member, four hashed evidence files per date, and zero future-data errors.
- Specialized PIT availability uses the conservative SEC filing date. Three after-market-close
  submissions whose UTC acceptance date preceded their SEC filing date were excluded from the
  prior close and their affected snapshots were rebuilt before the panel was frozen.
- Dedicated-parser DP0 gate: `PASS`; 90 metrics comprise 77 direct parser targets, seven
  parser-derived metrics, and six financial-derived metrics. All 112 active and 48 delisted
  identities are represented in a deterministic 14,400-row scope, including 2,535 applicable
  and 11,865 explicit `NOT_APPLICABLE` pairs. The contract records 29 development overlays,
  thirteen primary operating archetypes, the exact input/scope hashes, and the immutable v2
  baseline hashes. Seven additional non-scoring operands produce a 1,120-row supporting scope
  with 83 applicable and 1,037 explicit `NOT_APPLICABLE` pairs. The actual one-pass parser search
  set is 84 metrics. Parser execution and production remain disabled.
- Dedicated-parser DP2 offline fixtures: 77 final direct targets plus seven supporting operands
  are registered; every one has a positive offline discovery fixture and a prohibited
  cross-archetype fixture. Unit, period, issuer-scope, value-domain, conflict, duplicate, and
  deterministic-order gates are explicit. Broad evidence remains review-only and no production
  mapping exists.
- Dedicated-parser DP1 implementation: `PASS`; the new replay API and CLI are separate from
  assessment-only `--reassess-run-id`, require an immutable pre-policy base run, preserve the
  base evidence hash, and store evaluated overlays without source-document or provider calls.
  The machinery strategic-weight assertion is reconciled to the activated 0.05 value.
- Dedicated-parser DP3 source census: read-only reconciliation passes for all 3,019 base
  accessions. A bounded 205-accession supplemental scope produces 3,329 selected document rows:
  3,177 cached/byte-hash verified and 152 exact hydration gaps. Parser execution remains
  unauthorized until those 152 rows are hydrated or explicitly dispositioned and DP4 passes.
- Current scoring and shadow publication: 112 rows, 26 rank-ready and 86 blocked by explicit
  coverage/liquidity/quality gates; portfolio candidates, OOS-valid rows, research-eligible rows,
  and survivorship-corrected rows all remain zero.
- Transportation suites: 225 passed in the current regression run.
- DP1 compatibility suites: 86 dedicated-parser and 32 defense tests passed.
  The targeted machinery activation gate passes at strategic weight 0.05.
- Industrials plus portfolio-layer regression suite: 151 passed.
- Ruff: PASS for the changed transportation/shared files.

Raw-load evidence is written to
`output/industrials/transportation/historical_load/transportation_historical_raw_load_coverage.csv`
and `transportation_historical_raw_load_validation.json`. The validator opens the configured
database in SQLite read-only mode; it writes reports only.

The item-5 price-start and SEC review queues are resolved. The market minimum-history policy
still carries four expected short-history observations (AZUL, ELOG, FDXF, and RUBI), which are
not raw-load failures. Current-date feature construction, coverage measurement, scoring, shadow
publication, the portfolio-adapter contract, the historical specialized parse, and the complete
point-in-time feature panel are now complete. Walk-forward calibration was deliberately not run:
the next research batch must first freeze its training/validation windows, forward-return and
cost contracts, cohort treatment, optimization bounds, and promotion gates against this already
frozen panel.

The model remains research-only. A shadow publication always writes
`portfolio_candidate_gate=0`, `oos_score_valid_flag=0`, and
`survivorship_corrected_panel_flag=0`. The optional portfolio connection therefore ingests the
rows for diagnostics but cannot allocate to them until a separate reviewed OOS promotion freezes
the calibration contract.
