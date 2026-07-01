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
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\09_sync_defense_yahoo_fx_rates.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\08_build_defense_financial_features.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\08_validate_defense_financial_stage.py
C:\Users\josel\miniconda3\python.exe industrials\defense\scripts\04b_validate_defense_stage0_4_production_readiness.py
```

Stage 3 generated review reports are intentionally limited to the configured `output/industrials/defense/stage3` coverage CSVs. Persistent market data and features are stored in the shared SQLite database.

Stage 4 generated review reports are limited to `output/industrials/defense/stage4` coverage CSVs. Persistent raw SEC facts, mapped facts, canonical financial facts, issuer reporting profiles, FX rates, and financial features are stored in the shared SQLite database.

`07_sync_defense_sec_fundamentals.py` attempts SEC archive XML/inline-XBRL extraction for rows explicitly marked `SEC_RAW_ARCHIVE_REQUIRED` when CompanyFacts is unavailable or empty. Rows with no modern submissions endpoint remain tagged as archive-required instead of being promoted silently.

Stage 6 scoring policy starts with `industrials/defense/system_csvs/defense_scoring_eligibility_policy.csv` and can be audited with `10_validate_defense_scoring_eligibility_policy.py`; weak-data profiles must be explicitly governed before rank-ready promotion.

`04b_validate_defense_stage0_4_production_readiness.py` is the hard pre-Stage 5 gate. It validates the configured production DB in read-only mode and fails if scratch DB evidence is being substituted for production data.
