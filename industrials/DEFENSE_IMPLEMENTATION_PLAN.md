# Industrials Defense Implementation Plan

This document defines the staged implementation plan for an industrials-sector scoring and ranking system, starting with the defense subsector. The architecture should mirror the existing `technology` implementation: shared sector infrastructure under `industrials/core`, thin subsector-specific wrappers under `industrials/defense`, and one shared industrials database that can support future industrial subsectors without duplicate ingestion.

## Architecture

Use one shared package and database for the full industrials sector:

```text
industrials/
  config.yaml
  README.md
  STAGE_GATES.md
  industrials.sqlite
  core/
    config.py
    db.py
    universe_loader.py
    universe_validator.py
    source_registry.py
    scoring_features.py
    calibrated_scoring.py
    signal_diagnostics.py
    text_norm.py
  scripts/
    00_init_industrials_db.py
    03_sync_industrials_yahoo_adjusted_prices.py
    04_audit_industrials_market_data_policy.py
    05_build_industrials_market_features.py
    06_validate_industrials_market_stage.py
    07_sync_industrials_sec_fundamentals.py
    08_build_industrials_financial_features.py
    09_import_industrials_positioning.py
    11_sync_industrials_yahoo_fx_rates.py
    13_sync_industrials_positioning_upstream.py
    14_validate_industrials_sec_positioning_stages.py
  defense/
    system_csvs/
      defense_tickers.csv
      defense_historical_membership.csv
      aerospace_defense_delisted.csv
      defense_ticker_aliases.csv
      defense_cik_ticker_overrides.csv
    data/
      defense_universe_policy.yaml
      defense_cohorts.yaml
      defense_signal_registry.yaml
      defense_output_column_map.yaml
    scripts/
      01_load_defense_universe.py
      01b_load_defense_historical_membership.py
      01c_load_defense_ticker_aliases.py
      02_validate_defense_universe.py
      02b_validate_defense_identity_reconciliation.py
      06a_build_defense_scoring_features.py
      06a_validate_defense_scoring_features.py
      08_build_defense_oos_calibration_panel.py
      08_validate_defense_oos_calibration_artifacts.py
      10_build_defense_calibrated_scores.py
      10_validate_defense_calibrated_scores.py
      10b_publish_defense_dashboard_reports.py
      10b_validate_defense_dashboard_reports.py
      15_import_defense_norgate_delisted_prices.py
      16_publish_defense_lockbox_ledger.py
      17_run_defense_refresh_pipeline.py
      PL_validate_defense_portfolio_handoff.py
```

The shared database should be `industrials.sqlite`, not one database per subsector. All shared fact and feature tables should include `model_family`, with `defense` as the initial value. This lets future subsectors reuse market data, SEC data, positioning data, source registry metadata, and scoring contracts while keeping subsector-specific logic isolated.

Pipeline CSVs used as inputs or policy seeds should live under:

```text
industrials/defense/system_csvs/
```

The enriched `ticker_mapping/defense_tickers.csv` is the source used to create the initial defense system CSV. After that copy is validated, the pipeline should treat `industrials/defense/system_csvs/defense_tickers.csv` as the source-of-truth universe. This keeps pipeline inputs separate from generated output files.

The delisted-ticker calibration seed is `ticker_mapping/aerospace_defense_delisted.csv`. After copying, the pipeline should treat `industrials/defense/system_csvs/aerospace_defense_delisted.csv` as the source-of-truth delisted calibration universe.

Current known universe profile after enrichment:

- 94 total tickers in `industrials/defense/system_csvs/defense_tickers.csv`.
- 94 investable tickers.
- No duplicate tickers.
- No blank fields in the current system CSV.
- Four current defense calibration cohorts:
  - `aerospace_components_materials_and_mro`
  - `primes_diversified_and_govtech_services`
  - `space_satellite_and_advanced_air_mobility`
  - `emerging_defense_tech_drones_munitions_speculative`
- 40 delisted calibration rows in `industrials/defense/system_csvs/aerospace_defense_delisted.csv`.

## Artifact And CSV Policy

Goal: keep required system files separate from generated output and avoid uncontrolled CSV proliferation.

Rules:

- Pipeline input CSVs and manually maintained seed CSVs belong in `industrials/defense/system_csvs`.
- Generated dashboard output belongs in `output/industrials/defense/dashboard`.
- Stage scripts should write detailed intermediate diagnostics to SQLite tables and `data_quality_issues` by default, not dozens of ad hoc CSVs.
- CSV outputs should be limited to stable, intentional deliverables: the final rank table, explicit review queues when required, and research artifacts only when a research stage is intentionally run.
- Debug CSVs should require an explicit CLI flag such as `--write-debug-csvs` and should go under a clearly marked cache/debug folder, not the dashboard folder.
- Dashboard publishing should produce one required CSV contract: `defense_final_rank_table.csv`.

## True OOS Calibration File Standards

Goal: every generated file that can influence calibration, model promotion, walk-forward research, backtests, or portfolio-layer point-in-time score history must be valid for true out-of-sample analysis.

Rules:

- Calibration-relevant files must be point-in-time, immutable, dated, hash-manifested, and reproducible from recorded inputs.
- No generated calibration file may include source data unavailable as of the file's `asof_date`. Enforce source-specific availability dates such as `source_available_date`, `filing_accepted_date`, `publication_date`, `alias_effective_date`, universe `membership_start_date` and `membership_end_date`, and Norgate market-date coverage.
- Feature birthdates are required. A feature that did not exist or was not available at an historical `asof_date` must be emitted as null/not-loaded with low confidence, not backfilled as zero or a current-era neutral.
- The active, historical, and delisted defense universe must be represented through point-in-time membership before any artifact can be used for OOS calibration or promotion.
- Norgate delisted price coverage is required before calibration artifacts can be considered promotable; missing delisted coverage must be recorded as a blocking gate or an explicit no-data exception.
- Train, validation, holdout, and walk-forward split definitions must be stored with split dates, embargo windows, random seed, config hash, git commit, source table hashes, source DB path, and benchmark definition.
- Research artifacts must record schema version, scoring contract version, score model version, row count, as-of date range, source snapshot hashes, generated timestamp, generator script, and generator command.
- Same `asof_date`, model version, scoring contract version, config hash, and source hash combination must be write-once. A replacement requires an explicit replacement reason and a new manifest entry.
- Manual edits to generated CSVs are not allowed. Corrections must be made through system CSVs, source tables, manual override tables, or approved config changes, then regenerated.
- Generated files must load with stable dtypes and match the units contract: scores `0..100`, confidence/quality `0..1`, financial ratios and returns as decimals, dollar fields in USD, flags as `0`/`1`, and dates as ISO `YYYY-MM-DD`.
- `final_score` in any portfolio-facing defense rank table remains a native `0..100` score. It is not an alpha decimal; annualized-alpha calibration belongs downstream in the portfolio-layer calibration block.

Acceptance gates:

- `08_validate_defense_oos_calibration_artifacts.py` fails any calibration artifact with future-dated source data, missing source hashes, missing split definitions, unstable dtypes, schema drift, or source rows whose availability date is after `asof_date`.
- The validator fails if the output folder market date does not match the row-level `asof_date` for snapshot artifacts.
- The validator fails if row counts or hashes change for an already sealed snapshot without an explicit replacement manifest.
- The validator fails promotable calibration artifacts when historical/delisted membership or Norgate delisted prices are missing below the configured coverage threshold.
- The validator reports feature-birthdate coverage and blocks promotion when unavailable historical features are silently backfilled.

## Orchestration And Script Numbering

Goal: make the logical stage order explicit even though script prefixes intentionally follow the technology-pipeline convention.

The `00_`, `01_`, `06a_`, `10_`, `15_`, `16_`, and `17_` script prefixes are operational run-order conventions inherited from the technology implementation. They do not always match the logical stage numbers in this document. The logical stages describe acceptance gates and architecture; the script prefixes describe concrete command ordering and compatibility with existing pipeline habits.

The defense orchestrator should be `industrials/defense/scripts/17_run_defense_refresh_pipeline.py`. It should run the production path in this order:

1. `industrials/scripts/00_init_industrials_db.py`
2. `industrials/defense/scripts/01_load_defense_universe.py`
3. `industrials/defense/scripts/01b_load_defense_historical_membership.py`
4. `industrials/defense/scripts/01c_load_defense_ticker_aliases.py`
5. `industrials/defense/scripts/02_validate_defense_universe.py`
6. `industrials/defense/scripts/02b_validate_defense_identity_reconciliation.py`
7. `industrials/scripts/03_sync_industrials_yahoo_adjusted_prices.py`
8. `industrials/defense/scripts/15_import_defense_norgate_delisted_prices.py`
9. `industrials/scripts/04_audit_industrials_market_data_policy.py`
10. `industrials/scripts/05_build_industrials_market_features.py`
11. `industrials/scripts/06_validate_industrials_market_stage.py`
12. `industrials/scripts/07_sync_industrials_sec_fundamentals.py`
13. `industrials/scripts/11_sync_industrials_yahoo_fx_rates.py`
14. `industrials/scripts/08_build_industrials_financial_features.py`
15. Direct SEC ownership sync (planned; source `sec_ownership_direct` is registered but the loader is not yet implemented)
16. `industrials/scripts/13_sync_industrials_positioning_upstream.py`
17. `industrials/scripts/09_import_industrials_positioning.py`
18. `industrials/scripts/14_validate_industrials_sec_positioning_stages.py`
19. `industrials/defense/scripts/06a_build_defense_scoring_features.py`
20. `industrials/defense/scripts/06a_validate_defense_scoring_features.py`
21. Stage 6B defense overlay smoke/build/apply/validate scripts, once implemented.
22. `industrials/defense/scripts/10_build_defense_calibrated_scores.py`
23. `industrials/defense/scripts/10_validate_defense_calibrated_scores.py`
24. Optional research/backtest scripts for Stages 8 and 9.
25. `industrials/defense/scripts/08_validate_defense_oos_calibration_artifacts.py` for any generated calibration or walk-forward artifact that may be promoted.
26. `industrials/defense/scripts/10b_publish_defense_dashboard_reports.py`
27. `industrials/defense/scripts/10b_validate_defense_dashboard_reports.py`
28. `industrials/defense/scripts/PL_validate_defense_portfolio_handoff.py`
29. `industrials/defense/scripts/16_publish_defense_lockbox_ledger.py`

Acceptance gates:

- The orchestrator exposes skip flags for expensive or research-only steps.
- A dry-run/list mode prints the exact commands and resolved config paths without mutating state.
- The script order above is encoded in the orchestrator and covered by a test so future stages do not silently reorder dependencies.

## Stage 0 - Governance And Boundaries

Goal: create an independent `industrials` package with clear write boundaries.

Implementation:

- Add `industrials/__init__.py`, `industrials/config.yaml`, `industrials/README.md`, and `industrials/STAGE_GATES.md`.
- Add `industrials/core/config.py`, `text_norm.py`, `logging_utils.py`, `source_registry.py`, and `db.py`.
- Use environment-aware defaults:
  - Database: `${INDUSTRIALS_DB_DIR:-C:/Users/josel/Documents/STAGING/DB}/industrials.sqlite`
  - Reports: `../output/industrials`
  - Cache: `../output/industrials_cache`
- Keep upstream databases read-only except for explicitly approved upstream sync scripts.

Acceptance gates:

- `industrials` imports without importing `technology`, `biotech_index`, or `med_devices`.
- Config resolves database, output, cache, and source-registry paths.
- Industrials adapters do not write to `sec_insider.sqlite`, `market_positioning.sqlite`, biotech, med-device, or technology databases.
- External data products are read-only inputs to industrials adapters.

Tests:

- Config path-resolution unit test.
- Scratch DB smoke test with `--db C:\tmp\industrials.sqlite`.
- Static import scan that blocks cross-package script imports.
- Write-boundary test for read-only upstream connections.

## Stage 1 - Database Foundation

Goal: create the shared industrials SQLite foundation.

Required tables:

- `runs`
- `source_registry`
- `ingestion_runs`
- `raw_api_responses`
- `dim_company`
- `dim_security`
- `dim_identifier`
- `dim_company_alias`
- `dim_ticker_alias`
- `fact_corporate_action`
- `dim_industrials_taxonomy`
- `dim_universe_membership`
- `data_quality_issues`

Implementation:

- Create `industrials/scripts/00_init_industrials_db.py`.
- Port the technology schema style, but rename technology-specific taxonomy tables to industrials-specific names.
- Keep source registry fields broad enough for market data, SEC, positioning, government contracts, defense budgets, and manual overlays.
- Include `model_family` on taxonomy, membership, feature, and score tables.
- Add a ticker-alias/corporate-action layer for active ticker migrations, when-issued to regular-way transitions, spin-offs, renamed share classes, and delisted successor/predecessor lineage.

Acceptance gates:

- Init script creates a clean SQLite database.
- Required tables exist after init.
- Source registry loads with no duplicate `source_id`.
- Re-running init is idempotent.
- Active ticker aliases can be represented with predecessor ticker, active ticker, effective date, issuer id, reason, and optional price-lineage CSV.
- Technology-, biotech-, and med-device-specific tables are not required by the industrials pipeline.

Tests:

- Schema existence test.
- Idempotent init test.
- Source registry duplicate-key test.
- Scratch database smoke test.

## Stage 2 - Defense Security Master And Universe

Goal: load `industrials/defense/system_csvs/defense_tickers.csv` as the initial industrials universe.

Implementation:

- Create shared `industrials/core/universe_loader.py` and `industrials/core/universe_validator.py`.
- Create defense wrapper scripts:
  - `industrials/defense/scripts/01_load_defense_universe.py`
  - `industrials/defense/scripts/02_validate_defense_universe.py`
- Create defense policy files:
  - `industrials/defense/data/defense_universe_policy.yaml`
  - `industrials/defense/data/defense_cohorts.yaml`
- Create and maintain pipeline CSV inputs under:
  - `industrials/defense/system_csvs/defense_tickers.csv`
  - `industrials/defense/system_csvs/defense_historical_membership.csv`
  - `industrials/defense/system_csvs/aerospace_defense_delisted.csv`
- Seed `dim_company`, `dim_security`, `dim_identifier`, `dim_industrials_taxonomy`, and `dim_universe_membership`.
- Load all source tickers, including review and non-investable securities, but only rank-ready securities should proceed to scoring.

Current defense calibration cohorts:

- `aerospace_components_materials_and_mro`
- `primes_diversified_and_govtech_services`
- `space_satellite_and_advanced_air_mobility`
- `emerging_defense_tech_drones_munitions_speculative`

Acceptance gates:

- Source-of-truth CSV contains exactly 94 unique tickers.
- Duplicate tickers fail validation.
- Required identity fields are present or explicitly waived in policy.
- Missing CIK rows create data-quality issues and are not silently dropped.
- Every ticker has one of the four approved defense calibration cohorts.
- Current membership rows exist for all current source tickers.

Tests:

- `01_load_defense_universe.py --db C:\tmp\industrials.sqlite`
- `02_validate_defense_universe.py --db C:\tmp\industrials.sqlite`
- CSV count test.
- Duplicate ticker test.
- Required-field policy test.
- Cohort coverage test.
- Missing-CIK issue test.

## Stage 2B - Historical And Delisted Membership

Goal: load historical and delisted defense membership before calibration work starts.

Timing:

- Current-universe loading comes first in Stage 2.
- Historical/delisted membership should be loaded immediately after Stage 2 validation and before any Stage 8 signal diagnostics, Optuna calibration, or portfolio backtest.
- Norgate historical prices for delisted tickers should be imported after the market-data schema exists in Stage 3, then enforced as a Stage 8 prerequisite.

Implementation:

- Maintain `industrials/defense/system_csvs/defense_historical_membership.csv` for historical membership intervals.
- Maintain `industrials/defense/system_csvs/aerospace_defense_delisted.csv` as the delisted aerospace/defense calibration seed.
- Expected delisted seed columns are `ticker`, `company`, `cohort`, `exit_type`, `terminal_type`, `acquirer`, `exit_year`, `cik`, and `confidence`.
- The Norgate import script should resolve historical price symbols from the delisted seed's `ticker` and `cik`, then write a resolution/coverage status into the database or a controlled audit artifact.
- Create `industrials/defense/scripts/01b_load_defense_historical_membership.py`.
- Create `industrials/defense/scripts/15_import_defense_norgate_delisted_prices.py`.
- Seed `dim_universe_membership` with point-in-time intervals for historical and delisted tickers.
- Import Norgate total-return or adjusted historical prices into the same canonical price tables used by current Yahoo prices, with source priority preserved.

Acceptance gates:

- Historical membership CSVs live only under `industrials/defense/system_csvs`.
- Every delisted ticker has a cohort, exit type, terminal type, exit year, CIK where available, and confidence flag.
- Every delisted ticker is either resolved to Norgate historical prices or receives an explicit no-data/unresolved reason during the Norgate import stage.
- Successor/predecessor lineage is explicit so duplicate histories are not double-counted.
- Norgate price rows are tagged with `source_id=norgate_us_equities_total_return` or a defense-specific equivalent registered in `source_registry`.
- Calibration and backtest stages fail if historical membership is configured but Norgate delisted price coverage is below the configured threshold.
- Current scoring can run without delisted tickers, but research calibration cannot be promoted without the delisted-history gate.

Tests:

- Historical membership load test.
- Delisted ticker uniqueness and lineage test.
- Norgate price import smoke test.
- Norgate price coverage validator.
- Split-adjustment invariant for Norgate historical prices.

## Stage 2C - Active Ticker Aliases And Corporate Actions

Goal: handle active ticker migrations, spin-offs, when-issued symbols, renamed share classes, and same-issuer ticker changes before market data, scoring, and portfolio handoff.

Why this is separate from delistings:

- Stage 2B covers historical/delisted calibration membership and Norgate price history.
- Stage 2C covers live or active-name corporate actions where a contract ticker needs to map to a current market-data symbol while preserving issuer lineage.
- These events affect daily current scoring and portfolio-layer market-data fetches, not just historical calibration panels.

Implementation:

- Maintain `industrials/defense/system_csvs/defense_ticker_aliases.csv`.
- Create an alias loader/validator, either as part of `01_load_defense_universe.py` or as `01c_load_defense_ticker_aliases.py`.
- Store aliases in `dim_ticker_alias` and related corporate-action rows in `fact_corporate_action`.
- Export or sync the active alias map into the portfolio-layer `risk_panel.ticker_aliases` contract.
- Support the portfolio-layer alias fields:
  - `contract_ticker`
  - `active_ticker`
  - `predecessor_ticker`
  - `effective_date`
  - `price_history_csv`
  - `issuer_id`
  - `reason`
- Treat active ticker migrations as a reusable corporate-action pattern, but do not add synthetic segment bridges for one-off tickers without standalone public fundamentals. The `HONAV`/`HONA` case was removed from the defense universe to avoid adding parent-segment special handling for a single name.

Acceptance gates:

- Alias effective dates are ISO dates and cannot be blank.
- A predecessor and active ticker for the same issuer cannot both pass `rank_ready_flag=1` in the same as-of snapshot unless explicitly configured as separate securities.
- The final rank table is canonicalized to the contract ticker expected by the downstream sleeve, while market-data fetches can use the active ticker.
- Portfolio-layer alias export matches `risk_panel.ticker_aliases` semantics: active symbol first after effective date, predecessor history before effective date.
- Alias changes create data-quality/corporate-action audit rows.

Tests:

- Alias CSV schema test.
- Effective-date alias routing test using a fixture ticker before and after its migration date.
- Duplicate active/predecessor rank-ready rejection test.
- Portfolio alias export validation.
- Market-data symbol routing test.

## Stage 2D - CIK/Ticker Identity Reconciliation

Goal: verify that every loaded active and delisted ticker maps to the correct CIK and issuer before market data, SEC fundamentals, positioning, scoring, or portfolio handoff use the rows.

Implementation:

- Create `industrials/defense/scripts/02b_validate_defense_identity_reconciliation.py`.
- Maintain `industrials/defense/system_csvs/defense_cik_ticker_overrides.csv` for rare manual fixes and documented exceptions.
- Normalize all CIKs to 10-digit strings in the database while preserving the raw CSV value for audit.
- Reconcile active rows from `defense_tickers.csv` against authoritative identity sources:
  - SEC `company_tickers_exchange.json` for ticker, CIK, company name, and exchange.
  - SEC `company_tickers.json` as a fallback.
  - EDGAR browse/company search only when the bulk files cannot resolve the ticker.
  - Nasdaq Trader or exchange directory data for active listing status and ticker spelling.
- Reconcile delisted rows from `aerospace_defense_delisted.csv` against:
  - SEC company/ticker bulk files where the historical ticker is still present.
  - EDGAR company search by CIK and company name.
  - Norgate symbol resolution during Stage 3 import.
  - Manual override rows when historical ticker/CIK mapping has changed or is absent from current SEC files.
- Treat same-issuer share classes and dual listings as explicit allowed cases, not false conflicts. Examples include class-share rows such as `HEI`/`HEI-A` or `MOG-A`/`MOG-B` if they share one CIK and issuer.
- Treat active ticker aliases from Stage 2C as part of the identity graph. The validator should check both predecessor and active symbols against the same issuer/CIK when a same-issuer ticker migration is configured.
- Store reconciliation results in the database as audit rows, not as routine CSV sprawl. A controlled CSV export may be written only on failure or when `--write-audit-csv` is explicitly passed.

Acceptance gates:

- Every active ticker has a verified ticker/CIK/company mapping or an explicit approved override.
- Every delisted calibration ticker has a verified ticker/CIK/company mapping, a verified Norgate symbol mapping, or an explicit unresolved/no-data reason before calibration uses it.
- No active ticker is loaded with a CIK that resolves to a materially different issuer name unless an override documents the reason.
- No CIK maps to multiple unrelated active defense issuers.
- Shared CIKs are allowed only for documented share classes, ADR/ordinary pairs, or same-issuer aliases.
- Alias predecessor and active ticker rows resolve to the same issuer when the alias reason is same-issuer migration.
- Identity conflicts block Stage 3+ pipeline execution for affected tickers unless they are explicitly quarantined from rank-ready and calibration eligibility.

Tests:

- Active CIK/ticker reconciliation test.
- Delisted CIK/ticker reconciliation test.
- CIK normalization test.
- Company-name fuzzy-match threshold test.
- Same-issuer share-class allowlist test.
- Alias predecessor/active same-issuer test.
- Manual override provenance test.
- Identity-conflict quarantine test.

## Stage 3 - Market Data And Corporate Actions

Goal: load adjusted prices, corporate actions, liquidity, benchmarks, and current market technical features.

Implementation:

- Create shared industrials market ingestion scripts, modeled after technology:
  - `03_sync_industrials_yahoo_adjusted_prices.py`
  - `04_audit_industrials_market_data_policy.py`
  - `05_build_industrials_market_features.py`
  - `06_validate_industrials_market_stage.py`
- Add defense-specific Norgate delisted import after the canonical price tables exist:
  - `industrials/defense/scripts/15_import_defense_norgate_delisted_prices.py`
- Store adjusted OHLCV, dividends, splits, market snapshots, and benchmark prices.
- Store current Yahoo prices and Norgate delisted prices in the same canonical market-data tables with explicit `source_id`.
- Use defense and broad-market benchmarks:
  - `ITA`
  - `PPA`
  - `XAR`
  - `XLI`
  - `SPY`
  - Optional: `IWM`

Acceptance gates:

- Adjusted OHLCV exists for active rank-eligible tickers and benchmarks.
- Split and dividend adjustment status is explicit.
- Low-history and low-liquidity names are flagged as review conditions.
- One current market technical feature row exists for every active defense ticker with valid price history.
- Missing benchmark data fails validation.
- Norgate delisted prices are not required for daily current scoring, but they are required before Stage 8 calibration/backtest promotion.

Tests:

- Yahoo adjusted-price sync smoke test.
- Norgate delisted-price import smoke test.
- Corporate-action adjustment audit.
- Market feature row-count test.
- Benchmark coverage test.
- Low-history/low-liquidity review flag test.

## Stage 4 - SEC Financial Statements And FX

Goal: load point-in-time SEC filing and financial facts into industrials-owned tables.

Implementation:

- Port the technology SEC financial ingestion pattern into `industrials/core`.
- Store raw XBRL facts before canonical mapping.
- Support both `us-gaap` and `ifrs-full`.
- Track accepted filing time, period end, form type, accession number, taxonomy, currency, and source detail.
- Add `fact_fx_rate` and convert non-USD statements before valuation ratios.
- Build financial features such as margins, FCF, leverage, inventory, working capital, capex, R&D, SBC, dilution, EV, market cap, and valuation ratios.

Defense-specific financial extensions:

- Backlog and funded backlog where disclosed.
- Book-to-bill where calculable.
- Contract liabilities and remaining performance obligations.
- Inventory and working-capital stress.
- Program concentration or customer concentration where available.
- Capex and R&D intensity.

Foreign-issuer fallback:

- Classify foreign issuers into explicit reporting profiles:
  - `SEC_XBRL_US_GAAP`
  - `SEC_XBRL_IFRS`
  - `SEC_20F_METADATA_ONLY`
  - `FOREIGN_VENDOR_FUNDAMENTALS`
  - `FOREIGN_NEUTRAL_LOW_CONFIDENCE`
  - `NO_FINANCIALS_REVIEW`
- For names without usable SEC XBRL, such as foreign issuers that file 20-F metadata only or do not provide US XBRL, use a configured vendor-fundamental fallback when available.
- If no vendor fallback is available, emit neutral fundamental component values with low financial confidence and explicit review reasons, rather than silently dropping the ticker from the universe.
- Rank eligibility should distinguish between "missing because unavailable for issuer type" and "missing because ingestion failed."

Acceptance gates:

- Filing availability is based on accepted filing time.
- Raw XBRL facts are stored before canonical mapping.
- US GAAP and IFRS concepts map into canonical metrics.
- SEC lag and missing concepts are tracked in `data_quality_issues`.
- Non-USD statements use FX before USD valuation ratios are populated.
- TTM facts are reproducible without look-ahead.
- Foreign and non-SEC issuers are classified, not treated as generic failures.
- Foreign issuers without usable SEC XBRL have either vendor-derived financial features or neutral-low-confidence financial features with explicit review reasons.
- Ingestion failures and structural unavailability are reported as different issue types.

Tests:

- SEC sync on a small known ticker subset.
- Canonical metric coverage report.
- Filing-date point-in-time test.
- FX conversion invariant.
- Missing-concept issue test.
- Financial feature row-count test.
- Foreign-issuer fallback classification test.
- Neutral-low-confidence fallback test.

## Stage 5 - Ownership, Insider, And Positioning

Goal: sync direct SEC ownership filings, import read-only upstream positioning data, and build positioning features.

Implementation:

- Add industrials-owned tables for Forms 3/4/5 filings, transactions, holdings, and insider reporting profiles.
- Read upstream `sec_insider.sqlite` and `market_positioning.sqlite` only through adapters.
- Import and normalize:
  - Form 4 transactions.
  - 13F institutional ownership.
  - FINRA short interest.
  - IBKR borrow where available.
- Build `feature_positioning` for defense tickers.

Acceptance gates:

- Direct SEC ownership ingestion writes only to `industrials.sqlite`.
- Upstream positioning databases are read-only.
- FINRA short interest, 13F, and borrow coverage are required for rank-eligible US common stocks.
- ADRs, foreign ordinary shares, OTC names, and new listings have explicit coverage exceptions.
- Missing coverage creates data-quality issues.
- One current positioning feature row exists for every rank-eligible ticker.

Tests:

- Read-only adapter test.
- Direct Form 4 ingestion smoke test.
- Positioning coverage validator.
- Known exception test for foreign/OTC names.
- Current feature row-count test.

## Stage 6A - Defense Scoring Feature Contract

Goal: create the defense scoring input contract from shared industrials market, financial, and positioning features.

Core components:

- `quality`
- `growth`
- `valuation`
- `market_behavior`
- `positioning`
- `risk_control`

Reserved defense overlay components:

- `defense_budget_cycle`
- `contract_backlog`
- `program_execution_risk`
- `geopolitical_demand`
- `space_autonomy`
- `export_geography_risk`

Development-stage classification:

- Add `development_stage` to the scoring input contract. Suggested values:
  - `mature_operating`
  - `revenue_scaling`
  - `development_stage`
  - `pre_revenue`
  - `unknown_review`
- The `emerging_defense_tech_drones_munitions_speculative` cohort and part of `space_satellite_and_advanced_air_mobility` should be screened for pre-revenue/development-stage status.
- Do not let null or nonsensical margin, FCF, EV/gross-profit, or EV/operating-income features create false rank-ready rows.
- Development-stage names should use a cohort-aware eligibility profile. Mature-company quality and valuation components should be neutral/low-confidence unless valid fundamentals exist; market behavior, liquidity, risk control, cash runway, positioning, contract awards, and milestone/launch evidence may be used where available.
- This is analogous to biotech clinical-stage handling: score the name on the evidence that exists for its lifecycle stage, and make the lifecycle-stage gate explicit in `review_reason` and `model_status`.

Implementation:

- Create shared `industrials/core/scoring_features.py`.
- Create defense wrappers:
  - `industrials/defense/scripts/06a_build_defense_scoring_features.py`
  - `industrials/defense/scripts/06a_validate_defense_scoring_features.py`
- Populate:
  - `dim_scoring_component`
  - `feature_scoring_input`
  - `feature_scoring_component`
- Reserved overlay components should be neutral and explicitly marked `not_loaded` until Stage 6B.
- Maintain an explicit internal-component to final-output-pillar mapping in `industrials/defense/data/defense_output_column_map.yaml`.

Component-to-output mapping:

| Internal component | Final-rank table pillar |
| --- | --- |
| `valuation` | `valuation_{score,quality,status}` |
| `quality` | `quality_{score,quality,status}` |
| `risk_control` | `risk_control_{score,quality,status}` |
| `positioning` | `positioning_{score,status}` |
| `market_behavior` | `market_behavior_{score,quality,status}` |
| `growth` | `growth_{score,quality,status}` |
| `defense_budget_cycle` | `sector_cycle_{score,quality,status}` |
| `contract_backlog` plus validated budget/backlog demand blend | `defense_budget_backlog_{score,quality,status}` |
| `program_execution_risk`, `geopolitical_demand`, `space_autonomy`, `export_geography_risk` | Contribute to `sector_overlay_score` until separately promoted into output columns |

Acceptance gates:

- Stage 6A scripts live under `industrials/defense/scripts`.
- No scoring code imports `technology`, `biotech_index`, or `med_devices`.
- One scoring input row exists for every active defense ticker.
- One component row exists for every active defense ticker and every configured component.
- Core components are built from industrials-owned features.
- Reserved overlays are neutral until populated.
- Non-exempt mature operating tickers pass the mature-company core-data-quality gate.
- Development-stage tickers pass only the configured lifecycle-stage gate and cannot become rank-ready from invalid/null mature-fundamental ratios.
- Output pillar mapping is deterministic and covered by tests.

Tests:

- Build scoring features on scratch DB.
- Validate scoring features.
- Component completeness test.
- Rank-ready quality-gate test.
- Development-stage eligibility test.
- Invalid mature-fundamental ratio exclusion test.
- Output pillar mapping test.
- Unknown component failure test.
- Cross-package import scan.

## Stage 6B - Defense Sector Overlays

Goal: add defense-specific signals without changing the Stage 6A scoring table contract.

Candidate overlays:

- Defense budget and procurement cycle.
- Company backlog, funded backlog, and book-to-bill.
- Government contract award momentum.
- NATO and allied defense spending exposure.
- Space, drones, autonomy, munitions, shipbuilding, and missile-defense exposure.
- Export, geography, program concentration, and customer concentration risk.

Named source candidates:

- USAspending.gov award data for federal award momentum and agency/customer exposure.
- SAM.gov contract opportunity and award notices where available.
- Department of Defense Comptroller budget materials for procurement/RDT&E budget trends.
- U.S. Federal Procurement Data System extracts if available through approved access.
- NATO defense expenditure releases for allied spending trends.
- SIPRI military expenditure data for global defense budget context.
- Company SEC filings, earnings releases, and investor presentations for backlog, funded backlog, book-to-bill, and program concentration.
- FAA/launch/regulatory and company milestone disclosures for space and advanced air mobility names where relevant.

Implementation:

- Add a non-mutating source smoke test before loaders mutate the DB.
- Register every source with owner, refresh cadence, authentication status, license/API notes, and schema.
- Keep source-specific parsing separate from common scoring application.
- Apply overlays into `feature_scoring_component` without changing the Stage 6A contract.
- Stage new overlay sources as `shadow_neutral` until signal diagnostics show they improve IC, hit rate, or risk-adjusted portfolio behavior. Shadow overlays can be reported but should not alter `final_score` until promoted.

Acceptance gates:

- Smoke test writes a source report and does not update `industrials.sqlite`.
- Each overlay has source registry metadata.
- Each overlay source is classified as free/API-key/paid/manual before loaders are promoted.
- Missing overlay data remains neutral, not zero.
- Overlay component quality is explicit.
- Applying overlays does not alter core component scores.
- Stage 6A validation still passes after overlay application.
- Shadow overlays do not affect `sector_overlay_score` or `final_score` until explicitly promoted.

Tests:

- Non-mutating source smoke test.
- Overlay source coverage test.
- Source classification test.
- Shadow-neutral overlay test.
- Overlay component row-count test.
- Component-quality threshold test.
- Regression test that core scores remain unchanged.

## Stage 6C - Shadow Rank Table And PIT Snapshot History

Goal: publish contract-compatible defense rank tables for PIT data-contract validation while keeping portfolio and OOS gates disabled.

Implemented scripts:

- `16_run_defense_daily_refresh.py`: one-command daily fast path for a specific market `--asof` date. It runs incremental SEC sync, daily positioning refresh, Stage 3-6 validators, shadow publish, rank-table validation, and portfolio `tech_family` adapter shadow validation.
- `17_publish_defense_shadow_rank_table.py`: publishes `output/industrials/defense/dashboard/YYYY-MM-DD/defense_final_rank_table.csv` and `defense_final_rank_table_manifest.json`.
- `18_validate_defense_shadow_rank_table.py`: validates the final-rank-table schema, row count, manifest hash, 0-100 native score bounds, shadow gate pins, and neutralized defense overlay pillars.
- `19_build_defense_shadow_snapshot_history.py`: builds immutable shadow snapshots only for dates with sufficient loaded Stage 3 market, Stage 4 financial, and Stage 5 positioning coverage.
- `20_validate_defense_portfolio_adapter_shadow.py`: dry-runs the portfolio `tech_family` adapter without registering defense in `portfolio_layer/config.yaml`.

Policy:

- The published `final_score` is a native 0-100 defense composite score, not annualized alpha.
- Shadow snapshots set portfolio/OOS gates off: `portfolio_candidate_gate=0`, `oos_score_valid_flag=0`, `calibration_eligible_flag=0`, and `calibration_sample_role=excluded`.
- Dated dashboard artifacts are immutable by default. A valid existing artifact is kept; overwrite requires explicit `--allow-overwrite`.
- `sector_cycle_*` and `defense_budget_backlog_*` remain neutralized with status `neutralized_not_loaded` until validated overlay sources are promoted.
- Portfolio-layer registration is blocked until snapshot history and true OOS calibration validation pass.

Acceptance gates:

- The daily runner completes for the requested market date without triggering full-history refresh modes.
- Every active defense ticker has loaded market, financial, and positioning features for the published date.
- The rank-table validator passes for every dated shadow snapshot.
- The portfolio adapter shadow validator ingests the file with the existing `tech_family` adapter and produces zero investable, research-eligible, or OOS-valid rows.
- Generated snapshot-history reports live under `output/industrials/defense/stage6`.

## Stage 7 - Calibrated Defense Ranking

Goal: produce the first production defense ranking layer.

Source IDs:

- Baseline contract: `defense_scoring_contract`
- Production score: `defense_calibrated_score_v1`

Implementation:

- Create shared `industrials/core/calibrated_scoring.py`.
- Create defense wrappers:
  - `10_build_defense_calibrated_scores.py`
  - `10_validate_defense_calibrated_scores.py`
- Use config-driven component and subfeature weights.
- Write final rankings to `feature_scoring_model_output` and a CSV report.
- `final_score` must be sleeve-absolute comparable across all defense cohorts. Calibration cohorts are allowed to influence diagnostics, shrinkage, eligibility, and constraints, but the emitted `final_score` cannot be cohort-relative.
- Add a cross-cohort normalization step before emitting production scores so a prime contractor, aerospace supplier, space/SPAC name, and speculative drone/munitions name with the same `final_score` represent comparable expected evidence strength within the defense sleeve.
- Stage 7 v1 may use conservative/equal or expert-seeded provisional weights to create a first production-compatible ranking layer. Stage 8 is the report-only research loop that can promote validated weights back into a later Stage 7 model version, such as `defense_calibrated_score_v2`, after manual approval.

Acceptance gates:

- Stage 7 reads only validated Stage 6 inputs.
- Unknown component or subfeature weights fail fast.
- One latest production score exists for every rank-ready defense ticker.
- Review-only tickers have explicit reasons.
- Production scores use `source_id=defense_calibrated_score_v1`.
- Stage 7 does not overwrite baseline Stage 6 rows.
- `final_score` is not cohort-relative; cross-cohort score deciles are comparable across the full defense sleeve.
- The rank-ready set does not over-represent a small speculative cohort merely because its scores were normalized only within cohort.

Tests:

- Build calibrated scores.
- Validate calibrated scores.
- Weight-schema validation.
- Rank and percentile bounds test.
- Review-reason completeness test.
- Cross-cohort score comparability test.
- Speculative-cohort over-concentration test.

## Stage 8 - Signal Diagnostics And Calibration Research

Goal: run report-only IC diagnostics, constrained Optuna calibration, and walk-forward validation.

Implementation:

- Use `21_validate_defense_oos_calibration_readiness.py` as the current Stage 8 readiness scaffold before any OOS panel is promoted. It validates immutable shadow rank-table manifests, schema, native score units, shadow gate pins, PIT source dates, portfolio-adapter shadow ingestion, and benchmark pins.
- Create `08_build_defense_oos_calibration_panel.py` to produce point-in-time calibration panels and split metadata for research-only scoring diagnostics.
- Create `08_validate_defense_oos_calibration_artifacts.py` to enforce the True OOS Calibration File Standards.
- Build a point-in-time historical panel using `dim_universe_membership`.
- Require Norgate delisted historical prices for historical/delisted defense members before calibration outputs can be considered promotable.
- Compute forward excess and beta-hedged residual returns against a pinned sleeve benchmark.
- Use `XAR` as the primary `forward_excess_return_vs_sector` benchmark for the defense sleeve. Use `ITA` as a robustness comparator, not as an alternate production target unless the portfolio layer is updated at the same time.
- Report IC statistics by subfeature and component.
- Run constrained Optuna calibration with train/holdout split and embargo.
- Run walk-forward refit validation.
- Apply Bayesian/empirical-Bayes shrinkage for cohort-level calibration. Cohort-level slopes and weights should shrink toward the all-defense prior and, where available, an all-industrials prior. A 20-name cohort cannot independently set aggressive weights without enough evidence.
- Convert any cohort-level calibration result into a sleeve-absolute scoring scale before promotion back to Stage 7. Cohort-specific slopes may improve calibration, but they cannot make `final_score` a within-cohort percentile.
- Write OOS calibration panels, train/holdout files, walk-forward files, and research summaries only through the True OOS Calibration File Standards. Any artifact that may support promotion must carry PIT source cutoffs, feature birthdates, source hashes, split definitions, embargo settings, model/config hashes, and benchmark definition.
- Validate promotable research artifacts with `08_validate_defense_oos_calibration_artifacts.py` before they are reviewed for Stage 7 promotion.

Acceptance gates:

- `21_validate_defense_oos_calibration_readiness.py` passes in report-only mode for all available shadow snapshots.
- `21_validate_defense_oos_calibration_readiness.py --promotion-check` remains blocked until the configured minimum valid snapshot count is met.
- The calibration panel uses point-in-time universe membership.
- Current and historical/delisted defense members are included when eligible Norgate price data exists.
- Delisted ticker coverage from `industrials/defense/system_csvs/aerospace_defense_delisted.csv` meets the configured minimum coverage threshold.
- OOS calibration artifacts pass the True OOS Calibration File Standards validator before any promotion candidate can be considered.
- Train, validation, holdout, and walk-forward definitions, embargo windows, seed, config hash, git commit, source snapshot hashes, source DB path, feature birthdates, and benchmark definition are recorded.
- Generated calibration files contain no source rows with availability dates after row-level `asof_date`.
- Promotable calibration files are immutable, hash-manifested, and reproducible from recorded source snapshots.
- Duplicate lineage continuations are excluded when successor tickers already carry predecessor history.
- Candidate weights obey turnover, concentration, component, and cohort constraints.
- Cohort-specific calibration uses shrinkage toward all-defense and all-industrials priors, with shrinkage strength reported.
- Promotion candidates pass a cross-cohort calibration check: defense-wide final-score deciles should map monotonically to realized forward returns, and residual cohort bias must be reported.
- `forward_excess_return_vs_sector` uses `XAR` unless a coordinated config change updates both defense and portfolio-layer expectations.
- Promotion remains manual.
- Holdout, fold robustness, turnover, and cohort-concentration gates must pass before any production promotion.
- IC t-stats are Newey-West adjusted for overlapping forward-return windows.

Tests:

- Signal IC report.
- Signal birthdate report.
- Optuna trial feasibility report.
- Holdout validation report.
- Walk-forward out-of-sample report.
- Research-hardening validator.
- OOS calibration artifact manifest/hash validation.
- Future-date leakage validation.
- Feature-birthdate gating validation.
- Train/holdout/walk-forward split reproducibility validation.
- Shrinkage diagnostics by cohort.
- Benchmark consistency test for `XAR` versus portfolio-layer config.
- Cross-cohort monotonicity and residual cohort-bias report.

## Stage 9 - Portfolio Backtest

Goal: convert score history into report-only portfolio behavior.

Implementation:

- Backtest Stage 7 production, static review candidates, and Stage 8 report-only candidates separately.
- Use point-in-time universe membership and research price-source priority.
- Include transaction cost and borrow-cost assumptions.

Portfolio variants:

- Top decile.
- Top quintile.
- Long-short decile.
- Long-short quintile.
- Equal-weight and score-weight versions.
- Beta-hedged long-only and beta-neutral long-short versions where feasible.

Acceptance gates:

- Backtest reads the same point-in-time research panel used by calibration.
- Transaction costs, turnover, drawdown, hit rate, volatility, benchmark-relative returns, borrow cost, and cohort concentration are reported.
- Defense-specific and broad-market benchmarks are reported.
- Backtests do not write production scores.

Tests:

- Backtest summary output test.
- Period-level output test.
- Holding-level output test.
- Exposure and gross-normalization checks.
- Benchmark-relative return checks.

## Stage 10 - Dashboard And Static Reports

Goal: publish static reports for latest production defense rankings, risk flags, overlays, and backtest diagnostics.

Implementation:

- Create `10b_publish_defense_dashboard_reports.py`.
- Write the final dashboard CSV under a dated market-date folder:
  - `output/industrials/defense/dashboard/YYYY-MM-DD/defense_final_rank_table.csv`
- The folder date must match the market date represented by `asof_date`, not the wall-clock run date when those differ.
- Do not write duplicate final-rank CSVs outside the dated dashboard folder unless a separate explicit `latest` symlink/copy policy is approved.
- Treat the published final rank table as a portfolio-layer PIT score-history source. It must satisfy the True OOS Calibration File Standards when used by the portfolio layer, Stage 11, or any OOS calibration/backtest process.

Required output:

- `defense_final_rank_table.csv`

Optional non-CSV dashboard companions, such as a compact manifest JSON or static HTML page, may be added only when needed. Routine publishing should not create a large set of additional CSVs.

Snapshot immutability:

- A dated dashboard folder is an immutable market-date snapshot once published.
- Re-running the publisher for the same `asof_date`, `score_model_version`, `model_version`, and `scoring_contract_version` must either reproduce the same file hash or fail unless an explicit `--replace-snapshot` flag and replacement reason are provided.
- A compact manifest JSON is allowed to record file hash, row count, schema version, market date, model versions, source DB path, and generated timestamp.
- All rows in the final rank table must share the folder's `YYYY-MM-DD` market date as `asof_date`.

Units contract:

- Scores and component scores are on a `0..100` scale.
- `final_percentile` is on a `0..100` scale, matching the technology family output.
- Confidence and quality fields are on a `0..1` scale.
- Return, margin, yield, growth, volatility, drawdown, short-interest, borrow-rate, and ownership-delta fields are decimals, not display percentages.
- Dollar fields are USD unless explicitly named otherwise: `market_cap`, `latest_price`, `avg_dollar_volume_60d`, and `insider_net_value_90d`.
- `latest_price` is USD per share.
- Flags are `0` or `1`.
- Dates are ISO `YYYY-MM-DD`.
- `latest_sec_url` is blank or an absolute URL.

Final rank table contract:

The output must be schema-compatible with `semiconductor_final_rank_table.csv` for the cross-sector `tech_family` adapter. Keep column names byte-identical except for the one defense demand-overlay pillar replacing the semiconductor `big_tech_capex_*` triplet.

Ordered columns:

```text
ticker
asof_date
score_model_version
model_family
model_version
scoring_contract_version
company_name
sector
industry
subsector
country
currency
final_rank
final_percentile
final_score
core_score
sector_overlay_score
data_quality_confidence
rank_ready_flag
calibration_eligible_flag
model_status
review_reason
calibration_cohort_id
calibration_cohort
market_cap
latest_price
revenue_yoy_growth
gross_profit_yoy_growth
operating_income_yoy_growth
free_cash_flow_yoy_growth
revenue_acceleration
gross_margin
operating_margin
fcf_margin
fcf_to_net_income
net_cash_to_assets
sbc_pct_revenue
r_and_d_pct_revenue
share_count_yoy_growth
inventory_days
fcf_yield
ev_gross_profit
ev_operating_income
ret_3m
ret_12m_ex_1m
rel_strength_bench_3m
realized_vol_60d
max_drawdown_12m
distance_from_52w_high
avg_dollar_volume_60d
low_liquidity_flag
insider_net_value_90d
insider_cluster_buyers_90d
institutional_ownership_delta_pct
latest_short_interest_pct_float
short_interest_change_3m
latest_days_to_cover
latest_borrow_fee_rate
market_quality
financial_quality
positioning_quality
core_available_component_count
core_missing_component_count
core_data_quality_confidence
latest_sec_form
latest_sec_filing_date
latest_sec_url
valuation_score
valuation_quality
valuation_status
quality_score
quality_quality
quality_status
risk_control_score
risk_control_quality
risk_control_status
positioning_score
positioning_status
market_behavior_score
market_behavior_quality
market_behavior_status
growth_score
growth_quality
growth_status
sector_cycle_score
sector_cycle_quality
sector_cycle_status
defense_budget_backlog_score
defense_budget_backlog_quality
defense_budget_backlog_status
```

Column value rules:

- `ticker`: from `industrials/defense/system_csvs/defense_tickers.csv`.
- `asof_date`: snapshot point-in-time market date.
- `score_model_version`: provenance score version.
- `model_family`: `defense`; this is the source pipeline.
- `model_version`: model build id.
- `scoring_contract_version`: pinned scoring contract id.
- `company_name`: from the defense universe CSV.
- `sector`: `Industrials`.
- `industry`: `Aerospace & Defense`.
- `subsector`: `Defense`.
- `country`: emit the contract value `United States` for this final adapter-facing table; keep issuer domicile internally if needed for analytics.
- `currency`: `USD`.
- `final_rank`: within-sleeve rank.
- `final_percentile`: within-sector percentile.
- `final_score`: native 0-100 defense composite score. Do not emit annualized alpha or decimal expected return here; cross-sector annualized-alpha calibration happens downstream in the portfolio-layer `score_contract.sectors[].calibration` block.
- `core_score`: pre-overlay composite.
- `sector_overlay_score`: defense macro/demand overlay composite.
- `data_quality_confidence`: score confidence.
- `rank_ready_flag` and `calibration_eligible_flag`: eligibility gates.
- `model_status`: must be `complete` to be eligible.
- `review_reason`: eligibility reason.
- `calibration_cohort_id`: one of the four approved defense cohort IDs.
- `calibration_cohort`: defense calibration cohort label from the defense system CSV/cohort mapping.
- `defense_budget_backlog_*`: defense demand-cycle pillar replacing semiconductor `big_tech_capex_*`; use book-to-bill, DoD budget, backlog, or related defense-demand evidence.

Acceptance gates:

- Final rank table covers the current defense universe.
- `defense_final_rank_table.csv` is written only to `output/industrials/defense/dashboard/YYYY-MM-DD/`.
- The output column names and order exactly match the final rank table contract above.
- The output file contains no extra columns and no missing required columns.
- Units match the units contract above.
- A same-date rerun is hash-identical or requires explicit replacement.
- There is at most one row per canonical contract ticker in the final rank table.
- `final_rank` values are unique among rank-ready rows.
- `model_family` is `defense` for every row.
- `sector`, `industry`, `subsector`, and `currency` match the contract values for every row.
- `model_status=complete` is required for eligible/rank-ready rows.
- Review-only rows carry `review_reason`.
- Dashboard publishing is read-only with respect to model scores and source data.
- Portfolio-facing snapshots pass the applicable True OOS Calibration File Standards checks: market-date/asof match, immutable hash manifest, stable dtypes, source snapshot provenance, and no future-dated source availability.

Tests:

- Dashboard publish script.
- Final CSV path and market-date folder validation.
- Exact column-order/schema validation.
- Units validation.
- Snapshot immutability/hash validation.
- Portfolio PIT/OOS snapshot validation.
- Duplicate canonical ticker validation.
- No-extra-generated-CSV validation.
- Read-only publishing test.

## Stage PL - Portfolio Layer Integration

Goal: make the defense final rank table ingestible by the portfolio layer without adapter changes and keep market-data, alias, benchmark, units, and snapshot assumptions aligned.

This section is named `Stage PL` rather than `Stage 11` to avoid colliding with the portfolio layer's own Stage 11 survivorship walk-forward and lockbox OOS harness.

Inputs:

- `output/industrials/defense/dashboard/YYYY-MM-DD/defense_final_rank_table.csv`
- `industrials/defense/system_csvs/defense_ticker_aliases.csv`
- `portfolio_layer/config.yaml` `risk_panel.ticker_aliases`
- Portfolio-layer sleeve and macro/stock-overlay configs that consume `source_pipeline=model_family=defense`

Implementation:

- Add a validator such as `industrials/defense/scripts/PL_validate_defense_portfolio_handoff.py`.
- Validate that the final rank table follows the cross-sector contract expected by the technology-family adapter.
- Add a staged `score_contract.sectors` entry in `portfolio_layer/config.yaml`:
  - `model_family: defense`
  - `adapter: tech_family`
  - `enabled: true`
  - `required: false` during initial registration and dry-run validation
  - `staleness_tolerance_days: 3`
  - `sector: "Industrials"`
  - `industry: "Aerospace & Defense"`
  - `industry_aggregate: "Aerospace & Defense"`
  - `file_mode: dated`
  - `file_path: "industrials/defense/dashboard/{yyyy-mm-dd}/defense_final_rank_table.csv"`
  - `calibration: {neutral: "median", scale: 50.0, expected_alpha_at_full: 0.15}` as an initial placeholder until defense-specific calibration is validated.
- The duplicate `industry` and `industry_aggregate` value is deliberate for this sleeve. The `tech_family` adapter injects `industry_aggregate` from portfolio config, and the defense final-rank CSV intentionally omits that column.
- Registration order is mandatory:
  1. Add defense with `enabled: true` and `required: false` while `score_contract.min_successful_sectors` remains `5`.
  2. Publish a valid dated `defense_final_rank_table.csv`.
  3. Run Stage 1 collection/calibration/validation dry-runs.
  4. Flip defense to `required: true` and bump `score_contract.min_successful_sectors` from `5` to `6`.
- A required sixth sector without a published file will correctly fail Stage 1 collection, so do not flip `required: true` before the publisher is live.
- Add defense to portfolio-layer risk and rotation config:
  - `risk_panel.sector_etf_map.defense: XAR`
  - `sleeves.sector_factor_etfs.defense: XAR`
  - Include `XAR` in `risk_panel.benchmark_tickers`.
  - Include `XAR` in `risk_panel.hedge_rotation_etfs` and `rotation.rank_universe_etfs` so Stage 5 can form a defense rotation state.
- Add defense to `macro.sleeve_taxonomy`:
  - `macro_sector_fallback: "Industrials"`
  - `industries: ["Aerospace & Defense"]`
  - `industry_aggregates: ["Aerospace & Defense"]`
- Add defense to `black_litterman_fusion.strategic_sector_weights`, with a deliberate neutral weight and a rebalance of the existing five sleeve weights so the total remains 1.0.
- Add defense to any portfolio-layer factor or sleeve maps that mirror `risk_panel.sector_etf_map`.
- Validate that alias rows can be translated into the portfolio-layer `risk_panel.ticker_aliases` shape:
  - `active_ticker`
  - `predecessor_ticker`
  - `effective_date`
  - `price_history_csv`
  - `issuer_id`
  - `reason`
- Export a compact alias handoff artifact or patch-ready config snippet only when aliases exist.
- Enforce canonical ticker deduplication after alias resolution. A predecessor and active ticker for the same issuer must not both be eligible in the same defense sleeve snapshot.
- Pin the defense sleeve benchmark to `XAR` for `forward_excess_return_vs_sector`; keep `ITA` as a report-only comparator unless the portfolio config is changed at the same time.
- Validate that the final rank table market date is immutable and matches the dated output folder.
- Validate units before portfolio ingestion: scores `0..100`, confidence/quality `0..1`, ratios/returns as decimals, USD dollar fields, flags as `0/1`.
- Run a Stage 1 collection dry run after config registration:
  - `python portfolio_layer/scores/01_collect_sector_scores.py --as-of YYYY-MM-DD`
  - Follow with Stage 1 calibration/validation:
    - `python portfolio_layer/scores/02_calibrate_cross_sector_scores.py --as-of YYYY-MM-DD`
    - `python portfolio_layer/scores/03_validate_score_contract.py --as-of YYYY-MM-DD`

Cross-sleeve duplicate ownership:

- Defense introduces likely cross-sector ticker collisions. `PLTR` is already a live collision with `software_infrastructure`; `BBAI` and `AXON` are plausible future collisions.
- The owning sleeve must be explicitly pinned in `portfolio_layer/data/canonical_sector_overrides.csv` before defense is marked required.
- Recommended initial ownership decision: `PLTR -> software_infrastructure`, unless the defense model becomes the explicit owner by investment policy. The decision must be written to the override CSV either way.
- Any duplicate introduced by defense must resolve via `method=canonical_override` in `validation/duplicate_resolution.csv`; confidence/order fallback is not acceptable for known defense collisions.

Acceptance gates:

- The final rank table ingests with the existing cross-sector adapter contract and no defense-only adapter fork.
- `portfolio_layer/config.yaml` contains a defense `score_contract.sectors` entry with `adapter: tech_family`.
- Initial registration uses `required: false`; the final required-state gate uses `required: true`.
- `score_contract.min_successful_sectors` is `6` once defense is required.
- Defense appears in `risk_panel.sector_etf_map`, `sleeves.sector_factor_etfs`, `macro.sleeve_taxonomy`, and `black_litterman_fusion.strategic_sector_weights`.
- `industry_aggregate` is pinned to `Aerospace & Defense` in portfolio config; it is not emitted by the defense final-rank CSV.
- `XAR` is available to the risk panel and rotation stage as the defense sector ETF.
- Stage 1 collection dry run succeeds with six successful sectors.
- Stage 1 calibration and contract validation pass after defense registration.
- No duplicate canonical ticker rows remain after alias resolution.
- Known cross-sleeve collisions, starting with `PLTR`, are pinned in `portfolio_layer/data/canonical_sector_overrides.csv`.
- Active ticker aliases required for market-data fetching are present in the portfolio-layer alias contract or in a generated patch artifact.
- When-issued to regular-way migrations are handled with effective-date logic when included in the universe; `HONAV`/`HONA` is intentionally excluded from defense rather than bridged through parent-segment fundamentals.
- The `XAR` benchmark used by defense calibration matches the benchmark expected by the portfolio layer for defense excess-return and sleeve-risk calculations.
- Same-market-date snapshots are immutable unless an explicit replacement workflow records the reason and new hash.
- Portfolio-layer ingestion receives exactly one final rank CSV for a market date, not multiple competing CSVs.

Tests:

- Portfolio handoff schema test.
- Portfolio config registration test.
- Stage 1 `01_collect_sector_scores.py` dry-run test.
- Stage 1 `02_calibrate_cross_sector_scores.py` duplicate-resolution test.
- Stage 1 `03_validate_score_contract.py` full contract validation.
- Alias map compatibility test against `risk_panel.ticker_aliases`.
- Duplicate canonical ticker rejection test.
- Canonical override test for `PLTR`.
- Benchmark consistency test.
- Units contract test.
- Snapshot immutability test.
- End-to-end dry-run ingestion test using a dated `defense_final_rank_table.csv`.

## Stage 12 - Governance Lockbox And Signal Registry

Goal: freeze model evidence without changing source data, scores, or production weights.

Implementation:

- Maintain `industrials/defense/data/defense_signal_registry.yaml`.
- Create `16_publish_defense_lockbox_ledger.py`.
- Publish governance artifacts under `output/industrials/defense/governance`.

Acceptance gates:

- Signal registry documents every signal, source, transform, status, and production weight.
- Lockbox ledger records config hash, git commit, run date, source IDs, accepted model version, and manual promotion status.
- Governance publishing changes no source data or scores.

Tests:

- Signal registry schema validation.
- Lockbox manifest validation.
- Reproducibility metadata test.

## Recommended First Implementation Slice

Start with Stages 0 through 2:

1. Scaffold `industrials/core`.
2. Add `industrials/config.yaml`.
3. Add `industrials/scripts/00_init_industrials_db.py`.
4. Add `industrials/defense/data/defense_universe_policy.yaml`.
5. Add `industrials/defense/data/defense_cohorts.yaml`.
6. Create `industrials/defense/system_csvs/defense_tickers.csv` from the enriched `ticker_mapping/defense_tickers.csv`.
7. Add `01_load_defense_universe.py` and `02_validate_defense_universe.py`.
8. Load and validate `industrials/defense/system_csvs/defense_tickers.csv`.
9. Add `02b_validate_defense_identity_reconciliation.py` and confirm active ticker/CIK/company mappings before Stage 3.
10. Add historical membership, ticker-alias, and CIK/ticker override system CSV templates, and copy `ticker_mapping/aerospace_defense_delisted.csv` into `industrials/defense/system_csvs`, before Stage 8 and portfolio handoff work begins.
11. Add the development-stage classification fields and Stage 2C alias validation before any Stage 6/7 rank-ready gates are promoted.

This creates the stable universe contract required before market data, financials, positioning, and scoring work can be implemented safely.
