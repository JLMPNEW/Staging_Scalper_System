# Industrials Transportation Implementation Plan

Status: foundation, historical raw-data/disclosure loads, and frozen PIT feature panel implemented; positioning, OOS calibration, orchestration, and promotion pending  
Model family: `transportation`  
Implementation root: `industrials/transportation`  
Shared infrastructure root: `industrials`  
Shared database: `industrials.sqlite`

## Implementation checkpoint - 2026-07-23

The historical-data and point-in-time feature batches are complete in the configured shared
industrials database: foundation, active/delisted prices, FX, SEC raw data, specialized
disclosures, and the 92-date active-plus-inactive feature panel. Portfolio allocation remains
intentionally disabled.

| Acceptance gate | Result |
| --- | --- |
| Controlled active and delisted seeds | PASS: 112 active and all 48 delisted retained |
| Provider identity review | PASS: 160 mappings; no `review_required` rows |
| Calibration-usable coverage | PASS: 158 total; 112 active plus 46 delisted |
| Fail-closed provider conflicts | PASS: CGI and RRTS explicitly excluded while current/OTC |
| Exact delisted membership dates | PASS for all 46 included delisted histories |
| Family-pinned shared market stages | PASS: transportation, IYT/XTN/SPY, family outputs/policy |
| Active and benchmark price loading | PASS: 112 active plus IYT/XTN/SPY current through 2026-07-22 |
| Market policy audit | PASS WITH REVIEW: 115 rows, 0 failures, 4 short-history reviews |
| Delisted price loading | PASS: 46 names and 178,217 Norgate total-return bars |
| Portfolio survivorship export | PASS: price and event contracts for 46 names |
| Portfolio connection | PASS: optional `industrial_family` shadow source, OOS fail-closed |
| Historical SEC raw load | PASS: 160 profiles/tickers; 21,147 filings; 1,704,895 raw facts |
| FX raw load | PASS: 9 pinned pairs including NOKUSD; discovered JPYUSD retained |
| Security-continuity policy | PASS: 6 verified boundaries; no structural/venue price stitching |
| Targeted SEC recovery | PASS: 10/10 reviewed names have filing, raw-fact, and mapped-fact coverage |
| Read-only raw coverage gate | PASS: 160 ticker rows PASS, zero errors/warnings; specialized parsing delegated to Stage 08c |
| Financial routing | PASS: family-pinned SEC, financial feature/validation, policy, and FX wrappers |
| Specialized metric contract | PASS: 39 metrics; cohort/industry applicability and explicit missingness |
| Bounded specialized document recovery | PASS: 160/160 active-plus-inactive issuers |
| Specialized candidate contract | PASS: 373 bounded candidates; 82 accepted and 291 review-required |
| Current specialized availability | PASS: 4,368 rows; 1,529/2,278 applicable observed and 133/552 specialized observed |
| Historical specialized scale decision | PASS: `READY_FOR_BOUNDED_HISTORICAL_BACKFILL`; surface 25.0%, air 40.9%, marine 52.4% |
| Historical specialized disclosure load | PASS: 3,019/3,019 eligible filings scanned; 160 issuers; zero missing |
| Historical specialized candidate evidence | PASS: 4,450 candidates; 1,130 accepted and 3,320 review-required |
| PIT feature history | PASS / `FROZEN`: 92 dates, 9,496 membership rows, 370,344 metric rows, zero future-data errors |
| Scoring eligibility | PASS: required facts, staleness, liquidity, confidence, and specialized coverage fail closed |
| Shadow rank publication | PASS in shared DB: deterministic 112-row rank contract and hash manifest |
| Portfolio adapter execution | PASS: 112 rows ingested, zero investable/OOS rows |
| Regression safety | PASS: 151 industrials/portfolio tests plus Ruff |
| Production eligibility | NOT GRANTED: positioning, sealed OOS calibration, orchestration, and explicit promotion remain |

The six price-start reviews and ten SEC/reporting reviews are resolved. The bounded scan and
mature-cohort scale gates passed, the checkpointed historical parse reached exact 100% eligible
filing coverage, and the active-plus-inactive PIT feature panel is hash-frozen. Specialized
availability is conservative: period-ambiguous growth, subjective milestones, and multi-value
conflicts remain review-required, and an SEC filing is unavailable before its filing date even
when its UTC acceptance timestamp falls on the prior calendar day. The next research batch may
define and run walk-forward OOS calibration only against this frozen panel, after positioning
scope and the return/cost/training contracts are explicitly frozen. Production still requires
net-of-cost backtests, daily orchestration, and reviewed promotion.

## 1. Objective

Implement transportation as a production-quality industrials model family with requirements and stage gates comparable to defense. Transportation must reuse the shared industrials database, ingestion, point-in-time (PIT), provenance, quality-control, and portfolio contracts. Transportation-specific universe policy, taxonomy, financial interpretation, scoring, calibration, reports, and orchestration must remain isolated under `industrials/transportation`.

The implementation is complete only when it can:

1. Load and validate an active, historical, and delisted transportation universe.
2. Build PIT market, financial, ownership, positioning, and transportation-specific features.
3. Produce deterministic shadow scores and a validated final rank table.
4. Build survivorship-corrected historical panels and complete walk-forward OOS calibration.
5. Pass the shared industrial-family portfolio adapter without weakening defense contracts.
6. Run daily under the shared industrials refresh lock without mutating another model family's rows or artifacts.

## 2. Current Baseline and Authoritative Seeds

The two authoritative source files for the initial implementation are:

- Active seed: `ticker_mapping/transportation_tickers.csv`.
- Delisted seed: `ticker_mapping/transportation_delisted.csv`.

At the time of this plan, the active seed contains 112 investable rows and the delisted seed contains 48 rows, for 160 securities in the initial active-plus-delisted research universe. The cohort design is already encoded in the files and must be preserved:

| Calibration cohort | Active | Delisted | Total initial coverage | Active business mix |
| --- | ---: | ---: | ---: | --- |
| `surface_freight_and_logistics` | 40 | 21 | 61 | Freight/logistics, railroads, trucking, rail equipment, and fleet leasing |
| `air_transport_and_aviation_services` | 22 | 13 | 35 | Airlines, airports/aviation services, and aircraft/engine leasing |
| `marine_shipping_and_maritime` | 21 | 14 | 35 | Liner, tanker, dry bulk, containership leasing, gas shipping, and marine services |
| `development_stage_and_speculative_transport` | 29 | 0 | 29 | Pre-scale air mobility, logistics, shipping, rail technology, and small speculative operators |
| **Total** | **112** | **48** | **160** | |

The active cohort populations are:

- `surface_freight_and_logistics`: UPS, FDX, JBHT, CHRW, EXPD, FDXF, LSTR, GXO, PBI, CYRX, FWRD, RLGT, PAL, HUBG, ZTO, UNP, CSX, CP, CNI, NSC, TRN, GBX, FSTR, RAIL, GATX, ODFL, XPO, TFII, SAIA, SNDR, RXO, ARCB, WERN, MRTN, CVLG, HTLD, ULH, PAMT, KNX, and R.
- `air_transport_and_aviation_services`: ASR, CAAP, ASLE, OMAB, PAC, DAL, UAL, AAL, LUV, ALK, JBLU, ALGT, SKYW, ULCC, CPA, VLRS, AZUL, RYAAY, LTM, AER, FTAI, and WLFC.
- `marine_shipping_and_maritime`: KEX, MATX, HAFN, ZIM, SBLK, DAC, ECO, CMRE, GSL, CCEC, GNK, SB, HSHP, ASC, ESEA, PANL, CMDB, GASS, SHIP, DSX, and SMHI.
- `development_stage_and_speculative_transport`: JOBY, UP, SOAR, CRGO, SFWL, NCEW, PSIG, ELOG, JYD, CTNT, ATXG, CIIT, EDRY, HMR, GLBS, NCT, HTCO, USEA, UFG, EHLD, VNTG, PSHG, CTRM, ICON, CISS, RUBI, SWVL, RVSN, and TOPP.

The delisted cohort populations are:

- `surface_freight_and_logistics`: KSU, BNI, GWR, PWX, SWFT, USX, CGI, USAK, CNW, YELL, QLTY, DSKE, UTIW, PACR, ECHO, RRTS, EGL, STMP, ABF, DDMX, and FRTZ.
- `air_transport_and_aviation_services`: LCC, NWA, CAL, AAI, VA, HA, SAVE, AAWW, ATSG, AYR, FLY, PNCL, and RJET.
- `marine_shipping_and_maritime`: GNRT, NM, NNA, DRYS, TGP, GLOG, GLOP, ATCO, EGLE, OSG, GMLP, TOO, HRZ, and EXM.
- `development_stage_and_speculative_transport`: no curated delisted rows yet; this is a documented survivorship-research gap rather than an assumption that no failures occurred.

The 48 delisted rows consist of 41 acquisitions, six wipeouts, and one distressed non-zero exit. They must not be treated as a ticker list alone: `exit_type`, `terminal_type`, acquirer, exit year, CIK, confidence, final trading date, and terminal consideration are part of the survivorship and forward-return contract.

Before Stage 1 is accepted, an analyst must review business-model purity, duplicate foreign lines, ADR/ordinary-share pairs, preferred shares, warrants, inactive securities, holding-company classifications, cohort assignments, and companies whose economics span multiple operating groups. Industry remains an applicability tag inside a cohort; it is not a replacement for the four calibration cohorts.

The ticker-mapping files are ingestion seeds, not ongoing pipeline inputs. Copy reviewed, hash-recorded versions into `industrials/transportation/system_csvs/transportation_tickers.csv` and `industrials/transportation/system_csvs/transportation_delisted.csv`. Subsequent pipeline runs must use the system CSVs and must never read directly from `ticker_mapping`.

## 3. Architecture and Ownership

Use the existing sector architecture:

```text
industrials/
  config.yaml                         # shared plus family-scoped configuration
  core/                               # reusable contracts and helpers
  scripts/                            # reusable family-parameterized stage scripts
  industrials.sqlite                  # one shared DB outside the repository
  defense/                            # existing family; behavior must not regress
  machinery/                          # existing family; behavior must not regress
  transportation/
    __init__.py
    README.md
    IMPLEMENTATION_STATUS.md
    data/
      transportation_cohorts.yaml
      transportation_universe_policy.yaml
      transportation_signal_registry.yaml
      transportation_output_column_map.yaml
    system_csvs/
      README.md
      transportation_tickers.csv
      transportation_historical_membership.csv
      transportation_delisted.csv
      transportation_ticker_aliases.csv
      transportation_listing_dates.csv
      transportation_cik_ticker_overrides.csv
      transportation_norgate_symbol_overrides.csv
      transportation_scoring_eligibility_policy.csv
      transportation_sec_reporting_overrides.csv
      transportation_reporting_profile_graduations.csv
      transportation_positioning_overrides.csv
      transportation_capacity_overrides.csv
    scripts/
      __init__.py
      00_validate_transportation_seed.py
      01_load_transportation_universe.py
      01b_load_transportation_historical_membership.py
      01c_load_transportation_ticker_aliases.py
      02_validate_transportation_universe.py
      02b_validate_transportation_identity_reconciliation.py
      03_sync_transportation_prices.py
      04_audit_transportation_market_data_policy.py
      04b_validate_transportation_stage0_4_production_readiness.py
      05_build_transportation_market_features.py
      06_validate_transportation_market_stage.py
      06a_build_transportation_scoring_features.py
      06a_validate_transportation_scoring_features.py
      07_sync_transportation_sec_fundamentals.py
      08_build_transportation_financial_features.py
      08_validate_transportation_financial_stage.py
      09_evaluate_transportation_profile_graduation.py
      10_validate_transportation_scoring_eligibility_policy.py
      11_sync_transportation_fx_rates.py
      15_import_transportation_norgate_delisted_prices.py
      16_run_transportation_daily_refresh.py
      17_publish_transportation_shadow_rank_table.py
      18_validate_transportation_shadow_rank_table.py
      19_build_transportation_shadow_snapshot_history.py
      20_validate_transportation_portfolio_adapter_shadow.py
      21_validate_transportation_oos_calibration_readiness.py
      22_build_transportation_oos_calibration_panel.py
      23_validate_transportation_oos_calibration_artifacts.py
      24_run_transportation_optuna_calibration.py
      25_backtest_transportation_scores.py
      26_run_transportation_weekly_calibration_research.py
      27_promote_transportation_oos_production.py
      28_export_transportation_delisted_price_contract.py
```

Generated artifacts belong under `output/industrials/transportation`. Network caches remain under the shared `output/industrials_cache` tree and may be shared only where the cache key is issuer/security/source specific and cannot collide across families.

## 4. Mandatory Shared-Infrastructure Refactor

This is the first implementation phase. Do not copy defense scripts wholesale while the shared configuration still resolves defense paths.

### 4.1 Family-scoped configuration

Add an authoritative structure under `industrials/config.yaml`:

```yaml
model_families:
  defense: { ...existing defense values... }
  machinery: { ...existing machinery values... }
  transportation:
    sector: Industrials
    industry: Transportation
    subsector: Transportation
    seed_csv: transportation/system_csvs/transportation_tickers.csv
    seed_source_id: transportation_ticker_seed
    policy_path: transportation/data/transportation_universe_policy.yaml
    cohort_path: transportation/data/transportation_cohorts.yaml
    historical_membership_csv: transportation/system_csvs/transportation_historical_membership.csv
    delisted_seed_csv: transportation/system_csvs/transportation_delisted.csv
    ticker_aliases_csv: transportation/system_csvs/transportation_ticker_aliases.csv
    listing_dates_csv: transportation/system_csvs/transportation_listing_dates.csv
    cik_ticker_overrides_csv: transportation/system_csvs/transportation_cik_ticker_overrides.csv
    benchmark_ticker: IYT
    benchmark_tickers: [IYT, XTN, SPY]
```

Implement one resolver in `industrials/core/config.py`, for example `resolve_family_config(config, model_family)`. It must fail if the family is absent and must not fall back from transportation to `industrials_universe` defense values. Maintain temporary defense compatibility only behind explicit tests and a documented removal date.

Migrate every shared reader that currently consumes `industrials_universe.*` to the family resolver. The minimum audit list is scripts 03, 04, 05, 06, 07, 08, 08b, 09, 10, 11, 13, and 14. Family-scope all output paths, policy CSV paths, reporting overrides, profile graduations, benchmarks, and expected counts.

### 4.2 Shared database isolation

Retain one database. Review each table and mutation for these rules:

- Tables containing derived family-specific state must include `model_family` in their key or unique index.
- Delete/update operations must include `WHERE model_family = ?` when the table is family-scoped.
- Shared raw facts and prices may be issuer/ticker scoped, but a family refresh may not delete data required by another family.
- `data_quality_issues` must be opened, cleared, and resolved by both `model_family` and stage.
- Universe reloads may deactivate only taxonomy and membership rows for their own family.
- The shared refresh lock remains `output/industrials/.industrials_refresh.lock`; transportation, defense, and machinery must never write concurrently.

Add cross-family regression tests that seed the same ticker into defense and transportation, run a transportation reload/rebuild, and prove defense taxonomy, features, issues, and published files are unchanged.

### 4.3 Shared-versus-family code rule

Reusable mechanics belong in `industrials/core` or `industrials/scripts`: configuration resolution, DB access, adjusted-price ingestion, FX, SEC facts, generic statements, PIT filtering, positioning, reports, and run manifests. Transportation wrappers should pin `--model-family transportation`, family benchmarks, and output paths. Transportation economics and policy belong only under `industrials/transportation`.

Do not import implementation code from `industrials/defense`. Extract genuinely common logic to shared modules first. Defense output must remain byte/contract compatible after refactoring.

## 5. Stage 0 - Governance, Seed, and Contracts

### Implementation

1. Create the directory structure above and package initializers.
2. Copy the reviewed ticker mapping into `system_csvs/transportation_tickers.csv`.
3. Standardize the header to the industrial universe contract: `ticker`, `investability_status`, `company_name`, `cik`, `exchange`, `sector`, `industry`, `subsector`, `country`, `currency`, `security_type`, `listing_status`, `is_primary_listing`, `calibration_cohort`.
4. Populate `calibration_cohort` from the reviewed cohort YAML; do not derive it silently at runtime from Yahoo industry labels.
5. Copy and validate all 48 rows from `ticker_mapping/transportation_delisted.csv` into the system delisted seed. Create empty but schema-valid override, alias, listing-date, and historical-membership files; the delisted seed is populated, not empty.
6. Register all new seed and generated source IDs in `industrials/data/free_source_registry.yaml`.
7. Add `00_validate_transportation_seed.py` to check encoding, headers, normalized tickers/CIKs, duplicates, exact cohort membership, allowed statuses and security types, foreign-line identity, primary-listing uniqueness, and expected count.

### Acceptance gate

- Seed validator reports zero errors and an explicit reviewed warning count.
- Every active ticker maps to exactly one cohort or a deliberate review cohort.
- All 48 delisted tickers map to one of the three represented mature cohorts; development-stage delisted coverage remains an explicit known gap until researched.
- Every duplicate CIK/security family has exactly one primary investable line unless documented policy permits otherwise.
- No preferred share, warrant, fund, inactive line, or duplicate ADR/ordinary line enters the investable universe accidentally.
- No generated pipeline stage reads `ticker_mapping/transportation_tickers.csv` directly.

## 6. Stage 1 - Security Master, Taxonomy, and PIT Membership

Generalize the defense universe loader/validator into shared components or create transportation wrappers around extracted shared functions. Load:

- `dim_company`, `dim_security`, and `dim_identifier` identity rows.
- `dim_industrials_taxonomy` with `model_family='transportation'`.
- `dim_universe_membership` with non-overlapping effective intervals.
- ticker aliases and corporate-action lineage.
- active, historical, and delisted calibration membership.

Historical membership must include `internal_ticker`, `exchange_ticker`, price-source symbol, issuer identity, cohort, start/end dates, status, successor/predecessor, event type, confidence, source URL, and notes. Membership dates must be bounded by verified listing dates.

The delisted loader is a required Stage 1 deliverable. Implement it in `01b_load_transportation_historical_membership.py`, matching the combined historical-plus-delisted responsibility of the defense script after extracting any reusable mechanics. It must:

1. Reads all 48 curated rows from `transportation_delisted.csv`.
2. Writes `dim_delisted_calibration_seed` with `model_family='transportation'`, the original ticker, a collision-safe `internal_ticker`, cohort, exit metadata, CIK, and confidence.
3. Writes PIT `dim_universe_membership` rows with `membership_status='delisted'`, `membership_basis='delisted_calibration_seed'`, and reviewed start/end dates.
4. Preserves successor/acquirer lineage without merging acquired-company price history into the acquirer.
5. Disambiguates reused ticker symbols with a stable suffix convention when necessary.
6. Is idempotent and scopes all replacement/deletion operations to transportation.

### Acceptance gate

- DB active count equals the policy YAML count.
- CSV, cohort YAML, taxonomy table, and current membership reconcile exactly.
- Membership intervals do not overlap illegally and never precede a verified listing date.
- Exactly 48 delisted seed rows and 48 corresponding PIT membership records load before any exclusions; any later exclusion requires a reason and retained source row.
- Re-running the loaders is idempotent.
- Running transportation loaders does not alter defense or machinery row counts/hashes.

## 7. Stage 2 - Identity and Market-Data Reconciliation

Create alias and Norgate resolution comparable to defense/machinery. Transportation has substantial foreign and OTC representation, so distinguish:

- internal canonical ticker;
- exchange ticker and exchange;
- Yahoo price symbol;
- Norgate symbol;
- SEC ticker/CIK, when applicable;
- ADR and foreign ordinary-share relationships;
- primary versus secondary listing;
- predecessor/successor symbols and corporate actions.

Never assume that a missing CIK makes a foreign issuer invalid. Instead assign a documented reporting profile and source route. Never combine ADR and ordinary-share price histories without an effective-dated conversion and corporate-action contract.

For each delisted member, reconcile the curated ticker to Norgate (preferred), Yahoo when available, and any approved manual price file. Verify security name, last quoted date, exchange history, adjustment mode, and ticker-reuse risk. The six wipeouts must retain terminal value zero only after the legal/economic outcome and final quoted bar are verified. The one `distressed_nonzero` row must retain an explicit reviewed terminal value. Acquisition rows should carry cash/stock consideration when it is needed to calculate terminal returns; a blank terminal value is a flagged research gap, not an assumed zero.

### Acceptance gate

- Each included member has one unambiguous price lineage or an explicit fail-closed exclusion.
- Symbol reuse and ticker migrations cannot create spliced histories.
- Foreign securities have a supported reporting/FX route.
- Identity reconciliation produces zero unresolved investable collisions.

## 8. Stage 3 - Prices and Market Features

Use thin wrappers over shared scripts 03 through 06 with `model_family=transportation`. Proposed benchmarks are IYT as primary, XTN as robustness/equal-weight transportation exposure, and SPY as broad-market control; confirm availability and PIT continuity before freezing policy.

Build adjusted prices, volume, liquidity, momentum, skip-month momentum, volatility, drawdown, moving-average trend, beta, and relative strength. Keep benchmarks out of the investable universe. Import delisted history for survivorship correction.

Transportation-specific market diagnostics should include cohort-relative volatility and liquidity. Do not calibrate marine shippers, airlines, and railroads in a single unconditional percentile pool without testing cohort effects.

Delisted price loading is mandatory in this stage:

1. `15_import_transportation_norgate_delisted_prices.py` loads adjusted OHLCV for the 48 delisted members into `fact_price_ohlcv` using `model_family='transportation'` membership and reviewed symbol overrides.
2. The loader writes a coverage report containing source symbol, match reason, first/last bar, loaded rows, adjustment mode, and errors.
3. `28_export_transportation_delisted_price_contract.py` publishes `transportation_delisted_price_export.csv` and `transportation_delisting_events.csv` under `output/industrials_reports/market_data`.
4. The export follows `portfolio_layer/docs/delisted_price_export_contract.md`; the portfolio layer remains read-only and consumes flat files rather than opening `industrials.sqlite`.
5. The transportation daily runner may validate the current delisted contract, but full Norgate reloads remain an explicit bootstrap/research step rather than an unnecessary daily operation.

### Acceptance gate

- Required adjusted-price coverage and staleness thresholds pass.
- Corporate-action adjusted returns reconcile to the selected source policy.
- Every current and eligible historical member has data or an explicit exclusion.
- Every delisted member has price history through its verified final trading date or a blocking coverage issue; acquisition and bankruptcy endpoints are not inferred from a merely stale final bar.
- The two delisted export files contain the same contract tickers used by historical score snapshots and are discoverable by the portfolio survivorship globs.
- Market features are PIT, deterministic, and family-scoped.
- Defense/machinery market artifacts and DB rows remain unchanged.

## 9. Stage 4 - Fundamentals, FX, and Reporting Profiles

Reuse the shared SEC, IFRS, archive, canonical-statement, and FX layers. Add family-scoped transportation reporting overrides and graduation records. Foreign issuers must not be silently treated as US-GAAP CompanyFacts issuers.

The generic statement layer should retain revenue, operating income, EBITDA or approved proxy, net income, operating cash flow, capex, free cash flow, cash, debt, equity, assets, leases, interest, shares, and issuance proceeds with accession, accepted date, period, unit, currency, source taxonomy, and extraction method.

Add transportation-specific facts without relabeling unlike concepts. The metric registry must follow the four actual cohorts and use the active row's `industry` as a sub-profile/applicability control:

| Cohort | Covered active groups/tickers | Core cohort metrics | Industry-specific extensions |
| --- | --- | --- | --- |
| `surface_freight_and_logistics` | 40 freight/logistics, railroad, trucking, rail-equipment, and leasing names | Volume growth, yield/pricing, operating ratio or adjusted operating margin, fuel/labor intensity, capex/revenue, asset turnover, FCF conversion, and lease-adjusted leverage | Railroads: carloads, revenue ton-miles, network velocity/dwell, operating ratio, and network capex. Trucking: tractors/trailers, loaded miles, revenue per loaded mile, utilization, empty-mile ratio, and purchased transportation. Logistics: shipments, net revenue, gross margin, purchased transportation, and contract-logistics margin. Rail equipment/leasing: backlog/orders, fleet utilization, lease rate, fleet age, and residual-value exposure. |
| `air_transport_and_aviation_services` | 22 airlines, airports/services, and aircraft/engine lessors | Traffic/volume growth, unit revenue or yield, capacity growth, utilization, fuel sensitivity, fixed-charge/lease-adjusted leverage, capex commitments, and FCF conversion | Airlines: ASM, RPM, load factor, passenger yield, PRASM, CASM, and CASM ex-fuel. Airports: passengers, aircraft movements, aeronautical/non-aeronautical revenue, concession revenue, and traffic recovery. Lessors/services: owned/managed assets, lease yield, utilization, fleet age, order commitments, maintenance/service backlog, and residual values. |
| `marine_shipping_and_maritime` | 21 liner, tanker, dry-bulk, containership lessor, gas-shipping, and marine-service names | Fleet capacity, utilization, TCE or comparable day rate, cash breakeven, contract/charter coverage, vessel age, net debt/fleet value, capex/newbuild commitments, operating cash conversion, and distribution coverage | Vessel-type sub-profiles must distinguish tanker, dry bulk, container liner, containership owner, gas carrier, and offshore/marine services. Do not compare raw day rates, capacity units, or charter duration across incompatible vessel types without normalization. |
| `development_stage_and_speculative_transport` | 29 pre-scale/small air mobility, logistics, shipping, rail-technology, and trucking names | Cash and liquid investments, TTM cash burn, runway, gross and net capital raised, dilution/share growth, SBC, debt/convertible obligations, going-concern status, listing compliance, revenue status, liquidity, and financing dependence | When an issuer has commercial operations, apply the matching air/surface/marine operating sub-profile only to explicitly reported metrics. Pre-revenue issuers use milestone evidence such as certification/testing, deployed fleet/assets, signed contracts or backlog, production readiness, and commercialization timing; projections are never treated as realized KPIs. |

Metrics shared across a cohort still require compatible definitions. For example, a railroad operating ratio and airline CASM are both efficiency indicators but are separate raw features; they may enter a common normalized component only after cohort-specific transformation. Development-stage names must not be ranked favorably merely because mature-company metrics are not applicable.

These metrics require a candidate/evidence lane analogous to machinery disclosure candidates. Store the source label, exact text/table evidence, scope, unit, period, accession/document, confidence, parser version, and review status. Segment-only, non-comparable, or ambiguous disclosures stay in review and do not become scored facts.

### Acceptance gate

- No fact is visible before its public availability date.
- Currency conversion uses a PIT rate no later than the score date.
- Missing remains null; it is never converted to zero except under a narrowly documented accounting policy.
- Required generic metrics receive an exact availability status.
- Transportation disclosure parsers pass fixture tests for each cohort before values become score eligible.
- The bounded specialized scan recovers at least 95% of active and 90% of all active-plus-inactive
  issuer documents, with SEC URL, content hash, accession, evidence, unit, period, confidence,
  parser version, and status present for every candidate.
- Every cohort has candidate signal, while only unambiguous single-value, period-compatible
  candidates may be `ACCEPTED`; ambiguous growth, scope, period, and commercialization evidence
  remains review-required.
- A full historical specialized-disclosure backfill is authorized only when each mature cohort
  reaches at least 25% accepted active-ticker coverage in the bounded scan. A lower result is
  `PARSER_EXPANSION_REQUIRED`, not permission to repeat the full historical parse.
- Reporting-profile and fallback counts meet policy thresholds.

## 10. Stage 5 - Ownership, Short Interest, and Positioning

Reuse the shared Form 4, 13F, FINRA, IBKR, and positioning pipeline with explicit `model_family=transportation`. Ensure the positioning universe exported upstream is family-specific and includes historical aliases where needed.

For foreign/OTC names, unsupported feeds must create explicit missing-source statuses. They must not receive neutral or favorable scores. Preserve publication lags and filing dates for PIT calculations.

### Acceptance gate

- Each feed has a dated coverage report and source manifest.
- Family-specific imports cannot clear another family's `feature_positioning` rows.
- Missing coverage lowers confidence or eligibility according to policy.
- Stage 14 validates transportation only when invoked for transportation.

## 11. Stage 6 - Transportation Scoring Feature Contract

Create a transportation-specific scoring feature builder and validator. Begin in shadow mode. Recommended components are:

1. **Market/trend:** cohort-relative momentum, trend, volatility, drawdown, and liquidity.
2. **Quality:** margins, ROIC, asset turnover, cash conversion, balance-sheet resilience, and earnings quality.
3. **Growth/revisions proxy:** revenue and operating-income growth plus approved operating KPIs.
4. **Valuation:** EV/EBITDA, EV/sales where appropriate, FCF yield, and cohort-appropriate asset valuation diagnostics.
5. **Operating efficiency:** cohort-specific operating ratio, utilization, yield, load factor, or asset productivity.
6. **Capital intensity and balance-sheet risk:** capex burden, lease-adjusted leverage, interest coverage, dilution, and refinancing risk.
7. **Positioning:** insider, institutional, short-interest, and borrow signals with coverage-aware confidence.

Use cohort-specific feature applicability. An airline load factor must never enter a railroad percentile as missing or zero. Maintain a registry containing feature ID, formula, direction, cohort applicability, minimum history, winsorization, source fields, birthdate, confidence rule, and research/production status.

### Acceptance gate

- Exactly one feature row exists per eligible PIT member/date.
- Every score input distinguishes observed zero, missing, not applicable, and parser failure.
- Cross-sectional transforms are computed within the approved cohort/pool using only members active on that date.
- Feature birthdates prevent historical backfill before availability.
- Builder output and validator agree on schema, units, ranges, and provenance.

## 12. Stage 7 - Shadow Scores, Ranking, and Eligibility

Create an eligibility policy CSV and deterministic shadow publisher comparable to defense. The publisher must read transportation policy through the family resolver, not defense defaults.

The rank table must satisfy `portfolio_layer.scores.adapters`' `industrial_family` contract, including `final_score` on 0..100, contiguous ranks, confidence, calibration fields, survivorship flags, OOS validity fields, model/scoring versions, and portfolio candidate fields.

Initial status must be shadow/non-investable for portfolio use until Stage 12 promotion. Missing metrics reduce confidence and may block eligibility; they must never improve rank through implicit neutral filling.

### Acceptance gate

- Same inputs and versions produce byte-stable scores/ranks.
- Cohort and global ranks are explicit and ties follow a documented deterministic rule.
- Rank-ready and portfolio gates fail closed on stale/missing required data.
- The rank table passes its independent validator and the industrial-family adapter in shadow mode.
- Output is written only to `output/industrials/transportation/dashboard/<asof>/transportation_final_rank_table.csv` plus approved manifests/sidecars.

## 13. Stages 8-9 - PIT History, OOS Calibration, and Backtesting

Build immutable, survivorship-corrected weekly or approved-frequency snapshots from the configured start date. Each artifact must record as-of date, membership snapshot, source availability, feature birthdates, config hash, git commit, DB/source hashes, schema version, scoring contract, model version, and command.

Calibration must use embargoed walk-forward train/validation/holdout windows. Optimize constrained weights with stability penalties and bounds; compare against equal-weight and simple-factor baselines. Evaluate information coefficient, hit rate, spread returns, turnover, drawdown, capacity, cohort contribution, concentration, and sensitivity to costs.

Backtests must model at minimum bid/ask and commissions, liquidity participation, turnover, delistings, corporate actions, ADR/FX effects, and realistic rebalance timing. Results must be reported globally and by transportation cohort so a cyclical shipping or airline episode cannot dominate apparent model quality.

### Acceptance gate

- No future filings, membership, FX, prices, or later parser knowledge leaks backward.
- Delisted/historical coverage meets the policy threshold.
- Holdout performance and stability exceed predeclared thresholds net of costs.
- No single cohort or small ticker subset explains the full result.
- Calibration artifacts are immutable and hash-manifested.
- Failed research cannot alter production weights or OOS flags.

## 14. Stage 10 - Dashboard and Portfolio Integration

Add a transportation source block to `portfolio_layer/config.yaml` using adapter `industrial_family` and `model_family: transportation`. Keep the adapter generic; add transportation-specific logic only if the shared final-rank contract cannot represent a necessary field. The initial shadow configuration should be:

```yaml
- model_family: transportation
  adapter: industrial_family
  enabled: true
  required: false
  staleness_tolerance_days: 3
  sector: "Industrials"
  industry: "Transportation"
  industry_aggregate: "Transportation"
  file_mode: dated
  file_path: "industrials/transportation/dashboard/{yyyy-mm-dd}/transportation_final_rank_table.csv"
  require_oos_score_valid: true
  calibration: {neutral: "median", scale: 50.0, expected_alpha_at_full: 0.15}
```

The connection has two separate contracts:

1. **Daily score contract:** `20_validate_transportation_portfolio_adapter_shadow.py` calls `portfolio_layer.scores.adapters.run_adapter` against the dated rank table, validates exact as-of and expected PIT/current membership counts, and confirms shadow/pre-lock/production gate behavior.
2. **Research survivorship contract:** the Stage 3 delisted price/events exports land under `output/industrials_reports/market_data`, where the existing `portfolio_layer/config.yaml` `survivorship_panel.delisted_price_export_globs` and `delisting_events_globs` discover them automatically.

Add transportation cases to `tests/test_portfolio_industrial_family_adapter.py` and an end-to-end test modeled on the machinery adapter test. Do not use defense's current `tech_family` adapter choice as the transportation template; transportation must use the generic `industrial_family` contract.

Validate:

- exact as-of matching;
- schema and dtype stability;
- unique tickers and contiguous ranks;
- `final_score` in 0..100;
- confidence in 0..1;
- portfolio gate consistency;
- OOS validity and lock date;
- no collision with defense or machinery source IDs;
- correct sector sleeve and risk-budget mapping.
- portfolio-layer ingestion without importing transportation code or opening the industrials database;
- portfolio survivorship discovery of the transportation delisted prices and events, with all 48 curated names covered or explicitly incomplete.

Portfolio integration remains disabled until promotion approval. The implementation must not infer a portfolio allocation merely because a shadow rank table exists.

## 15. Stage 11 - Daily Orchestration and Operations

Implement `16_run_transportation_daily_refresh.py` using the same shared lock and manifest semantics as defense. Required production order:

1. Resolve family config and acquire shared industrials lock.
2. Sync adjusted prices and build/validate market features; verify the previously loaded delisted-price/events contract and fail research publication if required coverage is incomplete.
3. Sync FX and incremental SEC/IFRS/archive fundamentals.
4. Build and validate financial features and reporting-profile status.
5. Refresh/import/validate positioning.
6. Build and validate scoring features and eligibility.
7. Publish and validate the shadow/production rank table.
8. Validate the portfolio adapter handoff.
9. Write atomic logs, coverage summaries, hashes, and run manifest.

Support `--asof`, `--dry-run`, `--list-steps`, `--selftest`, and a positioning-through-publish recovery mode. Network steps must be marked in the manifest. A failed stage must stop downstream publication; an old successful file must never be mistaken for the requested as-of output.

### Acceptance gate

- Dry-run order is covered by a test.
- Lock-path parity with defense and machinery is tested.
- A scratch-DB bootstrap and current-date run pass end to end.
- A bootstrap/research run loads and exports all 48 curated delisted rows before survivorship-corrected history is built.
- Rerunning the same date is idempotent or creates an explicit replacement record.
- Failure injection proves no partial rank table is published.

## 16. Stage 12 - Production Promotion and Governance

Promotion requires a reviewed, immutable calibration bundle and an explicit command. `27_promote_transportation_oos_production.py` must freeze weights, bounds, feature list, preprocessing, cohort policy, versions, training windows, validation evidence, benchmark definition, and hashes.

After promotion, the daily publisher may set `oos_score_valid_flag=1` only for dates on or after the production start date and only when the runtime contract matches the frozen artifact. Pre-lock dates remain `pre_lock_research`.

Any change to universe policy, feature definitions, weights, cohort mapping, benchmark, winsorization, missingness policy, or portfolio gate requires versioning and, where material, recalibration. Manual edits to generated rank tables or calibration artifacts are prohibited.

## 17. Test Plan

Create `tests/industrials/transportation` with these layers:

### Unit tests

- family config resolution and missing-family failure;
- ticker, CIK, boolean, country, currency, and alias normalization;
- cohort assignment and duplicate-line rules;
- metric formulas, units, signs, null behavior, and cohort applicability;
- deterministic ranking and tie breaking;
- PIT availability and feature-birthdate rules.

### Contract tests

- seed/system CSV schemas;
- DB primary/unique keys include model family where required;
- source registry IDs are unique;
- final-rank header matches the industrial-family adapter;
- output paths are transportation-scoped;
- wrappers always inject `--model-family transportation`.

### Integration tests

- clean scratch DB Stage 0-7 smoke;
- active plus historical membership load;
- one SEC issuer, one foreign/IFRS issuer, one ADR/ordinary pair, one delisted issuer, and one corporate-action lineage;
- current-date and historical-as-of feature build;
- publish plus independent validation plus portfolio adapter.

### Non-regression tests

- capture defense and machinery row counts/hashes, execute every transportation mutation, and assert equality afterward;
- run defense after transportation and assert transportation rows/artifacts are unchanged;
- same ticker in two model families remains valid and isolated;
- family-specific quality-issue cleanup cannot clear another family.

### Research validation tests

- synthetic future filing/price/membership leakage is rejected;
- changed sealed artifact without replacement metadata is rejected;
- survivorship omission is rejected;
- unsupported neutral fill is rejected;
- costs and turnover are required in backtest output.

## 18. Implementation Sequence and Pull-Request Boundaries

Use small, reversible implementation batches:

1. **Shared family configuration:** resolver, config migration, and defense/machinery regression tests.
2. **Transportation scaffolding and seed:** directories, reviewed CSVs/YAML, source registry, seed validator.
3. **Universe and identity:** active/historical/delisted loaders, aliases, listing dates, reconciliation.
4. **Market stage:** wrappers, benchmarks, delisted prices, features, validators.
5. **Financial stage:** reporting policy, FX, generic statements, transportation disclosure candidates and parsers.
6. **Positioning stage:** family-scoped upstream sync/import/validation.
7. **Feature and shadow scoring:** registry, builder, eligibility, publisher, validators.
8. **Dashboard and portfolio shadow handoff:** generic adapter configuration and end-to-end smoke.
9. **Historical backfill and research:** immutable PIT snapshots, calibration panel, diagnostics, optimization, backtest.
10. **Promotion:** frozen model artifact, OOS contract, production daily runner, runbook.

Each batch must include tests and update `IMPLEMENTATION_STATUS.md`. Do not combine family-config migration with production promotion.

## 19. Definition of Done

Transportation is implemented—not merely scaffolded—when all of the following are true:

- The reviewed active, historical, and delisted universes pass all identity and membership gates.
- Shared scripts resolve transportation configuration without a defense fallback.
- Market, financial, FX, ownership, positioning, and scoring features have PIT provenance and pass coverage thresholds.
- Transportation-specific operating metrics have reviewed definitions and parser evidence.
- Shadow rank output passes independent validation and the portfolio adapter.
- A complete survivorship-corrected history has been generated and sealed.
- Walk-forward OOS calibration and net-of-cost backtests pass predeclared gates.
- Production artifacts, versions, hashes, and lock date are frozen.
- Daily orchestration is idempotent, fail-closed, and protected by the shared industrials lock.
- Defense and machinery regression suites pass with unchanged production contracts.
- Operational documentation describes normal refresh, recovery, investigation, backfill, recalibration, and rollback procedures.

Until every promotion gate passes, transportation must remain a shadow model with portfolio eligibility disabled.
