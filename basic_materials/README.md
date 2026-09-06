# Basic Materials

This directory is a self-contained, fail-closed Basic Materials data, scoring,
and ranking package. It owns its configuration, policies, source registry,
SQLite schema, caches, reports, commands, and tests. It neither imports another
sector implementation nor writes to another sector's database or output tree.

## Implemented Stage 4C acceptance candidate

The package currently provides:

- an immutable 134-ticker current universe across eight cohorts;
- a checksummed 72-security deactivated-company candidate census and a governed
  20-security historical pilot;
- effective-dated memberships, four ticker aliases, 22 security events, and 20
  terminal-event contracts;
- a v2 market contract over 158 Norgate assets and 162 roles, with 492,653
  adjusted bars, 5,388 actions, XLB/SPY, 4,446 calendar sessions, and 134
  technical feature rows in the clean acceptance build;
- schema v7 and an 18-source registry in the dedicated
  `basic_materials.sqlite` database design;
- an immutable 154-profile SEC reporting census, 22 canonical financial
  metrics, and 66 reviewed US-GAAP/IFRS concept mappings;
- immutable SEC filing/package, audited-HTML, security-ratio, and FX caches
  with exact payload hashes;
- 5,754 filing rows and 310,109 raw facts preserving accession, acceptance
  time, periods, units, currencies, taxonomy, amendment state, and evidence;
- 239,747 deterministic canonical facts with source precedence,
  supersession, TTM/prior-period logic, reported/USD values, and explicit
  quarantine/conflict states;
- 22,851 AUD/CAD/EUR/KRW/USD FX observations;
- a 47-row current security-unit contract: 42 direct shares and five ADSs;
- an OGC audited IFRS fallback with 42 tied-out 2024/2025 observations;
- 134 current common financial feature rows and 134 coverage/readiness rows;
  and
- atomic loaders, independent validators, machine-readable evidence reports,
  38 regression tests, and a clean static check.

The validated acceptance snapshot is
`basic_materials_sec:2026-09-05:2cf5219d8855588066ba`. Stage 4C resolves all
154 profiles: 140 standard routes and 14 explicit exception routes. OGC is now
`resolved_audited_html` and its current feature row is full. The audited parser
does not relabel the exhibit as XBRL; it deterministically maps audited tables,
retains exact SEC lineage, and fails unless balance, cash, and profit tie-outs
pass.

Feature quality is 95 full, 29 partial, 9 insufficient, and 1 stale; 50 rows
are financially rank-ready and 87 are valuation-ready. All 47 ratio-required
foreign listings have an effective SEC-evidenced conversion. The five ADS
ratios (issuer shares represented by one traded ADS) are BHP 2, ELVR 10, PKX
0.25, RIO 1, and TX 10. No missing ratio is inferred.

The source layers contain financial, market, and FX observations before
2019-01-01, but the model-ready financial feature table currently has only one
as-of date, 2026-09-05. A 2019-forward longitudinal point-in-time feature panel
is therefore not yet implemented. That is the next Stage 4D gate.

No company score, calibrated ranking, or portfolio candidate is produced yet.
All memberships remain `calibration_eligible=0`, and both
`portfolio_candidate_gate` and `oos_score_valid_flag` remain false.

The installed live database remains on the previous Stage 4B file until an
explicit live-replacement authorization is given. The fully validated Stage 4C
candidate is at
`output/basic_materials/verification_stage4c_20260906/basic_materials.sqlite`.

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
python basic_materials/scripts/08a_load_basic_materials_security_ratios.py
python basic_materials/scripts/11_run_basic_materials_stage4.py
python basic_materials/scripts/10_validate_basic_materials_financial_stage.py
python basic_materials/scripts/02_validate_basic_materials_universe.py
python basic_materials/scripts/02c_validate_basic_materials_historical_membership.py
python -m pytest basic_materials/tests -q
python -m ruff check basic_materials
```

The Stage 4C runner already loads and validates security ratios; command `08a`
exists for diagnosis or contract-only operation. After all evidence caches are
sealed, use deterministic offline replay:

```powershell
python basic_materials/scripts/11_run_basic_materials_stage4.py --cache-only
```

The runner performs security-ratio evidence verification, SEC ingestion, the
NUE/BHP/AEM/RMIX pilot, FX, full normalization/features, and independent
validation. For lower-level diagnosis, use:

```powershell
python basic_materials/scripts/08a_load_basic_materials_security_ratios.py --cache-only
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
- `IMPLEMENTATION_STATUS.md` records implemented state, limitations, counts,
  and deployment status.
- `HISTORICAL_DEACTIVATED_CANDIDATES.md` documents the 72-name candidate census
  and promotion process.
- `output/basic_materials/stage3/<as-of>` contains market coverage, features,
  terminal calculations, issues, summaries, and artifact hashes.
- `output/basic_materials/cache/sec_reporting_profiles/<as-of>` contains the
  immutable Stage 4A SEC census cache.
- `output/basic_materials/cache/sec_financials/2026-09-05-v2` contains governed
  Stage 4C filing-package/audited-HTML evidence and its sealed manifest.
- `output/basic_materials/cache/security_share_ratios/<as-of>` contains the 47
  verified SEC security-unit documents and sealed manifest.
- `output/basic_materials/stage4_financial_contract/<as-of>` contains the Stage
  4A contract evidence pack.
- `output/basic_materials/stage4c_financial_remediation/<as-of>` is the normal
  Stage 4C SEC, ratio, FX, normalization, feature, and validation evidence root.

Next: Stage 4D builds the 2019-01-01-forward common point-in-time panel. Stage
5A then freezes specialized metric definitions, variants, applicability, and
sourceability; Stage 6B loads accepted specialized observations as
measurement-only features with zero score weight.
