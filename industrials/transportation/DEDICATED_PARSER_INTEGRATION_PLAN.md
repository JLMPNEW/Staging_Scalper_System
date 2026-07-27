# Transportation Dedicated Parser Integration Plan

Status: implementation plan; no parser run, feature rebuild, recalibration, or
production promotion is authorized by this document.

Plan baseline date: 2026-07-26

## 1. Objective

Integrate the independent, sector-neutral `dedicated_parser` package with the
transportation subsector while preserving the same separation of
responsibilities used by defense and machinery.

The integration must produce one comprehensive historical evidence census for
all frozen transportation specialized metrics and all applicable active and
inactive tickers. Review and adjudication may iterate after that census, but
those iterations must not reopen or reparse filing documents. Historical
specialized features are materialized once after the evidence decisions are
sealed, and walk-forward calibration is run once against the resulting frozen
panel.

The controlling efficiency invariant is:

> Freeze metrics and source scope, hydrate once, parse each unique filing
> document once, adjudicate persisted evidence without reparsing, materialize
> the final specialized feature history once, and calibrate once.

## 2. Current State and Reusable Baseline

Transportation already has:

- 112 active and 48 inactive/delisted identities.
- Historical SEC submissions and CompanyFacts for all 160 identities.
- A cache-first transportation prose parser,
  `transportation_sec_filing_prose_v2`.
- 3,019 of 3,019 eligible historical periodic filings checkpointed as parsed.
- 4,450 source-linked specialized candidates:
  1,130 accepted and 3,320 review-required.
- A 39-metric registry containing 21 specialized metrics.
- A frozen 92-date point-in-time panel from 2019-01-02 through 2026-07-22.
- 9,496 exact historical membership observations and 370,344 metric
  availability observations.
- Zero future-data errors, including the conservative SEC filing-date rule for
  after-market-close submissions.
- A working shadow connection to the generic `industrial_family`
  portfolio-layer adapter.

The existing historical parse is the legacy baseline and review seed. It is
not discarded or overwritten. The dedicated parser produces an isolated
candidate version that is compared with it.

## 3. Findings from Defense and Machinery

The common integration pattern is:

1. A subsector adapter registers source metrics, concept patterns, downstream
   dependencies, document keywords, production mappings, and semantic rules.
2. The shared parser performs cache-first filing planning, document
   cataloging, provider normalization, parallel execution, immutable evidence
   storage, recovery assessment, and run bookkeeping.
3. A subsector wrapper performs plan-only and cache-completeness gates before
   execution.
4. Shadow evidence is compared with the complete baseline population.
5. Ambiguous and changed pairs are reviewed through a governed queue.
6. Exact review decisions generate a versioned policy and positive/negative
   golden-corpus expectations.
7. Promotion is isolated by subsector source ID and is idempotent.
8. Historical impact is determined from the evidence availability date.
   Machinery rebuilds only affected partitions.
9. Scoring, calibration, and portfolio eligibility remain blocked until the
   parser and historical point-in-time gates pass.

Defense contributes the appropriate full-universe pattern:

- Select current and historical identities, not just current members.
- Require an exhaustive cache audit before the review run.
- Produce a complete ticker-by-metric comparison.
- Use a small pair-level adjudication queue rather than a very large raw
  evidence-editing file.
- Keep shadow extraction out of production scoring.

Machinery contributes the appropriate historical promotion pattern:

- Run a read-only historical impact preflight first.
- Identify exact affected ticker/date partitions from filing acceptance dates.
- Preserve unaffected partitions.
- Revalidate every resulting portfolio-adapter artifact.

## 4. Ownership Boundary

### Shared `dedicated_parser` responsibilities

The independent package continues to own:

- Filing and document planning.
- Existing-cache document cataloging and content hashes.
- Arelle and EdgarTools provider normalization.
- Semantic HTML/table structure.
- Process isolation, deterministic parallel execution, and parent-only SQLite
  writes.
- Immutable work, run, normalized-fact, and evidence ledgers.
- Recovery classes and extraction funnels.
- Exact review-policy validation and golden-corpus validation.
- Generic policy-only replay described in Section 8.

The package must not import `industrials.transportation` or contain
transportation metric names, cohort names, ticker lists, applicability rules,
or scoring policy.

### Transportation responsibilities

Transportation owns:

- Metric definitions and applicability by cohort and industry.
- The exact filing/accession scope used for the historical evidence census.
- Transportation semantic extraction and rejection rules.
- Unit, value-domain, period, scope, freshness, and conflict policies.
- Pair-level review and reviewer attribution.
- The reviewed production-candidate publisher for nonmonetary specialized
  metrics.
- Historical feature impact analysis and materialization.
- Scoring, walk-forward calibration, portfolio tests, and production
  promotion.

### Dependency direction

The allowed direction is:

```text
industrials/transportation
        |
        | imports public contracts and runtime functions
        v
dedicated_parser
```

There must be no reverse import.

## 5. Extended Metric Discovery Scope

The current v2 registry has 21 specialized metrics. It is the immutable
comparison baseline, not the final v3 discovery contract.

The proposed DP0 v3 discovery universe is defined in
`TRANSPORTATION_SPECIALIZED_METRIC_UNIVERSE.md`:

- 90 final specialized metrics.
- 77 direct dedicated-parser targets.
- 7 non-scoring dedicated-parser supporting operands required by derived
  formulas.
- 7 metrics derived only from accepted parser evidence.
- 6 metrics derived from already-loaded point-in-time financial facts.
- A Cartesian applicability manifest with `160 x 90 = 14,400` rows.

The 90 metrics are intentionally broader than the eventual calibration set.
All are searched or derived in one research pass. After evidence review and
historical coverage analysis, the calibration-eligible subset is frozen
without reopening or reparsing the filing corpus.

### Current v2 parser-targeted disclosure metrics: 17

| Cohort | Metrics |
| --- | --- |
| Surface freight and logistics | `transport_volume_growth`, `pricing_or_yield_growth`, `operating_ratio`, `asset_utilization`, `purchased_transportation_ratio` |
| Air transport and aviation services | `traffic_growth`, `capacity_growth`, `load_factor_or_utilization`, `passenger_or_lease_yield`, `fuel_or_maintenance_intensity` |
| Marine shipping and maritime | `fleet_capacity`, `tce_or_day_rate`, `charter_coverage`, `fleet_utilization`, `fleet_age` |
| Development-stage and speculative transport | `going_concern_flag`, `commercialization_progress` |

### Current v2 existing-fact or derived specialized metrics: 4

- `cash_runway_years`
- `capital_raise_dependence`
- `diluted_share_growth`
- `stock_compensation_to_revenue`

The dedicated parser must not search filing prose for the four metrics that are
already derived from loaded financial facts. They remain in the same final
specialized-metric acceptance matrix and the same single calibration panel.

### Required semantic decisions before the exhaustive run

The following current composite definitions must be resolved before the
registry is frozen:

- Separate airline passenger load factor from aircraft-lessor fleet
  utilization, or constrain `load_factor_or_utilization` by industry with
  explicit units.
- Separate passenger yield from lease-rate economics, or constrain
  `passenger_or_lease_yield` by industry and prohibit cross-unit ranking.
- Define `fuel_or_maintenance_intensity` separately for airline fuel intensity
  and maintenance/service economics, or narrow its applicability.
- Define comparable surface volume units by industry. Railroad ton-miles,
  trucking loads/shipments, and logistics transactions must not be silently
  pooled as interchangeable levels.
- Normalize marine `fleet_capacity` to a comparable unit within an explicit
  shipping segment, or use it only as a within-issuer change metric.
- Pin the time horizon and denominator for `charter_coverage`.
- Give `commercialization_progress` a deterministic, point-in-time numerical
  rubric with evidence anchors, or mark it diagnostic-only. A text hit with a
  null value cannot be admitted to calibration.

Every proposed v3 specialized metric or split metric must be accepted,
diagnostic-only, or excluded under the metric-universe contract. Adding a new
metric that requires new filing evidence after the exhaustive parse invalidates
the one-pass contract.

DP0 freezes a discovery registry and scope manifest for:

```text
160 transportation identities x 90 discovery metrics = 14,400 rows
```

Each row records applicability, cohort, industry, source lane, allowed units,
value bounds, period type, freshness policy, and a reason when structurally
not applicable. After the one-pass evidence run, review, and coverage analysis,
the surviving calibration subset is frozen as
`transportation_metrics_v3_shadow`; removed metrics remain reproducible in the
research evidence and discovery panel.

## 6. Target Architecture

```text
existing SEC synchronization and sec_archive_xbrl cache
                       |
                       | exact sealed accession/document manifest
                       v
            dedicated_parser shared runtime
                       |
                       | immutable raw shadow evidence
                       v
       transportation semantic adapter and pair comparison
                       |
                       | reviewed policy-only evaluation
                       | (zero document reads and zero provider calls)
                       v
     transportation reviewed candidate source / impact preflight
                       |
                       | one specialized-only PIT materialization
                       v
          frozen transportation v3 historical panel
                       |
                       | one walk-forward calibration bundle
                       v
      explicit scoring and portfolio production promotion
```

The existing v2 frozen panel remains immutable. The candidate v3 panel is
written to a separate versioned output root until promotion.

## 7. Source-Scope and Retrieval Policy

The exhaustive parser must not discover its scope while parsing. A
transportation-owned source census must be sealed first.

### Base periodic scope

Reconcile the existing 3,019 eligible accessions from 2017-11-28 through
2026-07-22:

- `10-K`, `10-K/A`
- `10-Q`, `10-Q/A`
- `10-12B`, `10-12B/A`
- `20-F`, `20-F/A`
- `40-F`, `40-F/A`
- Financial-reporting `6-K` and `6-K/A` filings linked to loaded raw XBRL
  facts

### Supplemental scope

Before the run, make one explicit decision on:

- Earnings-result `8-K` exhibits that contain operating-statistics tables.
- Foreign-issuer results `6-K` exhibits not already selected by the raw-XBRL
  rule.
- Registration statements needed for IPO, relisting, spin-off, or
  development-stage history.
- PDFs whose native text or OCR is needed for a frozen metric.

Supplemental filings are selected from filing/index metadata and explicit
reviewed rules. The parser must not sweep every event filing.

### Exact source manifest

Create a sealed CSV/JSON manifest with:

- Ticker and canonical issuer/CIK.
- Accession, form, filing date, acceptance timestamp, and report date.
- Document name, kind, local path, size, and SHA-256.
- Primary/full-submission/exhibit flags.
- Selection rule and applicable metric families.
- Cache status.
- Explicit source-gap disposition when a document is unavailable.

The shared parser remains offline. Only the existing industrials SEC
synchronizer may hydrate missing files. Hydration is limited to the sealed
manifest and reuses valid cache files.

Same-CIK aliases and duplicate document hashes are identified during the
census. A document is provider-parsed once per unique content identity; any
valid evidence fan-out to multiple security identities must follow reviewed
security-continuity and issuer-identity rules.

## 8. Required Shared-Parser Enhancement: Policy-Only Replay

This enhancement is required before the transportation exhaustive run.

In parser release 0.4.6/schema 7, the review-policy SHA-256 participates in the
work key and policy application occurs during document parsing. Changing the
policy after a full run therefore schedules new work. That behavior conflicts
with the one-pass requirement.

Implement a sector-neutral review-evaluation layer in `dedicated_parser`:

1. Preserve adapter-emitted base evidence as immutable evidence.
2. Create a review-evaluation record keyed by base run ID, policy SHA-256,
   evaluation contract version, and evaluation timestamp.
3. Apply exact review policies to persisted evidence without opening source
   files, invoking Arelle, invoking EdgarTools, performing OCR, or rerunning
   the sector extractor.
4. Support reviewed materialization only when the accession/document was in
   the base run and the metric was requested.
5. Store evaluated evidence or an immutable overlay linked to both the base
   evidence key and the policy decision.
6. Allow comparison, recovery, golden validation, and promotion/publishing to
   select a review evaluation explicitly.
7. Preserve all prior runs and evaluations.

Add a distinct CLI mode such as `--policy-replay-run-id`; do not overload the
existing `--reassess-run-id`, which only rebuilds recovery artifacts.

Acceptance requires run metadata proving:

- `source_document_open_count = 0`
- `arelle_invocation_count = 0`
- `edgartools_invocation_count = 0`
- `ocr_invocation_count = 0`
- The base run and base evidence hashes are unchanged.

This is a generic parser capability. It must have no transportation imports or
metric-specific code and must remain backward compatible with defense and
machinery.

## 9. Transportation Adapter

Add:

`industrials/transportation/dedicated_parser_adapter.py`

The adapter exposes the same public functions used by the other subsectors:

- `select_tickers`
- `get_registry`
- `extract_metric_evidence`
- `map_normalized_facts`
- `postprocess_metric_evidence`

### Selector

`select_tickers` returns every transportation identity whose membership began
on or before the run as-of date. It must reconcile to 112 active plus 48
inactive/delisted identities for the current baseline.

### Registry

The adapter registry includes:

- Only the final frozen parser-targeted source metrics.
- Standard and extension concept patterns for US-GAAP and IFRS.
- Metric requirements and freshness limits.
- Supported forms matching the sealed source scope.
- Document keywords broad enough to select all frozen metrics.
- Transportation review-policy and generated-golden paths.

### Cohort and industry context

The adapter must use the reviewed transportation taxonomy, not infer a
business model from filing text or a vendor industry label. Each emitted
candidate is checked against the frozen ticker/metric applicability manifest.

### One-document/many-metric behavior

Each semantic document is constructed once. All applicable metric matchers run
over that shared representation. A metric loop must not re-read, re-tokenize,
or reparse the document.

### Evidence policy

Every candidate records:

- Ticker, CIK, accession, form, filing/acceptance/report dates.
- Document name and SHA-256.
- Metric, concept, value, unit, period start/end, and scope.
- Extraction method and adapter version.
- Confidence and deterministic status reason.
- Semantic block/table coordinates.
- Derivation operands when the value is calculated from disclosed components.

Automatic acceptance requires an explicit issuer scope, valid point-in-time
period, valid unit/domain, no conflicting accepted value for the same
observation, and metric-specific semantic rules. Segment, regional, market,
industry, transaction-target, pro-forma, and nonissuer observations are
rejected or routed to review.

## 10. Production-Candidate Publishing Contract

The shared monetary fact promoter used by defense and machinery cannot be used
unchanged for transportation. Its current production gate accepts monetary
currencies and writes canonical SEC financial facts, while transportation
specialized evidence includes ratios, booleans, counts, years, and
USD-per-day values.

Do not force these values into monetary `fact_sec_xbrl_fact` mappings.

Implement an idempotent transportation-owned publisher that:

- Reads only one sealed dedicated-parser base run and review evaluation.
- Publishes to the existing disclosure-candidate lane under isolated source
  `dedicated_parser_transportation_candidate`.
- Retains the dedicated-parser evidence key, base run ID, evaluation ID,
  policy ID, document hash, period, and reviewer lineage.
- Enforces the frozen unit and value-domain contract per metric.
- Blocks null values, invalid periods, cross-cohort metrics, future evidence,
  nonissuer scope, and conflicting accepted values.
- Treats dedicated-parser candidate evidence as authoritative for the frozen
  parser metrics. Legacy v2 candidates remain a comparison baseline and are
  not silently used as a fallback after a parser rejection.
- Is atomic and idempotent by evaluation ID and source ID.

This candidate source is research-only. It does not by itself set portfolio or
OOS-valid flags.

After successful calibration and explicit production promotion, the same
sealed rows may be aliased or copied to:

`dedicated_parser_transportation_production`

## 11. Implementation Sequence

### DP0 — Freeze the metric contract

1. Review the 90-metric discovery universe and the 21-metric v2 baseline.
2. Resolve the composite replacements in Section 5 and the metric-universe
   document.
3. Freeze all discovery metrics, parser-derived formulas, financial-derived
   formulas, and explicit exclusions before parsing.
4. Define units, bounds, period types, freshness, applicability, direction,
   operating archetype, and comparison population.
5. Generate and hash the 14,400-row scope manifest.
6. Record the current v2 panel, rank table, and portfolio-adapter hashes.

No source hydration or parser execution is allowed before DP0 passes.

DP0 implementation status: `PASS`. The versioned discovery registry,
13-archetype policy, 160-row reviewed archetype map, 14,400-row scope, and
baseline-hash manifest are materialized under `industrials/transportation/data`.
The contract also freezes seven supporting parser operands in a separate
1,120-row scope (83 applicable) so all derived formulas can be completed
without a later document search. The independent validator passes with
`parser_execution_authorized=false`.

### DP1 — Add generic policy-only replay

1. Add the sector-neutral review-evaluation schema and API.
2. Add the explicit CLI mode.
3. Add positive, rejection, override, materialization, idempotency, and
   zero-provider-invocation tests.
4. Run the existing dedicated-parser, defense, and machinery suites.

No transportation exhaustive parse is allowed before DP1 passes.

DP1 implementation status: `PASS`. New sector-neutral modules provide an
immutable review-evaluation overlay and a distinct
`--policy-replay-run-id` CLI without changing the existing assessment-only
`--reassess-run-id` behavior. Replay requires a pre-policy base run, limits
materialization to requested metrics and sealed documents, verifies the base
scope hash before and after evaluation and on idempotent reuse, and persists
zero source-document/Arelle/EdgarTools/OCR counters.

The overall exhaustive-parse readiness remains `NO_GO` until the bounded DP3
cache gaps are hydrated and DP4 passes. The machinery assertion has been
reconciled to the intentionally activated 0.05 strategic weight and its
portfolio-registration gate passes. The machine-readable DP1 audit is
`data/transportation_dp1_readiness_audit.json`.

### DP2 — Implement and fixture-test the transportation adapter

1. Reuse the existing transportation v2 semantic rules where they satisfy the
   frozen contract.
2. Move new rules into the transportation adapter, not the shared parser.
3. Add positive and prohibited fixtures for every parser metric and applicable
   cohort/industry.
4. Test prose, semantic tables, Inline XBRL, IFRS extensions, duplicate values,
   units, periods, scope, and after-close availability.
5. Use a small integration corpus only to validate real provider wiring. It
   must not trigger a feature rebuild or calibration.

DP2 offline status: `PASS`. The transportation-owned adapter registers all 77
final direct targets plus seven supporting operands, uses the sealed
ticker/metric applicability scopes, parses each document semantic structure
once, keeps broad discovery evidence in review status, has no production
mapping, and has positive and cross-archetype prohibited offline fixtures for
all 84 search metrics. Unit, period, issuer-scope, value-domain, conflicting
value, duplicate, and input-order determinism gates are explicit. Broad
evidence cannot become accepted without consolidated issuer scope. A separate
real-cache fixture parse is intentionally omitted: the first transportation
source-document/provider invocation will be the resumable exhaustive shadow
run after the cache and plan-only gates pass, preserving the one-pass policy.

### DP3 — Seal the exact source census

1. Generate the base and supplemental accession manifest.
2. Reconcile the original 3,019 periodic accessions.
3. Identify duplicate CIK/accession/document hashes.
4. Hydrate only manifest cache gaps through the existing SEC synchronizer.
5. Repeat the read-only cache audit until every row is cached or carries an
   approved source-gap disposition.
6. Seal the manifest hash and parser execution options.

DP3 census status: `NO_GO_CACHE_GAPS`. The read-only census reconciles all
3,019 base periodic accessions and adds a bounded 205-accession supplemental
scope: 53 result/event filings selected from positive SEC metadata and 152
registration, listing, relisting, or structural-break-window filings. It
contains 3,329 selected document rows, of which 3,177 are cached and byte-hash
verified. The exact unresolved hydration batch is 152 documents. An unbounded
7,206-filing non-XBRL 6-K sweep is explicitly excluded because those filings
have no positive results/release/presentation metadata signal. Parser
execution remains unauthorized until the 152 rows are cached or receive a
reviewed source-gap disposition and the census is resealed.

### DP4 — Plan-only full-universe gate

Run the transportation wrapper with:

- All 160 selected identities.
- The sealed accession manifest.
- Unlimited filing/document limits inside that manifest.
- All frozen parser metrics.
- Complete-cache required.
- Normalized providers enabled.
- No network access.

The plan is reviewed before any work is allocated.

### DP5 — Execute one exhaustive shadow parse

Run one immutable full-universe shadow census. Checkpoint and resume are
allowed after interruption; `--force` is not.

The base run is never repeated merely because:

- A review decision changes.
- A recovery report changes.
- Feature coverage is recalculated.
- A calibration configuration changes.

### DP6 — Compare and adjudicate without rebuilding features

1. Produce the complete applicable ticker-by-metric matrix.
2. Compare dedicated-parser evidence with legacy v2 candidates.
3. Produce evidence and pair-level review packages.
4. Review all recovered, ambiguous, conflicting, legacy-only, and
   policy-correction pairs.
5. Import decisions into the transportation review-policy registry.
6. Use policy-only replay until the review population is sealed.
7. Generate and pass the manual and policy-generated golden corpora.
8. Calculate projected coverage directly from reviewed evidence.
9. Assign a provisional disposition to every discovery metric:
   calibration candidate, archetype-only, longitudinal-only, diagnostic-only,
   or exclude.

Do not rebuild the 92-date feature panel and do not calibrate during DP6.

### DP7 — Publish the reviewed research candidate

1. Run the idempotent transportation candidate publisher.
2. Produce a current-date before/after metric report.
3. Verify that the existing production/shadow rank artifact is unchanged.
4. Seal the candidate-source manifest and hashes.

### DP8 — Historical impact preflight

Run a read-only preflight that maps each accepted/rejected decision to its
first valid filing-availability date and identifies affected ticker/date
partitions.

The preflight decides between:

- `GO_AFFECTED_SPECIALIZED_PARTITIONS_ONLY` when the metric registry shape is
  unchanged.
- `GO_ALL_SPECIALIZED_PARTITIONS_ONLY` when a metric was added, split, or
  removed.
- `NO_GO` when historical depth, provenance, or point-in-time gates fail.

Market prices, market features, generic financial facts, generic financial
features, reporting profiles, and membership history are not rebuilt.

### DP9 — Materialize and freeze the v3 panel once

1. Materialize all 90 discovery metrics and their explicit applicability
   states; do not materialize only the provisional calibration candidates.
2. Reuse the already-frozen market, financial, reporting-profile, and
   membership evidence.
3. Write the v3 panel to a new versioned output root.
4. Revalidate every date and generate final coverage, historical-depth,
   redundancy, and missingness reports from the materialized research panel.
5. Apply the predeclared coverage gates and freeze the calibration subset as a
   metric-selection manifest over the same panel.
6. Hash every artifact and freeze both the complete discovery panel and the
   calibration-subset manifest.

With the existing 18 generic metrics, the proposed discovery registry contains
108 total metrics. If the existing 9,496 historical memberships remain
unchanged, the exact row targets are:

```text
specialized discovery rows = 9,496 x 90 = 854,640
complete discovery rows    = 9,496 x 108 = 1,025,568
```

The calibration subset selects columns from the frozen complete panel and does
not trigger a second feature materialization. The row targets are recomputed
only if an independently approved membership or discovery-registry change
occurs.

### DP10 — Run calibration once

Only after DP9 passes:

1. Freeze train, validation, embargo, holdout, return, benchmark, transaction
   cost, cohort, metric-subset, and optimization contracts.
2. Build the calibration bundle from the selected columns of the single frozen
   v3 discovery panel.
3. Run the planned walk-forward calibration and net-of-cost validation once.
4. Seal weights, preprocessing, metric list, panel hashes, and results.

The existing month-end dates are observation/coverage dates. This parser plan
does not introduce a weekly rebalance rule.

If calibration fails, transportation remains shadow. A new metric/parser/panel
version and a separately approved rerun are required; the failed result is not
silently tuned and rerun.

### DP11 — Production and daily incremental operation

After explicit promotion:

- Enable only the sealed transportation production candidate source.
- Republish scoring and portfolio artifacts.
- Run the generic industrial-family portfolio adapter and downstream dry-run
  gates.
- In normal refreshes, plan only new or content-changed accessions.
- Parse every new document once for all frozen applicable metrics.
- Do not reopen the historical corpus when there are no changed inputs.

## 12. Planned Files

### Transportation-owned files

- `industrials/transportation/data/transportation_specialized_metric_discovery_registry.csv`
- `industrials/transportation/data/transportation_parser_supporting_metric_registry.csv`
- `industrials/transportation/data/transportation_operating_archetype_policy.yaml`
- `industrials/transportation/data/transportation_operating_archetypes.csv`
- `industrials/transportation/data/transportation_dp0_contract_manifest.json`
- `industrials/transportation/discovery_contract.py`
- `industrials/transportation/scripts/00b_build_transportation_dp0_contract.py`
- `industrials/transportation/scripts/00b_validate_transportation_dp0_contract.py`
- `industrials/transportation/dedicated_parser_adapter.py`
- `industrials/transportation/review_policies/dedicated_parser_review_policy.csv`
- `industrials/transportation/data/transportation_dedicated_parser_scope.csv`
- `industrials/transportation/data/transportation_dedicated_parser_support_scope.csv`
- `industrials/transportation/data/transportation_dp1_readiness_audit.json`
- `industrials/transportation/scripts/08f_run_transportation_dedicated_parser_shadow.py`
- `industrials/transportation/scripts/08g_compare_transportation_specialized_metrics.py`
- `industrials/transportation/scripts/08h_build_transportation_evidence_review_package.py`
- `industrials/transportation/scripts/08i_build_transportation_pair_adjudication_queue.py`
- `industrials/transportation/scripts/08j_import_transportation_adjudication.py`
- `industrials/transportation/scripts/08k_publish_transportation_parser_candidate.py`
- `industrials/transportation/scripts/19b_preflight_transportation_parser_impacts.py`
- `industrials/transportation/scripts/19c_materialize_transportation_parser_impacts.py`
- `tests/industrials/test_transportation_dedicated_parser_adapter.py`
- `tests/industrials/test_transportation_parser_promotion.py`

### Shared-parser files

Only sector-neutral policy-replay and manifest-input functionality may be
added under `dedicated_parser/`. The exact file names should follow the
package's current module layout. A transportation golden corpus follows the
existing defense/machinery convention:

- `dedicated_parser/review_replay.py`
- `dedicated_parser/policy_replay_cli.py`
- `dedicated_parser/golden_corpus/transportation_v1.json`
- `dedicated_parser/golden_corpus/transportation_policy_generated.json`

### Configuration

Add a family-scoped block under
`model_families.transportation.dedicated_parser` containing:

- Output root.
- Provider state directory.
- Candidate and production source IDs.
- Production-disabled default.
- Worker and PDF/OCR limits.
- Exact source-scope manifest path.
- Review-policy and golden-corpus paths.
- Minimum production confidence.
- Parser, adapter, registry, and scope versions.

Do not reuse the defense source
`dedicated_parser_defense_production` or machinery source
`dedicated_parser_production`.

## 13. Acceptance Gates

### G0 — Independence

- `dedicated_parser` has no import of any transportation module.
- Shared changes contain no transportation metric, cohort, ticker, or path.
- Existing dedicated-parser, defense, and machinery tests pass unchanged.
- Defense and machinery fixture fingerprints remain unchanged.

### G1 — Metric completeness

- The frozen 90-metric discovery registry and 14,400-row scope matrix have
  matching hashes.
- Every metric has an applicability, unit, value-domain, period, freshness,
  direction, and source-lane contract.
- Every metric has a predeclared coverage disposition rule; the final
  calibration-eligible, diagnostic-only, or excluded status is frozen after
  the one-pass coverage analysis.
- No unresolved composite definition or placeholder remains.

### G2 — Source completeness

- Active count is 112 and inactive/delisted count is 48.
- The base 3,019 periodic accessions reconcile exactly.
- Every supplemental accession has a documented selection rule.
- Every selected document is cached with a valid SHA-256 or has an approved,
  sealed source-gap disposition.
- The parser cannot use the network.

### G3 — Adapter quality

- Every parser metric has positive and prohibited fixture expectations.
- Cohort/industry applicability tests pass.
- Period, unit, scope, conflict, after-close, and future-data tests pass.
- One-worker and multiworker evidence fingerprints are identical.

### G4 — One-pass execution

- The sealed plan hash matches the executed plan hash.
- Every planned accession is completed or resume-linked exactly once.
- Failed work count is zero.
- Parser/provider invocation counts reconcile to unique planned content.
- No `--force` historical reparse occurs.
- Existing rank and portfolio artifact hashes remain unchanged.

### G5 — Complete comparison

- Every one of the 90 discovery metrics has a result for every one of the 160
  identities, with explicit applicability.
- Active and inactive counts reconcile to membership.
- Legacy-only, shadow-only, matched, policy-correction, ambiguous,
  parser-failure, missing-source, and not-found states are distinct.
- No missing comparison pair is permitted.

### G6 — Review and golden corpus

- Every required pair has an attributed, timestamped decision or an explicit
  `DEFER`.
- Deferred metrics/pairs are excluded from calibration eligibility.
- The manual and generated golden corpora are nonempty and pass.
- Known false-positive classes have prohibited-acceptance expectations.
- Policy-only replay performs zero document/provider/OCR operations.
- Repeating the same evaluation is byte-identical and idempotent.

### G7 — Candidate publishing

- Only reviewed, applicable, finite, period-valid, conflict-free evidence is
  published.
- Unit and value-domain rules pass for every published row.
- Publishing is atomic and idempotent.
- Dedicated-parser lineage is complete.
- Legacy fallback cannot reintroduce an explicitly rejected observation.
- Portfolio and OOS-valid flags remain false.

### G8 — Historical materialization

- A fresh read-only preflight authorizes the exact partitions.
- No market, generic financial, reporting-profile, or membership partition is
  rebuilt.
- The v2 frozen panel and hashes remain untouched.
- The v3 specialized discovery panel has 854,640 rows and the combined
  108-metric panel has 1,025,568 rows when the 9,496-member history is
  unchanged.
- The calibration-subset manifest selects from the complete panel without a
  second feature build.
- There are zero future-filing, after-close, future-membership, future-price,
  or future-FX errors.
- All v3 files and the final panel manifest are hash-frozen.

### G9 — Single calibration authorization

- G0 through G8 pass.
- The final panel hash equals the calibration input hash.
- All 90 discovery metrics have a final disposition, and the hashed
  calibration-subset manifest contains no diagnostic-only, deferred, or
  excluded metric.
- The calibration contract is frozen before execution.
- Only one final walk-forward calibration bundle is generated for the frozen
  v3 contract.
- Net-of-cost, holdout, turnover, concentration, capacity, cohort, and
  robustness gates pass before promotion.

### G10 — Portfolio and production

- The generic `industrial_family` adapter reads the promoted artifact.
- Exact ticker membership and selected-name counts reconcile.
- Portfolio Stage 1 and downstream dry runs pass.
- The production source ID is transportation-specific.
- Rollback disables the transportation production source without deleting
  shadow evidence, the v2 panel, review records, or calibration artifacts.

## 14. Stop Conditions

Stop before the exhaustive parse when:

- The final metric list or applicability is not frozen.
- The source manifest has unresolved cache gaps.
- Policy-only replay is unavailable.
- The adapter or provider fixture suite fails.

Stop before historical materialization when:

- Any mandatory review is incomplete.
- Golden validation fails.
- Published evidence contains conflicts or invalid units/periods.
- The historical impact preflight returns `NO_GO`.

Stop before calibration when:

- The v3 panel is not frozen.
- Any point-in-time or membership gate fails.
- Metric coverage is insufficient under the predeclared metric/cohort gate.

These stop conditions prevent the expensive sequence from becoming
parse-rebuild-calibrate-repeat.

## 15. Definition of Done

The integration is complete when:

1. The shared parser remains independent and its existing consumers pass.
2. All frozen transportation specialized metrics and all 160 identities are
   represented in the sealed applicability and evidence contracts.
3. The exhaustive source corpus was hydrated once and parsed once.
4. Review decisions were applied through policy-only replay.
5. The reviewed candidate source and v3 historical panel are reproducible and
   immutable.
6. Historical specialized features were materialized once after review was
   sealed.
7. One frozen walk-forward calibration bundle passed all research gates.
8. Production and portfolio eligibility were enabled only through an explicit
   promotion, with a tested rollback path.
