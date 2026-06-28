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
  defense/
    data/
      defense_universe_policy.yaml
      defense_cohorts.yaml
      defense_signal_registry.yaml
      defense_historical_membership.csv
    scripts/
      01_load_defense_universe.py
      02_validate_defense_universe.py
      06a_build_defense_scoring_features.py
      06a_validate_defense_scoring_features.py
      10_build_defense_calibrated_scores.py
      10_validate_defense_calibrated_scores.py
      17_run_defense_refresh_pipeline.py
```

The shared database should be `industrials.sqlite`, not one database per subsector. All shared fact and feature tables should include `model_family`, with `defense` as the initial value. This lets future subsectors reuse market data, SEC data, positioning data, source registry metadata, and scoring contracts while keeping subsector-specific logic isolated.

The defense source-of-truth universe is:

```text
ticker_mapping/defense_tickers.csv
```

Current known universe profile after enrichment:

- 172 total tickers.
- 168 investable tickers.
- 4 non-investable or review tickers: `MOBBW`, `DFNS`, `PRZO`, `KITT`.
- 56 tickers still missing CIK, mostly foreign or OTC names.
- `FTCFF` still has a blank `country` field and should be treated as identity review unless manually remediated.

## Stage 0 - Governance And Boundaries

Goal: create an independent `industrials` package with clear write boundaries.

Implementation:

- Add `industrials/__init__.py`, `industrials/config.yaml`, `industrials/README.md`, and `industrials/STAGE_GATES.md`.
- Add `industrials/core/config.py`, `text_norm.py`, `logging_utils.py`, `source_registry.py`, and `db.py`.
- Use environment-aware defaults:
  - Database: `${INDUSTRIALS_DB_DIR:-C:/Users/josel/Documents/STAGING/DB}/industrials.sqlite`
  - Reports: `../output/industrials_reports`
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
- `dim_industrials_taxonomy`
- `dim_universe_membership`
- `data_quality_issues`

Implementation:

- Create `industrials/scripts/00_init_industrials_db.py`.
- Port the technology schema style, but rename technology-specific taxonomy tables to industrials-specific names.
- Keep source registry fields broad enough for market data, SEC, positioning, government contracts, defense budgets, and manual overlays.
- Include `model_family` on taxonomy, membership, feature, and score tables.

Acceptance gates:

- Init script creates a clean SQLite database.
- Required tables exist after init.
- Source registry loads with no duplicate `source_id`.
- Re-running init is idempotent.
- Technology-, biotech-, and med-device-specific tables are not required by the industrials pipeline.

Tests:

- Schema existence test.
- Idempotent init test.
- Source registry duplicate-key test.
- Scratch database smoke test.

## Stage 2 - Defense Security Master And Universe

Goal: load `ticker_mapping/defense_tickers.csv` as the initial industrials universe.

Implementation:

- Create shared `industrials/core/universe_loader.py` and `industrials/core/universe_validator.py`.
- Create defense wrapper scripts:
  - `industrials/defense/scripts/01_load_defense_universe.py`
  - `industrials/defense/scripts/02_validate_defense_universe.py`
- Create defense policy files:
  - `industrials/defense/data/defense_universe_policy.yaml`
  - `industrials/defense/data/defense_cohorts.yaml`
- Seed `dim_company`, `dim_security`, `dim_identifier`, `dim_industrials_taxonomy`, and `dim_universe_membership`.
- Load all source tickers, including review and non-investable securities, but only rank-ready securities should proceed to scoring.

Suggested defense cohorts:

- `prime_contractors`
- `aerospace_airframes`
- `defense_electronics`
- `space_launch_satellites`
- `shipbuilding_naval`
- `missiles_munitions`
- `drones_autonomy`
- `training_services_simulation`
- `small_cap_speculative`
- `foreign_adr_otc_review`
- `non_investable_review`

Acceptance gates:

- Source-of-truth CSV contains exactly 172 unique tickers.
- Duplicate tickers fail validation.
- Required identity fields are present or explicitly waived in policy.
- Missing CIK rows create data-quality issues and are not silently dropped.
- `MOBBW`, `DFNS`, `PRZO`, and `KITT` load as review/non-investable, not rank-ready.
- `FTCFF` country gap is flagged unless manually remediated.
- Every ticker has a cohort assignment or lands in an explicit review cohort.
- Current membership rows exist for all current source tickers.

Tests:

- `01_load_defense_universe.py --db C:\tmp\industrials.sqlite`
- `02_validate_defense_universe.py --db C:\tmp\industrials.sqlite`
- CSV count test.
- Duplicate ticker test.
- Required-field policy test.
- Cohort coverage test.
- Missing-CIK issue test.

## Stage 3 - Market Data And Corporate Actions

Goal: load adjusted prices, corporate actions, liquidity, benchmarks, and current market technical features.

Implementation:

- Create shared industrials market ingestion scripts, modeled after technology:
  - `03_sync_industrials_yahoo_adjusted_prices.py`
  - `04_audit_industrials_market_data_policy.py`
  - `05_build_industrials_market_features.py`
  - `06_validate_industrials_market_stage.py`
- Store adjusted OHLCV, dividends, splits, market snapshots, and benchmark prices.
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

Tests:

- Yahoo adjusted-price sync smoke test.
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

Acceptance gates:

- Filing availability is based on accepted filing time.
- Raw XBRL facts are stored before canonical mapping.
- US GAAP and IFRS concepts map into canonical metrics.
- SEC lag and missing concepts are tracked in `data_quality_issues`.
- Non-USD statements use FX before USD valuation ratios are populated.
- TTM facts are reproducible without look-ahead.
- Foreign and non-SEC issuers are classified, not treated as generic failures.

Tests:

- SEC sync on a small known ticker subset.
- Canonical metric coverage report.
- Filing-date point-in-time test.
- FX conversion invariant.
- Missing-concept issue test.
- Financial feature row-count test.

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

Acceptance gates:

- Stage 6A scripts live under `industrials/defense/scripts`.
- No scoring code imports `technology`, `biotech_index`, or `med_devices`.
- One scoring input row exists for every active defense ticker.
- One component row exists for every active defense ticker and every configured component.
- Core components are built from industrials-owned features.
- Reserved overlays are neutral until populated.
- Non-exempt rank-ready tickers pass the core-data-quality gate.

Tests:

- Build scoring features on scratch DB.
- Validate scoring features.
- Component completeness test.
- Rank-ready quality-gate test.
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

Implementation:

- Add a non-mutating source smoke test before loaders mutate the DB.
- Register every source with owner, refresh cadence, authentication status, license/API notes, and schema.
- Keep source-specific parsing separate from common scoring application.
- Apply overlays into `feature_scoring_component` without changing the Stage 6A contract.

Acceptance gates:

- Smoke test writes a source report and does not update `industrials.sqlite`.
- Each overlay has source registry metadata.
- Missing overlay data remains neutral, not zero.
- Overlay component quality is explicit.
- Applying overlays does not alter core component scores.
- Stage 6A validation still passes after overlay application.

Tests:

- Non-mutating source smoke test.
- Overlay source coverage test.
- Overlay component row-count test.
- Component-quality threshold test.
- Regression test that core scores remain unchanged.

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

Acceptance gates:

- Stage 7 reads only validated Stage 6 inputs.
- Unknown component or subfeature weights fail fast.
- One latest production score exists for every rank-ready defense ticker.
- Review-only tickers have explicit reasons.
- Production scores use `source_id=defense_calibrated_score_v1`.
- Stage 7 does not overwrite baseline Stage 6 rows.

Tests:

- Build calibrated scores.
- Validate calibrated scores.
- Weight-schema validation.
- Rank and percentile bounds test.
- Review-reason completeness test.

## Stage 8 - Signal Diagnostics And Calibration Research

Goal: run report-only IC diagnostics, constrained Optuna calibration, and walk-forward validation.

Implementation:

- Build a point-in-time historical panel using `dim_universe_membership`.
- Compute forward beta-hedged residual returns against defense and broad-market benchmarks.
- Report IC statistics by subfeature and component.
- Run constrained Optuna calibration with train/holdout split and embargo.
- Run walk-forward refit validation.

Acceptance gates:

- The calibration panel uses point-in-time universe membership.
- Current and historical/delisted defense members are included when eligible data exists.
- Duplicate lineage continuations are excluded when successor tickers already carry predecessor history.
- Candidate weights obey turnover, concentration, component, and cohort constraints.
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
- Write latest files under `output/industrials_reports/defense/dashboard`.
- Snapshot each run under an `asof_date` subfolder.

Required outputs:

- Final rank table.
- Company scorecards.
- Cohort summary.
- Risk flags.
- Review queue.
- Overlay summary.
- Backtest summary link.
- Manifest.
- Static `index.html`.

Acceptance gates:

- Final rank table covers the current defense universe.
- Company scorecards include component scores, quality status, review reason, cohort, and source coverage.
- Review queue highlights missing CIKs, identity gaps, low liquidity, low history, and non-investable securities.
- Dashboard publishing is read-only with respect to model scores and source data.

Tests:

- Dashboard publish script.
- Manifest validation.
- Required CSV/JSON/HTML existence tests.
- Read-only publishing test.

## Stage 10B - Governance Lockbox And Signal Registry

Goal: freeze model evidence without changing source data, scores, or production weights.

Implementation:

- Maintain `industrials/defense/data/defense_signal_registry.yaml`.
- Create `16_publish_defense_lockbox_ledger.py`.
- Publish CSV and JSON governance artifacts under `output/industrials_reports/defense/governance`.

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
6. Add `01_load_defense_universe.py` and `02_validate_defense_universe.py`.
7. Load and validate `ticker_mapping/defense_tickers.csv`.

This creates the stable universe contract required before market data, financials, positioning, and scoring work can be implemented safely.
