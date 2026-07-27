# Defense Dedicated Parser Implementation

## Objective

Improve defense specialized-financial-metric coverage with the shared
`dedicated_parser` engine while keeping defense semantics, review policy,
evidence, and production promotion independent from machinery.

The parser is shadow-only until its evidence is adjudicated and the resulting
feature changes pass PIT/OOS recalibration. A shadow run cannot alter the
production defense score or rank table.

## Architecture

Shared infrastructure:

- Filing work planning, run recovery, process isolation, storage, provenance,
  and golden-corpus validation live in `dedicated_parser/`.
- The financial metric contract shared by industrial subsectors lives in
  `industrials/core/financial_metric_contract.py`.
- Cached SEC document text extraction is currently reused from
  `industrials/machinery/disclosure_documents.py`; this helper is generic and
  does not contain machinery metric policy.

Defense-owned implementation:

- Semantic rules: `industrials/defense/dedicated_parser_adapter.py`
- Review decisions:
  `industrials/defense/review_policies/dedicated_parser_review_policy.csv`
- Golden expectations: `dedicated_parser/golden_corpus/defense_v1.json`
- Shadow runner:
  `industrials/defense/scripts/08d_run_defense_dedicated_parser_shadow.py`
- Promotion runner:
  `industrials/defense/scripts/08e_promote_defense_dedicated_parser.py`
- Full comparison:
  `industrials/defense/scripts/08f_compare_defense_specialized_metrics.py`

## Stage DP0: Contract and Baseline

1. Register the required source, derived, and proxy metric contract.
2. Read the latest PIT financial feature row for each selected identity.
3. Preserve explicit availability rows when they exist.
4. Fall back to the PIT financial feature value when an older family has not
   yet produced metric-availability rows.
5. Record the production defense rank-table hash before any parser work.

Acceptance gates:

- All requested metrics resolve to a known contract definition.
- Baseline values come from the correct PIT row at or before `asof_date`.
- The production rank-table hash is recorded and remains unchanged.

## Stage DP1: Full-Universe Planning and Cache Audit

The selector includes every defense identity whose membership began on or
before the requested date:

- 94 active identities
- 40 historical/delisted identities
- 134 total identities

Planning creates work even when an issuer has no cached filing metadata. This
prevents no-cache issuers from disappearing from the comparison.

Acceptance gates:

- Selected ticker count equals the active plus historical membership count.
- Every selected ticker has one assessment for every required metric.
- Cache gaps are explicit and are never treated as a negative metric result.

## Stage DP2: Filing Hydration

`--archive-selected --archive-bootstrap` hydrates only explicitly selected
tickers. It is not enabled by default in the defense daily refresh.

Normal shadow runs use bounded filing and document limits. Evidence-review
runs use `--exhaustive-hydration`, which changes the contract:

- Filing and document limits are both zero (unlimited).
- Every unresolved accession is written to an exact hydration-scope CSV.
- Hydration is cache-only and cannot purge or rewrite canonical financial
  facts.
- Same-CIK aliases and share classes run serially within one worker.
- A process-wide SEC throttle applies across all cache workers.
- The evidence-review builder rejects partial hydration manifests, parser
  limits, missing assessment pairs, failed work, and unvalidated hydration
  errors.

Acceptance gates:

- `--archive-selected` is rejected unless `--tickers` is supplied.
- Filing and document caps are configuration-controlled.
- Failed downloads remain resumable and visible in run metadata.
- Daily incremental refresh cannot accidentally trigger a full archive sweep.
- Exhaustive review requires zero remaining cache accessions or an explicit,
  sealed source-gap outcome after all configured hydration passes.
- A failed hydration row must have a matching successful supplemental
  validation row for the same ticker and CIK.

## Stage DP3: Defense Semantic Extraction

The first defense metric set is:

- `orders`
- `funded_backlog`
- `reported_backlog`
- `remaining_performance_obligation`
- `rpo_current`

Accepted evidence must be consolidated, period-specific, unit-normalized, and
consistent across duplicate observations. The adapter rejects or routes to
review:

- Segment and dimensional facts
- Contract ceiling, maximum-potential, pipeline, proposal, and option values
- LOI/MOU and other nonbinding awards
- Acquired-backlog fair value
- Change-in-backlog values presented as backlog levels
- Prose without a clear total/consolidated scope and reporting period

Acceptance gates:

- Consolidated SEC RPO is accepted.
- Dimensional and segment facts cannot overwrite consolidated values.
- Duration metrics require valid, nonoverlapping period boundaries.
- Conflicting accepted values are demoted from automatic production use.

## Stage DP4: Shadow Assessment

The parser writes evidence and metric assessments to isolated shadow tables.
It does not write to the canonical financial source during this stage.

Acceptance gates:

- Run completes with zero failed work items.
- Every ticker/metric pair has an assessment, including missing-source states.
- Recovery classes distinguish not found, ambiguous, incomplete, and missing
  documents.
- The production source ID remains
  `dedicated_parser_defense_production` and receives no shadow writes.

## Stage DP5: Exhaustive Before/After Comparison

The comparison must cover all tickers, not only recovered names. It emits a
complete Cartesian matrix:

`selected tickers x required metrics`

Each row includes membership status, cohort, baseline value/status, shadow
value/status, evidence counts, searched/cached filing counts, recovery class,
and whether the parser changes coverage.

Acceptance gates:

- Comparison row count equals ticker count times metric count.
- Missing ticker/metric pairs cause a hard failure.
- Active and historical counts reconcile to membership.
- Production rank-table SHA remains unchanged.

## Stage DP6: Human Review and Golden Corpus

Ambiguous and recovered candidates must be reviewed against the original SEC
filing. Decisions are attributed and timestamped in the defense review policy,
then converted to golden expectations.

Acceptance gates:

- Golden corpus is nonempty and passes against the reviewed run.
- Review-policy decisions include `reviewed_by` and `reviewed_at`.
- Known false-positive classes have explicit negative expectations.
- Rerunning the reviewed corpus produces identical accepted/rejected outcomes.

## Stage DP7: Production Promotion

Promotion is fail-closed. It requires:

- `dedicated_parser.production_enabled: true`
- Isolated defense production source ID
- Fully completed run with zero failed work
- Nonempty adjudicated defense golden corpus
- At least one enabled, attributed, timestamped defense review decision
- Minimum confidence, consolidated scope, valid units/dates, and no conflicts

After promotion, rebuild financial features and rerun the defense PIT/OOS
calibration and holdout gates. Do not preserve production eligibility merely
because coverage increased.

Acceptance gates:

- Promoted rows retain filing, document, concept, period, and review lineage.
- Financial feature differences reconcile to promoted evidence.
- Weekly PIT calibration and holdout tests pass the existing promotion policy.
- Daily dashboard snapshots are republished only after recalibration.
- Portfolio-layer Stage 1 and downstream dry runs pass.

## Daily Orchestrator

Normal defense daily refresh behavior is unchanged. Shadow extraction is
explicitly opt-in:

```powershell
python industrials/defense/scripts/16_run_defense_daily_refresh.py `
  --asof 2026-07-24 `
  --include-dedicated-parser-shadow
```

The option adds the parser and exhaustive comparison after financial
validation and before profile graduation.

## 2026-07-24 Historical v1.2 Baseline

Parser run ID: `47`

Adapter version: `defense_specialized_metrics_v1.2`

- Tickers assessed: 134
- Active tickers: 94
- Historical tickers: 40
- Metrics per ticker: 5
- Expected comparison rows: 670
- Actual comparison rows: 670
- Missing assessment pairs: 0
- Failed parser work items: 0
- Baseline covered pairs: 67
- Shadow covered pairs: 152
- Net shadow coverage change: +85
- Total filing work items: 8,838
- Newly executed filing work items: 8,838
- Reused completed filing work items: 0
- Documents for newly executed work items: 19,125
- Remaining cache accessions: 0
- Evidence candidates: 20,149
- Evidence-review rows, including no-evidence pairs: 20,403

Coverage change by metric:

- `reported_backlog`: 0 to 43
- `funded_backlog`: 0 to 15
- `remaining_performance_obligation`: 67 to 69
- `rpo_current`: 0 to 25
- `orders`: 0

The v1.2 expansion has two fail-closed recovery paths:

- Backlog disclosures and annual orders disclosures can match the current
  assessment only when they are the latest eligible filing, no more than 457
  days old, and satisfy filing-date and period-end PIT ordering. Orders
  additionally require a 300-to-400-day duration.
- SEC RPO timing-axis schedules can derive `rpo_current` from either a validated
  earliest 12-month timing bucket or an explicit percentage multiplied by a
  unique, same-document consolidated RPO total. Monetary units are mandatory
  for every accepted RPO amount.

Nine covered pairs use the audited metric-freshness fallback. Twenty-three of
the 25 covered `rpo_current` pairs use a timing-axis derivation. No accepted RPO
amount has a blank or noncurrency unit.

Recovery classes:

- `BASELINE_POLICY_CORRECTION`: 1
- `BASELINE_REPORTED_HISTORICAL_ONLY`: 1
- `BASELINE_REPORTED_UNCONFIRMED`: 2
- `CONFIRMED_REPORTED`: 63
- `DISCLOSURE_REJECTED_POLICY`: 9
- `FOUND_AMBIGUOUS`: 185
- `HISTORICAL_RECOVERY_ONLY`: 71
- `NOT_FOUND_IN_SEARCHED_DOCUMENTS`: 252
- `RECOVERED_REPORTED`: 86

Hydration started with 7,723 missing accessions and finished with zero. The
original acquisition report retained two same-CIK temporary-file races
(`HEI`, `MOG-A`). The scheduler was corrected to group aliases by CIK, then
`HEI`, `HEI-A`, `MOG-A`, and `MOG-B` passed a separate sealed replay. The
review summary records `PASS_WITH_SUPPLEMENTAL_VALIDATION`; the original
failures were not deleted or rewritten.

The initial exhaustive assessment exposed a separate shared-CIK reporting bug:
recovery statistics joined the document catalog on ticker even though catalog
identity is CIK plus accession and document hash. That made `HEI` and `MOG-A`
appear to have zero documents after their sibling share classes last updated
the shared catalog rows. Recovery now joins on work-ledger CIK and accession.
An assessment-only replay of run `44` changed the four affected
`funded_backlog`/`rpo_current` pairs from `SOURCE_DOCUMENT_MISSING` to
`NOT_FOUND_IN_SEARCHED_DOCUMENTS`:

- `HEI`: 70 filings and 140 documents
- `MOG-A`: 52 filings and 104 documents

That correction did not change coverage and required no filing download or
reparse. Run `47` includes the corrected shared-CIK behavior.

The review summary also reconciles the no-evidence counts explicitly:

- 252 ordinary `NOT_FOUND_IN_SEARCHED_DOCUMENTS` pairs
- 2 high-priority `BASELINE_REPORTED_UNCONFIRMED` RPO pairs (`ARTX`, `EGL`)
- 254 total no-evidence rows

The 82 legacy SGML header messages are captured as one known EdgarTools
provider-warning class. The run-level stderr file is empty and sealed with zero
unknown stderr lines.

Artifacts:

- `output/industrials/defense/dedicated_parser/2026-07-24/dedicated_parser_cache_hydration.json`
- `output/industrials/defense/dedicated_parser/2026-07-24/dedicated_parser_cache_hydration_sync.csv`
- `output/industrials/defense/dedicated_parser/2026-07-24/dedicated_parser_shared_cik_validation.csv`
- `output/industrials/defense/dedicated_parser/2026-07-24/defense_specialized_metrics_before_after.csv`
- `output/industrials/defense/dedicated_parser/2026-07-24/defense_specialized_metrics_before_after_summary.json`
- `output/industrials/defense/dedicated_parser/2026-07-24/defense_specialized_metric_evidence_review.csv`
- `output/industrials/defense/dedicated_parser/2026-07-24/defense_specialized_metric_evidence_review_summary.json`

The production rank table remained unchanged at SHA-256:

`28494c606d83d114b7b2d99b986bc360ce6db11cd4c663a37ce1c7ecbfd7cdd8`

Production promotion remains disabled pending complete human adjudication,
golden-corpus completion, and PIT/OOS recalibration. The evidence-review CSV is
the complete review population; no partial-run decisions were promoted into
the defense policy.

## 2026-07-24 Authoritative v1.3 / Schema 7 Result

Parser run ID: `52`

Adapter version: `defense_specialized_metrics_v1.3`

Parser schema version: `7`

- Tickers assessed: 134
- Active tickers: 94
- Historical tickers: 40
- Metrics per ticker: 5
- Expected and actual comparison rows: 670
- Missing assessment pairs: 0
- Completed work items: 16,764
- Failed work items: 0
- Source documents processed: 28,224
- Remaining cache accessions: 0
- Evidence rows: 24,063
- Evidence-review rows including no-evidence pairs: 24,300
- Baseline covered pairs: 67
- Shadow covered pairs: 156
- Net shadow coverage change: +89

Coverage by metric:

- `reported_backlog`: 0 to 47
- `funded_backlog`: 0 to 15
- `remaining_performance_obligation`: 67 to 69
- `rpo_current`: 0 to 25
- `orders`: 0

The v1.3 policy rejects acquisition-target, combined-basis, unaudited target,
and pro-forma values as issuer-consolidated metrics. Targeted review found and
removed two false positives:

- `LUNR reported_backlog`: $685 million belonged to acquisition target
  Lanteris, not Intuitive Machines.
- `HRLY funded_backlog`: $383 million was explicitly pro forma in transaction
  registration documents.

Schema 7 includes the immutable parser work key in persisted evidence identity.
Later adapter or review-policy evaluations therefore create separate evidence
rows and cannot mutate rows linked to an earlier run. A no-change resume
completed as run `54` with zero scheduled work and 16,764 linked completions.
Run `52` was then regenerated after the resume and remained byte-identical at
comparison SHA-256:

`3d5c0610a21da50e47d191d1b0cef63d4a86e160c4728a53a2a03bf114ee5214`

The governed pair-level adjudication queue contains one row for each of the 670
ticker/metric pairs. It compares run `52` with the last trustworthy sealed
pre-expansion artifact, run `47`:

- Manual-review pairs: 372
- New covered pairs: 5 (`BAH`, `CVU`, `KRMN`, `MRCY`, `CACI`)
- Removed covered pairs: 1 (`HRLY`)
- Populated review decisions: 0
- Populated selected evidence keys: 0

Every evidence row has a nonblank, unique `evidence_key`. The pair queue carries
representative evidence and a bounded candidate preview but deliberately leaves
decision, selected-evidence, reviewer, and timestamp fields blank.

Authoritative artifacts:

- `output/industrials/defense/dedicated_parser/2026-07-24/defense_specialized_metrics_before_after.csv`
- `output/industrials/defense/dedicated_parser/2026-07-24/defense_specialized_metrics_before_after_summary.json`
- `output/industrials/defense/dedicated_parser/2026-07-24/defense_specialized_metric_evidence_review.csv`
- `output/industrials/defense/dedicated_parser/2026-07-24/defense_specialized_metric_evidence_review_summary.json`
- `output/industrials/defense/dedicated_parser/2026-07-24/defense_specialized_metric_pair_adjudication_queue.csv`
- `output/industrials/defense/dedicated_parser/2026-07-24/defense_specialized_metric_pair_adjudication_summary.json`

The obsolete 13.7 MB evidence-level adjudication skeleton is no longer written
by the defense wrapper unless explicitly requested. Defense uses the smaller,
reviewable pair-level queue instead.

The production rank table remains unchanged at SHA-256:

`28494c606d83d114b7b2d99b986bc360ce6db11cd4c663a37ce1c7ecbfd7cdd8`

Promotion remains blocked until pair-level human adjudication is complete, the
review policy and golden corpus are sealed, and specialized-metric PIT/OOS
recalibration passes.
