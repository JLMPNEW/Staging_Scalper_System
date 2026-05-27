# Medical Devices Scoring Model

This package is the independent implementation namespace for medical-device stock scoring. It can reuse broad infrastructure patterns from the biotech model, but it owns separate config, SQLite tables, source registry rows, feature builders, scores, reports, and output paths.

## Independence Rules

- Do not write to `biotech_index` tables, output folders, or scoring config.
- Default database: `${MED_DEVICES_DB_DIR:-C:/Users/josel/Documents/STAGING/DB}/med_devices.sqlite`.
- Default outputs: `output/med_devices_reports` and `output/med_devices_cache`.
- Medical-device scores use `med_device_daily_scores`, not biotech `daily_scores`.
- Shared upstream datasets can be reused only through explicit source ingestion or read-only external DB paths.

## Stage Ownership

Stage 1 is the database-build stage.

It owns:

- source registry
- raw response storage
- ingestion run metadata
- canonical dimensions and facts
- data-quality issue tables
- initial feature table shells

Stages 2 and later build on top of Stage 1:

- Stage 2: entity resolution across tickers, CIKs, FDA sponsors/manufacturers, UDI/device records, and reimbursement codes
- Stage 3: point-in-time controls and data-quality gates
- Stage 4: fundamental quality
- Stage 5: durable growth and product-cycle signals
- Stage 6: FDA/product risk
- Stage 7: reimbursement and market access
- Stage 8: valuation
- Stage 9: technical entry
- Stage 10: sentiment, policy, and catalyst risk
- Stage 11: composite score and ranking

## Initial Free Sources

The first source registry is tracked at `data/free_source_registry.yaml`. It starts with free sources only:

| Source | Use |
| --- | --- |
| SEC company tickers, submissions, companyfacts | ticker/CIK mapping, filings, fundamentals |
| Nasdaq Trader symbol directory | active listings and listing metadata |
| Yahoo Finance adjusted market data | primary adjusted OHLCV for historical scoring, calibration, and backtesting |
| Interactive Brokers | live trading validation, contract resolution, and fallback OHLCV when adjusted Yahoo rows are unavailable |
| openFDA device APIs | 510(k), PMA, classification, recalls, MAUDE, registration/listing, UDI |
| AccessGUDID | UDI/device identity and product mapping |
| FDA Data Dashboard | inspections and compliance actions |
| CMS Coverage API and CMS payment files | NCD/LCD/articles, HCPCS, DMEPOS, OPPS, IPPS |
| ClinicalTrials.gov API v2 | device studies and product pipeline |
| PatentsView | IP moat and R&D productivity proxies |
| Federal Register / Regulations.gov | FDA/CMS policy events and comment windows |
| FINRA short interest | positioning proxy |
| FRED | macro and discount-rate inputs |
| GDELT | free news/event monitoring |

Market-data note: adjusted historical scoring and calibration should use the configured `market_data_policy` order, currently Yahoo adjusted first and IB fallback. IB remains the live validation source and requires an IBKR account, a running TWS or IB Gateway session, and the appropriate market-data permissions. Any fallback to raw or nonadjusted bars must be visible in diagnostics before scores are trusted.

Free-source gaps to account for later: institutional-quality point-in-time analyst estimates, consensus revisions, full survivorship-free price history, borrow costs, options surfaces, procedure-volume datasets, and complete commercial payer coverage.

## Stage Gates

The full implementation stage plan and acceptance tests are in `STAGE_GATES.md`.

## Initialize The Database

Use a sandbox or staging DB path for smoke tests:

```powershell
python med_devices\scripts\00_init_med_devices_db.py --db C:\tmp\med_devices.sqlite
```

Production runs should use the configured `MED_DEVICES_DB_DIR` path.

Load the initial ticker universe after DB initialization:

```powershell
python med_devices\scripts\01_load_med_device_universe.py
```

The default seed file is configured as `../ticker_mapping/med_dev_tickers_clean_keep.csv`.

Load Yahoo adjusted historical prices for scoring/calibration:

```powershell
python med_devices\scripts\04_sync_med_device_yahoo_adjusted_prices.py
python med_devices\scripts\03_audit_med_device_market_data_policy.py
```

The Yahoo sync writes `source_id = yahoo_finance_backup` into `fact_price_ohlcv`. IB remains the live-validation and fallback source under the configured market-data policy.
Yahoo fetches are bounded by `yahoo_price_ingestion.parallel_workers`; database writes remain serialized.

Load SEC submissions and companyfacts into canonical filing/financial tables:

```powershell
python med_devices\scripts\05_sync_med_device_sec_fundamentals.py
```

The SEC sync writes `fact_sec_filing`, `fact_financial_statement`, and raw SEC responses for the active med-devices universe.

Build and publish the Stage 4 financial/valuation baseline:

```powershell
python med_devices\scripts\06_build_med_device_financial_features.py
python med_devices\scripts\07_publish_med_device_financial_baseline_qa.py
```

Load and score the first FDA/reimbursement layers:

```powershell
python med_devices\scripts\08_sync_med_device_fda_core.py --allow-partial
python med_devices\scripts\09_link_med_device_fda_to_companies.py
python med_devices\scripts\10_build_med_device_fda_features.py
python med_devices\scripts\14_sync_med_device_cms_reimbursement.py --allow-partial
python med_devices\scripts\15_link_med_device_reimbursement_to_companies.py
python med_devices\scripts\11_build_med_device_reimbursement_features.py
python med_devices\scripts\12_build_med_device_technical_features.py
python med_devices\scripts\13_build_med_device_daily_scores.py
```

The FDA sync writes official openFDA approvals, product classifications, recalls, enforcement rows, and MAUDE adverse events into the med-devices namespace. The FDA linker maps FDA manufacturers/sponsors to public companies only when the configured confidence threshold is met. The CMS reimbursement sync loads Coverage API policy rows, LCD/article HCPCS detail rows, and configured CMS payment CSV/ZIP files, including the current DMEPOS payment ZIP configured under `cms_reimbursement_ingestion.payment_files`. The reimbursement linker materializes company-policy and company-HCPCS mappings before the reimbursement feature builder scores coverage/payment evidence.
openFDA page fetches are bounded by `fda_core_ingestion.parallel_workers`; raw and canonical SQLite writes remain serialized.
CMS detail HCPCS fetches are bounded by `cms_reimbursement_ingestion.detail_parallel_workers`; payment-file ZIP downloads are cached at the configured `path`, and SQLite writes remain serialized.
Reimbursement mapping uses policy-text alias matches, FDA device-name descriptor matches against HCPCS descriptions, and optional manual mappings in `med_devices/data/reimbursement_mapping_overrides.csv`. CLFS and ASC payment-file entries are present but disabled because the CMS downloads route through the CMS/AMA license workflow.
The final daily scoring script writes the multi-sleeve composite into `med_device_daily_scores`.
