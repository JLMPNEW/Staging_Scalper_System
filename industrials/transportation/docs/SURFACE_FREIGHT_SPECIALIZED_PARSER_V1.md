# Surface-freight specialized parser v1

## Outcome

This batch adds a bounded, point-in-time parser lane for the 19-name
`surface_freight_core` cohort. It expands discovery for 23 directly parsed
specialized metrics and one downstream derived metric without changing the
independent `dedicated_parser` repository.

The implementation is fail-closed. Parser findings are run-scoped shadow
evidence or review candidates until their definitions, issuer scope, period,
unit, and source lineage have passed semantic review. Nothing in this batch
authorizes calibration or production promotion.

## Cohort and source contract

The issuer filing profiles are frozen in
`transportation_surface_filing_profiles_v1.csv`. They cover:

- Canadian 40-F issuers: CNI, CP, TFII.
- U.S. 10-K issuers: CSX, NSC, UNP, ARCB, ODFL, SAIA, XPO, HUBG, JBHT, KNX,
  SNDR, CHRW, EXPD, FDX, LSTR, UPS.

The metric-specific source, section, alias, table-title, applicability, and
source-posture rules are frozen in
`transportation_surface_metric_source_map_v1.csv`. The extraction cascade is:

1. Audited iXBRL operands from the already-loaded SEC fact store.
2. Section-aware tables in 10-K Item 7/Item 2, 40-F/20-F Item 5/Item 4/Item 18,
   and issuer-filed earnings supplements.
3. Event disclosures in 8-K Items 2.02, 7.01, and 8.01.
4. Regulatory/voluntary operational material for rail velocity, terminal
   dwell, service reliability, driver turnover, and empty miles.

The parser never treats an XBRL expense operand as a completed ratio. Ratio
operands and priority fallbacks are frozen in
`transportation_surface_xbrl_operand_map_v1.csv`. Numerators and denominators
must share the issuer, accession, fiscal period, currency, consolidation scope,
and—when both facts originate in one filing—the XBRL context and source
document.

## Implementation

`surface_metric_parser.py` provides:

- metric-specific table-label matching and units;
- preferred-section and expected-table validation;
- percentage, count, distance, weight, duration, currency, and rate parsing;
- year-over-year growth derivation only when comparable columns are present;
- peer, pro-forma, and non-issuer rejection;
- long-narrative-cell suppression to prevent prose numbers from becoming table
  facts;
- exact-context XBRL ratio pairing;
- complete source, document-hash, section, period, and formula provenance.

`dedicated_parser_adapter.py` wires these functions into the shared parser
contract. The adapter requests both local XBRL concept names and namespace-
qualified qnames, supports 40-F, and preserves the prior tanker and historical
transportation scopes. The shared `dedicated_parser` code is not modified.

The event-source terms in `source_census.py` and `source_exhaustion.py` now
include traffic and operating statistics, weekly railroad performance, network
velocity, terminal dwell, service reliability, sustainability/ESG, driver
turnover, and empty-mile disclosures.

## Execution sequence

The sequence is intentionally parse-once:

1. `36j_build_transportation_surface_delta_census.py` freezes the exact
   19-ticker/23-metric/accession/document contract.
2. `36l_hydrate_transportation_surface_delta_documents.py` downloads only
   exact cache gaps.
3. Rerun 36j and require a zero-gap census.
4. `36k_run_transportation_surface_delta_parser.py` plans and executes one
   bounded shared-parser run.
5. `36m_audit_transportation_surface_parser_coverage.py` audits the immutable
   source run even after the adapter advances, recording both source-run and
   current-audit versions.
6. `36o_build_transportation_surface_ratios_from_fact_store.py` derives ratios
   from already-loaded facts and cross-joins issuer-extension operands by exact
   accession and fiscal period. It reparses zero documents.
7. `36p_build_transportation_surface_semantic_review_queue.py` collapses
   repeated periods into one row per semantic definition. An accepted
   definition can then be replayed across its represented periods.

`36n_replay_transportation_surface_xbrl_derivations.py` remains available for
future runs whose normalized-fact request set contains all operands. It must not
be used to fabricate operands absent from an immutable source run.

## Executed 2026-07-30 batch

- Census: 19 tickers, 23 direct metrics, 1,594 accessions, 1,818 documents.
- Cache recovery: 14 exact gaps hydrated; final cache coverage 1,818/1,818.
- Shared parser run: run 105; 1,594/1,594 work items completed; zero failures.
- Run evidence: 20,967 rows: 92 accepted, 12,120 review-required, and 8,755
  policy-rejected.
- Already-loaded fact recovery: 4,244 ratio candidates and zero document
  reparses.
  - `operating_ratio`: 3,652 rows, 17 issuers, median 66 periods and 17.5 years.
  - `purchased_transportation_ratio`: 592 rows, 8 issuers, median 31 periods
    and 7.6 years.
- Domain-scoped semantic queue: 13,313 applicable numeric candidates collapsed
  to 270 definition-level reviews: 119 high, 2 medium, and 149 low priority.
  Of these, 228 can affect a calibration-candidate domain and 42 are
  diagnostic-only.

All fact-store candidates remain `REVIEW_REQUIRED`. The batch did not mutate
canonical candidates, historical feature tables, calibration artifacts, or
production state.

## Comparison-domain breadth policy

The original 15-of-19 gate is retained only for a genuinely cohort-wide
metric. It no longer constrains rail-, LTL-, truckload/intermodal-,
asset-light-, or parcel-specific metrics.

`transportation_surface_metric_comparison_domains_v1.csv` freezes five
outcome-blind comparison domains:

- rail networks: CNI, CP, CSX, NSC, UNP;
- LTL carriers: ARCB, ODFL, SAIA, TFII, XPO;
- truckload/intermodal: HUBG, JBHT, KNX, SNDR;
- asset-light logistics: CHRW, EXPD, HUBG, LSTR; and
- integrated parcel: FDX, UPS.

Metric-domain applicability may overlap because an issuer can have comparable
economics in more than one operating activity. HUBG is the explicit example.
Portfolio membership remains unique, so overlapping comparison domains never
duplicate a position.

For every metric-domain rule, required breadth is derived rather than chosen
after observing coverage:

`max(3, ceiling(75% * applicable metric-domain issuers))`

Sets with fewer than three applicable issuers are diagnostic-only. Passing
metrics are normalized within their comparison domain, while component weights
remain pooled across `surface_freight_core`; independent models may not be fit
to three-to-five-name domains.

The 2026-07-30 domain audit produced 55 direct metric-domain rules: 36
calibration candidates and 19 diagnostic rules. One accepted domain already
meets breadth and history (`operating_ratio::rail_networks`), and 25 additional
domains have enough discovered evidence to meet their unchanged gates if their
semantic definitions validate.

## Acceptance gates

The next gate is a single semantic-validation pass over the 270 definition
rows, starting with the 119 high-priority rows. A definition may be replayed
only when:

- the source describes the reporting issuer, not a peer or pro-forma entity;
- the value has the expected unit and economically plausible bounds;
- the fiscal period and filing acceptance date are point-in-time safe;
- ratio numerator and denominator definitions are compatible;
- broad concepts such as contracted services are confirmed in the note; and
- the source path or fact lineage is reproducible.

After replay, rerun the metric-specific breadth/depth audit exactly once. Only
metrics passing unchanged gates may enter a frozen historical feature rebuild.
Calibration and promotion remain downstream of that rebuilt panel and are not
implied by parser coverage.
