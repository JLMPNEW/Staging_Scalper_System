# SEC Fundamentals Tier-1 Pipeline

This folder provides a full SEC-native fundamentals pipeline designed to replace Yahoo fundamentals inputs.
Enhanced snapshots are the only supported production source.

## Scripts

1. `init_sec_fundamentals_db.py`
- Creates/updates `sec_fundamentals.sqlite` schema.

2. `ingest_sec_fundamentals_tier1.py`
- Pulls SEC `submissions` + `companyfacts`.
- Supports `daily`, `weekly`, `quarterly`, and `backfill` windows.
- Stores filing metadata and raw facts (audit-friendly).

3. `build_sec_fundamental_features_tier1.py`
- Applies tier-1 tag mappings (`tier1_tag_map.yaml`).
- Builds canonical period features and derived metrics (including `accruals_ratio`).
- Builds period-level features and cutover checks.

4. `build_sec_tier1_snapshot_enhanced.py`
- Builds enhanced point-in-time snapshots from period rows:
  - `sec_fundamental_snapshot_filled_security_t1`
  - `sec_fundamental_snapshot_filled_t1`
  - run/audit metadata tables
- Applies same-filing metric repair and strict as-of handling.

5. `export_sec_fundamentals_for_pipeline.py`
- Exports Yahoo-compatible aliases (`totalRevenue`, `ebitda`, `freeCashflow`, `marketCap`, etc.) as CSV + JSON.
- Reads enhanced snapshot output (no legacy proxy fallback).

6. `run_sec_fundamentals_pipeline.py`
- Orchestrates init -> ingest -> build period -> build enhanced snapshot -> export.

7. `run_sec_fundamental_snapshot_history.py`
- Builds historical enhanced `as_of_date` snapshots into:
  - `sec_fundamental_snapshot_filled_security_t1`
  - `sec_fundamental_snapshot_filled_t1`
- Supports daily (business-day) and weekly (Friday) cadences.
- Supports resume via `--skip-existing`.

## Config

Use `fundamental_data/config_sec_fundamentals.yaml`.

Key knobs:
- `sec_fundamentals.db_path`
- `sec_fundamentals.user_agent`
- `sec_fundamentals.run_mode`
- `sec_fundamentals.backfill_years`
- `sec_fundamentals.universe_csv` (expects `cik_ticker_mapping.csv` style columns, including `CIK`)
- `sec_fundamentals.features.cutover_tolerances`

## Typical commands

```powershell
python fundamental_data/init_sec_fundamentals_db.py --config fundamental_data/config_sec_fundamentals.yaml
python fundamental_data/ingest_sec_fundamentals_tier1.py --config fundamental_data/config_sec_fundamentals.yaml --mode backfill
python fundamental_data/build_sec_fundamental_features_tier1.py --config fundamental_data/config_sec_fundamentals.yaml
python fundamental_data/build_sec_tier1_snapshot_enhanced.py --config fundamental_data/config_sec_fundamentals.yaml
python fundamental_data/export_sec_fundamentals_for_pipeline.py --config fundamental_data/config_sec_fundamentals.yaml

# Historical as_of snapshots (daily + weekly), skip already-built dates
python fundamental_data/run_sec_fundamental_snapshot_history.py --config fundamental_data/config_sec_fundamentals.yaml --cadence both --skip-existing
```

## Supported Production Source

Use only the enhanced security snapshot table for fundamentals consumption:
- `sec_fundamental_snapshot_filled_security_t1`

Do not consume `sec_signal_proxy_snapshot_t1` (legacy/deprecated).

## Main pipeline integration (replace Yahoo fetch)

In `config.yaml`:

```yaml
fundamentals:
  source: sec_sqlite
  sec_db_path: "C:\\Users\\josel\\Documents\\PROD\\DB\\sec_fundamentals.sqlite"
```

`fundamentals_yfinance.py` now supports `fundamentals.source: sec_sqlite` and returns SEC-backed fundamentals with Yahoo-compatible aliases.
