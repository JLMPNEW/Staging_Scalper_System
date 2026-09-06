# Basic Materials

This directory is a self-contained, fail-closed Basic Materials data, scoring,
and ranking package. It owns its configuration, policies, source registry,
SQLite schema, caches, reports, commands, and tests. It neither imports another
sector implementation nor writes to another sector's database or output tree.

## Implemented through Stage 4B

The package currently provides:

- an immutable 134-ticker current universe across eight cohorts;
- a checksummed 72-security deactivated-company candidate census and a governed
  20-security historical pilot;
- effective-dated memberships, four ticker aliases, 22 security events, and 20
  terminal-event contracts;
- a v2 market contract over 158 Norgate assets and 162 roles, with 537,739
  adjusted bars, 5,648 actions, XLB/SPY, 4,446 calendar sessions, and 134
  technical feature rows;
- schema v6 in the dedicated `basic_materials.sqlite` database;
- an immutable 154-profile SEC reporting census, 22 canonical financial
  metrics, and 66 reviewed US-GAAP/IFRS concept mappings;
- immutable SEC filing/package and FX caches with exact payload hashes;
- 5,754 filing rows and 310,067 raw facts preserving accession, acceptance
  time, periods, units, currencies, taxonomy, amendment state, and evidence;
- 239,705 deterministic canonical facts with source precedence,
  supersession, TTM/prior-period logic, reported/USD values, and explicit
  quarantine/conflict states;
- 22,852 AUD/CAD/EUR/KRW/USD FX observations;
- 134 common financial feature rows and 134 coverage/readiness rows; and
- atomic loaders, independent validators, machine-readable evidence reports,
  36 regression tests, and a clean static check.

The live governed snapshot is
`basic_materials_sec:2026-09-05:06030312536c4d01f7ed`. Stage 4B resolves 140
profiles through the standard path and all 14 review profiles through explicit
routes. OGC is the one intentionally blocked current profile because no
cutoff-valid structured source was available. ASM and CGAU retain older usable
Company Facts while their latest annual exhibits remain explicit unstructured
gaps. TII uses the full XBRL instance from its original 40-F rather than the
cover-only amendment.

Feature quality is 94 full, 29 partial, 9 insufficient, 1 stale, and 1 blocked;
49 rows are financially rank-ready and 84 are valuation-ready. Forty-eight
foreign/current securities retain null market-cap/valuation features until a
reviewed ADR/ordinary-share ratio contract exists.

ARIS, AUGO, CRH, MTA, and TII are all active. AUGO and TII are now financially
rank-ready. ARIS and MTA remain gated by missing common metrics and foreign
share ratios; CRH has complete metric coverage but three stale observations at
the cutoff.

No company score, calibrated ranking, or portfolio candidate is produced yet.
All memberships remain `calibration_eligible=0`, and both
`portfolio_candidate_gate` and `oos_score_valid_flag` remain false.

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
python basic_materials/scripts/05_load_basic_materials_financial_contract.py
python basic_materials/scripts/06_validate_basic_materials_financial_contract.py
python basic_materials/scripts/11_run_basic_materials_stage4.py
python basic_materials/scripts/10_validate_basic_materials_financial_stage.py
python basic_materials/scripts/02_validate_basic_materials_universe.py
python basic_materials/scripts/02c_validate_basic_materials_historical_membership.py
python -m pytest basic_materials/tests -q
python -m ruff check basic_materials
```

After a Stage 4B cache is sealed, use this deterministic offline replay:

```powershell
python basic_materials/scripts/11_run_basic_materials_stage4.py --cache-only
```

The end-to-end Stage 4B runner performs ingestion, the representative
NUE/BHP/AEM/RMIX pilot, FX, full normalization/features, and independent
validation. For diagnosis, use the component order:

```powershell
python basic_materials/scripts/07_ingest_basic_materials_sec_financials.py --cache-only
python basic_materials/scripts/09_build_basic_materials_financial_features.py --pilot-only
python basic_materials/scripts/08_sync_basic_materials_fx_rates.py --cache-only
python basic_materials/scripts/09_build_basic_materials_financial_features.py
python basic_materials/scripts/10_validate_basic_materials_financial_stage.py
```

Do not use `--allow-partial` for an acceptance run.

`02d_build_basic_materials_market_instrument_review.py` and
`04a_build_basic_materials_reporting_profiles.py` are deliberate contract-build
commands, not routine refresh commands. Replacing a reviewed contract requires
`--replace-reviewed-contract`, review of the diff, and matching manifest
fingerprints.

By default, the database is
`C:/Users/josel/Documents/STAGING/DB/basic_materials.sqlite`. Set
`BASIC_MATERIALS_DB_DIR` to select a different database directory. Reports and
caches stay under `output/basic_materials` unless an explicit scratch path is
supplied. A scratch database must still be named `basic_materials.sqlite` for
commands that enforce the filename boundary.

## Key documents and outputs

- `BASIC_MATERIALS_IMPLEMENTATION_PLAN.md` is the living implementation
  authority and reusable sector-repository blueprint.
- `STAGE_GATES.md` defines exact pass/fail boundaries.
- `IMPLEMENTATION_STATUS.md` summarizes current state and the next slice.
- `HISTORICAL_DEACTIVATED_CANDIDATES.md` documents the 72-name candidate census
  and promotion process.
- `output/basic_materials/stage3/<as-of>` contains market coverage, features,
  terminal calculations, issues, summaries, and artifact hashes.
- `output/basic_materials/cache/sec_reporting_profiles/<as-of>` contains the
  immutable Stage 4A SEC census cache.
- `output/basic_materials/cache/sec_financials/<as-of>` contains governed Stage
  4B filing-package evidence, FX payloads, and sealed manifests.
- `output/basic_materials/stage4_financial_contract/<as-of>` contains the Stage
  4A contract evidence pack.
- `output/basic_materials/stage4b_financials/<as-of>` contains SEC ingestion,
  exception resolution, pilot/full normalization, FX, features, cohort/regime
  coverage, and validation evidence.

The next bounded slice is Stage 5: freeze positioning and commodity exposure
contracts, build release-aware measurement-only overlays, and publish the
foundation-readiness audit. Specialized cohort metrics begin only where that
audit shows a high-value, sourceable gap.
