# Technology Scoring Model

This package is the independent implementation namespace for technology-sector stock scoring. It starts with semiconductors, but the shared `technology` infrastructure is intended to support additional technology subsectors without creating a new permanent database for each subsector.

## Independence Rules

- Do not call or import `med_devices` scripts from this package.
- Do not write to biotech, med-device, Form 4, or market-positioning databases.
- Default database: `${TECHNOLOGY_DB_DIR:-C:/Users/josel/Documents/STAGING/DB}/technology.sqlite`.
- Default outputs: `output/technology_reports` and `output/technology_cache`.
- External data products such as `sec_insider.sqlite` and `market_positioning.sqlite` are read-only upstream sources. Technology adapters import filtered, normalized rows into technology-owned tables.

## Initial Stage

Initialize the database:

```powershell
python technology\scripts\00_init_technology_db.py
```

Use a scratch database for smoke tests:

```powershell
python technology\scripts\00_init_technology_db.py --db C:\tmp\technology.sqlite
```

The first universe load stage uses `ticker_mapping/semiconductor_tickers.csv` as the authoritative semiconductor ticker universe. The current semiconductor source-of-truth universe is expected to contain exactly 99 unique tickers.

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
python technology\scripts\09_import_technology_positioning.py
python technology\scripts\10_validate_technology_sec_positioning_stages.py
```

Direct SEC ownership ingestion reads Forms 3/4/5 XML from EDGAR into technology-owned filing, transaction, holding, and insider-reporting-profile tables. It also backfills direct Form 4 non-derivative transactions into `fact_sec_form4_transaction` under `source_id='sec_ownership_direct'` so the existing positioning feature builder can use them.

The positioning adapter never writes to `sec_insider.sqlite` or `market_positioning.sqlite`; it only normalizes available rows into `technology.sqlite`. Direct SEC ownership is the preferred diagnostic source when the upstream Form 4 database has gaps.
