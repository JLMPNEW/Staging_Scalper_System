# Basic Materials

This directory is a self-contained, fail-closed implementation of the Basic Materials scoring and ranking model. It owns its configuration, policies, source registry, SQLite schema, provider cache, validation reports, commands, and tests. It neither imports another sector implementation nor writes to another sector's database or output tree.

## Implemented through Stage 3

The package currently provides:

- an immutable 134-ticker current universe across eight cohorts;
- a checksummed 72-security deactivated-company candidate census;
- a governed 20-security historical pilot spanning all eight cohorts;
- effective-dated memberships, four ticker aliases, 22 security events, and 20 terminal-event contracts;
- schema v3 in the dedicated `basic_materials.sqlite` database;
- a governed 162-role market contract over 158 immutable Norgate asset IDs;
- raw OHLCV/dividends plus total-return-adjusted close, capital-event flags, XLB, SPY, and a SPY trading calendar;
- per-role history audits and 134 current-security market-feature rows;
- evidence-backed fixed-cash, stock-conversion, mixed, and pending-bankruptcy terminal calculations; and
- atomic canonical caches, read-only validators, machine-readable evidence reports, and 22 regression tests.

The live 2026-09-05 Stage 3 run loaded 537,739 bars, 5,648 corporate-action rows, and 4,446 calendar sessions. The current/benchmark rank-ready gate passed at 131/136, or 96.32%. ARIS, AUGO, CRH, MTA, and TII remain visible but non-rank-ready because of extreme quote-history gaps. Of 20 historical terminal events, 16 have calculable values and four—ANV, MCP, GMO, and BIOA—remain explicitly unresolved pending old-equity distribution evidence.

No company score, ranking, calibration claim, or portfolio candidate is produced yet. Current membership is still a current snapshot, the remaining 52 candidate-census names are not promoted, and all current and historical memberships remain `calibration_eligible=0`. Model promotion and portfolio flags remain false.

## Standard run order

Run from the repository root:

```powershell
python basic_materials/scripts/00a_validate_basic_materials_independence.py
python basic_materials/scripts/00_init_basic_materials_db.py
python basic_materials/scripts/01_load_basic_materials_universe.py
python basic_materials/scripts/02_validate_basic_materials_universe.py
python basic_materials/scripts/02b_validate_basic_materials_deactivated_candidates.py
python basic_materials/scripts/01b_load_basic_materials_historical_membership.py
python basic_materials/scripts/02c_validate_basic_materials_historical_membership.py
python basic_materials/scripts/03_load_basic_materials_market_contract.py
python basic_materials/scripts/03_run_basic_materials_market_stage.py --as-of YYYY-MM-DD
python basic_materials/scripts/04_validate_basic_materials_market_data.py --as-of YYYY-MM-DD
python basic_materials/scripts/02_validate_basic_materials_universe.py
python basic_materials/scripts/02c_validate_basic_materials_historical_membership.py
python -m pytest basic_materials/tests -q
python -m ruff check basic_materials
```

`02d_build_basic_materials_market_instrument_review.py` is not a routine refresh command. Use it only when deliberately rebuilding the governed provider-identity CSV; replacing an existing contract requires `--replace-reviewed-contract`, review of the diff, and a matching manifest fingerprint.

By default, the database is `C:/Users/josel/Documents/STAGING/DB/basic_materials.sqlite`. Set `BASIC_MATERIALS_DB_DIR` to select a different database directory. Reports and canonical provider caches stay under `output/basic_materials` unless a scratch `--report-dir` is supplied.

For a scratch run, pass the same `--db <scratch-path>/basic_materials.sqlite` argument to every mutating or validating command. The filename must remain `basic_materials.sqlite` so accidental cross-sector database use fails closed.

## Key documents and outputs

- `BASIC_MATERIALS_IMPLEMENTATION_PLAN.md` is the living implementation authority and reusable sector-repository blueprint.
- `STAGE_GATES.md` defines the exact pass/fail boundaries.
- `IMPLEMENTATION_STATUS.md` summarizes current state and the next slice.
- `HISTORICAL_DEACTIVATED_CANDIDATES.md` documents the 72-name candidate census and promotion process.
- `output/basic_materials/stage3/<as-of>` contains market coverage, features, terminal calculations, issues, summary, and artifact hashes.
- `output/basic_materials/cache/norgate/<as-of>` contains canonical per-asset cache files and the provider snapshot manifest.

The next implementation slice is Stage 4: point-in-time SEC/IFRS fundamentals, issuer reporting profiles, amendments, units, currency conversion, common financial features, and daily valuation repricing. Specialized cohort metrics start only after Stage 4 coverage identifies the actual filing-text gaps.
