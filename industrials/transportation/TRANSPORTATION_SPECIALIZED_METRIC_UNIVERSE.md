# Transportation Specialized Metric Discovery Universe

Status: DP0 discovery contract implemented and hash-sealed; not enabled in the
production registry, parser, feature panel, scoring, or calibration.

## 1. Decision

Start the dedicated-parser integration with **90 specialized final metrics**:

| Metric pack | Final metrics | Direct parser targets | Parser-derived metrics | Financial-derived metrics |
| --- | ---: | ---: | ---: | ---: |
| Surface freight and logistics | 25 | 24 | 1 | 0 |
| Air transport and aviation services | 30 | 26 | 4 | 0 |
| Marine shipping and maritime | 17 | 16 | 1 | 0 |
| Development-stage overlay | 18 | 11 | 1 | 6 |
| **Total** | **90** | **77** | **7** | **6** |

This is an intentionally extended **discovery universe**, not a commitment to
put all 90 metrics into the final score. The expected production calibration
set is a smaller subset selected only after the one-pass evidence run,
adjudication, and historical coverage analysis.

The current v2 registry has 21 specialized metrics. The 90-metric proposal
replaces ambiguous composites, adds the major operating drivers for each
transportation archetype, and preserves the six metrics that can be calculated
without searching filing prose.

The 90 final outputs are not the complete parser search vocabulary. Seven
non-scoring supporting operands are also frozen because five parser-derived
outputs cannot be calculated from the 77 final direct metrics alone:

- `airline_fuel_consumed`
- `airline_capacity_units`
- `airline_fuel_expense`
- `airport_aeronautical_revenue`
- `airport_non_aeronautical_revenue`
- `airport_passenger_throughput`
- `milestone_target_date`

The one-pass parser search contract is therefore **84 metrics: 77 final direct
targets plus seven supporting operands**. Supporting operands are retained for
derivation and audit only; they never become scoring columns.

## 2. One-Pass Efficiency Contract

The expensive unit of work is the sealed filing/document corpus, not an
individual final ratio. The parser will therefore:

1. scan each sealed document once;
2. search for all 77 direct disclosure targets and seven frozen supporting
   operands during that pass;
3. preserve the accepted raw evidence and its units;
4. calculate seven parser-derived metrics from accepted parser evidence without
   reopening a document;
5. calculate six development-risk metrics from already-loaded financial facts;
6. materialize one complete 90-metric research panel;
7. reduce the calibration list by predeclared quality and coverage gates; and
8. run one final walk-forward calibration on the frozen surviving subset.

No second document search is required when a metric is retained, downgraded to
diagnostic-only, or removed from calibration. A new metric that requires new
evidence after the corpus run is a new parse scope and is prohibited unless the
entire contract is deliberately versioned and reauthorized.

Source-lane labels used below:

- `DP`: direct dedicated-parser target. The adapter may accept an explicitly
  disclosed value or its disclosed numerator and denominator in the same pass.
- `DP-S`: non-scoring supporting parser evidence needed by a `DP-D` formula.
  It is searched in the same document pass and is never published as a final
  metric.
- `DP-D`: calculated only from accepted `DP` outputs. It creates no new filing
  search. Where the formula needs a raw operand rather than a final `DP`
  metric, the operand is frozen as `DP-S`.
- `FIN-D`: calculated from the already-loaded point-in-time financial facts. It
  is included in the specialized panel but is not a prose-search target.

Scoring-posture labels:

- `+`: higher is generally favorable inside the stated comparison population.
- `-`: lower is generally favorable inside the stated comparison population.
- `CTX`: economically useful but not assigned a universal direction before
  calibration. It may be a control, regime variable, or diagnostic.

## 3. Applicability Model

The existing four calibration cohorts remain authoritative:

- `surface_freight_and_logistics`
- `air_transport_and_aviation_services`
- `marine_shipping_and_maritime`
- `development_stage_and_speculative_transport`

The broad `industry` field is not precise enough to control specialized metric
applicability. For example, airport operators, air-mobility developers,
aircraft lessors, and aviation-maintenance providers can share an industry
label while reporting completely different operating statistics.

Before parsing, each of the 160 active-plus-inactive identities must therefore
receive one primary `operating_archetype`:

- `surface_rail_operator`
- `surface_rail_equipment`
- `surface_trucking`
- `surface_logistics`
- `surface_asset_leasing`
- `passenger_airline`
- `cargo_airline`
- `airport_operator`
- `aircraft_lessor`
- `aviation_services`
- `marine_operator`
- `marine_services`
- `precommercial_transport`

The development-stage cohort is an **overlay**, not a substitute for an
operating pack:

- a development-stage marine issuer receives the marine pack when it operates
  vessels, plus the applicable development-risk metrics;
- a development-stage logistics issuer receives the surface pack when it has
  operating volumes, plus the development-risk metrics;
- a precommercial air-mobility issuer receives the development overlay and
  only those air metrics supported by actual commercial operations.

Every nonapplicable ticker-metric pair must be emitted as
`NOT_APPLICABLE`. It must never be treated as missing or zero.

## 4. Surface Freight and Logistics Pack - 25 Metrics

The current 5-metric composite pack is expanded to distinguish railroad,
trucking, logistics, and surface-asset-leasing economics.

| # | Metric ID | Lane | Applicability | Unit | Posture | Economic meaning and normalization |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | `surface_volume_growth` | DP-D | All operating surface archetypes | ratio | + | One within-issuer growth series selected from the most representative accepted native volume metric. Never averages carloads, shipments, loads, and ton-miles together. |
| 2 | `rail_carload_growth` | DP | `surface_rail_operator` | ratio | + | Year-over-year growth in total carloads or explicitly comparable carload units. |
| 3 | `rail_intermodal_volume_growth` | DP | `surface_rail_operator`, applicable intermodal logistics | ratio | + | Growth in intermodal units or shipments; kept separate from carloads. |
| 4 | `revenue_ton_miles_growth` | DP | `surface_rail_operator`, reporting freight operators | ratio | + | Growth in revenue ton-miles or an explicitly equivalent freight-work measure. |
| 5 | `shipment_or_load_growth` | DP | `surface_trucking`, `surface_logistics` | ratio | + | Growth in shipments or loads. The underlying unit is stored and comparisons remain within the same unit/archetype. |
| 6 | `pricing_or_yield_growth` | DP | All operating surface archetypes | ratio | + | Change in price, yield, or revenue per comparable transportation unit, excluding a pure fuel-surcharge effect when disclosed. |
| 7 | `revenue_per_shipment_or_load` | DP | `surface_trucking`, `surface_logistics` | currency per unit | + | Revenue per shipment or load, with currency and unit retained; cross-sectional comparisons require matching units. |
| 8 | `revenue_per_tractor_or_power_unit` | DP | Asset-based `surface_trucking` | currency per asset-period | + | Revenue productivity per tractor or power unit for a stated week, month, quarter, or year. |
| 9 | `operating_ratio` | DP | `surface_rail_operator`, `surface_trucking` when reported | ratio | - | Operating expense divided by operating revenue under the issuer's stated definition; adjusted and GAAP variants remain distinct. |
| 10 | `purchased_transportation_ratio` | DP | `surface_trucking`, `surface_logistics` | ratio | CTX | Purchased transportation expense divided by revenue. Direction is not forced because asset-light and asset-heavy models differ structurally. |
| 11 | `fuel_surcharge_revenue_ratio` | DP | Surface issuers with separately disclosed fuel surcharge revenue | ratio | CTX | Fuel surcharge revenue divided by revenue; used to separate price realization from fuel pass-through. |
| 12 | `empty_mile_ratio` | DP | Asset-based `surface_trucking` | ratio | - | Empty miles divided by total miles under a consistent issuer definition. |
| 13 | `equipment_utilization` | DP | `surface_trucking`, `surface_asset_leasing`, applicable rail equipment | ratio or days | + | Utilized equipment divided by available equipment, or a clearly identified utilization-days measure. Units may not be mixed. |
| 14 | `fleet_or_equipment_count` | DP | Asset-based surface archetypes | count | CTX | Tractors, trailers, railcars, locomotives, or leased units. Used for within-issuer capacity change, not raw cross-archetype ranking. |
| 15 | `service_reliability_rate` | DP | Surface issuers with a consistent service KPI | ratio | + | On-time, service-standard, or delivery-reliability rate. Only the same named definition is linked through time. |
| 16 | `rail_network_velocity` | DP | `surface_rail_operator` | miles per day, mph, or issuer-native velocity unit | + | Train or network velocity under a consistent definition. Different velocity units and train classes are not pooled. |
| 17 | `terminal_dwell_time` | DP | `surface_rail_operator` | hours | - | Average terminal dwell time for a consistently defined network or terminal population. |
| 18 | `freight_weight_per_shipment` | DP | LTL and reporting `surface_logistics` issuers | weight per shipment | CTX | Average shipment weight. It controls mix and yield interpretation rather than receiving a universal direction. |
| 19 | `average_length_of_haul` | DP | `surface_trucking`, reporting rail/logistics issuers | distance | CTX | Average distance per shipment or load with mode and unit retained. |
| 20 | `driver_turnover_rate` | DP | Asset-based `surface_trucking` | ratio | - | Annualized or period driver turnover under the issuer's stated employee population. Voluntary, involuntary, company-driver, and contractor scopes remain distinct. |
| 21 | `surface_lease_yield` | DP | `surface_asset_leasing`, applicable `surface_rail_equipment` | ratio | + | Lease rent or lease revenue relative to the stated asset base under a consistent fleet definition. |
| 22 | `surface_asset_age` | DP | `surface_asset_leasing`, `surface_rail_equipment`, applicable asset-heavy surface issuers | years | - | Average age of the leased or operating fleet, with asset class and owned/managed scope retained. |
| 23 | `rail_fuel_efficiency` | DP | `surface_rail_operator` | fuel volume per gross-ton-mile | - | Fuel consumed per thousand gross ton-miles or an explicitly comparable rail-efficiency unit. |
| 24 | `insurance_claims_cost_ratio` | DP | `surface_trucking`, applicable logistics issuers | ratio | - | Insurance and claims expense divided by revenue, or a consistently reported claims-frequency/severity ratio. Ratio subtypes remain separate populations. |
| 25 | `logistics_net_revenue_margin` | DP | `surface_logistics` | ratio | + | Net revenue or gross profit divided by gross revenue under a consistent brokerage/forwarding definition. |

## 5. Air Transport and Aviation Services Pack - 30 Metrics

The current air composites are split into airline, airport, aircraft-leasing,
and aviation-services metrics. Passenger load factor is never pooled with
lessor fleet utilization, and passenger yield is never pooled with lease-rate
economics.

| # | Metric ID | Lane | Applicability | Unit | Posture | Economic meaning and normalization |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | `traffic_growth` | DP | `passenger_airline`, `cargo_airline` | ratio | + | Growth in RPM/RPK or RTM under the issuer's comparable traffic unit; passenger and cargo populations remain separate. |
| 2 | `capacity_growth` | DP | `passenger_airline`, `cargo_airline` | ratio | CTX | Growth in ASM/ASK, ATM, or a comparable capacity unit. Expansion is not assumed favorable without demand and unit-revenue context. |
| 3 | `passenger_load_factor` | DP | `passenger_airline` | ratio | + | RPM/ASM or RPK/ASK, or the directly reported equivalent. Prohibited for cargo-airline and lessor utilization. |
| 4 | `passenger_yield` | DP | `passenger_airline` | currency per passenger-distance | + | Passenger revenue per RPM/RPK or the directly reported passenger-yield definition. |
| 5 | `passenger_revenue_per_capacity_unit` | DP | `passenger_airline` | currency per ASM/ASK | + | PRASM or RASK; passenger-only and total-revenue definitions remain separate. |
| 6 | `total_revenue_per_capacity_unit` | DP | `passenger_airline` | currency per ASM/ASK | + | TRASM or total RASK under the issuer's stated definition. |
| 7 | `unit_cost` | DP | `passenger_airline` | currency per ASM/ASK | - | CASM or CASK under the reported definition. |
| 8 | `unit_cost_ex_fuel` | DP | `passenger_airline` | currency per ASM/ASK | - | CASM/CASK excluding fuel and any other explicitly stated adjustments; adjustment scope is preserved. |
| 9 | `fuel_price_per_gallon` | DP | `passenger_airline` | currency per gallon | - | Average economic or consumed fuel price per gallon. Hedging treatment is retained. |
| 10 | `fuel_efficiency_per_capacity_unit` | DP-D | `passenger_airline` | fuel volume per ASM/ASK | - | Fuel consumed divided by capacity, calculated from accepted values in matching periods. |
| 11 | `fuel_cost_per_capacity_unit` | DP-D | `passenger_airline` | currency per ASM/ASK | - | Fuel expense per capacity unit, calculated directly or from compatible unit-cost disclosures. |
| 12 | `aircraft_utilization_hours` | DP | `passenger_airline`, `cargo_airline`, applicable `aircraft_lessor` | hours per aircraft-day | + | Average block hours or utilization hours per aircraft-day. Passenger, cargo, and lessor populations remain separate. |
| 13 | `ancillary_revenue_per_passenger` | DP | `passenger_airline` | currency per passenger | + | Ancillary revenue divided by passengers, using a consistent issuer definition. |
| 14 | `passenger_throughput_growth` | DP | `airport_operator` | ratio | + | Growth in terminal passengers, segmented domestic/international when available. |
| 15 | `cargo_throughput_growth` | DP | `airport_operator` | ratio | + | Growth in cargo tonnage or another explicitly comparable airport-cargo unit. |
| 16 | `aircraft_movements_growth` | DP | `airport_operator` | ratio | + | Growth in takeoffs, landings, or total aircraft movements. |
| 17 | `aeronautical_revenue_per_passenger` | DP-D | `airport_operator` | currency per passenger | + | Aeronautical revenue divided by accepted passenger throughput for the same scope and period. |
| 18 | `non_aeronautical_revenue_per_passenger` | DP-D | `airport_operator` | currency per passenger | + | Commercial/non-aeronautical revenue divided by accepted passenger throughput for the same scope and period. |
| 19 | `lease_utilization` | DP | `aircraft_lessor` | ratio | + | Leased or revenue-generating aircraft divided by available owned aircraft. |
| 20 | `lease_rate_factor` | DP | `aircraft_lessor` | ratio | + | Monthly lease rent divided by aircraft value, or the issuer's consistently reported lease-rate factor. |
| 21 | `lease_collection_rate` | DP | `aircraft_lessor` | ratio | + | Cash lease collections divided by contractually due rent for the disclosed period. |
| 22 | `weighted_average_lease_term_remaining` | DP | `aircraft_lessor` | years | CTX | Weighted remaining lease term. It is a cash-flow-duration control, not automatically a positive signal. |
| 23 | `owned_or_managed_aircraft_count` | DP | `aircraft_lessor`, applicable `aviation_services` | count | CTX | Owned, managed, or serviced aircraft, with ownership category preserved. Used primarily for within-issuer growth. |
| 24 | `aircraft_fleet_age` | DP | `passenger_airline`, `cargo_airline`, `aircraft_lessor` | years | - | Weighted or simple average aircraft age. Method and owned/leased scope are retained. |
| 25 | `maintenance_service_event_growth` | DP | `aviation_services` | ratio | + | Growth in shop visits, overhauls, engine events, flight-hour events, or another consistent service-volume KPI. |
| 26 | `aviation_maintenance_intensity` | DP | `passenger_airline`, `cargo_airline`, `aircraft_lessor`, `aviation_services` | ratio | CTX | Maintenance expense, reserve revenue, or service revenue divided by the contractually defined base. Each subtype is a separate comparison population. |
| 27 | `contracted_aviation_backlog` | DP | `aviation_services`, applicable `aircraft_lessor` | currency or service units | + | Contracted aviation-service backlog or committed lease/service value; nonbinding opportunities are prohibited. |
| 28 | `completion_factor` | DP | `passenger_airline`, `cargo_airline` | ratio | + | Completed scheduled flights divided by scheduled flights under a consistent reporting definition. |
| 29 | `on_time_performance` | DP | `passenger_airline`, `cargo_airline` | ratio | + | On-time arrival or departure rate with the timing threshold and operating scope retained. |
| 30 | `aircraft_orderbook_commitments` | DP | `passenger_airline`, `cargo_airline`, `aircraft_lessor` | aircraft count and currency | CTX | Firm aircraft orders and remaining purchase commitments with delivery windows. Options are separately labeled and excluded from firm counts. |

## 6. Marine Shipping and Maritime Pack - 17 Metrics

Capacity values retain their native segment unit. DWT, TEU, cubic meters, and
vessel count are never pooled as equivalent levels. Cross-segment use is
limited to within-issuer growth or explicitly standardized ratios.

| # | Metric ID | Lane | Applicability | Unit | Posture | Economic meaning and normalization |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | `vessel_count` | DP | `marine_operator` | count | CTX | Owned, leased, chartered-in, managed, or operating vessels with fleet category preserved. |
| 2 | `fleet_capacity` | DP | `marine_operator` | DWT, TEU, cbm, or segment-native unit | CTX | Capacity by shipping segment. Raw levels are ranked only within a matching segment and unit. |
| 3 | `fleet_capacity_growth` | DP-D | `marine_operator` | ratio | CTX | Within-issuer change in the same accepted capacity unit and fleet scope. |
| 4 | `tce_day_rate` | DP | `marine_operator` | currency per day | + | Time-charter-equivalent revenue per available or operating day, with segment and denominator preserved. |
| 5 | `spot_or_charter_day_rate` | DP | `marine_operator` | currency per day | + | Realized spot or charter rate per day. Spot, time-charter, and bareboat rates remain distinct. |
| 6 | `fleet_utilization` | DP | `marine_operator` | ratio | + | Revenue/operating days divided by available days under a consistent definition. |
| 7 | `charter_coverage_next_12m` | DP | `marine_operator` | ratio | + | Percentage of available vessel days or capacity contracted for the next 12 months as of the evidence date. |
| 8 | `contracted_revenue_backlog` | DP | `marine_operator` | currency | + | Remaining fixed or minimum contracted revenue; options and unexercised extensions are excluded unless separately identified. |
| 9 | `weighted_average_charter_term` | DP | `marine_operator` | years | CTX | Weighted remaining charter duration, retained as a duration/regime control. |
| 10 | `fleet_age` | DP | `marine_operator` | years | - | Average fleet age under an identified owned/operated scope. |
| 11 | `newbuild_capacity_commitments` | DP | `marine_operator` | count and segment-native capacity | CTX | Contracted newbuild vessels and capacity, including expected delivery windows. |
| 12 | `capex_commitments` | DP | `marine_operator` | currency | CTX | Remaining committed payments for newbuilds, acquisitions, retrofits, and major drydock programs. |
| 13 | `vessel_opex_per_day` | DP | `marine_operator` | currency per day | - | Vessel operating expense per ownership or operating day, segmented when disclosed. |
| 14 | `cash_breakeven_per_day` | DP | `marine_operator` | currency per day | - | Issuer-defined cash breakeven per vessel/day, with included costs recorded. |
| 15 | `offhire_or_drydock_ratio` | DP | `marine_operator` | ratio | - | Off-hire or drydock days divided by available days. Scheduled and unscheduled causes remain distinguishable. |
| 16 | `revenue_days` | DP | `marine_operator` | days | CTX | Revenue-generating vessel days for the reported fleet and period; used with ownership/available days to audit utilization. |
| 17 | `spot_exposure_ratio` | DP | `marine_operator` | ratio | CTX | Percentage of vessel days or capacity exposed to spot rates over a stated forward or reported period. |

## 7. Development-Stage Overlay - 18 Metrics

These metrics apply by stage and business model. Financing-risk metrics apply
to the entire development-stage cohort; commercialization metrics apply only
to genuine precommercial or ramping issuers. An operating speculative issuer
can receive its relevant surface or marine pack without being forced into a
precommercial scoring rubric.

| # | Metric ID | Lane | Applicability | Unit | Posture | Economic meaning and normalization |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | `going_concern_flag` | DP | Development-stage cohort | boolean | - | Auditor or management substantial-doubt disclosure that is active as of the filing date. Boilerplate risk-factor language is prohibited. |
| 2 | `pre_revenue_flag` | FIN-D | Development-stage cohort | boolean | - | Point-in-time flag based on absent or immaterial operating revenue under the frozen financial rule. |
| 3 | `commercialization_stage` | DP | `precommercial_transport` | ordinal 0-5 | + | Deterministic evidence-anchored stage: concept, prototype/test, certification, pilot/preproduction, initial commercial delivery, or scaled operations. |
| 4 | `regulatory_certification_stage` | DP | Regulated precommercial issuers | ordinal 0-5 | + | Progress through named certification or operating-approval milestones; generic plans do not advance the stage. |
| 5 | `test_program_progress` | DP | `precommercial_transport` | ratio or milestone count | + | Completed test milestones divided by the frozen disclosed program or an explicitly comparable completed-milestone count. |
| 6 | `binding_order_units` | DP | Precommercial/ramping issuers | count | + | Firm, binding customer orders. Options, reservations, letters of intent, and memoranda are excluded. |
| 7 | `binding_order_value` | DP | Precommercial/ramping issuers | currency | + | Contracted value of binding orders after disclosed cancellations; unit and currency lineage are retained. |
| 8 | `nonbinding_reservation_units` | DP | Precommercial/ramping issuers | count | CTX | Nonbinding reservations, indications, options, or letters of intent, explicitly labeled lower-confidence demand evidence. |
| 9 | `customer_deposits` | DP | Precommercial/ramping issuers | currency | + | Customer cash deposits tied to orders or reservations; refundable and nonrefundable balances remain distinct. |
| 10 | `units_produced` | DP | Ramping manufacturers/operators | count | + | Units completed in the stated period under a consistent definition. |
| 11 | `units_delivered` | DP | Ramping manufacturers/operators | count | + | Revenue-capable or customer-accepted deliveries, not prototypes or internal test units unless separately labeled. |
| 12 | `production_capacity` | DP | Ramping manufacturers/operators | units per period | CTX | Current demonstrated or installed production capacity. Aspirational future targets remain separate evidence. |
| 13 | `cash_runway_years` | FIN-D | Development-stage cohort with positive burn | years | + | Unrestricted cash divided by annualized trailing cash burn under the frozen point-in-time formula. |
| 14 | `quarterly_cash_burn` | FIN-D | Development-stage cohort | currency per quarter | - | Positive cash-consumption amount based on trailing operating cash flow less capital expenditures; source periods and sign convention are fixed. |
| 15 | `capital_raise_dependence` | FIN-D | Development-stage cohort | ratio | - | External capital raised relative to cash use under the existing transportation financial-fact definition. |
| 16 | `diluted_share_growth` | FIN-D | Development-stage cohort | ratio | - | Point-in-time year-over-year growth in diluted weighted-average or period-end shares under a frozen share-basis rule. |
| 17 | `stock_compensation_to_revenue` | FIN-D | Development-stage cohort | ratio | - | Stock-based compensation divided by revenue, with an explicit fallback treatment for pre-revenue observations. |
| 18 | `milestone_slippage_days` | DP-D | `precommercial_transport` | days | - | Delay in the latest accepted target date versus the prior accepted target for the same named certification, production, or commercial milestone. Cancellations and scope changes remain separate states. |

## 8. Current Composite Metrics to Replace

The v3 discovery contract must not preserve the following ambiguous v2
semantics:

| Current v2 metric | v3 treatment |
| --- | --- |
| `transport_volume_growth` | Replace with exact surface volume metrics plus the controlled `surface_volume_growth` selector. |
| `load_factor_or_utilization` | Split into `passenger_load_factor`, `lease_utilization`, and `aircraft_utilization_hours`. |
| `passenger_or_lease_yield` | Split into `passenger_yield`, airline unit-revenue metrics, and `lease_rate_factor`. |
| `fuel_or_maintenance_intensity` | Split into airline fuel price/efficiency/cost metrics and `aviation_maintenance_intensity`. |
| `fleet_capacity` | Keep only with a mandatory segment/unit dimension and add within-issuer `fleet_capacity_growth`. |
| `charter_coverage` | Replace with the explicit `charter_coverage_next_12m` horizon. |
| `commercialization_progress` | Replace with deterministic stage, certification, test, order, production, and delivery metrics. |

Legacy v2 identifiers remain immutable in the v2 panel for comparison. They
are not silently aliased into v3 when their meaning differs.

## 9. Coverage and Reduction Gates

Coverage is measured only over applicable ticker-metric pairs and separately
for active and inactive/delisted identities. Monthly carry-forward rows do not
count as new source observations.

### 9.1 Mandatory evidence-quality gate

A metric cannot enter calibration unless:

- reviewed extraction precision is at least 95%;
- unit, period, scope, and numerator/denominator contracts pass;
- direct and derived values reconcile within the metric-specific tolerance;
- no unresolved composite or mixed-unit comparison remains; and
- point-in-time availability and after-close rules pass.

Metrics with 90% to less than 95% reviewed precision can remain
diagnostic-only. Metrics below 90%, or with an unresolvable unit/domain
contract, are excluded.

### 9.2 Calibration-core coverage gate

A broad-archetype metric is a calibration candidate when it has:

- current coverage for at least the greater of 5 active issuers or 30% of the
  applicable active archetype;
- a median historical span of at least three fiscal years and at least four
  distinct accepted reporting periods among covered issuers;
- usable evidence in inactive/delisted members when such filings exist, or an
  explicit survivor-bias limitation; and
- coverage that is not concentrated in one issuer, one filing date, or one
  parser template.

### 9.3 Niche-archetype gate

An airport, aircraft-lessor, aviation-services, or other narrow metric can be
retained for archetype-only calibration when it has:

- at least 3 covered issuers and at least 25% of that exact archetype;
- the same 95% evidence-precision standard; and
- at least three fiscal years or four distinct accepted periods for the median
  covered issuer.

It cannot be pooled with a different archetype merely to raise headline
coverage.

### 9.4 Longitudinal and diagnostic dispositions

- A metric with strong longitudinal history but weak current cross-sectional
  coverage can be retained as a within-issuer change feature.
- A metric with strong economic value but insufficient calibration breadth is
  retained in the research panel as diagnostic-only.
- Nonbinding demand metrics remain diagnostic until they demonstrate
  independent signal and stable definitions.
- Raw asset and capacity levels remain diagnostic or become within-issuer
  growth metrics; they are not automatically cross-sectionally ranked.

### 9.5 Redundancy and stability gate

After the full 90-metric research panel is built once:

- retain the higher-precision/higher-coverage metric when two metrics represent
  the same economic driver and have near-duplicate histories;
- preserve economically distinct numerator, denominator, price, volume, and
  utilization signals even when correlated;
- exclude metrics whose missingness is primarily a filing-format artifact;
- do not treat `NOT_DISCLOSED` as zero; and
- freeze the surviving metric IDs, transformations, comparison populations,
  and missingness rules before the single walk-forward calibration.

## 10. Metrics Intentionally Outside This Parser Pass

The 90-metric count excludes:

- the 18 existing generic market and financial metrics already handled by the
  transportation shared infrastructure;
- external spot-price, freight-index, fuel-index, airport-traffic, and macro
  series that are not issuer filing disclosures;
- valuation multiples and market-derived NAV discounts;
- generic ESG/emissions metrics without a defined investment use;
- accident, safety, labor-event, and regulatory-event feeds that require a
  separate event-data source; and
- unrestricted qualitative sentiment or management-language scores.

These can be added later as separate data-source families without reopening
the sealed issuer-document corpus. They should not be disguised as dedicated
filing-parser metrics.

## 11. DP0 Acceptance Gates

The 90-metric discovery universe is ready to freeze only when:

1. all 160 identities have a reviewed operating archetype and development
   overlay flag;
2. the scope manifest contains exactly `160 x 90 = 14,400` rows;
3. every row has an explicit applicability status and reason;
4. every metric has a source lane, unit/domain contract, period type,
   freshness rule, bounds, comparison population, and scoring posture;
5. all composite replacements in Section 8 are fixture-tested;
6. the seven `DP-D` formulas and six `FIN-D` formulas are frozen;
7. the 77 direct parser targets have positive and prohibited examples;
8. the current v2 registry, feature panel, rank table, and portfolio outputs
   remain immutable; and
9. the metric-catalog and scope-manifest hashes are included in the sealed
   parser plan.

Only after these gates pass should the source census be sealed and the
one-pass dedicated-parser run begin.
