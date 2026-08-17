# Consumer Defensive Implementation Stages And Acceptance Tests

This contract follows `technology/STAGE_GATES.md`. Script numbers are organizational; the Stage 12 runner's explicit step table becomes authoritative after it exists.

## Stage 0 - Architecture And Governance

Goal: freeze the independent sector boundary and all contracts needed before code or data ingestion.

Acceptance tests:

- Consumer Defensive imports no sector-specific module from Technology, Industrials, Medical Devices, or Biotech.
- Database, cache, output, source-registry, current-universe, and historical-universe paths resolve from Consumer Defensive configuration or package-owned executable policies.
- The sector label is `Consumer Defensive`, the Portfolio Layer label is `Consumer Staples`, and the model family is `consumer_defensive`.
- The four calibration cohorts and applicability subtypes are explicit.
- The first requested dated snapshot is `2019-01-02`.
- Market history begins no later than `2017-11-28`, providing the configured 400-calendar-day warm-up.
- `SPY` is the trading-calendar benchmark and `XLP` is the sector-relative benchmark.
- Reconstructed PIT history and strict OOS provenance are separate contracts.
- The specialized-metric candidate registry contains definitions and applicability but no production weights.
- Every reviewed local CSV input is tracked and reconciles to `data/authoritative_input_manifest.yaml` by exact path, parsed nonblank record count, schema/review metadata, and SHA-256 hash.
- Configuration fails closed if an authoritative CSV is missing, unlisted, tampered with, outside the repository root, or duplicated in the manifest. This gate runs before any stage database mutation.

## Stage 1 - Database Foundation

Goal: create the independent Consumer Defensive SQLite foundation.

Run order:

```powershell
python consumer_defensive\scripts\00_init_consumer_defensive_db.py
```

Acceptance tests:

- A clean scratch database initializes and reinitializes idempotently.
- Required foundation tables exist: `sector_database_identity`, `runs`, `source_registry`, `ingestion_runs`, `raw_api_responses`, `dim_company`, `dim_security`, `dim_identifier`, `dim_company_alias`, `dim_consumer_defensive_taxonomy`, `dim_universe_membership`, `dim_specialized_metric`, and `data_quality_issues`.
- Market, SEC, FX, positioning, specialized-observation, and feature table shells exist for later stages.
- The sector-neutral `sec_parser_*` schema exists in the Consumer Defensive database.
- Source and specialized-metric registries load without duplicate identifiers.
- No table name or default references another sector.
- The single database identity row equals `consumer_defensive` / `Consumer Defensive` / `consumer_defensive` for model family, internal sector, and schema owner.
- Connection and initialization reject a foreign identity, a foreign-sector table signature, or a nonempty unowned database before schema or journal-mode mutation. Only an empty database or the recognized legacy Consumer Defensive taxonomy may be claimed during migration.
- DDL uses SQLite complete-statement parsing and one outer `BEGIN IMMEDIATE` or nested `SAVEPOINT`. Statement, migration-backfill, registry, or foreign-key-postcondition failure rolls back the whole schema unit; reinitialization is idempotent.
- Initialization records a successful run and does not ingest the ticker universe.

## Stage 2 - Security Master And Universe

Goal: load the reviewed current universe and point-in-time historical/delisted membership.

Implemented run order:

```powershell
python consumer_defensive\scripts\01_load_consumer_defensive_universe.py
python consumer_defensive\scripts\01b_load_consumer_defensive_historical_membership.py
python consumer_defensive\scripts\02_validate_consumer_defensive_universe.py
```

Binding universe decisions are recorded in `consumer_defensive/UNIVERSE_DECISIONS.md` and enforced by `consumer_defensive/data/consumer_defensive_universe_policy.yaml`.

Norgate is the authoritative point-in-time provider for the union of Russell 3000, S&P Composite 1500, NYSE Composite, and Nasdaq Composite membership. XLP, IYK, and FSTA are current-surface validation sources only. `CCE -> CCEP` and `DPS -> KDP` are provider-continuous ticker lineages and must not create duplicate delisted securities.

Historical cohort counts below the target of 20 are reported as calibration diagnostics. They do not fail Stage 2 or pre-judge later promotion evidence; the exploratory floor remains 12.

The final isolated v5 replay passed Stage 2 with 108 current and 11 reviewed historical securities. All 108 current and all 11 historical candidates have exact four-index daily membership series and Norgate asset identities. Current cohort counts are 22 Beverages, 22 Consumer Staples Distribution & Retail, 25 Household/Personal/Tobacco, and 39 Packaged Foods/Agricultural Products. Two terminal-return-incomplete securities and the Beverages historical breadth floor of 19 remain explicit diagnostics.

Acceptance tests:

- `ticker_mapping/consumer_defensive.csv` is the authoritative current source.
- Current ticker, security, issuer, primary-listing, ADR, domicile, and exchange fields are auditable.
- Cohorts reconcile to Beverages, Consumer Staples Distribution & Retail, Household Personal & Tobacco, and Packaged Foods & Agricultural Products.
- Historical rows use exact non-overlapping membership intervals and never become live-investable.
- Ticker reuse, aliases, share classes, predecessor/successor lineages, and current-symbol collisions are explicit.
- Membership evidence records ETF/index or approved historical-provider provenance.
- Duplicate tickers and security keys fail closed.
- The loaded current and historical candidate sets exactly match their reviewed sources and terminal-event scope; an unresolved, missing, or extra candidate fails before publication.
- Provider symbols are stored as returned by Norgate, including punctuation-bearing share classes; ticker normalization cannot rewrite provider identity.
- Norgate `US Equities`, `US Equities Delisted`, and `US Indices` catalog, per-candidate extraction, and final fingerprints are identical for the complete run.
- Each reviewed index/vehicle has exact provider-date coverage. Daily breadth includes every cohort/date combination, including explicit zero-name combinations.
- Membership/provider identifiers are staged in memory and published in one transaction only after all provider calls and current-membership gates pass. Fingerprint drift leaves database membership state and Stage 2 report files unchanged.

## Stage 3 - Market Data And Corporate Actions

Goal: load adjusted historical prices, benchmarks, corporate actions, and terminal events.

Implemented run order:

```powershell
python consumer_defensive\scripts\03_sync_consumer_defensive_adjusted_prices.py
# Targeted rerun only: python consumer_defensive\scripts\03c_reconcile_consumer_defensive_terminal_events.py
python consumer_defensive\scripts\04_audit_consumer_defensive_market_data_policy.py
python consumer_defensive\scripts\05_build_consumer_defensive_market_features.py
python consumer_defensive\scripts\06_validate_consumer_defensive_market_stage.py
```

Acceptance tests:

Yahoo is the active-book and benchmark primary. Norgate is mandatory for delisted history and is an active whole-ticker fallback. Selection is stored per ticker and purpose; per-date provider splicing is prohibited. The exact contract is recorded in `MARKET_DATA_DECISIONS.md` and `data/consumer_defensive_market_data_policy.yaml`.

The final isolated v5 policy audits passed at both checkpoints. At `2019-01-02`, all 103 then-relevant candidates selected one qualifying source (92 Yahoo and 11 Norgate), with MAMA correctly excluded as future-only. At `2026-08-10`, all 121 required series (119 securities plus `XLP` and `SPY`) qualified, selecting 108 Yahoo and 13 Norgate series. The official current feature build wrote 108/108 full-quality active-security rows, and the Stage 3 validator passed. MAMA selected Norgate for all 1,549 expected sessions in its membership-aware `2020-06-10` through `2026-08-10` window; its older omissions are outside the required warm-up. These runs did not write the production database.

The aggregate Stage 3 loader now reconciles the 11 loaded delisted securities against a primary-document ledger. Ten events are survivorship-complete. WBA remains a visible `PASS_WITH_EXCLUSION`: its 11.45 cash floor is stored, but its non-transferable contingent right is not assigned a value and labels whose horizon crosses that event are barred from calibration until the right is resolved. Earlier WBA observations whose label horizon ends before the event remain usable.

CORE's reviewed successor price symbol is the tradable Yahoo symbol `PFGC`, not issuer shorthand `PFG`.

- Adjusted OHLCV exists for eligible current and historical securities, `SPY`, and `XLP`.
- The load begins no later than `2017-11-28`.
- Price-source priority and total-return basis are explicit.
- Split, dividend, merger, delisting, and terminal-value status are explicit.
- Cash-and-stock outcomes use one reviewed successor series and preserve successor total return from the first tradable reference bar through the requested horizon.
- An economic cancellation date overrides later provider quotes without deleting those raw observations.
- Low-history and low-liquidity names are reported rather than silently excluded.
- Historical rows without reconciled terminal truth are not survivorship-complete.
- Market features are reproducible as of each requested date without future bars.
- Yahoo and Norgate payloads must match requested ticker/provider symbol, requested window, chronological order, row shape, and finite-value rules before cache or database publication.
- A refresh replaces price and action rows only inside the exact requested range, deleting stale in-range rows while preserving observations outside the range.
- Completeness is checked against the relevant trading calendar, including internal-session gaps and active/delisted end-date policy; first/last dates and row counts alone are insufficient.
- The security-specific start is the later of global history start, listing start, or the configured 400-calendar-day warm-up before first calibration-eligible recognized membership. A reviewed terminal exclusion falls back to first recognized membership; a future-only name does not contaminate an earlier PIT audit.
- Missing trading sessions fail above 2% or above five consecutive sessions and warn above 1%. The audit publishes both providers' exact expected, observed, missing, ratio, and longest-gap diagnostics.
- Yahoo `--cache-only` forbids network access, rejects force refresh, and fails on any missing cache payload. Its result carries per-payload byte/hash lineage and a deterministic aggregate manifest hash.
- Norgate price/action facts publish only after both `US Equities` and `US Equities Delisted` fingerprints remain stable for the full extraction. Drift writes no price/action rows and records a failed zero-row ingestion run.

## Stage 4 - SEC Financial Statements And FX

Goal: load point-in-time filing metadata, raw XBRL facts, canonical financial statements, FX history, and common financial features.

Implemented run order:

```powershell
python consumer_defensive\scripts\07_sync_consumer_defensive_sec_fundamentals.py
python consumer_defensive\scripts\07a_sync_consumer_defensive_inline_xbrl_fallback.py
python consumer_defensive\scripts\11_sync_consumer_defensive_fx_rates.py
python consumer_defensive\scripts\08_build_consumer_defensive_financial_features.py
python consumer_defensive\scripts\08a_run_consumer_defensive_specialized_disclosure_census.py
python consumer_defensive\scripts\08b_validate_consumer_defensive_financial_and_disclosure_stage.py
python consumer_defensive\scripts\08c_build_consumer_defensive_census_review_pack.py
```

Acceptance tests:

- Filing availability uses SEC acceptance datetime.
- Raw `us-gaap`, `ifrs-full`, and `dei` facts are preserved before canonical mapping; unmapped facts remain in the raw table.
- Historical members are included when identifiers and membership intervals are eligible.
- Reported-currency facts remain preserved and USD valuation fields use PIT FX.
- Quarterly, annual, and TTM features do not use facts before acceptance.
- Foreign-private-issuer and inline-XBRL fallback paths are explicit.
- Missing or conflicting concepts become data-quality issues rather than implicit zeroes.
- Canonical reporting taxonomy/currency is unambiguous; revenue selection uses a same-context gross-profit identity; approved capex payment concepts have explicit sign normalization; and reported plus normalized values retain source-fact lineage.
- TTM flows use four nonoverlapping quarters or a safe annual-plus-current-minus-prior bridge. Stale annual values and cross-period, cross-taxonomy, or cross-currency ratio inputs fail closed.
- ROIC and inventory turnover use compatible beginning/ending balance context rather than an unexplained point balance.
- FX anomaly classification uses strictly prior observations. Raw rates and reasons remain stored, quarantined rates are excluded from conversion, and reviewed redenomination exemptions remain auditable.
- Yahoo FX chart arrays remain positional: `null` closes are missing observations, while every non-null close must be numeric, finite, and positive. No more than two rows within seven calendar days outside the requested boundary may be filtered; symbol/shape/order defects, material range mismatch, and zero usable in-window rows fail closed.
- Financial features carry the current definition version, basis period end, accepted-time lineage, rejected-input reasons, and explicit complete/partial/missing/stale quality state.
- Stage 4 schema migrations are immutable ordered units with registered checksums. They run atomically through migration v10, use bounded backfills, verify parity and foreign keys, and reject ledger gaps, future versions, or checksum drift. SEC ingestion-config v8 and issuer-scope contract v3 bind normalized reporting currency and explicit financial-form families. Companyfacts base forms may match only recognized submissions amendment or transitional variants in the same family; unrelated form conflicts fail closed. Migration v9 adds accession-keyed indexes for bounded shared-filing reconciliation; migration v10 adds sealed inline-XBRL fallback provenance and reporting-profile lineage without changing the SEC seal.
- The SEC ingestion as-of watermark advances in the same transaction as every full, targeted, partial, and reconciliation mutation. An older request fails before configuration/cache/provider access or database writes. Historical reconstruction begins at the earliest required date in a fresh scratch database and runs chronologically.
- Every complete reconciliation and immutable cache snapshot matches the current ingestion-config hash and exact issuer-scope hash. Legacy rows may be retired or quarantined but cannot masquerade as current reconciled evidence.
- Shared accessions and documents use explicit issuer bridges. Association lifecycle events are append-only, effective-dated, and deterministically hashed; raw/canonical observations have exact deterministic source identities.

- Every loaded active/historical security has one reviewed cohort applicability subtype.
- The discovery-only census routes candidate metrics by cohort and subtype, records hashed evidence locations, and distinguishes `applicable_term_hit`, `applicable_no_term_hit`, `parse_unavailable`, and `not_applicable`. A phrase hit is not treated as proof of numeric disclosure.
- Census output never creates specialized observations or nonzero model weights; Stage 6B still owns extraction, adjudication, and promotion of technically usable metrics.
- `08b` fails unless the census matrix is complete for the loaded taxonomy and registered metric set.
- SEC and FX `--cache-only` runs forbid network access and publish byte/SHA-256 cache manifests. SEC sync reports every missing required submission, Companyfacts, archive, or eligible filing-document cache object as an explicit failure; missing documents cannot be hidden as hydration status.
- SEC seals use a global immutable SHA-256 CAS plus contained date-local `sealed/YYYY-MM-DD` objects. Mutable acquisition aliases are never the immutable hardlink source. Canonical relative paths, nested document names, quoted URL segments, reserved names, symlinks, hashes, sizes, and resolved containment all fail closed on mismatch.
- Cache-only SEC reuse requires an exact current config, scope, complete reconciliation, lifecycle state, and verified date seal. The census reads only selected bytes from that seal, never a mutable alias or documents accumulated outside the snapshot.
- Before Stage 5, the stratified review in `CENSUS_TERMINOLOGY_AUDIT_2026-08-11.md` checks obvious census false positives and false negatives across cohorts/subtypes; this is terminology QA, not parser promotion.
- The shared parser's Consumer Defensive fail-closed catalog/direct-document intake boundary is implemented, but it is not a Consumer Defensive specialized-metric implementation. The sector adapter, historical filing/document inventory, shadow extraction, census reconciliation, golden corpus, and any promotion decision remain Stage 6B work. Production promotion is disabled.
- Stage 4 must be run across the complete loaded active/historical taxonomy before Stage 5 begins; the limited KO smoke test verifies plumbing only and is not the Stage 4 completion gate.

The `2026-08-11` report is retained only as a legacy pre-hardening baseline. The fresh isolated chronological v5 replay completed under schema migration v9, SEC ingestion-config v8, and issuer-scope v3 without writing the production database. SEC reconciliation covered all 119 issuers with zero failures: 209,111 associations, 208,705 unique accessions, 406 shared accessions, 2,149,695 raw facts, and 952 selected sealed documents. The 1,287-file SEC cache manifest is 947,150,199 bytes with SHA-256 `caf6d962f05485aa46a123bc488d32f53b851dc3b7f0e338e7adea3af6fd669c`; the association SHA-256 is `d1300f5fd1eb15b3dd1431b2f9312c4f5658df9689c5c2a9f8c6fd31437fa540`.

A fresh chronological migration-v10 replay began from an empty database and consumed only the retained `2026-08-10` caches through the Consumer Defensive Stage 0-4 entry points. It reproduced the exact SEC cache and association manifests. BTI and BUD were proven nonfinancial metadata-only 6-K anchors. FMX, JBS, KOF, and UL produced 13,176 numeric, 3,111 consolidated, and 130 mapped facts. After canonical rebuild the current validator passes 40/40 checks: 119/119 profiles are covered, fallback-provenance mismatches are zero, 2,152,806 raw facts and 231,024 canonical facts are present, canonical FX missing is zero, and all 119 feature rows retain the prior quality distribution. Streamed raw, canonical, feature, and current census-v3 semantic hashes exactly match the retained migration-v10 continuation. The production database and preserved v5 evidence database remain untouched.

The official `2010-01-01` through `2026-08-10` FX cache-only replay accepted 12 currencies and published 49,867 rates, including 49,815 usable and 52 quarantined. Its exact 12-file, 5,694,168-byte range manifest has SHA-256 `99deee8510b8e10b4ed581930fe1ad7f06fa01c67f9532563809126f19e486f6`. CLF is the sole source gap: its preserved payload has only an observation after the cutoff, so the sync correctly exits nonzero with partial status. CLF is not required by any selected canonical fact; all five required currencies are covered and `canonical_fx_missing` is zero. This upstream gap is disclosed but does not add a validator failure.

The Companyfacts/inline-XBRL code gate and generated 10-row stratified terminology adjudication are resolved. Census parser v3 removes standalone `sales leaders`, retains the representative-specific triggers, and the accepted `2026-08-10` sample remains bound to its exact seal. The explicitly approved production rollout completed at the existing `2026-08-11` watermark after a verified pre-change backup and backup-only rehearsal. Migrations v2-v10 are complete; production has 2,153,234 raw facts, 231,066 canonical facts, 49,879 FX rows, 119 features, 4,522 current census-v3 summaries, and 778 census-v3 evidence rows. Its exact reconciliation covers 119 issuers, 209,031 active associations, 208,625 active accessions, and 406 shared accessions, with 131 associations retired non-destructively. The live Stage 4 validator passes 40/40, `integrity_check=ok`, and the full code suite is 402 passed with 6 platform-specific skips. Stage 4 is closed for production; current-date seal completeness still does not satisfy Stage 6B's separate historical filing/document inventory requirement.

The pre-fix production rerun was aborted after more than 18 minutes because issuer replacement lacked an index on the canonical foreign-key child, used a raw index that did not exactly match the ticker/source/accepted-time delete, and inserted raw facts row by row. The implemented `idx_stage4_canonical_raw_fact_id`, exact `idx_stage4_raw_ticker_source_accepted`, and per-issuer bulk insert fix that query shape. The historical 48.2-second isolated result predates the current full reconciliation/sealing contract and is not a current end-to-end performance claim. Query-plan regression tests must continue to prove both delete paths use their intended indexes.

Stage 4 introduced a reviewed historical SEC CIK registry. Physical XBRL units such as `GAL`, `JOB`, and `TON` are excluded from the configured currency allowlist rather than treated as monetary facts.


## Stage 5 - Ownership, Insider, And Positioning

Goal: load Consumer Defensive-owned normalized ownership and positioning history.

Implemented run order:

```powershell
python consumer_defensive\scripts\09_sync_consumer_defensive_sec_ownership.py
python consumer_defensive\scripts\09a_sync_consumer_defensive_positioning_upstream.py
python consumer_defensive\scripts\10_import_consumer_defensive_positioning.py
python consumer_defensive\scripts\10a_validate_consumer_defensive_sec_positioning.py
python consumer_defensive\scripts\10b_audit_consumer_defensive_foundation_coverage.py
```

Acceptance tests:

- External upstream databases are read-only.
- The Consumer Defensive package owns every sector-specific schema, import, feature, validation, and audit step; it does not import Technology or Industrials scripts.
- Cache-only upstream rematching is performed only by the sector-neutral `market_positioning` owner utility against an explicitly selected target database.
- Direct SEC ownership facts are written only to Consumer Defensive-owned tables.
- 13F, FINRA short-interest, and borrow source birthdates are explicit.
- Missing-era observations are null and unavailable, not zero.
- The foundation-coverage audit reports daily cohort breadth, warm-up sufficiency, SEC/FX coverage, positioning availability, and terminal-event gaps from `2019-01-02`.
- It estimates the earliest potential common-feature date but does not certify the final historical scoring panel.
- The continuation review chooses full Stage 6 work, a limited shadow candidate, or defer.

Deployed acceptance at `2026-08-11`:

- The production validator passes 18/18 checks at the strict `100%` current gate: 13F 108/108, short-interest signal 108/108, and required numeric positioning features 108/108.
- Section 16 source coverage is 95/95 applicable current domestic issuers. Thirteen current foreign private issuers are explicit not-applicable rows; raw transaction-ticker counts are therefore not the coverage denominator.
- The transaction table contains 14,246 observations across 104 transaction tickers. The rematch imports 1,724 PIT 13F aggregates across 118 taxonomy tickers and 12,567 FINRA rows across 115 taxonomy tickers.
- Short-float percentage is 105/108. IMKTA, ODD, and STZ remain null at the ratio level because no safe PIT float proxy exists, but their days-to-cover values satisfy the v2 short-signal contract without inventing ratios.
- Borrow coverage is zero and remains optional with a zero minimum; missing borrow data are null, not neutral or zero observations.
- The reviewed public/provider split is now explicit: public `FDP`/CIK `1047340` maps to reviewed Norgate `DMC`/asset `132283`; DMC Global/`BOOM` remains out of scope. Fresh chronological replay and a production-copy rehearsal both passed before deployment.
- Production Stage 5 passes 18/18 at `2026-08-11`: current 13F 108/108, short interest 108/108, required numeric positioning features 108/108, and Section 16 source coverage 95/95 applicable domestic issuers. Thirteen current foreign private issuers are explicit not-applicable profiles.
- The neutral cache rematch ran with network access forbidden and produced 9,867,997 total 13F holdings plus 197,912 FINRA rows. Both production databases pass `quick_check` with zero foreign-key violations; the pristine pre-change backups retain their recorded hashes.
- The Stage 6A foundation audit passes with `proceed_stage6a` and earliest potential common-feature date `2026-05-28`. This remains a foundation gate, not the definitive Stage 6C historical-panel certification.

## Stage 6A - Core Scoring Feature Contract

Goal: create the stable scoring-table contract from Consumer Defensive-owned market, financial, and positioning features.

Acceptance tests:

- One scoring-input row exists for every eligible current security.
- Common component rows preserve raw values, normalized values, source dates, coverage, and exclusion reasons.
- Reserved specialized component rows exist with status `not_loaded` and zero weight.
- Missing values are not converted to neutral observations.
- Required core components have sufficient cross-sectional variance.

## Stage 6B - Specialized Cohort Overlays And Dedicated Parser

Goal: determine which registered candidate metrics are technically usable and apply valid overlays without changing the Stage 6A table shape.

Planned order:

1. freeze the Consumer Defensive metric definitions, cohort/subtype applicability, unit, period, scope, and plausibility policies;
2. implement the Consumer Defensive-owned dedicated-parser adapter without importing another sector adapter;
3. build the complete PIT historical filing/document inventory required from `2019-01-02`, reconcile it to exact Stage 4 date seals, then run plan-only completeness and hydrate missing eligible documents through Stage 4;
4. run dedicated-parser shadow extraction and reconcile parser results to Stage 4 census hits and misses;
5. build and pass a reviewed Consumer Defensive golden corpus containing positive and prohibited expectations;
6. adjudicate evidence and implement PIT numeric extraction only for technically viable metrics;
7. build specialized features and apply only validated zero-to-nonzero overlay candidates; and
8. re-run the unchanged Stage 6A/6B scoring-contract validation.

Acceptance tests:

- Coverage and attrition are reported by cohort, subtype, metric, form, and extraction channel.
- The parser is supplied an independently computed expected ingestion-config hash plus paired direct filings/documents. The filing set must match the current PIT parser view; every supplied document must match an active PIT bridge row and immutable seal; and the filing/document keysets must be exact.
- Direct document paths remain inside the sealed accession root and match manifest hash/size identity; traversal, absolute paths, reserved names, case collisions, and symlink escape fail closed.
- Applicability, units, definitions, amendments, and acceptance timestamps are versioned.
- Definition conflicts and insufficient coverage remain review or measurement-only.
- Stage 6B cannot add a nonzero production weight.
- The Stage 6A contract still validates after overlay application.


## Stage 6C - Final Historical Feature-Panel Readiness

Goal: after Stage 6A and Stage 6B are fixed, build and validate the exact point-in-time feature panel that signal diagnostics, factor validation, calibration, and eventual dated-file backfill will use.

Planned run order:

```powershell
python consumer_defensive\scripts\14_build_consumer_defensive_historical_feature_panel.py
python consumer_defensive\scripts\14a_validate_consumer_defensive_historical_feature_panel.py
```

Acceptance tests:

- The panel begins on `2019-01-02` when data permit and reports every unavailable earlier or later ticker/date explicitly.
- Each row uses PIT membership, SEC acceptance timestamps, source birthdates, applicable parser definition versions, and terminal-event eligibility.
- The manifest freezes the included common and specialized feature IDs, units, directions, applicability masks, source IDs, parser/adapter versions, and missing-data rules.
- Specialized metrics that failed Stage 6B remain absent or measurement-only; they are not converted to zero or silently included.
- Daily overall and cohort breadth, per-feature coverage, attrition reasons, and the earliest reproducible date are published.
- Rebuilding with identical inputs produces identical row counts and content hashes.
- The audit creates research-panel artifacts only. It does not create final scores, ranks, Portfolio Layer candidates, or claim strict OOS history.

This is the definitive historical-readiness audit. The Stage 5 audit is only a foundation-coverage checkpoint.

## Signal Diagnostics And Shared Factor Validation

Goal: measure common and specialized signals after Stage 6 and before Stage 7 scoring.

Acceptance tests:

- The Consumer Defensive adapter emits the shared `factor_validation` observation contract.
- Sector-wide and cohort analyses use PIT membership and XLP-relative targets.
- Local and shared-kernel per-date IC calculations reconcile within tolerance.
- Factor families, hypotheses, directions, horizons, and multiplicity controls are registered before acceptance.
- Sparse cohort dates are reported rather than padded.
- Diagnostics write evidence only and cannot change weights or Portfolio Layer files.

## Stage 7 - Calibrated Consumer Defensive Scoring

Goal: produce the reviewed baseline ranking layer from the validated Stage 6 contract.

Acceptance tests:

- Unknown component or subfeature weights fail fast.
- Every eligible current security has a score or explicit review reason.
- Stage 7 does not overwrite Stage 6 feature rows.
- Shadow scores remain observable while every portfolio gate stays off.
- Production weights are versioned and governance-controlled.

## Stage 8 - Constrained Calibration Research

Goal: run report-only constrained calibration and walk-forward validation.

Acceptance tests:

- The panel uses PIT membership and reconciled historical/delisted outcomes.
- Train, embargo, holdout, and walk-forward blocks are explicit.
- Candidate weights obey component, turnover, and cohort-concentration constraints.
- Factor-validation evidence and candidate provenance are hash-sealed.
- Stage 8 cannot modify Stage 7 weights automatically.
- Failed promotion evidence may remain useful for shadow monitoring.

## Stage 9 - Portfolio Backtest

Goal: compare Stage 7 and registered Stage 8 candidates through report-only portfolio simulations.

Acceptance tests:

- Backtests use the same PIT panel as calibration.
- Long-only, long-short, equal-weight, score-weight, and XLP-relative variants are reported.
- Turnover, transaction costs, borrow costs where available, drawdown, capacity, and cohort concentration are reported.
- Terminal values are consumed for eligible delisted outcomes.
- Stage 9 writes no production score changes.

## Stage 10 - Dashboard And Static Reports

Goal: publish current and dated final-rank tables, scorecards, cohort summaries, risk flags, review queues, overlay coverage, and backtest links.

Acceptance tests:

- Every current security has a deterministic score or review status.
- Dated outputs match their requested as-of date.
- Historical sidecar rows are always non-investable.
- Dashboard publishing is read-only with respect to source data and scores.

## Stage 10B - Governance Lockbox And Signal Registry

Goal: publish auditable evidence, weights, promotion state, and artifact hashes.

Acceptance tests:

- Every nonzero signal maps to registered evidence.
- Production-locked, research-candidate, measurement-only, and zero-weight signals are distinct.
- Stage 8, walk-forward, and Stage 9 decisions are recorded.
- `promoted`, `shadow_monitor`, and `deferred` states cannot be confused.

## Stage 11 - Portfolio Layer Adapter And End-To-End Handoff

Goal: integrate the Stage 10 file contract without coupling the Portfolio Layer to the sector package or database.

Acceptance tests:

- The adapter imports no Consumer Defensive module and opens no Consumer Defensive database.
- Missing required fields, stale rows, invalid OOS rows, and failed candidate gates fail closed.
- Stage 11 survivorship sidecars are calibration-only and non-investable.
- Canonical sector mapping is `Consumer Staples`.
- Collection, cross-sector calibration, contract validation, risk-panel, and optimizer smoke tests pass.

## Stage 12 - Refresh Orchestration

Goal: provide one independent, validated refresh entry point.

Acceptance tests:

- The explicit runner step table is the authoritative execution order.
- The runner supports `--asof`, `--dry-run`, `--skip-network`, research opt-ins, bounded steps, resume, and final audit.
- Routine refresh excludes Stage 8 searches, Stage 9 backtests, and one-time history imports.
- The runner stops on first failure by default.
- Step logs and run manifests are published.
- Promoted and shadow profiles use the same independent DB lane with different governance settings.

## Post-Stage-12 Historical Dashboard Backfill

Goal: generate restartable daily dated sector files and Stage 11 survivorship sidecars beginning `2019-01-02`, following Technology's historical runner/supervisor pattern.

Acceptance tests:

- The NYSE trading calendar contains every requested session.
- Every date is rebuilt from locally loaded PIT data using `--asof`.
- Rank tables, sidecars, and manifests validate for the same date.
- Failures are restartable by chunk and individual date.
- The current latest dashboard is restored after historical generation.
- Reconstructed deep history has `oos_score_valid_flag=0` unless the lock-date and contemporaneous-capture contract explicitly permits strict OOS.
