# Medical Devices Implementation Stages And Acceptance Tests

This plan keeps the medical-devices model independent from the biotech model. Biotech code, tables, reports, and config remain untouched unless a future shared read-only utility is explicitly introduced.

## Stage 0 - Mandate, Universe, And Benchmarks

Goal: define what the model is allowed to score.

Build:

- Medtech universe policy: U.S.-listed first, ADR/global optional later.
- Inclusion/exclusion rules for device, diagnostics equipment, robotic surgery, diabetes tech, orthopedics, cardiovascular, neuro, monitoring, imaging, wound care, and hospital equipment names.
- Liquidity and market-cap thresholds.
- Subsector taxonomy.
- Benchmark set, initially `IHI` and `XHE`.
- Manual include/exclude/review files.

Acceptance tests:

- Written universe policy exists in `med_devices/config.yaml` or a tracked policy file.
- Every included ticker has ticker, company name, exchange, country, currency, CIK if available, and subsector.
- Pure biotech/pharma names are excluded or explicitly marked `manual_review`.
- Benchmark tickers resolve in the market-data layer.
- A stage output CSV can be regenerated deterministically from the same inputs.

## Stage 1 - Data Engineering, Source Registry, And Database Build

Goal: build all med-devices database foundations.

This is the stage that builds the databases.

Build:

- Independent SQLite DB, defaulting to `med_devices.sqlite`.
- Source registry.
- Raw response table.
- Ingestion run metadata.
- Canonical dimensions and fact tables.
- Feature table shells.
- Data-quality issue table.
- Init script and smoke tests.

Acceptance tests:

- `med_devices/scripts/00_init_med_devices_db.py` creates a clean DB from scratch.
- `source_registry` loads without duplicate `source_id` values.
- Required tables exist: `dim_company`, `dim_security`, `dim_identifier`, `fact_price_ohlcv`, `fact_financial_statement`, `fact_fda_approval`, `fact_fda_recall`, `fact_fda_adverse_event`, `fact_reimbursement_policy`, and `med_device_daily_scores`.
- Biotech-specific tables such as `daily_scores` and `trials` are absent from the med-devices DB.
- Raw records carry `source_id`, request timestamp, response hash, and ingestion run id.
- Unit test: `python -m pytest tests/med_devices -q`.

## Stage 2 - Security Master And Company Master

Goal: create a stable public-company identity layer.

Build:

- `ticker_mapping/med_dev_tickers_clean_keep.csv` seed-file load.
- `med_devices/scripts/01_load_med_device_universe.py` loader.
- SEC `company_tickers.json` ingestion.
- SEC submissions ingestion.
- Nasdaq Trader listed/other-listed symbol ingestion.
- Company/security upsert logic.
- Company aliases and CIK/ticker mapping.
- Initial medtech subsector classification.

Acceptance tests:

- At least 95% of active scored tickers have ticker, company name, exchange, and listing status.
- At least 90% of U.S. filers have a valid 10-digit CIK.
- Duplicate tickers and duplicate CIK mappings are flagged.
- Delisted, test, ETF, fund, and non-primary listings are excluded unless manually allowed.
- Manual override rows win over automated classification and are auditable.

## Stage 3 - Market Data: Adjusted Historical Primary, IB Live Validation

Goal: load adjusted daily OHLCV and market-derived features without relying on Stooq or Alpha Vantage.

Build:

- Yahoo Finance adjusted market-data sync for historical scoring and backtesting.
- IB market-data validation for active trading status, contract resolution, and fallback bars.
- `med_devices/scripts/04_sync_med_device_yahoo_adjusted_prices.py` for Yahoo adjusted scoring/calibration bars.
- `med_devices/scripts/03_audit_med_device_market_data_policy.py` for selected-source policy validation.
- Price source priority policy: adjusted historical source first, IB fallback/live validation.
- Corporate-action fields: raw close, adjusted close, dividend amount, split factor when available.
- Continuity checks and stale-data detection.
- Benchmark bars for `IHI`, `XHE`, and any configured market benchmark.

Acceptance tests:

- Stooq and Alpha Vantage are not configured as med-devices market-data sources.
- Adjusted historical bars populate `fact_price_ohlcv` with the configured primary scoring source.
- IB bars populate `fact_price_ohlcv` with `source_id='ib_market_data'` only as fallback/live validation data.
- Every active universe ticker has at least 252 trading days of bars or a documented low-history reason.
- Latest selected scoring bar date is no more than the configured staleness threshold behind the market as-of date.
- Split/dividend adjustment fields are present, or the row is marked with an explicit adjustment-quality status.
- Price continuity report has no unexplained missing trading days above threshold.
- Technical features can be computed for benchmark tickers and at least 95% of active universe tickers.

## Stage 4 - SEC Fundamentals And Filing Events

Goal: build point-in-time financial facts and SEC event context.

Build:

- `med_devices/scripts/05_sync_med_device_sec_fundamentals.py` for canonical SEC submissions and companyfacts loading.
- SEC companyfacts ingestion.
- SEC filing metadata ingestion from submissions.
- 10-K, 10-Q, 8-K, 20-F, 6-K document sync where needed.
- Quarterly/TTM financial statement builder.
- Filing-date-aware availability logic.

Acceptance tests:

- At least 90% of active U.S. filers have recent financial statement rows.
- Financial rows use filing dates for as-of availability, not only period-end dates.
- Revenue, gross profit, operating income, net income, operating cash flow, capex, debt, cash, and shares have documented concept mappings.
- Missing concepts are recorded in data-quality issues rather than silently filled.
- TTM calculations are reproducible and do not use future filings.

## Stage 5 - FDA Core Device Data

Goal: ingest official U.S. device approval, classification, recall, and MAUDE data.

Build:

- openFDA 510(k), PMA, classification, recall, enforcement, MAUDE, registration/listing, and UDI ingestion.
- `med_devices/scripts/08_sync_med_device_fda_core.py` for the first openFDA core ingestion pass.
- Product-code dimension.
- Manufacturer/sponsor staging tables.
- Recall severity normalization.
- MAUDE event severity fields.

Acceptance tests:

- Core openFDA endpoints ingest into raw and canonical tables.
- 510(k), PMA, recall, and MAUDE rows preserve source payloads.
- Product codes map to `dim_fda_product_code`.
- Recall class is normalized to a severity weight.
- MAUDE death, injury, and malfunction indicators are parsed.
- Event dates and report/classification dates are stored separately.

## Stage 6 - Device Identity And Entity Resolution

Goal: connect FDA entities and device/product records to public companies.

Build:

- AccessGUDID ingestion.
- UDI/device identifier dimension.
- Public parent, subsidiary, manufacturer, sponsor, brand, product-code, and device mapping tables.
- `med_devices/scripts/09_link_med_device_fda_to_companies.py` for confidence-scored FDA manufacturer/sponsor mapping.
- Confidence scoring and manual override support.

Acceptance tests:

- Top revenue-weighted medtech companies have high-confidence FDA manufacturer/sponsor mappings.
- FDA manufacturer rows are not force-mapped when confidence is below threshold.
- AccessGUDID device records can map to product code and company where evidence exists.
- M&A/subsidiary aliases are supported.
- Manual review queue exists for ambiguous mappings.
- Mapping confidence is available to every downstream FDA feature.

## Stage 7 - FDA Risk And Product-Cycle Features

Goal: turn FDA data into medtech-specific innovation and risk signals.

Build:

- Regulatory innovation score: 510(k), PMA, De Novo where available, supplements, product-code expansion, and approval cadence.
- Regulatory risk score: recalls, MAUDE acceleration, warning letters, OAI/VAI/NAI inspection classifications, compliance actions.
- `med_devices/scripts/10_build_med_device_fda_features.py` for the first FDA/product risk feature rows.
- Product-code peer normalization.
- Hard red flags for core-product Class I recalls, unresolved warning letters, and OAI inspections.

Acceptance tests:

- Raw MAUDE counts are never used without normalization.
- Recall features are severity-weighted.
- Product-code peer groups exist for normalization.
- Recent Class I recall and OAI inspection examples generate risk flags.
- FDA score reason codes identify the records driving the score.
- Analyst review is required for top-ranked companies with material FDA red flags.

## Stage 8 - Reimbursement And Market Access

Goal: score whether products can be paid for and adopted economically.

Build:

- CMS Coverage API ingestion.
- CMS MCD downloads where useful.
- HCPCS quarterly files.
- DMEPOS fee schedule.
- OPPS and IPPS payment files.
- `med_devices/scripts/11_build_med_device_reimbursement_features.py` for conservative reimbursement feature rows from loaded CMS/mapping evidence.
- Product-to-code mapping.
- Coverage clarity and payment adequacy features.

Acceptance tests:

- HCPCS/CPT-like codes are stored in `dim_reimbursement_code` where available.
- NCD/LCD/article records retain effective dates and status.
- Major product lines are mapped to reimbursement codes or explicitly marked `not_applicable` / `unknown`.
- Restrictive or absent coverage generates negative reason codes.
- Reimbursement score is blocked from high confidence when mapping confidence is low.

## Stage 9 - Product Pipeline, Patents, And Durable Growth

Goal: capture device pipeline, installed-base durability, IP strength, and growth quality.

Build:

- ClinicalTrials.gov API v2 device-study ingestion.
- Sponsor/intervention mapping.
- PatentsView or USPTO patent ingestion.
- Product-cycle and patent-velocity features.
- Installed-base/consumables indicators from filings where available.

Acceptance tests:

- Device trials are separable from drug/biologic trials.
- Trial sponsor mapping uses company aliases and manual overrides.
- Trial status, enrollment, primary completion date, and last update date are stored.
- Patent assignee mapping has confidence scores.
- Durable growth score separates organic growth from acquisition-driven growth where disclosures allow.

## Stage 10 - Data Quality And Point-In-Time Controls

Goal: prevent leakage, bad mappings, stale data, and silent failure.

Build:

- Missingness checks.
- Duplicate checks.
- Outlier checks for price, financial, FDA, and reimbursement data.
- As-of availability policy.
- Mapping-confidence quality gates.
- Pipeline freshness checks.

Acceptance tests:

- Every feature row has an `asof_date`.
- Financial features use only filings available by the as-of date.
- Market features use only bars on or before the as-of date.
- FDA/reimbursement records distinguish event date from report/publication date.
- Data-quality issues are written for stale, missing, duplicate, and low-confidence records.
- Any failed critical source marks the run partial or failed, not successful.

## Stage 11 - Feature Builders

Goal: build independent medtech features.

Build:

- Fundamental quality.
- Durable growth.
- FDA/product risk.
- Reimbursement.
- Valuation.
- Technical entry.
- Sentiment/catalyst proxy.

Acceptance tests:

- Each feature builder writes one row per active scored company per as-of date, or writes a documented missingness issue.
- Scores are bounded 0-100.
- Feature payload JSON includes reason codes and source row counts.
- Missing values use documented priors or quality penalties.
- No feature imports biotech scoring weights or writes biotech output tables.

## Stage 12 - Composite Score, Ranking, And Gates

Goal: combine separate sleeves into a transparent ranking.

Build:

- Composite scoring with configured weights.
- Hard disqualifier logic.
- Bucket/classification rules.
- Positive and negative reason codes.
- Rank persistence.

Acceptance tests:

- Composite score equals configured weighted subscores after documented penalties.
- Minimum gates are enforced: composite, fundamental quality, FDA/product, reimbursement, valuation, and technical entry.
- Hard red flags can force `avoid` or `event_driven_only`.
- Top-ranked names have visible reason codes.
- Ranking is stable and reproducible for the same as-of inputs.

## Stage 13 - Reports And Diagnostics

Goal: make rankings auditable and usable.

Build:

- Daily score CSV.
- Top candidates.
- Watchlist.
- Avoid/red-flag report.
- FDA event summary.
- Reimbursement summary.
- Market-data fallback diagnostics.
- Data-quality report.

Acceptance tests:

- Report rows match DB score rows for the as-of date.
- Every report has `asof_date`, `ticker`, `company_id`, composite score, rank, classification, and source freshness fields.
- IB/Yahoo fallback usage is visible in diagnostics.
- Top candidates report excludes hard-red-flag names unless explicitly marked event-driven.
- Output paths are under `output/med_devices_reports`, not biotech report folders.

## Stage 14 - Backtesting And Calibration

Goal: validate whether rankings have historical value.

Build:

- Historical point-in-time score snapshots.
- Forward return builder from IB/Yahoo adjusted bars.
- Transaction-cost and liquidity model.
- Rank IC and portfolio simulations.
- Weight perturbation and ablation tests.

Acceptance tests:

- Backtests use next-bar entry and no future features.
- Results include top-quintile, top-decile, benchmark-relative, and ablation runs.
- Transaction costs and liquidity limits are applied.
- No single ticker or period explains most of the result.
- Model passes pre-defined minimum thresholds before any production allocation workflow.

## Stage 15 - Production Refresh Pipeline

Goal: run the complete model reliably.

Build:

- Orchestrator script.
- Step-level timing.
- Partial-run handling.
- Freshness preflight for IB, Yahoo fallback, SEC, FDA, CMS.
- Final validation.
- Snapshot output copies.

Acceptance tests:

- Full daily pipeline can run from clean inputs for a chosen as-of date.
- Failed critical steps stop downstream scoring.
- Noncritical fallback use is logged and visible.
- Final validation checks row coverage, required columns, source freshness, and report generation.
- Re-running the same as-of date is idempotent.

## Stage 16 - Governance, Analyst Review, And Portfolio Workflow

Goal: make the model decision-support ready.

Build:

- Manual override log.
- Analyst review queue.
- Investment memo generator.
- Position eligibility gates.
- Kill criteria and event-risk calendar.
- Model-change versioning.

Acceptance tests:

- Manual overrides include reason, owner, timestamp, active flag, and expiration/review date.
- Top candidates with FDA/reimbursement red flags require analyst approval.
- Start-position rule is reproducible from stored scores and flags.
- Model weight/config changes are version-controlled.
- Live score changes and manual overrides are auditable.
