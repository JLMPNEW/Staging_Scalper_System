# Dedicated SEC Parser

This package is the repository-level, sector-neutral SEC parsing layer. The
machinery subsector is the first pilot. Production machinery facts remain
authoritative; this parser writes only additive `sec_parser_*` shadow tables
until its promotion gates are approved.

## Storage Contract

The parser does not create a second filing archive. It reads:

- filing metadata from `fact_sec_filing`;
- normalized facts from `fact_sec_xbrl_fact_raw` and `fact_sec_xbrl_fact`;
- metric availability from `feature_financial_metric_availability`; and
- original documents from the configured existing `sec_archive_xbrl` cache.

The document catalog reuses a SHA-256 value when path, size, and modification
time are unchanged. Work is keyed by accession, document hashes, parser
release, adapter version, metric registry, and the SHA-256 of the sector
review-policy registry. Completed keys are skipped.

## Provider Roles

- EdgarTools `5.28.5` reads local full-submission SGML and attachment metadata.
- Arelle `2.42.1` reads local Inline XBRL contexts, units, extension concepts,
  explicit dimensions, and typed dimensions.
- Sector adapters extract and adjudicate sector-specific evidence.

Reviewed decisions live outside shared parser code. A sector registry supplies
an exact-match policy CSV keyed by model family, ticker, accession, source
document, metric, period, unit, and candidate value. Ambiguous or overlapping
rules fail the work item. Applied policy ID/version/reviewer provenance is
stored with the evidence. Enabled decisions automatically generate a sidecar
golden corpus; rejected decisions generate both a required rejection and a
prohibited-acceptance expectation.

The shared semantic HTML parser preserves headings, table rows, column
alignment, `colspan` structure, inferred headers, and source block indexes.
The Arelle provider matches concept names, labels, documentation, and related
presentation/calculation concepts. Sector adapters decide whether evidence is
consolidated, dimensional, non-operating, ambiguous, or structurally
inapplicable. Shared providers never make sector policy decisions.

PDFs use native text extraction first. Optional OCR requires PyMuPDF,
Pytesseract, Pillow, and a host Tesseract executable. If OCR is unavailable or
conversion fails, the affected observation is classified `PARSER_FAILURE`; it
is not mislabeled as issuer non-disclosure.

Neither provider is allowed to fetch a filing during the current cache-first
pilot. Network acquisition remains the responsibility of the existing SEC
synchronization stage. This is enforced in code: the Arelle web cache runs in
offline mode (unresolvable taxonomy references fail the parse instead of
downloading), and the EdgarTools local-data environment variables are set
unconditionally rather than with setdefault. The shared CLI has no default
adapter; `--adapter` is required so a bare invocation can never silently run
another sector's adapter.

## Parallel Runtime

An accession is one process-pool work item. Workers read immutable cache files
and return serializable results. Only the parent process writes SQLite, in
bounded transactions that roll back atomically on mid-batch failure. Attempt
bookkeeping is recorded with each persisted result, so `attempt_count` only
counts work that actually executed. A progress-based stall watchdog fails the
remaining items (and the run) if no worker completes anything within the
watchdog window, instead of hanging forever on a pathological document. This
avoids SQLite writer contention and makes one-worker and multi-worker output
deterministic.

Work keys hash only content-bearing filing identity (volatile planner metadata
does not force mass reparses). Evidence keys exclude review-mutable fields for
within-work observation deduplication, while persisted evidence keys also
include the immutable work key. Therefore an adapter or review-policy change
creates a new evaluated evidence row instead of mutating rows linked to an
earlier run. Schema version 7 deliberately reschedules older work once to
establish this run-immutability contract. Resume-skipped completed work is
linked into each new run so its recovery assessment sees prior evidence
instead of regressing those pairs.

The default machinery configuration uses four workers. Increase it only after
benchmarking memory use and OneDrive file hydration on the target machine.

The reviewed 13-accession machinery corpus produced the same 98 evidence keys
with one worker and four workers. On the pilot machine, the four-worker run
took 15.3 seconds and the one-worker run took 49.3 seconds.

## Recovery Classification

Every requested ticker/source-metric pair receives one recovery class. The
classes distinguish confirmed and recovered disclosures from structural N/A,
ambiguous evidence, policy rejection, parser failure, missing source
documents, partial source windows, and a completed search with no matching
disclosure. `SOURCE_DOCUMENT_INCOMPLETE` is used when any selected filing is
absent from the cache; those cells cannot be called `NOT_FOUND` conclusively.

The run writes:

```text
dedicated_parser_shadow_run.json
dedicated_parser_shadow_comparison.json
dedicated_parser_recovery_assessment.json
dedicated_parser_recovery_assessment.csv
dedicated_parser_review_queue.csv
dedicated_parser_extraction_funnel.json
dedicated_parser_extraction_funnel.csv
dedicated_parser_assessment_only_run.json
dedicated_parser_cache_gate.json
dedicated_parser_plan.json
```

Coverage after a policy correction is calculated from the predicted status.
A rejected baseline value therefore reduces corrected coverage instead of
remaining counted as reported.

The extraction funnel separates cache completeness, work status, normalized
fact providers, XBRL mapping, semantic tables, text derivations, prose, OCR,
candidate outcomes, and final recovery classes. This distinguishes source
gaps from parser, semantic, and policy bottlenecks.

## Machinery Shadow Run

Install the pinned providers in the pipeline environment:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe -m pip install -r dedicated_parser\requirements.txt
```

Plan without parsing:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\08d_run_machinery_dedicated_parser_shadow.py --asof 2026-07-22 --plan-only
```

Run the fast completeness audit and stop before work allocation when any
selected filing is absent:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\08d_run_machinery_dedicated_parser_shadow.py --asof 2026-07-22 --plan-only --require-complete-cache
```

Enabled providers are required at execution time. A normal parse now fails
before allocating a run if Arelle or EdgarTools is unavailable in the active
Python environment. `--disable-arelle` and `--disable-edgartools` remain
explicit reduced-capability modes; they cannot occur silently.

Hydrate only tickers with missing accessions through the existing machinery
SEC synchronizer, then re-audit without parsing:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\08d_run_machinery_dedicated_parser_shadow.py --asof 2026-07-22 --hydrate-missing-cache --hydration-only
```

`--hydrate-missing-cache` never passes `--force-archive`; valid cache content
is reused. Its manifest records affected tickers, the exact synchronizer
command, return code, and before/after completeness. Add
`--require-complete-cache` to a normal shadow run to prevent parsing when
hydration remains partial.

Build and execute a deterministic broad benchmark from the active tickers with
the most unresolved parser-supported source metrics:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe dedicated_parser\build_benchmark_cohort.py --db C:\Users\josel\Documents\STAGING\DB\industrials.sqlite --adapter industrials.machinery.dedicated_parser_adapter:extract_metric_evidence --asof 2026-07-22 --limit 50 --output-json output\industrials\machinery\dedicated_parser\2026-07-22\most_missing_50.json --output-csv output\industrials\machinery\dedicated_parser\2026-07-22\most_missing_50.csv
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\08d_run_machinery_dedicated_parser_shadow.py --asof 2026-07-22 --ticker-cohort output\industrials\machinery\dedicated_parser\2026-07-22\most_missing_50.csv --artifact-label benchmark_most_missing_50 --force --require-complete-cache
```

The JSON manifest freezes the ranking rule, selected metrics and tickers,
missing counts, adapter version, and selection SHA-256. `--artifact-label`
isolates benchmark artifacts from daily shadow output.

Rebuild classifications and funnel artifacts for an existing run without
parsing or allocating a new run:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\08d_run_machinery_dedicated_parser_shadow.py --reassess-run-id 12
```

Run the optional orchestrator stage:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\17_run_machinery_refresh_pipeline.py --asof 2026-07-22 --include-dedicated-parser-shadow --dedicated-parser-python C:\Users\josel\miniconda3\envs\scalper-staging\python.exe
```

The stage executes after financial availability and recoverability
classification. It does not participate in scoring or historical regeneration.

Validate a reviewed shadow run:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe dedicated_parser\validate_golden_corpus.py --db C:\Users\josel\Documents\STAGING\DB\industrials.sqlite --corpus dedicated_parser\golden_corpus\machinery_v1.json --corpus dedicated_parser\golden_corpus\machinery_policy_generated.json --run-id 12
```

## Production Promotion

The shared parser remains sector-neutral. Sector adapters register source
metrics, extraction policy, downstream requirements, and production mappings.
Machinery is the first production consumer.

Machinery production runs execute the parser and promoter immediately before
the Stage 4 financial build:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\17_run_machinery_refresh_pipeline.py --asof 2026-07-24 --dedicated-parser-python C:\Users\josel\miniconda3\envs\scalper-staging\python.exe
```

`dedicated_parser.production_enabled: true` activates the two-step flow:

1. `08d_dedicated_parser_shadow` creates immutable evidence and keeps
   ambiguous/rejected candidates outside financial facts.
2. `08e_dedicated_parser_production` synchronizes only accepted,
   consolidated, PIT-valid, conflict-free evidence at or above the configured
   confidence threshold into source `dedicated_parser_production`.

The promoter is idempotent by parser run and source ID. It also persists
reviewed suppressions. A structural-N/A override requires an enabled reviewed
policy; parser prose alone cannot change the scoring denominator.
`--skip-dedicated-parser-production` is the emergency rollback switch.

Validated machinery run 28 used parser release `0.4.1`, covered all 113 active
tickers and 4,403 cached accessions, linked 12,182 documents, and had zero
failed work items. Both reviewed corpora passed. Promotion 2 promoted 647
facts, blocked 5,112 nonqualifying candidates, retained eight conflicts for
review, and persisted 13 suppressions. Repeating promotion returned the same
promotion ID and counts.

Release `0.4.1` also makes reviewed decisions stable across conservative
extractor changes. If an exact reviewed candidate is no longer emitted, the
policy may materialize it only when the reviewed accession/document was part
of the parsed work and the metric was requested. Provenance records the
document SHA-256 and policy identity.

Current production release `0.4.6` remains the same shared parser package; the
release number is provenance metadata, not a separate parser implementation.
It adds accession-scoped conflict resolution, numeric XBRL value precedence,
dimensionless-total precedence, clause-bounded current-RPO parsing, and
deterministic rejection of timing buckets that are not a 12-month current-RPO
window. Explicit `--max-filings-per-ticker 0` and
`--max-documents-per-filing 0` now correctly mean unlimited, allowing genuine
full-history validation instead of silently falling back to configured limits.

Focused machinery run 34 processed all 85 locally cached GNRC filings and 123
documents with zero failures. It classified all 26 current-RPO timing
candidates as six accepted 12-month observations and 20 rejected non-12-month
observations. Both golden corpora passed. Promotion 6 published 32 accepted
facts, blocked 73 candidates, had zero conflicts, and persisted 20
cross-source suppressions so the shared SEC source cannot reintroduce the
rejected partial-year values.

## Promotion Gates

Promotion from `shadow` to a sector production source requires all of the
following:

1. The reviewed machinery golden corpus passes with no regressions.
2. Every shadow-only addition is accepted or rejected through analyst review.
3. Serial and parallel output fingerprints are identical.
4. Repeated runs schedule no completed work unless content or a released
   parser/adapter version changes.
5. The current machinery financial, scoring, dashboard, calibration, and
   portfolio-layer smoke tests pass.
6. Defense-scoped database rows remain unchanged.
7. Historical impact is calculated by filing acceptance date. Only affected
   ticker/date partitions may be regenerated without separate approval.

The current corpus is
`dedicated_parser/golden_corpus/machinery_v1.json`. It contains positive and
prohibited-row expectations. New parser defects must add a positive or
negative corpus expectation before they are fixed. Machinery review decisions
are versioned in
`industrials/machinery/review_policies/dedicated_parser_review_policy.csv`;
their generated expectations are written to
`dedicated_parser/golden_corpus/machinery_policy_generated.json`.

## Fifty-Ticker Benchmark

The frozen `2026-07-22` benchmark selects the 50 active machinery tickers with
the most unresolved applicable source metrics. It contains 155 unresolved
metric cells: 14 tickers have four missing source metrics, 27 have three, and
nine have two. The selection SHA-256 is
`41c2f53ec8829f6c7bf1e152a12d21776687b13e9260c43ddde9371781fbe4f5`.

The first cache audit found 27 missing accessions across `ATS`, `BLDP`, `KRNT`,
and `SHMD`. The existing machinery SEC synchronizer hydrated all 27 after the
archive and parser windows were aligned at 40 filings per ticker. The complete
benchmark then processed 1,915 accessions and 5,277 cached documents with zero
failed work items.

Run 13 produced 1,009 evidence rows and 250 exhaustive ticker/metric
assessments. A real-filing review found that VRT's `$107.6M` timing schedule was
noncurrent deferred revenue only, not an exhaustive total RPO schedule.
Adapter `machinery_specialized_metrics_v2.4` now requires a timing schedule to
include a valid current bucket before its sum can be accepted as total RPO.
Targeted real-cache runs 15 and 16 verified the correction for `VRT`, `TTC`,
`AMSC`, and `BLBD`.

After that correction, current coverage across the 200 applicable
non-funded-backlog cells moves from 38/200 to 39/200. The sole new current
recovery is TTC total RPO. Five additional metric chains have historical-only
evidence. The corrected classifications are 29 confirmed reported, one
recovered current, five historical-only recoveries, three baseline-reported
historical-only, 19 ambiguous, six baseline-reported but unconfirmed, 137 not
found in the searched source window, and 50 structural N/A. The automatic
current recovery rate is therefore 1/155, or 0.65%, of initially unresolved
cells.

This result supports the parser as an accuracy, classification, provenance,
and targeted-recovery utility. It does not justify broad automatic expansion
to other sectors as a coverage engine. Further machinery work should
adjudicate the 25 ambiguous or baseline-unconfirmed cells and add exact review
policies/golden expectations; another unchanged full-history parse is not
warranted.

## Fifty-Ticker Priority Review

The 25-cell bounded review is complete. Adapter
`machinery_specialized_metrics_v2.8` (the 2026-07-20 audit release) fixes
split multi-row header date parsing (no more fabricated December-31 period
ends), clause-bounds the current-RPO percentage derivation, extends the
horizon-column guard to every metric and to "12 months" phrasing (RPO horizon
columns now become `rpo_current` candidates), deduplicates timing-dimension
buckets with an ambiguity bail-out, requires ~12-month spacing before the
earliest timing bucket is accepted as current RPO, adds a word boundary after
table money scale letters, repairs the dead block-total cross-check, and
surfaces unreadable/no-native-text documents as `PARSER_FAILURE` instead of
silent non-disclosure. Its predecessor `machinery_specialized_metrics_v2.7`
added multi-row/`colspan` table headers,
explicit period and value overrides, canonical-observation deduplication,
stale-anchor advancement, noncommercial order rejection, acquisition-backlog
rejection, explicit 12-month guards for current RPO, and reviewed exhaustive
dimension aggregation. The policy registry contains 41 decisions and
generates 54 positive or prohibited golden expectations.

Runs 18 and 19 processed only the 27 affected accessions and 168 documents,
with zero failures. They did not repeat the 1,915-accession historical
benchmark. All 25 priority cells are now classified: five current recoveries,
four confirmations or period corrections, three historical-only recoveries,
11 rejected disclosures, and two invalid production baselines.

Accuracy-adjusted current coverage is 42/200 (21.0%), up from 38/200 (19.0%).
The bridge is six valid current recoveries (`BE`, `HUBB`, `LNN`, `SXI`, `TTC`,
and `XOS`) less two invalid baseline observations (`AEBI` reported backlog and
`ASTE` orders). By metric, orders move from 4/50 to 3/50, total RPO from 17/50
to 22/50, reported backlog remains 11/50 after one recovery and one removal,
and current RPO remains 6/50.

The detailed result is stored in
`output/industrials/machinery/dedicated_parser/2026-07-22/benchmark_most_missing_50/benchmark_review_result.json`.
These results remain shadow-only. Production promotion requires a controlled
write path, removal of the two invalid baselines, affected-partition rebuilds,
and the full machinery-to-portfolio smoke gate.

## Full Active Machinery Validation

The schema-v3 benchmark was expanded from the 50-ticker stress cohort to all
114 machinery tickers active on 2026-07-22. Use `--all-metrics` for this
exhaustive benchmark scope. It evaluates every selected source metric while
still resuming completed filing work; it is intentionally different from
`--force`, which reparses unchanged filings.

Runs 20 and 21 parsed 4,442 accessions and 12,056 cached document references
with zero failed work items. A targeted 44-accession repair then made legacy
review policies match either their original parser period or their reviewed
effective period. The first full reconciliation exposed an ATS currency
mismatch: `dim_company` said USD while point-in-time financial statements said
CAD. The shared catalog now prefers financial-statement reporting currency and
includes non-USD currency in the work identity. Final run 24 reparsed only the
76 affected ATS/SHMD accessions, reused the other 4,366 completed accessions,
and produced exactly 570 assessments (114 tickers times five source metrics),
with no cache gaps or parser failures.

Accuracy-adjusted coverage across the 373 applicable non-funded-backlog cells
is 153/373 (41.0%), versus 138/373 (37.0%) in the production baseline. The
metric bridge is:

| Source metric | Baseline | Parser shadow |
|---|---:|---:|
| Orders | 16/95 | 17/95 |
| Total RPO | 49/91 | 58/91 |
| Reported backlog | 38/96 | 40/96 |
| Current RPO | 35/91 | 38/91 |

Funded backlog has no applicable machinery tickers under the current reviewed
policy, so its 0/0 result is structural N/A rather than missing coverage. All
74 reviewed golden expectations across 17 problematic issuers pass for run
24. This validates the reviewed traps, not every unreviewed candidate in the
universe: 11 ticker/metric cells remain ambiguous and 30 production-reported
cells remain unconfirmed by the dedicated parser.

The complete run-24 artifacts are stored in
`output/industrials/machinery/dedicated_parser/2026-07-22/full_active_v31_accuracy_validated/`.
