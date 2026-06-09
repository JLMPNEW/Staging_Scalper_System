# Technology Implementation Stages And Acceptance Tests

## Stage 0 - Architecture And Governance

Goal: create an independent technology package with clear boundaries.

Acceptance tests:

- `technology` imports without importing `med_devices`.
- Config resolves database, output, cache, and source-registry paths.
- No scripts write to external upstream DBs.

## Stage 1 - Database Foundation

Goal: create the shared technology SQLite foundation.

Acceptance tests:

- `technology/scripts/00_init_technology_db.py` creates a clean SQLite DB.
- Required tables exist: `runs`, `source_registry`, `ingestion_runs`, `raw_api_responses`, `dim_company`, `dim_security`, `dim_identifier`, `dim_company_alias`, `dim_technology_taxonomy`, and `data_quality_issues`.
- Source registry loads without duplicate `source_id` values.
- FDA/CMS/reimbursement/med-device-specific tables are absent.

## Stage 2 - Security Master And Universe

Goal: load semiconductor tickers as the first technology universe.

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
python technology\scripts\09_import_technology_positioning.py
python technology\scripts\10_validate_technology_sec_positioning_stages.py
```

Acceptance tests:

- Direct SEC Forms 3/4/5 checks write filing-level, transaction-level, and holding-level rows into `technology.sqlite`.
- `dim_insider_reporting_profile` separates domestic expected coverage, post-HFIA FPI expected coverage, and qualifying-exemption/local-source cases.
- Direct SEC Form 4 non-derivative transactions backfill `fact_sec_form4_transaction` with `source_id='sec_ownership_direct'`.
- Form 4 transactions are imported from `sec_insider.sqlite` without writing to the upstream database.
- Missing Form 4 coverage is classified as local-source needed, filings-found-without-transactions, parser issue, or expected-missing review instead of a generic missing row.
- 13F, FINRA short-interest, and IBKR borrow adapters read `market_positioning.sqlite` read-only when semiconductor rows are available.
- Missing upstream coverage is stored as data-quality issues.
- One current `feature_positioning` row exists for each active semiconductor ticker.
