# Technology Implementation Stages And Acceptance Tests

## Stage 0 - Architecture And Governance

Goal: create an independent technology package with clear boundaries.

Acceptance tests:

- `technology` imports without importing `med_devices`.
- Config resolves database, output, cache, and source-registry paths.
- Technology adapters do not write to external upstream DBs.
- The dedicated upstream positioning-feed sync is the only technology entry point allowed to populate `market_positioning.sqlite` for FINRA short-interest, SEC 13F, and IBKR borrow history.

## Stage 1 - Database Foundation

Goal: create the shared technology SQLite foundation.

Acceptance tests:

- `technology/scripts/00_init_technology_db.py` creates a clean SQLite DB.
- Required tables exist: `runs`, `source_registry`, `ingestion_runs`, `raw_api_responses`, `dim_company`, `dim_security`, `dim_identifier`, `dim_company_alias`, `dim_technology_taxonomy`, and `data_quality_issues`.
- Source registry loads without duplicate `source_id` values.
- FDA/CMS/reimbursement/med-device-specific tables are absent.

## Stage 2 - Security Master And Universe

Goal: load semiconductor tickers as the first technology universe.

Run order:

```powershell
python technology\semiconductors\scripts\01_load_semiconductor_universe.py
python technology\semiconductors\scripts\02_validate_semiconductor_universe.py
```

Implementation note: subsector scripts should be thin wrappers over the shared `technology/core/universe_loader.py` and `technology/core/universe_validator.py` engines.

Acceptance tests:

- `ticker_mapping/semiconductor_tickers.csv` is the authoritative semiconductor ticker universe.
- The authoritative semiconductor universe contains exactly 99 unique tickers.
- Duplicate tickers are blocked.
- Missing CIK rows are flagged, not silently excluded.
- Subsector and calibration cohort assignments are auditable.

## Stage 3 - Market Data And Corporate Actions

Goal: load adjusted prices, benchmarks, and corporate actions.

Run order:

```powershell
python technology\scripts\03_sync_technology_yahoo_adjusted_prices.py
python technology\scripts\04_audit_technology_market_data_policy.py
python technology\scripts\05_build_technology_market_features.py
python technology\scripts\06_validate_technology_market_stage.py
```

Acceptance tests:

- Adjusted OHLCV exists for active tickers and benchmarks.
- Split/dividend adjustment status is explicit.
- Low-history and low-liquidity names are flagged.
- One current `feature_market_technical` row exists for each active semiconductor ticker.

## Stage 4 - SEC Financial Statements

Goal: load point-in-time SEC filing and financial facts.

Run order:

```powershell
python technology\scripts\07_sync_technology_sec_fundamentals.py
python technology\scripts\11_sync_technology_fx_rates.py
python technology\scripts\08_build_technology_financial_features.py
```

Acceptance tests:

- Filing availability uses accepted time.
- SEC companyfacts ingestion stores raw XBRL facts across supported taxonomies before canonical mapping.
- `us-gaap` and `ifrs-full` concepts map into a canonical financial-statement layer.
- SEC companyfacts lag versus the latest regular filing is tracked in `dim_issuer_reporting_profile`.
- Latest lagged 20-F/40-F filings use SEC inline XBRL fallback rows tagged as `inline_xbrl_fallback`.
- Non-USD statement currencies are converted through `fact_fx_rate` before market-cap and EV ratios are calculated.
- Annual flow facts prefer the current fiscal period end, avoiding stale comparative-year facts inside the same filing.
- Missing concepts and accounting/margin sanity checks are stored as data-quality issues.
- Quarterly, annual, and TTM facts are reproducible without using period-end dates before filing dates.
- IFRS 20-F semiconductor issuers are classified as `SEC_OK_IFRS_FULL`, not generic SEC gaps.
- New issuers without regular operating financials, such as `CBRS`, remain review-only.

## Stage 5 - Ownership, Insider, And Positioning

Goal: sync direct SEC ownership filings, import read-only upstream positioning data into technology-owned tables, and build positioning features.

Run order:

```powershell
python technology\scripts\12_sync_technology_sec_ownership.py
python technology\scripts\13_sync_technology_positioning_upstream.py
python technology\scripts\09_import_technology_positioning.py
python technology\scripts\10_validate_technology_sec_positioning_stages.py
```

Acceptance tests:

- Direct SEC Forms 3/4/5 checks write filing-level, transaction-level, and holding-level rows into `technology.sqlite`.
- `dim_insider_reporting_profile` separates domestic expected coverage, post-HFIA FPI expected coverage, and qualifying-exemption/local-source cases.
- Direct SEC Form 4 non-derivative transactions backfill `fact_sec_form4_transaction` with `source_id='sec_ownership_direct'`.
- Form 4 transactions are imported from `sec_insider.sqlite` without writing to the upstream database.
- Missing Form 4 coverage is classified as local-source needed, filings-found-without-transactions, parser issue, or expected-missing review instead of a generic missing row.
- 13F, FINRA short-interest, and IBKR borrow adapters read populated `market_positioning.sqlite` rows read-only.
- FINRA short-interest and IBKR borrow coverage are required for all active semiconductor tickers.
- 13F coverage is required for all active semiconductor tickers except explicit new-issuer exceptions such as `CBRS`; exceptions must be removed when rows become available.
- Missing upstream coverage is stored as data-quality issues.
- One current `feature_positioning` row exists for each active semiconductor ticker.

## Stage 6A - Semiconductor Scoring Feature Contract

Goal: create the semiconductor-owned score input contract using existing technology market, SEC financial, and positioning feature layers, without depending on biotech or med-device scripts.

Run order:

```powershell
python technology\semiconductors\scripts\06a_build_semiconductor_scoring_features.py
python technology\semiconductors\scripts\06a_validate_semiconductor_scoring_features.py
```

Acceptance tests:

- Stage 6A entry points live under `technology/semiconductors/scripts`.
- No technology scoring code imports or calls `biotech_index` or `med_devices`.
- Required tables exist: `dim_scoring_component`, `feature_scoring_input`, and `feature_scoring_component`.
- One current `feature_scoring_input` row exists for each active semiconductor ticker.
- One current `feature_scoring_component` row exists for each active semiconductor ticker and each configured core or reserved sector-overlay component.
- Core components are built from existing technology-owned financial, market, and positioning features.
- Reserved sector-overlay components are neutral and explicitly marked `not_loaded` until Stage 6B populates WSTS/SIA/SEMI, big-tech capex, innovation, and geography/customer risk features.
- Non-exempt tickers are rank-ready under the Stage 6A core-data-quality gate.
- New-issuer exceptions, such as `CBRS`, may remain review-only until required operating financials and 13F history become available.

## Stage 6B - Semiconductor Sector Overlays

Goal: verify and load the first semiconductor-specific sector overlays, then apply them to the Stage 6A scoring contract without changing the scoring table shape.

Run order:

```powershell
python technology\semiconductors\scripts\06b_smoke_test_semiconductor_overlays.py
python technology\semiconductors\scripts\06b_sync_wsts_billings.py
python technology\semiconductors\scripts\06b_build_sector_cycle_features.py
python technology\semiconductors\scripts\06b_sync_big_tech_capex.py
python technology\semiconductors\scripts\06b_build_big_tech_capex_features.py
python technology\semiconductors\scripts\06b_apply_semiconductor_overlay_scores.py
python technology\semiconductors\scripts\06b_validate_semiconductor_overlays.py
```

Acceptance tests:

- The smoke test is non-mutating and does not update `technology.sqlite`.
- A source report is written to `output/technology_reports/sector_overlays/stage6b_source_smoke_test.csv`.
- WSTS historical billings downloads from the explicit historical-billings subpage and parses the XLSX workbook.
- WSTS rows exist in `fact_semiconductor_wsts_billings` for at least ten years of monthly data.
- One current `feature_semiconductor_sector_cycle` row exists with positive component quality.
- SEC big-tech capex companyfacts rows exist in `fact_big_tech_capex` for `MSFT`, `AMZN`, `GOOGL`, `META`, and `ORCL`.
- One current `feature_big_tech_capex_cycle` row exists with sufficient company coverage.
- Stage 6B updates `sector_cycle` and `big_tech_capex` component rows for all active semiconductor tickers.
- `feature_scoring_input.sector_overlay_status` is no longer `not_loaded` for active semiconductor tickers.
- Stage 6A scoring-feature validation still passes after Stage 6B overlay application.
- Paid/manual sources, SSL issues, API-key requirements, and text-only sources are classified before building loaders.

## Stage 7 - Calibrated Semiconductor Scoring

Goal: produce the production semiconductor ranking layer from the validated Stage 6 feature contract.

Run order:

```powershell
python technology\semiconductors\scripts\10_build_semiconductor_calibrated_scores.py
python technology\semiconductors\scripts\10_validate_semiconductor_calibrated_scores.py
```

Acceptance tests:

- Stage 7 reads only technology-owned semiconductor scoring inputs.
- Unknown component or subfeature weights fail fast.
- One latest production score row exists for each active semiconductor ticker that passes quality gates.
- Demoted or review-only names have explicit data-quality reasons.
- Production scores are written under `source_id=semiconductor_calibrated_score_v1`.
- Stage 7 does not overwrite baseline Stage 6A feature rows.

## Stage 8 - Constrained Calibration Research

Goal: run report-only constrained weight optimization and walk-forward validation without automatically changing production weights.

Run order:

```powershell
python technology\semiconductors\scripts\11_run_semiconductor_optuna_calibration.py
python technology\semiconductors\scripts\11_validate_semiconductor_optuna_calibration.py
python technology\semiconductors\scripts\12_validate_semiconductor_research_hardening.py
python technology\semiconductors\scripts\13_run_semiconductor_walk_forward_calibration.py
```

Acceptance tests:

- The calibration panel uses point-in-time universe membership.
- Current and historical/delisted semiconductor members are included when eligible data exists.
- Duplicate lineage continuations are excluded when the successor ticker already carries the predecessor history.
- Candidate weights obey configured turnover, concentration, component, and cohort constraints.
- Promotion remains manual and requires configured holdout and fold-robustness gates.
- Walk-forward refits are evaluated on untouched future blocks after embargo.
- Calibration reports include trial-level feasibility, candidate weights, holdout results, and provenance.
- Diagnostic `ic_t_stat` values are adjusted with Newey-West lags for overlapping forward-return windows; raw t-stats are retained separately.

## Stage 9 - Portfolio Backtest

Goal: convert score history into report-only portfolio behavior.

Run order:

```powershell
python technology\semiconductors\scripts\09b_run_semiconductor_portfolio_backtest.py
```

Acceptance tests:

- The backtest reads the same point-in-time research panel used by calibration.
- Stage 7 production, static review candidates, and Stage 8 report-only candidates are evaluated separately.
- Top-decile, top-quintile, long-short-decile, and long-short-quintile portfolios are reported.
- Equal-weight and score-weight variants are reported.
- Long-short variants are gross-normalized before return statistics are compared with long-only variants.
- Beta-neutral long-short and beta-hedged long-only variants are reported.
- Borrow-cost estimates are applied to short legs when borrow-fee data is available.
- SMH and equal-weight-universe benchmark-relative returns are reported.
- Transaction costs, turnover, positions, drawdown, hit rate, volatility, and cohort concentration are reported.
- Outputs are written under `output/technology_reports/backtests/` and do not write scores to the database.

## Stage 10 - Dashboard And Static Reports

Goal: publish static reports for the latest production semiconductor rankings, risk flags, overlays, and backtest diagnostics.

Run order:

```powershell
python technology\semiconductors\scripts\10b_publish_semiconductor_dashboard_reports.py
```

Acceptance tests:

- The final rank table covers the current 99-ticker semiconductor universe.
- Company scorecards include component scores, quality status, review reason, cohort, and source links where available.
- Cohort summaries, risk flags, review queue, and overlay summaries are written.
- Stage 9 backtest summary is linked into the dashboard manifest when available.
- A static `index.html` is written under `output/technology_reports/semi_dashboard/`, with dated snapshot copies under each run's `asof_date` subfolder.
- Dashboard publishing is read-only with respect to model scores and source data.

## Stage 10B - Governance Lockbox And Signal Registry

Goal: publish auditable model-governance artifacts without changing source data, scores, or production weights.

Run order:

```powershell
python technology\semiconductors\scripts\16_publish_semiconductor_lockbox_ledger.py
```

Acceptance tests:

- `technology/semiconductors/data/semiconductor_signal_registry.yaml` exists and is the semiconductor signal metadata registry.
- `output/technology_reports/governance/semiconductor_signal_registry.csv` and `.json` are written.
- `output/technology_reports/governance/semiconductor_lockbox_ledger.csv` and `.json` are written.
- `output/technology_reports/governance/semiconductor_governance_manifest.json` is written.
- The signal registry records production-locked, research-candidate, measurement-only, zero-weight, and planned signals.
- The signal registry includes Stage 7 weights, Stage 8 candidate flags, signal birthdates, and Newey-West diagnostic IC statistics when available.
- The lockbox ledger records file paths, SHA-256 hashes, row counts, Stage 8 promotion decision, walk-forward verdict, reference backtest row, latest Stage 7 summary, and top ranked names.
- The publisher is read-only with respect to `technology.sqlite`.

## Stage 12 - Refresh Orchestration

Goal: provide one validated refresh entry point per technology subsector with reportable per-step status and logs.

Run order:

```powershell
python technology\semiconductors\scripts\17_run_semiconductor_refresh_pipeline.py
python technology\software_infrastructure\scripts\17_run_software_infrastructure_refresh_pipeline.py
```

Acceptance tests:

- The default run executes the production refresh path and excludes research-only, Optuna, and one-time Norgate backfill steps.
- The runner supports `--dry-run`, `--skip-network`, `--include-research`, `--include-optuna`, `--include-norgate-backfill`, `--from-step`, `--to-step`, `--only`, and `--list-steps`.
- The runner step table is the authoritative stage map; script numbers are historical and may not match stage labels.
- The production default path runs the hardening validator even when Optuna research is not requested.
- Each step runs as a subprocess using the current Python executable and the configured `technology/config.yaml`.
- The final audit is included in the default production path.
- A JSON manifest and CSV step ledger are written under `output/technology_reports/orchestration/`.
- Per-step logs are written under `output/technology_reports/orchestration/logs/`.
- The runner stops on first failure unless `--continue-on-error` is set.
- Stage 8 weight searches remain opt-in and are not part of routine refreshes.
- The software-infrastructure default path uses the deliberate neutral Stage 6B closure and existing validators instead of semiconductor-specific hardening/final-audit scripts.
