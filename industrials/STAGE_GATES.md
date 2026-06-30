# Industrials Stage Gates

## Stage 0-1 Foundation

- `industrials` imports independently.
- `industrials/config.yaml` resolves the database, output, cache, and source-registry paths.
- `00_init_industrials_db.py` creates the shared industrials schema idempotently.
- Source registry rows load without duplicate `source_id` values.

## Stage 2 Defense Universe

- `defense_tickers.csv` loads as exactly 95 active defense tickers.
- Active ticker rows create matching company, security, taxonomy, identifier, and current membership rows.
- `aerospace_defense_delisted.csv` loads as the delisted calibration seed.
- Reused delisted tickers are internalized so they cannot overwrite active issuers.
- `defense_ticker_aliases.csv` can be empty, but any populated row must include an effective date and verified status.
- Universe validation passes before market data ingestion.
- Identity reconciliation passes before market data ingestion.

## Stage 3 Defense Market Data

- Yahoo adjusted OHLCV sync covers the 95 active defense tickers plus `XAR`, `ITA`, and `SPY`.
- Effective ticker aliases route market-data fetch symbols while preserving the defense contract ticker for joins.
- Adjusted close and explicit price-adjustment status are present for every scoring bar.
- Market snapshots cover every active defense ticker and benchmark.
- Any out-of-band price loader, including future Norgate paths, must populate `fact_market_snapshot` or an equivalent validated snapshot row for every scored ticker and benchmark.
- Market feature rows exist for all 95 active defense tickers for the requested as-of date.
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
- Non-USD statements must either have an available FX rate in `fact_fx_rate` or remain in review with `fx_conversion_status=missing_fx_rate`.
- Complete financial rows must include core USD fields such as `revenue_usd` and `assets_usd`; incomplete rows are review rows with data-quality issues.
- Stage 4 validation passes before positioning, sector-cycle, scoring, calibration, dashboard publishing, or portfolio-layer handoff stages run.
