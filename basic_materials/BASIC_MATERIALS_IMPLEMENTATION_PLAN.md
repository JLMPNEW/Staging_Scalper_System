# Basic Materials Scoring and Ranking Model — Implementation Plan

Status: living implementation authority; Stage 4C and Stage 4D/5A F0 candidates implemented; F0.1/F1A control plane, F0.2 terminal-distribution closure, and F1A.1 real-document parser pilot implemented; source hydration, live promotion, PIT, and calibration blocked
Prepared: 2026-09-05  
Last implementation update: 2026-09-07
Authoritative current universe: ticker_mapping/basic_materials.csv
Current schema version: 9

## Document control and reuse contract

This file is the design authority, implementation ledger, and reusable build specification for the Basic Materials package. A code, schema, data-contract, command, stage-gate, or operating-procedure change is incomplete until this document is updated in the same change. Supporting documents may summarize a stage, but they do not replace this master description.

Required change discipline:

1. Update the affected stage's Build, Gate, Deliverables, run order, and failure behavior.
2. Add or revise the implementation ledger below with the concrete files, migration, tests, and remaining blocked gates.
3. Update `README.md`, `IMPLEMENTATION_STATUS.md`, and `STAGE_GATES.md` when operator commands or stage status change.
4. Add regression tests for every new invariant before calling a slice implemented.
5. Keep governed source artifacts immutable through checksummed manifests. A changed byte requires a new matching manifest hash and a passing validator.
6. Keep promotion states explicit. Loaded data is not automatically calibration-ready, rank-ready, or portfolio-authorized.
7. Preserve sector independence: copy a proven pattern into this namespace when appropriate, but do not create runtime imports or mutable state dependencies on another sector.

Implementation ledger:

| Date | Slice | Implemented evidence | Closed gates | Gates intentionally left open |
|---|---|---|---|---|
| 2026-09-05 | Stages 0–2A | Strict configuration; package ownership checks; schema v1; source registry; immutable 134-row universe manifest; atomic current-universe loader; current-universe reports | Exact current row/cohort/CIK counts; package and path isolation; idempotent load; current rows excluded from calibration | Historical survivorship correction; adjusted prices; fundamentals; scoring |
| 2026-09-05 | Stage 2B candidate intake | Immutable 72-row deactivated-security census; policy and manifest; validator; review workbook | Exact candidate/cohort/provider counts; review queue remains fail-closed | Candidate promotion and terminal economics |
| 2026-09-05 | Stage 2B reconciliation pilot | Schema v2; 20 effective-dated historical memberships; four ticker aliases; 22 security events; 20 terminal-event terms; four raw payload snapshots; atomic loader; database validator; reports; CLI and migration tests | All eight cohorts represented; current universe preserved; ticker reuse scoped; SEC event evidence present; historical membership marked survivorship-corrected; zero calibration activation | All 20 terminal values remain unresolved pending Stage 3 price/distribution work; remaining 52 census candidates remain outside the pilot |
| 2026-09-05 | Stage 3 adjusted market data and terminal returns | Schema v3; governed v2 162-role/158-asset Norgate identity and listing-window contract; XLB/SPY; 537,739 adjusted bars; 5,648 corporate actions; 4,446 SPY calendar sessions; 162 coverage audits; 134 market-feature rows; 20 terminal calculations; atomic cache and evidence reports | Stable provider-ID joins; snapshot fencing; 100% current/benchmark rank-ready gate; active/current major-exchange checks; listing-window feature bounds; 16 calculable terminal events resolved; no-future-price checks; Stage 2A/2B revalidation; 24 passing tests at slice completion | Four bankruptcy distributions remain unresolved; 52 candidate-census names remain outside the pilot; calibration and portfolio gates remain closed |
| 2026-09-05 | Stage 4A financial contract and reporting profiles | Schema v4; 16-source registry; immutable financial policy, manifest, 22-metric concept map, empty reviewed-override contract, and 154-row SEC-backed issuer-profile census; 315 cached SEC payloads; atomic loader; read-only validator and evidence pack | Exact 134 current/20 historical profile coverage; 22 metrics and 66 concept links; acceptance-time and amendment policy frozen; reporting/listing currency separation; row/artifact hashes; idempotent live load; every Stage 0–3 validator; 29 passing tests | Fourteen issuer profiles require governed Stage 4B fallback/review; raw filing facts, canonical statements, FX, features, valuation, specialized metrics, calibration, and portfolio gates remain closed |
| 2026-09-06 | Stage 4B point-in-time fundamentals, FX, and common features | Stage 4A checkpoint commit `4b6bce4`; immutable Stage 4B policy/cache seals; schema v5 fact/resolution/audit extensions and schema v6 performance indexes; 5,754 filings; 310,067 raw facts; 239,705 canonical facts; 22,852 FX rows; 134 feature/coverage rows; governed 14-profile exception resolution; scratch and live idempotency runs | Exact acceptance-time lineage; future-fact exclusion; deterministic source precedence, amendment supersession, TTM and sign rules; zero canonical conflicts and zero missing FX conversions; all Stage 0–4B validators; 36 passing tests | OGC remains explicitly blocked; 48 foreign securities remain valuation-gated pending share/ADR ratios; specialized metrics, survivorship-correct calibration, scoring, ranking, and portfolio gates remain closed |
| 2026-09-06 | Stage 4C OGC and listed-security-unit remediation | Financial policy v2; schema v7; deterministic audited-HTML parser; exact 47-row SEC-evidenced ratio contract; 42 direct-share and five ADS rows; ratio-aware market-cap lineage; hard OGC/ratio/formula gates; fresh Stage 0-4C acceptance build and cache-only replay | OGC has 42 usable raw and canonical facts and a full current row; zero unresolved ratio-required foreign listings; exact ADS ratios; zero Stage 4C validation errors/warnings; 38 tests and independence/Ruff pass | Live database replacement requires explicit authorization; longitudinal 2019-forward feature panel, specialized metrics, calibration, scoring, ranking, and portfolio gates remain closed |
| 2026-09-06 | Extraction-first parser and PIT sequence revision | Cross-repository review of the shared parser, Transportation, Consumer Defensive, Technology, Medical Devices, and Biotech; high-coverage gates; all-source census; content-addressed semantic cache; one resumable all-metric run; parse-free review; one unified PIT build | The former common-PIT-before-specialized plan is retired; historical materialization is blocked until specialized source and coverage closure | Stage 5A/6B code and data are not yet implemented; Stage 6C PIT and all calibration/promotion gates remain closed |
| 2026-09-06 | Stage 4D/5A F0 extraction-contract candidate | Schema v8; source registry v5; 64 specialized metrics; 16 operand links; 9,856-row exact identity-metric accounting matrix; 5,640-row SEC/IR/local-exchange/archive/technical/reserve/commodity/positioning census; content-addressed document and resumable parser ledgers; 93-date read-only PIT feasibility audit | Registry and source policies strict-load; Stage 5A has zero structural errors; every identity is accounted for across all 64 metrics and eight source families; Stage 4D feasibility passed with deterministic hashes and no database mutation; 41 tests, Ruff, and independence pass | Stage 5A is not sealed: 52 historical decisions, four terminal distributions, 134 current-name histories, 980 applicability reviews, 5,163 source rows, parser execution, and specialized coverage remain open; live database and all scoring/calibration/portfolio gates unchanged |
| 2026-09-06 | F0.1 closure workbench and F1A fixture-first parser contract | Strict query-only closure policy; evidence review queues; content/source de-duplication plan; exact contiguous regime-block validation; four parser adapters covering 32 table families; eight synthetic positive/prohibited/context/unit/period/scope/amendment/after-close fixtures; bounded parse scheduler | Workbench is structurally valid and byte-preserving; 5,163 source rows collapse to 4,244 acquisition units before content hashing; all 8 fixtures pass; maximum physical passes are frozen at one full plus one changed-evidence residual per parser-contract version; 45 tests and Ruff pass | No review decision is inferred; 52 historical decisions, four distributions, 134 histories, 980 applicability reviews, 5,163 source rows, source hydration, production extraction, accepted observations, coverage, PIT, scoring, and promotion remain open |
| 2026-09-07 | F0.2 terminal-distribution closure | Additive policy/CSV/manifest bound to the immutable Stage 3 contract; schema v9 review ledger; source registry v6; five exact SEC payloads; four reviewed zero distributions; isolated apply and read-only replay commands; overlay-aware Stage 3 refresh/validation | ANV, MCP, GMO, and BIOA have evidence-backed value 0; 20/20 terminal events resolve; terminal F0 and Stage 4D blockers fall to zero; cache replay, idempotence, integrity, foreign keys, 49 tests, Ruff, and independence pass | 52 historical decisions, 134 histories, 980 applicability reviews, 5,163 source rows, production parsing, accepted observations, specialized coverage, PIT, scoring, calibration, and promotion remain open |
| 2026-09-07 | F1A.1 real-document semantic compiler pilot | Basic Materials-owned adapter to shared `dedicated_parser`; strict pilot policy; 10 hash-sealed local SEC documents spanning all eight cohorts, one historical issuer, and HTML/XML/JSON; content-addressed semantic cache; evidence-funnel diagnostics; real-document candidate and golden-review reports; cache-only replay command | 10/10 documents and all media/cohort requirements compile; 75,715 semantic blocks are compiled once; terminology replay increases review-candidate coverage from 21/29 to 26/29 physical families and emits 295 candidates; three remaining families are classified `source_term_absent`; cache-only replay opens zero raw documents and performs zero physical compiles; no database mutation | Reviewed golden positives/hard negatives, source expansion for channel inventory/end-market mix/outage-turnaround, F0.3 seals, full hydration, accepted observations, coverage, PIT, scoring, calibration, and promotion remain open |

### Reusable sector-repository build sequence

The following sequence is the portable part of this implementation. A future sector should replace sector-specific cohorts, sources, metrics, and thresholds, while retaining the control flow and evidence boundaries.

| Step | Required implementation | Minimum acceptance evidence |
|---:|---|---|
| 1 | Inspect candidate reference repositories and separate structural patterns from sector economics | Written architecture decision naming what is reused conceptually and what is prohibited at runtime |
| 2 | Create a sector-owned namespace, configuration, database, output root, cache root, and import-boundary checker | Foreign imports and writes fail before mutation |
| 3 | Freeze the reviewed current universe with exact columns, row counts, classifications, and SHA-256 manifest | Byte match plus exact identifier and cohort validation |
| 4 | Add an owned database identity and append-only checksummed migrations | Empty initialization, repeat initialization, prior-version migration, and foreign-database rejection tests |
| 5 | Load the current universe atomically with explicit point-in-time limitations | Idempotent load and a report proving current rows are not mislabeled as survivorship-correct history |
| 6 | Build a separate deactivated-company candidate census | Candidate review states and calibration flags remain closed; discovery cannot mutate production tables |
| 7 | Promote a bounded historical pilot through four governed contracts: membership, aliases, security events, and terminal events | Exact cross-file keys; candidate-census reconciliation; primary-source events; explicit unresolved terminal states |
| 8 | Load and validate the historical pilot in one transaction | Current universe unchanged; historical rows effective-dated; aliases scoped; raw inputs retained; rerun idempotent; failure rollback proven |
| 9 | Ingest adjusted prices and reconcile cash, stock, mixed, bankruptcy, OTC, and successor returns | Every terminal event has an auditable final-return treatment before calibration eligibility can change |
| 10 | Add acceptance-bounded fundamentals, reporting profiles, FX, security-unit ratios, and current common features | Filing timing, units, currencies, cadence, amendments, listed-security basis, and current features validated |
| 11 | Freeze the complete specialized-metric, supporting-operand, applicability, and all-source census before historical materialization | Every governed identity/metric pair has an applicability and source-lane disposition; no PIT feature rows are written |
| 12 | Prove the decoder and terminology on a hash-sealed real-document pilot; then hydrate all selected sources, compile each unique document once, execute one resumable all-metric parse, and close coverage through parse-free review | Pilot document/compiler gates and reviewed golden fixtures first; then exact source seals, content-addressed cache, zero missing planned work, explicit accepted/missing/not-applicable states, and zero specialized weight |
| 13 | Materialize one unified common, cycle, positioning, and specialized point-in-time panel | Scheduled-date hashes, historical membership, contemporaneous ratios, source birthdates, accepted-at rules, and no-lookahead pass |
| 14 | Build diagnostics, shadow scores, constrained calibration, and backtests | Leakage tests, walk-forward evidence, untouched outer test, costs, capacity, and explicit no-promotion outcomes |
| 15 | Publish governed outputs and integrate downstream by file only | Dated hash-sealed contract; independent orchestration; rollback and last-success preservation |

For every future stage, use the same five-part unit of work: contract first, immutable inputs second, atomic loader third, independent validator/report fourth, and regression tests plus this document fifth. This ordering is mandatory because it prevents implementation code from silently defining its own data policy.

## 1. Executive decision

Build Basic Materials as a new, fully independent sector package. Use Consumer Defensive as the primary structural template, Technology as the source for reusable SEC/IFRS/FX and sector-overlay patterns, and Industrials/Machinery as the economic template for cyclicality, capital intensity, metric availability, lifecycle handling, and investable backtesting.

Do not clone any existing model wholesale and do not import another sector's code at runtime.

The recommended composition is:

| Need | Best existing reference | Decision for Basic Materials |
|---|---|---|
| Independent database, package identity, input manifest, stage schemas, and file-only downstream handoff | consumer_defensive | Primary package skeleton |
| One sector with multiple economically different cohorts | consumer_defensive | Use one Basic Materials model and one final rank contract, with eight cohorts |
| US-GAAP plus IFRS, 20-F/40-F/6-K handling, point-in-time FX, and daily valuation repricing | technology | Port the patterns into the Basic Materials namespace |
| Cyclic, capital-intensive financial metrics; loss-making valuation caps; lifecycle state; metric availability; D+1 execution | industrials/machinery | Primary financial and calibration design reference |
| Sector and cycle overlays that begin as measurement-only features | technology/semiconductors | Use for commodity, feedstock, and demand-cycle overlays |
| Specialized disclosure parsing and explicit applicability | consumer_defensive plus industrials/transportation | Use the repository-level dedicated_parser through a Basic Materials-owned adapter; adopt Transportation's content-deduplicated one-pass execution and Consumer Defensive's specialized-before-final-PIT ordering |
| Purged expanding walk-forward, untouched outer tests, reliability, and promotion discipline | biotech_index | Borrow the calibration safeguards only |
| Entity-resolution and analyst-review workflow | med_devices | Borrow only where useful for mine/property, commodity, and issuer-definition review |

The closest single template is Consumer Defensive because it is already one independent sector package with multiple cohorts, specialized metrics, point-in-time history, a single final-rank contract, and an independent orchestration lane. Machinery is the closest economic model, but it is not a valid package template because it imports industrials.core and shares industrials.sqlite.

### Architecture outcome

Basic Materials will own:

- its Python namespace;
- its SQLite database;
- its configuration and policies;
- its current and historical universe;
- its source registry and caches;
- its raw, canonical, feature, scoring, and governance tables;
- its dedicated-parser adapter and metric registry;
- its calibration and backtest artifacts;
- its dated final-rank outputs;
- its Portfolio Layer file adapter; and
- its independent refresh/orchestration lane.

The initial state must be shadow_monitor with zero portfolio authority. Production activation is a later evidence-based decision, not part of initial construction.

## 2. Why the other repositories are not the primary base

### 2.1 Technology

Technology has an excellent common scoring contract and clean US-GAAP/IFRS handling. Its common tables — dim_scoring_component, feature_scoring_input, feature_scoring_component, and feature_scoring_model_output — are good models for Basic Materials.

However, Technology is organized as several model families sharing technology.sqlite. Basic Materials should instead be one sector model with eight cohorts and one output contract. Technology's software, semiconductor, and hardware financial definitions also cannot be copied as Basic Materials economics.

Use Technology for:

- SEC submissions and Company Facts ingestion;
- canonical US-GAAP and ifrs-full mapping;
- inline-XBRL fallback;
- reported-currency and USD-converted fields;
- daily repricing of valuation ratios between filings;
- reserved zero-weight overlay components;
- source-birthdate gates;
- signal diagnostics;
- dated score publication; and
- monotonic, resumable refresh conventions.

Do not copy:

- technology-specific subfeatures such as SBC-centric quality, RPO, WSTS, or big-tech capex;
- technology model-family compatibility shims;
- existing model versions, weights, lock dates, or promotion receipts; or
- its shared-within-sector database topology.

### 2.2 Industrials/Machinery

Machinery is the best economic analogue. Its scoring code explicitly handles ROIC, asset turnover, incremental margin, inventory versus sales growth, cash conversion, net debt/EBITDA, interest coverage, capex intensity, backlog, book-to-bill, development-stage survival, negative-profit valuation, and D+1 adjusted-open portfolio evaluation.

Use Machinery for:

- capital-intensive canonical financial metrics;
- explicit availability states;
- negative-profit valuation score caps;
- operating-stage versus development-stage treatment;
- cycle and operating-metric components;
- point-in-time terminal-event handling;
- pre-registered candidate calibration;
- net long-only top-sleeve objectives;
- capacity, turnover, and concentration checks; and
- fail-closed source/artifact manifests.

Do not import industrials.core, write to industrials.sqlite, copy machinery's backlog semantics into unrelated cohorts, or inherit its existing activation state.

### 2.3 Consumer Defensive

Consumer Defensive is the primary structural reference because it already enforces:

- one independent sector database;
- a one-row database ownership identity;
- exact authoritative-input manifests and hashes;
- current plus historical/delisted membership;
- one sector model with multiple cohorts;
- a versioned specialized-metric registry and applicability matrix;
- measurement-only specialized features until validation;
- immutable point-in-time factor panels;
- hierarchical cohort calibration;
- a single dated final-rank table;
- file-only Portfolio Layer integration; and
- an independent db_group in orchestration.

Port these patterns, but do not copy Consumer Defensive's current exclusions, candidate hashes, activation registries, specialized metric definitions, or v3 promotion receipts. Those are evidence for a different sector and cannot authorize Basic Materials.

### 2.4 Med Devices

Med Devices is useful for explicit entity mapping, analyst review, hard-risk vetoes, and source-specific feature families. It is not the primary base because FDA, reimbursement, clinical-trial, and product-event logic does not fit Basic Materials.

Potentially useful patterns:

- reviewed issuer/entity mappings;
- expiration-dated analyst decisions;
- risk-veto fields separate from alpha scores;
- source freshness and coverage dashboards; and
- human review queues for ambiguous evidence.

### 2.5 Biotech

Biotech is too domain-specific to serve as the data or scoring base. Its calibration governance is valuable:

- nested purged expanding walk-forward evaluation;
- outer test data never used for optimization;
- cohort-aware calibration;
- score reliability;
- immutable model policies;
- explicit no-qualifying-policy outcomes; and
- separate provisional, shadow, and promoted states.

These controls should be adapted to Basic Materials without importing clinical or biotech modules.

## 3. Current universe contract

The current file has been inspected and is suitable as the authoritative intake file:

| Item | Current value |
|---|---:|
| Rows | 134 |
| Unique tickers | 134 |
| Blank CIK values | 0 |
| Active/investable rows | 134 |
| Cohorts | 8 |
| Non-U.S.-domiciled issuers | 51 |
| U.S.-domiciled issuers | 83 |
| Blank calibration_group values | 134 |

Current cohort counts:

| Cohort | Count |
|---|---:|
| specialty_chemicals_materials | 37 |
| precious_metals_producers | 30 |
| building_materials | 12 |
| commodity_chemicals | 12 |
| agricultural_inputs_crop_science | 11 |
| industrial_metals_mining | 11 |
| steel_producers_processors | 11 |
| mining_royalty_streaming | 10 |

### 3.1 Required interpretation

- subsector is the immutable cohort identity supplied by the source file.
- calibration_group should initially equal subsector.
- A separate policy field named calibration_parent should control partial pooling and fallback. Do not overload calibration_group with broad parent families.
- All 134 tickers remain in the current research universe unless a later effective-dated universe action changes status.
- Data insufficiency must change rank_ready_flag, calibration_eligible_flag, or portfolio_candidate_gate. It must not silently delete a row.
- Listing currency and financial reporting currency are different concepts. The current USD currency field represents the listed security. Stage 4 must derive the financial reporting currency from filings.
- Current membership is not historical membership. Historical calibration requires effective-dated listing and cohort episodes, delisted names, successor links, and terminal values.

### 3.2 Recommended calibration hierarchy

Keep exact cohort identities and use parent groups only for statistical shrinkage:

| Cohort | Count | Calibration parent | Standalone weight optimization at launch |
|---|---:|---|---|
| steel_producers_processors | 11 | metals_mining_producers | No |
| specialty_chemicals_materials | 37 | chemicals_inputs | Eligible after PIT gates |
| mining_royalty_streaming | 10 | metals_mining_asset_light | No |
| precious_metals_producers | 30 | metals_mining_producers | Eligible after PIT gates |
| industrial_metals_mining | 11 | metals_mining_producers | No |
| commodity_chemicals | 12 | chemicals_inputs | No |
| building_materials | 12 | basic_materials_sector | No |
| agricultural_inputs_crop_science | 11 | chemicals_inputs | No |

The launch model should have one sector-wide common factor policy plus cohort-specific measurement overlays. Cohort weight deviations should be hierarchically shrunk to the sector policy. No independent ten-name or eleven-name weight vector should be promoted merely because an optimizer can fit one.

### 3.3 Normalization fallback

For each metric on each as-of date:

1. Normalize within the exact cohort when at least eight valid, applicable peers exist and cross-sectional variance is non-zero.
2. Otherwise use the configured calibration_parent when at least twenty valid peers exist and the metric has the same economic meaning across that parent.
3. Otherwise use the full sector when at least forty valid peers exist and sector-wide comparability is explicitly permitted.
4. Otherwise mark the atomic score insufficient_peers.

Specialized operating metrics must never fall back across cohorts unless the metric registry explicitly states that the unit and definition are comparable.

## 4. Independence contract

### 4.1 Owned paths

Recommended defaults:

- Package root: basic_materials
- Database: C:/Users/josel/Documents/STAGING/DB/basic_materials.sqlite
- Database override: BASIC_MATERIALS_DB_DIR
- Output root: output/basic_materials
- Cache root: output/basic_materials/cache
- Dated dashboard: output/basic_materials/dashboard/YYYY-MM-DD
- Orchestration artifacts: output/basic_materials/orchestration
- Calibration artifacts: output/basic_materials/calibration
- Parser artifacts: output/basic_materials/dedicated_parser

### 4.2 Allowed dependencies

Runtime imports may include:

- Python standard library and declared third-party dependencies;
- basic_materials modules;
- the repository-level factor_validation package through a Basic Materials adapter;
- the repository-level dedicated_parser package through a Basic Materials adapter; and
- neutral utilities only after an explicit interface review.

Runtime imports must not include:

- technology;
- industrials;
- med_devices;
- biotech_index; or
- consumer_defensive.

Code may be ported from those packages, but the resulting implementation must live under basic_materials, use Basic Materials configuration, own its schema, and have Basic Materials tests.

### 4.3 Database and write boundaries

- basic_materials.sqlite is the only sector database this package may mutate.
- sec_insider.sqlite and market_positioning.sqlite are read-only upstream inputs.
- A Basic Materials adapter copies filtered, normalized upstream observations into Basic Materials-owned tables.
- Portfolio Layer receives files only. Portfolio Layer must not import basic_materials.
- The package must not write into Technology, Industrials, Med Devices, Biotech, or Consumer Defensive output trees.
- Every path resolver must reject paths outside the approved database, output, cache, and temporary roots.
- A one-row sector_database_identity table must identify model_family=basic_materials. A foreign or nonempty unowned database must fail before mutation.
- An AST import-boundary test must block forbidden sector imports.
- A database mutation test must prove that external databases are opened read-only.

## 5. Proposed package layout

~~~text
basic_materials/
    __init__.py
    README.md
    BASIC_MATERIALS_IMPLEMENTATION_PLAN.md
    STAGE_GATES.md
    config.yaml
    adapters/
        __init__.py
        dedicated_parser_adapter.py
        factor_validation.py
        portfolio_export.py
        positioning_import.py
    core/
        __init__.py
        atomic_io.py
        config.py
        db.py
        migrations.py
        input_manifest.py
        source_registry.py
        universe.py
        universe_validation.py
        historical_candidates.py
        historical_membership.py
        market_data_contract.py
        norgate_runtime.py
        norgate_prices.py
        market_data.py
        terminal_returns.py
        terminal_distribution_contract.py
        terminal_distribution_reviews.py
        sec_fundamentals.py
        inline_xbrl.py
        financial_semantics.py
        financial_features.py
        fx.py
        positioning.py
        commodity_registry.py
        commodity_data.py
        commodity_features.py
        metric_registry.py
        specialized_metrics.py
        scoring_features.py
        scoring.py
        score_reliability.py
        historical_panel.py
        signal_diagnostics.py
        calibration.py
        backtest.py
        reporting.py
        governance.py
        refresh.py
    data/
        authoritative_input_manifest.yaml
        free_source_registry.yaml
        basic_materials_universe_policy.yaml
        basic_materials_historical_candidate_policy.yaml
        basic_materials_historical_candidate_manifest.yaml
        basic_materials_historical_reconciliation_policy.yaml
        basic_materials_historical_reconciliation_manifest.yaml
        basic_materials_market_data_policy.yaml
        basic_materials_market_data_manifest.yaml
        basic_materials_terminal_distribution_policy.yaml
        basic_materials_terminal_distribution_manifest.yaml
        basic_materials_financial_concept_map.yaml
        basic_materials_scoring_policy.yaml
        basic_materials_calibration_policy.yaml
        basic_materials_metric_registry.yaml
        basic_materials_metric_applicability.csv
        basic_materials_commodity_registry.yaml
        basic_materials_company_commodity_exposure.csv
        basic_materials_source_birthdates.yaml
    system_csvs/
        basic_materials_deactivated_candidates.csv
        basic_materials_historical_membership.csv
        basic_materials_delisted.csv
        basic_materials_ticker_aliases.csv
        basic_materials_security_events.csv
        basic_materials_terminal_events.csv
        basic_materials_market_instruments.csv
        basic_materials_terminal_return_rules.csv
        basic_materials_terminal_distribution_reviews.csv
        basic_materials_lifecycle_overrides.csv
        basic_materials_reporting_overrides.csv
    review/
        basic_materials_deactivated_candidate_review.xlsx
    scripts/
        00_init_basic_materials_db.py
        00a_validate_basic_materials_independence.py
        01_load_basic_materials_universe.py
        01b_load_basic_materials_historical_membership.py
        02_validate_basic_materials_universe.py
        02b_validate_basic_materials_deactivated_candidates.py
        02c_validate_basic_materials_historical_membership.py
        02d_build_basic_materials_market_instrument_review.py
        03_load_basic_materials_market_contract.py
        03_run_basic_materials_market_stage.py
        04_validate_basic_materials_market_data.py
        07_sync_basic_materials_sec_fundamentals.py
        07b_sync_basic_materials_fx.py
        08_build_basic_materials_financial_features.py
        08a_validate_basic_materials_financial_stage.py
        09_import_basic_materials_positioning.py
        09b_sync_basic_materials_cycle_data.py
        09c_build_basic_materials_cycle_features.py
        10_validate_basic_materials_foundation.py
        11_build_basic_materials_scoring_features.py
        11a_validate_basic_materials_scoring_features.py
        12_run_basic_materials_parser_shadow.py
        12a_validate_basic_materials_specialized_metrics.py
        13_build_basic_materials_pit_panel.py
        13a_validate_basic_materials_pit_panel.py
        14_run_basic_materials_factor_validation.py
        14a_validate_basic_materials_factor_validation.py
        15_apply_basic_materials_terminal_distribution_reviews.py
        15a_validate_basic_materials_terminal_distribution_reviews.py
        16_build_basic_materials_shadow_scores.py
        16a_validate_basic_materials_shadow_scores.py
        16_run_basic_materials_calibration.py
        16a_validate_basic_materials_calibration.py
        17_run_basic_materials_backtest.py
        18_publish_basic_materials_dashboard.py
        18a_validate_basic_materials_dashboard.py
        19_publish_basic_materials_governance.py
        20_run_basic_materials_refresh_pipeline.py
    tests/
        test_foundation.py
        test_historical_candidates.py
        test_historical_membership.py
        test_cli_smoke.py
~~~

The exact script numbering may change before implementation, but one canonical entry point and one validator per stage should be retained.

## 6. Database design

### 6.1 Control and provenance

Required tables:

- sector_database_identity
- schema_migrations
- runs
- ingestion_runs
- ingestion_watermarks
- source_registry
- raw_api_responses
- data_quality_issues
- artifact_manifest

Every raw response and derived observation must retain source_id, retrieval timestamp, source availability timestamp when known, payload hash, parser version, and run ID.

### 6.2 Company, security, and universe

Required tables:

- dim_company
- dim_security
- dim_identifier
- dim_company_alias
- dim_ticker_alias
- dim_basic_materials_taxonomy
- dim_company_lifecycle
- dim_universe_membership
- fact_security_event
- fact_corporate_action
- fact_terminal_event_reconciliation

Company identity, listed security identity, ticker history, and cohort membership must remain separate. A ticker change or ADR/ordinary-share transition must not splice two companies or duplicate one economic issuer.

### 6.3 Market and benchmark data

Required tables:

- fact_price_ohlcv
- fact_market_snapshot
- fact_benchmark_price
- feature_market_technical
- fact_share_snapshot

Adjusted prices are mandatory for scoring. Unadjusted fallback data may be diagnostic but cannot enter return targets or final scores.

### 6.4 SEC, financial, and FX data

Implemented schema-v4 tables:

- `dim_financial_metric`;
- `bridge_financial_metric_concept`;
- `dim_issuer_reporting_profile`;
- `fact_financial_ingestion_snapshot`;
- `fact_sec_filing`;
- `fact_sec_xbrl_fact_raw`;
- `fact_financial_statement_canonical`;
- `fact_fx_rate`;
- `feature_financial_statement`; and
- `fact_financial_data_coverage`.

Stage 4A populates only the metric registry, concept bridge, issuer profiles,
and immutable raw contract payloads. The filing, fact, FX, feature, snapshot,
and coverage tables remain empty until the Stage 4B loader can satisfy their
point-in-time and lineage invariants.

The canonical layer must support us-gaap and ifrs-full, issuer extensions, 10-K/10-Q and 20-F/40-F/6-K cadences, amended filings, semiannual reporters, and reported-currency facts. Market capitalization and enterprise-value calculations use point-in-time USD conversion without discarding the original value or currency.

### 6.5 Positioning

Required tables:

- fact_sec_ownership_transaction
- fact_13f_positioning
- fact_short_interest
- fact_borrow_snapshot
- feature_positioning

Positioning source birthdates and publication lags must be explicit. Missing pre-birthdate data is unavailable, not zero.

### 6.6 Commodity and operating exposure

Required tables:

- dim_commodity
- bridge_company_commodity_exposure
- fact_commodity_observation
- fact_macro_cycle_observation
- feature_commodity_cycle
- feature_company_cycle_exposure

The company-to-commodity bridge must be effective-dated and include:

- ticker and issuer;
- commodity or feedstock;
- role: producer, processor, consumer, royalty, or mixed;
- exposure basis: revenue, EBITDA, production, purchase cost, or reviewed proxy;
- exposure weight;
- geography;
- source accession/document;
- source availability date;
- confidence; and
- policy version.

This bridge is essential. A generic copper or gold signal must not be applied to a diversified producer without a documented exposure weight.

### 6.7 Specialized evidence

Required tables:

- dim_specialized_metric
- bridge_specialized_metric_cohort
- bridge_specialized_metric_operand
- bridge_specialized_metric_applicability
- fact_specialized_source_census
- fact_specialized_document
- bridge_specialized_source_document
- fact_specialized_parser_work
- fact_specialized_metric_candidate
- fact_specialized_metric_observation
- feature_specialized_metric (Stage 6C, not yet created)
- fact_metric_adjudication (Stage 6B, not yet created)

Each accepted observation must preserve definition variant, unit, scale, period, publication/acceptance timestamp, accession or document ID, document hash, evidence text locator, extraction channel, confidence, review decision, and calibration eligibility.

### 6.8 Scoring, calibration, and governance

Required tables:

- dim_scoring_component
- feature_scoring_input
- feature_scoring_component
- feature_scoring_model_output
- model_contract
- component_weight_contract
- score_snapshot
- calibration_candidate_registry
- calibration_split_manifest
- calibration_result
- promotion_registry
- lockbox_ledger

Score rows and model contracts must be immutable by model version and as-of date. Rebuilding an old date creates a new research run or exact deterministic replay; it does not rewrite sealed evidence.

## 7. Historical and point-in-time contract

Basic Materials is more cyclical than most existing sector models. A short 2019-only calibration window would contain too little independent commodity-cycle variation.

Recommended history design:

- Market and common financial target start: 2010-01-04.
- Market warm-up: at least 400 calendar days before the first score date.
- Positioning-enhanced research start: no earlier than the actual source birthdate, generally 2019 or later.
- S-K 1300-style mining disclosure history: no synthetic pre-rule backfill; use each document's actual availability.
- Current daily score outputs: all available current members.
- Calibration: prohibited until survivorship-correct membership, delisted prices, and terminal events pass.

### 7.1 Availability rules

- SEC facts become available at SEC acceptance time, not fiscal-period end.
- A filing amendment supersedes eligible facts only after its own acceptance time.
- 6-K information uses the furnishing timestamp and explicit covered period.
- Macro and commodity data use release timestamps and vintage data where revisions are possible.
- Features measured at the close enter a tradable portfolio no earlier than the next session.
- A historical member remains eligible only inside its effective membership and listed-security interval.
- A terminal value may replace an unobservable horizon only under a reviewed terminal-event policy.

### 7.2 Survivorship controls

The current 134-name file cannot be used as the historical universe. Stage 2 must create:

- effective-dated historical membership;
- acquired, bankrupt, merged, and delisted names;
- ticker and security aliases;
- successor/predecessor links;
- delisting dates and terminal values;
- listing-start dates for recent IPOs and spin-offs; and
- cohort membership as it was knowable at each date.

Norgate total-return history is the preferred repository precedent when available. If its license or coverage is unavailable, calibration must remain blocked; current shadow scoring can continue.

## 8. Scoring architecture

### 8.1 Output philosophy

Every current ticker must appear in the dated output. The output distinguishes:

- research_score_available_flag;
- rank_ready_flag;
- calibration_eligible_flag;
- portfolio_candidate_gate;
- score_confidence;
- lifecycle_state;
- cohort_rank;
- sector_rank; and
- rank_exclusion_reason.

A pre-revenue developer, a royalty company, and an operating steel mill should not be forced through identical profitability math. All may receive research outputs, but only economically comparable and sufficiently complete rows receive the relevant production rank.

### 8.2 Lifecycle is orthogonal to cohort

Add an effective-dated lifecycle_state:

- operating_established
- operating_ramp
- development_or_precommercial
- restructuring_or_distress
- royalty_streaming_asset_light
- recent_listing_insufficient_history

The source cohort remains unchanged. Lifecycle controls metric applicability, valuation methods, confidence gates, and portfolio eligibility.

### 8.3 Common atomic factors

Recommended common factors:

| Component | Initial atomic metrics |
|---|---|
| quality | gross margin, operating margin, FCF margin, ROIC, asset turnover, interest coverage |
| balance_sheet_resilience | net debt/EBITDA, net cash/assets, interest coverage, current liquidity, debt maturity pressure |
| cash_flow_capital_allocation | FCF yield, cash conversion, capex/revenue, dilution, buybacks/dividends when applicable |
| fundamental_trend | revenue growth, gross-profit growth, operating-income growth, FCF growth, margin change, inventory-to-sales growth gap |
| valuation | EV/EBITDA, EV/EBIT, EV/gross profit where meaningful, FCF yield, cohort-specific normalized value |
| market_behavior | residual momentum, 3/6/12-month momentum, downside volatility, drawdown, distance from high, liquidity |
| positioning | insider net buying, institutional flow, short interest, days to cover, borrow fee |
| cycle_overlay | commodity, feedstock, volume, spread, utilization, and demand-cycle signals |

Data quality and score confidence are gates, not positive alpha factors. A company must not receive a higher score merely because it is easier to parse.

### 8.4 Initial shadow baseline

Register one simple, economically motivated baseline before looking at forward returns:

| Common component | Shadow baseline weight |
|---|---:|
| quality | 20% |
| balance_sheet_resilience | 15% |
| cash_flow_capital_allocation | 15% |
| valuation | 15% |
| fundamental_trend | 10% |
| market_behavior | 20% |
| positioning | 5% |
| specialized/cycle overlays | 0% |

This is a research baseline, not a production recommendation. Specialized and cycle features remain measurement-only until factor validation and walk-forward evidence authorize non-zero weights.

### 8.5 Metric scoring

- Use deterministic midrank percentile scoring from 0 to 100.
- Normalize within exact cohort when the peer gate passes.
- Winsorize only when a metric policy requires raw-value clipping; retain original values.
- Direction is versioned per metric.
- Apply a valuation cap of 25 to loss-making issuers when no economically meaningful profit denominator exists.
- Reprice market-cap and enterprise-value metrics daily between filings.
- Use point-in-time shares and debt/cash facts.
- Use a fixed neutral contribution of 50 for an applicable-but-missing weighted input and count its full weight in missing_component_weight.
- Exclude not_applicable metrics from that metric family's denominator.
- Never convert not_reported, stale, review_required, or not_applicable to numeric zero.
- A row is not rank-ready when score confidence or required coverage falls below policy.

Recommended initial rank gate:

- minimum score confidence: 0.70;
- maximum applicable missing weight: 0.30;
- minimum adjusted-price history for full market features: 252 sessions;
- minimum current sector rank-ready coverage: 85%;
- minimum valid peers per cohort metric: 8; and
- minimum valid sector cross-section: 40.

These thresholds must be frozen in Stage 0 and may be changed only through a new policy version.

## 9. Cohort-specific feature plan

### 9.1 steel_producers_processors

Primary economics:

- realized selling price per ton;
- shipments and volume growth;
- capacity and utilization;
- EAF versus blast-furnace exposure;
- scrap, iron ore, coking coal, electricity, and natural-gas input exposure;
- price-cost spread and pass-through lag;
- inventory and working-capital cycle;
- maintenance versus growth capex;
- EBITDA or operating income per ton;
- contract versus spot mix; and
- leverage through the cycle.

Initial high-value parser targets:

- shipment tons;
- realized price per ton;
- capacity utilization;
- steelmaking raw-material cost commentary;
- maintenance capex; and
- segment operating income.

Backlog is applicable only to processors that explicitly report a firm comparable measure. It is not a universal steel factor.

### 9.2 specialty_chemicals_materials

Primary economics:

- organic volume;
- price/mix;
- raw-material pass-through;
- gross and EBITDA margin;
- specialty versus commodity sales mix;
- end-market concentration;
- R&D intensity;
- new-product contribution where consistently disclosed;
- working-capital efficiency;
- maintenance capex;
- environmental/remediation liabilities; and
- customer concentration.

Initial high-value parser targets:

- reported organic volume change;
- price/mix change;
- segment adjusted EBITDA margin;
- R&D/revenue;
- working-capital change; and
- end-market mix.

Non-GAAP metrics require reconciliation and a stable issuer definition. Do not pool issuer-defined adjusted EBITDA variants without mapping.

### 9.3 mining_royalty_streaming

Primary economics:

- attributable production or gold-equivalent ounces;
- realized commodity price;
- revenue by producing asset;
- top-asset and top-counterparty concentration;
- number of producing assets;
- reserve life and development optionality;
- jurisdiction exposure;
- operator quality;
- net debt and funding commitments;
- embedded capex obligations; and
- price/NAV when a point-in-time defensible NAV is available.

Initial high-value parser targets:

- attributable production/GEOs;
- producing-asset count;
- top-five asset concentration;
- attributable reserves/resources by definition code;
- committed funding obligations; and
- net debt.

Royalty companies must not inherit producer AISC or sustaining-capex requirements when those metrics are not applicable.

### 9.4 precious_metals_producers

Primary economics:

- gold, silver, or equivalent production;
- all-in sustaining cost and cash cost;
- realized price;
- reserve and resource quantities;
- reserve life;
- grade and recovery;
- sustaining and growth capex;
- hedge-book exposure;
- by-product credits;
- mine and jurisdiction concentration;
- net debt and liquidity; and
- FCF sensitivity to conservative commodity-price assumptions.

Initial high-value parser targets:

- production ounces;
- AISC;
- cash cost;
- sustaining capex;
- proven/probable reserves;
- reserve life;
- average grade/recovery; and
- top-asset production share.

Reserve, resource, and technical-report definitions must retain their reporting code and cannot be silently mixed. SEC S-K 1300, Canadian NI 43-101, and JORC-style disclosures require explicit definition variants.

### 9.5 industrial_metals_mining

Primary economics:

- production by commodity;
- realized price;
- unit cash cost;
- grade and recovery;
- reserve life;
- stripping and processing intensity where relevant;
- sustaining and growth capex;
- by-product credits;
- commodity mix;
- mine and jurisdiction concentration;
- project execution risk; and
- balance-sheet sensitivity to commodity prices.

Initial high-value parser targets:

- production volume by commodity;
- unit cost;
- realized price;
- reserve/resource quantities;
- sustaining capex;
- top-asset concentration; and
- effective commodity exposure weights.

### 9.6 commodity_chemicals

Primary economics:

- benchmark product-feedstock spread;
- capacity utilization;
- volume and price/mix;
- natural-gas, NGL, oil, electricity, and other feedstock exposure;
- turnaround/outage effects;
- inventory cycle;
- mid-cycle margin;
- maintenance capex;
- leverage; and
- supply additions and closures.

Initial high-value parser targets:

- volume change;
- price/mix change;
- utilization/outage days;
- segment EBITDA;
- maintenance capex; and
- primary feedstock exposure.

Spot-period margins must not be treated as normalized earnings. Valuation diagnostics should include both trailing and mid-cycle views.

### 9.7 building_materials

Primary economics:

- price/volume/mix;
- shipments by product;
- aggregates reserves and reserve life;
- EBITDA per ton;
- local-market concentration;
- public infrastructure, residential, and non-residential exposure;
- energy and freight costs;
- backlog where definitionally comparable;
- maintenance capex;
- acquisition leverage; and
- weather/seasonality.

Initial high-value parser targets:

- shipment volume;
- price/mix;
- EBITDA per ton;
- aggregates reserves;
- public-versus-private end-market mix; and
- energy/freight cost effects.

### 9.8 agricultural_inputs_crop_science

Primary economics:

- nutrient, fertilizer, or crop-protection volume;
- realized price and price/mix;
- natural-gas/ammonia feedstock exposure;
- crop prices and planted acreage;
- farm-income and affordability conditions;
- inventory and channel destocking;
- capacity utilization;
- maintenance capex;
- patent/product-cycle exposure;
- environmental/regulatory risk; and
- working capital.

Initial high-value parser targets:

- volume;
- price/mix;
- nutrient or active-ingredient segment EBITDA;
- natural-gas/feedstock sensitivity;
- inventory/channel commentary;
- maintenance capex; and
- major product or patent concentration.

## 10. Commodity and macro overlay design

The Technology/Semiconductor overlay architecture is the correct model: reserve component identities in the scoring contract, populate them independently, and leave them at zero weight until validated.

### 10.1 Initial overlay families

- metals_price_cycle
- steel_spread_cycle
- energy_feedstock_cycle
- chemical_price_cost_cycle
- construction_demand_cycle
- crop_affordability_cycle
- inventory_cycle
- jurisdiction_concentration_risk
- reserve_life_quality
- operating_cost_curve

### 10.2 Public source baseline

Potential authoritative sources:

- SEC EDGAR submissions and XBRL Company Facts for issuer financials and filing chronology;
- Federal Reserve FRED/ALFRED for industrial production, producer prices, construction activity, rates, and vintage-aware macro series;
- U.S. Energy Information Administration for natural gas, petroleum, electricity, and energy-input series;
- U.S. Geological Survey National Minerals Information Center for mineral production, reserves, supply, and industry data;
- USDA NASS Quick Stats for crop production, acreage, and agricultural prices; and
- issuer filings and hash-sealed investor-relations documents for company operating metrics.

Useful official references:

- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- SEC mining disclosure guide: https://www.sec.gov/resources-small-businesses/small-business-compliance-guides/modernization-property-disclosures-mining-registrants-small-entity-compliance-guide
- FRED observations and vintage dates: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
- EIA Open Data: https://www.eia.gov/opendata/index.php/api
- USGS Mineral Commodity Summaries: https://www.usgs.gov/centers/national-minerals-information-center/mineral-commodity-summaries
- USDA NASS data access: https://data.nass.usda.gov/

Licensed commodity-price or continuous-futures data may be added later. A licensed series must carry entitlement, roll methodology, units, exchange calendar, settlement timestamp, and historical revision policy. A free proxy must never be presented as a physical spot price without an explicit basis.

### 10.3 Point-in-time safeguards

- Store observation period, publication date, retrieval time, and revision/vintage separately.
- Use ALFRED vintage data or first-release values when revised macro series enter a historical model.
- Apply documented release lags.
- Do not backdate annual USGS or reserve data to the measurement year.
- Do not use a quarter-end commodity exposure derived from a later annual filing before that filing was public.
- Freeze continuous-futures construction and roll policy before signal testing.

## 11. Specialized data capture and one-pass parser strategy

Specialized, commodity, positioning, and remaining historical-identity data
must be captured and coverage-frozen before a longitudinal feature panel is
written. Stage 4D is therefore a read-only historical feasibility preflight;
Stage 6C is the one authorized 2019-forward PIT materialization. There is no
intermediate common-only historical panel that must later be rebuilt.

High coverage never means manufacturing values. A structurally
not-applicable pair is excluded from the denominator, but not reported,
missing source, parser failure, conflicting, and review-required pairs remain
visible and count as uncovered until resolved or the metric is excluded from
the panel-required set.

### 11.1 Parser lessons adopted from the existing repositories

| Reference | Adopt for Basic Materials | Deliberate improvement or exclusion |
|---|---|---|
| repository-level `dedicated_parser` | Offline providers, immutable work/evidence ledgers, parent-only SQLite writes, deterministic multiprocessing, exact review policy, golden corpus, and policy-only replay | Shared code remains sector-neutral; Basic Materials owns every metric, source, cache, policy, database, and output |
| Industrials/Transportation | Freeze metrics and supporting operands first; seal the exact source manifest; content-hash deduplication; unique-content text cache; one document/many metrics; one resumable parse; parse-free adjudication; one panel build | Complete the SEC, issuer-IR, local-exchange, technical-report, and archived-source census before the first full parse so Transportation's later source-exhaustion delta cycle is not repeated |
| Consumer Defensive | Independent sector adapter; immutable content-addressed objects; contextual table/prose rejection rules; specialized observations before final historical panel; explicit applicability and missingness | Expand the golden corpus and table-family fixtures before the full run so repeated adapter/term and OCR repair passes are minimized |
| Technology/Software | Complete accession hydration, source hashes, standard-XBRL precedence, and measurement-only promotion | Use canaries only for synthetic/golden fixtures and plan validation. Do not parse the same production filing in canary, cohort, current, and historical waves |
| Medical Devices | Raw-before-canonical storage, event date separate from publication date, confidence-scored entity mapping, parallel retrieval with serialized writes | Use these patterns for mine/property/operator, commodity, technical-report, and government-data identity rather than importing Med Devices code |
| Biotech | Host-level throttling, bounded retries, explicit full-history mode, source-window controls, and changed-source detection | Historical source objects are immutable and content-addressed; mutable TTL cache state is not evidence authority |

### 11.2 Target data flow

```text
frozen universe + metric/operand registry + applicability
                         |
                         v
all-source document/API census with exact availability timestamps
                         |
                         v
immutable content-addressed raw objects (one object per SHA-256)
                         |
                         v
unique-document compiler: XBRL + tables + text + bounded PDF/OCR
                         |
                         v
semantic intermediate records keyed by content hash and decoder contract
                         |
                         v
one resumable issuer-context run evaluating every applicable metric
                         |
                         v
immutable candidates/evidence -> parse-free policy replay and derivations
                         |
                         v
coverage cube + final metric dispositions + frozen observation store
                         |
                         v
one unified 2019-forward PIT panel -> diagnostics -> calibration
```

Acquisition, physical decoding, semantic evaluation, adjudication, derivation,
feature materialization, and calibration are separate ledgers. A downstream
policy, coverage threshold, metric disposition, or score-weight change cannot
authorize retrieval or physical decoding.

### 11.3 Metric and applicability contract before retrieval

Stage 5A freezes the full discovery universe, not only the metrics expected to
reach calibration. Every registry row must declare:

- metric ID and version, cohort, operating archetype, and definition variant;
- role: `core_required`, `supporting_operand`, `diagnostic_candidate`,
  `derived_only`, or `excluded`;
- unit family, scale, sign/direction, allowed period kinds, value bounds, and
  freshness;
- issuer, mine/property, segment, commodity, geography, ownership, reserve
  code, and adjusted-versus-GAAP scope rules where relevant;
- direct source lanes, fallback source lanes, table families, concept aliases,
  contextual terms, and prohibited contexts;
- formula and exact operands for derived metrics;
- current and historical applicability with a reason for every
  not-applicable pair; and
- predeclared coverage, review, and final-disposition rules.

The applicability product covers all 134 current identities, all 20 governed
historical identities, and every additional deactivated identity promoted
before the source seal. The remaining 52 Stage 2B candidates must be promoted
or rejected before the final parser universe is frozen; adding an issuer after
the full parse requires a separately versioned delta rather than silently
changing the original run.

Supporting operands are first-class parser targets. Production ounces,
commodity-specific volumes, asset-level production, cost components, ownership
percentages, sustaining/growth capex, reserve classes, grades, recoveries,
price, volume, mix, segment EBITDA, capacity, utilization, outage days, and
end-market shares are captured even when the final feature is derived. This
prevents a later formula from forcing another document search.

### 11.4 All-source census and precedence

The source census is completed before the full parser run and includes:

1. Standard and issuer-extension SEC Inline XBRL facts.
2. SEC periodic forms and amendments: 10-K, 10-Q, 20-F, 40-F, and transition
   forms.
3. Financial and operating-statistics 8-K/6-K exhibits.
4. S-1/F-1 and relevant transaction filings for IPO, spin-off, relisting, and
   predecessor history.
5. Hash-sealed issuer annual reports, earnings releases, operating reports,
   presentations, reserve/resource reports, and technical reports.
6. Official local-exchange or mining-regulator filings when the SEC filing
   does not contain the operating schedule.
7. Public government/API sources for commodity, energy, crop, reserve, and
   macro data under separate raw/canonical ingestion contracts.
8. Manual adjudication only for an already captured evidence candidate; it
   cannot create an unsupported value.

Every selected document records issuer/security identity, accession or
document ID, form/type, report period, published/accepted/available timestamp,
source URL, local content-addressed path, byte size, SHA-256, document role,
selection rule, and all applicable metrics. Every planned document must be
cached and hash-verified or have a terminal, reviewed source-gap disposition
before parsing.

### 11.5 Compile once and evaluate all metrics

The Basic Materials adapter uses the shared parser runtime but owns a
sector-specific document compiler contract:

- Physical decoding is keyed by `content_sha256 + decoder_contract_sha256`.
  HTML/XML, Inline XBRL, XLS/XLSX, DOC/DOCX, native PDF text, and bounded OCR
  each produce one immutable result per unique content hash and option set.
- Inline XBRL is normalized once with contexts, units, dimensions, labels,
  presentation/calculation relations, and source coordinates retained.
- HTML, spreadsheet, and PDF tables compile to a generic table intermediate
  representation preserving headings, merged cells, row/column coordinates,
  units, scale, footnotes, and nearby section text.
- Narrative text compiles to section, paragraph, sentence, and bullet blocks.
  Narrative numeric matches are review-required unless a frozen metric rule
  explicitly permits deterministic acceptance.
- Duplicate content is decoded once and may be evaluated in multiple
  issuer/security contexts only through reviewed identity-continuity rules.
- Each issuer/content context constructs the semantic document once and runs
  every applicable metric/table-family matcher in the same work item. A metric
  loop may not reopen, retokenize, or reparse a document.
- Workers read immutable objects and return serializable evidence; only the
  parent process writes bounded SQLite transactions.
- Work keys include content identity, issuer context, decoder/evaluator
  release, metric registry, applicability, and extraction policy. Resume skips
  completed keys; `--force` is prohibited for the historical corpus.

The initial adapter is organized around reusable table families rather than
one regex per final feature:

- production, shipment, capacity, and utilization schedules;
- realized price, volume, mix, and unit-cost schedules;
- sustaining/growth capex and project-commitment schedules;
- reserve/resource, grade, recovery, ownership, and mine-life schedules;
- asset, commodity, geography, customer, and end-market mix schedules; and
- segment earnings, margin, outage/turnaround, and feedstock-sensitivity
  schedules.

Metric mapping and derivation operate from the stored semantic records and
accepted operands. Review decisions use policy-only replay with zero document
opens, provider calls, or OCR. A parser term, mapping, or policy correction
first re-evaluates stored semantic records; it does not physically decode the
source again.

#### 11.5.1 Implemented real-document pilot boundary

F1A.1 implements that boundary before broad hydration. Ten exact local SEC
objects cover every current cohort, one historical/deactivated issuer, and
HTML, XML, and structured JSON. The owned adapter compiles markup through the
sector-neutral `dedicated_parser` runtime and flattens SEC Company Facts into
the same immutable semantic-block contract. Cache identity is the full source
SHA-256 plus full decoder-contract SHA-256 stored inside each payload; a
shortened decoder-hash directory prefix keeps Windows atomic writes below the
legacy path limit without weakening full-hash validation.

The pilot separates source decoding from metric evaluation. Its evidence
funnel records uncapped lexical hits, numeric-eligible hits, prohibited
contexts, capped review candidates, exact block/evidence hashes, source
availability, and decoder lineage. A rule-only replay therefore distinguishes
`source_term_absent`, `numeric_evidence_absent`, `prohibited_context_only`, and
`candidates_found_review_required` without reopening raw documents. The first
real-document compile produced 75,715 semantic blocks from 10 unique hashes.
A cache-only terminology replay performed zero physical compiles and zero raw
opens while increasing physical-family candidate coverage from 21/29 to 26/29
and emitting 295 review rows.

The three remaining pilot gaps are evidence-acquisition targets, not decoder
retry targets: channel-inventory value evidence requires an agricultural
earnings/operating source, specialty end-market mix requires narrative
10-K/issuer material beyond Company Facts, and commodity-chemical
outage/turnaround evidence requires the annual-report exhibit or operating
release rather than a 40-F wrapper. The pilot writes no accepted observation,
SQLite row, PIT row, score, calibration input, or promotion receipt. The
reviewed golden gate and production-execution flag remain false.

### 11.6 Bounded retry and residual policy

Before the full run, every table family requires positive, hard-negative,
cross-cohort, unit, period, scope, duplicate, amendment, and after-close
fixtures. Each family should include at least 20 reviewed positives, 10 hard
negatives, and three historical/deactivated examples where the source
population permits.

After the one full run, unresolved pairs are partitioned exactly once:

- `review_required` or `conflicting`: adjudicate stored evidence and replay
  policy without parsing;
- `derived_operand_gap`: calculate from already accepted facts or retain the
  explicit missing operand;
- `parser_failure`: bounded format repair or OCR only for the affected content
  hashes;
- `source_document_missing`: include in one sealed residual acquisition batch
  only when the expected coverage lift is material;
- `searched_not_reported`: terminal for that source window and not retried; or
- `definition_not_comparable`: keep measurement-only or exclude the metric.

At most one consolidated residual acquisition/parse batch is allowed before
the metric freeze. It contains every approved residual source and all
applicable metrics, excludes previously completed content hashes, and reuses
the semantic cache. Further work requires a new parser/source contract version
and must not mutate the original evidence.

The executable scheduler interprets that limit as two physical passes per
content hash and parser-contract version, not as repeated full-corpus retries:

- the first eligible execution is one complete all-metric pass;
- a policy, terminology, mapping, or adjudication change replays immutable
  semantic/candidate evidence with zero source opens, decoder calls, or OCR;
- one residual pass is eligible only for unresolved work with a new content
  hash or a frozen parser-rule version change;
- unchanged unresolved work remains `policy_review_only`; and
- new evidence after the residual pass requires a new versioned contract and
  delta plan instead of a third physical pass or mutation of prior evidence.

This is the required meaning of "multiple parsing" for Basic Materials:
targeted, evidence-triggered, and versioned. Blind retries, completed-content
reparsing, and separate current/historical/cohort parsing waves are prohibited.

### 11.7 Coverage gates before PIT materialization

Coverage is reported by cohort, metric, definition variant, source lane,
current/historical role, year, and evidence state. The following are minimum
gates for the first unified historical panel:

- universe/applicability accounting: 100% of governed identity-metric pairs;
- source-manifest accounting: 100% cached-and-hashed or terminally disposed;
- parser execution: 100% completed/resume-linked work and zero unexplained
  failures;
- core current breadth: at least 80% of applicable current ticker-metric pairs
  in every cohort;
- core historical depth: at least 70% of expected applicable issuer-period
  observations from 2019 forward and at least 60% in each fixed chronological
  block (2019-2020, 2021-2022, and 2023-current);
- PIT feasibility: at least eight comparable issuers on at least 36 scheduled
  dates for any cohort metric admitted to calibration research; and
- evidence quality: zero unresolved conflicts, future-availability errors,
  invalid units/periods, or unreviewed prose/OCR observations in the accepted
  store.

These thresholds apply to the frozen `core_required` set. Sparse optional
metrics remain captured in the discovery store as measurement-only and do not
block the panel when they are explicitly classified diagnostic, deferred, or
excluded. If the core set misses a gate after the residual batch, narrow the
core set under the predeclared disposition policy or keep the cohort in a
limited shadow state; do not impute or repeatedly reparse to force coverage.

### 11.8 Metric states and admission to production

Every ticker-metric-as-of combination must have one state:

- reported;
- derived_from_reported;
- reviewed_proxy;
- not_reported;
- not_applicable;
- stale;
- conflicting;
- review_required;
- parser_failure;
- source_document_missing; or
- insufficient_history.

A specialized metric may receive non-zero score weight only when:

- definition, direction, unit, and transformation are versioned;
- applicability is complete for all current and governed historical tickers;
- point-in-time availability is proven;
- issuer-definition variants are handled;
- unit and plausibility checks pass;
- current and historical coverage pass;
- minimum independent dates and issuer breadth pass;
- factor-validation direction and FDR gates pass;
- turnover and regime stability pass;
- walk-forward evidence is positive or non-inferior; and
- promotion is explicitly hash-sealed.

Until then it remains `measurement_only` with weight zero.

## 12. Stage-by-stage implementation

## Stage 0 — Architecture, policy, and independence

Build:

- package skeleton;
- README and STAGE_GATES;
- config loader with strict unknown-key validation;
- authoritative-input manifest for ticker_mapping/basic_materials.csv;
- source registry;
- universe, cohort, lifecycle, benchmark, and history policies;
- database/output path guards;
- model status fixed to shadow_monitor;
- forbidden-import checker.

Gate:

- authoritative CSV has exactly 134 unique rows;
- exact cohort counts match this plan;
- all required identity fields are present;
- every calibration_group is resolved by policy;
- all paths are Basic Materials-owned;
- no forbidden sector import exists;
- no database or output mutation occurs during validation failure.

## Stage 1 — Independent database foundation

Build:

- sector_database_identity;
- transactional, checksum-versioned migrations;
- run, ingestion, watermark, source, raw-response, artifact, and issue tables;
- company/security/identifier/taxonomy tables;
- empty market, financial, positioning, commodity, parser, scoring, and governance schemas.

Gate:

- scratch initialization succeeds;
- a second initialization is idempotent;
- a foreign or nonempty unowned DB is rejected before mutation;
- foreign keys and indexes validate;
- migration checksums cannot drift;
- all writes remain under approved roots.

## Stage 2 — Current and historical universe

Implement this as two separately gated slices. Stage 2A owns the immutable 134-name current snapshot. Stage 2B owns deactivated-company discovery, review, and eventual promotion into effective-dated history. Candidate discovery must never write directly to historical membership tables.

Build:

- current-universe loader from basic_materials.csv;
- calibration_group=subsector policy;
- effective-dated cohort and lifecycle rows;
- aliases, listing intervals, share-class identity, corporate lineage;
- historical and delisted membership;
- terminal-event policy;
- source fingerprints and validation reports.

Gate:

- all 134 current rows are represented exactly once;
- no duplicate CIK/security collision is unresolved;
- membership intervals do not overlap incorrectly;
- ticker reuse and successor chains pass;
- historical rows never contaminate current output;
- current-source membership is not labeled survivorship-correct history.

Deliverables:

- `output/basic_materials/stage2_universe/YYYY-MM-DD/validation_summary.json`
- `output/basic_materials/stage2_universe/YYYY-MM-DD/validation_issues.csv`
- `output/basic_materials/stage2_universe/YYYY-MM-DD/universe_snapshot.csv`
- `output/basic_materials/stage2_universe/YYYY-MM-DD/cohort_census.csv`
- `output/basic_materials/stage2_universe/YYYY-MM-DD/artifact_manifest.json`
- `output/basic_materials/stage2_historical_membership/YYYY-MM-DD/historical_validation_summary.json`
- `output/basic_materials/stage2_historical_membership/YYYY-MM-DD/historical_validation_issues.csv`
- `output/basic_materials/stage2_historical_membership/YYYY-MM-DD/historical_membership_snapshot.csv`
- `output/basic_materials/stage2_historical_membership/YYYY-MM-DD/unresolved_terminal_events.csv`
- `output/basic_materials/stage2_historical_membership/YYYY-MM-DD/artifact_manifest.json`

### Stage 2B implementation record — candidate intake

Candidate intake was implemented on 2026-09-05:

- 72 deactivated-security candidates across all eight cohorts;
- 55 Tier 1 and 17 Tier 2 review priorities;
- 71 resolved Norgate provider assets and one explicit provider-mapping block;
- 16 initial primary-event source URLs;
- a checksummed candidate manifest and strict validator;
- a filterable human review workbook; and
- zero candidates activated for historical membership or calibration.

The candidate census is `basic_materials/system_csvs/basic_materials_deactivated_candidates.csv`. Promotion creates separate reviewed historical-membership, alias, security-event, and terminal-event files; it does not mutate the candidate census or the 134-name current universe.

### Stage 2B implementation record — governed reconciliation pilot

The first bounded promotion pilot was implemented on 2026-09-05. It promotes 20 of the 72 census candidates into a separate historical engineering universe while leaving the candidate census immutable. The 20 memberships are marked survivorship-corrected because their security intervals are effective-dated and inactive issuers are represented. They remain calibration-ineligible because terminal returns are not yet closed.

Pilot coverage:

| Cohort | Count | Historical tickers |
|---|---:|---|
| steel_producers_processors | 4 | X, RDUS, HAYN, ZEUS |
| specialty_chemicals_materials | 1 | VAL |
| mining_royalty_streaming | 2 | MMX, SAND |
| precious_metals_producers | 3 | GOLD (Randgold), GATO, ANV |
| industrial_metals_mining | 3 | MCP, GMO, ALTM |
| commodity_chemicals | 2 | BIOA, AXLL |
| building_materials | 2 | SUM, USCR |
| agricultural_inputs_crop_science | 3 | MON, POT, AGU |
| **Total** | **20** | All eight cohorts represented |

Terminal-outcome coverage is nine cash acquisitions, six stock mergers/acquisitions, one mixed cash/stock acquisition, and four bankruptcy outcomes. This mix exercises fixed cash, successor-share conversion, proration, ticker transitions, OTC continuation, ticker reuse, and unresolved old-equity distributions before the full 72-name history is expanded.

Governed source contracts:

| Artifact | Rows | Unique key | Source ID | SHA-256 |
|---|---:|---|---|---|
| `system_csvs/basic_materials_historical_membership.csv` | 20 | historical_ticker | basic_materials_historical_membership_review | `5f41f6e989aecad56561f56a1f3f3e294e8658575828c28444835d4fd42aa695` |
| `system_csvs/basic_materials_ticker_aliases.csv` | 4 | alias_key | basic_materials_historical_alias_review | `51cdc9b1a3d7d77918309f01445bf702f91ec4e1a6c1eb29f2b24ec4220807f8` |
| `system_csvs/basic_materials_security_events.csv` | 22 | event_key | basic_materials_security_event_review | `2b625121fd7654dfc1e25900fe41d18f182cede3cbe265f40dce20ff033dce8d` |
| `system_csvs/basic_materials_terminal_events.csv` | 20 | event_key | basic_materials_terminal_event_review | `4100a02c65e526fe15baa5c0d2dccd4535e579219028c69a24c3ec54eb90467e` |

The manifest at `data/basic_materials_historical_reconciliation_manifest.yaml` seals the hash, byte size, row count, source ID, path, and unique key for each artifact. The policy at `data/basic_materials_historical_reconciliation_policy.yaml` fixes the exact ticker set, cohort counts, calibration parents, allowed event states, review state, and closed activation flags.

Identity controls implemented in this pilot:

- RDUS retains SCHN as an effective-dated predecessor alias under provider asset 140985.
- Randgold's historical GOLD security is scoped to provider asset 131542 and CIK 0001175580.
- Barrick's former GOLD ticker maps to current canonical ticker B under CIK 0000756894 without merging it into Randgold history.
- MMX is scoped to Maverix provider asset 659463 because the raw ticker has been reused.
- Aliases are never loaded as separate securities and alias intervals for the same text ticker cannot overlap.

Schema migration v2 adds `dim_ticker_alias`, stable `event_key` columns, and unique event-key indexes. Migration initialization accepts an owned v1 database only long enough to verify its identity and checksummed ledger, applies v2 transactionally, updates the identity row, and then requires exact v2 identity for normal operation.

The Stage 2B loader performs this sequence:

1. Validate the candidate census policy and immutable candidate manifest.
2. Validate the Stage 2B policy and four-artifact manifest before opening a write transaction.
3. Verify exact CSV schemas, key uniqueness, row counts, cohort counts, provider identities, dates, source URLs, review states, and fail-closed flags.
4. Cross-check every promoted membership against its original census ticker, cohort, provider symbol, provider asset ID, company name, industry, and quoted date range.
5. Require the validated 134-name current universe and its eight cohort-to-parent mappings to exist.
6. Store all four CSV byte payloads in the raw layer with immutable hashes.
7. Upsert inactive companies, securities, CIK/ticker identifiers, taxonomy, and historical membership.
8. Resolve and upsert ticker aliases, including current-security aliases such as Barrick B.
9. Resolve and upsert 22 security events and 20 terminal-event records by stable event key.
10. Commit once. Any collision, missing canonical security, source-registration failure, or constraint error rolls back raw and canonical writes together.
11. Validate current-universe preservation, exact historical key/count sets, raw-payload hashes, foreign keys, unresolved terminal count, and zero calibration eligibility.
12. Publish atomic validation artifacts and an output manifest.

Operator sequence from the repository root:

```powershell
python basic_materials/scripts/00a_validate_basic_materials_independence.py
python basic_materials/scripts/00_init_basic_materials_db.py
python basic_materials/scripts/01_load_basic_materials_universe.py
python basic_materials/scripts/02_validate_basic_materials_universe.py
python basic_materials/scripts/02b_validate_basic_materials_deactivated_candidates.py
python basic_materials/scripts/01b_load_basic_materials_historical_membership.py
python basic_materials/scripts/02c_validate_basic_materials_historical_membership.py
python -m pytest basic_materials/tests -q
```

Implemented Stage 2B acceptance tests cover exact static contracts, manifest integrity, prohibited calibration activation, invalid provider identity, atomic rollback, repeat-load idempotency, migration, preservation of the 134 current rows, exact database counts, and the full CLI sequence. Stage 3 extended the suite to 24 passing tests, including stable market identities, current major-exchange listing-window controls, provider cache fencing, terminal formulas, and no-lookahead behavior. Stage 4A extends it to 29 passing tests, including v1/v2/v3-to-v4 migration paths, exact profile/metric contracts, SEC acceptance-bound profile inference, ordinary 6-K exclusion without financial XBRL evidence, reporting-currency non-substitution, row-hash tamper rejection, and atomic idempotent contract loading.

Stage 2B pilot completion did not close the historical calibration gate. Stage 3 may change database `resolved` only when a governed terminal calculation succeeds. It may not change source-contract `survivorship_complete=0`, membership `calibration_eligible=0`, model promotion state, or portfolio authority. After the implemented Stage 3 run, 16 database reconciliation rows are evidence-backed as resolved and four remain unresolved; all 20 remain calibration-ineligible.

## Stage 3 — Adjusted market data and corporate actions

Status: implemented and independently validated on 2026-09-05. This stage is market-feature-ready, not score-ready or calibration-ready.

### Stage 3 contract-first design

The governed v2 policy is `data/basic_materials_market_data_policy.yaml`. The
immutable manifest is `data/basic_materials_market_data_manifest.yaml`. It
seals two reviewed files:

- `system_csvs/basic_materials_market_instruments.csv`: 162 role rows over 158 unique Norgate assets;
- `system_csvs/basic_materials_terminal_return_rules.csv`: 20 terminal outcome rules.

The 162 roles are exact: 134 current-universe roles, 20 historical-pilot roles, XLB, SPY, and six event-specific stock-successor roles. Shared provider assets are represented once in `dim_market_instrument` and may have several rows in `bridge_market_instrument_role`. A role is never joined to history on raw ticker alone.

The market-instrument contract records provider source, database, symbol,
immutable asset ID, first and last quoted dates, expected load window, role,
event link, currency, review status, and current-gate applicability. For a
current role, the builder additionally requires the authoritative universe to
say active/investable/primary, Norgate to resolve the asset in `US Equities`
with no terminal quoted date, and the latest
`major_exchange_listed_timeseries` observation to equal one. Its expected
window begins with the first major-exchange-listed provider session on or after
the 2009-01-02 history boundary. The builder fails closed when any condition is
absent. `scripts/02d_build_basic_materials_market_instrument_review.py` is a
deliberate review-artifact builder, not a daily refresh step. Replacing an
existing reviewed contract requires `--replace-reviewed-contract`, a new
fingerprint, and a matching manifest edit.

The ZEUS event preserves the economic successor ticker RYI from the merger terms while mapping the provider lineage to RYZ asset ID 1606887. This is an event-specific provider override, not a rewrite of the Stage 2B economic record. RGLD, B, AG, and NTR successor assets are shared where they already exist in the current contract. NTR has two event roles, one for POT and one for AGU. TFPM is reused through its current-universe role for the mixed MMX calculation.

### Schema v3

Migration `basic_materials_adjusted_market_data` adds only Basic Materials-owned tables:

- `dim_market_instrument` and `bridge_market_instrument_role` for stable provider identity and semantic role separation;
- `fact_market_provider_snapshot` for provider fingerprints, contract hash, extraction date, cache manifest hash, and load status;
- `fact_adjusted_price_bar` for raw OHLC/close plus Norgate total-return adjusted close;
- `fact_corporate_action` for cash dividends and provider capital-event flags;
- `dim_trading_calendar_session` for the SPY-derived `XNYS_PROXY_SPY` calendar;
- `fact_market_data_coverage` for per-role expected-versus-observed history and rank readiness;
- `dim_terminal_return_rule` and `fact_terminal_return_calculation` for reviewed terminal economics and calculation evidence; and
- `feature_market_technical` for one point-in-time market feature row per current security and as-of date.

The v3 migration is append-only and checksummed. Under the current schema-v6
initializer, an owned older database advances through every missing migration
in ascending order; an unidentified non-empty database or any ledger checksum
mismatch is rejected.

### Schema v4 financial-contract foundation

Migration `basic_materials_financial_contract` adds the ten Basic
Materials-owned tables listed in Section 6.4 plus supporting indexes. The
migration is additive: Stage 2 identities and memberships and Stage 3 market
history remain unchanged. The contract loader stores the three sealed contract
payloads in the package raw layer, upserts the 22 canonical metrics and 66
taxonomy links, and replaces the 154 profile rows in one transaction. A failed
hash, row-level profile digest, identity join, foreign key, or policy check
prevents all Stage 4A canonical writes.

### Provider extraction and publication

`core/norgate_runtime.py` owns the Norgate database fingerprint and mid-run snapshot fence. `core/norgate_prices.py`:

1. reads only the loaded governed roles;
2. verifies every current provider symbol still resolves to the contracted asset ID;
3. requests raw unadjusted OHLCV/dividend fields and total-return-adjusted close for the same date window;
4. rejects duplicate, unordered, out-of-window, mismatched, nonpositive, or malformed bars;
5. captures capital-event flags and cash dividends;
6. writes one atomic canonical cache CSV per provider asset plus a hash-sealed cache manifest under `output/basic_materials/cache/norgate/<as-of>`;
7. rechecks both Norgate equity database timestamps before publication; and
8. publishes the snapshot, raw cache manifest, bars, actions, and SPY sessions in one database transaction.

Any asset-ID mismatch, provider update during extraction, missing required history, cache-hash change, invalid bar, or database error prevents market-data publication. Cache files can exist after a pre-publication failure, but no partial provider snapshot is committed.

### Coverage, listing regimes, and the Basic Materials sparse-session rule

The coverage audit retains strict diagnostics against SPY sessions: missing-session ratio, longest missing run, late start, stale end, invalid bars, and observation count. Full `complete` status still requires the strict policy thresholds.

Basic Materials also needs a separate rank-readiness decision because foreign
issuers, ADRs, Canadian listings, and thin miners can have valid but sparse U.S.
quote histories. The implemented hybrid rule therefore allows a current
security with at least 253 valid observations to be rank-ready when it is fresh,
its missing-session ratio is no more than 45%, and its longest missing run is no
more than 120 SPY sessions. The sparse diagnostics remain visible as `partial`;
they are not relabeled as complete. A true recent listing can be rank-ready with
shorter history under the explicit recent-listing policy. XLB and SPY do not
receive the sparse-security exception.

The v2 correction distinguishes provider asset history from the current U.S.
listing regime. Norgate may preserve thin OTC, ADR, foreign-market, predecessor,
or pre-uplisting observations under the same immutable economic asset. Those
observations are useful lineage evidence, but they are not expected SPY sessions
for current-listing coverage and must not fill 21/63/126/252-session features.
Coverage and current-security features therefore begin at the governed
major-exchange start. Recent-listing detection compares the first loaded bar
with that governed start rather than the provider asset's oldest quote.

The five-name review concluded that none was inactive:

| Ticker | Active U.S. listing as of 2026-09-05 | Governed start | Resolution evidence |
|---|---|---:|---|
| ARIS | NYSE: ARIS | 2023-09-14 | Aris began the same-security U.S. major-exchange lineage as NYSE American: ARMN on 2023-09-14, then moved to NYSE: ARIS on 2026-02-19. [Initial listing](https://aris-mining.com/news/aris-mining-commences-trading-on-the-nyse-american-as-armn/) · [current NYSE ticker](https://www.sec.gov/Archives/edgar/data/1964504/000175392626000336/g085137_ex99-1.htm) |
| AUGO | Nasdaq Global Select Market: AUGO | 2025-07-16 | The issuer's SEC-filed release states that Nasdaq trading began on 2025-07-16. [SEC exhibit](https://www.sec.gov/Archives/edgar/data/1468642/000095010325008953/dp231644_ex9901.htm) |
| CRH | NYSE: CRH | 2023-09-25 | CRH identifies 2023-09-25 as the start of its NYSE primary listing. [CRH listing page](https://www.crh.com/investors/share-price/) |
| MTA | NYSE American: MTA | 2020-01-08 | Metalla commenced NYSE American trading and ceased OTCQB trading on 2020-01-08. [Issuer release](https://www.metallaroyalty.com/_resources/news/2020/nr-20200108.pdf) |
| TII | NYSE American: TII | 2025-11-21 | Titan announced that its shares were trading on NYSE American on 2025-11-21 and had ceased OTCQB trading. [Issuer release](https://www.titanminingcorp.com/news/news-releases/titan-mining-commences-trading-on-the-nyse-american-as-tii) |

The local provider review independently places all five in `US Equities`,
returns no terminal quote date, sets their latest major-exchange flag to one,
and supplies a fresh 2026-09-04 bar. Under the corrected windows, ARIS has
747/747 sessions, AUGO 288/288, CRH 740/740, MTA 1,674/1,674, and TII 197/197,
with zero missing sessions in each case. The first four are `complete`; TII is
`recent_listing_short_history`. All five are rank-ready.

### Terminal-return reconciliation

`core/terminal_returns.py` requires the exact final historical quote and enforces a maximum seven-calendar-day successor reference lag. Every selected quote must be on or before the calculation as-of date. Calculations retain raw and adjusted final prices, successor price date, cash and stock components, share ratio context, distribution component, currency, fractional-share treatment, snapshot key, rule hash, and evidence hash.

Implemented outcomes:

- nine fixed-cash events: cash consideration per original share;
- six stock-conversion events: reviewed successor ratio times the first valid successor close on or after the reference date;
- one mixed/prorated event: 15% of MMX cash consideration plus 85% of its TFPM share-conversion value; and
- four bankruptcy/liquidation events: ANV, MCP, GMO, and BIOA remain `pending_distribution_evidence`, with null terminal value and explicit calibration exclusion.

Continuous per-original-share value is used for stock and mixed fractional shares. No unverified bankruptcy distribution is encoded as zero. A successful terminal calculation can set the database reconciliation flag to `resolved=1`; it never changes historical membership or model calibration flags.

### Market features

Core market features:

- 21/63/126/252-session returns;
- 12-month return excluding latest month;
- XLB residual momentum;
- SPY beta-residual momentum;
- realized and downside volatility;
- maximum drawdown;
- distance from 52-week high;
- 50/200-day trend;
- average dollar volume;
- price and history-age flags.

Features are calculated with
`expected_start_date <= bar_date <= asof_date` for current securities. This
prevents legacy OTC/ADR observations from manufacturing a full 252-session
history after a recent uplisting. Close-to-close return features use
total-return-adjusted close. Average dollar volume uses raw close and volume.
XLB and SPY enter only through their governed benchmark roles. Feature quality
remains one of `full`, `partial_history`, `insufficient_history`, or `stale`
with machine-readable reasons. ELE, MAKO, OGC, SCZM, SOLS, TII, and VMET
currently retain `partial_history` labels; all seven are rank-ready under the
governed recent-listing rule.

### Commands and reusable execution order

Run from the repository root:

```powershell
python basic_materials/scripts/02d_build_basic_materials_market_instrument_review.py
python basic_materials/scripts/03_load_basic_materials_market_contract.py
python basic_materials/scripts/03_run_basic_materials_market_stage.py --as-of YYYY-MM-DD
python basic_materials/scripts/04_validate_basic_materials_market_data.py --as-of YYYY-MM-DD
python basic_materials/scripts/02_validate_basic_materials_universe.py
python basic_materials/scripts/02c_validate_basic_materials_historical_membership.py
python -m pytest basic_materials/tests -q
python -m ruff check basic_materials
```

The contract builder is run only when intentionally creating or replacing the reviewed identity artifact. Routine refresh starts with `03_load_basic_materials_market_contract.py` to revalidate the manifest, then uses the full Stage 3 runner. The final two validators prove that earlier universe and historical contracts remain intact.

### Implemented live acceptance evidence

The 2026-09-05 live database at `C:/Users/josel/Documents/STAGING/DB/basic_materials.sqlite` passed with:

- 158 unique provider instruments and 162 roles;
- 537,739 adjusted daily bars;
- 5,648 cash-dividend/capital-event records;
- 4,446 SPY trading-calendar sessions;
- 162 role coverage rows;
- 134 current-security feature rows: 127 full and seven partial-history;
- 136 of 136 current/benchmark gate roles rank-ready, or 100%;
- 20 terminal calculations: 16 resolved and four explicitly pending;
- zero future-price violations and zero foreign-key errors;
- unchanged 134 current and 20 historical memberships; and
- 29 passing package tests plus a clean static check after Stage 4A.

The evidence pack is written under `output/basic_materials/stage3/2026-09-05`. The canonical provider cache is under `output/basic_materials/cache/norgate/2026-09-05`.

Gate:

- only adjusted sources enter scoring;
- no duplicate price keys;
- split and dividend tests pass;
- delisted terminal-return tests pass;
- current rank-ready market coverage is at least 95%;
- benchmark and trading-calendar coverage is complete;
- stale or insufficient-history names remain visible with reasons.

Gate result: passed for Stage 3 engineering use. Calibration, scoring, portfolio
use, and promotion remain closed. All current securities are rank-ready; seven
recent-listing feature histories retain explicit partial-history labels. Four
pending bankruptcy distributions remain visible and must be reviewed in later
work; they are not silently imputed.

## Stage 4 — SEC fundamentals, reporting profiles, and FX

Status: Stage 4A contract and reporting-profile foundation implemented on
2026-09-05; Stage 4B point-in-time ingestion, normalization, FX, and common
features implemented and independently validated on 2026-09-06; Stage 4C OGC
and security-unit remediation implemented as a fully validated acceptance
candidate on 2026-09-06. Live replacement remains separately authorized. No
financial score exists and no calibration or portfolio gate is open.

### Stage 4A implemented foundation

The contract-first slice freezes policy before bulk fact ingestion:

- `data/basic_materials_financial_data_policy.yaml` defines SEC source
  precedence, accepted annual/interim forms, Company Facts fallback rules,
  acceptance-time availability, non-destructive amendments, reporting cadence,
  reported-currency evidence, point-in-time FX methods, 22 canonical metrics,
  22 planned common features, and four closed promotion flags;
- `data/basic_materials_financial_concept_map.yaml` maps the 22 canonical
  metrics to 66 reviewed US-GAAP/IFRS concepts with statement, period, unit,
  and sign semantics;
- `system_csvs/basic_materials_reporting_profiles.csv` is the governed
  154-row issuer census, and
  `system_csvs/basic_materials_reporting_overrides.csv` is an intentionally
  empty, exact-schema override contract rather than a place for implicit fixes;
- `data/basic_materials_financial_data_manifest.yaml` seals all four inputs;
- the package source registry advances to v3 with 16 sources, adding SEC
  submissions, SEC Company Facts, inline-XBRL fallback, Yahoo FX, the reporting
  census, and the metric policy;
- `core/reporting_profiles.py` builds acceptance-bounded profiles from SEC
  submissions, submissions archives, and Company Facts, treating a 6-K as
  financial only when its accession has Company Facts evidence;
- `core/financial_data_contract.py` strictly validates the policy, manifest,
  profile row hashes, universe identities, metric/concept registry, and closed
  flags, then loads the contract atomically and emits read-only validation
  reports; and
- scripts `04a_build_basic_materials_reporting_profiles.py`,
  `05_load_basic_materials_financial_contract.py`, and
  `06_validate_basic_materials_financial_contract.py` separate deliberate
  artifact rebuilding, deterministic loading, and independent validation.

Frozen artifact evidence:

| Artifact | Rows | SHA-256 |
|---|---:|---|
| Reporting profiles | 154 | `8d562825a53286e37fca486d36f295129e9f38597b4ce4216508817a64e16230` |
| Financial concept map | 22 metrics / 66 links | `beee0b7aaef49afb12d6ee0e43834df1f1eb330de92c13ad0a7aed6984c25aad` |
| Financial policy | 1 contract | `087af14909223dd94b60450b44c75fa2f57219c6fbbc6c810c3615d6dc1da8ba` |
| Reporting overrides | 0 | `4705439481a5c9c339f4fddcb09924a8bb72325f6be7fcf41196d8a244f1b4ce` |
| Financial manifest | 4 artifacts | `02c240f6ea9aa815f1faa949fb3dfa53b093fd58061cfc151c021663b6cbb237` |

The SEC census cache contains 315 payloads; Company Facts is explicitly absent
for MAKO and SCZM. The 154 profiles classify as 102 domestic SEC, 16 foreign
private issuers, 34 Canadian MJDS issuers, and two unknown; accounting basis is
102 US-GAAP, 45 IFRS, and seven unresolved. Reporting currency is 143 USD, two
CAD, one AUD, one KRW, and seven unresolved. No unresolved currency was filled
from the listed security's trading currency.

Profile readiness is explicit rather than binary. The 140
`ready_for_ingestion` profiles may enter the standard Stage 4B Company Facts
path. Six `companyfacts_fallback_required` profiles—ASM, BHP, CGAU, CMCL, PKX,
and TII—need the governed filing fallback. Six `taxonomy_review_required`
profiles—AUGO, MAKO, SCZM, VMET, and historical AGU and POT—need evidence-backed
taxonomy/currency treatment. OGC and RMIX are
`annual_form_review_required`. These 14 exceptions remain visible and cannot
silently enter canonical facts.

Schema v4 and the Stage 4A contract were loaded twice into the live dedicated
database with identical count and contract-hash results: 154 profiles, 22
metrics, 66 concept links, and three raw contract payloads. The read-only
validator reported zero errors. All Stage 0–3 validators passed afterward, and
the full package suite passed 29 tests with a clean static check.

Stage 4A operator sequence from the repository root:

```powershell
# Deliberate rebuild only; review the diff and update the manifest afterward.
python basic_materials/scripts/04a_build_basic_materials_reporting_profiles.py --replace-reviewed-contract

# Normal contract load and independent validation.
python basic_materials/scripts/05_load_basic_materials_financial_contract.py
python basic_materials/scripts/06_validate_basic_materials_financial_contract.py
python basic_materials/scripts/00a_validate_basic_materials_independence.py
python basic_materials/scripts/02_validate_basic_materials_universe.py
python basic_materials/scripts/02c_validate_basic_materials_historical_membership.py
python basic_materials/scripts/04_validate_basic_materials_market_data.py
python -m pytest basic_materials/tests -q
python -m ruff check basic_materials
```

Stage 4A gate result: passed for engineering use and preserved as commit
`4b6bce4`. Stage 4B extends that contract append-only; it does not weaken or
rewrite the Stage 4A evidence boundary.

### Stage 4B implemented point-in-time financial layer

The implemented slice follows the six-step release sequence used here as the
reusable pattern for future sector repositories:

1. **Checkpoint the contract.** Stage 4A was committed cleanly before fact
   implementation so policy/schema review remains separable from data loading.
2. **Create immutable SEC ingestion.** `core/financial_ingestion.py` verifies
   every Stage 4A contract hash and the 315-payload reporting cache, reads root
   and archive submissions, stores exact SEC acceptance times, caches governed
   filing-package evidence atomically, and seals the final cache manifest.
3. **Load the standard path.** All 140 `ready_for_ingestion` profiles use the
   same Company Facts path; missing acceptance times are quarantined instead of
   being inferred from filing or period dates.
4. **Run a representative normalization pilot.** NUE, BHP, AEM, and RMIX cover
   domestic quarterly, foreign annual/inline-XBRL, Canadian mining, and
   interim-only/recent-listing behavior before the full rebuild.
5. **Resolve the 14-profile queue.** Every exception has an exact route,
   accession, document, acceptance timestamp, source hash, currency/taxonomy
   decision, and explicit canonical-eligibility state.
6. **Add FX and common features.** AUD, CAD, EUR, KRW, and USD observations are
   stored with immutable source evidence; canonical facts retain reported and
   USD values; 22 common financial features plus coverage/readiness rows are
   published for all 134 current securities.

Implemented files and boundaries:

- `data/basic_materials_financial_ingestion_policy.yaml` is the Stage 4B
  authority. It is bound to the Stage 4A manifest, profile census, concept map,
  policy, and reporting-cache hashes and fixes the 2026-09-05 cutoff.
- `core/financial_ingestion.py` owns SEC filing/fact ingestion, official filing
  package fallback, immutable evidence manifests, the resolution ledger, and a
  fail-closed reuse path for an already loaded matching snapshot.
- `core/financial_fx.py` owns direct/inverse Yahoo FX extraction, USD identity
  rows, exact payload hashes/times, and point-in-time rate loading.
- `core/financial_normalization.py` owns source/concept precedence, duplicate
  and conflict policy, signs and units, amendments/supersession, quarter and
  YTD differencing, annual fallback, TTM/prior-period construction, reported
  and USD values, and common feature generation. Weighted-average diluted
  shares are selected by period and never summed.
- `core/financial_validation.py` independently checks source seals, exact
  profile/exception states, no-lookahead, lineage, units/currencies/FX,
  canonical conflicts, feature counts, foreign-security valuation gates,
  stored error-severity audit rows, and foreign keys.
- scripts `07` through `10` expose each boundary separately;
  `11_run_basic_materials_stage4.py` is the normal end-to-end command.
- schema v5 adds the profile-resolution and normalization-issue ledgers plus
  the Stage 4B lineage fields. Schema v6 adds indexes on
  `(snapshot_key, ticker)` and `superseded_by_fact_id`; the latter prevents
  SQLite's self-referencing canonical-fact foreign key from turning bounded
  rebuilds into quadratic scans.

The governed exception result is:

| Resolution | Tickers | Treatment |
|---|---|---|
| `resolved_inline_xbrl` | AUGO, BHP, CMCL, PKX, VMET | Official inline-XBRL fallback supplements or replaces lagging Company Facts |
| `resolved_xbrl_instance` | MAKO, SCZM, TII | Full filing-package XBRL instance; TII uses the original 40-F rather than the cover-only amendment |
| `resolved_metadata_unstructured` | AGU, POT | Historical metadata evidence only; no unsupported structured facts are manufactured |
| `resolved_metadata_unstructured` | ASM, CGAU | Older Company Facts remain canonical-eligible; latest annual exhibits are explicitly unstructured gaps |
| `resolved_interim_only` | RMIX | Available domestic interim Company Facts are retained; unavailable annual/TTM values stay null |
| `missing_required_source` | OGC | No cutoff-valid structured source; current feature row is explicitly blocked |

Normalization and availability rules are fail-closed:

- a fact is usable only with an exact accession-matched acceptance timestamp on
  or before the issuer cutoff and not before its own period end;
- facts lacking exact acceptance remain in the raw audit layer with a reason;
- Company Facts has priority over governed filing fallback; equal-rank
  disagreements are conflicts, while equal duplicates collapse with lineage;
- amendments remain visible and supersession is explicit;
- duration facts use quarter/YTD/annual cadence-aware logic; instant facts and
  diluted shares use latest valid observations; and
- foreign listed-security valuation remains null until an explicit
  ADR/ordinary-share ratio contract exists. Reporting currency is never
  inferred from listing currency.

Stage 4B live acceptance evidence for snapshot
`basic_materials_sec:2026-09-05:06030312536c4d01f7ed`:

| Measure | Result |
|---|---:|
| Profiles / standard / exceptions | 154 / 140 / 14 |
| Company Facts / fallback payloads | 152 / 26 |
| SEC filings | 5,754 |
| Raw facts | 310,067 |
| Usable / quarantined raw facts | 253,687 / 56,380 |
| Canonical facts | 239,705 |
| Usable / superseded canonical facts | 109,210 / 130,495 |
| Canonical conflicts / missing FX | 0 / 0 |
| FX observations | 22,852 across AUD, CAD, EUR, KRW, USD |
| Current feature / coverage rows | 134 / 134 |
| Feature quality | 94 full, 29 partial, 9 insufficient, 1 stale, 1 blocked |
| Rank-ready / valuation-ready | 49 / 84 |

The pilot produced full rows for AEM and BHP, a partial row for NUE because its
mapped operating-income TTM is unavailable, and an insufficient row for RMIX
because only interim history exists. These outcomes are accepted missing-data
states, not imputed successes.

ARIS, AUGO, CRH, MTA, and TII are all active securities. AUGO and TII are now
financially rank-ready. ARIS and MTA have usable facts but remain non-rank-ready
because required common metrics and foreign share ratios are missing. CRH has
all 22 metrics but three stale observations and therefore remains
conservatively non-rank-ready for this cutoff.

Operator sequence from the repository root:

```powershell
# Normal end-to-end run for a reviewed policy/cache snapshot.
python basic_materials/scripts/11_run_basic_materials_stage4.py

# Deterministic offline replay after the immutable caches are sealed.
python basic_materials/scripts/11_run_basic_materials_stage4.py --cache-only

# Independent read-only acceptance check.
python basic_materials/scripts/10_validate_basic_materials_financial_stage.py
```

For diagnosis, the equivalent component order is `07` ingestion, `09
--pilot-only`, `08` FX, `09` full normalization/features, and `10` validation.
Do not use `--allow-partial` for an acceptance run.

The final scratch run, live load, and live cache-only rerun produced the same
snapshot key and exact row counts. Validation passed with zero errors and two
expected warnings: 48 foreign/current securities remain valuation-gated until
share ratios are governed, and OGC remains explicitly source-blocked. The full
package suite passes 36 tests, Ruff is clean, and every independent Stage 0–4B
validator passes after the live schema-v6 migration. A consistent pre-load
backup is retained at
`C:/Users/josel/Documents/STAGING/DB/basic_materials.pre_stage4b_20260906.sqlite`
with SHA-256
`037f65f73e1c93c8e1a11048200d73725ca62625c325945fd1860d9cf8678258`.

Stage 4B gate result: passed for engineering and future shadow-feature use.
Calibration eligibility, specialized-metric weights, score/rank publication,
`portfolio_candidate_gate`, and `oos_score_valid_flag` remain closed.

### Stage 4C implemented OGC and security-unit remediation

Stage 4C closes the two current-snapshot fail-closed queues left by Stage 4B
without weakening historical or calibration gates.

Build:

1. `data/basic_materials_security_ratio_policy.yaml` defines
   `issuer_shares_per_traded_security`, the market-cap formula, exact current
   coverage, ADS values, SEC evidence requirements, and explicit prohibitions
   on inference and historical backfill.
2. `system_csvs/basic_materials_security_share_ratios.csv` contains 47 reviewed
   effective-dated rows with CIK, exchange, registered title, basis, ratio,
   exact SEC accession/form/acceptance/document, URL, and review lineage.
3. `core/security_ratios.py` verifies the CSV hash and ticker census, proves
   each listed title appears in the SEC document, proves every ADS conversion
   phrase, seals payloads, loads the ratio dimension atomically, and reports
   evidence.
4. `core/audited_html_financials.py` extracts 21 OGC metrics for 2024 and 2025
   from the audited IFRS exhibit furnished on May 1, 2026. Exact statement
   tables plus balance-sheet, ending-cash, and net-profit tie-outs are required.
5. `core/financial_ingestion.py` adds `resolved_audited_html`, binds financial
   policy v2 to both ratio artifacts, and uses a new immutable
   `sec_financials/2026-09-05-v2` cache namespace.
6. `core/financial_normalization.py` applies
   `price * diluted issuer shares / issuer shares per traded security` and
   stores the complete ratio/evidence/policy lineage on applicable features.
7. Schema v7 adds `dim_security_share_ratio`, feature ratio lineage, and the
   audited-HTML resolution state. Source registry v4 adds the audited-HTML and
   security-ratio sources.
8. Validation requires the exact ratio census, no overlaps, the exact five ADS
   conversions, no unresolved ratio-required current foreign listing, exact
   market-cap arithmetic, and exactly 42 OGC raw and 42 canonical facts.

The five ADS conversions are issuer shares represented by one traded ADS:

| Ticker | Issuer shares per ADS |
|---|---:|
| BHP | 2 |
| ELVR | 10 |
| PKX | 0.25 |
| RIO | 1 |
| TX | 10 |

The other 42 contracted foreign securities are direct common/ordinary-share
listings with ratio 1. A ratio-1 ADS such as RIO remains classified as an ADS.
RMIX is a governed domestic interim filer and sits outside this 47-row foreign
contract.

OGC evidence is accession `0001628280-26-029399`, accepted exactly at
`2026-05-01T20:53:37Z`, document
`exhibit991-oceanagoldfinan.htm`. The source is retained as audited HTML, not
misrepresented as XBRL. OGC is now `full`, with 21 of 22 common metrics and a
direct-share ratio of 1.

Fresh isolated acceptance and cache-only replay reproduce snapshot
`basic_materials_sec:2026-09-05:2cf5219d8855588066ba`:

| Measure | Stage 4C result |
|---|---:|
| Profiles / standard / exceptions | 154 / 140 / 14 |
| Security ratios / direct / ADS | 47 / 42 / 5 |
| SEC filings | 5,754 |
| Raw / canonical facts | 310,109 / 239,747 |
| OGC raw / canonical | 42 / 42 |
| FX observations | 22,851 |
| Current feature / coverage rows | 134 / 134 |
| Feature quality | 95 full, 29 partial, 9 insufficient, 1 stale, 0 blocked |
| Rank-ready / valuation-ready | 50 / 87 |
| Validation errors / warnings | 0 / 0 |

The Stage 4C checkpoint passed 38 tests; the current package passes 41 tests,
Ruff, and the 60-file independence scan. The
verified database remains at
`output/basic_materials/verification_stage4c_20260906/basic_materials.sqlite`.
Replacing the installed live database is an explicit deployment action and was
not performed without that authorization.

Stage 4C passes as a current engineering acceptance candidate. It does not
authorize historical ratio backfill, calibration, specialized weights,
score/rank publication, or portfolio use.

### Stage 4D implemented - historical PIT feasibility preflight only

Stage 4B/4C loaded long source histories, but published only one current
financial-feature date:

| Layer | Verified coverage |
|---|---|
| Raw SEC facts | 2009-01-01 to 2026-07-05; 147,577 rows / 145 tickers from 2019-01-01 |
| Canonical facts | 2009-01-03 to 2026-07-05; 134,378 rows / 145 tickers from 2019-01-01 |
| Adjusted prices | 2009-01-02 to 2026-09-04 |
| FX | 2009-01-01 to 2026-09-04 |
| Common financial features | 134 rows at one as-of date: 2026-09-05 |

The historical model-ready time series is therefore not yet aligned with the
other repositories. Stage 4D did not write a common-only historical feature
panel that would later be rebuilt after specialized extraction. It performed a
read-only feasibility and partition preflight while retaining pre-2019 source
history for TTM construction and market warm-up.

Build:

- an immutable 93-date monthly last-session calendar from 2019-01-31 through
  2026-09-04;
- a read-only effective-membership, lifecycle, filing-availability, amendment,
  FX, price, ratio, and terminal-event feasibility matrix;
- explicit missing historical ratio, filing, currency, market, membership, and
  terminal-event queues;
- expected issuer/date and cohort/date row counts for the eventual unified
  panel;
- a deterministic partition plan identifying source warm-up and rebuild
  boundaries; and
- input hashes that Stage 6C must reproduce before materialization.

Gate:

- no historical feature, score, outcome, or rank rows are written;
- zero future-availability or current-universe-only history leakage in the
  streamed feasibility checks;
- no backward projection of 2025/2026 ratio evidence;
- every gap has an owner and terminal or remediable disposition;
- the same cache-only preflight reproduces identical counts and hashes; and
- calibration and portfolio flags remain closed.

## Stage 5 — Positioning, commodity data, and foundation audit

Stage 5A begins after the no-write Stage 4D feasibility preflight and before
any historical feature materialization. It closes the metric, universe, and
source scope needed for a one-pass extraction.

Build:

- resolve or reject the remaining 52 Stage 2B deactivated candidates and close
  the four pending bankruptcy-distribution treatments before the parser
  universe seal;
- read-only imports from SEC ownership and market_positioning;
- source-birthdate-aware positioning features;
- commodity registry and company exposure map;
- FRED/ALFRED, EIA, USGS, and USDA source adapters selected through policy;
- immutable raw and canonical commodity/cycle observations with release or
  vintage timestamps;
- a versioned specialized-metric, supporting-operand, formula, definition-
  variant, and table-family registry;
- an exact current-plus-historical ticker-by-metric applicability matrix;
- a complete declared SEC, issuer-IR, local-exchange, technical-report, and
  archived-source document census with source birthdates, exact parser targets,
  and terminal source-gap dispositions;
- expected-period and read-only historical coverage projections; and
- a foundation-readiness audit.

Gate:

- external databases remain unchanged;
- stale positioning rows fail their own feature gate;
- revised macro data use approved vintage handling;
- each company-cycle exposure has a dated source and confidence;
- no overlay has production weight;
- earliest reproducible score date is reported honestly;
- every specialized candidate is explicitly sourceable, deferred, or rejected
  before parser implementation starts;
- every governed issuer/metric pair has applicability and source-lane
  accounting;
- no source hydration or production-document parse starts until the registry,
  applicability matrix, source census, and fixture contract are hash-frozen;
- no historical feature panel exists; and
- calibration remains blocked if historical membership or terminal events are
  incomplete.

The Stage 5 decision is one of:

1. continue to common scoring and specialized research;
2. continue as a limited shadow model; or
3. defer expensive parser work while preserving the foundation.

Formal specialized-metric identification occurs in Stage 5A after the Stage 4D
preflight, not after a common historical panel. The output is a frozen
registry, definition variants, supporting operands, applicability matrix,
sourceability decision set, and sealed all-source plan; it does not yet create
score weight.

## Stage 6A — Common scoring feature contract

Build:

- versioned component registry;
- atomic raw and percentile scores;
- exact-cohort normalization with fallback logging;
- component scores;
- missing/applicability semantics;
- confidence and rank-readiness logic;
- current snapshot output;
- immutable contract hash.

Gate:

- all 134 current tickers appear;
- every weighted component is defined;
- component weights sum to one;
- no future source timestamp is present;
- not_applicable and missing remain distinct;
- negative-profit valuation cap passes;
- rank-ready fraction is at least 85% or the model remains limited shadow;
- reruns are deterministic.

## Stage 6B — Specialized metrics and cohort overlays

Specialized observations are loaded and coverage-frozen in Stage 6B after the
Stage 4D preflight, Stage 5A source/metric contract, and Stage 6A component
shape pass. They enter measurement-only tables with zero score weight; source
coverage alone cannot promote them.

Build:

- Basic Materials-owned content-addressed raw-object and semantic-record
  caches;
- one complete hydration pass for the frozen all-source manifest;
- a Basic Materials adapter to `dedicated_parser` organized by reusable table
  families and contextual narrative rules;
- one unique-document compiler pass for XBRL, HTML, spreadsheet, Word, native
  PDF, and approved bounded OCR inputs;
- one resumable all-issuer/all-metric semantic evaluation;
- candidate, evidence, adjudication, accepted-fact, and coverage tables;
- policy-only replay and exact derived-metric construction from accepted
  operands;
- one optional consolidated residual source/parse batch under Section 11.6;
- final metric dispositions and a frozen accepted-observation manifest;
- cohort overlay features;
- measurement-only component outputs.

Adapter/fixture implementation order:

1. shared production, shipment, capacity, utilization, price, volume, mix, and
   unit-cost table families;
2. reserves/resources, grade, recovery, mine-life, ownership, and asset-mix
   table families;
3. sustaining/growth capex, project commitments, outages, turnarounds, and
   feedstock-sensitivity table families;
4. cohort-specific contextual and definition-variant rules; and
5. full golden fixtures and plan-only source validation before the one
   production-document run.

Gate:

- the Section 11.7 applicability, source, parser, current-breadth, historical-
  depth, and PIT-feasibility thresholds pass for the frozen core set;
- deterministic positive and prohibited fixtures for every parser table family
  and production-capable mapping;
- every source hash, semantic-record hash, evidence locator, parser work key,
  and review-policy hash reconciles;
- unit, period, scope, duplicate, conflict, and amendment tests;
- policy replay performs zero source opens/provider/OCR calls;
- no previously completed content hash is physically decoded in the residual
  batch;
- no parser output directly mutates scores;
- all specialized weights remain zero until separately promoted.

## Stage 6C — Single unified historical PIT materialization

Stage 6C is the first and only model-ready historical feature-panel write.
Build once from the Stage 4D partition preflight and the final Stage 6B
observation/coverage seals:

- the immutable Stage 4D candidate monthly or 21-session panel dates;
- point-in-time membership and lifecycle;
- point-in-time market, common financial, security-ratio, positioning,
  commodity/cycle, and accepted specialized features;
- forward adjusted returns;
- XLB excess and residual returns;
- SPY beta-residual robustness targets;
- terminal-event outcomes;
- sample-role and source-lineage fields.

Gate:

- zero future-availability violations;
- historical membership and terminal-event coverage pass;
- each source is absent before its birthdate;
- every accepted specialized value obeys period-end and
  publication/acceptance availability;
- the Stage 4D expected partition counts and input hashes reconcile;
- the Stage 6B observation and metric-subset hashes are unchanged;
- row hashes reproduce;
- no current-universe-only panel is labeled survivorship-correct;
- panel coverage by year and cohort is published;
- source-date reconciliation passes;
- a cache-only replay is byte-identical; and
- a second full historical materialization is prohibited unless a new frozen
  input contract is explicitly authorized.

## Signal diagnostics — Before weight optimization

Run:

- per-date Spearman rank IC;
- Newey-West inference;
- BH-FDR by pre-registered factor family;
- top-minus-bottom and top-quintile net return;
- monotonicity;
- rank persistence and turnover;
- chronological-half stability;
- commodity, inflation, rate, recession, and risk regime splits;
- cohort and lifecycle sensitivity;
- missingness and parser-coverage sensitivity;
- component ablations.

Decision:

- keep;
- keep as measurement-only;
- invert only when economic meaning and pre-registration support it;
- retire; or
- collect more evidence.

No optimization should start until this diagnostic registry is sealed.

## Stage 7 — Shadow scores

Build:

- registered common baseline;
- accepted component set;
- cohort and sector ranks;
- lifecycle-aware research scores;
- model version and contract hash;
- OOS validity fields set to zero before a genuine lock date;
- dated final-rank-format shadow outputs.

Gate:

- baseline reproduces exactly;
- every row has explicit eligibility and confidence;
- all 134 current members appear;
- no unvalidated specialized metric has weight;
- no shadow row is portfolio eligible.

## Stage 8 — Constrained calibration

Research contract:

- primary horizon: 63 trading days;
- secondary horizons: 21 and 126 trading days;
- D+1 adjusted-open execution;
- primary target: XLB residual/excess return;
- robustness target: SPY beta-residual return;
- rebalance grid: every 21 trading sessions;
- long-only top-quintile product objective;
- sector minimum cross-section: 40;
- cohort diagnostic minimum: 8;
- horizon-aware purge and embargo;
- expanding walk-forward;
- untouched outer test;
- deterministic seeds;
- no more than twelve pre-registered first-round candidates;
- cohort deviations shrunk toward the sector baseline;
- transaction-cost sensitivity at 10, 20, and 40 basis points;
- maximum top-sleeve cohort share: 35%;
- minimum positions: 10;
- maximum turnover target: 60%.

Promotion-facing evidence must include:

- positive net top-sleeve excess return;
- directionally correct IC;
- chronological and regime stability;
- positive or non-inferior walk-forward results;
- acceptable concentration and turnover;
- capacity and liquidity;
- no dependence on one ticker or one period;
- score reliability;
- artifact and source hashes; and
- a clean, never-optimized outer test.

Optuna, if used, remains report-only. It cannot update config, scores, or Portfolio Layer automatically.

## Stage 9 — Portfolio backtest

Evaluate:

- top decile and top quintile;
- equal-weight and score-weight;
- baseline versus calibrated candidate;
- XLB and SPY comparisons;
- D+1 execution;
- exact turnover and transaction costs;
- liquidity and ADV capacity;
- name and cohort concentration;
- drawdown and tail loss;
- regime attribution;
- lifecycle exclusions; and
- delisted/terminal-event handling.

The initial product is long-only. A short book is separate research because borrow availability is materially worse for small miners and recent listings.

Gate:

- positive net validation and outer-test economics;
- drawdown within approved tolerance;
- no unsupported concentration;
- capacity multiple passes the intended sector sleeve;
- candidate improves or is non-inferior to the registered baseline;
- results reproduce from sealed Stage 8 artifacts.

## Stage 10 — Dashboard and score contract

Publish one row for every current ticker.

Required fields:

- asof_date
- ticker
- company_name
- sector
- cohort_id
- calibration_group
- calibration_parent
- lifecycle_state
- final_score
- sector_rank
- cohort_rank
- cohort_percentile
- score_confidence
- rank_ready_flag
- rank_ready_reason
- calibration_eligible_flag
- portfolio_candidate_gate
- model_status
- model_version
- scoring_contract_version
- oos_score_valid_flag
- oos_score_asof_date
- oos_invalid_reason
- calibration_lock_date
- source-lineage hashes
- common component scores
- accepted overlay component scores
- applicable_missing_weight

Output:

- output/basic_materials/dashboard/YYYY-MM-DD/basic_materials_final_rank_table.csv
- output/basic_materials/dashboard/YYYY-MM-DD/basic_materials_score_review.csv
- output/basic_materials/dashboard/YYYY-MM-DD/basic_materials_data_quality.csv
- output/basic_materials/dashboard/YYYY-MM-DD/basic_materials_manifest.json

## Stage 10B — Governance

Build:

- signal registry;
- model lockbox;
- calibration and promotion receipt;
- model schedule;
- artifact-hash manifest;
- rollback contract;
- shadow/production state validator.

Only an explicit reviewed promotion may set:

- promotion_state=active;
- oos_score_valid_flag=1 for dates covered by the lock;
- portfolio_candidate_gate=1 where row gates pass; and
- a non-zero Portfolio Layer cap.

Historical reconstruction, calibration output, or a good current backtest cannot self-promote.

## Stage 11 — Portfolio Layer handoff

Use a file-only adapter.

Required changes after promotion evidence exists:

- add Basic Materials adapter semantics to portfolio_layer/scores;
- add source pipeline basic_materials;
- map sector benchmark XLB;
- add Basic Materials taxonomy to macro/risk configuration;
- read the dated final-rank file;
- reject stale, OOS-invalid, low-confidence, and gate-zero rows;
- resolve overlap with any other sector universe;
- start with a zero cap in shadow;
- set a non-zero cap only in a separately approved change.

Basic Materials must remain a separate orchestration database group:

    group_order:
      basic_materials: [basic_materials]

Initial registry behavior:

- db_group: basic_materials
- required: false
- require_oos_valid: false for shadow health checks
- portfolio cap: 0.00
- network: true
- publish_glob: output/basic_materials/dashboard/{date}/basic_materials_final_rank_table.csv

Promotion changes required, require_oos_valid, and the cap together. A partial state must fail closed.

## Stage 12 — Independent refresh orchestration

The canonical runner should:

- accept --asof;
- support --only/--skip-step;
- support --resume;
- enforce monotonic current refreshes;
- route older dates to a historical runner;
- hold a Basic Materials-specific lock;
- use immutable per-run logs;
- preserve successful network steps on retry;
- publish atomically;
- write last-attempt and last-success separately;
- verify row counts, freshness, hashes, model identity, and output schema;
- never run calibration or broad historical parsing during a normal daily refresh.

Recommended daily order:

1. identity and input-manifest preflight;
2. current universe validation;
3. incremental adjusted prices and corporate actions;
4. SEC submissions/facts and FX;
5. positioning import;
6. commodity/macro incremental data;
7. bounded parser pass on new documents;
8. market, financial, positioning, and cycle features;
9. common scoring;
10. accepted overlays;
11. final score and rank publication;
12. dashboard validation;
13. health and run manifest.

Historical backfill, parser census, calibration, and promotion remain separate commands.

## 13. Calibration and ranking details

### 13.1 One sector rank, eight cohort ranks

Publish both:

- cohort_rank: comparison against economically closest peers; and
- sector_rank: comparison of calibrated, cohort-neutralized opportunity across all Basic Materials names.

The sector rank must not be a raw comparison of gross margins or EV/EBITDA across miners, chemical companies, and royalty businesses. It is built from common component percentiles, accepted cohort-specific overlays, and a calibrated cohort-neutral score.

### 13.2 Hierarchical cohort policy

At launch:

- one sector-wide common component weight vector;
- exact-cohort atomic normalization;
- zero-weight specialized overlays;
- sector-wide calibration objective with cohort concentration limits.

After evidence:

- allow a bounded cohort deviation from sector weights;
- shrink the deviation toward the sector vector;
- require cohort breadth and independent windows;
- cap total specialized weight;
- preserve one final sector contract.

### 13.3 Mid-cycle valuation

Trailing valuation alone is dangerous in Basic Materials because peak margins make cyclicals look cheapest near the top.

Publish both:

- trailing valuation; and
- normalized/mid-cycle valuation.

Normalized valuation must use only point-in-time historical observations available on the as-of date. Candidate methods:

- trailing five-year median margin;
- rolling full-cycle percentile;
- commodity-price sensitivity evaluated at a frozen conservative reference;
- issuer guidance only after publication and with explicit provenance.

Normalized valuation remains measurement-only until its construction and predictive value pass.

### 13.4 Development-stage treatment

If the lifecycle audit identifies development or precommercial companies:

- do not score them on meaningless P/E, EV/EBITDA, ROIC, or margin percentiles;
- build a separate survival/readiness score from cash runway, committed capex, financing dependence, dilution, project stage, permitting, and liquidity;
- publish a lifecycle peer rank;
- set core operating portfolio eligibility to zero until the operating transition policy is met;
- keep them in the 134-row output.

## 14. Test plan

### 14.1 Independence and package tests

- banned import AST scan;
- path-containment tests;
- read-only external database tests;
- foreign database identity rejection;
- scratch DB and idempotent migration tests;
- owned schema v1/v2/v3-to-v4 migration and migration-checksum tests;
- config unknown-key and duplicate-policy tests;
- authoritative-input hash, count, schema, and inventory tests.

### 14.2 Universe tests

- exact 134-row and eight-cohort census;
- unique ticker/CIK/security checks;
- exact 20-row Stage 2B pilot and eight-cohort historical census;
- immutable four-file manifest hash, byte-size, row-count, schema, and unique-key checks;
- candidate-census-to-promoted-membership reconciliation;
- ticker reuse and non-overlapping effective-dated alias fixtures;
- ADR/ordinary-share identity;
- listing start and end;
- overlapping membership;
- lifecycle effective dates;
- delisting and successor handling;
- current versus historical isolation;
- historical load idempotency and transaction rollback;
- exact security-event and terminal-event key reconciliation;
- current-universe validation after historical loading; and
- closed calibration and unresolved-terminal enforcement.

### 14.3 Market tests

- adjusted-only source selection;
- split/dividend total-return fixtures;
- stale and short-history handling;
- benchmark alignment;
- D+1 execution;
- delisted terminal values;
- no forward price access;
- deterministic feature windows.

### 14.4 Financial and FX tests

- exact 154-profile, 22-metric, and 66-concept-link contract census;
- financial policy, manifest, artifact hash, and per-profile row-hash integrity;
- atomic and repeat-load-idempotent schema-v4 contract publication plus
  append-only v5 and v6 migration checks;
- immutable Stage 4B cache seal, fallback path containment, payload checksums,
  resolution seal, and verified existing-snapshot reuse;
- exact 140-standard/14-exception resolution states, including ASM/CGAU
  unstructured-latest-report gaps and TII's original full XBRL instance;
- SEC acceptance-bound profile inference and future-filing exclusion;
- ordinary 6-K exclusion unless Company Facts identifies financial XBRL;
- reporting currency derived only from filing evidence, never listing currency;
- explicit missing Company Facts, taxonomy, currency, and annual-form review
  states;
- 10-K/10-Q and 20-F/40-F/6-K examples plus inline and traditional instance
  XBRL context, unit, scale, and lineage preservation;
- US-GAAP and IFRS concepts with deterministic source/concept precedence;
- issuer-extension mapping;
- amendments and supersession lineage;
- SEC acceptance-time gating;
- quarterly, semiannual, annual, and stub TTM;
- weighted-average share selection without summation and positive-outflow capex
  sign normalization;
- debt/cash and enterprise value;
- as-of valuation repricing against the Stage 3 price snapshot;
- point-in-time FX;
- mixed-currency quarantine;
- negative-profit valuation cap;
- reporting-currency versus listing-currency distinction;
- foreign ADR/ordinary-share ratio fail-closed valuation behavior;
- zero stored error-severity ingestion/normalization issues; and
- canonical self-reference/snapshot indexes that keep deterministic repeat
  rebuilds bounded.

### 14.5 Commodity and parser tests

- exact registry/supporting-operand/applicability Cartesian completeness;
- all-source manifest hash, row-count, availability-time, and terminal-gap
  validation;
- local-path containment, immutable object hashes, and cache-only replay;
- one physical decode per unique content hash and decoder contract;
- one semantic-document construction per issuer/content context with all
  applicable metrics evaluated together;
- serial/parallel evidence fingerprint equality;
- interrupted-run resume without force or completed-work repetition;
- policy-only replay with zero document, provider, or OCR calls;
- residual batch exclusion of all previously completed content hashes;
- effective-dated company exposure weights;
- publication lag and macro vintage;
- continuous-futures roll policy when applicable;
- unit conversion;
- reserve/resource definition codes;
- AISC versus cash-cost distinction;
- producer versus royalty applicability;
- price/volume/mix table extraction;
- consolidated versus segment scope;
- duplicate and conflict handling;
- prose review requirement;
- immutable evidence hashes;
- complete evidence-state and coverage-denominator reconciliation;
- core current/historical coverage thresholds;
- no historical feature materialization before the Stage 6B coverage seal;
- measurement-only zero-weight enforcement.

### 14.6 Scoring and calibration tests

- percentile ties and small peer groups;
- exact normalization fallback;
- not_applicable versus missing;
- fixed-neutral missing contribution;
- confidence and missing-weight gates;
- row-order determinism;
- contract hashes;
- future availability rejection;
- purge/embargo leakage;
- split determinism;
- untouched outer test;
- FDR family completeness;
- concentration, turnover, cost, and capacity;
- no automatic weight or policy promotion.

### 14.7 Reporting and orchestration tests

- all 134 current rows published;
- required-column validation;
- stale/OOS-invalid demotion;
- shadow zero-cap behavior;
- atomic output replacement;
- interrupted run resume;
- monotonic current as-of;
- historical route isolation;
- last-success preservation after failure;
- file-only Portfolio Layer ingestion;
- orchestration db_group isolation.

## 15. Delivery sequence

### Work package A — Foundation

Implement Stages 0-2:

- package and configuration;
- database identity and schema;
- input/source manifests;
- current universe;
- calibration group policy;
- historical membership structure;
- aliases and terminal-event structure;
- independence tests.

Exit condition: a fresh scratch DB loads and validates exactly 134 current rows without importing another sector package.

### Work package B — Current common data and feature contract

Implement Stages 3-6A:

- adjusted market history;
- SEC/IFRS/FX;
- common financial features;
- common component shape and specialized placeholders;
- current 134-row shadow output.

Exit condition: current score contract passes, all rows are visible, and no specialized metric or portfolio authority is active.

### Work package C — Source closure and one-pass specialized capture

Implement Stage 4D preflight, Stage 5A, and Stage 6B before any historical
feature materialization:

- remaining historical/deactivated identity and terminal-event closure;
- no-write 2019-forward feasibility and partition preflight;
- company exposure registry;
- raw and canonical positioning, macro, commodity, and government data;
- complete specialized metric/supporting-operand registry and applicability;
- all-source SEC, IR, local-exchange, archived, reserve/resource, and technical-
  report census;
- immutable hydration and unique-document semantic compilation;
- one resumable all-metric extraction plus parse-free review and derivations;
- at most one consolidated residual source batch;
- current/historical coverage cube and final metric dispositions;
- measurement-only overlays.

Exit condition: Section 11.7 source, execution, current-breadth, historical-
depth, and PIT-feasibility gates pass for the frozen core set; specialized
evidence is auditable; and zero-weight enforcement passes.

### Work package D — Single survivorship-correct PIT build and diagnostics

Implement Stage 6C and diagnostics:

- one unified common/cycle/positioning/specialized point-in-time panel;
- forward targets;
- factor validation;
- lifecycle and regime diagnostics.

Exit condition: the panel is survivorship-correct, PIT-safe, cache-reproducible,
and hash-bound to the final source, observation, applicability, and metric-
disposition contracts.

### Work package E — Calibration and product evidence

Implement Stages 7-10B:

- sealed baseline;
- pre-registered candidates;
- purged walk-forward;
- outer test;
- portfolio backtest;
- dashboard;
- governance lockbox.

Exit condition: either a candidate passes explicit promotion gates or the model remains an honest shadow monitor.

### Work package F — Integration and operations

Implement Stages 11-12:

- file-only Portfolio Layer adapter;
- zero-cap shadow registration;
- independent refresh runner;
- health checks;
- historical runner;
- rollback and recovery.

Exit condition: daily refresh is deterministic, resumable, independently scheduled, and incapable of changing another sector's state.

## 16. Implemented slices and next implementation slice

Completed slice A — independent foundation and current universe:

- package identity, strict configuration, path ownership, and forbidden-import validation;
- schema v1 with ownership identity and checksummed migration ledger;
- immutable 134-row current-universe manifest and eight-cohort policy;
- atomic current-universe loader and validator; and
- scratch, idempotency, collision, and CLI tests.

Completed slice B — deactivated candidate intake:

- immutable 72-row candidate census and review workbook;
- candidate policy, manifest, validator, and command; and
- fail-closed candidate tests with no direct database promotion.

Completed slice C — Stage 2B governed reconciliation pilot:

- four package-owned reviewed CSV contracts for 20 historical securities;
- policy and manifest sealing exact rows, keys, hashes, flags, and cohorts;
- schema v2 ticker-alias and stable event-key migration;
- an atomic loader for raw payloads, identities, taxonomy, memberships, aliases, security events, and terminal terms;
- a read-only database validator and atomic evidence reports; and
- 15 passing package tests including v1 migration, tampering, rollback, idempotency, current-history isolation, and complete CLI smoke coverage at slice completion.

Completed slice D — Stage 3 adjusted prices and terminal-return closure:

- package-owned market policy, 162-role instrument contract, 20-rule terminal contract, and immutable manifest;
- stable Norgate asset-ID mapping for 134 current, 20 historical, XLB, SPY, and required successor roles;
- schema v3 market identity, snapshot, price, action, calendar, coverage, terminal-calculation, and feature tables;
- fenced raw/total-return extraction with canonical cache files and a hash-sealed cache manifest;
- Basic Materials hybrid coverage gate separating strict completeness diagnostics from controlled sparse-history rank readiness;
- fail-closed active/current major-exchange checks and listing-regime-bounded
  coverage for every current role;
- 134 technical feature rows using only listing-window and
  on-or-before-as-of observations;
- nine fixed-cash, six stock-conversion, and one mixed terminal outcome resolved; four bankruptcy distributions explicitly pending;
- Stage 2A and 2B revalidation after Stage 3 mutation;
- read-only Stage 3 validation and atomic evidence reports; and
- 24 passing tests covering contracts, tampering, idempotence, both migration
  paths, current major-exchange listing windows, provider snapshot fencing,
  cache publication, terminal formulas, and no-lookahead behavior.

Slice D exit condition result: passed. The live rank-ready gate is 100%, every
pilot event has an explicit disposition, and calibration remains closed.

Completed slice E1 — Stage 4A financial contract and reporting-profile census:

- froze the v1 financial policy, four-artifact manifest, 22-metric/66-link
  concept map, and exact reviewed-override schema;
- expanded the owned source registry from 10 to 16 sources;
- added the append-only, checksummed schema-v4 financial tables;
- built a 154-issuer, row-hashed SEC reporting census from 315 cached payloads,
  including archive support for historical filers and explicit missing-payload
  states;
- loaded 134 current and 20 historical profiles, 22 metrics, 66 concept links,
  and three raw contract payloads atomically and idempotently;
- published an independent zero-error evidence pack with the 14-name review
  queue and all financial/calibration/promotion gates closed; and
- passed 29 package tests, static checks, and every Stage 0–3 validator after
  the live schema-v4 migration.

Slice E1 exit condition result: passed. Every in-scope issuer has a governed
profile, but a profile is only routing metadata. No SEC fact, canonical
statement, FX observation, financial feature, score, rank, or portfolio signal
was inferred from the census.

Completed slice E2 — Stage 4B point-in-time financial ingestion, FX, and common
features:

- preserved Stage 4A as clean checkpoint `4b6bce4` before implementation;
- froze the Stage 4B policy against every Stage 4A artifact and the immutable
  reporting cache;
- added append-only schema v5 ingestion/resolution/audit fields and schema v6
  canonical foreign-key/snapshot indexes;
- loaded exact-acceptance filing metadata for 154 profiles and the standard
  Company Facts path for all 140 ready profiles;
- completed the NUE/BHP/AEM/RMIX representative pilot before full
  normalization;
- gave all 14 exceptions primary-evidence routes without converting
  unstructured reports into invented structured facts;
- loaded 310,067 raw facts, preserving 56,380 quarantined observations for
  audit, and selected 239,705 canonical facts with zero value conflicts;
- loaded 22,852 point-in-time FX observations and retained both reported and
  USD lineage with zero missing required conversions;
- published 134 common-feature and coverage rows plus issuer/cohort/regime,
  pilot, exception, and validation evidence packs;
- implemented conservative foreign share-ratio and loss/denominator valuation
  gates rather than manufacturing market caps or attractive multiples;
- proved scratch and live cache-only idempotency with identical snapshot key
  and table counts;
- reran every Stage 0–4B validator, 36 package tests, and Ruff successfully;
  and
- retained all calibration, scoring, ranking, and portfolio flags in their
  closed state.

Slice E2 exit condition result: passed for engineering use. OGC is the one
explicitly blocked current profile; 48 foreign/current securities retain null
valuation until the next share/ADR-ratio contract. These are visible follow-up
contracts, not silent Stage 4B failures.

Completed slice E3 - Stage 4C OGC and listed-security-unit remediation:

- froze the 47-row security-ratio policy/CSV contract against exact SEC
  evidence, with 42 direct-share rows and five ADS conversions;
- added schema v7, the ratio dimension, feature ratio lineage, and two package
  source-registry entries;
- implemented deterministic OGC audited-HTML extraction with 42 tied-out
  observations and no claim that the source is XBRL;
- upgraded financial policy, source precedence, immutable cache namespace,
  market-cap formula, and validation gates;
- eliminated the OGC blocked state and all unresolved ratio-required current
  foreign listings in the acceptance candidate;
- produced 95 full, 29 partial, 9 insufficient, and 1 stale current rows, with
  50 rank-ready and 87 valuation-ready;
- passed fresh build, zero-error/zero-warning validation, cache-only replay, 38
  tests, Ruff, and independence checks; and
- retained all calibration, score, rank, specialized-weight, and portfolio
  flags closed.

Slice E3 exit condition result: passed as an engineering acceptance candidate.
The installed live database remains unchanged until explicit replacement
authorization is provided.

Implemented F0 foundation and original closure order - Stage 4D/5A:

1. Resolve or reject the remaining 52 deactivated candidates and finish the
   four pending terminal-distribution treatments so the historical parser
   universe will not change after extraction. F0.2 has now completed the
   terminal-distribution part; the 52 candidate decisions remain open.
2. Freeze candidate monthly/21-session dates from 2019-01-01 and run the
   Stage 4D read-only source/partition feasibility audit; write no feature
   history.
3. Convert the Section 9 candidates into a versioned registry of direct
   metrics, supporting operands, formulas, definition variants, table
   families, units, periods, scopes, and plausibility bounds.
4. Build and review the exact current-plus-historical ticker-by-metric
   applicability matrix.
5. Enumerate the complete SEC, issuer-IR, local-exchange, archived,
   reserve/resource, and technical-report source universe and freeze expected
   availability timestamps and source-gap dispositions.
6. Freeze commodity/positioning sources and effective-dated issuer exposure
   contracts.

F0 implementation result on 2026-09-06:

- `core/specialized_contract.py` strict-loads, validates, and atomically loads
  the metric registry, operand graph, complete identity-metric accounting, and
  all-source census. It refuses contract replacement after parser evidence
  exists.
- `core/historical_pit_preflight.py` opens SQLite query-only, freezes monthly
  last-XNYS-session dates, evaluates contemporaneous market/common-financial
  feasibility, hashes membership and source snapshots, and proves database
  size, timestamp, data version, and every table count remain unchanged.
- `scripts/12_load_basic_materials_specialized_contract.py`,
  `12a_validate_basic_materials_specialized_contract.py`, and
  `11a_preflight_basic_materials_historical_pit.py` provide independent load,
  read-only validation, and fail-closed preflight entry points.
- Schema v8 creates the exact specialized metric/cohort/operand/applicability
  tables, the source census, content-addressed document bridge, resumable work
  ledger with a two-attempt hard ceiling, candidate evidence store, and
  zero-weight accepted-observation store.
- The metric contract contains 64 definitions and 16 operand links. The exact
  matrix contains 9,856 rows for 154 identities by 64 metrics; 980
  issuer-selective pairs remain explicitly review-required.
- The declared source census contains 5,640 rows: 3,874 identified SEC filings,
  1,289 discovery-required rows, and 477 explicit not-applicable rows. A total
  of 5,163 applicable rows still require content hydration or discovery.
- The no-write schedule contains 93 dates in three blocks. Market history is
  at least 99.89% role-feasible per block and common-financial feasibility is
  71.18%, 70.67%, and 75.79% by chronological block. Feasibility passed; panel
  materialization did not.
- All 41 tests, Ruff, SQLite integrity/foreign-key checks, and the independence
  gate pass. The isolated database is
  `output/basic_materials/verification_f0_20260906/basic_materials.sqlite`;
  the installed live database was not changed.

F0 is not complete. After the F0.2 terminal gate closed, finish its remaining
dependencies in this order before F1 production hydration: (1) resolve/reject
52 historical candidates, (2) reconstruct effective-dated history for the 134
current-snapshot names, (3) adjudicate 980 issuer-selective applicability
pairs, and (4) discover/freeze every non-SEC, commodity, and positioning source
plus issuer exposure contract. Re-run Stage 4D after each input seal changes.

Implemented slice F0.1 - read-only closure workbench:

- `data/basic_materials_f0_closure_policy.yaml` freezes queue order, no-write
  controls, shared-driver grouping, accession/cache reuse, and the prohibition
  on inferred decisions, zero bankruptcy distributions, snapshot backfill, or
  source acquisition before the applicable F0 seal.
- `core/f0_closure.py` and
  `scripts/13_build_basic_materials_f0_closure_workbench.py` generate the
  historical-candidate, terminal-distribution, current-membership,
  applicability, blocker, and source-acquisition workbooks from query-only
  SQLite access and prove that the database is unchanged.
- The isolated F0 candidate produced 52 open candidate decisions, four open
  terminal distributions, 134 membership-history reviews, 980 applicability
  reviews, and 5,163 source rows. Grouping by accession, issuer/family, and
  shared commodity/positioning driver reduced those rows to 4,244 acquisition
  units, avoiding 919 duplicate acquisition attempts before content hashing.
- The workbench remains intentionally open. Blank review fields are not
  decisions, listing dates are lower bounds rather than membership proof, and
  every acquisition row is blocked until the upstream F0 contracts seal.

Implemented slice F1A - fixture-first parser contract and scheduler:

- `data/basic_materials_specialized_parser_policy.yaml` freezes four owned
  adapters, all 32 registry table families, immutable semantic-cache behavior,
  source/rule change detection, and the one-full-plus-one-residual ceiling.
- `data/basic_materials_specialized_parser_fixtures.yaml`,
  `core/specialized_parser_contract.py`, and
  `scripts/14_validate_basic_materials_specialized_parser_contract.py`
  validate eight synthetic guard types before any production document may be
  opened: positive, prohibited context, cross-cohort, unit, period, scope,
  amendment precedence, and after-close availability.
- The F1A gate passed 8 of 8 cases with all 32 table families assigned, 64
  metrics and 40 core metrics accounted for, zero issues, and production
  execution still explicitly false.
- The stricter Stage 4D loader now requires exact regime block identifiers,
  full target-window span, and contiguous non-overlapping dates. The read-only
  pre-F0.2 rerun retained 93 dates and seven expected downstream blockers.

Implemented slice F1A.1 - real-document compiler and parser-recall pilot:

- `adapters/dedicated_parser_adapter.py` is the sole Basic Materials boundary
  to the shared semantic parser. It preserves stable block, table, section,
  unit, period, and source-content identity and emits review candidates only.
- `data/basic_materials_specialized_parser_pilot_policy.yaml` freezes the
  decoder contract, exact real-document gates, all 29 physical family rules,
  three derived-only families, production golden targets, and every no-write
  flag. The committed document manifest seals 10 local SEC files by URL,
  accession, availability timestamp, byte size, and SHA-256.
- `core/specialized_parser_pilot.py` compiles unique content into deterministic
  gzip semantic objects, validates cache contracts, supports raw-free replay,
  reports the lexical/numeric/prohibited/review funnel, validates approved
  golden cases, and has no database parameter.
- `scripts/14b_build_basic_materials_real_document_parser_pilot.py` exposes
  explicit compile and `--cache-only` modes. `--allow-open` permits the
  measurement report to return zero while the reviewed golden gate remains
  open; it does not authorize production execution.
- The final replay covers all eight cohorts and HTML/XML/JSON, reports 10/10
  successful documents, 75,715 blocks, 16,138 table rows, 26,143 structured
  facts, 295 review candidates, and candidates for 26/29 physical families.
  The three uncovered families are `channel_inventory`, `end_market_mix`, and
  `outage_turnaround`, each classified `source_term_absent` for targeted source
  expansion. Cache-only replay records 10 cache hits, zero raw opens, and zero
  physical compiles.
- Tests cover strict committed contracts, each evidence-funnel stage, all-
  family fixture compilation, removal of raw fixtures followed by cache-only
  deterministic replay, and zero database/PIT writes. The full package passes
  52 tests, Ruff, and independence across 75 Python files.

F0.1/F1A/F1A.1 exit condition result: passed as an engineering control plane
and a real-document decoder/recall pilot. It does not claim specialized data
coverage: accepted specialized observations
remain zero, so current-breadth and historical-depth coverage are not yet
measurable. The isolated database SHA-256 remained
`494d906ac1b968f6c50ff5248998db387ae199d7ad45d3e101b99934a8fa0605`.

Implemented slice F0.2 - terminal-distribution evidence closure:

- `data/basic_materials_terminal_distribution_policy.yaml` freezes the exact
  four-event scope, allowed review states, value semantics, Stage 3 base hashes,
  source rules, cache-only replay, isolated-database default, and closed gates.
- `system_csvs/basic_materials_terminal_distribution_reviews.csv` records four
  explicit `zero_distribution_verified` decisions. The separate manifest binds
  the CSV and five SEC documents by URL, document date, relative cache path,
  byte size, and SHA-256.
- ANV's confirmed plan states that existing equity receives no recovery; MCP's
  confirmed/effective plan cancels equity with no distributions; BioAmber's
  court-appointed Monitor states that liquidation leaves no residual value for
  equity; and General Moly's confirmation release and filed restructuring term
  sheet cancel existing common equity.
- Schema v9 adds `fact_terminal_distribution_review` without changing prior
  migration checksums. Source registry v6 adds only the owned terminal-review
  source. All five exact source payloads are retained in
  `raw_source_payloads` as well as the content cache.
- `core/terminal_distribution_contract.py` rejects policy, manifest, row,
  source-link, date, host, hash, byte-size, status/value, and evidence-scope
  drift before mutation.
- `core/terminal_distribution_reviews.py` verifies each short evidence phrase
  in the sealed HTML, rejects conflicting loaded rules, writes reviews and
  source bytes atomically, overlays only the four pending bankruptcy rules, and
  preserves the base evidence inside each overlay row.
- Stage 3 contract reloads preserve only a matching reviewed overlay whose
  stored base hash equals the incoming immutable terminal-rule hash. Stage 3
  validation accepts only that exact row-level overlay lineage; unrelated stale
  rule hashes still fail.
- `scripts/15_apply_basic_materials_terminal_distribution_reviews.py` requires
  an explicit database path and rejects the configured live database by
  default. `15a_validate_basic_materials_terminal_distribution_reviews.py`
  performs cache-only, read-only replay.
- Regression tests cover exact contracts, source-byte tampering, no partial
  database write on conflict, idempotence, preservation through Stage 3 reload,
  zero-value terminal math, schema migration, and closed model gates.

F0.2 execution result on 2026-09-07:

- The schema-v8 source candidate was copied only after SHA-256
  `494d906ac1b968f6c50ff5248998db387ae199d7ad45d3e101b99934a8fa0605`
  matched. Only
  `output/basic_materials/verification_f02_20260907/basic_materials.sqlite`
  was migrated and mutated.
- The new schema-v9 candidate SHA-256 is
  `98eb3facaf2efb96263821147db668411a100cd064b92e61d90009ea4eb9a25e`.
  The installed live database remains schema v6 with four pending events and
  no terminal-review rows.
- Cache-only application and independent read-only validation pass. All five
  payload hashes match manifest SHA-256
  `8edf81724a052a93ee890844772b6f53645dd9d8b44a8014b4f5128dee4549d4`.
- Terminal reconciliation is 20/20 resolved: nine fixed cash, six stock
  conversion, one mixed prorated, and four zero bankruptcy distributions.
  Overlay-aware Stage 3 validation has zero errors and a 100% current-market
  gate.
- The query-only F0 rerun reports four blockers and
  `terminal_distributions_open=0`. Stage 4D reports six blockers and
  `unresolved_terminal_distributions=0`; PIT materialization remains false.
- SQLite integrity and foreign keys pass; all memberships remain
  calibration-ineligible; `portfolio_candidate_gate` and
  `oos_score_valid_flag` remain false; 49 tests, Ruff, and independence across
  70 Python files pass.

F0.2 exit condition result: passed. It closes only terminal-distribution
evidence. It does not authorize source hydration, production parsing,
historical PIT creation, calibration, ranking, portfolio use, or live database
replacement.

Remaining slice F1B - evidence closure, sealed hydration, and document compiler:

1. Complete F0.3 by resolving the 52 historical-candidate decisions,
   reconstructing 134 current-name membership histories, adjudicating 980
   applicability reviews, and sealing the resulting source/exposure scope
   without automated promotion or fabricated values.
2. Hash-seal the resulting universe, applicability, source, commodity,
   positioning, and exposure contracts, then rerun Stage 4D.
3. Adjudicate the F1A.1 real-document review queue: approve exact positives and
   hard negatives for the 26 surfaced families, and add targeted annual-report,
   earnings/operating, or issuer-IR documents for `channel_inventory`,
   `end_market_mix`, and `outage_turnaround`. Reuse the semantic cache for all
   policy/terminology changes; production execution remains disabled until
   every pilot golden minimum passes.
4. Expand the reviewed pilot into the production golden corpus: at least 20
   positives, 10 hard negatives, and three historical/deactivated examples per
   physical family where the source population permits.
5. Hydrate the entire approved source manifest once into Basic Materials-owned
   content-addressed storage, reusing SEC accessions and shared drivers.
6. Compile every unique content hash once into immutable XBRL/table/text
   semantic records and seal the decoder contract.
7. Pass the offline exact plan with all metrics, all issuers, zero unexplained
   documents, and a deterministic work ledger before enabling F2 extraction.

Following slice F2 - one-pass extraction and coverage closure:

1. Execute one resumable all-issuer/all-metric parser run.
2. Build candidates, accepted observations, derivations, conflicts, and the
   complete evidence-state coverage cube without building historical features.
3. Adjudicate stored evidence through policy-only replay.
4. If the Section 11.7 core coverage gate still has source-driven gaps, execute
   at most one hash-sealed residual acquisition/parse batch that excludes all
   completed content hashes.
5. Freeze final core, diagnostic, deferred, and excluded metric dispositions
   plus the accepted-observation manifest.

Following slice F3 - Stage 6C single unified historical PIT build:

1. Materialize common financial, market, ratio, positioning, commodity/cycle,
   and accepted specialized features together from 2019-01-01.
2. Leave historical valuation null where no contemporaneous ratio evidence
   exists; never project the current ratio contract backward.
3. Publish date/year/cohort/metric coverage, missing-source reasons, forward
   outcomes, and deterministic row/partition hashes.
4. Prove no-lookahead and cache-only reproduction.
5. Only after F3 passes may signal diagnostics and a preregistered calibration
   bundle begin.

Do not begin optimization, portfolio integration, production scheduling, or
historical PIT materialization in F0-F2.

## 17. Key risks and mitigations

| Risk | Consequence | Mitigation |
|---|---|---|
| Current-only universe | Survivorship-biased calibration | Historical membership, delisted names, terminal events before Stage 8 |
| Six cohorts have only 10-12 names | Overfit cohort weights | Sector baseline, exact-cohort ranks, hierarchical shrinkage, standalone promotion gates |
| 51 foreign-domiciled issuers | Cadence, IFRS, currency, and 6-K gaps | Technology-style reporting profiles, IFRS mapping, point-in-time FX, explicit annual/semiannual status |
| Commodity cyclicality | Peak earnings look cheap | Trailing and mid-cycle valuation, regime diagnostics, conservative price sensitivity |
| Heterogeneous business models | Invalid cross-sector comparisons | Exact cohort normalization, applicability registry, one sector score built from comparable component percentiles |
| Developer/precommercial names | Meaningless profitability ranks | Effective-dated lifecycle and separate survival/readiness score |
| Specialized disclosure inconsistency | False precision | Definition variants, evidence hashes, review-required prose, zero weight until validation |
| Revised macro data | Look-ahead bias | Release timestamps and ALFRED/vintage policy |
| Commodity data licensing/rolls | Non-reproducible signals | Entitlement registry, frozen roll methodology, public baseline, explicit proxy labels |
| Parser scope explosion | Long, expensive implementation | Freeze a value-ranked core plus full supporting operands, complete the all-source census first, compile unique content once, and allow only one consolidated residual batch |
| Automatic optimizer promotion | Model-selection overfit | Pre-registration, report-only optimization, untouched outer test, manual lockbox promotion |
| Cross-sector state coupling | Operational failure or contamination | Own DB/output/cache, banned imports, read-only upstream adapters, independent db_group |

## 18. Definition of done

The implementation is complete only when:

- the package is fully independent and passes banned-import/path/database tests;
- a fresh environment can create basic_materials.sqlite from versioned migrations;
- the current output contains all 134 source tickers;
- current and historical universe contracts are separate and valid;
- adjusted market, SEC/IFRS/FX, positioning, and commodity sources are point-in-time controlled;
- common and specialized metrics have explicit applicability and provenance;
- the frozen core specialized set passes the source, execution, current-
  breadth, historical-depth, and PIT-feasibility gates;
- each unique source document is physically decoded once per decoder contract,
  all applicable metrics are evaluated together, and review replay is
  parse-free;
- current shadow scores reproduce exactly;
- one unified historical research panel is survivorship-correct and
  point-in-time safe;
- calibration uses purged walk-forward and an untouched outer test;
- portfolio results include costs, capacity, concentration, and terminal events;
- the final rank contract is dated, hash-sealed, and validated;
- shadow and production states cannot be confused;
- the Portfolio Layer handoff is file-only;
- the refresh is resumable, monotonic, atomic, and independently scheduled; and
- either a model is explicitly promoted from qualifying evidence or it remains zero-cap shadow without being mislabeled as production.

## 19. Final recommendation

Proceed with the Basic Materials implementation using Consumer Defensive as
the structural scaffold, Machinery as the economic design reference, and
Transportation as the one-pass parser-execution reference. Port Technology's
foreign-issuer, financial, and overlay mechanics. Borrow Biotech's calibration
safeguards and only the narrow review/entity patterns needed from Med Devices.

The correct build order is the independent foundation and identity spine first;
adjusted prices and terminal returns second; acceptance-bounded financials and
current common features third; historical/universe and source feasibility
preflight fourth; specialized metric/operand definition, all-source hydration,
one-pass extraction, parse-free adjudication, and high-coverage freeze fifth;
one unified 2019-forward PIT panel sixth; survivorship-correct diagnostics,
calibration, and backtesting seventh; and Portfolio Layer integration last.
This produces an auditable ranking without repeated source parsing, small-
cohort overfitting, ticker ambiguity, current-universe survivorship bias, or
another sector's production state contaminating the model.
