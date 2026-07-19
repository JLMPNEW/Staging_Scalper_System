# Industrials Pipeline

This package owns shared infrastructure for industrials-sector scoring pipelines.
The first implemented subsector is `defense`.

Key boundaries:

- Shared industrials infrastructure lives in `industrials/core` and `industrials/scripts`.
- Defense-specific system CSVs live in `industrials/defense/system_csvs`.
- Defense-specific scripts live in `industrials/defense/scripts`.
- Generated dashboard output belongs under `output/industrials/defense/dashboard`.
- The package should not import implementation modules from `technology`, `biotech_index`, or `med_devices`.

Initial Stage 0-4 command order:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\scripts\00_init_industrials_db.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\01_load_defense_universe.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\01b_load_defense_historical_membership.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\01c_load_defense_ticker_aliases.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\02_validate_defense_universe.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\02b_validate_defense_identity_reconciliation.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\03_sync_defense_prices.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\04_audit_defense_market_data_policy.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\05_build_defense_market_features.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\06_validate_defense_market_stage.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\15_import_defense_norgate_delisted_prices.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\07_sync_defense_sec_fundamentals.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\11_sync_defense_yahoo_fx_rates.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\08_build_defense_financial_features.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\08_validate_defense_financial_stage.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\09_evaluate_defense_profile_graduation.py --asof YYYY-MM-DD
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\04b_validate_defense_stage0_4_production_readiness.py
```

Stage 3 generated review reports are intentionally limited to the configured `output/industrials/defense/stage3` coverage CSVs. Persistent market data and features are stored in the shared SQLite database.

Stage 4 generated review reports are limited to `output/industrials/defense/stage4` coverage CSVs. Persistent raw SEC facts, mapped facts, canonical financial facts, issuer reporting profiles, FX rates, and financial features are stored in the shared SQLite database.

`07_sync_defense_sec_fundamentals.py` attempts SEC archive XML/inline-XBRL extraction for rows explicitly marked `SEC_RAW_ARCHIVE_REQUIRED` when CompanyFacts is unavailable or empty. Rows with no modern submissions endpoint remain tagged as archive-required instead of being promoted silently.

Stage 4.5 evaluates `RECENT_IPO_DEVELOPMENT_STAGE` and `RECENT_PUBLIC_STUB` rows against periodic XBRL facts plus, when applicable, a certified audited predecessor bridge. It requires the configured market-history minimum, a current annual baseline, aligned annual-plus-interim TTM cash-flow windows, required statement components, and PIT FX coverage. Audit mode never changes a profile. Applying an eligible decision requires `--apply --effective-date YYYY-MM-DD`, where the effective date must be later than the evidence `--asof`; decisions are appended to `defense_reporting_profile_graduations.csv` and never rewrite the source override or an existing dated dashboard snapshot.

For de-SPAC issuers, Stage 4.5 may use `SEC_XBRL_US_GAAP_DESPAC_BRIDGE` only when audited predecessor facts come from a formal historical statement in an SEC registration filing. Pro-forma/projected tables, fallback-date rows, uncertain scales/currencies, and conflicting duplicate values are rejected. Expensed R&D remains in operating cash flow and is never reclassified as capex. Reparse one controlling cached accession with `08b_refresh_defense_predecessor_bridge.py --ticker TICKER --accession ACCESSION --asof YYYY-MM-DD`; the normal full archive sweep is not required.

Stage 6 scoring policy starts with `industrials/defense/system_csvs/defense_scoring_eligibility_policy.csv` and can be audited with `10_validate_defense_scoring_eligibility_policy.py`; weak-data profiles must be explicitly governed before rank-ready promotion.

Current defense daily refresh and shadow-publish command:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\16_run_defense_daily_refresh.py --asof 2026-07-02
```

The daily runner uses the fast path: incremental SEC sync, daily positioning refresh, Stage 3-6 validators, shadow rank-table publish, rank-table contract validation, and portfolio `tech_family` adapter shadow validation. It is the default command for refreshing an already-built defense database for a specific market date.

Stage 6 defense dashboard output is intentionally shadow-only until true PIT/OOS calibration is implemented. The publisher writes one immutable dated artifact:

```text
output/industrials/defense/dashboard/YYYY-MM-DD/defense_final_rank_table.csv
output/industrials/defense/dashboard/YYYY-MM-DD/defense_final_rank_table_manifest.json
```

The publisher refuses to overwrite an existing valid dated artifact unless `--allow-overwrite` is passed explicitly. Build or validate loaded PIT shadow snapshots with:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\19_build_defense_shadow_snapshot_history.py --start-date 2026-07-02 --end-date 2026-07-02
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\20_validate_defense_portfolio_adapter_shadow.py --asof 2026-07-02
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\21_validate_defense_oos_calibration_readiness.py --asof 2026-07-02
```

`21_validate_defense_oos_calibration_readiness.py` is report-only by default. It validates manifest hashes, contract schema, 0-100 native score units, shadow-only gates, point-in-time source dates, benchmark pins, and portfolio-adapter shadow ingestion. Use `--promotion-check` only when enough PIT history exists and the run should fail if the configured minimum snapshot count is not met.

Build the report-only Stage 8/9 research artifacts with:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\26_run_defense_weekly_calibration_research.py --start-date 2026-01-04
```

The weekly runner anchors buckets on `2026-01-04` and selects the last available market-date snapshot in each weekly bucket. It seals the calibration panel, split metadata, optional Optuna calibration report, and score backtest diagnostics under `output/industrials/defense/stage8/*_weekly` and `output/industrials/defense/stage9/*_weekly`. These artifacts remain research-only until the panel is survivorship-corrected, contains enough immutable PIT snapshots, has forward returns available, and passes `23_validate_defense_oos_calibration_artifacts.py --promotion-check`.

Defense is registered in `portfolio_layer/config.yaml` as a SHADOW sleeve (2026-07-04): `enabled: true, required: false, require_oos_score_valid: true`, so every defense row enters the Stage 1 contract with `investable_eligible=0` (`shadow_only_oos_calibration_not_available`) and the optimizer never sizes it. Supporting wiring is in place (XAR/ITA benchmarks, `sector_etf_map`/`sector_factor_etfs`/`sleeve_taxonomy` defense entries, `strategic_sector_weights.defense: 0.00`, PLTR/OSIS canonical ownership overrides). The registration preconditions were verified on the 2026-07-02 run: immutable snapshot history, OOS calibration validators, duplicate-ticker overrides, and a passing 6-sector portfolio Stage 1 collect/calibrate/validate. Promotion to a sized sleeve still requires Stage 8 OOS calibration to flip `calibration_eligible_flag`/`oos_score_valid_flag` in the rank table.

`04b_validate_defense_stage0_4_production_readiness.py` is the hard pre-Stage 5 gate. It validates the configured production DB in read-only mode and fails if scratch DB evidence is being substituted for production data.
