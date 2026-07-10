# MacroLayer Stage 1 Raw Ingestion

This package scaffolds a production-style raw macro ingestion layer with:

- A dedicated SQLite store for macro raw data
- Parallel HTTP fetch per source
- Single-writer SQLite upserts
- Registry-driven source and series definitions
- Seed tables for country metadata and release-calendar metadata

## Files

- `config_macro_raw.yaml`: macro raw config and worker settings
- `macro_metric_registry_seed.csv`: seed registry for fixed U.S. metrics
- `macro_country_metric_templates.csv`: foreign-country metric templates
- `macro_country_metadata_seed.csv`: seed foreign ETF country metadata
- `build_macro_registry_full.py`: compiles the U.S. base rows plus foreign-country templates into the runnable full registry
- `macro_metric_registry_full.csv`: generated full registry consumed by the pipeline
- `build_macro_metric_policy.py`: generates the tier-1 downstream policy file from the enabled runtime registry
- `macro_metric_policy.csv`: generated metric policy consumed by QA and future PIT serving
- `build_macro_feature_policy.py`: generates the tier-1 macro feature policy file
- `macro_feature_policy.csv`: generated feature policy consumed by the feature builder
- `build_macro_composite_policy.py`: generates the tier-1 macro composite policy file
- `macro_composite_policy.py`: loads and validates the composite policy file
- `macro_composite_policy.csv`: generated composite policy consumed by the composite builder
- `macro_release_calendar_seed.csv`: starter release-family metadata
- `init_macro_db.py`: creates the SQLite schema and views
- `init_macro_serving_db.py`: creates the SQLite schema for the PIT serving layer
- `run_macro_raw_pipeline.py`: orchestrates registry load, task planning, fetch, and upsert
- `qa_macro_raw.py`: persists QA runs, issues, span summaries, freshness summaries, and class-aware country coverage
- `build_macro_calendar_daily.py`: builds the serving calendar
- `build_macro_observation_daily_pit.py`: materializes one daily PIT row per `as_of_date` and `metric_key`
- `build_macro_metric_latest.py`: materializes the latest PIT snapshot per metric
- `build_macro_country_coverage_daily.py`: materializes daily country coverage and confidence inputs
- `build_macro_features.py`: materializes the event-level and daily macro feature layer
- `build_macro_composites.py`: materializes the daily composite layer and component-contribution layer
- `build_macro_probabilities.py`: materializes calibrated daily macro probabilities plus calibration diagnostics
- `macro_probability_v2.py`: pure multivariate ridge-logistic and four-state regime primitives for the shadow v2 model
- `build_macro_probabilities_v2.py`: builds versioned shadow probabilities against independent first-release GDP and CPI/PCE outcomes
- `validate_macro_probabilities_v2.py`: hard-checks v2 target independence, PIT label cutoffs, model payloads, probability integrity, and OOS evidence
- `build_macro_regime_v2_decision.py`: applies the production smoothing, transition, and hysteresis primitives to the namespaced v2 candidate
- `validate_macro_regime_v2_promotion.py`: compares v2 with v1 on common independent outcomes and emits the sealed, fail-closed promotion verdict
- `audit_macro_v2_vintage_gaps.py`: reports cell-level evidence deficits and the exact PIT/vintage inputs blocking an earlier model-ready date; optional ALFRED probing is manual
- `build_macro_regime_raw.py`: materializes the raw 4-state macro regime layer from Stage 6 probabilities
- `build_macro_regime_smoothed.py`: materializes the Stage 8 smoothed regime layer plus transition diagnostics
- `build_macro_regime_decision.py`: materializes the Stage 8.5 decision overlay for active portfolio regimes
- `build_macro_industry_fit.py`: materializes the Stage 9 weekly industry-first macro fit layer for sectors, industry aggregates, and industries
- `check_macro_industry_fit.py`: runs the Stage 9 acceptance diagnostics for stability, historical windows, and peer-group differentiation
- `build_macro_country_fit.py`: materializes the Stage 10 country macro fit, confidence, and rank layer for the optional foreign ETF sleeve
- `check_macro_country_fit.py`: runs the Stage 10 acceptance diagnostics for country scores, confidence haircuts, and rank stability
- `build_macro_stock_overlay.py`: materializes the Stage 11 stock-level macro overlay, selection score, and weight score layer
- `check_macro_stock_overlay.py`: runs the Stage 11 acceptance diagnostics for stock score dispersion and macro ranking impact
- `build_macro_portfolio_inputs.py`: materializes the Stage 12A optimizer-ready portfolio input layer for U.S. stocks and optional foreign ETFs
- `check_macro_portfolio_inputs.py`: runs the Stage 12A acceptance diagnostics for portfolio input completeness and export schemas
- `build_macro_stock_sleeve_targets.py`: materializes the Stage 12B stock sleeve industry and sector target weights
- `check_macro_stock_sleeve_targets.py`: runs the Stage 12B acceptance diagnostics for target sums, bands, caps, and exports
- `build_macro_foreign_sleeve_budget.py`: materializes the Stage 12C optional foreign sleeve budget and ETF candidate weights
- `check_macro_foreign_sleeve_budget.py`: runs the Stage 12C acceptance diagnostics for foreign budget activation, bounds, and candidate weights
- `run_macro_optimizer_integration.py`: runs Stage 12D final optimizer cases for baseline, macro-full, and stocks-only comparisons
- `check_macro_optimizer_integration.py`: runs the Stage 12D acceptance diagnostics for final optimizer weights, foreign budget, and target-band compliance
- `run_macro_serving_pipeline.py`: orchestrates the serving DAG in dependency order
- `check_macro_composite_regimes.py`: summarizes composite behavior across key macro stress and transition windows

## Design choices

- Keep macro data in its own SQLite DB. Do not mix it into the SEC DBs.
- Parallelize network I/O only. SQLite writes remain serialized.
- Registry rows are keyed by `registry_key`, while `metric_key` stays stable across preferred and fallback sources.
- Raw observations are long-form and audit-friendly.

## Connectors included

- `fred_alfred`
- `phillyfed_ads`
- `oecd_sdmx`
- `eia_seriesid`
- `imf_sdmx`

## Current seed status

- U.S. FRED and ALFRED coverage is wired and seeded.
- ADS is wired via page scraping of the current-vintage and all-vintages links.
- EIA direct energy rows are seeded via APIv1-style `seriesid` compatibility routes on API v2.
- Foreign tier-1 country rows are generated from a country-class model: `A_full`, `B_partial`, and `C_fallback`.
- Tier-1 foreign FX, NEER, and REER now use FRED H.10 spot rates plus BIS effective-rate series carried in FRED.
- OECD local-macro rows are only emitted for tier-1 country/metric pairs that are both policy-eligible and officially validated.
- Hong Kong and Taiwan are intentionally treated as partial-pack countries in tier 1; they do not require full OECD macro symmetry.
- IMF remains available as a future targeted fallback connector, but it is not a blocking dependency for the tier-1 runtime registry.

## Usage

Build the full registry first:

```powershell
python MacroLayer/build_macro_registry_full.py
```

Build the metric policy file after the runtime registry:

```powershell
python MacroLayer/build_macro_metric_policy.py
```

Build the feature policy file after the metric policy:

```powershell
python MacroLayer/build_macro_feature_policy.py
```

If you want the legacy backlog view with disabled foreign rows included, use:

```powershell
python MacroLayer/build_macro_registry_full.py --include-disabled-country-rows
```

Initialize the DB:

```powershell
python MacroLayer/init_macro_db.py --config MacroLayer/config_macro_raw.yaml
```

Initialize the serving DB:

```powershell
python MacroLayer/init_macro_serving_db.py --config MacroLayer/config_macro_raw.yaml
```

Dry-run task planning:

```powershell
python MacroLayer/run_macro_raw_pipeline.py --config MacroLayer/config_macro_raw.yaml --mode daily --dry-run
```

Backfill 25 years from an exact start date:

```powershell
python MacroLayer/run_macro_raw_pipeline.py --config MacroLayer/config_macro_raw.yaml --mode backfill --history-start-date 2001-01-01
```

Run the persisted QA gate against the latest completed ingest run:

```powershell
python MacroLayer/qa_macro_raw.py --config MacroLayer/config_macro_raw.yaml
```

Build the serving calendar:

```powershell
python MacroLayer/build_macro_calendar_daily.py --config MacroLayer/config_macro_raw.yaml
```

Build the daily PIT table:

```powershell
python MacroLayer/build_macro_observation_daily_pit.py --config MacroLayer/config_macro_raw.yaml
```

Build the latest PIT snapshot:

```powershell
python MacroLayer/build_macro_metric_latest.py --config MacroLayer/config_macro_raw.yaml
```

Build daily country coverage:

```powershell
python MacroLayer/build_macro_country_coverage_daily.py --config MacroLayer/config_macro_raw.yaml
```

Build the macro feature layer:

```powershell
python MacroLayer/build_macro_features.py --config MacroLayer/config_macro_raw.yaml
```

Build the macro composite policy:

```powershell
python MacroLayer/build_macro_composite_policy.py
```

Build the macro composite layer:

```powershell
python MacroLayer/build_macro_composites.py --config MacroLayer/config_macro_raw.yaml
```

Build the macro probability layer:

```powershell
python MacroLayer/build_macro_probabilities.py --config MacroLayer/config_macro_raw.yaml
```

Build and validate the shadow v2 candidate (this does not change the active regime source):

```powershell
python MacroLayer/build_macro_probabilities_v2.py --config MacroLayer/config_macro_raw.yaml
python MacroLayer/validate_macro_probabilities_v2.py --config MacroLayer/config_macro_raw.yaml
python MacroLayer/build_macro_regime_v2_decision.py --config MacroLayer/config_macro_raw.yaml
python MacroLayer/validate_macro_regime_v2_promotion.py --config MacroLayer/config_macro_raw.yaml
python MacroLayer/audit_macro_v2_vintage_gaps.py --config MacroLayer/config_macro_raw.yaml
```

To query ALFRED for the earliest provider vintage of locally deficient FRED series, run the audit manually
with `--probe-fred`. The daily serving DAG deliberately runs the local-only audit so broker/API availability
cannot block the macro refresh and credentials never appear in the report.

Run the full serving DAG in one command:

```powershell
python MacroLayer/run_macro_serving_pipeline.py --config MacroLayer/config_macro_raw.yaml
```

Build the raw Stage 7 regime layer:

```powershell
python MacroLayer/build_macro_regime_raw.py --config MacroLayer/config_macro_raw.yaml
```

Build the Stage 8 smoothed regime layer:

```powershell
python MacroLayer/build_macro_regime_smoothed.py --config MacroLayer/config_macro_raw.yaml
```

Build the Stage 8.5 regime decision layer:

```powershell
python MacroLayer/build_macro_regime_decision.py --config MacroLayer/config_macro_raw.yaml
```

Build the Stage 9 weekly industry macro layer:

```powershell
python MacroLayer/build_macro_industry_fit.py --config MacroLayer/config_macro_raw.yaml
```

Check whether the Stage 9 industry-first macro map is stable and interpretable:

```powershell
python MacroLayer/check_macro_industry_fit.py --config MacroLayer/config_macro_raw.yaml
```

Build the Stage 10 country macro layer:

```powershell
python MacroLayer/build_macro_country_fit.py --config MacroLayer/config_macro_raw.yaml
```

Check whether the Stage 10 country macro map is stable and interpretable:

```powershell
python MacroLayer/check_macro_country_fit.py --config MacroLayer/config_macro_raw.yaml
```

Build the Stage 11 stock-level macro overlay:

```powershell
python MacroLayer/build_macro_stock_overlay.py --config MacroLayer/config_macro_raw.yaml
```

Check whether the Stage 11 stock-level macro overlay is usable:

```powershell
python MacroLayer/check_macro_stock_overlay.py --config MacroLayer/config_macro_raw.yaml
```

Build the Stage 12A optimizer-ready portfolio inputs:

```powershell
python MacroLayer/build_macro_portfolio_inputs.py --config MacroLayer/config_macro_raw.yaml
```

Check whether the Stage 12A portfolio inputs and optimizer CSV exports are usable:

```powershell
python MacroLayer/check_macro_portfolio_inputs.py --config MacroLayer/config_macro_raw.yaml
```

Build the Stage 12B stock sleeve industry targets:

```powershell
python MacroLayer/build_macro_stock_sleeve_targets.py --config MacroLayer/config_macro_raw.yaml
```

Check whether the Stage 12B stock sleeve targets are usable:

```powershell
python MacroLayer/check_macro_stock_sleeve_targets.py --config MacroLayer/config_macro_raw.yaml
```

Build the Stage 12C optional foreign sleeve budget:

```powershell
python MacroLayer/build_macro_foreign_sleeve_budget.py --config MacroLayer/config_macro_raw.yaml
```

Check whether the Stage 12C foreign sleeve budget is usable:

```powershell
python MacroLayer/check_macro_foreign_sleeve_budget.py --config MacroLayer/config_macro_raw.yaml
```

Run the Stage 12D final optimizer integration cases:

```powershell
python MacroLayer/run_macro_optimizer_integration.py --config MacroLayer/config_macro_raw.yaml
```

Check whether the Stage 12D final optimizer outputs are usable:

```powershell
python MacroLayer/check_macro_optimizer_integration.py --config MacroLayer/config_macro_raw.yaml
```

Check whether the composites behave sensibly around the main macro stress windows:

```powershell
python MacroLayer/check_macro_composite_regimes.py --config MacroLayer/config_macro_raw.yaml
```

Run selected sources:

```powershell
python MacroLayer/run_macro_raw_pipeline.py --config MacroLayer/config_macro_raw.yaml --sources fred_alfred phillyfed_ads
```

Use YAML `api_key` fields or environment variables for API keys:

```powershell
$env:FRED_API_KEY="..."
$env:EIA_API_KEY="..."
```

YAML `api_key` takes precedence when both are present.

## Notes

- `macro_observation_raw` stores the canonical raw observation schema plus internal keys.
- `macro_observation_latest_current_v` exposes the latest row per registry series.
- `macro_observation_preferred_latest_v` exposes the latest preferred source row per `metric_key`.
- QA results are stored in `macro_qa_run`, `macro_qa_issue`, `macro_metric_span_summary`, `macro_metric_freshness_summary`, and `macro_country_coverage_summary`.
- The serving layer lives in a separate SQLite DB at `serving_db_path` and materializes `macro_calendar_daily`, `macro_observation_daily_pit`, `macro_metric_latest`, and `macro_country_coverage_daily`.
- The feature layer also lives in the serving DB and materializes `macro_feature_event` and `macro_feature_daily`.
- The composite layer also lives in the serving DB and materializes `macro_composite_daily` plus `macro_composite_component_daily`.
- The probability layer also lives in the serving DB and materializes `macro_probabilities_daily`, `macro_probability_calibration`, and `macro_probability_diagnostics`.
- The independent-outcome v2 research layer is isolated in `macro_probability_v2_target`,
  `macro_probability_v2_model`, `macro_probability_v2_daily`, `macro_probability_v2_diagnostics`, and
  `macro_regime_v2_daily`. Its namespaced decision/evidence layer is stored in
  `macro_regime_v2_smoothed_daily`, `macro_transition_v2_matrix`, `macro_transition_v2_diagnostics`,
  `macro_regime_v2_decision_daily`, `macro_regime_v2_promotion_evidence`, and
  `macro_regime_v2_promotion_summary`. It remains shadow-only while `portfolio_layer/config.yaml` has
  `macro.regime_source: v1`. Selecting `v2` fails closed unless the configured model has a current,
  sealed `PROMOTABLE` verdict for the same decision date whose artifacts and upstream validation files
  still match their hashes. Current-day probability confidence is diagnostic, not model-selection evidence;
  the decision layer's hysteresis carries the active regime when a promoted model is temporarily uncertain.
  Each candidate build also seals `macro_v2_vintage_gap_cells.csv`, `macro_v2_vintage_gap_inputs.csv`,
  `macro_v2_vintage_gap_summary.json`, and `macro_v2_vintage_gap_manifest.json` under the dated v2 output.
- The raw regime layer also lives in the serving DB and materializes `macro_regime_raw_daily`.
- The smoothed regime layer also lives in the serving DB and materializes `macro_regime_smoothed_daily`, `macro_transition_matrix`, and `macro_transition_diagnostics`.
- The Stage 8.5 decision overlay also lives in the serving DB and materializes `macro_regime_decision_daily`.
- The Stage 9 industry-first layer also lives in the serving DB and materializes `sector_macro_fit_daily`, `industry_aggregate_macro_fit_daily`, and `industry_macro_fit_daily`, while also exporting static prior files under `MacroLayer/out/industry_macro`.
- Stage 9 can source score snapshots from SEC resolved snapshots, BackTest history, canonical `tier1_optimizer_universe` snapshots under `output/`, or a `hybrid` mode that uses SEC/BackTest for historical continuity and canonical production snapshots for live dates.
- The Stage 9 acceptance check writes CSV diagnostics under `MacroLayer/out/industry_macro_checks`.
- The Stage 10 country layer also lives in the serving DB and materializes `country_macro_fit_daily`, `country_confidence_daily`, and `country_macro_rank_daily`, while exporting latest snapshots under `MacroLayer/out/country_macro`.
- The Stage 10 acceptance check writes CSV diagnostics under `MacroLayer/out/country_macro_checks`.
- The Stage 11 stock overlay also lives in the serving DB and materializes `stock_macro_fit_daily`, `stock_selection_score_daily`, and `stock_weight_score_daily`, while exporting latest snapshots under `MacroLayer/out/stock_macro_overlay`.
- The Stage 11 acceptance check writes CSV diagnostics under `MacroLayer/out/stock_macro_overlay_checks`.
- The Stage 12A portfolio input layer also lives in the serving DB and materializes `portfolio_inputs_daily` and `portfolio_allocation_summary`, while exporting macro-compatible latest optimizer CSVs under `MacroLayer/out/portfolio_inputs`.
- The Stage 12A acceptance check writes CSV diagnostics under `MacroLayer/out/portfolio_input_checks`.
- The Stage 12B stock sleeve target layer also lives in the serving DB and materializes `stock_industry_target_daily`, `stock_sector_target_daily`, and `stock_sleeve_target_summary`, while exporting latest target CSVs under `MacroLayer/out/stock_sleeve_targets`.
- The Stage 12B acceptance check writes CSV diagnostics under `MacroLayer/out/stock_sleeve_target_checks`.
- The Stage 12C foreign sleeve budget layer also lives in the serving DB and materializes `foreign_sleeve_budget_daily` and `foreign_sleeve_candidate_daily`, while exporting latest budget/candidate CSVs under `MacroLayer/out/foreign_sleeve_budget`.
- The Stage 12C acceptance check writes CSV diagnostics under `MacroLayer/out/foreign_sleeve_budget_checks`.
- The Stage 12D final optimizer integration runs the actual tier-1 optimizer into case-specific folders under `MacroLayer/out/final_optimizer`, with macro-on/off and foreign-sleeve ablations configured in `optimizer_integration_layer`.
- The Stage 12D acceptance check writes CSV diagnostics under `MacroLayer/out/final_optimizer/checks`.
- `run_macro_serving_pipeline.py` is the operational wrapper you use when you want the serving layers rebuilt as one consistent unit instead of running each builder manually.
- The default generated registry is a tier-1 runtime registry. Unresolved foreign rows are skipped rather than carried forward as disabled placeholders.
- A disabled registry row is still metadata-only: it is present in the inventory, but the runner will not fetch it because `enabled=0`.
