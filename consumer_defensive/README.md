# Consumer Defensive Scoring Model

This package is the independent implementation namespace for Consumer Defensive stock scoring. It owns its configuration, SQLite database, source registry, security master, point-in-time universe, parser state, features, scores, reports, and orchestration lane.

The implementation sequence follows `technology/README.md` and `technology/STAGE_GATES.md`. Consumer Defensive does not import from or write to Technology, Industrials, Medical Devices, or Biotech sector packages.

## Independence Rules

- Default database: `${CONSUMER_DEFENSIVE_DB_DIR:-C:/Users/josel/Documents/STAGING/DB}/consumer_defensive.sqlite`.
- Default outputs: `output/consumer_defensive`.
- External source databases are read-only inputs unless their owning pipeline is being run independently.
- Consumer Defensive may use the top-level sector-neutral `dedicated_parser` and `factor_validation` kernels through Consumer Defensive-owned adapters.
- Portfolio Layer integration is file-only. The Portfolio Layer adapter must not import this package or open its database.
- No table, default, path, or model-family value may silently fall back to another sector.
- Every initialized database carries the fixed `sector_database_identity` tuple for Consumer Defensive. Connections reject foreign identities, foreign-sector tables, and nonempty unowned databases before schema or journal-mode mutation.
- Configuration loading verifies the complete reviewed CSV inventory against `data/authoritative_input_manifest.yaml` by exact path, parsed record count, review/schema metadata, and SHA-256 before a stage may open its write path.

## Canonical Stage Order

1. Stage 0 - architecture and governance
2. Stage 1 - database foundation
3. Stage 2 - security master and current/PIT historical universe
4. Stage 3 - market data and corporate actions
5. Stage 4 - SEC financial statements and FX
6. Stage 5 - ownership, insider, and positioning
7. Stage 6A - core scoring feature contract
8. Stage 6B - dedicated parser and cohort-specialized overlays
9. Stage 6C - definitive historical feature-panel readiness
10. signal diagnostics and shared factor validation
11. Stage 7 - calibrated baseline scoring
12. Stage 8 - report-only constrained calibration and walk-forward validation
13. Stage 9 - report-only portfolio backtest
14. Stage 10 - dashboard and dated reports
15. Stage 10B - governance lockbox and signal registry
16. Stage 11 - Portfolio Layer adapter and end-to-end handoff
17. Stage 12 - refresh orchestration
18. post-Stage-12 daily historical dashboard and survivorship-sidecar backfill

## Historical Contract

- First requested dated snapshot: `2019-01-02`.
- Minimum market warm-up: 400 calendar days, beginning no later than `2017-11-28`.
- Trading calendar: `SPY`; sector benchmark: `XLP`.
- Current-universe replay is not accepted as point-in-time history.
- Historical membership must include exact intervals, aliases, lineages, delistings, and terminal events.
- Deep historical replays are reconstructed PIT calibration files. They cannot carry `oos_score_valid_flag=1` unless the configured lock-date and contemporaneous-capture rules permit it.

## Stage 1 Database Initialization

Initialize a scratch database:

```powershell
python consumer_defensive\scripts\00_init_consumer_defensive_db.py --db C:\tmp\consumer_defensive.sqlite
```

The initializer creates only Consumer Defensive-owned canonical tables and the sector-neutral dedicated-parser schema. It loads the reviewed source registry and specialized-metric candidate registry, but it does not ingest the ticker universe or external data. A new empty database is allowed; a nonempty database must already have the Consumer Defensive identity or the narrowly recognized legacy Consumer Defensive taxonomy needed for one-way identity migration. A foreign or otherwise unowned database fails before mutation.

Schema publication is atomic. DDL is parsed with SQLite's complete-statement boundary, then applied under one outer `BEGIN IMMEDIATE` or a nested `SAVEPOINT`. Any statement, migration-backfill, foreign-key postcondition, or registry failure rolls the complete schema unit back. Reinitialization is idempotent; it is not allowed to leave a partially upgraded database.

## Stage 2 Universe Load

Run the independent security-master and Norgate point-in-time membership sequence:

```powershell
python consumer_defensive\scripts\01_load_consumer_defensive_universe.py
python consumer_defensive\scripts\01b_load_consumer_defensive_historical_membership.py
python consumer_defensive\scripts\02_validate_consumer_defensive_universe.py
```

The binding membership, lineage, share-class, and provider decisions are in `UNIVERSE_DECISIONS.md`. Stage 2 uses `data/consumer_defensive_universe_policy.yaml`; it does not import another sector's universe infrastructure.

The Norgate loader writes per-index daily membership, major-exchange status, compressed approved-index-union intervals, provider asset identifiers, resolution diagnostics, and daily cohort breadth.

Membership extraction is snapshot-atomic and candidate-complete. The loader must reconcile the exact reviewed current and historical candidate sets, including the terminal-event ledger, before publication. It holds all candidate results in memory and fences the complete Norgate provider surface used by the run: `US Equities`, `US Equities Delisted`, and `US Indices`. Catalog, per-candidate, and final fingerprints must agree. Provider-symbol identities, including punctuation-bearing share classes, are preserved rather than reconstructed from tickers. Fingerprint drift, an unresolved reviewed candidate, or a candidate-scope mismatch publishes neither membership rows nor reports.

The validator checks exact date/vehicle coverage and emits daily overall and cohort breadth for the full provider date range. A zero-name cohort/date is explicit evidence, not a missing row that can disappear from the diagnostic.

The reviewed local inputs are enumerated with record counts and SHA-256 hashes in `data/authoritative_input_manifest.yaml`. A production or clean-room run must verify that manifest before mutating the database.

The final isolated v5 Stage 2 replay passed with 108 current and 11 reviewed historical securities. All 108 current and all 11 historical candidates have exact four-index daily membership series and Norgate asset identities. Current cohorts reconcile to 22 Beverages, 22 Consumer Staples Distribution & Retail, 25 Household/Personal/Tobacco, and 39 Packaged Foods/Agricultural Products. The two terminal-return-incomplete securities and the historical Beverages breadth floor of 19 remain explicit diagnostics, not hidden exclusions.

## Stage 3 Market Data

Run the independent market-data sequence after Stage 2 passes:

```powershell
python consumer_defensive\scripts\03_sync_consumer_defensive_adjusted_prices.py
# Targeted terminal-only rerun: python consumer_defensive\scripts\03c_reconcile_consumer_defensive_terminal_events.py
python consumer_defensive\scripts\04_audit_consumer_defensive_market_data_policy.py
python consumer_defensive\scripts\05_build_consumer_defensive_market_features.py
python consumer_defensive\scripts\06_validate_consumer_defensive_market_stage.py
```

The provider contract is binding:

- Yahoo adjusted chart data is the live primary source for active securities, `XLP`, and `SPY`.
- Norgate total-return data is mandatory for delisted securities and is a whole-ticker active fallback.
- A scoring return series selects one provider for the entire ticker. Date-level Yahoo/Norgate splicing is prohibited.
- Required coverage is PIT-scoped: it starts at the later of the global history start, listing start, or the configured 400-calendar-day feature warm-up before first calibration-eligible recognized membership. Explicit terminal-event exclusions still use their first recognized interval, while a future-only security cannot fail an earlier historical audit.
- Inside that required window, more than 2% missing trading sessions or more than five consecutive missing sessions fails. More than 1% is an explicit warning even when the series remains usable.
- Provider payloads must match the requested symbol, date window, ordering, and row shape before cache or database publication. A malformed or identity-mismatched cached payload fails in cache-only mode and is repaired only by a validated live response in network mode.
- A refresh replaces prices and corporate actions only inside its exact requested window. Earlier and later observations remain untouched, while stale rows inside the refreshed window are removed.
- Raw OHLCV stays unadjusted; `adjusted_close` carries the provider-specific return series.
- Recent listings with complete prices but too little lookback are reported as unavailable long-window features, not treated as provider failures.
- A delisted series is not survivorship-complete until its terminal consideration is separately reconciled.
- Reviewed fixed cash and wipeout values are stored directly. Cash-and-stock events retain nominal cash and roll successor shares with that successor's adjusted total-return series.
- Dean Foods ends economically on 2021-05-28 at zero even though Norgate preserves raw quotes through 2021-06-02.
- WBA is the sole explicit terminal-crossing exclusion: the 11.45 cash floor is known, while the contingent right remains unresolved and is never imputed. Pre-event labels that end before 2025-08-28 remain eligible.

Binding decisions are in `MARKET_DATA_DECISIONS.md`; executable policies are in `data/consumer_defensive_market_data_policy.yaml` and `data/consumer_defensive_terminal_event_policy.yaml`. The split loaders `03a_sync_consumer_defensive_yahoo_prices.py`, `03b_import_consumer_defensive_norgate_prices.py`, and `03c_reconcile_consumer_defensive_terminal_events.py` support controlled refreshes and diagnostics.

### Deterministic replay and atomic publication

- Yahoo, SEC, and FX network entry points support `--cache-only`. Cache-only mode forbids network access, rejects `--force-refresh`, and fails when a required cache object is missing.
- SEC cache-only sync treats a missing eligible filing-document cache object as an explicit sync failure; it may not be downgraded to a hydration-status-only result.
- Successful live cache writes use temporary-file replacement. Yahoo returns per-payload byte counts and SHA-256 hashes plus an aggregate payload hash; SEC and FX return deduplicated per-file byte/hash records plus an aggregate cache-manifest hash.
- Yahoo FX daily arrays remain positional: a JSON `null` close is a missing quote and is skipped. At most two observations within seven calendar days immediately outside the requested window may be validated and filtered; a wrong symbol, malformed or nonpositive non-null close, shape/order defect, material range mismatch, or absence of a usable in-window rate fails closed.
- Norgate price extraction verifies stable `US Equities` and `US Equities Delisted` provider-update fingerprints throughout extraction before publishing price/action facts in one transaction. Drift leaves those facts unchanged and records a failed zero-row ingestion run.
- Coverage is measured against the relevant trading calendar and requested window, including internal-session gaps; row counts or first/last dates alone cannot make an incomplete series pass.
- Coverage CSV rows publish expected, observed, missing, missing-ratio, and longest-gap diagnostics separately for Yahoo and Norgate.
- These controls prove input identity, repeatability, and atomic publication. They do not by themselves prove that a reconstructed run is strict point-in-time or strict OOS; the Historical Contract still governs those claims.

### Acceptance evidence

The final isolated v5 policy audits passed at both required checkpoints. At `2019-01-02`, all 103 then-relevant candidates selected one qualifying source (92 Yahoo and 11 Norgate); MAMA was correctly excluded as future-only. At `2026-08-10`, all 121 required series (119 securities plus `XLP` and `SPY`) qualified, selecting 108 Yahoo and 13 Norgate series. The official current feature build wrote 108/108 active-security rows, all with full quality, and the Stage 3 validator passed. MAMA's membership-aware required window is `2020-06-10` through `2026-08-10`; Norgate supplies all 1,549 expected sessions with no gap, while its 42 older omissions ended before the required warm-up window. These runs did not write the production database.

## Stage 4 SEC, FX, And Disclosure Feasibility

Run after Stage 3:

```powershell
python consumer_defensive\scripts\07_sync_consumer_defensive_sec_fundamentals.py
python consumer_defensive\scripts\07a_sync_consumer_defensive_inline_xbrl_fallback.py
python consumer_defensive\scripts\11_sync_consumer_defensive_fx_rates.py
python consumer_defensive\scripts\08_build_consumer_defensive_financial_features.py
python consumer_defensive\scripts\08a_run_consumer_defensive_specialized_disclosure_census.py
python consumer_defensive\scripts\08b_validate_consumer_defensive_financial_and_disclosure_stage.py
python consumer_defensive\scripts\08c_build_consumer_defensive_census_review_pack.py
```

Every command above is a Consumer Defensive entry point. The package owns its SEC, inline-XBRL fallback, FX, financial-feature, census, validation, and review-pack behavior; it does not invoke Technology scripts or use Technology data. Only sector-neutral path/seal helpers are shared. The census is an early, discovery-only extension: it searches accepted-time SEC filing documents only for metrics applicable to each security's reviewed cohort/subtype. It records coverage and hashed evidence locations but does not create specialized numeric observations or model weights.

`core/financial_semantics.py` owns the pure fail-closed decisions for prior-observation-only FX anomaly classification, accounting-identity revenue selection, approved capex payment-sign normalization, safe TTM construction, and reporting-context-compatible ratios. `core/financial_pipeline.py` applies those decisions to canonical facts and feature bundles while preserving reported and normalized values, accession/taxonomy/currency lineage, rejected candidates, average-balance ratio inputs, definition version, and explicit quality reasons.

FX sync preserves each raw positive rate, classifies anomalies from strictly prior observations, and quarantines unusable outliers instead of deleting or silently consuming them. Reviewed redenomination intervals remain visible exemptions. Financial conversion queries consume only `quality_status='usable'` rates.

### Hardened Stage 4 input and replay contract

Stage 4 schema evolution is an ordered, checksum-verified migration ledger through migration v10, with SEC ingestion-config v8 and issuer-scope contract v3. Migration units are immutable, run inside one transaction/savepoint, use bounded keyset backfills for large tables, verify backfill parity and foreign keys, and fail closed on a gap, future version, or checksum drift. Scope v3 binds normalized reporting currency in addition to ticker/company/CIK identity. Schema migration v8 quarantines older scope pointers non-destructively and invalidates trust when filing-company or reporting-currency inputs change; migration v9 adds the accession-keyed indexes required for bounded shared-filing reconciliation; migration v10 adds sealed inline-XBRL fallback provenance and reporting-profile lineage without changing the SEC acquisition seal. SEC ingestion-config v8 treats Companyfacts' canonical base form as equivalent only to a recognized submissions amendment or transitional financial-form variant (for example, `10-K` with `10-K/A` or `10-KT`); cross-family conflicts still fail closed. Legacy projections cannot be treated as current reconciled evidence.

SEC mutation is chronological. A singleton ingestion watermark advances transactionally with full, targeted, partial, and reconciliation mutations. A request older than the watermark is rejected before configuration/cache/provider access or database writes. Historical reconstruction must therefore start with a fresh scratch database at the earliest required date and advance in date order; the production database is not a reverse-time replay target.

Every accepted SEC snapshot binds the current ingestion-config hash and exact issuer scope to a complete reconciliation. The issuer scope includes the reviewed ticker/company/CIK identity and every other identity field declared by the current contract. Shared accessions use explicit many-to-many filing-company and document-company bridges. Association lifecycle changes are append-only, effective-dated, and deterministically hashed; raw and canonical facts carry deterministic source-observation identities.

Consumed SEC cache inputs are copied first into a global immutable SHA-256 content-addressed store and then linked or copied into the date-local `sealed/YYYY-MM-DD` snapshot. A seal never hardlinks directly to a mutable acquisition alias. Manifest paths, nested SEC document names, URL segments, Windows reserved names, symlinks, and resolved filesystem containment are validated fail-closed. Cache-only replay requires the exact current config, issuer scope, reconciliation, lifecycle state, and verified seal; a stale seal or mutable alias is not accepted.

The disclosure census reads only the exact bytes in the reconciled date seal and searches only documents selected into that snapshot. It cannot silently read a later mutable cache alias or all documents ever accumulated for an accession. The dedicated parser independently supplies the expected ingestion-config hash, recomputes the current issuer scope, validates exact lifecycle and manifest identities, and accepts only the sealed document inventory. Consumer Defensive parser production promotion remains disabled until Stage 6B's sector adapter, reviewed policies, golden corpus, and promotion gates exist.

### Acceptance status

The previously published `2026-08-11` report is a legacy pre-hardening baseline, not acceptance evidence for the current migration, scope, seal, lifecycle, and watermark contracts. It reported `19/20` checks, `113/119` Companyfacts reporting-profile coverage, 2,147,828 raw XBRL facts, 230,720 canonical facts, 49,879 FX rows including 52 quarantined rows, 956 hydrated filing documents, 4,522 census-summary rows, and 781 census-evidence rows. Those counts must not be copied into a current status report.

The fresh isolated chronological v5 replay completed under schema migration v9, SEC ingestion-config v8, and issuer-scope contract v3 without writing the production database. SEC cache-only reconciliation covered all 119 issuers with no sync failures: 209,111 issuer-filing associations, 208,705 unique accessions, 406 shared accessions, 2,149,695 raw facts, and 952 selected sealed documents. The exact 1,287-file, 947,150,199-byte SEC cache manifest has SHA-256 `caf6d962f05485aa46a123bc488d32f53b851dc3b7f0e338e7adea3af6fd669c`; the association manifest has SHA-256 `d1300f5fd1eb15b3dd1431b2f9312c4f5658df9689c5c2a9f8c6fd31437fa540`.

A fresh chronological migration-v10 replay was then run from an empty database through the Consumer Defensive Stage 0-4 entry points, using only the retained `2026-08-10` caches. SEC bootstrap reproduced the exact 209,111-association and 1,287-file cache manifests. BTI and BUD's later 6-K primaries contained no non-DEI numeric inline facts and were correctly treated as nonfinancial metadata filings. FMX, JBS, KOF, and UL produced 13,176 numeric facts, 3,111 consolidated facts, and 130 model-mapped facts under `consumer_defensive_inline_xbrl_v1`. The final validator passes all 40 current checks: 119/119 profiles are covered, fallback provenance mismatches are zero, raw facts total 2,152,806, canonical facts total 231,024, missing canonical FX conversions remain zero, and all 119 feature rows retain the 11 complete/85 partial/3 missing/9 stale/11 ineligible distribution. Streamed semantic hashes for every raw fact, canonical fact, and feature row exactly match the retained migration-v10 continuation; current census-v3 summaries and evidence also match exactly. This pre-production replay did not modify the production database or preserved v5 evidence database.

The 10-row terminology pack has now been manually adjudicated. Census parser v3 removes the unsafe standalone `sales leaders` trigger while retaining `active representatives` and `active distributors`. The exact-seal rerun parsed 952 documents and produced 4,522 summaries plus 779 evidence rows with zero failures. Its regenerated sample SHA-256 is `938ce70bf9151986e69c3664df15eb1cb585443cf0d8a543eff5d60e17f0071b`; the completed-ledger SHA-256 is `47938bc357c252d79150e0c3a1ba8f59a7399e3436b3aaa9e6a3bd1fa8c1dd61`. The ledger validates `ADJUDICATED` with six true negatives, four true positives, six `no_change`, and four `retain` actions. The current Stage 4 validator remains 40/40 PASS after the terminology change.

The official FX cache-only replay for `2010-01-01` through `2026-08-10` accepted 12 currencies and published 49,867 rows: 49,815 usable and 52 quarantined. Its exact 12-file, 5,694,168-byte range manifest has SHA-256 `99deee8510b8e10b4ed581930fe1ad7f06fa01c67f9532563809126f19e486f6`. CLF is the sole disclosed source gap: the preserved Yahoo payload contains only `2026-08-11`, outside the requested cutoff, so the sync correctly reports partial status and exits nonzero instead of inventing an in-window rate. No selected canonical fact currently requires CLF; all five required currencies are covered and `canonical_fx_missing` is zero. This is a non-gating upstream data gap, not a validator or conversion-code defect.

The explicitly approved production rollout completed at the existing `2026-08-11` chronological watermark after a transactionally consistent 1.382 GB pre-change backup and successful backup-only rehearsal. Migrations v2-v10 are complete. Full SEC reconciliation sealed 209,031 active issuer-filing associations, 208,625 active accessions, 406 shared accessions, and 131 non-destructively retired associations; the association SHA-256 is `365a2aefcc98007d0b2c816de6fa1fbedac96318756b116ac934c54200083e72`. The v10 fallback produced the accepted 13,176 numeric, 3,111 consolidated, and 130 mapped facts. Production now contains 2,153,234 raw facts, 231,066 canonical facts, 49,879 FX rates including 52 quarantined, 119 feature rows, 4,522 current census-v3 summaries, and 778 census-v3 evidence rows. The live validator passes 40/40 checks with 119/119 profiles covered, zero stale lifecycle outputs, zero canonical FX missing, zero lineage mismatches, zero foreign-key violations, and `integrity_check=ok`. The full Consumer Defensive/shared-parser suite passes 402 tests with 6 platform-specific skips; Ruff and scoped diff checks pass. Stage 4 production rollout is complete. Stage 6B separately requires a complete historical filing/document inventory before Consumer Defensive shadow extraction; current-date seal readiness does not establish that historical inventory.

The earlier long rerun exposed a database-access-path problem rather than a financial-semantic problem. Adding the canonical foreign-key child index `idx_stage4_canonical_raw_fact_id`, replacing the broad raw-fact index with the exact delete index `idx_stage4_raw_ticker_source_accepted`, and bulk-inserting issuer facts fixed that identified query shape. Query-plan tests lock both index paths, but the historical 48.2-second isolated result predates the current full reconciliation/sealing contract and is not a current end-to-end performance claim.

Stage 5 is now implemented as a Consumer Defensive-owned normalization layer. The sector package imports Form 4 observations from the SEC-insider store and authoritative 13F, FINRA short-interest, and optional borrow observations from the sector-neutral `market_positioning` store through read-only connections. It does not import or execute Technology or Industrials sector scripts. A separate neutral owner utility, `market_positioning/scripts/05_rematch_positioning_universe_from_cache.py`, can rebuild a specified upstream target from already retained source caches; Consumer Defensive itself never mutates the shared upstream database.

The disposable `2026-08-10` Stage 5 rehearsal imported 14,239 Form 4 observations across 104 taxonomy tickers, 1,652 PIT 13F observations across 114 tickers, and 12,335 FINRA short-interest observations across 114 tickers. No borrow observations were available, so borrow remains an explicitly optional null input. The current PIT gate covered 104/108 names for numeric 13F and 104/108 for numeric short interest (`96.296%` each); 100/108 current names (`92.593%`) had a complete required 13F-plus-short feature row, above the configured `80%` minimum. All lineage, birthdate, future-observation, missing-not-zero, ownership, feature-version, and foreign-key checks passed.

The foundation audit passed over 1,911 SPY trading dates from `2019-01-02` through `2026-08-10`, publishing 7,644 date/cohort rows. It reported no canonical FX gap, retained WBA as an explicit terminal-event case, estimated `2026-02-27` as the earliest potential common positioning-feature date, and recorded the continuation decision `proceed_stage6a`. This is a Stage 5 foundation result, not Stage 6C historical-panel certification. The rehearsal used disposable Consumer Defensive and upstream databases; neither the production Consumer Defensive database nor the shared production `market_positioning.sqlite` was modified.

The remaining binding order is:

1. rehearse and explicitly approve the production Stage 5 rollout, then apply the already accepted schema/import contracts to the production Consumer Defensive database and neutral upstream owner lane;
2. implement the stable Stage 6A common scoring-feature contract;
3. implement the Consumer Defensive Stage 6B metric adapter, complete historical filing/document inventory, shadow reconciliation, and golden-corpus validation; and
4. build and validate the definitive Stage 6C PIT historical feature panel before signal diagnostics.

The Stage 4 terminology review checks whether the discovery census is directionally useful; it does not attempt numeric specialized-metric extraction. Full parser validation waits for Stage 6B because the parser needs the final metric applicability policy and the stable Stage 6A downstream contract. The definitive historical-readiness audit runs in Stage 6C, after Stage 6B fixes the exact feature inventory.

No Stage 6B parser result may repair, replace, or bypass a failed Stage 4 or Stage 5 foundation gate.

## Specialized Metrics

Stage 0 registers candidate metrics and applicability. Stage 6B decides which candidates are technically usable after the disclosure census and parser validation. Diagnostics and Stage 8 decide whether technically usable metrics have enough evidence for nonzero scoring weight.

The candidate registry is `consumer_defensive/data/consumer_defensive_specialized_metric_registry.yaml`.
