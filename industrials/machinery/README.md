# Machinery Pipeline

The stage-by-stage implementation status, special financial metrics, and
acceptance gates are maintained in `IMPLEMENTATION_STATUS.md`.

This package owns machinery-specific universe, identity, scoring, ranking, and
portfolio-handoff behavior. It reuses only the shared infrastructure in
`industrials/core` and `industrials/scripts`; it does not import implementation
modules from defense, technology, biotech, or medical devices.

The machinery and defense subsectors share `industrials.sqlite`. Every
family-owned row is written with `model_family=machinery`, and loaders delete or
replace only machinery-scoped rows. Raw market and SEC facts remain shared by
ticker and source.

The canonical active seed has 114 tickers in five calibration cohorts. The
delisted seed has 50 research candidates. Norgate identity resolution is
explicit: `actual_ticker` is the historical exchange symbol and
`norgate_symbol` is the local Norgate database symbol, including Norgate's date
suffix when present. Unresolved or ambiguous mappings are never substituted
with a same-text but different issuer.

Core commands:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\00_validate_machinery_seed.py
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\01_resolve_machinery_norgate_history.py
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\15_import_machinery_norgate_prices.py --dry-run
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\17_run_machinery_refresh_pipeline.py --asof 2026-07-09 --dry-run
```

The one-time resumable financial-disclosure bootstrap adds
`--bootstrap-sec-archives`. It reuses cached SEC documents and extracts
explicit orders plus funded/firm backlog tables; it never treats generic
backlog or RPO as funded backlog.

The normal refresh updates the shared SEC Form 4 database before importing
machinery positioning. For the first production bootstrap, add
`--full-positioning-refresh` to populate FINRA, 13F, and IBKR history in
`market_positioning.sqlite`; later daily runs use the incremental positioning
path by default. `--skip-network` omits both upstream database refreshes.

The historical portfolio calibration sequence is opt-in because a daily
2019-present rebuild is expensive. Add `--include-historical-backfill`; the
orchestrator runs Stage 11 with point-in-time feature rebuilds before it
publishes and validates the current-date dashboard. The orchestrated backfill
excludes the exact as-of date so Stage 10 exclusively owns that snapshot.
`--history-start-date`
defaults to `2019-01-02`, and `--history-frequency` may be `daily` or `weekly`.

To include the optional local Norgate import from the staging-environment
orchestrator, pass `--include-norgate-backfill --norgate-python
C:\Users\josel\miniconda3\python.exe`.

The dashboard publisher writes immutable dated portfolio contracts:

```text
output/industrials/machinery/dashboard/YYYY-MM-DD/machinery_final_rank_table.csv
output/industrials/machinery/dashboard/YYYY-MM-DD/machinery_stage11_survivorship_calibration_panel.csv
output/industrials/machinery/dashboard/YYYY-MM-DD/machinery_final_rank_table_manifest.json
```

Until an OOS calibration is sealed, every dashboard row is shadow-only and
non-investable. The survivorship-corrected sidecar may still be consumed for
research calibration when its row-level eligibility fields pass.
