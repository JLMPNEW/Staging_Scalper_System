# Technology Scoring Model

This package is the independent implementation namespace for technology-sector stock scoring. It starts with semiconductors, but the shared `technology` infrastructure is intended to support additional technology subsectors without creating a new permanent database for each subsector.

## Independence Rules

- Do not call or import `med_devices` scripts from this package.
- Do not write to biotech, med-device, or Form 4 databases.
- Only the dedicated upstream positioning-feed sync may write to `market_positioning.sqlite`; the technology positioning adapter must read that database read-only.
- Default database: `${TECHNOLOGY_DB_DIR:-C:/Users/josel/Documents/STAGING/DB}/technology.sqlite`.
- Default outputs: `output/technology_reports` and `output/technology_cache`.
- External data products such as `sec_insider.sqlite` and populated `market_positioning.sqlite` are read-only inputs to technology adapters. Technology adapters import filtered, normalized rows into technology-owned tables.

## Initial Stage

Initialize the database:

```powershell
python technology\scripts\00_init_technology_db.py
```

Use a scratch database for smoke tests:

```powershell
python technology\scripts\00_init_technology_db.py --db C:\tmp\technology.sqlite
```

The first universe load stage uses `ticker_mapping/semiconductor_tickers.csv` as the authoritative current semiconductor ticker universe. The current semiconductor source-of-truth universe is expected to contain exactly 99 unique tickers. The loader also seeds `dim_universe_membership` with current-source rows; those rows protect the production universe and give Stage 8 a stable place to add true historical/delisted membership intervals.

## Script Layout

- `technology/scripts/` contains technology-wide infrastructure scripts that should remain reusable across subsectors.
- `technology/core/universe_loader.py` and `technology/core/universe_validator.py` contain the reusable universe/cohort engine for all technology subsectors.
- `technology/semiconductors/scripts/` contains semiconductor-specific orchestration, universe/cohort checks, overlays, scoring, calibration, and report publishing.

Load and validate the semiconductor universe:

```powershell
python technology\semiconductors\scripts\01_load_semiconductor_universe.py
python technology\semiconductors\scripts\01b_load_semiconductor_historical_membership.py
python technology\semiconductors\scripts\02_validate_semiconductor_universe.py
```

The legacy `technology\scripts\01_load_technology_universe.py` and `technology\scripts\02_validate_technology_universe.py` entry points are compatibility shims that delegate to the semiconductor scripts.

For additional technology subsectors, add thin wrappers under `technology\<subsector>\scripts\` that pass subsector-specific defaults into the shared universe loader and validator engines.

## Semiconductor Market Data

Stage 3 loads Yahoo adjusted daily OHLCV, dividends, splits, market snapshots, benchmark prices, and the first market technical feature set.

```powershell
python technology\scripts\03_sync_technology_yahoo_adjusted_prices.py
python technology\scripts\04_audit_technology_market_data_policy.py
python technology\scripts\05_build_technology_market_features.py
python technology\scripts\06_validate_technology_market_stage.py
```

Low-history tickers are review flags by default, not gate failures, because newly listed names can still contribute partial market signals.

## SEC And Positioning Stages

Stage 4 loads SEC submissions and companyfacts, stores raw XBRL facts across supported taxonomies, maps `us-gaap` and `ifrs-full` concepts into canonical financial facts, then builds point-in-time financial features:

```powershell
python technology\scripts\07_sync_technology_sec_fundamentals.py
python technology\scripts\11_sync_technology_fx_rates.py
python technology\scripts\08_build_technology_financial_features.py
```

Foreign private issuers that file 20-F reports are treated as annual SEC fundamental issuers. Valid `ifrs-full` facts are mapped into the same canonical metrics as US GAAP facts. When SEC companyfacts lags the latest 20-F/40-F metadata, the SEC sync attempts an inline XBRL fallback and tags those raw rows with `source_detail='inline_xbrl_fallback'`.

Financial features preserve reported-currency accounting values and add USD conversion fields for valuation ratios. FX rates are stored in `fact_fx_rate`; non-USD statements must convert before market-cap, EV, and FCF-yield calculations are populated. New issuers without regular operating financials remain review-only.

Stage 5 syncs direct SEC ownership filings, imports positioning facts from read-only upstream databases, and builds positioning features:

```powershell
python technology\scripts\12_sync_technology_sec_ownership.py
python technology\scripts\13_sync_technology_positioning_upstream.py
python technology\scripts\09_import_technology_positioning.py
python technology\scripts\10_validate_technology_sec_positioning_stages.py
```

Direct SEC ownership ingestion reads Forms 3/4/5 XML from EDGAR into technology-owned filing, transaction, holding, and insider-reporting-profile tables. It also backfills direct Form 4 non-derivative transactions into `fact_sec_form4_transaction` under `source_id='sec_ownership_direct'` so the existing positioning feature builder can use them.

The positioning adapter never writes to `sec_insider.sqlite` or `market_positioning.sqlite`; it only normalizes available rows into `technology.sqlite`. The upstream positioning sync intentionally writes to `market_positioning.sqlite`, because that database owns FINRA short-interest, SEC 13F, and IBKR borrow history. Direct SEC ownership is the preferred diagnostic source when the upstream Form 4 database has gaps.

With `positioning_import.include_historical_members: true`, the upstream sync builds `output/technology_cache/positioning/semiconductor_positioning_universe.csv` from the 99 current tickers plus the historical semiconductor membership seed. This lets the free SEC 13F data sets and FINRA short-interest files backfill acquired/delisted tickers without mixing those rows into the current 99-ticker scoring feature output. The standard free-source historical refresh is:

```powershell
python technology\scripts\13_sync_technology_positioning_upstream.py --history-start 2013-01-01 --skip-ibkr-borrow
```

IBKR borrow is skipped in that historical run because it is a current/live broker feed, not a free historical delisted-ticker source.

## Semiconductor Scoring Contract

Stage 6A builds the semiconductor scoring feature contract from existing technology-owned market, financial, and positioning feature layers. It does not import or call biotech or med-device code.

```powershell
python technology\semiconductors\scripts\06a_build_semiconductor_scoring_features.py
python technology\semiconductors\scripts\06a_validate_semiconductor_scoring_features.py
```

The Stage 6A output tables are:

- `dim_scoring_component`
- `feature_scoring_input`
- `feature_scoring_component`

Sector-cycle components such as WSTS/SIA/SEMI, big-tech capex, innovation, and geography/customer risk are present as neutral `not_loaded` placeholders in Stage 6A. Stage 6B should load those external semiconductor overlays and populate the reserved component rows without changing the scoring table contract.

Before refreshing Stage 6B ingestion, run the non-mutating source smoke test:

```powershell
python technology\semiconductors\scripts\06b_smoke_test_semiconductor_overlays.py
```

The smoke test writes `output/technology_reports/sector_overlays/stage6b_source_smoke_test.csv` and does not update `technology.sqlite`.

Run the implemented Stage 6B overlay pipeline:

```powershell
python technology\semiconductors\scripts\06b_sync_wsts_billings.py
python technology\semiconductors\scripts\06b_build_sector_cycle_features.py
python technology\semiconductors\scripts\06b_sync_big_tech_capex.py
python technology\semiconductors\scripts\06b_build_big_tech_capex_features.py
python technology\semiconductors\scripts\06b_apply_semiconductor_overlay_scores.py
python technology\semiconductors\scripts\06b_validate_semiconductor_overlays.py
```

Stage 6B currently populates `sector_cycle` from WSTS and `big_tech_capex` from SEC companyfacts. Remaining overlay components stay neutral until their sources are implemented. Re-running the Stage 6A build preserves already-applied Stage 6B overlay state for the same as-of date instead of resetting it to neutral.

## Signal Diagnostics (IC Validation)

The diagnostics stage builds a point-in-time historical panel (financials by filing date, insider flows by Form 4 filing date, 13F by period filing date, short interest by FINRA publication date), computes forward beta-hedged residual returns against `SMH`, and reports rank-IC statistics per subfeature and per component:

```powershell
python technology\semiconductors\scripts\07_run_semiconductor_signal_diagnostics.py
```

It opens `technology.sqlite` read-only and writes `output/technology_reports/signal_diagnostics/subfeature_ic.csv`, `component_ic.csv`, and `suggested_weights.csv` (IC-proportional weights for review; nothing is applied automatically). Subfeatures with `keep_candidate=0` lack a statistically significant positive IC over the panel window and should be reviewed before being given weight. `ic_t_stat` is Newey-West adjusted for overlapping forward-return windows, while `raw_ic_t_stat` preserves the unadjusted statistic for comparison. The Stage 6A validator additionally fails when any core component has zero quality for more than `max_dead_core_component_pct` of the universe, or has degenerate cross-sectional variance.

Valuation ratios (EV/GP, EV/OI, FCF yield) are re-priced from the filing date to the evaluation date using the adjusted-close ratio, both in the diagnostics panel and in the production Stage 6A build, so valuation signals move with price between filings instead of staying frozen at the last filing's price.

Positioning subfeatures are gated by per-signal birthdates so unavailable feed eras are not scored as valid zeroes. The diagnostics stage writes those dates to `output/technology_reports/signal_diagnostics/signal_birthdates.csv`.

The diagnostics stage also writes WSTS cycle-exposure review artifacts before that signal is allowed into production weighting: `wsts_cycle_regime_ic.csv`, `wsts_cycle_cohort_ic.csv`, `wsts_cycle_lag_sensitivity.csv`, and `wsts_cycle_correlations.csv`.

## Stage 7 Calibrated Scoring

Stage 7 reads the validated Stage 6 feature contract, re-percentiles the raw subfeatures, applies config-driven component/subfeature weights (`semiconductor_calibrated_scoring` in `config.yaml`), and writes a separate ranking layer under `source_id=semiconductor_calibrated_score_v1` without touching the baseline:

```powershell
python technology\semiconductors\scripts\10_build_semiconductor_calibrated_scores.py
python technology\semiconductors\scripts\10_validate_semiconductor_calibrated_scores.py
```

Outputs go to `feature_scoring_input`, `feature_scoring_component`, and `feature_scoring_model_output` plus a CSV ranking report. Unknown component or subfeature names in the weight config fail fast. The growth component must stay neutralized (weight 0) in v1: trailing growth showed negative IC over the diagnostic panel. Tickers the Stage 7 quality gates demote below the baseline's rank-ready set are validation warnings when explained by Stage 7's own reasons, and errors otherwise.

Static weight changes should be reviewed through the report-only comparison runner before editing production config:

```powershell
python technology\semiconductors\scripts\14_compare_semiconductor_stage7_static_candidates.py
```

It compares the current Stage 7 baseline, a conservative no-growth v1.1 candidate, and a clearly marked non-production growth probe. Outputs land in `output/technology_reports/scoring/semiconductor_stage7_static_candidate_review.csv`, `semiconductor_stage7_static_candidate_weights.csv`, and `semiconductor_stage7_static_candidate_review.json`.

## Stage 8 Optuna Weight Calibration (report-only)

Stage 8 rebuilds the point-in-time panel, splits it into train/holdout with a horizon-derived embargo, and searches constrained component/subfeature weights with Optuna against an objective that combines mean IC, IC stability (std penalty), hit rate, net-of-cost quintile spread (`turnover_cost_bps`), and a complexity penalty per weighted subfeature. The best candidate is compared against the Stage 7 baseline on the untouched holdout and across contiguous date folds (`robustness_folds`), and per-WSTS-regime ICs (up/down cycle) are reported:

```powershell
python technology\semiconductors\scripts\11_run_semiconductor_optuna_calibration.py
python technology\semiconductors\scripts\11_validate_semiconductor_optuna_calibration.py
python technology\semiconductors\scripts\12_validate_semiconductor_research_hardening.py
```

It writes only reports to `output/technology_reports/optuna_calibration/` (trials, summary, by-date ICs, fold robustness, subfeature correlation matrix, candidate scores, and `stage8_best_weights.json` with config hash / git commit / seed provenance, plus the persisted Optuna study in `stage8_optuna_study.sqlite`). Promotion to Stage 7 config is a human decision; `promotion_candidate=1` requires holdout improvement, IC/hit-rate floors, turnover and cohort caps, and a majority of fold wins.

Stage 8 filters each panel date through `dim_universe_membership`. The historical membership loader seeds `point_in_time_flag=1` intervals for the 99 current tickers using first local adjusted-price availability, plus acquired/delisted rows from `technology/semiconductors/data/semiconductor_historical_membership.csv`. The hardening validator checks current PIT coverage, inactive/delisted PIT coverage, and a synthetic 10-for-1 split repricing invariant. Historical price/fundamental rows for inactive tickers are a separate data backfill; Stage 8 will include them automatically once those rows exist.

With `hard_constraints_in_search: true` (default), candidates that breach the turnover or cohort-concentration caps on the train window are heavily penalized inside the search itself, so the returned best trial is always promotable on those dimensions whenever a feasible candidate exists (the `feasible` column in `stage8_trials.csv` records this per trial).

## Walk-Forward Refit Validation

The walk-forward runner answers the stronger question — does *re-calibrating* weights periodically beat the static Stage 7 baseline out of sample? — by repeating the TPE search on expanding train windows and evaluating each refit candidate against Stage 7 on the next embargoed, untouched test block:

```powershell
python technology\semiconductors\scripts\13_run_semiconductor_walk_forward_calibration.py
```

Outputs land in `output/technology_reports/optuna_calibration/walk_forward/`: `walk_forward_blocks.csv` (per-block objectives, ICs, and each refit's weights) and `walk_forward_summary.json` with the refit win rate, mean objective improvement, paired t-statistic, and the `procedure_adds_value` verdict. Block size, initial train length, and trials per refit live under `semiconductor_optuna_calibration.walk_forward`.

## Stage 9 Portfolio Backtest

Stage 9 converts the research score panel into report-only portfolio simulations. It compares the current Stage 7 production baseline, static review candidates, and the Stage 8 best candidate without promoting any weights automatically:

```powershell
python technology\semiconductors\scripts\09b_run_semiconductor_portfolio_backtest.py
```

It writes summary, period-level, holding-level, and manifest outputs to `output/technology_reports/backtests/`. The backtest uses point-in-time universe membership, the research price-source priority list, monthly-style rebalance dates from the diagnostics panel, transaction-cost assumptions from `semiconductor_portfolio_backtest`, and both equal-weight and score-weight versions of top-decile, top-quintile, long-short-decile, and long-short-quintile portfolios. Long-only portfolios are also tested as beta-hedged longs; long-short portfolios are gross-normalized and tested as both dollar-neutral and beta-neutral books. Period rows include SMH and equal-weight-universe benchmark returns, borrow-cost estimates for short legs, stock/hedge turnover and transaction-cost columns, gross exposure, and beta exposure.

## Stage 10 Dashboard Reports

Stage 10 publishes static semiconductor reports from the latest production Stage 7 scores and Stage 9 portfolio diagnostics:

```powershell
python technology\semiconductors\scripts\10b_publish_semiconductor_dashboard_reports.py
```

It writes the final rank table, company scorecards, cohort summary, risk flags, review queue, overlay summary, manifest, and a static `index.html` to `output/technology_reports/dashboard/`. These reports are read-only publishing artifacts; they do not write new model scores to the database.

## Governance Lockbox And Signal Registry

The governance publisher freezes the current model evidence into a lockbox ledger and a signal registry without changing data, scores, or weights:

```powershell
python technology\semiconductors\scripts\16_publish_semiconductor_lockbox_ledger.py
```

It reads `technology/semiconductors/data/semiconductor_signal_registry.yaml`, the Stage 7 config weights, diagnostics, Stage 8 research outputs, Stage 9 backtests, Stage 10 dashboard manifest, and the latest production score rows. Outputs land under `output/technology_reports/governance/`: `semiconductor_signal_registry.csv`, `semiconductor_signal_registry.json`, `semiconductor_lockbox_ledger.csv`, `semiconductor_lockbox_ledger.json`, `semiconductor_governance_manifest.json`, and a timestamped snapshot under `snapshots/`. The registry shows production-locked, research-candidate, measurement-only, zero-weight, and planned signals with their diagnostic IC/NW t-stats and birthdate gates. The lockbox ledger records artifact paths, file hashes, row counts, Stage 8 promotion status, walk-forward verdict, backtest reference row, and latest top ranked names.

## Stage 12 Refresh Orchestration

Stage 12 provides one production refresh entry point for each validated subsector sequence.

Semiconductors:

```powershell
python technology\semiconductors\scripts\17_run_semiconductor_refresh_pipeline.py
```

The default run executes the production path only: DB/schema initialization, universe validation, prices, PIT membership, market features, SEC fundamentals, FX, positioning, Stage 6A, Stage 6B, Stage 7, the hardening validator, dashboard publishing, governance lockbox, and the final audit. It writes a manifest and per-step logs under `output/technology_reports/orchestration/`.

Script numbers are historical and no longer map one-to-one to stage labels; the Stage 12 orchestrator step table is the authoritative execution map.

Useful modes:

```powershell
python technology\semiconductors\scripts\17_run_semiconductor_refresh_pipeline.py --dry-run
python technology\semiconductors\scripts\17_run_semiconductor_refresh_pipeline.py --skip-network
python technology\semiconductors\scripts\17_run_semiconductor_refresh_pipeline.py --include-research
python technology\semiconductors\scripts\17_run_semiconductor_refresh_pipeline.py --include-optuna
python technology\semiconductors\scripts\17_run_semiconductor_refresh_pipeline.py --list-steps
```

Research diagnostics, Stage 9 backtests, Stage 8 Optuna, and one-time Norgate delisted-price backfill are opt-in so routine refreshes do not accidentally rerun model-review workflows.

Software infrastructure:

```powershell
python technology\software_infrastructure\scripts\17_run_software_infrastructure_refresh_pipeline.py
```

The software default run executes the production path only: DB/schema initialization, universe and PIT membership validation, prices, market features, SEC fundamentals, FX, positioning, Stage 6A, deliberate neutral Stage 6B closure, production calibrated scores, dashboard publishing, and governance lockbox validation. It writes a manifest and per-step logs under `output/technology_reports/software_infrastructure/orchestration/`.

Useful software modes:

```powershell
python technology\software_infrastructure\scripts\17_run_software_infrastructure_refresh_pipeline.py --dry-run
python technology\software_infrastructure\scripts\17_run_software_infrastructure_refresh_pipeline.py --skip-network
python technology\software_infrastructure\scripts\17_run_software_infrastructure_refresh_pipeline.py --include-research
python technology\software_infrastructure\scripts\17_run_software_infrastructure_refresh_pipeline.py --include-optuna
python technology\software_infrastructure\scripts\17_run_software_infrastructure_refresh_pipeline.py --list-steps
```

## Extended History And Historical Fundamentals

Price, FX, and SEC-fundamentals syncs start at 2010 so the research panel spans multiple semiconductor cycles (diagnostics panel start: 2011). With `sec_fundamentals.include_historical_members: true`, the SEC sync and financial-feature build also cover historical members' CIKs from the membership seed, so their point-in-time fundamentals are panel-ready.

Stage 15 backfills historical/delisted semiconductor price rows from the local licensed Norgate database into `fact_price_ohlcv` under `source_id=norgate_us_equities_total_return`:

```powershell
python technology\semiconductors\scripts\15_import_semiconductor_norgate_delisted_prices.py
```

Research calibration uses `semiconductor_research.price_source_ids` in priority order, currently Yahoo first and Norgate second. Production Stages 6/7 remain active-universe only and keep filtering on the current 99-ticker universe. Historical membership excludes duplicate lineage continuations when the successor ticker already carries the full Yahoo history, such as `IIVI`/`COHR`; rerunning Stage 15 removes stale duplicate-lineage Norgate rows.

## Cycle-Exposure Signal (measurement-only)

`wsts_cycle_exposure` is the first ticker-specific cycle signal: each name's monthly returns are regressed on innovations in the publication-lagged WSTS worldwide billings YoY, betas are shrunk toward their calibration-cohort mean (empirical Bayes), and the shrunk beta is multiplied by the current YoY state — high-beta names rank high in up-cycles and low in down-cycles. It is computed inside the diagnostics and Stage 8 panels and IC-tested like every other subfeature, but carries **no production weight** and stays out of the Stage 8 candidate space until it shows significant IC in both WSTS regimes on the extended multi-cycle history.

## Pipeline State Audit

A read-only audit of the live database (coverage, latest financial field population, component health, WSTS freshness, Stage 7 output coverage at the latest as-of, Stage 8 report presence) writes JSON/CSV reports under `output/technology_reports/audits/`:

```powershell
python technology\semiconductors\scripts\08_audit_semiconductor_pipeline_state.py
```

Stage 8 output checks only warn until Stage 8 has been run (set `semiconductor_pipeline_audit.require_stage8_outputs: true` to enforce them). Governance lockbox and signal-registry outputs are required by default through `semiconductor_pipeline_audit.require_governance_reports`.
