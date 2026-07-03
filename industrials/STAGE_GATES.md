# Industrials Stage Gates

## Stage 0-1 Foundation

- `industrials` imports independently.
- `industrials/config.yaml` resolves the database, output, cache, and source-registry paths.
- `00_init_industrials_db.py` creates the shared industrials schema idempotently.
- Source registry rows load without duplicate `source_id` values.

## Stage 2 Defense Universe

- `defense_tickers.csv` loads as exactly 94 active defense tickers.
- Active ticker rows create matching company, security, taxonomy, identifier, and current membership rows.
- `aerospace_defense_delisted.csv` loads as the delisted calibration seed.
- Reused delisted tickers are internalized so they cannot overwrite active issuers.
- `defense_ticker_aliases.csv` can be empty, but any populated row must include an effective date and verified status.
- Universe validation passes before market data ingestion.
- Identity reconciliation passes before market data ingestion.

## Stage 3 Defense Market Data

- Yahoo adjusted OHLCV sync covers the 94 active defense tickers plus `XAR`, `ITA`, and `SPY`.
- Effective ticker aliases route market-data fetch symbols while preserving the defense contract ticker for joins.
- Adjusted close and explicit price-adjustment status are present for every scoring bar.
- Market snapshots cover every active defense ticker and benchmark.
- Any out-of-band price loader, including future Norgate paths, must populate `fact_market_snapshot` or an equivalent validated snapshot row for every scored ticker and benchmark.
- `15_import_defense_norgate_delisted_prices.py` imports delisted calibration prices under `source_id=norgate_us_equities_total_return` using internal delisted tickers, and writes one coverage CSV to `output/industrials/defense/stage3`.
- `15_import_defense_norgate_delisted_prices.py --dry-run` must be read-only against SQLite; it may write only its coverage report.
- Delisted Norgate `adj_close` must use the configured `norgate_delisted_import.price_adjustment_mode` and store that mode in `price_adjustment`; `CAPITAL` is split/capital-action adjusted and excludes ordinary-dividend total return, so total-return and price-return bases must not be mixed silently in calibration features.
- Reviewed Norgate lineage exceptions must live in `industrials/defense/system_csvs/defense_norgate_symbol_overrides.csv`; missing override symbols are treated as explicit unresolved rows, not inferred silently.
- Market feature rows exist for all 94 active defense tickers for the requested as-of date.
- Stale, future-dated, missing, unadjusted, low-history, and low-liquidity cases are surfaced through validation or data-quality issues.
- If `--asof` is omitted, audit and validation use the loaded panel date instead of the wall-clock date so historical replay panels can be validated without false staleness failures.
- Low-history rows are review-only unless validation is run with `--strict-history`.
- Stage 3 validation passes before financial, positioning, sector-cycle, scoring, or dashboard stages run.

## Stage 4 Defense Financials

- SEC submissions and companyfacts sources are registered as active in `source_registry`.
- Raw companyfacts payloads are stored before mapped facts, and mapped facts retain `raw_fact_id` lineage.
- `dim_xbrl_concept_map` is seeded with US GAAP and IFRS concepts needed for revenue, profitability, cash flow, balance sheet, dilution, debt, contract liabilities, and remaining performance obligations.
- `fact_sec_filing` stores accession number, form type, filing date, accepted filing time, report period, primary document, and filing URL for PIT filtering.
- `fact_financial_statement_canonical` contains only facts whose accepted filing date and period end are valid for the requested as-of date.
- Canonical projection is priority-aware: when multiple XBRL concepts map to the same metric/period/accession/unit, the lowest `source_priority` concept wins and the selected `concept_name` is retained for audit.
- `feature_financial_statement` has one row for every active defense ticker for the requested as-of date.
- `*_ttm` feature columns are populated only from four actual quarterly/interim periods; annual values are not copied into TTM columns.
- `book_to_bill` and `funded_backlog` remain null until true bookings and funded-backlog source data are available; RPO is published only as `remaining_performance_obligation`.
- Foreign issuers and issuers without usable SEC XBRL are classified explicitly through `dim_issuer_reporting_profile`; they receive neutral-low-confidence financial feature rows unless a future vendor-fundamental fallback supplies usable data.
- `SEC_RAW_ARCHIVE_REQUIRED` rows attempt SEC archive XML/inline-XBRL extraction when modern CompanyFacts is unavailable or empty; if no modern submissions index is available, they remain explicit archive-required review rows.
- Non-USD statements must either have an available FX rate in `fact_fx_rate` or remain in review with `fx_conversion_status=missing_fx_rate`.
- `09_sync_defense_yahoo_fx_rates.py` is the default Stage 4 FX loader and must populate `fact_fx_rate` before non-USD financial rows are promoted from FX review.
- Complete financial rows must include core USD fields such as `revenue_usd` and `assets_usd`; incomplete rows are review rows with data-quality issues.
- Stage 4 validation passes before positioning, sector-cycle, scoring, calibration, dashboard publishing, or portfolio-layer handoff stages run.

## Stage 6 Scoring Policy

- `defense_scoring_eligibility_policy.csv` must include a policy row for every reporting profile and lifecycle-stage combination present in active defense rows.
- Recent IPO/development-stage rows, parent-segment rows, non-filing rows, raw-archive-required rows, and partial-XBRL rows cannot become rank-ready unless their explicit policy gate allows it.
- `10_validate_defense_scoring_eligibility_policy.py` must pass before a Stage 6 scorer promotes rank-ready or calibration-eligible rows.

## Pre-Stage 5 Production Readiness

- `04b_validate_defense_stage0_4_production_readiness.py` must be run against the configured production `industrials.sqlite`, not a scratch DB.
- The readiness validator opens the database read-only and must not create or mutate production data.
- The gate fails when the production DB is missing, required Stage 0-4 tables are missing, or active/delisted ticker identity rows are incomplete.
- The gate requires 94 active defense tickers, the full delisted calibration seed, active current membership rows, delisted non-current membership rows, and loaded ticker aliases.
- The gate requires active Stage 3 Yahoo price, market snapshot, and market feature coverage for active tickers plus configured benchmarks.
- The gate requires Stage 4 issuer reporting profiles and financial feature rows for all active defense tickers.
- Live SEC fact coverage is reported; it can be warning-only when fallback financial rows are intentionally allowed, or blocking when the validator is run with `--require-live-sec-facts`.
- Delisted Norgate price coverage must pass before true OOS calibration promotion; the readiness validator can enforce it with `--require-delisted-price-history`.
- Stage 5 positioning work should not be promoted as production-ready until this gate passes.
