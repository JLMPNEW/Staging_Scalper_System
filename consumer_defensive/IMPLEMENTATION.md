# Consumer Defensive Implementation Plan

Status: Stages 0-5 are deployed; Stage 4 passes 40/40 at `2026-08-14`, Stage 5 passes 18/18 at the latest fully supported PIT cutoff `2026-08-11`, and the foundation audit records `proceed_stage6a`
Package: `consumer_defensive`  
Canonical Portfolio Layer sector: `Consumer Staples`  
Internal universe sector label: `Consumer Defensive`

## 1. Objective

Build a fully independent Consumer Defensive scoring pipeline that:

- owns its database, configuration, source registry, parser state, feature tables, scores, outputs, and orchestration lane;
- covers four broad calibration cohorts with enough current names for cross-sectional work;
- uses point-in-time universe membership and survivorship-correct historical data;
- extracts and validates cohort-specific operating metrics from issuer disclosures;
- uses the shared top-level `factor_validation` kernel through a Consumer Defensive-owned adapter;
- publishes a strict dated score contract through a dedicated Portfolio Layer adapter; and
- supports either promoted production or an explicitly labeled shadow-monitoring state, based first on a low-cost feasibility study and later on full OOS evidence.

Consumer Defensive must not import from, write to, or share a database with `industrials`, `technology`, `biotech_index`, or `med_devices`. The only permitted code dependencies outside the package are sector-neutral repository services with explicit contracts, including `factor_validation`, the top-level `dedicated_parser` kernel, and file-only Portfolio Layer integration. Consumer Defensive must own every sector-specific wrapper, registry, policy, table, and validator around those neutral kernels.

## 2. Reference Architecture And Deliberate Differences

The implementation should preserve the strongest Technology patterns:

| Technology pattern | Consumer Defensive implementation |
|---|---|
| Independent sector database | `${CONSUMER_DEFENSIVE_DB_DIR:-C:/Users/josel/Documents/STAGING/DB}/consumer_defensive.sqlite` |
| Database ownership identity | Fixed one-row `sector_database_identity`; foreign or nonempty unowned databases fail before mutation |
| Reviewed local-input gate | Exact CSV inventory, parsed counts, schema/review state, and SHA-256 verified by `core/input_manifest.py` |
| Sector-owned source registry and normalized facts | Consumer Defensive-owned registry and tables |
| PIT security master and membership intervals | Active plus historical/delisted listing episodes with explicit dates |
| Dedicated parser schema and specialized metric facts | Consumer Defensive parser contract and metric registry |
| Production and research paths separated | Routine refresh excludes calibration searches and research backfills |
| Dated rank tables with OOS fields | `output/consumer_defensive/dashboard/{date}/consumer_defensive_final_rank_table.csv` |
| Immutable diagnostics and governance artifacts | Hash-sealed factor evidence, model lockbox, and run manifests |
| File-only Portfolio Layer adapter | Dedicated `consumer_defensive` adapter; no sector package imports |
| Independent orchestration lane | `db_group: consumer_defensive` |

Deliberate differences from Technology:

- Consumer Defensive is one sector model with four calibration cohorts, not a shared sector database containing independently scored model families.
- There are no compatibility shims into another sector package.
- Specialized metrics may remain measurement-only while coverage is being established; common validated factors can support feasibility testing and shadow monitoring without pretending that missing metrics are production evidence.
- The Portfolio Layer receives one Consumer Defensive final-rank contract, not four independently budgeted cohort sleeves.
- The factor-validation adapter must be written for Consumer Defensive rather than copying Technology's hard-coded software-infrastructure shadow bridge.

## 3. Technology-Aligned Sequence And Early Decision Points

The canonical implementation references are `technology/README.md` and `technology/STAGE_GATES.md`. Consumer Defensive follows their order: architecture, database, security master and universe, market data, SEC financials, ownership and positioning, scoring contract, specialized overlays, diagnostics, calibrated scoring, report-only calibration, portfolio backtest, dashboard, governance, Portfolio Layer handoff, refresh orchestration, and historical snapshot generation.

There is no valid date-by-date feasibility audit before data are loaded. The early decision is therefore made after the same foundational stages used by Technology, not before Stage 1.

### 3.1 Minimum Foundation Before The First Feasibility Review

1. **Stage 0 - Architecture and governance:** freeze independence rules, source registry contracts, the authoritative-input manifest, cohort definitions, the January 2, 2019 snapshot target, the required pre-2019 warm-up, and output contracts.
2. **Stage 1 - Database foundation:** validate the reviewed-input gate, then initialize the independently identified Consumer Defensive database, source registry, run metadata, raw-response storage, canonical dimensions/facts, parser tables, and data-quality tables.
3. **Stage 2 - Security master and universe:** load current securities, historical/delisted securities, aliases, corporate lineages, and exact point-in-time membership intervals.
4. **Stage 3 - Market data and corporate actions:** load adjusted OHLCV, dividends, splits, benchmarks, delisting events, and terminal values far enough back to support the January 2, 2019 score date.
5. **Stages 4 and 5 - SEC and positioning foundation:** load filing metadata and raw/canonical financial facts using acceptance timestamps, then load ownership and positioning history with source birthdates.

Only after those foundations exist can the pipeline publish the Stage 5 foundation-coverage audit: eligible counts by date and cohort, feature coverage, membership gaps, delisted-event gaps, and an estimate of the earliest reproducible common-feature date. No specialized parser, calibrated score, Portfolio Layer adapter, or orchestration entry is required for this checkpoint. The definitive historical feature-panel readiness audit remains Stage 6C work.

### 3.2 First Foundation Review

After Stages 0-5 have loaded enough history, publish an evidence inventory rather than an invented weighted score:

- NYSE trading dates beginning January 2, 2019;
- PIT eligible and price-covered names by date and cohort;
- warm-up sufficiency for each planned market feature;
- SEC filing, canonical financial, and FX coverage available on each date;
- positioning-source birthdates and the dates on which each signal becomes usable;
- active, historical, delisted, alias, and terminal-event coverage;
- the earliest date on which the common Stage 6A contract can be reproduced; and
- unresolved work required for specialized Stage 6B metrics.

The report states what can be built and what remains unknown. It does not assign an artificial probability of promotion before diagnostics and OOS research exist.

### 3.3 Foundation Review Decision

The first foundation review produces one of three implementation decisions:

1. **Continue through Stage 6B and research** - the common historical foundation is reproducible and the remaining specialized work is justified.
2. **Continue as a limited shadow candidate** - the common foundation is usable, but specialized or survivorship coverage needs forward monitoring before promotion can be evaluated.
3. **Defer** - likely evidence value does not justify the remaining implementation cost. Preserve the loaded foundation and reports so the review can be repeated when the universe or data improve.

These are continuation decisions, not statistical promotion decisions. Promotion remains unknown until the later Technology-style diagnostics, calibration, backtest, and operational stages are complete.

### 3.4 Shadow Monitoring Contract

An intentional shadow lane may persist as long as it remains useful. It must:

- be labeled `promotion_state: shadow_monitor` in manifests and outputs;
- use `required: false` in orchestration so it cannot block the production master run;
- use a zero Portfolio Layer cap or remain disabled for investment;
- set `portfolio_candidate_gate=0` for every row;
- report current qualification gaps, evidence age, coverage, and next review date;
- distinguish missing and not-applicable specialized metrics from numeric zero;
- never describe current-universe replay as survivorship-correct history; and
- move to promoted status only after explicit review of full OOS, survivorship, cost, and integration evidence.

This mirrors Transportation's intended purpose: monitor whether the evidence eventually qualifies, without forcing capital allocation or pretending it has already qualified.

## 4. Package Layout

Implemented layout through Stage 4:

```text
consumer_defensive/
    __init__.py
    README.md
    IMPLEMENTATION.md
    STAGE_GATES.md
    UNIVERSE_DECISIONS.md
    MARKET_DATA_DECISIONS.md
    config.yaml
    adapters/
        __init__.py
    core/
        __init__.py
        config.py
        db.py
        financial_pipeline.py
        financial_semantics.py
        inline_xbrl.py
        input_manifest.py
        market_data.py
        market_validation.py
        metric_registry.py
        norgate_membership.py
        norgate_prices.py
        script_runtime.py
        source_registry.py
        stage3_runtime.py
        stage4.py
        terminal_events.py
        universe.py
        universe_validation.py
        yahoo_prices.py
    data/
        authoritative_input_manifest.yaml
        consumer_defensive_financial_concept_map.yaml
        consumer_defensive_historical_issuer_identifiers.csv
        consumer_defensive_market_data_policy.yaml
        consumer_defensive_specialized_disclosure_terms.yaml
        consumer_defensive_specialized_metric_registry.yaml
        consumer_defensive_metric_applicability.csv
        consumer_defensive_terminal_event_policy.yaml
        consumer_defensive_universe_policy.yaml
        free_source_registry.yaml
        stage2_source_registry.yaml
    system_csvs/
        consumer_defensive_delisted.csv
        consumer_defensive_security_events.csv
        consumer_defensive_terminal_events.csv
        consumer_defensive_ticker_aliases.csv
    scripts/
        00_init_consumer_defensive_db.py
        00a_audit_norgate_history_access.py
        01_load_consumer_defensive_universe.py
        01b_load_consumer_defensive_historical_membership.py
        02_validate_consumer_defensive_universe.py
        03_sync_consumer_defensive_adjusted_prices.py
        03a_sync_consumer_defensive_yahoo_prices.py
        03b_import_consumer_defensive_norgate_prices.py
        03c_reconcile_consumer_defensive_terminal_events.py
        04_audit_consumer_defensive_market_data_policy.py
        05_build_consumer_defensive_market_features.py
        06_validate_consumer_defensive_market_stage.py
        07_sync_consumer_defensive_sec_fundamentals.py
        07a_sync_consumer_defensive_inline_xbrl_fallback.py
        08_build_consumer_defensive_financial_features.py
        08a_run_consumer_defensive_specialized_disclosure_census.py
        08b_validate_consumer_defensive_financial_and_disclosure_stage.py
        08c_build_consumer_defensive_census_review_pack.py
        11_sync_consumer_defensive_fx_rates.py
tests/
    consumer_defensive/
```

Stage 5 modules now occupy scripts `09`, `09a`, `10`, `10a`, and `10b`. Stage 6 scoring features, the Consumer Defensive dedicated-parser and factor-validation adapters, calibrated scoring, publishing, Portfolio Layer handoff, orchestration, and backfill remain deferred. Their filenames and numbers must not collide with implemented entry points. Once Stage 12 exists, the refresh runner's explicit step table is the authoritative run order.

The repository ignores `*.csv` by default. Every reviewed input listed in `data/authoritative_input_manifest.yaml` must have a narrow `.gitignore` exception and be tracked, including `ticker_mapping/consumer_defensive.csv`, `consumer_defensive/system_csvs/*.csv`, and the authoritative CSVs under `consumer_defensive/data/`. `core/input_manifest.py` verifies the entire discovered authoritative CSV inventory, exact repository-relative paths, parsed nonblank row counts, review/schema metadata, and SHA-256 hashes. Missing, extra, duplicate, escaping, or tampered inputs fail configuration loading before database mutation.

## 5. Cohort Taxonomy And Universe Contract

Authoritative current source: `ticker_mapping/consumer_defensive.csv`.

Reviewed current source counts:

| Current source label | Production cohort ID | Production display name | Current rows |
|---|---|---|---:|
| `Beverages` | `beverages` | Beverages | 22 |
| `Discount Stores` | `consumer_staples_distribution_retail` | Consumer Staples Distribution & Retail | 22 |
| `Household & Personal Products` | `household_personal_tobacco` | Household Personal & Tobacco | 25 |
| `Packaged Foods` | `packaged_foods_agricultural_products` | Packaged Foods & Agricultural Products | 39 |

The broad Beverage cohort replaces brewer, non-alcoholic beverage, winery, and distiller subclasses for calibration. Product-type attributes may remain descriptive fields but cannot create separate score universes.

Each current security must have:

- stable internal security and issuer IDs;
- ticker and provider-specific price symbols;
- CIK when applicable;
- exchange and primary-listing flag;
- security type and ADR/ADS status;
- issuer domicile and listing country stored separately;
- cohort ID and metric-applicability subtype;
- membership start date and current status;
- ETF/index membership evidence;
- liquidity evidence and investability status; and
- explicit exclusion reason when not investable.

Historical securities belong only in PIT membership intervals and Stage 11 research sidecars. They must never enter the current live rank table.

## 6. Metric Architecture

### 6.1 Common Cross-Sector Components

All cohorts start from the same auditable component families:

1. `valuation`
   - forward and trailing P/E where meaningful;
   - EV/EBITDA;
   - FCF yield;
   - EV/sales only for low-margin or temporarily unprofitable cases;
   - cohort-relative and sector-relative valuation percentiles.
2. `quality`
   - gross and operating margin;
   - ROIC and return on tangible capital;
   - FCF margin and FCF conversion;
   - accrual quality;
   - leverage and interest coverage.
3. `durable_growth`
   - revenue and EPS growth;
   - organic growth when disclosed;
   - stability of growth across periods;
   - analyst revision breadth when an approved source exists.
4. `operating_resilience`
   - margin stability;
   - drawdown and beta behavior in risk-off regimes;
   - earnings variability;
   - working-capital discipline;
   - pricing offset versus input inflation.
5. `market_behavior`
   - medium-term residual momentum;
   - realized volatility and downside volatility;
   - drawdown recovery;
   - liquidity and gap risk.
6. `positioning`
   - direct SEC insider activity;
   - 13F ownership and flow where PIT-safe;
   - FINRA short interest;
   - borrow cost only when the source has a valid historical birthdate.
7. `specialized_operating_metrics`
   - applicable cohort metrics listed below;
   - availability-aware weighting;
   - no implicit zeroes or cross-definition pooling.

No production weights are fixed in this document. Candidate weights are constrained and validated in the factor-validation and walk-forward stages. Every weighted signal must exist in the signal registry with direction, availability birthdate, applicable cohort, definition version, source tier, and production status.

### 6.2 Beverages

Candidate parser metrics:

| Metric ID | Purpose |
|---|---|
| `organic_revenue_growth_pct` | Separate underlying demand from FX and acquisitions |
| `volume_growth_pct` | Measure case or shipment demand |
| `price_mix_growth_pct` | Measure pricing and premiumization contribution |
| `revenue_per_unit_growth_pct` | Normalize price realization when units are disclosed consistently |
| `gross_margin_change_bps` | Capture pricing versus commodity and packaging pressure |
| `advertising_promotion_pct_sales` | Measure brand support intensity |
| `market_share_change_bps` | Capture competitive momentum when issuer-disclosed |
| `distribution_points_growth_pct` | Capture route-to-market expansion where consistently reported |
| `alcohol_depletion_growth_pct` | Alcohol-only demand measure |
| `non_alcohol_unit_case_growth_pct` | Non-alcohol beverage demand measure |

Derived candidate signals:

- balanced organic growth: volume plus price/mix rather than price-only growth;
- volume elasticity after price increases;
- gross-margin resilience to sugar, coffee, cocoa, aluminum, PET, freight, and FX changes;
- advertising efficiency relative to organic growth; and
- market-share-supported growth.

Alcohol-only and non-alcohol-only metrics use applicability masks. They remain inside the broad Beverage calibration cohort but are never compared as if they were universally reported.

### 6.3 Consumer Staples Distribution & Retail

Candidate parser metrics:

| Metric ID | Applicability | Purpose |
|---|---|---|
| `comparable_sales_growth_pct` | Store-based retail | Core sales momentum |
| `traffic_growth_pct` | Store-based retail | Customer-count contribution |
| `average_ticket_growth_pct` | Store-based retail | Price and basket contribution |
| `net_store_growth_pct` | Store-based retail | Footprint expansion or contraction |
| `sales_per_square_foot` | Store-based retail | Store productivity |
| `shrink_change_bps` | Store-based retail | Inventory loss and execution risk |
| `private_label_sales_mix_pct` | Retail and distribution | Margin and customer-value differentiation |
| `digital_sales_mix_pct` | Retail | Omnichannel development |
| `inventory_turnover` | All | Inventory discipline |
| `case_volume_growth_pct` | Food distribution | Underlying distribution demand |
| `gross_profit_per_case` | Food distribution | Unit economics and mix |
| `independent_customer_mix_pct` | Food distribution | Exposure to higher-margin independent customers |
| `lease_adjusted_net_leverage` | Store-based retail | Fixed-obligation risk |
| `fixed_charge_coverage` | Store-based retail | Rent and interest coverage |

Derived candidate signals:

- traffic-led versus ticket-only comparable-sales growth;
- store productivity and disciplined unit growth;
- inventory/shrink control;
- private-label and digital-mix progression;
- distributor volume plus gross-profit-per-case quality; and
- lease-adjusted balance-sheet resilience.

`retail`, `food_distribution`, and `hybrid` are metric-applicability subtypes, not separate calibration cohorts. Historical GICS overrides such as `FDO` and `CORE` must retain an auditable business-cohort-override flag.

### 6.4 Household Personal & Tobacco

Candidate parser metrics:

| Metric ID | Applicability | Purpose |
|---|---|---|
| `organic_revenue_growth_pct` | All operating issuers | Underlying growth |
| `volume_growth_pct` | Household and personal care | Unit demand |
| `price_mix_growth_pct` | All | Pricing and premiumization |
| `gross_margin_change_bps` | All | Input-cost and pricing resilience |
| `advertising_promotion_pct_sales` | Household and personal care | Brand investment |
| `market_share_change_bps` | Household and personal care | Competitive strength |
| `innovation_sales_mix_pct` | Household and personal care | New-product contribution |
| `active_customer_growth_pct` | Direct selling | Customer-base health |
| `active_representative_growth_pct` | Direct selling | Distribution-force health |
| `tobacco_shipment_volume_growth_pct` | Tobacco | Combustible demand decline |
| `tobacco_price_mix_growth_pct` | Tobacco | Pricing offset to volume decline |
| `reduced_risk_sales_mix_pct` | Tobacco | Portfolio transition |
| `excise_tax_impact_bps` | Tobacco | Regulatory and tax burden |

Derived candidate signals:

- price/volume balance and brand-investment efficiency;
- innovation-supported organic growth;
- market-share-supported margin durability;
- tobacco pricing coverage of shipment declines;
- reduced-risk product transition quality; and
- direct-selling customer and representative health.

`household_personal`, `tobacco`, and `direct_selling_wellness` are applicability subtypes. Tobacco volume declines cannot be scored with the same raw direction as household product volume; all directions are metric-version-specific.

### 6.5 Packaged Foods & Agricultural Products

Candidate parser metrics:

| Metric ID | Applicability | Purpose |
|---|---|---|
| `organic_revenue_growth_pct` | Branded and packaged foods | Underlying growth |
| `volume_growth_pct` | Branded and packaged foods | Unit demand |
| `price_mix_growth_pct` | Branded and packaged foods | Pricing contribution |
| `gross_margin_change_bps` | All | Pricing versus commodity pressure |
| `advertising_promotion_pct_sales` | Branded foods | Brand investment |
| `branded_sales_mix_pct` | Mixed branded/private label | Mix quality |
| `market_share_change_bps` | Branded foods | Competitive strength |
| `commodity_cost_impact_bps` | All when disclosed | Input-cost headwind or benefit |
| `inventory_turnover` | All | Working-capital discipline |
| `capacity_utilization_pct` | Processors and manufacturers | Asset utilization |
| `production_volume_growth_pct` | Processors and manufacturers | Throughput demand |
| `agricultural_processing_margin` | Agricultural processors | Crush or processing economics |
| `livestock_feed_cost_change_pct` | Protein producers | Feed-cost pressure |
| `net_debt_to_ebitda` | All | Balance-sheet resilience |

Derived candidate signals:

- balanced organic growth and volume elasticity;
- pricing coverage of commodity inflation;
- gross-margin recovery without excessive volume loss;
- branded mix and advertising efficiency;
- inventory and capacity discipline;
- agricultural processing margin normalization; and
- leverage-adjusted FCF resilience.

`branded_food`, `private_label`, `protein_dairy`, and `agricultural_processor` are applicability subtypes. Agricultural processing margins and branded-food market share must not be pooled as the same raw metric.

### 6.6 Metric Admission Rules

A specialized metric can enter production scoring only when:

- its definition and direction are versioned;
- applicability is explicit for every cohort subtype;
- source availability datetime is PIT-safe;
- issuer-specific definition variants are not silently pooled;
- amendments supersede prior facts without deleting history;
- plausibility and unit checks pass;
- applicable current-name coverage meets the configured threshold;
- at least the configured minimum number of PIT dates and independent windows exists;
- factor-validation evidence passes; and
- the production weight is explicitly promoted and lockbox-sealed.

Otherwise the metric remains `measurement_only`, `research_candidate`, `not_applicable`, or `review_required` with production weight zero.

## 7. Dedicated Parser Contract

### 7.1 Ownership Boundary

`consumer_defensive/core/dedicated_parser/` owns the Consumer Defensive parser contract. It may call the sector-neutral top-level `dedicated_parser` schema/run-control APIs, but it must not import `technology.core.dedicated_parser`, `industrials`, or any sector-specific parser.

The parser writes only to `consumer_defensive.sqlite`. It cannot update production scores or Portfolio Layer outputs.

### 7.2 Source Priority

1. SEC XBRL facts with canonical or issuer extension concepts.
2. Hash-sealed filing tables with deterministic row/column extraction.
3. Structured filing sections and exhibits.
4. Prose candidates with full evidence text and document hash.
5. Manual adjudication policy for unresolved definition conflicts.

Forms must include 10-K, 10-Q, 8-K exhibits, 20-F, and 6-K where applicable. S-1/F-1 history may be included for newly listed issuers and historical disclosure backfill.

Prose-only values are review candidates by default. They cannot become calibration facts merely because a numeric pattern matched.

### 7.3 Consumer-Owned Tables

Required tables and views:

- shared run-control shadow tables installed into the Consumer Defensive DB;
- `dim_consumer_defensive_metric_definition`;
- `dim_consumer_defensive_metric_applicability`;
- `fact_consumer_defensive_specialized_metric_candidate`;
- `fact_consumer_defensive_specialized_metric`;
- `feature_consumer_defensive_specialized_metric`;
- `feature_financial_metric_availability`;
- `consumer_defensive_sec_parser_filing_input` view; and
- `consumer_defensive_sec_parser_financial_fact_input` view.

Every accepted specialized fact must store:

- model family, ticker, issuer ID, and CIK;
- metric name and metric version;
- numeric value or normalized text value;
- unit, period start, and period end;
- filing date and SEC acceptance datetime;
- accession number and form type;
- source document and SHA-256;
- evidence key and extraction method;
- definition variant and scope;
- confidence and review flag;
- calibration-eligibility flag;
- adjudication decision hash; and
- complete provenance JSON.

### 7.4 Parser Acceptance Tests

- Fresh scratch DB initialization installs every required object.
- No import path begins with `technology.` or `industrials.`.
- Future-available facts are rejected from earlier as-of dates.
- A newer amendment supersedes but does not delete a prior filing fact.
- Values with incompatible definition variants never pool.
- Percent, basis-point, currency, per-unit, and absolute-unit conversions are tested.
- `NOT_REPORTED` and `NOT_APPLICABLE` remain distinct from zero.
- Prose candidates require adjudication unless a metric policy explicitly permits deterministic acceptance.
- Source documents and decisions are hash-sealed.
- Re-running the same inputs is idempotent.
- Parser output cannot mutate scoring or Portfolio Layer tables.

## 8. Factor-Validation Sector Adapter

### 8.1 Adapter Files

Implement:

- `consumer_defensive/adapters/factor_validation.py`;
- a deferred Consumer Defensive factor-validation run wrapper; and
- a deferred Consumer Defensive factor-validation validator.

Their final script numbers are assigned only after the Stage 6C entry points are frozen; the already planned Stage 6C `14`/`14a` names must not be reused.

The adapter must construct `factor_validation.FactorObservation` rows from a Consumer Defensive-owned immutable PIT panel. It must not duplicate the statistical kernel or accept caller-constructed validation results.

### 8.2 Observation Contract

Required source columns:

- `asof_date`;
- `ticker`;
- `cohort_id`;
- `factor_id` or registered factor columns;
- factor availability status and source datetime;
- PIT membership and investability flags;
- sample role;
- market and input-cost regime labels;
- forward total return;
- forward XLP excess return;
- forward SPY beta-residual return; and
- terminal-event inclusion status for historical listings.

Primary target: `forward_xlp_residual_return`.  
Secondary robustness target: `forward_spy_beta_residual_return`.

Initial research horizons: 21, 63, and 126 trading days. Features measured using close-date data enter at the next tradable session, so the default entry lag is one trading day unless a source-specific availability rule requires a longer lag.

### 8.3 Cross-Section Rules

- Sector-wide validation: minimum 30 eligible names per date.
- Cohort validation: target 20 names and hard minimum 12 eligible names per date.
- A date below the relevant minimum is excluded and reported; it is not padded with inactive or non-investable securities.
- Factor families are pre-registered by metric family, cohort scope, horizon, target, and direction.
- BH-FDR membership and alpha are sealed before publication.
- Input-cost regimes and risk-on/risk-off regimes are robustness splits rather than post-hoc factor-selection devices.

When cohort data are too sparse for a standalone signal, use a predeclared sector-wide signal with cohort neutralization or hierarchical shrinkage. Do not manufacture a cohort-specific result from an insufficient cross-section.

### 8.4 Promotion Evidence

Shared-kernel acceptance is necessary but not sufficient. Sector promotion additionally requires:

- directionally correct mean rank IC;
- promotion-facing deflated inference and BH-FDR pass;
- chronological-half sign stability;
- acceptable regime stability;
- minimum independent windows;
- positive net top-minus-bottom spread after configured trading costs;
- bounded turnover and rank persistence;
- no concentration in a single cohort or metric-applicability subtype;
- walk-forward improvement or non-inferiority versus the registered baseline; and
- economic interpretation consistent with the metric definition.

Evidence publishes only below `output/consumer_defensive/factor_validation/`. The root factor-validation ledger and evidence manifests remain immutable. The adapter cannot write production scores or Portfolio Layer files.

## 9. Implementation Stages

Canonical crosswalk:

| Technology stage | Consumer Defensive equivalent |
|---|---|
| Stage 0 Architecture and Governance | Stage 0 independent package, sources, cohorts, history and output contracts |
| Stage 1 Database Foundation | Stage 1 Consumer Defensive SQLite and parser foundation |
| Stage 2 Security Master and Universe | Stage 2 current plus PIT historical/delisted universe |
| Stage 3 Market Data and Corporate Actions | Stage 3 adjusted history, benchmarks, actions and terminal events |
| Stage 4 SEC Financial Statements | Stage 4 SEC/IFRS canonical facts, FX and common financial features |
| Stage 5 Ownership, Insider and Positioning | Stage 5 Consumer Defensive-owned normalized positioning features |
| Stage 6A Scoring Feature Contract | Stage 6A stable common scoring contract with reserved overlay rows |
| Stage 6B Sector Overlays | Stage 6B dedicated parser and cohort-specialized overlays |
| Signal Diagnostics | Consumer Defensive diagnostics plus shared factor-validation adapter |
| Stage 7 Calibrated Scoring | Stage 7 versioned Consumer Defensive baseline |
| Stage 8 Constrained Calibration Research | Stage 8 report-only calibration and walk-forward testing |
| Stage 9 Portfolio Backtest | Stage 9 report-only portfolio simulations |
| Stage 10 Dashboard and Reports | Stage 10 dated final-rank and review artifacts |
| Stage 10B Governance | Stage 10B signal registry, lockbox and promotion state |
| Stage 12 Refresh Orchestration | Stage 12 independent refresh runner |
| Technology scripts 18/19 historical backfill | Post-Stage 12 daily historical dashboard and Stage 11 sidecar backfill |

Stage 11 is the Consumer Defensive-specific extension required for the dedicated Portfolio Layer adapter and end-to-end handoff. It does not change the Technology sector-build order; it consumes the Stage 10 published contract before Stage 12 registers the operational lane.

### Stage 0 - Architecture And Governance

Freeze the independent-package boundary, database/output paths, source ownership, current and historical universe contracts, cohort taxonomy, benchmark policy, January 2, 2019 historical snapshot target, pre-start warm-up policy, final-rank schema, and promotion/shadow governance.

Exit gate:

- Consumer Defensive imports no sector-specific code from other sector packages;
- external upstream data stores are read-only through explicit adapters;
- current, historical, research, production, and Portfolio Layer outputs have separate contracts; and
- source availability claims are documented without pretending unloaded data have been validated; and
- the hash-sealed authoritative-input inventory validates before any stage write path opens.

### Stage 1 - Database Foundation

Create the Consumer Defensive DB, source registry, run and ingestion controls, raw-response storage, canonical security/issuer/taxonomy dimensions, canonical fact tables, parser tables, and quality-issue tables.

Exit gate:

- scratch DB initialization and migration tests pass;
- DDL is split only at SQLite complete-statement boundaries and one outer transaction or nested savepoint covers the entire schema unit;
- statement, registry, backfill, or foreign-key-postcondition failure leaves no partial migration, and reinitialization is idempotent;
- no industrial or technology tables/defaults exist;
- the database identity tuple is exact and a foreign or nonempty unowned database is rejected without mutation;
- parser tables and views validate; and
- DB/output paths fail closed when missing or cross-sector.

### Stage 2 - Current And Historical Universe

Load `ticker_mapping/consumer_defensive.csv`, normalize cohort labels, correct security metadata, and load PIT membership and collision-safe historical listing episodes.

Exit gate:

- no duplicate security or membership keys;
- active ticker count reconciles to the reviewed source;
- four cohort counts reconcile;
- every active row has an investability decision;
- historical rows have exact non-overlapping membership intervals; and
- reused ticker symbols are blocked from automatic historical price lookup; and
- the exact reviewed current/historical candidate set and terminal-event scope reconcile before publication;
- provider symbols are preserved exactly, including punctuation-bearing share classes;
- stable Norgate fingerprints for `US Equities`, `US Equities Delisted`, and `US Indices` cover catalog, candidate, and final reads before all membership rows and reports publish atomically; and
- exact vehicle/date coverage and complete cohort/date breadth, including zero-name combinations, validate.

### Stage 3 - Market Data And Corporate Actions

Load adjusted OHLCV, dividends, splits, delisting events, benchmarks `SPY` and `XLP`, and approved input-cost series. The first load must include the configured warm-up before January 2, 2019 and cover historical members, not only today's universe.

Yahoo adjusted chart data is the active-book and benchmark primary; Norgate total return is mandatory for delisted history and a whole-ticker active fallback. Provider selection is one continuous source per ticker and purpose. Date-level provider splicing is forbidden. Recent listings are retained in the PIT universe and expose unavailable long-lookback features until enough observations accrue.

Yahoo cache writes use temporary-file replacement. `--cache-only` forbids network calls, is mutually exclusive with force refresh, fails on a missing payload, and returns per-payload byte/SHA-256 lineage plus an aggregate manifest hash. This is a deterministic replay contract, not evidence that the replay is strict PIT or strict OOS.

Yahoo and Norgate responses must match the requested provider symbol, date range, chronological order, payload shape, and finite-value rules before cache or database publication. Refreshes replace prices and corporate actions only inside the requested range: stale in-range rows are deleted and out-of-range history is preserved. Coverage is measured against the relevant trading calendar and flags internal-session gaps; first/last dates and row counts are not sufficient evidence.

The source-coverage window is tied to point-in-time investability rather than today's taxonomy surface. For each security it begins at the later of `2017-11-28`, listing start, or the configured 400-calendar-day warm-up before first calibration-eligible recognized membership. WBA and any other explicit terminal-event research exclusion still use first recognized membership so the exclusion cannot erase required audit history. Names whose first applicable interval is later than the requested historical as-of date are future-only and are not allowed to fail that earlier audit. Within the resulting window, a missing-session ratio above 2% or a consecutive gap above five sessions fails; a ratio above 1% warns and remains visible in the coverage report. Selection remains one whole-ticker provider.

The isolated v5 audit at `2026-08-10` reconciled all 119 securities plus `XLP` and `SPY`: all 121 had a qualifying source, with 108 Yahoo and 13 Norgate selections. The official feature build wrote 108/108 full-quality active-security rows, and the Stage 3 validator passed. MAMA selected Norgate with 1,549/1,549 expected sessions over its required `2020-06-10` to `2026-08-10` window. Its 42 older omissions ended in 2019 and are outside the required 400-day warm-up for its `2021-07-15` first admission. At `2019-01-02`, all 103 then-relevant candidates qualified and MAMA was correctly future-only. These isolated runs did not write the production database.

Norgate price extraction stages provider results until both `US Equities` and `US Equities Delisted` update fingerprints are unchanged across the full run. A stable snapshot publishes price/action facts in one transaction; drift leaves those facts untouched and persists only a failed zero-row ingestion-run audit record.

Exit gate:

- active adjusted-price coverage meets policy;
- split and dividend adjustments reconcile;
- low-history and low-liquidity exceptions are explicit;
- successor and cash/stock merger events are represented correctly; and
- historical rows without terminal truth remain research-ineligible.

Price ingestion, whole-ticker source selection, coverage audit, current market-feature build, validation, and terminal-event reconciliation are implemented. The terminal contract consists of:

- `consumer_defensive/system_csvs/consumer_defensive_terminal_events.csv` for primary-document terms;
- `data/consumer_defensive_terminal_event_policy.yaml` for the exact 11-security scope and 126-session successor-history rule;
- `fact_terminal_event_reconciliation` plus canonical `fact_security_event` rows;
- `core/terminal_events.py` for loading, structural validation, economic-date overrides, and cash/stock horizon values; and
- `scripts/03c_reconcile_consumer_defensive_terminal_events.py` for a targeted rerun.

Ten terminal events are calibration eligible. WBA remains explicitly excluded only for labels whose horizon crosses its terminal event because the contingent right is unresolved; the known 11.45 cash floor is stored but is not treated as complete terminal truth. WBA labels ending before 2025-08-28 remain eligible.

### Stage 4 - SEC Financial Statements

Load SEC submissions, filing metadata, raw XBRL facts, canonical financial statements, issuer reporting profiles, and FX history before building point-in-time common financial features.
Implemented Consumer Defensive order. It follows the same SEC -> FX -> canonical-feature data dependency but does not invoke Technology scripts, configuration, caches, tables, or sector logic:

```powershell
python consumer_defensive\scripts\07_sync_consumer_defensive_sec_fundamentals.py
python consumer_defensive\scripts\07a_sync_consumer_defensive_inline_xbrl_fallback.py
python consumer_defensive\scripts\11_sync_consumer_defensive_fx_rates.py
python consumer_defensive\scripts\08_build_consumer_defensive_financial_features.py
python consumer_defensive\scripts\08a_run_consumer_defensive_specialized_disclosure_census.py
python consumer_defensive\scripts\08b_validate_consumer_defensive_financial_and_disclosure_stage.py
python consumer_defensive\scripts\08c_build_consumer_defensive_census_review_pack.py
```

Stage 4 also runs the first disclosure-availability census because it already owns the accepted-time filing cache. `data/consumer_defensive_metric_applicability.csv` assigns all 108 active and 11 historical names to a reviewed applicability subtype. `data/consumer_defensive_specialized_disclosure_terms.yaml` defines explicit search phrases for every registered candidate metric. The census is routed by cohort and subtype and stores only discovery evidence and coverage status; it does not parse numeric specialized observations and cannot change a production weight.

`core/financial_semantics.py` contains pure deterministic semantic controls: prior-observation-only FX anomaly classification with reviewed redenomination exemptions, same-context accounting-identity revenue selection, approved capex payment-sign normalization, safe TTM selection, and ratio construction that refuses incompatible period/taxonomy/currency inputs. `core/financial_pipeline.py` composes those controls into canonical selections and feature bundles with reported and normalized values, source-fact/accession/taxonomy/currency lineage, rejected candidates, average-balance inputs, definition version, basis period, and explicit quality reasons.

SEC and FX cache writes are atomic and return per-file byte/SHA-256 records plus an aggregate cache-manifest hash. Their `--cache-only` modes forbid network access and reject force refresh. SEC cache-only sync treats missing eligible filing-document cache entries as explicit failures rather than merely recording unhydrated documents. FX sync stores raw rates and classifier reasons, quarantines unusable anomalies, and allows financial conversion to consume only usable rates.

Yahoo FX validation preserves the provider's positional timestamp/close semantics. A JSON `null` close represents a missing daily quote and is skipped; non-null values must remain numeric, finite, and positive. At most two rows no more than seven calendar days beyond the requested boundaries may be validated and filtered, covering Yahoo's in-progress next-day FX row without allowing a materially wrong cache object. The exact symbol, equal nonempty arrays, unique increasing timestamps and UTC dates, and at least one usable requested-window observation remain mandatory.

Stage 4 schema changes are frozen ordered migration units registered by checksum through migration v10. The current SEC ingestion-config is v8, and issuer-scope contract v3 binds normalized reporting currency with ticker/company/CIK identity. Ingestion-config v8 explicitly maps recognized amendment and transitional financial-form variants to their base Companyfacts family while continuing to reject cross-family conflicts. Schema migration v8 quarantines earlier-scope reconciliation/snapshot pointers in place and invalidates trust when filing-company or reporting-currency inputs change. Migration v9 adds exact accession-keyed indexes for bounded shared-accession reconciliation; migration v10 adds exact sealed inline-XBRL fallback run provenance and reporting-profile fallback lineage without changing the SEC acquisition snapshot. A migration ledger gap, future version, checksum mismatch, backfill-parity failure, or foreign-key failure aborts the complete unit. Large identity/lifecycle backfills use bounded keyset batches. Legacy current projections and incompatible legacy identities cannot be accepted as a current reconciliation.

The SEC ingestion contract is chronological. The singleton as-of watermark advances transactionally with full, targeted, partial, and reconciliation mutations. A request older than the watermark is rejected before configuration, cache, provider, or database mutation. Historical reconstruction from `2019-01-02` must use a fresh scratch database initialized at the earliest required date and advance in order; the current production database is never rewound.

A complete SEC reconciliation binds the current ingestion-config hash and exact issuer-scope hash to the immutable cache snapshot. The scope includes every issuer identity field declared by the current contract, rather than only a ticker count. Shared SEC accessions and documents use many-to-many issuer bridges. Association changes are represented by append-only effective-dated lifecycle events with deterministic hashes, and raw/canonical facts retain deterministic source-observation identities.

SEC inputs are copied into a global immutable SHA-256 content-addressed store before date-local `sealed/YYYY-MM-DD` objects are linked or copied from that store. A mutable acquisition alias is never used as the immutable hardlink source. Logical paths, nested SEC document names, URL quoting, Windows reserved names, symlink resolution, hashes, sizes, and containment under the configured cache/seal roots fail closed. Cache-only reuse requires the exact current config, issuer scope, complete reconciliation, lifecycle state, and verified seal.

The Stage 4 census reads only the exact selected bytes in the reconciled date seal. It cannot consume a later mutable alias or every document accumulated historically for an accession. The shared dedicated-parser kernel has a Consumer Defensive-specific fail-closed catalog/direct-input boundary: it independently receives the expected ingestion-config hash, recomputes the exact issuer scope, verifies lifecycle/manifest identities, requires the direct filing set to equal the PIT parser view, requires every supplied document to match an active PIT bridge row and exact seal, enforces identical filing/document keysets, and reads only sealed objects. This boundary does not implement Consumer Defensive metric policies or enable promotion.

The identified Stage 4 performance root cause was SQLite access shape, not financial-semantic logic: raw-fact replacement had no index on the canonical table's `source_raw_fact_id` foreign-key child, the legacy raw index did not exactly match the ticker/source/accepted-time delete predicate, and Companyfacts observations were inserted one row at a time. The fix adds `idx_stage4_canonical_raw_fact_id`, migrates to `idx_stage4_raw_ticker_source_accepted`, and uses one per-issuer bulk insert. `tests/consumer_defensive/test_stage4_query_plans.py` locks the two delete query plans and existing-database index migration. The historical 48.2-second optimized run predates the current full reconciliation/sealing contract and is not a current end-to-end performance claim.

This early census answers which specialized metrics have enough issuer disclosure support before Stage 6B parser work begins. Stage 6B consumes the census, implements numeric extraction only for viable candidates, adjudicates definitions/units, and keeps unsupported candidates missing and zero-weight.

The `2026-08-11` report is retained only as a legacy pre-hardening baseline. The fresh isolated chronological v5 replay completed under schema migration v9, SEC ingestion-config v8, and issuer-scope v3 without writing the production database. SEC cache-only reconciliation covered all 119 issuers with zero failures: 209,111 issuer-filing associations, 208,705 unique accessions, 406 shared accessions, 2,149,695 raw XBRL facts, and 952 selected sealed documents. The exact 1,287-file, 947,150,199-byte SEC cache manifest has SHA-256 `caf6d962f05485aa46a123bc488d32f53b851dc3b7f0e338e7adea3af6fd669c`; the association manifest has SHA-256 `d1300f5fd1eb15b3dd1431b2f9312c4f5658df9689c5c2a9f8c6fd31437fa540`.

A fresh chronological migration-v10 replay began with an empty database and consumed only the retained `2026-08-10` caches. BTI and BUD's later 6-K primary documents contain no non-DEI numeric inline facts, so they do not move the financial reporting anchor. FMX, JBS, KOF, and UL produced 13,176 numeric facts, 3,111 consolidated facts, and 130 model-mapped facts under `consumer_defensive_inline_xbrl_v1`. After rebuild the validator passes 40/40 checks with 119/119 covered profiles, zero fallback-provenance mismatches, 2,152,806 raw facts, 231,024 canonical facts, zero missing canonical FX conversions, and 119 feature rows with the prior quality distribution. Streamed semantic hashes for raw, canonical, feature, and current census-v3 tables exactly match the retained migration-v10 continuation.

The official FX cache-only replay for `2010-01-01` through `2026-08-10` accepted 12 currencies and published 49,867 rows: 49,815 usable and 52 quarantined. Its exact 12-file, 5,694,168-byte range manifest has SHA-256 `99deee8510b8e10b4ed581930fe1ad7f06fa01c67f9532563809126f19e486f6`. CLF is the sole upstream source gap because its preserved Yahoo payload contains only `2026-08-11`, outside the requested cutoff; the sync correctly reports partial status and exits nonzero. No selected canonical fact requires CLF, all five required currencies are covered, and `canonical_fx_missing` is zero, so this disclosed source gap is not an additional validator failure.

The Companyfacts/inline-XBRL code gap is resolved. The terminology workflow generated and adjudicated a deterministic 10-row review pack from 1,067 candidates, covering all four cohorts, both census outcomes, active/historical roles, domestic/foreign profiles, annual/interim forms, and all six metric families. The reviewed standalone `sales leaders` trigger was removed, census parser v3 was rerun from the exact seal, and the regenerated ledger validates `ADJUDICATED`. The FDP-corrected production rebuild now advances Stage 4 through `2026-08-14`: 2,179,929 raw facts, 233,510 canonical facts, 49,879 FX rows, 119 feature rows, 4,522 census summaries, and 782 evidence rows. The live validator passes 40/40, production integrity and foreign keys are clean, and the post-deployment Consumer Defensive/shared-parser suite passes 419 tests with 6 platform-specific skips. Separately, Stage 6B must establish a complete PIT historical filing/document inventory back to `2019-01-02`; a complete current-date seal does not prove that inventory.


Exit gate:

- filing availability is based on SEC acceptance datetime;
- domestic and foreign-private-issuer paths work;
- common financial features exist for eligible active names;
- historical members are included when their membership and identifier records are eligible;
- reported-currency values and USD valuation conversions are reproducible;
- quarterly, annual, and TTM facts never use information before its acceptance timestamp;
- canonical fact selection is reporting-context unambiguous, revenue/capex semantics are explicit, and rejected facts retain explainable lineage;
- ratio inputs match period, taxonomy, and currency, while average-balance ratios use compatible beginning and ending facts;
- quarantined FX rates cannot enter USD conversions and raw anomaly/exemption evidence remains auditable;
- financial features carry the current definition version, basis period, lineage, and explicit quality reasons;
- every loaded security has one nonblank reviewed applicability subtype;
- the specialized registry and disclosure-term registry have identical metric sets;
- the census publishes a complete ticker-by-metric applicability matrix with explicit parse-unavailable status; and
- specialized observations and weights remain untouched.

### Stage 5 - Ownership And Positioning

Load Consumer Defensive-owned normalized insider, 13F, short-interest, and borrow facts from approved sources. External upstream stores are read-only.

Implemented boundary:

- `core/stage5_schema.py` owns the ordered checksummed Stage 5 v1-v2 migrations, source contract, and issuer-level Section 16 coverage ledger.
- `core/stage5_import.py` imports Form 4 current truth by CIK, independently records issuer filing coverage, imports authoritative-source 13F/FINRA/borrow rows, retains stable observation identities, and uses PIT share-count proxies only when they are safe.
- `core/stage5.py` owns upstream handoff generation, read-only source audits, validation, and the daily foundation-coverage audit.
- scripts `09`, `09a`, `10`, `10a`, and `10b` are Consumer Defensive entry points. The optional cache rematch utility is owned by the neutral `market_positioning` package and writes only the explicit target passed to it.
- source birthdates, age limits, required coverage thresholds, and exact allowed upstream source names are configuration contracts. Missing pre-birth or unavailable data remain null.

Exit gate:

- source birthdates are explicit;
- missing-era data are null rather than zero;
- current coverage and staleness meet policy;
- one PIT source row exists for each eligible ticker/date where the source should exist;
- the foundation-coverage audit reports daily cohort breadth, source availability, identifier gaps, and terminal-event gaps from January 2, 2019; and
- the continuation review decides whether to proceed to Stage 6A, implement a limited shadow candidate, or defer. This checkpoint does not certify the final feature panel.

Deployed acceptance evidence at `2026-08-11` (18/18 validator checks PASS):

- reviewed identity: Fresh Del Monte Produce is public `FDP`/CIK `1047340` and reviewed Norgate `DMC`/asset `132283`; DMC Global/`BOOM` is out of scope;
- Section 16 source: 95/95 applicable current domestic issuers, with 13 current foreign private issuers explicitly not applicable;
- eligible Form 4 P/S facts: 14,246 observations / 104 transaction tickers; the gate is 95/95 applicable domestic issuers, with 13 current foreign private issuers explicitly not applicable;
- 13F: 1,724 PIT aggregates / 118 taxonomy tickers, with 108/108 current coverage;
- FINRA short interest: 12,567 observations / 115 taxonomy tickers, with 108/108 current short-signal coverage;
- short-float ratio: 105/108 current; IMKTA, ODD, and STZ use valid days-to-cover while retaining null unsafe ratios;
- borrow: zero observations, explicitly optional;
- required positioning features: 108/108 current names complete at the configured `100%` threshold.

The rollout used a fresh chronological replay, transactional backups, a full production-copy rehearsal, and a network-forbidden neutral cache rematch before production mutation. Production Stage 2 contains 108 current plus 11 historical securities and exactly 119 membership intervals; Stage 3 qualifies 121/121 required series and writes 108/108 full-quality current features. Stage 4 passes 40/40 at `2026-08-14`; Stage 5 passes 18/18 at `2026-08-11`. The foundation audit passes with `proceed_stage6a` and an earliest potential common-feature date of `2026-05-28`. The later Stage 5 cutoff was not used because retained FDP FINRA evidence is 48 days old at `2026-08-14`, beyond the strict 45-day limit.

### Stage 6A - Core Scoring Feature Contract

Following Technology Stage 6A, build the stable scoring-table contract from Consumer Defensive-owned market, financial, and positioning features. Reserve cohort-specialized component rows without changing the table shape later.

Exit gate:

- one scoring input row per eligible active ticker;
- component variance and coverage tests pass;
- core components contain no unavailable-data placeholders disguised as observations;
- reserved specialized components are explicitly `not_loaded` and zero-weight until Stage 6B;
- no required core component is constant or entirely neutral; and
- rank-ready failures have explicit reasons.

### Stage 6B - Specialized Cohort Overlays And Dedicated Parser

The dedicated parser is deliberately sequenced after Stage 5 and Stage 6A. The Stage 4 census is an inexpensive disclosure-feasibility map, while the Stage 6B parser is a controlled numeric-evidence system. Implementing the parser first would force metric/unit/scope policies and downstream mappings before the common scoring contract and historical-readiness results are known.

The already implemented shared-kernel Consumer Defensive intake controls are a Stage 4/Stage 6B boundary, not early metric implementation. They ensure that future Stage 6B work cannot parse a stale reconciliation, mutable cache alias, wrong issuer scope, mismatched direct-document list, or escaped path. Consumer Defensive production promotion is hard-disabled until the sector adapter, reviewed policy, historical document inventory, shadow evidence, golden corpus, and explicit promotion gates exist.

Within Stage 6B, use this order: freeze metric policies; implement `consumer_defensive/adapters/dedicated_parser_adapter.py`; build the complete PIT filing/document inventory back to `2019-01-02` and reconcile each required date to an exact Stage 4 seal; audit cache completeness; run shadow extraction; compare census-versus-parser outcomes; create and pass a reviewed Consumer Defensive golden corpus; then implement numeric specialized observations and overlays for viable metrics. Technology, Machinery, or other sector adapters may be inspected as contract examples but may not be imported or reused as Consumer Defensive metric logic.

Parser reconciliation must report four distinct outcomes: census hit/parser confirmed, census hit/parser rejected, census miss/parser discovered, and document/parser unavailable. The reconciliation improves census terminology and selects parser work; it does not turn a census phrase hit into an accepted observation.

A limited manual review of stratified census hits and misses is allowed at Stage 4 closure to catch obvious search-term defects. That review must not grow into an early Stage 6B implementation.

Run the non-mutating disclosure census first. Then load and adjudicate dedicated-parser observations, build PIT specialized features, apply only valid cohort-specific overlays to the reserved Stage 6A component rows, and revalidate the unchanged scoring contract.

Exit gate:

- disclosure availability and attrition are published by cohort, metric, and subtype;
- future availability, unit, applicability, and definition-version tests pass;
- insufficient coverage remains missing or measurement-only rather than numeric zero;
- Stage 6B does not change the Stage 6A table shape; and
- Stage 6A validation still passes after overlay application.

### Stage 6C - Final Historical Feature-Panel Readiness


After Stage 6B fixes the common and specialized feature inventory, build the exact point-in-time historical feature panel before running signal diagnostics or shared factor validation. This is the definitive historical-readiness audit because it can see the final applicability masks, parser outcomes, definition versions, units, directions, and missing-data policies.

The Stage 6C manifest must freeze:

- every included and excluded feature ID;
- common versus specialized status and production/measurement-only status;
- cohort/subtype applicability;
- unit, sign direction, period, freshness, and definition version;
- SEC accepted-at and source-birthdate rules;
- membership, delisting, and terminal-event eligibility;
- missing, not-applicable, parser-unavailable, and structurally excluded semantics; and
- input/config/parser/adapter hashes.

It publishes the historical research panel, daily and cohort breadth, feature coverage and attrition, earliest reproducible date, deterministic hashes, and explicit gaps. It does not yet generate final dated rank files. Those require the later scoring, publishing, Portfolio Layer, and orchestration contracts.

Signal diagnostics and `factor_validation` must read this frozen panel or a deterministic view of it. They may not independently reconstruct a different feature universe.


### Signal Diagnostics And Shared Factor Validation

As in Technology, diagnostics run after the Stage 6 feature contract and before Stage 7 calibrated scoring. Build the PIT signal panel, reconcile local rank IC with the shared `factor_validation` kernel through the Consumer Defensive adapter, register complete factor families, publish evidence, and verify the ledger.

Acceptance tests:

- local/shared per-date IC reconciliation is within tolerance;
- minimum cross-section and independent-window outcomes are published, with any failure recorded as a promotion gap rather than hidden;
- evidence packages and campaign ledger verify;
- accepted statistical evidence is clearly separated from sector promotion; and
- no production score or portfolio artifact is modified.

### Stage 7 - Calibrated Consumer Defensive Scoring

Produce the versioned baseline ranking layer from the validated Stage 6A/6B feature contract. The reviewed initial weights may be conservative; subsequent Stage 8 research cannot change them automatically.

Exit gate:

- unknown component or subfeature weights fail fast;
- every eligible current ticker has a score or explicit review/demotion reason;
- shadow status forces all portfolio gates off without neutralizing the research scores; and
- Stage 7 does not overwrite Stage 6 feature rows.

### Stage 8 - Constrained Calibration Research

Following Technology Stage 8, run report-only constrained calibration, embargoed holdout testing, shared factor-validation acceptance, cohort-concentration checks, and walk-forward refits. Stage 8 cannot modify Stage 7 weights automatically.

Exit gate:

- PIT membership is enforced on every date;
- all eligible delisted names use reconciled terminal total returns;
- unresolved historical names are reported but excluded;
- candidate weights obey component and cohort caps;
- holdout and fold results are published; failures prevent promotion but may support continued shadow monitoring;
- trial, candidate-weight, factor-evidence, walk-forward, and provenance artifacts are complete; and
- promotion remains a reviewed lockbox decision.

### Stage 9 - Portfolio Backtest

Convert the same PIT research panel used by Stage 8 into report-only portfolio simulations for the Stage 7 baseline and each registered Stage 8 candidate.

Exit gate:

- long-only, long-short, equal-weight, score-weight, and XLP-relative variants are reported;
- turnover, transaction costs, borrow costs where available, drawdown, capacity, and cohort concentration are reported;
- PIT membership and reconciled terminal returns are enforced; and
- Stage 9 writes reports only and never promotes weights.

### Stage 10 - Dashboard And Static Reports

Publish the final rank table, company scorecards, cohort summary, risk flags, review queue, specialized-overlay coverage, and Stage 9 summary from the current Stage 7 scoring layer.

Exit gate:

- every current security has a deterministic score/review status;
- promoted rows require valid OOS provenance, while shadow rows remain non-investable;
- final-rank and Stage 11 sidecar schemas validate;
- latest and dated snapshot paths are both published; and
- dashboard publishing does not change model scores or source data.

### Stage 10B - Governance Lockbox And Signal Registry

Publish the signal registry, evidence ledger, model lockbox, artifact hashes, promotion state, qualification gaps, and governance manifest without changing source data, scores, or weights.

Exit gate:

- every nonzero signal maps to registered evidence and a reviewed Stage 7 weight;
- measurement-only, zero-weight, research-candidate, and production-locked signals are distinct;
- the Stage 8 decision, walk-forward verdict, and Stage 9 reference results are recorded; and
- shadow, promoted, and deferred states cannot be confused.

### Stage 11 - Portfolio Layer Adapter And End-To-End Tests

Implement the Portfolio Layer adapter and all config changes in a disabled test fixture first. Run Stage 1 collection, cross-sector score calibration, score-contract validation, risk-panel construction, and optimizer smoke tests against fixture outputs.

Exit gate:

- adapter imports no Consumer Defensive code and opens no Consumer Defensive DB;
- missing required final-rank fields fail closed;
- sidecar-only historical rows remain non-investable but can be research-eligible;
- stale or OOS-invalid current rows cannot become investable;
- cross-sector ticker overlaps resolve through canonical ownership; and
- the full Portfolio Layer smoke run passes.

### Stage 12 - Refresh Orchestration

Implement the Consumer Defensive refresh runner after the production sequence and publishers exist. As in Technology, its explicit step table is the authoritative execution order even when script numbers do not match stage labels.

Exit gate:

- `--asof`, `--dry-run`, `--skip-network`, `--include-research`, bounded step selection, resume/repair, and final-audit modes are tested;
- the default run excludes Stage 8 searches, Stage 9 backtests, and one-time historical imports;
- the runner stops on first failure by default;
- manifests and per-step logs are published; and
- promoted and shadow registry profiles use the same independent `db_group: consumer_defensive` with different governance settings.

### Post-Stage 12 - Historical Dashboard And Stage 11 Backfill

Following Technology scripts 18 and 19, generate restartable daily dated snapshots only after the complete local scoring and publishing path exists:

1. derive the NYSE trading calendar beginning January 2, 2019;
2. rebuild PIT market, financial, positioning, Stage 6, and Stage 7 rows for each date from locally loaded history;
3. publish and validate the dated final-rank table and Stage 11 survivorship sidecar;
4. write a per-date manifest, failure ledger, and restartable chunk status;
5. restore the current latest dashboard after historical work; and
6. archive Portfolio Layer snapshots only on dates satisfying its enabled-sector coverage contract.

Deep historical replays are reconstructed PIT calibration files and must not be mislabeled strict OOS. `oos_score_valid_flag=1` begins only when the configured model lock and contemporaneous-capture rules permit it.

## 10. Final-Rank Publishing Contract

The dated final-rank CSV must include at least:

```text
asof_date
ticker
company_name
sector
industry
industry_aggregate
calibration_cohort
final_score
final_rank
rank_ready_flag
model_status
promotion_state
score_confidence
score_model_version
model_version
scoring_contract_version
portfolio_candidate_gate
portfolio_candidate_score
portfolio_candidate_status
portfolio_candidate_reason
calibration_eligible_flag
research_calibration_input_eligible_flag
research_calibration_reason
calibration_sample_role
stage11_calibration_panel_source
stage11_calibration_input_eligible_flag
stage11_calibration_input_reason
survivorship_corrected_panel_flag
oos_score_valid_flag
oos_score_asof_date
oos_invalid_reason
calibration_lock_date
market_cap
avg_dollar_volume_60d
valuation_score
quality_score
durable_growth_score
operating_resilience_score
market_behavior_score
positioning_score
specialized_operating_metrics_score
```

The Stage 11 sidecar uses the same score fields plus exact historical sample roles and terminal-event completeness. Historical sidecar rows must always have `portfolio_candidate_gate=0` and cannot be live-investable.

## 11. Dedicated Portfolio Layer Adapter

Add `_adapt_consumer_defensive` and register adapter name `consumer_defensive` in `portfolio_layer/scores/adapters.py`.

The adapter must:

- validate a Consumer Defensive-specific required-column set before reading values;
- use `portfolio_candidate_gate`, `portfolio_candidate_status`, and `oos_score_valid_flag` as authoritative;
- require `require_oos_score_valid: true`;
- reject non-finite scores;
- preserve score confidence and source as-of date;
- merge only Stage 11 sidecar rows explicitly marked calibration-input eligible;
- force all sidecar-only historical rows to non-investable roles;
- map the internal sector to canonical `Consumer Staples`; and
- return `CanonicalScore` rows without importing the sector package or opening its DB.

Planned Portfolio Layer configuration after promotion:

```yaml
- model_family: consumer_defensive
  adapter: consumer_defensive
  enabled: true
  required: true
  staleness_tolerance_days: 3
  sector: "Consumer Staples"
  industry: "Consumer Defensive"
  industry_aggregate: "Consumer Staples"
  file_mode: dated
  file_path: "consumer_defensive/dashboard/{yyyy-mm-dd}/consumer_defensive_final_rank_table.csv"
  require_oos_score_valid: true
  calibration:
    neutral: "median"
    scale: 50.0
    expected_alpha_at_full: <approved_oos_value>
```

Required Portfolio Layer integration changes:

- add `consumer_defensive: XLP` to `risk_panel.sector_etf_map`;
- add `consumer_defensive: XLP` to Stage 7 sector-factor ETF maps;
- add the Consumer Defensive DB as a read-only adjusted-price fallback if Portfolio Layer needs local prices;
- add canonical macro taxonomy for Consumer Staples;
- add Consumer Defensive valuation-method allowlists;
- add Consumer Defensive pillars to component-IC research configuration;
- add a reviewed `optimizer.sector_weight_caps.consumer_defensive` value greater than zero;
- update cross-sector minimum-success counts;
- add canonical-pipeline overrides for any cross-sector overlap; and
- add collection, contract, risk, optimizer, and end-to-end tests.

For a **promoted** state, the live cap must be decided from validated risk/capacity work and must be nonzero. A **shadow-monitor** state intentionally has zero capital and either keeps the Portfolio Layer sector disabled or permits calibration-only collection with every row non-investable.

## 12. Orchestration Integration

The registry entry is added in Stage 12 and reflects the promoted or shadow-monitor state recorded by Stage 10B governance. Both use an independent DB lane. The promoted profile follows the Technology operational contract:

```yaml
group_order:
  consumer_defensive: [consumer_defensive]

sectors:
  - name: consumer_defensive
    db_group: consumer_defensive
    dependency_tier: 0
    required: true
    network: true
    entry_script: consumer_defensive/scripts/25_run_consumer_defensive_refresh_pipeline.py
    date_flag: "--asof"
    args_template: ["--asof", "{date}"]
    publish_glob: "output/consumer_defensive/dashboard/{date}/consumer_defensive_final_rank_table.csv"
    publish_date_format: "%Y-%m-%d"
    oos_column: "oos_score_valid_flag"
    require_oos_valid: true
    staleness_tolerance_days: 3
    health:
      manifest: "output/consumer_defensive/orchestration/consumer_defensive_refresh_manifest.json"
      status_keys: ["status"]
```

The shadow-monitor profile changes the governance fields while keeping the same independent runner:

~~~yaml
    required: false
    require_oos_valid: false
    promotion_state: shadow_monitor
~~~

If valid OOS scores already exist, a shadow lane may set `require_oos_valid: true`; this still does not make it promoted. Its manifest and rank output must retain `portfolio_candidate_gate=0`, and it cannot count toward required-sector health.

The implemented registry arguments must be verified against the actual runner's `parse_args()` rather than copied blindly from this proposal.

Backfill must publish daily dated rank tables and Stage 11 survivorship sidecars. Research calibration searches and governance actions that cannot honor `--asof` remain outside per-date catch-up runs.

## 13. Promotion Assessment And Monitoring States

The post-Stage-5 foundation review answers whether the remaining implementation appears worth the cost; it does not guarantee promotion. After implementation, promotion review considers:

- statistically usable contemporaneous breadth under the registered sector-wide, cohort, or hierarchical calibration design;
- active-universe, taxonomy, market-data, and corporate-action quality;
- configured survivorship and terminal-return coverage;
- common and specialized feature coverage, including explicit applicability and attrition;
- accepted evidence for every nonzero signal;
- embargoed holdout, walk-forward, transaction-cost, risk, turnover, and concentration results;
- valid final-rank and Stage 11 sidecar contracts;
- observed refresh reliability;
- dedicated Portfolio Layer adapter and end-to-end tests; and
- an approved nonzero cap and rollback plan.

The decision record must publish:

~~~text
promotion_state: promoted | shadow_monitor | deferred
qualification_gaps
last_evaluated_date
next_review_date
oos_evidence_status
eligible_cross_section_summary
specialized_metric_coverage
portfolio_cap
~~~

A sector can remain a shadow monitor when the pipeline is useful but promotion evidence is incomplete. Shadow status is not a failed implementation and has no forced deadline. It exists to accumulate honest forward evidence and is reviewed periodically. Promoted status additionally requires `required: true`, `require_oos_valid: true`, a nonzero cap, and explicit authorization.

## 14. Minimum Test Matrix

### Package And Database

- scratch DB initialization and idempotent migrations;
- config rejects unknown keys and cross-sector paths;
- nested config typos and duplicated config/policy contract drift fail closed;
- authoritative-input manifest success, hash tamper, record-count drift, and unlisted-inventory tests;
- foreign-identity and nonempty-unowned database rejection without mutation;
- Stage 4 query-plan regression covers exact raw-fact deletion, canonical foreign-key child lookup, legacy-index migration, and idempotent bootstrap;
- Stage 4 migration-ledger tests cover ordering, immutable checksums, bounded backfills, rollback, parity, foreign keys, and non-destructive legacy quarantine/retirement;
- SEC chronology tests prove reverse-time requests fail before cache/provider/database mutation and watermark changes are transactional;
- exact config/scope reconciliation, append-only lifecycle hashes, source-observation identities, current-scope invalidation, and shared-accession bridges fail closed;
- immutable-seal tests cover global-CAS last-good preservation, no alias hardlinks, exact manifest hashes/sizes, canonical seal paths, nested SEC names, traversal/reserved-name rejection, and symlink containment;
- AST import test blocks `technology.*` and `industrials.*` imports;
- all writes remain inside Consumer Defensive-owned DB/output paths.

### Universe And Survivorship

- current cohort counts and minimum-size gates;
- Norgate membership fingerprint drift publishes neither database rows nor reports;
- issuer/listing country and ADR tests;
- duplicate share-class and ticker-collision rejection;
- PIT start/end interval validation;
- successor-chain and terminal-event tests;
- active versus historical isolation.

### Parser And Metrics

- financial semantic tests cover reporting context, accounting-identity revenue, capex sign, safe TTM, average balances, ratio compatibility, and PIT FX quarantine/exemptions;
- financial pipeline tests preserve canonical and feature lineage, definition versions, listing-date eligibility, stale-input rejection, and foreign-issuer contexts;
- Yahoo/SEC/FX cache-only no-network behavior, missing-cache failures, malformed-cache repair policy, atomic cache replacement, exact sealed replay, and deterministic payload/cache manifests;
- disclosure census reads only exact sealed snapshot bytes and fails on stale config, issuer scope, lifecycle, reconciliation, or mutable-alias drift;
- Consumer Defensive parser catalog and direct-input tests require independently expected config identity, exact PIT scope/document sets, immutable sealed paths, and hard-disabled production promotion;
- registry/applicability completeness;
- XBRL/table/prose channel policy;
- unit and plausibility checks;
- amendment and definition-version behavior;
- future-availability rejection;
- missing versus not-applicable behavior;
- measurement-only signals remain zero-weight.

### Factor Validation And Calibration

- local/shared per-date IC reconciliation;
- FDR family completeness and tamper verification;
- minimum cross-section behavior;
- regime and chronological-half diagnostics;
- embargo and walk-forward leakage tests;
- deterministic seeds and artifact hashes.

### Portfolio Layer And Orchestration

- adapter missing-column failure;
- OOS-invalid and stale-row demotion;
- Stage 11 sidecar calibration-only handling;
- current investable rows map to canonical Consumer Staples;
- canonical overlap resolution;
- Stage 1/2/3 Portfolio Layer smoke run;
- registry path and argument validation;
- independent DB-group scheduling;
- dated publish and health-manifest verification;
- promoted and shadow profiles cannot be confused: shadow is optional, zero-cap, and always non-investable; promoted is required, OOS-valid, and explicitly capitalized.

## 15. Recommended First Work Slice

Following Technology, the first work slice covers Stages 0-2:

1. freeze the independent package, path, source, taxonomy, benchmark, and January 2, 2019 history contracts;
2. initialize the scratch Consumer Defensive database and source registry;
3. create canonical security, issuer, identifier, membership, raw-response, parser, and data-quality tables;
4. load the reviewed current universe;
5. load collision-safe historical/delisted securities, aliases, lineages, and exact membership intervals; and
6. run database, import-boundary, universe, and PIT-membership validation.

The second slice is Technology Stage 3: load the complete market/corporate-action foundation, including the configured warm-up before January 2, 2019. The third slice is Stages 4-5: load SEC/FX and ownership/positioning history and then run the Stage 5 foundation-coverage audit. That post-load audit—not an unloaded preflight—determines whether to continue into Stage 6A and the specialized parser. The definitive historical feature-panel readiness audit remains Stage 6C work.

### Current ordered checkpoint

Stages 0-5 and terminal-event reconciliation are implemented in production. The FDP-corrected chronological replay, transactionally consistent two-database backup, production-copy rehearsal, and approved deployment are complete. Production Stage 4 passes 40/40 at `2026-08-14` with 119/119 reporting profiles covered, zero stale lifecycle outputs, zero required FX gaps, zero lineage mismatches, and clean foreign keys. Production Stage 5 passes 18/18 at `2026-08-11` with current 13F, short-interest, and numeric positioning coverage all 108/108; Section 16 coverage is 95/95 applicable issuers. The foundation audit records `proceed_stage6a`. The remaining work must proceed in this order:

1. implement and validate Stage 6A common scoring inputs with specialized rows reserved and zero-weight;
2. implement Stage 6B's Consumer Defensive parser adapter, complete historical document inventory, shadow reconciliation, reviewed golden corpus, and viable specialized overlays;
3. build and validate the definitive Stage 6C PIT historical feature panel from `2019-01-02`, then run signal diagnostics and the dedicated `factor_validation` adapter before Stage 7 calibration; and
4. continue through Stages 7-12 only in the documented dependency order.

Therefore, parser validation has priority over the definitive historical-readiness audit, and that audit has priority over signal diagnostics and factor validation. The earlier Stage 5 checkpoint is foundation coverage only.
