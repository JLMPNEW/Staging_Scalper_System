# Transportation specialized-metric semantic coverage

## Scope and execution

This record covers the final metric-specific parser and semantic-review batch for the two investable transportation operating cohorts:

- `surface_freight_core`: parser run 106, as of 2026-07-30.
- `oil_tanker_operators`: parser run 107, as of 2026-08-13.

Both parser runs used the existing complete SEC cache in one-document/many-metrics mode. Surface processed 1,594 accessions and 1,818 documents; tanker processed 429 accessions and 782 documents. Both completed with zero failed work and zero network requests. Semantic review did not reopen or reparse any source document.

## Implementation changes

- Added exact surface-freight aliases and strict extraction for paired purchased-transportation/revenue rows, signed LTL yield and shipment changes, terminal-dwell wording, and owned/operated power-unit counts.
- Required surface equipment counts to bind to the first material tractor or locomotive value, excluding footnote numbers and later trailer/container values.
- Annualized explicit weekly, monthly, quarterly, semiannual, and nine-month revenue-per-power-unit disclosures; unknown periods remain rejected.
- Added strict tanker table extraction for reported operating KPIs and auditable derivations for fleet age, revenue days, off-hire ratio, and fixed-day coverage.
- Added tanker-specific semantic queue, definition review, replay, and post-review coverage scripts (`36t` through `36w`).
- Added exact accession/document catalog binding plus physical file hashing for XBRL evidence that does not duplicate the document hash in fact provenance.
- Preserved the independent `dedicated_parser` repository: all cohort-specific parsing and policy code remains in `industrials/transportation`.

## Semantic-review result

| Cohort | Definitions | Approved | Rejected | Candidate rows | Accepted rows | Unresolved |
|---|---:|---:|---:|---:|---:|---:|
| Surface freight | 283 | 119 | 164 | 13,832 | 5,912 | 0 |
| Oil tankers | 192 | 77 | 115 | 5,765 | 1,854 | 0 |

The semantic policy rejects qualitative disclosures, filing years used as values, property dollars used as equipment counts, individual-vessel DWT used as aggregate fleet capacity, period-incomparable revenue-per-tractor observations, and percentage changes used as KPI levels.

## Accepted surface-freight metric domains

The unchanged domain gates produce nine accepted metric-domain contracts across six distinct metric IDs.

| Metric | Comparison domain | Accepted issuers / required | Median periods | Median history (years) |
|---|---|---:|---:|---:|
| `operating_ratio` | rail networks | 5 / 4 | 74 | 18.50 |
| `operating_ratio` | LTL carriers | 5 / 4 | 33 | 8.14 |
| `operating_ratio` | truckload/intermodal | 4 / 3 | 50.5 | 9.29 |
| `purchased_transportation_ratio` | asset-light logistics | 3 / 3 | 35 | 8.33 |
| `purchased_transportation_ratio` | truckload/intermodal | 3 / 3 | 35 | 8.50 |
| `fleet_or_equipment_count` | truckload/intermodal | 3 / 3 | 9 | 8.00 |
| `freight_weight_per_shipment` | LTL carriers | 4 / 4 | 16 | 4.00 |
| `shipment_or_load_growth` | LTL carriers | 3 / 3 | 32 | 8.00 |
| `pricing_or_yield_growth` | LTL carriers | 4 / 3 | 17.5 | 6.93 |

## Accepted tanker metrics

The unchanged tanker gates require at least 8 of 11 issuers, median depth of at least four periods, and median history of at least three years.

| Metric | Accepted issuers / required | Median periods | Median history (years) |
|---|---:|---:|---:|
| `vessel_count` | 9 / 8 | 5 | 6.72 |
| `tce_day_rate` | 10 / 8 | 15 | 6.85 |
| `fleet_age` | 11 / 8 | 8 | 8.00 |
| `revenue_days` | 8 / 8 | 25 | 7.49 |

`fleet_capacity` is intentionally not accepted: only two issuers have semantically valid aggregate fleet-capacity series after individual-vessel DWT rows and filing years are excluded.

## Requested surface targets that remain below gate

- `average_length_of_haul`: LTL has breadth 3/3 but only 0.71 median history years; truckload/intermodal remains 2/3 because SNDR has no valid numeric series.
- `rail_network_velocity`: 3/4; NSC has no valid mph level in the cached disclosures.
- `revenue_per_shipment_or_load` for truckload/intermodal: 2/3; HUBG has qualitative direction but no accepted numeric level.
- `fleet_or_equipment_count`: rail is 2/4 and LTL is 2/4. ODFL and SAIA pass on tractor counts; ARCB and XPO do not provide a comparable accepted series in the selected sources.
- `rail_fuel_efficiency`: 2/4; NSC and UNP disclosures found in this batch were changes or qualitative statements, not comparable level values.
- `equipment_utilization` for truckload/intermodal: 0/3; the found disclosures use different constructs and units.
- `fuel_surcharge_revenue_ratio` for rail: 0/3; no explicit numerator/denominator pair was found for CSX, NSC, or UNP.
- `terminal_dwell_time`: 2/4. CP has 52 accepted periods; CNI and CSX have no accepted numeric level under the exact definition.

## Acceptance boundary

These results authorize freezing the passing metric contracts for feature-table reconstruction. They do not authorize calibration or production promotion. Metrics below gate remain diagnostic and must not be admitted by lowering thresholds or by accepting qualitative/non-comparable evidence.
